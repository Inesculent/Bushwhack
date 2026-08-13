from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence

from src.config import get_settings
from src.domain.schemas import ReviewFinding
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.orchestration.routing.finding_dedupe import ensure_unique_finding_ids
from src.orchestration.routing.review_obligations import recall_audit_for_final_findings

trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _merge_duplicate_maps(*maps: Any) -> Dict[str, List[str]]:
    merged: Dict[str, List[str]] = {}
    for raw_map in maps:
        if not isinstance(raw_map, dict):
            continue
        for raw_keeper, raw_ids in raw_map.items():
            keeper = str(raw_keeper)
            if not isinstance(raw_ids, list):
                continue
            for raw_id in raw_ids:
                dropped = str(raw_id)
                if dropped == keeper:
                    continue
                merged.setdefault(keeper, [])
                if dropped not in merged[keeper]:
                    merged[keeper].append(dropped)
    return merged


def _canonicalize_duplicate_map(
    duplicate_map: Dict[str, List[str]],
    final_ids: set[str],
) -> Dict[str, List[str]]:
    parent: Dict[str, str] = {}
    for keeper, dropped_ids in duplicate_map.items():
        for dropped in dropped_ids:
            if dropped != keeper:
                parent[dropped] = keeper

    def resolve(item: str) -> str:
        seen: set[str] = set()
        current = item
        while current in parent and current not in seen:
            seen.add(current)
            next_item = parent[current]
            if next_item in final_ids:
                return next_item
            current = next_item
        for candidate in seen:
            if candidate in final_ids:
                return candidate
        return current

    canonical: Dict[str, List[str]] = {}
    for dropped in sorted(parent):
        keeper = resolve(dropped)
        if keeper == dropped:
            continue
        canonical.setdefault(keeper, [])
        if dropped not in canonical[keeper]:
            canonical[keeper].append(dropped)
    return canonical


def _compact_reconciliation_card(finding: ReviewFinding) -> Dict[str, Any]:
    def short(value: str, limit: int) -> str:
        value = (value or "").strip()
        return value if len(value) <= limit else value[: limit - 1] + "…"

    return {
        "id": finding.id,
        "file_path": finding.file_path,
        "line_start": finding.line_start,
        "line_end": finding.line_end,
        "content": short(finding.content, 320),
        "expected_behavior": short(finding.expected_behavior, 220),
        "evidence_for_contract": short(finding.evidence_for_contract, 220),
        "counterexample": short(finding.counterexample, 220),
        "behavioral_symptom": finding.behavioral_symptom,
        "root_operation": finding.root_operation,
        "claim_digest": short(finding.claim_digest, 220),
    }


def _reconciliation_batches(findings: Sequence[ReviewFinding]) -> List[Dict[str, Any]]:
    """Bound AI context without using lexical semantics to choose comparison peers."""
    by_file: Dict[str, List[ReviewFinding]] = {}
    for finding in sorted(findings, key=lambda item: (item.file_path, item.line_start, item.id)):
        by_file.setdefault(finding.file_path, []).append(finding)

    batches: List[Dict[str, Any]] = []
    for file_findings in by_file.values():
        current: List[Dict[str, Any]] = []
        current_chars = 0
        for finding in file_findings:
            card = _compact_reconciliation_card(finding)
            card_chars = len(str(card))
            if current and (len(current) >= 16 or current_chars + card_chars > 18000):
                batches.append({"members": current})
                current = []
                current_chars = 0
            current.append(card)
            current_chars += card_chars
        if current:
            batches.append({"members": current})

    for index, batch in enumerate(batches, start=1):
        batch["cluster_id"] = f"cluster-{index}"
        batch["selection_reason"] = (
            "adjudicated findings from the same changed file; classify by behavioral claim"
        )
    return batches


def _claim_cluster_classification_errors(
    audit: Any,
    expected_by_cluster: Mapping[str, set[str]],
) -> List[str]:
    errors: List[str] = []
    decisions = {item.cluster_id: item for item in audit.clusters}
    unknown_clusters = sorted(set(decisions) - set(expected_by_cluster))
    if unknown_clusters:
        errors.append(f"unknown_clusters={unknown_clusters}")
    for cluster_id, expected_ids in expected_by_cluster.items():
        decision = decisions.get(cluster_id)
        if decision is None:
            errors.append(f"{cluster_id}:missing_decision")
            continue
        counts: Dict[str, int] = {}

        def record(candidate_id: str) -> None:
            counts[candidate_id] = counts.get(candidate_id, 0) + 1

        for group in decision.duplicate_groups:
            record(group.keeper_id)
            for candidate_id in group.absorbed_ids:
                record(candidate_id)
            for candidate_id in group.rejected_ids:
                record(candidate_id)
        for candidate_id in decision.distinct_ids:
            record(candidate_id)
        for candidate_id in decision.rejected_ids:
            record(candidate_id)

        missing = sorted(expected_ids - set(counts))
        unknown = sorted(set(counts) - expected_ids)
        repeated = sorted(candidate_id for candidate_id, count in counts.items() if count != 1)
        if missing:
            errors.append(f"{cluster_id}:missing_ids={missing}")
        if unknown:
            errors.append(f"{cluster_id}:unknown_ids={unknown}")
        if repeated:
            errors.append(f"{cluster_id}:multiply_classified={repeated}")
    return errors


def _apply_adjudicated_duplicate_audit(
    findings: Sequence[ReviewFinding],
    audit: Any,
) -> tuple[List[ReviewFinding], Dict[str, List[str]], Dict[str, str], List[str]]:
    by_id = {finding.id: finding for finding in findings}
    duplicate_to_keeper: Dict[str, str] = {}
    rejected_ids: List[str] = []
    for decision in audit.clusters:
        rejected_ids.extend(candidate_id for candidate_id in decision.rejected_ids if candidate_id in by_id)
        for group in decision.duplicate_groups:
            rejected_ids.extend(candidate_id for candidate_id in group.rejected_ids if candidate_id in by_id)
            for duplicate_id in group.absorbed_ids:
                if duplicate_id in by_id and group.keeper_id in by_id and duplicate_id != group.keeper_id:
                    duplicate_to_keeper[duplicate_id] = group.keeper_id
    duplicates: Dict[str, List[str]] = {}
    for duplicate_id, keeper_id in duplicate_to_keeper.items():
        duplicates.setdefault(keeper_id, []).append(duplicate_id)
    reconciled = [finding for finding in findings if finding.id not in duplicate_to_keeper]
    return reconciled, duplicates, duplicate_to_keeper, sorted(set(rejected_ids))


def _reconcile_adjudicated_finding_duplicates(
    state: GraphState,
    findings: List[ReviewFinding],
) -> tuple[List[ReviewFinding], Dict[str, List[str]], Dict[str, Any], int, List[Dict[str, Any]]]:
    metadata = state.get("metadata", {}) or {}
    if not isinstance(metadata.get("review_adjudicator"), dict) or len(findings) < 2:
        return findings, {}, {}, 0, []
    try:
        from src.orchestration.nodes.application.cleanup import (  # noqa: PLC0415
            SemanticClaimClusterOutput,
            _render_semantic_claim_cluster_prompt,
        )
    except Exception as exc:  # noqa: BLE001
        return findings, {}, {"warnings": [f"claim_cluster_import_failed:{exc.__class__.__name__}: {exc}"]}, 0, []

    clusters = _reconciliation_batches(findings)
    if not clusters:
        return findings, {}, {"cluster_count": 0}, 0, []

    resolved = get_settings()
    selected_model = getattr(resolved, "reviewer_worker_model_key", None)
    audits = []
    warnings: List[str] = []
    llm_tokens = 0
    llm_trace: List[Dict[str, Any]] = []
    valid_cluster_ids: set[str] = set()
    for cluster in clusters:
        cluster_id = str(cluster["cluster_id"])
        expected_ids = {str(member["id"]) for member in cluster["members"]}
        try:
            llm = Models.worker(
                SemanticClaimClusterOutput,
                model_key=selected_model,
                max_completion_tokens=2200,
            )
            traced = trace_llm_call(
                llm,
                _render_semantic_claim_cluster_prompt([cluster]),
                state=state,
                node_name="review_synthesizer_claim_cluster_reconciliation",
                model_key=selected_model,
                schema_name="SemanticClaimClusterOutput",
                input_summary={"cluster_id": cluster_id, "finding_count": len(expected_ids)},
            )
            llm_tokens += traced.tokens
            llm_trace.extend(traced.trace_records)
            batch_audit = parse_structured_output(traced.result, SemanticClaimClusterOutput)
            errors = _claim_cluster_classification_errors(
                batch_audit,
                {cluster_id: expected_ids},
            )
            if errors:
                warnings.extend(f"claim_cluster_invalid:{error}" for error in errors)
                continue
            audits.extend(batch_audit.clusters)
            warnings.extend(batch_audit.warnings)
            valid_cluster_ids.add(cluster_id)
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"claim_cluster_reconciliation_failed:{cluster_id}:{exc.__class__.__name__}: {exc}")
            llm_trace.extend(trace_from_exception(exc))

    audit = SemanticClaimClusterOutput(clusters=audits, warnings=warnings)
    reconciled, duplicates, duplicate_to_keeper, rejected_ids = _apply_adjudicated_duplicate_audit(
        findings,
        audit,
    )
    preserved_ids = sorted(set(rejected_ids))
    return ensure_unique_finding_ids(reconciled), duplicates, {
        "cluster_count": len(clusters),
        "valid_cluster_count": len(valid_cluster_ids),
        "claim_cluster_groups": clusters,
        "claim_cluster_audits": [item.model_dump(mode="json") for item in audit.clusters],
        "claim_cluster_warnings": warnings,
        "claim_cluster_duplicate_to_keeper": duplicate_to_keeper,
        "claim_cluster_rejected": {candidate_id: "preserved_after_adjudication" for candidate_id in rejected_ids},
        "claim_cluster_preserved_after_adjudication": preserved_ids,
    }, llm_tokens, llm_trace


def synthesizer_node(state: GraphState) -> Dict[str, Any]:
    findings = state.get("findings", []) or []
    llm_tokens = 0
    llm_trace: List[Dict[str, Any]] = []

    severity_rank = {"high": 0, "medium": 1, "low": 2}
    sorted_findings = sorted(
        findings,
        key=lambda item: (
            severity_rank.get(item.severity, 99),
            item.file_path,
            item.line_start,
            item.id,
        ),
    )
    final = ensure_unique_finding_ids(sorted_findings)

    reports = state.get("reviewer_worker_reports", []) or []
    reflection_reports = state.get("reflection_reports", []) or []
    candidates = state.get("candidate_findings", []) or []
    metadata = dict(state.get("metadata", {}))
    coverage_by_task = {}
    pipe = metadata.get("critique_pipeline") if isinstance(metadata.get("critique_pipeline"), dict) else {}
    by_task = pipe.get("by_task") if isinstance(pipe.get("by_task"), dict) else {}
    for task_id, slot in by_task.items():
        if isinstance(slot, dict) and isinstance(slot.get("coverage_evaluation"), dict):
            coverage_by_task[str(task_id)] = slot["coverage_evaluation"]
    cleanup_meta = metadata.get("adversarial_cleanup")
    adjudicator_meta = metadata.get("review_adjudicator")
    lifecycle = cleanup_meta.get("candidate_lifecycle", {}) if isinstance(cleanup_meta, dict) else {}
    adjudicator_lifecycle = (
        adjudicator_meta.get("candidate_lifecycle", {})
        if isinstance(adjudicator_meta, dict)
        else {}
    )
    if adjudicator_lifecycle:
        lifecycle = adjudicator_lifecycle
    cleanup_candidate_duplicates = (
        cleanup_meta.get("semantic_dedupe_duplicates", {}) if isinstance(cleanup_meta, dict) else {}
    )
    cleanup_finding_duplicates = (
        cleanup_meta.get("semantic_dedupe_finding_duplicates", {})
        if isinstance(cleanup_meta, dict)
        else {}
    )
    cleanup_claim_cluster_duplicates = (
        cleanup_meta.get("semantic_claim_cluster_duplicates", {})
        if isinstance(cleanup_meta, dict)
        else {}
    )
    adjudicator_merge_map = (
        adjudicator_meta.get("merge_map", {}) if isinstance(adjudicator_meta, dict) else {}
    )
    raw_combined_duplicate_map = _merge_duplicate_maps(
        cleanup_candidate_duplicates,
        cleanup_finding_duplicates,
        cleanup_claim_cluster_duplicates,
        adjudicator_merge_map,
    )
    final, synthesizer_claim_cluster_duplicates, synthesizer_claim_cluster_meta, dedupe_tokens, dedupe_trace = (
        _reconcile_adjudicated_finding_duplicates(state, final)
    )
    llm_tokens += dedupe_tokens
    llm_trace.extend(dedupe_trace)
    if synthesizer_claim_cluster_duplicates:
        raw_combined_duplicate_map = _merge_duplicate_maps(
            raw_combined_duplicate_map,
            synthesizer_claim_cluster_duplicates,
        )
    promoted_candidate_ids = {
        str(candidate_id)
        for candidate_id, entry in lifecycle.items()
        if isinstance(entry, dict) and entry.get("decision") == "promoted"
    }
    final_ids = {finding.id for finding in final}
    combined_duplicate_map = _canonicalize_duplicate_map(raw_combined_duplicate_map, final_ids)
    dropped_duplicate_ids = sorted(
        {
            dropped
            for dropped_ids in combined_duplicate_map.values()
            for dropped in dropped_ids
        }
    )
    equivalent_keeper = {
        str(dropped): str(keeper)
        for keeper, ids in combined_duplicate_map.items()
        for dropped in ids
        if str(dropped) != str(keeper)
    }
    lost_promoted_ids = sorted(
        cid
        for cid in promoted_candidate_ids
        if cid not in final_ids and equivalent_keeper.get(cid) not in final_ids
    )
    metadata["review_synthesizer"] = {
        "worker_count": len(reports),
        "reflection_report_count": len(reflection_reports),
        "raw_finding_count": len(findings),
        "final_finding_count": len(final),
        "dropped_duplicate_ids": dropped_duplicate_ids,
        "semantic_dedupe_duplicates": combined_duplicate_map,
        "dropped_resolution_only_ids": [],
        "lost_promoted_candidate_ids": lost_promoted_ids,
        "claim_cluster_duplicates": synthesizer_claim_cluster_duplicates,
        "claim_cluster_reconciliation": synthesizer_claim_cluster_meta,
        "recall_audit": recall_audit_for_final_findings(
            obligations_by_task=coverage_by_task,
            candidates=candidates,
            final_findings=final,
            duplicate_map=combined_duplicate_map,
        ),
        "worker_reports": [report.model_dump() for report in reports],
        "reflection_reports": [
            r.model_dump() if hasattr(r, "model_dump") else r for r in reflection_reports
        ],
    }

    if _trace_enabled(state):
        trace_logger.info(
            "TRACE synthesizer run_id=%s workers=%s reflections=%s raw_findings=%s final_findings=%s dropped_duplicates=%s",
            state.get("run_id", "unknown"),
            len(reports),
            len(reflection_reports),
            len(findings),
            len(final),
            dropped_duplicate_ids,
        )
        for finding in final:
            trace_logger.info(
                "TRACE final_finding run_id=%s id=%s severity=%s file=%s lines=%s-%s",
                state.get("run_id", "unknown"),
                finding.id,
                finding.severity,
                finding.file_path,
                finding.line_start,
                finding.line_end,
            )

    return {
        "final_findings": final,
        "metadata": metadata,
        "node_history": ["review_synthesizer"],
        "token_usage": llm_tokens,
        "llm_trace": llm_trace,
        "next_step": "finalize",
    }

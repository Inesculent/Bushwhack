"""LLM adjudication middleware for final review findings."""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import (
    CandidateFinding,
    CritiqueRevisionDigest,
    FocusedContextResult,
    ReflectionReport,
    ReviewAdjudicationItem,
    ReviewAdjudicationOutput,
    ReviewCheckResult,
    ReviewEvidenceTriageItem,
    ReviewFinding,
    SourceFact,
)
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.routing.finding_dedupe import (
    changed_files_from_diff,
    ensure_unique_finding_ids,
    resolve_repo_file_path,
)

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _coerce_candidate(raw: Any) -> CandidateFinding | None:
    if isinstance(raw, CandidateFinding):
        return raw
    if isinstance(raw, dict):
        try:
            return CandidateFinding.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_check_result(raw: Any) -> ReviewCheckResult | None:
    if isinstance(raw, ReviewCheckResult):
        return raw
    if isinstance(raw, dict):
        try:
            return ReviewCheckResult.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_reflection(raw: Any) -> ReflectionReport | None:
    if isinstance(raw, ReflectionReport):
        return raw
    if isinstance(raw, dict):
        try:
            return ReflectionReport.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_focused(raw: Any) -> FocusedContextResult | None:
    if isinstance(raw, FocusedContextResult):
        return raw
    if isinstance(raw, dict):
        try:
            return FocusedContextResult.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_verifier(raw: Any) -> VerifierReport | None:
    if isinstance(raw, VerifierReport):
        return raw
    if isinstance(raw, dict):
        try:
            return VerifierReport.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_source_fact(raw: Any) -> SourceFact | None:
    if isinstance(raw, SourceFact):
        return raw
    if isinstance(raw, dict):
        try:
            return SourceFact.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_digest(raw: Any) -> CritiqueRevisionDigest | None:
    if isinstance(raw, CritiqueRevisionDigest):
        return raw
    if isinstance(raw, dict):
        try:
            return CritiqueRevisionDigest.model_validate(raw)
        except Exception:
            return None
    return None


def _truncate(value: str, max_chars: int) -> str:
    text = value or ""
    if len(text) <= max_chars:
        return text
    return text[: max(0, max_chars - 18)] + "\n... [truncated]"


def _candidate_sort_key(candidate: CandidateFinding) -> tuple[str, int, str]:
    return (candidate.file_path, int(candidate.line_start or 0), candidate.candidate_id)


def _candidate_map(state: GraphState) -> Dict[str, CandidateFinding]:
    out: Dict[str, CandidateFinding] = {}
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is not None:
            out[cand.candidate_id] = cand
    return out


def _check_results_by_candidate(state: GraphState) -> Dict[str, List[ReviewCheckResult]]:
    grouped: Dict[str, List[ReviewCheckResult]] = {}
    for raw in state.get("review_check_results", []) or []:
        result = _coerce_check_result(raw)
        if result is None:
            continue
        if result.candidate is not None:
            grouped.setdefault(result.candidate.candidate_id, []).append(result)
    return grouped


def _reflections_by_candidate(state: GraphState) -> Dict[str, List[ReflectionReport]]:
    grouped: Dict[str, List[ReflectionReport]] = {}
    for raw in state.get("reflection_reports", []) or []:
        report = _coerce_reflection(raw)
        if report is not None:
            grouped.setdefault(report.candidate_id, []).append(report)
    return grouped


def _focused_by_candidate(state: GraphState) -> Dict[str, List[FocusedContextResult]]:
    grouped: Dict[str, List[FocusedContextResult]] = {}
    for raw in (state.get("focused_context_results", {}) or {}).values():
        result = _coerce_focused(raw)
        if result is not None:
            grouped.setdefault(result.candidate_id, []).append(result)
    return grouped


def _verifier_by_candidate(state: GraphState) -> Dict[str, List[VerifierReport]]:
    grouped: Dict[str, List[VerifierReport]] = {}
    for raw in state.get("verifier_reports", []) or []:
        report = _coerce_verifier(raw)
        if report is not None:
            grouped.setdefault(report.candidate_id, []).append(report)
    return grouped


def _source_facts_by_candidate(state: GraphState) -> Dict[str, List[SourceFact]]:
    grouped: Dict[str, List[SourceFact]] = {}
    for raw in state.get("source_facts", []) or []:
        fact = _coerce_source_fact(raw)
        if fact is not None:
            grouped.setdefault(fact.candidate_id, []).append(fact)
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    verifier = metadata.get("verifier") if isinstance(metadata, dict) else {}
    by_candidate = verifier.get("source_facts_by_candidate") if isinstance(verifier, dict) else {}
    if isinstance(by_candidate, dict):
        for cid, rows in by_candidate.items():
            if not isinstance(rows, list):
                continue
            for raw in rows:
                fact = _coerce_source_fact(raw)
                if fact is not None:
                    grouped.setdefault(str(cid), []).append(fact)
    return grouped


def _triage_by_candidate(state: GraphState) -> Dict[str, ReviewEvidenceTriageItem]:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    triage = metadata.get("review_evidence_triage") if isinstance(metadata, dict) else {}
    rows = triage.get("items") if isinstance(triage, dict) else []
    out: Dict[str, ReviewEvidenceTriageItem] = {}
    for raw in rows or []:
        try:
            item = raw if isinstance(raw, ReviewEvidenceTriageItem) else ReviewEvidenceTriageItem.model_validate(raw)
        except Exception:
            continue
        out[item.candidate_id] = item
    return out


def _digests_by_candidate(state: GraphState) -> Dict[str, List[CritiqueRevisionDigest]]:
    grouped: Dict[str, List[CritiqueRevisionDigest]] = {}
    for raw in (state.get("critique_revision_digests", {}) or {}).values():
        digest = _coerce_digest(raw)
        if digest is not None:
            grouped.setdefault(digest.candidate_id, []).append(digest)
    return grouped


def _revision_rows_by_candidate(metadata: Mapping[str, Any]) -> Dict[str, List[Mapping[str, Any]]]:
    block = metadata.get("critique_revision") if isinstance(metadata, Mapping) else {}
    revisions = block.get("revisions") if isinstance(block, Mapping) else []
    grouped: Dict[str, List[Mapping[str, Any]]] = {}
    for row in revisions or []:
        if not isinstance(row, Mapping):
            continue
        cid = str(row.get("candidate_id") or "")
        if cid:
            grouped.setdefault(cid, []).append(row)
    return grouped


def _prior_lifecycle_by_candidate(metadata: Mapping[str, Any]) -> Mapping[str, Any]:
    cleanup = metadata.get("adversarial_cleanup") if isinstance(metadata, Mapping) else {}
    lifecycle = cleanup.get("candidate_lifecycle") if isinstance(cleanup, Mapping) else {}
    return lifecycle if isinstance(lifecycle, Mapping) else {}


def _compact_focused_result(result: FocusedContextResult, *, max_chars: int) -> Dict[str, Any]:
    payload = result.model_dump(mode="json")
    for key in ("file_snippets", "file_contents_full"):
        mapping = payload.get(key)
        if not isinstance(mapping, dict):
            continue
        payload[key] = {
            str(path): _truncate(str(body or ""), max_chars)
            for path, body in mapping.items()
        }
    hits = payload.get("search_hits")
    if isinstance(hits, dict):
        compact_hits: Dict[str, Any] = {}
        for query, rows in hits.items():
            compact_hits[str(query)] = list(rows or [])[:5]
        payload["search_hits"] = compact_hits
    return payload


def _compact_verifier_report(report: VerifierReport) -> Dict[str, Any]:
    payload = report.model_dump(mode="json")
    attempts = payload.get("attempts") or []
    payload["attempts"] = [
        {
            "attempt_number": item.get("attempt_number"),
            "exit_code": item.get("exit_code"),
            "timeout": item.get("timeout"),
            "failure_class": item.get("failure_class"),
            "stdout": _truncate(str(item.get("stdout") or ""), 800),
            "stderr": _truncate(str(item.get("stderr") or ""), 800),
        }
        for item in attempts
        if isinstance(item, dict)
    ]
    return payload


def build_review_adjudication_packets(
    state: GraphState,
    *,
    max_focused_chars: int = 2400,
) -> List[Dict[str, Any]]:
    """Build one bounded evidence packet per candidate without semantic pruning."""
    metadata = dict(state.get("metadata", {}) or {})
    candidates = sorted(_candidate_map(state).values(), key=_candidate_sort_key)
    checks = _check_results_by_candidate(state)
    reflections = _reflections_by_candidate(state)
    focused = _focused_by_candidate(state)
    verifier = _verifier_by_candidate(state)
    source_facts = _source_facts_by_candidate(state)
    triage = _triage_by_candidate(state)
    digests = _digests_by_candidate(state)
    revisions = _revision_rows_by_candidate(metadata)
    lifecycle = _prior_lifecycle_by_candidate(metadata)
    verifier_hints = metadata.get("verifier_hints") if isinstance(metadata.get("verifier_hints"), dict) else {}

    packets: List[Dict[str, Any]] = []
    for candidate in candidates:
        cid = candidate.candidate_id
        packets.append(
            {
                "candidate": candidate.model_dump(mode="json"),
                "originating_checks": [
                    item.model_dump(mode="json", exclude={"candidate"})
                    for item in checks.get(cid, [])
                ],
                "reflection_reports": [
                    item.model_dump(mode="json")
                    for item in reflections.get(cid, [])
                ],
                "focused_context": [
                    _compact_focused_result(item, max_chars=max_focused_chars)
                    for item in focused.get(cid, [])
                ],
                "verifier_reports": [
                    _compact_verifier_report(item)
                    for item in verifier.get(cid, [])
                ],
                "source_facts": [
                    item.model_dump(mode="json")
                    for item in source_facts.get(cid, [])
                ],
                "triage": triage[cid].model_dump(mode="json") if cid in triage else None,
                "verifier_hint": verifier_hints.get(cid) if isinstance(verifier_hints, Mapping) else None,
                "critique_revision_digests": [
                    item.model_dump(mode="json")
                    for item in digests.get(cid, [])
                ],
                "critique_revision_rows": list(revisions.get(cid, [])),
                "prior_lifecycle_hint": lifecycle.get(cid) if isinstance(lifecycle, Mapping) else None,
            }
        )
    return packets


def _packet_json(packet: Mapping[str, Any], *, max_candidate_chars: int) -> str:
    raw = json.dumps(packet, indent=2, ensure_ascii=False)
    if len(raw) <= max_candidate_chars:
        return raw
    compact = dict(packet)
    candidate = compact.get("candidate")
    compact["candidate"] = candidate
    compact["focused_context"] = [
        {
            "request_id": row.get("request_id"),
            "candidate_id": row.get("candidate_id"),
            "warnings": row.get("warnings", []),
            "summary": "focused context omitted from this oversized packet; use other evidence fields",
        }
        for row in compact.get("focused_context", [])
        if isinstance(row, Mapping)
    ]
    raw = json.dumps(compact, indent=2, ensure_ascii=False)
    return _truncate(raw, max_candidate_chars)


def plan_adjudication_batches(
    packets: Sequence[Mapping[str, Any]],
    *,
    max_batch_chars: int,
    max_candidate_chars: int,
) -> List[List[Mapping[str, Any]]]:
    """Group packets into prompt-sized batches without dropping candidates."""
    batches: List[List[Mapping[str, Any]]] = []
    current: List[Mapping[str, Any]] = []
    current_size = 0
    limit = max(1, int(max_batch_chars))
    for packet in packets:
        piece = len(_packet_json(packet, max_candidate_chars=max_candidate_chars))
        if current and current_size + piece > limit:
            batches.append(current)
            current = []
            current_size = 0
        current.append(packet)
        current_size += piece
    if current:
        batches.append(current)
    return batches


def _candidate_ids_from_packets(packets: Iterable[Mapping[str, Any]]) -> List[str]:
    ids: List[str] = []
    for packet in packets:
        candidate = packet.get("candidate")
        if isinstance(candidate, Mapping):
            cid = str(candidate.get("candidate_id") or "")
            if cid:
                ids.append(cid)
    return ids


def _render_packets(packets: Sequence[Mapping[str, Any]], *, max_candidate_chars: int) -> str:
    sections: List[str] = []
    for packet in packets:
        cid = ""
        candidate = packet.get("candidate")
        if isinstance(candidate, Mapping):
            cid = str(candidate.get("candidate_id") or "")
        sections.append(
            f"### Candidate {cid or '(unknown)'}\n"
            f"{_packet_json(packet, max_candidate_chars=max_candidate_chars)}"
        )
    return "\n\n".join(sections)


def _render_adjudication_prompt(
    state: GraphState,
    packets: Sequence[Mapping[str, Any]],
    *,
    mode: str,
    max_candidate_chars: int,
) -> str:
    return render_reviewer_prompt(
        "review_adjudicator.md",
        {
            "Adjudication Mode": mode,
            "Candidate Evidence Packets": _render_packets(
                packets,
                max_candidate_chars=max_candidate_chars,
            ),
            "Git Diff Excerpt": _truncate(state.get("git_diff", "") or "", 8000),
        },
    )


def _render_reduce_prompt(
    state: GraphState,
    candidate_ids: Sequence[str],
    batch_outputs: Sequence[Mapping[str, Any]],
) -> str:
    payload = {
        "candidate_ids": list(candidate_ids),
        "batch_outputs": list(batch_outputs),
    }
    return render_reviewer_prompt(
        "review_adjudicator.md",
        {
            "Adjudication Mode": "reduce cross-batch adjudication results into one final decision per candidate",
            "Candidate Evidence Packets": json.dumps(payload, indent=2, ensure_ascii=False),
            "Git Diff Excerpt": _truncate(state.get("git_diff", "") or "", 8000),
        },
    )


def _invoke_adjudicator(
    *,
    state: GraphState,
    prompt: str,
    model_key: str | None,
    max_completion_tokens: int,
    request_label: str,
    input_summary: Mapping[str, Any],
) -> tuple[ReviewAdjudicationOutput | None, List[str], int, List[Dict[str, Any]]]:
    selected_model = model_key or get_settings().reviewer_worker_model_key
    warnings: List[str] = []
    try:
        llm = Models.worker(
            ReviewAdjudicationOutput,
            model_key=selected_model,
            max_completion_tokens=max_completion_tokens,
        )
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="review_adjudicator",
            model_key=selected_model,
            schema_name="ReviewAdjudicationOutput",
            request_label=request_label,
            input_summary=dict(input_summary),
        )
        response = parse_structured_output(traced.result, ReviewAdjudicationOutput)
        return response, warnings, traced.tokens, append_trace([], traced)
    except Exception as exc:  # noqa: BLE001
        warnings.append(f"review_adjudicator_llm_failed:{request_label}:{exc.__class__.__name__}: {exc}")
        logger.warning("review_adjudicator failed request=%s reason=%s", request_label, exc)
        return None, warnings, 0, trace_from_exception(exc)


def _feedback_type_for_candidate(candidate: CandidateFinding) -> str:
    if candidate.feedback_type != "other":
        return candidate.feedback_type
    if candidate.suspected_category in {"logic", "security"}:
        return "defect_detection"
    if candidate.suspected_category == "performance":
        return "optimization"
    return "other"


def _fallback_finding(candidate: CandidateFinding) -> ReviewFinding:
    return ReviewFinding(
        id=candidate.candidate_id,
        file_path=candidate.file_path,
        line_start=candidate.line_start,
        line_end=candidate.line_end,
        content=candidate.content,
        severity=candidate.severity,
        feedback_type=_feedback_type_for_candidate(candidate),  # type: ignore[arg-type]
        recommendation=candidate.recommendation,
        behavioral_symptom=candidate.behavioral_symptom,
        root_operation=candidate.root_operation,
        claim_digest=candidate.claim_digest,
        evidence_for_contract=candidate.evidence_for_contract,
        counterexample=candidate.counterexample,
        rejection_check=candidate.rejection_check,
    )


def _normalize_adjudication_items(
    *,
    output: ReviewAdjudicationOutput | None,
    candidates: Mapping[str, CandidateFinding],
    changed_files: set[str],
) -> tuple[List[ReviewFinding], Dict[str, Any], Dict[str, List[str]], List[str]]:
    warnings: List[str] = []
    lifecycle: Dict[str, Any] = {}
    merge_map: Dict[str, List[str]] = {}
    promoted: List[ReviewFinding] = []
    seen: set[str] = set()
    items = list(output.items) if output is not None else []
    candidate_ids = set(candidates.keys())

    for item in items:
        cid = item.candidate_id
        if cid not in candidate_ids:
            warnings.append(f"adjudication_unknown_candidate:{cid}")
            continue
        if cid in seen:
            warnings.append(f"adjudication_duplicate_candidate:{cid}")
            continue
        seen.add(cid)
        candidate = candidates[cid]

        if item.decision == "drop":
            lifecycle[cid] = {
                "decision": "dropped",
                "reason": "adjudicator_drop",
                "rationale": item.rationale,
                "evidence_refs": list(item.evidence_refs),
                "warnings": list(item.warnings),
            }
            continue

        if item.decision == "merge":
            keeper = (item.merge_into or "").strip()
            if keeper not in candidate_ids:
                warnings.append(f"adjudication_invalid_merge:{cid}->{keeper}")
                lifecycle[cid] = {
                    "decision": "dropped",
                    "reason": "invalid_merge_target",
                    "rationale": item.rationale,
                }
                continue
            merge_map.setdefault(keeper, [])
            if cid != keeper and cid not in merge_map[keeper]:
                merge_map[keeper].append(cid)
            lifecycle[cid] = {
                "decision": "merged",
                "reason": "adjudicator_merge",
                "equivalent_to": keeper,
                "rationale": item.rationale,
                "evidence_refs": list(item.evidence_refs),
                "warnings": list(item.warnings),
            }
            continue

        finding = item.finding or _fallback_finding(candidate)
        if changed_files and resolve_repo_file_path(finding.file_path, changed_files) is None:
            warnings.append(f"adjudication_promote_outside_changed_files:{cid}:{finding.file_path}")
            lifecycle[cid] = {
                "decision": "dropped",
                "reason": "promoted_path_not_changed",
                "rationale": item.rationale,
            }
            continue
        if finding.line_end < finding.line_start:
            warnings.append(f"adjudication_invalid_line_range:{cid}")
            lifecycle[cid] = {
                "decision": "dropped",
                "reason": "invalid_line_range",
                "rationale": item.rationale,
            }
            continue
        finding = finding.model_copy(update={"id": cid})
        promoted.append(finding)
        lifecycle[cid] = {
            "decision": "promoted",
            "reason": "adjudicator_promote",
            "finding_id": cid,
            "rationale": item.rationale,
            "evidence_refs": list(item.evidence_refs),
            "warnings": list(item.warnings),
        }

    for cid in sorted(candidate_ids - seen):
        lifecycle[cid] = {
            "decision": "dropped",
            "reason": "adjudicator_missing_decision",
            "rationale": "No adjudication item was returned for this candidate.",
        }
        warnings.append(f"adjudication_missing_candidate:{cid}")

    return ensure_unique_finding_ids(promoted), lifecycle, merge_map, warnings


def _completion_cap(settings: Settings) -> int:
    return int(getattr(settings, "reviewer_adjudicator_max_completion_tokens", 16384))


def make_review_adjudicator_node(
    model_key: str | None = None,
    use_llm: bool = True,
    settings: Settings | None = None,
):
    node_name = "review_adjudicator"

    def review_adjudicator_node(state: GraphState) -> Dict[str, Any]:
        resolved_settings = settings or get_settings()
        run_id = state.get("run_id", "unknown")
        candidates = _candidate_map(state)
        metadata = dict(state.get("metadata", {}) or {})
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        warnings: List[str] = []

        if not candidates:
            metadata[node_name] = {
                "candidate_count": 0,
                "promoted_count": 0,
                "warnings": [],
                "candidate_lifecycle": {},
                "merge_map": {},
            }
            return {
                "findings": [],
                "metadata": metadata,
                "node_history": [f"{node_name}:skipped"],
            }

        packets = build_review_adjudication_packets(
            state,
            max_focused_chars=int(resolved_settings.reviewer_adjudicator_focused_context_max_chars),
        )
        batches = plan_adjudication_batches(
            packets,
            max_batch_chars=int(resolved_settings.reviewer_adjudicator_max_batch_chars),
            max_candidate_chars=int(resolved_settings.reviewer_adjudicator_max_candidate_chars),
        )
        changed_files = changed_files_from_diff(state.get("git_diff", "") or "")
        outputs: List[ReviewAdjudicationOutput] = []
        batch_records: List[Dict[str, Any]] = []

        if use_llm:
            for index, batch in enumerate(batches):
                batch_ids = _candidate_ids_from_packets(batch)
                prompt = _render_adjudication_prompt(
                    state,
                    batch,
                    mode="decide one outcome for every candidate in this batch",
                    max_candidate_chars=int(resolved_settings.reviewer_adjudicator_max_candidate_chars),
                )
                output, batch_warnings, tokens, trace = _invoke_adjudicator(
                    state=state,
                    prompt=prompt,
                    model_key=model_key,
                    max_completion_tokens=_completion_cap(resolved_settings),
                    request_label=f"batch_{index + 1}_of_{len(batches)}",
                    input_summary={"candidate_ids": batch_ids, "batch_index": index},
                )
                warnings.extend(batch_warnings)
                llm_tokens += tokens
                llm_trace.extend(trace)
                record = {
                    "batch_index": index,
                    "candidate_ids": batch_ids,
                    "item_count": len(output.items) if output is not None else 0,
                    "warnings": batch_warnings + (output.warnings if output is not None else []),
                }
                batch_records.append(record)
                if output is not None:
                    outputs.append(output)

            if len(outputs) > 1:
                intermediate = [
                    output.model_dump(mode="json")
                    for output in outputs
                ]
                prompt = _render_reduce_prompt(
                    state,
                    sorted(candidates),
                    intermediate,
                )
                reduced, reduce_warnings, tokens, trace = _invoke_adjudicator(
                    state=state,
                    prompt=prompt,
                    model_key=model_key,
                    max_completion_tokens=_completion_cap(resolved_settings),
                    request_label="reduce",
                    input_summary={"candidate_ids": sorted(candidates), "batch_count": len(outputs)},
                )
                warnings.extend(reduce_warnings)
                llm_tokens += tokens
                llm_trace.extend(trace)
                if reduced is not None:
                    outputs = [reduced]
                else:
                    warnings.append("review_adjudicator_reduce_failed_using_batch_outputs")
        else:
            warnings.append("review_adjudicator_llm_disabled")

        if outputs:
            combined = ReviewAdjudicationOutput(
                items=[item for output in outputs for item in output.items],
                warnings=[warning for output in outputs for warning in output.warnings],
            )
            warnings.extend(combined.warnings)
        else:
            combined = None

        findings, lifecycle, merge_map, norm_warnings = _normalize_adjudication_items(
            output=combined,
            candidates=candidates,
            changed_files=changed_files,
        )
        warnings.extend(norm_warnings)

        severity_rank = {"high": 0, "medium": 1, "low": 2}
        findings = sorted(
            findings,
            key=lambda item: (
                severity_rank.get(item.severity, 99),
                item.file_path,
                item.line_start,
                item.id,
            ),
        )
        metadata[node_name] = {
            "candidate_count": len(candidates),
            "batch_count": len(batches),
            "batch_records": batch_records,
            "promoted_count": len(findings),
            "candidate_lifecycle": lifecycle,
            "merge_map": merge_map,
            "warnings": warnings,
            "packet_candidate_ids": _candidate_ids_from_packets(packets),
        }

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE review_adjudicator run_id=%s candidates=%s batches=%s promoted=%s warnings=%s",
                run_id,
                len(candidates),
                len(batches),
                len(findings),
                len(warnings),
            )

        return {
            "findings": findings,
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return review_adjudicator_node

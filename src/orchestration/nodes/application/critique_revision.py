"""Second-pass critique after focused context: map (digest shards) + reduce (final verdicts)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence, Set

from src.config import get_settings
from src.domain.schemas import (
    CandidateFinding,
    CritiqueRevisionDigest,
    CritiqueRevisionDigestOutput,
    CritiqueRevisionOutput,
    CritiqueRevisionShardPayload,
    FocusedContextResult,
    ReflectionReport,
)
from src.domain.verifier_schemas import VerifierReport
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.prompts.renderer import render_reviewer_prompt

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


def _coerce_focused_result(raw: Any) -> FocusedContextResult | None:
    if isinstance(raw, FocusedContextResult):
        return raw
    if isinstance(raw, dict):
        try:
            return FocusedContextResult.model_validate(raw)
        except Exception:
            return None
    return None


def _needs_revision_candidates(state: GraphState) -> List[str]:
    ids: set[str] = set()
    for raw in state.get("reflection_reports", []) or []:
        if isinstance(raw, ReflectionReport):
            report = raw
        elif isinstance(raw, dict):
            try:
                report = ReflectionReport.model_validate(raw)
            except Exception:
                continue
        else:
            continue
        if report.verdict in ("needs_context", "needs_verification"):
            ids.add(report.candidate_id)
    return sorted(ids)


def _candidate_ids_needs_verification(state: GraphState) -> Set[str]:
    out: Set[str] = set()
    for raw in state.get("reflection_reports", []) or []:
        if isinstance(raw, ReflectionReport):
            report = raw
        elif isinstance(raw, dict):
            try:
                report = ReflectionReport.model_validate(raw)
            except Exception:
                continue
        else:
            continue
        if report.verdict == "needs_verification":
            out.add(report.candidate_id)
    return out


def _has_verifier_report_for_candidate(state: GraphState, candidate_id: str) -> bool:
    for raw in state.get("verifier_reports") or []:
        if isinstance(raw, VerifierReport):
            r = raw
        elif isinstance(raw, dict):
            try:
                r = VerifierReport.model_validate(raw)
            except Exception:
                continue
        else:
            continue
        if r.candidate_id == candidate_id:
            return True
    return False


def revision_inputs_ready(state: GraphState, candidate_ids: Sequence[str]) -> bool:
    """True when every revision candidate has focused evidence and/or a verifier report if reflection requested it."""
    nv = _candidate_ids_needs_verification(state)
    for cid in candidate_ids:
        if _focused_results_for_candidate(state, cid):
            continue
        if cid in nv and _has_verifier_report_for_candidate(state, cid):
            continue
        return False
    return True


def _has_focused_evidence(state: GraphState, candidate_ids: Sequence[str]) -> bool:
    want = set(candidate_ids)
    for raw in (state.get("focused_context_results", {}) or {}).values():
        res = _coerce_focused_result(raw)
        if res is None:
            continue
        if res.candidate_id in want and (res.file_snippets or res.search_hits or res.file_contents_full):
            return True
    return False


def _candidates_by_id(state: GraphState, candidate_ids: Set[str]) -> Dict[str, CandidateFinding]:
    out: Dict[str, CandidateFinding] = {}
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is None:
            continue
        if cand.candidate_id in candidate_ids:
            out[cand.candidate_id] = cand
    return out


def _focused_results_for_candidate(
    state: GraphState,
    candidate_id: str,
) -> List[FocusedContextResult]:
    rows: List[FocusedContextResult] = []
    for raw in (state.get("focused_context_results", {}) or {}).values():
        res = _coerce_focused_result(raw)
        if res is None:
            continue
        if res.candidate_id == candidate_id:
            rows.append(res)
    rows.sort(key=lambda r: r.request_id)
    return rows


def plan_critique_revision_shards(
    state: GraphState,
    candidate_ids: Sequence[str],
    *,
    max_shard_chars: int,
    max_candidate_chars: int,
) -> List[CritiqueRevisionShardPayload]:
    """Partition focused context into bounded shards (one candidate per shard payload)."""
    ids_set = set(candidate_ids)
    candidates = _candidates_by_id(state, ids_set)
    shards: List[CritiqueRevisionShardPayload] = []

    for cid in sorted(candidate_ids):
        candidate = candidates.get(cid)
        if candidate is None:
            continue
        results = _focused_results_for_candidate(state, cid)
        if not results:
            if cid in _candidate_ids_needs_verification(state) and _has_verifier_report_for_candidate(state, cid):
                shards.append(
                    CritiqueRevisionShardPayload(
                        shard_id=f"{cid}:verifier_only",
                        candidate_id=cid,
                        candidate=candidate,
                        focused_results=[],
                    )
                )
            continue

        cand_weight = min(len(candidate.model_dump_json()), max_candidate_chars)
        bucket: List[FocusedContextResult] = []
        size = cand_weight
        shard_idx = 0

        for res in results:
            piece = len(res.model_dump_json())
            if bucket and size + piece > max_shard_chars:
                sid = f"{cid}:{shard_idx}"
                shards.append(
                    CritiqueRevisionShardPayload(
                        shard_id=sid,
                        candidate_id=cid,
                        candidate=candidate,
                        focused_results=list(bucket),
                    )
                )
                shard_idx += 1
                bucket = []
                size = cand_weight

            bucket.append(res)
            size += piece

        if bucket:
            sid = f"{cid}:{shard_idx}"
            shards.append(
                CritiqueRevisionShardPayload(
                    shard_id=sid,
                    candidate_id=cid,
                    candidate=candidate,
                    focused_results=list(bucket),
                )
            )

    return shards


def _render_digest_shard_prompt(
    shard: CritiqueRevisionShardPayload,
    *,
    max_candidate_chars: int,
) -> str:
    cand_raw = shard.candidate.model_dump_json()
    if len(cand_raw) > max_candidate_chars:
        cand_raw = cand_raw[:max_candidate_chars] + "\n... [truncated]"
    parts: List[str] = [f"### Candidate ({shard.candidate_id})\n{cand_raw}"]
    for res in shard.focused_results:
        parts.append(f"#### Focused context {res.request_id}\n{res.model_dump_json()}")
    return "\n\n".join(parts)


def _render_verifier_advisory_section(
    state: GraphState,
    candidate_ids: Sequence[str],
    *,
    max_chars: int = 8000,
) -> str:
    want = set(candidate_ids)
    parts: List[str] = []
    for raw in state.get("verifier_reports") or []:
        if isinstance(raw, VerifierReport):
            r = raw
        elif isinstance(raw, dict):
            try:
                r = VerifierReport.model_validate(raw)
            except Exception:  # noqa: BLE001
                continue
        else:
            continue
        if r.candidate_id not in want:
            continue
        parts.append(r.model_dump_json())
    if not parts:
        return ""
    blob = "\n\n".join(parts)
    if len(blob) > max_chars:
        blob = blob[:max_chars] + "\n... [truncated]"
    return f"### Runtime verifier (advisory; scope may limit interpretability)\n{blob}"


def _render_reduction_bundle(
    state: GraphState,
    candidate_ids: Sequence[str],
    digests: Mapping[str, CritiqueRevisionDigest],
    *,
    max_candidate_chars: int,
) -> str:
    """Compact text for the final reducer (digests only, no raw focused results)."""
    want = set(candidate_ids)
    candidates = _candidates_by_id(state, want)
    sections: List[str] = []

    for cid in sorted(candidate_ids):
        cand = candidates.get(cid)
        if cand is None:
            continue
        cand_raw = cand.model_dump_json()
        if len(cand_raw) > max_candidate_chars:
            cand_raw = cand_raw[:max_candidate_chars] + "\n... [truncated]"
        sections.append(f"### Candidate {cid}\n{cand_raw}")

        shard_digests = [d for d in digests.values() if d.candidate_id == cid]
        shard_digests.sort(key=lambda d: d.shard_id)
        for d in shard_digests:
            bullets = "\n".join(f"- {b}" for b in d.evidence_bullets) or "(no bullets)"
            sections.append(
                f"#### Digest shard {d.shard_id} (requests {d.request_ids})\n"
                f"impact={d.impact}\n{bullets}"
            )

        vline = _render_verifier_advisory_section(state, [cid])
        if vline:
            sections.append(vline)

    return "\n\n".join(sections)


def _normalize_revision_rows(
    revisions: Sequence[Any],
    expected_ids: Set[str],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Drop unknown ids; dedupe by candidate_id (last wins) with warnings."""
    warnings: List[str] = []
    ordered: Dict[str, Dict[str, Any]] = {}
    for raw in revisions:
        if hasattr(raw, "model_dump"):
            row = raw.model_dump()
        elif isinstance(raw, dict):
            row = dict(raw)
        else:
            continue
        cid = str(row.get("candidate_id") or "")
        if not cid:
            warnings.append("critique_revision_missing_candidate_id")
            continue
        if cid not in expected_ids:
            warnings.append(f"critique_revision_unknown_candidate:{cid}")
            continue
        if cid in ordered:
            warnings.append(f"critique_revision_duplicate_candidate:{cid}")
        ordered[cid] = row
    return list(ordered.values()), warnings


def make_critique_revision_digest_node(model_key: str | None = None, use_llm: bool = True):
    node_name = "critique_revision_digest"

    def critique_revision_digest_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        raw_shard = state.get("critique_revision_shard")
        if not raw_shard:
            return {"node_history": [f"{node_name}:skipped"]}

        try:
            shard = CritiqueRevisionShardPayload.model_validate(raw_shard)
        except Exception as exc:  # noqa: BLE001
            logger.warning("%s invalid shard run_id=%s reason=%s", node_name, run_id, exc)
            return {"node_history": [f"{node_name}:invalid_shard:{exc.__class__.__name__}"]}

        settings = get_settings()
        max_candidate_chars = settings.reviewer_critique_revision_max_candidate_chars
        prompt = render_reviewer_prompt(
            "critique_revision_digest.md",
            {
                "Shard Id": shard.shard_id,
                "Candidate And Focused Evidence": _render_digest_shard_prompt(
                    shard,
                    max_candidate_chars=max_candidate_chars,
                ),
                "Git Diff Excerpt": (state.get("git_diff", "") or "")[:6000],
                "Verifier Advisory": _render_verifier_advisory_section(state, [shard.candidate_id]),
            },
        )

        warnings: List[str] = []
        digest: CritiqueRevisionDigest
        llm_tokens = 0

        if use_llm:
            selected_model = model_key or getattr(settings, "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(CritiqueRevisionDigestOutput, model_key=selected_model)
                invoke_result = llm.invoke(prompt)
                response = parse_structured_output(invoke_result, CritiqueRevisionDigestOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                if response.candidate_id != shard.candidate_id:
                    warnings.append(
                        f"digest_candidate_mismatch:expected={shard.candidate_id} got={response.candidate_id}"
                    )
                rid_list = list(response.request_ids) or [r.request_id for r in shard.focused_results]
                digest = CritiqueRevisionDigest(
                    shard_id=shard.shard_id,
                    candidate_id=shard.candidate_id,
                    request_ids=rid_list,
                    evidence_bullets=list(response.evidence_bullets),
                    impact=response.impact,
                    warnings=list(response.warnings) + warnings,
                )
            except Exception as exc:  # noqa: BLE001
                w = f"critique_revision_digest_llm_failed:{exc.__class__.__name__}: {exc}"
                warnings.append(w)
                logger.warning("%s failed run_id=%s shard=%s reason=%s", node_name, run_id, shard.shard_id, exc)
                digest = CritiqueRevisionDigest(
                    shard_id=shard.shard_id,
                    candidate_id=shard.candidate_id,
                    request_ids=[r.request_id for r in shard.focused_results],
                    evidence_bullets=[],
                    impact="unclear",
                    warnings=warnings,
                )
        else:
            digest = CritiqueRevisionDigest(
                shard_id=shard.shard_id,
                candidate_id=shard.candidate_id,
                request_ids=[r.request_id for r in shard.focused_results],
                evidence_bullets=[f"[offline] condensed shard {shard.shard_id}"],
                impact="unclear",
                warnings=warnings,
            )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critique_revision_digest run_id=%s shard=%s candidate=%s",
                run_id,
                shard.shard_id,
                shard.candidate_id,
            )

        return {
            "critique_revision_digests": {shard.shard_id: digest},
            "node_history": [node_name],
            "token_usage": llm_tokens,
        }

    return critique_revision_digest_node


def make_critique_revision_reduce_node(model_key: str | None = None, use_llm: bool = True):
    node_name = "critique_revision_reduce"

    def critique_revision_reduce_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        candidate_ids = _needs_revision_candidates(state)
        digests_map = dict(state.get("critique_revision_digests") or {})

        if not candidate_ids:
            return {"node_history": [f"{node_name}:skipped"]}

        settings = get_settings()
        max_candidate_chars = settings.reviewer_critique_revision_max_candidate_chars

        # Coerce digest values to models (checkpoint may store dicts).
        digests: Dict[str, CritiqueRevisionDigest] = {}
        for key, raw in digests_map.items():
            if isinstance(raw, CritiqueRevisionDigest):
                digests[key] = raw
            elif isinstance(raw, dict):
                try:
                    digests[key] = CritiqueRevisionDigest.model_validate(raw)
                except Exception:
                    continue

        if not revision_inputs_ready(state, candidate_ids):
            return {"node_history": [f"{node_name}:skipped_no_results"]}

        bundle = _render_reduction_bundle(
            state,
            candidate_ids,
            digests,
            max_candidate_chars=max_candidate_chars,
        )

        prompt = render_reviewer_prompt(
            "critique_revision.md",
            {
                "Candidates And Digest Summaries": bundle,
                "Git Diff Excerpt": (state.get("git_diff", "") or "")[:8000],
            },
        )

        revisions: List[Dict[str, Any]] = []
        warnings: List[str] = []
        llm_tokens = 0

        expected_ids = set(candidate_ids)

        if use_llm:
            selected_model = model_key or getattr(settings, "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(CritiqueRevisionOutput, model_key=selected_model)
                invoke_result = llm.invoke(prompt)
                response = parse_structured_output(invoke_result, CritiqueRevisionOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                rows, norm_warnings = _normalize_revision_rows(response.revisions, expected_ids)
                revisions = rows
                warnings.extend(norm_warnings)
                warnings.extend(response.warnings)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"critique_revision_reduce_llm_failed:{exc.__class__.__name__}: {exc}")
                logger.warning(
                    "%s failed run_id=%s reason=%s: %s",
                    node_name,
                    run_id,
                    exc.__class__.__name__,
                    exc,
                )

        shard_ids_expected = {
            s.shard_id
            for s in plan_critique_revision_shards(
                state,
                candidate_ids,
                max_shard_chars=settings.reviewer_critique_revision_max_shard_chars,
                max_candidate_chars=max_candidate_chars,
            )
        }
        missing_shards = sorted(shard_ids_expected - set(digests.keys()))

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critique_revision_reduce run_id=%s candidates=%s revisions=%s digests=%s",
                run_id,
                candidate_ids,
                len(revisions),
                len(digests),
            )

        metadata = dict(state.get("metadata", {}))
        metadata["critique_revision"] = {
            "revisions": revisions,
            "warnings": warnings,
            "candidate_count": len(candidate_ids),
            "shard_count_planned": len(shard_ids_expected),
            "digest_count": len(digests),
            "digest_shard_ids": sorted(digests.keys()),
            "missing_digest_shards": missing_shards,
        }

        return {
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
        }

    return critique_revision_reduce_node


def make_critique_revision_node(model_key: str | None = None, use_llm: bool = True):
    """Backward-compatible alias for tests importing the old factory name."""

    return make_critique_revision_reduce_node(model_key=model_key, use_llm=use_llm)

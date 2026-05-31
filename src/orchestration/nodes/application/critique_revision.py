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
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.nodes.verifier.failure_class import (
    verifier_confidence_label,
    verifier_refutation_applies,
)
from src.orchestration.context.context_packets import (
    build_critique_revision_shard_packet,
    packet_to_prompt_sections,
)
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.routing.reflection_consolidation import (
    candidate_has_local_defect_signature,
    consolidate_reflection_reports,
)
from src.orchestration.routing.claim_tiering import classify_claim_tier, review_kb_context_for_candidate

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


def _reject_recheck_revision_candidates(state: GraphState) -> Set[str]:
    """Source-local claims rejected after focused context was gathered."""
    out: Set[str] = set()
    reports = consolidate_reflection_reports(state.get("reflection_reports", []) or [])
    by_id = _all_candidates_by_id(state)
    for report in reports:
        if report.verdict != "reject":
            continue
        if report.reflector_specialty not in ("logic", "security"):
            continue
        if not _focused_results_for_candidate(state, report.candidate_id):
            continue
        cand = by_id.get(report.candidate_id)
        if cand is None:
            continue
        if report.support_scope == "local" or (
            report.support_scope is None and candidate_has_local_defect_signature(cand)
        ):
            out.add(report.candidate_id)
    return out


def _needs_revision_candidates(state: GraphState) -> List[str]:
    ids: set[str] = set()
    for report in consolidate_reflection_reports(state.get("reflection_reports", []) or []):
        if report.verdict in ("needs_context", "needs_verification"):
            ids.add(report.candidate_id)
    ids.update(_reject_recheck_revision_candidates(state))
    by_id = _all_candidates_by_id(state)
    metadata = state.get("metadata") or {}
    filtered: set[str] = set()
    for cid in ids:
        cand = by_id.get(cid)
        if cand is None:
            filtered.add(cid)
            continue
        tier = classify_claim_tier(
            cand,
            review_kb_context=review_kb_context_for_candidate(metadata, cand),
        )
        if tier == "speculative_guard":
            continue
        if (
            tier == "direct_regression"
            and not _focused_results_for_candidate(state, cid)
            and not _has_verifier_report_for_candidate(state, cid)
        ):
            continue
        filtered.add(cid)
    return sorted(filtered)


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


def expected_critique_revision_shard_ids(
    state: GraphState,
    candidate_ids: Sequence[str],
    *,
    max_shard_chars: int | None = None,
    max_candidate_chars: int | None = None,
) -> set[str]:
    """Shard ids that must exist in ``critique_revision_digests`` before reduce runs."""
    settings = get_settings()
    if max_shard_chars is None:
        max_shard_chars = settings.reviewer_critique_revision_max_shard_chars
    if max_candidate_chars is None:
        max_candidate_chars = settings.reviewer_critique_revision_max_candidate_chars
    return {
        s.shard_id
        for s in plan_critique_revision_shards(
            state,
            list(candidate_ids),
            max_shard_chars=max_shard_chars,
            max_candidate_chars=max_candidate_chars,
        )
    }


def _candidate_revision_input_ready(state: GraphState, candidate_id: str) -> bool:
    """Per-candidate: focused hits and/or verifier report when reflection asked for runtime proof."""
    if _focused_results_for_candidate(state, candidate_id):
        return True
    if (
        candidate_id in _candidate_ids_needs_verification(state)
        and _has_verifier_report_for_candidate(state, candidate_id)
    ):
        return True
    return False


def revision_ready_candidate_ids(
    state: GraphState,
    candidate_ids: Sequence[str] | None = None,
) -> List[str]:
    """Subset of revision candidates that have enough input to run digest/reduce (does not block siblings)."""
    ids = list(candidate_ids) if candidate_ids is not None else _needs_revision_candidates(state)
    return [cid for cid in ids if _candidate_revision_input_ready(state, cid)]


def critique_revision_digests_complete(state: GraphState) -> bool:
    """True when every planned digest shard for revision-ready candidates is present."""
    candidate_ids = revision_ready_candidate_ids(
        state, _dedupe_revision_candidate_ids(state, _needs_revision_candidates(state))
    )
    if not candidate_ids:
        return True
    expected = expected_critique_revision_shard_ids(state, candidate_ids)
    if not expected:
        return True
    have = set((state.get("critique_revision_digests") or {}).keys())
    return expected <= have


def revision_inputs_ready(state: GraphState, candidate_ids: Sequence[str]) -> bool:
    """True when at least one revision candidate has focused evidence and/or a verifier report."""
    return bool(revision_ready_candidate_ids(state, candidate_ids))


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
    state: GraphState,
    shard: CritiqueRevisionShardPayload,
    *,
    verifier_advisory: str = "",
) -> str:
    """Render shard evidence via ContextPacket (never reads critique_pipeline direct_context)."""
    packet = build_critique_revision_shard_packet(
        state,
        candidate=shard.candidate,
        focused_results=shard.focused_results,
        verifier_advisory=verifier_advisory,
    )
    sections = packet_to_prompt_sections(packet)
    return sections.get("Candidate And Focused Evidence", "")


def _compact_verifier_report_json(report: VerifierReport) -> str:
    """Advisory blob for revision prompts — omit multi-kB generated test_code."""
    import json

    payload = report.model_dump(mode="json")
    attempts = payload.get("attempts") or []
    slim_attempts: List[Dict[str, Any]] = []
    for att in attempts:
        if not isinstance(att, dict):
            continue
        stdout = str(att.get("stdout") or "")
        stderr = str(att.get("stderr") or "")
        if len(stdout) > 1500:
            stdout = stdout[-1500:]
        if len(stderr) > 1500:
            stderr = stderr[-1500:]
        slim_attempts.append(
            {
                "attempt_number": att.get("attempt_number"),
                "exit_code": att.get("exit_code"),
                "timeout": att.get("timeout"),
                "sandbox_mode": att.get("sandbox_mode"),
                "failure_class": att.get("failure_class"),
                "stdout": stdout,
                "stderr": stderr,
            }
        )
    payload["attempts"] = slim_attempts
    meta = dict(payload.get("metadata") or {})
    payload["metadata"] = meta
    return json.dumps(payload, indent=2, ensure_ascii=False)


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
        parts.append(_compact_verifier_report_json(r))
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


def _all_candidates_by_id(state: GraphState) -> Dict[str, CandidateFinding]:
    out: Dict[str, CandidateFinding] = {}
    for raw in state.get("candidate_findings", []) or []:
        cand = _coerce_candidate(raw)
        if cand is not None:
            out[cand.candidate_id] = cand
    return out


def _dedupe_revision_candidate_ids(state: GraphState, candidate_ids: List[str]) -> List[str]:
    """Drop duplicate ReDoS/security siblings on the same file (keep first by id)."""
    by_id = _all_candidates_by_id(state)
    by_file_redos: Dict[str, str] = {}
    out: List[str] = []
    for cid in candidate_ids:
        cand = by_id.get(cid)
        if cand is None:
            out.append(cid)
            continue
        blob = f"{cand.content} {cand.failure_mode}".lower()
        is_redos = cand.claim_type == "security_risk" and (
            "redos" in blob or "backtrack" in blob or "catastrophic" in blob
        )
        if is_redos:
            key = cand.file_path.strip().lower()
            if key in by_file_redos:
                continue
            by_file_redos[key] = cid
        out.append(cid)
    return out


def _apply_digest_contradict_policy(
    revisions: List[Dict[str, Any]],
    digests: Mapping[str, CritiqueRevisionDigest],
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Downgrade accept → reject when any digest shard contradicts the candidate."""
    contradicts_by_candidate: Dict[str, int] = {}
    for digest in digests.values():
        if digest.impact == "contradicts":
            contradicts_by_candidate[digest.candidate_id] = (
                contradicts_by_candidate.get(digest.candidate_id, 0) + 1
            )

    warnings: List[str] = []
    adjusted: List[Dict[str, Any]] = []
    for row in revisions:
        row = dict(row)
        cid = str(row.get("candidate_id") or "")
        if str(row.get("verdict", "")).lower() == "accept" and contradicts_by_candidate.get(cid, 0) > 0:
            summary = str(row.get("updated_evidence_summary") or "")
            row["verdict"] = "reject"
            row["updated_evidence_summary"] = (
                f"{summary} Digest impact=contradicts; acceptance overridden.".strip()
            )
            warnings.append(f"critique_revision_digest_contradicts:{cid}")
        adjusted.append(row)
    return adjusted, warnings


def _apply_verifier_policy_to_revisions(
    revisions: List[Dict[str, Any]],
    state: GraphState,
) -> tuple[List[Dict[str, Any]], List[str]]:
    """Adjust revision rows using verifier_hints (harness-aware)."""
    hints: Dict[str, Any] = dict((state.get("metadata") or {}).get("verifier_hints") or {})
    warnings: List[str] = []
    adjusted: List[Dict[str, Any]] = []
    candidates_by_id = _all_candidates_by_id(state)
    for row in revisions:
        row = dict(row)
        cid = str(row.get("candidate_id") or "")
        hint = hints.get(cid)
        if not isinstance(hint, dict):
            adjusted.append(row)
            continue
        harness = bool(hint.get("harness_error"))
        v_verdict = str(hint.get("verdict") or "").lower()
        scope = str(hint.get("verification_scope") or "")
        confidence = str(hint.get("confidence") or "")
        verdict = str(row.get("verdict") or "").lower()
        summary = str(row.get("updated_evidence_summary") or "")
        cand = candidates_by_id.get(cid)
        cand_dict = cand.model_dump(mode="json") if cand is not None else {"failure_mode": ""}
        if not confidence:
            confidence = verifier_confidence_label(
                cand_dict,
                verifier_verdict=v_verdict,
                verification_scope=scope,
                harness_error=harness,
                product_verified=bool(hint.get("product_verified")),
            )
        if harness:
            note = "runtime unverified (harness)"
            if note not in summary:
                row["updated_evidence_summary"] = f"{summary} {note}".strip()
            warnings.append(f"critique_revision_harness:{cid}")
        elif (
            v_verdict == "refuted"
            and scope == "concrete_behavior"
            and confidence == "clean_product_signal"
            and verdict == "accept"
        ):
            if verifier_refutation_applies(
                cand_dict,
                verifier_verdict=v_verdict,
                verification_scope=scope,
                harness_error=harness,
            ):
                row["verdict"] = "reject"
                row["updated_evidence_summary"] = (
                    f"{summary} Runtime verifier refuted concrete_behavior claim.".strip()
                )
                warnings.append(f"critique_revision_verifier_refuted:{cid}")
            else:
                note = "runtime inconclusive for wrong-output claim (exit 0 without STATUS: SAFE)"
                if note not in summary:
                    row["updated_evidence_summary"] = f"{summary} {note}".strip()
                warnings.append(f"critique_revision_verifier_inconclusive_wrong_output:{cid}")
        elif v_verdict == "refuted" and verdict == "accept":
            note = f"runtime advisory only ({confidence})"
            if note not in summary:
                row["updated_evidence_summary"] = f"{summary} {note}".strip()
            if confidence == "static_claim_not_runtime_refutable":
                warnings.append(f"critique_revision_verifier_inconclusive_wrong_output:{cid}")
            warnings.append(f"critique_revision_verifier_advisory:{cid}:{confidence}")
        adjusted.append(row)
    return adjusted, warnings


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


def _partition_revision_batches(
    candidate_ids: Sequence[str],
    batch_size: int,
) -> List[List[str]]:
    size = max(1, int(batch_size))
    ids = list(candidate_ids)
    return [ids[i : i + size] for i in range(0, len(ids), size)]


def _critique_revision_completion_cap(settings: Any) -> int:
    return int(settings.reviewer_critique_revision_max_completion_tokens)


def _invoke_reduce_batch(
    *,
    state: GraphState,
    batch_ids: Sequence[str],
    digests: Mapping[str, CritiqueRevisionDigest],
    max_candidate_chars: int,
    model_key: str | None,
    use_llm: bool,
    batch_index: int,
    batch_count: int,
) -> tuple[List[Dict[str, Any]], List[str], int, List[Dict[str, Any]], bool]:
    """Run one reduce LLM call; return rows, warnings, tokens, trace, failed."""
    run_id = state.get("run_id", "unknown")
    settings = get_settings()
    warnings: List[str] = []
    llm_tokens = 0
    expected_ids = set(batch_ids)

    bundle = _render_reduction_bundle(
        state,
        batch_ids,
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

    if _trace_enabled(state):
        trace_logger.info(
            "TRACE critique_revision_reduce_batch run_id=%s batch=%s/%s candidates=%s prompt_chars=%s bundle_chars=%s",
            run_id,
            batch_index + 1,
            batch_count,
            list(batch_ids),
            len(prompt),
            len(bundle),
        )

    if not use_llm:
        return [], warnings, llm_tokens, [], False

    selected_model = model_key or getattr(settings, "reviewer_worker_model_key", None)
    llm_trace: List[Dict[str, Any]] = []
    try:
        llm = Models.worker(
            CritiqueRevisionOutput,
            model_key=selected_model,
            max_completion_tokens=_critique_revision_completion_cap(settings),
        )
        traced = trace_llm_call(
            llm,
            prompt,
            state=state,
            node_name="critique_revision_reduce",
            model_key=selected_model,
            schema_name="CritiqueRevisionOutput",
            request_label=f"batch_{batch_index + 1}_of_{batch_count}",
            input_summary={"candidate_ids": list(batch_ids), "batch_index": batch_index},
        )
        invoke_result = traced.result
        response = parse_structured_output(invoke_result, CritiqueRevisionOutput)
        llm_tokens = traced.tokens
        llm_trace = append_trace(llm_trace, traced)
        rows, norm_warnings = _normalize_revision_rows(response.revisions, expected_ids)
        warnings.extend(norm_warnings)
        warnings.extend(response.warnings)
        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critique_revision_reduce_batch_done run_id=%s batch=%s/%s revisions=%s tokens=%s",
                run_id,
                batch_index + 1,
                batch_count,
                len(rows),
                llm_tokens,
            )
        return rows, warnings, llm_tokens, llm_trace, False
    except Exception as exc:  # noqa: BLE001
        llm_trace.extend(trace_from_exception(exc))
        w = f"critique_revision_reduce_batch_failed:{batch_index}:{exc.__class__.__name__}: {exc}"
        warnings.append(w)
        logger.warning(
            "critique_revision_reduce batch failed run_id=%s batch=%s/%s reason=%s",
            run_id,
            batch_index + 1,
            batch_count,
            exc,
        )
        return [], warnings, llm_tokens, llm_trace, True


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
        verifier_advisory = _render_verifier_advisory_section(state, [shard.candidate_id])
        shard_packet = build_critique_revision_shard_packet(
            state,
            candidate=shard.candidate,
            focused_results=shard.focused_results,
            verifier_advisory=verifier_advisory,
            settings=settings,
        )
        shard_sections = packet_to_prompt_sections(shard_packet)
        prompt = render_reviewer_prompt(
            "critique_revision_digest.md",
            {
                "Shard Id": shard.shard_id,
                "Candidate And Focused Evidence": shard_sections.get(
                    "Candidate And Focused Evidence",
                    _render_digest_shard_prompt(state, shard, verifier_advisory=verifier_advisory),
                ),
                "Git Diff Excerpt": shard_sections.get(
                    "Git Diff Excerpt",
                    (state.get("git_diff", "") or "")[:6000],
                ),
                "Verifier Advisory": verifier_advisory,
            },
        )

        warnings: List[str] = []
        digest: CritiqueRevisionDigest
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []

        if use_llm:
            selected_model = model_key or getattr(settings, "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(
                    CritiqueRevisionDigestOutput,
                    model_key=selected_model,
                    max_completion_tokens=_critique_revision_completion_cap(settings),
                )
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=selected_model,
                    schema_name="CritiqueRevisionDigestOutput",
                    input_summary={"shard_id": shard.shard_id, "candidate_id": shard.candidate_id},
                )
                invoke_result = traced.result
                response = parse_structured_output(invoke_result, CritiqueRevisionDigestOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
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
                llm_trace.extend(trace_from_exception(exc))
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
            "llm_trace": llm_trace,
        }

    return critique_revision_digest_node


def make_critique_revision_reduce_node(model_key: str | None = None, use_llm: bool = True):
    node_name = "critique_revision_reduce"

    def critique_revision_reduce_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        metadata_existing = dict(state.get("metadata", {}) or {})
        if metadata_existing.get("critique_revision", {}).get("reduce_completed"):
            return {"node_history": [f"{node_name}:idempotent_skip"]}

        if not critique_revision_digests_complete(state):
            if _trace_enabled(state):
                expected = expected_critique_revision_shard_ids(
                    state,
                    revision_ready_candidate_ids(
                        state,
                        _dedupe_revision_candidate_ids(
                            state, _needs_revision_candidates(state)
                        ),
                    ),
                )
                have = set((state.get("critique_revision_digests") or {}).keys())
                trace_logger.info(
                    "TRACE critique_revision_reduce_barrier_waiting run_id=%s have=%s expected=%s",
                    run_id,
                    sorted(have),
                    sorted(expected),
                )
            return {"node_history": [f"{node_name}:barrier_incomplete"]}

        candidate_ids = _dedupe_revision_candidate_ids(
            state, _needs_revision_candidates(state)
        )
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

        ready_ids = revision_ready_candidate_ids(state, candidate_ids)
        if not ready_ids:
            return {"node_history": [f"{node_name}:skipped_no_results"]}
        candidate_ids = ready_ids

        batch_size = int(settings.reviewer_critique_revision_reduce_batch_size)
        batches = _partition_revision_batches(candidate_ids, batch_size)
        raw_revisions: List[Dict[str, Any]] = []
        warnings: List[str] = []
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        failed_batches = 0

        for batch_index, batch_ids in enumerate(batches):
            rows, batch_warnings, batch_tokens, batch_trace, batch_failed = _invoke_reduce_batch(
                state=state,
                batch_ids=batch_ids,
                digests=digests,
                max_candidate_chars=max_candidate_chars,
                model_key=model_key,
                use_llm=use_llm,
                batch_index=batch_index,
                batch_count=len(batches),
            )
            llm_tokens += batch_tokens
            llm_trace.extend(batch_trace)
            warnings.extend(batch_warnings)
            if batch_failed:
                failed_batches += 1
            raw_revisions.extend(rows)

        revisions, policy_warnings = _apply_verifier_policy_to_revisions(raw_revisions, state)
        warnings.extend(policy_warnings)
        revisions, digest_warnings = _apply_digest_contradict_policy(revisions, digests)
        warnings.extend(digest_warnings)

        shard_ids_expected = expected_critique_revision_shard_ids(
            state,
            candidate_ids,
            max_candidate_chars=max_candidate_chars,
        )
        missing_shards = sorted(shard_ids_expected - set(digests.keys()))

        reduce_failed = use_llm and len(revisions) == 0 and bool(candidate_ids)

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critique_revision_reduce run_id=%s candidates=%s revisions=%s digests=%s batches=%s failed_batches=%s",
                run_id,
                candidate_ids,
                len(revisions),
                len(digests),
                len(batches),
                failed_batches,
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
            "reduce_batch_size": batch_size,
            "reduce_batch_count": len(batches),
            "reduce_failed_batches": failed_batches,
            "reduce_failed": reduce_failed,
            "reduce_completed": True,
        }

        return {
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return critique_revision_reduce_node


def make_critique_revision_node(model_key: str | None = None, use_llm: bool = True):
    """Backward-compatible alias for tests importing the old factory name."""

    return make_critique_revision_reduce_node(model_key=model_key, use_llm=use_llm)

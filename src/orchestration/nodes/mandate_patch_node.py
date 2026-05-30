"""Patch BehavioralSpec from exploration ledger deltas."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from src.config import Settings, get_settings
from src.domain.schemas import BehavioralEvidenceRef, BehavioralSpec
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.context.mandate_loop_context import (
    bootstrap_digest,
    build_bootstrap_digest,
    format_delta_ledger_for_patch,
    ledger_since_last_patch,
    patch_seq,
    spec_excerpt_for_prompt,
)
from src.orchestration.nodes.application.planner import _target_files
from src.orchestration.context.mandate_loop_context import mm_meta
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.review_principles import DECLARED_INPUT_CONTRACT_GUIDANCE

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


class MandatePatchOutput(BaseModel):
    behavioral_expectations: str = Field(default="")
    contract_boundaries: str = Field(default="")
    historical_precedents: str = Field(default="")
    risk_hypotheses: str = Field(default="")
    reviewer_guidance: str = Field(default="")
    uncertainties: str = Field(default="")


def _apply_patch_to_spec(
    *,
    prior: BehavioralSpec | None,
    intent_summary: str,
    patch: MandatePatchOutput,
    changed_files: List[str],
) -> BehavioralSpec:
    guidance = patch.reviewer_guidance.strip() or (
        f"Stay structural and unbiased. {DECLARED_INPUT_CONTRACT_GUIDANCE}"
    )
    refs: List[BehavioralEvidenceRef] = []
    if prior:
        refs = list(prior.evidence_refs)
    seen = {r.ref for r in refs}
    for fp in changed_files[:12]:
        if fp not in seen:
            refs.append(BehavioralEvidenceRef(kind="file", ref=fp, note="Changed in this PR"))
            seen.add(fp)
    return BehavioralSpec(
        intent_summary=intent_summary or (prior.intent_summary if prior else ""),
        behavioral_expectations=patch.behavioral_expectations.strip()
        or (prior.behavioral_expectations if prior else ""),
        contract_boundaries=patch.contract_boundaries.strip()
        or (prior.contract_boundaries if prior else ""),
        historical_precedents=patch.historical_precedents.strip()
        or (prior.historical_precedents if prior else ""),
        risk_hypotheses=patch.risk_hypotheses.strip()
        or (prior.risk_hypotheses if prior else "Hypotheses only; verify in code."),
        reviewer_guidance=guidance,
        evidence_refs=refs,
        confidence=0.65 if prior else 0.55,
        uncertainties=patch.uncertainties.strip()
        or (prior.uncertainties if prior else "Verify against repository evidence."),
    )


def make_mandate_patch_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "mandate_patch"

    def mandate_patch_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        run_id = str(state.get("run_id", "unknown"))
        meta, slot = mm_meta(state)
        intent = dict(slot.get("intent_extractor") or {})
        intent_summary = str(intent.get("intent_summary", ""))
        prev_seq = patch_seq(state)
        patch_mode = "initial" if prev_seq == 0 else "delta"
        ref = state.get("behavioral_spec_ref")
        ref_str = ref if isinstance(ref, str) else None
        store = BehavioralSpecStore(resolved)
        prior: BehavioralSpec | None = None
        if ref_str:
            try:
                prior = store.read(ref_str)
            except Exception:  # noqa: BLE001
                prior = None

        delta_text = format_delta_ledger_for_patch(state)
        spec_excerpt = spec_excerpt_for_prompt(ref_str, resolved)
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        warnings: List[str] = []
        patch_out = MandatePatchOutput()

        if use_llm:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/mandate_patch.md",
                    {
                        "patch_mode": patch_mode,
                        "intent_summary": intent_summary[:4000],
                        "spec_excerpt": spec_excerpt,
                        "delta_ledger": delta_text[:14000],
                    },
                )
                llm = Models.worker(MandatePatchOutput, model_key=resolved.reviewer_worker_model_key)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=resolved.reviewer_worker_model_key,
                    schema_name="MandatePatchOutput",
                    request_label=patch_mode,
                    input_summary={
                        "patch_mode": patch_mode,
                        "delta_entries": len(ledger_since_last_patch(state)),
                    },
                )
                invoke_result = traced.result
                patch_out = parse_structured_output(invoke_result, MandatePatchOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s fallback: %s", node_name, exc)

        if not patch_out.behavioral_expectations and not prior:
            patch_out = MandatePatchOutput(
                behavioral_expectations=(
                    "Preserve observable behavior unless the diff explicitly changes it."
                ),
                contract_boundaries=(
                    "Treat public APIs and types in changed files as stability boundaries."
                ),
                historical_precedents="Follow patterns in touched modules.",
                risk_hypotheses="Review diff-local control flow and data handling.",
                uncertainties="Bootstrap patch may be incomplete.",
            )

        spec = _apply_patch_to_spec(
            prior=prior,
            intent_summary=intent_summary,
            patch=patch_out,
            changed_files=_target_files(state),
        )
        ref_new, abs_path = store.write(run_id, spec)
        new_seq = prev_seq + 1
        inventory = slot.get("diff_surface_inventory")
        surface_inventory = (
            [str(x) for x in inventory if str(x).strip()]
            if isinstance(inventory, list)
            else None
        )
        digest = build_bootstrap_digest(
            spec,
            max_chars=int(resolved.reviewer_mandate_bootstrap_digest_max_chars),
            surface_inventory=surface_inventory,
        )

        slot["patch_seq"] = new_seq
        if patch_mode == "initial":
            slot["bootstrap_digest"] = digest
            slot["bootstrap_completed"] = True
        elif patch_out.contract_boundaries.strip() or patch_out.risk_hypotheses.strip():
            slot["bootstrap_digest"] = digest

        loop = dict(slot.get("coupled_loop", {}) or {})
        loop["patch_count"] = int(loop.get("patch_count", 0)) + 1
        loop["patch_seq"] = new_seq
        slot["coupled_loop"] = loop
        meta["mental_model"] = slot

        cache_refs = dict(state.get("cache_refs") or {})
        cache_refs["behavioral_spec"] = abs_path

        delta_count = len(ledger_since_last_patch(state))
        slot["ledger_applied_count"] = int(slot.get("ledger_applied_count", 0)) + delta_count
        meta["mental_model"] = slot

        if meta.get("review_trace_enabled"):
            trace_logger.info(
                "TRACE mandate_patch run_id=%s seq=%s mode=%s delta_entries=%s",
                run_id,
                new_seq,
                patch_mode,
                len(ledger_since_last_patch(state)),
            )

        return {
            "behavioral_spec_ref": ref_new,
            "cache_refs": cache_refs,
            "metadata": meta,
            "node_history": [f"{node_name}:{patch_mode}"],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return mandate_patch_node

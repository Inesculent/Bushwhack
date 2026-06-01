"""Phase 0: mental model formulation (intent, contracts, history, mandate)."""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Dict, List

from pydantic import BaseModel, Field

from src.config import Settings, get_settings
from src.domain.schemas import BehavioralEvidenceRef, BehavioralSpec
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import append_trace, trace_from_exception, trace_llm_call
from src.orchestration.context.context_packets import (
    build_intent_extractor_packet,
    build_mandate_synthesizer_packet,
    enrich_intent_summary_with_diff_scope,
    packet_to_prompt_sections,
)
from src.orchestration.context.mandate_loop_context import mm_meta
from src.orchestration.context.surface_ledger import (
    build_surface_invariants_from_ledger,
    build_surface_ledger_from_diff,
    surface_inventory_names,
    surface_ledger_from_state,
)
from src.orchestration.nodes.application.planner import _extract_files_from_diff, _target_files
from src.orchestration.prompts.renderer import render_reviewer_prompt
from src.orchestration.review_principles import DECLARED_INPUT_CONTRACT_GUIDANCE

logger = logging.getLogger(__name__)


def _git_recent_messages(repo_path: str, *, max_lines: int = 8) -> str:
    try:
        proc = subprocess.run(
            ["git", "log", "-n", str(max_lines), "--oneline", "--no-decorate"],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        return proc.stdout.strip()[:4000]
    except Exception as exc:  # noqa: BLE001
        logger.debug("historical_miner git log skipped: %s", exc)
        return ""


def _new_files_from_diff(git_diff: str) -> List[str]:
    new_files: List[str] = []
    current: str | None = None
    for line in git_diff.splitlines():
        if line.startswith("diff --git "):
            parts = line.split()
            current = None
            if len(parts) >= 4 and parts[3].startswith("b/"):
                current = parts[3].removeprefix("b/")
            continue
        if current and (line.startswith("new file mode") or line.startswith("--- /dev/null")):
            new_files.append(current)
    return sorted({p for p in new_files if p and p != "/dev/null"})


def _fallback_paths_for_new_files(repo_path: str, new_files: List[str], *, max_paths: int = 6) -> List[str]:
    root = Path(repo_path).resolve()
    collected: List[str] = []
    seen: set[str] = set()
    for rel in new_files:
        try:
            nf_path = (root / rel).resolve()
            nf_path.relative_to(root)
        except Exception:
            continue
        dir_path = nf_path.parent
        if not dir_path.is_dir():
            continue
        preferred = dir_path / "nodes.py"
        if preferred.is_file():
            candidate = preferred.relative_to(root).as_posix()
            if candidate not in seen and candidate != rel:
                collected.append(candidate)
                seen.add(candidate)
                if len(collected) >= max_paths:
                    return collected
        for fp in sorted(dir_path.glob("*.py")):
            candidate = fp.relative_to(root).as_posix()
            if candidate == rel or candidate in seen:
                continue
            collected.append(candidate)
            seen.add(candidate)
            if len(collected) >= max_paths:
                return collected
    return collected


def _git_log_for_paths(repo_path: str, paths: List[str], *, max_lines: int = 8) -> str:
    if not paths:
        return ""
    try:
        proc = subprocess.run(
            ["git", "log", "-n", str(max_lines), "--oneline", "--no-decorate", "--", *paths],
            cwd=repo_path,
            capture_output=True,
            text=True,
            timeout=12,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return ""
        return proc.stdout.strip()[:4000]
    except Exception as exc:  # noqa: BLE001
        logger.debug("historical_miner git log for paths skipped: %s", exc)
        return ""


class IntentExtractorOutput(BaseModel):
    intent_summary: str = Field(default="", description="Concise PR intent and scope.")
    non_goals: str = Field(default="", description="Explicit out-of-scope items if any.")


class MandateSynthesizerOutput(BaseModel):
    behavioral_expectations: str = Field(default="", description="Approximate expected behavior.")
    risk_hypotheses: str = Field(default="", description="Hypotheses only, not asserted defects.")
    reviewer_guidance: str = Field(
        default="",
        description="Remind reviewers to stay structural and unbiased.",
    )
    uncertainties: str = Field(default="", description="Known unknowns.")


def make_intent_extractor_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "intent_extractor"

    def intent_extractor_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        meta, slot = mm_meta(state)
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        intent_summary = ""
        non_goals = ""
        warnings: List[str] = []

        if use_llm:
            try:
                packet = build_intent_extractor_packet(state, settings=resolved)
                prompt = render_reviewer_prompt(
                    "mental_model/intent_extractor.md",
                    packet_to_prompt_sections(packet),
                )
                llm = Models.worker(IntentExtractorOutput, model_key=resolved.reviewer_worker_model_key)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=resolved.reviewer_worker_model_key,
                    schema_name="IntentExtractorOutput",
                    input_summary={"changed_files": _extract_files_from_diff(state.get("git_diff", "") or "")},
                )
                invoke_result = traced.result
                out = parse_structured_output(invoke_result, IntentExtractorOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
                intent_summary = out.intent_summary.strip()
                non_goals = out.non_goals.strip()
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback: %s", node_name, exc)

        if not intent_summary:
            files = _extract_files_from_diff(state.get("git_diff", "") or "")
            goals = str(state.get("user_goals", "") or "")
            intent_summary = (
                f"Heuristic intent: change touches {len(files)} file(s). "
                f"Review for regressions and contract fit. User goals: {goals[:800] or '(none)'}"
            ).strip()

        git_diff = state.get("git_diff", "") or ""
        surface_ledger = build_surface_ledger_from_diff(git_diff)
        inventory = surface_inventory_names(surface_ledger)
        intent_summary, scope_warnings = enrich_intent_summary_with_diff_scope(
            intent_summary, git_diff
        )
        warnings.extend(scope_warnings)

        slot["intent_extractor"] = {
            "intent_summary": intent_summary,
            "non_goals": non_goals,
            "warnings": warnings,
        }
        slot["surface_ledger"] = [s.model_dump(mode="json") for s in surface_ledger]
        slot["diff_surface_inventory"] = inventory
        meta["mental_model"] = slot
        return {
            "metadata": meta,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return intent_extractor_node


def make_mandate_synthesizer_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "mandate_synthesizer"

    def mandate_synthesizer_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        run_id = str(state.get("run_id", "unknown"))
        meta, slot = mm_meta(state)
        intent = dict(slot.get("intent_extractor") or {})
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        expectations = ""
        risks = ""
        guidance = (
            f"{DECLARED_INPUT_CONTRACT_GUIDANCE} "
            "Treat the behavioral mandate as directional context only. "
            "Do not assume defects exist because they are hypothesized here. "
            "Prioritize direct code evidence from the diff and repository."
        )
        uncertainties = ""
        warnings: List[str] = []

        if use_llm:
            try:
                synth_packet = build_mandate_synthesizer_packet(state, settings=resolved)
                prompt = render_reviewer_prompt(
                    "mental_model/mandate_synthesizer.md",
                    packet_to_prompt_sections(synth_packet),
                )
                llm = Models.worker(MandateSynthesizerOutput, model_key=resolved.reviewer_worker_model_key)
                traced = trace_llm_call(
                    llm,
                    prompt,
                    state=state,
                    node_name=node_name,
                    model_key=resolved.reviewer_worker_model_key,
                    schema_name="MandateSynthesizerOutput",
                    input_summary={"intent_chars": len(str(intent.get("intent_summary", "")))},
                )
                invoke_result = traced.result
                out = parse_structured_output(invoke_result, MandateSynthesizerOutput)
                llm_tokens = traced.tokens
                llm_trace = append_trace(llm_trace, traced)
                expectations = out.behavioral_expectations.strip()
                risks = out.risk_hypotheses.strip()
                if out.reviewer_guidance.strip():
                    guidance = f"{out.reviewer_guidance.strip()} {DECLARED_INPUT_CONTRACT_GUIDANCE}"
                uncertainties = out.uncertainties.strip()
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback: %s", node_name, exc)

        if not expectations:
            expectations = (
                "Expect the change to preserve existing observable behavior unless the diff explicitly changes it."
            )

        evidence_refs: List[BehavioralEvidenceRef] = []
        for fp in _target_files(state)[:12]:
            evidence_refs.append(BehavioralEvidenceRef(kind="file", ref=fp, note="Changed in this PR"))

        surface_ledger = surface_ledger_from_state({**state, "metadata": meta})
        if surface_ledger:
            slot["surface_ledger"] = [s.model_dump(mode="json") for s in surface_ledger]
            slot["diff_surface_inventory"] = surface_inventory_names(surface_ledger)

        store_read: BehavioralSpec | None = None
        if isinstance(state.get("behavioral_spec_ref"), str):
            try:
                store_read = BehavioralSpecStore(resolved).read(state["behavioral_spec_ref"])
            except Exception:  # noqa: BLE001
                store_read = None

        spec = BehavioralSpec(
            intent_summary=str(intent.get("intent_summary", "")),
            behavioral_expectations=expectations,
            contract_boundaries=(
                store_read.contract_boundaries if store_read else ""
            ),
            historical_precedents=(
                store_read.historical_precedents if store_read else ""
            ),
            risk_hypotheses=risks or "None stated; stay unbiased.",
            reviewer_guidance=guidance,
            evidence_refs=evidence_refs,
            surfaces=surface_ledger,
            surface_invariants=build_surface_invariants_from_ledger(
                surface_ledger,
                risk_hypotheses=risks,
            ),
            confidence=0.55 if warnings else 0.7,
            uncertainties=uncertainties or "LLM synthesis may be incomplete; verify against code.",
        )

        store = BehavioralSpecStore(resolved)
        ref, abs_path = store.write(run_id, spec)
        cache_refs = dict(state.get("cache_refs") or {})
        cache_refs["behavioral_spec"] = abs_path

        slot["mandate_synthesizer"] = {"warnings": warnings, "path": abs_path}
        meta["mental_model"] = slot

        return {
            "behavioral_spec_ref": ref,
            "cache_refs": cache_refs,
            "metadata": meta,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return mandate_synthesizer_node

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
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.nodes.application.planner import _extract_files_from_diff, _target_files
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)


def _mm_meta(state: GraphState) -> tuple[dict[str, Any], dict[str, Any]]:
    meta = dict(state.get("metadata", {}) or {})
    slot = dict(meta.get("mental_model", {}) or {})
    return meta, slot


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


class ContractInspectorOutput(BaseModel):
    contract_boundaries: str = Field(
        default="",
        description="Types, interfaces, public APIs, and invariants suggested by structure.",
    )


class HistoricalMinerOutput(BaseModel):
    historical_precedents: str = Field(
        default="",
        description="Conventions or precedent; cite paths or commit subjects when known.",
    )


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
        meta, slot = _mm_meta(state)
        diff_excerpt = (state.get("git_diff", "") or "")[:12000]
        goals = str(state.get("user_goals", "") or "")
        docs = str(state.get("docs_prebrief_summary", "") or "")
        llm_tokens = 0
        intent_summary = ""
        non_goals = ""
        warnings: List[str] = []

        if use_llm:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/intent_extractor.md",
                    {
                        "User goals": goals,
                        "Docs prebrief": docs,
                        "Git diff excerpt": diff_excerpt,
                    },
                )
                llm = Models.worker(IntentExtractorOutput, model_key=resolved.reviewer_worker_model_key)
                invoke_result = llm.invoke(prompt)
                out = parse_structured_output(invoke_result, IntentExtractorOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                intent_summary = out.intent_summary.strip()
                non_goals = out.non_goals.strip()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback: %s", node_name, exc)

        if not intent_summary:
            files = _extract_files_from_diff(state.get("git_diff", "") or "")
            intent_summary = (
                f"Heuristic intent: change touches {len(files)} file(s). "
                f"Review for regressions and contract fit. User goals: {goals[:800] or '(none)'}"
            ).strip()
        slot["intent_extractor"] = {
            "intent_summary": intent_summary,
            "non_goals": non_goals,
            "warnings": warnings,
        }
        meta["mental_model"] = slot
        return {
            "metadata": meta,
            "node_history": [node_name],
            "token_usage": llm_tokens,
        }

    return intent_extractor_node


def make_contract_inspector_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "contract_inspector"

    def contract_inspector_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        meta, slot = _mm_meta(state)
        graph = state.get("structural_graph_node_link") or {}
        topo = state.get("structural_topology")
        summaries = state.get("community_summaries") or []
        comm_count = len(summaries) if isinstance(summaries, list) else 0
        llm_tokens = 0
        boundaries = ""
        warnings: List[str] = []

        struct_hint = f"graph_nodes={len(graph.get('nodes', [])) if isinstance(graph, dict) else 0}; "
        struct_hint += f"community_summaries={comm_count}; topology_present={bool(topo)}"

        if use_llm:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/contract_inspector.md",
                    {
                        "Structural hint": struct_hint,
                        "Changed files": str(_target_files(state)),
                        "Git diff excerpt": (state.get("git_diff", "") or "")[:10000],
                    },
                )
                llm = Models.worker(ContractInspectorOutput, model_key=resolved.reviewer_worker_model_key)
                invoke_result = llm.invoke(prompt)
                out = parse_structured_output(invoke_result, ContractInspectorOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                boundaries = out.contract_boundaries.strip()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback: %s", node_name, exc)

        if not boundaries:
            boundaries = (
                "Heuristic contracts: treat public APIs and types in changed files as stability boundaries; "
                "verify error handling and data validation at boundaries."
            )
        slot["contract_inspector"] = {"contract_boundaries": boundaries, "warnings": warnings}
        meta["mental_model"] = slot
        return {"metadata": meta, "node_history": [node_name], "token_usage": llm_tokens}

    return contract_inspector_node


def make_historical_miner_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "historical_miner"

    def historical_miner_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        meta, slot = _mm_meta(state)
        repo_path = str(state.get("repo_path", "") or "")
        git_log = _git_recent_messages(repo_path) if repo_path else ""
        new_files = _new_files_from_diff(state.get("git_diff", "") or "")
        fallback_paths: List[str] = []
        fallback_log = ""
        if repo_path and new_files:
            fallback_paths = _fallback_paths_for_new_files(repo_path, new_files)
            fallback_log = _git_log_for_paths(repo_path, fallback_paths)
        insights = state.get("global_insights", []) or []
        gaps = state.get("knowledge_gaps", []) or []
        gap_lines: List[str] = []
        for g in gaps[:10]:
            if hasattr(g, "model_dump"):
                try:
                    gap_lines.append(str(g.model_dump()))
                except Exception:
                    gap_lines.append(str(g))
            else:
                gap_lines.append(str(g))
        llm_tokens = 0
        precedents = ""
        warnings: List[str] = []

        ctx = (
            f"Recent commits (oneline):\n{git_log or '(unavailable)'}\n\n"
            f"New files in diff: {new_files or '(none)'}\n"
            f"Sibling history fallback (bounded):\n{fallback_log or '(none)'}\n"
            f"Sibling fallback paths: {fallback_paths or '(none)'}\n\n"
            f"Global insights:\n{insights[:20]}\n\n"
            f"Knowledge gaps:\n{chr(10).join(gap_lines) if gap_lines else '(none)'}"
        )

        if use_llm:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/historical_miner.md",
                    {
                        "Repository context": ctx[:14000],
                        "Git diff excerpt": (state.get("git_diff", "") or "")[:8000],
                    },
                )
                llm = Models.worker(HistoricalMinerOutput, model_key=resolved.reviewer_worker_model_key)
                invoke_result = llm.invoke(prompt)
                out = parse_structured_output(invoke_result, HistoricalMinerOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                precedents = out.historical_precedents.strip()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback: %s", node_name, exc)

        if not precedents:
            precedents = (
                "Heuristic precedent: follow existing patterns in touched modules; "
                "prefer consistency with neighboring symbols and file organization."
            )
        slot["historical_miner"] = {
            "historical_precedents": precedents,
            "git_log_excerpt": git_log[:2000],
            "warnings": warnings,
        }
        meta["mental_model"] = slot
        return {"metadata": meta, "node_history": [node_name], "token_usage": llm_tokens}

    return historical_miner_node


def make_mandate_synthesizer_node(settings: Settings | None = None, *, use_llm: bool = True):
    node_name = "mandate_synthesizer"

    def mandate_synthesizer_node(state: GraphState) -> Dict[str, Any]:
        resolved = settings or get_settings()
        run_id = str(state.get("run_id", "unknown"))
        meta, slot = _mm_meta(state)
        intent = dict(slot.get("intent_extractor") or {})
        contract = dict(slot.get("contract_inspector") or {})
        history = dict(slot.get("historical_miner") or {})

        combined = (
            f"INTENT:\n{intent.get('intent_summary', '')}\nNON-GOALS:\n{intent.get('non_goals', '')}\n\n"
            f"CONTRACTS:\n{contract.get('contract_boundaries', '')}\n\n"
            f"HISTORY:\n{history.get('historical_precedents', '')}"
        )

        llm_tokens = 0
        expectations = ""
        risks = ""
        guidance = (
            "Treat the behavioral mandate as directional context only. "
            "Do not assume defects exist because they are hypothesized here. "
            "Prioritize direct code evidence from the diff and repository."
        )
        uncertainties = ""
        warnings: List[str] = []

        if use_llm:
            try:
                prompt = render_reviewer_prompt(
                    "mental_model/mandate_synthesizer.md",
                    {"Phase 0 inputs": combined[:20000]},
                )
                llm = Models.worker(MandateSynthesizerOutput, model_key=resolved.reviewer_worker_model_key)
                invoke_result = llm.invoke(prompt)
                out = parse_structured_output(invoke_result, MandateSynthesizerOutput)
                llm_tokens = extract_total_tokens_from_llm_result(invoke_result)
                expectations = out.behavioral_expectations.strip()
                risks = out.risk_hypotheses.strip()
                if out.reviewer_guidance.strip():
                    guidance = out.reviewer_guidance.strip()
                uncertainties = out.uncertainties.strip()
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"{node_name}_llm_fallback:{exc.__class__.__name__}")
                logger.warning("%s LLM fallback: %s", node_name, exc)

        if not expectations:
            expectations = (
                "Expect the change to preserve existing observable behavior unless the diff explicitly changes it."
            )

        evidence_refs: List[BehavioralEvidenceRef] = []
        for fp in _target_files(state)[:12]:
            evidence_refs.append(BehavioralEvidenceRef(kind="file", ref=fp, note="Changed in this PR"))

        spec = BehavioralSpec(
            intent_summary=str(intent.get("intent_summary", "")),
            behavioral_expectations=expectations,
            contract_boundaries=str(contract.get("contract_boundaries", "")),
            historical_precedents=str(history.get("historical_precedents", "")),
            risk_hypotheses=risks or "None stated; stay unbiased.",
            reviewer_guidance=guidance,
            evidence_refs=evidence_refs,
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
        }

    return mandate_synthesizer_node

"""Per-task critique pipeline: probe context, optional mental-model pull, LLM critiquer."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.config import get_settings
from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.orchestration.context.context_packets import (
    build_critique_packet,
    merge_probe_flags,
    packet_to_storage_dict,
    probe_direct_context_for_task,
)
from src.orchestration.context.task_evidence import build_task_evidence
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.critiquer import make_general_critiquer_node
from src.orchestration.routing.review_obligations import derive_review_obligations
from src.tools.mental_model_tools import query_mental_model

_MANDATE_BULLET_MAX = 5


def _task_from_state(state: GraphState) -> Optional[ReviewTask]:
    task_id = state.get("current_task_id")
    registry = state.get("task_registry", {}) or {}
    if not task_id or task_id not in registry:
        return None
    return registry[task_id]


def _mental_model_bullets(answer: str, *, max_bullets: int = _MANDATE_BULLET_MAX) -> str:
    """Normalize mandate pull to short hypothesis bullets (tier 4, non-defect)."""
    if not answer.strip():
        return ""
    lines: List[str] = []
    for raw in answer.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("##"):
            line = line.lstrip("#").strip()
        line = re.sub(r"^[-*•]\s*", "", line)
        if line:
            lines.append(line)
    if not lines:
        return f"- {answer.strip()[:600]}"
    bullets = lines[:max_bullets]
    return "\n".join(f"- {b}" for b in bullets)


def _mental_model_query_for_task(task: ReviewTask) -> str:
    files = ", ".join(task.target_files[:8]) or "(none)"
    return (
        f"For task {task.id} on files [{files}] ({task.specialty}): list contract assumptions, "
        f"risk hypotheses, and uncertainties relevant to this task only. "
        f"Do not restate full PR intent."
    )


def _should_skip_mental_model(state: GraphState) -> tuple[bool, str]:
    settings = get_settings()
    if settings.reviewer_legacy_planner_mode:
        return True, "legacy_planner_mode"
    if not state.get("behavioral_spec_ref"):
        return True, "no_behavioral_spec_ref"
    return False, ""


def make_critique_context_probe_node(context_provider: LazyReviewContextProvider):
    """Gather task-scoped repo context before mental-model pull."""

    node_name = "critique_context_probe"

    def critique_context_probe_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        context = context_provider.collect_for_critique(state=state, task=task)
        bundle = build_task_evidence(state, task, context_provider, context)
        packet = build_critique_packet(
            state,
            task,
            context,
            provider=context_provider,
            code_evidence=bundle.rendered,
            evidence_metadata=bundle.to_storage_dict(),
        )
        stored = packet_to_storage_dict(packet)
        code_evidence, probe_warnings = probe_direct_context_for_task(stored)
        if bundle.rendered.strip():
            code_evidence = bundle.rendered
            probe_warnings = [w for w in probe_warnings if w != "probe_missing_code_evidence"]
        flags = merge_probe_flags(packet)
        flags["byte_chop"] = flags.get("byte_chop") or bundle.byte_chop
        flags["files_complete"] = bundle.files_complete
        flags["symbols_included"] = bundle.symbols_included
        probe_task_warnings = list(context.warnings) + list(bundle.warnings) + probe_warnings

        meta = dict(state.get("metadata", {}) or {})
        pipe = dict(meta.get("critique_pipeline", {}) or {})
        by_task = dict(pipe.get("by_task", {}) or {})
        evidence_slot = bundle.to_storage_dict()
        by_task[task.id] = {
            "context_packet": stored,
            "task_evidence": evidence_slot,
            "direct_context": code_evidence,
            "probe_flags": flags,
            "warnings": probe_task_warnings,
            "ast_included_files": list(context.ast_included_files),
            "coverage_obligations": derive_review_obligations(task, evidence_slot),
            "ast_mode": (
                "local"
                if "ast_capability:local_enabled" in context.warnings
                else "sandbox"
                if any(
                    w in context.warnings
                    for w in ("ast_capability:sandbox_enabled", "ast_capability:sandbox_partial")
                )
                else "structural_only_remote"
                if "review_outline_source:structural_graph_fallback" in context.warnings
                else "disabled"
            ),
        }
        pipe["by_task"] = by_task
        meta["critique_pipeline"] = pipe

        if context.ast_included_files:
            prev = meta.get("ast_included_files")
            base = list(prev) if isinstance(prev, list) else []
            meta["ast_included_files"] = sorted(
                {
                    p.strip().replace("\\", "/")
                    for p in base + context.ast_included_files
                    if isinstance(p, str) and p.strip()
                }
            )

        return {
            "metadata": meta,
            "node_history": [node_name],
        }

    return critique_context_probe_node


def make_mental_model_context_enricher_node():
    """Task-scoped behavioral mandate pull after code evidence (always when spec exists)."""

    node_name = "mental_model_context_enricher"

    def mental_model_context_enricher_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        meta = dict(state.get("metadata", {}) or {})
        pipe = dict(meta.get("critique_pipeline", {}) or {})
        by_task = dict(pipe.get("by_task", {}) or {})
        slot = dict(by_task.get(task.id, {}) or {})
        ledger_patch: List[Dict[str, Any]] = []
        tokens = 0

        skip, skip_reason = _should_skip_mental_model(state)
        if skip:
            slot["mental_model_excerpt"] = ""
            slot["mental_model_skipped"] = True
            slot["mental_model_skip_reason"] = skip_reason
        else:
            result = query_mental_model(
                state=state,
                query=_mental_model_query_for_task(task),
                topic="critique_task",
                task_id=task.id,
                caller=node_name,
            )
            raw_answer = str(result.get("answer") or "")
            excerpt = _mental_model_bullets(raw_answer)
            ledger_patch = list(result.get("exploration_ledger") or [])
            slot["mental_model_excerpt"] = excerpt
            slot["mental_model_skipped"] = bool(result.get("skipped"))
            slot["mental_model_skip_reason"] = str(result.get("skip_reason") or "")
            if not excerpt.strip() and not result.get("skipped"):
                slot["mental_model_skip_reason"] = "empty_mandate_answer"

        by_task[task.id] = slot
        pipe["by_task"] = by_task
        meta["critique_pipeline"] = pipe

        out: Dict[str, Any] = {
            "metadata": meta,
            "node_history": [node_name],
            "token_usage": tokens,
        }
        if ledger_patch:
            out["exploration_ledger"] = ledger_patch
        return out

    return mental_model_context_enricher_node


# Parent graph merges parallel Send outputs in one super-step. A compiled subgraph's
# invoke() returns the full merged state (including run_id / repo_path / git_diff),
# which would write last-value channels N times — InvalidUpdateError. Only forward
# keys that use Annotated reducers on GraphState.
_CRITIQUE_PARENT_UPDATE_KEYS = frozenset(
    {
        "metadata",
        "node_history",
        "token_usage",
        "exploration_ledger",
        "candidate_findings",
        "focused_context_requests",
        "task_status_by_id",
    }
)


def _critique_subgraph_parent_updates(full: Dict[str, Any]) -> Dict[str, Any]:
    return {k: full[k] for k in _CRITIQUE_PARENT_UPDATE_KEYS if k in full}


def build_critique_review_subgraph(context_provider: LazyReviewContextProvider):
    """Compile probe → enricher → critiquer for parallel Send branches."""
    from langgraph.graph import END, START, StateGraph

    from src.domain.state import GraphState

    g = StateGraph(GraphState)
    g.add_node("critique_context_probe", make_critique_context_probe_node(context_provider))
    g.add_node("mental_model_context_enricher", make_mental_model_context_enricher_node())
    g.add_node("general_critiquer", make_general_critiquer_node(context_provider, use_pipeline_cache=True))
    g.add_edge(START, "critique_context_probe")
    g.add_edge("critique_context_probe", "mental_model_context_enricher")
    g.add_edge("mental_model_context_enricher", "general_critiquer")
    g.add_edge("general_critiquer", END)
    inner = g.compile()

    def run_critique_review_subgraph(state: GraphState) -> Dict[str, Any]:
        return _critique_subgraph_parent_updates(inner.invoke(state))

    return run_critique_review_subgraph

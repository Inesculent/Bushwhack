"""Per-task critique pipeline: probe context, optional mental-model pull, LLM critiquer."""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from src.config import get_settings
from src.domain.schemas import ReviewTask
from src.domain.state import GraphState
from src.orchestration.context.review_context import LazyReviewContextProvider
from src.orchestration.nodes.application.worker import ReviewTaskContext
from src.orchestration.nodes.application.critiquer import make_general_critiquer_node
from src.orchestration.review_principles import DECLARED_INPUT_CONTRACT_GUIDANCE
from src.tools.mental_model_tools import query_mental_model

_RISK_PATTERN = re.compile(
    r"(eval\s*\(|exec\s*\(|subprocess|os\.system|pickle\.|__import__|sql|sqlite|password|secret|token|"
    r"auth|oauth|jwt|crypto|deserialize|requests\.|urllib)",
    re.IGNORECASE,
)


def _task_from_state(state: GraphState) -> Optional[ReviewTask]:
    task_id = state.get("current_task_id")
    registry = state.get("task_registry", {}) or {}
    if not task_id or task_id not in registry:
        return None
    return registry[task_id]


def _probe_flags(direct_context: str) -> Dict[str, Any]:
    return {
        "long_context": len(direct_context) > 15000,
        "risky_keywords": bool(_RISK_PATTERN.search(direct_context)),
        "char_len": len(direct_context),
    }


def make_critique_context_probe_node(context_provider: LazyReviewContextProvider):
    """Gather direct repo context before any mental-model query."""

    node_name = "critique_context_probe"

    def critique_context_probe_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        context: ReviewTaskContext = context_provider.collect_for_task(state=state, task=task)
        rendered = f"## Review principles\n{DECLARED_INPUT_CONTRACT_GUIDANCE}\n\n{context.render()}"
        flags = _probe_flags(rendered)

        meta = dict(state.get("metadata", {}) or {})
        pipe = dict(meta.get("critique_pipeline", {}) or {})
        by_task = dict(pipe.get("by_task", {}) or {})
        by_task[task.id] = {
            "direct_context": rendered,
            "probe_flags": flags,
            "warnings": list(context.warnings),
            "ast_included_files": list(context.ast_included_files),
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


def _should_query_mental_model(flags: Dict[str, Any], task: ReviewTask) -> bool:
    settings = get_settings()
    if settings.reviewer_legacy_planner_mode:
        return False
    if flags.get("long_context") or flags.get("risky_keywords"):
        return True
    desc = f"{task.title} {task.description}".lower()
    if any(k in desc for k in ("auth", "security", "migration", "breaking", "api", "contract")):
        return True
    return False


def make_mental_model_context_enricher_node():
    """Optionally query behavioral mandate after direct context exists."""

    node_name = "mental_model_context_enricher"

    def mental_model_context_enricher_node(state: GraphState) -> Dict[str, Any]:
        task = _task_from_state(state)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        meta = dict(state.get("metadata", {}) or {})
        pipe = dict(meta.get("critique_pipeline", {}) or {})
        by_task = dict(pipe.get("by_task", {}) or {})
        slot = dict(by_task.get(task.id, {}) or {})
        flags = dict(slot.get("probe_flags") or {})
        ledger_patch: List[Dict[str, Any]] = []
        excerpt = ""
        tokens = 0

        if not _should_query_mental_model(flags, task):
            slot["mental_model_excerpt"] = ""
            slot["mental_model_skipped"] = True
            slot["mental_model_skip_reason"] = "probe_heuristic_not_met"
        else:
            q = (
                f"Summarize behavioral expectations, contracts, and reviewer guidance relevant to task "
                f"{task.id} ({task.title}) targeting files {task.target_files[:5]}."
            )
            result = query_mental_model(
                state=state,
                query=q,
                topic="critique_task",
                task_id=task.id,
                caller=node_name,
            )
            excerpt = str(result.get("answer") or "")
            ledger_patch = list(result.get("exploration_ledger") or [])
            slot["mental_model_excerpt"] = excerpt
            slot["mental_model_skipped"] = bool(result.get("skipped"))
            slot["mental_model_skip_reason"] = str(result.get("skip_reason") or "")

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

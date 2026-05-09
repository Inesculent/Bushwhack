"""Compiled verifier workflow (single-candidate invoke)."""

from __future__ import annotations

from typing import Any, Dict

from src.orchestration.nodes.verifier.verifier_runner import invoke_verifier_for_candidate


def run_verifier_invocation(input_state: Dict[str, Any]) -> Dict[str, Any]:
    """Invoke verifier from a flat dict (e.g. tests). Returns ``verifier_report`` key."""
    report = invoke_verifier_for_candidate(
        run_id=str(input_state.get("run_id", "")),
        repo_path=str(input_state.get("repo_path", "")),
        candidate=input_state["candidate_finding"],
        focused_context_snippets=str(input_state.get("focused_context_snippets", "")),
        git_diff_excerpt=str(input_state.get("git_diff_excerpt", "")),
        use_llm=bool(input_state.get("use_llm", True)),
    )
    return {"verifier_report": report.model_dump(mode="json")}


def build_verifier_graph():
    """Reserved for a future LangGraph StateGraph; callers use ``invoke_verifier_for_candidate``."""
    return None

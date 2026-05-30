"""Compiled verifier workflow (multi-node LangGraph).

While we could separate out the nodes into different files, they are relatively small and tightly coupled, so we keep them together for now. 
The main purpose of this structure is to enable more flexible routing and state management within the verifier workflow, 
while maintaining compatibility with existing interfaces.

"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from langgraph.graph import END, START, StateGraph

from src.config import get_settings
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.nodes.verifier.result_judge import (
    build_retry_feedback,
    infer_verification_scope,
    judge_attempt,
    verifier_hint_flags_for_attempts,
)
from src.orchestration.nodes.verifier.sandbox_executor import execute_test_script
from src.orchestration.nodes.verifier.test_generator import generate_test_script
from src.orchestration.nodes.verifier.verifier_runner import _infer_verifier_repo_root, _sandbox_ok

logger = logging.getLogger(__name__)


def verifier_preflight_node(state: GraphState) -> Dict[str, Any]:
    """Check if verifier is enabled/eligible and prepare context."""
    settings = get_settings()
    raw = state.get("verifier_candidate")
    if not raw:
        return {"verifier_skipped_reason": "no_candidate", "node_history": ["verifier_preflight"]}

    cand_dict = dict(raw)
    candidate_id = str(cand_dict.get("candidate_id") or "")
    scope = infer_verification_scope(cand_dict)
    repo_path = str(state.get("repo_path", ""))
    repo_root = _infer_verifier_repo_root(repo_path, settings)

    from src.orchestration.routing.verifier_fanout import focused_context_text_for_candidate

    fc = focused_context_text_for_candidate(state, candidate_id)

    if not settings.verifier_enabled:
        return {
            "verifier_skipped_reason": "verifier_disabled",
            "verifier_scope": scope,
            "verifier_repo_root": repo_root,
            "verifier_last_rationale": "Verifier disabled in settings.",
            "verifier_focused_context_text": fc,
            "node_history": ["verifier_preflight"],
        }

    if settings.verifier_skip_if_no_sandbox and not _sandbox_ok(settings):
        return {
            "verifier_skipped_reason": "no_sandbox_runtime",
            "verifier_scope": scope,
            "verifier_repo_root": repo_root,
            "verifier_last_rationale": (
                f"Sandbox runtime unavailable (backend={settings.sandbox_backend}); skipped verifier."
            ),
            "verifier_focused_context_text": fc,
            "node_history": ["verifier_preflight"],
        }

    return {
        "verifier_attempt_idx": 0,
        "verifier_retry_feedback": "",
        "verifier_scope": scope,
        "verifier_repo_root": repo_root,
        "verifier_attempts": [],
        "verifier_focused_context_text": fc,
        "node_history": ["verifier_preflight"],
    }


def verifier_generate_node(state: GraphState) -> Dict[str, Any]:
    """LLM call to generate a standalone test script."""
    settings = get_settings()
    cand_dict = dict(state["verifier_candidate"])
    attempt_idx = (state.get("verifier_attempt_idx") or 0) + 1

    git_excerpt = (state.get("git_diff", "") or "")[:8000]

    generated = generate_test_script(
        candidate=cand_dict,
        focused_context_snippets=state.get("verifier_focused_context_text", ""),
        git_diff_excerpt=git_excerpt,
        retry_feedback=state.get("verifier_retry_feedback", ""),
        repo_root=state.get("verifier_repo_root", ""),
        state=state,
        settings=settings,
        use_llm=state.get("use_llm", True),
    )
    if len(generated) == 2:
        code, tok = generated
        llm_trace = []
    else:
        code, tok, llm_trace = generated

    if not code.strip():
        return {
            "verifier_attempt_idx": attempt_idx,
            "verifier_last_rationale": "Test generation failed or returned empty code.",
            "token_usage": tok,
            "llm_trace": llm_trace,
            "node_history": ["verifier_generate"],
        }

    return {
        "verifier_attempt_idx": attempt_idx,
        "verifier_current_test_code": code,
        "token_usage": tok,
        "llm_trace": llm_trace,
        "node_history": ["verifier_generate"],
    }


def verifier_execute_node(state: GraphState) -> Dict[str, Any]:
    """Run the generated script in the Docker sandbox."""
    settings = get_settings()
    cand_dict = dict(state["verifier_candidate"])
    candidate_id = str(cand_dict.get("candidate_id") or "")

    record = execute_test_script(
        repo_path=state.get("repo_path", ""),
        candidate_id=candidate_id,
        attempt_number=state.get("verifier_attempt_idx", 1),
        test_code=state.get("verifier_current_test_code", ""),
        settings=settings,
        graph_state=state,
    )

    return {
        "verifier_attempts": [record],
        "node_history": ["verifier_execute"],
    }


def verifier_judge_node(state: GraphState) -> Dict[str, Any]:
    """Evaluate execution result and decide if retry is needed."""
    attempts = state.get("verifier_attempts", [])
    if not attempts:
        return {
            "verifier_verdict": "inconclusive",
            "verifier_last_rationale": "No attempts completed.",
            "node_history": ["verifier_judge"],
        }

    cand_dict = dict(state.get("verifier_candidate") or {})
    target_file_path = str(cand_dict.get("file_path") or "")

    record = attempts[-1]
    verdict, rationale = judge_attempt(record, target_file_path=target_file_path)

    update: Dict[str, Any] = {
        "verifier_verdict": verdict,
        "verifier_last_rationale": rationale,
        "node_history": ["verifier_judge"],
    }

    if verdict == "inconclusive":
        prior = attempts[:-1] if len(attempts) > 1 else []
        update["verifier_retry_feedback"] = build_retry_feedback(
            record,
            prior_attempts=prior,
            target_file_path=target_file_path,
        )

    return update


def verifier_finalize_node(state: GraphState) -> Dict[str, Any]:
    """Collate final report and update metadata."""
    cand_dict = dict(state["verifier_candidate"])
    candidate_id = str(cand_dict.get("candidate_id") or "")
    verdict = state.get("verifier_verdict", "inconclusive")
    rationale = state.get("verifier_last_rationale", "")
    scope = state.get("verifier_scope", "concrete_behavior")
    attempts = state.get("verifier_attempts", [])

    if state.get("verifier_skipped_reason"):
        summary = f"Verifier skipped: {state.get('verifier_skipped_reason')}"
    elif verdict != "inconclusive":
        summary = f"Runtime verifier: {verdict} ({rationale}) scope={scope} attempts={len(attempts)}"
    else:
        summary = (
            f"Runtime verifier: inconclusive after {len(attempts)} attempt(s). {rationale}"
        )

    report = VerifierReport(
        run_id=state.get("run_id", ""),
        candidate_id=candidate_id,
        verdict=verdict,
        verification_scope=scope,
        final_rationale=rationale,
        updated_evidence_summary=summary,
        attempts=attempts,
        skipped_reason=state.get("verifier_skipped_reason", ""),
        metadata={
            # Branch payloads zero parent token_usage; this is subgraph-only LLM usage.
            "llm_tokens": int(state.get("token_usage") or 0),
            "verifier_repo_root": state.get("verifier_repo_root", ""),
        },
    )

    from src.orchestration.nodes.verifier.failure_class import verifier_confidence_label
    from src.orchestration.routing.verifier_fanout import _lint_advisory_from_report

    meta = dict(state.get("metadata") or {})
    hints = dict(meta.get("verifier_hints") or {})
    target_file_path = str(cand_dict.get("file_path") or "")
    hint_flags = verifier_hint_flags_for_attempts(
        verdict=report.verdict,
        attempts=report.attempts,
        target_file_path=target_file_path,
    )
    harness_error = hint_flags["harness_error"]
    product_verified = hint_flags["product_verified"]
    last_attempt = report.attempts[-1] if report.attempts else None
    confidence = verifier_confidence_label(
        cand_dict,
        verifier_verdict=report.verdict,
        verification_scope=report.verification_scope,
        harness_error=harness_error,
        product_verified=product_verified,
        stdout=last_attempt.stdout if last_attempt is not None else "",
        stderr=last_attempt.stderr if last_attempt is not None else "",
    )
    hints[report.candidate_id] = {
        "verdict": report.verdict,
        "verification_scope": report.verification_scope,
        "updated_evidence_summary": report.updated_evidence_summary,
        "final_rationale": report.final_rationale,
        "attempts": len(report.attempts),
        "skipped_reason": report.skipped_reason,
        "lint_advisory": _lint_advisory_from_report(report),
        "harness_error": harness_error,
        "product_verified": product_verified,
        "confidence": confidence,
    }
    meta["verifier_hints"] = hints
    vrun = dict(meta.get("verifier") or {})
    by_c = dict(vrun.get("by_candidate") or {})
    by_c[report.candidate_id] = report.model_dump(mode="json")
    vrun["by_candidate"] = by_c
    meta["verifier"] = vrun

    return {
        "verifier_reports": [report],
        "metadata": meta,
        "node_history": ["verifier_finalize"],
    }


def verifier_routing(state: GraphState) -> str:
    """Decide next step in the verifier loop."""
    if state.get("verifier_skipped_reason"):
        return "finalize"

    if state.get("verifier_verdict") and state.get("verifier_verdict") != "inconclusive":
        return "finalize"

    settings = get_settings()
    if (state.get("verifier_attempt_idx") or 0) >= settings.verifier_max_attempts:
        return "finalize"

    if (
        state.get("verifier_last_rationale")
        == "Test generation failed or returned empty code."
    ):
        return "finalize"

    return "generate"


def build_verifier_graph():
    """Compile the multi-node verifier subgraph."""
    builder = StateGraph(GraphState)

    builder.add_node("verifier_preflight", verifier_preflight_node)
    builder.add_node("verifier_generate", verifier_generate_node)
    builder.add_node("verifier_execute", verifier_execute_node)
    builder.add_node("verifier_judge", verifier_judge_node)
    builder.add_node("verifier_finalize", verifier_finalize_node)

    builder.add_edge(START, "verifier_preflight")
    builder.add_conditional_edges(
        "verifier_preflight",
        verifier_routing,
        {"generate": "verifier_generate", "finalize": "verifier_finalize"},
    )
    builder.add_edge("verifier_generate", "verifier_execute")
    builder.add_edge("verifier_execute", "verifier_judge")
    builder.add_conditional_edges(
        "verifier_judge",
        verifier_routing,
        {"generate": "verifier_generate", "finalize": "verifier_finalize"},
    )
    builder.add_edge("verifier_finalize", END)

    return builder.compile()


def run_verifier_invocation(input_state: Dict[str, Any]) -> Dict[str, Any]:
    """Compatibility wrapper: invoke the graph instead of the monolithic runner."""
    # Ensure keys needed by nodes are in the state
    if "verifier_candidate" not in input_state and "candidate_finding" in input_state:
        input_state["verifier_candidate"] = input_state["candidate_finding"]

    graph = build_verifier_graph()
    result = graph.invoke(input_state)

    # Return structure expected by existing callers (e.g. tests)
    if result.get("verifier_reports"):
        report = result["verifier_reports"][-1]
        return {"verifier_report": report.model_dump(mode="json")}
    return {"verifier_report": None}

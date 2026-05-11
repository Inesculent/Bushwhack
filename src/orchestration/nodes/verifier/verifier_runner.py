"""Single-candidate verifier orchestration (retry loop inside one invoke)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

import docker

from src.config import Settings, get_settings
from src.domain.schemas import CandidateFinding
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport
from src.orchestration.nodes.verifier.result_judge import (
    build_retry_feedback,
    infer_verification_scope,
    judge_attempt,
)
from src.orchestration.nodes.verifier.sandbox_executor import execute_test_script
from src.orchestration.nodes.verifier.test_generator import generate_test_script

logger = logging.getLogger(__name__)


def _docker_ok() -> bool:
    try:
        client = docker.from_env()
        return bool(client.ping())
    except Exception:  # noqa: BLE001
        return False


def _coerce_candidate_dict(candidate: Dict[str, Any] | CandidateFinding) -> Dict[str, Any]:
    if isinstance(candidate, CandidateFinding):
        return candidate.model_dump(mode="json")
    return dict(candidate)


def _infer_verifier_repo_root(repo_path: str, settings: Settings) -> str:
    raw = (repo_path or "").strip()
    if raw and Path(raw).is_dir():
        return "/repo"
    if settings.verifier_clone_remote_in_container:
        return "/repo"
    return "/workspace"


def invoke_verifier_for_candidate(
    *,
    run_id: str,
    repo_path: str,
    candidate: Dict[str, Any] | CandidateFinding,
    focused_context_snippets: str,
    git_diff_excerpt: str,
    settings: Optional[Settings] = None,
    use_llm: bool = True,
    graph_state: GraphState | None = None,
) -> VerifierReport:
    """Run up to ``verifier_max_attempts`` generate/execute/judge cycles for one candidate."""
    settings = settings or get_settings()
    cand_dict = _coerce_candidate_dict(candidate)
    candidate_id = str(cand_dict.get("candidate_id") or "")
    scope = infer_verification_scope(cand_dict)
    repo_root = _infer_verifier_repo_root(repo_path, settings)

    if not settings.verifier_enabled:
        return VerifierReport(
            run_id=run_id,
            candidate_id=candidate_id,
            verdict="inconclusive",
            verification_scope=scope,
            final_rationale="Verifier disabled in settings.",
            skipped_reason="verifier_disabled",
            metadata={"llm_tokens": 0, "verifier_repo_root": repo_root},
        )

    if settings.verifier_skip_if_no_docker and not _docker_ok():
        return VerifierReport(
            run_id=run_id,
            candidate_id=candidate_id,
            verdict="inconclusive",
            verification_scope=scope,
            final_rationale="Docker unavailable; skipped verifier.",
            skipped_reason="no_docker",
            metadata={"llm_tokens": 0, "verifier_repo_root": repo_root},
        )

    attempts = []
    retry_feedback = ""
    total_tokens = 0
    last_rationale = ""

    for attempt_idx in range(1, settings.verifier_max_attempts + 1):
        code, tok = generate_test_script(
            candidate=cand_dict,
            focused_context_snippets=focused_context_snippets,
            git_diff_excerpt=git_diff_excerpt,
            retry_feedback=retry_feedback,
            repo_root=repo_root,
            settings=settings,
            use_llm=use_llm,
        )
        total_tokens += tok
        if not code.strip():
            last_rationale = "Test generation failed or returned empty code."
            break

        record = execute_test_script(
            repo_path=repo_path,
            candidate_id=candidate_id,
            attempt_number=attempt_idx,
            test_code=code,
            settings=settings,
            graph_state=graph_state,
        )
        verdict, rationale = judge_attempt(record)
        last_rationale = rationale
        attempts.append(record)

        if verdict != "inconclusive":
            summary = (
                f"Runtime verifier: {verdict} ({rationale}) scope={scope} attempts={attempt_idx}"
            )
            return VerifierReport(
                run_id=run_id,
                candidate_id=candidate_id,
                verdict=verdict,
                verification_scope=scope,
                final_rationale=rationale,
                updated_evidence_summary=summary,
                attempts=attempts,
                metadata={"llm_tokens": total_tokens, "verifier_repo_root": repo_root},
            )

        retry_feedback = build_retry_feedback(record)

    summary = f"Runtime verifier: inconclusive after {len(attempts)} attempt(s). {last_rationale}"
    return VerifierReport(
        run_id=run_id,
        candidate_id=candidate_id,
        verdict="inconclusive",
        verification_scope=scope,
        final_rationale=last_rationale or "No attempts completed.",
        updated_evidence_summary=summary,
        attempts=attempts,
        metadata={"llm_tokens": total_tokens, "verifier_repo_root": repo_root},
    )

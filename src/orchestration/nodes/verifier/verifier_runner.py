"""Single-candidate verifier orchestration (retry loop inside one invoke)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, Optional

from src.config import Settings, get_settings
from src.infrastructure.sandbox import sandbox_runtime_available
from src.domain.schemas import CandidateFinding
from src.domain.state import GraphState
from src.domain.verifier_schemas import VerifierReport, VerifierVerdict
from src.orchestration.nodes.verifier.result_judge import (
    build_retry_feedback,
    infer_verification_scope,
    judge_attempt,
    verifier_env_diagnostics_for_attempts,
    verifier_hint_flags_for_attempts,
)
from src.orchestration.nodes.verifier.sandbox_executor import execute_test_script
from src.orchestration.nodes.verifier.test_generator import generate_test_script

logger = logging.getLogger(__name__)


def _sandbox_ok(settings: Settings | None = None) -> bool:
    resolved = settings or get_settings()
    return sandbox_runtime_available(resolved)


def _coerce_candidate_dict(candidate: Dict[str, Any] | CandidateFinding) -> Dict[str, Any]:
    if isinstance(candidate, CandidateFinding):
        return candidate.model_dump(mode="json")
    return dict(candidate)


def _execution_workspace_root(settings: Settings, workspace_name: str = "verify_exec") -> str:
    if getattr(settings, "sandbox_backend", "") == "apptainer":
        return f"/tmp/{workspace_name}"
    return f"/{workspace_name}"


def _infer_verifier_repo_root(repo_path: str, settings: Settings) -> str:
    raw = (repo_path or "").strip()
    if raw and Path(raw).is_dir():
        if settings.verifier_use_execution_workspace:
            return _execution_workspace_root(settings)
        return "/repo"
    if settings.verifier_clone_remote_in_container:
        if settings.verifier_use_execution_workspace:
            return _execution_workspace_root(settings)
        return "/repo"
    if settings.verifier_require_repo_in_container:
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

    if settings.verifier_skip_if_no_sandbox and not _sandbox_ok(settings):
        return VerifierReport(
            run_id=run_id,
            candidate_id=candidate_id,
            verdict="inconclusive",
            verification_scope=scope,
            final_rationale=(
                f"Sandbox runtime unavailable (backend={settings.sandbox_backend}); skipped verifier."
            ),
            skipped_reason="no_sandbox_runtime",
            metadata={"llm_tokens": 0, "verifier_repo_root": repo_root},
        )

    attempts: list = []
    retry_feedback = ""
    total_tokens = 0
    last_rationale = ""
    last_verdict: VerifierVerdict = "inconclusive"
    target_file_path = str(cand_dict.get("file_path") or "")

    def _report_metadata(verdict: VerifierVerdict) -> Dict[str, Any]:
        flags = verifier_hint_flags_for_attempts(
            verdict=verdict,
            attempts=attempts,
            target_file_path=target_file_path,
        )
        env_diagnostics = verifier_env_diagnostics_for_attempts(
            attempts,
            target_file_path=target_file_path,
        )
        env_hints_used = bool(
            env_diagnostics["missing_modules"]
            or env_diagnostics["failed_target_import_probes"]
            or env_diagnostics["repeated_harness_error_count"] >= 2
        )
        return {
            "llm_tokens": total_tokens,
            "verifier_repo_root": repo_root,
            "harness_error": flags["harness_error"],
            "product_verified": flags["product_verified"],
            "verifier_env_repair_hints_used": env_hints_used,
            "verifier_repeated_harness_error_count": env_diagnostics["repeated_harness_error_count"],
            "verifier_unrepaired_missing_modules": env_diagnostics["missing_modules"],
        }

    for attempt_idx in range(1, settings.verifier_max_attempts + 1):
        generated = generate_test_script(
            candidate=cand_dict,
            focused_context_snippets=focused_context_snippets,
            git_diff_excerpt=git_diff_excerpt,
            retry_feedback=retry_feedback,
            repo_root=repo_root,
            settings=settings,
            use_llm=use_llm,
        )
        if len(generated) == 2:
            code, tok = generated
            _trace = []
        else:
            code, tok, _trace = generated
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
        verdict, rationale = judge_attempt(record, target_file_path=target_file_path)
        last_rationale = rationale
        last_verdict = verdict
        attempts.append(record)

        if verdict != "inconclusive":
            metadata = _report_metadata(verdict)
            report_verdict = verdict
            report_rationale = rationale
            if verdict == "verified" and not metadata.get("product_verified"):
                report_verdict = "inconclusive"
                report_rationale = (
                    f"{rationale} Harness/setup errors were present or product proof was not clean; "
                    "treating verifier signal as advisory, not product verification."
                )
            summary = (
                f"Runtime verifier: {report_verdict} ({report_rationale}) "
                f"scope={scope} attempts={attempt_idx}"
            )
            return VerifierReport(
                run_id=run_id,
                candidate_id=candidate_id,
                verdict=report_verdict,
                verification_scope=scope,
                final_rationale=report_rationale,
                updated_evidence_summary=summary,
                attempts=attempts,
                metadata=metadata,
            )

        retry_feedback = build_retry_feedback(
            record,
            prior_attempts=attempts[:-1],
            target_file_path=target_file_path,
        )

    summary = f"Runtime verifier: inconclusive after {len(attempts)} attempt(s). {last_rationale}"
    return VerifierReport(
        run_id=run_id,
        candidate_id=candidate_id,
        verdict="inconclusive",
        verification_scope=scope,
        final_rationale=last_rationale or "No attempts completed.",
        updated_evidence_summary=summary,
        attempts=attempts,
        metadata=_report_metadata(last_verdict),
    )

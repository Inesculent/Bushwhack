"""Schemas for the external self-healing verifier subgraph (runtime proof attempts)."""

from __future__ import annotations

from enum import Enum
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class VerificationStatus(str, Enum):
    PENDING = "pending"
    GENERATING_TEST = "generating_test"
    EXECUTING = "executing"
    JUDGING = "judging"
    RETRYING = "retrying"
    COMPLETED = "completed"
    FAILED = "failed"


VerifierVerdict = Literal["verified", "refuted", "inconclusive"]
VerificationScope = Literal["concrete_behavior", "abstract_or_unverifiable"]

LintToolName = Literal["ruff", "flake8"]


class VerifierLintRun(BaseModel):
    """Optional linter output captured during a verifier attempt (advisory)."""

    tool: LintToolName
    command: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""


class VerifierAttemptRecord(BaseModel):
    """One execution attempt for a candidate."""

    attempt_number: int
    test_code: str = ""
    exit_code: Optional[int] = None
    stdout: str = ""
    stderr: str = ""
    timeout: bool = False
    execution_time_seconds: float = 0.0
    status: VerificationStatus = VerificationStatus.PENDING
    lint_runs: List[VerifierLintRun] = Field(default_factory=list)
    sandbox_mode: str = ""
    repo_root: str = ""


class VerifierReport(BaseModel):
    """Merged output from one verifier invocation (one candidate)."""

    run_id: str
    candidate_id: str
    verdict: VerifierVerdict = "inconclusive"
    verification_scope: VerificationScope = "concrete_behavior"
    final_rationale: str = ""
    updated_evidence_summary: str = ""
    attempts: List[VerifierAttemptRecord] = Field(default_factory=list)
    skipped_reason: str = ""
    metadata: Dict[str, Any] = Field(default_factory=dict)


class VerifierTestGeneratorOutput(BaseModel):
    """Structured LLM output for standalone verification script."""

    test_code: str = Field(description="Standalone Python script only, no markdown.")

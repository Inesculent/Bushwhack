"""Tests for critique revision on reject + focused context."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, FocusedContextResult, ReflectionReport
from src.domain.state import GraphState
from src.orchestration.nodes.application.critique_revision import _needs_revision_candidates


def test_needs_revision_includes_logic_reject_with_focus_and_tier1() -> None:
    cid = "logic-4-2"
    state: GraphState = {
        "candidate_findings": [
            CandidateFinding(
                candidate_id=cid,
                patch_task_id="4",
                file_path="src/x.py",
                line_start=1,
                line_end=5,
                content="re.findall",
                claim_type="defect",
                failure_mode="findall tuple indexing loses capturing groups",
                evidence_summary="uses m[0]",
                recommendation="fix",
                reflection_specialties=["logic"],
                suspected_category="logic",
            )
        ],
        "reflection_reports": [
            ReflectionReport(
                candidate_id=cid,
                reflector_specialty="logic",
                verdict="reject",
                rationale="Incorrect stdlib claim.",
            )
        ],
        "focused_context_results": {
            "ctx": FocusedContextResult(
                request_id="ctx",
                candidate_id=cid,
                file_snippets={"src/x.py": "matches = re.findall(...)"},
            )
        },
    }
    assert cid in _needs_revision_candidates(state)

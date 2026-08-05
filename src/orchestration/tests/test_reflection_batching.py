"""Tests for adversarial reflection batching helpers."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, ReflectionBatchOutput, ReflectionReport
from src.orchestration.nodes.application.reflection import _chunk_candidates, _normalize_reports


def _cand(cid: str) -> CandidateFinding:
    return CandidateFinding(
        candidate_id=cid,
        patch_task_id="1",
        file_path="a.py",
        line_start=1,
        line_end=2,
        content="issue",
        claim_type="defect",
        failure_mode="wrong branch when mode unexpected",
        evidence_summary="no else return",
        recommendation="add else",
        reflection_specialties=["logic"],
        suspected_category="logic",
    )


def test_chunk_candidates_splits_by_batch_size() -> None:
    items = [_cand(f"c{i}") for i in range(5)]
    chunks = _chunk_candidates(items, 3)
    assert len(chunks) == 2
    assert len(chunks[0]) == 3
    assert len(chunks[1]) == 2


def test_normalize_reports_remaps_needs_context_without_request() -> None:
    batch = ReflectionBatchOutput(
        reports=[
            ReflectionReport(
                candidate_id="c1",
                reflector_specialty="logic",
                verdict="needs_context",
                rationale="Need to confirm whether group_index=0 is intended to mean the full match.",
                focused_request=None,
                support_scope="needs_context",
            )
        ]
    )
    reports, requests, warnings = _normalize_reports(
        {"repo_path": "/repo", "git_diff": "", "metadata": {}},
        batch,
        "logic",
        batch_candidates=[_cand("c1")],
    )
    assert requests == []
    assert len(reports) == 1
    assert reports[0].verdict == "needs_verification"
    assert reports[0].support_scope == "needs_context" or reports[0].support_scope == "runtime_dependent"
    assert any("reflection_needs_context_without_request:c1" in item for item in warnings)
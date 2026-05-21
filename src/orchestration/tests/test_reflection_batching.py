"""Tests for adversarial reflection batching helpers."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding
from src.orchestration.nodes.application.reflection import _chunk_candidates


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

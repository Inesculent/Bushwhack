"""Normalize critiquer LLM output: ids/patch task + single reflection specialty hardcap."""

from __future__ import annotations

from typing import List

from src.domain.schemas import CandidateFinding, ReviewTask
from src.orchestration.routing.candidate_reflection_specialty import (
    correct_specialty_before_hardcap,
    with_single_reflection_specialty,
)


def _maybe_retag_findall_first_group_loss(candidate: CandidateFinding) -> CandidateFinding:
    """findall tuples indexed with m[0] are correctness defects, not perf-only."""
    blob = " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
        ]
    ).lower()
    if candidate.claim_type != "performance_regression":
        return candidate
    if "findall" not in blob or "m[0]" not in blob:
        return candidate
    return candidate.model_copy(
        update={
            "claim_type": "defect",
            "reflection_specialties": ["logic"],
            "suspected_category": "logic",
        }
    )


def normalize_critiquer_candidates(task: ReviewTask, candidates: List[CandidateFinding]) -> List[CandidateFinding]:
    """Apply stable ids and collapse ``reflection_specialties`` to exactly one domain."""
    normalized: List[CandidateFinding] = []
    for index, cand in enumerate(candidates, start=1):
        cid = cand.candidate_id.strip() or f"{task.id}:c{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        with_ids = cand.model_copy(
            update={
                "candidate_id": cid,
                "patch_task_id": task.id,
            }
        )
        corrected, _ = correct_specialty_before_hardcap(with_ids)
        retagged = _maybe_retag_findall_first_group_loss(corrected)
        normalized.append(with_single_reflection_specialty(retagged))
    return normalized

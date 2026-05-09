"""Normalize critiquer LLM output: ids/patch task + single reflection specialty hardcap."""

from __future__ import annotations

from typing import List

from src.domain.schemas import CandidateFinding, ReviewTask
from src.orchestration.routing.candidate_reflection_specialty import with_single_reflection_specialty


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
        normalized.append(with_single_reflection_specialty(with_ids))
    return normalized

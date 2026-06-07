"""Normalize critiquer LLM output: ids/patch task + single reflection specialty hardcap."""

from __future__ import annotations

from typing import List, Mapping

from src.domain.schemas import CandidateFinding, ReviewTask
from src.orchestration.routing.candidate_line_anchor import apply_line_anchor_policy
from src.orchestration.routing.candidate_reflection_specialty import (
    correct_specialty_before_hardcap,
    with_single_reflection_specialty,
)
from src.orchestration.routing.finding_dedupe import (
    candidate_with_behavioral_metadata,
    dedupe_candidates_by_signature,
    ensure_unique_candidate_ids,
    extract_subject_class,
)

_STRUCTURED_EXTRACTION_TASK_IDS = frozenset({"review-logic-structured-extraction"})
_STRUCTURED_TASK_TITLE_MARKER = "structured extraction"


def _ensure_subject_in_content(candidate: CandidateFinding) -> CandidateFinding:
    if extract_subject_class(candidate.content):
        return candidate
    subject = extract_subject_class(
        candidate.failure_mode,
        candidate.evidence_summary,
        candidate.recommendation or "",
    )
    if not subject:
        return candidate
    body = (candidate.content or "").strip()
    if body.lower().startswith(f"class {subject.lower()}"):
        return candidate
    return candidate.model_copy(update={"content": f"class {subject}: {body}"[:600]})


def _is_structured_extraction_task(task: ReviewTask) -> bool:
    if task.id in _STRUCTURED_EXTRACTION_TASK_IDS:
        return True
    return _STRUCTURED_TASK_TITLE_MARKER in f"{task.title} {task.description}".lower()


def _out_of_scope_for_structured_task(candidate: CandidateFinding) -> bool:
    """Branch-exhaustiveness claims belong on diff-local tasks, not structured-extraction."""
    normalized = candidate_with_behavioral_metadata(candidate)
    return (
        normalized.behavioral_symptom == "missing_return"
        and normalized.root_operation == "dispatch"
    )


def normalize_critiquer_candidates(
    task: ReviewTask,
    candidates: List[CandidateFinding],
    *,
    file_contents: Mapping[str, str] | None = None,
    git_diff: str = "",
) -> tuple[List[CandidateFinding], List[str], dict[str, list[str]]]:
    """Apply stable ids, line-anchor repair/drop, and collapse reflection specialties."""
    warnings: List[str] = []
    normalized: List[CandidateFinding] = []
    structured_task = _is_structured_extraction_task(task)
    for index, cand in enumerate(candidates, start=1):
        with_behavior = candidate_with_behavioral_metadata(cand)
        if structured_task and _out_of_scope_for_structured_task(with_behavior):
            warnings.append(
                f"{task.id}:c{index}:structured_task_scope_drop:missing_return_dispatch"
            )
            continue
        cid = with_behavior.candidate_id.strip() or f"{task.id}:c{index}"
        if not cid.startswith(task.id):
            cid = f"{task.id}:{cid}"
        with_ids = with_behavior.model_copy(
            update={
                "candidate_id": cid,
                "patch_task_id": task.id,
            }
        )
        corrected, _ = correct_specialty_before_hardcap(with_ids)
        anchored_content = _ensure_subject_in_content(corrected)
        completed = candidate_with_behavioral_metadata(anchored_content)
        normalized.append(with_single_reflection_specialty(completed))

    normalized = ensure_unique_candidate_ids(normalized)

    anchored, anchor_warnings, dropped_ids = apply_line_anchor_policy(
        normalized,
        file_contents=file_contents,
        git_diff=git_diff,
    )
    warnings.extend(anchor_warnings)
    if dropped_ids:
        warnings.append(f"line_anchor_dropped:{','.join(dropped_ids)}")

    deduped, duplicate_map = dedupe_candidates_by_signature(anchored, git_diff=git_diff)
    if duplicate_map:
        warnings.append(
            f"semantic_dedupe_collapsed:{sum(len(v) for v in duplicate_map.values())}"
        )
    return deduped, warnings, duplicate_map

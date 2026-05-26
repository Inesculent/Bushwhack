"""Validate and repair candidate line ranges against cited class/method in file evidence."""

from __future__ import annotations

import re
from typing import Mapping, Optional, Tuple

from src.domain.schemas import CandidateFinding
from src.orchestration.routing.finding_dedupe import (
    class_def_lines_from_diff,
    extract_subject_class,
    extract_subject_class_from_claim,
)

_CLASS_DEF_RE = re.compile(r"^class\s+([A-Za-z_][A-Za-z0-9_]*)\s*[:\(]", re.MULTILINE)


def class_line_range_in_file(file_text: str, class_name: str) -> Optional[Tuple[int, int]]:
    """1-based inclusive line range for ``class ClassName`` through the next top-level class or EOF."""
    if not file_text.strip() or not class_name:
        return None
    lines = file_text.splitlines()
    start: Optional[int] = None
    for idx, line in enumerate(lines, start=1):
        match = _CLASS_DEF_RE.match(line.strip())
        if match and match.group(1) == class_name:
            start = idx
            break
    if start is None:
        return None
    end = len(lines)
    for idx in range(start, len(lines)):
        if idx > start and _CLASS_DEF_RE.match(lines[idx - 1].strip()):
            end = idx - 1
            break
    return start, end


def class_line_range_with_tail(
    file_text: str,
    class_name: str,
    *,
    tail_lines: int = 3,
) -> Optional[Tuple[int, int]]:
    """Inclusive 1-based range for a class, extended up to ``tail_lines`` without crossing the next class."""
    base = class_line_range_in_file(file_text, class_name)
    if base is None:
        return None
    start, end = base
    if tail_lines <= 0:
        return start, end
    lines = file_text.splitlines()
    extended = end
    for line_no in range(end + 1, min(len(lines), end + tail_lines) + 1):
        if _CLASS_DEF_RE.match(lines[line_no - 1].strip()):
            break
        extended = line_no
    return start, extended


def class_at_line(file_text: str, line: int) -> Optional[str]:
    """Name of the class whose body contains ``line`` (1-based), if any."""
    if not file_text.strip() or line < 1:
        return None
    current: Optional[str] = None
    for idx, raw in enumerate(file_text.splitlines(), start=1):
        match = _CLASS_DEF_RE.match(raw.strip())
        if match:
            current = match.group(1)
        if idx == line:
            return current
    return current


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


def anchor_candidate_lines(
    candidate: CandidateFinding,
    *,
    file_text: str = "",
    git_diff: str = "",
) -> Tuple[Optional[CandidateFinding], Optional[str]]:
    """
    Return (candidate, None) when lines bracket the cited class (possibly after repair).

    Return (None, reason) when the anchor is irreparable and the candidate should be dropped.
    """
    claim_subject = extract_subject_class_from_claim(
        candidate.content or "",
        candidate.failure_mode or "",
        candidate.evidence_summary or "",
    )
    subject = claim_subject or extract_subject_class(candidate.recommendation or "")
    if not subject:
        return candidate, None

    class_range = class_line_range_in_file(file_text, subject) if file_text.strip() else None
    ls, le = int(candidate.line_start or 1), int(candidate.line_end or candidate.line_start or 1)

    if class_range is not None:
        c_start, c_end = class_range
        if _ranges_overlap(ls, le, c_start, c_end):
            if subject.lower() not in (candidate.content or "").lower():
                return (
                    candidate.model_copy(update={"content": f"class {subject}:"}),
                    None,
                )
            return candidate, None
        at_line = class_at_line(file_text, ls) if file_text.strip() else None
        if claim_subject and at_line and at_line != claim_subject:
            return (
                None,
                f"line_anchor_class_mismatch:cited={claim_subject}:at_line={at_line}:lines={ls}-{le}",
            )
        return (
            None,
            f"line_anchor_class_mismatch:cited={subject}:lines={ls}-{le}:class_range={c_start}-{c_end}",
        )

    if git_diff:
        anchors = class_def_lines_from_diff(git_diff)
        anchor = anchors.get(subject)
        if anchor is not None and abs(ls - anchor) <= 120:
            return candidate, None
        if anchor is not None:
            return None, f"line_anchor_mismatch:{subject}:lines={ls}-{le}:anchor={anchor}"

    if file_text.strip():
        at_line = class_at_line(file_text, ls)
        if at_line and at_line != subject:
            return None, f"line_anchor_wrong_class:cited={subject}:at_line={at_line}"

    return candidate, None


def apply_line_anchor_policy(
    candidates: list[CandidateFinding],
    *,
    file_contents: Mapping[str, str] | None = None,
    git_diff: str = "",
) -> tuple[list[CandidateFinding], list[str], list[str]]:
    """Repair or drop candidates whose line ranges do not match the cited class."""
    kept: list[CandidateFinding] = []
    warnings: list[str] = []
    dropped: list[str] = []
    files = file_contents or {}

    for cand in candidates:
        fp = (cand.file_path or "").replace("\\", "/").lstrip("/")
        file_text = ""
        for key, body in files.items():
            norm = key.replace("\\", "/").lstrip("/")
            if norm == fp or norm.endswith("/" + fp):
                file_text = body
                break

        fixed, drop_reason = anchor_candidate_lines(
            cand,
            file_text=file_text,
            git_diff=git_diff,
        )
        if drop_reason:
            dropped.append(cand.candidate_id)
            warnings.append(f"{cand.candidate_id}:{drop_reason}")
            continue
        if fixed is None:
            dropped.append(cand.candidate_id)
            warnings.append(f"{cand.candidate_id}:line_anchor_unresolved")
            continue
        if (
            fixed.line_start != cand.line_start
            or fixed.line_end != cand.line_end
        ):
            warnings.append(
                f"{cand.candidate_id}:line_anchor_repaired:"
                f"{cand.line_start}-{cand.line_end}->"
                f"{fixed.line_start}-{fixed.line_end}"
            )
        kept.append(fixed)

    return kept, warnings, dropped

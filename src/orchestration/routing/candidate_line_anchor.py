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
_FUNC_DEF_RE = re.compile(r"^def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", re.MULTILINE)
_QUALIFIED_METHOD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")


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


def method_name_from_claim(*parts: str) -> Optional[str]:
    blob = " ".join(part for part in parts if part)
    match = _QUALIFIED_METHOD_RE.search(blob)
    if match and match.group(1)[:1].isupper():
        return match.group(2)
    return None


def function_line_range_in_scope(
    file_text: str,
    function_name: str,
    *,
    scope_start: int = 1,
    scope_end: int | None = None,
) -> Optional[Tuple[int, int]]:
    if not file_text.strip() or not function_name:
        return None
    lines = file_text.splitlines()
    end_limit = scope_end or len(lines)
    start: Optional[int] = None
    start_indent = 0
    for idx in range(max(1, scope_start), min(len(lines), end_limit) + 1):
        raw = lines[idx - 1]
        match = _FUNC_DEF_RE.match(raw.strip())
        if match and match.group(1) == function_name:
            start = idx
            start_indent = len(raw) - len(raw.lstrip(" "))
            break
    if start is None:
        return None
    end = end_limit
    for idx in range(start + 1, min(len(lines), end_limit) + 1):
        raw = lines[idx - 1]
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= start_indent and (_FUNC_DEF_RE.match(stripped) or _CLASS_DEF_RE.match(stripped)):
            end = idx - 1
            break
    return start, end


def _ranges_overlap(a_start: int, a_end: int, b_start: int, b_end: int) -> bool:
    return a_start <= b_end and b_start <= a_end


_MODE_COMPARISON_RE = re.compile(r"\bmode\s*==\s*['\"]([^'\"]{2,80})['\"]", re.IGNORECASE)


def _line_slice(file_text: str, line_start: int, line_end: int) -> str:
    if not file_text.strip() or line_start < 1 or line_end < line_start:
        return ""
    lines = file_text.splitlines()
    return "\n".join(lines[line_start - 1 : min(len(lines), line_end)])


def _branch_terms_from_claim(*parts: str) -> list[str]:
    blob = " ".join(part for part in parts if part)
    lowered = blob.lower()
    if not any(marker in lowered for marker in ("branch", "mode", "return", "falls through", "fall through")):
        return []
    terms = [term.strip() for term in _MODE_COMPARISON_RE.findall(blob) if term.strip()]
    for match in re.finditer(
        r"['\"]([^'\"]{2,80})['\"].{0,80}?(?:branch|mode|return|falls?\s+through)",
        blob,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        terms.append(match.group(1).strip())
    return list(dict.fromkeys(terms))


def _candidate_range_misses_claimed_branch(
    file_text: str,
    candidate: CandidateFinding,
    class_start: int,
    class_end: int,
) -> bool:
    terms = _branch_terms_from_claim(
        candidate.content or "",
        candidate.failure_mode or "",
        candidate.evidence_summary or "",
        candidate.recommendation or "",
    )
    if not terms:
        return False
    current = _line_slice(file_text, int(candidate.line_start or 1), int(candidate.line_end or 1)).lower()
    class_body = _line_slice(file_text, class_start, class_end).lower()
    return any(term.lower() in class_body and term.lower() not in current for term in terms)


def _branch_block_for_term(file_text: str, scope_start: int, scope_end: int, term: str) -> str:
    lines = file_text.splitlines()
    term_lower = term.lower()
    start: Optional[int] = None
    start_indent = 0
    for idx in range(max(1, scope_start), min(len(lines), scope_end) + 1):
        raw = lines[idx - 1]
        if term_lower not in raw.lower():
            continue
        if not _MODE_COMPARISON_RE.search(raw):
            continue
        start = idx
        start_indent = len(raw) - len(raw.lstrip(" "))
        break
    if start is None:
        return ""
    end = scope_end
    for idx in range(start + 1, min(len(lines), scope_end) + 1):
        raw = lines[idx - 1]
        stripped = raw.strip()
        if not stripped:
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        if indent <= start_indent and (
            stripped.startswith(("elif ", "else:"))
            or _FUNC_DEF_RE.match(stripped)
            or _CLASS_DEF_RE.match(stripped)
        ):
            end = idx - 1
            break
    return _line_slice(file_text, start, end)


def _method_lacks_terminal_fallback(file_text: str, method_start: int, method_end: int) -> bool:
    body = _line_slice(file_text, method_start, method_end)
    has_branch_chain = bool(re.search(r"^\s*elif\s+", body, flags=re.MULTILINE))
    has_terminal_else = bool(re.search(r"^\s*else\s*:", body, flags=re.MULTILINE))
    return has_branch_chain and not has_terminal_else


def _branch_fallthrough_claim_repair(
    file_text: str,
    candidate: CandidateFinding,
    *,
    class_name: str,
    method_name: str,
    method_start: int,
    method_end: int,
) -> dict[str, object]:
    blob = " ".join(
        part
        for part in (
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation,
            candidate.counterexample,
        )
        if part
    ).lower()
    if not any(marker in blob for marker in ("fall through", "falls through", "fallthrough", "missing return", "missing_return", "implicit none")):
        return {}
    terms = _branch_terms_from_claim(
        candidate.content or "",
        candidate.failure_mode or "",
        candidate.evidence_summary or "",
        candidate.recommendation or "",
        candidate.counterexample or "",
    )
    if not terms or not _method_lacks_terminal_fallback(file_text, method_start, method_end):
        return {}
    returning_terms = [
        term
        for term in terms
        if "return" in _branch_block_for_term(file_text, method_start, method_end, term)
    ]
    if not returning_terms:
        return {}
    subject = f"{class_name}.{method_name}"
    return {
        "line_start": method_start,
        "line_end": method_end,
        "content": f"{subject} lacks a terminal fallback return for unexpected mode values.",
        "failure_mode": "Unexpected mode values fall through without returning the declared output shape.",
    }


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
        method = method_name_from_claim(
            candidate.content or "",
            candidate.failure_mode or "",
            candidate.evidence_summary or "",
            candidate.recommendation or "",
        )
        method_range = (
            function_line_range_in_scope(file_text, method, scope_start=c_start, scope_end=c_end)
            if method
            else None
        )
        if _ranges_overlap(ls, le, c_start, c_end):
            updates: dict[str, object] = {}
            fallback_updates: dict[str, object] = {}
            if method_range is not None:
                m_start, m_end = method_range
                if not _ranges_overlap(ls, le, m_start, m_end) or (le - ls) > (m_end - m_start + 20):
                    updates.update({"line_start": m_start, "line_end": m_end})
                fallback_updates = _branch_fallthrough_claim_repair(
                    file_text,
                    candidate,
                    class_name=subject,
                    method_name=method or "",
                    method_start=m_start,
                    method_end=m_end,
                )
                updates.update(fallback_updates)
            if not fallback_updates and _candidate_range_misses_claimed_branch(file_text, candidate, c_start, c_end):
                updates.update({"line_start": c_start, "line_end": c_end})
            if subject.lower() not in (candidate.content or "").lower():
                updates["content"] = f"class {subject}:"
            if updates:
                return candidate.model_copy(update=updates), None
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

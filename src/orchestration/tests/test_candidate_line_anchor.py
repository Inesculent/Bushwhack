"""Tests for candidate line anchor validation and repair."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding
from src.orchestration.routing.candidate_line_anchor import (
    anchor_candidate_lines,
    apply_line_anchor_policy,
    class_line_range_in_file,
    class_line_range_with_tail,
)


def _cand(**kwargs) -> CandidateFinding:
    base = dict(
        candidate_id="task_3:task_3_002",
        patch_task_id="task_3",
        file_path="comfy_extras/nodes_string.py",
        line_start=143,
        line_end=158,
        content="RegexExtract uses match.group(group_index)",
        claim_type="defect",
        failure_mode="IndexError on group_index",
        evidence_summary="RegexExtract.execute() group bounds",
        recommendation="validate group_index",
        reflection_specialties=["logic"],
        suspected_category="logic",
        severity="medium",
    )
    base.update(kwargs)
    return CandidateFinding(**base)  # type: ignore[arg-type]


_SAMPLE = """import re

class StringContains():
    def execute(self):
        pass

class RegexExtract():
    def execute(self, string, regex_pattern, mode, group_index, **kwargs):
        if mode == "All Matches":
            matches = re.findall(regex_pattern, string)
            return matches[0],
"""


def test_class_line_range_in_file() -> None:
    assert class_line_range_in_file(_SAMPLE, "RegexExtract") == (7, 11)
    assert class_line_range_in_file(_SAMPLE, "Missing") is None


_STRING_COMPARE_TAIL = "\n".join(
    [
        "class StringContains():",
        "    def execute(self):",
        "        pass",
        "",
        "class StringCompare():",
        "    def execute(self, string_a, string_b, mode, case_sensitive, **kwargs):",
        "        if mode == 'Equal':",
        "            return string_a == string_b,",
        "        elif mode == 'Starts With':",
        "            return string_a.startswith(string_b),",
        "        elif mode == 'Ends With':",
        "            return string_a.endswith(string_b),",
        "",
        "class RegexMatch():",
        "    def execute(self):",
        "        pass",
    ]
)


def test_class_line_range_with_tail_includes_terminal_return() -> None:
    ranged = class_line_range_with_tail(_STRING_COMPARE_TAIL, "StringCompare")
    assert ranged is not None
    start, end = ranged
    body = "\n".join(_STRING_COMPARE_TAIL.splitlines()[start - 1 : end])
    assert "return string_a.endswith(string_b)," in body
    assert "class RegexMatch" not in body


def test_drops_misanchored_lines_far_from_cited_class() -> None:
    cand = _cand()
    fixed, reason = anchor_candidate_lines(cand, file_text=_SAMPLE)
    assert fixed is None
    assert reason is not None
    assert "line_anchor_class_mismatch" in reason


def test_drops_when_claim_class_differs_from_line_body_class() -> None:
    multi = _STRING_COMPARE_TAIL + "\n\n" + _SAMPLE
    cand = _cand(
        line_start=228,
        line_end=323,
        content="StringCompare missing terminal else on discriminant dispatch",
        failure_mode="missing else after elif chain",
        recommendation="Review m[0] tuple handling in RegexExtract All Matches mode",
    )
    fixed, reason = anchor_candidate_lines(cand, file_text=multi)
    assert fixed is None
    assert reason is not None
    assert "line_anchor_class_mismatch" in reason


def test_repairs_content_when_lines_already_overlap_class() -> None:
    cand = _cand(line_start=7, line_end=11, content="wrong label")
    fixed, reason = anchor_candidate_lines(cand, file_text=_SAMPLE)
    assert reason is None
    assert fixed is not None
    assert fixed.content == "class RegexExtract:"


def test_repairs_overlapping_class_range_when_claimed_branch_is_outside_slice() -> None:
    cand = _cand(
        line_start=5,
        line_end=9,
        content="StringCompare.execute mode == 'Ends With': no return statement.",
        failure_mode="missing_return",
        evidence_summary="'Ends With' branch falls through.",
        recommendation="Add return to the 'Ends With' branch.",
    )
    fixed, reason = anchor_candidate_lines(cand, file_text=_STRING_COMPARE_TAIL)
    assert reason is None
    assert fixed is not None
    assert (fixed.line_start, fixed.line_end) == (6, 13)
    assert fixed.content == "StringCompare.execute lacks a terminal fallback return for unexpected mode values."
    assert fixed.failure_mode == "Unexpected mode values fall through without returning the declared output shape."


def test_drops_when_class_absent_and_lines_far_from_diff_anchor() -> None:
    git_diff = "\n".join(
        [
            "@@ -0,0 +250,20 @@",
            "+class RegexExtract():",
            "+    def execute(self):",
            "+        pass",
        ]
    )
    cand = _cand(line_start=100, line_end=110)
    fixed, reason = anchor_candidate_lines(cand, file_text="", git_diff=git_diff)
    assert fixed is None
    assert reason is not None
    assert "line_anchor_mismatch" in reason


def test_apply_line_anchor_policy_batch_drops_far_misanchor() -> None:
    cand = _cand()
    kept, warnings, dropped = apply_line_anchor_policy(
        [cand],
        file_contents={"comfy_extras/nodes_string.py": _SAMPLE},
    )
    assert kept == []
    assert cand.candidate_id in dropped
    assert any("line_anchor_class_mismatch" in w for w in warnings)

"""Parser for the solo-agent tagged review transcript.

Converts the free-form ``<diff>/<side>/<note>/<notesplit />/<end />`` response into
a list of ``ReviewFinding`` objects. Parse failures for individual comment blocks
are recorded as warnings rather than raising, so a malformed block never invalidates
the rest of the review.
"""
from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from typing import Final, List

from src.domain.schemas import ReviewFinding

_END_TAG: Final[re.Pattern[str]] = re.compile(r"<\s*end\s*/?\s*>", re.IGNORECASE)
_SPLIT_TAG: Final[re.Pattern[str]] = re.compile(r"<\s*notesplit\s*/?\s*>", re.IGNORECASE)
_DIFF_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"<\s*diff\s*>(?P<body>.*?)<\s*/\s*diff\s*>", re.IGNORECASE | re.DOTALL
)
_SIDE_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"<\s*side\s*>(?P<body>.*?)<\s*/\s*side\s*>", re.IGNORECASE | re.DOTALL
)
_NOTE_BLOCK: Final[re.Pattern[str]] = re.compile(
    r"<\s*note\s*>(?P<body>.*?)<\s*/\s*note\s*>", re.IGNORECASE | re.DOTALL
)


@dataclass(frozen=True, slots=True)
class SoloParseResult:
    findings: List[ReviewFinding]
    warnings: List[str]
    had_end_tag: bool


def parse_solo_response(
    response_text: str,
    finding_id_prefix: str,
    default_file_path: str = "unknown",
) -> SoloParseResult:
    """Parse a solo-agent LLM response into ReviewFinding objects.

    ``finding_id_prefix`` is combined with a per-block counter to produce stable
    ids (e.g. ``{prefix}-0``, ``{prefix}-1``). ``default_file_path`` is used when
    the ``<diff>`` block does not contain an obvious filepath header — the solo
    prompt does not ask the model to emit filepaths, so the caller should pass
    the PR-level path (if known) or leave the default.
    """
    warnings: List[str] = []
    text = response_text or ""

    end_match = _END_TAG.search(text)
    had_end_tag = end_match is not None
    if had_end_tag:
        text = text[: end_match.start()]
    else:
        warnings.append("missing_end_tag")

    blocks = [block.strip() for block in _SPLIT_TAG.split(text) if block.strip()]
    findings: List[ReviewFinding] = []

    for index, block in enumerate(blocks):
        diff_match = _DIFF_BLOCK.search(block)
        note_match = _NOTE_BLOCK.search(block)
        side_match = _SIDE_BLOCK.search(block)

        if diff_match is None and note_match is None:
            # Blocks before the first real comment (e.g. preamble prose) are expected;
            # only warn if the block contains one tag but not the other.
            continue
        if diff_match is None:
            warnings.append(f"block_{index}_missing_diff")
            continue
        if note_match is None:
            warnings.append(f"block_{index}_missing_note")
            continue

        diff_body = diff_match.group("body").strip("\n")
        note_body = note_match.group("body").strip()
        side_body = (side_match.group("body").strip().lower() if side_match else "")
        if side_body not in {"left", "right"}:
            if side_match is not None:
                warnings.append(f"block_{index}_invalid_side:{side_body!r}")
            side_body = "right"

        findings.append(
            ReviewFinding(
                id=f"{finding_id_prefix}-{index}",
                file_path=default_file_path,
                line_start=1,
                line_end=1,
                content=diff_body,
                severity="medium",
                feedback_type="other",
                recommendation=note_body,
                references=[f"side={side_body}"],
            )
        )

    return SoloParseResult(findings=findings, warnings=warnings, had_end_tag=had_end_tag)


def new_finding_prefix(run_id: str | None = None) -> str:
    """Build a stable per-review prefix for finding ids."""
    return f"solo-{run_id or uuid.uuid4().hex[:8]}"

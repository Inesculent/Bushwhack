"""Semantic deduplication and quality gates for candidates and final findings."""

from __future__ import annotations

import re
from typing import Any, Iterable, List, Mapping, Optional, Sequence, Tuple

from src.domain.schemas import CandidateFinding, ReviewFinding

_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)
_METHOD_CLASS_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.execute\b")
_HANDLER_NODE_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})\s+node\b", re.IGNORECASE)
_EXECUTE_CLASS_RE = re.compile(
    r"\bThe\s+`?([A-Z][A-Za-z0-9_]{2,})`?\.execute\b",
    re.IGNORECASE,
)
_POSSESSIVE_CLASS_RE = re.compile(r"\b([A-Z][A-Za-z0-9_]{2,})'s\b")
_DIFF_CLASS_RE = re.compile(r"^\+.*\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b")

_MISSING_BRANCH_MARKERS = (
    "missing else",
    "missing return",
    "no else",
    "no final return",
    "implicit none",
    "implicitly return none",
    "branch exhaust",
    "unhandled mode",
    "without a fallback",
)
_SECURITY_CLAIM_MARKERS = (
    "redos",
    "backtrack",
    "catastrophic",
    "pattern length",
    "user-controlled",
    "denial of service",
    "timeout mechanism",
    "regex timeout",
    "unvalidated pattern",
    "complexity analyzer",
)
_RESOURCE_CLAIM_MARKERS = (
    "resource use",
    "resource exhaust",
    "resource-amplification",
    "unbounded work",
    "unbounded input",
    "memory exhaustion",
    "excessive memory",
    "timeout",
    "cache",
    "caching",
    "compile cost",
    "bounded execution",
)
_STRUCTURED_SLOT_MARKERS = (
    "m[0]",
    "matches[0]",
    "match[0]",
    "row[0]",
    "rows[0]",
    "first element",
    "first slot",
    "only the first",
    "wrong slot",
    "data loss",
    "wrong index into",
    "structured result",
)
# Kept for upstream-none guard and perf retag paths.
_FINDALL_MARKERS = _STRUCTURED_SLOT_MARKERS + ("findall", "finditer", "tuple", "all matches")
_GROUP_INDEX_MARKERS = (
    "group_index",
    "groups()",
    "group 0",
    "capture group",
    "full match",
    "full-match",
    "empty group",
    "falsy",
    "truthiness",
)
_NONE_JOIN_MARKERS = (
    "nonetype",
    "optional group",
    "optional capture",
    "none before join",
    "none element",
    "none value",
    "none in aggregat",
    "absent element",
    "absent value",
    "absent capture",
    "missing group",
    "missing capture",
)
_AGGREGATION_ACTION_MARKERS = ("join(", "str.join", "join_delimiter.join", "join", "aggregat")

_RESOLUTION_ONLY_MARKERS = (
    "no action needed",
    "no action required",
    "no fix needed",
    "no change needed",
    "no critical defect",
    "no critical issue",
    "no correctness defect",
    "no correctness issue",
    "false positive",
    "not a bug",
    "not a defect",
    "not a correctness bug",
    "no defect",
    "no issue",
    "actually safe",
    "actually correct",
    "already handles",
    "already handled",
    "correct as-is",
    "correct as written",
    "no failure mode",
    "recommendation: none",
    "consider logging",
    "add logging",
    "logging for observability",
    "document the expected",
    "consider documenting",
    "debugging aid",
    "style-only",
    "style only",
    "maintainability only",
)

_REQUIRED_PARAM_NONE_MARKERS = (
    "passing none",
    "pass none",
    "none to ",
    "none passed",
    "when none",
    "none guard",
    "null guard",
    "nonetype",
    "is none",
    "is null",
    "without null",
    "without none",
    "null check",
    "none check",
    "isinstance",
)

_SEVERITY_RANK = {"high": 0, "medium": 1, "low": 2}
_LOGIC_DEFECT_FAMILIES = frozenset(
    {
        "missing_branch_return",
        "structured_slot_truncation",
        "aggregation_none_type",
        "regex_group_index",
        "index_bounds",
    }
)
_FAMILY_BEHAVIOR: dict[str, tuple[str, str]] = {
    "missing_branch_return": ("missing_return", "dispatch"),
    "structured_slot_truncation": ("data_loss", "indexing"),
    "aggregation_none_type": ("crash", "aggregation"),
    "regex_group_index": ("wrong_output", "indexing"),
    "index_bounds": ("crash", "indexing"),
    "redos": ("unbounded_work", "resource_use"),
    "resource_amplification": ("unbounded_work", "resource_use"),
    "empty_find_replace": ("wrong_output", "contract"),
    "duplicate_registration": ("contract_mismatch", "contract"),
}


def _blob_parts(*parts: str) -> str:
    return " ".join(p for p in parts if p).lower()


_POST_CONTEXT_SPLIT_RE = re.compile(r"\n\npost-context evidence:\s*", re.IGNORECASE)


def split_claim_and_post_context(text: str) -> tuple[str, str]:
    """Split promoted finding content into pre-revision claim vs revision appendix."""
    if not text.strip():
        return "", ""
    parts = _POST_CONTEXT_SPLIT_RE.split(text, maxsplit=1)
    if len(parts) == 1:
        return text.strip(), ""
    return parts[0].strip(), f"Post-context evidence: {parts[1].strip()}" if parts[1].strip() else ""


def extract_subject_class(*texts: str) -> Optional[str]:
    """Best-effort primary class name cited in finding text."""
    blob = " ".join(texts)
    for pattern in (_CLASS_RE, _METHOD_CLASS_RE):
        match = pattern.search(blob)
        if match:
            return match.group(1)
    handler = _HANDLER_NODE_RE.search(blob)
    if handler:
        return handler.group(1)
    execute = _EXECUTE_CLASS_RE.search(blob)
    if execute:
        return execute.group(1)
    possessive = _POSSESSIVE_CLASS_RE.search(blob)
    if possessive:
        return possessive.group(1)
    return None


def normalized_subject_for_key(
    *,
    file_path: str,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    recommendation: str = "",
) -> str:
    """Stable handler class name for dedupe keys (never empty when prose cites a node)."""
    claim_content, _ = split_claim_and_post_context(content)
    subject = extract_subject_class_from_claim(
        claim_content, failure_mode, evidence_summary
    ) or extract_subject_class(recommendation) or ""
    return subject.lower()


def extract_subject_class_from_claim(
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
) -> Optional[str]:
    """Primary class from claim fields only (excludes recommendation)."""
    base, _ = split_claim_and_post_context(content)
    return extract_subject_class(base, failure_mode, evidence_summary)


def _mentioned_handler_classes(text: str) -> set[str]:
    found = set(_CLASS_RE.findall(text))
    found.update(_METHOD_CLASS_RE.findall(text))
    return found


def revision_summary_conflicts_with_claim(
    *,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    revision_summary: str = "",
) -> bool:
    """True when revision text cites a different class than the candidate claim."""
    claim_subject = extract_subject_class_from_claim(content, failure_mode, evidence_summary)
    if not claim_subject or not revision_summary.strip():
        return False
    mentioned = _mentioned_handler_classes(revision_summary)
    mentioned.discard(claim_subject)
    return bool(mentioned)


def is_security_or_unbounded_pattern_claim(*texts: str) -> bool:
    blob = _blob_parts(*texts)
    return any(m in blob for m in _SECURITY_CLAIM_MARKERS)


def _has_absent_value_aggregation_claim(blob: str) -> bool:
    return any(m in blob for m in _NONE_JOIN_MARKERS) and any(
        m in blob for m in _AGGREGATION_ACTION_MARKERS
    )


def defect_family(*texts: str) -> str:
    blob = _blob_parts(*texts)
    if any(m in blob for m in _MISSING_BRANCH_MARKERS):
        return "missing_branch_return"
    if is_security_or_unbounded_pattern_claim(blob):
        return "redos"
    if any(m in blob for m in _RESOURCE_CLAIM_MARKERS):
        return "resource_amplification"
    if _has_absent_value_aggregation_claim(blob):
        return "aggregation_none_type"
    if any(m in blob for m in _STRUCTURED_SLOT_MARKERS) and (
        "[0]" in blob
        or "m[0]" in blob
        or "matches[0]" in blob
        or "first slot" in blob
        or "first element" in blob
        or "data loss" in blob
        or "findall" in blob
        or "all matches" in blob
    ):
        return "structured_slot_truncation"
    if any(m in blob for m in _GROUP_INDEX_MARKERS):
        return "regex_group_index"
    if "indexerror" in blob or "off-by-one" in blob:
        return "index_bounds"
    if "duplicate" in blob and "import" in blob:
        return "duplicate_registration"
    if "empty" in blob and "replace" in blob:
        return "empty_find_replace"
    return "general"


def infer_behavioral_symptom(*texts: str) -> str:
    """Infer a generic behavioral symptom from candidate prose."""
    blob = _blob_parts(*texts)
    if any(m in blob for m in _MISSING_BRANCH_MARKERS):
        return "missing_return"
    if "uncaught" in blob or "not caught" in blob or "outside" in blob and "try" in blob:
        return "uncaught_exception"
    if any(m in blob for m in _SECURITY_CLAIM_MARKERS) or any(
        m in blob for m in _RESOURCE_CLAIM_MARKERS
    ):
        return "unbounded_work"
    if (
        _has_absent_value_aggregation_claim(blob)
        or ("typeerror" in blob and "join" in blob)
        or "indexerror" in blob
        or "crash" in blob
    ):
        return "crash"
    if any(m in blob for m in _STRUCTURED_SLOT_MARKERS) and (
        "data loss" in blob or "drop" in blob or "discard" in blob or "only the first" in blob
    ):
        return "data_loss"
    if any(m in blob for m in _GROUP_INDEX_MARKERS) or "wrong output" in blob or "wrong value" in blob:
        return "wrong_output"
    if "contract" in blob or "return type" in blob or "return shape" in blob:
        return "contract_mismatch"
    family = defect_family(blob)
    return _FAMILY_BEHAVIOR.get(family, ("other", "other"))[0]


def infer_root_operation(*texts: str) -> str:
    """Infer a generic root operation from candidate prose."""
    blob = _blob_parts(*texts)
    if "try" in blob or "except" in blob or "uncaught" in blob or "exception scope" in blob:
        return "exception_scope"
    if _has_absent_value_aggregation_claim(blob) or ("typeerror" in blob and "join" in blob):
        return "aggregation"
    if any(m in blob for m in _STRUCTURED_SLOT_MARKERS) or any(m in blob for m in _GROUP_INDEX_MARKERS):
        return "indexing"
    if any(m in blob for m in _MISSING_BRANCH_MARKERS) or "dispatch" in blob or "mode" in blob:
        return "dispatch"
    if any(m in blob for m in _SECURITY_CLAIM_MARKERS) or any(
        m in blob for m in _RESOURCE_CLAIM_MARKERS
    ):
        return "resource_use"
    if "join" in blob or "format" in blob:
        return "aggregation"
    if "serializ" in blob:
        return "serialization"
    if "contract" in blob or "return type" in blob or "return shape" in blob:
        return "contract"
    family = defect_family(blob)
    return _FAMILY_BEHAVIOR.get(family, ("other", "other"))[1]


def candidate_with_behavioral_metadata(candidate: CandidateFinding) -> CandidateFinding:
    """Fill optional generic symptom/root-operation fields without overwriting LLM output."""
    symptom = candidate.behavioral_symptom
    if not symptom and candidate.failure_mode.strip():
        symptom = infer_behavioral_symptom(candidate.failure_mode)
    if not symptom or symptom == "other":
        symptom = infer_behavioral_symptom(
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
        )

    root = candidate.root_operation
    if not root and candidate.failure_mode.strip():
        root = infer_root_operation(candidate.failure_mode)
    if not root or root == "other":
        root = infer_root_operation(
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
        )
    return candidate.model_copy(update={"behavioral_symptom": symptom, "root_operation": root})


def _patch_task_specialty(patch_task_id: str) -> str:
    pid = (patch_task_id or "").strip().lower()
    if not pid:
        return "unknown"
    if pid.startswith("security") or "-security" in pid or "_security" in pid:
        return "security"
    if pid.startswith("logic") or "-logic" in pid or "_logic" in pid:
        return "logic"
    if pid.startswith("general") or "-general" in pid or "_general" in pid:
        return "general"
    if pid.startswith("performance") or "perf" in pid:
        return "performance"
    return "unknown"


def _specialty_rank_for_family(patch_task_id: str, family: str) -> int:
    """Lower is better when picking among duplicate candidates."""
    specialty = _patch_task_specialty(patch_task_id)
    if family in {"redos", "resource_amplification"}:
        order = ("security", "logic", "performance", "general", "unknown")
    elif family in _LOGIC_DEFECT_FAMILIES:
        order = ("logic", "security", "performance", "general", "unknown")
    else:
        order = ("logic", "security", "general", "performance", "unknown")
    try:
        return order.index(specialty)
    except ValueError:
        return len(order)


def _content_cites_subject(candidate: CandidateFinding) -> bool:
    subject = extract_subject_class(
        candidate.content,
        candidate.failure_mode,
        candidate.evidence_summary,
    )
    if not subject:
        return False
    return bool(re.search(rf"\bclass\s+{re.escape(subject)}\b", candidate.content, re.IGNORECASE))


def _defect_family_for_dedupe(
    *,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    recommendation: str = "",
) -> str:
    """Prefer failure_mode for family so shared evidence_summary does not collapse distinct bugs."""
    if failure_mode.strip():
        family = defect_family(failure_mode)
        if family != "general":
            return family
    return defect_family(content, failure_mode, evidence_summary, recommendation)


def semantic_finding_key(
    *,
    file_path: str,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    recommendation: str = "",
) -> Tuple[str, str, str]:
    claim_content, _ = split_claim_and_post_context(content)
    subject = normalized_subject_for_key(
        file_path=file_path,
        content=claim_content,
        failure_mode=failure_mode,
        evidence_summary=evidence_summary,
        recommendation=recommendation,
    )
    family = _defect_family_for_dedupe(
        content=claim_content,
        failure_mode=failure_mode,
        evidence_summary=evidence_summary,
        recommendation=recommendation,
    )
    return ((file_path or "").strip().lower(), subject, family)


def candidate_signature_key(candidate: CandidateFinding) -> tuple[str, str, str, str, str]:
    """Candidate dedupe key: file + subject + family + behavioral symptom + root operation."""
    normalized = candidate_with_behavioral_metadata(candidate)
    claim_content, _ = split_claim_and_post_context(normalized.content)
    subject = normalized_subject_for_key(
        file_path=normalized.file_path,
        content=claim_content,
        failure_mode=normalized.failure_mode,
        evidence_summary=normalized.evidence_summary,
        recommendation=normalized.recommendation or "",
    )
    symptom = normalized.behavioral_symptom or "other"
    root = normalized.root_operation or "other"
    if symptom == "other" and root == "other":
        family = _defect_family_for_dedupe(
            content=claim_content,
            failure_mode=normalized.failure_mode,
            evidence_summary=normalized.evidence_summary,
            recommendation=normalized.recommendation or "",
        )
        symptom, root = _FAMILY_BEHAVIOR.get(family, ("other", "other"))
    else:
        family = _defect_family_for_dedupe(
            content=claim_content,
            failure_mode=normalized.failure_mode,
            evidence_summary=normalized.evidence_summary,
            recommendation=normalized.recommendation or "",
        )
    return ((normalized.file_path or "").strip().lower(), subject, family, symptom, root)


def ensure_unique_finding_ids(findings: Sequence[ReviewFinding]) -> List[ReviewFinding]:
    """Guarantee distinct ``id`` values when promotion paths emit the same candidate_id twice."""
    seen: dict[str, int] = {}
    out: List[ReviewFinding] = []
    for finding in findings:
        base = (finding.id or "finding").strip() or "finding"
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count == 0:
            out.append(finding)
            continue
        out.append(finding.model_copy(update={"id": f"{base}__{count + 1}"}))
    return out


def ensure_unique_candidate_ids(candidates: Sequence[CandidateFinding]) -> List[CandidateFinding]:
    """Guarantee distinct candidate ids before lifecycle tracking and dedupe."""
    seen: dict[str, int] = {}
    out: List[CandidateFinding] = []
    for candidate in candidates:
        base = (candidate.candidate_id or "candidate").strip() or "candidate"
        count = seen.get(base, 0)
        seen[base] = count + 1
        if count == 0:
            out.append(candidate)
            continue
        out.append(candidate.model_copy(update={"candidate_id": f"{base}__{count + 1}"}))
    return out


def is_resolution_only_finding(*texts: str) -> bool:
    blob = _blob_parts(*texts)
    return any(marker in blob for marker in _RESOLUTION_ONLY_MARKERS)


def is_required_upstream_none_guard_claim(candidate: CandidateFinding) -> bool:
    """
    True when the claim is only missing None/null guards on required parameters.

    In-function contract guidance does not override the upstream declared-input rule.
    """
    blob = _candidate_blob(candidate)
    if not any(m in blob for m in _REQUIRED_PARAM_NONE_MARKERS):
        return False
    if any(m in blob for m in _MISSING_BRANCH_MARKERS):
        return False
    if any(m in blob for m in _FINDALL_MARKERS):
        return False
    if any(m in blob for m in _NONE_JOIN_MARKERS):
        return False
    if "typeerror" in blob and "join" in blob:
        return False
    if "optional" in blob or "nullable" in blob or "any/" in blob:
        return False
    if "in-function contract" in blob and ("schema don't" in blob or "schemas don't" in blob):
        return True
    if "isinstance" in blob and "required" in blob:
        return True
    if "input_types" in blob and "required" in blob and ("none" in blob or "null" in blob):
        return True
    if "required" in blob and ("none" in blob or "null" in blob) and "guard" in blob:
        return True
    return False


def _candidate_blob(candidate: CandidateFinding) -> str:
    return _blob_parts(
        candidate.content,
        candidate.failure_mode,
        candidate.evidence_summary,
        candidate.recommendation or "",
        " ".join(candidate.required_context),
    )


def class_def_lines_from_diff(git_diff: str) -> dict[str, int]:
    """Map class name -> approximate file line where ``class Name`` appears in added lines."""
    lines = git_diff.splitlines()
    anchors: dict[str, int] = {}
    new_line = 0
    for raw in lines:
        if raw.startswith("@@"):
            match = re.search(r"\+(\d+)", raw)
            new_line = int(match.group(1)) if match else new_line
            continue
        if raw.startswith("+++") or raw.startswith("---"):
            continue
        if raw.startswith("+") and not raw.startswith("+++"):
            class_match = _DIFF_CLASS_RE.match(raw)
            if class_match:
                anchors[class_match.group(1)] = new_line
            new_line += 1
        elif raw.startswith(" "):
            new_line += 1
    return anchors


def _anchor_distance(candidate: CandidateFinding, *, git_diff: str = "") -> int:
    subject = extract_subject_class(
        candidate.content,
        candidate.failure_mode,
        candidate.evidence_summary,
    )
    if not subject or not git_diff or not candidate.line_start:
        return 10_000
    anchors = class_def_lines_from_diff(git_diff)
    if subject not in anchors:
        return 10_000
    return abs(int(candidate.line_start) - anchors[subject])


def line_anchor_score(
    candidate: CandidateFinding,
    *,
    git_diff: str = "",
) -> int:
    """Higher is better: content class matches diff hunk lines."""
    subject = extract_subject_class(
        candidate.content,
        candidate.failure_mode,
        candidate.evidence_summary,
    )
    if not subject:
        return 0
    score = 0
    blob = _candidate_blob(candidate)
    if subject.lower() in blob:
        score += 2
    other_classes = set(_CLASS_RE.findall(blob)) | set(_METHOD_CLASS_RE.findall(blob))
    other_classes.discard(subject)
    for other in other_classes:
        if other.lower() in blob and other != subject:
            score -= 3
    if git_diff:
        anchors = class_def_lines_from_diff(git_diff)
        if subject in anchors:
            anchor = anchors[subject]
            if candidate.line_start and candidate.line_end:
                if candidate.line_start <= anchor + 80 and candidate.line_end >= anchor:
                    score += 5
                elif abs(candidate.line_start - anchor) > 120:
                    score -= 8
    return score


def _severity_rank(severity: str) -> int:
    return _SEVERITY_RANK.get((severity or "").strip().lower(), 99)


def _merge_failure_mode_text(keeper: str, other: str) -> str:
    """Combine failure_mode detail from deduped-away candidates into the kept row."""
    base = (keeper or "").strip()
    extra = (other or "").strip()
    if not extra:
        return base
    if not base:
        return extra
    if extra.lower() in base.lower():
        return base
    if base.lower() in extra.lower():
        return extra if len(extra) > len(base) else base
    return f"{base}; also: {extra}"


def pick_preferred_candidate(
    existing: CandidateFinding,
    challenger: CandidateFinding,
    *,
    git_diff: str = "",
) -> CandidateFinding:
    existing = candidate_with_behavioral_metadata(existing)
    challenger = candidate_with_behavioral_metadata(challenger)
    family = _defect_family_for_dedupe(
        content=existing.content,
        failure_mode=existing.failure_mode,
        evidence_summary=existing.evidence_summary,
        recommendation=existing.recommendation or "",
    )

    def _score(cand: CandidateFinding) -> tuple:
        specialty_rank = _specialty_rank_for_family(cand.patch_task_id, family)
        anchor = (
            0 if _content_cites_subject(cand) else 1,
            -_anchor_distance(cand, git_diff=git_diff),
            line_anchor_score(cand, git_diff=git_diff),
            len(cand.evidence_summary or ""),
        )
        if family in _LOGIC_DEFECT_FAMILIES or family in {"redos", "resource_amplification"}:
            return (-specialty_rank, -_severity_rank(cand.severity), *anchor)
        return (-_severity_rank(cand.severity), -specialty_rank, *anchor)

    return challenger if _score(challenger) > _score(existing) else existing


def dedupe_candidates_by_signature(
    candidates: Sequence[CandidateFinding],
    *,
    git_diff: str = "",
) -> tuple[List[CandidateFinding], dict[str, list[str]]]:
    """Collapse same file/class/symptom/root-operation; keep strongest anchored candidate."""
    kept: dict[tuple[str, str, str, str, str], CandidateFinding] = {}
    duplicates: dict[str, list[str]] = {}
    order: list[tuple[str, str, str, str, str]] = []

    for cand in candidates:
        cand = candidate_with_behavioral_metadata(cand)
        key = candidate_signature_key(cand)
        if key not in kept:
            order.append(key)
            kept[key] = cand
            continue
        primary = kept[key]
        preferred = pick_preferred_candidate(primary, cand, git_diff=git_diff)
        dropped = primary if preferred.candidate_id == cand.candidate_id else cand
        if preferred.candidate_id != dropped.candidate_id:
            duplicates.setdefault(preferred.candidate_id, []).append(dropped.candidate_id)
        merged_mode = _merge_failure_mode_text(preferred.failure_mode, dropped.failure_mode)
        if merged_mode != preferred.failure_mode:
            preferred = preferred.model_copy(update={"failure_mode": merged_mode})
        kept[key] = preferred

    return [kept[k] for k in order], duplicates


def review_finding_semantic_key(finding: ReviewFinding) -> tuple[str, str, str, str, str]:
    claim_content, _ = split_claim_and_post_context(finding.content or "")
    recommendation = finding.recommendation or ""
    # Promoted findings often use stub content ("class Foo():") with the real claim in recommendation.
    combined = f"{claim_content}\n{recommendation}".strip()
    subject = normalized_subject_for_key(
        file_path=finding.file_path,
        content=combined,
        failure_mode=recommendation,
        evidence_summary=claim_content,
        recommendation=recommendation,
    )
    family = _defect_family_for_dedupe(
        content=claim_content,
        failure_mode="",
        evidence_summary=claim_content,
        recommendation="",
    )
    if family == "general":
        family = _defect_family_for_dedupe(
            content=combined,
            failure_mode=recommendation,
            evidence_summary=claim_content,
            recommendation=recommendation,
        )
    symptom = infer_behavioral_symptom(combined)
    root = infer_root_operation(combined)
    if symptom == "other" and root == "other":
        symptom, root = _FAMILY_BEHAVIOR.get(family, ("other", "other"))
    return ((finding.file_path or "").strip().lower(), subject, family, symptom, root)


def _finding_preference_score(finding: ReviewFinding, *, family: str) -> tuple:
    return (
        -_severity_rank(finding.severity),
        len(finding.content or ""),
    )


def dedupe_review_findings_by_signature(
    findings: Sequence[ReviewFinding],
) -> tuple[List[ReviewFinding], dict[str, list[str]]]:
    """Collapse same file/class/family; keep strongest row without merging unlike text."""
    kept: dict[tuple[str, str, str, str, str], ReviewFinding] = {}
    duplicates: dict[str, list[str]] = {}
    order: list[tuple[str, str, str, str, str]] = []

    for finding in findings:
        key = review_finding_semantic_key(finding)
        if key not in kept:
            order.append(key)
            kept[key] = finding
            continue
        primary = kept[key]
        family = key[2]
        if _finding_preference_score(finding, family=family) > _finding_preference_score(
            primary, family=family
        ):
            if finding.id != primary.id:
                duplicates.setdefault(finding.id, []).append(primary.id)
            kept[key] = finding
        else:
            if primary.id != finding.id:
                duplicates.setdefault(primary.id, []).append(finding.id)

    return [kept[k] for k in order], duplicates


def changed_files_from_diff(git_diff: str) -> set[str]:
    paths: set[str] = set()
    for line in (git_diff or "").splitlines():
        if line.startswith("+++ b/"):
            path = line.removeprefix("+++ b/").strip()
            if path and path != "/dev/null":
                paths.add(path.replace("\\", "/"))
        elif line.startswith("diff --git "):
            parts = line.split()
            if len(parts) >= 4 and parts[3].startswith("b/"):
                paths.add(parts[3].removeprefix("b/").replace("\\", "/"))
    return paths


def resolve_repo_file_path(path: str, allowed: set[str]) -> str | None:
    """Map finding path to a changed-file path; fix single-segment typos when unambiguous."""
    norm = (path or "").strip().replace("\\", "/").lstrip("/")
    if not norm:
        return None
    if norm in allowed:
        return norm
    basename = norm.split("/")[-1]
    matches = [a for a in allowed if a.endswith("/" + basename) or a == basename]
    if len(matches) == 1:
        return matches[0]
    return None


def recommendation_cites_foreign_class(
    *,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    recommendation: str = "",
) -> bool:
    """True when recommendation names a different class than the cited subject."""
    subject = extract_subject_class_from_claim(content, failure_mode, evidence_summary) or ""
    if not subject:
        return False
    rec = recommendation or ""
    cited = set(_CLASS_RE.findall(rec)) | {m[0] for m in _METHOD_CLASS_RE.findall(rec)}
    cited.discard(subject)
    return bool(cited)

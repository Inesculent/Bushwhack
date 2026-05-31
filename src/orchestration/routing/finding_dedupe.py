"""Semantic deduplication and quality gates for candidates and final findings."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, List, Mapping, Optional, Sequence

from src.domain.schemas import CandidateFinding, ReviewFinding

_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)
_METHOD_CLASS_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.execute\b")
_DIFF_CLASS_RE = re.compile(r"^\+.*\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b")

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


@dataclass(frozen=True)
class FindingSignature:
    """Structured semantic identity for candidate/final finding dedupe."""

    file_path: str
    subject: str
    claim_kind: str
    behavioral_symptom: str
    root_operation: str

    def key(self) -> tuple[str, str, str, str, str]:
        return (
            self.file_path,
            self.subject,
            self.claim_kind,
            self.behavioral_symptom,
            self.root_operation,
        )


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
    """Primary class name from explicit class or ``Class.execute`` references."""
    blob = " ".join(texts)
    for pattern in (_CLASS_RE, _METHOD_CLASS_RE):
        match = pattern.search(blob)
        if match:
            return match.group(1)
    return None


def normalized_subject_for_key(
    *,
    file_path: str,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    recommendation: str = "",
) -> str:
    """Stable explicit class name for dedupe keys."""
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


def candidate_with_behavioral_metadata(candidate: CandidateFinding) -> CandidateFinding:
    """Normalize structured behavior metadata without text-based inference."""
    symptom = candidate.behavioral_symptom or "other"
    root = candidate.root_operation or "other"
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


def _specialty_rank_for_signature(patch_task_id: str, signature: FindingSignature) -> int:
    """Lower is better when picking among duplicate candidates."""
    specialty = _patch_task_specialty(patch_task_id)
    if signature.claim_kind == "security_risk":
        order = ("security", "logic", "performance", "general", "unknown")
    elif signature.claim_kind == "performance_regression":
        order = ("performance", "logic", "security", "general", "unknown")
    elif (
        signature.root_operation == "resource_use"
        or signature.behavioral_symptom == "unbounded_work"
    ):
        order = ("security", "logic", "performance", "general", "unknown")
    elif signature.behavioral_symptom in {
        "missing_return",
        "data_loss",
        "crash",
        "wrong_output",
        "contract_mismatch",
    }:
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


def _normalized_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("/").lower()


def _subject_or_line_scope(subject: str, line_start: int | None, line_end: int | None) -> str:
    if subject:
        return subject
    if line_start and line_end:
        return f"lines:{int(line_start)}-{int(line_end)}"
    if line_start:
        return f"line:{int(line_start)}"
    return ""


def semantic_finding_key(
    *,
    file_path: str,
    content: str = "",
    failure_mode: str = "",
    evidence_summary: str = "",
    recommendation: str = "",
    claim_kind: str = "defect",
    behavioral_symptom: str = "other",
    root_operation: str = "other",
) -> tuple[str, str, str, str, str]:
    claim_content, _ = split_claim_and_post_context(content)
    subject = normalized_subject_for_key(
        file_path=file_path,
        content=claim_content,
        failure_mode=failure_mode,
        evidence_summary=evidence_summary,
        recommendation=recommendation,
    )
    signature = FindingSignature(
        file_path=_normalized_path(file_path),
        subject=subject,
        claim_kind=(claim_kind or "other").strip().lower(),
        behavioral_symptom=behavioral_symptom or "other",
        root_operation=root_operation or "other",
    )
    return signature.key()


def candidate_finding_signature(
    candidate: CandidateFinding,
    *,
    git_diff: str = "",
) -> FindingSignature:
    """Candidate dedupe identity from structured metadata plus anchored subject."""
    normalized = candidate_with_behavioral_metadata(candidate)
    claim_content, _ = split_claim_and_post_context(normalized.content)
    subject = normalized_subject_for_key(
        file_path=normalized.file_path,
        content=claim_content,
        failure_mode=normalized.failure_mode,
        evidence_summary=normalized.evidence_summary,
        recommendation=normalized.recommendation or "",
    )
    return FindingSignature(
        file_path=_normalized_path(normalized.file_path),
        subject=_subject_or_line_scope(subject, normalized.line_start, normalized.line_end),
        claim_kind=(normalized.claim_type or "other").strip().lower(),
        behavioral_symptom=normalized.behavioral_symptom or "other",
        root_operation=normalized.root_operation or "other",
    )


def candidate_signature_key(
    candidate: CandidateFinding,
    *,
    git_diff: str = "",
) -> tuple[str, str, str, str, str]:
    """Candidate dedupe key: file + subject + claim kind + symptom + root operation."""
    return candidate_finding_signature(candidate, git_diff=git_diff).key()


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
    normalized = candidate_with_behavioral_metadata(candidate)
    if normalized.behavioral_symptom not in {None, "other"}:
        return False
    if normalized.root_operation not in {None, "other"}:
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
    signature = candidate_finding_signature(existing, git_diff=git_diff)

    def _score(cand: CandidateFinding) -> tuple:
        specialty_rank = _specialty_rank_for_signature(cand.patch_task_id, signature)
        anchor = (
            0 if _content_cites_subject(cand) else 1,
            -_anchor_distance(cand, git_diff=git_diff),
            line_anchor_score(cand, git_diff=git_diff),
            len(cand.evidence_summary or ""),
        )
        return (-specialty_rank, -_severity_rank(cand.severity), *anchor)

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
        key = candidate_signature_key(cand, git_diff=git_diff)
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


def review_finding_signature(finding: ReviewFinding) -> FindingSignature:
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
    return FindingSignature(
        file_path=_normalized_path(finding.file_path),
        subject=_subject_or_line_scope(subject, finding.line_start, finding.line_end),
        claim_kind=(finding.feedback_type or "other").strip().lower(),
        behavioral_symptom=finding.behavioral_symptom or "other",
        root_operation=finding.root_operation or "other",
    )


def review_finding_semantic_key(finding: ReviewFinding) -> tuple[str, str, str, str, str]:
    return review_finding_signature(finding).key()


def _finding_preference_score(finding: ReviewFinding, *, signature: FindingSignature) -> tuple:
    blob = f"{finding.content or ''}\n{finding.recommendation or ''}".lower()
    concrete = 0 if re.match(r"^\s*class\s+\w+\(?\)?:?\s*$", finding.content or "") else 1
    terminal_dispatch = 1 if any(
        marker in blob for marker in ("terminal else", "unexpected mode", "unhandled mode", "fallback")
    ) else 0
    contradicted_shape = -1 if any(
        marker in blob for marker in ("incomplete in the diff", "diff is truncated", "code cuts off")
    ) else 0
    if signature.behavioral_symptom == "missing_return" and signature.root_operation == "dispatch":
        return (
            terminal_dispatch,
            -_severity_rank(finding.severity),
            concrete,
            contradicted_shape,
            len(finding.content or ""),
        )
    return (
        -_severity_rank(finding.severity),
        terminal_dispatch,
        concrete,
        contradicted_shape,
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
        signature = review_finding_signature(finding)
        key = signature.key()
        if key not in kept:
            order.append(key)
            kept[key] = finding
            continue
        primary = kept[key]
        if _finding_preference_score(finding, signature=signature) > _finding_preference_score(
            primary, signature=signature
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

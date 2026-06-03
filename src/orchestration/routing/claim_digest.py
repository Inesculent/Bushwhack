"""Compact contract-claim identity helpers for reviewer dedupe and ownership."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence

from src.domain.schemas import CandidateFinding, ReviewCheck, ReviewCheckResult, ReviewFinding

_CLASS_RE = re.compile(r"\bclass\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)
_METHOD_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\.([A-Za-z_][A-Za-z0-9_]*)\b")
_DEF_RE = re.compile(r"\bdef\s+([A-Za-z_][A-Za-z0-9_]*)\b", re.IGNORECASE)
_QUOTED_RE = re.compile(r"['\"]([^'\"]{2,48})['\"]")
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_GENERIC_DIGEST_TERMS = frozenset(
    {
        "src",
        "py",
        "com",
        "org",
        "lib",
        "pkg",
        "node",
        "nodes",
        "string",
        "contract",
        "impact",
        "variant",
        "unknown",
    }
)


def _norm_path(path: str) -> str:
    return (path or "").strip().replace("\\", "/").lstrip("/").lower()


def _slug(value: str, *, default: str = "unknown") -> str:
    tokens = _TOKEN_RE.findall((value or "").lower())
    return "_".join(tokens[:5]) or default


def _blob(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts if part is not None)


def _subject_from_text(*texts: str, fallback: str = "") -> str:
    blob = _blob(*texts)
    match = _CLASS_RE.search(blob)
    if match:
        return match.group(1)
    method_match = _METHOD_RE.search(blob)
    if method_match and method_match.group(1)[:1].isupper():
        return f"{method_match.group(1)}.{method_match.group(2)}"
    def_match = _DEF_RE.search(blob)
    if def_match:
        return def_match.group(1)
    return fallback.strip() or "unknown"


def _variant_terms(*texts: str) -> list[str]:
    blob = _blob(*texts)
    lower = blob.lower()
    variants: list[str] = []
    for quoted in _QUOTED_RE.findall(blob):
        if any(ch.isalpha() for ch in quoted):
            variants.append(_slug(quoted))
    for marker in (
        "all matches",
        "all groups",
        "first match",
        "first group",
        "fallback",
        "unknown mode",
        "default",
        "optional",
        "empty",
        "duplicate",
        "legacy",
    ):
        if marker in lower:
            variants.append(_slug(marker))
    return list(dict.fromkeys(term for term in variants if term != "unknown"))[:3]


def _contract_terms(*texts: str) -> list[str]:
    lower = _blob(*texts).lower()
    terms: list[str] = []
    if any(term in lower for term in ("tuple", "field", "slot", "element", "cardinal", "all ", "every", "each")):
        terms.append("cardinality")
    if any(term in lower for term in ("drop", "dropped", "skip", "lost", "loss", "omit", "truncate", "preserve")):
        terms.append("preservation")
    if any(term in lower for term in ("index", "indices", "bounds", "out-of-range", "out of range", "off-by-one")):
        terms.append("bounds")
    if any(term in lower for term in ("selector", "selected value", "selected item", "group_index", "group index", "match.group", "groups()")):
        terms.append("selection")
    if any(term in lower for term in ("return", "fallback", "fall through", "fallthrough", "else", "branch", "mode")):
        terms.append("dispatch")
    if any(term in lower for term in ("schema", "type", "serialization", "serialize", "protocol", "representation")):
        terms.append("representation")
    if any(term in lower for term in ("resource", "timeout", "memory", "hot path", "loop", "retry", "unbounded")):
        terms.append("resource")
    if not terms:
        terms.append("contract")
    return list(dict.fromkeys(terms))[:3]


def _impact_terms(*texts: str) -> list[str]:
    lower = _blob(*texts).lower()
    terms: list[str] = []
    for marker, label in (
        ("data loss", "data_loss"),
        ("wrong output", "wrong_output"),
        ("crash", "crash"),
        ("indexerror", "crash"),
        ("implicit none", "missing_return"),
        ("missing return", "missing_return"),
        ("unbounded", "unbounded_work"),
        ("contract mismatch", "contract_mismatch"),
    ):
        if marker in lower:
            terms.append(label)
    return list(dict.fromkeys(terms))[:2]


def synthesize_claim_digest(
    *,
    file_path: str,
    subject_hint: str = "",
    changed_code_anchor: str = "",
    behavioral_symptom: str | None = None,
    root_operation: str | None = None,
    texts: Sequence[str] = (),
) -> str:
    """Return a compact root-claim marker for ownership and cluster grouping."""
    text_blob = _blob(*texts)
    subject = _subject_from_text(text_blob, fallback=subject_hint or changed_code_anchor)
    variants = _variant_terms(changed_code_anchor, text_blob)
    contracts = _contract_terms(changed_code_anchor, text_blob)
    impacts = _impact_terms(text_blob, behavioral_symptom or "", root_operation or "")
    if behavioral_symptom and not impacts:
        impacts.append(_slug(behavioral_symptom))
    parts = [_norm_path(file_path), _slug(subject)]
    if variants:
        parts.append(f"variant={'+'.join(variants)}")
    if contracts:
        parts.append(f"contract={'+'.join(contracts)}")
    if impacts:
        parts.append(f"impact={'+'.join(impacts)}")
    return "::".join(part for part in parts if part)


def claim_digest_terms(digest: str) -> set[str]:
    return set(_TOKEN_RE.findall((digest or "").lower()))


def claim_digest_subject(digest: str) -> str:
    parts = (digest or "").split("::")
    return parts[1] if len(parts) > 1 else ""


def claim_digest_field(digest: str, field: str) -> str:
    prefix = f"{field}="
    for part in (digest or "").split("::"):
        if part.startswith(prefix):
            return part[len(prefix) :]
    return ""


def claim_digests_overlap(left: str, right: str) -> bool:
    left_terms = claim_digest_terms(left)
    right_terms = claim_digest_terms(right)
    if not left_terms or not right_terms:
        return False
    if claim_digest_subject(left) != claim_digest_subject(right):
        return False
    left_meaningful = left_terms - _GENERIC_DIGEST_TERMS
    right_meaningful = right_terms - _GENERIC_DIGEST_TERMS
    common = left_meaningful & right_meaningful
    if len(common) >= 2:
        return True
    return bool(claim_digest_field(left, "variant") and claim_digest_field(left, "variant") == claim_digest_field(right, "variant"))


def owned_contract_scope_for_check(check: ReviewCheck) -> str:
    if check.owned_contract_scope.strip():
        return check.owned_contract_scope.strip()[:240]
    return synthesize_claim_digest(
        file_path=check.file_path,
        subject_hint=check.changed_code_anchor,
        changed_code_anchor=check.changed_code_anchor,
        texts=[
            check.behavioral_question,
            check.affected_invariant,
            " ".join(check.required_evidence),
            " ".join(check.report_criteria),
        ],
    )[:240]


def claim_digest_for_result(result: ReviewCheckResult, check: ReviewCheck) -> str:
    if result.claim_digest.strip():
        return result.claim_digest.strip()[:240]
    candidate = result.candidate
    if candidate is not None and candidate.claim_digest.strip():
        return candidate.claim_digest.strip()[:240]
    return synthesize_claim_digest(
        file_path=check.file_path,
        subject_hint=check.changed_code_anchor,
        changed_code_anchor=check.changed_code_anchor,
        behavioral_symptom=getattr(candidate, "behavioral_symptom", None),
        root_operation=getattr(candidate, "root_operation", None),
        texts=[
            check.behavioral_question,
            check.affected_invariant,
            result.reportable_reason,
            result.evidence_for_contract,
            result.counterexample,
            result.rejection_check,
            getattr(candidate, "content", ""),
            getattr(candidate, "failure_mode", ""),
            getattr(candidate, "evidence_summary", ""),
        ],
    )[:240]


def claim_digest_for_candidate(candidate: CandidateFinding) -> str:
    if candidate.claim_digest.strip():
        return candidate.claim_digest.strip()[:240]
    return normalized_claim_digest_for_candidate(candidate)


def normalized_claim_digest_for_candidate(candidate: CandidateFinding) -> str:
    return synthesize_claim_digest(
        file_path=candidate.file_path,
        behavioral_symptom=candidate.behavioral_symptom,
        root_operation=candidate.root_operation,
        texts=[
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.evidence_for_contract,
            candidate.counterexample,
            candidate.rejection_check,
            candidate.recommendation or "",
        ],
    )[:240]


def claim_digest_for_finding(finding: ReviewFinding) -> str:
    existing = getattr(finding, "claim_digest", "") or ""
    if existing.strip():
        return existing.strip()[:240]
    return normalized_claim_digest_for_finding(finding)


def normalized_claim_digest_for_finding(finding: ReviewFinding) -> str:
    return synthesize_claim_digest(
        file_path=finding.file_path,
        behavioral_symptom=finding.behavioral_symptom,
        root_operation=finding.root_operation,
        texts=[
            finding.content,
            finding.recommendation or "",
            finding.evidence_for_contract,
            finding.counterexample,
            finding.rejection_check,
        ],
    )[:240]


def duplicate_digest_map(items: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    """Return exact digest duplicates as keeper -> duplicate ids."""
    seen: dict[str, str] = {}
    duplicates: dict[str, list[str]] = {}
    for item in items:
        item_id = str(item.get("id") or item.get("candidate_id") or "")
        digest = str(item.get("claim_digest") or "")
        if not item_id or not digest:
            continue
        if digest not in seen:
            seen[digest] = item_id
            continue
        duplicates.setdefault(seen[digest], []).append(item_id)
    return duplicates

"""Deterministic promotion of accepted candidates into ReviewFinding objects."""

from __future__ import annotations

import ast
import logging
import re
from typing import Any, Dict, List, Literal, Mapping, Sequence

from pydantic import BaseModel, Field

from src.domain.schemas import (
    CandidateFinding,
    FocusedContextResult,
    ReflectionReport,
    ReviewCategory,
    ReviewFinding,
)
from src.config import Settings, get_settings
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import parse_structured_output
from src.infrastructure.llm.trace import trace_from_exception, trace_llm_call
from src.orchestration.routing.misroute_recovery import parse_misroute_redirect_category
from src.orchestration.nodes.verifier.failure_class import verifier_refutation_applies
from src.orchestration.routing.finding_dedupe import (
    candidate_with_behavioral_metadata,
    changed_files_from_diff,
    dedupe_candidates_by_signature,
    dedupe_review_findings_by_signature,
    ensure_unique_candidate_ids,
    ensure_unique_finding_ids,
    is_required_upstream_none_guard_claim,
    is_resolution_only_finding,
    recommendation_cites_foreign_class,
    resolve_repo_file_path,
    review_finding_semantic_key,
    revision_summary_conflicts_with_claim,
)
from src.orchestration.routing.claim_tiering import (
    classify_candidates,
    classify_claim_tier,
    review_kb_context_for_candidate,
    security_boundary_is_concrete,
)
from src.orchestration.routing.reflection_consolidation import (
    candidate_has_local_defect_signature,
    consolidate_reflection_reports,
)

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

EXPECTED_REFLECTORS = {"security", "logic", "performance", "general"}
DOMAIN_REFLECTORS = {"security", "logic", "performance", "general"}
PROMOTABLE_CLAIM_TYPES = {"defect", "security_risk", "performance_regression", "missing_test"}
CONTEXT_REQUIRED_CLAIM_TYPES = frozenset({"security_risk", "performance_regression"})


class RevisionSupportAuditOutput(BaseModel):
    verdict: Literal["resolved", "unresolved", "unsupported"] = "unsupported"
    rationale: str = Field(default="", max_length=500)
    warnings: List[str] = Field(default_factory=list)


class SemanticEquivalenceAuditItem(BaseModel):
    finding_id: str
    equivalent_to: str
    verdict: Literal["same_issue", "distinct", "unsupported"] = "unsupported"
    rationale: str = Field(default="", max_length=500)


class SemanticEquivalenceAuditOutput(BaseModel):
    items: List[SemanticEquivalenceAuditItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


_CONCRETE_LOCAL_SYMPTOMS = frozenset(
    {
        "wrong_output",
        "data_loss",
        "crash",
        "missing_return",
        "uncaught_exception",
        "contract_mismatch",
    }
)

# Tier 2: claims that typically need cross-file / framework evidence before promotion.
_TIER2_EXTERNAL_CONTEXT_MARKERS = (
    "caller",
    "callers",
    "calling ",
    "middleware",
    "decorator",
    "upstream",
    "downstream",
    "authorization",
    "authorize",
    "authenticated",
    "permission",
    "tenant ",
    "tenant_",
    "isolation",
    "integration",
    "external api",
    "remote ",
    "framework ",
    "orm ",
    "database ",
    "service ",
    "contract",
)
_BROAD_RISK_BOUNDARY_MARKERS = (
    "attacker",
    "untrusted",
    "user-controlled",
    "external",
    "remote",
    "public",
    "request",
    "tenant",
    "cross-boundary",
    "shared",
    "network",
    "endpoint",
)
_SCOPE_OUTSIDE_MARKERS = (
    "outside the try",
    "outside try",
    "not wrapped",
    "not enclosed",
    "not caught",
    "uncaught",
)
_SCOPE_INSIDE_MARKERS = (
    "inside the try",
    "wrapped by try",
    "enclosed by try",
    "caught by",
)
_NO_RETURN_MARKERS = (
    "lacks return",
    "lacks a return",
    "missing return",
    "missing a return",
    "no return",
    "without return",
    "without a return",
    "does not return",
    "doesn't return",
    "implicit none",
    "falls through",
    "fall through",
)
_EXTRACTION_MODE_MARKERS = (
    "all matches",
    "all groups",
    "first match",
    "first group",
)

def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _reports_by_candidate(reports: Sequence[Any]) -> Dict[str, List[ReflectionReport]]:
    grouped: Dict[str, List[ReflectionReport]] = {}
    for item in reports:
        report: ReflectionReport | None
        if isinstance(item, ReflectionReport):
            report = item
        elif isinstance(item, dict):
            try:
                report = ReflectionReport.model_validate(item)
            except Exception:
                report = None
        else:
            report = None
        if report is None:
            continue
        grouped.setdefault(report.candidate_id, []).append(report)
    return grouped


def _revision_map(metadata: Mapping[str, Any]) -> Dict[str, Mapping[str, Any]]:
    block = metadata.get("critique_revision") or {}
    revisions = block.get("revisions") or []
    out: Dict[str, Mapping[str, Any]] = {}
    for entry in revisions:
        if isinstance(entry, dict) and entry.get("candidate_id"):
            out[str(entry["candidate_id"])] = entry
    return out


def _focused_hits_for_candidate(state: GraphState, candidate_id: str) -> bool:
    for raw in (state.get("focused_context_results", {}) or {}).values():
        if isinstance(raw, dict):
            result = FocusedContextResult.model_validate(raw)
        else:
            result = raw
        if getattr(result, "candidate_id", None) == candidate_id:
            if (
                getattr(result, "file_snippets", None)
                or getattr(result, "file_contents_full", None)
                or getattr(result, "search_hits", None)
            ):
                return True
    return False


def _final_category(candidate: CandidateFinding, reports: List[ReflectionReport]) -> ReviewCategory:
    category: ReviewCategory = candidate.suspected_category
    for report in reports:
        if report.verdict == "reclassify" and report.reclassified_category:
            category = report.reclassified_category
    return category


def _relevant_reflectors(candidate: CandidateFinding, category: ReviewCategory) -> set[str]:
    routed = {
        specialty
        for specialty in candidate.reflection_specialties
        if specialty in DOMAIN_REFLECTORS
    }
    if routed:
        return routed
    if category in DOMAIN_REFLECTORS:
        return {category}
    return set(DOMAIN_REFLECTORS)


def _category_to_feedback(category: ReviewCategory):
    if category == "security":
        return "defect_detection"
    if category == "logic":
        return "defect_detection"
    if category == "performance":
        return "optimization"
    if category == "general":
        return "code_improvement"
    return "other"


def _candidate_has_actionability(candidate: CandidateFinding) -> bool:
    return bool(
        candidate.failure_mode.strip()
        and candidate.evidence_summary.strip()
        and (candidate.recommendation or "").strip()
    )


def _candidate_evidence_blob(candidate: CandidateFinding) -> str:
    return " ".join(
        [
            candidate.content,
            candidate.failure_mode,
            candidate.evidence_summary,
            candidate.recommendation or "",
            " ".join(candidate.required_context),
        ]
    ).lower()


def _resource_oriented_candidate(candidate: CandidateFinding) -> bool:
    normalized = candidate_with_behavioral_metadata(candidate)
    if normalized.root_operation == "resource_use" or normalized.behavioral_symptom == "unbounded_work":
        return True
    if candidate.claim_type in {"security_risk", "performance_regression"}:
        blob = _candidate_evidence_blob(candidate)
        return any(marker in blob for marker in ("timeout", "cache", "memory", "limit", "unbounded"))
    return False


def _resource_oriented_finding(finding: ReviewFinding) -> bool:
    return finding.root_operation == "resource_use" or finding.behavioral_symptom == "unbounded_work"


def _candidate_has_concrete_behavior(candidate: CandidateFinding) -> bool:
    normalized = candidate_with_behavioral_metadata(candidate)
    if normalized.behavioral_symptom in _CONCRETE_LOCAL_SYMPTOMS:
        return True
    blob = _candidate_evidence_blob(normalized)
    return any(
        marker in blob
        for marker in (
            "wrong output",
            "data loss",
            "drops ",
            "loses ",
            "crash",
            "raises ",
            "implicit none",
            "missing return",
            "contract mismatch",
        )
    )


def _candidate_has_source_local_evidence(
    state: GraphState,
    candidate: CandidateFinding,
    changed_files: set[str],
) -> bool:
    if changed_files and resolve_repo_file_path(candidate.file_path, changed_files) is None:
        return False
    return bool(_candidate_code_evidence_text(state, candidate).strip())


def _supported_by_relevant_reflection(relevant_reports: Sequence[ReflectionReport]) -> bool:
    return any(
        report.verdict in {"accept", "reclassify", "needs_context", "needs_verification"}
        for report in relevant_reports
    )


def _concrete_local_candidate_supported(
    state: GraphState,
    candidate: CandidateFinding,
    relevant_reports: Sequence[ReflectionReport],
    changed_files: set[str],
) -> bool:
    return (
        _candidate_has_concrete_behavior(candidate)
        and _candidate_has_source_local_evidence(state, candidate, changed_files)
        and _supported_by_relevant_reflection(relevant_reports)
    )


def _extraction_modes(text: str) -> set[str]:
    blob = (text or "").lower()
    return {marker for marker in _EXTRACTION_MODE_MARKERS if marker in blob}


def _reflection_family_or_surface_conflict(
    candidate: CandidateFinding,
    reports: Sequence[ReflectionReport],
) -> bool:
    """Detect accept/reject splits where the accept pivots to a different bug surface."""
    accepts = [r for r in reports if r.verdict in {"accept", "reclassify"}]
    rejects = [r for r in reports if r.verdict == "reject"]
    if not accepts or not rejects:
        return False

    candidate_blob = " ".join(
        [candidate.content, candidate.failure_mode, candidate.evidence_summary]
    )
    candidate_modes = _extraction_modes(candidate_blob)
    for report in accepts:
        rationale = report.rationale or ""
        rationale_modes = _extraction_modes(rationale)
        if candidate_modes and rationale_modes and candidate_modes.isdisjoint(rationale_modes):
            return True
    return False


_INCOMPLETE_EVIDENCE_MARKERS = (
    "truncated code",
    "truncated context",
    "incomplete code",
    "incomplete context",
    "cannot determine",
    "can't determine",
    "insufficient context",
    "needs verification",
    "needs more context",
)
_BOUNDARY_MISSING_BODY_MARKERS = (
    "cut off",
    "cuts off",
    "truncated",
    "incomplete",
    "provided evidence",
    "diff excerpt",
    "line slice",
    "branch lacks implementation",
    "missing implementation",
    "complete the",
)
_BRANCH_BODY_MARKERS = (
    "return ",
    "result =",
    ".append(",
    "yield ",
    ".join(",
    "raise ",
)


def _candidate_depends_on_incomplete_evidence(candidate: CandidateFinding) -> bool:
    blob = _candidate_evidence_blob(candidate)
    return any(marker in blob for marker in _INCOMPLETE_EVIDENCE_MARKERS)


def _candidate_claims_boundary_missing_body(candidate: CandidateFinding) -> bool:
    blob = _candidate_evidence_blob(candidate)
    if not any(marker in blob for marker in _BOUNDARY_MISSING_BODY_MARKERS):
        return False
    return any(
        marker in blob
        for marker in (
            "branch",
            "body",
            "implementation",
            "return statement",
            "missing return",
            "falls through",
            "fall through",
        )
    )


def _node_span(node: ast.AST) -> tuple[int, int]:
    start = int(getattr(node, "lineno", 1) or 1)
    end = int(getattr(node, "end_lineno", start) or start)
    return start, end


def _target_functions_from_code(
    tree: ast.AST,
    candidate: CandidateFinding,
) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    line_start = int(candidate.line_start or 0)
    blob = _candidate_evidence_blob(candidate)
    funcs: list[ast.FunctionDef | ast.AsyncFunctionDef] = []
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        start, end = _node_span(node)
        if line_start and start <= line_start <= end:
            funcs.append(node)
            continue
        if re.search(rf"(?<![A-Za-z0-9_]){re.escape(node.name.lower())}(?![A-Za-z0-9_])", blob):
            funcs.append(node)
    return funcs


def _stmt_guarantees_exit(stmt: ast.stmt) -> bool:
    if isinstance(stmt, (ast.Return, ast.Raise)):
        return True
    if isinstance(stmt, ast.If):
        return (
            bool(stmt.body and stmt.orelse)
            and _block_guarantees_exit(stmt.body)
            and _block_guarantees_exit(stmt.orelse)
        )
    if isinstance(stmt, ast.Try):
        return bool(stmt.body) and _block_guarantees_exit(stmt.body) and all(
            _block_guarantees_exit(handler.body) for handler in stmt.handlers
        )
    return False


def _block_guarantees_exit(stmts: Sequence[ast.stmt]) -> bool:
    for stmt in stmts:
        if _stmt_guarantees_exit(stmt):
            return True
    return False


def _source_proves_all_target_paths_exit(state: GraphState, candidate: CandidateFinding) -> bool:
    text = _candidate_code_evidence_text(state, candidate)
    if not text.strip():
        return False
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    funcs = _target_functions_from_code(tree, candidate)
    return bool(funcs) and all(_block_guarantees_exit(func.body) for func in funcs)


def _branch_return_claim_contradicted_by_text(
    state: GraphState,
    candidate: CandidateFinding,
    text_blob: str,
) -> bool:
    blob = (text_blob or "").lower()
    if not any(marker in blob for marker in _NO_RETURN_MARKERS):
        return False
    return _source_proves_all_target_paths_exit(state, candidate)


def _branch_return_claim_contradicted(state: GraphState, candidate: CandidateFinding) -> bool:
    blob = _candidate_evidence_blob(candidate)
    return _branch_return_claim_contradicted_by_text(state, candidate, blob)


def _raw_reflection_reject_contradicted_by_code(
    state: GraphState,
    candidate: CandidateFinding,
    reports: Sequence[ReflectionReport],
) -> bool:
    if _revision_accepts(candidate.candidate_id, _revision_map(state.get("metadata", {}) or {})):
        return False
    metadata = state.get("metadata", {}) if isinstance(state.get("metadata"), dict) else {}
    verifier_hints: Dict[str, Any] = dict(metadata.get("verifier_hints") or {})
    if _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints):
        return False
    for report in reports:
        if report.verdict != "reject":
            continue
        rationale = (report.rationale or "").lower()
        if not any(
            marker in rationale
            for marker in (
                "contradict",
                "false positive",
                "incorrect",
                "code shows",
                "actually present",
                "has a return",
                "have explicit returns",
                "does have a return",
            )
        ):
            continue
        if _branch_return_claim_contradicted_by_text(state, candidate, rationale):
            return True
    return False


def _branch_has_nontrivial_body(text: str, branch_name: str) -> bool:
    if not text.strip() or not branch_name.strip():
        return False
    branch = re.escape(branch_name.strip())
    header_re = re.compile(
        rf"^\s*(?:if|elif)\s+.*['\"]{branch}['\"].*:\s*$",
        re.IGNORECASE,
    )
    lines = text.splitlines()
    for idx, raw in enumerate(lines):
        if not header_re.match(raw):
            continue
        indent = len(raw) - len(raw.lstrip(" "))
        for body_line in lines[idx + 1 :]:
            stripped = body_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            body_indent = len(body_line) - len(body_line.lstrip(" "))
            if body_indent <= indent and re.match(r"(elif|else|except|finally|def|class)\b", stripped):
                break
            if any(marker in stripped for marker in _BRANCH_BODY_MARKERS):
                return True
        continue
    return False


def _focused_context_text_for_candidate(state: GraphState, candidate_id: str, file_path: str) -> str:
    norm_fp = (file_path or "").replace("\\", "/").lstrip("/")
    chunks: list[str] = []
    for raw in (state.get("focused_context_results", {}) or {}).values():
        if isinstance(raw, dict):
            result = FocusedContextResult.model_validate(raw)
        else:
            result = raw
        if getattr(result, "candidate_id", None) != candidate_id:
            continue
        for mapping_name in ("file_snippets", "file_contents_full"):
            mapping = getattr(result, mapping_name, {}) or {}
            if not isinstance(mapping, Mapping):
                continue
            for raw_path, body in mapping.items():
                fp = str(raw_path).replace("\\", "/").lstrip("/")
                if not norm_fp or fp == norm_fp or fp.endswith("/" + norm_fp) or norm_fp.endswith("/" + fp):
                    chunks.append(str(body or ""))
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


def _incomplete_claim_contradicted_by_code_evidence(
    state: GraphState,
    candidate: CandidateFinding,
) -> bool:
    if not (
        _candidate_depends_on_incomplete_evidence(candidate)
        or _candidate_claims_boundary_missing_body(candidate)
    ):
        return False
    text = _candidate_code_evidence_text(state, candidate)
    if not text.strip():
        return False
    blob = _candidate_evidence_blob(candidate)
    quoted = _quoted_scope_terms(blob)
    branch_terms = [term for term in quoted if term.lower() in _EXTRACTION_MODE_MARKERS]
    branch_terms.extend(term for term in _EXTRACTION_MODE_MARKERS if term in blob)
    branch_terms = list(dict.fromkeys(branch_terms))
    if branch_terms and any(_branch_has_nontrivial_body(text, term) for term in branch_terms):
        return True
    return False


def _incomplete_claim_has_positive_absence_evidence(
    state: GraphState,
    candidate: CandidateFinding,
) -> bool:
    if not _candidate_claims_boundary_missing_body(candidate):
        return True
    text = _candidate_code_evidence_text(state, candidate)
    blob = _candidate_evidence_blob(candidate)
    branch_terms = [term for term in _quoted_scope_terms(blob) if term.lower() in _EXTRACTION_MODE_MARKERS]
    branch_terms.extend(term for term in _EXTRACTION_MODE_MARKERS if term in blob)
    branch_terms = list(dict.fromkeys(branch_terms))
    if branch_terms:
        return bool(text.strip()) and all(not _branch_has_nontrivial_body(text, term) for term in branch_terms)
    return False


def _task_evidence_file_text(state: GraphState, candidate: CandidateFinding) -> str:
    metadata = state.get("metadata") if isinstance(state.get("metadata"), dict) else {}
    pipe = metadata.get("critique_pipeline") if isinstance(metadata.get("critique_pipeline"), dict) else {}
    by_task = pipe.get("by_task") if isinstance(pipe.get("by_task"), dict) else {}
    task_ids = [candidate.patch_task_id] + [tid for tid in by_task if tid != candidate.patch_task_id]
    norm_fp = (candidate.file_path or "").replace("\\", "/").lstrip("/")
    chunks: list[str] = []
    for tid in task_ids:
        slot = by_task.get(tid) if isinstance(by_task, dict) else None
        if not isinstance(slot, dict):
            continue
        te = slot.get("task_evidence") if isinstance(slot.get("task_evidence"), dict) else {}
        for key in ("rendered_units", "file_contents"):
            files = te.get(key) if isinstance(te.get(key), dict) else {}
            for raw_path, body in files.items():
                fp = str(raw_path).replace("\\", "/").lstrip("/")
                if fp == norm_fp or fp.endswith("/" + norm_fp) or norm_fp.endswith("/" + fp):
                    chunks.append(str(body or ""))
        rendered = str(te.get("rendered") or "")
        if rendered and norm_fp and norm_fp in rendered:
            chunks.append(rendered)
    return "\n\n".join(chunk for chunk in chunks if chunk.strip())


def _candidate_code_evidence_text(state: GraphState, candidate: CandidateFinding) -> str:
    return "\n\n".join(
        chunk
        for chunk in (
            _task_evidence_file_text(state, candidate),
            _focused_context_text_for_candidate(state, candidate.candidate_id, candidate.file_path),
        )
        if chunk.strip()
    )


def _quoted_scope_terms(text: str) -> list[str]:
    terms = re.findall(r"'([^']{2,80})'|\"([^\"]{2,80})\"", text)
    out: list[str] = []
    for a, b in terms:
        term = (a or b).strip()
        if term and term.lower() not in {"try", "except", "else", "finally"}:
            out.append(term)
    return out[:4]


def _line_inside_indented_try(lines: list[str], line_no: int) -> bool:
    if line_no < 1 or line_no > len(lines):
        return False
    active: list[int] = []
    for idx, raw in enumerate(lines, start=1):
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        active = [level for level in active if stripped == "" or indent > level]
        if stripped.startswith(("except", "finally")) and stripped.endswith(":"):
            active = [level for level in active if indent > level]
        if idx == line_no:
            return bool(active)
        if stripped.startswith("try") and stripped.endswith(":"):
            active.append(indent)
    return False


def _scope_claim_contradicted(state: GraphState, candidate: CandidateFinding) -> bool:
    blob = _candidate_evidence_blob(candidate)
    says_outside = any(marker in blob for marker in _SCOPE_OUTSIDE_MARKERS)
    says_inside = any(marker in blob for marker in _SCOPE_INSIDE_MARKERS)
    if not says_outside and not says_inside:
        return False
    text = _task_evidence_file_text(state, candidate)
    if not text.strip():
        return False
    lines = text.splitlines()
    probe_lines: list[int] = []
    for term in _quoted_scope_terms(blob):
        for idx, raw in enumerate(lines, start=1):
            if term in raw.lower():
                probe_lines.append(idx)
                break
    if not probe_lines:
        probe_lines.append(int(candidate.line_start or 1))
    inside = any(_line_inside_indented_try(lines, line_no) for line_no in probe_lines)
    if says_outside and inside:
        return True
    if says_inside and not inside:
        return True
    return False


def _broad_risk_without_impact_path(candidate: CandidateFinding) -> bool:
    if not _resource_oriented_candidate(candidate):
        return False
    blob = _candidate_evidence_blob(candidate)
    if any(marker in blob for marker in _BROAD_RISK_BOUNDARY_MARKERS):
        return False
    if candidate.required_context:
        return False
    return True


def _has_concrete_boundary_context(candidate: CandidateFinding) -> bool:
    blob = _candidate_evidence_blob(candidate)
    return any(marker in blob for marker in _BROAD_RISK_BOUNDARY_MARKERS)


def _optimization_without_impact(candidate: CandidateFinding) -> bool:
    if not _resource_oriented_candidate(candidate):
        return False
    blob = _candidate_evidence_blob(candidate)
    optimization_markers = ("cache", "caching", "compile cost", "faster", "overhead", "optimization")
    impact_markers = ("regression", "failure", "timeout", "exhaust", "crash", "wrong output", "data loss")
    return any(marker in blob for marker in optimization_markers) and not any(
        marker in blob for marker in impact_markers
    )


def _resource_claim_has_promotion_support(
    candidate: CandidateFinding,
    relevant_reports: Sequence[ReflectionReport],
    *,
    has_focused_context: bool,
    revision_accepted: bool,
    verifier_concrete: bool,
    concrete_local_supported: bool,
) -> bool:
    if not _resource_oriented_candidate(candidate):
        return True
    if verifier_concrete or revision_accepted or has_focused_context or concrete_local_supported:
        return True
    return _accepted_local_source_supported_claim(candidate, relevant_reports)


def _redirect_has_independent_support(
    candidate: CandidateFinding,
    raw_reports: Sequence[ReflectionReport],
    redirect_category: ReviewCategory,
    *,
    has_focused_context: bool,
    revision_accepted: bool,
    verifier_concrete: bool,
) -> bool:
    if revision_accepted or verifier_concrete:
        return True
    domain_reports = [report for report in raw_reports if report.reflector_specialty == redirect_category]
    if any(report.verdict in {"accept", "reclassify"} for report in domain_reports):
        return True
    if any(report.verdict in {"needs_context", "needs_verification"} for report in domain_reports):
        return has_focused_context
    if candidate.claim_type == "security_risk" and _has_concrete_boundary_context(candidate):
        return True
    return has_focused_context and _has_concrete_boundary_context(candidate) and not _optimization_without_impact(candidate)


def _language_defined_preference_without_contract(
    candidate: CandidateFinding,
    reports: Sequence[ReflectionReport],
) -> bool:
    normalized = candidate_with_behavioral_metadata(candidate)
    if (
        normalized.behavioral_symptom,
        normalized.root_operation,
    ) in {
        ("missing_return", "dispatch"),
        ("data_loss", "indexing"),
        ("crash", "aggregation"),
    }:
        return False
    blob = _candidate_evidence_blob(candidate)
    if any(marker in blob for marker in ("return_types", "declared return", "must return", "documented project")):
        return False
    language_report = False
    for report in reports:
        rationale = (report.rationale or "").lower()
        if any(
            marker in rationale
            for marker in (
                "documented behavior",
                "well-defined",
                "valid regex",
                "valid pattern",
                "not a bug",
                "not a defect",
                "design choice",
                "technically correct",
                "default behavior",
                "python semantics",
            )
        ):
            language_report = True
            break
    if not language_report:
        return False
    return any(
        marker in blob
        for marker in (
            "negative indices",
            "out-of-range",
            "out of range",
            "empty pattern",
            "empty regex",
            "document the behavior",
            "unexpected behavior",
        )
    )


def _high_risk_claim_needs_external_context(candidate: CandidateFinding) -> bool:
    """When claim_type is security_risk or performance_regression, decide if promotion requires focused hits."""
    blob = _candidate_evidence_blob(candidate)
    if any(marker in blob for marker in _TIER2_EXTERNAL_CONTEXT_MARKERS):
        return True
    if candidate_has_local_defect_signature(candidate):
        return False
    return True


def _candidate_requires_context(candidate: CandidateFinding) -> bool:
    if candidate.required_context:
        return True
    if _resource_oriented_candidate(candidate):
        return _high_risk_claim_needs_external_context(candidate)
    if candidate.claim_type not in CONTEXT_REQUIRED_CLAIM_TYPES:
        return False
    return _high_risk_claim_needs_external_context(candidate)


def _accepted_local_source_supported_claim(
    candidate: CandidateFinding,
    reports: Sequence[ReflectionReport],
) -> bool:
    """A relevant accept can settle source-local claims despite stale required_context text."""
    accepting_reports = [report for report in reports if report.verdict in {"accept", "reclassify"}]
    if not accepting_reports:
        return False
    if any(report.support_scope == "local" for report in accepting_reports):
        return True
    if any(
        report.support_scope in {"needs_context", "runtime_dependent", "unclear"}
        for report in accepting_reports
    ):
        return False
    return candidate_has_local_defect_signature(candidate)


def _revision_accepts(candidate_id: str, revisions: Mapping[str, Mapping[str, Any]]) -> bool:
    rev = revisions.get(candidate_id) or {}
    return str(rev.get("verdict", "")).lower() == "accept"


def _revision_overrides_reflector_reject(
    state: GraphState,
    candidate_id: str,
    revisions: Mapping[str, Mapping[str, Any]],
    verifier_hints: Mapping[str, Any],
) -> bool:
    """Post-revision accept supersedes an earlier reflector reject when second-pass evidence exists."""
    if not _revision_accepts(candidate_id, revisions):
        return False
    if _focused_hits_for_candidate(state, candidate_id):
        return True
    if _verifier_concrete_behavior_verified(candidate_id, verifier_hints):
        return True
    hint = verifier_hints.get(candidate_id)
    if isinstance(hint, dict) and str(hint.get("verdict", "")).lower() == "verified":
        rationale = str(hint.get("final_rationale") or "").lower()
        if "mismatch" in rationale:
            return True
    return False


def _verifier_harness_error(candidate_id: str, verifier_hints: Mapping[str, Any]) -> bool:
    hint = verifier_hints.get(candidate_id)
    return isinstance(hint, dict) and bool(hint.get("harness_error"))


def _revision_evidence_extra(
    candidate: CandidateFinding,
    revision: Mapping[str, Any],
) -> str:
    summary = revision.get("updated_evidence_summary")
    if not isinstance(summary, str) or not summary.strip():
        return ""
    if revision_summary_conflicts_with_claim(
        content=candidate.content or "",
        failure_mode=candidate.failure_mode or "",
        evidence_summary=candidate.evidence_summary or "",
        revision_summary=summary,
    ):
        return ""
    return f"\n\nPost-context evidence: {summary}"


def _revision_accept_has_concrete_support(
    candidate: CandidateFinding,
    revision: Mapping[str, Any],
) -> bool:
    return False


def _render_revision_support_audit_prompt(
    *,
    candidate: CandidateFinding,
    reports: Sequence[ReflectionReport],
    revision: Mapping[str, Any],
    verifier_hint: Mapping[str, Any] | None,
    focused_context: Sequence[Mapping[str, Any]],
) -> str:
    return (
        "Audit whether a critique-revision accept resolves an earlier unresolved reflection.\n"
        "Return resolved only when the revision supplies concrete source, focused-context, or runtime evidence "
        "that directly answers the uncertainty in the needs_context or needs_verification reports.\n"
        "Return unresolved when the revision mostly restates the claim, uses generic language, or verifier evidence "
        "is inconclusive/harness-only. Return unsupported if the provided material is insufficient to judge.\n\n"
        f"Candidate JSON:\n{candidate.model_dump_json()}\n\n"
        f"Relevant Reflection Reports JSON:\n{json_for_cleanup_prompt([r.model_dump(mode='json') for r in reports])}\n\n"
        f"Revision JSON:\n{json_for_cleanup_prompt(dict(revision))}\n\n"
        f"Focused Context JSON:\n{json_for_cleanup_prompt(list(focused_context))}\n\n"
        f"Verifier Advisory JSON:\n{json_for_cleanup_prompt(dict(verifier_hint or {}))}"
    )


def json_for_cleanup_prompt(value: Any, *, max_chars: int = 12000) -> str:
    import json

    text = json.dumps(value, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...<truncated>"


def _focused_context_payload_for_candidate(state: GraphState, candidate_id: str) -> List[Mapping[str, Any]]:
    raw = state.get("focused_context_results") or {}
    items = raw.values() if isinstance(raw, dict) else raw
    out: List[Mapping[str, Any]] = []
    for item in items or []:
        if isinstance(item, FocusedContextResult):
            row = item.model_dump(mode="json")
        elif isinstance(item, dict):
            row = dict(item)
        else:
            continue
        if str(row.get("candidate_id") or "") == candidate_id:
            out.append(row)
    return out[:3]


def _lines_overlap_or_close(a: ReviewFinding, b: ReviewFinding, *, distance: int = 40) -> bool:
    if a.line_end < b.line_start:
        return b.line_start - a.line_end <= distance
    if b.line_end < a.line_start:
        return a.line_start - b.line_end <= distance
    return True


def _reflection_duplicate_hint(reports: Sequence[ReflectionReport]) -> bool:
    return any("duplicate" in str(report.rationale or "").lower() for report in reports)


def _semantic_equivalence_pairs(
    findings: Sequence[ReviewFinding],
    raw_by_cand: Mapping[str, Sequence[ReflectionReport]],
) -> List[Dict[str, Any]]:
    pairs: List[Dict[str, Any]] = []
    keys = {finding.id: review_finding_semantic_key(finding) for finding in findings}
    for left_index, left in enumerate(findings):
        for right in findings[left_index + 1 :]:
            left_key = keys[left.id]
            right_key = keys[right.id]
            same_file = left_key[0] == right_key[0]
            same_subject = bool(left_key[1] and left_key[1] == right_key[1])
            close_lines = left.file_path == right.file_path and _lines_overlap_or_close(left, right)
            left_reports = list(raw_by_cand.get(left.id, []))
            right_reports = list(raw_by_cand.get(right.id, []))
            duplicate_hint = _reflection_duplicate_hint(left_reports) or _reflection_duplicate_hint(right_reports)
            if not (same_file and (same_subject or close_lines or duplicate_hint)):
                continue
            pairs.append(
                {
                    "finding_a": left.model_dump(mode="json"),
                    "finding_b": right.model_dump(mode="json"),
                    "selection_reason": {
                        "same_subject": same_subject,
                        "close_lines": close_lines,
                        "reflection_duplicate_hint": duplicate_hint,
                    },
                    "reflection_a": [report.model_dump(mode="json") for report in left_reports[:3]],
                    "reflection_b": [report.model_dump(mode="json") for report in right_reports[:3]],
                }
            )
            if len(pairs) >= 8:
                return pairs
    return pairs


def _render_semantic_equivalence_audit_prompt(pairs: Sequence[Mapping[str, Any]]) -> str:
    return (
        "Audit possible duplicate final review findings.\n"
        "Return same_issue only when two findings describe the same root cause, the same violated behavior, "
        "and substantially the same practical fix. Preserve distinct findings when they cover different "
        "dimensions, symptoms, triggers, data elements, or fixes, even on the same surface.\n"
        "Return distinct for nearby but orthogonal issues. Return unsupported if the provided material is "
        "insufficient to decide.\n\n"
        f"Pairs JSON:\n{json_for_cleanup_prompt(list(pairs), max_chars=18000)}"
    )


def _line_precision_score(finding: ReviewFinding) -> int:
    if finding.line_start <= 0 or finding.line_end <= 0 or finding.line_end < finding.line_start:
        return -1000
    return -1 * (finding.line_end - finding.line_start)


def _finding_content_precision_score(finding: ReviewFinding) -> int:
    text = " ".join(
        [
            finding.content,
            str(getattr(finding, "failure_mode", "") or ""),
            str(getattr(finding, "evidence_summary", "") or ""),
            finding.recommendation,
        ]
    ).lower()
    score = 0
    if finding.id.endswith(":source-only") or "source-only" in text or "source local" in text:
        score -= 2
    if re.match(r"^\s*class\s+\w+\s*(?:\([^)]*\))?\s*:?\s*$", finding.content):
        score -= 2
    if finding.content.strip().lower().startswith(("does ", "check ", "verify ")):
        score -= 1
    evidence_summary = str(getattr(finding, "evidence_summary", "") or "")
    if len(evidence_summary.strip()) >= 40:
        score += 1
    if len(finding.content.strip()) >= 40:
        score += 1
    return score


def _preferred_semantic_equivalence_keeper(
    left: ReviewFinding,
    right: ReviewFinding,
    order: Mapping[str, int],
) -> tuple[str, str]:
    left_score = (
        _finding_content_precision_score(left),
        _line_precision_score(left),
        len(str(getattr(left, "evidence_summary", "") or "").strip()),
        -order[left.id],
    )
    right_score = (
        _finding_content_precision_score(right),
        _line_precision_score(right),
        len(str(getattr(right, "evidence_summary", "") or "").strip()),
        -order[right.id],
    )
    if right_score > left_score:
        return right.id, left.id
    return left.id, right.id


def _apply_semantic_equivalence_audit(
    findings: Sequence[ReviewFinding],
    audit: SemanticEquivalenceAuditOutput,
) -> tuple[List[ReviewFinding], Dict[str, List[str]]]:
    by_id = {finding.id: finding for finding in findings}
    order = {finding.id: index for index, finding in enumerate(findings)}
    duplicates: Dict[str, List[str]] = {}
    dropped: set[str] = set()
    for item in audit.items:
        if item.verdict != "same_issue":
            continue
        left = item.finding_id
        right = item.equivalent_to
        if left not in by_id or right not in by_id or left == right:
            continue
        if left in dropped or right in dropped:
            continue
        keeper, duplicate = _preferred_semantic_equivalence_keeper(
            by_id[left],
            by_id[right],
            order,
        )
        dropped.add(duplicate)
        duplicates.setdefault(keeper, []).append(duplicate)
    return [finding for finding in findings if finding.id not in dropped], duplicates


def _verifier_concrete_behavior_verified(candidate_id: str, verifier_hints: Mapping[str, Any]) -> bool:
    hint = verifier_hints.get(candidate_id)
    if not isinstance(hint, dict):
        return False
    if str(hint.get("verdict", "")).lower() != "verified":
        return False
    if str(hint.get("verification_scope", "")) != "concrete_behavior":
        return False
    if hint.get("harness_error"):
        return False
    # Prefer explicit product proof flag from verifier finalize; fall back for older hints.
    if "product_verified" in hint:
        return bool(hint.get("product_verified"))
    final_rationale = str(hint.get("final_rationale") or "").lower()
    return "mismatch" in final_rationale or "target file" in final_rationale


def _verifier_evidence_extra(
    candidate_id: str,
    verifier_hints: Mapping[str, Any],
    existing: str = "",
) -> str:
    if not _verifier_concrete_behavior_verified(candidate_id, verifier_hints):
        return ""
    hint = verifier_hints.get(candidate_id)
    if not isinstance(hint, dict):
        return ""
    summary = str(hint.get("updated_evidence_summary") or hint.get("final_rationale") or "").strip()
    if not summary or summary in existing:
        return ""
    return f"\n\nRuntime verifier evidence: {summary}"


def _required_context_satisfied_by_verifier(
    candidate: CandidateFinding,
    candidate_id: str,
    verifier_hints: Mapping[str, Any],
) -> bool:
    """Runtime concrete_behavior proof supersedes doc/test required_context for localized defects."""
    if not _verifier_concrete_behavior_verified(candidate_id, verifier_hints):
        return False
    if candidate.claim_type != "defect":
        return False
    return candidate_has_local_defect_signature(candidate)


def _missing_contract_proof_fields(candidate: CandidateFinding) -> List[str]:
    missing: List[str] = []
    if not candidate.evidence_for_contract.strip():
        missing.append("evidence_for_contract")
    if not candidate.counterexample.strip():
        missing.append("counterexample")
    if not candidate.rejection_check.strip():
        missing.append("rejection_check")
    return missing


def _contract_proof_is_weak(candidate: CandidateFinding) -> bool:
    blob = " ".join(
        [
            candidate.evidence_for_contract,
            candidate.counterexample,
            candidate.rejection_check,
        ]
    ).lower()
    return any(
        marker in blob
        for marker in (
            "may be intentional",
            "might be intentional",
            "could be intentional",
            "may not align with user expectations",
            "clarify whether",
        )
    )


def _misroute_redirect_category(not_applicable_reports: Sequence[ReflectionReport]) -> ReviewCategory | None:
    for report in not_applicable_reports:
        parsed = parse_misroute_redirect_category(report.rationale)
        if parsed is not None:
            return parsed
        if report.reclassified_category:
            return report.reclassified_category
    return None


def make_adversarial_cleanup_node(settings: Settings | None = None):
    node_name = "adversarial_cleanup"

    def adversarial_cleanup_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        candidates: List[CandidateFinding] = []
        for raw in state.get("candidate_findings", []) or []:
            if isinstance(raw, CandidateFinding):
                candidates.append(raw)
            elif isinstance(raw, dict):
                candidates.append(CandidateFinding.model_validate(raw))
        raw_by_cand = _reports_by_candidate(state.get("reflection_reports", []) or [])
        reports = consolidate_reflection_reports(state.get("reflection_reports", []) or [])
        metadata = dict(state.get("metadata", {}))
        revisions = _revision_map(metadata)
        verifier_hints: Dict[str, Any] = dict(metadata.get("verifier_hints") or {})
        resolved_settings = settings or get_settings()
        selected_model = getattr(resolved_settings, "reviewer_worker_model_key", None)
        llm_tokens = 0
        llm_trace: List[Dict[str, Any]] = []
        revision_support_audits: Dict[str, Dict[str, Any]] = {}

        if not candidates:
            return {
                "findings": [],
                "metadata": metadata,
                "node_history": [f"{node_name}:empty"],
            }
        candidates = [
            candidate_with_behavioral_metadata(candidate)
            for candidate in ensure_unique_candidate_ids(candidates)
        ]

        git_diff = (state.get("git_diff", "") or "")[:50000]
        changed_files = changed_files_from_diff(git_diff)
        ast_files = metadata.get("ast_included_files") or []
        if isinstance(ast_files, list):
            for raw_path in ast_files:
                if isinstance(raw_path, str) and raw_path.strip():
                    changed_files.add(raw_path.strip().replace("\\", "/"))
        candidates, semantic_duplicates = dedupe_candidates_by_signature(candidates, git_diff=git_diff)
        claim_tiering = classify_candidates(candidates, metadata=metadata)
        metadata["claim_tiering"] = {"by_candidate": claim_tiering}

        by_cand = _reports_by_candidate(reports)
        cleanup_settings = settings or get_settings()
        promoted: List[ReviewFinding] = []
        dropped: List[str] = []
        ignored_rejections: Dict[str, List[str]] = {}
        ignored_context_requests: Dict[str, List[str]] = {}
        missing_required_reflections: Dict[str, List[str]] = {}
        misrouted_candidates: Dict[str, List[Dict[str, str]]] = {}
        recommendation_reference_advisories: List[str] = []
        lifecycle: Dict[str, Dict[str, Any]] = {}
        contract_proof_drops: Dict[str, List[str]] = {
            "missing_contract_proof": [],
            "weak_contract_proof": [],
        }

        def drop(candidate: CandidateFinding, reason: str, details: Dict[str, Any] | None = None) -> None:
            dropped.append(candidate.candidate_id)
            if reason in contract_proof_drops:
                contract_proof_drops[reason].append(candidate.candidate_id)
            tier_meta = claim_tiering.get(candidate.candidate_id) or {}
            lifecycle[candidate.candidate_id] = {
                "decision": "dropped",
                "reason": reason,
                "claim_tier": tier_meta.get("tier"),
                "claim_type": candidate.claim_type,
                "suspected_category": candidate.suspected_category,
                **(details or {}),
            }

        for candidate in candidates:
            candidate = candidate_with_behavioral_metadata(candidate)
            verifier_verified_early = _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
            claim_tier = classify_claim_tier(
                candidate,
                review_kb_context=review_kb_context_for_candidate(metadata, candidate),
            )
            claim_tiering.setdefault(candidate.candidate_id, {})["tier"] = claim_tier
            rev_early = revisions.get(candidate.candidate_id) or {}
            rev_summary_early = str(rev_early.get("updated_evidence_summary") or "")
            cand_reports_early = by_cand.get(candidate.candidate_id, [])
            category_early = _final_category(candidate, cand_reports_early)
            relevant_reports_early = [
                report
                for report in cand_reports_early
                if report.reflector_specialty in _relevant_reflectors(candidate, category_early)
            ]
            concrete_local_supported = _concrete_local_candidate_supported(
                state,
                candidate,
                relevant_reports_early,
                changed_files,
            )
            if is_resolution_only_finding(
                candidate.content,
                candidate.recommendation or "",
                candidate.evidence_summary,
                rev_summary_early,
            ):
                drop(candidate, "resolution_only_not_promotable")
                continue
            if is_required_upstream_none_guard_claim(candidate):
                drop(candidate, "required_param_none_guard_out_of_scope")
                continue
            if claim_tier == "speculative_guard" and not (
                candidate.claim_type == "security_risk" and security_boundary_is_concrete(candidate)
            ) and not concrete_local_supported:
                drop(candidate, "speculative_guard_without_concrete_regression")
                continue
            if not verifier_verified_early and _scope_claim_contradicted(state, candidate):
                drop(candidate, "scope_claim_contradicted_by_code_evidence")
                continue
            if not verifier_verified_early and _branch_return_claim_contradicted(state, candidate):
                drop(candidate, "branch_return_claim_contradicted_by_code_evidence")
                continue
            if not verifier_verified_early and _incomplete_claim_contradicted_by_code_evidence(state, candidate):
                drop(candidate, "incomplete_claim_contradicted_by_code_evidence")
                continue
            early_harness_error = _verifier_harness_error(candidate.candidate_id, verifier_hints)
            early_revision_accepted = _revision_accepts(candidate.candidate_id, revisions)
            if (
                claim_tier == "coverage_gap"
                and candidate.severity != "high"
                and not early_revision_accepted
                and not _focused_hits_for_candidate(state, candidate.candidate_id)
                and not concrete_local_supported
            ):
                drop(candidate, "coverage_gap_without_high_impact_evidence")
                continue
            if (
                _candidate_depends_on_incomplete_evidence(candidate)
                and not early_revision_accepted
                and not _focused_hits_for_candidate(state, candidate.candidate_id)
                and not verifier_verified_early
            ):
                drop(candidate, "incomplete_evidence_without_followup")
                continue
            if (
                _broad_risk_without_impact_path(candidate)
                and not (
                    candidate.claim_type == "security_risk"
                    and early_harness_error
                    and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
                    and not _focused_hits_for_candidate(state, candidate.candidate_id)
                    and not early_revision_accepted
                )
            ):
                drop(candidate, "broad_risk_without_concrete_impact_path")
                continue

            if changed_files:
                resolved_path = resolve_repo_file_path(candidate.file_path, changed_files)
                if resolved_path is None:
                    drop(candidate, "file_path_not_in_changed_set", {"file_path": candidate.file_path})
                    continue
                if resolved_path != candidate.file_path:
                    candidate = candidate.model_copy(update={"file_path": resolved_path})

            if recommendation_cites_foreign_class(
                content=candidate.content,
                failure_mode=candidate.failure_mode,
                evidence_summary=candidate.evidence_summary,
                recommendation=candidate.recommendation or "",
            ):
                recommendation_reference_advisories.append(candidate.candidate_id)

            cand_reports = by_cand.get(candidate.candidate_id, [])
            specialties = {r.reflector_specialty for r in cand_reports}
            missing = EXPECTED_REFLECTORS - specialties
            if missing and _trace_enabled(state):
                trace_logger.info(
                    "TRACE cleanup_missing_reflectors run_id=%s candidate=%s missing=%s",
                    run_id,
                    candidate.candidate_id,
                    sorted(missing),
                )

            category = _final_category(candidate, cand_reports)
            relevant_reflectors = _relevant_reflectors(candidate, category)
            missing_relevant = relevant_reflectors - specialties

            relevant_reports = [
                report for report in cand_reports if report.reflector_specialty in relevant_reflectors
            ]
            raw_relevant_reports = [
                report
                for report in raw_by_cand.get(candidate.candidate_id, cand_reports)
                if report.reflector_specialty in relevant_reflectors
            ]

            abstaining_reflectors: frozenset[str] | None = None
            if missing_relevant:
                require_full = cleanup_settings.reviewer_cleanup_require_full_reflection_quorum
                if require_full or not relevant_reports:
                    missing_required_reflections[candidate.candidate_id] = sorted(missing_relevant)
                    drop(
                        candidate,
                        "missing_required_reflection",
                        {"expected_reflectors": sorted(relevant_reflectors)},
                    )
                    continue
                abstaining_reflectors = frozenset(missing_relevant)
                if _trace_enabled(state):
                    trace_logger.info(
                        "TRACE cleanup_partial_reflection_quorum run_id=%s candidate=%s abstaining=%s "
                        "reports_from=%s",
                        run_id,
                        candidate.candidate_id,
                        sorted(missing_relevant),
                        sorted({r.reflector_specialty for r in relevant_reports}),
                    )

            relevant_needs_verification = any(
                report.verdict == "needs_verification" for report in relevant_reports
            )
            has_focused_context = _focused_hits_for_candidate(state, candidate.candidate_id)
            revision_accepted = _revision_accepts(candidate.candidate_id, revisions)
            rev = revisions.get(candidate.candidate_id) or {}
            revision_supported = _revision_accept_has_concrete_support(candidate, rev)
            if (
                revision_accepted
                and not revision_supported
                and (any(report.verdict == "needs_context" for report in relevant_reports) or relevant_needs_verification)
                and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
            ):
                try:
                    llm = Models.worker(RevisionSupportAuditOutput, model_key=selected_model, max_completion_tokens=900)
                    prompt = _render_revision_support_audit_prompt(
                        candidate=candidate,
                        reports=relevant_reports,
                        revision=rev,
                        verifier_hint=verifier_hints.get(candidate.candidate_id),
                        focused_context=_focused_context_payload_for_candidate(state, candidate.candidate_id),
                    )
                    traced = trace_llm_call(
                        llm,
                        prompt,
                        state=state,
                        node_name="adversarial_cleanup_revision_support_audit",
                        model_key=selected_model,
                        schema_name="RevisionSupportAuditOutput",
                        input_summary={"candidate_id": candidate.candidate_id},
                    )
                    audit = parse_structured_output(traced.result, RevisionSupportAuditOutput)
                    llm_tokens += traced.tokens
                    llm_trace.extend(traced.trace_records)
                    revision_support_audits[candidate.candidate_id] = audit.model_dump(mode="json")
                    revision_supported = audit.verdict == "resolved"
                except Exception as exc:  # noqa: BLE001
                    llm_trace.extend(trace_from_exception(exc))
                    revision_support_audits[candidate.candidate_id] = {
                        "verdict": "unsupported",
                        "rationale": f"audit_failed:{exc.__class__.__name__}: {exc}",
                        "warnings": ["revision_support_audit_failed"],
                    }
            revision_overrides_reject = _revision_overrides_reflector_reject(
                state,
                candidate.candidate_id,
                revisions,
                verifier_hints,
            )
            off_domain_reports = [
                report for report in cand_reports if report.reflector_specialty not in relevant_reflectors
            ]
            off_domain_rejections = [
                report.reflector_specialty for report in off_domain_reports if report.verdict == "reject"
            ]
            if off_domain_rejections:
                ignored_rejections[candidate.candidate_id] = off_domain_rejections

            if (
                any(report.verdict in {"accept", "reclassify"} for report in raw_relevant_reports)
                and any(report.verdict == "reject" for report in raw_relevant_reports)
                and _raw_reflection_reject_contradicted_by_code(
                    state,
                    candidate,
                    raw_relevant_reports,
                )
                and not revision_accepted
                and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
            ):
                drop(
                    candidate,
                    "raw_reflection_reject_contradicted_by_code_evidence",
                    {
                        "verdicts": [
                            f"{report.reflector_specialty}:{report.verdict}"
                            for report in raw_relevant_reports
                        ],
                    },
                )
                continue

            if (
                _candidate_claims_boundary_missing_body(candidate)
                and has_focused_context
                and not _incomplete_claim_has_positive_absence_evidence(state, candidate)
            ):
                drop(candidate, "boundary_missing_body_without_positive_absence_evidence")
                continue

            if not relevant_reports:
                drop(
                    candidate,
                    "missing_relevant_reflection",
                    {"expected_reflectors": sorted(relevant_reflectors)},
                )
                continue

            if (
                _reflection_family_or_surface_conflict(candidate, raw_relevant_reports)
                and not revision_accepted
                and not has_focused_context
                and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
            ):
                drop(
                    candidate,
                    "reflection_family_conflict_without_followup",
                    {
                        "verdicts": [
                            f"{report.reflector_specialty}:{report.verdict}"
                            for report in raw_relevant_reports
                        ],
                    },
                )
                continue

            not_applicable_reports = [
                report for report in relevant_reports if report.verdict == "not_applicable"
            ]
            affirmative_reports = [
                report
                for report in relevant_reports
                if report.verdict in {"accept", "reclassify", "needs_context", "needs_verification"}
            ]
            if not_applicable_reports:
                misrouted_candidates[candidate.candidate_id] = [
                    {
                        "reflector_specialty": report.reflector_specialty,
                        "rationale": report.rationale,
                    }
                    for report in not_applicable_reports
                ]
            if not_applicable_reports and not affirmative_reports:
                redirect_category = _misroute_redirect_category(not_applicable_reports)
                if (
                    redirect_category
                    and redirect_category in DOMAIN_REFLECTORS
                    and redirect_category not in relevant_reflectors
                    and _candidate_has_actionability(candidate)
                    and candidate.claim_type in PROMOTABLE_CLAIM_TYPES
                ):
                    if not _redirect_has_independent_support(
                        candidate,
                        raw_by_cand.get(candidate.candidate_id, cand_reports),
                        redirect_category,
                        has_focused_context=has_focused_context,
                        revision_accepted=revision_accepted,
                        verifier_concrete=_verifier_concrete_behavior_verified(
                            candidate.candidate_id, verifier_hints
                        ),
                    ):
                        drop(
                            candidate,
                            "off_domain_redirect_without_independent_support",
                            {"redirect_category": redirect_category},
                        )
                        continue
                    if (
                        candidate.claim_type == "security_risk"
                        and _verifier_harness_error(candidate.candidate_id, verifier_hints)
                        and not _verifier_concrete_behavior_verified(
                            candidate.candidate_id, verifier_hints
                        )
                        and not has_focused_context
                        and not revision_accepted
                    ):
                        drop(candidate, "security_unverified_harness_error")
                        continue
                    missing_contract_proof = _missing_contract_proof_fields(candidate)
                    if missing_contract_proof:
                        drop(
                            candidate,
                            "missing_contract_proof",
                            {"missing_fields": missing_contract_proof},
                        )
                        continue
                    if _contract_proof_is_weak(candidate):
                        drop(candidate, "weak_contract_proof")
                        continue
                    category = redirect_category
                    feedback_type = _category_to_feedback(category)  # type: ignore[arg-type]
                    rev = revisions.get(candidate.candidate_id) or {}
                    evidence_extra = _revision_evidence_extra(candidate, rev)
                    evidence_extra += _verifier_evidence_extra(
                        candidate.candidate_id,
                        verifier_hints,
                        existing=evidence_extra,
                    )
                    runtime_note = ""
                    if _verifier_harness_error(candidate.candidate_id, verifier_hints):
                        runtime_note = "\n\n(runtime unverified: verifier harness error)"
                    promoted.append(
                        ReviewFinding(
                            id=candidate.candidate_id,
                            file_path=candidate.file_path,
                            line_start=candidate.line_start,
                            line_end=candidate.line_end,
                            content=candidate.content + evidence_extra + runtime_note,
                            severity=candidate.severity,
                            feedback_type=feedback_type,  # type: ignore[arg-type]
                            recommendation=candidate.recommendation,
                            references=[],
                            behavioral_symptom=candidate.behavioral_symptom,
                            root_operation=candidate.root_operation,
                            evidence_for_contract=candidate.evidence_for_contract,
                            counterexample=candidate.counterexample,
                            rejection_check=candidate.rejection_check,
                        )
                    )
                    lifecycle[candidate.candidate_id] = {
                        "decision": "promoted",
                        "reason": "misroute_recovered_from_not_applicable",
                        "claim_tier": claim_tier,
                        "claim_type": candidate.claim_type,
                        "final_category": category,
                        "relevant_reflectors": sorted(relevant_reflectors),
                        "redirect_category": redirect_category,
                        "had_focused_context": _focused_hits_for_candidate(state, candidate.candidate_id),
                    }
                    continue
                drop(
                    candidate,
                    "misrouted_not_applicable",
                    {"reports": misrouted_candidates[candidate.candidate_id]},
                )
                continue

            if any(r.verdict == "reject" for r in relevant_reports) and not revision_overrides_reject:
                drop(
                    candidate,
                    "relevant_reflector_reject",
                    {
                        "rejecting_reflectors": [
                            report.reflector_specialty
                            for report in relevant_reports
                            if report.verdict == "reject"
                        ],
                    },
                )
                continue

            if not (
                any(
                    r.verdict in {"accept", "reclassify", "needs_context", "needs_verification"}
                    for r in relevant_reports
                )
                or revision_overrides_reject
            ):
                drop(
                    candidate,
                    "no_relevant_acceptance",
                    {"verdicts": [report.verdict for report in relevant_reports]},
                )
                continue

            if candidate.claim_type not in PROMOTABLE_CLAIM_TYPES:
                drop(candidate, "non_promotable_claim_type")
                continue

            if _language_defined_preference_without_contract(candidate, raw_relevant_reports):
                drop(candidate, "language_defined_behavior_without_project_contract")
                continue

            if not _candidate_has_actionability(candidate):
                drop(
                    candidate,
                    "missing_actionability_fields",
                    {
                        "has_failure_mode": bool(candidate.failure_mode.strip()),
                        "has_evidence_summary": bool(candidate.evidence_summary.strip()),
                        "has_recommendation": bool((candidate.recommendation or "").strip()),
                    },
                )
                continue

            off_domain_context = [
                report.reflector_specialty for report in off_domain_reports if report.verdict == "needs_context"
            ]
            if off_domain_context:
                ignored_context_requests[candidate.candidate_id] = off_domain_context

            needs_context = any(r.verdict == "needs_context" for r in relevant_reports)
            requires_context = _candidate_requires_context(candidate)
            harness_error = _verifier_harness_error(candidate.candidate_id, verifier_hints)
            if (
                _optimization_without_impact(candidate)
                and not revision_supported
                and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
            ):
                drop(candidate, "optimization_without_concrete_impact")
                continue
            if not (
                candidate.claim_type == "security_risk" and harness_error
            ) and not _resource_claim_has_promotion_support(
                candidate,
                relevant_reports,
                has_focused_context=has_focused_context,
                revision_accepted=revision_supported,
                verifier_concrete=_verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints),
                concrete_local_supported=concrete_local_supported,
            ):
                drop(candidate, "resource_risk_without_concrete_support")
                continue
            if (
                candidate.claim_type == "security_risk"
                and harness_error
                and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
                and not has_focused_context
                and not revision_accepted
            ):
                drop(candidate, "security_unverified_harness_error")
                continue

            verifier_satisfies_context = _required_context_satisfied_by_verifier(
                candidate, candidate.candidate_id, verifier_hints
            )
            context_requirement_overridden = False
            if requires_context and not has_focused_context:
                context_requirement_overridden = (
                    _accepted_local_source_supported_claim(candidate, relevant_reports)
                    or revision_supported
                    or verifier_satisfies_context
                )
                if (
                    candidate.claim_type == "security_risk"
                    and harness_error
                    and not verifier_satisfies_context
                    and not revision_supported
                ):
                    context_requirement_overridden = False
            if requires_context and not has_focused_context and not context_requirement_overridden:
                drop(
                    candidate,
                    "required_context_not_gathered",
                    {
                        "required_context": list(candidate.required_context),
                        "has_focused_context": has_focused_context,
                        "verifier_concrete_verified": _verifier_concrete_behavior_verified(
                            candidate.candidate_id, verifier_hints
                        ),
                        "revision_ran": bool(revisions),
                    },
                )
                if _trace_enabled(state):
                    trace_logger.info(
                        "TRACE cleanup_required_context_drop run_id=%s candidate=%s "
                        "required_context=%s focused=%s verifier_concrete=%s",
                        run_id,
                        candidate.candidate_id,
                        candidate.required_context,
                        has_focused_context,
                        _verifier_concrete_behavior_verified(
                            candidate.candidate_id, verifier_hints
                        ),
                    )
                continue

            if needs_context or relevant_needs_verification:
                verdict = str(rev.get("verdict", "")).lower()
                if verdict == "reject":
                    drop(candidate, "revision_reject")
                    continue
                hint = verifier_hints.get(candidate.candidate_id)
                if isinstance(hint, dict) and hint.get("harness_error"):
                    pass
                elif (
                    isinstance(hint, dict)
                    and verdict == "accept"
                    and verifier_refutation_applies(
                        candidate.model_dump(mode="json"),
                        verifier_verdict=str(hint.get("verdict", "")),
                        verification_scope=str(hint.get("verification_scope", "")),
                        harness_error=bool(hint.get("harness_error")),
                    )
                ):
                    drop(candidate, "verifier_refuted_concrete_behavior")
                    continue
                verified_hint = (
                    isinstance(hint, dict)
                    and str(hint.get("verdict", "")).lower() == "verified"
                    and not hint.get("harness_error")
                )
                only_unresolved_reflection = not any(
                    report.verdict in {"accept", "reclassify"} for report in relevant_reports
                )
                if (
                    only_unresolved_reflection
                    and harness_error
                    and not verified_hint
                    and not _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
                ):
                    drop(candidate, "needs_context_with_inconclusive_verifier")
                    continue
                if (
                    verdict == "accept"
                    and not (
                        revision_supported
                        or verified_hint
                        or _verifier_concrete_behavior_verified(candidate.candidate_id, verifier_hints)
                    )
                ):
                    drop(candidate, "needs_context_without_concrete_followup")
                    continue
                if (
                    verdict != "accept"
                    and not has_focused_context
                    and not harness_error
                    and not (relevant_needs_verification and verified_hint)
                ):
                    drop(candidate, "needs_context_without_supporting_revision")
                    continue

            feedback_type = _category_to_feedback(category)  # type: ignore[arg-type]
            evidence_extra = _revision_evidence_extra(candidate, rev)
            evidence_extra += _verifier_evidence_extra(
                candidate.candidate_id,
                verifier_hints,
                existing=evidence_extra,
            )
            if harness_error and revision_accepted and "runtime unverified" not in evidence_extra.lower():
                evidence_extra += "\n\n(runtime unverified: verifier harness error)"

            missing_contract_proof = _missing_contract_proof_fields(candidate)
            if missing_contract_proof:
                drop(
                    candidate,
                    "missing_contract_proof",
                    {"missing_fields": missing_contract_proof},
                )
                continue
            if _contract_proof_is_weak(candidate):
                drop(candidate, "weak_contract_proof")
                continue

            promoted.append(
                ReviewFinding(
                    id=candidate.candidate_id,
                    file_path=candidate.file_path,
                    line_start=candidate.line_start,
                    line_end=candidate.line_end,
                    content=candidate.content + evidence_extra,
                    severity=candidate.severity,
                    feedback_type=feedback_type,  # type: ignore[arg-type]
                    recommendation=candidate.recommendation,
                    references=[],
                    behavioral_symptom=candidate.behavioral_symptom,
                    root_operation=candidate.root_operation,
                    evidence_for_contract=candidate.evidence_for_contract,
                    counterexample=candidate.counterexample,
                    rejection_check=candidate.rejection_check,
                )
            )
            promote_reason = (
                "critique_revision_accept_overrides_reject"
                if revision_overrides_reject
                else "accepted_by_relevant_reflectors"
            )
            lifecycle[candidate.candidate_id] = {
                "decision": "promoted",
                "reason": promote_reason,
                "claim_tier": claim_tier,
                "claim_type": candidate.claim_type,
                "final_category": category,
                "relevant_reflectors": sorted(relevant_reflectors),
                "had_focused_context": has_focused_context,
            }
            if revision_overrides_reject:
                lifecycle[candidate.candidate_id]["overridden_rejecting_reflectors"] = [
                    report.reflector_specialty
                    for report in relevant_reports
                    if report.verdict == "reject"
                ]
            if abstaining_reflectors:
                lifecycle[candidate.candidate_id]["abstaining_reflectors"] = sorted(abstaining_reflectors)
            if context_requirement_overridden:
                if revision_accepted:
                    lifecycle[candidate.candidate_id]["context_requirement_overridden"] = (
                        "critique_revision_accept"
                    )
                elif verifier_satisfies_context:
                    lifecycle[candidate.candidate_id]["context_requirement_overridden"] = (
                        "runtime_verifier_concrete_behavior"
                    )
                elif harness_error:
                    lifecycle[candidate.candidate_id]["context_requirement_overridden"] = (
                        "verifier_harness_error"
                    )
                else:
                    lifecycle[candidate.candidate_id]["context_requirement_overridden"] = (
                        "localized_defect_accepted_by_relevant_reflector"
                    )
            if candidate.candidate_id in verifier_hints:
                lifecycle[candidate.candidate_id]["verifier_advisory"] = verifier_hints[candidate.candidate_id]

        severity_rank = {"high": 0, "medium": 1, "low": 2}
        promoted = sorted(
            promoted,
            key=lambda item: (
                severity_rank.get(item.severity, 99),
                item.file_path,
                item.line_start,
                item.id,
            ),
        )
        resource_kept: set[tuple[str, str, str]] = set()
        resource_filtered: List[ReviewFinding] = []
        dropped_resource_ids: List[str] = []
        for finding in promoted:
            key = review_finding_semantic_key(finding)
            if _resource_oriented_finding(finding):
                scope = (key[0], key[1], key[4])
                if scope in resource_kept:
                    dropped_resource_ids.append(finding.id)
                    continue
                resource_kept.add(scope)
            resource_filtered.append(finding)
        resource_filtered = ensure_unique_finding_ids(resource_filtered)
        promoted, finding_duplicates = dedupe_review_findings_by_signature(resource_filtered)
        promoted = ensure_unique_finding_ids(promoted)
        semantic_equivalence_audits: List[Dict[str, Any]] = []
        semantic_equivalence_duplicates: Dict[str, List[str]] = {}
        semantic_equivalence_warnings: List[str] = []
        equivalence_pairs = _semantic_equivalence_pairs(promoted, raw_by_cand)
        if equivalence_pairs:
            try:
                llm = Models.worker(
                    SemanticEquivalenceAuditOutput,
                    model_key=selected_model,
                    max_completion_tokens=1200,
                )
                traced = trace_llm_call(
                    llm,
                    _render_semantic_equivalence_audit_prompt(equivalence_pairs),
                    state=state,
                    node_name="adversarial_cleanup_semantic_equivalence_audit",
                    model_key=selected_model,
                    schema_name="SemanticEquivalenceAuditOutput",
                    input_summary={"pair_count": len(equivalence_pairs)},
                )
                audit = parse_structured_output(traced.result, SemanticEquivalenceAuditOutput)
                llm_tokens += traced.tokens
                llm_trace.extend(traced.trace_records)
                semantic_equivalence_audits = [item.model_dump(mode="json") for item in audit.items]
                semantic_equivalence_warnings.extend(audit.warnings)
                promoted, semantic_equivalence_duplicates = _apply_semantic_equivalence_audit(promoted, audit)
            except Exception as exc:  # noqa: BLE001
                llm_trace.extend(trace_from_exception(exc))
                semantic_equivalence_warnings.append(
                    f"semantic_equivalence_audit_failed:{exc.__class__.__name__}: {exc}"
                )
        promoted = ensure_unique_finding_ids(promoted)
        dropped_semantic_finding_ids = [
            fid for ids in finding_duplicates.values() for fid in ids
        ] + [
            fid for ids in semantic_equivalence_duplicates.values() for fid in ids
        ] + dropped_resource_ids

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE adversarial_cleanup run_id=%s promoted=%s dropped=%s",
                run_id,
                len(promoted),
                dropped,
            )

        cleanup_meta = {
            "promoted_count": len(promoted),
            "dropped_candidate_ids": dropped,
            "ignored_off_domain_rejections": ignored_rejections,
            "ignored_off_domain_context_requests": ignored_context_requests,
            "missing_required_reflections": missing_required_reflections,
            "misrouted_candidate_ids": misrouted_candidates,
            "recommendation_reference_advisories": recommendation_reference_advisories,
            "candidate_lifecycle": lifecycle,
            "contract_proof_drops": {
                "missing_contract_proof_candidate_ids": contract_proof_drops["missing_contract_proof"],
                "weak_contract_proof_candidate_ids": contract_proof_drops["weak_contract_proof"],
                "missing_contract_proof_count": len(contract_proof_drops["missing_contract_proof"]),
                "weak_contract_proof_count": len(contract_proof_drops["weak_contract_proof"]),
            },
            "dropped_semantic_duplicate_finding_ids": dropped_semantic_finding_ids,
        }
        if semantic_duplicates:
            cleanup_meta["semantic_dedupe_duplicates"] = semantic_duplicates
        if finding_duplicates:
            cleanup_meta["semantic_dedupe_finding_duplicates"] = finding_duplicates
        if semantic_equivalence_audits:
            cleanup_meta["semantic_equivalence_audits"] = semantic_equivalence_audits
        if semantic_equivalence_duplicates:
            cleanup_meta["semantic_equivalence_duplicates"] = semantic_equivalence_duplicates
        if semantic_equivalence_warnings:
            cleanup_meta["semantic_equivalence_warnings"] = semantic_equivalence_warnings
        if revision_support_audits:
            cleanup_meta["revision_support_audits"] = revision_support_audits
        metadata["adversarial_cleanup"] = cleanup_meta

        return {
            "findings": promoted,
            "metadata": metadata,
            "node_history": [node_name],
            "token_usage": llm_tokens,
            "llm_trace": llm_trace,
        }

    return adversarial_cleanup_node

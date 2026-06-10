"""Fulfill bounded focused-context requests after reflection."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Sequence

from src.domain.interfaces import IGitHubContextProvider
from src.domain.schemas import FocusedContextRequest, FocusedContextResult, ReflectionReport
from src.domain.state import GraphState
from src.orchestration.context.focused_query_sanitize import sanitize_focused_context_request
from src.orchestration.context.review_context import BoundedReviewContextFulfiller, LazyReviewContextProvider

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _coerce_focus_request(raw: Any) -> FocusedContextRequest | None:
    if isinstance(raw, FocusedContextRequest):
        return raw
    if isinstance(raw, dict):
        try:
            return FocusedContextRequest.model_validate(raw)
        except Exception:
            return None
    return None


def _coerce_reflection(raw: Any) -> ReflectionReport | None:
    if isinstance(raw, ReflectionReport):
        return raw
    if isinstance(raw, dict):
        try:
            return ReflectionReport.model_validate(raw)
        except Exception:
            return None
    return None


def _pending_requests(state: GraphState) -> List[FocusedContextRequest]:
    """Collect deduped requests from reducer list and embedded reflection reports."""
    seen: set[tuple[str, str]] = set()
    pending: List[FocusedContextRequest] = []
    for req in state.get("focused_context_requests", []) or []:
        coerced = _coerce_focus_request(req)
        if coerced is None:
            continue
        key = _request_result_key(coerced)
        if key in seen:
            continue
        seen.add(key)
        pending.append(coerced)
    for raw in state.get("reflection_reports", []) or []:
        report = _coerce_reflection(raw)
        if report is None:
            continue
        if report.verdict != "needs_context" or report.focused_request is None:
            continue
        nested = _coerce_focus_request(report.focused_request)
        if nested is None:
            continue
        key = _request_result_key(nested)
        if key in seen:
            continue
        seen.add(key)
        pending.append(nested)
    return pending


def _request_result_key(request: FocusedContextRequest) -> tuple[str, str]:
    return (str(request.candidate_id or ""), str(request.request_id or ""))


def _result_storage_key(request: FocusedContextRequest) -> str:
    candidate_id, request_id = _request_result_key(request)
    return f"{candidate_id}:{request_id}" if candidate_id else request_id


def _existing_result_for_request(
    existing: Mapping[str, Any],
    request: FocusedContextRequest,
) -> FocusedContextResult | None:
    storage_key = _result_storage_key(request)
    candidates = [existing.get(storage_key), existing.get(request.request_id)]
    for raw in existing.values():
        candidates.append(raw)
    for raw in candidates:
        if raw is None:
            continue
        try:
            result = raw if isinstance(raw, FocusedContextResult) else FocusedContextResult.model_validate(raw)
        except Exception:
            continue
        if result.candidate_id == request.candidate_id and result.request_id == request.request_id:
            return result
    return None


def _norm_path(path: str) -> str:
    return path.strip().replace("\\", "/").lstrip("/")


def _request_paths(request: FocusedContextRequest) -> list[str]:
    return sorted({_norm_path(path) for path in request.file_paths if str(path).strip()})


def _result_paths(result: FocusedContextResult) -> list[str]:
    paths: set[str] = set()
    paths.update(_norm_path(path) for path in result.file_snippets if path != "repository_kb_context")
    paths.update(_norm_path(path) for path in result.file_contents_full)
    for rows in result.search_hits.values():
        for hit in rows or []:
            path = hit.file_path if hasattr(hit, "file_path") else (
                hit.get("file_path") if isinstance(hit, Mapping) else ""
            )
            if path:
                paths.add(_norm_path(str(path)))
    return sorted(path for path in paths if path)


def _has_hits(result: FocusedContextResult) -> bool:
    if any(str(body or "").strip() for body in result.file_snippets.values()):
        return True
    if any(str(body or "").strip() for body in result.file_contents_full.values()):
        return True
    return any(rows for rows in result.search_hits.values())


def _queries_changed(before: FocusedContextRequest, after: FocusedContextRequest) -> bool:
    return (
        list(before.symbol_queries) != list(after.symbol_queries)
        or list(before.text_queries) != list(after.text_queries)
    )


def _diagnostic_row(
    request: FocusedContextRequest,
    *,
    outcomes: Sequence[str],
    result: FocusedContextResult | None = None,
    warning: str | None = None,
) -> Dict[str, Any]:
    requested = _request_paths(request)
    effective = _result_paths(result) if result is not None else []
    return {
        "request_id": request.request_id,
        "candidate_id": request.candidate_id,
        "requested_paths": requested,
        "effective_paths": effective,
        "outcomes": list(dict.fromkeys(outcomes)),
        "warnings": ([warning] if warning else []) + (list(result.warnings) if result is not None else []),
    }


def make_focused_context_node(
    context_provider: LazyReviewContextProvider,
    github_provider: IGitHubContextProvider | None = None,
):
    node_name = "focused_context"
    fulfiller = BoundedReviewContextFulfiller(
        context_provider,
        github_provider=github_provider,
    )

    def focused_context_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        existing = dict(state.get("focused_context_results", {}) or {})
        pending = _pending_requests(state)
        if not pending:
            metadata = dict(state.get("metadata", {}))
            fc_meta = dict(metadata.get("focused_context", {}) or {})
            fc_meta["dispatch_status"] = "not_dispatched"
            metadata["focused_context"] = fc_meta
            return {"metadata": metadata, "node_history": [f"{node_name}:skipped"]}

        merged: Dict[str, FocusedContextResult] = {}
        warnings: List[str] = []
        diagnostics: List[Dict[str, Any]] = []
        for req in pending:
            sanitized = sanitize_focused_context_request(req)
            base_outcomes = ["sanitized_query"] if _queries_changed(req, sanitized) else []
            storage_key = _result_storage_key(sanitized)
            existing_model = _existing_result_for_request(existing, sanitized)
            if existing_model is not None:
                diagnostics.append(
                    _diagnostic_row(
                        sanitized,
                        outcomes=[*base_outcomes, "already_fulfilled"],
                        result=existing_model,
                    )
                )
                continue
            try:
                result = fulfiller.fulfill(
                    state,
                    sanitized,
                    existing_result=existing_model,
                )
                merged[storage_key] = result
                outcomes = list(base_outcomes)
                if not _has_hits(result):
                    outcomes.append("no_hits")
                requested_paths = set(_request_paths(sanitized))
                effective_paths = set(_result_paths(result))
                if requested_paths and effective_paths and not requested_paths.intersection(effective_paths):
                    outcomes.append("path_mismatch")
                if any(str(w).startswith("truncated") for w in result.warnings):
                    outcomes.append("budget_omission")
                diagnostics.append(_diagnostic_row(sanitized, outcomes=outcomes or ["fulfilled"], result=result))
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"fulfill_failed:{req.request_id}:{exc.__class__.__name__}: {exc}")
                diagnostics.append(
                    _diagnostic_row(
                        sanitized,
                        outcomes=[*base_outcomes, "tool_unavailable"],
                        warning=f"{exc.__class__.__name__}: {exc}",
                    )
                )
                logger.warning(
                    "focused_context fulfill failed run_id=%s request_id=%s reason=%s",
                    run_id,
                    req.request_id,
                    exc,
                )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE focused_context run_id=%s new_results=%s pending_seen=%s",
                run_id,
                list(merged.keys()),
                len(pending),
            )

        metadata = dict(state.get("metadata", {}))
        fc_meta = dict(metadata.get("focused_context", {}) or {})
        fc_meta["fulfilled_ids"] = sorted(set(fc_meta.get("fulfilled_ids", [])) | set(merged.keys()))
        fc_meta["warnings"] = list(fc_meta.get("warnings", [])) + warnings
        prev_diagnostics = fc_meta.get("diagnostics")
        fc_meta["diagnostics"] = (list(prev_diagnostics) if isinstance(prev_diagnostics, list) else []) + diagnostics
        effective_paths = {
            path
            for row in fc_meta["diagnostics"]
            if isinstance(row, Mapping)
            for path in row.get("effective_paths", [])
            if isinstance(path, str) and path
        }
        requested_paths = {
            path
            for row in fc_meta["diagnostics"]
            if isinstance(row, Mapping)
            for path in row.get("requested_paths", [])
            if isinstance(path, str) and path
        }
        fc_meta["focused_effective_path_count"] = len(effective_paths)
        fc_meta["focused_requested_path_count"] = len(requested_paths)
        metadata["focused_context"] = fc_meta

        return {
            "focused_context_results": merged,
            "metadata": metadata,
            "node_history": [node_name],
        }

    return focused_context_node

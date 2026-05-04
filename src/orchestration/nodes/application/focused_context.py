"""Fulfill bounded focused-context requests after reflection."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.domain.schemas import FocusedContextRequest, FocusedContextResult, ReflectionReport
from src.domain.state import GraphState
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
    seen: set[str] = set()
    pending: List[FocusedContextRequest] = []
    for req in state.get("focused_context_requests", []) or []:
        coerced = _coerce_focus_request(req)
        if coerced is None:
            continue
        if coerced.request_id in seen:
            continue
        seen.add(coerced.request_id)
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
        rid = nested.request_id
        if rid in seen:
            continue
        seen.add(rid)
        pending.append(nested)
    return pending


def make_focused_context_node(context_provider: LazyReviewContextProvider):
    node_name = "focused_context"
    fulfiller = BoundedReviewContextFulfiller(context_provider)

    def focused_context_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        existing = dict(state.get("focused_context_results", {}) or {})
        pending = _pending_requests(state)
        if not pending:
            return {"node_history": [f"{node_name}:skipped"]}

        merged: Dict[str, FocusedContextResult] = {}
        warnings: List[str] = []
        for req in pending:
            if req.request_id in existing:
                continue
            existing_val = existing.get(req.request_id)
            existing_model: FocusedContextResult | None = None
            if isinstance(existing_val, FocusedContextResult):
                existing_model = existing_val
            elif isinstance(existing_val, dict):
                existing_model = FocusedContextResult.model_validate(existing_val)
            try:
                merged[req.request_id] = fulfiller.fulfill(
                    state,
                    req,
                    existing_result=existing_model,
                )
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"fulfill_failed:{req.request_id}:{exc.__class__.__name__}: {exc}")
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
        metadata["focused_context"] = fc_meta

        return {
            "focused_context_results": merged,
            "metadata": metadata,
            "node_history": [node_name],
        }

    return focused_context_node

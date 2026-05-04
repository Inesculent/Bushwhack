"""Second-pass critique after focused context for candidates that needed evidence."""

from __future__ import annotations

import logging
from typing import Any, Dict, List

from src.config import get_settings
from src.domain.schemas import (
    CandidateFinding,
    CritiqueRevisionOutput,
    FocusedContextResult,
    ReflectionReport,
)
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _needs_revision_candidates(state: GraphState) -> List[str]:
    ids: set[str] = set()
    for raw in state.get("reflection_reports", []) or []:
        if isinstance(raw, ReflectionReport):
            report = raw
        elif isinstance(raw, dict):
            try:
                report = ReflectionReport.model_validate(raw)
            except Exception:
                continue
        else:
            continue
        if report.verdict == "needs_context":
            ids.add(report.candidate_id)
    return sorted(ids)


def _render_revision_bundle(state: GraphState, candidate_ids: List[str]) -> str:
    candidates: List[CandidateFinding] = []
    for raw in state.get("candidate_findings", []) or []:
        if isinstance(raw, CandidateFinding):
            candidates.append(raw)
        elif isinstance(raw, dict):
            candidates.append(CandidateFinding.model_validate(raw))
    candidates = [c for c in candidates if c.candidate_id in candidate_ids]
    results = state.get("focused_context_results", {}) or {}
    parts: List[str] = []
    for cand in candidates:
        parts.append(f"### Candidate {cand.candidate_id}\n{cand.model_dump_json()}")
        for _rid, raw_res in results.items():
            if isinstance(raw_res, FocusedContextResult):
                res = raw_res
            elif isinstance(raw_res, dict):
                res = FocusedContextResult.model_validate(raw_res)
            else:
                continue
            if res.candidate_id == cand.candidate_id:
                parts.append(f"#### Focused context {res.request_id}\n{res.model_dump_json()}")
    return "\n\n".join(parts)


def _has_focused_evidence(state: GraphState, candidate_ids: List[str]) -> bool:
    for raw in (state.get("focused_context_results", {}) or {}).values():
        if isinstance(raw, FocusedContextResult):
            res = raw
        elif isinstance(raw, dict):
            try:
                res = FocusedContextResult.model_validate(raw)
            except Exception:
                continue
        else:
            continue
        if res.candidate_id in candidate_ids and (res.file_snippets or res.search_hits):
            return True
    return False


def make_critique_revision_node(model_key: str | None = None, use_llm: bool = True):
    node_name = "critique_revision"

    def critique_revision_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        candidate_ids = _needs_revision_candidates(state)
        if not candidate_ids:
            return {"node_history": [f"{node_name}:skipped"]}

        if not _has_focused_evidence(state, candidate_ids):
            return {"node_history": [f"{node_name}:skipped_no_results"]}

        prompt = render_reviewer_prompt(
            "critique_revision.md",
            {
                "Candidates And Focused Context": _render_revision_bundle(state, candidate_ids),
                "Git Diff Excerpt": (state.get("git_diff", "") or "")[:8000],
            },
        )

        revisions: List[dict[str, Any]] = []
        warnings: List[str] = []

        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(CritiqueRevisionOutput, model_key=selected_model)
                response = llm.invoke(prompt)
                revisions = [item.model_dump() for item in response.revisions]
                warnings.extend(response.warnings)
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"critique_revision_llm_failed:{exc.__class__.__name__}: {exc}")
                logger.warning(
                    "%s failed run_id=%s reason=%s: %s",
                    node_name,
                    run_id,
                    exc.__class__.__name__,
                    exc,
                )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE critique_revision run_id=%s candidates=%s revisions=%s",
                run_id,
                candidate_ids,
                len(revisions),
            )

        metadata = dict(state.get("metadata", {}))
        metadata["critique_revision"] = {
            "revisions": revisions,
            "warnings": warnings,
        }

        return {
            "metadata": metadata,
            "node_history": [node_name],
        }

    return critique_revision_node

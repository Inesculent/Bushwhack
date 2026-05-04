from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Protocol

from pydantic import BaseModel, Field

from src.config import get_settings
from src.domain.schemas import CodeEntity, ReviewFinding, ReviewTask, ReviewerWorkerReport, SearchResult
from src.domain.state import GraphState
from src.infrastructure.llm.factory import Models
from src.orchestration.prompts.renderer import render_reviewer_prompt

logger = logging.getLogger(__name__)
trace_logger = logging.getLogger("research_pipeline.reviewer_trace")

SUPPORTED_SPECIALTIES = {"security", "logic", "performance", "general"}


@dataclass(frozen=True)
class ReviewTaskContext:
    explored_files: List[str] = field(default_factory=list)
    file_snippets: Dict[str, str] = field(default_factory=dict)
    entities_by_file: Dict[str, List[CodeEntity]] = field(default_factory=dict)
    search_results: Dict[str, List[SearchResult]] = field(default_factory=dict)
    warnings: List[str] = field(default_factory=list)

    def render(self, max_chars: int = 24000) -> str:
        sections: List[str] = []
        if self.file_snippets:
            sections.append("File excerpts:")
            for file_path, content in self.file_snippets.items():
                sections.append(f"\n--- {file_path} ---\n{content[:5000]}")
        if self.entities_by_file:
            sections.append("\nAST entities:")
            for file_path, entities in self.entities_by_file.items():
                entity_lines = [
                    f"- {entity.type} {entity.name}: {entity.signature}"
                    for entity in entities[:20]
                ]
                sections.append(f"\n--- {file_path} ---\n" + "\n".join(entity_lines))
        if self.search_results:
            sections.append("\nSearch results:")
            for query, results in self.search_results.items():
                result_lines = [
                    f"- {result.file_path}:{result.line_number}: {result.content}"
                    for result in results[:20]
                ]
                sections.append(f"\nQuery: {query}\n" + "\n".join(result_lines))
        if self.warnings:
            sections.append("\nContext warnings:\n" + "\n".join(f"- {warning}" for warning in self.warnings))
        return "\n".join(sections)[:max_chars]


class ReviewContextProvider(Protocol):
    def collect_for_task(self, state: GraphState, task: ReviewTask) -> ReviewTaskContext:
        """Collect direct repository context for one review task."""


class WorkerReviewOutput(BaseModel):
    summary: str = Field(description="Brief evidence summary for this specialist review.")
    findings: List[ReviewFinding] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


def _task_from_state(state: GraphState, expected_specialty: str) -> ReviewTask | None:
    task_id = state.get("current_task_id")
    registry = state.get("task_registry", {}) or {}
    if not task_id or task_id not in registry:
        return None
    task = registry[task_id]
    if task.specialty != expected_specialty:
        return None
    return task


def _trace_enabled(state: GraphState) -> bool:
    metadata = state.get("metadata", {}) or {}
    return bool(metadata.get("review_trace_enabled"))


def _render_worker_prompt(
    state: GraphState,
    task: ReviewTask,
    context: ReviewTaskContext,
    specialty: str,
) -> str:
    return render_reviewer_prompt(
        f"workers/{specialty}.md",
        {
            "Assigned Task": (
                f"Task ID: {task.id}\n"
                f"Task title: {task.title}\n"
                f"Task description: {task.description}\n"
                f"Target files: {task.target_files}"
            ),
            "Direct Context Gathered By Tools": context.render(),
            "Git Diff Excerpt": (state.get("git_diff", "") or "")[:16000],
        },
    )


def _normalize_findings(task: ReviewTask, findings: List[ReviewFinding]) -> List[ReviewFinding]:
    normalized: List[ReviewFinding] = []
    for index, finding in enumerate(findings, start=1):
        finding_id = finding.id.strip() or f"{task.id}:finding-{index}"
        if not finding_id.startswith(task.id):
            finding_id = f"{task.id}:{finding_id}"
        normalized.append(finding.model_copy(update={"id": finding_id}))
    return normalized


def make_specialist_worker_node(
    specialty: str,
    context_provider: ReviewContextProvider,
    model_key: str | None = None,
    use_llm: bool = True,
):
    if specialty not in SUPPORTED_SPECIALTIES:
        raise ValueError(f"Unsupported reviewer worker specialty: {specialty}")

    node_name = f"{specialty}_review_worker"

    def specialist_worker_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        task = _task_from_state(state, specialty)
        if task is None:
            return {"node_history": [f"{node_name}:skipped"]}

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE worker_start run_id=%s node=%s task_id=%s specialty=%s files=%s",
                run_id,
                node_name,
                task.id,
                task.specialty,
                task.target_files,
            )

        context = context_provider.collect_for_task(state=state, task=task)
        findings: List[ReviewFinding] = []
        warnings = list(context.warnings)
        summary = ""

        if use_llm:
            selected_model = model_key or getattr(get_settings(), "reviewer_worker_model_key", None)
            try:
                llm = Models.worker(WorkerReviewOutput, model_key=selected_model)
                response = llm.invoke(_render_worker_prompt(state, task, context, specialty))
                findings = _normalize_findings(task=task, findings=response.findings)
                warnings.extend(response.warnings)
                summary = response.summary
            except Exception as exc:  # noqa: BLE001 - a single specialist should not fail the whole review
                warning = f"worker_llm_failed:{exc.__class__.__name__}: {exc}"
                warnings.append(warning)
                logger.warning(
                    "%s failed run_id=%s task_id=%s reason=%s: %s",
                    node_name,
                    run_id,
                    task.id,
                    exc.__class__.__name__,
                    exc,
                )

        if _trace_enabled(state):
            trace_logger.info(
                "TRACE worker_done run_id=%s node=%s task_id=%s findings=%s explored_files=%s warnings=%s",
                run_id,
                node_name,
                task.id,
                len(findings),
                context.explored_files,
                warnings,
            )

        report = ReviewerWorkerReport(
            task_id=task.id,
            specialty=task.specialty,
            explored_files=context.explored_files,
            context_summary=summary,
            warnings=warnings,
        )

        return {
            "findings": findings,
            "reviewer_worker_reports": [report],
            "task_status_by_id": {task.id: "completed"},
            "node_history": [node_name],
        }

    return specialist_worker_node

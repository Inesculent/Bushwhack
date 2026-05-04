from typing import TypedDict, List, Annotated, Dict, Any, Literal, Required, NotRequired
import operator
from .schemas import (
    CandidateFinding,
    FocusedContextRequest,
    FocusedContextResult,
    PreflightParseIssue,
    PreflightSummary,
    ReflectionReport,
    RepositoryMap,
    ReviewTask,
    ReviewFinding,
    ReviewerWorkerReport,
    StructuralExtractionGap,
    StructuralTopologySummary,
    TaskStatus,
)


def merge_graph_metadata(
    left: Dict[str, Any] | None,
    right: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Deep-merge metadata dicts so parallel nodes (e.g. general_critiquer) can update disjoint keys safely."""
    merged: Dict[str, Any] = dict(left or {})
    for key, val in (right or {}).items():
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = merge_graph_metadata(merged[key], val)
        else:
            merged[key] = val
    return merged


class GraphState(TypedDict, total=False):
    # Required identity and inputs
    run_id: Required[str]
    repo_path: Required[str]
    git_diff: Required[str]

    # Context
    user_goals: NotRequired[str]
    repo_map: NotRequired[RepositoryMap]
    next_step: NotRequired[Literal["explore", "plan", "review", "finalize"]]
    global_insights: Annotated[List[str], operator.add]

    # Optional references for externalized payloads (e.g., Redis-backed cache blobs)
    cache_refs: NotRequired[Dict[str, str]]
    diff_manifest_ref: NotRequired[str]
    preflight_summary: NotRequired[PreflightSummary]
    preflight_errors: Annotated[List[PreflightParseIssue], operator.add]
    preflight_warnings: Annotated[List[str], operator.add]
    structural_graph_node_link: NotRequired[Dict[str, Any]]
    structural_topology: NotRequired[StructuralTopologySummary]
    structural_extraction_gaps: Annotated[List[StructuralExtractionGap], operator.add]

    # Task state: canonical task payloads + lifecycle status by task id.
    # Dict union reducers support compact per-task updates that are cache-friendly.
    root_task_id: NotRequired[str]
    task_registry: Annotated[Dict[str, ReviewTask], operator.or_]
    task_status_by_id: Annotated[Dict[str, TaskStatus], operator.or_]

    # Results
    findings: Annotated[List[ReviewFinding], operator.add]
    reviewer_worker_reports: Annotated[List[ReviewerWorkerReport], operator.add]
    final_findings: NotRequired[List[ReviewFinding]]

    # Adversarial review loop (critiquer → reflection → focused context → cleanup)
    candidate_findings: Annotated[List[CandidateFinding], operator.add]
    reflection_reports: Annotated[List[ReflectionReport], operator.add]
    focused_context_requests: Annotated[List[FocusedContextRequest], operator.add]
    focused_context_results: Annotated[Dict[str, FocusedContextResult], operator.or_]

    # Data for debugging and analysis
    current_task_id: NotRequired[str]
    metadata: Annotated[Dict[str, Any], merge_graph_metadata]
    token_usage: Annotated[int, operator.add]
    node_history: Annotated[List[str], operator.add]


from typing import TypedDict, List, Annotated, Dict, Any, Literal, Required, NotRequired
import operator
from .schemas import (
    PreflightParseIssue,
    PreflightSummary,
    RepositoryMap,
    ReviewTask,
    ReviewFinding,
    ReviewerWorkerReport,
    StructuralExtractionGap,
    StructuralTopologySummary,
    TaskStatus,
)


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

    # Data for debugging and analysis
    current_task_id: NotRequired[str]
    metadata: NotRequired[Dict[str, Any]]
    token_usage: Annotated[int, operator.add]
    node_history: Annotated[List[str], operator.add]


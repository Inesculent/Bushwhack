from typing import TypedDict, List, Annotated, Dict, Any, Literal, Required, NotRequired
import operator
from .schemas import RepositoryMap, ReviewTask, ReviewFinding, TaskStatus


class GraphState(TypedDict, total=False):
    # Required identity and inputs
    run_id: Required[str]
    repo_path: Required[str]
    git_diff: Required[str]

    # Context
    repo_map: NotRequired[RepositoryMap]
    next_step: NotRequired[Literal["explore", "plan", "review", "finalize"]]
    global_insights: Annotated[List[str], operator.add]

    # Optional references for externalized payloads (e.g., Redis-backed cache blobs)
    cache_refs: NotRequired[Dict[str, str]]

    # Task state: canonical task payloads + lifecycle status by task id.
    # Dict union reducers support compact per-task updates that are cache-friendly.
    root_task_id: NotRequired[str]
    task_registry: Annotated[Dict[str, ReviewTask], operator.or_]
    task_status_by_id: Annotated[Dict[str, TaskStatus], operator.or_]

    # Results
    findings: Annotated[List[ReviewFinding], operator.add]

    # Data for debugging and analysis
    metadata: NotRequired[Dict[str, Any]]
    token_usage: Annotated[int, operator.add]
    node_history: Annotated[List[str], operator.add]


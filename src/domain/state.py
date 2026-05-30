from typing import TypedDict, List, Annotated, Dict, Any, Literal, Required, NotRequired
import operator
from .schemas import (
    CandidateFinding,
    CommunitySemanticSummary,
    CritiqueRevisionDigest,
    FocusedContextRequest,
    FocusedContextResult,
    KnowledgeGap,
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
    UnverifiedCallTarget,
)
from .verifier_schemas import (
    VerificationScope,
    VerifierAttemptRecord,
    VerifierReport,
    VerifierVerdict,
)


def merge_graph_metadata(
    left: Dict[str, Any] | None,
    right: Dict[str, Any] | None,
) -> Dict[str, Any]:
    """Deep-merge metadata dicts so parallel nodes (e.g. general_critiquer) can update disjoint keys safely."""

    def _norm_ast_path(p: str) -> str:
        return p.strip().replace("\\", "/")

    merged: Dict[str, Any] = dict(left or {})
    for key, val in (right or {}).items():
        if key == "ast_included_files":
            prev = merged.get(key)
            left_list = list(prev) if isinstance(prev, list) else []
            right_list = list(val) if isinstance(val, list) else []
            merged[key] = sorted(
                {
                    _norm_ast_path(str(p))
                    for p in left_list + right_list
                    if isinstance(p, str) and str(p).strip()
                }
            )
            continue
        if key in merged and isinstance(merged[key], dict) and isinstance(val, dict):
            merged[key] = merge_graph_metadata(merged[key], val)
        else:
            merged[key] = val
    return merged


def replace_list_reducer(
    left: List[Any] | None,
    right: List[Any] | None,
) -> List[Any]:
    """Replace list when the incoming update includes the key (even if empty)."""
    if right is not None:
        return list(right)
    return list(left or [])


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

    # Documentation pre-brief (optional, before semantic scan)
    docs_prebrief_summary: NotRequired[str]
    docs_prebrief_sources: NotRequired[List[str]]

    # Optional references for externalized payloads (e.g., Redis-backed cache blobs)
    cache_refs: NotRequired[Dict[str, str]]
    diff_manifest_ref: NotRequired[str]
    preflight_summary: NotRequired[PreflightSummary]
    preflight_errors: Annotated[List[PreflightParseIssue], operator.add]
    preflight_warnings: Annotated[List[str], operator.add]
    structural_graph_node_link: NotRequired[Dict[str, Any]]
    structural_topology: NotRequired[StructuralTopologySummary]
    structural_extraction_gaps: Annotated[List[StructuralExtractionGap], operator.add]

    # Phase 2 semantic enrichment (transient until snapshot_pin externalizes)
    community_summaries: Annotated[List[CommunitySemanticSummary], operator.add]
    unverified_call_targets: Annotated[List[UnverifiedCallTarget], operator.add]
    resolved_unverified_calls: Annotated[List[UnverifiedCallTarget], replace_list_reducer]
    knowledge_gaps: Annotated[List[KnowledgeGap], operator.add]
    global_summary: NotRequired[str]
    snapshot_root: NotRequired[str]
    snapshot_id: NotRequired[str]
    snapshot_source: NotRequired[Literal["explore", "loaded"]]
    semantic_community_work_item: NotRequired[Dict[str, Any]]
    semantic_community_work_queue: Annotated[List[Dict[str, Any]], replace_list_reducer]
    semantic_dispatch_cursor: NotRequired[int]
    repository_kb_summary_records: NotRequired[List[Dict[str, Any]]]

    # Mental model (externalized spec; ref only — never full BehavioralSpec on state for Send payloads)
    behavioral_spec_ref: NotRequired[str]
    exploration_ledger: Annotated[List[Dict[str, Any]], operator.add]

    # Mandate explorer ReAct loop (transient; bootstrap + targeted passes)
    mandate_explorer_mode: NotRequired[str]
    mandate_explorer_step_idx: NotRequired[int]
    mandate_explorer_finished: NotRequired[bool]
    mandate_explorer_retry_feedback: NotRequired[str]
    mandate_explorer_last_summary: NotRequired[str]

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
    critique_revision_digests: Annotated[Dict[str, CritiqueRevisionDigest], operator.or_]
    critique_revision_shard: NotRequired[Dict[str, Any]]

    # Optional runtime verification (parallel Send branches carry verifier_candidate)
    verifier_candidate: NotRequired[Dict[str, Any]]
    verifier_reports: Annotated[List[VerifierReport], operator.add]

    # Verifier loop state (transient)
    verifier_attempt_idx: NotRequired[int]
    verifier_retry_feedback: NotRequired[str]
    verifier_last_rationale: NotRequired[str]
    verifier_repo_root: NotRequired[str]
    verifier_scope: NotRequired[VerificationScope]
    verifier_verdict: NotRequired[VerifierVerdict]
    verifier_skipped_reason: NotRequired[str]
    verifier_current_test_code: NotRequired[str]
    verifier_focused_context_text: NotRequired[str]
    verifier_attempts: Annotated[List[VerifierAttemptRecord], operator.add]

    # Data for debugging and analysis
    current_task_id: NotRequired[str]
    metadata: Annotated[Dict[str, Any], merge_graph_metadata]
    llm_trace: Annotated[List[Dict[str, Any]], operator.add]
    token_usage: Annotated[int, operator.add]
    node_history: Annotated[List[str], operator.add]


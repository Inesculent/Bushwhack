from pydantic import BaseModel, Field, model_validator
from typing import Any, Dict, List, Literal, Optional, Self


TaskStatus = Literal["pending", "in_progress", "completed"]


class SearchResult(BaseModel):
    file_path: str = Field(description="Repository-relative file path using '/' separators.")
    line_number: int
    content: str
    context_lines: List[str]


class CodeEntity(BaseModel):
    name: str
    type: str
    signature: str
    body: str
    dependencies: List[str] = Field(default_factory=list)
    definition_line: Optional[int] = Field(
        default=None,
        description="1-based line number of the definition signature when known (AST).",
    )


SymbolDefinitionSource = Literal["jedi", "tree_sitter", "regex", "mcp"]


class SymbolDefinition(BaseModel):
    """Resolved definition site for a symbol name (repo-relative)."""

    file_path: str = Field(description="Repository-relative path using '/' separators.")
    line_start: int = Field(ge=1, description="1-based line of the definition.")
    entity_name: str
    entity_type: str = "unknown"
    signature: str = ""
    source: SymbolDefinitionSource = "tree_sitter"


class CodeSnippet(BaseModel):
    file_path: str = Field(description="Repository-relative file path using '/' separators.")
    content: str
    purpose: Literal["context", "usage_example", "dependency", "other"]
    relevance_score: float = Field(ge=0.0, le=1.0)

class RepositoryMap(BaseModel):

    # Basic repository information
    root_directory: str = Field(description="Absolute path to the repository root.")
    repository_id: Optional[str] = Field(default=None, description="Optional stable identifier for multi-repo experiments.")
    detected_languages: List[str]
    structure: Dict[str, List[str]] = Field(description="Mapping of directories to their contained files")
    entities: List[CodeEntity]
    snippets: List[CodeSnippet]


    # For exploration part of the process
    unresolved_imports: List[str] = Field(default_factory=list, description="List of imports that could not be resolved during analysis")
    knowledge_gaps: List[str] = Field(default_factory=list, description="Areas where the repository lacks documentation or clear structure, which may require special attention during review")
    insights: List[str] = Field(default_factory=list, description="High-level insights about the repository that may inform the review process, such as potential areas of concern or complexity")
    iteration: int = Field(default=0, description="Number of iterations taken to build the repository map, useful for tracking progress and convergence during analysis")
    is_sufficient: bool = Field(default=False, description="Flag indicating whether the repository map is considered sufficient for the review process, based on predefined criteria such as coverage of key files and entities")

    @model_validator(mode="after")
    def validate_exploration(self):
        if not self.entities and not self.snippets:
            raise ValueError("At least one of 'entities' or 'snippets' must be provided.")
        return self
    

    
class ReviewTask(BaseModel):

    # Basic task information
    id: str = Field(description="Unique identifier for the review task")
    title: str = Field(description="Short title summarizing the review task")
    description: str = Field(description="Detailed description of the review task")
    target_files: List[str] = Field(default_factory=list)
    
    # Defining recursive subtasks as necessary
    subtasks: List[Self] = Field(default_factory=list)
    
    # Static planning metadata for orchestration
    specialty: Literal["security", "performance", "logic", "style", "general"] = "general"
    depth: int = Field(default = 0, ge = 0, le = 5, description="Depth level for the review task") # Might be useful if we need to define a max depth
    assigned_model: Optional[str] = None


class BehavioralEvidenceRef(BaseModel):
    """Pointer to evidence used when forming the behavioral mandate (not a bug claim)."""

    kind: Literal["file", "symbol", "community", "doc", "diff", "other"] = "other"
    ref: str = Field(description="Human-readable reference, e.g. repo-relative path or node id.")
    note: str = Field(default="", description="Why this evidence matters.")


class BehavioralSpec(BaseModel):
    """
    Heuristic behavioral mandate for pull-request review.
    Downstream reviewers must treat this as directional context, not a checklist of expected defects.
    """

    intent_summary: str = Field(default="", description="Concise PR intent and scope.")
    behavioral_expectations: str = Field(
        default="",
        description="Approximate expected behavior and explicit non-goals.",
    )
    contract_boundaries: str = Field(
        default="",
        description="Type, interface, API, and data-contract signals from the repo.",
    )
    historical_precedents: str = Field(
        default="",
        description="Relevant precedent or conventions (bounded, evidence-linked).",
    )
    risk_hypotheses: str = Field(
        default="",
        description="Broad areas worth attention, explicitly marked as hypotheses.",
    )
    reviewer_guidance: str = Field(
        default="",
        description="Instructions to stay structural and unbiased; avoid anchoring on predicted bugs.",
    )
    evidence_refs: List[BehavioralEvidenceRef] = Field(default_factory=list)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    uncertainties: str = Field(default="", description="Known gaps in understanding.")


class ReviewFinding(BaseModel):

    # Basic finding information
    id: str = Field(description="Inherited from the review task ID for traceability")
    file_path: str = Field(description="Repository-relative file path using '/' separators.")
    line_start: int
    line_end: int

    # The content of the finding, and the context around it
    content: str = Field(description="The specific code snippet or issue identified")
    severity: Literal["low", "medium", "high"] = "medium"
    feedback_type: Literal["code_improvement", "defect_detection", "optimization", "other"] = "other"

    # The recommendation for fixing the issue, and any references to documentation or code examples
    recommendation: Optional[str] = None
    references: List[str] = Field(default_factory=list)


class ReviewerWorkerReport(BaseModel):
    task_id: str
    specialty: Literal["security", "performance", "logic", "style", "general"]
    explored_files: List[str] = Field(default_factory=list)
    context_summary: str = ""
    warnings: List[str] = Field(default_factory=list)


ReviewCategory = Literal["security", "logic", "performance", "general", "other"]
ReflectionVerdict = Literal[
    "accept",
    "reject",
    "needs_context",
    "needs_verification",
    "reclassify",
    "not_applicable",
]
ClaimType = Literal[
    "defect",
    "security_risk",
    "performance_regression",
    "missing_test",
    "positive_observation",
    "uncertain",
]


class CandidateFinding(BaseModel):
    """Draft finding before adversarial reflection and cleanup."""

    candidate_id: str = Field(description="Stable id for this candidate within the graph run.")
    patch_task_id: str = Field(description="Planner task id this candidate belongs to.")
    file_path: str = Field(description="Repository-relative file path using '/' separators.")
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)
    content: str = Field(description="Issue description with evidence pointers.")
    claim_type: ClaimType = Field(
        default="uncertain",
        description="Type of claim. Only actionable negative claims are eligible for promotion.",
    )
    failure_mode: str = Field(default="", description="What breaks, regresses, or can be exploited.")
    evidence_summary: str = Field(default="", description="Short note on what evidence supports this.")
    required_context: List[str] = Field(
        default_factory=list,
        description="External facts or code paths that must be checked before promotion.",
    )
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    suspected_category: ReviewCategory = "other"
    reflection_specialties: List[Literal["security", "performance", "logic", "general"]] = Field(
        default_factory=list,
        description=(
            "Reflector routing tags. The general critiquer normalizes this to exactly one specialty "
            "(hardcap: security > logic > performance > general). Legacy runs may still contain multiple entries."
        ),
    )
    feedback_type: Literal["code_improvement", "defect_detection", "optimization", "other"] = "other"
    severity: Literal["low", "medium", "high"] = "medium"
    recommendation: Optional[str] = Field(default=None, description="Concrete suggested fix or verification step.")

    @model_validator(mode="after")
    def validate_line_range(self) -> Self:
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class FocusedContextRequest(BaseModel):
    """Bounded request for additional evidence after reflection."""

    request_id: str
    candidate_id: str
    requested_by_specialty: Literal["security", "performance", "logic", "style", "general"]
    file_read_mode: Literal["slice", "full"] = Field(
        default="slice",
        description="slice: bounded excerpts; full: whole file up to review_full_file_max_chars per path.",
    )
    file_paths: List[str] = Field(default_factory=list, description="Max few paths to read slices from.")
    symbol_queries: List[str] = Field(default_factory=list, description="Symbols to resolve via search.")
    text_queries: List[str] = Field(default_factory=list, description="Ripgrep patterns or plain search strings.")
    reason: str = Field(default="", description="Why this context is needed.")


class FocusedContextResult(BaseModel):
    """Fulfilled snippets and search hits for one request."""

    request_id: str
    candidate_id: str
    file_snippets: Dict[str, str] = Field(default_factory=dict)
    file_contents_full: Dict[str, str] = Field(
        default_factory=dict,
        description="Full file bodies when file_read_mode was 'full' (separate from snippets).",
    )
    search_hits: Dict[str, List[SearchResult]] = Field(default_factory=dict)
    warnings: List[str] = Field(default_factory=list)


class ReflectionReport(BaseModel):
    """Specialist reflection on a single candidate."""

    candidate_id: str
    reflector_specialty: Literal["security", "performance", "logic", "style", "general"]
    verdict: ReflectionVerdict
    rationale: str = ""
    reclassified_category: Optional[ReviewCategory] = None
    focused_request: Optional[FocusedContextRequest] = None


class ReflectionBatchOutput(BaseModel):
    """Structured output for one specialty reflecting on all candidates."""

    reports: List[ReflectionReport] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class CritiquerOutput(BaseModel):
    """General critiquer structured output."""

    summary: str = Field(default="", description="Brief overview of the critique pass.")
    candidates: List[CandidateFinding] = Field(default_factory=list)
    initial_focus_requests: List[FocusedContextRequest] = Field(
        default_factory=list,
        description="Optional bounded follow-up context before reflection.",
    )
    warnings: List[str] = Field(default_factory=list)


class ReflectionOutput(BaseModel):
    """Single reflector response for one candidate."""

    verdict: ReflectionVerdict
    rationale: str = ""
    reclassified_category: Optional[ReviewCategory] = None
    focused_request: Optional[FocusedContextRequest] = None


class CritiqueRevisionItem(BaseModel):
    candidate_id: str
    verdict: ReflectionVerdict
    updated_evidence_summary: str = ""


class CritiqueRevisionOutput(BaseModel):
    """Post-focused-context revision for candidates that needed more evidence."""

    revisions: List[CritiqueRevisionItem] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


CritiqueRevisionImpact = Literal["supports", "weakens", "contradicts", "unclear"]


class CritiqueRevisionShardPayload(BaseModel):
    """One map-step unit: one candidate plus a bounded subset of focused context results."""

    shard_id: str
    candidate_id: str
    candidate: CandidateFinding
    focused_results: List[FocusedContextResult] = Field(default_factory=list)


class CritiqueRevisionDigestOutput(BaseModel):
    """Structured output for condensing one shard before the final revision merge."""

    candidate_id: str
    request_ids: List[str] = Field(default_factory=list)
    evidence_bullets: List[str] = Field(
        default_factory=list,
        description="Very short bullets summarizing what this shard adds; do not paste large code.",
    )
    impact: CritiqueRevisionImpact = "unclear"
    warnings: List[str] = Field(default_factory=list)


class CritiqueRevisionDigest(BaseModel):
    """Stored digest from one shard, keyed by shard_id in graph state."""

    shard_id: str
    candidate_id: str
    request_ids: List[str] = Field(default_factory=list)
    evidence_bullets: List[str] = Field(default_factory=list)
    impact: CritiqueRevisionImpact = "unclear"
    warnings: List[str] = Field(default_factory=list)


class Insight(BaseModel):
    source_node: str
    content: str
    affected_files: List[str] = Field(default_factory=list)


class ExplorationRequest(BaseModel):
    content: str
    target_symbol: str
    context_hint: str
    priority: Literal["low", "medium", "high"] = "medium"


class RunMetadata(BaseModel):
    repo: str
    base_sha: str
    head_sha: str
    run_id: Optional[str] = None
    timestamp: Optional[str] = None


class RepoDocument(BaseModel):
    path: str = Field(description="Repository-relative doc path using '/' separators.")
    ref: Optional[str] = Field(default=None, description="Git ref used to fetch this document.")
    content: str = Field(description="Raw document content (bounded by caller).")
    truncated: bool = Field(default=False, description="True if content was truncated to fit limits.")


class RepoDocsBundle(BaseModel):
    repo: str = Field(description="Repository slug 'owner/repo'.")
    ref: Optional[str] = Field(default=None, description="Git ref used for the bundle.")
    documents: List[RepoDocument] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)


class RepoStructureEntry(BaseModel):
    """One entry from a repository structure listing."""

    type: str = Field(description="Entry type: file or dir.")
    path: str = Field(description="Repository-relative path using '/' separators.")
    name: str = ""
    sha: Optional[str] = None


class RepoStructure(BaseModel):
    """Directory listing response for repo structure discovery."""

    owner: str
    repo: str
    path: str = ""
    ref: Optional[str] = None
    entries: List[RepoStructureEntry] = Field(default_factory=list)
    error: Optional[str] = None


class RepoMetadata(BaseModel):
    """Lightweight repository metadata from GitHub."""

    owner: str
    repo: str
    default_branch: Optional[str] = None


class GitHubPullRequestContext(BaseModel):
    number: int
    title: str = ""
    body: str = ""
    html_url: Optional[str] = None
    base_ref: Optional[str] = None
    head_ref: Optional[str] = None
    author: Optional[str] = None
    state: Optional[str] = None


class GitHubIssueContext(BaseModel):
    number: int
    title: str = ""
    body: str = ""
    html_url: Optional[str] = None
    state: Optional[str] = None


class GitHubIssueComment(BaseModel):
    author: Optional[str] = None
    body: str = ""
    html_url: Optional[str] = None
    created_at: Optional[str] = None


DiffChangeType = Literal["A", "M", "D", "R"]
ParseIssueSeverity = Literal["warning", "error"]
StaticSignalSource = Literal[
    "regex_heuristic",
    "ast_heuristic",
    "path_heuristic",
    "diff_heuristic",
    "other",
]
RiskSignalCategory = Literal[
    "auth",
    "permissions",
    "secrets",
    "sql",
    "network",
    "serialization",
    "concurrency",
    "other",
]
AmbiguityCategory = Literal[
    "dynamic_import",
    "reflection",
    "dependency_injection",
    "monkey_patching",
    "runtime_dispatch",
    "other",
]


class PreflightParseIssue(BaseModel):
    code: str = Field(description="Stable error or warning code for this issue.")
    message: str = Field(description="Human-readable issue description.")
    severity: ParseIssueSeverity = "error"
    filepath: Optional[str] = Field(default=None, description="Repository-relative file path when applicable.")
    line_number: Optional[int] = Field(default=None, ge=1)


class PreflightDiffFileInput(BaseModel):
    filepath: str = Field(description="Repository-relative file path using '/' separators.")
    change_type: Optional[DiffChangeType] = None
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    raw_diff: Optional[str] = None


class PreflightRequest(BaseModel):
    run_metadata: RunMetadata
    raw_diff: Optional[str] = None
    files: List[PreflightDiffFileInput] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_diff_source(self) -> Self:
        if not self.raw_diff and not self.files:
            raise ValueError("At least one of 'raw_diff' or 'files' must be provided.")
        return self


class DiffFileManifestEntry(BaseModel):
    filepath: str = Field(description="Repository-relative file path using '/' separators.")
    old_filepath: Optional[str] = Field(default=None, description="Original path for rename operations.")
    change_type: DiffChangeType
    additions: int = Field(default=0, ge=0)
    deletions: int = Field(default=0, ge=0)
    hunk_count: int = Field(default=0, ge=0)
    language: Optional[str] = None
    is_generated: bool = False
    is_binary: bool = False
    is_vendor: bool = False
    raw_diff: Optional[str] = None
    parse_errors: List[PreflightParseIssue] = Field(default_factory=list)


class DiffManifestAggregateMetrics(BaseModel):
    total_files_changed: int = Field(default=0, ge=0)
    total_additions: int = Field(default=0, ge=0)
    total_deletions: int = Field(default=0, ge=0)
    total_hunks: int = Field(default=0, ge=0)
    language_breakdown: Dict[str, int] = Field(default_factory=dict)


class PreflightEvidenceRef(BaseModel):
    line_start: Optional[int] = Field(default=None, ge=1)
    line_end: Optional[int] = Field(default=None, ge=1)
    hunk_index: Optional[int] = Field(default=None, ge=0)
    symbol_name: Optional[str] = None


class StaticRiskSignal(BaseModel):
    category: RiskSignalCategory
    signal_source: StaticSignalSource = "other"
    filepath: str = Field(description="Repository-relative file path using '/' separators.")
    rule_id: str = Field(description="Deterministic rule identifier that raised this hint.")
    confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    detail: Optional[str] = None
    evidence_ref: Optional[PreflightEvidenceRef] = None


class StructuralAmbiguityFlag(BaseModel):
    category: AmbiguityCategory
    signal_source: StaticSignalSource = "other"
    filepath: str = Field(description="Repository-relative file path using '/' separators.")
    rule_id: str = Field(description="Deterministic rule identifier that raised this flag.")
    confidence: float = Field(default=0.4, ge=0.0, le=1.0)
    detail: Optional[str] = None
    evidence_ref: Optional[PreflightEvidenceRef] = None


class DiffManifest(BaseModel):
    manifest_version: str = "1.0"
    manifest_id: str
    run_metadata: RunMetadata
    files: List[DiffFileManifestEntry] = Field(default_factory=list)
    aggregate_metrics: DiffManifestAggregateMetrics
    risk_hints: List[StaticRiskSignal] = Field(default_factory=list)
    ambiguity_flags: List[StructuralAmbiguityFlag] = Field(default_factory=list)
    errors: List[PreflightParseIssue] = Field(default_factory=list)
    warnings: List[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize_ordering(self) -> Self:
        # Keep manifests deterministic for cache keys and replay behavior.
        self.files = sorted(self.files, key=lambda item: item.filepath)
        return self


class PreflightSummary(BaseModel):
    manifest_id: str
    total_files_changed: int = Field(default=0, ge=0)
    total_hunks: int = Field(default=0, ge=0)
    total_additions: int = Field(default=0, ge=0)
    total_deletions: int = Field(default=0, ge=0)
    has_errors: bool = False
    has_ambiguity: bool = False


class StructuralExtractionGap(BaseModel):
    filepath: str = Field(description="Repository-relative file path using '/' separators.")
    reason: str = Field(description="Stable reason for degraded structural extraction.")
    detail: Optional[str] = None


class StructuralTopologyCommunity(BaseModel):
    community_id: int
    node_ids: List[str] = Field(default_factory=list)
    cohesion: float = Field(default=0.0, ge=0.0, le=1.0)
    file_count: int = Field(default=0, ge=0)
    symbol_count: int = Field(default=0, ge=0)


class StructuralTopologySummary(BaseModel):
    algorithm: str
    community_count: int = Field(default=0, ge=0)
    communities: List[StructuralTopologyCommunity] = Field(default_factory=list)
    node_to_community: Dict[str, int] = Field(default_factory=dict)
    splits_applied: int = Field(default=0, ge=0)
    config: Dict[str, Any] = Field(default_factory=dict)


# --- Phase 2: semantic bubble-up ---


class SymbolSemanticSummary(BaseModel):
    symbol_node_id: str
    purpose: str
    rationale: Optional[str] = None
    confidence: float = Field(ge=0.0, le=1.0)


class FileSemanticSummary(BaseModel):
    file_node_id: str
    purpose: str
    key_symbols: List[str] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


class UnverifiedCallTarget(BaseModel):
    source_symbol_id: str
    target_name: str
    source_community_id: int
    context_hint: str = ""
    resolved: bool = False
    resolved_target_id: Optional[str] = None
    resolution_summary: Optional[str] = None


class CommunitySemanticSummary(BaseModel):
    community_id: int
    label: str
    purpose: str
    file_summaries: List[FileSemanticSummary] = Field(default_factory=list)
    symbol_summaries: List[SymbolSemanticSummary] = Field(default_factory=list)
    unverified_calls: List[UnverifiedCallTarget] = Field(default_factory=list)
    cross_community_dependencies: List[int] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)


KnowledgeGapType = Literal["isolated_symbol", "low_cohesion", "ambiguous_heavy", "extraction_gap", "unverified_call"]


class KnowledgeGap(BaseModel):
    gap_type: KnowledgeGapType
    description: str
    affected_node_ids: List[str] = Field(default_factory=list)
    community_id: Optional[int] = None
    severity: Literal["low", "medium", "high"] = "medium"


class SnapshotDiagnostics(BaseModel):
    god_nodes: List[Dict[str, Any]] = Field(default_factory=list)
    bridge_nodes: List[Dict[str, Any]] = Field(default_factory=list)
    cross_community_edges: List[Dict[str, Any]] = Field(default_factory=list)
    knowledge_gaps: List[KnowledgeGap] = Field(default_factory=list)


ExplorationSnapshotStatus = Literal["exploration_complete", "partial", "failed"]


class ExplorationSnapshot(BaseModel):
    snapshot_id: str
    run_id: str
    snapshot_root: str
    status: ExplorationSnapshotStatus
    community_count: int = Field(ge=0)
    total_nodes: int = Field(ge=0)
    total_edges: int = Field(ge=0)
    unresolved_call_count: int = Field(ge=0)
    extraction_gap_count: int = Field(ge=0)
    metadata: Dict[str, Any] = Field(default_factory=dict)


class CommunityWorkItem(BaseModel):
    """Payload for ``Send()`` to ``community_semantic_agent``."""

    community_id: int
    file_paths: List[str] = Field(default_factory=list)
    symbol_context_lines: List[str] = Field(
        default_factory=list,
        description="Pre-rendered lines: node id, name, signature, truncated body.",
    )
    outbound_cross_community_targets: List[str] = Field(
        default_factory=list,
        description="Callee symbol names referenced across community boundaries (no summaries).",
    )
    target_communities_hint: List[int] = Field(
        default_factory=list,
        description="Community ids of outbound cross-boundary targets (same order as targets when possible).",
    )


class CommunityAgentOutput(BaseModel):
    """Structured LLM output for one community."""

    summary: CommunitySemanticSummary
    warnings: List[str] = Field(default_factory=list)


class GlobalSemanticSynthesisOutput(BaseModel):
    global_summary: str = Field(default="", description="Repository-level synthesis from community summaries.")


class ResolverSymbolSummaryOutput(BaseModel):
    """One-shot summary when resolving a symbol via AST in the resolver tier."""

    symbol_node_id: str
    one_line_summary: str = Field(default="", description="Single-sentence purpose summary.")


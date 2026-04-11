from pydantic import BaseModel, Field, model_validator
from typing import Dict, List, Literal, Optional, Self


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





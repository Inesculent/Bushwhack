from pydantic import BaseModel, Field, model_validator
from typing import List, Optional, Literal, Dict, Self


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



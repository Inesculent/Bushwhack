from functools import lru_cache
from pathlib import Path
from typing import List, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
	"""Application settings loaded from environment variables."""

	model_config = SettingsConfigDict(
		env_file=".env",
		env_prefix="REVIEW_",
		extra="ignore",
	)

	ast_enabled: bool = Field(
		default=True,
		description="Enable AST parsing for repository understanding.",
	)

	ast_mcp_enabled: bool = Field(
		default=False,
		description="Use MCP transport for AST parsing when enabled; otherwise use native in-process parsing.",
	)
	ast_mcp_command: str = Field(
		default="python",
		description="Command used to start the AST MCP server process.",
	)
	ast_mcp_args: List[str] = Field(
		default_factory=lambda: ["mcp/fs-mcp/server.py"],
		description="Arguments for the AST MCP server command.",
	)
	ast_mcp_cwd: Optional[str] = Field(
		default=None,
		description="Optional working directory used when launching AST MCP server.",
	)
	ast_mcp_timeout_seconds: int = Field(
		default=30,
		ge=1,
		le=300,
		description="Timeout for each MCP request in seconds.",
	)
	ast_mcp_parse_tool: str = Field(
		default="parse_file",
		description="Tool name used to parse a file AST.",
	)
	ast_mcp_entity_tool: str = Field(
		default="get_entity_details",
		description="Tool name used to fetch a single entity from a file.",
	)
	ast_cache_ttl_seconds: int = Field(
		default=3600,
		ge=1,
		description="TTL for AST cache entries.",
	)
	ast_parser_version: str = Field(
		default="v1",
		description="Version tag included in AST cache keys for invalidation.",
	)
	ast_fallback_to_search: bool = Field(
		default=True,
		description="Keep non-AST fallback paths available when MCP is unavailable.",
	)
	redis_enabled: bool = Field(
		default=True,
		description="Enable Redis-backed LangGraph checkpointing.",
	)
	redis_url: str = Field(
		default="redis://localhost:6379/0",
		description="Redis connection URL used for LangGraph checkpointing.",
	)
	redis_namespace: str = Field(
		default="langgraph",
		description="Namespace prefix for Redis checkpoint keys.",
	)
	redis_ttl_seconds: int = Field(
		default=3600,
		ge=1,
		description="TTL for Redis checkpoint entries.",
	)
	github_personal_access_token: Optional[str] = Field(
		default=None,
		validation_alias=AliasChoices(
			"REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN",
			"GITHUB_PERSONAL_ACCESS_TOKEN",
		),
		description="GitHub personal access token for PR API enrichment.",
	)
	google_api_key: Optional[str] = Field(
		default=None,
		validation_alias=AliasChoices("REVIEW_GOOGLE_API_KEY", "GOOGLE_API_KEY"),
		description="Google API key for Gemini model access.",
	)
	openai_api_key: Optional[str] = Field(
		default=None,
		validation_alias=AliasChoices("REVIEW_OPENAI_API_KEY", "OPENAI_API_KEY"),
		description="OpenAI API key for hosted OpenAI model access.",
	)
	anthropic_api_key: Optional[str] = Field(
		default=None,
		validation_alias=AliasChoices("REVIEW_ANTHROPIC_API_KEY", "ANTHROPIC_API_KEY"),
		description="Anthropic API key for Claude model access.",
	)
	local_llm_base_url: str = Field(
		default="http://localhost:8000/v1",
		description="OpenAI-compatible base URL for local models such as Qwen through Ollama, LM Studio, or vLLM.",
	)
	local_llm_api_key: str = Field(
		default="local",
		description="API key placeholder for OpenAI-compatible local model servers.",
	)
	local_llm_timeout_seconds: int = Field(
		default=180,
		ge=1,
		le=600,
		description="Request timeout for OpenAI-compatible local model servers.",
	)
	local_llm_max_retries: int = Field(
		default=0,
		ge=0,
		le=10,
		description="Retry count for OpenAI-compatible local model requests.",
	)

	structural_topology_enabled: bool = Field(
		default=True,
		description="Run community detection and cohesion scoring after structural graph build.",
	)
	community_max_fraction: float = Field(
		default=0.25,
		ge=0.01,
		le=1.0,
		description="Communities larger than this fraction of clustering-graph nodes may be split.",
	)
	community_min_split_size: int = Field(
		default=10,
		ge=1,
		description="Minimum node count before fractional split threshold applies.",
	)
	community_max_files: int = Field(
		default=0,
		ge=0,
		description="If >0, split communities with more file nodes than this cap.",
	)
	community_max_symbols: int = Field(
		default=0,
		ge=0,
		description="If >0, split communities with more symbol nodes than this cap.",
	)
	louvain_seed: int = Field(
		default=42,
		description="Random seed for NetworkX Louvain fallback (deterministic partitions).",
	)

	solo_agent_output_dir: Path = Field(
		default=Path("logs/solo_agent"),
		description="Root directory for solo-agent experiment artifacts (raw transcripts, parsed findings, manifests).",
	)
	solo_agent_max_diff_chars: int = Field(
		default=60_000,
		ge=1_000,
		description="Maximum characters of the unified diff inlined into the solo-agent prompt.",
	)
	solo_agent_model_key: str = Field(
		default="gpt-5.4",
		description="Model key (from Models factory) used by the solo-agent worker for free-form tagged output.",
	)
	solo_agent_prompt_version: str = Field(
		default="v1",
		description="Prompt template version stamped on solo-agent run metadata for experiment tracking.",
	)

	reviewer_agent_output_dir: Path = Field(
		default=Path("logs/reviewer_agent"),
		description="Root directory for reviewer-graph experiment artifacts.",
	)
	reviewer_planner_model_key: str = Field(
		default="qwen2.5-coder-32b",
		description=(
			"Model key (from Models factory) used by the reviewer planner. "
			"Must match a key in infrastructure.llm.factory.MODELS; for Ollama use e.g. qwen2.5-coder-32b-ollama."
		),
	)
	reviewer_worker_model_key: str = Field(
		default="qwen2.5-coder-32b",
		description=(
			"Model key (from Models factory) used by reviewer workers, critiquer, reflection, and revision nodes. "
			"Aligns with Models.DEFAULT_ROLE_MODELS['worker']. For Ollama set to qwen2.5-coder-32b-ollama and "
			"REVIEW_LOCAL_LLM_BASE_URL to your OpenAI-compatible endpoint."
		),
	)
	reviewer_use_legacy_specialist_workers: bool = Field(
		default=False,
		description="When true, route review_planner tasks to legacy specialist workers instead of the adversarial critiquer loop.",
	)

	def get_ast_mcp_cwd(self) -> str:
		"""Return an absolute working directory for MCP server startup."""
		if self.ast_mcp_cwd:
			return str(Path(self.ast_mcp_cwd).resolve())
		return str(Path(__file__).resolve().parents[1])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
	return Settings()

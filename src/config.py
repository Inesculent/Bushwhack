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
	reviewer_cleanup_redis_checkpoints: bool = Field(
		default=True,
		description="Delete per-PR reviewer graph Redis checkpoints after artifacts are written.",
	)
	github_personal_access_token: Optional[str] = Field(
		default=None,
		validation_alias=AliasChoices(
			"REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN",
			"GITHUB_PERSONAL_ACCESS_TOKEN",
		),
		description="GitHub personal access token for PR API enrichment.",
	)
	github_mcp_enabled: bool = Field(
		default=True,
		description="Enable GitHub MCP for documentation and PR context enrichment.",
	)
	github_mcp_command: str = Field(
		default="python",
		description="Command used to start the GitHub MCP server process.",
	)
	github_mcp_args: List[str] = Field(
		default_factory=lambda: ["mcp/github-mcp/server.py"],
		description="Arguments for the GitHub MCP server command.",
	)
	github_mcp_cwd: Optional[str] = Field(
		default=None,
		description="Optional working directory used when launching the GitHub MCP server.",
	)
	github_mcp_timeout_seconds: int = Field(
		default=30,
		ge=1,
		le=300,
		description="Timeout for GitHub MCP tool calls in seconds.",
	)
	github_mcp_cache_ttl_seconds: int = Field(
		default=3600,
		ge=60,
		description="TTL for GitHub MCP cache entries in seconds.",
	)
	github_mcp_doc_max_chars: int = Field(
		default=12000,
		ge=1000,
		description="Max characters per documentation file fetched via GitHub MCP.",
	)
	github_mcp_doc_max_total_chars: int = Field(
		default=40000,
		ge=2000,
		description="Max total characters across documentation files fetched via GitHub MCP.",
	)
	github_mcp_pr_max_comments: int = Field(
		default=20,
		ge=0,
		description="Max PR/issue comments to fetch via GitHub MCP.",
	)
	github_mcp_pr_comment_max_chars: int = Field(
		default=2000,
		ge=200,
		description="Max characters per PR/issue comment fetched via GitHub MCP.",
	)
	github_mcp_doc_paths: List[str] = Field(
		default_factory=lambda: [
			"README.md",
			"README.rst",
			"README.txt",
			"CONTRIBUTING.md",
			"SECURITY.md",
			"CHANGELOG.md",
			"docs/README.md",
			"docs/index.md",
			".github/CONTRIBUTING.md",
			".github/SECURITY.md",
		],
		description="Ordered doc paths to attempt for the GitHub MCP pre-brief.",
	)
	docs_prebrief_enabled: bool = Field(
		default=True,
		description="Generate a documentation-based pre-brief before semantic scanning.",
	)
	docs_prebrief_model_key: str = Field(
		default="qwen3.5-35b-a3b",
		description="Model key used for the documentation pre-brief summary.",
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
		default=600,
		ge=1,
		le=3600,
		description="Request timeout for OpenAI-compatible local model servers.",
	)
	local_llm_status_timeout_seconds: float = Field(
		default=5.0,
		ge=0.5,
		le=60.0,
		description="Short timeout for local model server health/status probes.",
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
		default="qwen3.5-35b-a3b",
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
		default="qwen3.5-35b-a3b",
		description=(
			"Model key (from Models factory) used by the reviewer planner. "
			"Must match a key in infrastructure.llm.factory.MODELS; for Ollama use the corresponding local model key."
		),
	)
	reviewer_worker_model_key: str = Field(
		default="qwen3.5-35b-a3b",
		description=(
			"Model key (from Models factory) used by reviewer workers, critiquer, reflection, and revision nodes. "
			"Aligns with Models.DEFAULT_ROLE_MODELS['worker']. For Ollama set to the corresponding local model key and "
			"REVIEW_LOCAL_LLM_BASE_URL to your OpenAI-compatible endpoint."
		),
	)
	reviewer_use_legacy_specialist_workers: bool = Field(
		default=False,
		description="When true, route review_planner tasks to legacy specialist workers instead of the adversarial critiquer loop.",
	)
	reviewer_cleanup_redis_checkpoints: bool = Field(
		default=True,
		description="Delete per-PR Redis checkpoints after reviewer-agent experiments finish each graph run.",
	)
	reviewer_critique_revision_max_shard_chars: int = Field(
		default=12_000,
		ge=2_000,
		description="Approximate max characters of focused context JSON per critique-revision digest shard.",
	)
	reviewer_critique_revision_max_candidate_chars: int = Field(
		default=8_000,
		ge=500,
		description="Truncate inlined CandidateFinding JSON per digest shard prompt.",
	)
	reviewer_reflection_retry_backoff_seconds: float = Field(
		default=5.0,
		ge=0.0,
		le=120.0,
		description="Base backoff between adversarial reflection retries when the local LLM server is still active.",
	)
	reviewer_reflection_timeout_patience_seconds: int = Field(
		default=1800,
		ge=0,
		le=7200,
		description=(
			"Extra wall-clock budget for adversarial reflectors to keep waiting/retrying local LLM timeouts "
			"while the model server still answers status probes."
		),
	)

	# Phase 2 semantic enrichment + snapshot layout
	snapshot_base_path: Path = Field(
		default=Path("./bushwhack_runs"),
		description="Root directory for exploration snapshot disk trees.",
	)
	snapshot_keep_full_graph: bool = Field(
		default=True,
		description="Keep graph/full_graph.json after snapshot_pin (False deletes after pin).",
	)
	semantic_enrichment_enabled: bool = Field(
		default=True,
		description="Run Phase 2 semantic bubble-up after structural extraction when topology exists.",
	)
	semantic_max_tokens_per_community: int = Field(
		default=8000,
		ge=500,
		description="Approximate max prompt characters budget per community agent (rough token proxy).",
	)
	semantic_max_files_per_agent: int = Field(
		default=20,
		ge=1,
		description="Max file nodes to include per community work item.",
	)
	semantic_max_symbols_per_agent: int = Field(
		default=50,
		ge=1,
		description="Max symbol nodes to include per community work item.",
	)
	semantic_max_parallel_agents: int = Field(
		default=4,
		ge=1,
		le=64,
		description="Max Phase 2 community semantic agents allowed to call the LLM concurrently.",
	)
	semantic_agent_max_retries: int = Field(
		default=2,
		ge=0,
		le=10,
		description="Retries per community semantic LLM call before emitting a degraded summary.",
	)
	semantic_agent_retry_backoff_seconds: float = Field(
		default=5.0,
		ge=0.0,
		le=120.0,
		description="Base backoff between community semantic LLM retries.",
	)
	semantic_agent_timeout_patience_seconds: int = Field(
		default=1800,
		ge=0,
		le=7200,
		description=(
			"Extra wall-clock budget for semantic agents to keep waiting/retrying local LLM timeouts "
			"while the model server still answers status probes."
		),
	)
	unverified_call_max_resolution_rounds: int = Field(
		default=3,
		ge=1,
		le=10,
		description="Max resolver self-loop rounds for newly surfaced unverified targets.",
	)
	semantic_model_key: str = Field(
		default="qwen3.5-35b-a3b",
		description=(
			"Models factory key for community semantic agents (same registry as reviewer_worker_model_key). "
			"Defaults to the local Qwen stack; set e.g. gemini-pro only if langchain-google-genai is installed."
		),
	)
	semantic_merge_model_key: str = Field(
		default="qwen3.5-35b-a3b",
		description=(
			"Models factory key for global semantic synthesis at merge (same as Models.DEFAULT_ROLE_MODELS['synthesizer']). "
			"Defaults to local Qwen; override with REVIEW_SEMANTIC_MERGE_MODEL_KEY."
		),
	)
	skip_trivial_communities: bool = Field(
		default=True,
		description="Synthesize trivial __init__-only communities without LLM.",
	)
	diagnostics_god_nodes_top_n: int = Field(default=10, ge=1, le=100)
	diagnostics_bridge_nodes_top_n: int = Field(default=5, ge=1, le=50)
	diagnostics_cross_community_edges_top_n: int = Field(default=10, ge=1, le=200)
	diagnostics_low_cohesion_threshold: float = Field(
		default=0.15,
		ge=0.0,
		le=1.0,
		description="Communities below this cohesion with enough nodes are flagged as knowledge gaps.",
	)
	semantic_snapshot_pointer_ttl_seconds: int = Field(
		default=86400,
		ge=60,
		description="TTL for Redis snapshot pointer keys (separate from checkpoint TTL).",
	)

	def get_ast_mcp_cwd(self) -> str:
		"""Return an absolute working directory for MCP server startup."""
		if self.ast_mcp_cwd:
			return str(Path(self.ast_mcp_cwd).resolve())
		return str(Path(__file__).resolve().parents[1])


@lru_cache(maxsize=1)
def get_settings() -> Settings:
	return Settings()

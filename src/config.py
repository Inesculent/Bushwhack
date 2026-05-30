import sys
from functools import lru_cache
from pathlib import Path
from typing import List, Literal, Optional

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from src.infrastructure.llm.defaults import DEFAULT_LOCAL_MODEL_KEY


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
		default_factory=lambda: sys.executable,
		description="Command used to start the AST MCP server process.",
	)
	ast_mcp_args: List[str] = Field(
		default_factory=lambda: ["docker_mcp/fs-mcp/server.py"],
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
	ast_mcp_definitions_tool: str = Field(
		default="find_symbol_definitions",
		description="MCP tool name for repo-wide symbol definition search.",
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
	review_full_file_max_chars: int = Field(
		default=500_000,
		ge=5_000,
		le=2_000_000,
		description="Max characters returned per file when focused context requests full-file reads.",
	)
	review_full_file_max_total_chars: int = Field(
		default=600_000,
		ge=10_000,
		le=3_000_000,
		description="Max total characters across full-file payloads in one FocusedContextResult.",
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
	sandbox_backend: Literal["docker", "apptainer"] = Field(
		default="docker",
		description="Sandbox runtime: docker (--local) or apptainer (--remote).",
	)
	run_profile: Optional[Literal["local", "remote"]] = Field(
		default=None,
		description="Last applied execution profile (local or remote), if set via CLI or env.",
	)
	apptainer_binary: str = Field(
		default="apptainer",
		description="Apptainer/Singularity executable name on PATH.",
	)
	apptainer_image: str = Field(
		default="",
		description="Path to review sandbox .sif (git, ripgrep, python). Used when sandbox_backend=apptainer.",
	)
	apptainer_verifier_image: str = Field(
		default="",
		description="Path to verifier .sif; falls back to apptainer_image when empty.",
	)
	apptainer_instance_dir: Optional[str] = Field(
		default=None,
		description="Optional Apptainer instance state directory on the compute node.",
	)
	apptainer_bind_tmpfs: bool = Field(
		default=True,
		description="Use --writable-tmpfs for Apptainer instances (in-container RW for clones).",
	)
	apptainer_extra_bind: List[str] = Field(
		default_factory=list,
		description="Extra Apptainer bind mounts (host:container[:opts]).",
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
		default_factory=lambda: sys.executable,
		description="Command used to start the GitHub MCP server process.",
	)
	github_mcp_args: List[str] = Field(
		default_factory=lambda: ["docker_mcp/github-mcp/server.py"],
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
	github_mcp_focused_context_doc_fallback: bool = Field(
		default=False,
		description=(
			"When true, fetch github_mcp_doc_paths via GitHub MCP if sandbox ripgrep finds no hits "
			"for symbol/text queries. Default false: code search stays in the cloned sandbox; "
			"avoids repeated README/CONTRIBUTING fetches per focused-context request."
		),
	)
	github_mcp_doc_discovery_enabled: bool = Field(
		default=True,
		description="Discover markdown docs via GitHub API to reduce 404s and stale paths.",
	)
	github_mcp_doc_discovery_max_paths: int = Field(
		default=30,
		ge=0,
		description="Max discovered doc paths to attempt before falling back to static list.",
	)
	docs_prebrief_enabled: bool = Field(
		default=True,
		description="Generate a documentation-based pre-brief before semantic scanning.",
	)
	docs_prebrief_model_key: str = Field(
		default=DEFAULT_LOCAL_MODEL_KEY,
		description=(
			"Model key used for the documentation pre-brief summary. "
			"Default from infrastructure.llm.defaults.DEFAULT_LOCAL_MODEL_KEY."
		),
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
	llm_temperature: Optional[float] = Field(
		default=None,
		ge=0.0,
		le=2.0,
		description="Optional temperature override for LLM calls (planner/worker/synthesizer).",
	)
	llm_presence_penalty: Optional[float] = Field(
		default=None,
		ge=-2.0,
		le=2.0,
		description="Optional presence_penalty override for OpenAI-compatible providers.",
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
		default=DEFAULT_LOCAL_MODEL_KEY,
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
		default=DEFAULT_LOCAL_MODEL_KEY,
		description=(
			"Model key (from Models factory) used by the reviewer planner. "
			"Must match a key in infrastructure.llm.factory.MODELS; for Ollama use the corresponding local model key."
		),
	)
	reviewer_planner_max_completion_tokens: int = Field(
		default=12288,
		ge=256,
		le=32768,
		description=(
			"Per-invocation cap on completion tokens for planner-role LLM calls (draft/revise plan, monolithic planner). "
			"Run-level totals in run_meta.json (total_llm_tokens) sum many calls and are unrelated to this cap."
		),
	)
	reviewer_worker_model_key: str = Field(
		default=DEFAULT_LOCAL_MODEL_KEY,
		description=(
			"Model key (from Models factory) used by reviewer workers, critiquer, reflection, and revision nodes. "
			"Aligns with Models.DEFAULT_ROLE_MODELS['worker']. For Ollama set to the corresponding local model key and "
			"REVIEW_LOCAL_LLM_BASE_URL to your OpenAI-compatible endpoint."
		),
	)
	reviewer_worker_max_completion_tokens: int = Field(
		default=12288,
		ge=512,
		le=65536,
		description=(
			"Per-invocation cap on completion tokens for worker-role calls (critiquer, reflection, Phase 0 mental-model "
			"nodes, specialist workers, verifier test gen). Structured outputs for wide PRs can need more than a few "
			"thousand tokens per call; run_meta total_llm_tokens is the sum across all calls, not this setting."
		),
	)
	reviewer_critiquer_max_completion_tokens: int = Field(
		default=20480,
		ge=512,
		le=65536,
		description=(
			"Per-invocation completion token cap for the general critiquer structured-output call. "
			"Falls back to a compact retry when the model hits the length limit."
		),
	)
	reviewer_critiquer_single_file_max_chars: int = Field(
		default=80_000,
		ge=5_000,
		le=500_000,
		description=(
			"Max characters per target file excerpt when a review task has exactly one target file "
			"(avoids truncating mid-class logic for COMBO/execute review tasks)."
		),
	)
	reviewer_allow_host_pr_worktree: bool = Field(
		default=True,
		description=(
			"When true, snapshot AACR resume clones the PR head into a host worktree under the snapshot root "
			"(enables AST and local ripgrep). Set REVIEW_REVIEWER_ALLOW_HOST_PR_WORKTREE=false to use Docker sandbox only."
		),
	)
	reviewer_use_legacy_specialist_workers: bool = Field(
		default=False,
		description="When true, route review_planner tasks to legacy specialist workers instead of the adversarial critiquer loop.",
	)
	reviewer_legacy_planner_mode: bool = Field(
		default=False,
		description=(
			"When true, skip Phase 0 mental-model formulation and actor-critic planning; use the monolithic review_planner. "
			"Orthogonal to reviewer_use_legacy_specialist_workers."
		),
	)
	reviewer_actor_critic_max_plan_revisions: int = Field(
		default=4,
		ge=0,
		le=5,
		description="Max plan_revision cycles after plan_critic before emitting tasks anyway.",
	)
	reviewer_mental_model_max_queries_per_run: int = Field(
		default=40,
		ge=0,
		le=500,
		description="Cap query_mental_model tool invocations per graph run (across parallel tasks).",
	)
	reviewer_mental_model_max_answer_chars: int = Field(
		default=2500,
		ge=200,
		le=16000,
		description="Max characters returned from query_mental_model (excerpt of BehavioralSpec).",
	)
	reviewer_mandate_explorer_enabled: bool = Field(
		default=True,
		description="When true, run mandate_explorer (bootstrap + critic-targeted) before/during coupled planning.",
	)
	reviewer_mandate_bootstrap_max_steps: int = Field(
		default=8,
		ge=1,
		le=24,
		description="ReAct steps for mandatory bootstrap mandate_explorer pass.",
	)
	reviewer_mandate_targeted_max_steps: int = Field(
		default=4,
		ge=1,
		le=16,
		description="ReAct steps per critic-triggered targeted explorer pass.",
	)
	reviewer_mandate_explorer_max_observation_chars: int = Field(
		default=4000,
		ge=500,
		le=32000,
		description="Max characters per mandate explorer tool result.",
	)
	reviewer_mandate_ledger_max_total_chars: int = Field(
		default=48000,
		ge=4000,
		le=200000,
		description="Soft cap on total mandate_tool_observation preview chars in exploration_ledger.",
	)
	reviewer_mandate_bootstrap_digest_max_chars: int = Field(
		default=1200,
		ge=200,
		le=8000,
		description="Planner/critic bootstrap digest size stored in metadata.mental_model.",
	)
	reviewer_mandate_plan_max_cycles: int = Field(
		default=3,
		ge=1,
		le=8,
		description="Max joint critic cycles (explorer/patch/revision) before plan_emit.",
	)
	reviewer_mandate_spec_excerpt_max_chars: int = Field(
		default=8000,
		ge=1000,
		le=32000,
		description="BehavioralSpec JSON excerpt size for mandate synthesizer prompts (full spec).",
	)
	reviewer_context_intent_max_chars: int = Field(
		default=4000,
		ge=500,
		le=32000,
		description="ContextPacket char budget for intent_extractor.",
	)
	reviewer_context_mandate_synth_max_chars: int = Field(
		default=8000,
		ge=1000,
		le=32000,
		description="ContextPacket char budget for mandate_synthesizer.",
	)
	reviewer_context_plan_critic_max_chars: int = Field(
		default=6000,
		ge=1000,
		le=32000,
		description="ContextPacket char budget for draft_planner / plan_critic / plan_revision.",
	)
	reviewer_critique_packet_max_chars: int = Field(
		default=22000,
		ge=4000,
		le=64000,
		description=(
			"Quality ceiling for task-scoped critique evidence and critiquer ContextPacket "
			"(code slices + principles; diff hunk capped separately)."
		),
	)
	reviewer_context_critique_probe_max_chars: int = Field(
		default=16000,
		ge=2000,
		le=64000,
		description="ContextPacket char budget for critique_context_probe (pre-critiquer gather).",
	)
	reviewer_context_critiquer_max_chars: int = Field(
		default=20000,
		ge=2000,
		le=64000,
		description="ContextPacket char budget for general_critiquer LLM prompt.",
	)
	reviewer_context_critiquer_diff_hunk_max_chars: int = Field(
		default=4000,
		ge=500,
		le=32000,
		description="Per-section cap for git diff excerpt inside critiquer ContextPacket.",
	)
	reviewer_context_reflection_max_chars: int = Field(
		default=14000,
		ge=500,
		le=32000,
		description="ContextPacket char budget per adversarial_reflection batch.",
	)
	reviewer_context_verifier_gen_max_chars: int = Field(
		default=4000,
		ge=500,
		le=32000,
		description="ContextPacket char budget for verifier test_generator.",
	)
	reviewer_cleanup_redis_checkpoints: bool = Field(
		default=True,
		description="Delete per-PR Redis checkpoints after reviewer-agent experiments finish each graph run.",
	)
	reviewer_cleanup_require_full_reflection_quorum: bool = Field(
		default=False,
		description=(
			"When true, adversarial_cleanup drops a candidate unless every routed reflector specialty produced a "
			"ReflectionReport (strict quorum). When false (default), missing specialties abstain: if at least one "
			"relevant report exists and none of them reject, promotion uses only the reports that arrived "
			"(e.g. logic timed out but general accepted — avoids losing findings to graph/LLM timeouts)."
		),
	)
	reviewer_critique_revision_max_shard_chars: int = Field(
		default=16_000,
		ge=2_000,
		description="Approximate max characters of focused context JSON per critique-revision digest shard.",
	)
	reviewer_critique_revision_max_candidate_chars: int = Field(
		default=8_000,
		ge=500,
		description="Truncate inlined CandidateFinding JSON per digest shard prompt.",
	)
	reviewer_critique_revision_reduce_batch_size: int = Field(
		default=2,
		ge=1,
		le=4,
		description="Candidates per critique_revision_reduce LLM call (1–2 recommended to avoid output token ceiling).",
	)
	reviewer_critique_revision_max_completion_tokens: int = Field(
		default=16_384,
		ge=512,
		le=65536,
		description=(
			"Completion token cap for critique_revision digest and reduce structured-output calls "
			"(overrides reviewer_worker_max_completion_tokens for those nodes only)."
		),
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

	# Self-healing verifier (optional runtime proof in Docker)
	verifier_enabled: bool = Field(
		default=True,
		description=(
			"Enable verifier after focused_context or post_reflection_evidence_pass for eligible claim types. "
			"When true, runs only for claim types allowed below; use verifier_skip_if_no_sandbox to no-op when the sandbox runtime is absent."
		),
	)
	verifier_image: str = Field(
		default="verifier-test-env:latest",
		description="Docker image for verifier script execution when using a host-mounted checkout.",
	)
	verifier_clone_image: str = Field(
		default="agent-fs-sandbox",
		description=(
			"Docker image for verifier runs that git-clone the PR inside the container. "
			"Must include git (e.g. agent-fs-sandbox). Rebuild verifier-test-env after Dockerfile.verifier "
			"changes if you prefer a single image for both clone and test execution."
		),
	)
	verifier_clone_remote_in_container: bool = Field(
		default=True,
		description=(
			"When true (default), if repo_path is not a local directory, clone review_repo_url inside the "
			"verifier container at /repo (requires git in verifier_image). When false, use an empty /workspace "
			"only if verifier_require_repo_in_container is also false."
		),
	)
	verifier_require_repo_in_container: bool = Field(
		default=True,
		description=(
			"When true (default), verifier refuses snippet-only runs when no local checkout and remote clone "
			"metadata/URL is unavailable (attempt records harness/inconclusive instead of fake /repo symlink)."
		),
	)
	verifier_use_execution_workspace: bool = Field(
		default=True,
		description=(
			"After mounting or cloning at /repo, copy tree into a writable /exec_* workspace and run verifier "
			"scripts there (matches review sandbox RO + RW test area pattern)."
		),
	)
	verifier_reflection_batch_size: int = Field(
		default=3,
		ge=1,
		le=10,
		description="Max candidates per adversarial reflection LLM call (per specialty).",
	)
	verifier_test_timeout_seconds: int = Field(
		default=300,
		ge=10,
		le=3600,
		description="Wall-clock timeout per verifier script execution.",
	)
	verifier_max_attempts: int = Field(
		default=4,
		ge=1,
		le=5,
		description="Max test-generation/execute cycles per candidate before inconclusive (self-healing retries).",
	)
	verifier_run_on_defect: bool = Field(
		default=True,
		description="Run verifier for claim_type defect when other gates pass.",
	)
	verifier_run_on_security: bool = Field(
		default=True,
		description="Run verifier for claim_type security_risk (e.g. ReDoS, crash-on-input probes).",
	)
	verifier_run_on_performance: bool = Field(
		default=False,
		description="Run verifier for claim_type performance_regression.",
	)
	verifier_mock_heavy_deps: bool = Field(
		default=True,
		description="Instruct the generator to use sys.modules MagicMock prelude for heavy deps.",
	)
	verifier_total_budget_per_pr: int = Field(
		default=10,
		ge=1,
		le=50,
		description="Max verifier Send branches per focused_context wave.",
	)
	verifier_skip_if_no_sandbox: bool = Field(
		default=True,
		validation_alias=AliasChoices(
			"REVIEW_VERIFIER_SKIP_IF_NO_SANDBOX",
			"REVIEW_VERIFIER_SKIP_IF_NO_DOCKER",
		),
		description="If the active sandbox runtime is unavailable, skip verifier and continue the reviewer graph.",
	)
	verifier_require_focused_evidence: bool = Field(
		default=True,
		description=(
			"When true, only verify candidates that already have focused_context_results with snippets or search hits. "
			"Set false to allow verifier using candidate JSON + git diff only (e.g. needs_context without a focused_request)."
		),
	)
	verifier_ruff_enabled: bool = Field(
		default=True,
		description="Run `python -m ruff check . --no-cache` inside the verifier sandbox (advisory; avoids cache writes on read-only mounts).",
	)
	verifier_flake8_enabled: bool = Field(
		default=False,
		description="Run `python -m flake8` inside the verifier sandbox when enabled (after Ruff).",
	)
	verifier_lint_output_max_chars: int = Field(
		default=32_000,
		ge=1_000,
		le=500_000,
		description="Truncate each linter stdout/stderr stream stored on verifier attempts.",
	)
	verifier_focused_context_max_chars: int = Field(
		default=16_000,
		ge=2_000,
		le=120_000,
		description="Max characters of focused-context JSON passed to the verifier test generator.",
	)
	verifier_test_generator_max_completion_tokens: int = Field(
		default=8192,
		ge=1024,
		le=32_768,
		description="Completion token cap for verifier test-script generation (separate from worker default).",
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
		description="Approximate prompt token budget per community agent, converted to a rough character cap.",
	)
	semantic_agent_max_completion_tokens: int = Field(
		default=8192,
		ge=512,
		le=32768,
		description="Completion token cap for high-level community semantic summaries.",
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
		default=DEFAULT_LOCAL_MODEL_KEY,
		description=(
			"Models factory key for community semantic agents (same registry as reviewer_worker_model_key). "
			"Defaults to the local Qwen stack; set e.g. gemini-pro only if langchain-google-genai is installed."
		),
	)
	semantic_merge_model_key: str = Field(
		default=DEFAULT_LOCAL_MODEL_KEY,
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

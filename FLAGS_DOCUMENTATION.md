# Bushwhack Research: Comprehensive Flags & Configuration Guide

This document provides detailed documentation on all available command-line flags, environment variables, and configuration options for running research with Bushwhack.

---

## Table of Contents

1. [CLI Entry Points](#cli-entry-points)
2. [Command Flags](#command-flags)
3. [Environment Variables](#environment-variables)
4. [Configuration Groups](#configuration-groups)
5. [Usage Examples](#usage-examples)
6. [Troubleshooting](#troubleshooting)

---

## CLI Entry Points

Bushwhack provides these main entry points for running research:

### 1. `reviewer-agent` (Primary Research Entry Point)
**Package:** `src.reviewer_agent.main:main`

The parallel reviewer graph orchestrator for running multi-agent code reviews over benchmark datasets.

```bash
reviewer-agent [OPTIONS]
```

### 2. `remote-review-workflow` (Infrastructure Entry Point)
**Package:** `src.infrastructure.remote_review_workflow:main`

Low-level remote review workflow runner for direct repository analysis.

```bash
remote-review-workflow [OPTIONS]
```

### 3. Direct Python Scripts
- `python -m src.main [OPTIONS]` - Baseline one-node LangGraph flow
- `python -m src.solo_agent.main [OPTIONS]` - Solo-agent AACR dataset harness
- `python -m src.data.run_research_pipeline [OPTIONS]` - SWE-PRBench/AACR preprocessing pipeline
- `python scripts/cli.py --review` - Git-staged changes review

### 4. Cluster Check-First Wrapper
**Script:** `scripts/cluster/submit_batch2_review_checks.sh`

Convenience wrapper for the current check-first mental-model benchmark path. It submits
`scripts/cluster/run_bushwhack_custom_urls_2.sbatch` with `--review-check-mode`
set to `enforced` by default.

```bash
scripts/cluster/submit_batch2_review_checks.sh enforced
```

The full-suite Slurm launcher also defaults to the same current path. With no
extra reviewer flags, it runs the full dataset with `--trace`; check-first
execution is the reviewer default:

```bash
sbatch scripts/cluster/run_bushwhack_full_suite.sbatch
```

---

## Command Flags

### `reviewer-agent` Flags

The primary research command with comprehensive options for dataset-driven analysis.

#### Dataset & Input Selection

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--dataset` | choice | `aacr` | Which benchmark harness to use. Currently only `aacr` is fully wired. |
| `--dataset-path` | path | `data/processed/aacr_bench_graph_ready.csv` | Path to the processed dataset CSV file. |
| `--pr-url` | string | `None` | Optional exact PR URL to run from the processed dataset before applying `--limit`. Useful for targeted single-PR analysis. |
| `--pr-urls` | list | `None` | Optional explicit PR URL list to run instead of scanning the dataset. Used by the custom cluster launchers. |
| `--snapshot-id` | string | `None` | Load an existing exploration snapshot and skip Phase 2 semantic enrichment for matching PRs. In the modern default planner this still runs the mental-model/mandate path. |
| `--local` / `--remote` | choice | `--local` | Apply local Docker or remote Apptainer execution profile presets. |

#### Run Management

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--run-id` | string | `None` | Optional run identifier; a short UUID is automatically generated when omitted. Used for tracking and logging. |
| `--limit` | integer | `None` | Optional cap on the number of unique PRs to process. If combined with `--pr-url`, the exact PR is processed first, then remaining up to limit. |
| `--range` | range | `None` | Optional 1-based inclusive PR range after de-duplication, such as `11:20`, `11-`, or `11`. |
| `--output-root` | path | `None` | Override `reviewer_agent_output_dir` from settings. Specifies where artifacts are written (logs, manifests, findings). |
| `--repo-root` | path | `None` | Optional local repository root for direct context smoke runs. Enables testing against a local repository instead of cloned repos. |
| `--keep-redis-checkpoints` | boolean | `False` | Leave reviewer graph Redis checkpoints in place after each PR run. By default they are cleaned up after artifacts are written. |

#### Debugging & Tracing

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--trace` | boolean | `False` | Emit reviewer graph tracing logs, including bounded LLM request/response summaries and per-call token usage. Verbose output for debugging agent behavior. |
| `--review-check-mode` | choice | `enforced` | Debug override for check-first reviewer mode: `off`, `log_only`, or `enforced`. Omit it for the current contract-question/check-first path. |

#### LLM Configuration Overrides

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--llm-timeout` | integer | `None` | Override `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS` for local Qwen/OpenAI-compatible calls. In seconds. |
| `--llm-max-retries` | integer | `None` | Override `REVIEW_LOCAL_LLM_MAX_RETRIES` for local Qwen/OpenAI-compatible calls. |

#### Complete Flag Reference with Examples

```bash
# Full production run with all PRs
reviewer-agent \
  --dataset aacr \
  --dataset-path data/processed/aacr_bench_graph_ready.csv

# Limited run on first 5 PRs with tracing
reviewer-agent --limit 5 --trace

# Range run after PR de-duplication
reviewer-agent --range 11:20 --trace

# Single PR analysis
reviewer-agent --pr-url "https://github.com/infiniflow/ragflow/pull/6553"

# Current mental-model/check-first path on a single PR
reviewer-agent \
  --pr-url "https://github.com/infiniflow/ragflow/pull/6553" \
  --trace

# Snapshot resume with the same current path
reviewer-agent \
  --snapshot-id 28d358fa3aaf_comfyanonymous__ComfyUI__pr7952 \
  --pr-url "https://github.com/comfyanonymous/ComfyUI/pull/8000" \
  --trace

# Custom output directory
reviewer-agent --output-root /custom/path/logs

# Local repository smoke test
reviewer-agent --repo-root /local/repo/path --limit 1

# LLM override for faster testing
reviewer-agent \
  --limit 2 \
  --llm-timeout 60 \
  --llm-max-retries 2 \
  --trace
```

---

### `src.main` Flags

Baseline one-node LangGraph flow for simple analysis.

```bash
python -m src.main [OPTIONS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo-path` | path | `.` | Absolute or relative repository path to analyze. |
| `--diff-file` | path | `None` | Optional path to a text file containing git diff content. If not provided, empty diff is used. |
| `--user-goals` | string | `Baseline exploration run` | Optional high-level goals for the review. Influences agent behavior. |
| `--local` / `--remote` | choice | `--local` | Apply local Docker or remote Apptainer execution profile presets. |

#### Examples

```bash
# Basic run on current directory
python -m src.main

# Run with custom repository
python -m src.main --repo-path /path/to/repo

# Run with specific diff file and goals
python -m src.main \
  --repo-path /path/to/repo \
  --diff-file /path/to/changes.diff \
  --user-goals "Review for security vulnerabilities and performance optimizations"
```

---

### `src.solo_agent.main` Flags

Solo-agent one-shot AACR dataset harness.

```bash
python -m src.solo_agent.main [OPTIONS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--dataset` | choice | `aacr` | Which benchmark harness to use. Only `aacr` is wired today. |
| `--dataset-path` | path | `data/processed/aacr_bench_graph_ready.csv` | Path to the processed dataset CSV. |
| `--run-id` | string | `None` | Optional run identifier; generated when omitted. |
| `--limit` | integer | `None` | Optional cap on unique PRs to process. |
| `--output-root` | path | `None` | Override `solo_agent_output_dir` from settings. |
| `--local` / `--remote` | choice | `--local` | Apply local Docker or remote Apptainer execution profile presets. |

---

### `remote-review-workflow` Flags

Low-level remote sandbox preflight, AST, and optional structural graph workflow.

```bash
remote-review-workflow [OPTIONS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--repo-url` | string | `SANDBOX_REMOTE_TEST_URL` | Remote repository URL to clone into the sandbox. |
| `--head-commit` | string | `SANDBOX_REMOTE_TEST_HEAD` or `SANDBOX_REMOTE_TEST_COMMIT` | Head commit to inspect. |
| `--base-commit` | string | `SANDBOX_REMOTE_TEST_BASE` | Base commit for diffing; defaults to `<head>^` in the workflow when omitted. |
| `--ast-scope` | choice | `repository` | AST extraction scope: `repository` or `changed`. |
| `--max-ast-files` | integer | `0` | Optional cap for AST-scanned files; `0` means no cap. |
| `--ast-dump-output` | path | _(empty)_ | Optional output path for formatted AST dump JSON. |
| `--ast-dump-max-chars` | integer | `0` | Optional per-entry formatted AST cap; `0` means no cap. |
| `--graph-output` | path | _(empty)_ | Optional PNG path for an AST summary graph. |
| `--graph-title` | string | `Remote Review AST Summary` | Title for `--graph-output`. |
| `--structural-graph-json` | path | _(empty)_ | Optional StructuralGraphBuilder node-link JSON output path. |
| `--structural-topology-json` | path | _(empty)_ | Optional community/topology JSON output path. |
| `--topology-graph-output` | path | _(empty)_ | Optional PNG path for the topology graph. |
| `--topology-graph-title` | string | `Structural topology` | Title for `--topology-graph-output`. |
| `--community-max-fraction` | float | setting default | Override `REVIEW_COMMUNITY_MAX_FRACTION`. |
| `--community-min-split-size` | integer | setting default | Override `REVIEW_COMMUNITY_MIN_SPLIT_SIZE`. |
| `--community-max-files` | integer | setting default | Override `REVIEW_COMMUNITY_MAX_FILES`. |
| `--community-max-symbols` | integer | setting default | Override `REVIEW_COMMUNITY_MAX_SYMBOLS`. |

---

### `src.data.run_research_pipeline` Flags

Dataset preprocessing for SWE-PRBench and AACR-Bench.

```bash
python -m src.data.run_research_pipeline [OPTIONS]
```

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--target-languages` | list | configured target languages | Target language filter, such as `Python`. |
| `--skip-plots` | boolean | `False` | Skip plot generation and only produce processed CSVs. |
| `--no-raw-dump` | boolean | `False` | Skip writing raw dataset snapshots to `data/raw`. |

---

### `scripts/cli.py` Flags

Git-integrated pre-commit code review.

```bash
python scripts/cli.py [OPTIONS]
```

| Flag | Type | Description |
|------|------|-------------|
| `--review` | boolean | Trigger an agentic code review of staged changes. Reads from `git diff --cached`. |

#### Example

```bash
# Stage changes and trigger review
git add .
python scripts/cli.py --review
```

---

## Environment Variables

All environment variables must be prefixed with `REVIEW_` and can be set in a `.env` file or directly in the shell.

### AST (Abstract Syntax Tree) Configuration

#### Enable/Disable

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_AST_ENABLED` | boolean | `true` | Enable AST parsing for repository understanding. |
| `REVIEW_AST_PARSER_VERSION` | string | `v1` | Version tag included in AST cache keys for invalidation. Increment to force re-parse. |

#### MCP (Model Context Protocol) Transport

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_AST_MCP_ENABLED` | boolean | `false` | Use MCP transport for AST parsing; otherwise use native in-process parsing. |
| `REVIEW_AST_MCP_COMMAND` | string | `python` | Command used to start the AST MCP server process. |
| `REVIEW_AST_MCP_ARGS` | list | `["docker_mcp/fs-mcp/server.py"]` | Arguments for the AST MCP server command. Comma-separated or JSON array. |
| `REVIEW_AST_MCP_CWD` | path | `None` | Optional working directory used when launching AST MCP server. Defaults to project root. |
| `REVIEW_AST_MCP_TIMEOUT_SECONDS` | integer | `30` | Timeout for each MCP request in seconds. Range: 1-300. |
| `REVIEW_AST_MCP_PARSE_TOOL` | string | `parse_file` | Tool name used to parse a file AST via MCP. |
| `REVIEW_AST_MCP_ENTITY_TOOL` | string | `get_entity_details` | Tool name used to fetch a single entity from a file via MCP. |
| `REVIEW_AST_MCP_DEFINITIONS_TOOL` | string | `find_symbol_definitions` | MCP tool name for repo-wide symbol definition search. |

#### AST Caching

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_AST_CACHE_TTL_SECONDS` | integer | `3600` | TTL (Time-To-Live) for AST cache entries in Redis. In seconds. |
| `REVIEW_AST_FALLBACK_TO_SEARCH` | boolean | `true` | Keep non-AST fallback paths available when MCP is unavailable. |

---

### GitHub MCP Context Configuration

#### Enable/Disable

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_GITHUB_MCP_ENABLED` | boolean | `true` | Enable optional GitHub MCP context enrichment for documentation, PR context, linked issues, and focused-context fallback. When disabled, the reviewer preserves the local sandbox, ripgrep, and AST paths. |
| `REVIEW_DOCS_PREBRIEF_ENABLED` | boolean | `true` | Generate a bounded documentation/PR pre-brief before structural and semantic scanning. Disable to skip only the proposed-understanding stage. |

#### MCP Transport

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_GITHUB_MCP_COMMAND` | string | `python` | Command used to start the GitHub MCP server process. |
| `REVIEW_GITHUB_MCP_ARGS` | list | `["docker_mcp/github-mcp/server.py"]` | Arguments for the GitHub MCP server command. Comma-separated or JSON array. |
| `REVIEW_GITHUB_MCP_CWD` | path | `None` | Optional working directory used when launching the GitHub MCP server. |
| `REVIEW_GITHUB_MCP_TIMEOUT_SECONDS` | integer | `30` | Timeout for each GitHub MCP request in seconds. Range: 1-300. |

#### Context Bounds And Caching

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_GITHUB_MCP_CACHE_TTL_SECONDS` | integer | `3600` | TTL for GitHub MCP cache entries. In seconds. |
| `REVIEW_GITHUB_MCP_DOC_MAX_CHARS` | integer | `12000` | Maximum characters retained from each documentation file fetched via GitHub MCP. |
| `REVIEW_GITHUB_MCP_DOC_MAX_TOTAL_CHARS` | integer | `40000` | Maximum total characters retained across a documentation bundle fetched via GitHub MCP. |
| `REVIEW_GITHUB_MCP_PR_MAX_COMMENTS` | integer | `20` | Maximum number of PR or issue comments fetched for the docs pre-brief. |
| `REVIEW_GITHUB_MCP_PR_COMMENT_MAX_CHARS` | integer | `2000` | Maximum characters retained from each PR or issue comment. |
| `REVIEW_GITHUB_MCP_REVIEW_HISTORY_ENABLED` | boolean | `true` | Fetch bounded prior PR review/comment history for changed files before mandate planning. |
| `REVIEW_GITHUB_MCP_REVIEW_HISTORY_COMMITS_PER_FILE` | integer | `12` | Max recent commits to inspect per changed file for historical review context. |
| `REVIEW_GITHUB_MCP_REVIEW_HISTORY_PRS_PER_FILE` | integer | `3` | Max prior PRs to inspect per changed file for historical review context. |
| `REVIEW_GITHUB_MCP_REVIEW_HISTORY_COMMENTS_PER_PR` | integer | `30` | Max review/issue comments to fetch per prior PR. |
| `REVIEW_GITHUB_MCP_REVIEW_HISTORY_MAX_TOTAL_CHARS` | integer | `8000` | Max rendered characters of prior review history added to the mental-model ledger. |
| `REVIEW_GITHUB_MCP_DOC_PATHS` | list | common README/CONTRIBUTING/SECURITY/CHANGELOG/docs paths | Ordered documentation paths attempted for docs pre-brief and focused-context fallback. |
| `REVIEW_GITHUB_MCP_FOCUSED_CONTEXT_DOC_FALLBACK` | boolean | `false` | Fetch configured docs through GitHub MCP if sandbox ripgrep finds no hits for focused-context symbol/text queries. |
| `REVIEW_GITHUB_MCP_DOC_DISCOVERY_ENABLED` | boolean | `true` | Discover markdown docs via GitHub API before falling back to the static doc-path list. |
| `REVIEW_GITHUB_MCP_DOC_DISCOVERY_MAX_PATHS` | integer | `30` | Max discovered doc paths to attempt. |
| `REVIEW_DOCS_PREBRIEF_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used to summarize the documentation/PR pre-brief. Must match a key in `infrastructure.llm.factory.MODELS`. |

---

### Execution profiles (`--local` / `--remote`)

| CLI flag | `REVIEW_SANDBOX_BACKEND` | Typical use |
|----------|--------------------------|-------------|
| `--local` (default) | `docker` | Laptop / dev: Docker sandbox, `docker-compose.redis.yml`, LLM via SSH port-forward |
| `--remote` | `apptainer` | Slurm node: Apptainer `.sif`, in-job or loopback Redis, job-local vLLM |

Cluster launch examples live in `scripts/cluster/`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_RUN_PROFILE` | string | _(unset)_ | Optional preset: `local` or `remote` (same as CLI flags). |
| `REVIEW_SANDBOX_BACKEND` | string | `docker` | Sandbox runtime: `docker` or `apptainer`. |
| `REVIEW_APPTAINER_BINARY` | string | `apptainer` | Apptainer executable on PATH. |
| `REVIEW_APPTAINER_IMAGE` | string | _(empty)_ | Path to review sandbox `.sif`. |
| `REVIEW_APPTAINER_VERIFIER_IMAGE` | string | _(empty)_ | Path to verifier `.sif`. |
| `REVIEW_APPTAINER_INSTANCE_DIR` | string | _(unset)_ | Optional Apptainer instance state directory. |
| `REVIEW_APPTAINER_BIND_TMPFS` | boolean | `true` | Use `--writable-tmpfs` for Apptainer instances. |
| `REVIEW_APPTAINER_EXTRA_BIND` | list | `[]` | Extra bind mounts (`host:container[:opts]`). |

---

### Redis Configuration

#### Connection & Checkpointing

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REDIS_ENABLED` | boolean | `true` | Enable Redis-backed LangGraph checkpointing. Disable for local testing without Redis. |
| `REVIEW_REDIS_URL` | string | `redis://localhost:6379/0` | Redis connection URL used for LangGraph checkpointing. Format: `redis://[user:password@]host:port/db` |
| `REVIEW_REDIS_NAMESPACE` | string | `langgraph` | Namespace prefix for Redis checkpoint keys. Prevents key collisions in shared instances. |
| `REVIEW_REDIS_TTL_SECONDS` | integer | `3600` | TTL for Redis checkpoint entries. In seconds. |
| `REVIEW_REVIEWER_CLEANUP_REDIS_CHECKPOINTS` | boolean | `true` | Delete per-PR reviewer graph Redis checkpoints after artifacts are written. `reviewer-agent --keep-redis-checkpoints` sets this false for the current process. |

#### Example Docker Compose Setup (`--local`)

```bash
docker-compose -f docker-compose.redis.yml up -d
export REVIEW_REDIS_URL=redis://localhost:6379/0
export REVIEW_REDIS_ENABLED=true
```

#### Example cluster loopback Redis (`--remote`)

```bash
source scripts/cluster/start_local_redis.sh
python -m src.reviewer_agent.main --remote ...
```

---

### API Keys & Authentication

#### Cloud LLM Providers

| Variable | Type | Description |
|----------|------|-------------|
| `REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN` or `GITHUB_PERSONAL_ACCESS_TOKEN` | string | GitHub personal access token for PR API enrichment (fetching PR metadata, comments, reviews). |
| `REVIEW_GOOGLE_API_KEY` or `GOOGLE_API_KEY` | string | Google API key for Gemini model access. |
| `REVIEW_OPENAI_API_KEY` or `OPENAI_API_KEY` | string | OpenAI API key for hosted OpenAI model access. |
| `REVIEW_ANTHROPIC_API_KEY` or `ANTHROPIC_API_KEY` | string | Anthropic API key for Claude model access. |

#### LangSmith Tracing

These settings are mirrored to the `LANGSMITH_*` / `LANGCHAIN_CALLBACKS_BACKGROUND` environment variables that LangChain and LangGraph read at runtime.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_LANGSMITH_TRACING` or `LANGSMITH_TRACING` | boolean | `false` | Enable LangSmith tracing for LangGraph spans and LangChain model calls. |
| `REVIEW_LANGSMITH_API_KEY` or `LANGSMITH_API_KEY` | string | _(unset)_ | LangSmith API key. Required when tracing is enabled. |
| `REVIEW_LANGSMITH_PROJECT` or `LANGSMITH_PROJECT` | string | `bushwhack` | LangSmith project name. |
| `REVIEW_LANGSMITH_ENDPOINT` or `LANGSMITH_ENDPOINT` | string | _(unset)_ | Optional regional or self-hosted LangSmith API endpoint. |
| `REVIEW_LANGSMITH_WORKSPACE_ID` or `LANGSMITH_WORKSPACE_ID` | string | _(unset)_ | Optional workspace ID for API keys with multiple workspaces. |
| `REVIEW_LANGSMITH_CALLBACKS_BACKGROUND` or `LANGCHAIN_CALLBACKS_BACKGROUND` | boolean | _(unset)_ | Optional callback background mode for trace flushing. |
| `REVIEW_LANGSMITH_HIDE_INPUTS` or `LANGSMITH_HIDE_INPUTS` | boolean | `false` | Hide run inputs before upload. Useful if input payloads exceed LangSmith limits or contain sensitive data. |
| `REVIEW_LANGSMITH_HIDE_OUTPUTS` or `LANGSMITH_HIDE_OUTPUTS` | boolean | `true` | Hide run outputs before upload. Enabled by default because reviewer graph outputs may contain large LangGraph state payloads. |

#### Local LLM Configuration (Ollama/LM Studio/vLLM)

**Hotswap default model:** edit `src/infrastructure/llm/defaults.py` (`DEFAULT_LOCAL_MODEL_KEY` and `DEFAULT_LOCAL_MODEL_PATH`). That registers the vLLM model id and sets defaults for all `REVIEW_*_MODEL_KEY` settings unless overridden in `.env`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_LOCAL_LLM_BASE_URL` | string | `http://localhost:8000/v1` | OpenAI-compatible base URL for local models (Qwen via Ollama, LM Studio, vLLM). |
| `REVIEW_LOCAL_LLM_API_KEY` | string | `local` | API key placeholder for OpenAI-compatible local model servers. |
| `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS` | integer | `600` | Request timeout for OpenAI-compatible local model servers. Range: 1-3600. |
| `REVIEW_LOCAL_LLM_STATUS_TIMEOUT_SECONDS` | float | `5.0` | Short timeout for local model server health/status probes. Range: 0.5-60. |
| `REVIEW_LOCAL_LLM_MAX_RETRIES` | integer | `0` | Retry count for OpenAI-compatible local model requests. Range: 0-10. |
| `REVIEW_LLM_TEMPERATURE` | float | _(unset)_ | Optional temperature override for planner, worker, and synthesizer LLM calls. |
| `REVIEW_LLM_PRESENCE_PENALTY` | float | _(unset)_ | Optional presence-penalty override for OpenAI-compatible providers. |

---

### Reviewer Agent Configuration

#### Model Selection

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REVIEWER_PLANNER_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used by the reviewer planner. Must match a key in `infrastructure.llm.factory.MODELS`. For Ollama: use the corresponding local model key. |
| `REVIEW_REVIEWER_PLANNER_MAX_COMPLETION_TOKENS` | integer | `12288` | Per-invocation completion token cap for planner-role LLM calls. |
| `REVIEW_REVIEWER_WORKER_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used by reviewer workers, critiquer, reflection, and revision nodes. For Ollama: use the corresponding local model key. |
| `REVIEW_REVIEWER_WORKER_MAX_COMPLETION_TOKENS` | integer | `12288` | Per-invocation completion token cap for worker-role LLM calls. |
| `REVIEW_REVIEWER_CRITIQUER_MAX_COMPLETION_TOKENS` | integer | `20480` | Completion token cap for the general critiquer structured-output call. |
| `REVIEW_REVIEWER_CRITIQUER_SINGLE_FILE_MAX_CHARS` | integer | `80000` | Max target-file excerpt size when a review task has exactly one target file. |

#### Agent Behavior

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REVIEWER_CHECK_MODE` | choice | `enforced` | Debug override for reviewer check mode: `enforced` uses evidence-backed checks, `log_only` compiles and validates checks before the current critiquer, and `off` preserves candidate-first review for comparison. |
| `REVIEW_REVIEWER_ALLOW_HOST_PR_WORKTREE` | boolean | `true` | Allow snapshot AACR resume to clone the PR head into a host worktree under the snapshot root for AST and local ripgrep. |
| `REVIEW_REVIEWER_ACTOR_CRITIC_MAX_PLAN_REVISIONS` | integer | `5` | Max plan-revision cycles after `plan_critic` before emitting tasks. |
| `REVIEW_REVIEWER_MENTAL_MODEL_MAX_QUERIES_PER_RUN` | integer | `40` | Cap `query_mental_model` tool invocations per graph run. |
| `REVIEW_REVIEWER_MENTAL_MODEL_MAX_ANSWER_CHARS` | integer | `2500` | Max characters returned from `query_mental_model`. |
| `REVIEW_REVIEWER_MANDATE_BOOTSTRAP_MAX_STEPS` | integer | `8` | ReAct steps for the mandatory bootstrap `mandate_explorer` pass. |
| `REVIEW_REVIEWER_MANDATE_TARGETED_MAX_STEPS` | integer | `4` | ReAct steps per critic-triggered targeted explorer pass. |
| `REVIEW_REVIEWER_MANDATE_EXPLORER_MAX_OBSERVATION_CHARS` | integer | `4000` | Max characters per mandate-explorer tool result. |
| `REVIEW_REVIEWER_MANDATE_LEDGER_MAX_TOTAL_CHARS` | integer | `48000` | Soft cap on total mandate tool-observation preview chars in `exploration_ledger`. |
| `REVIEW_REVIEWER_MANDATE_BOOTSTRAP_DIGEST_MAX_CHARS` | integer | `1200` | Planner/critic bootstrap digest size stored in mental-model metadata. |
| `REVIEW_REVIEWER_MANDATE_PLAN_MAX_CYCLES` | integer | `5` | Max joint critic cycles before `plan_emit`. |
| `REVIEW_REVIEWER_MANDATE_SPEC_EXCERPT_MAX_CHARS` | integer | `8000` | BehavioralSpec JSON excerpt size for mandate synthesizer prompts. |

Current reviewer path:

- Full graph with mental-model planning, mandate explorer, adversarial workers, and review adjudication is the only live implementation.
- Check-first execution is the default. Use `--review-check-mode off` or `REVIEW_REVIEWER_CHECK_MODE=off` only for short-term debugging comparisons.
- `run_meta.json` and each raw PR artifact include `effective_reviewer_mode` so hidden environment state cannot make two artifacts look identical while taking different reviewer paths.

Check-first AACR runs also write health artifacts:

- `coverage_audit.json`, when generated with positive-sample inputs available, reports compiled, valid, focused-context, executor, candidate, and final coverage by positive path.
- `manifest.csv` includes invalid reason JSON and review-check health warnings.
- `run_meta.json` includes GitHub MCP preflight status. If MCP tools change, deploy `docker_mcp/github-mcp/server.py` with `src`; replacing only `src` can leave stale cluster MCP tools.

---

### Reviewer Context, Triage, And Adjudication Budgets

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REVIEW_FULL_FILE_MAX_CHARS` | integer | `500000` | Max characters returned per file for focused-context full-file reads. |
| `REVIEW_REVIEW_FULL_FILE_MAX_TOTAL_CHARS` | integer | `600000` | Max total characters across full-file payloads in one focused-context result. |
| `REVIEW_REVIEWER_CONTEXT_INTENT_MAX_CHARS` | integer | `4000` | ContextPacket char budget for `intent_extractor`. |
| `REVIEW_REVIEWER_CONTEXT_MANDATE_SYNTH_MAX_CHARS` | integer | `8000` | ContextPacket char budget for mandate synthesis. |
| `REVIEW_REVIEWER_CONTEXT_PLAN_CRITIC_MAX_CHARS` | integer | `6000` | ContextPacket char budget for draft planner, plan critic, and plan revision. |
| `REVIEW_REVIEWER_CRITIQUE_PACKET_MAX_CHARS` | integer | `22000` | Quality ceiling for task-scoped critique evidence and critiquer ContextPacket. |
| `REVIEW_REVIEWER_CONTEXT_CRITIQUE_PROBE_MAX_CHARS` | integer | `16000` | ContextPacket char budget for `critique_context_probe`. |
| `REVIEW_REVIEWER_CONTEXT_CRITIQUER_MAX_CHARS` | integer | `20000` | ContextPacket char budget for `general_critiquer`. |
| `REVIEW_REVIEWER_CONTEXT_CRITIQUER_DIFF_HUNK_MAX_CHARS` | integer | `4000` | Per-section git-diff excerpt cap inside critiquer packets. |
| `REVIEW_REVIEWER_CONTEXT_REFLECTION_MAX_CHARS` | integer | `14000` | ContextPacket char budget per adversarial-reflection batch. |
| `REVIEW_REVIEWER_CONTEXT_VERIFIER_GEN_MAX_CHARS` | integer | `4000` | ContextPacket char budget for verifier test generation. |
| `REVIEW_REVIEWER_CLEANUP_REQUIRE_FULL_REFLECTION_QUORUM` | boolean | `false` | Require every routed reflector specialty before promotion when true; by default missing specialties abstain. |
| `REVIEW_REVIEWER_TRIAGE_MAX_BATCH_CHARS` | integer | `28000` | Approximate max candidate-evidence chars per review-evidence-triage batch. |
| `REVIEW_REVIEWER_TRIAGE_MAX_CANDIDATE_CHARS` | integer | `8000` | Max rendered chars for one candidate packet in triage prompts. |
| `REVIEW_REVIEWER_TRIAGE_MAX_COMPLETION_TOKENS` | integer | `12288` | Completion token cap for review-evidence-triage structured-output calls. |
| `REVIEW_REVIEWER_ADJUDICATOR_MAX_BATCH_CHARS` | integer | `32000` | Approximate max candidate-evidence chars per review-adjudicator batch. |
| `REVIEW_REVIEWER_ADJUDICATOR_MAX_CANDIDATE_CHARS` | integer | `10000` | Max rendered chars for one candidate evidence packet in adjudicator prompts. |
| `REVIEW_REVIEWER_ADJUDICATOR_FOCUSED_CONTEXT_MAX_CHARS` | integer | `2400` | Per focused-context blob cap inside adjudicator evidence packets. |
| `REVIEW_REVIEWER_ADJUDICATOR_MAX_COMPLETION_TOKENS` | integer | `16384` | Completion token cap for review-adjudicator structured-output calls. |
| `REVIEW_REVIEWER_CRITIQUE_REVISION_MAX_SHARD_CHARS` | integer | `16000` | Approximate focused-context JSON budget per critique-revision digest shard. |
| `REVIEW_REVIEWER_CRITIQUE_REVISION_MAX_CANDIDATE_CHARS` | integer | `8000` | Truncation guard for CandidateFinding JSON in digest prompts. |
| `REVIEW_REVIEWER_CRITIQUE_REVISION_REDUCE_BATCH_SIZE` | integer | `2` | Candidates per critique-revision-reduce LLM call. |
| `REVIEW_REVIEWER_CRITIQUE_REVISION_MAX_COMPLETION_TOKENS` | integer | `16384` | Completion token cap for critique-revision digest and reduce calls. |
| `REVIEW_REVIEWER_REFLECTION_RETRY_BACKOFF_SECONDS` | float | `5.0` | Base backoff between adversarial reflection retries while the local model server is active. |
| `REVIEW_REVIEWER_REFLECTION_TIMEOUT_PATIENCE_SECONDS` | integer | `1800` | Extra wall-clock budget for reflector timeout retries while status probes succeed. |

---

### Runtime Verifier Configuration

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_VERIFIER_ENABLED` | boolean | `true` | Enable verifier after focused-context or post-reflection evidence routing for eligible claim types. |
| `REVIEW_VERIFIER_IMAGE` | string | `verifier-test-env:latest` | Docker image for verifier script execution when using a host-mounted checkout. |
| `REVIEW_VERIFIER_CLONE_IMAGE` | string | `agent-fs-sandbox` | Docker image for verifier runs that clone the PR inside the container. |
| `REVIEW_VERIFIER_CLONE_REMOTE_IN_CONTAINER` | boolean | `true` | Clone remote review repos inside the verifier container when no local checkout exists. |
| `REVIEW_VERIFIER_REQUIRE_REPO_IN_CONTAINER` | boolean | `true` | Refuse snippet-only verifier runs when no local checkout and no remote clone metadata/URL are available. |
| `REVIEW_VERIFIER_USE_EXECUTION_WORKSPACE` | boolean | `true` | Copy mounted/cloned repo into a writable `/exec_*` workspace before running verifier scripts. |
| `REVIEW_VERIFIER_REFLECTION_BATCH_SIZE` | integer | `3` | Max candidates per adversarial reflection LLM call per specialty. |
| `REVIEW_VERIFIER_TEST_TIMEOUT_SECONDS` | integer | `300` | Wall-clock timeout per verifier script execution. |
| `REVIEW_VERIFIER_MAX_ATTEMPTS` | integer | `4` | Max test-generation/execute cycles per candidate. |
| `REVIEW_VERIFIER_RUN_ON_DEFECT` | boolean | `true` | Run verifier for `defect` candidates when other gates pass. |
| `REVIEW_VERIFIER_RUN_ON_SECURITY` | boolean | `true` | Run verifier for `security_risk` candidates when other gates pass. |
| `REVIEW_VERIFIER_RUN_ON_PERFORMANCE` | boolean | `false` | Run verifier for `performance_regression` candidates. |
| `REVIEW_VERIFIER_MOCK_HEAVY_DEPS` | boolean | `true` | Instruct the generator to use `sys.modules` MagicMock prelude for heavy deps. |
| `REVIEW_VERIFIER_PREPARE_ENV_ENABLED` | boolean | `true` | Enable best-effort verifier venv prep and target import probes. |
| `REVIEW_VERIFIER_PREPARE_ENV_INSTALL_DEPS` | boolean | `false` | Reserved for future targeted dependency installation; broad requirements install remains off. |
| `REVIEW_VERIFIER_TOTAL_BUDGET_PER_PR` | integer | `10` | Max verifier Send branches per focused-context wave. |
| `REVIEW_VERIFIER_SKIP_IF_NO_SANDBOX` or `REVIEW_VERIFIER_SKIP_IF_NO_DOCKER` | boolean | `true` | Skip verifier and continue if the active sandbox runtime is unavailable. |
| `REVIEW_VERIFIER_REQUIRE_FOCUSED_EVIDENCE` | boolean | `true` | Only verify candidates with focused-context snippets/search hits when true. |
| `REVIEW_VERIFIER_RUFF_ENABLED` | boolean | `true` | Run `python -m ruff check . --no-cache` inside verifier sandbox when available. |
| `REVIEW_VERIFIER_FLAKE8_ENABLED` | boolean | `false` | Run `python -m flake8` inside verifier sandbox when enabled. |
| `REVIEW_VERIFIER_LINT_OUTPUT_MAX_CHARS` | integer | `32000` | Truncate each linter stdout/stderr stream stored on verifier attempts. |
| `REVIEW_VERIFIER_FOCUSED_CONTEXT_MAX_CHARS` | integer | `16000` | Max focused-context JSON chars passed to verifier test generation. |
| `REVIEW_VERIFIER_TEST_GENERATOR_MAX_COMPLETION_TOKENS` | integer | `8192` | Completion token cap for verifier test-script generation. |
| `REVIEW_VERIFIER_SOURCE_ONLY_STATIC_ENABLED` | boolean | `true` | Run cheap source-only verifier checks before sandbox execution. |

---

### Output & Paths

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_SOLO_AGENT_OUTPUT_DIR` | path | `logs/solo_agent` | Root directory for solo-agent experiment artifacts (raw transcripts, findings, manifests). |
| `REVIEW_REVIEWER_AGENT_OUTPUT_DIR` | path | `logs/reviewer_agent` | Root directory for reviewer-graph experiment artifacts. |

---

### Structural Topology Configuration

#### Community Detection

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_STRUCTURAL_TOPOLOGY_ENABLED` | boolean | `true` | Run community detection and cohesion scoring after structural graph build. |
| `REVIEW_COMMUNITY_MAX_FRACTION` | float | `0.25` | Communities larger than this fraction of clustering-graph nodes may be split. Range: 0.01-1.0. |
| `REVIEW_COMMUNITY_MIN_SPLIT_SIZE` | integer | `10` | Minimum node count before fractional split threshold applies. |
| `REVIEW_COMMUNITY_MAX_FILES` | integer | `0` | If >0, split communities with more file nodes than this cap. |
| `REVIEW_COMMUNITY_MAX_SYMBOLS` | integer | `0` | If >0, split communities with more symbol nodes than this cap. |
| `REVIEW_LOUVAIN_SEED` | integer | `42` | Random seed for NetworkX Louvain fallback (deterministic partitions). |

---

### Semantic Enrichment And Repository KB

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_SNAPSHOT_BASE_PATH` | path | `./bushwhack_runs` | Root directory for exploration snapshot disk trees. |
| `REVIEW_SNAPSHOT_KEEP_FULL_GRAPH` | boolean | `true` | Keep `graph/full_graph.json` after `snapshot_pin`. |
| `REVIEW_SEMANTIC_ENRICHMENT_ENABLED` | boolean | `true` | Run semantic enrichment after structural extraction when topology exists. |
| `REVIEW_SEMANTIC_LEGACY_COMMUNITY_AGENTS_ENABLED` | boolean | `false` | Run legacy LLM community semantic agents after Review KB dispatch. Modern runs leave this off. |
| `REVIEW_SEMANTIC_MAX_TOKENS_PER_COMMUNITY` | integer | `8000` | Approximate prompt token budget per community agent. |
| `REVIEW_SEMANTIC_AGENT_MAX_COMPLETION_TOKENS` | integer | `8192` | Completion token cap for high-level community semantic summaries. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_MAX_COMPLETION_TOKENS` | integer | `2048` | Completion token cap for bounded Repository KB distillation calls. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_MODE` | choice | `on_demand` | Repository KB LLM enrichment mode: `review_neighborhood`, `on_demand`, `full`, or `off`. |
| `REVIEW_REPOSITORY_KB_INTELLIGENCE_PROFILE` | choice | `standard` | Soft effort profile for adaptive Repository KB intelligence: `lean`, `standard`, `deep`, or `offline`. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_BUDGET_MODE` | choice | `adaptive` | Budget strategy for Repository KB distillation: `adaptive` or `unbounded`. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_HARD_TOKEN_CEILING` | integer | _(unset)_ | Optional safety ceiling for adaptive Repository KB distillation. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_REVIEW_NEIGHBORHOOD_MAX_COMMUNITIES` | integer | `4` | Max review-neighborhood communities used for boundary-pack distillation. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_COMMUNITIES_PER_CALL` | integer | `1` | Max community evidence packs included in one distillation call. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_MAX_PROMPT_CHARS` | integer | `8000` | Approximate prompt character cap for one distillation call. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_MAX_SHARDS_PER_COMMUNITY` | integer | `6` | Max bounded evidence shards to distill per community. |
| `REVIEW_REPOSITORY_KB_DISTILLATION_SHARD_MERGE_MAX_PROMPT_CHARS` | integer | `16000` | Approximate prompt cap for merging shard summaries into a community summary. |
| `REVIEW_SEMANTIC_MERGE_MAX_PROMPT_CHARS` | integer | `24000` | Prompt character cap for global semantic merge synthesis. |
| `REVIEW_SEMANTIC_MERGE_MAX_COMPLETION_TOKENS` | integer | `2048` | Completion token cap for global semantic merge synthesis. |
| `REVIEW_SEMANTIC_MAX_FILES_PER_AGENT` | integer | `20` | Max file nodes to include per community work item. |
| `REVIEW_SEMANTIC_MAX_SYMBOLS_PER_AGENT` | integer | `50` | Max symbol nodes to include per community work item. |
| `REVIEW_SEMANTIC_MAX_PARALLEL_AGENTS` | integer | `4` | Max Phase 2 community semantic agents allowed to call the LLM concurrently. |
| `REVIEW_SEMANTIC_AGENT_MAX_RETRIES` | integer | `2` | Retries per community semantic LLM call before degraded summary. |
| `REVIEW_SEMANTIC_AGENT_RETRY_BACKOFF_SECONDS` | float | `5.0` | Base backoff between community semantic LLM retries. |
| `REVIEW_SEMANTIC_AGENT_TIMEOUT_PATIENCE_SECONDS` | integer | `1800` | Extra wall-clock retry budget while the local LLM server responds to status probes. |
| `REVIEW_UNVERIFIED_CALL_MAX_RESOLUTION_ROUNDS` | integer | `3` | Max resolver self-loop rounds for newly surfaced unverified call targets. |
| `REVIEW_SEMANTIC_MODEL_KEY` | string | `qwen3.5-122b` | Models factory key for community semantic agents. |
| `REVIEW_SEMANTIC_MERGE_MODEL_KEY` | string | `qwen3.5-122b` | Models factory key for global semantic synthesis. |
| `REVIEW_SKIP_TRIVIAL_COMMUNITIES` | boolean | `true` | Synthesize trivial `__init__`-only communities without LLM. |
| `REVIEW_DIAGNOSTICS_GOD_NODES_TOP_N` | integer | `10` | Number of high-degree nodes to include in diagnostics. |
| `REVIEW_DIAGNOSTICS_BRIDGE_NODES_TOP_N` | integer | `5` | Number of bridge nodes to include in diagnostics. |
| `REVIEW_DIAGNOSTICS_CROSS_COMMUNITY_EDGES_TOP_N` | integer | `10` | Number of cross-community edges to include in diagnostics. |
| `REVIEW_DIAGNOSTICS_LOW_COHESION_THRESHOLD` | float | `0.15` | Cohesion threshold for flagging knowledge gaps. |
| `REVIEW_SEMANTIC_SNAPSHOT_POINTER_TTL_SECONDS` | integer | `86400` | TTL for Redis snapshot pointer keys. |

---

### Solo Agent Configuration

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_SOLO_AGENT_MAX_DIFF_CHARS` | integer | `60000` | Maximum characters of unified diff inlined into the solo-agent prompt. |
| `REVIEW_SOLO_AGENT_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used by the solo-agent worker. |
| `REVIEW_SOLO_AGENT_PROMPT_VERSION` | string | `v1` | Prompt template version stamped on solo-agent run metadata. |

---

## Configuration Groups

### Configuration by Use Case

#### 1. **Local Development & Testing**

```bash
# .env file
REVIEW_REDIS_ENABLED=false
REVIEW_AST_MCP_ENABLED=false
REVIEW_GITHUB_MCP_ENABLED=false
REVIEW_LOCAL_LLM_BASE_URL=http://localhost:8000/v1
# Defaults: src/infrastructure/llm/defaults.py (DEFAULT_LOCAL_MODEL_KEY / PATH)
```

```bash
# Command
reviewer-agent --limit 1 --trace --repo-root /local/repo
```

#### 2. **Production Benchmark Run**

```bash
# .env file
REVIEW_REDIS_ENABLED=true
REVIEW_REDIS_URL=redis://prod-redis-host:6379/0
REVIEW_AST_MCP_ENABLED=true
REVIEW_GITHUB_MCP_ENABLED=true
# Defaults: src/infrastructure/llm/defaults.py (DEFAULT_LOCAL_MODEL_KEY / PATH)
```

```bash
# Command
reviewer-agent \
  --dataset aacr \
  --dataset-path data/processed/aacr_bench_graph_ready.csv \
  --output-root /results/aacr_run_$(date +%s)
```

#### 3. **Single PR Analysis**

```bash
reviewer-agent \
  --pr-url "https://github.com/infiniflow/ragflow/pull/6553" \
  --trace \
  --output-root /results/single_pr
```

#### 4. **Current Check-First Mental-Model Run**

```bash
# Local or direct invocation
reviewer-agent \
  --pr-url "https://github.com/infiniflow/ragflow/pull/6553" \
  --trace

# Cluster full-suite launcher; no reviewer-mode flags required
sbatch scripts/cluster/run_bushwhack_full_suite.sbatch
```

This routes each task through `review_check_compiler` / validator / executor /
evidence gate instead of relying on direct candidate generation.

#### 5. **Performance Tuning (Fast Iteration)**

```bash
# .env file
REVIEW_LOCAL_LLM_TIMEOUT_SECONDS=120
REVIEW_LOCAL_LLM_MAX_RETRIES=2
REVIEW_AST_CACHE_TTL_SECONDS=7200
```

```bash
# Command
reviewer-agent --limit 5 --llm-timeout 120 --llm-max-retries 2
```

#### 6. **Cloud LLM with OpenAI**

```bash
# .env file
REVIEW_OPENAI_API_KEY=sk-...
REVIEW_REVIEWER_PLANNER_MODEL_KEY=<registered-openai-model-key>
REVIEW_REVIEWER_WORKER_MODEL_KEY=<registered-openai-model-key>
```

```bash
reviewer-agent --limit 10
```

---

## Usage Examples

### Example 1: Quick Local Test

```bash
# Set up minimal local environment
export REVIEW_REDIS_ENABLED=false
export REVIEW_AST_MCP_ENABLED=false
export REVIEW_GITHUB_MCP_ENABLED=false

# Run on a single PR with verbose output
reviewer-agent \
  --pr-url "https://github.com/example/repo/pull/123" \
  --trace \
  --llm-timeout 120
```

### Example 2: Benchmark Dataset Run

```bash
# Run complete AACR benchmark with 50 PRs
reviewer-agent \
  --dataset aacr \
  --dataset-path data/processed/aacr_bench_graph_ready.csv \
  --limit 50 \
  --output-root logs/reviewer_agent/aacr_full_run_$(date +%Y%m%d_%H%M%S)
```

### Example 3: Check-First Contract-Question Run

```bash
# Run the current mental-model path on the custom cluster PR list
scripts/cluster/submit_batch2_review_checks.sh enforced
```

Equivalent direct shape:

```bash
python -m src.reviewer_agent.main \
  --remote \
  --trace \
  --pr-urls "https://github.com/example/repo/pull/123"
```

### Example 4: Smoke Test with Local Repo

```bash
# Test on a local repository with first 3 PRs worth of data
reviewer-agent \
  --repo-root /path/to/local/repo \
  --limit 3 \
  --trace
```

### Example 5: Baseline Analysis

```bash
# Run simple baseline model without graph orchestration
python -m src.main \
  --repo-path /path/to/repo \
  --diff-file changes.diff \
  --user-goals "Performance review: identify N+1 queries and inefficient database operations"
```

### Example 6: Git Pre-Commit Integration

```bash
# Stage your changes
git add src/new_feature.py

# Trigger review via CLI
python scripts/cli.py --review
```

---

## Troubleshooting

### Common Issues & Solutions

#### 1. Redis Connection Error

**Error:** `ConnectionError: Error -2 connecting to localhost:6379`

**Solution:**
```bash
# Start Redis container
docker-compose -f docker-compose.redis.yml up -d

# Or disable Redis if not needed for testing
export REVIEW_REDIS_ENABLED=false
```

#### 2. LLM Timeout

**Error:** `Timeout waiting for LLM response`

**Solution:**
```bash
# Increase timeout via flag
reviewer-agent --llm-timeout 300 --llm-max-retries 3

# Or via environment variable
export REVIEW_LOCAL_LLM_TIMEOUT_SECONDS=300
export REVIEW_LOCAL_LLM_MAX_RETRIES=3
```

#### 3. MCP Server Connection Issues

**Error:** `MCP server failed to start` or `Connection refused`

**Solution:**
```bash
# Disable MCP and fall back to in-process parsing
export REVIEW_AST_MCP_ENABLED=false
export REVIEW_AST_FALLBACK_TO_SEARCH=true

# Disable optional GitHub context enrichment while preserving local review
export REVIEW_GITHUB_MCP_ENABLED=false
```

For check-first cluster runs, inspect `run_meta.json -> mcp_preflight`. A degraded preflight with `missing_required_tools: ["get_commits_for_path"]` usually means `docker_mcp/github-mcp/server.py` was not uploaded with the current code.

#### 4. Out of Memory During AST Parsing

**Solution:**
```bash
# Clear AST cache and reduce TTL
export REVIEW_AST_CACHE_TTL_SECONDS=1800

# Or disable AST entirely for testing
export REVIEW_AST_ENABLED=false
```

#### 5. Dataset Not Found

**Error:** `FileNotFoundError: data/processed/aacr_bench_graph_ready.csv`

**Solution:**
```bash
# Verify dataset exists
ls -la data/processed/

# Or specify custom path
reviewer-agent --dataset-path /path/to/dataset.csv
```

#### 6. No Output Artifacts Generated

**Solution:**
```bash
# Check output directory permissions
ls -la logs/reviewer_agent/

# Or specify custom writable directory
reviewer-agent --output-root /tmp/test_output
```

---

## Configuration Precedence

Configurations are applied in this order (highest to lowest priority):

1. **Command-line flags** (`--flag-name`)
2. **Environment variables** (`REVIEW_*`)
3. **.env file** (loaded automatically from project root)
4. **Code defaults** (defined in `src/config.py`)

**Example:** If `--llm-timeout 60` is set via CLI, it overrides `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS=600` from `.env`.

---

## Advanced Configuration

### Custom Model Selection

```bash
# Use different registered model keys for planning vs. execution
export REVIEW_REVIEWER_PLANNER_MODEL_KEY=<registered-planner-model-key>
export REVIEW_REVIEWER_WORKER_MODEL_KEY=<registered-worker-model-key>

reviewer-agent --limit 5
```

### Community Detection Tuning

```bash
# Stricter community splitting
export REVIEW_COMMUNITY_MAX_FRACTION=0.15
export REVIEW_COMMUNITY_MIN_SPLIT_SIZE=20
export REVIEW_COMMUNITY_MAX_FILES=100

reviewer-agent --trace
```

### AST Cache Management

```bash
# Force re-parsing by incrementing parser version
export REVIEW_AST_PARSER_VERSION=v2
export REVIEW_AST_CACHE_TTL_SECONDS=86400  # 24 hours

reviewer-agent
```

---

## Additional Resources

- **Configuration Source:** [src/config.py](src/config.py)
- **CLI Entry Point:** [src/reviewer_agent/main.py](src/reviewer_agent/main.py)
- **Main Baseline:** [src/main.py](src/main.py)
- **Project Documentation:** [readme.md](readme.md)
- **Reviewer Graph:** [orchestration.md](orchestration.md)

---

**Last Updated:** 2026-06-24
**Version:** 1.1

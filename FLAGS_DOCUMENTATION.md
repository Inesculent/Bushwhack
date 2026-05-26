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

Bushwhack provides three main entry points for running research:

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
- `python scripts/cli.py --review` - Git-staged changes review

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

#### Run Management

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--run-id` | string | `None` | Optional run identifier; a short UUID is automatically generated when omitted. Used for tracking and logging. |
| `--limit` | integer | `None` | Optional cap on the number of unique PRs to process. If combined with `--pr-url`, the exact PR is processed first, then remaining up to limit. |
| `--output-root` | path | `None` | Override `reviewer_agent_output_dir` from settings. Specifies where artifacts are written (logs, manifests, findings). |
| `--repo-root` | path | `None` | Optional local repository root for direct context smoke runs. Enables testing against a local repository instead of cloned repos. |

#### Debugging & Tracing

| Flag | Type | Default | Description |
|------|------|---------|-------------|
| `--trace` | boolean | `False` | Emit reviewer graph tracing logs for planning, worker dispatch, and synthesis. Verbose output for debugging agent behavior. |
| `--basic-graph` | boolean | `False` | Use the basic reviewer graph without adversarial critique/reflection nodes. |

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

# Single PR analysis
reviewer-agent --pr-url "https://github.com/infiniflow/ragflow/pull/6553"

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
| `REVIEW_GITHUB_MCP_DOC_PATHS` | list | common README/CONTRIBUTING/SECURITY/CHANGELOG/docs paths | Ordered documentation paths attempted for docs pre-brief and focused-context fallback. |
| `REVIEW_DOCS_PREBRIEF_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used to summarize the documentation/PR pre-brief. Must match a key in `infrastructure.llm.factory.MODELS`. |

---

### Redis Configuration

#### Connection & Checkpointing

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REDIS_ENABLED` | boolean | `true` | Enable Redis-backed LangGraph checkpointing. Disable for local testing without Redis. |
| `REVIEW_REDIS_URL` | string | `redis://localhost:6379/0` | Redis connection URL used for LangGraph checkpointing. Format: `redis://[user:password@]host:port/db` |
| `REVIEW_REDIS_NAMESPACE` | string | `langgraph` | Namespace prefix for Redis checkpoint keys. Prevents key collisions in shared instances. |
| `REVIEW_REDIS_TTL_SECONDS` | integer | `3600` | TTL for Redis checkpoint entries. In seconds. |

#### Example Docker Compose Setup

```bash
docker-compose -f docker-compose.redis.yml up -d
export REVIEW_REDIS_URL=redis://localhost:6379/0
export REVIEW_REDIS_ENABLED=true
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

#### Local LLM Configuration (Ollama/LM Studio/vLLM)

**Hotswap default model:** edit `src/infrastructure/llm/defaults.py` (`DEFAULT_LOCAL_MODEL_KEY` and `DEFAULT_LOCAL_MODEL_PATH`). That registers the vLLM model id and sets defaults for all `REVIEW_*_MODEL_KEY` settings unless overridden in `.env`.

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_LOCAL_LLM_BASE_URL` | string | `http://localhost:8000/v1` | OpenAI-compatible base URL for local models (Qwen via Ollama, LM Studio, vLLM). |
| `REVIEW_LOCAL_LLM_API_KEY` | string | `local` | API key placeholder for OpenAI-compatible local model servers. |
| `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS` | integer | `180` | Request timeout for OpenAI-compatible local model servers. Range: 1-600. |
| `REVIEW_LOCAL_LLM_MAX_RETRIES` | integer | `0` | Retry count for OpenAI-compatible local model requests. Range: 0-10. |

---

### Reviewer Agent Configuration

#### Model Selection

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REVIEWER_PLANNER_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used by the reviewer planner. Must match a key in `infrastructure.llm.factory.MODELS`. For Ollama: use the corresponding local model key. |
| `REVIEW_REVIEWER_WORKER_MODEL_KEY` | string | `qwen3.5-122b` (see `infrastructure.llm.defaults`) | Model key used by reviewer workers, critiquer, reflection, and revision nodes. For Ollama: use the corresponding local model key. |

#### Agent Behavior

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_REVIEWER_USE_LEGACY_SPECIALIST_WORKERS` | boolean | `false` | Route review_planner tasks to legacy specialist workers instead of adversarial critiquer loop. |

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

### Solo Agent Configuration

| Variable | Type | Default | Description |
|----------|------|---------|-------------|
| `REVIEW_SOLO_AGENT_MAX_DIFF_CHARS` | integer | `60000` | Maximum characters of unified diff inlined into the solo-agent prompt. |
| `REVIEW_SOLO_AGENT_MODEL_KEY` | string | `gpt-5.4` | Model key used by the solo-agent worker. |
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

#### 4. **Performance Tuning (Fast Iteration)**

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

#### 5. **Cloud LLM with OpenAI**

```bash
# .env file
REVIEW_OPENAI_API_KEY=sk-...
REVIEW_REVIEWER_PLANNER_MODEL_KEY=gpt-4-turbo
REVIEW_REVIEWER_WORKER_MODEL_KEY=gpt-4-turbo
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

### Example 3: Smoke Test with Local Repo

```bash
# Test on a local repository with first 3 PRs worth of data
reviewer-agent \
  --repo-root /path/to/local/repo \
  --limit 3 \
  --trace
```

### Example 4: Baseline Analysis

```bash
# Run simple baseline model without graph orchestration
python -m src.main \
  --repo-path /path/to/repo \
  --diff-file changes.diff \
  --user-goals "Performance review: identify N+1 queries and inefficient database operations"
```

### Example 5: Git Pre-Commit Integration

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

**Example:** If `--llm-timeout 60` is set via CLI, it overrides `REVIEW_LOCAL_LLM_TIMEOUT_SECONDS=180` from `.env`.

---

## Advanced Configuration

### Custom Model Selection

```bash
# Use different models for planning vs. execution
export REVIEW_REVIEWER_PLANNER_MODEL_KEY=gpt-4
export REVIEW_REVIEWER_WORKER_MODEL_KEY=gpt-3.5-turbo

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
- **Architecture Guide:** [agents.md](agents.md)

---

**Last Updated:** 2026-05-07
**Version:** 1.0

# Bushwhack Code Review Orchestrator

Autonomous code review orchestration with LangGraph, plus research pipelines for benchmark evaluation.

The codebase follows Hexagonal Architecture (Ports and Adapters): domain schemas/interfaces in the core, orchestration graphs in the middle, and infrastructure adapters on the edge.

## What Is Implemented Today

- Baseline context graph that summarizes diffs and builds a structural graph from the repository.
- Reviewer graphs that plan review tasks and route them to specialist worker nodes, with an optional critique/reflection loop.
- AST parsing via native Tree-sitter or a local MCP server, with caching.
- Optional Redis checkpointing for LangGraph runs.
- Research pipelines for SWE-PRBench and AACR-Bench dataset processing and evaluation.
- A stub HTTP API and CLI for future review submission (not yet wired to the graphs).

## Architecture Summary

### 1) Domain Layer (`src/domain`)

Pure core contracts and schemas:

- `GraphState` and reducer-safe state composition.
- Pydantic schemas for tasks, findings, repository map, and insights.
- Abstract interfaces (ports) for search, AST parsing, cache, and LLM services.

No direct infrastructure dependencies should live here.

### 2) Orchestration Layer (`src/orchestration`)

LangGraph orchestration logic:

- `context_graph.py` runs `explorer` and `structural_extractor` for baseline context building.
- `reviewer_graph.py` runs a full planner/worker/synthesizer flow with critique + reflection nodes.
- `reviewer_graph_basic.py` runs a simpler parallel reviewer flow without critique/reflection.

This layer uses dependency injection and interfaces from `src/domain`, not direct infrastructure clients.

### 3) Infrastructure Layer (`src/infrastructure`)

Adapter implementations for external systems:

- Search adapter (`ripgrep`) for fast local search.
- AST parsers: native Tree-sitter in-process or MCP-backed AST server.
- Cache adapter (in-memory; Redis cache stubs exist but are not wired).
- Redis checkpointing via LangGraph in the orchestration entrypoints.
- HTTP gateway stub and sandbox utilities.

## Current Repository Layout

```text
.
├── data/
├── logs/
├── docker_mcp/
│   ├── fs-mcp/
│   └── github-mcp/
├── plots/
├── scripts/
│   ├── cli.py
│   └── review.bat
├── src/
│   ├── config.py
│   ├── main.py
│   ├── benchmark.py
│   ├── data/
│   ├── domain/
│   ├── infrastructure/
│   ├── orchestration/
│   ├── reviewer_agent/
│   └── solo_agent/
├── conftest.py
├── pytest.ini
├── requirements.txt
└── readme.md
```

## Program Usage

## 1) Environment Setup

Use Python 3.12+.

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

## 2) Configure Environment Variables

Create a local `.env` file at repository root. Settings are loaded from `src/config.py` using prefix `REVIEW_`.
See `FLAGS_DOCUMENTATION.md` for the full list.

Useful settings to get started:

- `REVIEW_AST_ENABLED=true`
- `REVIEW_AST_MCP_ENABLED=false`
- `REVIEW_AST_MCP_COMMAND=python`
- `REVIEW_AST_MCP_ARGS=["docker_mcp/fs-mcp/server.py"]`
- `REVIEW_REDIS_ENABLED=true`
- `REVIEW_LOCAL_LLM_BASE_URL=http://localhost:8000/v1`
- `REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN=...` (for dataset enrichment)
- `REVIEW_LANGSMITH_TRACING=true` and `REVIEW_LANGSMITH_API_KEY=...` to send LangGraph/LangChain traces to LangSmith.

## 2.1) Run Redis For LangGraph Checkpointing (Optional)

**Local profile (`--local`, default):** start Redis from repo root:

```powershell
docker compose -f docker-compose.redis.yml up -d
```

**Cluster profile (`--remote`):** use loopback Redis in the Slurm job — see [documentation/apptainer_cluster_guide.md](documentation/apptainer_cluster_guide.md).

Stop Redis:

```powershell
docker compose -f docker-compose.redis.yml down
```

This Redis container is separate from MCP Dockerfiles in `docker_mcp/fs-mcp` and `docker_mcp/github-mcp`.
Those Dockerfiles are for MCP server processes, while this compose service is only for shared state/checkpoint storage.

If Redis is unavailable, set `REVIEW_REDIS_ENABLED=false` to run without checkpointing.

Recommended `.env` values:

- `REVIEW_REDIS_ENABLED=true`
- `REVIEW_REDIS_URL=redis://localhost:6379/0`
- `REVIEW_REDIS_NAMESPACE=langgraph`
- `REVIEW_REDIS_TTL_SECONDS=3600`

## 3) Run the Baseline Context Graph

This runs the baseline explorer + structural extractor graph.

```powershell
python -m src.main --repo-path . --diff-file path\to\diff.txt
```

## 4) Run the Reviewer Graphs (Dataset Harness)

Parallel reviewer graph over the processed AACR dataset:

```powershell
python -m src.reviewer_agent.main --dataset aacr
```

Use `--local` (Docker sandbox, compose Redis, LLM via SSH port-forward) or `--remote` (Apptainer on Slurm). See [documentation/apptainer_cluster_guide.md](documentation/apptainer_cluster_guide.md).

Useful flags:

- `--basic-graph` to skip critique/reflection nodes.
- `--trace` to emit review-trace logs, including bounded LLM I/O summaries and per-call token usage.
- `--limit 10` for smoke runs.

LangSmith tracing is separate from `--trace`: set `REVIEW_LANGSMITH_TRACING=true`, `REVIEW_LANGSMITH_API_KEY`, and optionally `REVIEW_LANGSMITH_PROJECT` in `.env`. Local OpenAI-compatible models are still created with `langchain-openai`; the factory adds LangSmith metadata so local model IDs show clearly in traces. `REVIEW_LANGSMITH_HIDE_OUTPUTS` defaults to `true` because full LangGraph states can exceed LangSmith upload limits.

## 5) Run the Solo Agent (Dataset Harness)

```powershell
python -m src.solo_agent.main --dataset aacr
```

## 6) Run the API Gateway (Stub)

Start FastAPI (placeholder endpoint only):

```powershell
python -m uvicorn src.infrastructure.http.app:app --host 127.0.0.1 --port 8000 --reload
```

Current endpoint:

- `POST /review`

Note: The handler is a stub that prints the payload and returns `{"status": "approved"}`. It does not invoke the LangGraph flows yet.

## 7) Trigger a Review from CLI (Stub)

The CLI sends staged git diff content to the API stub.

```powershell
python scripts/cli.py --review
```

Expected behavior:

- If no staged changes exist: exits with "No staged changes found".
- If API responds with approved status: prints "Code review approved".
- Otherwise: prints "Code review failed".

## 8) Run Tests

```powershell
pytest -q
```

You can also scope to infrastructure tests:

```powershell
pytest src/infrastructure/tests -q
```

## 9) Run Research Dataset Pipeline

The repository now includes a modular two-phase dataset pipeline for:

- `foundry-ai/swe-prbench` (PR-level macro evaluation)
- `Alibaba-Aone/aacr-bench` (comment-level micro evaluation + GitHub enrichment)

Run from repo root:

```powershell
python -m src.data.run_research_pipeline
```

Set a GitHub PAT in `.env` before running AACR enrichment:

- `GITHUB_PERSONAL_ACCESS_TOKEN=...`

(`REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN` is also supported.)

Optional flags:

- `--target-languages Python`
- `--skip-plots`
- `--no-raw-dump`

Outputs are generated automatically:

- `data/raw/`
- `data/processed/swe_prbench_graph_ready.csv`
- `data/processed/aacr_bench_graph_ready.csv`
- `plots/dataset_composition/`
- `plots/metric_distributions/`
- `logs/research_pipeline.log`

Processed CSVs now also include `repo_size_kb` (GitHub repository size in KB) for additional scaling analyses.

Repository structure complexity metrics are also included:

- `repo_total_files`
- `repo_python_files`
- `repo_total_directories`
- `repo_max_directory_depth`

## Development Notes

- `reference.md` contains the full architecture and feature plan.
- `structure.txt` captures the intended structure baseline.
- `orchestration.md` and `status.md` track ongoing work and research checkpoints.


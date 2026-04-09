# Verily Code Review Orchestrator

Autonomous multi-agent code review orchestrator for repository-wide, context-aware analysis.

This project is designed around a LangGraph-driven workflow and Hexagonal Architecture (Ports and Adapters), with strict separation between domain logic, orchestration logic, and infrastructure adapters.

## What This Project Does

- Builds repository context from code changes.
- Plans specialized review tasks dynamically.
- Executes specialist reviewers in parallel.
- Synthesizes findings into a final review payload.

The target pattern is Plan-and-Solve + Reflexion, where context building, planning, execution, and synthesis are separated into explicit graph nodes.

## Architecture Summary

### 1) Domain Layer (`src/domain`)

Pure core contracts and schemas:

- `GraphState` and reducer-safe state composition.
- Pydantic schemas for tasks, findings, repository map, and insights.
- Abstract interfaces (ports) for search, AST parsing, cache, and LLM services.

No direct infrastructure dependencies should live here.

### 2) Orchestration Layer (`src/orchestration`)

LangGraph orchestration logic:

- `explorer` node: gathers context for changed code.
- `planner` node: generates structured review tasks.
- `worker` node(s): performs specialized reviews.
- `synthesizer` node: deduplicates, validates, and formats findings.

This layer should use dependency injection and interfaces from `src/domain`, not direct infrastructure clients.

### 3) Infrastructure Layer (`src/infrastructure`)

Adapter implementations for external systems:

- Search adapter (`ripgrep`) for fast local search.
- MCP client + AST parser adapter.
- Cache adapters (memory/Redis-style interface).
- HTTP gateway and sandbox integrations.

## Current Repository Layout

```text
.
├── mcp/
│   ├── fs-mcp/
│   └── github-mcp/
├── scripts/
│   ├── cli.py
│   └── review.bat
├── src/
│   ├── benchmark.py
│   ├── config.py
│   ├── main.py
│   ├── domain/
│   ├── infrastructure/
│   └── orchestration/
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

Useful settings:

- `REVIEW_AST_MCP_ENABLED=false`
- `REVIEW_AST_MCP_COMMAND=python`
- `REVIEW_AST_MCP_ARGS=["mcp/fs-mcp/server.py"]`
- `REVIEW_AST_MCP_TIMEOUT_SECONDS=30`

## 3) Run the API Gateway

Start FastAPI:

```powershell
python -m uvicorn src.infrastructure.http.app:app --host 127.0.0.1 --port 8000 --reload
```

Current endpoint:

- `POST /review`

## 4) Trigger a Review from CLI

The CLI sends staged git diff content to the API.

```powershell
python scripts/cli.py --review
```

Expected behavior:

- If no staged changes exist: exits with "No staged changes found".
- If API responds with approved status: prints "Code review approved".
- Otherwise: prints "Code review failed".

## 5) Run Tests

```powershell
pytest -q
```

You can also scope to infrastructure tests:

```powershell
pytest src/infrastructure/tests -q
```

## Development Notes

- `reference.md` contains the full architecture and feature plan.
- `structure.txt` captures the intended structure baseline.
- Several orchestration files are currently scaffolded and are expected to be filled as implementation progresses.

## New Repository Initialization (When You Are Ready)

If this folder is not initialized yet and you want a clean repo later:

```powershell
git init
git add .
git status --short
```

Before first commit, confirm local-only files are excluded by `.gitignore` (virtual env, `.env`, caches, and planning docs).

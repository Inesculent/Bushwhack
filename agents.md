# AGENTS.md - Developer & AI Contributor Guidelines

**Maintainers:** Verily
**Project:** Autonomous Multi-Agent Code Review Orchestrator (Research & Production)

## 1. The Prime Directive (For AI Coding Assistants)
If you are an AI coding assistant (Cursor, Copilot, etc.) reading this file: **Do not hallucinate imports, and do not break architectural boundaries.** This is a production-grade research repository built on strict Hexagonal Architecture (Ports and Adapters). We prioritize clean, observable, and modular code over quick hacks. 

Before generating any code, review the structural boundaries in Section 2. 

## 2. Repository Structure & Strict Boundaries
Code must be strictly segregated to decouple the LangGraph orchestration from the physical infrastructure (Redis, MCP, HTTP).

* **`src/domain/` (The Inner Core)**
    * **What goes here:** Pydantic schemas, `GraphState` definitions, and abstract interfaces (ABCs / Ports).
    * **Strict Rule:** **ZERO external infrastructure dependencies.** You may not import `redis`, `fastapi`, `mcp`, or `langchain` tools here. This layer must remain pure Python.
* **`src/orchestration/` (The Brain)**
    * **What goes here:** LangGraph StateGraph compilation, edge routing, and the Agent Nodes (`explorer.py`, `planner.py`, `specialists.py`).
    * **Strict Rule:** Nodes must *never* instantiate infrastructure clients directly. All tools (AST parsers, search clients) must be injected via Dependency Injection using the interfaces defined in `domain/`.
* **`src/infrastructure/` (The Adapters)**
    * **What goes here:** The actual implementations of the domain interfaces. Redis clients, MCP Server subprocess managers, FastAPI routing, and LangChain LLM wrappers.

## 3. LangGraph State Management
The `GraphState` is the single source of truth. When modifying or adding state variables:
1.  **Immutability:** Treat the state as immutable where possible. 
2.  **Reducers:** Use `typing.Annotated` with LangGraph reducers (e.g., `operator.add`) for any lists or dictionaries that multiple agents will write to concurrently.
3.  **Check-pointing:** The system uses Redis for LangGraph check-pointing. Do not write local SQLite checkpoint logic unless inside a specific `tests/` mock.

## 4. Building New Agent Nodes
When adding a new specialized reviewer node (e.g., a `performance_reviewer.py`), follow this pattern:
1.  **Input:** The node function must accept the current `GraphState`.
2.  **Schema Enforcement:** The node must enforce a strict Pydantic structured output (`with_structured_output`) on its LLM call to guarantee the JSON shape of the review findings.
3.  **Output:** The node must return a dictionary containing *only* the specific keys in the `GraphState` it is updating. Do not return the entire state.

## 5. Tooling & MCP Integration
We utilize a hybrid tooling approach to maximize performance and avoid unnecessary abstraction.
* **Native Tools:** For stateless, simple local execution (e.g., `ripgrep`, standard filesystem reads), write standard Python functions wrapped with the `@tool` decorator.
* **MCP Servers:** Reserve the Model Context Protocol strictly for heavy, stateful context retrieval (e.g., `mcp-server-tree-sitter` for ASTs, `lsp-mcp` for definition tracing). 
* **Caching is Mandatory:** Any tool that queries an MCP server (especially Tree-sitter) must first check the Redis context cache using the `ICacheService` interface to prevent redundant heavy parsing.

## 6. Coding Standards & Observability
* **Typing:** Strict Python 3.12+ type hinting is absolutely mandatory across the entire codebase. Use `mypy` to validate.
* **Configuration:** Never use `os.environ.get()` inside business logic. All environment variables must be declared and validated inside `src/config.py` using `pydantic-settings`.
* **Logging:** The use of `print()` is strictly forbidden. You must use `structlog`. Every log emission inside an agent node must be bound with the `run_id` and the `node_name` (e.g., `logger.bind(run_id=state["run_id"], node="Execution Planner")`).
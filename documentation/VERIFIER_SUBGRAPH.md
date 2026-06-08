# Self-healing verifier subgraph

This document summarizes the **external verifier** work: runtime proof attempts in Docker, how they connect to the adversarial reviewer graph, configuration, artifacts, and how to build the test image.

## Goals

- Run **optional** Python probes in an isolated container (repo mounted read-only at `/repo`) for candidates that need stronger evidence after reflection.
- Keep verifier output **advisory**: promotion rules stay driven by reflection + critique revision; verifier results inform digests and lifecycle metadata, not hard drops on `refuted`.
- **Defect and security runs by default** when the verifier is enabled: `REVIEW_VERIFIER_ENABLED` defaults on; `REVIEW_VERIFIER_RUN_ON_SECURITY` defaults on so `security_risk` candidates (e.g. ReDoS) are eligible for sandbox probes. Disable per environment if you want diff-only reviews.

## Reflection routing

- Use verdict **`needs_verification`** (see reflection prompts) when runtime Python proof is needed; the graph routes those candidates toward the verifier path instead of only Graph-RAG `text_queries`.

## What changed (high level)

| Area | Change |
|------|--------|
| **Schemas** | [`src/domain/verifier_schemas.py`](../src/domain/verifier_schemas.py) — `VerifierReport`, `VerifierAttemptRecord`, `VerifierTestGeneratorOutput`. |
| **Graph state** | [`src/domain/state.py`](../src/domain/state.py) — `verifier_candidate` (Send payload), `verifier_reports` (list reducer). |
| **Sandbox** | [`src/infrastructure/sandbox.py`](../src/infrastructure/sandbox.py) — `SandboxExecResult`, `execute_result()` (exit code + demuxed stdout/stderr), `write_file_in_container()` for `/tmp` scripts. |
| **Verifier runtime** | [`src/orchestration/nodes/verifier/`](../src/orchestration/nodes/verifier/) — LLM test generation, Docker execution with timeout, deterministic judgment + retry loop (max attempts from settings). |
| **Orchestration** | [`src/orchestration/verifier_graph.py`](../src/orchestration/verifier_graph.py) — `run_verifier_invocation()` helper; `build_verifier_graph()` reserved. |
| **Routing** | [`src/orchestration/routing/verifier_fanout.py`](../src/orchestration/routing/verifier_fanout.py) — eligibility, `Send` fan-out, `make_verifier_subgraph_node`. [`src/orchestration/routing/adversarial_after_reflection.py`](../src/orchestration/routing/adversarial_after_reflection.py) — `route_focused_after_reflection` (second focused fetch vs `post_reflection_evidence_pass` vs cleanup). |
| **Reviewer graph** | [`src/orchestration/reviewer_graph.py`](../src/orchestration/reviewer_graph.py) — `post_reflection_evidence_pass` no-op bridge, `focused_context` and bridge both feed `_route_after_focused_context` (verifier then critique revision). |
| **Critique revision** | [`src/orchestration/nodes/application/critique_revision.py`](../src/orchestration/nodes/application/critique_revision.py) — optional “Runtime verifier (advisory)” sections in digest/reduce prompts. |
| **Cleanup** | [`src/orchestration/nodes/application/cleanup.py`](../src/orchestration/nodes/application/cleanup.py) — `verifier_advisory` on promoted candidate lifecycle when `metadata.verifier_hints` exists. |
| **Config** | [`src/config.py`](../src/config.py) — `REVIEW_VERIFIER_*` (verifier on by default; security-risk runs on by default; image, timeouts, `verifier_require_focused_evidence`, Docker skip, PR budget). |
| **Prompts** | [`src/orchestration/prompts/reviewer/verifier/`](../src/orchestration/prompts/reviewer/verifier/) — test generator + retry hint templates. |
| **Docker** | [`Dockerfile.verifier`](../Dockerfile.verifier), [`scripts/build_verifier_image.sh`](../scripts/build_verifier_image.sh), [`scripts/build_verifier_image.ps1`](../scripts/build_verifier_image.ps1). |
| **AACR logs** | [`src/reviewer_agent/harness/aacr.py`](../src/reviewer_agent/harness/aacr.py) — raw JSON includes `verifier_reports`. |
| **Tests** | [`src/orchestration/tests/test_verifier_integration.py`](../src/orchestration/tests/test_verifier_integration.py); [`src/infrastructure/tests/test_verifier_sandbox_integration.py`](../src/infrastructure/tests/test_verifier_sandbox_integration.py) (opt-in Docker smoke, `VERIFIER_SANDBOX_INTEGRATION=1`). |

## Reviewer graph flow (adversarial path)

The verifier sits **after** focused context gathering and **before** critique revision digest/reduce when enabled and eligible. A **post-reflection bridge** ensures verifier/critique routing still runs when reflectors return `needs_context` **without** embedding a `focused_request`.

```mermaid
flowchart TB
  subgraph critLoop [Adversarial critiquer loop]
    GC[general_critiquer]
    IFC[initial_focused_context]
    AR[adversarial_reflection]
    FC[focused_context]
    PEP[post_reflection_evidence_pass]
    VS[verifier_subgraph]
    CRD[critique_revision_digest]
    CRR[critique_revision_reduce]
    RA[review_adjudicator]
    GC --> IFC --> AR
    AR -->|embedded_focus_request| FC
    AR -->|needs_revision_ids| PEP
    AR -->|no_revision_work| RA
    FC --> RVC[_route_after_focused_context]
    PEP --> RVC
    RVC -->|Send_per_candidate| VS
    RVC -->|digest_shards| CRD
    RVC -->|no_work| RA
    VS --> RVC2[_route_critique_revision]
    RVC2 --> CRD
    RVC2 --> RA
    CRD --> CRR --> RA
  end
```

Notes:

- `_route_after_focused_context` tries **verifier** `Send` branches first; if none, it delegates to existing **critique revision** routing (`_route_critique_revision`).
- `verifier_subgraph` merges parallel branches into `verifier_reports` and updates `metadata.verifier` / `metadata.verifier_hints`.

## Verifier single-candidate loop (internal)

Retries are **inside** one invocation per candidate (not LangGraph edges in the parent graph).

```mermaid
flowchart LR
  START([invoke_verifier_for_candidate])
  GEN[test_generator LLM]
  EXE[sandbox_executor]
  JUD[result_judge]
  FIN([VerifierReport])
  START --> GEN --> EXE --> JUD
  JUD -->|inconclusive_and_attempts_left| GEN
  JUD -->|verified_or_refuted_or_cap| FIN
```

## Verifier environment prep

The verifier environment prep is not a one-shot agent. It is a deterministic, observable setup loop inside the sandbox executor. Its job is to prepare enough Python runtime state for the current verifier target, record what worked, and leave final evidence decisions to the verifier judge and critique-revision policy.

The loop is intentionally advisory:

- A usable environment can support a runtime verifier attempt.
- A failed environment, missing import, timeout, syntax error, harness error, or inconclusive probe does not accept or reject a candidate.
- Only clean product behavior signals, such as a verifier script reaching `STATUS: MISMATCH` or `STATUS: CRASHED` without harness contamination, can influence the runtime advisory section.

### Structure

```mermaid
flowchart TB
  START([execute_test_script])
  SANDBOX[start or clone sandbox]
  FINGERPRINT[hash dependency file fingerprints]
  REUSE{venv already usable?}
  CREATE[create .verifier_venv_<fingerprint>]
  PROBE_PY[probe python executable]
  TARGETS[derive verifier target files]
  PROBE_IMPORTS[probe target module imports]
  RECORD[record env_metadata]
  RUN[run verifier script with prepared python if usable]

  START --> SANDBOX --> FINGERPRINT --> REUSE
  REUSE -->|yes| TARGETS
  REUSE -->|no| CREATE --> PROBE_PY --> TARGETS
  TARGETS --> PROBE_IMPORTS --> RECORD --> RUN
```

### Inputs

The prep loop receives:

- `workdir`: the sandbox execution directory, usually `/repo` or `/exec_*`.
- Dependency fingerprints from `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `setup.py`, and `setup.cfg`.
- Target files from `graph_state["verifier_candidate"]`, using `file_path`, `file_paths`, and `target_files` when present.

Target files are converted to importable module names when possible:

- `pkg/mod.py` -> `pkg.mod`
- `pkg/__init__.py` -> `pkg`
- Non-Python paths or paths with non-importable segments are skipped.

### What The Loop Does

1. Compute a dependency-file fingerprint.
2. Reuse `.verifier_venv_<fingerprint>` if its Python executable already works.
3. Otherwise create the venv and probe `python -c "import sys"`.
4. Probe only the target modules needed by the current verifier candidate.
5. Record missing modules from those target import probes.
6. Run the generated verifier script with the prepared `python_path` only if the venv is usable; otherwise fall back to `python`.

This is deliberately narrower than "install the whole repo." In theory, the verifier only needs dependencies reachable from the review target and test harness. Broad dependency installation is avoided because it is expensive, network-sensitive, and can make unrelated repo dependencies look like verifier blockers.

### Metadata Contract

Each `VerifierAttemptRecord` can carry `env_metadata`. `verifier_finalize_node` copies the latest attempt's environment summary into `metadata.verifier_env[candidate_id]`.

Important fields:

| Field | Meaning |
|-------|---------|
| `status` | `usable`, `failed`, or `disabled`. |
| `fingerprint` | Hash of dependency-file contents used to name/reuse the venv. |
| `venv_dir` | Sandbox path for `.verifier_venv_<fingerprint>`. |
| `python_path` | Python executable used for verifier scripts when `status == "usable"`. |
| `reused` | Whether an existing prepared venv was reused. |
| `target_files` | Candidate-scoped files used for import probes. |
| `target_import_probes` | One record per importable target module, including status, exit code, stdout/stderr, and missing modules. |
| `missing_modules` | Union of missing modules from target import probes. |
| `install_attempts` | Reserved for targeted dependency installation attempts. It should stay empty unless a future targeted installer is added. |
| `dependency_install_policy` | Currently `targeted_only`; broad `pip install -r requirements*.txt` is intentionally not part of normal prep. |
| `failure_reason` | Setup failure class such as `venv_create_failed` or `python_probe_failed`. |

### Dependency Policy

The current policy is `targeted_only`.

That means:

- The prep loop identifies missing imports by probing the candidate target module(s).
- It records missing modules instead of treating them as product behavior.
- It does not install every requirements file as a default recovery step.
- If targeted installation is added later, it should map a missing target import to the narrowest dependency candidate and record each attempt in `install_attempts`.

This keeps runtime verification useful when clean, but prevents environment setup from becoming a second repository-specific reviewer or a noisy source of false negatives.

## Configuration quick reference

| Env / setting | Role |
|---------------|------|
| `REVIEW_VERIFIER_ENABLED` | Master switch (default **on**; set `false` to skip). |
| `REVIEW_VERIFIER_IMAGE` | Image tag (default `verifier-test-env:latest`). |
| `REVIEW_VERIFIER_RUN_ON_DEFECT` / `_SECURITY` / `_PERFORMANCE` | Claim-type gates (`_SECURITY` defaults **on**). |
| `REVIEW_VERIFIER_REQUIRE_FOCUSED_EVIDENCE` | If `true`, require focused snippets/hits for that candidate; if `false`, allow diff + candidate JSON only. |
| `REVIEW_VERIFIER_SKIP_IF_NO_DOCKER` | Skip verifier when Docker is unreachable. |
| `REVIEW_VERIFIER_PREPARE_ENV_ENABLED` | Enable best-effort venv prep and target import probes. |
| `REVIEW_VERIFIER_PREPARE_ENV_INSTALL_DEPS` | Reserved for future targeted dependency installation; broad requirements installation is intentionally avoided. |

## Building the verifier image (manual)

From repository root:

```bash
# Linux / macOS
./scripts/build_verifier_image.sh
```

```powershell
# Windows
.\scripts\build_verifier_image.ps1
```

The default main `RepoSandbox` image (`agent-fs-sandbox`) is **not** built by this repo; only the verifier image is defined by `Dockerfile.verifier`.

## Opt-in integration test

```bash
VERIFIER_SANDBOX_INTEGRATION=1 pytest src/infrastructure/tests/test_verifier_sandbox_integration.py -m integration
```

Requires Docker and a locally built (or pullable) verifier image matching `REVIEW_VERIFIER_IMAGE`.

# Test generator (self-healing verifier)

Generate a **standalone** Python script that tries to reproduce or refute the candidate finding using the repository at `/repo` on `sys.path`.

## Candidate (JSON)

{candidate_json}

## Focused context snippets

{focused_context_snippets}

## Git diff excerpt (truncated)

{git_diff_excerpt}

## Retry feedback (may be empty)

{retry_feedback}

## Requirements

1. **Heavy-dependency prelude (optional)** — when mock_heavy_deps is **{mock_heavy_deps_label}**, you may start with this **generic** pattern (torch/PIL only — no framework-specific typing stubs):

```python
__HEAVY_DEP_PRELUDE__
```

When mock_heavy_deps is **disabled**, do not mock torch/PIL unless an import actually fails.

2. **Repo-agnostic imports (required)**
   - Use the repo root hint `{repo_root}` when adding to `sys.path`. Do **not** hardcode `/repo`.
   - Before calling target code: use `inspect.signature` and/or read `INPUT_TYPES` (or equivalent) from the **actual** module under test.
   - Import the **smallest** surface needed (one class or function), not whole package graphs when avoidable.
   - On import failure: add **minimal** `sys.modules` stubs only for names appearing in the traceback (e.g. one missing attribute on a namespace — do not copy a fixed enum list for a specific framework).

3. Import **only** what you need from the repo; avoid `pytest` / `unittest` test runners.

4. Exercise the code path around `{file_path}` near lines {line_start}-{line_end} using the **failure_mode** field as guidance.

5. **Exit protocol** (no other frameworks):
   - On success / bug not reproduced: print `STATUS: SAFE` and `sys.exit(0)`.
   - On exception/crash in product code (after target is loaded and invoked): print `STATUS: CRASHED | ExceptionType: message` and `sys.exit(1)`.
   - On wrong output without crash (data loss, wrong structured field, silent wrong value): assert expected vs actual, then print `STATUS: MISMATCH | expected=... actual=...` and `sys.exit(1)`.
   - On import/setup/syntax failures in the verifier script or before product invoke: print `STATUS: HARNESS_ERROR | message` and `sys.exit(2)`.
   - **Never** call `sys.exit(0)` when imports fail or the repository is unavailable.

   For wrong-result / data-loss claims, prefer small inline repros with **assertions** on outputs (e.g. `re.findall`, calling the changed function), not only try/except for crashes.

6. Add `{repo_root}` (or `/exec_*` workspace path when provided) to `sys.path` before importing target modules. Do not rely on symlinking empty `/workspace` to `/repo`.

7. No network, no writes outside the repo workspace and temp stdout/stderr. Complete within `{timeout_seconds}` seconds.

Return **only** the Python source code (no markdown fences, no explanation).

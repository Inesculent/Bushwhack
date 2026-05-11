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

1. **Mock-first prelude** when heavy dependencies may block imports — when mock_heavy_deps is **{mock_heavy_deps_label}**, copy this pattern at the top:

```python
import sys
from unittest.mock import MagicMock

heavy_deps = [
    "torch", "torchvision", "numpy", "pandas", "PIL", "Pillow",
    "transformers", "comfy", "comfy.comfy_types",
]
for mod in heavy_deps:
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()
```

2. Use the repo root hint `{repo_root}` when adding to `sys.path`. Do **not** hardcode `/repo`.
    - Example: set `repo_root = "{repo_root}"`, add it to `sys.path` if it exists.
    - If it does not exist, fall back to the first existing path in `"/repo"`, then `"/workspace"`.

3. Import **only** what you need from the repo; avoid `pytest` / `unittest` test runners.

4. Exercise the code path around `{file_path}` near lines `{line_start}`–`{line_end}` using the **failure_mode** field as guidance.

5. **Exit protocol** (no other frameworks):
   - On success / no exception: print `STATUS: SAFE` and `sys.exit(0)`.
   - On the bug manifesting (exception): print `STATUS: CRASHED | ExceptionType: message` and `sys.exit(1)`.

6. No network, no writes outside importing from `/repo` and temp stdout/stderr. Complete within `{timeout_seconds}` seconds.

Return **only** the Python source code (no markdown fences, no explanation).

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

1. **Mock-first prelude** when heavy dependencies may block imports — when mock_heavy_deps is **{mock_heavy_deps_label}**, copy this pattern at the top (do **not** register bare `MagicMock` as `PIL` — that breaks `PIL.PngImagePlugin` imports):

```python
import sys
import types
from unittest.mock import MagicMock

def _install_module_stub(name: str, *, is_package: bool = False) -> types.ModuleType:
    mod = types.ModuleType(name)
    if is_package:
        mod.__path__ = []
    sys.modules[name] = mod
    return mod

def _install_heavy_dep_stubs() -> None:
    for mod in (
        "torch", "torchvision", "numpy", "pandas", "transformers",
        "safetensors", "safetensors.torch",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    if "PIL" not in sys.modules:
        pil = _install_module_stub("PIL", is_package=True)
        for sub in ("Image", "ImageDraw", "ImageFont", "PngImagePlugin", "JpegImagePlugin"):
            submod = _install_module_stub(f"PIL.{{sub}}")
            setattr(pil, sub, submod)
    if "Pillow" not in sys.modules:
        sys.modules["Pillow"] = sys.modules.get("PIL") or MagicMock()

_install_heavy_dep_stubs()

# Comfy stubs: use simple namespaces (not bare MagicMock dicts) for node typing.
if "comfy" not in sys.modules:
    comfy_pkg = types.ModuleType("comfy")
    sys.modules["comfy"] = comfy_pkg
if "comfy.comfy_types" not in sys.modules:
    comfy_types = types.ModuleType("comfy.comfy_types")
    sys.modules["comfy.comfy_types"] = comfy_types
if "comfy.comfy_types.node_typing" not in sys.modules:
    node_typing = types.ModuleType("comfy.comfy_types.node_typing")
    node_typing.IO = types.SimpleNamespace(STRING="STRING", INT="INT")
    sys.modules["comfy.comfy_types.node_typing"] = node_typing
```

Never use `__import__("unittest.mock")` — always `from unittest.mock import MagicMock`.

2. Use the repo root hint `{repo_root}` when adding to `sys.path`. Do **not** hardcode `/repo`.
    - Example: set `repo_root = "{repo_root}"`, add it to `sys.path` if it exists.
    - If it does not exist, fall back to the first existing path in `"/repo"`, then `"/workspace"`.

3. Import **only** what you need from the repo; avoid `pytest` / `unittest` test runners.

4. Exercise the code path around `{file_path}` near lines `{line_start}`–`{line_end}` using the **failure_mode** field as guidance.

5. **Exit protocol** (no other frameworks):
   - On success / bug not reproduced: print `STATUS: SAFE` and `sys.exit(0)`.
   - On the bug manifesting in product code: print `STATUS: CRASHED | ExceptionType: message` and `sys.exit(1)`.
   - On import/setup/syntax failures (cannot load repo modules, invalid script): print `STATUS: HARNESS_ERROR | message` and `sys.exit(2)`.
   - **Never** call `sys.exit(0)` when imports fail or the repository is unavailable.

6. Add `{repo_root}` (or `/exec_*` workspace path when provided) to `sys.path` before importing target modules. Do not rely on symlinking empty `/workspace` to `/repo`.

7. No network, no writes outside the repo workspace and temp stdout/stderr. Complete within `{timeout_seconds}` seconds.

Return **only** the Python source code (no markdown fences, no explanation).

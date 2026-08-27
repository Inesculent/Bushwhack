"""Run generated verifier scripts inside RepoSandbox with timeout.

Verifier uses ``Settings.verifier_image`` for host-mounted checkouts and
``Settings.verifier_clone_image`` (default ``agent-fs-sandbox``, includes git) when cloning a
remote PR inside Docker. After clone at ``/repo``, it may copy to a writable ``/exec_*`` workspace
via ``create_execution_workspace()``, then runs generated scripts there. Rebuild
``verifier-test-env:latest`` from ``Dockerfile.verifier`` if you want git in the slim image too.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from src.config import Settings, get_settings
from src.domain.verifier_schemas import VerificationStatus, VerifierAttemptRecord, VerifierLintRun
from src.infrastructure.sandbox import (
    RepoSandbox,
    SandboxRuntime,
    build_repo_sandbox,
    resolve_verifier_sandbox_image,
)

logger = logging.getLogger(__name__)


def _truncate_stream(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + "\n... [truncated]"


def _collect_lint_runs(sandbox: RepoSandbox, settings: Settings) -> List[VerifierLintRun]:
    """Run Ruff/Flake8 inside the cloned repo at /repo (best-effort; missing tools are recorded)."""
    runs: List[VerifierLintRun] = []
    cap = settings.verifier_lint_output_max_chars
    if settings.verifier_ruff_enabled:
        r = sandbox.execute_result(
            [
                "ruff",
                "check",
                ".",
                "--no-cache",
                "--output-format",
                "concise",
            ],
            workdir="/repo",
        )
        runs.append(
            VerifierLintRun(
                tool="ruff",
                command="ruff check . --no-cache --output-format concise",
                exit_code=r.exit_code,
                stdout=_truncate_stream(r.stdout, cap),
                stderr=_truncate_stream(r.stderr, cap),
            )
        )
    if settings.verifier_flake8_enabled:
        r = sandbox.execute_result(
            ["python", "-m", "flake8", ".", "--count"],
            workdir="/repo",
        )
        runs.append(
            VerifierLintRun(
                tool="flake8",
                command="python -m flake8 . --count",
                exit_code=r.exit_code,
                stdout=_truncate_stream(r.stdout, cap),
                stderr=_truncate_stream(r.stderr, cap),
            )
        )
    return runs


def _safe_candidate_path_fragment(candidate_id: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9_.-]+", "_", candidate_id).strip("_")
    return (cleaned or "candidate")[:80]


def _graph_metadata(graph_state: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not isinstance(graph_state, dict):
        return {}
    raw_meta = graph_state.get("metadata")
    return raw_meta if isinstance(raw_meta, dict) else {}


def _resolve_remote_clone_target(
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """Return (repo_url, checkout_ref) for in-container clone."""
    raw = (repo_path or "").strip()
    meta = _graph_metadata(graph_state)

    repo_url = str(meta.get("review_repo_url") or "").strip()
    checkout_ref = str(meta.get("review_checkout_ref") or "").strip()
    pr_number = meta.get("pr_number")
    if pr_number is None:
        pr_number = meta.get("review_pr_number")

    if not repo_url and raw.startswith(("http://", "https://")):
        repo_url = raw

    if not checkout_ref and pr_number is not None and str(pr_number).strip():
        checkout_ref = f"pull/{pr_number}/head"

    if not checkout_ref:
        checkout_ref = "HEAD"

    return repo_url, checkout_ref


def _needs_remote_clone(
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
    settings: Settings,
) -> bool:
    raw = (repo_path or "").strip()
    if raw and Path(raw).is_dir():
        return False
    repo_url, _ = _resolve_remote_clone_target(raw, graph_state)
    return bool(settings.verifier_clone_remote_in_container and repo_url)


def _verifier_sandbox_image(
    settings: Settings,
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
) -> str:
    """Pick image or SIF: clone-capable stack for remote PR checkout, else verifier test image."""
    return resolve_verifier_sandbox_image(
        settings,
        needs_clone=_needs_remote_clone(repo_path, graph_state, settings),
    )


def _start_verifier_sandbox(
    sandbox: RepoSandbox,
    repo_path: str,
    graph_state: Optional[Dict[str, Any]],
    *,
    settings: Settings | None = None,
) -> str:
    """Mount or clone repository; optionally prepare writable exec workspace.

    Returns sandbox_mode: ``repo_mount`` | ``exec_workspace`` | ``snippet_workspace``.
    """
    settings = settings or get_settings()
    raw = (repo_path or "").strip()
    local = Path(raw)
    if raw and local.is_dir():
        sandbox.start(str(local.resolve()))
        if getattr(settings, "verifier_use_execution_workspace", False):
            sandbox.create_execution_workspace(workspace_name="verify_exec")
            return "exec_workspace"
        return "repo_mount"

    repo_url, checkout_ref = _resolve_remote_clone_target(raw, graph_state)
    should_clone = settings.verifier_clone_remote_in_container and bool(repo_url)

    if not should_clone:
        if settings.verifier_require_repo_in_container:
            raise FileNotFoundError(
                "Verifier requires a repository in-container but no review_repo_url / https "
                f"repo_path is available. repo_path={raw!r}."
            )
        sandbox.start_snippet_workspace()
        return "snippet_workspace"

    sandbox.start_from_remote_ref(repo_url=repo_url, ref=checkout_ref)
    if getattr(settings, "verifier_use_execution_workspace", False):
        sandbox.create_execution_workspace(workspace_name="verify_exec")
        return "exec_workspace"
    return "repo_mount"


def _repo_dependency_fingerprint(sandbox: RepoSandbox, workdir: str) -> str:
    files = ("requirements.txt", "requirements-dev.txt", "pyproject.toml", "setup.py", "setup.cfg")
    parts: List[str] = []
    for name in files:
        result = sandbox.execute_result(["sh", "-lc", f"test -f {name} && sha1sum {name} || true"], workdir=workdir)
        if result.stdout.strip():
            parts.append(result.stdout.strip())
    raw = "\n".join(parts) or workdir
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


_MISSING_MODULE_RE = re.compile(r"No module named ['\"]([^'\"]+)['\"]")
_TYPING_EXTENSIONS_SHIM = '''"""Verifier compatibility shim for minimal runtime imports."""
from __future__ import annotations

import typing as _typing

for _name in dir(_typing):
    if not _name.startswith("_"):
        globals()[_name] = getattr(_typing, _name)


class _Fallback:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, target=None, *args, **kwargs):
        if target is None:
            return lambda value: value
        return target

    def __getitem__(self, item):
        return self


def _identity_decorator(*args, **kwargs):
    if args and callable(args[0]) and len(args) == 1 and not kwargs:
        return args[0]
    return lambda value: value


def __getattr__(name):
    if name in {"override", "deprecated", "dataclass_transform"}:
        return _identity_decorator
    return getattr(_typing, name, _Fallback())


__all__ = [name for name in globals() if not name.startswith("_")]
'''
_LIGHTWEIGHT_COMPAT_SHIMS = {"typing_extensions": _TYPING_EXTENSIONS_SHIM}


def _module_name_for_path(file_path: str) -> str:
    path = file_path.strip().replace("\\", "/").lstrip("/")
    if not path.endswith(".py"):
        return ""
    parts = [part for part in path.removesuffix(".py").split("/") if part]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    if not parts or not all(re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", part) for part in parts):
        return ""
    return ".".join(parts)


def _target_files_from_graph_state(graph_state: Optional[Dict[str, Any]]) -> List[str]:
    if not isinstance(graph_state, dict):
        return []
    candidate = graph_state.get("verifier_candidate")
    if not isinstance(candidate, dict):
        return []
    raw_paths: List[str] = []
    file_path = candidate.get("file_path")
    if isinstance(file_path, str) and file_path.strip():
        raw_paths.append(file_path)
    for key in ("file_paths", "target_files"):
        value = candidate.get(key)
        if isinstance(value, list):
            raw_paths.extend(str(item) for item in value if str(item or "").strip())
    out: List[str] = []
    seen: set[str] = set()
    for raw in raw_paths:
        path = raw.strip().replace("\\", "/").lstrip("/")
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _probe_target_imports(
    sandbox: RepoSandbox,
    *,
    python_path: str,
    workdir: str,
    target_files: List[str],
) -> tuple[List[Dict[str, Any]], List[str]]:
    probes: List[Dict[str, Any]] = []
    missing: List[str] = []
    for file_path in target_files:
        module = _module_name_for_path(file_path)
        if not module:
            continue
        probe = sandbox.execute_result(
            [python_path, "-c", f"import importlib; importlib.import_module({module!r})"],
            workdir=workdir,
        )
        combined = f"{probe.stdout}\n{probe.stderr}"
        missing_modules = sorted(set(_MISSING_MODULE_RE.findall(combined)))
        missing.extend(missing_modules)
        probes.append(
            {
                "file_path": file_path,
                "module": module,
                "exit_code": probe.exit_code,
                "status": "ok" if probe.exit_code == 0 else "failed",
                "missing_modules": missing_modules,
                "stdout": _truncate_stream(probe.stdout, 1000),
                "stderr": _truncate_stream(probe.stderr, 1000),
            }
        )
    return probes, sorted(set(missing))


def _venv_site_packages(
    sandbox: RepoSandbox,
    *,
    python_path: str,
    workdir: str,
) -> str:
    result = sandbox.execute_result(
        [
            python_path,
            "-c",
            (
                "import site, sysconfig; "
                "paths = site.getsitepackages() or [sysconfig.get_paths().get('purelib', '')]; "
                "print(paths[0] if paths else '')"
            ),
        ],
        workdir=workdir,
    )
    return result.stdout.strip() if result.exit_code == 0 else ""


def _module_available(
    sandbox: RepoSandbox,
    *,
    python_path: str,
    workdir: str,
    module_name: str,
) -> bool:
    result = sandbox.execute_result(
        [python_path, "-c", f"import importlib; importlib.import_module({module_name!r})"],
        workdir=workdir,
    )
    return result.exit_code == 0


def _ensure_lightweight_compat_shims(
    sandbox: RepoSandbox,
    *,
    python_path: str,
    workdir: str,
    metadata: Dict[str, Any],
) -> None:
    site_packages = ""
    for module_name, shim_source in _LIGHTWEIGHT_COMPAT_SHIMS.items():
        if _module_available(
            sandbox,
            python_path=python_path,
            workdir=workdir,
            module_name=module_name,
        ):
            continue
        if not site_packages:
            site_packages = _venv_site_packages(sandbox, python_path=python_path, workdir=workdir)
        attempt: Dict[str, Any] = {
            "target": module_name,
            "action": "compat_shim",
            "exit_code": 1,
        }
        if not site_packages:
            attempt["failure_reason"] = "site_packages_unavailable"
            metadata["install_attempts"].append(attempt)
            continue
        dest = f"{site_packages.rstrip('/')}/{module_name}.py"
        sandbox.write_file_in_container(dest, shim_source.encode("utf-8"))
        ok = _module_available(
            sandbox,
            python_path=python_path,
            workdir=workdir,
            module_name=module_name,
        )
        attempt["exit_code"] = 0 if ok else 1
        metadata["install_attempts"].append(attempt)


def _prepare_verifier_env(
    sandbox: RepoSandbox,
    *,
    workdir: str,
    settings: Settings,
    target_files: List[str] | None = None,
) -> Dict[str, Any]:
    """Best-effort per-attempt venv preparation; failures are advisory metadata only."""
    if not getattr(settings, "verifier_prepare_env_enabled", True):
        return {"status": "disabled", "python_path": "python"}

    target_files = target_files or []
    fingerprint = _repo_dependency_fingerprint(sandbox, workdir)
    venv_dir = f"{workdir.rstrip('/')}/.verifier_venv_{fingerprint}"
    python_path = f"{venv_dir}/bin/python"
    metadata: Dict[str, Any] = {
        "status": "preparing",
        "fingerprint": fingerprint,
        "venv_dir": venv_dir,
        "python_path": python_path,
        "install_attempts": [],
        "dependency_install_policy": "targeted_only",
        "missing_modules": [],
        "target_files": target_files,
        "target_import_probes": [],
        "reused": False,
    }

    existing_probe = sandbox.execute_result([python_path, "-c", "import sys; print(sys.executable)"], workdir=workdir)
    if existing_probe.exit_code == 0:
        metadata["status"] = "usable"
        metadata["reused"] = True
        _ensure_lightweight_compat_shims(
            sandbox,
            python_path=python_path,
            workdir=workdir,
            metadata=metadata,
        )
        probes, missing = _probe_target_imports(
            sandbox,
            python_path=python_path,
            workdir=workdir,
            target_files=target_files,
        )
        metadata["target_import_probes"] = probes
        metadata["missing_modules"] = missing
        return metadata

    create = sandbox.execute_result(["python", "-m", "venv", venv_dir], workdir=workdir)
    if create.exit_code != 0:
        metadata.update(
            {
                "status": "failed",
                "failure_reason": "venv_create_failed",
                "stdout": _truncate_stream(create.stdout, 2000),
                "stderr": _truncate_stream(create.stderr, 2000),
                "python_path": "python",
            }
        )
        return metadata

    probe = sandbox.execute_result([python_path, "-c", "import sys; print(sys.executable)"], workdir=workdir)
    if probe.exit_code != 0:
        metadata.update(
            {
                "status": "failed",
                "failure_reason": "python_probe_failed",
                "stdout": _truncate_stream(probe.stdout, 2000),
                "stderr": _truncate_stream(probe.stderr, 2000),
                "python_path": "python",
            }
        )
        return metadata

    probes, missing = _probe_target_imports(
        sandbox,
        python_path=python_path,
        workdir=workdir,
        target_files=target_files,
    )
    metadata["target_import_probes"] = probes
    metadata["missing_modules"] = missing

    _ensure_lightweight_compat_shims(
        sandbox,
        python_path=python_path,
        workdir=workdir,
        metadata=metadata,
    )
    metadata["status"] = "usable"
    return metadata


def validate_test_code(test_code: str) -> str | None:
    """Return an error message if ``test_code`` is not valid Python."""
    try:
        ast.parse(test_code)
    except SyntaxError as exc:
        return f"SyntaxError: {exc}"
    return None


def execute_test_script(
    *,
    repo_path: str,
    candidate_id: str,
    attempt_number: int,
    test_code: str,
    settings: Settings | None = None,
    sandbox_factory: Callable[[Settings, str], SandboxRuntime] | None = None,
    graph_state: Optional[Dict[str, Any]] = None,
) -> VerifierAttemptRecord:
    """Write script to /tmp and run ``python`` with timeout; stop sandbox after."""
    settings = settings or get_settings()
    make_sandbox = sandbox_factory or (
        lambda s, image: build_repo_sandbox(s, image_name=image)
    )
    image_name = _verifier_sandbox_image(settings, repo_path, graph_state)
    sandbox = make_sandbox(settings, image_name)
    frag = _safe_candidate_path_fragment(candidate_id)
    remote_path = f"/tmp/verify_{frag}_{attempt_number}.py"
    record = VerifierAttemptRecord(
        attempt_number=attempt_number,
        test_code=test_code,
        status=VerificationStatus.EXECUTING,
    )
    started = time.perf_counter()
    try:
        syntax_err = validate_test_code(test_code)
        if syntax_err:
            record.exit_code = 2
            record.stdout = f"STATUS: HARNESS_ERROR | {syntax_err}"
            record.stderr = syntax_err
            record.status = VerificationStatus.COMPLETED
            record.sandbox_mode = "harness_preflight"
            return record

        sandbox_mode = _start_verifier_sandbox(
            sandbox, repo_path, graph_state, settings=settings
        )
        exec_wd = sandbox.execution_workdir
        record.repo_root = exec_wd
        record.sandbox_mode = sandbox_mode
        env_meta = _prepare_verifier_env(
            sandbox,
            workdir=exec_wd,
            settings=settings,
            target_files=_target_files_from_graph_state(graph_state),
        )
        record.env_metadata = env_meta
        record.lint_runs = _collect_lint_runs(sandbox, settings)
        sandbox.write_file_in_container(remote_path, test_code.encode("utf-8"))
        python_cmd = str(env_meta.get("python_path") or "python") if env_meta.get("status") == "usable" else "python"
        cmd = [python_cmd, remote_path]

        def _run() -> tuple[int, str, str]:
            r = sandbox.execute_result(cmd, workdir=exec_wd)
            return r.exit_code, r.stdout, r.stderr

        # Do not use ``with ThreadPoolExecutor`` on the timeout path: its __exit__ calls
        # shutdown(wait=True) before we stop the container, so a stuck docker exec blocks forever.
        pool = ThreadPoolExecutor(max_workers=1)
        timed_out = False
        try:
            fut = pool.submit(_run)
            try:
                exit_code, stdout, stderr = fut.result(
                    timeout=settings.verifier_test_timeout_seconds
                )
            except FuturesTimeout:
                timed_out = True
                record.timeout = True
                record.exit_code = -1
                record.stdout = ""
                record.stderr = "Verifier execution timed out."
                record.status = VerificationStatus.FAILED
                logger.warning(
                    "verifier timeout candidate_id=%s attempt=%s",
                    candidate_id,
                    attempt_number,
                )
            else:
                record.exit_code = exit_code
                record.stdout = stdout
                record.stderr = stderr
                record.status = VerificationStatus.COMPLETED
        finally:
            if timed_out:
                try:
                    sandbox.stop()
                except Exception:  # noqa: BLE001
                    pass
                pool.shutdown(wait=False, cancel_futures=True)
            else:
                pool.shutdown(wait=True)
    except Exception as exc:  # noqa: BLE001
        record.exit_code = 2
        record.stdout = f"STATUS: HARNESS_ERROR | {exc.__class__.__name__}: {exc}"
        record.stderr = f"{exc.__class__.__name__}: {exc}"
        record.status = VerificationStatus.COMPLETED
        record.sandbox_mode = record.sandbox_mode or "harness_preflight"
        logger.warning("verifier sandbox error: %s", exc)
    finally:
        record.execution_time_seconds = time.perf_counter() - started
        try:
            sandbox.stop()
        except Exception:  # noqa: BLE001
            pass

    return record

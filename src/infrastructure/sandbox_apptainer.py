"""Apptainer-backed repository sandbox (cluster / --remote profile)."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import List, Optional, Sequence
from uuid import uuid4

from src.infrastructure.sandbox_runtime import SandboxExecResult


class ApptainerRepoSandbox:
    """Long-lived Apptainer instance with Docker-parity lifecycle methods."""

    def __init__(
        self,
        *,
        sif_path: str,
        apptainer_binary: str = "apptainer",
        instance_dir: Optional[str] = None,
        bind_tmpfs: bool = True,
        extra_binds: Optional[Sequence[str]] = None,
    ) -> None:
        self.image_name = sif_path
        self._binary = apptainer_binary
        self._instance_dir = instance_dir
        self._bind_tmpfs = bind_tmpfs
        self._extra_binds = list(extra_binds or [])
        self.container: str | None = None
        self._instance_name: str | None = None
        self._execution_workdir = "/repo"
        self._bind_args: List[str] = []
        self._staging_dir: Path | None = None
        self._started = False

        resolved = Path(sif_path).expanduser().resolve()
        if not resolved.is_file():
            raise FileNotFoundError(
                f"Apptainer image not found: {resolved}. "
                "Build SIF files with scripts/cluster/build_sif_images.sh or set REVIEW_APPTAINER_IMAGE."
            )
        self._sif_path = str(resolved)

    @property
    def execution_workdir(self) -> str:
        return self._execution_workdir

    def _scratch_root(self) -> Path:
        for key in ("SLURM_TMPDIR", "TMPDIR", "TEMP", "TMP"):
            value = os.environ.get(key, "").strip()
            if value:
                root = Path(value) / "bw-sandbox"
                root.mkdir(parents=True, exist_ok=True)
                return root
        root = Path(self._instance_dir or "/tmp") / "bw-sandbox"
        root.mkdir(parents=True, exist_ok=True)
        return root

    def _instance_file_args(self) -> List[str]:
        if not self._instance_dir:
            return []
        path = Path(self._instance_dir)
        path.mkdir(parents=True, exist_ok=True)
        return ["--instance-dir", str(path)]

    def _base_run_args(self) -> List[str]:
        args = [self._binary]
        args.extend(self._instance_file_args())
        return args

    def _run(
        self,
        args: List[str],
        *,
        check: bool = True,
        input_bytes: bytes | None = None,
    ) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            input=input_bytes,
            errors="replace",
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip()
            raise RuntimeError(
                f"Apptainer command failed ({result.returncode}): {' '.join(args)}\n{detail}"
            )
        return result

    def _instance_uri(self) -> str:
        if not self._instance_name:
            raise RuntimeError("Sandbox is not started.")
        return f"instance://{self._instance_name}"

    def _start_instance(self, *, binds: List[str], workdir: str) -> str:
        if self._started:
            raise RuntimeError("Sandbox is already started.")

        if not self._instance_name:
            self._instance_name = f"bw-{uuid4().hex[:12]}"
        self._bind_args = list(binds) + list(self._extra_binds)
        self._staging_dir = self._scratch_root() / self._instance_name
        self._staging_dir.mkdir(parents=True, exist_ok=True)
        staging_bind = f"{self._staging_dir}:/tmp/bw-staging"
        if staging_bind not in self._bind_args:
            self._bind_args.append(staging_bind)

        cmd = self._base_run_args() + ["instance", "start"]
        if self._bind_tmpfs:
            cmd.append("--writable-tmpfs")
        for bind in self._bind_args:
            cmd.extend(["--bind", bind])
        cmd.extend([self._sif_path, self._instance_name])

        self._run(cmd)
        self._started = True
        self.container = self._instance_name
        return self._instance_name

    def create_execution_workspace(self, workspace_name: Optional[str] = None) -> str:
        if not self._started:
            raise RuntimeError("Sandbox not started.")

        name = workspace_name or f"exec_{uuid4().hex[:8]}"
        workspace_path = f"/tmp/{name}"
        self.execute(["mkdir", "-p", workspace_path], check_exit_code=True)
        copy_script = (
            "set -e; "
            "for p in /repo/* /repo/.[!.]* /repo/..?*; do "
            '  [ -e "$p" ] || continue; '
            '  name="$(basename "$p")"; '
            "  case \"$name\" in "
            "    .|..|.git|.venv|__pycache__|.pytest_cache|.mypy_cache|.ruff_cache) continue ;; "
            "  esac; "
            f'  cp -a "$p" "{workspace_path}/"; '
            "done"
        )
        self.execute(["sh", "-lc", copy_script], check_exit_code=True)
        self._execution_workdir = workspace_path
        return workspace_path

    def start(self, local_repo_path: str) -> str:
        abs_path = os.path.abspath(local_repo_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Path {abs_path} does not exist.")
        self._execution_workdir = "/repo"
        return self._start_instance(binds=[f"{abs_path}:/repo:ro"], workdir="/repo")

    def start_snippet_workspace(self) -> str:
        self._execution_workdir = "/workspace"
        return self._start_instance(binds=[], workdir="/workspace")

    def start_from_remote(self, repo_url: str, commit_hash: str) -> str:
        self._execution_workdir = "/repo"
        if not self._instance_name:
            self._instance_name = f"bw-{uuid4().hex[:12]}"
        host_repo_dir = self._scratch_root() / self._instance_name / "repo"
        host_repo_dir.mkdir(parents=True, exist_ok=True)
        instance_id = self._start_instance(binds=[f"{host_repo_dir}:/repo"], workdir="/")
        try:
            self.execute(["git", "clone", repo_url, "/repo"], check_exit_code=True)
            self.execute(
                ["git", "-C", "/repo", "checkout", "--detach", commit_hash],
                check_exit_code=True,
            )
        except Exception:
            self.stop()
            raise
        return instance_id

    def start_from_remote_ref(self, repo_url: str, ref: str) -> str:
        self._execution_workdir = "/repo"
        if not self._instance_name:
            self._instance_name = f"bw-{uuid4().hex[:12]}"
        host_repo_dir = self._scratch_root() / self._instance_name / "repo"
        host_repo_dir.mkdir(parents=True, exist_ok=True)
        instance_id = self._start_instance(binds=[f"{host_repo_dir}:/repo"], workdir="/")
        try:
            self.execute(["git", "clone", repo_url, "/repo"], check_exit_code=True)
            if ref.startswith("pull/"):
                local_ref = f"review-{uuid4().hex[:8]}"
                self.execute(
                    ["git", "-C", "/repo", "fetch", "origin", f"{ref}:{local_ref}"],
                    check_exit_code=True,
                )
                self.execute(
                    ["git", "-C", "/repo", "checkout", "--detach", local_ref],
                    check_exit_code=True,
                )
            else:
                self.execute(
                    ["git", "-C", "/repo", "checkout", "--detach", ref],
                    check_exit_code=True,
                )
        except Exception:
            self.stop()
            raise
        return instance_id

    def execute(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
        check_exit_code: bool = False,
    ) -> str:
        result = self.execute_result(cmd, workdir=workdir)
        if check_exit_code and result.exit_code != 0:
            raise RuntimeError(
                f"Sandbox command failed with exit code {result.exit_code}: {' '.join(cmd)}\n"
                f"{result.stdout}\n{result.stderr}"
            )
        return result.stdout

    def execute_result(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
    ) -> SandboxExecResult:
        if not self._started:
            raise RuntimeError("Sandbox not started.")

        run_cmd = self._base_run_args() + ["exec"]
        if workdir:
            run_cmd.extend(["--pwd", workdir])
        run_cmd.append(self._instance_uri())
        run_cmd.extend(cmd)

        completed = self._run(run_cmd, check=False)
        return SandboxExecResult(
            exit_code=int(completed.returncode),
            stdout=completed.stdout or "",
            stderr=completed.stderr or "",
        )

    def write_file_in_container(self, dest_path: str, content: bytes) -> None:
        if not self._started or self._staging_dir is None:
            raise RuntimeError("Sandbox not started.")

        dest_path = dest_path.replace("\\", "/")
        staging = self._staging_dir / Path(dest_path.lstrip("/")).name
        staging.parent.mkdir(parents=True, exist_ok=True)
        staging.write_bytes(content)

        in_container = f"/tmp/bw-staging/{staging.name}"
        self.execute(
            ["sh", "-lc", f"cp {in_container!r} {dest_path!r}"],
            check_exit_code=True,
        )

    def stop(self) -> None:
        if not self._started or not self._instance_name:
            return
        try:
            self._run(
                self._base_run_args() + ["instance", "stop", self._instance_name],
                check=False,
            )
        finally:
            if self._staging_dir and self._staging_dir.exists():
                shutil.rmtree(self._staging_dir, ignore_errors=True)
            self._started = False
            self._instance_name = None
            self.container = None
            self._staging_dir = None

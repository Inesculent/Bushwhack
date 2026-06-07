"""Docker-backed repository sandbox (local / --local profile)."""

from __future__ import annotations

import io
import os
import tarfile
from typing import List, Optional
from uuid import uuid4

import docker

from src.infrastructure.sandbox_runtime import SandboxExecResult


class DockerRepoSandbox:
    def __init__(self, image_name: str = "agent-fs-sandbox"):
        try:
            self.client = docker.from_env()
        except Exception as e:
            raise RuntimeError(f"Docker is not running or not accessible: {e}")

        self.image_name = image_name
        self.container = None
        self._execution_workdir = "/repo"

    @property
    def execution_workdir(self) -> str:
        """Working directory for verifier/exec helpers: ``/repo`` when mounted or cloned, else ``/workspace``."""
        return self._execution_workdir

    def create_execution_workspace(self, workspace_name: Optional[str] = None) -> str:
        """Copy read-only ``/repo`` into a writable workspace inside the container."""
        if not self.container:
            raise RuntimeError("Sandbox not started.")

        name = workspace_name or f"exec_{uuid4().hex[:8]}"
        workspace_path = f"/{name}"

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
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        abs_path = os.path.abspath(local_repo_path)
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Path {abs_path} does not exist.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            volumes={abs_path: {"bind": "/repo", "mode": "ro"}},
            working_dir="/repo",
        )
        self._execution_workdir = "/repo"
        return self.container.id

    def start_snippet_workspace(self) -> str:
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            tty=True,
            working_dir="/workspace",
        )
        self._execution_workdir = "/workspace"
        return self.container.id

    def start_from_remote(self, repo_url: str, commit_hash: str) -> str:
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            tty=True,
            working_dir="/",
        )
        self._execution_workdir = "/repo"

        try:
            self.execute(["git", "clone", repo_url, "/repo"], check_exit_code=True)
            self.execute(
                ["git", "-C", "/repo", "checkout", "--detach", commit_hash],
                check_exit_code=True,
            )
        except Exception:
            self.stop()
            raise

        return self.container.id

    def start_from_remote_ref(self, repo_url: str, ref: str) -> str:
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            tty=True,
            working_dir="/",
        )
        self._execution_workdir = "/repo"

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

        return self.container.id

    def execute(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
        check_exit_code: bool = False,
    ) -> str:
        if not self.container:
            raise RuntimeError("Sandbox is not started.")

        exit_code, output = self.container.exec_run(cmd, workdir=workdir)
        decoded_output = output.decode("utf-8", errors="replace")

        if check_exit_code and exit_code != 0:
            raise RuntimeError(
                f"Sandbox command failed with exit code {exit_code}: {' '.join(cmd)}\n{decoded_output}"
            )

        return decoded_output

    def execute_result(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
    ) -> SandboxExecResult:
        if not self.container:
            raise RuntimeError("Sandbox not started.")

        exit_code, output = self.container.exec_run(cmd, workdir=workdir, demux=True)
        if output is None:
            stdout_b, stderr_b = b"", b""
        else:
            stdout_b, stderr_b = output
            stdout_b = stdout_b or b""
            stderr_b = stderr_b or b""
        return SandboxExecResult(
            exit_code=int(exit_code),
            stdout=stdout_b.decode("utf-8", errors="replace"),
            stderr=stderr_b.decode("utf-8", errors="replace"),
        )

    def write_file_in_container(self, dest_path: str, content: bytes) -> None:
        if not self.container:
            raise RuntimeError("Sandbox not started.")

        dest_path = dest_path.replace("\\", "/")
        parent = os.path.dirname(dest_path) or "/"
        base = os.path.basename(dest_path)

        tar_stream = io.BytesIO()
        with tarfile.open(fileobj=tar_stream, mode="w") as tar:
            data = io.BytesIO(content)
            info = tarfile.TarInfo(name=base)
            info.size = len(content)
            tar.addfile(info, fileobj=data)
        tar_stream.seek(0)
        ok = self.container.put_archive(parent, tar_stream.read())
        if not ok:
            raise RuntimeError(f"put_archive failed for {dest_path}")

    def stop(self) -> None:
        if self.container:
            self.container.stop()
            self.container.remove()
            self.container = None

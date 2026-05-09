import io
import os
import tarfile
from dataclasses import dataclass
from uuid import uuid4
from typing import List, Optional

import docker


@dataclass(frozen=True)
class SandboxExecResult:
    """Result of a container exec with exit code and split streams."""

    exit_code: int
    stdout: str
    stderr: str


class RepoSandbox:
    def __init__(self, image_name: str = "agent-fs-sandbox"):
        try:
            self.client = docker.from_env()
        except Exception as e:
            raise RuntimeError(f"Docker is not running or not accessible: {e}")
        
        self.image_name = image_name
        self.container = None

    def create_execution_workspace(self, workspace_name: Optional[str] = None) -> str:
        """
        Creates a Read-Write copy of the Read-Only mounted repo 
        so the agent can safely compile code and run tests.
        """
        if not self.container:
            raise RuntimeError("Sandbox not started.")

        name = workspace_name or f"exec_{uuid4().hex[:8]}"
        workspace_path = f"/{name}"
            
        # 1. Create a fresh directory inside the container (not on your host)
        self.execute(["mkdir", "-p", workspace_path], check_exit_code=True)

        # 2. Copy the code from the Read-Only mount to the Ephemeral Workspace.
        # Skip heavy ephemeral directories to keep integration tests fast.
        copy_script = (
            "set -e; "
            "for p in /repo/* /repo/.[!.]* /repo/..?*; do "
            "  [ -e \"$p\" ] || continue; "
            "  name=\"$(basename \"$p\")\"; "
            "  case \"$name\" in "
            "    .|..|.git|.venv|__pycache__|.pytest_cache|.mypy_cache|.ruff_cache) continue ;; "
            "  esac; "
            f"  cp -a \"$p\" \"{workspace_path}/\"; "
            "done"
        )
        self.execute(["sh", "-lc", copy_script], check_exit_code=True)
        
        return workspace_path

    def start(self, local_repo_path: str):
        """Spins up the container and mounts the repo."""
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        abs_path = os.path.abspath(local_repo_path)
        
        # Verify the path exists before trying to mount
        if not os.path.exists(abs_path):
            raise FileNotFoundError(f"Path {abs_path} does not exist.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            volumes={abs_path: {'bind': '/repo', 'mode': 'ro'}}, # Read-only for safety
            working_dir="/repo"
        )
        return self.container.id

    def start_from_remote(self, repo_url: str, commit_hash: str) -> str:
        """
        Spins up a container and clones a remote repository inside the container
        filesystem, then checks out a specific commit.
        """
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            tty=True,
            working_dir="/",
        )

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
        """
        Spins up a container, clones a remote repository, and checks out a ref.

        Supports normal refs/SHAs as well as GitHub pull request refs such as
        ``pull/123/head``.
        """
        if self.container:
            raise RuntimeError("Sandbox is already started.")

        self.container = self.client.containers.run(
            self.image_name,
            detach=True,
            tty=True,
            working_dir="/",
        )

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
        """Runs a command inside the sandbox and returns stdout."""
        if not self.container:
            raise RuntimeError("Sandbox is not started.")
        
        exit_code, output = self.container.exec_run(cmd, workdir=workdir)
        decoded_output = output.decode("utf-8", errors="replace")

        if check_exit_code and exit_code != 0:
            raise RuntimeError(
                f"Sandbox command failed with exit code {exit_code}: {' '.join(cmd)}\n{decoded_output}"
            )
        
        # In a real review, we'd want to handle exit_code != 0
        return decoded_output

    def execute_result(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
    ) -> SandboxExecResult:
        """Run a command and return exit code plus stdout/stderr (demux)."""
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
        """Write bytes to ``dest_path`` inside the running container (e.g. /tmp/script.py)."""
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

    def stop(self):
        """Cleans up the container."""
        if self.container:
            self.container.stop()
            self.container.remove()
            self.container = None
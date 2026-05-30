"""Shared sandbox types and protocol."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Protocol, runtime_checkable


@dataclass(frozen=True)
class SandboxExecResult:
    """Result of a sandbox exec with exit code and split streams."""

    exit_code: int
    stdout: str
    stderr: str


@runtime_checkable
class SandboxRuntime(Protocol):
    """Isolated repo execution surface (Docker or Apptainer)."""

    image_name: str
    container: object | None

    @property
    def execution_workdir(self) -> str: ...

    def create_execution_workspace(self, workspace_name: Optional[str] = None) -> str: ...

    def start(self, local_repo_path: str) -> str: ...

    def start_snippet_workspace(self) -> str: ...

    def start_from_remote(self, repo_url: str, commit_hash: str) -> str: ...

    def start_from_remote_ref(self, repo_url: str, ref: str) -> str: ...

    def execute(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
        check_exit_code: bool = False,
    ) -> str: ...

    def execute_result(
        self,
        cmd: List[str],
        workdir: Optional[str] = None,
    ) -> SandboxExecResult: ...

    def write_file_in_container(self, dest_path: str, content: bytes) -> None: ...

    def stop(self) -> None: ...

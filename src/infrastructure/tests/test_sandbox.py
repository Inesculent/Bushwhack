import os
from pathlib import Path
from uuid import uuid4

import pytest


def _debug_enabled() -> bool:
    return os.getenv("SANDBOX_TEST_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_print(title: str, content: str) -> None:
    if _debug_enabled():
        print(f"\n[{title}]\n{content}", flush=True)


@pytest.fixture(scope="module")
def sandbox_session(repo_root: Path):
    from src.infrastructure.sandbox import RepoSandbox

    sandbox = RepoSandbox()
    try:
        sandbox.start(str(repo_root))
        yield sandbox
    finally:
        sandbox.stop()


@pytest.fixture(scope="module")
def execution_workspace(sandbox_session) -> str:
    workspace_name = f"test_env_{uuid4().hex[:8]}"
    workspace_path = sandbox_session.create_execution_workspace(workspace_name)
    _debug_print("Execution Workspace", workspace_path)
    return workspace_path


def test_repo_sandbox_can_list_repo_files(sandbox_session) -> None:
    output = sandbox_session.execute(["ls", "-1"], check_exit_code=True)
    _debug_print("Repo Listing", output)
    assert "src" in output, "Expected repository listing to include 'src'"


def test_execution_workspace_contains_repo_copy(
    sandbox_session,
    execution_workspace: str,
) -> None:
    output = sandbox_session.execute(["ls", "-1", execution_workspace], check_exit_code=True)
    _debug_print("Workspace Root Listing", output)
    assert "src" in output, "Expected copied workspace to contain src"
    assert "requirements.txt" in output, "Expected copied workspace to contain requirements.txt"


def test_execution_workspace_is_writable_and_isolated(
    sandbox_session,
    execution_workspace: str,
    repo_root: Path,
) -> None:
    probe_dir_name = f"sandbox_write_probe_{uuid4().hex[:8]}"
    probe_dir_path = f"{execution_workspace}/{probe_dir_name}"

    sandbox_session.execute(["mkdir", "-p", probe_dir_path], check_exit_code=True)
    output = sandbox_session.execute(["ls", "-1", execution_workspace], check_exit_code=True)
    _debug_print("Workspace After Write Probe", output)

    assert probe_dir_name in output, "Expected write probe directory in execution workspace"
    assert not (repo_root / probe_dir_name).exists(), "Write probe leaked to host repository"


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()  # Ensure .env variables are loaded for the test run
    pytest_args = [str(Path(__file__))]
    if _debug_enabled():
        pytest_args.insert(0, "-s")

    raise SystemExit(pytest.main(pytest_args))
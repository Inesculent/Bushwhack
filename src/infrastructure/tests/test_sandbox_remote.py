import os
import json
import pytest


def _debug_enabled() -> bool:
    return os.getenv("SANDBOX_TEST_DEBUG", "").strip().lower() in {"1", "true", "yes", "on"}


def _debug_print(title: str, content: str) -> None:
    if _debug_enabled():
        print(f"\n[{title}]\n{content}", flush=True)


class _FakeContainer:
    def __init__(self, container_id: str = "container-123") -> None:
        self.id = container_id
        self.stopped = False
        self.removed = False

    def stop(self) -> None:
        self.stopped = True

    def remove(self) -> None:
        self.removed = True


class _FakeContainersApi:
    def __init__(self) -> None:
        self.last_run_args = None
        self.last_run_kwargs = None
        self.last_container = None

    def run(self, *args, **kwargs):
        self.last_run_args = args
        self.last_run_kwargs = kwargs
        self.last_container = _FakeContainer()
        return self.last_container


class _FakeClient:
    def __init__(self) -> None:
        self.containers = _FakeContainersApi()


def test_start_from_remote_bootstraps_repo(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.infrastructure.sandbox import RepoSandbox

    fake_client = _FakeClient()
    monkeypatch.setattr("src.infrastructure.sandbox.docker.from_env", lambda: fake_client)

    sandbox = RepoSandbox(image_name="agent-fs-sandbox")

    commands = []

    def _fake_execute(cmd, workdir=None, check_exit_code=False):
        commands.append((cmd, workdir, check_exit_code))
        return ""

    monkeypatch.setattr(sandbox, "execute", _fake_execute)

    container_id = sandbox.start_from_remote(
        "https://example.com/org/repo.git",
        "deadbeef",
    )

    assert container_id == "container-123"
    assert fake_client.containers.last_run_args == ("agent-fs-sandbox",)
    assert fake_client.containers.last_run_kwargs == {
        "detach": True,
        "tty": True,
        "working_dir": "/",
    }
    assert commands == [
        (["git", "clone", "https://example.com/org/repo.git", "/repo"], None, True),
        (["git", "-C", "/repo", "checkout", "--detach", "deadbeef"], None, True),
    ]


def test_start_from_remote_cleans_up_on_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from src.infrastructure.sandbox import RepoSandbox

    fake_client = _FakeClient()
    monkeypatch.setattr("src.infrastructure.sandbox.docker.from_env", lambda: fake_client)

    sandbox = RepoSandbox(image_name="agent-fs-sandbox")

    def _failing_execute(cmd, workdir=None, check_exit_code=False):
        raise RuntimeError("clone failed")

    monkeypatch.setattr(sandbox, "execute", _failing_execute)

    with pytest.raises(RuntimeError, match="clone failed"):
        sandbox.start_from_remote("https://example.com/org/repo.git", "deadbeef")

    assert sandbox.container is None, "Sandbox should clean container reference on failure"
    assert fake_client.containers.last_container is not None
    assert fake_client.containers.last_container.stopped is True
    assert fake_client.containers.last_container.removed is True


@pytest.mark.integration
def test_start_from_remote_smoke_if_configured() -> None:
    from src.infrastructure.sandbox import RepoSandbox

    repo_url = os.getenv("SANDBOX_REMOTE_TEST_URL", "").strip()
    commit_hash = os.getenv("SANDBOX_REMOTE_TEST_COMMIT", "").strip()

    if not repo_url or not commit_hash:
        pytest.skip("Set SANDBOX_REMOTE_TEST_URL and SANDBOX_REMOTE_TEST_COMMIT to run this test")

    sandbox = RepoSandbox()
    try:
        sandbox.start_from_remote(repo_url, commit_hash)
        listing = sandbox.execute(["ls", "-1", "/repo"], check_exit_code=True)
        _debug_print("Remote Repo Listing", listing)
        assert listing.strip(), "Expected non-empty /repo listing after remote clone"
    finally:
        sandbox.stop()


@pytest.mark.integration
def test_remote_preflight_and_ast_smoke_if_configured() -> None:
    from src.infrastructure.remote_review_workflow import (
        resolve_remote_commits_from_env,
        run_remote_review_workflow,
    )

    repo_url, base_commit, head_commit = resolve_remote_commits_from_env()
    if not repo_url or not head_commit:
        pytest.skip("Set SANDBOX_REMOTE_TEST_URL and SANDBOX_REMOTE_TEST_HEAD (or SANDBOX_REMOTE_TEST_COMMIT)")

    result = run_remote_review_workflow(
        repo_url=repo_url,
        head_commit=head_commit,
        base_commit=base_commit,
    )

    _debug_print("Commit Diff", result.diff[:4000])
    _debug_print("AST Summary", json.dumps(result.ast_summary, indent=2))

    assert result.manifest.aggregate_metrics.total_files_changed >= 1
    assert result.manifest.errors == []
    assert result.ast_summary, "Expected at least one AST summary entry"



if __name__ == "__main__":
    from pathlib import Path
    from dotenv import load_dotenv

    load_dotenv()  # Ensure .env variables are loaded for the test run.

    # Hardcoded remote smoke-test target for now.
    os.environ["SANDBOX_REMOTE_TEST_URL"] = "https://github.com/Inesculent/AgenticReview"
    os.environ["SANDBOX_REMOTE_TEST_HEAD"] = "19cf41ba3999d9294e89f6654c3b68b6c201effc"
    os.environ.setdefault("SANDBOX_REMOTE_TEST_BASE", "19cf41ba3999d9294e89f6654c3b68b6c201effc^")
    os.environ.setdefault("SANDBOX_TEST_DEBUG", "1")

    pytest_args = [
        "-s",
        f"{Path(__file__)}::test_remote_preflight_and_ast_smoke_if_configured",
    ]

    raise SystemExit(pytest.main(pytest_args))
    
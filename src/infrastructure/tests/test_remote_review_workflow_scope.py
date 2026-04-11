from src.infrastructure.remote_review_workflow import run_remote_review_workflow


class _FakeSandbox:
    def __init__(self) -> None:
        self.commands: list[list[str]] = []

    def start_from_remote(self, repo_url: str, commit_hash: str) -> str:
        self.commands.append(["start_from_remote", repo_url, commit_hash])
        return "container-id"

    def execute(self, cmd, check_exit_code=False, workdir=None):
        self.commands.append(list(cmd))

        if cmd[:4] == ["git", "-C", "/repo", "diff"]:
            return (
                "diff --git a/utils.py b/utils.py\n"
                "index 1111111..2222222 100644\n"
                "--- a/utils.py\n"
                "+++ b/utils.py\n"
                "@@ -1 +1 @@\n"
                "-def old():\n"
                "+def new():\n"
            )

        if cmd[:5] == ["git", "-C", "/repo", "ls-files", "*.py"]:
            return "utils.py\nhelpers.py\n"

        if len(cmd) >= 3 and cmd[0] == "python" and cmd[1] == "-c":
            files = cmd[3:]
            payload = [
                {
                    "file": path,
                    "total_top_level_nodes": 1,
                    "top_level_nodes": ["FunctionDef"],
                    "total_ast_nodes": 3,
                    "top_level_symbols": [{"name": "fn", "type": "FunctionDef"}],
                }
                for path in files
            ]
            import json

            return json.dumps(payload)

        return ""

    def stop(self) -> None:
        self.commands.append(["stop"])


def test_run_remote_review_workflow_repository_scope_scans_all_python_files() -> None:
    sandbox = _FakeSandbox()

    result = run_remote_review_workflow(
        repo_url="https://example.com/repo.git",
        head_commit="deadbeef",
        base_commit="deadbeef^",
        sandbox=sandbox,
        ast_scope="repository",
    )

    assert result.changed_python_files == ["utils.py"]
    assert result.scanned_python_files == ["utils.py", "helpers.py"]
    assert sorted(item["file"] for item in result.ast_summary) == ["helpers.py", "utils.py"]


def test_run_remote_review_workflow_changed_scope_scans_changed_files_only() -> None:
    sandbox = _FakeSandbox()

    result = run_remote_review_workflow(
        repo_url="https://example.com/repo.git",
        head_commit="deadbeef",
        base_commit="deadbeef^",
        sandbox=sandbox,
        ast_scope="changed",
    )

    assert result.changed_python_files == ["utils.py"]
    assert result.scanned_python_files == ["utils.py"]
    assert [item["file"] for item in result.ast_summary] == ["utils.py"]

from src.infrastructure.remote_review_workflow import run_remote_review_workflow


CHANGED_FILE = "module_changed.py"
REPO_PY_FILES = ["module_changed.py", "module_adjacent.py"]


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
                f"diff --git a/{CHANGED_FILE} b/{CHANGED_FILE}\n"
                "index 1111111..2222222 100644\n"
                f"--- a/{CHANGED_FILE}\n"
                f"+++ b/{CHANGED_FILE}\n"
                "@@ -1 +1 @@\n"
                "-def old():\n"
                "+def new():\n"
            )

        if cmd[:5] == ["git", "-C", "/repo", "ls-files", "*.py"]:
            return "\n".join(REPO_PY_FILES) + "\n"

        if len(cmd) >= 3 and cmd[0] == "python" and cmd[1] == "-c":
            script = str(cmd[2])
            has_limit_arg = len(cmd) >= 4 and str(cmd[3]).isdigit()
            files = cmd[4:] if has_limit_arg else cmd[3:]

            if "formatted_ast" in script:
                payload = [
                    {
                        "file": path,
                        "formatted_ast": "Module(body=[...])",
                        "truncated": False,
                    }
                    for path in files
                ]
            else:
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

    assert result.changed_python_files == [CHANGED_FILE]
    assert result.scanned_python_files == REPO_PY_FILES
    assert sorted(item["file"] for item in result.ast_summary) == sorted(REPO_PY_FILES)


def test_run_remote_review_workflow_changed_scope_scans_changed_files_only() -> None:
    sandbox = _FakeSandbox()

    result = run_remote_review_workflow(
        repo_url="https://example.com/repo.git",
        head_commit="deadbeef",
        base_commit="deadbeef^",
        sandbox=sandbox,
        ast_scope="changed",
    )

    assert result.changed_python_files == [CHANGED_FILE]
    assert result.scanned_python_files == [CHANGED_FILE]
    assert [item["file"] for item in result.ast_summary] == [CHANGED_FILE]


def test_run_remote_review_workflow_can_include_ast_dump() -> None:
    sandbox = _FakeSandbox()

    result = run_remote_review_workflow(
        repo_url="https://example.com/repo.git",
        head_commit="deadbeef",
        base_commit="deadbeef^",
        sandbox=sandbox,
        ast_scope="changed",
        include_ast_dump=True,
    )

    assert result.scanned_python_files == [CHANGED_FILE]
    assert len(result.ast_dump) == 1
    assert result.ast_dump[0]["file"] == CHANGED_FILE
    assert "formatted_ast" in result.ast_dump[0]

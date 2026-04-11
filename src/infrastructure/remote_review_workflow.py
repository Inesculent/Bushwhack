import argparse
import json
import os
from dataclasses import dataclass
from typing import Any, Optional
from src.domain.schemas import DiffManifest, PreflightRequest, RunMetadata
from src.infrastructure.preflight.service import PreflightManifestService
from src.infrastructure.sandbox import RepoSandbox


@dataclass(frozen=True)
class RemoteReviewWorkflowResult:
    repo_url: str
    base_commit: str
    head_commit: str
    diff: str
    manifest: DiffManifest
    changed_python_files: list[str]
    ast_summary: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_url": self.repo_url,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "diff": self.diff,
            "manifest": self.manifest.model_dump(mode="json", exclude_none=True),
            "changed_python_files": self.changed_python_files,
            "ast_summary": self.ast_summary,
        }


def resolve_remote_commits_from_env() -> tuple[str, str, str]:
    repo_url = os.getenv("SANDBOX_REMOTE_TEST_URL", "").strip()
    head_commit = (
        os.getenv("SANDBOX_REMOTE_TEST_HEAD", "").strip()
        or os.getenv("SANDBOX_REMOTE_TEST_COMMIT", "").strip()
    )
    base_commit = os.getenv("SANDBOX_REMOTE_TEST_BASE", "").strip()

    if repo_url and head_commit and not base_commit:
        base_commit = f"{head_commit}^"

    return repo_url, base_commit, head_commit


def collect_python_ast_summary(sandbox: RepoSandbox, file_paths: list[str]) -> list[dict[str, Any]]:
    if not file_paths:
        return []

    script = (
        "import ast, json, pathlib, sys; "
        "out=[]; "
        "repo=pathlib.Path('/repo'); "
        "files=sys.argv[1:]; "
        "\nfor rel in files:\n"
        "    p = repo / rel\n"
        "    try:\n"
        "        src = p.read_text(encoding='utf-8', errors='replace')\n"
        "    except Exception as exc:\n"
        "        out.append({'file': rel, 'error': str(exc)})\n"
        "        continue\n"
        "    try:\n"
        "        tree = ast.parse(src)\n"
        "    except Exception as exc:\n"
        "        out.append({'file': rel, 'error': f'{exc.__class__.__name__}: {exc}'})\n"
        "        continue\n"
        "    top = [type(node).__name__ for node in tree.body]\n"
        "    out.append({'file': rel, 'total_top_level_nodes': len(top), 'top_level_nodes': top[:20]})\n"
        "print(json.dumps(out))"
    )

    output = sandbox.execute(["python", "-c", script, *file_paths], check_exit_code=True)
    payload = output.strip()
    if not payload:
        return []
    return json.loads(payload)


def run_remote_review_workflow(
    repo_url: str,
    head_commit: str,
    base_commit: Optional[str] = None,
    sandbox: Optional[RepoSandbox] = None,
    preflight_service: Optional[PreflightManifestService] = None,
    max_ast_files: int = 20,
    fallback_python_files: int = 5,
) -> RemoteReviewWorkflowResult:
    if not repo_url.strip():
        raise ValueError("repo_url is required")
    if not head_commit.strip():
        raise ValueError("head_commit is required")

    resolved_base_commit = (base_commit or f"{head_commit}^").strip()
    own_sandbox = sandbox is None
    resolved_sandbox = sandbox or RepoSandbox()
    resolved_preflight = preflight_service or PreflightManifestService()

    try:
        resolved_sandbox.start_from_remote(repo_url, head_commit)

        diff = resolved_sandbox.execute(
            [
                "git",
                "-C",
                "/repo",
                "diff",
                "--no-color",
                "--unified=0",
                f"{resolved_base_commit}..{head_commit}",
            ],
            check_exit_code=True,
        )

        manifest = resolved_preflight.build_diff_manifest(
            PreflightRequest(
                run_metadata=RunMetadata(
                    repo=repo_url,
                    base_sha=resolved_base_commit,
                    head_sha=head_commit,
                ),
                raw_diff=diff,
            )
        )

        changed_python_files = [
            entry.filepath
            for entry in manifest.files
            if entry.filepath.endswith(".py") and entry.change_type in {"A", "M", "R"}
        ]

        if not changed_python_files:
            listing = resolved_sandbox.execute(
                ["git", "-C", "/repo", "ls-files", "*.py"],
                check_exit_code=True,
            )
            changed_python_files = [line.strip() for line in listing.splitlines() if line.strip()][:fallback_python_files]

        ast_summary = collect_python_ast_summary(resolved_sandbox, changed_python_files[:max_ast_files])

        return RemoteReviewWorkflowResult(
            repo_url=repo_url,
            base_commit=resolved_base_commit,
            head_commit=head_commit,
            diff=diff,
            manifest=manifest,
            changed_python_files=changed_python_files,
            ast_summary=ast_summary,
        )
    finally:
        if own_sandbox:
            resolved_sandbox.stop()


def run_remote_review_workflow_from_env() -> RemoteReviewWorkflowResult:
    repo_url, base_commit, head_commit = resolve_remote_commits_from_env()
    if not repo_url or not head_commit:
        raise ValueError(
            "Set SANDBOX_REMOTE_TEST_URL and SANDBOX_REMOTE_TEST_HEAD "
            "(or SANDBOX_REMOTE_TEST_COMMIT); optional SANDBOX_REMOTE_TEST_BASE"
        )

    return run_remote_review_workflow(
        repo_url=repo_url,
        head_commit=head_commit,
        base_commit=base_commit,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run remote sandbox preflight + AST workflow and print JSON output.",
    )
    parser.add_argument("--repo-url", default=os.getenv("SANDBOX_REMOTE_TEST_URL", ""))
    parser.add_argument(
        "--head-commit",
        default=(
            os.getenv("SANDBOX_REMOTE_TEST_HEAD", "")
            or os.getenv("SANDBOX_REMOTE_TEST_COMMIT", "")
        ),
    )
    parser.add_argument("--base-commit", default=os.getenv("SANDBOX_REMOTE_TEST_BASE", ""))
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.repo_url or not args.head_commit:
        raise SystemExit(
            "Missing required values. Provide --repo-url and --head-commit, "
            "or set SANDBOX_REMOTE_TEST_URL and SANDBOX_REMOTE_TEST_HEAD."
        )

    result = run_remote_review_workflow(
        repo_url=args.repo_url,
        head_commit=args.head_commit,
        base_commit=args.base_commit or None,
    )
    print(json.dumps(result.as_dict(), indent=2))


if __name__ == "__main__":
    main()

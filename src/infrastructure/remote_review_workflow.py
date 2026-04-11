import argparse
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import networkx as nx

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
    scanned_python_files: list[str]
    ast_summary: list[dict[str, Any]]

    def as_dict(self) -> dict[str, Any]:
        return {
            "repo_url": self.repo_url,
            "base_commit": self.base_commit,
            "head_commit": self.head_commit,
            "diff": self.diff,
            "manifest": self.manifest.model_dump(mode="json", exclude_none=True),
            "changed_python_files": self.changed_python_files,
            "scanned_python_files": self.scanned_python_files,
            "ast_summary": self.ast_summary,
        }


def build_ast_summary_graph(ast_summary: list[dict[str, Any]]) -> nx.DiGraph:
    """Build a deterministic graph from AST summary payloads."""
    graph = nx.DiGraph()

    for item in sorted(ast_summary, key=lambda entry: str(entry.get("file", ""))):
        file_path = str(item.get("file", "unknown"))
        file_node = f"file:{file_path}"
        graph.add_node(file_node, node_type="file", file_path=file_path)

        if item.get("error"):
            error_node = f"error:{file_path}"
            graph.add_node(error_node, node_type="error", message=str(item["error"]))
            graph.add_edge(file_node, error_node, edge_type="parse_error")
            continue

        top_nodes = item.get("top_level_nodes")
        top_symbols = item.get("top_level_symbols")
        if isinstance(top_symbols, list) and top_symbols:
            for index, symbol in enumerate(top_symbols):
                if not isinstance(symbol, dict):
                    continue
                symbol_name = str(symbol.get("name", "unknown"))
                symbol_type = str(symbol.get("type", "symbol"))
                symbol_node = f"symbol:{file_path}:{index}:{symbol_name}"
                graph.add_node(
                    symbol_node,
                    node_type="symbol",
                    symbol_type=symbol_type,
                    symbol_name=symbol_name,
                    file_path=file_path,
                )
                graph.add_edge(file_node, symbol_node, edge_type="contains")
            continue

        if not isinstance(top_nodes, list):
            continue

        for index, node_type in enumerate(top_nodes):
            symbol_node = f"symbol:{file_path}:{index}:{node_type}"
            graph.add_node(symbol_node, node_type="symbol", symbol_type=str(node_type), file_path=file_path)
            graph.add_edge(file_node, symbol_node, edge_type="contains")

    return graph


def draw_ast_summary_graph(
    ast_summary: list[dict[str, Any]],
    output_path: str,
    title: str = "Remote Review AST Summary",
) -> str:
    """Render an AST summary graph image using nx.draw()."""
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception as exc:  # pragma: no cover - exercised via runtime environments
        raise RuntimeError(
            "matplotlib is required to render graph images. Install matplotlib to enable drawing."
        ) from exc

    graph = build_ast_summary_graph(ast_summary)
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)

    figure = plt.figure(figsize=(10, 7))
    if graph.number_of_nodes() == 0:
        plt.text(0.5, 0.5, "No AST summary data available", ha="center", va="center")
        plt.axis("off")
    else:
        positions = nx.spring_layout(graph, seed=42)
        nx.draw(
            graph,
            pos=positions,
            with_labels=True,
            node_size=1100,
            font_size=7,
            arrows=True,
        )

    plt.title(title)
    figure.savefig(output_file, dpi=170)
    plt.close(figure)
    return str(output_file)


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
        "    symbols = []\n"
        "    for node in tree.body:\n"
        "        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):\n"
        "            symbols.append({'name': node.name, 'type': type(node).__name__})\n"
        "    total_nodes = sum(1 for _ in ast.walk(tree))\n"
        "    out.append({'file': rel, 'total_top_level_nodes': len(top), 'top_level_nodes': top[:20], 'total_ast_nodes': total_nodes, 'top_level_symbols': symbols[:50]})\n"
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
    ast_scope: Literal["repository", "changed"] = "repository",
    max_ast_files: int = 0,
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

        if ast_scope == "repository":
            listing = resolved_sandbox.execute(
                ["git", "-C", "/repo", "ls-files", "*.py"],
                check_exit_code=True,
            )
            scanned_python_files = [line.strip() for line in listing.splitlines() if line.strip()]
        else:
            scanned_python_files = list(changed_python_files)

        if max_ast_files > 0:
            scanned_python_files = scanned_python_files[:max_ast_files]

        ast_summary = collect_python_ast_summary(resolved_sandbox, scanned_python_files)

        return RemoteReviewWorkflowResult(
            repo_url=repo_url,
            base_commit=resolved_base_commit,
            head_commit=head_commit,
            diff=diff,
            manifest=manifest,
            changed_python_files=changed_python_files,
            scanned_python_files=scanned_python_files,
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
    parser.add_argument(
        "--ast-scope",
        choices=["repository", "changed"],
        default="repository",
        help="AST extraction scope: full repository baseline or changed files only.",
    )
    parser.add_argument(
        "--max-ast-files",
        type=int,
        default=0,
        help="Optional cap for AST-scanned files; 0 means no cap.",
    )
    parser.add_argument(
        "--graph-output",
        default="",
        help="Optional output path for an AST summary graph image (PNG).",
    )
    parser.add_argument(
        "--graph-title",
        default="Remote Review AST Summary",
        help="Optional title used when rendering --graph-output.",
    )
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
        ast_scope=args.ast_scope,
        max_ast_files=args.max_ast_files,
    )
    payload = result.as_dict()
    if args.graph_output:
        payload["graph_image_path"] = draw_ast_summary_graph(
            ast_summary=result.ast_summary,
            output_path=args.graph_output,
            title=args.graph_title,
        )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

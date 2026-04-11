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
    ast_dump: list[dict[str, Any]]

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
            "ast_dump": self.ast_dump,
        }


def build_ast_summary_graph(ast_summary: list[dict[str, Any]]) -> nx.DiGraph:
    """Build a deterministic graph from AST summary payloads."""
    graph = nx.DiGraph()
    file_node_by_path: dict[str, str] = {}
    symbol_node_by_file_and_name: dict[tuple[str, str], str] = {}

    for item in sorted(ast_summary, key=lambda entry: str(entry.get("file", ""))):
        file_path = str(item.get("file", "unknown"))
        file_node = f"file:{file_path}"
        graph.add_node(file_node, node_type="file", file_path=file_path)
        file_node_by_path[file_path] = file_node

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
                symbol_node_by_file_and_name[(file_path, symbol_name)] = symbol_node
                graph.add_edge(file_node, symbol_node, edge_type="contains")
            continue

        if not isinstance(top_nodes, list):
            continue

        for index, node_type in enumerate(top_nodes):
            symbol_node = f"symbol:{file_path}:{index}:{node_type}"
            graph.add_node(symbol_node, node_type="symbol", symbol_type=str(node_type), file_path=file_path)
            graph.add_edge(file_node, symbol_node, edge_type="contains")

    module_to_file = _module_to_file_map(sorted(file_node_by_path.keys()))
    for item in sorted(ast_summary, key=lambda entry: str(entry.get("file", ""))):
        file_path = str(item.get("file", "unknown"))
        file_node = file_node_by_path.get(file_path)
        if file_node is None or item.get("error"):
            continue

        imports = item.get("imports")
        if not isinstance(imports, list):
            continue

        for import_entry in imports:
            if not isinstance(import_entry, dict):
                continue

            module_name = str(import_entry.get("module", "")).strip()
            if not module_name:
                continue

            module_node = f"module:{module_name}"
            if not graph.has_node(module_node):
                graph.add_node(module_node, node_type="module", module_name=module_name)

            graph.add_edge(file_node, module_node, edge_type="imports")

            target_file = _resolve_module_to_file(module_name, module_to_file)
            if target_file is not None:
                target_file_node = file_node_by_path.get(target_file)
                if target_file_node is not None:
                    graph.add_edge(file_node, target_file_node, edge_type="depends_on_file")

                imported_names = import_entry.get("names")
                if isinstance(imported_names, list):
                    for raw_name in imported_names:
                        symbol_name = str(raw_name).strip()
                        if not symbol_name:
                            continue
                        target_symbol_node = symbol_node_by_file_and_name.get((target_file, symbol_name))
                        if target_symbol_node is not None:
                            graph.add_edge(file_node, target_symbol_node, edge_type="imports_symbol")
                        else:
                            external_symbol_node = f"external_symbol:{module_name}.{symbol_name}"
                            if not graph.has_node(external_symbol_node):
                                graph.add_node(
                                    external_symbol_node,
                                    node_type="external_symbol",
                                    symbol_name=f"{module_name}.{symbol_name}",
                                )
                            graph.add_edge(file_node, external_symbol_node, edge_type="imports_symbol")

    return graph


def _module_to_file_map(file_paths: list[str]) -> dict[str, str]:
    mapping: dict[str, str] = {}
    for path in file_paths:
        normalized = path.replace("\\", "/")
        if normalized.endswith(".py"):
            module_name = normalized[:-3].replace("/", ".")
            mapping[module_name] = normalized

            stem = Path(normalized).stem
            if stem:
                mapping.setdefault(stem, normalized)

            if normalized.endswith("/__init__.py"):
                package_module = normalized[: -len("/__init__.py")].replace("/", ".")
                if package_module:
                    mapping[package_module] = normalized
    return mapping


def _resolve_module_to_file(module_name: str, module_to_file: dict[str, str]) -> str | None:
    if module_name in module_to_file:
        return module_to_file[module_name]

    prefixes = module_name.split(".")
    while len(prefixes) > 1:
        prefixes.pop()
        candidate = ".".join(prefixes)
        if candidate in module_to_file:
            return module_to_file[candidate]
    return None


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
        "    imports = []\n"
        "    for node in tree.body:\n"
        "        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):\n"
        "            symbols.append({'name': node.name, 'type': type(node).__name__})\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.Import):\n"
        "            for alias in node.names:\n"
        "                imports.append({'type': 'import', 'module': alias.name, 'names': [], 'level': 0})\n"
        "        elif isinstance(node, ast.ImportFrom):\n"
        "            imports.append({'type': 'from', 'module': node.module or '', 'names': [alias.name for alias in node.names], 'level': node.level})\n"
        "    total_nodes = sum(1 for _ in ast.walk(tree))\n"
        "    out.append({'file': rel, 'total_top_level_nodes': len(top), 'top_level_nodes': top[:20], 'total_ast_nodes': total_nodes, 'top_level_symbols': symbols[:50], 'imports': imports[:200]})\n"
        "print(json.dumps(out))"
    )

    output = sandbox.execute(["python", "-c", script, *file_paths], check_exit_code=True)
    payload = output.strip()
    if not payload:
        return []
    return json.loads(payload)


def collect_python_ast_dump(
    sandbox: RepoSandbox,
    file_paths: list[str],
    max_dump_chars: int = 0,
) -> list[dict[str, Any]]:
    if not file_paths:
        return []

    max_chars = max(0, max_dump_chars)
    script = (
        "import ast, json, pathlib, sys; "
        "out=[]; "
        "repo=pathlib.Path('/repo'); "
        "limit=int(sys.argv[1]); "
        "files=sys.argv[2:]; "
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
        "    formatted = ast.dump(tree, indent=2)\n"
        "    truncated = False\n"
        "    if limit > 0 and len(formatted) > limit:\n"
        "        formatted = formatted[:limit] + '\\n...<truncated>'\n"
        "        truncated = True\n"
        "    out.append({'file': rel, 'formatted_ast': formatted, 'truncated': truncated})\n"
        "print(json.dumps(out))"
    )

    output = sandbox.execute(["python", "-c", script, str(max_chars), *file_paths], check_exit_code=True)
    payload = output.strip()
    if not payload:
        return []
    return json.loads(payload)


def write_ast_dump_file(ast_dump: list[dict[str, Any]], output_path: str) -> str:
    output_file = Path(output_path)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(json.dumps(ast_dump, indent=2), encoding="utf-8")
    return str(output_file)


def run_remote_review_workflow(
    repo_url: str,
    head_commit: str,
    base_commit: Optional[str] = None,
    sandbox: Optional[RepoSandbox] = None,
    preflight_service: Optional[PreflightManifestService] = None,
    ast_scope: Literal["repository", "changed"] = "repository",
    max_ast_files: int = 0,
    include_ast_dump: bool = False,
    ast_dump_max_chars: int = 0,
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
        ast_dump = []
        if include_ast_dump:
            ast_dump = collect_python_ast_dump(
                sandbox=resolved_sandbox,
                file_paths=scanned_python_files,
                max_dump_chars=ast_dump_max_chars,
            )

        return RemoteReviewWorkflowResult(
            repo_url=repo_url,
            base_commit=resolved_base_commit,
            head_commit=head_commit,
            diff=diff,
            manifest=manifest,
            changed_python_files=changed_python_files,
            scanned_python_files=scanned_python_files,
            ast_summary=ast_summary,
            ast_dump=ast_dump,
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
        "--ast-dump-output",
        default="",
        help="Optional output path for formatted AST dump JSON.",
    )
    parser.add_argument(
        "--ast-dump-max-chars",
        type=int,
        default=0,
        help="Optional max characters per formatted AST entry; 0 means no cap.",
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
        include_ast_dump=bool(args.ast_dump_output),
        ast_dump_max_chars=args.ast_dump_max_chars,
    )
    payload = result.as_dict()
    if args.graph_output:
        payload["graph_image_path"] = draw_ast_summary_graph(
            ast_summary=result.ast_summary,
            output_path=args.graph_output,
            title=args.graph_title,
        )
    if args.ast_dump_output:
        payload["ast_dump_path"] = write_ast_dump_file(result.ast_dump, args.ast_dump_output)
        payload["ast_dump_file_count"] = len(result.ast_dump)
        payload.pop("ast_dump", None)
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

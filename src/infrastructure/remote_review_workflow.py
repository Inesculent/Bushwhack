import argparse
import dataclasses
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Optional

import networkx as nx

from src.config import get_settings
from src.domain.schemas import DiffManifest, PreflightRequest, RunMetadata
from src.infrastructure.preflight.service import PreflightManifestService
from src.infrastructure.sandbox import RepoSandbox
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.infrastructure.structural_topology import (
    apply_community_attributes,
    build_topology_summary,
    draw_topology_graph,
    run_structural_topology,
    write_topology_summary_json,
)


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
    structural_entities: dict[str, Any] = dataclasses.field(default_factory=dict)

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
            _add_graph_edge(
                graph=graph,
                source=file_node,
                target=error_node,
                edge_type="parse_error",
                source_file=file_path,
            )
            continue

        top_nodes = item.get("top_level_nodes")
        top_symbols = item.get("top_level_symbols")
        if isinstance(top_symbols, list) and top_symbols:
            for index, symbol in enumerate(top_symbols):
                if not isinstance(symbol, dict):
                    continue
                symbol_name = str(symbol.get("name", "unknown"))
                symbol_type = str(symbol.get("type", "symbol"))
                symbol_line = symbol.get("line")
                symbol_node = f"symbol:{file_path}:{index}:{symbol_name}"
                graph.add_node(
                    symbol_node,
                    node_type="symbol",
                    symbol_type=symbol_type,
                    symbol_name=symbol_name,
                    file_path=file_path,
                )
                symbol_node_by_file_and_name[(file_path, symbol_name)] = symbol_node
                _add_graph_edge(
                    graph=graph,
                    source=file_node,
                    target=symbol_node,
                    edge_type="contains",
                    source_file=file_path,
                    source_location=_line_ref(symbol_line),
                )
            continue

        if not isinstance(top_nodes, list):
            continue

        for index, node_type in enumerate(top_nodes):
            symbol_node = f"symbol:{file_path}:{index}:{node_type}"
            graph.add_node(symbol_node, node_type="symbol", symbol_type=str(node_type), file_path=file_path)
            _add_graph_edge(
                graph=graph,
                source=file_node,
                target=symbol_node,
                edge_type="contains",
                source_file=file_path,
            )

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

            source_location = _line_ref(import_entry.get("line"))
            _add_graph_edge(
                graph=graph,
                source=file_node,
                target=module_node,
                edge_type="imports",
                source_file=file_path,
                source_location=source_location,
            )

            target_file = _resolve_module_to_file(module_name, module_to_file)
            if target_file is not None:
                target_file_node = file_node_by_path.get(target_file)
                if target_file_node is not None:
                    _add_graph_edge(
                        graph=graph,
                        source=file_node,
                        target=target_file_node,
                        edge_type="depends_on_file",
                        source_file=file_path,
                        source_location=source_location,
                    )

                imported_names = import_entry.get("names")
                if isinstance(imported_names, list):
                    for raw_name in imported_names:
                        symbol_name = str(raw_name).strip()
                        if not symbol_name:
                            continue
                        target_symbol_node = symbol_node_by_file_and_name.get((target_file, symbol_name))
                        if target_symbol_node is not None:
                            _add_graph_edge(
                                graph=graph,
                                source=file_node,
                                target=target_symbol_node,
                                edge_type="imports_symbol",
                                source_file=file_path,
                                source_location=source_location,
                            )
                        else:
                            external_symbol_node = f"external_symbol:{module_name}.{symbol_name}"
                            if not graph.has_node(external_symbol_node):
                                graph.add_node(
                                    external_symbol_node,
                                    node_type="external_symbol",
                                    symbol_name=f"{module_name}.{symbol_name}",
                                )
                            _add_graph_edge(
                                graph=graph,
                                source=file_node,
                                target=external_symbol_node,
                                edge_type="imports_symbol",
                                source_file=file_path,
                                source_location=source_location,
                            )

    return graph


def _line_ref(line_number: Any) -> str:
    if isinstance(line_number, int) and line_number > 0:
        return f"L{line_number}"
    return "L?"


def _edge_payload(edge_type: str, source_file: str, source_location: str) -> dict[str, Any]:
    return {
        "edge_type": edge_type,
        "relation": edge_type,
        "provenance": "EXTRACTED",
        "confidence": "EXTRACTED",
        "source_ref": source_file,
        "source_file": source_file,
        "source_location": source_location,
        "weight": 1.0,
    }


def _add_graph_edge(
    graph: nx.DiGraph,
    source: str,
    target: str,
    edge_type: str,
    source_file: str,
    source_location: str = "L?",
) -> None:
    graph.add_edge(
        source,
        target,
        **_edge_payload(
            edge_type=edge_type,
            source_file=source_file,
            source_location=source_location,
        ),
    )


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
        "            symbols.append({'name': node.name, 'type': type(node).__name__, 'line': getattr(node, 'lineno', None)})\n"
        "    for node in ast.walk(tree):\n"
        "        if isinstance(node, ast.Import):\n"
        "            for alias in node.names:\n"
        "                imports.append({'type': 'import', 'module': alias.name, 'names': [], 'level': 0, 'line': getattr(node, 'lineno', None)})\n"
        "        elif isinstance(node, ast.ImportFrom):\n"
        "            imports.append({'type': 'from', 'module': node.module or '', 'names': [alias.name for alias in node.names], 'level': node.level, 'line': getattr(node, 'lineno', None)})\n"
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


# ---------------------------------------------------------------------------
# Sandbox-side structural entity extraction
# ---------------------------------------------------------------------------

_STRUCTURAL_EXTRACT_SCRIPT = r"""
import ast as stdlib_ast, json, os, pathlib, re, sys

REPO = pathlib.Path("/repo")

SUPPORTED = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb",
}
SKIP = {
    ".git", ".venv", "node_modules", "vendor", "third_party",
    "external", "deps", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
LANG_MAP = {
    ".py": "Python", ".js": "JavaScript", ".jsx": "JavaScript",
    ".ts": "TypeScript", ".tsx": "TypeScript", ".java": "Java",
    ".go": "Go", ".rs": "Rust", ".c": "C", ".h": "C",
    ".cpp": "C++", ".hpp": "C++", ".cs": "C#", ".php": "PHP", ".rb": "Ruby",
}
TS_LANG_MAP = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript",
    ".ts": "typescript", ".tsx": "tsx", ".java": "java",
    ".go": "go", ".rs": "rust", ".c": "c", ".h": "c",
    ".cpp": "cpp", ".hpp": "cpp", ".cs": "c_sharp", ".php": "php", ".rb": "ruby",
}
ENTITY_NODE_TYPES = {
    "function_definition", "method_definition", "class_definition",
    "function_declaration", "class_declaration", "interface_declaration",
    "enum_declaration", "struct_item", "impl_item",
}
IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)

have_ts = False
try:
    from tree_sitter import Node
    from tree_sitter_language_pack import get_parser
    have_ts = True
except Exception:
    pass


def _skip(rel):
    return bool(set(rel.split("/")) & SKIP)


def _node_name(node, src_bytes):
    for fn in ("name", "declarator"):
        n = node.child_by_field_name(fn)
        if n is not None:
            return src_bytes[n.start_byte:n.end_byte].decode("utf-8", errors="replace")
    for ch in node.children:
        if ch.type in {"identifier", "type_identifier", "property_identifier"}:
            return src_bytes[ch.start_byte:ch.end_byte].decode("utf-8", errors="replace")
    return f"{node.type}@{node.start_point[0]+1}"


def _node_is_entity(node):
    if node.type in ENTITY_NODE_TYPES:
        return True
    if node.child_by_field_name("name") is None:
        return False
    lt = node.type.lower()
    return any(t in lt for t in ("function", "method", "class", "interface", "enum", "struct"))


def _norm_type(nt):
    lt = nt.lower()
    if "class" in lt: return "class"
    if "method" in lt or "function" in lt: return "function"
    if "interface" in lt: return "interface"
    if "enum" in lt: return "enum"
    if "struct" in lt: return "struct"
    return "entity"


def _deps(src):
    return sorted({m.group(1) for m in IMPORT_RE.finditer(src)})


def _ts_entities(source, lang):
    parser = get_parser(lang)
    sb = source.encode("utf-8")
    tree = parser.parse(sb)
    lines = source.splitlines()
    ents = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        if not _node_is_entity(node):
            continue
        sl = node.start_point[0]
        sig = lines[sl].strip() if 0 <= sl < len(lines) else ""
        body = sb[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
        ents.append({
            "name": _node_name(node, sb),
            "type": _norm_type(node.type),
            "signature": sig,
            "body": body,
            "dependencies": _deps(body),
        })
    return ents


def _stdlib_entities(source):
    try:
        tree = stdlib_ast.parse(source)
    except Exception:
        return []
    ents = []
    for node in tree.body:
        if not isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef, stdlib_ast.ClassDef)):
            continue
        sl = getattr(node, "lineno", 1) - 1
        lines = source.splitlines()
        sig = lines[sl].strip() if 0 <= sl < len(lines) else ""
        el = getattr(node, "end_lineno", sl + 1)
        body = "\n".join(lines[sl:el])
        deps = []
        for child in stdlib_ast.walk(node):
            if isinstance(child, stdlib_ast.Import):
                for alias in child.names:
                    deps.append(alias.name)
            elif isinstance(child, stdlib_ast.ImportFrom):
                if child.module:
                    deps.append(child.module)
        ents.append({
            "name": node.name,
            "type": "class" if isinstance(node, stdlib_ast.ClassDef) else "function",
            "signature": sig,
            "body": body,
            "dependencies": sorted(set(deps)),
        })
    return ents


results = {"files": {}, "gaps": [], "file_languages": {}}
for p in sorted(REPO.rglob("*")):
    if not p.is_file():
        continue
    rel = p.relative_to(REPO).as_posix()
    if _skip(rel):
        continue
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        continue
    lang = LANG_MAP.get(ext)
    results["file_languages"][rel] = lang
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        results["gaps"].append({"filepath": rel, "reason": "read_failed", "detail": str(exc)})
        continue
    ts_lang = TS_LANG_MAP.get(ext)
    if have_ts and ts_lang:
        try:
            ents = _ts_entities(src, ts_lang)
            results["files"][rel] = ents
            continue
        except Exception:
            pass
    if ext == ".py":
        ents = _stdlib_entities(src)
        results["files"][rel] = ents
    else:
        results["gaps"].append({
            "filepath": rel,
            "reason": "no_parser_available",
            "detail": f"tree-sitter unavailable and no stdlib fallback for {ext}",
        })

print(json.dumps(results))
"""


def collect_structural_entities(
    sandbox: RepoSandbox,
) -> dict[str, Any]:
    """Run entity extraction inside the sandbox and return the parsed result.

    Returns a dict with keys ``files`` (path→entity list), ``gaps`` (list of
    gap dicts), and ``file_languages`` (path→language string).
    """
    output = sandbox.execute(
        ["python", "-c", _STRUCTURAL_EXTRACT_SCRIPT],
        check_exit_code=True,
    )
    payload = output.strip()
    if not payload:
        return {"files": {}, "gaps": [], "file_languages": {}}
    return json.loads(payload)


def run_structural_preflight_explore(
    sandbox_entities: dict[str, Any],
    *,
    structural_graph_json_path: str = "",
    structural_topology_json_path: str = "",
    topology_graph_output_path: str = "",
    topology_graph_title: str = "Structural topology",
    community_max_fraction: Optional[float] = None,
    community_min_split_size: Optional[int] = None,
    community_max_files: Optional[int] = None,
    community_max_symbols: Optional[int] = None,
    louvain_seed: Optional[int] = None,
) -> dict[str, Any]:
    """Build structural graph + optional topology from sandbox-extracted entities.

    ``sandbox_entities`` is the dict returned by :func:`collect_structural_entities`
    (keys: ``files``, ``gaps``, ``file_languages``).  All source reading happened
    inside the sandbox — this function only does graph construction and topology
    on the host.
    """
    from src.domain.schemas import CodeEntity, StructuralExtractionGap

    settings = get_settings()
    frac = settings.community_max_fraction if community_max_fraction is None else community_max_fraction
    min_split = settings.community_min_split_size if community_min_split_size is None else community_min_split_size
    max_f = settings.community_max_files if community_max_files is None else community_max_files
    max_sym = settings.community_max_symbols if community_max_symbols is None else community_max_symbols
    seed = settings.louvain_seed if louvain_seed is None else louvain_seed

    entities_by_file: dict[str, list[CodeEntity]] = {}
    for filepath, raw_entities in sandbox_entities.get("files", {}).items():
        entities_by_file[filepath] = [CodeEntity.model_validate(e) for e in raw_entities]

    extraction_gaps = [
        StructuralExtractionGap.model_validate(g)
        for g in sandbox_entities.get("gaps", [])
    ]
    file_languages: dict[str, str | None] = sandbox_entities.get("file_languages", {})

    build_result = StructuralGraphBuilder.build_from_entities(
        entities_by_file=entities_by_file,
        file_languages=file_languages,
        extraction_gaps=extraction_gaps,
    )

    payload: dict[str, Any] = {
        "structural_node_count": build_result.graph.number_of_nodes(),
        "structural_edge_count": build_result.graph.number_of_edges(),
        "structural_files_attempted": build_result.files_attempted,
        "structural_files_parsed": build_result.files_parsed,
        "structural_extraction_gaps": [gap.model_dump() for gap in build_result.gaps],
    }

    need_topology = bool(structural_topology_json_path or topology_graph_output_path)
    topology_summary = None
    topo = None
    if need_topology and build_result.graph.number_of_nodes() > 0:
        topo = run_structural_topology(
            build_result.graph,
            max_fraction=frac,
            min_split_size=min_split,
            max_files=max_f,
            max_symbols=max_sym,
            louvain_seed=seed,
        )
        apply_community_attributes(build_result.graph, topo.partition)
        config_snapshot = {
            "community_max_fraction": frac,
            "community_min_split_size": min_split,
            "community_max_files": max_f,
            "community_max_symbols": max_sym,
            "louvain_seed": seed,
        }
        topology_summary = build_topology_summary(topo, build_result.graph, config_snapshot)
        payload["topology_algorithm"] = topology_summary.algorithm
        payload["topology_community_count"] = topology_summary.community_count
        payload["topology_splits_applied"] = topology_summary.splits_applied

    graph_payload = StructuralGraphBuilder.serialize(build_result.graph)

    if structural_graph_json_path:
        out_path = Path(structural_graph_json_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(graph_payload, indent=2), encoding="utf-8")
        payload["structural_graph_json_path"] = str(out_path.resolve())

    if structural_topology_json_path and topology_summary is not None:
        payload["structural_topology_json_path"] = write_topology_summary_json(
            topology_summary, structural_topology_json_path
        )

    if topology_graph_output_path and topo is not None:
        payload["topology_graph_image_path"] = draw_topology_graph(
            topo.clustering_graph,
            topo.partition,
            topology_graph_output_path,
            title=topology_graph_title,
        )

    return payload


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
    include_structural_entities: bool = False,
) -> "RemoteReviewWorkflowResult":
    """Run the remote sandbox workflow.

    When *sandbox* is provided by the caller the sandbox is **not** stopped on
    return — the caller owns the lifecycle and may run additional sandbox
    commands (e.g. structural entity extraction) before stopping it.

    When *sandbox* is ``None`` an internal sandbox is created and stopped in
    the ``finally`` block unless ``include_structural_entities`` is ``True``,
    in which case the caller must stop the returned sandbox via
    ``result.sandbox.stop()``.
    """
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
        ast_dump: list[dict[str, Any]] = []
        if include_ast_dump:
            ast_dump = collect_python_ast_dump(
                sandbox=resolved_sandbox,
                file_paths=scanned_python_files,
                max_dump_chars=ast_dump_max_chars,
            )

        structural_entities: dict[str, Any] = {}
        if include_structural_entities:
            structural_entities = collect_structural_entities(resolved_sandbox)

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
            structural_entities=structural_entities,
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
    parser.add_argument(
        "--structural-graph-json",
        default="",
        help="Optional path for StructuralGraphBuilder node-link JSON (directed graph).",
    )
    parser.add_argument(
        "--structural-topology-json",
        default="",
        help="Optional path for community partition + cohesion JSON (runs topology pass).",
    )
    parser.add_argument(
        "--topology-graph-output",
        default="",
        help="Optional PNG path for clustering-graph layout colored by community.",
    )
    parser.add_argument(
        "--topology-graph-title",
        default="Structural topology",
        help="Title for --topology-graph-output.",
    )
    parser.add_argument(
        "--community-max-fraction",
        type=float,
        default=None,
        help="Override REVIEW_community_max_fraction for topology (default: from settings).",
    )
    parser.add_argument(
        "--community-min-split-size",
        type=int,
        default=None,
        help="Override REVIEW_community_min_split_size for topology.",
    )
    parser.add_argument(
        "--community-max-files",
        type=int,
        default=None,
        help="Override REVIEW_community_max_files for topology.",
    )
    parser.add_argument(
        "--community-max-symbols",
        type=int,
        default=None,
        help="Override REVIEW_community_max_symbols for topology.",
    )
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    if not args.repo_url or not args.head_commit:
        raise SystemExit(
            "Missing required values. Provide --repo-url and --head-commit, "
            "or set SANDBOX_REMOTE_TEST_URL and SANDBOX_REMOTE_TEST_HEAD."
        )

    need_structural = any(
        [
            bool(args.structural_graph_json),
            bool(args.structural_topology_json),
            bool(args.topology_graph_output),
        ]
    )

    result = run_remote_review_workflow(
        repo_url=args.repo_url,
        head_commit=args.head_commit,
        base_commit=args.base_commit or None,
        ast_scope=args.ast_scope,
        max_ast_files=args.max_ast_files,
        include_ast_dump=bool(args.ast_dump_output),
        ast_dump_max_chars=args.ast_dump_max_chars,
        include_structural_entities=need_structural,
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

    if need_structural:
        explore = run_structural_preflight_explore(
            sandbox_entities=result.structural_entities,
            structural_graph_json_path=args.structural_graph_json,
            structural_topology_json_path=args.structural_topology_json,
            topology_graph_output_path=args.topology_graph_output,
            topology_graph_title=args.topology_graph_title,
            community_max_fraction=args.community_max_fraction,
            community_min_split_size=args.community_min_split_size,
            community_max_files=args.community_max_files,
            community_max_symbols=args.community_max_symbols,
        )
        payload.update(explore)

    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()

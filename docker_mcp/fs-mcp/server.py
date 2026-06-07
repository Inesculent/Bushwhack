import re
import sys
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from mcp.server.fastmcp import FastMCP
from tree_sitter import Node
from tree_sitter_language_pack import get_parser


logging.basicConfig(stream=sys.stderr, level=logging.INFO)
logger = logging.getLogger("fs-mcp")

mcp = FastMCP("Filesystem-AST-Server")

LANGUAGE_BY_EXTENSION = {
    ".py": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".java": "java",
    ".go": "go",
    ".rs": "rust",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".hpp": "cpp",
    ".cs": "c_sharp",
    ".php": "php",
    ".rb": "ruby",
}

ENTITY_NODE_TYPES = {
    "function_definition",
    "method_definition",
    "class_definition",
    "function_declaration",
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "struct_item",
    "impl_item",
}

IMPORT_PATTERN = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_\.]+)", re.MULTILINE)

SKIP_PATH_SEGMENTS = {
    ".git",
    ".venv",
    "node_modules",
    "vendor",
    "third_party",
    "external",
    "deps",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def _safe_file_read(repository_path: str, file_path: str) -> str:
    repo_root = Path(repository_path).resolve()
    target_path = (repo_root / file_path).resolve()

    try:
        target_path.relative_to(repo_root)
    except ValueError as exc:
        raise ValueError("file_path must be inside repository_path") from exc

    if not target_path.is_file():
        raise FileNotFoundError(f"File not found: {file_path}")

    return target_path.read_text(encoding="utf-8", errors="replace")


def _detect_language(file_path: str) -> str:
    suffix = Path(file_path).suffix.lower()
    if suffix in LANGUAGE_BY_EXTENSION:
        return LANGUAGE_BY_EXTENSION[suffix]
    raise ValueError(f"Unsupported file extension for AST parsing: {suffix or '<none>'}")


def _node_name(node: Node, source_bytes: bytes) -> str:
    for field_name in ("name", "declarator"):
        named_node = node.child_by_field_name(field_name)
        if named_node is not None:
            return source_bytes[named_node.start_byte : named_node.end_byte].decode("utf-8", errors="replace")

    for child in node.children:
        if child.type in {"identifier", "type_identifier", "property_identifier"}:
            return source_bytes[child.start_byte : child.end_byte].decode("utf-8", errors="replace")

    return f"{node.type}@{node.start_point[0] + 1}"


def _node_is_entity(node: Node) -> bool:
    if node.type in ENTITY_NODE_TYPES:
        return True

    if node.child_by_field_name("name") is None:
        return False

    lower_type = node.type.lower()
    return any(token in lower_type for token in ("function", "method", "class", "interface", "enum", "struct"))


def _normalize_entity_type(node_type: str) -> str:
    lowered = node_type.lower()
    if "class" in lowered:
        return "class"
    if "method" in lowered or "function" in lowered:
        return "function"
    if "interface" in lowered:
        return "interface"
    if "enum" in lowered:
        return "enum"
    if "struct" in lowered:
        return "struct"
    return "entity"


def _extract_dependencies(source: str) -> List[str]:
    deps = {match.group(1) for match in IMPORT_PATTERN.finditer(source)}
    return sorted(deps)


def _collect_entities(source: str, language: str) -> List[Dict[str, Any]]:
    parser = get_parser(language)
    source_bytes = source.encode("utf-8")
    tree = parser.parse(source_bytes)
    lines = source.splitlines()

    entities: List[Dict[str, Any]] = []
    stack: List[Node] = [tree.root_node]

    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))

        if not _node_is_entity(node):
            continue

        start_line = node.start_point[0]
        signature = lines[start_line].strip() if 0 <= start_line < len(lines) else ""
        body = source_bytes[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

        entities.append(
            {
                "name": _node_name(node, source_bytes),
                "type": _normalize_entity_type(node.type),
                "signature": signature,
                "body": body,
                "dependencies": _extract_dependencies(body),
                "definition_line": int(start_line) + 1,
            }
        )

    return entities


@mcp.tool()
def parse_file(repository_path: str, file_path: str) -> Dict[str, Any]:
    """Parse a repository-relative file and return AST-derived entities."""
    logger.info("Parsing file '%s' in '%s'", file_path, repository_path)

    source = _safe_file_read(repository_path=repository_path, file_path=file_path)
    language = _detect_language(file_path=file_path)
    entities = _collect_entities(source=source, language=language)

    return {
        "file_path": file_path.replace("\\", "/"),
        "language": language,
        "entities": entities,
    }


@mcp.tool()
def get_entity_details(repository_path: str, file_path: str, entity_name: str) -> Dict[str, Any]:
    """Return one entity from a parsed file by exact or suffix match."""
    parsed = parse_file(repository_path=repository_path, file_path=file_path)

    for entity in parsed["entities"]:
        candidate = entity["name"]
        if candidate == entity_name or candidate.endswith(f".{entity_name}"):
            return {"entity": entity}

    return {"entity": None}


def _symbol_matches(entity_name: str, symbol_name: str) -> bool:
    if entity_name == symbol_name:
        return True
    if entity_name.endswith(f".{symbol_name}"):
        return True
    if "." in symbol_name:
        return _symbol_matches(entity_name, symbol_name.split(".")[-1])
    return False


def _iter_repo_relative_paths(
    repository_path: str,
    candidate_file_paths: Optional[List[str]],
    *,
    max_files: int = 8000,
) -> List[str]:
    root = Path(repository_path).resolve()
    if not root.is_dir():
        return []

    extensions = set(LANGUAGE_BY_EXTENSION.keys())
    collected: List[str] = []

    if candidate_file_paths:
        for raw in candidate_file_paths:
            normalized = raw.replace("\\", "/").lstrip("/")
            if not normalized or ".." in normalized.split("/"):
                continue
            target = (root / normalized).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                continue
            if target.is_file() and target.suffix.lower() in extensions:
                collected.append(normalized.replace("\\", "/"))
        return collected

    count = 0
    for path in root.rglob("*"):
        if count >= max_files:
            break
        if not path.is_file():
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.suffix.lower() not in extensions:
            continue
        if any(seg in SKIP_PATH_SEGMENTS for seg in path.parts):
            continue
        collected.append(path.relative_to(root).as_posix())
        count += 1
    collected.sort()
    return collected


@mcp.tool()
def find_symbol_definitions(
    repository_path: str,
    symbol_name: str,
    candidate_file_paths: Optional[List[str]] = None,
    max_results: int = 50,
) -> Dict[str, Any]:
    """Search a repository checkout for AST entities whose name matches ``symbol_name``."""
    logger.info("find_symbol_definitions %r in %s", symbol_name, repository_path)
    definitions: List[Dict[str, Any]] = []
    seen: Set[Tuple[str, int, str]] = set()

    for rel_path in _iter_repo_relative_paths(repository_path, candidate_file_paths):
        if len(definitions) >= max_results:
            break
        try:
            source = _safe_file_read(repository_path=repository_path, file_path=rel_path)
            language = _detect_language(rel_path)
        except (OSError, FileNotFoundError, ValueError):
            continue
        for entity in _collect_entities(source=source, language=language):
            if not _symbol_matches(str(entity.get("name", "")), symbol_name):
                continue
            line_start = int(entity.get("definition_line") or 1)
            name = str(entity.get("name", ""))
            key = (rel_path.replace("\\", "/"), line_start, name)
            if key in seen:
                continue
            seen.add(key)
            definitions.append(
                {
                    "file_path": rel_path.replace("\\", "/"),
                    "line_start": line_start,
                    "entity_name": name,
                    "entity_type": str(entity.get("type", "unknown")),
                    "signature": str(entity.get("signature", "")),
                    "source": "mcp",
                }
            )
            if len(definitions) >= max_results:
                break

    return {"definitions": definitions, "symbol_name": symbol_name}


if __name__ == "__main__":
    logger.info("Starting filesystem AST MCP server")
    mcp.run(transport="stdio")

"""AST entity extraction inside the review Docker sandbox (no host repo checkout)."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Sequence

from src.domain.schemas import CodeEntity
from src.infrastructure.sandbox import RepoSandbox

# Bounded per-file extraction (tree-sitter or stdlib ast) for explicit paths only.
_SANDBOX_FILE_ENTITIES_SCRIPT = r"""
import ast as stdlib_ast, json, pathlib, re, sys

REPO = pathlib.Path("/repo")
PATHS = json.loads(sys.argv[1])
MAX_BODY_CHARS = int(sys.argv[2]) if len(sys.argv) > 2 else 12000

SUPPORTED = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs",
    ".c", ".h", ".cpp", ".hpp", ".cs", ".php", ".rb",
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
    from tree_sitter_language_pack import get_parser
    have_ts = True
except Exception:
    pass


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
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n...<truncated>"
        ents.append({
            "name": _node_name(node, sb),
            "type": _norm_type(node.type),
            "signature": sig,
            "body": body,
            "dependencies": _deps(body),
            "definition_line": sl + 1,
        })
    return ents


def _stdlib_entities(source):
    try:
        tree = stdlib_ast.parse(source)
    except Exception:
        return []
    ents = []
    lines = source.splitlines()
    for node in tree.body:
        if not isinstance(node, (stdlib_ast.FunctionDef, stdlib_ast.AsyncFunctionDef, stdlib_ast.ClassDef)):
            continue
        sl = getattr(node, "lineno", 1) - 1
        sig = lines[sl].strip() if 0 <= sl < len(lines) else ""
        el = getattr(node, "end_lineno", sl + 1)
        body = "\n".join(lines[sl:el])
        if len(body) > MAX_BODY_CHARS:
            body = body[:MAX_BODY_CHARS] + "\n...<truncated>"
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
            "definition_line": sl + 1,
        })
    return ents


results = {"files": {}, "gaps": []}
for rel in PATHS:
    rel = str(rel).replace("\\", "/").lstrip("/")
    if not rel or ".." in rel.split("/"):
        results["gaps"].append({"filepath": rel, "reason": "invalid_path", "detail": "path rejected"})
        continue
    p = REPO / rel
    if not p.is_file():
        results["gaps"].append({"filepath": rel, "reason": "not_found", "detail": "file missing in /repo"})
        continue
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        results["gaps"].append({
            "filepath": rel,
            "reason": "unsupported_extension",
            "detail": f"no parser for {ext}",
        })
        continue
    try:
        src = p.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        results["gaps"].append({"filepath": rel, "reason": "read_failed", "detail": str(exc)})
        continue
    ts_lang = TS_LANG_MAP.get(ext)
    ents = []
    if have_ts and ts_lang:
        try:
            ents = _ts_entities(src, ts_lang)
        except Exception:
            ents = []
    if not ents and ext == ".py":
        ents = _stdlib_entities(src)
    if ents:
        results["files"][rel] = ents
    else:
        results["gaps"].append({
            "filepath": rel,
            "reason": "parse_failed",
            "detail": "no entities extracted",
        })

print(json.dumps(results))
"""


def collect_sandbox_file_entities(
    sandbox: RepoSandbox,
    file_paths: Sequence[str],
    *,
    max_body_chars: int = 12_000,
) -> dict[str, Any]:
    """Extract entities for specific files inside the sandbox; returns JSON-shaped dict."""
    normalized = [
        str(p).replace("\\", "/").lstrip("/")
        for p in file_paths
        if isinstance(p, str) and str(p).strip() and ".." not in str(p).split("/")
    ]
    if not normalized:
        return {"files": {}, "gaps": []}

    paths_json = json.dumps(normalized)
    output = sandbox.execute(
        ["python", "-c", _SANDBOX_FILE_ENTITIES_SCRIPT, paths_json, str(max(256, int(max_body_chars)))],
        workdir="/repo",
        check_exit_code=False,
    )
    payload = (output or "").strip()
    if not payload:
        return {"files": {}, "gaps": [{"filepath": "*", "reason": "empty_output", "detail": "sandbox AST script returned nothing"}]}
    try:
        return json.loads(payload)
    except json.JSONDecodeError as exc:
        decoder = json.JSONDecoder()
        for idx, char in enumerate(payload):
            if char != "{":
                continue
            try:
                recovered, _ = decoder.raw_decode(payload[idx:])
            except json.JSONDecodeError:
                continue
            if isinstance(recovered, dict) and (
                isinstance(recovered.get("files"), dict) or isinstance(recovered.get("gaps"), list)
            ):
                return recovered
        return {
            "files": {},
            "gaps": [{"filepath": "*", "reason": "invalid_json", "detail": f"{exc.__class__.__name__}: {exc}"}],
        }


def entities_from_sandbox_payload(payload: dict[str, Any]) -> Dict[str, List[CodeEntity]]:
    """Deserialize sandbox AST JSON into CodeEntity lists per file."""
    out: Dict[str, List[CodeEntity]] = {}
    files = payload.get("files") or {}
    if not isinstance(files, dict):
        return out
    for filepath, raw_entities in files.items():
        if not isinstance(raw_entities, list):
            continue
        entities: List[CodeEntity] = []
        for item in raw_entities:
            if not isinstance(item, dict):
                continue
            try:
                entities.append(CodeEntity.model_validate(item))
            except Exception:
                continue
        if entities:
            out[str(filepath)] = entities
    return out

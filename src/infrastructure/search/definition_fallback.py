"""Best-effort definition lookup without AST (regex over repository files)."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Pattern, Sequence, Set

from src.domain.schemas import SymbolDefinition

_SKIP_SEGMENTS = {
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

_SUFFIXES = {".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".java", ".go", ".rs", ".rb", ".php"}


def _patterns_for_symbol(symbol_name: str) -> Dict[str, Pattern[str]]:
    """Language-keyed regex patterns matching likely definition lines."""
    escaped = re.escape(symbol_name)
    return {
        "python": re.compile(rf"^(\s*)(?:async\s+)?def\s+{escaped}\s*\(|^(\s*)class\s+{escaped}\s*[\(:]"),
        "js_ts": re.compile(
            rf"^(\s*)(?:export\s+)?(?:async\s+)?function\s+{escaped}\s*\(|"
            rf"^(\s*)(?:export\s+)?(?:async\s+)?const\s+{escaped}\s*=|"
            rf"^(\s*)(?:export\s+)?(?:async\s+)?let\s+{escaped}\s*=|"
            rf"^(\s*)(?:export\s+)?class\s+{escaped}\s*[\{{<]"
        ),
        "java": re.compile(rf"^(\s*)(?:public|private|protected)?.*\b(?:class|interface|enum)\s+{escaped}\b"),
        "go": re.compile(rf"^(\s*)func\s+(?:\([^)]*\)\s*)?{escaped}\s*\("),
        "ruby": re.compile(rf"^(\s*)def\s+(?:self\.)?{escaped}\b"),
        "rust": re.compile(rf"^(\s*)(?:pub\s+)?(?:async\s+)?fn\s+{escaped}\s*\("),
        "php": re.compile(rf"^(\s*)(?:function\s+{escaped}\s*\(|class\s+{escaped}\b)"),
    }


def _language_for_suffix(suffix: str) -> str | None:
    s = suffix.lower()
    if s in {".py", ".pyi"}:
        return "python"
    if s in {".js", ".jsx", ".ts", ".tsx"}:
        return "js_ts"
    if s == ".java":
        return "java"
    if s == ".go":
        return "go"
    if s == ".rb":
        return "ruby"
    if s == ".rs":
        return "rust"
    if s == ".php":
        return "php"
    return None


def find_symbol_definitions_regex(
    repository_path: str,
    symbol_name: str,
    *,
    candidate_file_paths: Sequence[str] | None = None,
    max_results: int = 50,
    max_files_scanned: int = 8000,
) -> List[SymbolDefinition]:
    """Scan text files with definition-like regexes (fallback when AST is unavailable)."""
    root = Path(repository_path).resolve()
    if not root.is_dir() or not symbol_name.strip():
        return []

    patterns = _patterns_for_symbol(symbol_name.strip())
    results: List[SymbolDefinition] = []
    seen: Set[tuple[str, int, str]] = set()
    scanned = 0

    def handle_line(rel: str, line_no: int, line: str, lang: str) -> None:
        pat = patterns.get(lang)
        if pat is None or not pat.search(line):
            return
        key = (rel, line_no, symbol_name)
        if key in seen:
            return
        seen.add(key)
        etype = "function"
        if lang == "python" and "class" in line:
            etype = "class"
        if lang == "js_ts" and "class" in line:
            etype = "class"
        if lang == "java" and "interface" in line:
            etype = "interface"
        results.append(
            SymbolDefinition(
                file_path=rel,
                line_start=line_no,
                entity_name=symbol_name,
                entity_type=etype,
                signature=line.strip()[:500],
                source="regex",
            )
        )

    if candidate_file_paths:
        for raw in candidate_file_paths:
            normalized = raw.replace("\\", "/").lstrip("/")
            if not normalized or ".." in normalized.split("/"):
                continue
            path = (root / normalized).resolve()
            try:
                path.relative_to(root)
            except ValueError:
                continue
            if not path.is_file():
                continue
            lang = _language_for_suffix(path.suffix)
            if lang is None:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            rel = path.relative_to(root).as_posix()
            for idx, line in enumerate(text.splitlines(), start=1):
                handle_line(rel, idx, line, lang)
                if len(results) >= max_results:
                    return results
        return results

    for path in root.rglob("*"):
        if scanned >= max_files_scanned or len(results) >= max_results:
            break
        if not path.is_file():
            continue
        if any(seg in _SKIP_SEGMENTS for seg in path.parts):
            continue
        if path.suffix.lower() not in _SUFFIXES:
            continue
        lang = _language_for_suffix(path.suffix)
        if lang is None:
            continue
        try:
            path.relative_to(root)
        except ValueError:
            continue
        scanned += 1
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(root).as_posix()
        for idx, line in enumerate(text.splitlines(), start=1):
            handle_line(rel, idx, line, lang)
            if len(results) >= max_results:
                return results

    return results

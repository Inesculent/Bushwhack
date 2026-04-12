from dataclasses import dataclass
from hashlib import sha1
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Set

import networkx as nx

from src.domain.interfaces import IASTParser
from src.domain.schemas import CodeEntity, DiffFileManifestEntry, DiffManifest, StructuralExtractionGap


@dataclass(frozen=True)
class StructuralBuildResult:
    graph: nx.DiGraph
    gaps: List[StructuralExtractionGap]
    files_attempted: int
    files_parsed: int


class StructuralGraphBuilder:
    """Build deterministic structural graph snapshots from repository source."""

    _SUPPORTED_EXTENSIONS: Set[str] = {
        ".py",
        ".js",
        ".jsx",
        ".ts",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".c",
        ".h",
        ".cpp",
        ".hpp",
        ".cs",
        ".php",
        ".rb",
    }

    _SKIPPED_SEGMENTS: Set[str] = {
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

    _LANGUAGE_BY_EXTENSION: Dict[str, str] = {
        ".py": "Python",
        ".js": "JavaScript",
        ".jsx": "JavaScript",
        ".ts": "TypeScript",
        ".tsx": "TypeScript",
        ".java": "Java",
        ".go": "Go",
        ".rs": "Rust",
        ".c": "C",
        ".h": "C",
        ".cpp": "C++",
        ".hpp": "C++",
        ".cs": "C#",
        ".php": "PHP",
        ".rb": "Ruby",
    }

    _CALL_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
    _CLASS_BASE_PATTERN = re.compile(r"class\s+[A-Za-z_][A-Za-z0-9_]*\s*\(([^)]*)\)")
    _EXTENDS_PATTERN = re.compile(r"\bextends\s+([A-Za-z_][A-Za-z0-9_\.]*)")
    _IDENTIFIER_PATTERN = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\b")
    _RESERVED_WORDS: Set[str] = {
        "if",
        "for",
        "while",
        "return",
        "def",
        "class",
        "with",
        "lambda",
        "print",
        "len",
        "range",
        "and",
        "or",
        "not",
        "try",
        "except",
        "finally",
        "raise",
        "assert",
        "await",
        "async",
        "switch",
        "case",
        "new",
        "super",
        "this",
        "self",
    }

    def __init__(self, ast_parser: IASTParser | None) -> None:
        self.ast_parser = ast_parser

    # ------------------------------------------------------------------
    # Public entry-points
    # ------------------------------------------------------------------

    def build(
        self,
        manifest: DiffManifest,
        repository_path: str,
    ) -> StructuralBuildResult:
        """Extract entities via IASTParser from a local path, then build the graph.

        Used by the LangGraph orchestrator path where the host already has the
        repo (bind-mounted read-only via ``RepoSandbox.start``).
        """
        entries = self._repository_entries(manifest=manifest, repository_path=repository_path)
        gaps: List[StructuralExtractionGap] = []
        entities_by_file: Dict[str, List[CodeEntity]] = {}

        if self.ast_parser is None:
            for entry in entries:
                gaps.append(
                    StructuralExtractionGap(
                        filepath=entry.filepath,
                        reason="ast_parser_unavailable",
                        detail="AST parser is disabled or unavailable; extraction degraded.",
                    )
                )
            return self.build_from_entities(
                entities_by_file={},
                file_languages={e.filepath: e.language for e in entries},
                extraction_gaps=gaps,
            )

        files_parsed = 0
        for entry in entries:
            try:
                entities = self.ast_parser.get_file_structure(repository_path=repository_path, file_path=entry.filepath)
            except Exception as exc:
                gaps.append(
                    StructuralExtractionGap(
                        filepath=entry.filepath,
                        reason="ast_parse_failed",
                        detail=f"{exc.__class__.__name__}: {exc}",
                    )
                )
                continue

            entities_by_file[entry.filepath] = entities
            files_parsed += 1

        file_languages = {e.filepath: e.language for e in entries}
        return self.build_from_entities(
            entities_by_file=entities_by_file,
            file_languages=file_languages,
            extraction_gaps=gaps,
        )

    @classmethod
    def build_from_entities(
        cls,
        entities_by_file: Dict[str, List[CodeEntity]],
        file_languages: Optional[Dict[str, Optional[str]]] = None,
        extraction_gaps: Optional[List[StructuralExtractionGap]] = None,
    ) -> StructuralBuildResult:
        """Build the structural graph from pre-extracted entities (no filesystem access).

        This is the host-side entrypoint for the remote sandbox workflow: entities
        are extracted inside the sandbox container; the resulting dicts are
        deserialized into ``CodeEntity`` objects and passed here.
        """
        resolved_languages = file_languages or {}
        gaps: List[StructuralExtractionGap] = list(extraction_gaps or [])

        graph = nx.DiGraph()
        symbol_node_by_name: Dict[str, str] = {}

        all_file_paths = sorted(
            set(entities_by_file.keys())
            | set(resolved_languages.keys())
        )
        for filepath in all_file_paths:
            graph.add_node(
                cls._file_node_id(filepath),
                node_type="file",
                file_path=filepath,
                language=resolved_languages.get(filepath),
            )

        for filepath in sorted(entities_by_file):
            file_node_id = cls._file_node_id(filepath)
            for entity in sorted(entities_by_file[filepath], key=lambda item: (item.name, item.signature, item.type)):
                symbol_node_id = cls._symbol_node_id(filepath, entity.name, entity.signature)
                symbol_node_by_name[entity.name] = symbol_node_id
                graph.add_node(
                    symbol_node_id,
                    node_type="symbol",
                    file_path=filepath,
                    symbol_name=entity.name,
                    symbol_type=entity.type,
                    signature=entity.signature,
                )
                graph.add_edge(
                    file_node_id,
                    symbol_node_id,
                    **cls._edge_payload(
                        edge_type="defines",
                        source_file=filepath,
                    ),
                )

                for dependency in sorted(set(entity.dependencies)):
                    module_node_id = cls._module_node_id(dependency)
                    graph.add_node(
                        module_node_id,
                        node_type="module",
                        module_name=dependency,
                    )
                    graph.add_edge(
                        symbol_node_id,
                        module_node_id,
                        **cls._edge_payload(
                            edge_type="imports",
                            source_file=filepath,
                        ),
                    )

        for file_path in sorted(entities_by_file):
            source_ref = file_path
            for entity in sorted(entities_by_file[file_path], key=lambda item: (item.name, item.signature, item.type)):
                symbol_node_id = symbol_node_by_name.get(entity.name)
                if symbol_node_id is None:
                    continue

                for call_target in cls._extract_call_targets(entity.body):
                    target_node = cls._resolve_symbol_target(call_target, symbol_node_by_name)
                    cls._add_structural_edge(
                        graph=graph,
                        source=symbol_node_id,
                        target=target_node,
                        edge_type="calls",
                        source_file=source_ref,
                    )

                for base_name in cls._extract_base_types(entity.signature):
                    target_node = cls._resolve_symbol_target(base_name, symbol_node_by_name)
                    cls._add_structural_edge(
                        graph=graph,
                        source=symbol_node_id,
                        target=target_node,
                        edge_type="inherits",
                        source_file=source_ref,
                    )

                existing_call_targets = cls._existing_targets_by_edge_type(
                    graph=graph,
                    source_node=symbol_node_id,
                    edge_type="calls",
                )
                existing_inherit_targets = cls._existing_targets_by_edge_type(
                    graph=graph,
                    source_node=symbol_node_id,
                    edge_type="inherits",
                )
                for reference_name in cls._extract_reference_targets(
                    body=entity.body,
                    known_symbols=set(symbol_node_by_name.keys()),
                    source_symbol=entity.name,
                ):
                    target_node = symbol_node_by_name.get(reference_name)
                    if target_node is None:
                        continue
                    if target_node in existing_call_targets or target_node in existing_inherit_targets:
                        continue
                    cls._add_structural_edge(
                        graph=graph,
                        source=symbol_node_id,
                        target=target_node,
                        edge_type="references",
                        source_file=source_ref,
                    )

        files_with_entities = len(entities_by_file)
        return StructuralBuildResult(
            graph=graph,
            gaps=gaps,
            files_attempted=len(all_file_paths),
            files_parsed=files_with_entities,
        )

    @staticmethod
    def serialize(graph: nx.DiGraph) -> Dict[str, Any]:
        payload = nx.node_link_data(graph, edges="edges")
        payload["nodes"] = sorted(payload.get("nodes", []), key=lambda item: str(item.get("id", "")))
        payload["edges"] = sorted(
            payload.get("edges", []),
            key=lambda item: (
                str(item.get("source", "")),
                str(item.get("target", "")),
                str(item.get("edge_type", "")),
                str(item.get("source_file", "")),
            ),
        )
        return payload

    @staticmethod
    def deserialize(payload: Dict[str, Any]) -> nx.DiGraph:
        return nx.node_link_graph(payload, edges="edges")

    @classmethod
    def _repository_entries(cls, manifest: DiffManifest, repository_path: str) -> List[DiffFileManifestEntry]:
        manifest_entries = {
            entry.filepath: entry
            for entry in manifest.files
            if entry.change_type in {"A", "M", "R"} and not entry.is_binary
        }

        repo_root = Path(repository_path)
        discovered: Dict[str, DiffFileManifestEntry] = {}

        if repo_root.is_dir():
            for candidate in sorted(repo_root.rglob("*")):
                if not candidate.is_file():
                    continue

                relative_path = candidate.relative_to(repo_root).as_posix()
                if cls._is_skipped_path(relative_path):
                    continue
                if not cls._is_supported_ast_file(relative_path):
                    continue

                existing = manifest_entries.get(relative_path)
                if existing is not None:
                    discovered[relative_path] = existing
                else:
                    discovered[relative_path] = DiffFileManifestEntry(
                        filepath=relative_path,
                        change_type="M",
                        language=cls._language_from_extension(relative_path),
                    )

        for path, entry in manifest_entries.items():
            if path not in discovered and cls._is_supported_ast_file(path):
                discovered[path] = entry

        return sorted(discovered.values(), key=lambda item: item.filepath)

    @classmethod
    def _is_skipped_path(cls, filepath: str) -> bool:
        segments = set(filepath.split("/"))
        return any(segment in cls._SKIPPED_SEGMENTS for segment in segments)

    @classmethod
    def _is_supported_ast_file(cls, filepath: str) -> bool:
        return Path(filepath).suffix.lower() in cls._SUPPORTED_EXTENSIONS

    @classmethod
    def _language_from_extension(cls, filepath: str) -> str | None:
        return cls._LANGUAGE_BY_EXTENSION.get(Path(filepath).suffix.lower())

    @staticmethod
    def _file_node_id(filepath: str) -> str:
        return f"file:{filepath}"

    @staticmethod
    def _module_node_id(module_name: str) -> str:
        return f"module:{module_name}"

    @staticmethod
    def _symbol_node_id(filepath: str, symbol_name: str, signature: str) -> str:
        seed = f"{filepath}|{symbol_name}|{signature}".encode("utf-8")
        digest = sha1(seed).hexdigest()[:16]
        return f"symbol:{digest}:{symbol_name}"

    @staticmethod
    def _external_symbol_node_id(symbol_name: str) -> str:
        return f"external_symbol:{symbol_name}"

    @classmethod
    def _extract_call_targets(cls, body: str) -> List[str]:
        targets = {
            match.group(1)
            for match in cls._CALL_PATTERN.finditer(body)
            if match.group(1) not in cls._RESERVED_WORDS
        }
        return sorted(targets)

    @classmethod
    def _extract_base_types(cls, signature: str) -> List[str]:
        results: Set[str] = set()

        python_match = cls._CLASS_BASE_PATTERN.search(signature)
        if python_match:
            for item in python_match.group(1).split(","):
                candidate = item.strip().split(".")[-1]
                if candidate and candidate not in cls._RESERVED_WORDS:
                    results.add(candidate)

        for match in cls._EXTENDS_PATTERN.finditer(signature):
            candidate = match.group(1).strip().split(".")[-1]
            if candidate and candidate not in cls._RESERVED_WORDS:
                results.add(candidate)

        return sorted(results)

    @classmethod
    def _extract_reference_targets(
        cls,
        body: str,
        known_symbols: Set[str],
        source_symbol: str,
    ) -> List[str]:
        found = {
            token
            for token in cls._IDENTIFIER_PATTERN.findall(body)
            if token in known_symbols and token != source_symbol and token not in cls._RESERVED_WORDS
        }
        return sorted(found)

    @classmethod
    def _resolve_symbol_target(cls, symbol_name: str, symbol_node_by_name: Dict[str, str]) -> str:
        target = symbol_node_by_name.get(symbol_name)
        if target is not None:
            return target
        return cls._external_symbol_node_id(symbol_name)

    @staticmethod
    def _existing_targets_by_edge_type(graph: nx.DiGraph, source_node: str, edge_type: str) -> Set[str]:
        targets: Set[str] = set()
        for _, target, edge_data in graph.out_edges(source_node, data=True):
            if edge_data.get("edge_type") == edge_type:
                targets.add(target)
        return targets

    @staticmethod
    def _add_structural_edge(
        graph: nx.DiGraph,
        source: str,
        target: str,
        edge_type: str,
        source_file: str,
        source_location: str = "L?",
    ) -> None:
        if not graph.has_node(target):
            if target.startswith("external_symbol:"):
                graph.add_node(
                    target,
                    node_type="external_symbol",
                    symbol_name=target.split(":", 1)[1],
                )
            else:
                graph.add_node(target, node_type="symbol")

        if graph.has_edge(source, target):
            return

        graph.add_edge(
            source,
            target,
            **StructuralGraphBuilder._edge_payload(
                edge_type=edge_type,
                source_file=source_file,
                source_location=source_location,
            ),
        )

    @staticmethod
    def _edge_payload(
        edge_type: str,
        source_file: str,
        source_location: str = "L?",
    ) -> Dict[str, Any]:
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

from dataclasses import dataclass
from hashlib import sha1
from typing import Any, Dict, List

import networkx as nx

from src.domain.interfaces import IASTParser
from src.domain.schemas import DiffFileManifestEntry, DiffManifest, StructuralExtractionGap


@dataclass(frozen=True)
class StructuralBuildResult:
    graph: nx.DiGraph
    gaps: List[StructuralExtractionGap]
    files_attempted: int
    files_parsed: int


class StructuralGraphBuilder:
    """Build deterministic structural graph snapshots from a diff manifest."""

    def __init__(self, ast_parser: IASTParser | None) -> None:
        self.ast_parser = ast_parser

    def build(self, manifest: DiffManifest, repository_path: str) -> StructuralBuildResult:
        graph = nx.DiGraph()
        gaps: List[StructuralExtractionGap] = []
        entries = self._eligible_entries(manifest)

        for entry in entries:
            graph.add_node(
                self._file_node_id(entry.filepath),
                node_type="file",
                file_path=entry.filepath,
                language=entry.language,
            )

        if self.ast_parser is None:
            for entry in entries:
                gaps.append(
                    StructuralExtractionGap(
                        filepath=entry.filepath,
                        reason="ast_parser_unavailable",
                        detail="AST parser is disabled or unavailable; extraction degraded.",
                    )
                )
            return StructuralBuildResult(
                graph=graph,
                gaps=gaps,
                files_attempted=len(entries),
                files_parsed=0,
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

            files_parsed += 1
            file_node_id = self._file_node_id(entry.filepath)
            for entity in sorted(entities, key=lambda item: (item.name, item.signature, item.type)):
                symbol_node_id = self._symbol_node_id(entry.filepath, entity.name, entity.signature)
                graph.add_node(
                    symbol_node_id,
                    node_type="symbol",
                    file_path=entry.filepath,
                    symbol_name=entity.name,
                    symbol_type=entity.type,
                    signature=entity.signature,
                )
                graph.add_edge(
                    file_node_id,
                    symbol_node_id,
                    edge_type="defines",
                    provenance="EXTRACTED",
                    source_ref=entry.filepath,
                )

                for dependency in sorted(set(entity.dependencies)):
                    module_node_id = self._module_node_id(dependency)
                    graph.add_node(
                        module_node_id,
                        node_type="module",
                        module_name=dependency,
                    )
                    graph.add_edge(
                        symbol_node_id,
                        module_node_id,
                        edge_type="imports",
                        provenance="EXTRACTED",
                        source_ref=entry.filepath,
                    )

        return StructuralBuildResult(
            graph=graph,
            gaps=gaps,
            files_attempted=len(entries),
            files_parsed=files_parsed,
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
                str(item.get("source_ref", "")),
            ),
        )
        return payload

    @staticmethod
    def deserialize(payload: Dict[str, Any]) -> nx.DiGraph:
        return nx.node_link_graph(payload, edges="edges")

    @staticmethod
    def _eligible_entries(manifest: DiffManifest) -> List[DiffFileManifestEntry]:
        entries = [
            entry
            for entry in manifest.files
            if entry.change_type in {"A", "M", "R"} and not entry.is_binary
        ]
        return sorted(entries, key=lambda item: item.filepath)

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

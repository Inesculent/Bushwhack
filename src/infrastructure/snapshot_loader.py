"""Load exploration snapshots from disk into domain models."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import Settings
from src.domain.schemas import (
    CommunitySemanticSummary,
    ExplorationSnapshot,
    FileSemanticSummary,
    SnapshotDiagnostics,
    StructuralTopologySummary,
    SymbolSemanticSummary,
    UnverifiedCallTarget,
)
from src.infrastructure.review_kb import load_review_kb


class SnapshotLoader:
    """Load exploration snapshots from disk into domain models."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._base_path = Path(settings.snapshot_base_path).resolve()

    def load_snapshot_pointer(self, run_id: str) -> ExplorationSnapshot:
        """
        Load and validate snapshot.json from bushwhack_runs directory.

        Args:
            run_id: The snapshot directory name (e.g., "28d358fa3aaf_comfyanonymous__ComfyUI__pr7952")

        Returns:
            ExplorationSnapshot model with validated metadata

        Raises:
            FileNotFoundError: If snapshot.json does not exist
            ValueError: If snapshot.json is malformed or missing required fields
        """
        snapshot_path = self._base_path / run_id / "snapshot.json"
        if not snapshot_path.exists():
            raise FileNotFoundError(f"Snapshot not found: {snapshot_path}")

        content = snapshot_path.read_text(encoding="utf-8")
        data = json.loads(content)

        required = ["run_id", "snapshot_id", "snapshot_root", "status"]
        for field in required:
            if field not in data:
                raise ValueError(f"Missing required field: {field}")

        # The ExplorationSnapshot model is nested inside the 'exploration_snapshot' key
        snapshot_data = data.get("exploration_snapshot", data)
        return ExplorationSnapshot.model_validate(snapshot_data)

    def load_graph_payload(self, snapshot_root: str) -> Dict[str, Any]:
        """
        Load full_graph.json and return in native node-link format.

        Args:
            snapshot_root: Absolute path to snapshot directory

        Returns:
            Graph payload in NetworkX node-link JSON format

        Raises:
            FileNotFoundError: If full_graph.json does not exist
        """
        graph_path = Path(snapshot_root) / "graph" / "full_graph.json"
        if not graph_path.exists():
            raise FileNotFoundError(f"Full graph not found: {graph_path}")

        content = graph_path.read_text(encoding="utf-8")
        return json.loads(content)

    def load_topology(self, snapshot_root: str) -> StructuralTopologySummary:
        """
        Parse topology.json into StructuralTopologySummary model.

        Args:
            snapshot_root: Absolute path to snapshot directory

        Returns:
            StructuralTopologySummary with community structure

        Raises:
            FileNotFoundError: If topology.json does not exist
        """
        topology_path = Path(snapshot_root) / "graph" / "topology.json"
        if not topology_path.exists():
            raise FileNotFoundError(f"Topology not found: {topology_path}")

        content = topology_path.read_text(encoding="utf-8")
        data = json.loads(content)
        return StructuralTopologySummary.model_validate(data)

    def load_global_summary(self, snapshot_root: str) -> str:
        """
        Read semantic/global_summary.md contents.

        Args:
            snapshot_root: Absolute path to snapshot directory

        Returns:
            Global summary markdown content

        Raises:
            FileNotFoundError: If global_summary.md does not exist
        """
        summary_path = Path(snapshot_root) / "semantic" / "global_summary.md"
        if not summary_path.exists():
            raise FileNotFoundError(f"Global summary not found: {summary_path}")

        return summary_path.read_text(encoding="utf-8")

    def load_diagnostics(self, snapshot_root: str) -> SnapshotDiagnostics:
        """
        Parse diagnostics.json into SnapshotDiagnostics model.

        Args:
            snapshot_root: Absolute path to snapshot directory

        Returns:
            SnapshotDiagnostics with god_nodes, bridge_nodes, cross_community_edges

        Raises:
            FileNotFoundError: If diagnostics.json does not exist
        """
        diagnostics_path = Path(snapshot_root) / "semantic" / "diagnostics.json"
        if not diagnostics_path.exists():
            raise FileNotFoundError(f"Diagnostics not found: {diagnostics_path}")

        content = diagnostics_path.read_text(encoding="utf-8")
        data = json.loads(content)
        return SnapshotDiagnostics.model_validate(data)

    def load_review_kb(self, snapshot_root: str) -> Dict[str, Any]:
        """Load the persisted review knowledge base for query-time retrieval."""
        return load_review_kb(snapshot_root)

    def load_repository_kb(self, snapshot_root: str) -> Dict[str, Any]:
        """Load the persisted repository knowledge base for query-time retrieval."""
        return load_review_kb(snapshot_root)

    def load_community_shards(self, snapshot_root: str) -> List[CommunitySemanticSummary]:
        """
        Load all semantic/communities/*.md files into CommunitySemanticSummary objects.

        Parses the markdown format written by _render_community_markdown().
        Returns empty list if semantic enrichment was disabled (no community files).

        Args:
            snapshot_root: Absolute path to snapshot directory

        Returns:
            List of CommunitySemanticSummary models

        Note:
            Does NOT raise if no community files exist - returns empty list instead.
        """
        communities_path = Path(snapshot_root) / "semantic" / "communities"
        if not communities_path.exists():
            return []

        summaries: List[CommunitySemanticSummary] = []
        for md_file in sorted(communities_path.glob("*.md")):
            summary = self._parse_community_markdown(md_file.read_text(encoding="utf-8"))
            if summary:
                summaries.append(summary)

        return summaries

    def _parse_community_markdown(self, content: str) -> Optional[CommunitySemanticSummary]:
        """
        Parse community markdown into CommunitySemanticSummary.

        Expected format:
            # Community {id}: {label}

            **Purpose:** {purpose}

            ## Files
            - `{file_node_id}`: {purpose} (confidence {confidence})

            ## Symbols
            - `{symbol_node_id}`: {purpose} (confidence {confidence})
              - _Rationale:_ {rationale}

            ## Cross-community dependencies
            {comma-separated community ids}

            ## Unverified / resolved calls
            {call entries}
        """
        lines = content.strip().split('\n')
        if not lines:
            return None

        # Parse header: # Community {id}: {label}
        header_match = re.match(r'# Community (\d+): (.+)', lines[0])
        if not header_match:
            return None

        community_id = int(header_match.group(1))
        label = header_match.group(2).strip()

        # Parse purpose
        purpose_match = re.search(r'\*\*Purpose:\*\* (.+)', content)
        purpose = purpose_match.group(1).strip() if purpose_match else ""

        # Parse files section
        file_summaries: List[FileSemanticSummary] = []
        file_section = self._extract_section(content, "## Files")
        if file_section:
            for line in file_section.split('\n'):
                file_match = re.match(r'- `([^`]+)`: (.+) \(confidence ([\d.]+)\)', line.strip())
                if file_match:
                    file_summaries.append(FileSemanticSummary(
                        file_node_id=file_match.group(1),
                        purpose=file_match.group(2).strip(),
                        confidence=float(file_match.group(3)),
                    ))

        # Parse symbols section
        symbol_summaries: List[SymbolSemanticSummary] = []
        symbol_section = self._extract_section(content, "## Symbols")
        if symbol_section:
            current_symbol = None
            for line in symbol_section.split('\n'):
                symbol_match = re.match(r'- `([^`]+)`: (.+) \(confidence ([\d.]+)\)', line.strip())
                if symbol_match:
                    if current_symbol:
                        symbol_summaries.append(current_symbol)
                    current_symbol = SymbolSemanticSummary(
                        symbol_node_id=symbol_match.group(1),
                        purpose=symbol_match.group(2).strip(),
                        confidence=float(symbol_match.group(3)),
                        rationale=None,
                    )
                rationale_match = re.match(r'\s*- _Rationale:_ (.+)', line)
                if rationale_match and current_symbol:
                    current_symbol = current_symbol.model_copy(update={
                        "rationale": rationale_match.group(1).strip()
                    })
            if current_symbol:
                symbol_summaries.append(current_symbol)

        # Parse cross-community dependencies
        cross_deps: List[int] = []
        cross_section = self._extract_section(content, "## Cross-community dependencies")
        if cross_section:
            for match in re.finditer(r'\b(\d+)\b', cross_section):
                cross_deps.append(int(match.group(1)))

        # Parse unverified calls (not fully reconstructed, just count)
        unverified_calls: List[UnverifiedCallTarget] = []
        calls_section = self._extract_section(content, "## Unverified / resolved calls")
        if calls_section:
            for line in calls_section.split('\n'):
                call_match = re.match(r'- (unresolved|resolved): `([^`]+)` from `([^`]+)` — (.+)', line.strip())
                if call_match:
                    source_ref = call_match.group(3)
                    # Extract symbol id from "id:name" format
                    if ':' in source_ref:
                        source_symbol_id = source_ref.split(':')[0]
                    else:
                        source_symbol_id = source_ref
                    unverified_calls.append(UnverifiedCallTarget(
                        source_symbol_id=source_symbol_id,
                        target_name=call_match.group(2),
                        source_community_id=community_id,
                        context_hint=call_match.group(4).strip(),
                        resolved=call_match.group(1) == "resolved",
                    ))

        # Extract confidence from first symbol or default
        confidence = symbol_summaries[0].confidence if symbol_summaries else 0.5

        return CommunitySemanticSummary(
            community_id=community_id,
            label=label,
            purpose=purpose,
            file_summaries=file_summaries,
            symbol_summaries=symbol_summaries,
            unverified_calls=unverified_calls,
            cross_community_dependencies=cross_deps,
            confidence=confidence,
        )

    def _extract_section(self, content: str, section_header: str) -> Optional[str]:
        """Extract content between a section header and the next ## header."""
        pattern = rf'{re.escape(section_header)}\n(.*?)(?:\n## |$)'
        match = re.search(pattern, content, re.DOTALL)
        return match.group(1).strip() if match else None

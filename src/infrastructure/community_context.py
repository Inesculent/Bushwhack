"""Assemble per-community context for Phase 2 semantic agents (no LLM)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

import networkx as nx

from src.config import Settings
from src.domain.schemas import (
    CommunitySemanticSummary,
    CommunityWorkItem,
    StructuralTopologyCommunity,
    StructuralTopologySummary,
)
from src.infrastructure.structural_graph import StructuralGraphBuilder


@dataclass(frozen=True)
class CommunityContext:
    """Resolved structural slice for one community."""

    community_id: int
    node_ids: Tuple[str, ...]
    file_paths: Tuple[str, ...]
    symbol_node_ids: Tuple[str, ...]


_CROSS_EDGE_TYPES: frozenset[str] = frozenset({"calls", "references"})


def _node_community(node_id: str, node_to_community: Dict[str, int]) -> Optional[int]:
    cid = node_to_community.get(node_id)
    return int(cid) if cid is not None else None


def _is_init_only_file_path(path: str) -> bool:
    base = path.rsplit("/", maxsplit=1)[-1].lower()
    return base == "__init__.py"


def is_trivial_init_community(
    community: StructuralTopologyCommunity,
    graph: nx.DiGraph,
    *,
    skip_trivial: bool,
) -> bool:
    """True when community has no symbols and only package init files."""
    if not skip_trivial:
        return False
    if community.symbol_count > 0:
        return False
    file_paths = [
        str(graph.nodes[n].get("file_path", ""))
        for n in community.node_ids
        if graph.has_node(n) and graph.nodes[n].get("node_type") == "file"
    ]
    if not file_paths:
        return False
    return all(_is_init_only_file_path(fp) for fp in file_paths if fp)


def synthetic_trivial_community_summary(community_id: int) -> CommunitySemanticSummary:
    return CommunitySemanticSummary(
        community_id=community_id,
        label="Package Initializers",
        purpose="Package initializer, no significant logic.",
        file_summaries=[],
        symbol_summaries=[],
        unverified_calls=[],
        cross_community_dependencies=[],
        confidence=1.0,
    )


def _collect_cross_boundary_targets(
    graph: nx.DiGraph,
    community_id: int,
    node_ids: Sequence[str],
    node_to_community: Dict[str, int],
) -> Tuple[List[str], List[int]]:
    """Return callee names and optional target community ids for outbound cross edges."""
    in_community: Set[str] = set(node_ids)
    names: List[str] = []
    comms: List[int] = []
    seen: Set[str] = set()

    for source, target, attrs in graph.edges(data=True):
        et = str(attrs.get("edge_type", "") or "")
        if et not in _CROSS_EDGE_TYPES:
            continue
        sc = _node_community(source, node_to_community)
        tc = _node_community(target, node_to_community)
        if sc is None or tc is None:
            continue
        if sc != community_id and tc != community_id:
            continue
        if sc == community_id and tc == community_id:
            continue
        # Edge touches this community and another
        if source in in_community and target not in in_community:
            other = target
            other_c = tc if sc == community_id else sc
        elif target in in_community and source not in in_community:
            other = source
            other_c = sc if tc == community_id else tc
        else:
            continue
        if not graph.has_node(other):
            continue
        if graph.nodes[other].get("node_type") != "symbol":
            continue
        sym_name = str(graph.nodes[other].get("symbol_name") or "")
        if not sym_name or sym_name in seen:
            continue
        seen.add(sym_name)
        names.append(sym_name)
        comms.append(int(other_c) if other_c is not None else -1)
    return names, comms


def _symbol_context_lines(
    graph: nx.DiGraph,
    symbol_ids: Sequence[str],
    *,
    max_symbols: int,
    max_chars: int,
) -> List[str]:
    lines: List[str] = []
    used = 0
    for sid in list(symbol_ids)[:max_symbols]:
        if not graph.has_node(sid):
            continue
        attrs = graph.nodes[sid]
        name = str(attrs.get("symbol_name", ""))
        sig = str(attrs.get("signature", ""))[:200]
        body = str(attrs.get("body", ""))[:4000]
        chunk = f"{sid} | {name} | {sig}\n{body}"
        if used + len(chunk) > max_chars:
            remain = max_chars - used - 50
            if remain < 200:
                break
            chunk = chunk[:remain] + "\n... [truncated]"
        lines.append(chunk)
        used += len(chunk)
    return lines


def build_community_work_item(
    community: StructuralTopologyCommunity,
    graph: nx.DiGraph,
    topology: StructuralTopologySummary,
    settings: Settings,
) -> Optional[CommunityWorkItem]:
    """Build a single work item, or None if the community has nothing to analyze."""
    node_to_community = dict(topology.node_to_community)
    max_chars = min(settings.semantic_max_tokens_per_community * 4, 96_000)

    file_paths = sorted(
        {
            str(graph.nodes[n].get("file_path", ""))
            for n in community.node_ids
            if graph.has_node(n) and graph.nodes[n].get("node_type") == "file" and graph.nodes[n].get("file_path")
        }
    )
    symbol_ids = [
        n
        for n in community.node_ids
        if graph.has_node(n) and graph.nodes[n].get("node_type") == "symbol"
    ]
    if not symbol_ids and not file_paths:
        return None

    file_paths = file_paths[: settings.semantic_max_files_per_agent]
    symbol_ids = symbol_ids[: settings.semantic_max_symbols_per_agent]

    targets, target_communities = _collect_cross_boundary_targets(
        graph,
        community.community_id,
        community.node_ids,
        node_to_community,
    )

    lines = _symbol_context_lines(
        graph,
        symbol_ids,
        max_symbols=settings.semantic_max_symbols_per_agent,
        max_chars=max_chars,
    )

    return CommunityWorkItem(
        community_id=community.community_id,
        file_paths=list(file_paths),
        symbol_context_lines=lines,
        outbound_cross_community_targets=targets[:200],
        target_communities_hint=target_communities[:200],
    )


def plan_community_dispatch(
    topology: StructuralTopologySummary,
    graph_payload: Dict[str, Any],
    settings: Settings,
) -> Tuple[List[CommunitySemanticSummary], List[CommunityWorkItem]]:
    """
    Partition communities into trivial synthetic summaries vs LLM work items.

    Returns (trivial_summaries, work_items).
    """
    graph = StructuralGraphBuilder.deserialize(graph_payload)
    trivial: List[CommunitySemanticSummary] = []
    work: List[CommunityWorkItem] = []

    for community in topology.communities:
        if is_trivial_init_community(community, graph, skip_trivial=settings.skip_trivial_communities):
            trivial.append(synthetic_trivial_community_summary(community.community_id))
            continue
        item = build_community_work_item(community, graph, topology, settings)
        if item is not None:
            work.append(item)

    return trivial, work

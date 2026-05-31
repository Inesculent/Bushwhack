"""Repository knowledge base built from structural exploration data.

The module name remains ``review_kb`` for compatibility with existing callers,
but the core records are repository-scoped. PR/review-specific diff facts live
in a separate overlay.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import networkx as nx

from src.domain.schemas import (
    CommunitySemanticSummary,
    FileSemanticSummary,
    ReviewKBEvidence,
    ReviewKBManifest,
    ReviewKBRecord,
    ReviewKBResult,
    StructuralTopologySummary,
    SymbolSemanticSummary,
)
from src.infrastructure.structural_graph import StructuralGraphBuilder

REVIEW_KB_SCHEMA_VERSION = "1"
REVIEW_KB_DIRNAME = "review_kb"
REVIEW_OVERLAY_FILENAME = "review_overlay.json"

_TERM_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{1,}")
_FACT_LINE_RE = re.compile(
    r"\b("
    r"tensor|shape|dtype|dimension|dim|batch|input|output|return|returns|"
    r"expects?|requires?|contract|invariant|side effect|mutates?|raises?|"
    r"config|compatibility|signature|schema"
    r")\b",
    re.IGNORECASE,
)


class ReviewKBBundle:
    """In-memory repository KB bundle ready for snapshot persistence."""

    def __init__(
        self,
        *,
        manifest: ReviewKBManifest,
        repo: ReviewKBRecord,
        communities: List[ReviewKBRecord],
        files: List[ReviewKBRecord],
        symbols: List[ReviewKBRecord],
        facts: List[ReviewKBRecord],
        edges: List[ReviewKBRecord],
        summaries: List[ReviewKBRecord],
        lexical_index: Dict[str, List[str]],
        review_overlay: Dict[str, Any] | None = None,
    ) -> None:
        self.manifest = manifest
        self.repo = repo
        self.communities = communities
        self.files = files
        self.symbols = symbols
        self.facts = facts
        self.edges = edges
        self.summaries = summaries
        self.lexical_index = lexical_index
        self.review_overlay = review_overlay or {}


def _norm_path(value: str) -> str:
    return value.strip().replace("\\", "/")


def _stable_id(kind: str, *parts: object) -> str:
    raw = "|".join(str(p) for p in parts)
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]
    return f"{kind}:{digest}"


def _metadata_int(record: ReviewKBRecord, key: str, default: int = -1) -> int:
    value = record.metadata.get(key)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _terms(*values: object) -> List[str]:
    seen: set[str] = set()
    out: List[str] = []
    for value in values:
        for term in _TERM_RE.findall(str(value or "").lower()):
            if len(term) < 2 or term in seen:
                continue
            seen.add(term)
            out.append(term)
    return out


def _tags_for_text(*values: object) -> List[str]:
    text = " ".join(str(v or "").lower() for v in values)
    tags: set[str] = set()
    if "tensor" in text or "shape" in text or "dtype" in text or "dim" in text:
        tags.add("tensor-shape")
    if "signature" in text or "def " in text or "class " in text:
        tags.add("signature")
    if "lora" in text:
        tags.add("lora")
    if "train" in text or "training" in text:
        tags.add("training")
    if "config" in text or "schema" in text:
        tags.add("config")
    if "raise" in text or "error" in text or "exception" in text:
        tags.add("error-condition")
    if "mutate" in text or "side effect" in text:
        tags.add("side-effect")
    if "contract" in text or "expects" in text or "requires" in text:
        tags.add("contract")
    return sorted(tags)


def build_review_overlay(
    *,
    changed_file_paths: Iterable[str] = (),
    base_ref: str = "",
    head_ref: str = "",
    diff_source: str = "git_diff",
) -> Dict[str, Any]:
    """Build PR/review-specific overlay metadata kept out of core KB records."""
    changed = sorted({_norm_path(p) for p in changed_file_paths if p})
    return {
        "overlay_scope": "review_diff",
        "diff_source": diff_source,
        "base_ref": base_ref,
        "head_ref": head_ref,
        "changed_files": changed,
        "changed_symbols": [],
        "hunks": [],
        "review_task_anchors": [],
        "diff_risk_hints": [],
    }


def _top_symbol_record_ids(
    symbol_record_by_node: Dict[str, ReviewKBRecord],
    symbol_node_ids: Sequence[str],
    *,
    bridge_symbols: Sequence[str] = (),
    limit: int = 8,
) -> List[str]:
    bridge = set(bridge_symbols)
    ranked = sorted(
        symbol_node_ids,
        key=lambda node_id: (
            0 if node_id in bridge else 1,
            str(symbol_record_by_node.get(node_id).metadata.get("symbol_name") if symbol_record_by_node.get(node_id) else node_id),
        ),
    )
    out: List[str] = []
    for node_id in ranked:
        record = symbol_record_by_node.get(str(node_id))
        if record is not None:
            out.append(record.id)
        if len(out) >= limit:
            break
    return out


def build_summary_records(
    *,
    repo: ReviewKBRecord,
    communities: Sequence[ReviewKBRecord],
    files: Sequence[ReviewKBRecord],
    symbols: Sequence[ReviewKBRecord],
    facts: Sequence[ReviewKBRecord],
    symbol_record_by_node: Dict[str, ReviewKBRecord],
) -> List[ReviewKBRecord]:
    """Build repository-scoped summary records with cited KB evidence ids."""
    file_by_path = {str(r.metadata.get("file_path") or ""): r for r in files}
    facts_by_path: Dict[str, List[ReviewKBRecord]] = defaultdict(list)
    for fact in facts:
        fp = str(fact.metadata.get("file_path") or "")
        if fp:
            facts_by_path[fp].append(fact)

    summaries: List[ReviewKBRecord] = []
    for community in communities:
        cid = int(community.metadata.get("community_id") or 0)
        community_files = [str(p) for p in community.metadata.get("files") or []]
        community_symbols = [str(s) for s in community.metadata.get("symbols") or []]
        bridge_symbols = [str(s) for s in community.metadata.get("bridge_symbols") or []]
        cited_file_records = [file_by_path[p] for p in community_files[:8] if p in file_by_path]
        cited_symbol_ids = _top_symbol_record_ids(
            symbol_record_by_node,
            community_symbols,
            bridge_symbols=bridge_symbols,
            limit=8,
        )
        cited_fact_records: List[ReviewKBRecord] = []
        for fp in community_files:
            cited_fact_records.extend(facts_by_path.get(fp, [])[:2])
            if len(cited_fact_records) >= 6:
                break
        evidence_ids = [
            community.id,
            *[r.id for r in cited_file_records],
            *cited_symbol_ids,
            *[r.id for r in cited_fact_records],
        ]
        bridge_count = len(bridge_symbols)
        fact_count = sum(len(facts_by_path.get(fp, [])) for fp in community_files)
        summary = (
            f"Community {cid} contains {len(community_files)} files and {len(community_symbols)} symbols. "
            f"It exposes {bridge_count} bridge symbols and {fact_count} mined repository facts."
        )
        summaries.append(
            ReviewKBRecord(
                id=f"summary:community:{cid}",
                kind="summary",
                summary=summary,
                evidence=community.evidence,
                confidence="inferred",
                tags=sorted(set(["summary", "community", *_tags_for_text(*community_files[:20])])),
                metadata={
                    "summary_scope": "community",
                    "community_id": cid,
                    "source_record_ids": list(dict.fromkeys(evidence_ids)),
                    "omitted_files": max(0, len(community_files) - len(cited_file_records)),
                    "omitted_symbols": max(0, len(community_symbols) - len(cited_symbol_ids)),
                    "omitted_facts": max(0, fact_count - len(cited_fact_records)),
                    "llm_distillation_status": "not_run",
                },
            )
        )

    summaries.append(
        ReviewKBRecord(
            id="summary:repo",
            kind="summary",
            summary=(
                "KB-backed repository understanding has not been synthesized yet. "
                f"Use deterministic topology and {len(communities)} community summaries for navigation."
            ),
            evidence=repo.evidence,
            confidence="inferred",
            tags=["summary", "repo"],
            metadata={
                "summary_scope": "repo",
                "source_record_ids": [repo.id, *[c.id for c in communities]],
                "llm_distillation_status": "not_run",
            },
        )
    )
    bridge_counts = [
        (int(c.metadata.get("community_id") or 0), len(c.metadata.get("bridge_symbols") or []))
        for c in communities
    ]
    top_bridge_communities = [
        f"community:{cid}:{count}"
        for cid, count in sorted(bridge_counts, key=lambda row: (-row[1], row[0]))[:8]
        if count
    ]
    summaries.append(
        ReviewKBRecord(
            id="summary:repo:topology",
            kind="summary",
            summary=(
                f"Static topology: {len(files)} files, {len(symbols)} symbols, {len(communities)} communities, "
                f"{sum(len(c.metadata.get('cross_community_dependencies') or []) for c in communities)} "
                "cross-community dependency references."
            ),
            evidence=repo.evidence,
            confidence="inferred",
            tags=["summary", "repo", "topology"],
            metadata={
                "summary_scope": "repo_topology",
                "source_record_ids": [repo.id, *[c.id for c in communities]],
                "file_count": len(files),
                "symbol_count": len(symbols),
                "community_count": len(communities),
                "fact_count": len(facts),
                "top_bridge_communities": top_bridge_communities,
                "llm_distillation_status": "not_applicable",
            },
        )
    )
    return summaries


def _record_by_id(
    *,
    repo: ReviewKBRecord,
    communities: Sequence[ReviewKBRecord],
    files: Sequence[ReviewKBRecord],
    symbols: Sequence[ReviewKBRecord],
    facts: Sequence[ReviewKBRecord],
    edges: Sequence[ReviewKBRecord],
) -> Dict[str, ReviewKBRecord]:
    return {record.id: record for record in [repo, *communities, *files, *symbols, *facts, *edges]}


def _symbol_records_for_file(symbols: Sequence[ReviewKBRecord], file_path: str) -> List[ReviewKBRecord]:
    return [
        record
        for record in symbols
        if _norm_path(str(record.metadata.get("file_path") or "")) == file_path
    ]


def _edge_records_for_symbols(
    edges: Sequence[ReviewKBRecord],
    symbol_records: Sequence[ReviewKBRecord],
) -> tuple[List[ReviewKBRecord], List[ReviewKBRecord], List[ReviewKBRecord]]:
    symbol_ids = {record.id for record in symbol_records}
    inbound: List[ReviewKBRecord] = []
    outbound: List[ReviewKBRecord] = []
    boundary: List[ReviewKBRecord] = []
    for edge in edges:
        source_id = str(edge.metadata.get("source_record_id") or "")
        target_id = str(edge.metadata.get("target_record_id") or "")
        if source_id in symbol_ids:
            outbound.append(edge)
        if target_id in symbol_ids:
            inbound.append(edge)
        if source_id in symbol_ids or target_id in symbol_ids:
            boundary.append(edge)
    return inbound, outbound, boundary


def _compact_ids(records: Sequence[ReviewKBRecord], limit: int = 12) -> List[str]:
    return [record.id for record in records[:limit]]


def build_boundary_summary_records(
    *,
    repo: ReviewKBRecord,
    communities: Sequence[ReviewKBRecord],
    files: Sequence[ReviewKBRecord],
    symbols: Sequence[ReviewKBRecord],
    facts: Sequence[ReviewKBRecord],
    edges: Sequence[ReviewKBRecord],
    changed_file_paths: Iterable[str],
) -> List[ReviewKBRecord]:
    """Build deterministic maps of changed-file boundaries and cascade routes."""
    changed = sorted({_norm_path(path) for path in changed_file_paths if path})
    if not changed:
        return []

    files_by_path = {str(record.metadata.get("file_path") or ""): record for record in files}
    facts_by_path: Dict[str, List[ReviewKBRecord]] = defaultdict(list)
    for fact in facts:
        facts_by_path[_norm_path(str(fact.metadata.get("file_path") or ""))].append(fact)

    by_id = _record_by_id(repo=repo, communities=communities, files=files, symbols=symbols, facts=facts, edges=edges)
    records: List[ReviewKBRecord] = []
    touched: Dict[int, List[ReviewKBRecord]] = defaultdict(list)

    for file_path in changed:
        file_record = files_by_path.get(file_path)
        if file_record is None:
            continue
        cid = _metadata_int(file_record, "community_id")
        touched[cid].append(file_record)
        file_symbols = _symbol_records_for_file(symbols, file_path)
        nearby_facts = sorted(
            facts_by_path.get(file_path, []),
            key=lambda r: (0 if {"contract", "tensor-shape", "signature"} & set(r.tags) else 1, r.id),
        )
        inbound, outbound, boundary_edges = _edge_records_for_symbols(edges, file_symbols)
        dependency_cids: set[int] = set()
        cross_edges: List[ReviewKBRecord] = []
        for edge in boundary_edges:
            edge_crosses = False
            for key in ("source_record_id", "target_record_id"):
                other = by_id.get(str(edge.metadata.get(key) or ""))
                other_cid = _metadata_int(other, "community_id") if other is not None else -1
                if other_cid not in {-1, cid}:
                    dependency_cids.add(other_cid)
                    edge_crosses = True
            if edge_crosses:
                cross_edges.append(edge)

        anchors = ", ".join(str(s.metadata.get("symbol_name") or s.id) for s in file_symbols[:6])
        if not anchors:
            anchors = "file-level change"
        cascade: List[str] = []
        if outbound:
            cascade.append(f"{len(outbound)} outbound dependency edges can propagate changed behavior.")
        if inbound:
            cascade.append(f"{len(inbound)} inbound callers may observe changed contracts.")
        if dependency_cids:
            cascade.append(f"Crosses communities {', '.join(str(x) for x in sorted(dependency_cids)[:6])}.")
        if nearby_facts:
            cascade.append("Nearby signature/contract facts should anchor exact-code review.")
        summary = (
            f"Boundary map for changed file {file_path} in community {cid}: anchors {anchors}. "
            f"fan_in={len(inbound)}, fan_out={len(outbound)}, cross_community_edges={len(cross_edges)}. "
            + " ".join(cascade)
        ).strip()
        source_ids = [
            file_record.id,
            *_compact_ids(file_symbols, 8),
            *_compact_ids(nearby_facts, 6),
            *_compact_ids(boundary_edges, 8),
        ]
        records.append(
            ReviewKBRecord(
                id=_stable_id("summary:boundary:file", file_path),
                kind="summary",
                summary=summary,
                evidence=file_record.evidence,
                confidence="inferred",
                tags=sorted(
                    set(
                        [
                            "summary",
                            "boundary",
                            "cascade",
                            "risk-surface",
                            *_tags_for_text(file_path, anchors, summary),
                        ]
                    )
                ),
                metadata={
                    "summary_scope": "boundary",
                    "boundary_scope": "file",
                    "file_path": file_path,
                    "community_id": cid,
                    "changed_symbol_ids": _compact_ids(file_symbols, 16),
                    "fact_record_ids": _compact_ids(nearby_facts, 12),
                    "inbound_edge_ids": _compact_ids(inbound, 12),
                    "outbound_edge_ids": _compact_ids(outbound, 12),
                    "cross_community_edge_ids": _compact_ids(cross_edges, 12),
                    "dependency_community_ids": sorted(dependency_cids)[:12],
                    "fan_in": len(inbound),
                    "fan_out": len(outbound),
                    "source_record_ids": list(dict.fromkeys(source_ids))[:32],
                    "llm_distillation_status": "not_run",
                },
            )
        )

    community_by_id = {_metadata_int(record, "community_id"): record for record in communities}
    for cid, changed_files in sorted(touched.items()):
        community = community_by_id.get(cid)
        if community is None:
            continue
        source_records: List[ReviewKBRecord] = [community, *changed_files]
        symbol_records: List[ReviewKBRecord] = []
        fact_records: List[ReviewKBRecord] = []
        inbound_total = 0
        outbound_total = 0
        dependency_cids: set[int] = set()
        for file_record in changed_files:
            file_path = str(file_record.metadata.get("file_path") or "")
            file_symbols = _symbol_records_for_file(symbols, file_path)
            symbol_records.extend(file_symbols)
            facts_for_file = facts_by_path.get(file_path, [])[:4]
            fact_records.extend(facts_for_file)
            inbound, outbound, boundary_edges = _edge_records_for_symbols(edges, file_symbols)
            inbound_total += len(inbound)
            outbound_total += len(outbound)
            source_records.extend([*file_symbols[:4], *facts_for_file, *boundary_edges[:4]])
            for edge in boundary_edges:
                for key in ("source_record_id", "target_record_id"):
                    other = by_id.get(str(edge.metadata.get(key) or ""))
                    other_cid = _metadata_int(other, "community_id") if other is not None else -1
                    if other_cid not in {-1, cid}:
                        dependency_cids.add(other_cid)
        file_list = ", ".join(str(r.metadata.get("file_path") or r.id) for r in changed_files[:8])
        summary = (
            f"Boundary map for touched community {cid}: changed files {file_list}. "
            f"fan_in={inbound_total}, fan_out={outbound_total}, "
            f"dependency_communities={', '.join(str(x) for x in sorted(dependency_cids)[:8]) or 'none'}."
        )
        records.append(
            ReviewKBRecord(
                id=f"summary:boundary:community:{cid}",
                kind="summary",
                summary=summary,
                evidence=community.evidence,
                confidence="inferred",
                tags=sorted(set(["summary", "boundary", "cascade", "risk-surface", *_tags_for_text(file_list)])),
                metadata={
                    "summary_scope": "boundary",
                    "boundary_scope": "community",
                    "community_id": cid,
                    "changed_files": [str(r.metadata.get("file_path") or "") for r in changed_files[:16]],
                    "changed_symbol_ids": _compact_ids(symbol_records, 24),
                    "fact_record_ids": _compact_ids(fact_records, 16),
                    "dependency_community_ids": sorted(dependency_cids)[:12],
                    "fan_in": inbound_total,
                    "fan_out": outbound_total,
                    "source_record_ids": list(dict.fromkeys(r.id for r in source_records))[:40],
                    "llm_distillation_status": "not_run",
                },
            )
        )

    return records


def _line_for_signature(repo_root: Path, file_path: str, signature: str, symbol_name: str) -> int | None:
    if not repo_root.is_dir() or not file_path:
        return None
    candidate = (repo_root / file_path).resolve()
    try:
        if not candidate.is_file() or repo_root.resolve() not in candidate.parents:
            return None
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    needle = signature.strip()
    for idx, line in enumerate(lines, start=1):
        stripped = line.strip()
        if needle and stripped == needle:
            return idx
        if symbol_name and re.search(rf"\b(def|class)\s+{re.escape(symbol_name)}\b", stripped):
            return idx
    return None


def _extract_fact_records(repo_root: Path, file_path: str, file_record_id: str) -> List[ReviewKBRecord]:
    if not repo_root.is_dir() or not file_path:
        return []
    root = repo_root.resolve()
    candidate = (root / file_path).resolve()
    try:
        if not candidate.is_file() or root not in candidate.parents:
            return []
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []

    records: List[ReviewKBRecord] = []
    for idx, raw in enumerate(lines, start=1):
        line = raw.strip()
        if not line or len(line) > 500 or not _FACT_LINE_RE.search(line):
            continue
        source_like_fact = (
            line.startswith(("#", "//", "/*", "*", '"""', "'''", "raise "))
            or ":" in line
            or "=" in line
        )
        if not source_like_fact:
            continue
        summary = line.lstrip("#/* '\"").strip()
        if len(summary) < 12:
            continue
        record_id = _stable_id("fact", file_path, idx, summary)
        tags = ["fact"] + _tags_for_text(file_path, summary)
        records.append(
            ReviewKBRecord(
                id=record_id,
                kind="fact",
                summary=summary[:400],
                evidence=[
                    ReviewKBEvidence(
                        file_path=file_path,
                        line_start=idx,
                        line_end=idx,
                        graph_node_id=file_record_id,
                        note="source fact line",
                    )
                ],
                confidence="inferred",
                tags=sorted(set(tags)),
                metadata={"file_path": file_path},
            )
        )
        if len(records) >= 40:
            break
    return records


def _community_members(
    graph: nx.DiGraph,
    topology: StructuralTopologySummary,
) -> Dict[int, Dict[str, List[str]]]:
    out: Dict[int, Dict[str, List[str]]] = {}
    for comm in topology.communities:
        files: List[str] = []
        symbols: List[str] = []
        for node_id in comm.node_ids:
            if not graph.has_node(node_id):
                continue
            node_type = graph.nodes[node_id].get("node_type")
            if node_type == "file":
                fp = _norm_path(str(graph.nodes[node_id].get("file_path") or ""))
                if fp:
                    files.append(fp)
            elif node_type == "symbol":
                symbols.append(str(node_id))
        out[int(comm.community_id)] = {
            "files": sorted(set(files)),
            "symbols": sorted(set(symbols)),
        }
    return out


def _cross_community_symbol_ids(
    graph: nx.DiGraph,
    node_to_community: Dict[str, int],
    community_id: int,
) -> List[str]:
    ids: set[str] = set()
    for source, target, attrs in graph.edges(data=True):
        if attrs.get("edge_type") not in {"calls", "references", "inherits"}:
            continue
        sc = node_to_community.get(str(source))
        tc = node_to_community.get(str(target))
        if sc is None or tc is None or sc == tc:
            continue
        if sc == community_id and graph.has_node(source) and graph.nodes[source].get("node_type") == "symbol":
            ids.add(str(source))
        if tc == community_id and graph.has_node(target) and graph.nodes[target].get("node_type") == "symbol":
            ids.add(str(target))
    return sorted(ids)


def build_review_kb(
    *,
    run_id: str,
    repo_path: str,
    graph_payload: Dict[str, Any],
    topology: StructuralTopologySummary,
    changed_file_paths: Iterable[str] = (),
    repo_identity: str = "",
    checkout_ref: str = "",
    base_ref: str = "",
    head_ref: str = "",
) -> ReviewKBBundle:
    graph = StructuralGraphBuilder.deserialize(graph_payload)
    node_to_community = {str(k): int(v) for k, v in topology.node_to_community.items()}
    repo_root = Path(repo_path) if repo_path else Path()
    members = _community_members(graph, topology)
    changed_paths = {_norm_path(path) for path in changed_file_paths if path}
    review_overlay = build_review_overlay(
        changed_file_paths=changed_paths,
        base_ref=base_ref,
        head_ref=head_ref,
    )

    files: List[ReviewKBRecord] = []
    symbols: List[ReviewKBRecord] = []
    facts: List[ReviewKBRecord] = []
    edges: List[ReviewKBRecord] = []
    summaries: List[ReviewKBRecord] = []

    file_record_by_path: Dict[str, ReviewKBRecord] = {}
    symbol_record_by_node: Dict[str, ReviewKBRecord] = {}
    symbol_ids_by_file: Dict[str, List[str]] = defaultdict(list)
    signature_facts_by_file: Dict[str, List[ReviewKBRecord]] = defaultdict(list)

    for node_id, attrs in sorted(graph.nodes(data=True), key=lambda item: str(item[0])):
        if attrs.get("node_type") != "file":
            continue
        file_path = _norm_path(str(attrs.get("file_path") or ""))
        if not file_path:
            continue
        cid = node_to_community.get(str(node_id))
        tags = set(["file", *_tags_for_text(file_path)])
        record = ReviewKBRecord(
            id=_stable_id("file", file_path),
            kind="file",
            summary=f"{file_path} in community {cid if cid is not None else 'unknown'}.",
            evidence=[ReviewKBEvidence(file_path=file_path, graph_node_id=str(node_id), note="file node")],
            confidence="structural",
            tags=sorted(tags),
            metadata={
                "file_path": file_path,
                "file_node_id": str(node_id),
                "community_id": cid,
            },
        )
        files.append(record)
        file_record_by_path[file_path] = record

    for node_id, attrs in sorted(graph.nodes(data=True), key=lambda item: str(item[0])):
        if attrs.get("node_type") != "symbol":
            continue
        file_path = _norm_path(str(attrs.get("file_path") or ""))
        name = str(attrs.get("symbol_name") or "")
        signature = str(attrs.get("signature") or "")
        symbol_type = str(attrs.get("symbol_type") or "symbol")
        cid = node_to_community.get(str(node_id))
        line = attrs.get("definition_line")
        if line is None:
            line = _line_for_signature(repo_root, file_path, signature, name)
        summary = f"{symbol_type} {name}"
        if signature:
            summary = f"{summary}: {signature}"
        record = ReviewKBRecord(
            id=_stable_id("symbol", node_id),
            kind="symbol",
            summary=summary[:500],
            evidence=[
                ReviewKBEvidence(
                    file_path=file_path,
                    line_start=int(line) if isinstance(line, int) else None,
                    line_end=int(line) if isinstance(line, int) else None,
                    graph_node_id=str(node_id),
                    note="symbol node",
                )
            ],
            confidence="structural",
            tags=sorted(set(["symbol", symbol_type, *_tags_for_text(file_path, name, signature)])),
            metadata={
                "symbol_node_id": str(node_id),
                "file_path": file_path,
                "symbol_name": name,
                "qualified_name": f"{file_path}:{name}" if file_path and name else name,
                "symbol_type": symbol_type,
                "signature": signature,
                "community_id": cid,
                "callers": [],
                "callees": [],
            },
        )
        symbols.append(record)
        symbol_record_by_node[str(node_id)] = record
        if file_path:
            symbol_ids_by_file[file_path].append(record.id)
        if file_path and signature:
            fact_summary = f"Signature contract for {name}: {signature}"
            signature_facts_by_file[file_path].append(
                ReviewKBRecord(
                    id=_stable_id("fact", file_path, name, signature),
                    kind="fact",
                    summary=fact_summary[:400],
                    evidence=[
                        ReviewKBEvidence(
                            file_path=file_path,
                            line_start=int(line) if isinstance(line, int) else None,
                            line_end=int(line) if isinstance(line, int) else None,
                            graph_node_id=str(node_id),
                            note="symbol signature fact",
                        )
                    ],
                    confidence="structural",
                    tags=sorted(set(["fact", "signature", *_tags_for_text(file_path, name, signature)])),
                    metadata={
                        "file_path": file_path,
                        "symbol_node_id": str(node_id),
                        "symbol_name": name,
                    },
                )
            )

    for source, target, attrs in sorted(
        graph.edges(data=True),
        key=lambda row: (str(row[0]), str(row[1]), str(row[2].get("edge_type") or "")),
    ):
        edge_type = str(attrs.get("edge_type") or "edge")
        src = str(source)
        dst = str(target)
        src_record = symbol_record_by_node.get(src)
        dst_record = symbol_record_by_node.get(dst)
        if src_record is not None:
            src_record.metadata.setdefault("callees", []).append(dst)
        if dst_record is not None:
            dst_record.metadata.setdefault("callers", []).append(src)
        if edge_type not in {"calls", "references", "inherits", "imports", "defines"}:
            continue
        record = ReviewKBRecord(
            id=_stable_id("edge", src, dst, edge_type),
            kind="edge",
            summary=f"{src} {edge_type} {dst}",
            evidence=[
                ReviewKBEvidence(
                    file_path=str(attrs.get("source_file") or ""),
                    graph_node_id=src,
                    note=f"structural {edge_type} edge",
                )
            ],
            confidence="structural",
            tags=sorted(set(["edge", edge_type])),
            metadata={
                "source": src,
                "target": dst,
                "edge_type": edge_type,
                "source_record_id": src_record.id if src_record else "",
                "target_record_id": dst_record.id if dst_record else "",
            },
        )
        edges.append(record)

    for file_record in files:
        file_path = str(file_record.metadata.get("file_path") or "")
        file_record.metadata["symbols"] = sorted(symbol_ids_by_file.get(file_path, []))
        file_facts = [
            *signature_facts_by_file.get(file_path, []),
            *_extract_fact_records(repo_root, file_path, file_record.id),
        ]
        facts.extend(file_facts)
        file_record.metadata["facts"] = [f.id for f in file_facts]
        if file_facts:
            file_record.tags = sorted(set(file_record.tags + ["has-facts"]))

    communities: List[ReviewKBRecord] = []
    for comm in sorted(topology.communities, key=lambda c: c.community_id):
        cid = int(comm.community_id)
        cm = members.get(cid, {"files": [], "symbols": []})
        bridge_symbols = _cross_community_symbol_ids(graph, node_to_community, cid)
        dep_comms = sorted(
            {
                node_to_community[str(target)]
                for source, target, attrs in graph.edges(data=True)
                if node_to_community.get(str(source)) == cid
                and node_to_community.get(str(target)) not in {None, cid}
                and attrs.get("edge_type") in {"calls", "references", "inherits"}
            }
        )
        summary = (
            f"Community {cid}: {len(cm['files'])} files, {len(cm['symbols'])} symbols, "
            f"{len(bridge_symbols)} bridge symbols."
        )
        communities.append(
            ReviewKBRecord(
                id=f"community:{cid}",
                kind="community",
                summary=summary,
                evidence=[
                    ReviewKBEvidence(
                        graph_node_id=f"community:{cid}",
                        note="topology community",
                    )
                ],
                confidence="structural",
                tags=sorted(set(["community", *(_tags_for_text(*cm["files"][:20]))])),
                metadata={
                    "community_id": cid,
                    "files": cm["files"],
                    "symbols": cm["symbols"],
                    "bridge_symbols": bridge_symbols,
                    "cross_community_dependencies": dep_comms,
                    "cohesion": comm.cohesion,
                },
            )
        )

    language_counter = Counter(
        str(attrs.get("language") or "")
        for _, attrs in graph.nodes(data=True)
        if attrs.get("node_type") == "file" and attrs.get("language")
    )
    repo = ReviewKBRecord(
        id="repo",
        kind="repo",
        summary=(
            f"Repository structural KB with {len(files)} files, {len(symbols)} symbols, "
            f"{topology.community_count} communities, and {len(edges)} dependency edges."
        ),
        evidence=[ReviewKBEvidence(note="review KB manifest")],
        confidence="structural",
        tags=["repo"],
        metadata={
            "run_id": run_id,
            "repo_path": repo_path,
            "repo_identity": repo_identity or repo_path,
            "kb_scope": "repository",
            "graph_source": "repo_checkout",
            "checkout_ref": checkout_ref or "unknown_local_checkout",
            "base_ref": base_ref,
            "head_ref": head_ref,
            "languages": dict(language_counter),
            "communities": [c.id for c in communities],
        },
    )

    summaries = build_summary_records(
        repo=repo,
        communities=communities,
        files=files,
        symbols=symbols,
        facts=facts,
        symbol_record_by_node=symbol_record_by_node,
    )
    summaries.extend(
        build_boundary_summary_records(
            repo=repo,
            communities=communities,
            files=files,
            symbols=symbols,
            facts=facts,
            edges=edges,
            changed_file_paths=changed_paths,
        )
    )

    all_records = [repo, *communities, *files, *symbols, *facts, *edges, *summaries]
    lexical: Dict[str, List[str]] = defaultdict(list)
    for record in all_records:
        text_parts = _lexical_text_parts(record)
        for term in _terms(*text_parts):
            if record.id not in lexical[term]:
                lexical[term].append(record.id)

    counts = {
        "communities": len(communities),
        "files": len(files),
        "symbols": len(symbols),
        "facts": len(facts),
        "edges": len(edges),
        "summaries": len(summaries),
    }
    manifest = ReviewKBManifest(
        schema_version=REVIEW_KB_SCHEMA_VERSION,
        run_id=run_id,
        repo_path=repo_path,
        counts=counts,
        coverage={
            "files_with_symbols": sum(1 for f in files if f.metadata.get("symbols")),
            "files_with_facts": sum(1 for f in files if f.metadata.get("facts")),
        },
        diagnostics={
            "builder": "deterministic_structural_v1",
            "repo_path_available": repo_root.is_dir(),
            "kb_scope": "repository",
            "overlay_scope": review_overlay["overlay_scope"] if review_overlay["changed_files"] else "",
        },
    )
    return ReviewKBBundle(
        manifest=manifest,
        repo=repo,
        communities=communities,
        files=files,
        symbols=symbols,
        facts=facts,
        edges=edges,
        summaries=summaries,
        lexical_index={k: v[:200] for k, v in sorted(lexical.items())},
        review_overlay=review_overlay,
    )


def compatibility_summaries_from_kb(bundle: ReviewKBBundle) -> List[CommunitySemanticSummary]:
    file_by_id = {r.id: r for r in bundle.files}
    symbol_by_node = {
        str(r.metadata.get("symbol_node_id") or ""): r
        for r in bundle.symbols
        if r.metadata.get("symbol_node_id")
    }
    summary_by_community = {
        _metadata_int(r, "community_id"): r
        for r in bundle.summaries
        if r.metadata.get("summary_scope") == "community"
    }
    summaries: List[CommunitySemanticSummary] = []
    for comm in bundle.communities:
        cid = int(comm.metadata.get("community_id") or 0)
        distilled = summary_by_community.get(cid)
        file_summaries: List[FileSemanticSummary] = []
        for file_path in list(comm.metadata.get("files") or [])[:8]:
            record = file_by_id.get(_stable_id("file", file_path))
            summary = record.summary if record else f"{file_path} belongs to community {cid}."
            file_summaries.append(
                FileSemanticSummary(
                    file_node_id=f"file:{file_path}",
                    purpose=summary,
                    key_symbols=[],
                    confidence=0.8,
                )
            )
        bridge = list(comm.metadata.get("bridge_symbols") or [])
        symbol_ids = list(dict.fromkeys(bridge + list(comm.metadata.get("symbols") or [])))[:15]
        symbol_summaries: List[SymbolSemanticSummary] = []
        for node_id in symbol_ids:
            record = symbol_by_node.get(str(node_id))
            if record is None:
                continue
            symbol_summaries.append(
                SymbolSemanticSummary(
                    symbol_node_id=str(node_id),
                    purpose=record.summary,
                    rationale="Derived from structural Review KB.",
                    confidence=0.8,
                )
            )
        summaries.append(
            CommunitySemanticSummary(
                community_id=cid,
                label=f"Community {cid}",
                purpose=distilled.summary if distilled is not None else comm.summary,
                file_summaries=file_summaries,
                symbol_summaries=symbol_summaries,
                unverified_calls=[],
                cross_community_dependencies=[
                    int(x) for x in comm.metadata.get("cross_community_dependencies") or []
                ],
                confidence=0.8,
            )
        )
    return summaries


def write_review_kb(root: Path, bundle: ReviewKBBundle) -> Path:
    kb_dir = root / REVIEW_KB_DIRNAME
    kb_dir.mkdir(parents=True, exist_ok=True)
    (kb_dir / "manifest.json").write_text(bundle.manifest.model_dump_json(indent=2), encoding="utf-8")
    (kb_dir / "repo.json").write_text(bundle.repo.model_dump_json(indent=2), encoding="utf-8")
    _write_jsonl(kb_dir / "communities.jsonl", bundle.communities)
    _write_jsonl(kb_dir / "files.jsonl", bundle.files)
    _write_jsonl(kb_dir / "symbols.jsonl", bundle.symbols)
    _write_jsonl(kb_dir / "facts.jsonl", bundle.facts)
    _write_jsonl(kb_dir / "edges.jsonl", bundle.edges)
    _write_jsonl(kb_dir / "summaries.jsonl", bundle.summaries)
    (kb_dir / REVIEW_OVERLAY_FILENAME).write_text(
        json.dumps(bundle.review_overlay, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    (kb_dir / "lexical_index.json").write_text(
        json.dumps(bundle.lexical_index, indent=2, sort_keys=True),
        encoding="utf-8",
    )
    return kb_dir


def _lexical_text_parts(record: ReviewKBRecord) -> List[str]:
    metadata_keys = (
        "summary_scope",
        "boundary_scope",
        "file_path",
        "symbol_name",
        "qualified_name",
        "symbol_type",
        "community_id",
        "edge_type",
        "dependency_community_ids",
        "changed_files",
        "source_record_ids",
    )
    compact_meta: Dict[str, Any] = {}
    for key in metadata_keys:
        value = record.metadata.get(key)
        if value is None:
            continue
        if isinstance(value, list):
            compact_meta[key] = [str(item) for item in value[:12]]
        else:
            compact_meta[key] = value
    return [
        record.id,
        record.kind,
        record.summary,
        " ".join(record.tags),
        json.dumps(compact_meta, sort_keys=True),
    ]


def rebuild_review_kb_lexical_index(bundle: ReviewKBBundle) -> Dict[str, List[str]]:
    """Rebuild the compact lexical index after summary records are replaced."""
    lexical: Dict[str, List[str]] = defaultdict(list)
    for record in [
        bundle.repo,
        *bundle.communities,
        *bundle.files,
        *bundle.symbols,
        *bundle.facts,
        *bundle.edges,
        *bundle.summaries,
    ]:
        text_parts = _lexical_text_parts(record)
        for term in _terms(*text_parts):
            if record.id not in lexical[term]:
                lexical[term].append(record.id)
    bundle.lexical_index = {k: v[:200] for k, v in sorted(lexical.items())}
    return bundle.lexical_index


def _write_jsonl(path: Path, records: Sequence[ReviewKBRecord]) -> None:
    path.write_text(
        "".join(record.model_dump_json() + "\n" for record in records),
        encoding="utf-8",
    )


def load_review_kb(snapshot_root: str) -> Dict[str, Any]:
    kb_dir = Path(snapshot_root) / REVIEW_KB_DIRNAME
    if not kb_dir.exists():
        raise FileNotFoundError(f"Review KB not found: {kb_dir}")
    manifest = ReviewKBManifest.model_validate_json((kb_dir / "manifest.json").read_text(encoding="utf-8"))
    repo = ReviewKBRecord.model_validate_json((kb_dir / "repo.json").read_text(encoding="utf-8"))
    collections = {
        "communities": _read_jsonl(kb_dir / "communities.jsonl"),
        "files": _read_jsonl(kb_dir / "files.jsonl"),
        "symbols": _read_jsonl(kb_dir / "symbols.jsonl"),
        "facts": _read_jsonl(kb_dir / "facts.jsonl"),
        "edges": _read_jsonl(kb_dir / "edges.jsonl"),
        "summaries": _read_jsonl(kb_dir / "summaries.jsonl"),
    }
    lexical = json.loads((kb_dir / "lexical_index.json").read_text(encoding="utf-8"))
    overlay_path = kb_dir / REVIEW_OVERLAY_FILENAME
    review_overlay = json.loads(overlay_path.read_text(encoding="utf-8")) if overlay_path.exists() else {}
    records = [repo]
    for rows in collections.values():
        records.extend(rows)
    by_id = {r.id: r for r in records}
    return {
        "manifest": manifest,
        "repo": repo,
        **collections,
        "review_overlay": review_overlay,
        "lexical_index": lexical,
        "by_id": by_id,
    }


def _read_jsonl(path: Path) -> List[ReviewKBRecord]:
    if not path.exists():
        return []
    rows: List[ReviewKBRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(ReviewKBRecord.model_validate_json(line))
    return rows


def query_loaded_review_kb(
    kb: Dict[str, Any],
    *,
    query: str,
    path: str | None = None,
    symbol: str | None = None,
    community_id: int | None = None,
    topics: Sequence[str] | None = None,
    include_dependencies: bool = True,
    use_review_overlay: bool = False,
    max_results: int = 8,
) -> ReviewKBResult:
    by_id: Dict[str, ReviewKBRecord] = dict(kb.get("by_id") or {})
    files: List[ReviewKBRecord] = list(kb.get("files") or [])
    symbols: List[ReviewKBRecord] = list(kb.get("symbols") or [])
    facts: List[ReviewKBRecord] = list(kb.get("facts") or [])
    edges: List[ReviewKBRecord] = list(kb.get("edges") or [])
    summaries: List[ReviewKBRecord] = list(kb.get("summaries") or [])
    if not use_review_overlay:
        summaries = [
            record
            for record in summaries
            if record.metadata.get("summary_scope") == "boundary"
            or str(record.metadata.get("distillation_scope") or "core_repository")
            not in {"review_neighborhood", "on_demand"}
        ]
    communities: List[ReviewKBRecord] = list(kb.get("communities") or [])
    lexical: Dict[str, List[str]] = dict(kb.get("lexical_index") or {})
    review_overlay: Dict[str, Any] = dict(kb.get("review_overlay") or {})

    selected: List[ReviewKBRecord] = []
    related: List[ReviewKBRecord] = []
    seen: set[str] = set()

    def add(record: ReviewKBRecord | None, *, primary: bool = True) -> None:
        if record is None or record.id in seen:
            return
        if (
            record.kind == "summary"
            and not use_review_overlay
            and record.metadata.get("summary_scope") != "boundary"
            and str(record.metadata.get("distillation_scope") or "core_repository") in {"review_neighborhood", "on_demand"}
        ):
            return
        seen.add(record.id)
        (selected if primary else related).append(record)

    norm_path = _norm_path(path or "")
    symbol_key = (symbol or "").strip().lower()
    if norm_path:
        for record in files:
            if record.metadata.get("file_path") == norm_path:
                add(record)
                for summary in summaries:
                    if (
                        summary.metadata.get("summary_scope") == "boundary"
                        and summary.metadata.get("boundary_scope") == "file"
                        and summary.metadata.get("file_path") == norm_path
                    ):
                        add(summary, primary=False)
                for sid in record.metadata.get("symbols") or []:
                    add(by_id.get(str(sid)), primary=False)
                for fid in record.metadata.get("facts") or []:
                    add(by_id.get(str(fid)), primary=False)
                break

    if use_review_overlay and not norm_path and not symbol_key:
        overlay_paths = [
            _norm_path(str(p))
            for p in review_overlay.get("changed_files", [])
            if str(p).strip()
        ]
        for overlay_path in overlay_paths[: max(1, max_results)]:
            for record in files:
                if record.metadata.get("file_path") == overlay_path:
                    add(record)
                    for sid in record.metadata.get("symbols") or []:
                        add(by_id.get(str(sid)), primary=False)
                    break

    if symbol_key:
        for record in symbols:
            candidates = {
                str(record.metadata.get("symbol_name") or "").lower(),
                str(record.metadata.get("qualified_name") or "").lower(),
                str(record.metadata.get("symbol_node_id") or "").lower(),
            }
            if symbol_key in candidates or any(c.endswith(f":{symbol_key}") for c in candidates):
                add(record)

    if community_id is not None:
        for record in summaries:
            if (
                record.metadata.get("summary_scope") == "boundary"
                and _metadata_int(record, "community_id") == int(community_id)
            ):
                add(record, primary=False)
        for record in communities:
            if _metadata_int(record, "community_id") == int(community_id):
                add(record, primary=False)
                break
        for record in summaries:
            if _metadata_int(record, "community_id") == int(community_id):
                add(record, primary=False)

    if include_dependencies:
        seed_nodes = {
            str(r.metadata.get("symbol_node_id") or "")
            for r in [*selected, *related]
            if r.kind == "symbol" and r.metadata.get("symbol_node_id")
        }
        for record in [*selected, *related]:
            if record.kind != "file":
                continue
            for symbol_record_id in record.metadata.get("symbols") or []:
                sym = by_id.get(str(symbol_record_id))
                if sym is None:
                    continue
                node_id = str(sym.metadata.get("symbol_node_id") or "")
                if node_id:
                    seed_nodes.add(node_id)
        for edge in edges:
            src = str(edge.metadata.get("source") or "")
            dst = str(edge.metadata.get("target") or "")
            if src in seed_nodes:
                add(edge, primary=False)
                target_id = str(edge.metadata.get("target_record_id") or "")
                add(by_id.get(target_id), primary=False)
            elif dst in seed_nodes:
                add(edge, primary=False)
                source_id = str(edge.metadata.get("source_record_id") or "")
                add(by_id.get(source_id), primary=False)

    topic_terms = [t.strip().lower() for t in (topics or []) if t and t.strip()]
    query_terms = _terms(query)
    for record in facts:
        tags = {str(t).lower() for t in record.tags}
        haystack = " ".join(_lexical_text_parts(record)).lower()
        if any(t in tags or t in haystack for t in topic_terms) or any(t in haystack for t in query_terms):
            add(record)

    def summary_rank(record: ReviewKBRecord) -> tuple[int, str]:
        scope = str(record.metadata.get("summary_scope") or "")
        if scope == "boundary":
            return (0, record.id)
        if scope == "community":
            return (1, record.id)
        if scope == "community_shard":
            return (2, record.id)
        if scope == "repo":
            return (3, record.id)
        return (4, record.id)

    for record in sorted(summaries, key=summary_rank):
        haystack = " ".join(_lexical_text_parts(record)).lower()
        if any(t in haystack for t in topic_terms) or any(t in haystack for t in query_terms):
            add(record, primary=False)

    for term in [*topic_terms, *query_terms]:
        for rid in lexical.get(term, [])[:50]:
            add(by_id.get(str(rid)), primary=False)

    primary = selected[:max_results]
    related_budget = max(0, max_results - len(primary))
    related_out = related[:related_budget]
    total_found = len(selected) + len(related)
    evidence: List[ReviewKBEvidence] = []
    for record in [*primary, *related_out]:
        evidence.extend(record.evidence)
    return ReviewKBResult(
        query=query,
        primary_records=primary,
        related_records=related_out,
        evidence=evidence[: max_results * 2],
        omitted_count=max(0, total_found - len(primary) - len(related_out)),
        diagnostics={
            "path": norm_path,
            "symbol": symbol or "",
            "topics": topic_terms,
            "use_review_overlay": use_review_overlay,
            "overlay_changed_files": len(review_overlay.get("changed_files") or []),
            "max_results": max_results,
            "records_considered": len(by_id),
        },
    )


query_loaded_repository_kb = query_loaded_review_kb

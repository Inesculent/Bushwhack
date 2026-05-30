"""Write Phase 2 exploration snapshots to disk (community-sharded layout)."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from src.config import Settings
from src.domain.schemas import (
    CommunitySemanticSummary,
    ExplorationSnapshot,
    ReviewKBRecord,
    SnapshotDiagnostics,
    StructuralTopologySummary,
    UnverifiedCallTarget,
)
from src.infrastructure.review_kb import build_review_kb, rebuild_review_kb_lexical_index, write_review_kb
from src.infrastructure.structural_graph import StructuralGraphBuilder


def _slug_label(label: str, *, max_len: int = 48) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "_", label.strip().lower()).strip("_")
    if not cleaned:
        cleaned = "community"
    return cleaned[:max_len]


def _safe_path_segment(value: str, *, max_len: int = 160) -> str:
    """Return a cross-platform safe single path segment while preserving readability."""
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip()).strip(" ._")
    if not cleaned:
        cleaned = "run"
    return cleaned[:max_len]


def _render_community_markdown(summary: CommunitySemanticSummary, calls: Sequence[UnverifiedCallTarget]) -> str:
    lines: List[str] = [
        f"# Community {summary.community_id}: {summary.label}",
        "",
        f"**Purpose:** {summary.purpose}",
        "",
        "## Files",
    ]
    for fs in summary.file_summaries:
        lines.append(f"- `{fs.file_node_id}`: {fs.purpose} (confidence {fs.confidence:.2f})")
    lines.append("")
    lines.append("## Symbols")
    for ss in summary.symbol_summaries:
        lines.append(f"- `{ss.symbol_node_id}`: {ss.purpose} (confidence {ss.confidence:.2f})")
        if ss.rationale:
            lines.append(f"  - _Rationale:_ {ss.rationale}")
    lines.append("")
    lines.append("## Cross-community dependencies")
    lines.append(", ".join(str(c) for c in summary.cross_community_dependencies) or "(none)")
    lines.append("")
    lines.append("## Unverified / resolved calls")
    for c in calls:
        status = "resolved" if c.resolved else "unresolved"
        lines.append(
            f"- {status}: `{c.target_name}` from `{c.source_symbol_id}` — {c.context_hint[:200]}"
        )
        if c.resolution_summary:
            lines.append(f"  - {c.resolution_summary}")
    return "\n".join(lines) + "\n"


class SnapshotWriter:
    """Deterministic writer for the Phase 2 snapshot directory tree."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def write_snapshot(
        self,
        *,
        run_id: str,
        repo_path: str,
        enriched_graph_payload: Dict[str, Any],
        topology: StructuralTopologySummary,
        community_summaries: Sequence[CommunitySemanticSummary],
        global_summary: str,
        diagnostics: SnapshotDiagnostics,
        unresolved_calls: Sequence[UnverifiedCallTarget],
        extraction_gap_count: int,
        changed_file_paths: Iterable[str] = (),
        repository_kb_summary_records: Sequence[Dict[str, Any]] = (),
    ) -> tuple[ExplorationSnapshot, str]:
        run_dir_name = _safe_path_segment(run_id)
        base = Path(self._settings.snapshot_base_path).resolve() / run_dir_name
        graph_dir = base / "graph"
        sem_dir = base / "semantic"
        comm_graph_dir = graph_dir / "communities"
        comm_sem_dir = sem_dir / "communities"
        literal_dir = base / "literal"
        for d in (comm_graph_dir, comm_sem_dir, literal_dir):
            d.mkdir(parents=True, exist_ok=True)

        graph = StructuralGraphBuilder.deserialize(enriched_graph_payload)
        partition = dict(topology.node_to_community)
        kb_bundle = build_review_kb(
            run_id=run_id,
            repo_path=repo_path,
            graph_payload=enriched_graph_payload,
            topology=topology,
            changed_file_paths=changed_file_paths,
            repo_identity=repo_path,
        )
        if repository_kb_summary_records:
            parsed_summaries: List[ReviewKBRecord] = []
            expected = {r.id for r in kb_bundle.summaries}
            for raw in repository_kb_summary_records:
                try:
                    record = ReviewKBRecord.model_validate(raw)
                except Exception:
                    continue
                if record.kind == "summary" and (
                    record.id in expected or record.metadata.get("summary_scope") == "community_shard"
                ):
                    parsed_summaries.append(record)
            if parsed_summaries:
                by_id = {r.id: r for r in kb_bundle.summaries}
                appended_ids: List[str] = []
                for record in parsed_summaries:
                    if record.id not in by_id:
                        appended_ids.append(record.id)
                    by_id[record.id] = record
                kb_bundle.summaries = [by_id[r.id] for r in kb_bundle.summaries] + [
                    by_id[rid] for rid in appended_ids
                ]
                kb_bundle.manifest.counts["summaries"] = len(kb_bundle.summaries)
                rebuild_review_kb_lexical_index(kb_bundle)

        # Full graph
        full_path = graph_dir / "full_graph.json"
        full_path.write_text(
            json.dumps(enriched_graph_payload, sort_keys=True, indent=2),
            encoding="utf-8",
        )

        topology_path = graph_dir / "topology.json"
        topology_path.write_text(
            topology.model_dump_json(indent=2),
            encoding="utf-8",
        )

        kb_dir = write_review_kb(base, kb_bundle)

        cross_path = graph_dir / "cross_community_edges.json"
        cross_path.write_text(
            json.dumps(diagnostics.cross_community_edges, indent=2, sort_keys=True),
            encoding="utf-8",
        )

        calls_by_community: Dict[int, List[UnverifiedCallTarget]] = {c.community_id: [] for c in community_summaries}
        for t in unresolved_calls:
            cid = t.source_community_id
            calls_by_community.setdefault(cid, []).append(t)

        for comm in topology.communities:
            cid = comm.community_id
            members = set(comm.node_ids)
            sub_nodes: List[Dict[str, Any]] = []
            for node_id in members:
                if not graph.has_node(node_id):
                    continue
                attrs = dict(graph.nodes[node_id])
                if attrs.get("node_type") == "symbol":
                    fp = attrs.get("file_path")
                    attrs["literal_pointer"] = {
                        "file": fp,
                        "start_line": attrs.get("start_line"),
                        "end_line": attrs.get("end_line"),
                    }
                sub_nodes.append({"id": node_id, **attrs})
            sub_edges: List[Dict[str, Any]] = []
            for u, v, data in graph.edges(data=True):
                if u in members and v in members:
                    row = dict(data)
                    row["source"] = u
                    row["target"] = v
                    sub_edges.append(row)
            shard = {"community_id": cid, "nodes": sub_nodes, "edges": sub_edges}
            (comm_graph_dir / f"{cid:02d}.json").write_text(
                json.dumps(shard, indent=2, sort_keys=True),
                encoding="utf-8",
            )

        summary_by_id = {s.community_id: s for s in community_summaries}
        for comm in topology.communities:
            summ = summary_by_id.get(comm.community_id)
            if summ is None:
                continue
            slug = _slug_label(summ.label)
            md = _render_community_markdown(summ, calls_by_community.get(comm.community_id, []))
            (comm_sem_dir / f"{comm.community_id:02d}_{slug}.md").write_text(md, encoding="utf-8")

        (sem_dir / "global_summary.md").write_text(global_summary + "\n", encoding="utf-8")
        (sem_dir / "diagnostics.json").write_text(
            diagnostics.model_dump_json(indent=2),
            encoding="utf-8",
        )

        unresolved_count = sum(1 for t in unresolved_calls if not t.resolved)
        status = "exploration_complete" if unresolved_count == 0 else "partial"
        pointer_body = {
            "run_id": run_id,
            "run_dir_name": run_dir_name,
            "repo_path": repo_path,
            "status": status,
            "community_count": topology.community_count,
            "total_nodes": len(enriched_graph_payload.get("nodes", [])),
            "total_edges": len(enriched_graph_payload.get("edges", [])),
            "unresolved_call_count": unresolved_count,
            "extraction_gap_count": extraction_gap_count,
            "review_kb_path": str(kb_dir),
            "repository_kb_path": str(kb_dir),
        }
        snapshot_id = hashlib.sha256(
            json.dumps(pointer_body, sort_keys=True).encode("utf-8")
        ).hexdigest()[:16]
        pointer_body["snapshot_id"] = snapshot_id
        pointer_body["snapshot_root"] = str(base)

        snap = ExplorationSnapshot(
            snapshot_id=snapshot_id,
            run_id=run_id,
            snapshot_root=str(base),
            status=status,  # type: ignore[arg-type]
            community_count=topology.community_count,
            total_nodes=int(pointer_body["total_nodes"]),
            total_edges=int(pointer_body["total_edges"]),
            unresolved_call_count=int(unresolved_count),
            extraction_gap_count=int(extraction_gap_count),
            metadata={
                "repo_path": repo_path,
                "run_dir_name": run_dir_name,
                "review_kb_path": str(kb_dir),
                "repository_kb_path": str(kb_dir),
                "review_kb": kb_bundle.manifest.model_dump(mode="json"),
                "repository_kb": kb_bundle.manifest.model_dump(mode="json"),
                "review_overlay": kb_bundle.review_overlay,
            },
        )

        snapshot_json = base / "snapshot.json"
        snapshot_json.write_text(
            json.dumps({**pointer_body, "exploration_snapshot": snap.model_dump(mode="json")}, indent=2),
            encoding="utf-8",
        )

        if not self._settings.snapshot_keep_full_graph:
            try:
                full_path.unlink()
            except OSError:
                pass

        return snap, str(base)

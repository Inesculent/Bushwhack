"""Pin exploration snapshot to disk and publish pointer (Phase 2)."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Sequence

from src.config import Settings, get_settings
from src.domain.schemas import (
    CommunitySemanticSummary,
    SnapshotDiagnostics,
    StructuralTopologySummary,
    UnverifiedCallTarget,
)
from src.domain.state import GraphState
from src.infrastructure.snapshot_pointer_store import SnapshotPointerStore
from src.infrastructure.snapshot_writer import SnapshotWriter

logger = logging.getLogger(__name__)


def _coerce_summaries(raw: Sequence[Any]) -> List[CommunitySemanticSummary]:
    out: List[CommunitySemanticSummary] = []
    for item in raw:
        if isinstance(item, CommunitySemanticSummary):
            out.append(item)
        elif isinstance(item, dict):
            try:
                out.append(CommunitySemanticSummary.model_validate(item))
            except Exception:
                continue
    return out


def _coerce_calls(state: GraphState) -> List[UnverifiedCallTarget]:
    resolved = state.get("resolved_unverified_calls")
    if resolved:
        rows: List[UnverifiedCallTarget] = []
        for item in resolved:
            if isinstance(item, UnverifiedCallTarget):
                rows.append(item)
            elif isinstance(item, dict):
                try:
                    rows.append(UnverifiedCallTarget.model_validate(item))
                except Exception:
                    continue
        return rows
    rows = []
    for item in state.get("unverified_call_targets", []) or []:
        if isinstance(item, UnverifiedCallTarget):
            rows.append(item)
        elif isinstance(item, dict):
            try:
                rows.append(UnverifiedCallTarget.model_validate(item))
            except Exception:
                continue
    return rows


def make_snapshot_pin_node(
    writer: SnapshotWriter,
    pointer_store: SnapshotPointerStore,
    *,
    settings: Settings | None = None,
):
    """Materialize snapshot tree and write coordination pointer."""

    def snapshot_pin_node(state: GraphState) -> Dict[str, Any]:
        # Loaded exploration snapshot: do not materialize a second on-disk tree; merge refs only.
        if state.get("snapshot_source") == "loaded":
            meta = dict(state.get("metadata", {}) or {})
            snap_out: Dict[str, Any] = dict(meta.get("exploration_snapshot") or {})
            if state.get("snapshot_id") is not None:
                snap_out["snapshot_id"] = state["snapshot_id"]
            if state.get("snapshot_root") is not None:
                snap_out["snapshot_root"] = state["snapshot_root"]
            inner_meta = dict(snap_out.get("metadata") or {})
            if state.get("behavioral_spec_ref"):
                inner_meta["behavioral_spec_ref"] = state["behavioral_spec_ref"]
            if inner_meta:
                snap_out["metadata"] = inner_meta
            meta["exploration_snapshot"] = snap_out
            return {
                "metadata": meta,
                "node_history": ["snapshot_pin:loaded_passthrough"],
                "next_step": "plan",
            }

        run_id = str(state.get("run_id", "unknown"))
        repo_path = str(state.get("repo_path", "") or "")
        topo = state.get("structural_topology")
        if not isinstance(topo, StructuralTopologySummary):
            topo = StructuralTopologySummary.model_validate(topo) if topo else None
        graph_payload = state.get("structural_graph_node_link") or {}
        if topo is None or not isinstance(graph_payload, dict):
            logger.warning("snapshot_pin skipped: missing topology or graph")
            return {"node_history": ["snapshot_pin:skipped"]}

        summaries = _coerce_summaries(state.get("community_summaries") or [])
        calls = _coerce_calls(state)
        global_summary = str(state.get("global_summary") or "")
        diag_raw = (state.get("metadata") or {}).get("snapshot_diagnostics") or {}
        try:
            diagnostics = SnapshotDiagnostics.model_validate(diag_raw)
        except Exception:
            diagnostics = SnapshotDiagnostics()

        extraction_gap_count = len(state.get("structural_extraction_gaps") or [])

        snap, root = writer.write_snapshot(
            run_id=run_id,
            repo_path=repo_path,
            enriched_graph_payload=graph_payload,
            topology=topo,
            community_summaries=summaries,
            global_summary=global_summary,
            diagnostics=diagnostics,
            unresolved_calls=calls,
            extraction_gap_count=extraction_gap_count,
        )

        try:
            pointer_store.write_pointer(
                run_id=run_id,
                snapshot_id=snap.snapshot_id,
                snapshot_root=root,
                status="exploration_complete" if snap.unresolved_call_count == 0 else "partial",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("snapshot pointer store failed run_id=%s err=%s", run_id, exc)

        meta = dict(state.get("metadata", {}))
        snap_dump = snap.model_dump(mode="json")
        if state.get("behavioral_spec_ref"):
            m = dict(snap_dump.get("metadata") or {})
            m["behavioral_spec_ref"] = state["behavioral_spec_ref"]
            snap_dump["metadata"] = m
        meta["exploration_snapshot"] = snap_dump

        return {
            "snapshot_root": root,
            "snapshot_id": snap.snapshot_id,
            "next_step": "plan",
            "metadata": meta,
            "node_history": ["snapshot_pin"],
        }

    return snapshot_pin_node

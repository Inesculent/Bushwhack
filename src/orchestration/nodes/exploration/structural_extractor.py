from typing import Any, Dict, Optional

from src.config import get_settings
from src.domain.interfaces import IASTParser, IPreflightService
from src.domain.schemas import PreflightRequest, PreflightSummary, RunMetadata
from src.domain.state import GraphState
from src.infrastructure.structural_graph import StructuralGraphBuilder
from src.infrastructure.structural_topology import (
    apply_community_attributes,
    build_topology_summary,
    run_structural_topology,
)


def make_structural_extractor_node(
    preflight_service: IPreflightService,
    ast_parser: Optional[IASTParser],
):
    builder = StructuralGraphBuilder(ast_parser=ast_parser)

    def structural_extractor_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        repo_path = state.get("repo_path", "")
        git_diff = state.get("git_diff", "") or "\n"
        settings = get_settings()

        manifest = preflight_service.build_diff_manifest(
            PreflightRequest(
                run_metadata=RunMetadata(
                    repo=repo_path,
                    base_sha="unknown",
                    head_sha=run_id,
                    run_id=run_id,
                ),
                raw_diff=git_diff,
            )
        )

        build_result = builder.build(manifest=manifest, repository_path=repo_path)
        topology_summary = None
        if settings.structural_topology_enabled and build_result.graph.number_of_nodes() > 0:
            topo = run_structural_topology(
                build_result.graph,
                max_fraction=settings.community_max_fraction,
                min_split_size=settings.community_min_split_size,
                max_files=settings.community_max_files,
                max_symbols=settings.community_max_symbols,
                louvain_seed=settings.louvain_seed,
            )
            apply_community_attributes(build_result.graph, topo.partition)
            config_snapshot = {
                "structural_topology_enabled": settings.structural_topology_enabled,
                "community_max_fraction": settings.community_max_fraction,
                "community_min_split_size": settings.community_min_split_size,
                "community_max_files": settings.community_max_files,
                "community_max_symbols": settings.community_max_symbols,
                "louvain_seed": settings.louvain_seed,
            }
            topology_summary = build_topology_summary(topo, build_result.graph, config_snapshot)

        graph_payload = builder.serialize(build_result.graph)

        metadata = dict(state.get("metadata", {}))
        structural_meta: Dict[str, Any] = {
            "files_attempted": build_result.files_attempted,
            "files_parsed": build_result.files_parsed,
            "gap_count": len(build_result.gaps),
            "node_count": build_result.graph.number_of_nodes(),
            "edge_count": build_result.graph.number_of_edges(),
        }
        if topology_summary is not None:
            structural_meta["topology_algorithm"] = topology_summary.algorithm
            structural_meta["community_count"] = topology_summary.community_count
            structural_meta["topology_splits_applied"] = topology_summary.splits_applied
        metadata["structural_extractor"] = structural_meta

        preflight_summary = PreflightSummary(
            manifest_id=manifest.manifest_id,
            total_files_changed=manifest.aggregate_metrics.total_files_changed,
            total_hunks=manifest.aggregate_metrics.total_hunks,
            total_additions=manifest.aggregate_metrics.total_additions,
            total_deletions=manifest.aggregate_metrics.total_deletions,
            has_errors=bool(manifest.errors),
            has_ambiguity=bool(manifest.ambiguity_flags),
        )

        out: Dict[str, Any] = {
            "diff_manifest_ref": manifest.manifest_id,
            "preflight_summary": preflight_summary,
            "preflight_errors": manifest.errors,
            "preflight_warnings": manifest.warnings,
            "structural_graph_node_link": graph_payload,
            "structural_extraction_gaps": build_result.gaps,
            "metadata": metadata,
            "node_history": ["structural_extractor"],
            "next_step": "plan",
        }
        if topology_summary is not None:
            out["structural_topology"] = topology_summary
        return out

    return structural_extractor_node

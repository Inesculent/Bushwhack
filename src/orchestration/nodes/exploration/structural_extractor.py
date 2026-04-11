from typing import Any, Dict, Optional

from src.domain.interfaces import IASTParser, IPreflightService
from src.domain.schemas import PreflightRequest, PreflightSummary, RunMetadata
from src.domain.state import GraphState
from src.infrastructure.structural_graph import StructuralGraphBuilder


def make_structural_extractor_node(
    preflight_service: IPreflightService,
    ast_parser: Optional[IASTParser],
):
    builder = StructuralGraphBuilder(ast_parser=ast_parser)

    def structural_extractor_node(state: GraphState) -> Dict[str, Any]:
        run_id = state.get("run_id", "unknown")
        repo_path = state.get("repo_path", "")
        git_diff = state.get("git_diff", "") or "\n"

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
        graph_payload = builder.serialize(build_result.graph)

        metadata = dict(state.get("metadata", {}))
        metadata["structural_extractor"] = {
            "files_attempted": build_result.files_attempted,
            "files_parsed": build_result.files_parsed,
            "gap_count": len(build_result.gaps),
            "node_count": build_result.graph.number_of_nodes(),
            "edge_count": build_result.graph.number_of_edges(),
        }

        preflight_summary = PreflightSummary(
            manifest_id=manifest.manifest_id,
            total_files_changed=manifest.aggregate_metrics.total_files_changed,
            total_hunks=manifest.aggregate_metrics.total_hunks,
            total_additions=manifest.aggregate_metrics.total_additions,
            total_deletions=manifest.aggregate_metrics.total_deletions,
            has_errors=bool(manifest.errors),
            has_ambiguity=bool(manifest.ambiguity_flags),
        )

        return {
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

    return structural_extractor_node

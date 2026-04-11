from typing import Optional

from src.domain.interfaces import IASTParser, IPreflightService
from src.domain.schemas import CodeEntity, DiffFileManifestEntry, DiffManifest, DiffManifestAggregateMetrics, PreflightRequest, RunMetadata
from src.domain.state import GraphState
from src.orchestration.nodes.exploration.structural_extractor import make_structural_extractor_node


class _FakePreflightService(IPreflightService):
    def build_diff_manifest(self, request: PreflightRequest) -> DiffManifest:
        return DiffManifest(
            manifest_id="manifest-123",
            run_metadata=request.run_metadata,
            files=[DiffFileManifestEntry(filepath="src/demo.py", change_type="M")],
            aggregate_metrics=DiffManifestAggregateMetrics(total_files_changed=1),
            warnings=["demo warning"],
        )


class _FakeASTParser(IASTParser):
    def get_file_structure(self, repository_path: str, file_path: str) -> list[CodeEntity]:
        return [
            CodeEntity(
                name="demo",
                type="function",
                signature="def demo():",
                body="def demo():\n    import os\n    return 1",
                dependencies=["os"],
            )
        ]

    def get_entity_details(
        self,
        repository_path: str,
        file_path: str,
        entity_name: str,
    ) -> Optional[CodeEntity]:
        if entity_name == "demo":
            return CodeEntity(
                name="demo",
                type="function",
                signature="def demo():",
                body="def demo():\n    import os\n    return 1",
                dependencies=["os"],
            )
        return None


def test_structural_extractor_node_outputs_graph_payload() -> None:
    node = make_structural_extractor_node(
        preflight_service=_FakePreflightService(),
        ast_parser=_FakeASTParser(),
    )

    state: GraphState = {
        "run_id": "run-1",
        "repo_path": "repo",
        "git_diff": "diff --git a/src/demo.py b/src/demo.py\n",
    }
    result = node(state)

    assert result["diff_manifest_ref"] == "manifest-123"
    assert result["next_step"] == "plan"
    assert result["node_history"] == ["structural_extractor"]
    assert result["preflight_warnings"] == ["demo warning"]
    assert result["structural_extraction_gaps"] == []
    assert result["structural_graph_node_link"]["nodes"]
    assert result["structural_graph_node_link"]["edges"]

from typing import Dict, List, Optional

from src.domain.interfaces import IASTParser
from src.domain.schemas import CodeEntity, DiffFileManifestEntry, DiffManifest, DiffManifestAggregateMetrics, RunMetadata
from src.infrastructure.structural_graph import StructuralGraphBuilder


class _FakeASTParser(IASTParser):
    def __init__(self, by_file: Dict[str, List[CodeEntity]], fail_files: Optional[set[str]] = None) -> None:
        self.by_file = by_file
        self.fail_files = fail_files or set()

    def get_file_structure(self, repository_path: str, file_path: str) -> List[CodeEntity]:
        if file_path in self.fail_files:
            raise RuntimeError("parse failed")
        return self.by_file.get(file_path, [])

    def get_entity_details(
        self,
        repository_path: str,
        file_path: str,
        entity_name: str,
    ) -> Optional[CodeEntity]:
        for item in self.by_file.get(file_path, []):
            if item.name == entity_name:
                return item
        return None


def _manifest() -> DiffManifest:
    files = [
        DiffFileManifestEntry(filepath="src/b.py", change_type="M"),
        DiffFileManifestEntry(filepath="src/a.py", change_type="A"),
    ]
    return DiffManifest(
        manifest_id="m1",
        run_metadata=RunMetadata(repo="repo", base_sha="base", head_sha="head"),
        files=files,
        aggregate_metrics=DiffManifestAggregateMetrics(total_files_changed=2),
    )


def test_structural_graph_builder_is_deterministic() -> None:
    parser = _FakeASTParser(
        by_file={
            "src/a.py": [
                CodeEntity(
                    name="alpha",
                    type="function",
                    signature="def alpha():",
                    body="def alpha():\n    import os",
                    dependencies=["os"],
                )
            ],
            "src/b.py": [
                CodeEntity(
                    name="beta",
                    type="function",
                    signature="def beta():",
                    body="def beta():\n    import json",
                    dependencies=["json"],
                )
            ],
        }
    )
    builder = StructuralGraphBuilder(ast_parser=parser)

    first = builder.build(_manifest(), repository_path="/repo")
    second = builder.build(_manifest(), repository_path="/repo")

    assert first.files_attempted == 2
    assert first.files_parsed == 2
    assert first.gaps == []
    assert builder.serialize(first.graph) == builder.serialize(second.graph)


def test_structural_graph_builder_records_parse_gaps() -> None:
    parser = _FakeASTParser(
        by_file={
            "src/a.py": [
                CodeEntity(
                    name="alpha",
                    type="function",
                    signature="def alpha():",
                    body="def alpha():\n    return 1",
                    dependencies=[],
                )
            ]
        },
        fail_files={"src/b.py"},
    )
    builder = StructuralGraphBuilder(ast_parser=parser)

    result = builder.build(_manifest(), repository_path="/repo")

    assert result.files_attempted == 2
    assert result.files_parsed == 1
    assert len(result.gaps) == 1
    assert result.gaps[0].filepath == "src/b.py"
    assert result.gaps[0].reason == "ast_parse_failed"

from pathlib import Path
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


def _write_repo_file(repo_root: Path, relative_path: str, content: str = "") -> None:
    target = repo_root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content or "# demo\n", encoding="utf-8")


def test_structural_graph_builder_is_deterministic(tmp_path: Path) -> None:
    _write_repo_file(tmp_path, "src/a.py", "def alpha():\n    return 1\n")
    _write_repo_file(tmp_path, "src/b.py", "def beta():\n    return 1\n")

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

    first = builder.build(_manifest(), repository_path=str(tmp_path))
    second = builder.build(_manifest(), repository_path=str(tmp_path))

    assert first.files_attempted == 2
    assert first.files_parsed == 2
    assert first.gaps == []
    assert builder.serialize(first.graph) == builder.serialize(second.graph)


def test_structural_graph_builder_records_parse_gaps(tmp_path: Path) -> None:
    _write_repo_file(tmp_path, "src/a.py", "def alpha():\n    return 1\n")
    _write_repo_file(tmp_path, "src/b.py", "def beta():\n    return 1\n")

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

    result = builder.build(_manifest(), repository_path=str(tmp_path))

    assert result.files_attempted == 2
    assert result.files_parsed == 1
    assert len(result.gaps) == 1
    assert result.gaps[0].filepath == "src/b.py"
    assert result.gaps[0].reason == "ast_parse_failed"


def test_structural_graph_builder_emits_holistic_structural_edges(tmp_path: Path) -> None:
    _write_repo_file(tmp_path, "src/a.py", "# source")

    parser = _FakeASTParser(
        by_file={
            "src/a.py": [
                CodeEntity(
                    name="Base",
                    type="class",
                    signature="class Base:",
                    body="class Base:\n    pass",
                    dependencies=[],
                ),
                CodeEntity(
                    name="Child",
                    type="class",
                    signature="class Child(Base):",
                    body="class Child(Base):\n    pass",
                    dependencies=[],
                ),
                CodeEntity(
                    name="target",
                    type="function",
                    signature="def target():",
                    body="def target():\n    return 1",
                    dependencies=[],
                ),
                CodeEntity(
                    name="observer",
                    type="function",
                    signature="def observer():",
                    body="def observer():\n    return 2",
                    dependencies=[],
                ),
                CodeEntity(
                    name="caller",
                    type="function",
                    signature="def caller():",
                    body=(
                        "def caller():\n"
                        "    import os\n"
                        "    target()\n"
                        "    alias = target\n"
                        "    marker = observer\n"
                        "    return alias\n"
                    ),
                    dependencies=["os"],
                ),
            ]
        }
    )
    manifest = DiffManifest(
        manifest_id="m2",
        run_metadata=RunMetadata(repo="repo", base_sha="base", head_sha="head"),
        files=[DiffFileManifestEntry(filepath="src/a.py", change_type="M")],
        aggregate_metrics=DiffManifestAggregateMetrics(total_files_changed=1),
    )

    builder = StructuralGraphBuilder(ast_parser=parser)
    result = builder.build(manifest, repository_path=str(tmp_path))

    edge_types = {attrs["edge_type"] for _, _, attrs in result.graph.edges(data=True)}
    assert "defines" in edge_types
    assert "imports" in edge_types
    assert "calls" in edge_types
    assert "inherits" in edge_types
    assert "references" in edge_types


def test_structural_graph_builder_scans_supported_repo_files_beyond_diff(tmp_path: Path) -> None:
    _write_repo_file(tmp_path, "src/a.py", "def alpha():\n    return 1\n")
    _write_repo_file(tmp_path, "src/adjacent.py", "def adjacent():\n    return 2\n")
    _write_repo_file(tmp_path, "docs/readme.md", "# ignored non-ast extension\n")

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
            ],
            "src/adjacent.py": [
                CodeEntity(
                    name="adjacent",
                    type="function",
                    signature="def adjacent():",
                    body="def adjacent():\n    return 2",
                    dependencies=[],
                )
            ],
        }
    )
    manifest = DiffManifest(
        manifest_id="m3",
        run_metadata=RunMetadata(repo="repo", base_sha="base", head_sha="head"),
        files=[DiffFileManifestEntry(filepath="src/a.py", change_type="M")],
        aggregate_metrics=DiffManifestAggregateMetrics(total_files_changed=1),
    )

    builder = StructuralGraphBuilder(ast_parser=parser)
    result = builder.build(manifest, repository_path=str(tmp_path))

    assert result.files_attempted == 2
    assert result.files_parsed == 2
    node_ids = {node_id for node_id, _ in result.graph.nodes(data=True)}
    assert "file:src/a.py" in node_ids
    assert "file:src/adjacent.py" in node_ids

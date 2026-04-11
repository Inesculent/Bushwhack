from pathlib import Path

import pytest

from src.infrastructure.ast.native_parser import NativeASTParser
from src.infrastructure.cache.memory_cache import InMemoryCache


def test_get_file_structure_extracts_entities_for_python_file(tmp_path: Path) -> None:
    repo_root = tmp_path
    source_file = repo_root / "sample.py"
    source_file.write_text(
        """
class Demo:
    def method(self) -> int:
        return 1


def demo() -> int:
    import json
    return len(json.dumps({"ok": True}))
""".strip()
        + "\n",
        encoding="utf-8",
    )

    parser = NativeASTParser(cache=InMemoryCache())

    entities = parser.get_file_structure(str(repo_root), "sample.py")
    names = {entity.name for entity in entities}

    assert "Demo" in names
    assert "demo" in names
    assert any(entity.name == "demo" and "json" in entity.dependencies for entity in entities)


def test_get_entity_details_returns_match(tmp_path: Path) -> None:
    repo_root = tmp_path
    source_file = repo_root / "service.py"
    source_file.write_text(
        """
def build_payload() -> dict:
    return {"ok": True}
""".strip()
        + "\n",
        encoding="utf-8",
    )

    parser = NativeASTParser(cache=InMemoryCache())

    entity = parser.get_entity_details(str(repo_root), "service.py", "build_payload")

    assert entity is not None
    assert entity.name == "build_payload"


def test_get_file_structure_rejects_path_escape(tmp_path: Path) -> None:
    parser = NativeASTParser(cache=InMemoryCache())

    with pytest.raises(ValueError, match="inside repository_path"):
        parser.get_file_structure(str(tmp_path), "../outside.py")


def test_get_file_structure_rejects_unsupported_extension(tmp_path: Path) -> None:
    repo_root = tmp_path
    source_file = repo_root / "notes.txt"
    source_file.write_text("hello", encoding="utf-8")

    parser = NativeASTParser(cache=InMemoryCache())

    with pytest.raises(ValueError, match="Unsupported file extension"):
        parser.get_file_structure(str(repo_root), "notes.txt")

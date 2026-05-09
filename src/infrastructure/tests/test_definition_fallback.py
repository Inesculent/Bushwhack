from pathlib import Path

from src.infrastructure.search.definition_fallback import find_symbol_definitions_regex


def test_regex_fallback_finds_python_definition(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text(
        "\n\nclass Thing:\n    pass\n\n\ndef target(x):\n    return x\n",
        encoding="utf-8",
    )
    defs = find_symbol_definitions_regex(str(tmp_path), "target", max_results=10)
    assert defs
    assert defs[0].entity_name == "target"
    assert defs[0].source == "regex"
    assert "pkg/mod.py" in defs[0].file_path.replace("\\", "/")


def test_regex_fallback_respects_candidate_paths(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("def foo():\n    pass\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("def bar():\n    pass\n", encoding="utf-8")
    defs = find_symbol_definitions_regex(
        str(tmp_path),
        "foo",
        candidate_file_paths=["b.py"],
        max_results=10,
    )
    assert defs == []

from pathlib import Path

from src.infrastructure.source_provenance import collect_source_provenance


def _write(root: Path, relative_path: str, content: str) -> None:
    path = root / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_source_fingerprint_is_stable_and_tracks_runtime_sources(tmp_path: Path) -> None:
    _write(tmp_path, "src/example.py", "value = 1\n")
    _write(tmp_path, "src/prompt.md", "Review carefully.\n")
    _write(tmp_path, "src/example.pyc", "ignored")

    first = collect_source_provenance(tmp_path)
    second = collect_source_provenance(tmp_path)

    assert first["source_tree_sha256"] == second["source_tree_sha256"]
    assert first["source_file_count"] == 2
    assert first["git"]["available"] is False

    _write(tmp_path, "src/example.py", "value = 2\n")
    changed = collect_source_provenance(tmp_path)

    assert changed["source_tree_sha256"] != first["source_tree_sha256"]

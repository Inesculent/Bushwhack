"""Best-effort provenance for the Bushwhack source loaded by a run."""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path
from typing import Any


_SOURCE_SUFFIXES = frozenset({".md", ".py"})


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def collect_source_provenance(repository_root: Path | None = None) -> dict[str, Any]:
    """Fingerprint runtime Python and prompt sources without requiring Git."""
    root = (repository_root or Path(__file__).resolve().parents[2]).resolve()
    source_root = root / "src"
    source_files = sorted(
        (
            path
            for path in source_root.rglob("*")
            if path.is_file() and path.suffix.lower() in _SOURCE_SUFFIXES
        ),
        key=lambda path: path.relative_to(root).as_posix(),
    )

    digest = hashlib.sha256(b"bushwhack-runtime-source-v1\0")
    for path in source_files:
        relative_path = path.relative_to(root).as_posix()
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())

    git_commit = _git_output(root, "rev-parse", "HEAD")
    git_root = _git_output(root, "rev-parse", "--show-toplevel") if git_commit else None
    git_status = _git_output(root, "status", "--porcelain") if git_commit else None

    return {
        "schema_version": 1,
        "repository_root": str(root),
        "source_tree_sha256": digest.hexdigest(),
        "source_file_count": len(source_files),
        "included_extensions": sorted(_SOURCE_SUFFIXES),
        "git": {
            "available": git_commit is not None,
            "root": git_root,
            "commit": git_commit,
            "dirty": bool(git_status) if git_status is not None else None,
        },
    }

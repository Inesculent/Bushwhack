"""Persist BehavioralSpec outside GraphState (pointer-only in checkpoints)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Tuple

from src.config import Settings
from src.domain.schemas import BehavioralSpec


def _safe_path_segment(value: str, *, max_len: int = 160) -> str:
    cleaned = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "_", value.strip()).strip(" ._")
    if not cleaned:
        cleaned = "run"
    return cleaned[:max_len]


class BehavioralSpecStore:
    """Read/write behavioral_spec.json under the run directory (alongside snapshots)."""

    def __init__(self, settings: Settings) -> None:
        self._base = Path(settings.snapshot_base_path).resolve()

    def ref_for_run(self, run_id: str) -> str:
        """Opaque ref string consumed by query_mental_model."""
        run_dir = _safe_path_segment(run_id)
        path = self._base / run_dir / "mental_model" / "behavioral_spec.json"
        return f"file:{path.as_posix()}"

    def path_from_ref(self, ref: str) -> Path | None:
        if not ref or not ref.startswith("file:"):
            return None
        return Path(ref.removeprefix("file:"))

    def write(self, run_id: str, spec: BehavioralSpec) -> Tuple[str, str]:
        """Write spec to disk; returns (ref, absolute path as string)."""
        run_dir_name = _safe_path_segment(run_id)
        dir_path = self._base / run_dir_name / "mental_model"
        dir_path.mkdir(parents=True, exist_ok=True)
        file_path = dir_path / "behavioral_spec.json"
        file_path.write_text(spec.model_dump_json(indent=2), encoding="utf-8")
        return f"file:{file_path.as_posix()}", str(file_path.resolve())

    def read(self, ref: str) -> BehavioralSpec:
        path = self.path_from_ref(ref)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"BehavioralSpec not found for ref={ref!r}")
        data = json.loads(path.read_text(encoding="utf-8"))
        return BehavioralSpec.model_validate(data)

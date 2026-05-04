from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Mapping


PROMPT_ROOT = Path(__file__).resolve().parent / "reviewer"


@lru_cache(maxsize=32)
def load_reviewer_prompt(relative_path: str) -> str:
    prompt_path = (PROMPT_ROOT / relative_path).resolve()
    prompt_path.relative_to(PROMPT_ROOT.resolve())
    return prompt_path.read_text(encoding="utf-8").strip()


def render_reviewer_prompt(
    role_prompt_path: str,
    sections: Mapping[str, str],
    *,
    include_global: bool = True,
) -> str:
    parts: list[str] = []
    if include_global:
        parts.append(load_reviewer_prompt("global.md"))
    parts.append(load_reviewer_prompt(role_prompt_path))

    for title, content in sections.items():
        cleaned = content.strip()
        if not cleaned:
            continue
        parts.append(f"## {title}\n{cleaned}")

    return "\n\n".join(parts)

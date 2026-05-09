"""Verifier subgraph package (lazy import to avoid pulling optional deps on submodules)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

__all__ = ["invoke_verifier_for_candidate"]

if TYPE_CHECKING:
    from src.orchestration.nodes.verifier.verifier_runner import invoke_verifier_for_candidate


def __getattr__(name: str) -> Any:
    if name == "invoke_verifier_for_candidate":
        from src.orchestration.nodes.verifier.verifier_runner import invoke_verifier_for_candidate

        return invoke_verifier_for_candidate
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

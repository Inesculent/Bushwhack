"""Execution profiles: --local (Docker) vs --remote (Apptainer)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal

RunProfile = Literal["local", "remote"]

_PROFILE_ENV = "REVIEW_RUN_PROFILE"
_BACKEND_ENV = "REVIEW_SANDBOX_BACKEND"


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _default_sif_dir() -> Path:
    return _repo_root() / "containers"


def apply_run_profile(profile: RunProfile) -> None:
    """Apply environment presets for local or cluster execution.

    Callers should invoke ``get_settings.cache_clear()`` after this function.
    """
    os.environ[_PROFILE_ENV] = profile

    if profile == "local":
        os.environ[_BACKEND_ENV] = "docker"
        os.environ["REVIEW_REDIS_ENABLED"] = "true"
        os.environ["REVIEW_REDIS_URL"] = "redis://localhost:6379/0"
        return

    os.environ[_BACKEND_ENV] = "apptainer"
    os.environ.setdefault("REVIEW_REDIS_ENABLED", "true")
    os.environ.setdefault("REVIEW_REDIS_URL", "redis://127.0.0.1:6379/0")
    os.environ["REVIEW_AST_MCP_ENABLED"] = "false"
    os.environ["REVIEW_LOCAL_LLM_BASE_URL"] = "http://127.0.0.1:8000/v1"

    sif_dir = _default_sif_dir()
    review_sif = sif_dir / "agent-fs-sandbox.sif"
    verifier_sif = sif_dir / "verifier-test-env.sif"
    os.environ["REVIEW_APPTAINER_IMAGE"] = str(review_sif)
    os.environ["REVIEW_APPTAINER_VERIFIER_IMAGE"] = str(verifier_sif)

    scratch = (
        os.environ.get("SLURM_TMPDIR", "").strip()
        or os.environ.get("TMPDIR", "").strip()
        or "/tmp"
    )
    os.environ.setdefault("APPTAINER_CACHEDIR", f"{scratch}/apptainer-cache")
    os.environ.setdefault("SINGULARITY_CACHEDIR", f"{scratch}/apptainer-cache")


def parse_run_profile_from_cli(*, remote: bool) -> RunProfile:
    return "remote" if remote else "local"


def add_run_profile_arguments(parser) -> None:
    """Register mutually exclusive ``--local`` / ``--remote`` on an argparse parser."""
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--local",
        dest="run_profile",
        action="store_const",
        const="local",
        help="Docker sandbox, docker-compose Redis, LLM via SSH port-forward (default).",
    )
    mode.add_argument(
        "--remote",
        dest="run_profile",
        action="store_const",
        const="remote",
        help="Apptainer sandbox, in-job Redis, job-local vLLM (cluster).",
    )
    parser.set_defaults(run_profile="local")


def configure_run_profile_from_args(args) -> RunProfile:
    """Apply profile from parsed CLI args and refresh settings cache."""
    profile = getattr(args, "run_profile", "local") or "local"
    apply_run_profile(profile)
    from src.config import get_settings

    get_settings.cache_clear()
    return profile


def apply_run_profile_from_env() -> RunProfile | None:
    """If REVIEW_RUN_PROFILE is set in the environment, apply its presets."""
    raw = os.environ.get(_PROFILE_ENV, "").strip().lower()
    if raw in {"local", "remote"}:
        apply_run_profile(raw)  # type: ignore[arg-type]
        return raw  # type: ignore[return-value]
    return None

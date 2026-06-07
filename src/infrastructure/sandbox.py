"""Sandbox factory: Docker (local) and Apptainer (remote) backends."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from src.infrastructure.sandbox_apptainer import ApptainerRepoSandbox
from src.infrastructure.sandbox_docker import DockerRepoSandbox
from src.infrastructure.sandbox_runtime import SandboxExecResult, SandboxRuntime

if TYPE_CHECKING:
    from src.config import Settings

# Backward-compatible alias
RepoSandbox = SandboxRuntime


def resolve_sandbox_image(settings: Settings, image_name: str | None = None) -> str:
    """Resolve Docker image name or Apptainer SIF path from settings."""
    if image_name and str(image_name).strip():
        candidate = str(image_name).strip()
        if settings.sandbox_backend == "apptainer":
            path = Path(candidate)
            if path.suffix == ".sif" or path.is_file():
                return str(path.expanduser())
        return candidate

    if settings.sandbox_backend == "apptainer":
        review_sif = (settings.apptainer_image or "").strip()
        if review_sif:
            return review_sif
        return "agent-fs-sandbox.sif"

    return "agent-fs-sandbox"


def resolve_verifier_sandbox_image(
    settings: Settings,
    *,
    needs_clone: bool,
) -> str:
    """Pick verifier artifact for mount-only vs clone workflows."""
    if settings.sandbox_backend == "apptainer":
        if needs_clone:
            clone_sif = (settings.apptainer_verifier_image or settings.apptainer_image or "").strip()
            if not clone_sif:
                clone_sif = (settings.verifier_clone_image or "").strip()
            if clone_sif.endswith(".sif") or Path(clone_sif).is_file():
                return clone_sif
            return clone_sif or "agent-fs-sandbox.sif"
        test_sif = (settings.apptainer_verifier_image or "").strip()
        if test_sif:
            return test_sif
        docker_name = (settings.verifier_image or "").strip()
        if docker_name.endswith(".sif"):
            return docker_name
        return test_sif or "verifier-test-env.sif"

    if needs_clone:
        return (settings.verifier_clone_image or "agent-fs-sandbox").strip()
    return (settings.verifier_image or "verifier-test-env:latest").strip()


def build_repo_sandbox(
    settings: Settings,
    *,
    image_name: str | None = None,
) -> SandboxRuntime:
    """Construct the active sandbox backend from settings."""
    artifact = resolve_sandbox_image(settings, image_name)

    if settings.sandbox_backend == "apptainer":
        return ApptainerRepoSandbox(
            sif_path=artifact,
            apptainer_binary=settings.apptainer_binary,
            instance_dir=settings.apptainer_instance_dir,
            bind_tmpfs=settings.apptainer_bind_tmpfs,
            extra_binds=settings.apptainer_extra_bind,
        )

    return DockerRepoSandbox(image_name=artifact)


def sandbox_runtime_available(settings: Settings) -> bool:
    """Return True when the configured sandbox backend is usable."""
    if settings.sandbox_backend == "apptainer":
        try:
            import shutil
            import subprocess

            if not shutil.which(settings.apptainer_binary):
                return False
            subprocess.run(
                [settings.apptainer_binary, "version"],
                capture_output=True,
                check=True,
                timeout=30,
            )
            review_sif = resolve_sandbox_image(settings)
            return Path(review_sif).expanduser().is_file()
        except Exception:
            return False

    try:
        import docker

        return bool(docker.from_env().ping())
    except Exception:
        return False


__all__ = [
    "ApptainerRepoSandbox",
    "DockerRepoSandbox",
    "RepoSandbox",
    "SandboxExecResult",
    "SandboxRuntime",
    "build_repo_sandbox",
    "resolve_sandbox_image",
    "resolve_verifier_sandbox_image",
    "sandbox_runtime_available",
]

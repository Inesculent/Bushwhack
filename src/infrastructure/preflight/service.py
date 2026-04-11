import hashlib
import json
from typing import List, Optional

from src.domain.interfaces import IPreflightService
from src.domain.schemas import (
    DiffFileManifestEntry,
    DiffManifest,
    DiffManifestAggregateMetrics,
    PreflightParseIssue,
    PreflightRequest,
)
from src.infrastructure.preflight.normalizer import DiffManifestNormalizer
from src.infrastructure.preflight.parser import ParsedFilePatch, UnifiedDiffParser


class PreflightManifestService(IPreflightService):
    """Deterministic preflight manifest generation from diff inputs."""

    def __init__(
        self,
        parser: Optional[UnifiedDiffParser] = None,
        normalizer: Optional[DiffManifestNormalizer] = None,
    ) -> None:
        self._parser = parser or UnifiedDiffParser()
        self._normalizer = normalizer or DiffManifestNormalizer()

    def build_diff_manifest(self, request: PreflightRequest) -> DiffManifest:
        parsed_patches: List[ParsedFilePatch] = []
        warnings: List[str] = []
        errors: List[PreflightParseIssue] = []

        if request.raw_diff:
            try:
                parsed_patches = self._parser.parse(request.raw_diff)
            except Exception as exc:
                errors.append(
                    PreflightParseIssue(
                        code="preflight_parser_error",
                        message=f"Parser failed: {exc.__class__.__name__}: {exc}",
                        severity="error",
                    )
                )

        files = self._normalizer.normalize(parsed_patches, request.files)

        if request.raw_diff and not files:
            warnings.append("Raw diff was provided but no file entries were extracted.")

        for entry in files:
            for issue in entry.parse_errors:
                if issue.severity == "error":
                    errors.append(issue)

        metrics = self._aggregate(files)
        manifest_id = self._manifest_id(request=request, files=files)

        return DiffManifest(
            manifest_id=manifest_id,
            run_metadata=request.run_metadata,
            files=files,
            aggregate_metrics=metrics,
            risk_hints=[],
            ambiguity_flags=[],
            errors=errors,
            warnings=warnings,
        )

    def _aggregate(self, files: List[DiffFileManifestEntry]) -> DiffManifestAggregateMetrics:
        language_breakdown: dict[str, int] = {}
        for entry in files:
            if entry.language:
                language_breakdown[entry.language] = language_breakdown.get(entry.language, 0) + 1

        return DiffManifestAggregateMetrics(
            total_files_changed=len(files),
            total_additions=sum(item.additions for item in files),
            total_deletions=sum(item.deletions for item in files),
            total_hunks=sum(item.hunk_count for item in files),
            language_breakdown=language_breakdown,
        )

    def _manifest_id(self, request: PreflightRequest, files: List[DiffFileManifestEntry]) -> str:
        payload = {
            "manifest_version": "1.0",
            "run_metadata": request.run_metadata.model_dump(mode="json", exclude_none=True),
            "files": [item.model_dump(mode="json", exclude_none=True) for item in files],
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

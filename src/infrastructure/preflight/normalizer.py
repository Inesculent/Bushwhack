from __future__ import annotations

from typing import Dict, List, Optional

from src.domain.schemas import (
    DiffFileManifestEntry,
    PreflightDiffFileInput,
)
from src.infrastructure.preflight.parser import ParsedFilePatch


class DiffManifestNormalizer:
    """Convert parser output and fallback input into deterministic manifest entries."""

    _BINARY_EXTENSIONS = {
        "png",
        "jpg",
        "jpeg",
        "gif",
        "webp",
        "bmp",
        "ico",
        "pdf",
        "zip",
        "gz",
        "tar",
        "jar",
        "exe",
        "dll",
        "so",
        "woff",
        "woff2",
        "ttf",
        "otf",
    }

    _VENDOR_SEGMENTS = {"node_modules", "vendor", "third_party", "external", "deps"}

    _GENERATED_SUFFIXES = {".min.js", ".min.css", ".pb.go", ".generated.ts", ".snap"}

    _GENERATED_FILENAMES = {
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "cargo.lock",
    }

    def normalize(
        self,
        parsed_patches: List[ParsedFilePatch],
        fallback_files: List[PreflightDiffFileInput],
    ) -> List[DiffFileManifestEntry]:
        entries_by_path: Dict[str, DiffFileManifestEntry] = {}

        for patch in parsed_patches:
            normalized_path = self.normalize_path(patch.filepath, strip_git_prefix=True)
            entries_by_path[normalized_path] = DiffFileManifestEntry(
                filepath=normalized_path,
                old_filepath=self.normalize_path(patch.old_filepath, strip_git_prefix=True) if patch.old_filepath else None,
                change_type=patch.change_type,
                additions=patch.additions,
                deletions=patch.deletions,
                hunk_count=patch.hunk_count,
                language=self.language_from_extension(normalized_path),
                is_generated=self.is_generated_file(normalized_path),
                is_binary=patch.is_binary_hint or self.is_binary_file(normalized_path, patch.raw_diff),
                is_vendor=self.is_vendor_file(normalized_path),
                raw_diff=patch.raw_diff,
                parse_errors=patch.parse_issues,
            )

        for fallback in fallback_files:
            normalized_path = self.normalize_path(fallback.filepath)
            existing = entries_by_path.get(normalized_path)

            if existing is None:
                entries_by_path[normalized_path] = DiffFileManifestEntry(
                    filepath=normalized_path,
                    change_type=fallback.change_type or "M",
                    additions=fallback.additions,
                    deletions=fallback.deletions,
                    hunk_count=0,
                    language=self.language_from_extension(normalized_path),
                    is_generated=self.is_generated_file(normalized_path),
                    is_binary=self.is_binary_file(normalized_path, fallback.raw_diff),
                    is_vendor=self.is_vendor_file(normalized_path),
                    raw_diff=fallback.raw_diff,
                )
                continue

            # Merge fallback values only when parser metadata is absent or underspecified.
            if existing.change_type == "M" and fallback.change_type is not None:
                existing.change_type = fallback.change_type
            if existing.additions == 0 and fallback.additions > 0:
                existing.additions = fallback.additions
            if existing.deletions == 0 and fallback.deletions > 0:
                existing.deletions = fallback.deletions
            if existing.raw_diff is None and fallback.raw_diff is not None:
                existing.raw_diff = fallback.raw_diff

        return sorted(entries_by_path.values(), key=lambda entry: entry.filepath)
    

    @staticmethod
    def normalize_path(filepath: str, strip_git_prefix: bool = False) -> str:
        normalized = filepath.strip().replace("\\", "/")
        if strip_git_prefix and (normalized.startswith("a/") or normalized.startswith("b/")):
            normalized = normalized[2:]
        while normalized.startswith("./"):
            normalized = normalized[2:]
        return normalized

    def language_from_extension(self, filepath: str) -> Optional[str]:
        if "." not in filepath:
            return None
        ext = filepath.rsplit(".", 1)[-1].lower()
        return {
            "py": "Python",
            "js": "JavaScript",
            "ts": "TypeScript",
            "tsx": "TypeScript",
            "jsx": "JavaScript",
            "java": "Java",
            "go": "Go",
            "rb": "Ruby",
            "cpp": "C++",
            "c": "C",
            "cs": "C#",
            "rs": "Rust",
            "php": "PHP",
            "scala": "Scala",
            "kt": "Kotlin",
            "swift": "Swift",
            "json": "JSON",
            "yaml": "YAML",
            "yml": "YAML",
            "toml": "TOML",
            "md": "Markdown",
            "sh": "Shell",
            "sql": "SQL",
        }.get(ext)

    def is_binary_file(self, filepath: str, raw_diff: Optional[str]) -> bool:
        if raw_diff and "Binary files" in raw_diff:
            return True
        if "." not in filepath:
            return False
        extension = filepath.rsplit(".", 1)[-1].lower()
        return extension in self._BINARY_EXTENSIONS

    def is_vendor_file(self, filepath: str) -> bool:
        parts = set(filepath.split("/"))
        return any(segment in parts for segment in self._VENDOR_SEGMENTS)

    def is_generated_file(self, filepath: str) -> bool:
        file_lower = filepath.lower()
        if file_lower in self._GENERATED_FILENAMES:
            return True
        if any(file_lower.endswith(suffix) for suffix in self._GENERATED_SUFFIXES):
            return True
        return "/generated/" in file_lower


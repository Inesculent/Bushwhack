import re
from dataclasses import dataclass, field
from typing import List, Optional

from src.domain.schemas import DiffChangeType, PreflightParseIssue


@dataclass(frozen=True)
class ParsedFilePatch:
	"""Internal parsed representation of one changed file from unified diff text."""

	filepath: str
	change_type: DiffChangeType = "M"
	old_filepath: Optional[str] = None
	additions: int = 0
	deletions: int = 0
	hunk_count: int = 0
	is_binary_hint: bool = False
	raw_diff: Optional[str] = None
	parse_issues: List[PreflightParseIssue] = field(default_factory=list)


class UnifiedDiffParser:
	"""Parse unified git diff text into deterministic file-level patches."""

	_DIFF_HEADER_RE = re.compile(r"^diff --git a/(.+) b/(.+)$")

	def parse(self, raw_diff: str) -> List[ParsedFilePatch]:
		if not raw_diff.strip():
			return []

		lines = raw_diff.splitlines()
		sections = self._split_sections(lines)
		patches: List[ParsedFilePatch] = []

		for section in sections:
			patch = self._parse_section(section)
			if patch is not None:
				patches.append(patch)

		return patches

	def _split_sections(self, lines: List[str]) -> List[List[str]]:
		sections: List[List[str]] = []
		current: List[str] = []

		for line in lines:
			if line.startswith("diff --git ") and current:
				sections.append(current)
				current = [line]
			else:
				current.append(line)

		if current:
			sections.append(current)

		return sections

	def _parse_section(self, section: List[str]) -> Optional[ParsedFilePatch]:
		if not section:
			return None

		header = section[0]
		header_old_path: Optional[str] = None
		header_new_path: Optional[str] = None

		match = self._DIFF_HEADER_RE.match(header)
		if match:
			header_old_path = self._clean_path(match.group(1))
			header_new_path = self._clean_path(match.group(2))

		old_path: Optional[str] = None
		new_path: Optional[str] = None
		rename_from: Optional[str] = None
		rename_to: Optional[str] = None
		additions = 0
		deletions = 0
		hunk_count = 0
		is_binary = False
		issues: List[PreflightParseIssue] = []
		change_type_hint: Optional[DiffChangeType] = None

		for line in section[1:]:
			if line.startswith("new file mode"):
				change_type_hint = "A"
				continue
			if line.startswith("deleted file mode"):
				change_type_hint = "D"
				continue
			if line.startswith("rename from "):
				rename_from = self._clean_path(line.replace("rename from ", "", 1).strip())
				change_type_hint = "R"
				continue
			if line.startswith("rename to "):
				rename_to = self._clean_path(line.replace("rename to ", "", 1).strip())
				change_type_hint = "R"
				continue
			if line.startswith("Binary files "):
				is_binary = True
				continue
			if line.startswith("--- "):
				candidate = self._clean_diff_path(line[4:].strip())
				if candidate is not None:
					old_path = candidate
				continue
			if line.startswith("+++ "):
				candidate = self._clean_diff_path(line[4:].strip())
				if candidate is not None:
					new_path = candidate
				continue
			if line.startswith("@@"):
				hunk_count += 1
				continue
			if line.startswith("+") and not line.startswith("+++"):
				additions += 1
				continue
			if line.startswith("-") and not line.startswith("---"):
				deletions += 1

		change_type = self._resolve_change_type(change_type_hint, old_path, new_path)
		resolved_filepath = self._resolve_filepath(
			change_type=change_type,
			header_new_path=header_new_path,
			header_old_path=header_old_path,
			new_path=rename_to or new_path,
			old_path=rename_from or old_path,
		)

		if not resolved_filepath:
			issues.append(
				PreflightParseIssue(
					code="preflight_unresolved_filepath",
					message="Could not resolve file path from diff section.",
					severity="error",
				)
			)
			return None

		old_filepath = rename_from or old_path or header_old_path
		return ParsedFilePatch(
			filepath=resolved_filepath,
			change_type=change_type,
			old_filepath=old_filepath,
			additions=additions,
			deletions=deletions,
			hunk_count=hunk_count,
			is_binary_hint=is_binary,
			raw_diff="\n".join(section),
			parse_issues=issues,
		)

	@staticmethod
	def _clean_diff_path(path: str) -> Optional[str]:
		if path == "/dev/null":
			return None
		return UnifiedDiffParser._clean_path(path)

	@staticmethod
	def _clean_path(path: str) -> str:
		normalized = path.strip().replace("\\", "/")
		if normalized.startswith("a/") or normalized.startswith("b/"):
			normalized = normalized[2:]
		while normalized.startswith("./"):
			normalized = normalized[2:]
		return normalized

	@staticmethod
	def _resolve_change_type(
		change_type_hint: Optional[DiffChangeType],
		old_path: Optional[str],
		new_path: Optional[str],
	) -> DiffChangeType:
		if change_type_hint is not None:
			return change_type_hint
		if old_path is None and new_path is not None:
			return "A"
		if new_path is None and old_path is not None:
			return "D"
		return "M"

	@staticmethod
	def _resolve_filepath(
		change_type: DiffChangeType,
		header_new_path: Optional[str],
		header_old_path: Optional[str],
		new_path: Optional[str],
		old_path: Optional[str],
	) -> Optional[str]:
		if change_type == "D":
			return old_path or header_old_path or header_new_path
		return new_path or header_new_path or old_path or header_old_path


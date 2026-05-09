import json
import re
from typing import List, Sequence

from src.domain.interfaces import ICodeSearcher
from src.domain.schemas import SearchResult
from src.infrastructure.sandbox import RepoSandbox


MAX_MATCH_LINE_CHARS = 1000
TRUNCATION_MARKER = "... [truncated]"


def _truncate_match_line(text: str) -> str:
    stripped = text.strip()
    if len(stripped) <= MAX_MATCH_LINE_CHARS:
        return stripped
    return f"{stripped[:MAX_MATCH_LINE_CHARS]}{TRUNCATION_MARKER}"


class RipgrepSearcher(ICodeSearcher):
    def __init__(self, sandbox: RepoSandbox):
        self.sandbox = sandbox

    def search_text(
        self,
        query: str,
        repository_path: str = "/repo",
        file_paths: Sequence[str] | None = None,
    ) -> List[SearchResult]:
        """
        Executes a ripgrep search inside the sandbox and parses the JSON stream.
        """
        # We use --json to get structured output and --heading to group by file
        # repository_path defaults to /repo as defined in our Docker mount
        search_paths = self._search_paths(repository_path=repository_path, file_paths=file_paths)
        cmd = [
            "rg",
            "--json",
            "-C",
            "2",
            "--max-columns",
            str(MAX_MATCH_LINE_CHARS),
            "--",
            query,
            *search_paths,
        ]
        raw_output = self.sandbox.execute(cmd)

        return self._parse_output(raw_output)

    def find_symbol(self, symbol_name: str, repository_path: str = "/repo") -> List[SearchResult]:
        """
        Searches for exact word boundaries to find symbol definitions/usages.
        """
        pattern = f"\\b{symbol_name}\\b"
        return self.search_text(pattern, repository_path)

    def find_definition_candidates(self, symbol_name: str, repository_path: str = "/repo") -> List[SearchResult]:
        """Ripgrep-based heuristic scan for likely definition lines (``def`` / ``class`` / ``function``)."""
        escaped = re.escape(symbol_name)
        pattern = (
            f"(?:^|\\s)(?:async\\s+)?def\\s+{escaped}\\s*\\(|"
            f"(?:^|\\s)class\\s+{escaped}\\s*[\\(:]|"
            f"(?:^|\\s)(?:export\\s+)?(?:async\\s+)?function\\s+{escaped}\\s*\\("
        )
        return self.search_text(pattern, repository_path)

    @staticmethod
    def _search_paths(repository_path: str, file_paths: Sequence[str] | None) -> List[str]:
        if not file_paths:
            return [repository_path]

        paths: List[str] = []
        base = repository_path.rstrip("/")
        for raw_path in file_paths:
            normalized = raw_path.replace("\\", "/").lstrip("/")
            if not normalized or ".." in normalized.split("/"):
                continue
            if ":" in normalized.split("/", maxsplit=1)[0]:
                continue
            paths.append(f"{base}/{normalized}")
        return paths or [repository_path]

    def _parse_output(self, raw_output: str) -> List[SearchResult]:
        results = []
        # Ripgrep --json outputs multiple JSON objects, one per line
        for line in raw_output.strip().split('\n'):
            if not line:
                continue

            try:
                data = json.loads(line)
                # We only care about actual matches, not header or summary data
                if data.get("type") == "match":
                    payload = data["data"]
                    content = _truncate_match_line(payload["lines"]["text"])
                    results.append(SearchResult(
                        file_path=payload["path"]["text"].replace("/repo/", ""),
                        line_number=payload["line_number"],
                        content=content,
                        context_lines=[
                            # Ripgrep doesn't provide easy context in the JSON blob
                            # without extra parsing, but for now we store the match lines
                            content
                        ]
                    ))
            except (json.JSONDecodeError, KeyError):
                continue

        return results
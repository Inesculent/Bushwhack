import json
from typing import List
from src.domain.interfaces import ICodeSearcher
from src.domain.schemas import SearchResult
from src.infrastructure.sandbox import RepoSandbox

class RipgrepSearcher(ICodeSearcher):
    def __init__(self, sandbox: RepoSandbox):
        self.sandbox = sandbox

    def search_text(self, query: str, repository_path: str = "/repo") -> List[SearchResult]:
        """
        Executes a ripgrep search inside the sandbox and parses the JSON stream.
        """
        # We use --json to get structured output and --heading to group by file
        # repository_path defaults to /repo as defined in our Docker mount
        cmd = ["rg", "--json", "-C", "2", query, repository_path]
        raw_output = self.sandbox.execute(cmd)
        
        return self._parse_output(raw_output)

    def find_symbol(self, symbol_name: str, repository_path: str = "/repo") -> List[SearchResult]:
        """
        Searches for exact word boundaries to find symbol definitions/usages.
        """
        pattern = f"\\b{symbol_name}\\b"
        return self.search_text(pattern, repository_path)

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
                    results.append(SearchResult(
                        file_path=payload["path"]["text"].replace("/repo/", ""),
                        line_number=payload["line_number"],
                        content=payload["lines"]["text"].strip(),
                        context_lines=[
                            # Ripgrep doesn't provide easy context in the JSON blob
                            # without extra parsing, but for now we store the match lines
                            payload["lines"]["text"].strip()
                        ]
                    ))
            except (json.JSONDecodeError, KeyError):
                continue
                
        return results
from __future__ import annotations

import json

from src.infrastructure.search.ripgrep import MAX_MATCH_LINE_CHARS, TRUNCATION_MARKER, RipgrepSearcher


def test_ripgrep_searcher_truncates_long_match_lines() -> None:
    long_line = "x" * (MAX_MATCH_LINE_CHARS + 50)
    raw_output = json.dumps(
        {
            "type": "match",
            "data": {
                "path": {"text": "/repo/comfy/text_encoders/qwen25_tokenizer/vocab.json"},
                "line_number": 1,
                "lines": {"text": long_line},
            },
        }
    )

    searcher = RipgrepSearcher(sandbox=None)  # type: ignore[arg-type]
    results = searcher._parse_output(raw_output)  # noqa: SLF001

    assert len(results) == 1
    assert results[0].content.endswith(TRUNCATION_MARKER)
    assert len(results[0].content) == MAX_MATCH_LINE_CHARS + len(TRUNCATION_MARKER)
    assert results[0].context_lines == [results[0].content]


def test_ripgrep_searcher_scopes_to_safe_repo_relative_paths() -> None:
    paths = RipgrepSearcher._search_paths(  # noqa: SLF001
        repository_path="/repo",
        file_paths=["middleware/cache_middleware.py", "../outside.py", "/absolute.py"],
    )

    assert paths == ["/repo/middleware/cache_middleware.py", "/repo/absolute.py"]

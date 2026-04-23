"""Prompt template for the solo-agent (non-agentic, no-code-context) ACR worker.

The template is a one-shot review prompt that emits free-form tagged output
(`<diff>/<side>/<note>/<notesplit />/<end />`). Do NOT bolt `with_structured_output`
onto the LLM call that consumes this template, because the response is intentionally
not JSON-shaped.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Final

PROMPT_VERSION: Final[str] = "v1"

PROMPT_TEMPLATE: Final[str] = """You are an expert code reviewer. Your task is to review the following code changes and provide constructive feedback.

## Pull Request Information
**Title:** {pr_title}
**Description:** {pr_description}

## Code Changes to Review
```diff
{diff_hunk}
```

## Instructions
Please review the code changes above and provide detailed feedback. For each issue you identify:
1. Use `<diff>...</diff>` tags to wrap **only the minimal code snippet** that contains the specific issue you're commenting on. Do NOT include the entire diff_hunk - extract only the relevant lines that demonstrate the problem.
2. Use `<side>left</side>` or `<side>right</side>` to indicate whether your comment applies to the old code (left) or new code (right)
3. Use `<note>...</note>` tags to wrap your review comment
4. Use `<notesplit />` to separate different review comments
5. Use `<end />` to end your response

**Important**: The `<diff>` section should contain the smallest possible code fragment that clearly shows the issue. If multiple separate issues exist in different parts of the diff_hunk, create separate review comments for each with their own minimal `<diff>` snippets.

Focus on:
- Code defects
- Performance issues
- Security vulnerability
- Maintainability and readability

## Example Format
<diff>
+ def example_function():
+     pass
</diff>
<side>right</side>
<note>
This function lacks documentation. Please add a docstring explaining its purpose, parameters, and return value.
</note>
<notesplit />
<end />

## Output Requirements
- If you find issues with the code changes, provide your review comments following the format above
- If you believe the code changes are acceptable and have no issues to report, simply output `<end />`
- Always end your response with `<end />` after all review comments (or immediately if no issues found)

Now provide your review:
"""


@dataclass(frozen=True, slots=True)
class RenderedPrompt:
    text: str
    diff_truncated: bool
    diff_chars_dropped: int


def render(
    pr_title: str,
    pr_description: str,
    diff_hunk: str,
    max_diff_chars: int,
) -> RenderedPrompt:
    """Render the solo-agent prompt, truncating the diff to ``max_diff_chars``.

    Truncation is done with an explicit, visible marker so downstream analyses can
    distinguish truncated prompts from full-diff prompts.
    """
    if max_diff_chars <= 0:
        raise ValueError("max_diff_chars must be positive")

    safe_title = (pr_title or "").strip() or "(no title provided)"
    safe_description = (pr_description or "").strip() or "(no description provided)"
    raw_diff = diff_hunk or ""

    truncated = False
    dropped = 0
    if len(raw_diff) > max_diff_chars:
        dropped = len(raw_diff) - max_diff_chars
        raw_diff = raw_diff[:max_diff_chars] + f"\n[...truncated {dropped} chars...]"
        truncated = True

    text = PROMPT_TEMPLATE.format(
        pr_title=safe_title,
        pr_description=safe_description,
        diff_hunk=raw_diff,
    )
    return RenderedPrompt(text=text, diff_truncated=truncated, diff_chars_dropped=dropped)

"""LLM-backed generation of standalone verifier scripts."""

from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict

from src.config import Settings, get_settings
from src.domain.verifier_schemas import VerifierTestGeneratorOutput
from src.infrastructure.llm.factory import Models
from src.infrastructure.llm.token_usage import extract_total_tokens_from_llm_result, parse_structured_output
from src.orchestration.prompts.renderer import load_reviewer_prompt

logger = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"^```(?:python)?\s*(.*?)```\s*$", re.DOTALL | re.IGNORECASE)


def _strip_wrapping_fence(text: str) -> str:
    text = text.strip()
    m = _CODE_FENCE.match(text)
    if m:
        return m.group(1).strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        return "\n".join(lines).strip()
    return text


def build_test_generator_prompt(
    *,
    candidate: Dict[str, Any],
    focused_context_snippets: str,
    git_diff_excerpt: str,
    retry_feedback: str,
    mock_heavy_deps: bool,
    timeout_seconds: int,
    repo_root: str,
) -> str:
    template = load_reviewer_prompt("verifier/test_generator.md")
    candidate_json = json.dumps(candidate, indent=2, ensure_ascii=False)
    return template.format(
        candidate_json=candidate_json,
        focused_context_snippets=focused_context_snippets or "(none)",
        git_diff_excerpt=git_diff_excerpt or "(none)",
        retry_feedback=retry_feedback or "(none)",
        mock_heavy_deps_label="enabled" if mock_heavy_deps else "disabled",
        file_path=candidate.get("file_path", ""),
        line_start=candidate.get("line_start", 0),
        line_end=candidate.get("line_end", 0),
        timeout_seconds=timeout_seconds,
        repo_root=repo_root or "/repo",
    )


def generate_test_script(
    *,
    candidate: Dict[str, Any],
    focused_context_snippets: str,
    git_diff_excerpt: str,
    retry_feedback: str,
    repo_root: str,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
) -> tuple[str, int]:
    """Return (test_code, llm_tokens). Empty string on failure."""
    settings = settings or get_settings()
    prompt = build_test_generator_prompt(
        candidate=candidate,
        focused_context_snippets=focused_context_snippets,
        git_diff_excerpt=git_diff_excerpt,
        retry_feedback=retry_feedback,
        mock_heavy_deps=settings.verifier_mock_heavy_deps,
        timeout_seconds=settings.verifier_test_timeout_seconds,
        repo_root=repo_root,
    )
    if not use_llm:
        return "", 0

    selected = model_key or getattr(settings, "reviewer_worker_model_key", None)
    try:
        llm = Models.worker(VerifierTestGeneratorOutput, model_key=selected)
        invoke_result = llm.invoke(prompt)
        parsed = parse_structured_output(invoke_result, VerifierTestGeneratorOutput)
        tokens = extract_total_tokens_from_llm_result(invoke_result)
        code = _strip_wrapping_fence(parsed.test_code)
        return code, tokens
    except Exception as exc:  # noqa: BLE001
        logger.warning("verifier test generation failed: %s: %s", exc.__class__.__name__, exc)
        return "", 0

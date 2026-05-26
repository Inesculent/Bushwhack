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
from src.domain.state import GraphState
from src.orchestration.context.context_packets import build_verifier_generator_packet
from src.orchestration.nodes.verifier.harness_stubs import HEAVY_DEP_STUB_PRELUDE
from src.orchestration.prompts.renderer import load_reviewer_prompt

logger = logging.getLogger(__name__)

_CODE_FENCE = re.compile(r"^```(?:python)?\s*(.*?)```\s*$", re.DOTALL | re.IGNORECASE)


def _is_length_finish_error(exc: Exception) -> bool:
    return "LengthFinish" in exc.__class__.__name__


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
    focused_context_snippets: str = "",
    git_diff_excerpt: str = "",
    retry_feedback: str = "",
    mock_heavy_deps: bool = False,
    timeout_seconds: int = 30,
    repo_root: str = "/repo",
    compact: bool = False,
    state: GraphState | None = None,
    settings: Settings | None = None,
) -> str:
    template = load_reviewer_prompt("verifier/test_generator.md")
    if state is not None:
        packet = build_verifier_generator_packet(state, candidate, settings=settings)
        by_key = {s.key: s.content for s in packet.sections}
        candidate_json = by_key.get("verifier_candidate_json", "{}")
        focused_context_snippets = by_key.get("verifier_focused_snippets", "(none)")
        git_diff_excerpt = by_key.get("verifier_diff_excerpt", "(none)")
    else:
        keep = (
            "candidate_id",
            "file_path",
            "line_start",
            "line_end",
            "claim_type",
            "content",
            "failure_mode",
            "evidence_summary",
            "required_context",
            "confidence",
            "severity",
        )
        slim = {k: candidate[k] for k in keep if k in candidate}
        candidate_json = json.dumps(
            slim if compact else candidate,
            indent=None if compact else 2,
            ensure_ascii=False,
        )
        if compact and len(focused_context_snippets) > 8000:
            focused_context_snippets = focused_context_snippets[:8000] + "\n... [truncated]"
    raw_prelude = (
        HEAVY_DEP_STUB_PRELUDE.strip()
        if mock_heavy_deps
        else "# (mock_heavy_deps disabled — only add heavy-dep mocks if import fails)"
    )
    rendered = template.format(
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
    return rendered.replace("__HEAVY_DEP_PRELUDE__", raw_prelude)


def generate_test_script(
    *,
    candidate: Dict[str, Any],
    focused_context_snippets: str = "",
    git_diff_excerpt: str = "",
    retry_feedback: str = "",
    repo_root: str = "/repo",
    state: GraphState | None = None,
    settings: Settings | None = None,
    model_key: str | None = None,
    use_llm: bool = True,
) -> tuple[str, int]:
    """Return (test_code, llm_tokens). Empty string on failure."""
    settings = settings or get_settings()
    max_completion = int(settings.verifier_test_generator_max_completion_tokens)
    selected = model_key or getattr(settings, "reviewer_worker_model_key", None)

    def _run(*, compact: bool) -> tuple[str, int]:
        prompt = build_test_generator_prompt(
            candidate=candidate,
            focused_context_snippets=focused_context_snippets,
            git_diff_excerpt=git_diff_excerpt,
            retry_feedback=retry_feedback,
            mock_heavy_deps=settings.verifier_mock_heavy_deps,
            timeout_seconds=settings.verifier_test_timeout_seconds,
            repo_root=repo_root,
            compact=compact,
            state=state,
            settings=settings,
        )
        llm = Models.worker(
            VerifierTestGeneratorOutput,
            model_key=selected,
            max_completion_tokens=max_completion,
        )
        invoke_result = llm.invoke(prompt)
        parsed = parse_structured_output(invoke_result, VerifierTestGeneratorOutput)
        tokens = extract_total_tokens_from_llm_result(invoke_result)
        code = _strip_wrapping_fence(parsed.test_code)
        return code, tokens

    if not use_llm:
        return "", 0

    try:
        return _run(compact=False)
    except Exception as exc:  # noqa: BLE001
        if not _is_length_finish_error(exc):
            logger.warning("verifier test generation failed: %s: %s", exc.__class__.__name__, exc)
            return "", 0
        try:
            logger.warning("verifier test generation retrying with compact prompt: %s", exc.__class__.__name__)
            return _run(compact=True)
        except Exception as retry_exc:  # noqa: BLE001
            logger.warning(
                "verifier test generation failed: %s: %s",
                retry_exc.__class__.__name__,
                retry_exc,
            )
            return "", 0

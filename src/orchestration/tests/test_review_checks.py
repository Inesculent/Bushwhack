from __future__ import annotations

import json

from typing import Any

from src.config import Settings
from src.domain.schemas import (
    BehavioralSpec,
    CandidateFinding,
    FocusedContextRequest,
    ReviewCheck,
    ReviewCheckCompilerOutput,
    ReviewCheckExecutorOutput,
    ReviewCheckResult,
    ReviewSurface,
    ReviewTask,
    SurfaceInvariant,
)
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.nodes.application import critique_pipeline
from src.orchestration.nodes.application.review_checks import (
    make_review_check_compiler_node,
    make_review_check_context_planner_node,
    make_review_check_evidence_gate_node,
    make_review_check_executor_node,
    make_review_check_validator_node,
    validate_review_check,
)
from src.orchestration.context.surface_ledger import build_migration_invariants_from_diff, surface_ids_for_task
from src.reviewer_agent.harness import aacr
from src.reviewer_agent.harness.aacr import (
    _coverage_audit_for_pr,
    _github_mcp_preflight,
    _load_positive_samples_by_pr,
    _positive_labels_for_pr,
    _review_check_metrics,
    _write_coverage_audit,
    _write_raw,
)


class _Raw:
    usage_metadata = {
        "input_tokens": 3,
        "output_tokens": 4,
        "total_tokens": 7,
    }
    response_metadata = None
    content = "raw"


class _FakeLLM:
    def __init__(self, result: Any) -> None:
        self.result = result
        self.prompts: list[str] = []

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        return self.result


def _task() -> ReviewTask:
    return ReviewTask(
        id="review-logic",
        title="Diff-local correctness",
        description="Check changed return contracts.",
        target_files=["src/app.py"],
        specialty="logic",
    )


def _check(**overrides: object) -> ReviewCheck:
    data = {
        "check_id": "review-logic:check:1",
        "patch_task_id": "review-logic",
        "lens": "api_compatibility",
        "file_path": "src/app.py",
        "line_start": 1,
        "line_end": 3,
        "changed_code_anchor": "handle",
        "behavioral_question": "Does handle still return the declared result on every changed path?",
        "affected_invariant": "return contract completeness",
        "required_evidence": ["changed handle implementation", "declared return contract"],
        "suppress_criteria": ["All changed paths return the declared result."],
        "report_criteria": ["A reachable changed path returns None or the wrong result."],
        "allowed_retrieval": ["task_evidence", "focused_context"],
        "budget": 2,
    }
    data.update(overrides)
    return ReviewCheck(**data)


def _state(**overrides: object) -> dict[str, Any]:
    task = _task()
    base: dict[str, Any] = {
        "run_id": "r1",
        "repo_path": "/repo",
        "git_diff": "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n+def handle():\n+    return None\n",
        "current_task_id": task.id,
        "task_registry": {task.id: task},
        "metadata": {
            "review_trace_enabled": True,
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def handle():\n    return None\n",
                        "mental_model_excerpt": "- handlers return strings",
                        "review_kb_excerpt": "handle is a public handler",
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def handle():\n    return None\n",
                            }
                        },
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "handle",
                                "dimension": "contract completeness",
                                "evidence": "entry point implies a return contract",
                            }
                        ],
                    }
                }
            },
        },
        "review_checks": [],
        "invalid_review_checks": [],
        "review_check_results": [],
        "candidate_findings": [],
        "focused_context_requests": [],
        "focused_context_results": {},
    }
    base.update(overrides)
    return base


def test_review_check_validator_rejects_vague_checks() -> None:
    vague = _check(behavioral_question="Look for security bugs")
    assert "vague_behavioral_question" in validate_review_check(vague)

    valid = _check()
    assert validate_review_check(valid) == []


def test_review_check_executor_schema_generates_with_candidate_forward_ref() -> None:
    schema = ReviewCheckExecutorOutput.model_json_schema()
    assert "ReviewCheckResult" in schema.get("$defs", {})


def test_review_check_compiler_records_checks_and_trace(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_compiler_node()(_state())

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert task_meta["compiled_count"] == 1
    assert out["token_usage"] == 7
    assert any(record["node"] == "review_check_compiler" for record in out["llm_trace"])


def test_review_check_compiler_adds_coverage_floor_for_unchecked_changed_file(monkeypatch) -> None:
    task = _task().model_copy(update={"target_files": ["src/app.py", "src/other.py"]})
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n+def handle():\n+    return None\n"
            "diff --git a/src/other.py b/src/other.py\n+++ b/src/other.py\n@@\n+def other():\n+    return None\n"
        ),
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert task_meta["compiled_count"] == 2
    assert task_meta["compiler_coverage_floor"]["missed_files"] == ["src/other.py"]


def test_review_check_compiler_adds_coverage_floor_for_uncovered_obligation(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def handle():\n+    return None\n"
            "+def parse_index(value):\n+    return value[0]\n"
        ),
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": (
                            "def handle():\n    return None\n"
                            "def parse_index(value):\n    return value[0]\n"
                        ),
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "handle",
                                "dimension": "contract completeness",
                                "evidence": "entry point implies a return contract",
                            },
                            {
                                "file_path": "src/app.py",
                                "surface": "parse_index",
                                "dimension": "boundary/index handling",
                                "evidence": "new index access needs bounds behavior",
                            },
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    added = task_meta["compiler_coverage_floor"]["added_checks"]
    assert len(added) == 1
    assert added[0]["changed_code_anchor"] == "parse_index"
    assert validate_review_check(ReviewCheck(**added[0])) == []
    origins = task_meta["compiled_check_origins"]
    assert origins["review-logic:check:1"]["origin_kind"] == "llm_compiled"
    added_origin = task_meta["compiler_coverage_floor"]["added_check_origins"][added[0]["check_id"]]
    assert added_origin["origin_kind"] == "coverage_obligation"
    assert added_origin == origins[added[0]["check_id"]]


def test_review_check_coverage_floor_uses_surface_anchor() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=7,
        line_end=7,
        confidence=0.95,
    )
    state = _state(
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        }
    )

    out = make_review_check_compiler_node(use_llm=False)(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert compiled[0]["surface_ids"] == ["surface:handle"]
    assert compiled[0]["line_start"] == 7
    assert compiled[0]["changed_code_anchor"] == "handle"


def test_review_check_compiler_narrows_generic_execute_surface_to_named_anchor(monkeypatch) -> None:
    regex = ReviewSurface(
        surface_id="surface:regex",
        name="RegexExtract",
        kind="class",
        file_path="src/app.py",
        line_start=20,
        line_end=40,
        confidence=0.95,
    )
    execute = ReviewSurface(
        surface_id="surface:execute",
        name="execute",
        kind="function",
        file_path="src/app.py",
        line_start=25,
        line_end=35,
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [regex.surface_id, execute.surface_id]})
    output = ReviewCheckCompilerOutput(
        summary="compiled",
        checks=[
            _check(
                check_id="review-logic:check:regex",
                changed_code_anchor="RegexExtract",
                behavioral_question="Does RegexExtract validate group_index before accessing match groups?",
                surface_ids=[],
                line_start=20,
                line_end=40,
            )
        ],
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [regex.model_dump(mode="json"), execute.model_dump(mode="json")]},
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    check = next(item for item in compiled if item["check_id"] == "review-logic:check:regex")
    assert check["surface_ids"] == ["surface:regex"]


def test_review_check_compiler_adds_behavior_check_for_uncovered_symbol(monkeypatch) -> None:
    handle = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=7,
        line_end=8,
        confidence=0.95,
    )
    parse = ReviewSurface(
        surface_id="surface:parse",
        name="parse_payload",
        kind="function",
        file_path="src/app.py",
        line_start=20,
        line_end=25,
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [handle.surface_id, parse.surface_id]})
    output = ReviewCheckCompilerOutput(
        summary="compiled",
        checks=[_check(surface_ids=[handle.surface_id], line_start=7, line_end=8, changed_code_anchor="handle")],
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [handle.model_dump(mode="json"), parse.model_dump(mode="json")]},
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    floor = task_meta["compiler_coverage_floor"]
    assert floor["missing_primary_surface_ids"] == []
    added_parse = [check for check in floor["added_checks"] if check["surface_ids"] == ["surface:parse"]]
    assert added_parse
    assert added_parse[0]["check_id"].startswith("review-logic:uncovered-behavior:")


def test_surface_invariant_checks_are_compiled_before_fallback(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=7,
        line_end=7,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="change handle",
        surfaces=[surface],
        surface_invariants=[
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="contract completeness",
                expected_behavior="handle returns the declared result.",
                required_evidence=["changed handle implementation"],
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    state = _state(
        behavioral_spec_ref=ref,
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def handle():\n    return None\n",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node(use_llm=False, settings=settings)(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert compiled[0]["check_id"] == "review-logic:surface:1"
    assert compiled[0]["surface_ids"] == ["surface:handle"]
    assert compiled[0]["line_start"] == 7


def test_surface_ids_for_task_matches_spaced_camel_case_structured_extraction() -> None:
    regex = ReviewSurface(
        surface_id="surface:regex",
        name="RegexExtract",
        kind="class",
        file_path="comfy_extras/nodes_string.py",
        line_start=252,
        line_end=330,
        confidence=0.95,
    )
    concat = ReviewSurface(
        surface_id="surface:concat",
        name="StringConcatenate",
        kind="class",
        file_path="comfy_extras/nodes_string.py",
        line_start=20,
        line_end=40,
        confidence=0.95,
    )
    compare = ReviewSurface(
        surface_id="surface:compare",
        name="StringCompare",
        kind="class",
        file_path="comfy_extras/nodes_string.py",
        line_start=210,
        line_end=250,
        confidence=0.95,
    )
    task = ReviewTask(
        id="review-logic",
        title="Regex Extract structured extraction",
        description="Check all matches and group extraction behavior.",
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )

    assert surface_ids_for_task(task, [concat, compare, regex]) == ["surface:regex"]

    compare_task = task.model_copy(
        update={
            "title": "String Compare return contract",
            "description": "Check missing return behavior for unexpected mode values.",
        }
    )
    assert surface_ids_for_task(compare_task, [concat, compare, regex]) == ["surface:compare"]


def test_review_check_compiler_keeps_focused_llm_check_ahead_of_surface_cap(monkeypatch, tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surfaces = [
        ReviewSurface(
            surface_id=f"surface:s{i}",
            name=f"Surface{i}",
            kind="class",
            file_path="comfy_extras/nodes_string.py",
                line_start=(i + 1) * 10,
                line_end=(i + 1) * 10 + 3,
            confidence=0.95,
        )
        for i in range(15)
    ]
    regex = ReviewSurface(
        surface_id="surface:regex",
        name="RegexExtract",
        kind="class",
        file_path="comfy_extras/nodes_string.py",
        line_start=252,
        line_end=330,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="many surfaces",
        surfaces=[regex, *surfaces],
        surface_invariants=[
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="changed-surface behavior",
                expected_behavior=f"{surface.name} preserves behavior.",
                required_evidence=[f"changed {surface.name} implementation"],
            )
            for surface in [regex, *surfaces]
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    focused = _check(
        check_id="review-logic:regexextract-tuple-indexing",
        file_path="comfy_extras/nodes_string.py",
        line_start=252,
        line_end=330,
        changed_code_anchor="RegexExtract",
        surface_ids=[regex.surface_id],
        affected_invariant="RegexExtract preserves group tuple extraction.",
    )
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[focused])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    task = ReviewTask(
        id="review-logic",
        title="Regex Extract structured extraction",
        description="Check RegexExtract all matches and group extraction.",
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
        surface_ids=[surface.surface_id for surface in [regex, *surfaces]],
    )
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={task.id: task},
        git_diff="diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n+++ b/comfy_extras/nodes_string.py\n@@\n+class RegexExtract:\n+    pass\n",
        metadata={
            **_state()["metadata"],
            "mental_model": {
                "surface_ledger": [surface.model_dump(mode="json") for surface in [regex, *surfaces]],
            },
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "class RegexExtract:\n    pass\n",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node(settings=settings)(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled_ids = [check["check_id"] for check in task_meta["compiled_checks"]]
    assert len(compiled_ids) == 12
    assert compiled_ids[0] == "review-logic:regexextract-tuple-indexing"
    assert task_meta["compiler_coverage_floor"]["trimmed_existing_check_ids"]


def test_review_check_compiler_adds_tiny_uncovered_surface_behavior_floor(monkeypatch) -> None:
    surfaces = [
        ReviewSurface(
            surface_id=f"surface:s{i}",
            name=f"Surface{i}",
            kind="class",
            file_path="src/app.py",
            line_start=i * 10 + 1,
            line_end=i * 10 + 5,
            confidence=0.95,
        )
        for i in range(4)
    ]
    concrete = _check(
        check_id="review-logic:surface0-return",
        surface_ids=[surfaces[0].surface_id],
        changed_code_anchor="Surface0",
        behavioral_question="Does Surface0 return the declared result on every branch?",
        affected_invariant="declared return shape",
        required_evidence=["changed implementation for Surface0"],
    )
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[concrete])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    task = _task().model_copy(update={"surface_ids": [surface.surface_id for surface in surfaces]})
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {
                "surface_ledger": [surface.model_dump(mode="json") for surface in surfaces],
            },
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "class Surface0: pass\nclass Surface1: pass\nclass Surface2: pass\nclass Surface3: pass\n",
                        "task_evidence": {
                            "file_contents": {"src/app.py": "class Surface0: pass\n"},
                            "files_complete": {"src/app.py": True},
                        },
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled = task_meta["compiled_checks"]
    behavior_checks = [
        check for check in compiled if ":uncovered-behavior:" in check["check_id"]
    ]
    assert len(behavior_checks) == 2
    assert {check["changed_code_anchor"] for check in behavior_checks} == {"Surface1", "Surface2"}
    assert not any(check["changed_code_anchor"] == "Surface0" for check in behavior_checks)


def test_migration_invariants_capture_vllm_style_caller_reliance() -> None:
    surface = ReviewSurface(
        surface_id="surface:allocate",
        name="allocate_slots",
        kind="function",
        file_path="vllm/v1/core/kv_cache_manager.py",
        line_start=116,
        line_end=180,
        confidence=0.95,
    )
    diff = (
        "diff --git a/vllm/v1/core/scheduler.py b/vllm/v1/core/scheduler.py\n"
        "+++ b/vllm/v1/core/scheduler.py\n"
        "@@ -140,7 +140,7 @@\n"
        "-    manager.append_slots(request, new_computed_blocks)\n"
        "+    manager.allocate_slots(request, [])\n"
        "diff --git a/vllm/v1/core/kv_cache_manager.py b/vllm/v1/core/kv_cache_manager.py\n"
        "+++ b/vllm/v1/core/kv_cache_manager.py\n"
        "@@ -220,7 +220,6 @@\n"
        "-def append_slots(request, computed_blocks):\n"
        "-    return allocate_slots(request, computed_blocks)\n"
    )

    invariants = build_migration_invariants_from_diff(
        [surface],
        diff,
        intent_summary="Merge append_slots into allocate_slots and migrate scheduler call sites.",
    )

    assert invariants
    evidence = " ".join(invariants[0].required_evidence)
    assert "old-path" in evidence
    assert "required arguments/state inputs" in evidence
    assert "caller reliance" in evidence


def test_review_check_compiler_adds_migration_floor_check() -> None:
    surface = ReviewSurface(
        surface_id="surface:allocate",
        name="allocate_slots",
        kind="function",
        file_path="src/cache.py",
        line_start=10,
        line_end=30,
        confidence=0.95,
    )
    task = _task().model_copy(
        update={
            "description": "Check migrated call sites from append_slots to allocate_slots.",
            "surface_ids": [surface.surface_id],
        }
    )
    state = _state(
        git_diff=(
            "diff --git a/src/cache.py b/src/cache.py\n"
            "+++ b/src/cache.py\n"
            "@@ -20,7 +20,7 @@\n"
            "-    append_slots(request, computed_blocks)\n"
            "+    allocate_slots(request, [])\n"
        ),
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )

    out = make_review_check_compiler_node(use_llm=False)(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert any(check["affected_invariant"] == "migration caller-reliance contract" for check in compiled)
    migration_check = next(
        check for check in compiled if check["affected_invariant"] == "migration caller-reliance contract"
    )
    assert "caller reliance" in " ".join(migration_check["required_evidence"])


def test_review_check_compiler_adds_concrete_maintainability_floor_check() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=3,
        confidence=0.95,
    )
    task = _task().model_copy(
        update={
            "description": "Check behavior first and include concrete maintainability/readability issues.",
            "surface_ids": [surface.surface_id],
        }
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n"
            "+++ b/src/app.py\n"
            "@@ -1,2 +1,3 @@\n"
            " def handle():\n"
            "+    # cacheing result for callers\n"
            "     return None\n"
        ),
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def handle():\n    # cacheing result for callers\n    return None\n",
                        "mental_model_excerpt": "Maintainability/readability concerns must be concrete.",
                        "review_kb_excerpt": "",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node(use_llm=False)(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert any(check["affected_invariant"] == "maintainability contract" for check in compiled)


def test_review_check_validator_rejects_surface_without_line_anchor() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    check = _check(surface_ids=[surface.surface_id])
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )

    reasons = validate_review_check(check, state=state, task=task, slot={})

    assert "missing_surface_line_anchor" in reasons


def test_review_check_compiler_ranks_obligations_with_mental_model(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def RegexExtract(pattern):\n+    return []\n"
            "+def StringSubstring(value, index):\n+    return value[index]\n"
        ),
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": (
                            "def RegexExtract(pattern):\n    return []\n"
                            "def StringSubstring(value, index):\n    return value[index]\n"
                        ),
                        "mental_model_excerpt": (
                            "- risks\n"
                            "- RegexExtract must handle invalid regex patterns without uncaught exceptions.\n"
                        ),
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "StringSubstring",
                                "dimension": "boundary/index handling",
                                "evidence": "index parameter present",
                            },
                            {
                                "file_path": "src/app.py",
                                "surface": "RegexExtract",
                                "dimension": "exception/control-flow scope",
                                "evidence": "regex pattern can be invalid",
                            },
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    floor = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage_floor"]
    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert floor["ranked_obligations"][0]["surface"] == "RegexExtract"
    assert "surface_in_mental_model" in floor["ranked_obligations"][0]["relevance_reasons"]
    assert "evidence_matches_context" in floor["ranked_obligations"][0]["relevance_reasons"]
    assert compiled[0]["changed_code_anchor"] == "RegexExtract"
    assert any(
        "invalid regex patterns" in item
        for item in compiled[0]["required_evidence"]
    )


def test_review_check_compiler_uses_source_order_without_relevance_signal(monkeypatch) -> None:
    task = ReviewTask(
        id="neutral-task",
        title="Changed code audit",
        description="Audit changed behavior.",
        target_files=["src/app.py"],
        specialty="logic",
    )
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        current_task_id=task.id,
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def first_surface(value):\n+    return value[0]\n"
            "+def second_surface():\n+    return None\n"
        ),
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": (
                            "def first_surface(value):\n    return value[0]\n"
                            "def second_surface():\n    return None\n"
                        ),
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "first_surface",
                                "dimension": "boundary/index handling",
                                "evidence": "index parameter present",
                            },
                            {
                                "file_path": "src/app.py",
                                "surface": "second_surface",
                                "dimension": "contract completeness",
                                "evidence": "return contract present",
                            },
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    floor = out["metadata"]["review_checks"]["by_task"][task.id]["compiler_coverage_floor"]
    compiled = out["metadata"]["review_checks"]["by_task"][task.id]["compiled_checks"]
    assert [row["surface"] for row in floor["ranked_obligations"]] == [
        "first_surface",
        "second_surface",
    ]
    assert compiled[0]["changed_code_anchor"] == "first_surface"


def test_review_check_compiler_floor_adds_high_relevance_obligation_first(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def RegexExtract(pattern):\n+    return []\n"
            "+def StringSubstring(value, index):\n+    return value[index]\n"
        ),
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": (
                            "def RegexExtract(pattern):\n    return []\n"
                            "def StringSubstring(value, index):\n    return value[index]\n"
                        ),
                        "mental_model_excerpt": (
                            "- RegexExtract must handle invalid regex patterns without uncaught exceptions."
                        ),
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "StringSubstring",
                                "dimension": "boundary/index handling",
                                "evidence": "index parameter present",
                            },
                            {
                                "file_path": "src/app.py",
                                "surface": "RegexExtract",
                                "dimension": "exception/control-flow scope",
                                "evidence": "regex pattern can be invalid",
                            },
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    added = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage_floor"]["added_checks"]
    assert added[0]["changed_code_anchor"] == "RegexExtract"


def test_review_check_compiler_prompt_includes_ranked_obligation_reasons(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[])
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )
    state = _state(
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def handle():\n    return None\n",
                        "mental_model_excerpt": "- contracts\n- RETURN_TYPES requires a string result.",
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "handle",
                                "dimension": "contract completeness",
                                "evidence": "entry point implies a return contract",
                            }
                        ],
                    }
                }
            },
        },
    )

    make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    prompt = fake.prompts[0]
    assert "## Ranked Coverage Obligations" in prompt
    assert "relevance_reasons" in prompt
    assert "## Mental Model Contract Material" in prompt
    assert "RETURN_TYPES requires a string result" in prompt


def test_review_check_compiler_coverage_floor_respects_cap(monkeypatch) -> None:
    llm_checks = [
        _check(check_id=f"review-logic:check:{idx}", changed_code_anchor="handle")
        for idx in range(1, 10)
    ]
    output = ReviewCheckCompilerOutput(summary="compiled", checks=llm_checks)
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def handle():\n    return None\n",
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": f"surface_{idx}",
                                "dimension": "boundary/index handling",
                                "evidence": "needs bounds behavior",
                            }
                            for idx in range(3)
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    floor = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage_floor"]
    assert out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_count"] == 12
    assert len(floor["added_checks"]) == 3
    assert len(floor["skipped_due_to_cap"]) == 0


def test_review_check_compiler_prioritizes_local_behavior_floor_over_broad_surface_cap(monkeypatch) -> None:
    llm_checks = [
        _check(
            check_id=f"review-logic:surface:{idx}",
            changed_code_anchor=f"Surface{idx}",
            affected_invariant="api/signature compatibility",
            required_evidence=[
                f"changed implementation for Surface{idx}",
                "repository contract or local caller evidence when the local code is insufficient",
            ],
            report_criteria=[f"The changed Surface{idx} violates api contract on a reachable path."],
        )
        for idx in range(1, 13)
    ]
    output = ReviewCheckCompilerOutput(summary="compiled", checks=llm_checks)
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": (
                            "def fold_widgets(tokens):\n"
                            "    return '-'.join(tokens)\n"
                            "def emit_checksum(parts):\n"
                            "    return ''.join(parts)\n"
                        ),
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "fold_widgets",
                                "dimension": "widget token folding",
                                "evidence": "local widget token fold table present",
                                "files_complete": True,
                            },
                            {
                                "file_path": "src/app.py",
                                "surface": "emit_checksum",
                                "dimension": "adapter checksum emission",
                                "evidence": "local checksum join path present",
                                "files_complete": True,
                            },
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled_ids = [check["check_id"] for check in task_meta["compiled_checks"]]
    floor = task_meta["compiler_coverage_floor"]
    assert task_meta["compiled_count"] == 12
    assert "review-logic:coverage:13" in compiled_ids
    assert "review-logic:coverage:14" in compiled_ids
    assert "review-logic:surface:11" in floor["trimmed_existing_check_ids"]
    assert "review-logic:surface:12" in floor["trimmed_existing_check_ids"]


def test_review_check_compiler_fans_out_omitted_file_check(monkeypatch) -> None:
    task = _task().model_copy(update={"target_files": ["src/app.py", "src/other.py"]})
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n+def handle():\n"
            "diff --git a/src/other.py b/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n+VALUE = 1\n"
        ),
        metadata={
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def handle():\n    return None\n",
                        "task_evidence": {
                            "primary_files": ["src/app.py"],
                            "omitted_prompt_files": ["src/other.py"],
                        },
                    }
                }
            }
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled = task_meta["compiled_checks"]
    omitted = [check for check in compiled if check["check_id"].startswith("review-logic:omitted-file:")]
    assert omitted, compiled
    assert omitted[0]["file_path"] == "src/other.py"
    assert omitted[0]["allowed_retrieval"] == ["focused_context", "task_evidence"]


def test_review_check_compiler_fans_out_omitted_changed_surfaces(monkeypatch) -> None:
    one = ReviewSurface(
        surface_id="surface:one",
        name="first_changed",
        kind="function",
        file_path="src/other.py",
        line_start=2,
        line_end=3,
        confidence=0.95,
    )
    two = ReviewSurface(
        surface_id="surface:two",
        name="second_changed",
        kind="function",
        file_path="src/other.py",
        line_start=6,
        line_end=7,
        confidence=0.95,
    )
    task = _task().model_copy(update={"target_files": ["src/app.py", "src/other.py"]})
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n+def handle():\n"
            "diff --git a/src/other.py b/src/other.py\n+++ b/src/other.py\n@@ -2,6 +2,6 @@\n"
            "+def first_changed():\n+    return 1\n+def second_changed():\n+    return 2\n"
        ),
        metadata={
            "mental_model": {"surface_ledger": [one.model_dump(mode="json"), two.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def handle():\n    return None\n",
                        "task_evidence": {
                            "primary_files": ["src/app.py"],
                            "omitted_prompt_files": ["src/other.py"],
                        },
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    omitted_surface_ids = [
        check["surface_ids"][0]
        for check in compiled
        if check["check_id"].startswith("review-logic:omitted-surface:")
    ]
    assert omitted_surface_ids == ["surface:one", "surface:two"]


def test_review_check_compiler_does_not_trim_mandatory_omitted_file_under_cap(monkeypatch) -> None:
    task = _task().model_copy(update={"target_files": ["src/app.py", "src/other.py"]})
    llm_checks = [
        _check(check_id=f"review-logic:check:{idx}", changed_code_anchor="handle")
        for idx in range(1, 13)
    ]
    output = ReviewCheckCompilerOutput(summary="compiled", checks=llm_checks)
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@ -1 +1 @@\n+def handle():\n"
            "diff --git a/src/other.py b/src/other.py\n+++ b/src/other.py\n@@ -1 +1 @@\n+VALUE = 1\n"
        ),
        metadata={
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def handle():\n    return None\n",
                        "task_evidence": {
                            "primary_files": ["src/app.py"],
                            "omitted_prompt_files": ["src/other.py"],
                        },
                    }
                }
            }
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled_ids = [check["check_id"] for check in task_meta["compiled_checks"]]
    skipped = task_meta["compiler_coverage_floor"]["skipped_due_to_cap"]
    assert task_meta["compiled_count"] == 13
    assert any(check_id.startswith("review-logic:omitted-file:") for check_id in compiled_ids), compiled_ids
    assert not any(item.get("dimension") == "omitted prompt file" for item in skipped)
    assert all(item.get("origin_kind") for item in skipped)


def test_review_check_validator_moves_only_valid_checks_to_state() -> None:
    valid = _check()
    invalid = _check(check_id="review-logic:check:bad", behavioral_question="Find bugs")
    state = _state(
        metadata={
            **_state()["metadata"],
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "compiled_checks": [
                            valid.model_dump(mode="json"),
                            invalid.model_dump(mode="json"),
                        ]
                    }
                }
            },
        }
    )

    out = make_review_check_validator_node()(state)  # type: ignore[arg-type]

    assert [check.check_id for check in out["review_checks"]] == [valid.check_id]
    assert out["invalid_review_checks"][0].check.check_id == invalid.check_id


def test_review_check_validator_rejects_anchor_outside_changed_code() -> None:
    valid = _check()
    invalid = _check(
        check_id="review-logic:check:bad-anchor",
        file_path="src/other.py",
        changed_code_anchor="unrelated",
    )
    state = _state(
        metadata={
            **_state()["metadata"],
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "compiled_checks": [
                            valid.model_dump(mode="json"),
                            invalid.model_dump(mode="json"),
                        ]
                    }
                }
            },
        }
    )

    out = make_review_check_validator_node()(state)  # type: ignore[arg-type]

    assert [check.check_id for check in out["review_checks"]] == [valid.check_id]
    assert "anchor_not_in_changed_code" in out["invalid_review_checks"][0].reasons


def test_review_check_validator_accepts_descriptive_allowed_retrieval() -> None:
    check = _check(allowed_retrieval=["BrowserConfig class source code"])
    state = _state(
        metadata={
            **_state()["metadata"],
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "compiled_checks": [check.model_dump(mode="json")]
                    }
                }
            },
        }
    )

    out = make_review_check_validator_node()(state)  # type: ignore[arg-type]

    assert [item.check_id for item in out["review_checks"]] == [check.check_id]
    assert out["invalid_review_checks"] == []


def test_review_check_context_planner_creates_check_scoped_requests() -> None:
    state = _state(review_checks=[_check(required_evidence=["caller authorization guard"])])

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    req = out["focused_context_requests"][0]
    assert isinstance(req, FocusedContextRequest)
    assert req.request_id == "check:review-logic:check:1:1"
    assert req.candidate_id == "review-logic:check:1"


def test_review_check_context_planner_treats_descriptive_retrieval_as_focused() -> None:
    state = _state(
        review_checks=[
            _check(
                required_evidence=["caller authorization guard"],
                allowed_retrieval=["caller source code and repository evidence"],
            )
        ]
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    assert out["focused_context_requests"]


def test_review_check_context_planner_loops_on_missing_evidence_with_budget() -> None:
    first = FocusedContextRequest(
        request_id="check:review-logic:check:1:1",
        candidate_id="review-logic:check:1",
        requested_by_specialty="logic",
        file_paths=["src/app.py"],
        text_queries=["caller authorization guard"],
        reason="first pass",
    )
    state = _state(
        review_checks=[_check(required_evidence=["caller authorization guard"], budget=2)],
        focused_context_requests=[first],
        review_check_results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["other guard still enforces the rule"],
            )
        ],
    )

    assert critique_pipeline._route_after_review_check_executor(state) == "review_check_context_planner"
    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    req = out["focused_context_requests"][0]
    assert req.request_id == "check:review-logic:check:1:2"
    assert req.text_queries == ["src/app.py handle other guard still enforces the rule"]
    assert "anchor=handle" in req.reason


def test_review_check_context_planner_replaces_generic_queries_with_anchored_query() -> None:
    state = _state(review_checks=[_check(required_evidence=["changed code behavior"])])

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    req = out["focused_context_requests"][0]
    assert req.text_queries
    assert "src/app.py" in req.text_queries[0]
    assert "handle" in req.text_queries[0]


def test_review_check_context_planner_requests_external_contract_evidence() -> None:
    state = _state(
        review_checks=[
            _check(
                required_evidence=["changed handle implementation"],
                report_criteria=["The public API caller contract requires a string result."],
            )
        ]
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    req = out["focused_context_requests"][0]
    assert all("src/app.py" in query for query in req.text_queries)
    assert all("handle" in query for query in req.text_queries)
    assert any("public API caller contract" in query for query in req.text_queries)


def test_review_check_context_planner_dedupes_repeated_requests() -> None:
    existing = FocusedContextRequest(
        request_id="check:review-logic:check:1:1",
        candidate_id="review-logic:check:1",
        requested_by_specialty="logic",
        file_paths=["src/app.py"],
        symbol_queries=["handle"],
        text_queries=["src/app.py handle other guard still enforces the rule"],
        reason="already asked",
    )
    state = _state(
        review_checks=[_check(required_evidence=["caller authorization guard"], budget=2)],
        focused_context_requests=[existing],
        review_check_results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["other guard still enforces the rule"],
            )
        ],
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    assert out["focused_context_requests"] == []


def test_review_check_context_planner_allows_new_missing_evidence_after_dedupe() -> None:
    existing = FocusedContextRequest(
        request_id="check:review-logic:check:1:1",
        candidate_id="review-logic:check:1",
        requested_by_specialty="logic",
        file_paths=["src/app.py"],
        symbol_queries=["handle"],
        text_queries=["src/app.py handle other guard still enforces the rule"],
        reason="already asked",
    )
    state = _state(
        review_checks=[_check(required_evidence=["caller authorization guard"], budget=2)],
        focused_context_requests=[existing],
        review_check_results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["repository convention for handle fallback"],
            )
        ],
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    assert len(out["focused_context_requests"]) == 1
    assert "repository convention" in out["focused_context_requests"][0].text_queries[0]


def test_review_check_context_planner_retries_full_file_after_truncated_class_evidence() -> None:
    check = _check(
        check_id="review-logic:compare",
        changed_code_anchor="StringCompare",
        file_path="src/app.py",
        line_start=159,
        line_end=189,
        budget=2,
    )
    existing = FocusedContextRequest(
        request_id="check:review-logic:compare:1",
        candidate_id=check.check_id,
        requested_by_specialty="logic",
        file_read_mode="slice",
        file_paths=["src/app.py"],
        symbol_queries=["StringCompare"],
        text_queries=["src/app.py StringCompare class body"],
        reason="first slice",
    )
    state = _state(
        review_checks=[check],
        focused_context_requests=[existing],
        review_check_results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["Full class definition including execute method"],
                reportable_reason="Focused evidence was truncated and only class declaration was visible.",
            )
        ],
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    req = out["focused_context_requests"][0]
    assert req.request_id == "check:review-logic:compare:2"
    assert req.file_read_mode == "full"
    assert req.file_paths == ["src/app.py"]


def test_review_check_loop_stops_when_budget_exhausted() -> None:
    requests = [
        FocusedContextRequest(
            request_id=f"check:review-logic:check:1:{idx}",
            candidate_id="review-logic:check:1",
            requested_by_specialty="logic",
            file_paths=["src/app.py"],
            text_queries=["caller authorization guard"],
            reason="loop",
        )
        for idx in (1, 2)
    ]
    state = _state(
        review_checks=[_check(required_evidence=["caller authorization guard"], budget=2)],
        focused_context_requests=requests,
        review_check_results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["other guard still enforces the rule"],
            )
        ],
    )

    assert critique_pipeline._route_after_review_check_executor(state) == "review_check_evidence_gate"


def test_review_check_executor_marks_budget_exhausted(monkeypatch) -> None:
    check = _check(required_evidence=["other guard still enforces the rule"], budget=1)
    request = FocusedContextRequest(
        request_id="check:review-logic:check:1:1",
        candidate_id="review-logic:check:1",
        requested_by_specialty="logic",
        file_paths=["src/app.py"],
        text_queries=["other guard still enforces the rule"],
        reason="spent budget",
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["other guard still enforces the rule"],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(
        _state(review_checks=[check], focused_context_requests=[request])
    )  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "budget_exhausted"
    assert "review_check_budget_exhausted" in result.warnings


def test_review_check_executor_downgrades_weak_no_finding(monkeypatch) -> None:
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="no_finding",
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(_state(review_checks=[_check()]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "unsupported"
    assert result.missing_evidence
    assert "weak_no_finding_requires_more_evidence" in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert "executor_weak_no_finding_downgraded:review-logic:check:1" in meta["executor_warnings"]


def test_review_check_executor_downgrades_weak_no_finding_to_budget_exhausted(monkeypatch) -> None:
    check = _check(budget=1)
    request = FocusedContextRequest(
        request_id="check:review-logic:check:1:1",
        candidate_id="review-logic:check:1",
        requested_by_specialty="logic",
        file_paths=["src/app.py"],
        text_queries=["declared return contract"],
        reason="spent budget",
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=["Insufficient evidence to confirm a defect."],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(
        _state(review_checks=[check], focused_context_requests=[request])
    )  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "budget_exhausted"
    assert "weak_no_finding_requires_more_evidence" in result.warnings
    assert "review_check_budget_exhausted" in result.warnings


def test_review_check_executor_source_only_overrides_missing_return_no_finding(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does execute have a missing return fallthrough?",
        affected_invariant="missing return fallthrough",
        required_evidence=["changed execute implementation"],
        report_criteria=["A changed path falls through without returning."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=["All branches return and an else fallback exists."],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        review_checks=[check],
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def execute(mode):\n    if mode == 'A':\n        return (True,)\n"
                            },
                            "files_complete": {"src/app.py": True},
                        }
                    }
                }
            },
        },
    )

    out = make_review_check_executor_node()(state)  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "candidate"
    assert result.candidate is not None
    assert result.candidate.behavioral_symptom == "missing_return"
    assert "source_only_no_finding_overridden" in result.warnings


def test_review_check_executor_source_only_overrides_syntax_no_finding(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does the changed file avoid syntax parse errors?",
        affected_invariant="syntax parse validity",
        required_evidence=["changed source parses"],
        report_criteria=["The changed source does not parse."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=["The changed file parses successfully."],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        review_checks=[check],
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "task_evidence": {
                            "file_contents": {"src/app.py": "def execute():\n    if True:\n"},
                            "files_complete": {"src/app.py": True},
                        }
                    }
                }
            },
        },
    )

    out = make_review_check_executor_node()(state)  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "candidate"
    assert result.candidate is not None
    assert result.candidate.behavioral_symptom == "crash"
    assert "SyntaxError" in result.reportable_reason


def test_review_check_executor_source_only_overrides_removed_import_no_finding(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does the changed import removal leave all names defined?",
        affected_invariant="removed import is not still used",
        required_evidence=["removed imports are not referenced"],
        report_criteria=["A removed import name is still used."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=["No removed import names are still referenced."],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        review_checks=[check],
        git_diff="diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n-import time\n",
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "task_evidence": {
                            "file_contents": {"src/app.py": "def execute():\n    return time.sleep(1)\n"},
                            "files_complete": {"src/app.py": True},
                        }
                    }
                }
            },
        },
    )

    out = make_review_check_executor_node()(state)  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "candidate"
    assert result.candidate is not None
    assert "time" in result.reportable_reason


def test_review_check_executor_batches_checks_and_preserves_results(monkeypatch) -> None:
    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 5)]
    outputs = [
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=check.check_id,
                    patch_task_id="review-logic",
                    decision="no_finding",
                    evidence_refs=["src/app.py:1"],
                    suppressing_evidence=["changed path keeps the contract"],
                )
                for check in checks[:3]
            ]
        ),
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[3].check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["caller contract"],
                )
            ]
        ),
    ]

    def fake_worker(*_args: object, **_kwargs: object) -> _FakeLLM:
        return _FakeLLM({"parsed": outputs.pop(0), "raw": _Raw()})

    monkeypatch.setattr("src.orchestration.nodes.application.review_checks.Models.worker", fake_worker)

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    assert [result.check_id for result in out["review_check_results"]] == [
        check.check_id for check in checks
    ]
    assert out["token_usage"] == 14
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_batch_size"] == 3
    assert meta["executor_batch_count"] == 2


def test_review_check_executor_stages_candidate_in_result(monkeypatch) -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        failure_mode="Caller receives None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        recommendation="Return the declared result on this path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom="missing_return",
        root_operation="contract",
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="candidate",
                evidence_refs=["src/app.py:1"],
                reportable_reason="The changed path returns None.",
                candidate=candidate,
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(_state(review_checks=[_check()]))  # type: ignore[arg-type]

    assert out["review_check_results"][0].candidate is not None
    assert out["review_check_results"][0].decision == "candidate"
    assert out["token_usage"] == 7


def test_review_check_evidence_gate_promotes_only_supported_candidates_and_records_gate_results() -> None:
    good = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        failure_mode="handle returns None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        recommendation="Return the declared result on this path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    speculative = good.model_copy(
        update={
            "candidate_id": "review-logic:check:1:spec",
            "content": "This might be wrong.",
        }
    )
    results = [
        ReviewCheckResult(
            check_id="review-logic:check:1",
            patch_task_id="review-logic",
            decision="candidate",
            evidence_refs=["src/app.py:1"],
            reportable_reason="The changed path returns None.",
            candidate=good,
        ),
        ReviewCheckResult(
            check_id="review-logic:check:1",
            patch_task_id="review-logic",
            decision="candidate",
            evidence_refs=["src/app.py:1"],
            reportable_reason="Maybe wrong.",
            candidate=speculative,
        ),
    ]

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=results)
    )  # type: ignore[arg-type]

    assert [cand.candidate_id for cand in out["candidate_findings"]] == [good.candidate_id]
    assert [result.gate_decision for result in out["review_check_results"]] == ["passed", "dropped"]
    assert [result.gate_reason for result in out["review_check_results"]] == [
        "evidence_gate_passed",
        "speculative_or_uncertain_claim",
    ]
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["promoted_count"] == 1
    assert gate["dropped_count"] == 1
    assert gate["reason_counts"] == {
        "evidence_gate_passed": 1,
        "speculative_or_uncertain_claim": 1,
    }
    assert out["task_status_by_id"] == {"review-logic": "completed"}


def test_review_check_evidence_gate_records_candidate_liveness_warning() -> None:
    out = make_review_check_evidence_gate_node()(
        _state(
            review_checks=[_check()],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="no_finding",
                )
            ],
        )
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert "no_executor_candidates_for_valid_checks" in gate["health_warnings"]
    assert "evidence_gate_not_exercised" in gate["health_warnings"]


def test_check_mode_routing(monkeypatch) -> None:
    monkeypatch.setattr(
        critique_pipeline,
        "get_settings",
        lambda: Settings(reviewer_check_mode="off"),
    )
    assert critique_pipeline._route_after_mental_model_enricher({}) == "general_critiquer"

    monkeypatch.setattr(
        critique_pipeline,
        "get_settings",
        lambda: Settings(reviewer_check_mode="log_only"),
    )
    assert critique_pipeline._route_after_mental_model_enricher({}) == "review_check_compiler"
    assert critique_pipeline._route_after_review_check_validator({}) == "general_critiquer"

    monkeypatch.setattr(
        critique_pipeline,
        "get_settings",
        lambda: Settings(reviewer_check_mode="enforced"),
    )
    assert critique_pipeline._route_after_review_check_validator({}) == "review_check_context_planner"


def test_review_check_artifacts_include_raw_and_manifest_metrics(tmp_path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    result = {
        "metadata": {
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "gate": {"dropped_count": 1},
                    }
                }
            }
        },
        "review_checks": [_check()],
        "invalid_review_checks": [],
        "review_check_results": [
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="candidate",
                candidate=CandidateFinding(
                    candidate_id="c1",
                    patch_task_id="review-logic",
                    file_path="src/app.py",
                    line_start=1,
                    line_end=1,
                    content="Issue",
                ),
            )
            .model_copy(update={"gate_decision": "dropped", "gate_reason": "missing_repo_evidence_ref"}),
            ReviewCheckResult(
                check_id="review-logic:check:2",
                patch_task_id="review-logic",
                decision="budget_exhausted",
                missing_evidence=["caller guard"],
            ),
        ],
        "candidate_findings": [],
    }

    path = _write_raw(raw_dir, "slug", result)
    text = path.read_text(encoding="utf-8")
    metrics = _review_check_metrics(result)

    assert '"review_checks"' in text
    assert metrics["compiled_check_count"] == 1
    assert metrics["check_candidate_count"] == 1
    assert metrics["evidence_gate_drop_count"] == 1
    assert metrics["budget_exhausted_check_count"] == 1
    assert json.loads(metrics["review_check_health_warnings"]) == []


def test_review_check_metrics_reports_invalid_reason_health() -> None:
    invalid = _check(file_path="src/other.py", changed_code_anchor="other")
    result = {
        "metadata": {
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "validation": {
                            "reason_counts": {"anchor_not_in_changed_code": 3, "missing_budget": 1}
                        }
                    }
                }
            }
        },
        "review_checks": [],
        "invalid_review_checks": [{"check": invalid.model_dump(mode="json"), "reasons": ["anchor_not_in_changed_code"]}],
        "review_check_results": [],
        "candidate_findings": [],
    }

    metrics = _review_check_metrics(result)

    assert json.loads(metrics["invalid_reason_counts"]) == {
        "anchor_not_in_changed_code": 3,
        "missing_budget": 1,
    }
    assert metrics["dominant_invalid_reason"] == "anchor_not_in_changed_code"
    assert "dominant_invalid_reason:anchor_not_in_changed_code" in json.loads(
        metrics["review_check_health_warnings"]
    )


def test_github_mcp_preflight_records_present_and_missing_tools(monkeypatch) -> None:
    class PresentClient:
        def __init__(self, **_kwargs: object) -> None:
            pass

        def list_tools(self) -> list[str]:
            return ["get_commits_for_path", "get_pull_request"]

    monkeypatch.setattr(aacr, "MCPClient", PresentClient)

    healthy = _github_mcp_preflight(Settings(github_mcp_enabled=True))

    assert healthy["status"] == "ok"
    assert healthy["missing_required_tools"] == []

    class MissingClient(PresentClient):
        def list_tools(self) -> list[str]:
            return ["get_pull_request"]

    monkeypatch.setattr(aacr, "MCPClient", MissingClient)

    degraded = _github_mcp_preflight(Settings(github_mcp_enabled=True))

    assert degraded["status"] == "degraded"
    assert degraded["missing_required_tools"] == ["get_commits_for_path"]


def test_coverage_audit_reports_stage_coverage_and_writes_json(tmp_path) -> None:
    raw = {
        "metadata": {
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "compiled_checks": [_check().model_dump(mode="json")],
                    }
                }
            }
        },
        "review_checks": [_check().model_dump(mode="json")],
        "invalid_review_checks": [],
        "focused_context_requests": [
            FocusedContextRequest(
                request_id="check:review-logic:check:1:1",
                candidate_id="review-logic:check:1",
                requested_by_specialty="logic",
                file_paths=["src/app.py"],
            ).model_dump(mode="json")
        ],
        "focused_context_results": {
            "check:review-logic:check:1:1": {
                "file_snippets": {"src/app.py": "def handle(): ..."},
                "search_hits": {},
            }
        },
        "review_check_results": [
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="candidate",
                candidate=CandidateFinding(
                    candidate_id="c1",
                    patch_task_id="review-logic",
                    file_path="src/app.py",
                    line_start=1,
                    line_end=1,
                    content="Issue",
                ),
            ).model_dump(mode="json")
        ],
        "candidate_findings": [],
    }

    record = _coverage_audit_for_pr(
        pr_url="https://github.com/example/repo/pull/1",
        slug="example__repo__pr1",
        raw=raw,
        final_findings=[],
        labels=[{"path": "src/app.py"}, {"path": "src/missed.py"}],
    )
    payload = _write_coverage_audit(tmp_path / "coverage_audit.json", [record])

    assert record["summary"]["positive_path_count"] == 2
    assert record["summary"]["compiled_path_count"] == 1
    assert record["summary"]["candidate_path_count"] == 1
    assert payload["summary"]["positive_path_count"] == 2


def test_positive_sample_lookup_uses_canonical_pr_url(tmp_path) -> None:
    path = tmp_path / "positive_samples.json"
    path.write_text(
        json.dumps(
            [
                {
                    "githubPrUrl": "https://github.com/comfyanonymous/ComfyUI/pull/7952",
                    "comments": [
                        {
                            "path": "comfy_extras/nodes_string.py",
                            "from_line": 252,
                            "to_line": 330,
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    labels_by_pr = _load_positive_samples_by_pr(path)

    labels = _positive_labels_for_pr(
        labels_by_pr,
        "https://github.com/ComfyAnonymous/ComfyUI/pull/7952/",
    )

    assert labels[0]["path"] == "comfy_extras/nodes_string.py"

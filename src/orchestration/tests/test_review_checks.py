from __future__ import annotations

import json

from typing import Any

from src.config import Settings
from src.domain.schemas import (
    BehavioralSpec,
    CandidateFinding,
    ContractQuestion,
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
from src.orchestration.nodes.application import review_check_compiler_support as compiler_support
from src.orchestration.nodes.application.review_checks import (
    make_review_check_compiler_node,
    make_review_check_context_planner_node,
    make_review_check_evidence_gate_node,
    make_review_check_executor_node,
    make_review_check_scout_node,
    make_review_check_validator_node,
    should_run_review_check_scout,
    validate_review_check,
)
from src.orchestration.nodes.application.review_check_executor_support import (
    no_finding_has_strong_suppression,
    normalize_executor_results,
)
from src.orchestration.context.surface_ledger import (
    build_migration_invariants_from_diff,
    surface_ids_for_task,
)
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
        self._results = list(result) if isinstance(result, list) else None

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        if self._results is not None:
            return self._results.pop(0)
        return self.result


class _SequencedLLM:
    def __init__(self, actions: list[Any], prompts: list[str]) -> None:
        self.actions = actions
        self.prompts = prompts

    def invoke(self, prompt: str) -> Any:
        self.prompts.append(prompt)
        action = self.actions.pop(0)
        if isinstance(action, BaseException):
            raise action
        return {"parsed": action, "raw": _Raw()}


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
        "expected_behavior": "handle returns the declared result on every changed path.",
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


def test_no_finding_requires_suppression_evidence_on_target_file() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_record",
        owned_contract_scope="serialize_record aggregation preserves optional record fields",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized aggregation preserves optional record fields",
        report_criteria=[
            "A reachable aggregation path drops optional record fields or joins non-string values."
        ],
        suppress_criteria=[
            "Aggregation normalizes every optional record field before joining."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:3"],
        reportable_reason="serialize_record handles an empty collection.",
        suppressing_evidence=[
            "serialize_record converts optional record fields to strings before joining and preserves every field."
        ],
        answer_scope="exact",
        suppression_basis=(
            "The produced record contains the optional fields, the selected values include each field, "
            "and the joined output consumes normalized strings for all fields."
        ),
    )

    assert no_finding_has_strong_suppression(result, check)


def test_same_claim_suppression_accepts_direct_refutation() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_record",
        owned_contract_scope="serialize_record aggregation preserves optional record fields",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized aggregation preserves optional record fields",
        report_criteria=[
            "A reachable aggregation path drops optional record fields or joins non-string values."
        ],
        suppress_criteria=[
            "Aggregation normalizes every optional record field before joining."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:3"],
        reportable_reason="serialize_record normalizes optional record fields.",
        suppressing_evidence=[
            "serialize_record converts optional record fields to strings before joining, so non-string values are not joined."
        ],
    )

    assert no_finding_has_strong_suppression(result, check)


def test_projection_evidence_remains_visible_as_no_finding_evidence() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_rows",
        owned_contract_scope="serialize_rows preserves structured row cardinality",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized output preserves every structured row field",
        report_criteria=[
            "A reachable aggregation path selects, skips, drops, or truncates part of each structured row."
        ],
        suppress_criteria=[
            "Concrete evidence shows every structured row field is preserved or intentionally narrowed."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:9"],
        reportable_reason="serialize_rows preserves structured data.",
        suppressing_evidence=[
            "serialize_rows handles structured rows by extracting row[0] from each row and joining all results."
        ],
    )

    assert no_finding_has_strong_suppression(result, check)


def test_documented_projection_can_suppress_preservation_check() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_rows",
        owned_contract_scope="serialize_rows preserves structured row cardinality",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized output preserves every structured row field unless narrowed by contract",
        report_criteria=[
            "A reachable aggregation path selects, skips, drops, or truncates part of each structured row."
        ],
        suppress_criteria=[
            "Concrete evidence shows every structured row field is preserved or intentionally narrowed."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:9"],
        reportable_reason="serialize_rows has a documented projection contract.",
        suppressing_evidence=[
            "serialize_rows extracts row[0], and the documented projection contract says only the first field is required."
        ],
    )

    assert no_finding_has_strong_suppression(result, check)


def test_contract_question_check_requires_action_value_flow_evidence() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=10,
        line_end=30,
        confidence=0.95,
    )
    question = ContractQuestion(
        owner="RecordExtract.execute",
        surface_id=surface.surface_id,
        dimension="data_preservation_cardinality",
        expected_behavior=(
            "RecordExtract.execute projects produced records into selected payload values "
            "for the serialized node output."
        ),
        contract_evidence="RecordExtract.execute produces records and serializes selected values.",
        trigger_variant="multi-record extraction",
        operation="record projection and output serialization",
        breach_question="Can projection select only part of each produced record payload?",
        required_evidence=["record producer and serializer source"],
        source_confidence=0.9,
    )

    check = compiler_support._check_from_contract_question(
        task=_task(),
        question=question,
        surface=surface,
        index=1,
    )

    required = " ".join(check.required_evidence)
    suppress = " ".join(check.suppress_criteria)
    assert "produced value shape" in required
    assert "selected or transformed value shape" in required
    assert "returned, consumed, joined, or serialized value shape" in required
    assert "same action contract" in suppress
    assert check.expected_behavior == question.expected_behavior
    assert "record projection and output serialization" in check.owned_contract_scope


def test_neighboring_suppression_stays_visible_for_adjudication() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_record",
        owned_contract_scope="serialize_record aggregation preserves optional record fields",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized aggregation preserves optional record fields",
        report_criteria=[
            "A reachable aggregation path drops optional record fields or joins non-string values."
        ],
        suppress_criteria=[
            "Aggregation normalizes every optional record field before joining."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:3"],
        reportable_reason="serialize_record handles an empty collection.",
        suppressing_evidence=[
            "serialize_record proves only that joining an empty collection returns an empty output."
        ],
    )
    state = _state()

    normalized, warnings = normalize_executor_results(
        state=state,
        task=_task(),
        slot=state["metadata"]["critique_pipeline"]["by_task"]["review-logic"],
        checks=[check],
        results=[result],
        git_diff="",
        check_budget_remaining=lambda _state, _check: True,
        evidence_requirements_for_check=lambda item: list(item.required_evidence),
        compiled_check_is_source_local=lambda _check: False,
    )

    assert normalized[0].decision == "unsupported"
    assert normalized[0].suppressing_evidence
    assert any("executor_exact_question_mismatch" in item for item in warnings)


def test_executor_transformation_no_finding_without_exact_scope_becomes_unsupported() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_record",
        owned_contract_scope="serialize_record aggregation preserves optional record fields",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized aggregation preserves optional record fields",
        report_criteria=[
            "A reachable aggregation path drops optional record fields or joins non-string values."
        ],
        suppress_criteria=[
            "Aggregation normalizes every optional record field before joining."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:3"],
        reportable_reason="serialize_record reaches join.",
        suppressing_evidence=["The aggregation branch calls join on the selected values."],
        suppression_basis="The aggregation branch calls join on the selected values.",
    )
    state = _state()

    normalized, warnings = normalize_executor_results(
        state=state,
        task=_task(),
        slot=state["metadata"]["critique_pipeline"]["by_task"]["review-logic"],
        checks=[check],
        results=[result],
        git_diff="",
        check_budget_remaining=lambda _state, _check: True,
        evidence_requirements_for_check=lambda item: list(item.required_evidence),
        compiled_check_is_source_local=lambda _check: False,
    )

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:missing_exact_transformation_scope" in normalized[0].warnings
    assert any("executor_exact_question_mismatch" in item for item in warnings)


def test_executor_exact_question_mismatch_becomes_unsupported() -> None:
    check = _check(
        lens="data_shape_consistency",
        changed_code_anchor="serialize_record",
        owned_contract_scope="serialize_record aggregation preserves optional record fields",
        issue_family="aggregation_cardinality",
        affected_invariant="serialized aggregation preserves optional record fields",
        report_criteria=[
            "A reachable aggregation path drops optional record fields or joins non-string values."
        ],
        suppress_criteria=[
            "Aggregation normalizes every optional record field before joining."
        ],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:3"],
        reportable_reason="serialize_record handles an empty collection.",
        suppressing_evidence=[
            "serialize_record proves only that joining an empty collection returns an empty output."
        ],
        answer_scope="neighboring invariant",
        suppression_basis="This answers empty collection behavior, not optional field serialization.",
    )
    state = _state()

    normalized, warnings = normalize_executor_results(
        state=state,
        task=_task(),
        slot=state["metadata"]["critique_pipeline"]["by_task"]["review-logic"],
        checks=[check],
        results=[result],
        git_diff="",
        check_budget_remaining=lambda _state, _check: True,
        evidence_requirements_for_check=lambda item: list(item.required_evidence),
        compiled_check_is_source_local=lambda _check: False,
    )

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:neighboring_answer_scope" in normalized[0].warnings
    assert any("executor_exact_question_mismatch" in item for item in warnings)


def test_review_check_validator_rejects_vague_checks() -> None:
    vague = _check(behavioral_question="Look for security bugs")
    assert "vague_behavioral_question" in validate_review_check(vague)

    valid = _check()
    assert validate_review_check(valid) == []


def test_compiler_normalization_marks_unbacked_feedback_hardening_audit_only() -> None:
    check = _check(
        check_id="feedback",
        lens="error_propagation",
        behavioral_question="Does handle provide clear user-facing error messages?",
        affected_invariant="invalid input feedback",
        expected_behavior="handle should return user-friendly error messages and log invalid input.",
        required_evidence=["handle implementation lines 10-20", "try/except block"],
        report_criteria=["Invalid input is caught without a clear error message or logging."],
        suppress_criteria=["A user-facing error message is returned."],
        audit_only=False,
    )

    normalized = compiler_support.normalize_compiled_checks(_state(), _task(), [check])

    assert normalized[0].audit_only is True


def test_review_check_executor_schema_generates_with_candidate_forward_ref() -> None:
    schema = ReviewCheckExecutorOutput.model_json_schema()
    assert "ReviewCheckResult" in schema.get("$defs", {})


def test_review_check_executor_prompt_frames_result_completeness_as_accounting(monkeypatch) -> None:
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["changed handle implementation"],
            )
        ]
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    make_review_check_executor_node()(_state(review_checks=[_check()]))  # type: ignore[arg-type]

    prompt = fake.prompts[0]
    assert "compact contract packet" in prompt
    assert "It is acceptable to omit an undecidable check" in prompt
    assert "Check Contract Packets" in prompt
    assert "Validated Checks JSON" not in prompt
    assert "directly addresses the check's report criteria" in prompt


def test_review_check_compiler_records_checks_and_trace(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_compiler_node()(_state())

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert task_meta["compiled_count"] == 1
    assert task_meta["contract_lens_selection"]["selected_keys"]
    assert "scores" in task_meta["contract_lens_selection"]
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
    assert compiled[0]["expected_behavior"] == "handle returns the declared result."


def test_contract_questions_compile_before_surface_invariants(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:handle-execute",
        name="Handle.execute",
        kind="method",
        file_path="src/app.py",
        line_start=7,
        line_end=12,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="change handle",
        surfaces=[surface],
        contract_questions=[
            ContractQuestion(
                question_id="q1",
                owner="Handle.execute",
                surface_id=surface.surface_id,
                dimension="return_output_totality",
                expected_behavior="Handle.execute returns the declared output for every owned path.",
                contract_evidence="RETURN_TYPES declares one output.",
                trigger_variant="unrecognized dispatch variant",
                operation="dispatch",
                breach_question="Can a reachable dispatch variant exit without the declared output?",
                direct_suppressor="Concrete runtime or caller evidence proves the variant cannot occur.",
                required_evidence=["declared output shape", "changed dispatch implementation"],
                source_confidence=0.8,
            )
        ],
        surface_invariants=[
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="changed-surface behavior",
                expected_behavior="Handle.execute preserves broad behavior.",
                required_evidence=["changed implementation"],
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    task = _task().model_copy(
        update={
            "surface_ids": [surface.surface_id],
            "target_files": ["src/app.py"],
        }
    )
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "class Handle:\n    def execute(self, mode):\n        return (True,)\n",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node(use_llm=False, settings=settings)(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert compiled[0]["check_id"] == "review-logic:contract-question:1"
    assert compiled[0]["changed_code_anchor"] == "Handle.execute"
    assert compiled[0]["expected_behavior"] == "Handle.execute returns the declared output for every owned path."
    assert not any(check["check_id"].startswith("review-logic:surface:") for check in compiled)


def test_contract_question_compiles_only_for_preferred_surface_task(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:handle-execute",
        name="Handle.execute",
        kind="method",
        file_path="src/app.py",
        line_start=7,
        line_end=12,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="change handle",
        surfaces=[surface],
        contract_questions=[
            ContractQuestion(
                question_id="q1",
                owner="Handle.execute",
                surface_id=surface.surface_id,
                dimension="return_output_totality",
                expected_behavior="Handle.execute returns the declared output.",
                contract_evidence="RETURN_TYPES declares one output.",
                trigger_variant="fallback branch",
                operation="dispatch",
                breach_question="Can fallback exit without the declared output?",
                direct_suppressor="Caller evidence proves fallback cannot occur.",
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    baseline = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    surface_task = baseline.model_copy(
        update={
            "id": "review-logic-surface-fill-1",
            "title": "Diff-local correctness: Handle.execute",
            "surface_ids": [surface.surface_id],
        }
    )
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={baseline.id: baseline, surface_task.id: surface_task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )

    assert compiler_support.checks_from_contract_questions(state, baseline, settings=settings) == []
    owned = compiler_support.checks_from_contract_questions(state, surface_task, settings=settings)

    assert [check.check_id for check in owned] == ["review-logic-surface-fill-1:contract-question:1"]


def test_contract_question_compilation_round_robins_multi_owner_task(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surfaces = [
        ReviewSurface(
            surface_id=f"surface:owner-{idx}",
            name=f"Owner{idx}.execute",
            kind="method",
            file_path="src/app.py",
            line_start=idx * 10,
            line_end=idx * 10 + 5,
            confidence=0.95,
        )
        for idx in range(1, 5)
    ]
    dimensions = [
        "return_output_totality",
        "variant_completeness",
        "data_preservation_cardinality",
        "serialization_type_closure",
    ]
    spec = BehavioralSpec(
        intent_summary="change owners",
        surfaces=surfaces,
        contract_questions=[
            ContractQuestion(
                owner=surface.name,
                surface_id=surface.surface_id,
                dimension=dimension,
                expected_behavior=f"{surface.name} satisfies {dimension}.",
                contract_evidence="Declared node contract.",
                trigger_variant=dimension,
                operation=dimension,
                breach_question=f"Can {surface.name} violate {dimension}?",
            )
            for surface in surfaces
            for dimension in dimensions
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    task = _task().model_copy(update={"surface_ids": [surface.surface_id for surface in surfaces]})
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json") for surface in surfaces]},
        },
    )

    checks = compiler_support.checks_from_contract_questions(state, task, settings=settings)

    first_round_anchors = [check.changed_code_anchor for check in checks[:4]]
    assert first_round_anchors == [surface.name for surface in surfaces]
    assert "Owner4.execute" in [check.changed_code_anchor for check in checks]
    assert len(checks) == 16


def test_surface_invariants_suppressed_for_surfaces_with_routed_contract_questions(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="Handle.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="change handle",
        surfaces=[surface],
        contract_questions=[
            ContractQuestion(
                owner="Handle.execute",
                surface_id=surface.surface_id,
                dimension="return_output_totality",
                expected_behavior="Handle.execute returns the declared output.",
                contract_evidence="RETURN_TYPES declares one output.",
                trigger_variant="fallback branch",
                operation="dispatch",
                breach_question="Can fallback exit without the declared output?",
            )
        ],
        surface_invariants=[
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="changed-surface behavior",
                expected_behavior="Handle.execute preserves broad behavior.",
                required_evidence=["changed implementation"],
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        behavioral_spec_ref=ref,
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )

    invariant_checks = compiler_support.checks_from_surface_invariants(state, task, settings=settings)

    assert invariant_checks == []


def test_broad_surface_invariant_checks_are_audit_only(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="change handle",
        surfaces=[surface],
        surface_invariants=[
            SurfaceInvariant(
                surface_id=surface.surface_id,
                dimension="changed-surface behavior",
                expected_behavior="handle preserves its externally visible contract.",
                required_evidence=["changed implementation"],
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    state = _state(
        behavioral_spec_ref=ref,
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )

    checks = compiler_support.checks_from_surface_invariants(
        state,
        _task().model_copy(update={"surface_ids": [surface.surface_id]}),
        settings=settings,
    )

    assert checks
    assert checks[0].audit_only is True


def test_implementation_shaped_expected_behavior_becomes_audit_only() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )
    check = _check(
        surface_ids=[surface.surface_id],
        expected_behavior="Before calling group(index), the code checks len(groups) >= index.",
        diff_signal_family="llm_compiled",
    )

    normalized = compiler_support.normalize_compiled_checks(state, task, [check])  # type: ignore[arg-type]

    assert normalized[0].audit_only is True


def test_contract_question_expected_behavior_can_name_contract_terms() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )
    check = _check(
        surface_ids=[surface.surface_id],
        expected_behavior="handle returns the declared output shape on every branch.",
        diff_signal_family="contract_question",
    )

    normalized = compiler_support.normalize_compiled_checks(state, task, [check])  # type: ignore[arg-type]

    assert normalized[0].audit_only is False


def test_contract_question_implementation_shaped_expected_behavior_becomes_audit_only() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )
    check = _check(
        surface_ids=[surface.surface_id],
        expected_behavior="Before calling project(record), the code checks len(records) >= index.",
        diff_signal_family="contract_question",
    )

    normalized = compiler_support.normalize_compiled_checks(state, task, [check])  # type: ignore[arg-type]

    assert normalized[0].audit_only is True


def test_generic_coverage_obligation_becomes_audit_only() -> None:
    check = compiler_support.coverage_check_for_obligation(
        _state(),  # type: ignore[arg-type]
        _task(),
        {
            "file_path": "src/app.py",
            "surface": "handle",
            "dimension": "changed-surface behavior",
            "evidence": "review changed behavior",
        },
        1,
    )

    assert check.audit_only is True


def test_normalize_compiled_checks_narrows_non_integration_multi_surface() -> None:
    first = ReviewSurface(
        surface_id="surface:first",
        name="First.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=4,
        confidence=0.95,
    )
    second = first.model_copy(
        update={
            "surface_id": "surface:second",
            "name": "Second.execute",
            "line_start": 8,
            "line_end": 12,
        }
    )
    task = _task().model_copy(update={"surface_ids": [first.surface_id, second.surface_id]})
    state = _state(
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [first.model_dump(mode="json"), second.model_dump(mode="json")]},
        },
    )
    check = _check(
        surface_ids=[first.surface_id, second.surface_id],
        changed_code_anchor="execute",
        behavioral_question="Does execute return the declared value?",
    )

    normalized = compiler_support.normalize_compiled_checks(state, task, [check])  # type: ignore[arg-type]

    assert len(normalized[0].surface_ids) == 1


def test_review_check_compiler_carries_completeness_material_without_extra_check(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(
        summary="compiled",
        checks=[
            _check(
                check_id="review-logic:check:structured",
                lens="data_shape_consistency",
                changed_code_anchor="emit_payload",
                behavioral_question="Does emit_payload preserve structured output fields?",
                affected_invariant="structured output preservation",
                required_evidence=["changed emit_payload implementation"],
                report_criteria=["A reachable path drops a structured output field."],
            )
        ],
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def emit_payload(records):\n+    return records\n"
        ),
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def emit_payload(records):\n    return records\n",
                        "mental_model_excerpt": (
                            "- emit_payload should preserve every field in each structured record."
                        ),
                        "coverage_obligations": [],
                    }
                }
            }
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled = task_meta["compiled_checks"]
    assert task_meta["compiled_count"] == 1
    assert compiled[0]["check_id"] == "review-logic:check:structured"
    assert any(
        item.startswith("mental-model completeness/cardinality contract:")
        for item in compiled[0]["required_evidence"]
    )
    assert any("selects, skips, drops" in item for item in compiled[0]["report_criteria"])


def test_review_check_compiler_does_not_add_completeness_material_without_signal(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(
        summary="compiled",
        checks=[
            _check(
                check_id="review-logic:check:structured",
                lens="data_shape_consistency",
                changed_code_anchor="emit_payload",
                behavioral_question="Does emit_payload preserve structured output fields?",
                affected_invariant="structured output preservation",
                required_evidence=["changed emit_payload implementation"],
            )
        ],
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def emit_payload(records):\n+    return records\n"
        ),
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def emit_payload(records):\n    return records\n",
                        "mental_model_excerpt": "- emit_payload returns the documented value.",
                        "coverage_obligations": [],
                    }
                }
            }
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    assert not any(
        item.startswith("mental-model completeness/cardinality contract:")
        for item in compiled[0]["required_evidence"]
    )


def test_review_check_compiler_completeness_enrichment_does_not_expand_cap(monkeypatch) -> None:
    focused = _check(
        check_id="review-logic:check:structured",
        lens="data_shape_consistency",
        changed_code_anchor="emit_payload",
        behavioral_question="Does emit_payload preserve structured output fields?",
        affected_invariant="structured output preservation",
        required_evidence=["changed emit_payload implementation"],
    )
    llm_checks = [
        focused,
        *[
            _check(
                check_id=f"review-logic:check:{idx}",
                changed_code_anchor=f"helper_{idx}",
                behavioral_question=f"Does helper_{idx} preserve its return contract?",
            )
            for idx in range(2, 13)
        ],
    ]
    output = ReviewCheckCompilerOutput(summary="compiled", checks=llm_checks)
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n"
            "+def emit_payload(records):\n+    return records\n"
        ),
        metadata={
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def emit_payload(records):\n    return records\n",
                        "mental_model_excerpt": (
                            "- emit_payload should preserve every field in each structured record."
                        ),
                        "coverage_obligations": [],
                    }
                }
            }
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    compiled = task_meta["compiled_checks"]
    compiled_ids = [check["check_id"] for check in compiled]
    focused_check = next(check for check in compiled if check["check_id"] == focused.check_id)
    assert task_meta["compiled_count"] == 12
    assert focused.check_id in compiled_ids
    assert any(
        item.startswith("mental-model completeness/cardinality contract:")
        for item in focused_check["required_evidence"]
    )


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
    assert len(compiled_ids) == 16
    assert compiled_ids[0] == "review-logic:regexextract-tuple-indexing"
    assert task_meta["compiler_coverage_floor"]["adaptive_max_checks"] == 16
    assert task_meta["compiler_coverage_floor"]["adaptive_cap_reason"] == "many_primary_owners"
    assert task_meta["compiler_coverage_floor"]["skipped_due_to_cap"]


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


def test_migration_invariants_not_emitted_for_pure_addition() -> None:
    surface = ReviewSurface(
        surface_id="surface:new",
        name="NewNode.execute",
        kind="method",
        file_path="src/nodes.py",
        line_start=10,
        line_end=20,
        confidence=0.95,
    )
    diff = (
        "diff --git a/src/nodes.py b/src/nodes.py\n"
        "+++ b/src/nodes.py\n"
        "@@ -0,0 +1,5 @@\n"
        "+class NewNode:\n"
        "+    def execute(self):\n"
        "+        return (True,)\n"
    )

    invariants = build_migration_invariants_from_diff(
        [surface],
        diff,
        intent_summary="Add a new node.",
    )

    assert invariants == []


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


def test_review_check_compiler_skips_migration_floor_for_pure_addition_prose() -> None:
    surface = ReviewSurface(
        surface_id="surface:new",
        name="NewNode.execute",
        kind="method",
        file_path="src/nodes.py",
        line_start=10,
        line_end=20,
        confidence=0.95,
    )
    task = _task().model_copy(
        update={
            "description": "Check replacement and migration behavior for the new node.",
            "target_files": ["src/nodes.py"],
            "surface_ids": [surface.surface_id],
        }
    )
    state = _state(
        git_diff=(
            "diff --git a/src/nodes.py b/src/nodes.py\n"
            "+++ b/src/nodes.py\n"
            "@@ -0,0 +1,5 @@\n"
            "+class NewNode:\n"
            "+    def execute(self):\n"
            "+        return (True,)\n"
        ),
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "class NewNode:\n    def execute(self):\n        return (True,)\n",
                        "mental_model_excerpt": "This prose mentions migration and replacement.",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )

    checks = compiler_support.migration_floor_checks(
        state,  # type: ignore[arg-type]
        task,
        compiler_support.pipeline_slot(state, task.id),  # type: ignore[arg-type]
        [],
        1,
    )

    assert checks == []


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


def test_review_check_compiler_fair_cap_keeps_later_surface_source_local_check(monkeypatch) -> None:
    alpha = ReviewSurface(
        surface_id="surface:alpha",
        name="alpha",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=3,
        confidence=0.95,
    )
    beta = ReviewSurface(
        surface_id="surface:beta",
        name="beta",
        kind="function",
        file_path="src/app.py",
        line_start=5,
        line_end=7,
        confidence=0.95,
    )
    task = _task().model_copy(update={"surface_ids": [alpha.surface_id, beta.surface_id]})
    llm_checks = [
        _check(
            check_id=f"review-logic:alpha:{idx}",
            surface_ids=[alpha.surface_id],
            changed_code_anchor="alpha",
            behavioral_question="Does alpha preserve branch behavior and return shape?",
            affected_invariant="branch behavior and return shape",
            required_evidence=["changed alpha implementation", "branch and return evidence"],
            report_criteria=["A reachable alpha branch returns the wrong shape."],
        )
        for idx in range(1, 17)
    ]
    llm_checks.append(
        _check(
            check_id="review-logic:beta:1",
            surface_ids=[beta.surface_id],
            changed_code_anchor="beta",
            behavioral_question="Does beta preserve branch behavior and return shape?",
            affected_invariant="branch behavior and return shape",
            required_evidence=["changed beta implementation", "branch and return evidence"],
            report_criteria=["A reachable beta branch returns the wrong shape."],
        )
    )
    output = ReviewCheckCompilerOutput(summary="compiled", checks=llm_checks)
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        metadata={
            "mental_model": {"surface_ledger": [alpha.model_dump(mode="json"), beta.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def alpha():\n    return 1\n\ndef beta():\n    return 2\n",
                            },
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
    compiled_ids = [check["check_id"] for check in task_meta["compiled_checks"]]
    assert task_meta["compiled_count"] == 16
    assert task_meta["compiler_coverage_floor"]["adaptive_max_checks"] == 16
    assert task_meta["compiler_coverage_floor"]["adaptive_cap_reason"] == "eligible_non_audit_over_base_cap"
    assert "review-logic:beta:1" in compiled_ids
    assert "review-logic:alpha:16" in task_meta["compiler_coverage_floor"]["trimmed_existing_check_ids"]


def test_review_check_compiler_keeps_late_owner_high_signal_checks_under_adaptive_cap(monkeypatch) -> None:
    surfaces = [
        ReviewSurface(
            surface_id=f"surface:owner-{idx}",
            name=f"Owner{idx}.execute",
            kind="method",
            file_path="src/app.py",
            line_start=idx * 10,
            line_end=idx * 10 + 5,
            confidence=0.95,
        )
        for idx in range(1, 5)
    ]
    task = _task().model_copy(update={"surface_ids": [surface.surface_id for surface in surfaces]})
    early_checks = [
        _check(
            check_id=f"review-logic:owner{owner}:{idx}",
            surface_ids=[surfaces[owner - 1].surface_id],
            changed_code_anchor=f"Owner{owner}.execute",
            behavioral_question=f"Does Owner{owner}.execute preserve declared return shape for branch {idx}?",
            affected_invariant="declared return shape",
            required_evidence=[f"changed Owner{owner}.execute implementation"],
            report_criteria=["A reachable branch returns the wrong shape."],
        )
        for owner in range(1, 4)
        for idx in range(1, 6)
    ]
    late_checks = [
        _check(
            check_id="review-logic:owner4:tuple-cardinality",
            surface_ids=[surfaces[3].surface_id],
            changed_code_anchor="Owner4.execute",
            behavioral_question="Does Owner4.execute preserve tuple/cardinality data groups?",
            affected_invariant="tuple/cardinality data preservation",
            required_evidence=["changed Owner4.execute implementation", "tuple cardinality data group handling"],
            report_criteria=["A reachable path drops tuple data groups."],
        ),
        _check(
            check_id="review-logic:owner4:join-none",
            surface_ids=[surfaces[3].surface_id],
            changed_code_anchor="Owner4.execute",
            issue_family="serialization_type_closure",
            behavioral_question="Does Owner4.execute serialize None/type closure safely before join?",
            affected_invariant="serialization/type closure",
            required_evidence=["changed Owner4.execute implementation", "join serialization type closure"],
            report_criteria=["A reachable path joins None or non-string values."],
        ),
        _check(
            check_id="review-logic:owner4:group-index",
            surface_ids=[surfaces[3].surface_id],
            changed_code_anchor="Owner4.execute",
            issue_family="index_bounds",
            behavioral_question="Does Owner4.execute handle group_index bounds for group 0 and captured groups?",
            affected_invariant="group_index bounds",
            required_evidence=["changed Owner4.execute implementation", "group_index bounds handling"],
            report_criteria=["A reachable group index is checked against the wrong bound."],
        ),
        _check(
            check_id="review-logic:owner4:aggregation",
            surface_ids=[surfaces[3].surface_id],
            changed_code_anchor="Owner4.execute",
            issue_family="aggregation",
            behavioral_question="Does Owner4.execute aggregate all produced values before returning?",
            affected_invariant="aggregation completeness",
            required_evidence=["changed Owner4.execute implementation", "aggregate combine path"],
            report_criteria=["A reachable path omits a value from the aggregation."],
        ),
    ]
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[*early_checks, *late_checks])
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )
    state = _state(
        task_registry={task.id: task},
        metadata={
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json") for surface in surfaces]},
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "\n\n".join(
                            f"class Owner{idx}:\n    def execute(self):\n        return ()"
                            for idx in range(1, 5)
                        ),
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "\n\n".join(
                                    f"class Owner{idx}:\n    def execute(self):\n        return ()"
                                    for idx in range(1, 5)
                                )
                            },
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
    compiled_ids = [check["check_id"] for check in task_meta["compiled_checks"]]
    floor = task_meta["compiler_coverage_floor"]
    assert task_meta["compiled_count"] == 16
    assert floor["adaptive_max_checks"] == 16
    assert floor["adaptive_cap_reason"] == "many_primary_owners"
    assert "review-logic:owner4:tuple-cardinality" in compiled_ids
    assert "review-logic:owner4:join-none" in compiled_ids
    assert "review-logic:owner4:group-index" in compiled_ids
    assert "review-logic:owner4:aggregation" in compiled_ids
    assert "Owner4.execute" in floor["owner_fair_cap"]["selected_checks_by_primary_owner"]


def test_compiler_swaps_same_owner_floor_for_trimmed_high_signal_check() -> None:
    surface = ReviewSurface(
        surface_id="surface:owner",
        name="Owner.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=20,
        confidence=0.95,
    )
    selected = [
        _check(
            check_id=f"review-logic:check:{idx}",
            surface_ids=[surface.surface_id],
            changed_code_anchor="Owner.execute",
            behavioral_question=f"Does Owner.execute preserve declared return shape {idx}?",
            affected_invariant="declared return shape",
            required_evidence=["changed Owner.execute implementation"],
        )
        for idx in range(1, 12)
    ]
    floor = _check(
        check_id="review-logic:coverage:12",
        surface_ids=[surface.surface_id],
        changed_code_anchor="Owner.execute",
        behavioral_question="Does Owner.execute preserve assigned surface behavior?",
        affected_invariant="assigned surface behavior",
        required_evidence=["changed Owner.execute implementation"],
    )
    selected.append(floor)
    incoming = _check(
        check_id="review-logic:tuple-payload",
        surface_ids=[surface.surface_id],
        changed_code_anchor="Owner.execute",
        issue_family="data_preservation_cardinality",
        behavioral_question="Does Owner.execute preserve tuple/cardinality data groups?",
        affected_invariant="tuple/cardinality data preservation",
        required_evidence=["changed Owner.execute implementation", "tuple cardinality data group handling"],
        report_criteria=["A reachable path drops tuple data groups."],
    )

    capped, swaps = compiler_support.preserve_trimmed_high_signal_checks(
        selected,
        [*selected, incoming],
        original_ids={*(check.check_id for check in selected[:-1]), incoming.check_id},
        mandatory_ids=set(),
        by_id={surface.surface_id: surface},
        slot={"direct_context": "class Owner:\n    def execute(self):\n        return ()\n"},
        task_files=["src/app.py"],
    )

    capped_ids = [check.check_id for check in capped]
    assert len(capped_ids) == 12
    assert incoming.check_id in capped_ids
    assert floor.check_id not in capped_ids
    assert swaps == [
        {
            "incoming_check_id": incoming.check_id,
            "replaced_check_id": floor.check_id,
            "family": "data_cardinality",
            "primary_owner_labels": ["Owner.execute"],
        }
    ]


def test_check_signal_family_uses_structured_metadata_not_prose() -> None:
    check = _check(
        check_id="task:check:prose",
        lens="other",
        issue_family="",
        diff_signal_family="",
        behavioral_question="Can join of optional grouped values lose nested cardinality?",
        affected_invariant="Tuple group indexing and serialization must preserve values.",
        required_evidence=["groups, tuples, joins, and indexes"],
    )

    assert compiler_support._check_signal_family(check) == "other"

    structured = check.model_copy(update={"issue_family": "serialization_type_closure"})

    assert compiler_support._check_signal_family(structured) == "serialization_type"


def test_review_check_compiler_broad_surface_check_does_not_cover_specific_dimension(monkeypatch) -> None:
    broad = _check(
        check_id="review-logic:surface:1",
        changed_code_anchor="handle",
        behavioral_question="Does the changed handle preserve api contract?",
        affected_invariant="handle preserves caller-visible inputs, outputs, and exception behavior unless changed.",
        required_evidence=["changed implementation for handle"],
        report_criteria=["The changed handle violates api contract on a reachable path."],
    )
    output = ReviewCheckCompilerOutput(summary="compiled", checks=[broad])
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
                        "direct_context": "def handle(mode):\n    if mode:\n        return 1\n",
                        "coverage_obligations": [
                            {
                                "file_path": "src/app.py",
                                "surface": "handle",
                                "dimension": "branch exhaustiveness",
                                "evidence": "conditional branch chain present",
                            }
                        ],
                    }
                }
            },
        },
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    floor = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage_floor"]
    assert any(check["affected_invariant"] == "branch exhaustiveness" for check in floor["added_checks"])


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
        for idx in range(1, 18)
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
    assert task_meta["compiled_count"] == 16
    assert floor["adaptive_max_checks"] == 16
    assert floor["adaptive_cap_reason"] == "eligible_non_audit_over_base_cap"
    assert any(check_id.startswith("review-logic:coverage:") for check_id in compiled_ids)
    assert any(check_id.startswith("review-logic:surface:") for check_id in floor["trimmed_existing_check_ids"])


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
    assert all("surface_already_selected" in item for item in skipped)


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
    assert "exact_question_mismatch:generic_suppression_basis" in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert (
        "executor_exact_question_mismatch:review-logic:check:1:generic_suppression_basis"
        in meta["executor_warnings"]
    )


def test_review_check_executor_downgrades_generic_suppression_basis(monkeypatch) -> None:
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppression_basis="Looks correct.",
                suppressing_evidence=["Looks correct."],
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
    assert "exact_question_mismatch:generic_suppression_basis" in result.warnings


def test_review_check_executor_downgrades_dimension_thin_no_finding(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does build_output preserve each aggregation field and slot?",
        affected_invariant="aggregation field preservation",
        required_evidence=["changed build_output aggregation path", "field and slot preservation evidence"],
        suppress_criteria=["Each aggregated field and slot is preserved."],
        report_criteria=["A reachable path drops, truncates, or reorders an aggregated field."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=["All branches return a string and the return type is consistent."],
            )
        ]
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "unsupported"
    assert "exact_question_mismatch:missing_exact_transformation_scope" in result.warnings
    assert len(fake.prompts) == 1


def test_review_check_executor_keeps_dimension_specific_no_finding(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does build_output preserve each aggregation field and slot?",
        affected_invariant="aggregation field preservation",
        required_evidence=["changed build_output aggregation path", "field and slot preservation evidence"],
        suppress_criteria=["Each aggregated field and slot is preserved."],
        report_criteria=["A reachable path drops, truncates, or reorders an aggregated field."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=[
                    "Each field and slot is preserved in order during aggregation; no entries are dropped."
                ],
                answer_scope="exact",
                suppression_basis=(
                    "The produced aggregation fields and slots are selected in order and the returned value "
                    "contains every field without truncation."
                ),
            )
        ]
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    assert out["review_check_results"][0].decision == "no_finding"


def test_review_check_executor_downgrades_neighboring_mode_suppression(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does mode B preserve the selected structured slot?",
        affected_invariant="mode-specific slot preservation",
        required_evidence=["changed mode B path", "selected slot preservation evidence"],
        suppress_criteria=["Mode B preserves the selected slot."],
        report_criteria=["Mode B drops or reorders the selected slot."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=["Mode A preserves the selected slot and returns the expected shape."],
            )
        ]
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "no_finding"
    assert "llm_suppression_audit_insufficient" not in result.warnings
    assert len(fake.prompts) == 1
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert check.check_id not in meta["suppression_audits"]


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
    assert "exact_question_mismatch:generic_suppression_basis" in result.warnings
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
    assert result.decision == "no_finding"
    assert result.candidate is None
    assert "source_only_no_finding_overridden" not in result.warnings


def test_review_check_executor_source_only_abstains_on_unconditional_return(monkeypatch) -> None:
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
                suppressing_evidence=["The changed function returns unconditionally."],
            )
        ]
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
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
                                "src/app.py": "def execute(mode):\n    return (True,)\n"
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
    assert result.decision == "no_finding"
    assert result.candidate is None
    assert "source_only_no_finding_overridden" not in result.warnings


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
    assert result.decision == "no_finding"
    assert result.candidate is None
    assert "source_only_no_finding_overridden" not in result.warnings


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
    assert result.decision == "no_finding"
    assert result.candidate is None
    assert "source_only_no_finding_overridden" not in result.warnings


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


def test_review_check_executor_records_omitted_batch_results(monkeypatch) -> None:
    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 4)]
    outputs = [
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[0].check_id,
                    patch_task_id="review-logic",
                    decision="no_finding",
                    evidence_refs=["src/app.py:1"],
                    suppressing_evidence=["changed path keeps the contract"],
                )
            ]
        )
    ]

    def fake_worker(*_args: object, **_kwargs: object) -> _FakeLLM:
        return _FakeLLM({"parsed": outputs.pop(0), "raw": _Raw()})

    monkeypatch.setattr("src.orchestration.nodes.application.review_checks.Models.worker", fake_worker)

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    assert [result.check_id for result in out["review_check_results"]] == [
        check.check_id for check in checks
    ]
    assert all(result.candidate is None for result in out["review_check_results"])
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_missing_result_check_ids"] == [checks[1].check_id, checks[2].check_id]
    assert meta["executor_retry_count"] == 0
    assert meta["executor_retry_success_count"] == 0
    omitted = {result.check_id: result for result in out["review_check_results"]}
    assert "executor_omitted_result_recorded_unsupported" in omitted[checks[1].check_id].warnings
    assert omitted[checks[1].check_id].missing_evidence


def test_review_check_executor_records_missing_result_without_retry(monkeypatch) -> None:
    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 3)]
    prompts: list[str] = []
    actions: list[Any] = [
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[0].check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["changed handle implementation"],
                )
            ]
        ),
    ]
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _SequencedLLM(actions, prompts),
    )

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    assert [result.check_id for result in out["review_check_results"]] == [check.check_id for check in checks]
    assert len(prompts) == 1
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_missing_result_check_ids"] == [checks[1].check_id]
    assert meta["executor_retry_count"] == 0
    assert meta["executor_length_limit_retry_count"] == 0


def test_review_check_executor_compact_retry_uses_smaller_context() -> None:
    from src.orchestration.nodes.application.review_checks import _render_executor_prompt

    task = _task()
    check = _check()
    slot = {
        "direct_context": "x" * 12000,
        "mental_model_excerpt": "m" * 6000,
        "review_kb_excerpt": "k" * 6000,
        "task_evidence": {"file_contents": {"src/app.py": "def handle():\n    return None\n"}},
    }
    state = _state()

    full = _render_executor_prompt(state, task, [check], slot)
    compact = _render_executor_prompt(state, task, [check], slot, compact_retry=True)

    assert "This retry contains exactly one input check" in compact
    assert len(compact) < len(full)
    assert "x" * 5000 in full
    assert "x" * 5000 not in compact


def test_review_check_executor_canonicalizes_duplicate_results(monkeypatch) -> None:
    check = _check()
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=3,
        content="handle returns the wrong result on a changed path",
        claim_type="defect",
        failure_mode="wrong result",
        evidence_summary="changed handle implementation returns the wrong value",
        confidence=0.8,
        suspected_category="logic",
        feedback_type="defect_detection",
        severity="high",
        recommendation="Return the declared result for the changed path.",
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="unsupported",
                missing_evidence=["changed handle implementation"],
            ),
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="candidate",
                evidence_refs=["src/app.py:1"],
                reportable_reason="The changed path returns the wrong result.",
                candidate=candidate,
            ),
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    assert len(out["review_check_results"]) == 1
    result = out["review_check_results"][0]
    assert result.decision == "candidate"
    assert result.candidate is not None
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_duplicate_result_check_ids"] == [check.check_id]
    assert meta["executor_result_count_before_canonicalization"] == 2


def test_review_check_executor_same_batch_continuation_revises_target(monkeypatch) -> None:
    check1 = _check(check_id="review-logic:check:1")
    check2 = _check(
        check_id="review-logic:check:2",
        behavioral_question="Does the adjacent changed branch preserve its output contract?",
    )
    first_candidate = CandidateFinding(
        candidate_id="review-logic:c1",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="first issue",
        claim_type="defect",
        failure_mode="first failure",
        evidence_summary="local evidence",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    second_candidate = CandidateFinding(
        candidate_id="review-logic:c2",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=2,
        line_end=3,
        content="second issue",
        claim_type="defect",
        failure_mode="second failure",
        evidence_summary="local evidence",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    fake = _FakeLLM(
        [
            {"parsed": ReviewCheckExecutorOutput(
                results=[
                    ReviewCheckResult(
                        check_id=check1.check_id,
                        patch_task_id="review-logic",
                        decision="candidate",
                        evidence_refs=["src/app.py:1"],
                        reportable_reason="first issue is reportable",
                        candidate=first_candidate,
                    ),
                    ReviewCheckResult(
                        check_id=check2.check_id,
                        patch_task_id="review-logic",
                        decision="no_finding",
                        suppressing_evidence=["looked at nearby branch"],
                    ),
                ]
            ), "raw": _Raw()},
            {"parsed": ReviewCheckExecutorOutput(
                results=[
                    ReviewCheckResult(
                        check_id=check2.check_id,
                        patch_task_id="review-logic",
                        decision="candidate",
                        evidence_refs=["src/app.py:2"],
                        reportable_reason="second issue is distinct",
                        candidate=second_candidate,
                    )
                ]
            ), "raw": _Raw()},
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_check_executor_node()(_state(review_checks=[check1, check2]))  # type: ignore[arg-type]

    decisions = {result.check_id: result.decision for result in out["review_check_results"]}
    assert decisions == {check1.check_id: "candidate", check2.check_id: "candidate"}
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_same_batch_continuation_count"] == 1
    assert meta["executor_same_batch_continuation_revised_check_ids"] == [check2.check_id]
    assert "SAME-BATCH CONTINUATION" in fake.prompts[1]


def test_review_check_executor_same_batch_continuation_rejects_out_of_batch_ids(monkeypatch) -> None:
    check1 = _check(check_id="review-logic:check:1")
    check2 = _check(check_id="review-logic:check:2")
    candidate = CandidateFinding(
        candidate_id="review-logic:c1",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="first issue",
        claim_type="defect",
        failure_mode="first failure",
        evidence_summary="local evidence",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    fake = _FakeLLM(
        [
            {"parsed": ReviewCheckExecutorOutput(
                results=[
                    ReviewCheckResult(
                        check_id=check1.check_id,
                        patch_task_id="review-logic",
                        decision="candidate",
                        evidence_refs=["src/app.py:1"],
                        reportable_reason="first issue is reportable",
                        candidate=candidate,
                    ),
                    ReviewCheckResult(
                        check_id=check2.check_id,
                        patch_task_id="review-logic",
                        decision="unsupported",
                    ),
                ]
            ), "raw": _Raw()},
            {"parsed": ReviewCheckExecutorOutput(
                results=[
                    ReviewCheckResult(
                        check_id="review-logic:check:outside",
                        patch_task_id="review-logic",
                        decision="candidate",
                        candidate=candidate,
                    )
                ]
            ), "raw": _Raw()},
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_check_executor_node()(_state(review_checks=[check1, check2]))  # type: ignore[arg-type]

    decisions = {result.check_id: result.decision for result in out["review_check_results"]}
    assert decisions[check2.check_id] == "unsupported"
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_same_batch_continuation_revised_check_ids"] == []


def test_review_check_executor_retries_length_limit_batch_as_single_checks(monkeypatch) -> None:
    class LengthFinishReasonError(Exception):
        pass

    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 3)]
    prompts: list[str] = []
    actions: list[Any] = [
        LengthFinishReasonError("length limit was reached"),
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[0].check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["changed handle implementation"],
                )
            ]
        ),
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[1].check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["declared return contract"],
                )
            ]
        ),
    ]

    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _SequencedLLM(actions, prompts),
    )

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    assert [result.check_id for result in out["review_check_results"]] == [check.check_id for check in checks]
    assert len(prompts) == 3
    assert "This retry contains exactly one input check" in prompts[1]
    assert "This retry contains exactly one input check" in prompts[2]
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_length_limit_batch_failures"] == [
        {"batch_index": 1, "check_ids": [check.check_id for check in checks]}
    ]
    assert meta["executor_length_limit_retry_count"] == 2
    assert meta["executor_length_limit_retry_success_count"] == 2
    assert meta["executor_length_limit_retry_failed_check_ids"] == []
    assert "executor_length_limit_batch_retry:1" in meta["executor_warnings"]


def test_review_check_executor_length_limit_retry_can_stage_candidate(monkeypatch) -> None:
    class LengthFinishReasonError(Exception):
        pass

    check = _check()
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
    prompts: list[str] = []
    actions: list[Any] = [
        LengthFinishReasonError("Could not parse response content as the length limit was reached"),
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=check.check_id,
                    patch_task_id="review-logic",
                    decision="candidate",
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns None.",
                    candidate=candidate,
                )
            ]
        ),
    ]
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _SequencedLLM(actions, prompts),
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "candidate"
    assert result.candidate is not None
    assert result.candidate.candidate_id == candidate.candidate_id
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_length_limit_retry_success_count"] == 1
    assert meta["executor_candidate_ids"] == [candidate.candidate_id]


def test_review_check_executor_length_limit_retry_failure_is_per_check(monkeypatch) -> None:
    class LengthFinishReasonError(Exception):
        pass

    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 3)]
    prompts: list[str] = []
    actions: list[Any] = [
        LengthFinishReasonError("length limit was reached"),
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[0].check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["changed handle implementation"],
                )
            ]
        ),
        LengthFinishReasonError("length limit was reached again"),
    ]
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _SequencedLLM(actions, prompts),
    )

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    results = {result.check_id: result for result in out["review_check_results"]}
    assert results[checks[0].check_id].warnings == []
    assert results[checks[1].check_id].decision == "unsupported"
    assert "executor_length_limit_retry_failed" in results[checks[1].check_id].warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_length_limit_retry_count"] == 2
    assert meta["executor_length_limit_retry_success_count"] == 1
    assert meta["executor_length_limit_retry_failed_check_ids"] == [checks[1].check_id]


def test_review_check_executor_non_length_failure_uses_existing_batch_fallback(monkeypatch) -> None:
    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 3)]
    prompts: list[str] = []
    actions: list[Any] = [RuntimeError("service unavailable")]
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _SequencedLLM(actions, prompts),
    )

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    assert all(result.decision == "unsupported" for result in out["review_check_results"])
    assert all("review_check_executor_batch_failed:1" in result.warnings for result in out["review_check_results"])
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_length_limit_retry_count"] == 0
    assert any(warning.startswith("review_check_executor_batch_failed:1") for warning in meta["executor_warnings"])


def test_review_check_executor_marks_omitted_result_without_retry(monkeypatch) -> None:
    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 3)]
    outputs = [
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=checks[0].check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["changed handle implementation"],
                )
            ]
        ),
    ]

    def fake_worker(*_args: object, **_kwargs: object) -> _FakeLLM:
        return _FakeLLM({"parsed": outputs.pop(0), "raw": _Raw()})

    monkeypatch.setattr("src.orchestration.nodes.application.review_checks.Models.worker", fake_worker)

    out = make_review_check_executor_node()(_state(review_checks=checks))  # type: ignore[arg-type]

    results = {result.check_id: result for result in out["review_check_results"]}
    assert results[checks[1].check_id].decision == "unsupported"
    assert results[checks[1].check_id].candidate is None
    assert "executor_omitted_result_recorded_unsupported" in results[checks[1].check_id].warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_retry_count"] == 0
    assert meta["executor_retry_success_count"] == 0


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


def test_review_check_executor_synthesizes_missing_candidate_payload(monkeypatch) -> None:
    check = _check()
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="candidate",
                evidence_refs=["src/app.py:1"],
                reportable_reason="The changed handle path returns None on a reachable changed path.",
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "candidate"
    assert result.expected_behavior == check.expected_behavior
    assert result.candidate is not None
    assert result.candidate.candidate_id == f"{check.check_id}:candidate"
    assert result.candidate.expected_behavior == check.expected_behavior
    assert result.candidate.evidence_for_contract
    assert result.candidate.counterexample
    assert result.candidate.rejection_check
    assert "executor_candidate_payload_synthesized" in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_candidate_payload_synthesized_check_ids"] == [check.check_id]
    assert meta["executor_contract_proof_backfill_count"] == 0


def test_review_check_executor_records_contract_proof_backfills(monkeypatch) -> None:
    check = _check()
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
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
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

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_contract_proof_backfill_count"] == 1
    assert meta["executor_contract_proof_backfills"] == [
        {
            "check_id": check.check_id,
            "fields": [
                "expected_behavior",
                "evidence_for_contract",
                "counterexample",
                "rejection_check",
            ],
        }
    ]
    result = out["review_check_results"][0]
    assert result.claim_digest
    assert result.expected_behavior == check.expected_behavior
    assert result.candidate is not None
    assert result.candidate.expected_behavior == check.expected_behavior
    assert result.candidate.claim_digest == result.claim_digest
    assert meta["executor_claim_digests"] == {
        result.candidate.candidate_id: result.candidate.claim_digest
    }
    assert meta["executor_claim_digest_count"] == 1


def test_review_check_executor_downgrades_missing_candidate_payload_without_evidence(monkeypatch) -> None:
    check = _check()
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="candidate",
                reportable_reason="Maybe there is a problem.",
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "unsupported"
    assert result.candidate is None
    assert "executor_candidate_missing_payload" in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_candidate_missing_payload_check_ids"] == [check.check_id]


def test_review_check_executor_audits_schema_only_fallback_suppression(monkeypatch) -> None:
    check = _check(
        behavioral_question="Does execute have an explicit fallback for unexpected mode values?",
        affected_invariant="dispatch fallback completeness",
        report_criteria=["A reachable unexpected mode falls through without a return."],
        suppress_criteria=["The fallback or schema enforcement directly handles unexpected modes."],
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="no_finding",
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=[
                    "The declared mode enum lists all visible options, so no explicit fallback is needed."
                ],
            )
        ]
    )
    fake = _FakeLLM({"parsed": output, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "no_finding"
    assert "llm_suppression_audit_insufficient" not in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert check.check_id not in meta["suppression_audits"]


def test_review_check_evidence_gate_promotes_only_supported_candidates_and_records_gate_results() -> None:
    good = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        expected_behavior="handle returns the declared result on every changed path.",
        failure_mode="handle returns None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        evidence_for_contract="The check invariant and declared return contract require handle to return the result.",
        counterexample="Calling handle on the changed path returns None.",
        rejection_check="The task evidence does not show intentional narrowing or a caller guarantee.",
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


def test_review_check_evidence_gate_drops_audit_only_check_candidates() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        expected_behavior="handle returns the declared result on every changed path.",
        failure_mode="handle returns None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        evidence_for_contract="The check invariant and declared return contract require handle to return the result.",
        counterexample="Calling handle on the changed path returns None.",
        rejection_check="The task evidence does not show intentional narrowing or a caller guarantee.",
        recommendation="Return the declared result on this path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    out = make_review_check_evidence_gate_node()(
        _state(
            review_checks=[_check(audit_only=True)],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="candidate",
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns None.",
                    candidate=candidate,
                )
            ],
        )
    )  # type: ignore[arg-type]

    assert out["candidate_findings"] == []
    result = out["review_check_results"][0]
    assert result.gate_decision == "dropped"
    assert result.gate_reason == "audit_only_check_not_promotable"


def test_review_check_scout_emits_bounded_concrete_obligation_checks() -> None:
    state = _state(
        review_checks=[_check()],
        review_check_results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
            )
        ],
    )
    task = _task()
    state["metadata"]["review_checks"] = {
        "by_task": {
            task.id: {
                "gate": {
                    "promoted_count": 0,
                    "unsupported_high_confidence_surface_ids": ["surface:1"],
                },
                "compiler_coverage_floor": {
                    "uncovered_obligations": [
                        {
                            "file_path": "src/app.py",
                            "surface": "handle",
                            "dimension": "return contract completeness",
                            "evidence": "changed handle implementation",
                            "issue_family": "branch_return",
                            "diff_signal": "+ return None",
                            "line_start": 1,
                            "line_end": 2,
                        }
                    ]
                },
            }
        }
    }

    assert should_run_review_check_scout(state) is True  # type: ignore[arg-type]
    out = make_review_check_scout_node()(state)  # type: ignore[arg-type]

    checks = out["review_checks"]
    assert len(checks) == 1
    assert checks[0].check_id == "review-logic:scout:1"
    assert checks[0].issue_family == "branch_return"
    assert checks[0].allowed_retrieval == ["task_evidence"]
    assert out["metadata"]["review_checks"]["by_task"]["review-logic"]["scout"]["status"] == "emitted"


def test_review_check_scout_accepts_generic_contract_delta_obligations() -> None:
    state = _state(
        review_checks=[_check()],
        review_check_results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
            )
        ],
    )
    task = _task()
    state["metadata"]["review_checks"] = {
        "by_task": {
            task.id: {
                "gate": {
                    "promoted_count": 0,
                    "unsupported_high_confidence_surface_ids": ["surface:1"],
                },
                "compiler_coverage_floor": {
                    "uncovered_obligations": [
                        {
                            "file_path": "src/app.py",
                            "surface": "handle",
                            "dimension": "changed contract delta",
                            "evidence": "changed handle implementation",
                            "diff_signal_family": "contract_delta",
                            "diff_signal": "+ handle now maps legacy values differently",
                            "line_start": 1,
                            "line_end": 2,
                        }
                    ]
                },
            }
        }
    }

    assert should_run_review_check_scout(state) is True  # type: ignore[arg-type]
    out = make_review_check_scout_node()(state)  # type: ignore[arg-type]

    check = out["review_checks"][0]
    assert check.issue_family == "contract_delta"
    assert check.diff_signal_family == "contract_delta"
    assert check.audit_only is False


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


def test_review_check_evidence_gate_records_malformed_candidate_result() -> None:
    out = make_review_check_evidence_gate_node()(
        _state(
            review_checks=[_check()],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="candidate",
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns the wrong result.",
                )
            ],
        )
    )  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.gate_decision == "dropped"
    assert result.gate_reason == "candidate_payload_missing"
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["malformed_candidate_result_check_ids"] == ["review-logic:check:1"]
    assert gate["reason_counts"] == {"candidate_payload_missing": 1}


def test_review_check_evidence_gate_requires_contract_proof_fields() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        expected_behavior="handle returns the declared result on every changed path.",
        failure_mode="handle returns None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        evidence_for_contract="The declared return contract requires a result.",
        rejection_check="No caller guarantee or intentional narrowing is shown.",
        recommendation="Return the declared result on this path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    result = ReviewCheckResult(
        check_id="review-logic:check:1",
        patch_task_id="review-logic",
        decision="candidate",
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path returns None.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gated = out["review_check_results"][0]
    assert gated.gate_decision == "dropped"
    assert gated.gate_reason == "missing_counterexample"
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["reason_counts"] == {"missing_counterexample": 1}
    assert gate["contract_proof"]["missing_candidate_ids"] == [candidate.candidate_id]
    assert gate["contract_proof"]["missing_count"] == 1


def test_review_check_evidence_gate_requires_expected_behavior() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        failure_mode="handle returns None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        evidence_for_contract="The declared return contract requires a result.",
        counterexample="Calling handle on the changed path returns None.",
        rejection_check="No caller guarantee or intentional narrowing is shown.",
        recommendation="Return the declared result on this path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    result = ReviewCheckResult(
        check_id="review-logic:check:1",
        patch_task_id="review-logic",
        decision="candidate",
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path returns None.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gated = out["review_check_results"][0]
    assert gated.gate_decision == "dropped"
    assert gated.gate_reason == "missing_expected_behavior"
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["contract_proof"]["missing_candidate_ids"] == [candidate.candidate_id]


def test_review_check_evidence_gate_drops_advisory_expected_behavior() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler should add a cache.",
        claim_type="performance_regression",
        expected_behavior="Consider adding a cache as a best practice.",
        failure_mode="Repeated work may be slower.",
        evidence_summary="Task evidence shows no cache.",
        evidence_for_contract="Caching would be beneficial.",
        counterexample="Repeated calls recompute the value.",
        rejection_check="No cache exists.",
        recommendation="Add a cache.",
        suspected_category="performance",
        reflection_specialties=["performance"],
    )
    result = ReviewCheckResult(
        check_id="review-logic:check:1",
        patch_task_id="review-logic",
        decision="candidate",
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path does not cache.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gated = out["review_check_results"][0]
    assert gated.gate_decision == "dropped"
    assert gated.gate_reason == "generic_expected_behavior_not_contract"


def test_review_check_evidence_gate_drops_self_doubting_contract_proof() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler now returns None.",
        claim_type="defect",
        expected_behavior="handle returns the declared result on every changed path.",
        failure_mode="handle returns None instead of the declared result.",
        evidence_summary="Task evidence shows handle returns None.",
        evidence_for_contract="The declared return contract requires a result.",
        counterexample="Calling handle on this path returns None, though this may be intentional.",
        rejection_check="No caller guarantee is shown.",
        recommendation="Return the declared result on this path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    result = ReviewCheckResult(
        check_id="review-logic:check:1",
        patch_task_id="review-logic",
        decision="candidate",
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path returns None.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gated = out["review_check_results"][0]
    assert gated.gate_decision == "dropped"
    assert gated.gate_reason == "weak_contract_proof"
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["contract_proof"]["weak_candidate_ids"] == [candidate.candidate_id]
    assert gate["contract_proof"]["weak_count"] == 1


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


def test_aacr_default_check_mode_is_log_only(monkeypatch) -> None:
    monkeypatch.delenv("REVIEW_REVIEWER_CHECK_MODE", raising=False)

    info = aacr._resolve_aacr_review_check_mode(None)

    assert info == {"mode": "log_only", "source": "harness_default"}
    assert aacr.get_settings().reviewer_check_mode == "log_only"
    aacr.get_settings.cache_clear()


def test_aacr_check_mode_prefers_cli_over_harness_default(monkeypatch) -> None:
    monkeypatch.delenv("REVIEW_REVIEWER_CHECK_MODE", raising=False)

    info = aacr._resolve_aacr_review_check_mode({"review_check_mode": "enforced"})

    assert info == {"mode": "enforced", "source": "cli"}
    assert aacr.get_settings().reviewer_check_mode == "enforced"
    aacr.get_settings.cache_clear()


def test_review_check_scout_routes_to_executor_only_when_new_checks_emitted() -> None:
    state = _state()
    task = _task()
    state["metadata"]["review_checks"] = {
        "by_task": {
            task.id: {
                "scout": {
                    "status": "emitted",
                    "emitted_check_ids": ["review-logic:scout:1"],
                }
            }
        }
    }

    assert critique_pipeline._route_after_review_check_scout(state) == "review_check_executor"


def test_review_check_scout_routes_to_end_when_no_new_checks_emitted() -> None:
    state = _state()
    task = _task()
    state["metadata"]["review_checks"] = {
        "by_task": {
            task.id: {
                "scout": {
                    "status": "no_concrete_obligations",
                    "emitted_check_ids": [],
                }
            }
        }
    }

    assert critique_pipeline._route_after_review_check_scout(state) == "end"


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

    healthy = _github_mcp_preflight(
        Settings(github_mcp_enabled=True, github_personal_access_token="token")
    )

    assert healthy["status"] == "ok"
    assert healthy["tool_discovery_available"] is True
    assert healthy["missing_required_tools"] == []
    assert healthy["token_present"] is True
    assert healthy["cwd"]

    class MissingClient(PresentClient):
        def list_tools(self) -> list[str]:
            return ["get_pull_request"]

    monkeypatch.setattr(aacr, "MCPClient", MissingClient)

    degraded = _github_mcp_preflight(
        Settings(github_mcp_enabled=True, github_personal_access_token="token")
    )

    assert degraded["status"] == "degraded"
    assert degraded["tool_discovery_available"] is True
    assert degraded["missing_required_tools"] == ["get_commits_for_path"]

    class FailingDiscoveryClient(PresentClient):
        def list_tools(self) -> list[str]:
            raise RuntimeError("unhandled errors in a TaskGroup (1 sub-exception)")

    monkeypatch.setattr(aacr, "MCPClient", FailingDiscoveryClient)

    discovery_error = _github_mcp_preflight(
        Settings(github_mcp_enabled=True, github_personal_access_token="token")
    )

    assert discovery_error["status"] == "server_startup_failed"
    assert discovery_error["tool_discovery_available"] is False
    assert discovery_error["missing_required_tools"] == []
    assert "TaskGroup" in discovery_error["error"]
    assert "TaskGroup" in discovery_error["error_details"]


def test_github_mcp_preflight_reports_missing_token(monkeypatch) -> None:
    monkeypatch.delenv("GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("REVIEW_GITHUB_PERSONAL_ACCESS_TOKEN", raising=False)

    class NoTokenSettings:
        github_mcp_enabled = True
        github_personal_access_token = None
        github_mcp_cwd = None
        github_mcp_args = ["docker_mcp/github-mcp/server.py"]
        github_mcp_command = "python"
        github_mcp_timeout_seconds = 30

    result = _github_mcp_preflight(NoTokenSettings())

    assert result["status"] == "missing_token"
    assert result["tool_discovery_available"] is False
    assert result["token_present"] is False


def test_github_mcp_preflight_reports_missing_server_file() -> None:
    result = _github_mcp_preflight(
        Settings(
            github_mcp_enabled=True,
            github_personal_access_token="token",
            github_mcp_args=["missing/server.py"],
        )
    )

    assert result["status"] == "server_file_missing"
    assert result["tool_discovery_available"] is False


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
            "c1:check:review-logic:check:1:1": {
                "file_snippets": {"src/app.py": "def handle(): ..."},
                "file_contents_full": {"src/app.py": "def handle():\n    return 1\n"},
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
    assert record["summary"]["focused_result_path_count"] == 1
    assert record["summary"]["candidate_path_count"] == 1
    paths = {item["path"]: item for item in record["paths"]}
    assert paths["src/app.py"]["reason_state"] == "dropped_by_cleanup"
    assert paths["src/app.py"]["last_stage"] == "candidate"
    assert paths["src/missed.py"]["reason_state"] == "no_task"
    assert paths["src/missed.py"]["last_stage"] == "none"
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

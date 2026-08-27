from __future__ import annotations

import json

from typing import Any

from src.config import Settings
from src.domain.schemas import (
    BehavioralSpec,
    CandidateFinding,
    ContractQuestion,
    ContractSourceRef,
    FocusedContextRequest,
    FocusedContextResult,
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
    make_review_check_validator_node,
    should_continue_review_check_loop,
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
    _add_review_check_health_warning,
    _coverage_audit_for_pr,
    _effective_reviewer_mode,
    _github_mcp_preflight,
    _load_positive_samples_by_pr,
    _positive_labels_for_pr,
    _review_check_mode_source,
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


def _candidate_contract_proof() -> dict[str, str]:
    return {
        "expected_behavior": "handle returns the declared result on every changed path.",
        "evidence_for_contract": "The declared return contract requires handle to return a result.",
        "counterexample": "Calling handle on the changed path returns None.",
        "rejection_check": "No intentional narrowing or caller guarantee permits None.",
    }


def _contract_backed() -> dict[str, Any]:
    """Executor fields that make a candidate contract-backed."""
    return {
        "contract_status": "contradicted",
        "contract_source": ContractSourceRef(
            kind="schema",
            ref="src/app.py:1",
            note="The declared return contract requires handle to return a result.",
        ),
    }


def _contract_supported(kind: str = "schema", ref: str = "src/app.py:1") -> dict[str, Any]:
    """Executor fields that make a no_finding contract-backed."""
    return {
        "contract_status": "supported",
        "contract_source": ContractSourceRef(kind=kind, ref=ref, note="declared contract"),  # type: ignore[arg-type]
    }


def test_review_check_executor_schema_excludes_internal_gate_lifecycle() -> None:
    schema = json.dumps(ReviewCheckExecutorOutput.model_json_schema())

    assert "gate_decision" not in schema
    assert "gate_reason" not in schema
    assert '"suppressed"' not in schema


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


def test_executor_mechanic_only_suppression_is_contract_missing() -> None:
    check = _check(
        lens="api_compatibility",
        changed_code_anchor="parse_variant",
        owned_contract_scope="parse_variant:mode selection:representation",
        affected_invariant="mode selection preserves the intended representation",
        expected_behavior="parse_variant preserves the intended representation for each mode.",
        required_evidence=[
            "changed parse_variant implementation",
        ],
        report_criteria=["A reachable mode selects only part of the intended representation."],
        suppress_criteria=["Concrete contract evidence shows this mode intentionally selects that representation."],
    )
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:3"],
        suppressing_evidence=[
            "parse_variant mode selection representation produced tuple records; the implementation selected item[0] and returned joined selected values."
        ],
        answer_scope="exact",
        suppression_basis=(
            "parse_variant mode selection representation produced tuple records; the implementation selected item[0] and returned joined selected values."
        ),
    )

    normalized, warnings = normalize_executor_results(
        state=_state(),
        task=_task(),
        slot=_state()["metadata"]["critique_pipeline"]["by_task"]["review-logic"],
        checks=[check],
        results=[result],
        git_diff="",
        check_budget_remaining=lambda _state, _check: True,
        evidence_requirements_for_check=lambda item: list(item.required_evidence),
        compiled_check_is_source_local=lambda _check: False,
    )

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:contract_missing" in normalized[0].warnings
    assert normalized[0].missing_evidence == []
    assert any("contract_missing" in item for item in warnings)


def test_executor_focused_context_no_hits_blocks_exact_suppression() -> None:
    check = _check()
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="no_finding",
        evidence_refs=["src/app.py:1"],
        suppressing_evidence=["All changed paths return the declared result."],
        answer_scope="exact",
        suppression_basis="All changed paths return the declared result.",
    )
    state = _state(
        metadata={
            **_state()["metadata"],
            "focused_context": {
                "diagnostics": [
                    {
                        "candidate_id": check.check_id,
                        "outcomes": ["no_hits"],
                        "requested_paths": ["src/app.py"],
                        "effective_paths": [],
                    }
                ]
            },
        }
    )

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
    assert "exact_question_mismatch:focused_context_no_hits" in normalized[0].warnings
    assert any("focused_context_no_hits" in item for item in warnings)


def test_review_check_validator_rejects_vague_checks() -> None:
    vague = _check(behavioral_question="Look for security bugs")
    assert "vague_behavioral_question" in validate_review_check(vague)

    valid = _check()
    assert validate_review_check(valid) == []


def test_compiler_prompt_keeps_unbacked_hardening_audit_only() -> None:
    prompt = compiler_support.render_compiler_prompt(
        _state(),
        _task(),
        _state()["metadata"]["critique_pipeline"]["by_task"]["review-logic"],
    )

    assert "Reject generic hardening and optimization checks" in prompt
    assert "audit-only" in prompt
    assert "repository docs, tests, callers, prior behavior" in prompt
    assert "Name the contract source for every check" in prompt


def test_compiler_normalization_does_not_keyword_demote_hardening_claims() -> None:
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

    assert normalized[0].audit_only is False


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

    out = make_review_check_executor_node()(_state(review_checks=[_check()]))  # type: ignore[arg-type]

    prompt = fake.prompts[0]
    assert "compact contract packet" in prompt
    assert "It is acceptable to omit an undecidable check" in prompt
    assert "Check Contract Packets" in prompt
    assert "Validated Checks JSON" not in prompt
    assert "directly addresses the check's report criteria" in prompt
    assert "Judge the exact behavioral claim" in prompt
    assert "nearby reassuring code" in prompt
    assert "Set `contract_status` on every result" in prompt
    assert "missing_contract_source" in prompt
    context_presence = out["metadata"]["review_checks"]["by_task"]["review-logic"]["executor_context_presence"]
    assert context_presence["direct_context"] is True
    assert context_presence["task_evidence_file_count"] == 1
    assert context_presence["mental_model_excerpt"] is True
    assert context_presence["review_kb_excerpt"] is True


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
    assert "checks_per_selected_lens" in task_meta
    assert out["token_usage"] == 7
    assert any(record["node"] == "review_check_compiler" for record in out["llm_trace"])


def test_review_check_compiler_allows_focused_retrieval_when_contract_source_unknown(monkeypatch) -> None:
    output = ReviewCheckCompilerOutput(
        summary="compiled",
        checks=[
            _check(
                check_id="review-logic:check:shape",
                lens="data_shape_consistency",
                changed_code_anchor="extract_records",
                behavioral_question="Does extract_records preserve each selected field when joining output?",
                affected_invariant="selection and aggregation preserve field cardinality",
                expected_behavior="extract_records preserves the intended field representation.",
                required_evidence=["changed extract_records implementation"],
                suppress_criteria=["The join preserves every intended selected field."],
                report_criteria=["A reachable path drops a field during selection or aggregation."],
                allowed_retrieval=["task_evidence"],
            )
        ],
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_compiler_node()(_state())  # type: ignore[arg-type]

    compiled = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiled_checks"]
    check = compiled[0]
    assert not any("contract-justification" in item for item in check["required_evidence"])
    assert check["contract_source"] is None
    assert "focused_context" in check["allowed_retrieval"]


def test_review_check_context_planner_requests_contract_source_named_by_compiler() -> None:
    check = _check(
        check_id="review-logic:check:shape",
        lens="data_shape_consistency",
        changed_code_anchor="extract_records",
        behavioral_question="Does extract_records preserve each selected field when joining output?",
        affected_invariant="selection and aggregation preserve field cardinality",
        expected_behavior="extract_records preserves the intended field representation.",
        required_evidence=[
            "changed extract_records implementation",
            "record schema declaration that fixes the selected field representation",
        ],
        suppress_criteria=["The join preserves every intended selected field."],
        report_criteria=["A reachable path drops a field during selection or aggregation."],
        allowed_retrieval=["task_evidence", "focused_context"],
    )
    state = _state(
        review_checks=[check],
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def extract_records(records):\n    return ','.join(record[0] for record in records)\n",
                        "mental_model_excerpt": "",
                        "review_kb_excerpt": "",
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def extract_records(records):\n    return ','.join(record[0] for record in records)\n"
                            },
                            "files_complete": {"src/app.py": True},
                        },
                    }
                }
            },
        },
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    requests = out["focused_context_requests"]
    assert len(requests) == 1
    assert requests[0].candidate_id == check.check_id
    assert any("record schema declaration" in query for query in requests[0].text_queries)


def test_review_check_context_planner_includes_paths_named_by_executor() -> None:
    check = _check(
        check_id="review-logic:check:shape",
        lens="data_shape_consistency",
        changed_code_anchor="extract_records",
        behavioral_question="Does extract_records preserve the repository record shape?",
        affected_invariant="selection preserves the documented record schema",
        expected_behavior="extract_records preserves the documented record representation.",
        required_evidence=["changed extract_records implementation"],
        allowed_retrieval=["task_evidence", "focused_context"],
        budget=3,
    )
    latest = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id="review-logic",
        decision="unsupported",
        missing_evidence=["record schema convention declared in src/schema.py"],
    )
    state = _state(
        review_checks=[check],
        review_check_results=[latest],
        metadata={
            **_state()["metadata"],
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def extract_records(records):\n    return records[0]\n",
                        "mental_model_excerpt": "",
                        "review_kb_excerpt": "",
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def extract_records(records):\n    return records[0]\n"
                            },
                            "files_complete": {"src/app.py": True},
                        },
                    }
                }
            },
        },
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    requests = out["focused_context_requests"]
    assert len(requests) == 1
    assert requests[0].file_paths[:2] == ["src/app.py", "src/schema.py"]
    assert "contract_context_paths=src/schema.py" in requests[0].reason


def test_review_check_context_planner_ignores_paths_only_mentioned_in_repository_text() -> None:
    check = _check(
        check_id="review-logic:check:shape",
        lens="data_shape_consistency",
        changed_code_anchor="extract_records",
        behavioral_question="Does extract_records preserve the repository record shape?",
        affected_invariant="selection preserves the documented record schema",
        expected_behavior="extract_records preserves the documented record representation.",
        required_evidence=[
            "changed extract_records implementation",
            "contract-justification evidence: schema, caller, repository convention, or representation invariant explaining why the expected behavior is correct",
        ],
        allowed_retrieval=["task_evidence", "focused_context"],
    )
    state = _state(
        review_checks=[check],
        metadata={
            **_state()["metadata"],
            "snapshot_diagnostics": {
                "cross_community_edges": [
                    {
                        "source_file": "src/app.py",
                        "target_file": "src/schema.py",
                        "reason": "schema convention for records",
                    }
                ]
            },
            "critique_pipeline": {
                "by_task": {
                    "review-logic": {
                        "direct_context": "def extract_records(records):\n    return records[0]\n",
                        "mental_model_excerpt": "Record shape is documented in file:src/schema.py.",
                        "review_kb_excerpt": "See comfy/controlnet.py and comfy/cldm/cldm.py for conventions.",
                        "task_evidence": {
                            "file_contents": {
                                "src/app.py": "def extract_records(records):\n    return records[0]\n"
                            },
                            "files_complete": {"src/app.py": True},
                        },
                    }
                }
            },
        },
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    requests = out["focused_context_requests"]
    assert len(requests) == 1
    assert requests[0].file_paths == ["src/app.py"]
    assert "contract_context_paths=" not in requests[0].reason


def test_checks_per_selected_lens_uses_open_metadata() -> None:
    checks = [
        _check(
            check_id="shape",
            owned_contract_scope="lens:shape_cardinality:record-output",
            issue_family="lens:shape_cardinality",
        ),
        _check(
            check_id="mode",
            diff_signal_family="lens:mode_variant_completeness",
        ),
    ]

    counts = compiler_support.checks_per_selected_lens(
        checks,
        ["shape_cardinality", "mode_variant_completeness", "work_amplification"],
    )

    assert counts == {
        "shape_cardinality": 1,
        "mode_variant_completeness": 1,
        "work_amplification": 0,
    }


def test_review_check_compiler_runs_exactly_one_llm_pass(monkeypatch) -> None:
    primary = ReviewCheckCompilerOutput(summary="compiled", checks=[_check()])
    fake = _FakeLLM({"parsed": primary, "raw": _Raw()})
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: fake,
    )
    base = _state()
    state = _state(
        metadata={
            **base["metadata"],
            "review_planner": {"warnings": ["plan_critic_misaligned_after_budget"]},
        }
    )

    out = make_review_check_compiler_node()(state)  # type: ignore[arg-type]

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert "coverage_critic" not in task_meta
    assert "compiler_coverage" in task_meta
    assert len(fake.prompts) == 1


def test_review_check_compiler_records_unchecked_changed_file_without_inventing_check(monkeypatch) -> None:
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
    assert task_meta["compiled_count"] == 1
    assert task_meta["compiler_coverage"]["missed_files"] == ["src/other.py"]
    assert "compiler_coverage_missed_files:1" in task_meta["compiler_warnings"]


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


def test_normalize_compiled_checks_assigns_cross_file_evidence_paths() -> None:
    task = _task().model_copy(
        update={
            "title": "Integration: registry and implementation consistency",
            "description": "Compare registry entries across both changed files.",
            "target_files": ["src/registry.py", "src/app.py"],
        }
    )
    check = _check(
        file_path="src/registry.py",
        behavioral_question="Do registry entries match implementations across both changed files?",
        required_evidence=["registry declarations and implementation signatures"],
    )

    normalized = compiler_support.normalize_compiled_checks(_state(), task, [check])

    assert set(normalized[0].evidence_paths) == {"src/registry.py", "src/app.py"}
    assert normalized[0].evidence_paths[0] == normalized[0].file_path


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
    assert task_meta["compiled_count"] == 10
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


def test_review_check_compiler_reports_uncovered_primary_surfaces_without_inventing_checks(monkeypatch) -> None:
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
    compiled_ids = [check["check_id"] for check in task_meta["compiled_checks"]]
    assert compiled_ids == ["review-logic:surface0-return"]
    coverage = task_meta["compiler_coverage"]
    assert coverage["missing_primary_surface_ids"] == ["surface:s1", "surface:s2", "surface:s3"]
    assert "compiler_coverage_missing_primary_surfaces:3" in task_meta["compiler_warnings"]


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

    floor = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage"]
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

    floor = out["metadata"]["review_checks"]["by_task"][task.id]["compiler_coverage"]
    compiled = out["metadata"]["review_checks"]["by_task"][task.id]["compiled_checks"]
    assert [row["surface"] for row in floor["ranked_obligations"]] == [
        "first_surface",
        "second_surface",
    ]
    assert compiled[0]["changed_code_anchor"] == "first_surface"


def test_review_check_compiler_prompt_caps_stage_context() -> None:
    task = _task()
    state = _state(task_registry={task.id: task})
    slot = {
        "direct_context": "D" * 14_000 + "DIRECT_CONTEXT_TAIL",
        "mental_model_excerpt": "M" * 3_000 + "MENTAL_MODEL_TAIL",
        "review_kb_excerpt": "K" * 3_000 + "REVIEW_KB_TAIL",
        "coverage_obligations": [],
    }

    prompt = compiler_support.render_compiler_prompt(state, task, slot)  # type: ignore[arg-type]

    assert "DIRECT_CONTEXT_TAIL" not in prompt
    assert "MENTAL_MODEL_TAIL" not in prompt
    assert "REVIEW_KB_TAIL" not in prompt
    assert len(prompt) < 40_000


def test_review_check_compiler_reports_uncovered_obligations_ranked_by_relevance(monkeypatch) -> None:
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

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert [check["check_id"] for check in task_meta["compiled_checks"]] == ["review-logic:check:1"]
    uncovered = task_meta["compiler_coverage"]["uncovered_obligations"]
    assert [row["surface"] for row in uncovered] == ["RegexExtract", "StringSubstring"]


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


def test_compiler_lens_selection_uses_contract_question_text(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:refresh",
        name="RefreshState.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=10,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="refresh state",
        surfaces=[surface],
        contract_questions=[
            ContractQuestion(
                owner="RefreshState.execute",
                surface_id=surface.surface_id,
                dimension="lifecycle_state_ordering",
                expected_behavior="RefreshState.execute consumes the current cached state after async refresh.",
                contract_evidence="The owner name and caller contract require fresh state.",
                trigger_variant="stale cached snapshot after await",
                operation="capture cached state, await refresh, consume state",
                breach_question="Can the changed path use a stale captured state after async refresh?",
                direct_suppressor="The code reloads state after the await before consumption.",
                source_confidence=0.9,
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def execute():\n    pass\n",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )
    slot = state["metadata"]["critique_pipeline"]["by_task"][task.id]

    diagnostics = compiler_support.compiler_lens_selection_diagnostics(
        task,
        slot,
        state=state,  # type: ignore[arg-type]
        settings=settings,
    )

    assert "time_state_freshness" in diagnostics["selected_keys"]


def test_compiler_prompt_renders_lens_metadata_and_provenance_instruction(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:emit",
        name="EmitRecord.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=12,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="emit records",
        surfaces=[surface],
        contract_questions=[
            ContractQuestion(
                owner="EmitRecord.execute",
                surface_id=surface.surface_id,
                dimension="data_preservation_cardinality",
                expected_behavior="EmitRecord.execute preserves every field in each emitted record.",
                contract_evidence="The output schema names a full record payload.",
                trigger_variant="multi-field emitted record",
                operation="select record fields and serialize output",
                breach_question="Can the changed path serialize only part of each record?",
                source_confidence=0.9,
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "critique_pipeline": {
                "by_task": {
                    task.id: {
                        "direct_context": "def execute(records):\n    return records\n",
                        "coverage_obligations": [],
                    }
                }
            },
        },
    )
    slot = state["metadata"]["critique_pipeline"]["by_task"][task.id]

    prompt = compiler_support.render_compiler_prompt(
        state,
        task,
        slot,
        settings=settings,
    )  # type: ignore[arg-type]

    assert "## Selected Contract Lens Metadata" in prompt
    assert '"key": "shape_cardinality"' in prompt
    assert "One selected lens may produce multiple checks" in prompt
    assert "Preserve lens-card provenance" in prompt


def test_contract_question_value_flow_detects_generic_selection_language(tmp_path) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    surface = ReviewSurface(
        surface_id="surface:emit",
        name="EmitRecord.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=12,
        confidence=0.95,
    )
    spec = BehavioralSpec(
        intent_summary="emit records",
        surfaces=[surface],
        contract_questions=[
            ContractQuestion(
                owner="EmitRecord.execute",
                surface_id=surface.surface_id,
                dimension="other",
                expected_behavior="EmitRecord.execute preserves every emitted record field.",
                contract_evidence="The surrounding API consumes full record payloads.",
                trigger_variant="multi-field record",
                operation="select record fields and join emitted output",
                breach_question="Can the changed path return only part of each record?",
                source_confidence=0.9,
            )
        ],
    )
    ref, _ = BehavioralSpecStore(settings).write("r1", spec)
    task = _task().model_copy(update={"surface_ids": [surface.surface_id]})
    state = _state(
        behavioral_spec_ref=ref,
        task_registry={task.id: task},
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
        },
    )

    checks = compiler_support.checks_from_contract_questions(
        state,
        task,
        settings=settings,
    )  # type: ignore[arg-type]

    assert len(checks) == 1
    required = " ".join(checks[0].required_evidence)
    suppress = " ".join(checks[0].suppress_criteria)
    assert "produced value shape before the operation" in required
    assert "selected or transformed value shape at the operation" in required
    assert "returned, consumed, joined, or serialized value shape after the operation" in required
    assert "same action contract" in suppress


def test_coverage_obligation_operation_markers_prevent_nearby_check_match() -> None:
    check = _check(
        lens="data_shape_consistency",
        behavioral_question="Does handle preserve the declared output shape?",
        affected_invariant="structured output shape",
        required_evidence=["changed handle implementation"],
        report_criteria=["The changed handler returns the wrong outer shape."],
    )
    obligation = {
        "file_path": "src/app.py",
        "surface": "handle",
        "dimension": "structured output shape",
        "evidence": "handler emits structured records",
        "operation_markers": ["select nested record fields"],
    }

    assert compiler_support.check_covers_obligation(check, obligation) is False

    exact = check.model_copy(
        update={
            "owned_contract_scope": "handle:select nested record fields",
            "required_evidence": ["changed handle implementation", "select nested record fields"],
        }
    )
    assert compiler_support.check_covers_obligation(exact, obligation) is True


def test_review_check_compiler_keeps_llm_checks_and_reports_uncovered_obligations(monkeypatch) -> None:
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

    task_meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    coverage = task_meta["compiler_coverage"]
    assert task_meta["compiled_count"] == 9
    assert coverage["trimmed_check_ids"] == []
    assert len(coverage["uncovered_obligations"]) == 3
    assert not any(":coverage:" in check["check_id"] for check in task_meta["compiled_checks"])


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
    assert task_meta["compiled_count"] == 10
    assert task_meta["compiler_coverage"]["max_checks"] == 10
    assert task_meta["compiler_coverage"]["adaptive_cap_reason"] == "eligible_non_audit_over_base_cap"
    assert "review-logic:beta:1" in compiled_ids
    assert "review-logic:alpha:16" in task_meta["compiler_coverage"]["trimmed_check_ids"]


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
    floor = task_meta["compiler_coverage"]
    assert task_meta["compiled_count"] == 10
    assert floor["max_checks"] == 10
    assert floor["adaptive_cap_reason"] == "many_primary_owners"
    assert "review-logic:owner4:tuple-cardinality" in compiled_ids
    assert "review-logic:owner4:join-none" in compiled_ids
    assert "review-logic:owner4:group-index" in compiled_ids
    assert "review-logic:owner4:aggregation" in compiled_ids
    assert "Owner4.execute" in floor["owner_fair_cap"]["selected_checks_by_primary_owner"]


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

    coverage = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage"]
    assert [row["dimension"] for row in coverage["uncovered_obligations"]] == ["branch exhaustiveness"]


def test_review_check_compiler_records_omitted_file_without_inventing_check(monkeypatch) -> None:
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
    assert not any(check["check_id"].startswith("review-logic:omitted-file:") for check in compiled)
    assert task_meta["compiler_coverage"]["evidence_omitted_files"] == ["src/other.py"]


def test_review_check_compiler_does_not_expand_omitted_changed_surfaces(monkeypatch) -> None:
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
    assert not any(check["check_id"].startswith("review-logic:omitted-surface:") for check in compiled)
    floor = out["metadata"]["review_checks"]["by_task"]["review-logic"]["compiler_coverage"]
    assert floor["evidence_omitted_files"] == ["src/other.py"]


def test_review_check_compiler_keeps_real_cap_when_evidence_was_omitted(monkeypatch) -> None:
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
    floor = task_meta["compiler_coverage"]
    assert task_meta["compiled_count"] <= floor["max_checks"]
    assert not any(check_id.startswith("review-logic:omitted-file:") for check_id in compiled_ids)
    assert floor["evidence_omitted_files"] == ["src/other.py"]


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


def test_review_check_validator_reports_file_outside_task_targets() -> None:
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
    reasons = out["invalid_review_checks"][0].reasons
    assert "file_not_in_task_targets" in reasons
    assert "anchor_not_in_changed_code" not in reasons


def test_review_check_validator_accepts_ledger_proven_changed_surface_anchor() -> None:
    surface = ReviewSurface(
        surface_id="surface:serialize",
        name="serialize",
        kind="function",
        file_path="src/backend/base/langflow/serialization/serialization.py",
        line_start=1,
        line_end=100,
        confidence=0.95,
    )
    task = _task().model_copy(
        update={
            "target_files": [surface.file_path],
            "surface_ids": [surface.surface_id],
        }
    )
    check = _check(
        file_path=surface.file_path,
        line_start=1,
        line_end=100,
        changed_code_anchor="serialize",
        surface_ids=[surface.surface_id],
    )
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/backend/base/langflow/serialization/serialization.py "
            "b/src/backend/base/langflow/serialization/serialization.py\n"
            "+++ b/src/backend/base/langflow/serialization/serialization.py\n"
            "@@\n+SERIALIZERS = {}\n"
        ),
        metadata={
            **_state()["metadata"],
            "mental_model": {"surface_ledger": [surface.model_dump(mode="json")]},
            "review_checks": {
                "by_task": {
                    task.id: {
                        "compiled_checks": [check.model_dump(mode="json")]
                    }
                }
            },
        },
    )

    out = make_review_check_validator_node()(state)  # type: ignore[arg-type]

    assert [item.check_id for item in out["review_checks"]] == [check.check_id]
    assert out["invalid_review_checks"] == []


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


def test_review_check_context_planner_requests_all_declared_evidence_paths() -> None:
    task = _task().model_copy(
        update={"target_files": ["src/app.py", "src/registry.py"]}
    )
    check = _check(
        evidence_paths=["src/app.py", "src/registry.py"],
        required_evidence=["caller authorization guard"],
    )
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/app.py b/src/app.py\n+++ b/src/app.py\n@@\n+def handle():\n"
            "diff --git a/src/registry.py b/src/registry.py\n+++ b/src/registry.py\n@@\n+REGISTRY = {}\n"
        ),
        review_checks=[check],
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    assert out["focused_context_requests"][0].file_paths == ["src/app.py", "src/registry.py"]


def test_review_check_context_planner_keeps_evidence_paths_before_contract_context() -> None:
    check = _check(
        file_path="src/backend/base/langflow/serialization/serialization.py",
        evidence_paths=["src/backend/base/langflow/serialization/serialization.py"],
        required_evidence=[
            "contract justification from repository convention in src/backend/base/langflow/api/v1/chat.py"
        ],
    )
    task = _task().model_copy(update={"target_files": [check.file_path]})
    state = _state(
        task_registry={task.id: task},
        git_diff=(
            "diff --git a/src/backend/base/langflow/serialization/serialization.py "
            "b/src/backend/base/langflow/serialization/serialization.py\n"
            "+++ b/src/backend/base/langflow/serialization/serialization.py\n"
            "@@\n+def serialize(value):\n+    return value\n"
        ),
        review_checks=[check],
        metadata={
            **_state()["metadata"],
            "mental_model": {
                "contract_boundaries": [
                    {"file_path": f"src/backend/base/langflow/api/v1/context_{idx}.py"}
                    for idx in range(8)
                ]
            },
        },
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    req = out["focused_context_requests"][0]
    assert req.file_paths[0] == "src/backend/base/langflow/serialization/serialization.py"


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


def test_review_check_loop_stops_when_no_context_retry_path() -> None:
    check = _check(
        allowed_retrieval=["task_evidence"],
        required_evidence=["full implementation of handle"],
        budget=1,
    )
    state = _state(
        review_checks=[check],
        review_check_results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id=check.patch_task_id,
                decision="unsupported",
                missing_evidence=["full implementation of handle"],
            )
        ],
    )

    assert should_continue_review_check_loop(state) is False  # type: ignore[arg-type]
    assert critique_pipeline._route_after_review_check_executor(state) == "review_check_evidence_gate"


def test_review_check_loop_stops_when_planner_has_only_duplicate_request() -> None:
    check = _check(required_evidence=["caller authorization guard"], budget=2)
    request = FocusedContextRequest(
        request_id="check:review-logic:check:1:1",
        candidate_id=check.check_id,
        requested_by_specialty="logic",
        file_paths=["src/app.py"],
        symbol_queries=["handle"],
        text_queries=["src/app.py handle caller authorization guard"],
        reason="existing equivalent request",
    )
    state = _state(
        review_checks=[check],
        focused_context_requests=[request],
        review_check_results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id=check.patch_task_id,
                decision="unsupported",
                missing_evidence=["caller authorization guard"],
            )
        ],
    )

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    assert out["focused_context_requests"] == []
    assert should_continue_review_check_loop(state) is False  # type: ignore[arg-type]


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


def test_review_check_executor_marks_no_retry_path_budget_exhausted(monkeypatch) -> None:
    check = _check(
        allowed_retrieval=["task_evidence"],
        required_evidence=["full implementation of handle"],
        budget=1,
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id=check.patch_task_id,
                decision="unsupported",
                missing_evidence=["full implementation of handle"],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _FakeLLM({"parsed": output, "raw": _Raw()}),
    )

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    result = out["review_check_results"][0]
    assert result.decision == "budget_exhausted"
    assert "review_check_no_retry_path" in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert f"executor_no_retry_path_budget_exhausted:{check.check_id}" in meta["executor_warnings"]


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
    assert result.missing_evidence == []
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
                **_contract_supported(),
                evidence_refs=["src/app.py:1"],
                suppressing_evidence=[
                    "Each field and slot is preserved in order during aggregation; no entries are dropped."
                ],
                answer_scope="exact",
                suppression_basis=(
                    "The produced aggregation fields and slots are selected in order and the returned value "
                    "contains every field without truncation."
                ),
                evidence_for_contract=(
                    "The output schema documents each aggregation field and slot as part of the returned value."
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
    assert result.decision == "unsupported"
    assert any("exact_question_mismatch" in warning for warning in result.warnings)
    assert len(fake.prompts) == 1


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
                **_contract_supported(),
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
                **_contract_supported(),
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
                **_contract_supported(),
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
                **_contract_supported(),
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


def test_review_check_executor_prompt_includes_cross_file_source_evidence() -> None:
    from src.orchestration.nodes.application.review_checks import _render_executor_prompt

    task = _task().model_copy(
        update={"target_files": ["src/app.py", "src/registry.py"]}
    )
    check = _check(evidence_paths=["src/app.py", "src/registry.py"])
    slot = {
        "direct_context": "primary rendered context",
        "task_evidence": {
            "file_contents": {
                "src/app.py": "def handle():\n    return 'ok'\n",
                "src/registry.py": "REGISTRY = {'handle': handle}\n",
            }
        },
    }

    prompt = _render_executor_prompt(_state(), task, [check], slot)

    assert "Repository Source Evidence By Check" in prompt
    assert "REGISTRY = {'handle': handle}" in prompt


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
        **_candidate_contract_proof(),
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
                **_contract_backed(),
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


def test_review_check_executor_does_not_reconsider_terminal_same_batch_results(monkeypatch) -> None:
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
        recommendation="Return the declared result.",
        **_candidate_contract_proof(),
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
        recommendation="Return the declared result.",
        **_candidate_contract_proof(),
    )
    fake = _FakeLLM(
        [
            {"parsed": ReviewCheckExecutorOutput(
                results=[
                    ReviewCheckResult(
                        check_id=check1.check_id,
                        patch_task_id="review-logic",
                        decision="candidate",
                        **_contract_backed(),
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
                        **_contract_backed(),
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
    assert decisions == {check1.check_id: "candidate", check2.check_id: "unsupported"}
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert len(fake.prompts) == 1
    assert not any(key.startswith("executor_same_batch_continuation") for key in meta)


def test_review_check_executor_does_not_make_followup_call_for_unsupported_peer(monkeypatch) -> None:
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
        recommendation="Return the declared result.",
        **_candidate_contract_proof(),
    )
    fake = _FakeLLM(
        [
            {"parsed": ReviewCheckExecutorOutput(
                results=[
                    ReviewCheckResult(
                        check_id=check1.check_id,
                        patch_task_id="review-logic",
                        decision="candidate",
                        **_contract_backed(),
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
                        **_contract_backed(),
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
    assert len(fake.prompts) == 1
    assert not any(key.startswith("executor_same_batch_continuation") for key in meta)


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


def test_review_check_executor_splits_oversized_batch_before_llm(monkeypatch) -> None:
    checks = [_check(check_id=f"review-logic:check:{idx}") for idx in range(1, 4)]
    state = _state(review_checks=checks)
    slot = state["metadata"]["critique_pipeline"]["by_task"]["review-logic"]
    slot["direct_context"] = "def handle():\n    return None\n" + ("# expanded evidence\n" * 4000)
    prompts: list[str] = []
    actions: list[Any] = [
        ReviewCheckExecutorOutput(
            results=[
                ReviewCheckResult(
                    check_id=check.check_id,
                    patch_task_id="review-logic",
                    decision="unsupported",
                    missing_evidence=["declared return contract"],
                )
            ]
        )
        for check in checks
    ]
    monkeypatch.setattr(
        "src.orchestration.nodes.application.review_checks.Models.worker",
        lambda *_args, **_kwargs: _SequencedLLM(actions, prompts),
    )

    out = make_review_check_executor_node()(state)  # type: ignore[arg-type]

    assert len(prompts) == 3
    assert checks[0].check_id in prompts[0]
    assert checks[1].check_id not in prompts[0]
    assert checks[2].check_id not in prompts[0]
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_batch_count"] == 3
    assert meta["executor_oversized_batch_splits"] == [
        {
            "batch_index": 1,
            "check_ids": [check.check_id for check in checks],
            "prompt_chars": meta["executor_oversized_batch_splits"][0]["prompt_chars"],
        }
    ]
    assert meta["executor_oversized_batch_splits"][0]["prompt_chars"] > meta[
        "executor_max_multi_check_prompt_chars"
    ]
    assert "executor_oversized_batch_split:1" in meta["executor_warnings"]
    assert meta["executor_length_limit_retry_count"] == 0


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
        **_candidate_contract_proof(),
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
                    **_contract_backed(),
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
        **_candidate_contract_proof(),
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="candidate",
                **_contract_backed(),
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


def test_review_check_executor_downgrades_missing_candidate_payload_even_with_evidence(monkeypatch) -> None:
    check = _check()
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="candidate",
                **_contract_backed(),
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
    assert result.decision == "unsupported"
    assert result.expected_behavior == check.expected_behavior
    assert result.candidate is None
    assert "executor_candidate_missing_payload" in result.warnings
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_candidate_missing_payload_check_ids"] == [check.check_id]
    assert "executor_candidate_payload_synthesized_check_ids" not in meta


def test_review_check_executor_downgrades_missing_contract_proof(monkeypatch) -> None:
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
                **_contract_backed(),
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
    assert meta["executor_candidate_missing_contract_proof_count"] == 1
    assert meta["executor_candidate_missing_contract_proof"] == [
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
    assert result.decision == "unsupported"
    assert result.claim_digest
    assert result.expected_behavior == check.expected_behavior
    assert result.candidate is None
    assert result.missing_evidence == [
        "candidate.expected_behavior",
        "candidate.evidence_for_contract",
        "candidate.counterexample",
        "candidate.rejection_check",
    ]
    assert meta["executor_claim_digests"] == {}
    assert meta["executor_claim_digest_count"] == 0


def test_review_check_executor_downgrades_missing_candidate_payload_without_evidence(monkeypatch) -> None:
    check = _check()
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
                patch_task_id="review-logic",
                decision="candidate",
                **_contract_backed(),
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


def test_review_check_executor_accepts_schema_exact_fallback_suppression(monkeypatch) -> None:
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
                **_contract_supported(),
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


def test_review_check_evidence_gate_uses_latest_result_and_records_lifecycle_in_metadata() -> None:
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
            **_contract_backed(),
            evidence_refs=["src/app.py:1"],
            reportable_reason="The changed path returns None.",
            candidate=good,
        ),
        ReviewCheckResult(
            check_id="review-logic:check:1",
            patch_task_id="review-logic",
            decision="candidate",
            **_contract_backed(),
            evidence_refs=["src/app.py:1"],
            reportable_reason="Maybe wrong.",
            candidate=speculative,
        ),
    ]

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=results)
    )  # type: ignore[arg-type]

    assert out["candidate_findings"] == []
    assert "review_check_results" not in out
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["promoted_count"] == 0
    assert gate["dropped_count"] == 1
    assert gate["reason_counts"] == {"speculative_or_uncertain_claim": 1}
    assert gate["candidate_lifecycle"][speculative.candidate_id]["decision"] == "dropped"
    assert out["task_status_by_id"] == {"review-logic": "completed"}


def test_review_check_evidence_gate_prefers_promotable_check_for_duplicate_check_id() -> None:
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
            review_checks=[_check(audit_only=False), _check(audit_only=True)],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="candidate",
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns None.",
                    candidate=candidate,
                )
            ],
        )
    )  # type: ignore[arg-type]

    assert [cand.candidate_id for cand in out["candidate_findings"]] == [candidate.candidate_id]
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "evidence_gate_passed"
    assert "review_check_results" not in out


def test_review_check_evidence_gate_latest_duplicate_lifecycle_wins() -> None:
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
    weak_duplicate = candidate.model_copy(update={"content": "This might be wrong."})

    out = make_review_check_evidence_gate_node()(
        _state(
            review_checks=[_check()],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="candidate",
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns None.",
                    candidate=candidate,
                ),
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="candidate",
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="Maybe wrong.",
                    candidate=weak_duplicate,
                ),
            ],
        )
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert out["candidate_findings"] == []
    assert gate["candidate_lifecycle"][candidate.candidate_id]["decision"] == "dropped"
    assert gate["reason_counts"] == {"speculative_or_uncertain_claim": 1}


def test_review_check_evidence_gate_promotes_concrete_audit_only_candidates() -> None:
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
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns None.",
                    candidate=candidate,
                )
            ],
        )
    )  # type: ignore[arg-type]

    assert len(out["candidate_findings"]) == 1
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "evidence_gate_passed_audit_only"


def test_review_check_evidence_gate_still_drops_weak_audit_only_candidates() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="Consider validating inputs more carefully.",
        claim_type="defect",
        expected_behavior="Inputs should be validated.",
        failure_mode="",
        evidence_summary="",
        evidence_for_contract="",
        counterexample="",
        rejection_check="",
        recommendation="",
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
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="Maybe validate.",
                    candidate=candidate,
                )
            ],
        )
    )  # type: ignore[arg-type]

    assert out["candidate_findings"] == []
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    lifecycle = gate["candidate_lifecycle"][candidate.candidate_id]
    assert lifecycle["decision"] == "dropped"
    assert lifecycle["reason"] != "audit_only_check_not_promotable"


def test_review_check_evidence_gate_drops_partial_context_uncertainty() -> None:
    candidate = CandidateFinding(
        candidate_id="review-logic:check:1:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=2,
        content="The changed handler may return a list on the requested path.",
        claim_type="defect",
        expected_behavior="handle returns the declared result on every changed path.",
        failure_mode="handler returns the wrong shape",
        evidence_summary="Focused context could not retrieve the requested path evidence.",
        evidence_for_contract="The declared return contract requires a value.",
        counterexample="The requested path returns a list instead of the declared result.",
        rejection_check="The focused context request for this exact check produced no source hits.",
        recommendation="Inspect the full source before reporting.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    out = make_review_check_evidence_gate_node()(
        _state(
            metadata={
                **_state()["metadata"],
                "focused_context": {
                    "diagnostics": [
                        {
                            "candidate_id": "review-logic:check:1",
                            "reason": "no_hits",
                            "requested_paths": ["src/app.py"],
                            "effective_paths": [],
                        }
                    ]
                },
            },
            review_checks=[_check()],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:check:1",
                    patch_task_id="review-logic",
                    decision="candidate",
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="Focused context returned no hits for the requested exact path.",
                    candidate=candidate,
                )
            ],
        )
    )  # type: ignore[arg-type]

    assert out["candidate_findings"] == []
    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "focused_context_no_hits"


def test_review_check_evidence_gate_does_not_warn_when_no_candidate_decisions_exist() -> None:
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
    assert gate["candidate_decision_count"] == 0
    assert gate["gate_expected_count"] == 0
    assert gate["gate_evaluated_count"] == 0
    assert "no_executor_candidates_for_valid_checks" not in gate["health_warnings"]
    assert "evidence_gate_not_exercised" not in gate["health_warnings"]


def test_review_check_evidence_gate_warns_when_candidate_decisions_are_not_evaluated() -> None:
    candidate = CandidateFinding(
        candidate_id="unknown:candidate",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=1,
        content="Issue",
        expected_behavior="handle returns a value.",
        evidence_for_contract="RETURN_TYPES declares a value.",
        counterexample="handle returns None.",
        rejection_check="No suppressor is present.",
        failure_mode="missing return",
        evidence_summary="Source shows missing return.",
        recommendation="Return the value.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    out = make_review_check_evidence_gate_node()(
        _state(
            review_checks=[_check()],
            review_check_results=[
                ReviewCheckResult(
                    check_id="review-logic:unknown",
                    patch_task_id="review-logic",
                    decision="candidate",
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns None.",
                    candidate=candidate,
                )
            ],
        )
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_decision_count"] == 1
    assert gate["gate_expected_count"] == 1
    assert gate["gate_evaluated_count"] == 0
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
                    **_contract_backed(),
                    evidence_refs=["src/app.py:1"],
                    reportable_reason="The changed path returns the wrong result.",
                )
            ],
        )
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"]["review-logic:check:1"] == {
        "decision": "dropped",
        "check_id": "review-logic:check:1",
        "reason": "candidate_payload_missing",
    }
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
        **_contract_backed(),
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path returns None.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "missing_counterexample"
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
        **_contract_backed(),
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path returns None.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "missing_expected_behavior"
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
        **_contract_backed(),
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path does not cache.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "generic_expected_behavior_not_contract"


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
        **_contract_backed(),
        evidence_refs=["src/app.py:1"],
        reportable_reason="The changed path returns None.",
        candidate=candidate,
    )

    out = make_review_check_evidence_gate_node()(
        _state(review_checks=[_check()], review_check_results=[result])
    )  # type: ignore[arg-type]

    gate = out["metadata"]["review_checks"]["by_task"]["review-logic"]["gate"]
    assert gate["candidate_lifecycle"][candidate.candidate_id]["reason"] == "weak_contract_proof"
    assert gate["contract_proof"]["weak_candidate_ids"] == [candidate.candidate_id]
    assert gate["contract_proof"]["weak_count"] == 1


def test_check_mode_routing(monkeypatch) -> None:
    monkeypatch.setattr(critique_pipeline, "get_settings", lambda: Settings())
    assert critique_pipeline._route_after_mental_model_enricher({}) == "review_check_compiler"
    assert critique_pipeline._route_after_review_check_validator({}) == "review_check_context_planner"

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


def test_effective_reviewer_mode_default_reaches_executor() -> None:
    mode = _effective_reviewer_mode(Settings(), snapshot_resume=False)

    assert mode["check_mode"] == "enforced"
    assert mode["check_nodes_reached"] == {
        "compiler": True,
        "validator": True,
        "executor": True,
        "evidence_gate": True,
    }


def test_review_check_mode_source_precedence(monkeypatch) -> None:
    monkeypatch.delenv("REVIEW_REVIEWER_CHECK_MODE", raising=False)
    assert _review_check_mode_source({}) == "settings_default"

    monkeypatch.setenv("REVIEW_REVIEWER_CHECK_MODE", "log_only")
    assert _review_check_mode_source({}) == "env"
    assert _review_check_mode_source({"review_check_mode": "enforced"}) == "cli"


def test_add_review_check_health_warning_deduplicates() -> None:
    row = {"review_check_health_warnings": '["known_positive_no_draft_candidate"]'}

    _add_review_check_health_warning(row, "positive_eval_check_mode_log_only")
    _add_review_check_health_warning(row, "positive_eval_check_mode_log_only")

    assert json.loads(row["review_check_health_warnings"]) == [
        "known_positive_no_draft_candidate",
        "positive_eval_check_mode_log_only",
    ]


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
                **_contract_backed(),
                candidate=CandidateFinding(
                    candidate_id="c1",
                    patch_task_id="review-logic",
                    file_path="src/app.py",
                    line_start=1,
                    line_end=1,
                    content="Issue",
                ),
            ),
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


def test_review_check_metrics_reports_tooling_degradation_health() -> None:
    result = {
        "metadata": {
            "mcp_preflight": {
                "status": "tool_discovery_error",
                "missing_required_tools": ["get_commits_for_path"],
                "fallback_context_sufficient": False,
            },
            "semantic_phase2": {
                "global_summary_llm_status": "failed",
                "global_summary_degraded": True,
            },
            "review_planner": {
                "warnings": ["plan_critic_misaligned_after_budget"],
            },
            "focused_context": {
                "diagnostics": [
                    {
                        "candidate_id": "review-logic:check:1",
                        "reason": "no_hits",
                    },
                    {
                        "candidate_id": "review-logic:check:2",
                        "reason": "path_mismatch",
                    },
                ],
            },
            "review_checks": {
                "health_warnings": [
                    "evidence_gate_not_exercised",
                ],
            },
        },
        "review_checks": [],
        "invalid_review_checks": [],
        "review_check_results": [],
        "candidate_findings": [],
    }

    metrics = _review_check_metrics(result)
    warnings = set(json.loads(metrics["review_check_health_warnings"]))

    assert "mcp_preflight_tool_discovery_error" in warnings
    assert "mcp_preflight_missing_required_tools" in warnings
    assert "global_summary_degraded" in warnings
    assert "plan_critic_misaligned_after_budget" in warnings
    assert "focused_context_no_hits" in warnings
    assert "focused_context_path_mismatch" in warnings
    assert "evidence_gate_not_exercised" in warnings


def test_review_check_metrics_no_candidate_warning_is_run_level() -> None:
    result = {
        "metadata": {"review_checks": {"by_task": {}}},
        "review_checks": [_check()],
        "invalid_review_checks": [],
        "review_check_results": [
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="unsupported",
            )
        ],
        "candidate_findings": [
            CandidateFinding(
                candidate_id="c1",
                patch_task_id="review-logic",
                file_path="src/app.py",
                line_start=1,
                line_end=1,
                content="Issue",
            )
        ],
    }

    metrics = _review_check_metrics(result)

    assert "no_executor_candidates_for_valid_checks" not in json.loads(
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

    assert discovery_error["status"] == "tool_discovery_error"
    assert discovery_error["tool_discovery_available"] is False
    assert discovery_error["missing_required_tools"] == ["get_commits_for_path"]
    assert "TaskGroup" in discovery_error["error"]


def test_github_mcp_preflight_missing_token_does_not_spawn_server(monkeypatch) -> None:
    class MissingTokenSettings:
        github_mcp_enabled = True
        github_personal_access_token = ""

    class ExplodingClient:
        def __init__(self, **_kwargs: object) -> None:
            raise AssertionError("MCPClient should not be constructed without a token")

    monkeypatch.setattr(aacr, "MCPClient", ExplodingClient)

    out = _github_mcp_preflight(MissingTokenSettings())

    assert out["status"] == "disabled_missing_token"
    assert out["reason"] == "github_token_missing"
    assert out["missing_required_tools"] == ["get_commits_for_path"]


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
                "file_snippets": {"repository_kb_context": "Relevant source in (src/app.py): def handle(): ..."},
                "search_hits": {},
            }
        },
        "review_check_results": [
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="candidate",
                **_contract_backed(),
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
    assert paths["src/app.py"]["focused_result"] is True
    assert paths["src/app.py"]["executor_decision_counts"] == {"candidate": 1}
    assert paths["src/missed.py"]["reason_state"] == "no_task"
    assert paths["src/missed.py"]["last_stage"] == "none"
    assert record["summary"]["reason_state_counts"] == {
        "dropped_by_cleanup": 1,
        "no_task": 1,
    }
    assert record["summary"]["focused_effective_path_count"] == 1
    assert payload["summary"]["positive_path_count"] == 2


def test_coverage_audit_distinguishes_inventory_and_post_gate_misses() -> None:
    raw = {
        "metadata": {
            "changed_files": ["src/app.py"],
            "review_checks": {
                "by_task": {
                    "review-logic": {
                        "compiled_checks": [_check().model_dump(mode="json")],
                        "gate": {
                            "candidate_lifecycle": {
                                "c1": {
                                    "decision": "passed",
                                    "check_id": "review-logic:check:1",
                                    "reason": "evidence_gate_passed",
                                }
                            }
                        },
                    }
                }
            },
        },
        "review_checks": [_check().model_dump(mode="json")],
        "review_check_results": [
            ReviewCheckResult(
                check_id="review-logic:check:1",
                patch_task_id="review-logic",
                decision="candidate",
                **_contract_backed(),
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
        labels=[{"path": "src/app.py"}, {"path": "src/missing.py"}],
    )

    paths = {item["path"]: item for item in record["paths"]}
    assert paths["src/app.py"]["reason_state"] == "passed_gate_dropped_afterward"
    assert paths["src/missing.py"]["reason_state"] == "absent_from_changed_inventory"
    assert paths["src/missing.py"]["changed_inventory_present"] is False


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


def test_review_check_validator_rejects_anchor_outside_changed_code() -> None:
    valid = _check()
    invalid = _check(
        check_id="review-logic:check:bad-anchor",
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


def test_length_limit_failure_metrics_count_tokens_and_time() -> None:
    result = {
        "llm_trace": [
            {"event": "llm_request", "node": "review_check_executor"},
            {
                "event": "llm_error",
                "node": "review_check_executor",
                "error_type": "LengthFinishReasonError",
                "elapsed_ms": 291486,
                "error": (
                    "Could not parse response content as the length limit was reached - "
                    "CompletionUsage(completion_tokens=20480, prompt_tokens=20210, total_tokens=40690, "
                    "completion_tokens_details=None, prompt_tokens_details=None)"
                ),
            },
            {
                "event": "llm_error",
                "node": "review_evidence_triage",
                "error_type": "TimeoutError",
                "elapsed_ms": 5,
                "error": "timeout",
            },
            {
                "event": "llm_error",
                "node": "review_evidence_triage",
                "error_type": "LengthFinishReasonError",
                "elapsed_ms": 1000,
                "error": "CompletionUsage(completion_tokens=12288, prompt_tokens=3174, total_tokens=15462)",
            },
        ]
    }

    metrics = aacr._length_limit_failure_metrics(result)

    assert metrics == {
        "length_limit_failure_count": 2,
        "length_limit_failure_tokens": 56152,
        "length_limit_failure_ms": 292486,
    }


# ---------------------------------------------------------------------------
# Change 3: contract-evidence representation
# ---------------------------------------------------------------------------


def _normalize_one(
    check: ReviewCheck,
    result: ReviewCheckResult,
    *,
    budget_remaining: bool = True,
) -> tuple[list[ReviewCheckResult], list[str]]:
    state = _state()
    return normalize_executor_results(
        state=state,
        task=_task(),
        slot=state["metadata"]["critique_pipeline"]["by_task"]["review-logic"],
        checks=[check],
        results=[result],
        git_diff=state["git_diff"],
        check_budget_remaining=lambda _state, _check: budget_remaining,
        evidence_requirements_for_check=lambda item: list(item.required_evidence),
        compiled_check_is_source_local=lambda _check: False,
    )


def _supported_no_finding(check: ReviewCheck, **overrides: Any) -> ReviewCheckResult:
    data: dict[str, Any] = {
        "check_id": check.check_id,
        "patch_task_id": check.patch_task_id,
        "decision": "no_finding",
        "evidence_refs": ["src/app.py:2"],
        "suppressing_evidence": ["Both changed branches return the declared tuple (src/app.py:2)."],
        "answer_scope": "exact",
        "suppression_basis": "Both changed branches return the declared tuple; no path falls through.",
        "contract_status": "supported",
    }
    data.update(overrides)
    return ReviewCheckResult(**data)


def _candidate_result(check: ReviewCheck, **overrides: Any) -> ReviewCheckResult:
    candidate = CandidateFinding(
        candidate_id=f"{check.check_id}:candidate",
        patch_task_id=check.patch_task_id,
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
        **_candidate_contract_proof(),
    )
    data: dict[str, Any] = {
        "check_id": check.check_id,
        "patch_task_id": check.patch_task_id,
        "decision": "candidate",
        "evidence_refs": ["src/app.py:1"],
        "reportable_reason": "The changed path returns None.",
        "candidate": candidate,
    }
    data.update(overrides)
    return ReviewCheckResult(**data)


def test_no_finding_stands_with_each_supported_contract_source_kind() -> None:
    fixtures = [
        ("caller", "focused_context:check:review-logic:check:1:1", "server.py unpacks the returned tuple"),
        ("schema", "src/app.py:1", "RETURN_TYPES declares one output"),
        ("convention", "docs/CONTRIBUTING.md:12", "handlers return tuples"),
        ("old_behavior", "diff", "the replaced branch returned (result,)"),
        ("framework", "framework/base_node.py:40", "the framework unpacks handler output as a tuple"),
    ]
    for kind, ref, note in fixtures:
        check = _check()
        result = _supported_no_finding(
            check,
            contract_source=ContractSourceRef(kind=kind, ref=ref, note=note),  # type: ignore[arg-type]
        )

        normalized, warnings = _normalize_one(check, result)

        assert normalized[0].decision == "no_finding", kind
        assert normalized[0].contract_source is not None
        assert normalized[0].contract_source.kind == kind
        assert not any("exact_question_mismatch" in item for item in warnings), kind


def test_no_finding_with_missing_contract_names_the_source_to_retrieve() -> None:
    check = _check(required_evidence=["changed handle implementation", "declared return contract"])
    result = _supported_no_finding(
        check,
        contract_status="missing",
        missing_contract_source="RETURN_TYPES declaration for handle in src/schema.py",
    )

    normalized, warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:contract_missing" in normalized[0].warnings
    assert normalized[0].missing_evidence == ["RETURN_TYPES declaration for handle in src/schema.py"]
    assert any("contract_missing" in item for item in warnings)


def test_no_finding_with_unstated_contract_gets_no_fallback_retrieval_text() -> None:
    check = _check(required_evidence=["changed handle implementation", "declared return contract"])
    result = _supported_no_finding(check, contract_status="missing")

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:contract_missing" in normalized[0].warnings
    assert normalized[0].missing_evidence == []


def test_no_finding_supported_without_source_reference_is_unsupported() -> None:
    check = _check()
    result = _supported_no_finding(check, contract_source=ContractSourceRef(kind="schema", ref="   "))

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:contract_source_unreferenced" in normalized[0].warnings


def test_no_finding_with_contradicted_contract_is_unsupported() -> None:
    check = _check()
    result = _supported_no_finding(
        check,
        contract_status="contradicted",
        contract_source=ContractSourceRef(kind="schema", ref="src/app.py:1"),
    )

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:contract_contradicted" in normalized[0].warnings


def test_no_finding_with_missing_contract_and_no_budget_is_budget_exhausted() -> None:
    check = _check()
    result = _supported_no_finding(check, contract_status="missing", missing_contract_source="caller in src/server.py")

    normalized, _warnings = _normalize_one(check, result, budget_remaining=False)

    assert normalized[0].decision == "budget_exhausted"
    assert "exact_question_mismatch:contract_missing" in normalized[0].warnings
    assert "review_check_budget_exhausted" in normalized[0].warnings


def test_audit_only_no_finding_still_requires_contract_support() -> None:
    check = _check(audit_only=True)
    result = _supported_no_finding(check, contract_status="missing")

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "exact_question_mismatch:contract_missing" in normalized[0].warnings


def test_candidate_stands_with_contradicted_contract_source() -> None:
    check = _check()
    result = _candidate_result(
        check,
        contract_status="contradicted",
        contract_source=ContractSourceRef(kind="old_behavior", ref="diff", note="the replaced branch returned (result,)"),
    )

    normalized, warnings = _normalize_one(check, result)

    assert normalized[0].decision == "candidate"
    assert normalized[0].candidate is not None
    assert not any("contract_unbacked" in item for item in warnings)


def test_candidate_with_missing_contract_is_unsupported_and_retrieves_the_source() -> None:
    check = _check()
    result = _candidate_result(
        check,
        contract_status="missing",
        missing_contract_source="framework validation of STRING inputs in framework/execution.py",
    )

    normalized, warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert normalized[0].candidate is None
    assert "candidate_contract_unbacked:contract_missing" in normalized[0].warnings
    assert normalized[0].missing_evidence == ["framework validation of STRING inputs in framework/execution.py"]
    assert any(item.startswith("executor_candidate_contract_unbacked:") for item in warnings)


def test_candidate_with_supported_contract_is_incoherent_and_unsupported() -> None:
    check = _check()
    result = _candidate_result(
        check,
        contract_status="supported",
        contract_source=ContractSourceRef(kind="schema", ref="src/app.py:1"),
    )

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "candidate_contract_unbacked:contract_supported" in normalized[0].warnings


def test_candidate_contradicted_without_source_reference_is_unsupported() -> None:
    check = _check()
    result = _candidate_result(check, contract_status="contradicted")

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert "candidate_contract_unbacked:contract_source_unreferenced" in normalized[0].warnings


def test_unsupported_result_puts_missing_contract_source_first_in_retrieval_targets() -> None:
    check = _check()
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id=check.patch_task_id,
        decision="unsupported",
        contract_status="missing",
        missing_contract_source="caller of handle in src/server.py",
        missing_evidence=["declared return contract"],
    )

    normalized, _warnings = _normalize_one(check, result)

    assert normalized[0].decision == "unsupported"
    assert normalized[0].missing_evidence == ["caller of handle in src/server.py", "declared return contract"]


def test_unanswered_check_keeps_requirement_hints_for_retry() -> None:
    check = _check()

    normalized, warnings = _normalize_one(check, ReviewCheckResult(check_id="other", patch_task_id="review-logic"))

    assert normalized[0].check_id == check.check_id
    assert normalized[0].decision == "unsupported"
    assert normalized[0].missing_evidence == ["changed handle implementation", "declared return contract"]
    assert "executor_result_unknown_check:other" in warnings


def test_review_check_context_planner_retrieves_missing_contract_source_paths() -> None:
    check = _check(budget=3)
    latest = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id="review-logic",
        decision="unsupported",
        contract_status="missing",
        missing_contract_source="input validation contract in src/validation.py",
        missing_evidence=["input validation contract in src/validation.py", "declared return contract"],
    )
    state = _state(review_checks=[check], review_check_results=[latest])

    out = make_review_check_context_planner_node()(state)  # type: ignore[arg-type]

    requests = out["focused_context_requests"]
    assert len(requests) == 1
    assert "src/validation.py" in requests[0].file_paths
    assert "input validation contract" in requests[0].text_queries[0]


def test_normalize_compiled_checks_keeps_named_contract_source_without_forcing_retrieval() -> None:
    check = _check(
        allowed_retrieval=["task_evidence"],
        contract_source=ContractSourceRef(kind="schema", ref="src/app.py:1", note="RETURN_TYPES declares one output"),
    )

    normalized = compiler_support.normalize_compiled_checks(_state(), _task(), [check])

    assert normalized[0].contract_source is not None
    assert normalized[0].contract_source.kind == "schema"
    assert "focused_context" not in normalized[0].allowed_retrieval


def test_normalize_compiled_checks_allows_retrieval_when_contract_source_unknown() -> None:
    check = _check(allowed_retrieval=["task_evidence"])

    normalized = compiler_support.normalize_compiled_checks(_state(), _task(), [check])

    assert normalized[0].contract_source is None
    assert "focused_context" in normalized[0].allowed_retrieval
    assert not any("contract-justification" in item for item in normalized[0].required_evidence)


def test_contract_question_source_kind_becomes_check_contract_source() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="handle",
        kind="function",
        file_path="src/app.py",
        line_start=1,
        line_end=3,
        confidence=0.9,
    )
    question = ContractQuestion(
        owner="handle",
        surface_id=surface.surface_id,
        dimension="return_output_totality",
        expected_behavior="handle returns the declared tuple on every path.",
        contract_evidence="RETURN_TYPES declares one output.",
        contract_source_kind="schema",
        breach_question="Can a changed path return without the declared tuple?",
        required_evidence=["handle source"],
        source_confidence=0.9,
    )

    check = compiler_support._check_from_contract_question(task=_task(), question=question, surface=surface, index=1)

    assert check.contract_source is not None
    assert check.contract_source.kind == "schema"
    assert check.contract_source.ref == "src/app.py:1-3"
    assert "RETURN_TYPES declares one output." in check.contract_source.note

    inferred = question.model_copy(update={"contract_source_kind": None})
    assert (
        compiler_support._check_from_contract_question(task=_task(), question=inferred, surface=surface, index=2).contract_source
        is None
    )


def test_review_check_executor_packet_carries_compiler_contract_source(monkeypatch) -> None:
    check = _check(
        contract_source=ContractSourceRef(kind="caller", ref="src/server.py:20", note="server unpacks the returned tuple"),
    )
    output = ReviewCheckExecutorOutput(
        results=[
            ReviewCheckResult(
                check_id=check.check_id,
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

    out = make_review_check_executor_node()(_state(review_checks=[check]))  # type: ignore[arg-type]

    prompt = fake.prompts[0]
    assert '"caller"' in prompt
    assert "src/server.py:20" in prompt
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_contract_status_counts"] == {"missing": 1}
    assert meta["executor_candidate_contract_unbacked"] == []


def test_review_check_executor_records_contract_unbacked_candidates(monkeypatch) -> None:
    check = _check()
    output = ReviewCheckExecutorOutput(
        results=[
            _candidate_result(
                check,
                contract_status="missing",
                missing_contract_source="caller contract for handle in src/server.py",
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
    assert result.missing_evidence == ["caller contract for handle in src/server.py"]
    meta = out["metadata"]["review_checks"]["by_task"]["review-logic"]
    assert meta["executor_candidate_contract_unbacked"] == [{"check_id": check.check_id, "reason": "contract_missing"}]
    assert meta["executor_candidate_contract_unbacked_count"] == 1
    assert meta["executor_candidate_ids"] == []


def test_compact_check_result_includes_contract_evidence_fields() -> None:
    from src.orchestration.nodes.application.review_adjudicator import _compact_check_result

    check = _check(contract_source=ContractSourceRef(kind="schema", ref="src/app.py:1", note="declares one output"))
    result = ReviewCheckResult(
        check_id=check.check_id,
        patch_task_id="review-logic",
        decision="unsupported",
        contract_status="missing",
        missing_contract_source="caller in src/server.py",
    )

    payload = _compact_check_result(result, check)

    assert payload["contract_status"] == "missing"
    assert payload["contract_source"] is None
    assert payload["missing_contract_source"] == "caller in src/server.py"
    assert payload["originating_check"]["contract_source"]["kind"] == "schema"


def test_executor_schema_exposes_contract_status_and_source() -> None:
    schema = ReviewCheckExecutorOutput.model_json_schema()

    assert "ContractSourceRef" in schema["$defs"]
    status = schema["$defs"]["ReviewCheckResult"]["properties"]["contract_status"]
    assert status["enum"] == ["supported", "missing", "contradicted"]
    assert "missing_contract_source" in schema["$defs"]["ReviewCheckResult"]["properties"]


def test_contract_evidence_prompts_name_the_structured_fields() -> None:
    from src.orchestration.prompts.renderer import load_reviewer_prompt

    executor = load_reviewer_prompt("review_check_executor.md")
    compiler = load_reviewer_prompt("review_check_compiler.md")
    owner_questions = load_reviewer_prompt("mental_model/owner_contract_questions.md")

    assert "`contract_status`" in executor
    assert "`missing_contract_source`" in executor
    assert "contract-justification evidence" not in executor
    assert "include the check's `owned_contract_scope`" not in executor
    assert "`contract_source`" in compiler
    assert "Do not invent a source" in compiler
    assert "`contract_source_kind`" in owner_questions

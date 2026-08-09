"""Tests for mental-model orchestration helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.config import Settings
from src.domain.schemas import BehavioralSpec, ContractQuestion, ExplorationSnapshot, ReviewSurface, StructuralTopologySummary
from src.domain.state import GraphState
from src.infrastructure.behavioral_spec_store import BehavioralSpecStore
from src.orchestration.prompts.ledger_formatter import format_exploration_ledger_for_prompt
from src.orchestration.routing.send_payload import payload_for_send
from src.tools.mental_model_tools import query_mental_model
from src.orchestration.nodes.mental_model import _cap_fallback_questions_per_owner, _normalize_contract_questions
from src.orchestration.nodes.mandate_patch_node import (
    MandatePatchOutput,
    _apply_patch_to_spec,
    make_mandate_patch_node,
)
from src.orchestration.context.surface_ledger import build_contract_questions_from_ledger


def test_behavioral_spec_store_roundtrip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    store = BehavioralSpecStore(settings)
    spec = BehavioralSpec(
        intent_summary="Add login",
        behavioral_expectations="Users can sign in.",
        contract_boundaries="Session API stable.",
        historical_precedents="Prior PRs used JWT.",
        risk_hypotheses="Hypothesis: token expiry edge cases.",
        reviewer_guidance="Stay structural.",
    )
    ref, _abs = store.write("run-abc", spec)
    assert ref.startswith("file:")
    loaded = store.read(ref)
    assert loaded.intent_summary == "Add login"


def test_payload_for_send_is_shallow_copy() -> None:
    state: GraphState = {  # type: ignore[assignment]
        "run_id": "r1",
        "repo_path": "/repo",
        "git_diff": "diff",
        "exploration_ledger": [{"kind": "mental_model_query", "dedupe_key": "k1"}],
        "token_usage": 17,
        "node_history": ["parent"],
    }
    p = payload_for_send(state, current_task_id="t1")
    assert p["current_task_id"] == "t1"
    assert p["exploration_ledger"] == state["exploration_ledger"]
    assert p["token_usage"] == 0
    assert p["node_history"] == []
    p["extra"] = True
    assert "extra" not in state


def test_payload_for_send_allows_explicit_additive_overrides() -> None:
    state: GraphState = {"token_usage": 17, "node_history": ["parent"]}  # type: ignore[assignment]

    payload = payload_for_send(state, token_usage=3, node_history=["branch"])

    assert payload["token_usage"] == 3
    assert payload["node_history"] == ["branch"]


def test_format_exploration_ledger_caps_and_prioritizes_task() -> None:
    ledger = [
        {"kind": "mental_model_query", "dedupe_key": "a", "query_preview": "q1", "answer_preview": "a1", "task_id": "t2"},
        {"kind": "mental_model_query", "dedupe_key": "b", "query_preview": "t1 files", "answer_preview": "a2", "task_id": "t1"},
    ]
    text, stats = format_exploration_ledger_for_prompt(
        ledger,
        task_id="t1",
        target_files=["src/x.py"],
        max_entries=1,
        max_chars=500,
    )
    assert "t1" in text or "files" in text
    assert stats.rendered <= 1


def test_query_mental_model_dedupe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(snapshot_base_path=str(tmp_path))
    store = BehavioralSpecStore(settings)
    spec = BehavioralSpec(intent_summary="Hello")
    ref, _ = store.write("run-dedupe", spec)
    state: GraphState = {  # type: ignore[assignment]
        "run_id": "run-dedupe",
        "repo_path": str(tmp_path),
        "git_diff": "",
        "behavioral_spec_ref": ref,
        "exploration_ledger": [],
        "metadata": {},
    }
    monkeypatch.setattr("src.tools.mental_model_tools.get_settings", lambda: settings)
    r1 = query_mental_model(state=state, query="What is intent?", caller="t1")
    assert not r1["skipped"]
    state2 = {**state, "exploration_ledger": list(r1["exploration_ledger"])}
    r2 = query_mental_model(state=state2, query="What is intent?", caller="t1")
    assert r2["skipped"] and r2["skip_reason"] == "dedupe_cache_hit"


def test_contract_question_normalization_dedupes_by_owner_dimension_trigger() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="Handle.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    question = ContractQuestion(
        owner="Handle.execute",
        surface_id=surface.surface_id,
        dimension="return_output_totality",
        expected_behavior="Handle.execute returns its declared output.",
        contract_evidence="Declared return shape.",
        trigger_variant="fallback branch",
        operation="dispatch",
        breach_question="Can the fallback branch exit without the declared output?",
        direct_suppressor="Caller evidence proves the fallback branch cannot occur.",
    )

    normalized = _normalize_contract_questions([question, question], surfaces=[surface])

    assert len(normalized) == 1
    assert normalized[0].question_id.startswith("cq:")
    assert normalized[0].required_evidence


def test_contract_question_normalization_keeps_central_high_confidence_questions() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="Handle.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    questions = [
        ContractQuestion(
            owner="Handle.execute",
            surface_id=surface.surface_id,
            dimension="other",
            expected_behavior=f"Handle.execute keeps incidental behavior {index}.",
            contract_evidence="",
            trigger_variant=f"incidental {index}",
            operation="misc",
            breach_question=f"Can incidental behavior {index} drift?",
            direct_suppressor="n/a",
            source_confidence=0.1,
        )
        for index in range(4)
    ]
    central = ContractQuestion(
        owner="Handle.execute",
        surface_id=surface.surface_id,
        dimension="return_output_totality",
        expected_behavior="Handle.execute returns the declared output for every mode.",
        contract_evidence="RETURN_TYPES declares one output.",
        trigger_variant="declared modes",
        operation="dispatch",
        breach_question="Can any dispatch branch exit without the declared output?",
        direct_suppressor="none",
        source_confidence=0.95,
    )

    normalized = _normalize_contract_questions([*questions, central], surfaces=[surface])

    assert len(normalized) == 4
    assert any(item.breach_question == central.breach_question for item in normalized)
    kept = next(item for item in normalized if item.breach_question == central.breach_question)
    assert kept.direct_suppressor == ""
    assert kept.required_evidence[0] == "RETURN_TYPES declares one output."


def test_contract_question_normalization_keeps_concrete_operation_question_under_owner_cap() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=20,
        confidence=0.95,
    )
    generic = [
        ContractQuestion(
            owner="RecordExtract.execute",
            surface_id=surface.surface_id,
            dimension=dimension,  # type: ignore[arg-type]
            expected_behavior=f"RecordExtract.execute satisfies {dimension}.",
            contract_evidence="Declared node contract.",
            trigger_variant=f"generic {dimension}",
            operation=operation,
            breach_question=f"Can generic {dimension} fail?",
            source_confidence=0.35,
        )
        for dimension, operation in [
            ("return_output_totality", "execute return contract"),
            ("variant_completeness", "variant dispatch"),
            ("data_preservation_cardinality", "aggregation or extraction"),
            ("serialization_type_closure", "output serialization"),
        ]
    ]
    concrete = ContractQuestion(
        owner="RecordExtract.execute",
        surface_id=surface.surface_id,
        dimension="data_preservation_cardinality",
        expected_behavior="RecordExtract.execute preserves projected payload elements.",
        contract_evidence="The operation names an extraction contract.",
        trigger_variant="multi-record extraction",
        operation="element projection from produced records",
        breach_question="Can projection select only part of the produced record payload?",
        source_confidence=0.9,
    )

    normalized = _normalize_contract_questions([*generic, concrete], surfaces=[surface])

    assert len(normalized) == 4
    assert any(question.operation == concrete.operation for question in normalized)
    assert not any(question.operation == "aggregation or extraction" for question in normalized)


def test_contract_question_fallback_uses_existing_schema_without_new_fields() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract-execute",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=10,
        line_end=30,
        confidence=0.95,
    )

    questions = build_contract_questions_from_ledger(
        [surface],
        risk_hypotheses="Preserve every grouped record field during extraction.",
    )

    dumped = [question.model_dump() for question in questions]
    assert dumped
    assert all("preferred_specialty" not in item for item in dumped)
    assert {question.dimension for question in questions} >= {
        "return_output_totality",
        "data_preservation_cardinality",
        "serialization_type_closure",
    }


def test_contract_question_fallback_does_not_leak_global_operation_terms_to_unrelated_owner() -> None:
    concat = ReviewSurface(
        surface_id="surface:concat-execute",
        name="StringConcatenate.execute",
        kind="method",
        file_path="src/nodes_string.py",
        line_start=10,
        line_end=15,
        confidence=0.95,
    )
    extract = ReviewSurface(
        surface_id="surface:extract-execute",
        name="RegexExtract.execute",
        kind="method",
        file_path="src/nodes_string.py",
        line_start=40,
        line_end=90,
        confidence=0.95,
    )

    questions = build_contract_questions_from_ledger(
        [concat, extract],
        risk_hypotheses="RegexExtract uses re.findall, m[0], groups, and join serialization.",
    )

    by_owner: dict[str, set[str]] = {}
    for question in questions:
        by_owner.setdefault(question.owner, set()).add(question.dimension)
    assert "data_preservation_cardinality" not in by_owner.get("StringConcatenate.execute", set())
    assert "serialization_type_closure" not in by_owner.get("StringConcatenate.execute", set())
    assert "data_preservation_cardinality" in by_owner.get("RegexExtract.execute", set())


def test_contract_question_fallback_skips_dimensions_with_existing_questions() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract-execute",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=10,
        line_end=30,
        confidence=0.95,
    )
    existing = ContractQuestion(
        owner="RecordExtract.execute",
        surface_id=surface.surface_id,
        dimension="data_preservation_cardinality",
        expected_behavior="RecordExtract.execute preserves projected payload elements.",
        contract_evidence="The operation names an extraction contract.",
        trigger_variant="multi-record extraction",
        operation="element projection from produced records",
        breach_question="Can projection select only part of the produced record payload?",
    )

    questions = build_contract_questions_from_ledger(
        [surface],
        risk_hypotheses="Preserve every grouped record field during extraction.",
        existing_questions=[existing],
    )

    assert all(question.dimension != "data_preservation_cardinality" for question in questions)
    assert any(question.dimension == "serialization_type_closure" for question in questions)


def test_partial_llm_success_caps_fallback_questions_per_owner() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract-execute",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=10,
        line_end=30,
        confidence=0.95,
    )
    fallback = build_contract_questions_from_ledger(
        [surface],
        risk_hypotheses="Preserve every grouped record field during extraction.",
    )

    capped = _cap_fallback_questions_per_owner(fallback, enabled=True)

    assert len(capped) == 1
    assert capped[0].owner == "RecordExtract.execute"


def test_mandate_patch_adds_contract_questions_when_prior_has_none() -> None:
    surface = ReviewSurface(
        surface_id="surface:handle",
        name="Handle.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=5,
        confidence=0.95,
    )
    patch = MandatePatchOutput(
        behavioral_expectations="Handle returns its declared output.",
        contract_questions=[
            ContractQuestion(
                owner="Handle.execute",
                surface_id=surface.surface_id,
                dimension="return_output_totality",
                expected_behavior="Handle.execute returns its declared output.",
                contract_evidence="Declared output shape.",
                trigger_variant="fallback path",
                operation="return",
                breach_question="Can fallback path exit without the declared output?",
            )
        ],
    )

    spec = _apply_patch_to_spec(
        prior=BehavioralSpec(intent_summary="x", surfaces=[surface]),
        intent_summary="x",
        patch=patch,
        changed_files=["src/app.py"],
        surfaces=[surface],
        surface_invariants=[],
    )

    assert any(
        question.breach_question == "Can fallback path exit without the declared output?"
        for question in spec.contract_questions
    )


def test_mandate_patch_fallback_respects_existing_question_dimensions() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=20,
        confidence=0.95,
    )
    patch = MandatePatchOutput(
        behavioral_expectations="RecordExtract returns its declared output.",
        risk_hypotheses="Preserve every grouped record field during extraction.",
        contract_questions=[
            ContractQuestion(
                owner="RecordExtract.execute",
                surface_id=surface.surface_id,
                dimension="return_output_totality",
                expected_behavior="RecordExtract.execute returns its declared output.",
                contract_evidence="Declared output shape.",
                trigger_variant="fallback path",
                operation="return",
                breach_question="Can fallback path exit without the declared output?",
                source_confidence=0.2,
            )
        ],
    )

    spec = _apply_patch_to_spec(
        prior=BehavioralSpec(intent_summary="x", surfaces=[surface]),
        intent_summary="x",
        patch=patch,
        changed_files=["src/app.py"],
        surfaces=[surface],
        surface_invariants=[],
    )

    return_questions = [
        question
        for question in spec.contract_questions
        if question.owner == "RecordExtract.execute"
        and question.dimension == "return_output_totality"
    ]
    assert len(return_questions) == 1


def test_mandate_patch_high_confidence_question_allows_missing_dimension_fallback() -> None:
    surface = ReviewSurface(
        surface_id="surface:extract",
        name="RecordExtract.execute",
        kind="method",
        file_path="src/app.py",
        line_start=1,
        line_end=20,
        confidence=0.95,
    )
    patch = MandatePatchOutput(
        behavioral_expectations="RecordExtract projects records into a serialized output.",
        risk_hypotheses="Preserve every grouped record field during extraction.",
        contract_questions=[
            ContractQuestion(
                owner="RecordExtract.execute",
                surface_id=surface.surface_id,
                dimension="data_preservation_cardinality",
                expected_behavior=(
                    "RecordExtract.execute projects produced records into selected payload values "
                    "for the serialized node output."
                ),
                contract_evidence="RecordExtract.execute produces and projects records.",
                trigger_variant="multi-record extraction",
                operation="record projection",
                breach_question="Can projection select only part of each produced record payload?",
                source_confidence=0.9,
            )
        ],
    )

    spec = _apply_patch_to_spec(
        prior=BehavioralSpec(intent_summary="x", surfaces=[surface]),
        intent_summary="x",
        patch=patch,
        changed_files=["src/app.py"],
        surfaces=[surface],
        surface_invariants=[],
    )

    dimensions = {question.dimension for question in spec.contract_questions}
    assert "data_preservation_cardinality" in dimensions
    assert "return_output_totality" in dimensions
    assert sum(
        1
        for question in spec.contract_questions
        if question.dimension == "data_preservation_cardinality"
    ) == 1


class _FakePatchScaffoldProvider:
    def __init__(self, files: dict[str, str]) -> None:
        self.files = files
        self.started = False

    def get_sandbox(self, state: GraphState) -> object:
        self.started = True
        return object()

    def read_full_file(self, file_path: str, *, max_chars: int | None = None) -> str:
        text = self.files.get(file_path, "")
        return text[:max_chars] if max_chars is not None else text


def test_mandate_patch_uses_owner_contract_scaffold_from_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = ReviewSurface(
        surface_id="surface:execute",
        name="Example.execute",
        kind="method",
        file_path="pkg/node.py",
        line_start=2,
        line_end=3,
        source="diff",
        confidence=0.95,
    )
    provider = _FakePatchScaffoldProvider(
        {"pkg/node.py": "class Example:\n    def execute(self):\n        return ('ok',)\n"}
    )
    captured: dict[str, str] = {}

    def capture_render(role_prompt_path: str, sections: dict[str, str]) -> str:
        captured.update(sections)
        raise RuntimeError("stop after prompt assembly")

    monkeypatch.setattr(
        "src.orchestration.nodes.mandate_patch_node.render_reviewer_prompt",
        capture_render,
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.mandate_patch_node.synthesize_owner_isolated_contract_questions",
        lambda *args, **kwargs: ([], {}, 0, [], []),
    )

    settings = Settings(snapshot_base_path=str(tmp_path))
    node = make_mandate_patch_node(settings=settings, context_provider=provider)
    state: GraphState = {  # type: ignore[assignment]
        "run_id": "run-patch-scaffold",
        "repo_path": "https://example.test/repo.git",
        "git_diff": (
            "diff --git a/pkg/node.py b/pkg/node.py\n"
            "+++ b/pkg/node.py\n"
            "@@ -2,1 +2,1 @@\n"
            "+        return ('ok',)\n"
        ),
        "metadata": {
            "mental_model": {
                "intent_extractor": {"intent_summary": "Add Example node."},
                "surface_ledger": [surface.model_dump(mode="json")],
            }
        },
    }

    out = node(state)
    scaffold_meta = out["metadata"]["mental_model"]["owner_contract_scaffold"]

    assert provider.started
    assert "owner_contract_scaffold" in captured
    assert "return ('ok',)" in captured["owner_contract_scaffold"]
    assert scaffold_meta["primary_owner_count"] == 1
    assert scaffold_meta["owner_snippets"][0]["source_status"] == "sandbox_provider"
    assert out["metadata"]["mental_model"]["changed_file_inventory_diagnostics"]["status"] == "ok"
    assert out["metadata"]["mental_model"]["mandate_patch"]["owner_contract_scaffold_status"] == "ok"
    assert out["metadata"]["mental_model"]["mandate_patch"]["owners_without_authored_action_questions"]


def test_mandate_patch_writes_owner_agent_questions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    surface = ReviewSurface(
        surface_id="surface:execute",
        name="Example.execute",
        kind="method",
        file_path="pkg/node.py",
        line_start=2,
        line_end=5,
        source="diff",
        confidence=0.95,
    )
    question = ContractQuestion(
        owner="Example.execute",
        surface_id=surface.surface_id,
        dimension="return_output_totality",
        expected_behavior="Example.execute returns the declared output tuple for the node API.",
        contract_evidence="The owner scaffold shows execute returns the node output.",
        trigger_variant="normal execution",
        operation="node output return",
        breach_question="Can normal execution fail to return the declared node output tuple?",
        source_confidence=0.9,
    )

    monkeypatch.setattr(
        "src.orchestration.nodes.mandate_patch_node.render_reviewer_prompt",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("skip broad prompt")),
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.mandate_patch_node.synthesize_owner_isolated_contract_questions",
        lambda *args, **kwargs: (
            [question],
            {"status": "ok", "partition_count": 1},
            0,
            [],
            [],
        ),
    )

    settings = Settings(snapshot_base_path=str(tmp_path))
    node = make_mandate_patch_node(settings=settings, context_provider=None)
    state: GraphState = {  # type: ignore[assignment]
        "run_id": "run-owner-agent",
        "repo_path": str(tmp_path),
        "git_diff": (
            "diff --git a/pkg/node.py b/pkg/node.py\n"
            "+++ b/pkg/node.py\n"
            "@@ -2,1 +2,1 @@\n"
            "+    def execute(self):\n"
        ),
        "metadata": {
            "mental_model": {
                "intent_extractor": {"intent_summary": "Add Example node."},
                "surface_ledger": [surface.model_dump(mode="json")],
            }
        },
    }

    out = node(state)
    spec = BehavioralSpecStore(settings).read(out["behavioral_spec_ref"])

    assert any(item.breach_question == question.breach_question for item in spec.contract_questions)
    assert out["metadata"]["mental_model"]["owner_contract_agentic"]["partition_count"] == 1


def test_snapshot_pin_skips_write_when_snapshot_source_loaded() -> None:
    import sys
    import types
    from unittest.mock import MagicMock

    if "redis" not in sys.modules:
        _redis_stub = types.ModuleType("redis")
        _redis_stub.Redis = MagicMock  # type: ignore[attr-defined]
        sys.modules["redis"] = _redis_stub

    from src.config import Settings
    from src.orchestration.nodes.exploration.snapshot_pin import make_snapshot_pin_node

    settings = Settings()
    writer = MagicMock()
    ptr = MagicMock()
    node = make_snapshot_pin_node(writer, ptr, settings=settings)
    out = node(
        {
            "run_id": "resume-run",
            "repo_path": "/repo",
            "git_diff": "",
            "snapshot_source": "loaded",
            "snapshot_id": "snap1",
            "snapshot_root": "/snap/root",
            "structural_graph_node_link": {"nodes": [], "edges": []},
            "structural_topology": StructuralTopologySummary(
                algorithm="test",
                community_count=2,
                communities=[],
            ),
            "global_summary": "loaded summary",
            "behavioral_spec_ref": "file:/tmp/spec.json",
            "metadata": {"exploration_snapshot": {"snapshot_id": "old"}},
        }
    )
    writer.write_snapshot.assert_not_called()
    ptr.write_pointer.assert_not_called()
    assert "snapshot_pin:loaded_passthrough" in out["node_history"]
    meta_snap = out["metadata"]["exploration_snapshot"]
    assert meta_snap["snapshot_id"] == "snap1"
    assert meta_snap["metadata"]["behavioral_spec_ref"] == "file:/tmp/spec.json"
    assert out["snapshot_source"] == "loaded"
    assert out["metadata"]["exploration_context_ready"] == {
        "source": "loaded",
        "snapshot_id": "snap1",
        "has_graph": True,
        "has_topology": True,
        "community_count": 2,
        "has_global_summary": True,
    }


def test_snapshot_pin_stamps_live_exploration_context_ready() -> None:
    import sys
    import types
    from unittest.mock import MagicMock

    if "redis" not in sys.modules:
        _redis_stub = types.ModuleType("redis")
        _redis_stub.Redis = MagicMock  # type: ignore[attr-defined]
        sys.modules["redis"] = _redis_stub

    from src.config import Settings
    from src.orchestration.nodes.exploration.snapshot_pin import make_snapshot_pin_node

    snap = ExplorationSnapshot(
        snapshot_id="live-snap",
        run_id="live-run",
        snapshot_root="/snap/live",
        status="exploration_complete",
        community_count=1,
        total_nodes=2,
        total_edges=1,
        unresolved_call_count=0,
        extraction_gap_count=0,
    )
    writer = MagicMock()
    writer.write_snapshot.return_value = (snap, "/snap/live")
    ptr = MagicMock()
    node = make_snapshot_pin_node(writer, ptr, settings=Settings())

    out = node(
        {
            "run_id": "live-run",
            "repo_path": "/repo",
            "git_diff": "",
            "structural_graph_node_link": {"nodes": [{"id": "n"}], "edges": []},
            "structural_topology": StructuralTopologySummary(
                algorithm="test",
                community_count=1,
                communities=[],
            ),
            "community_summaries": [],
            "global_summary": "live summary",
            "metadata": {},
        }
    )

    assert out["snapshot_source"] == "explore"
    assert out["snapshot_id"] == "live-snap"
    assert out["metadata"]["exploration_context_ready"] == {
        "source": "explore",
        "snapshot_id": "live-snap",
        "has_graph": True,
        "has_topology": True,
        "community_count": 1,
        "has_global_summary": True,
    }

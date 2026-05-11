"""Tests for adversarial reviewer graph, context caps, and cleanup."""

from __future__ import annotations

import pytest

from src.config import Settings, get_settings
from src.domain.schemas import (
    CandidateFinding,
    CritiqueRevisionDigest,
    FocusedContextRequest,
    FocusedContextResult,
    ReflectionBatchOutput,
    ReflectionReport,
    ReviewFinding,
    SearchResult,
    ReviewTask,
)
from src.domain.state import merge_graph_metadata
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.nodes.application.reflection import make_adversarial_reflection_node


def test_merge_graph_metadata_deep_merges_parallel_critiquer_shapes() -> None:
    a = {"general_critiquer": {"by_task": {"t1": {"summary": "s1"}}}}
    b = {"general_critiquer": {"by_task": {"t2": {"summary": "s2"}}}}
    merged = merge_graph_metadata(a, b)
    assert merged["general_critiquer"]["by_task"]["t1"]["summary"] == "s1"
    assert merged["general_critiquer"]["by_task"]["t2"]["summary"] == "s2"


def test_critique_subgraph_parent_updates_strips_last_value_channels() -> None:
    """Parallel Send merges must not repeat run_id/repo_path/git_diff from subgraph.invoke()."""
    from src.orchestration.nodes.application.critique_pipeline import _critique_subgraph_parent_updates

    full = {
        "run_id": "r1",
        "repo_path": "/tmp",
        "git_diff": "diff",
        "metadata": {"a": 1},
        "node_history": ["n"],
        "token_usage": 3,
    }
    out = _critique_subgraph_parent_updates(full)
    assert "run_id" not in out
    assert "repo_path" not in out
    assert "git_diff" not in out
    assert out == {"metadata": {"a": 1}, "node_history": ["n"], "token_usage": 3}


def test_merge_graph_metadata_unions_ast_included_files() -> None:
    a = {"ast_included_files": ["src/a.py"]}
    b = {"ast_included_files": ["src/b.py"]}
    merged = merge_graph_metadata(a, b)
    assert merged["ast_included_files"] == ["src/a.py", "src/b.py"]

    dup = merge_graph_metadata(
        {"ast_included_files": ["src/a.py"]},
        {"ast_included_files": ["src\\a.py"]},
    )
    assert dup["ast_included_files"] == ["src/a.py"]


def test_structural_critiquer_context_excerpt_neighbors_and_peers() -> None:
    from src.orchestration.context.review_context import structural_critiquer_context_excerpt

    state = {
        "structural_graph_node_link": {
            "nodes": [
                {"id": "file:a.py", "node_type": "file", "file_path": "a.py", "community_id": 0},
                {"id": "file:b.py", "node_type": "file", "file_path": "b.py", "community_id": 0},
                {
                    "id": "sym:x",
                    "node_type": "symbol",
                    "file_path": "a.py",
                    "symbol_name": "foo",
                    "label": "foo",
                },
            ],
            "edges": [
                {"source": "file:a.py", "target": "sym:x", "edge_type": "defines"},
            ],
        },
        "structural_topology": {
            "algorithm": "test",
            "community_count": 1,
            "communities": [
                {
                    "community_id": 0,
                    "node_ids": ["file:a.py", "file:b.py"],
                    "cohesion": 0.5,
                    "file_count": 2,
                    "symbol_count": 1,
                }
            ],
            "node_to_community": {"file:a.py": 0, "file:b.py": 0, "sym:x": 0},
            "splits_applied": 0,
            "config": {},
        },
    }
    out = structural_critiquer_context_excerpt(state, ["a.py"])  # type: ignore[arg-type]
    assert "Structural context" in out
    assert "a.py" in out
    assert "neighbors (1-hop)" in out
    assert "[defines]" in out
    assert "b.py" in out


def test_entities_for_file_from_structural_graph_maps_symbols_and_imports() -> None:
    from src.orchestration.context.review_context import entities_for_file_from_structural_graph

    state = {
        "structural_graph_node_link": {
            "nodes": [
                {"id": "file:a.py", "node_type": "file", "file_path": "a.py"},
                {
                    "id": "symbol:abc:foo",
                    "node_type": "symbol",
                    "file_path": "a.py",
                    "symbol_name": "foo",
                    "symbol_type": "function",
                    "signature": "def foo():",
                },
                {"id": "module:os", "node_type": "module", "module_name": "os"},
            ],
            "edges": [
                {"source": "file:a.py", "target": "symbol:abc:foo", "edge_type": "defines"},
                {"source": "symbol:abc:foo", "target": "module:os", "edge_type": "imports"},
            ],
        }
    }
    ents = entities_for_file_from_structural_graph(state, "a.py")  # type: ignore[arg-type]
    assert len(ents) == 1
    assert ents[0].name == "foo"
    assert ents[0].type == "function"
    assert "os" in ents[0].dependencies


def test_reviewer_graph_compiles_adversarial_path() -> None:
    pytest.importorskip("langgraph")
    from src.orchestration.reviewer_graph import build_graph

    graph = build_graph()
    assert graph is not None


def test_reviewer_graph_compiles_legacy_workers(monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("langgraph")
    from src.orchestration.reviewer_graph import build_graph

    monkeypatch.setenv("REVIEW_REVIEWER_USE_LEGACY_SPECIALIST_WORKERS", "true")
    get_settings.cache_clear()
    try:
        graph = build_graph()
        assert graph is not None
    finally:
        monkeypatch.delenv("REVIEW_REVIEWER_USE_LEGACY_SPECIALIST_WORKERS", raising=False)
        get_settings.cache_clear()


def test_bounded_fulfiller_respects_file_cap() -> None:
    try:
        from src.orchestration.context.review_context import BoundedReviewContextFulfiller
    except ImportError as exc:
        pytest.skip(f"review context stack unavailable ({exc})")

    calls: dict[str, int] = {"reads": 0}

    class StubProvider:
        def _ensure_started(self, state: dict) -> None:
            return None

        def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
            calls["reads"] += 1
            return f"body-{file_path}"

        def search_bounded(self, query: str, *, max_hits: int, file_paths=None):
            return []

        def ast_entities_for_file(self, file_path: str, **kwargs):
            return [], []

    fulfiller = BoundedReviewContextFulfiller(StubProvider())  # type: ignore[arg-type]
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="c1",
        requested_by_specialty="security",
        file_paths=[f"f{i}.py" for i in range(20)],
        symbol_queries=[],
        text_queries=[],
    )
    state: dict = {"run_id": "t", "metadata": {}}
    result = fulfiller.fulfill(state, req)  # type: ignore[arg-type]
    assert len(result.file_snippets) <= 5
    assert calls["reads"] <= 5


def test_bounded_fulfiller_scopes_searches_to_requested_files() -> None:
    try:
        from src.orchestration.context.review_context import BoundedReviewContextFulfiller
    except ImportError as exc:
        pytest.skip(f"review context stack unavailable ({exc})")

    calls: list[tuple[str, tuple[str, ...] | None]] = []

    class StubProvider:
        def _ensure_started(self, state: dict) -> None:
            return None

        def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
            return ""

        def search_bounded(self, query: str, *, max_hits: int, file_paths=None):
            calls.append((query, tuple(file_paths) if file_paths else None))
            return [
                SearchResult(
                    file_path="middleware/cache_middleware.py",
                    line_number=1,
                    content="fragments",
                    context_lines=["fragments"],
                )
            ]

        def ast_entities_for_file(self, file_path: str, **kwargs):
            return [], []

    fulfiller = BoundedReviewContextFulfiller(StubProvider())  # type: ignore[arg-type]
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="c1",
        requested_by_specialty="general",
        file_paths=["middleware/cache_middleware.py"],
        symbol_queries=["cache_control"],
        text_queries=["fragments"],
    )

    result = fulfiller.fulfill({"run_id": "t", "metadata": {}}, req)  # type: ignore[arg-type]

    assert result.search_hits["fragments"][0].file_path == "middleware/cache_middleware.py"
    assert calls == [
        ("cache_control", ("middleware/cache_middleware.py",)),
        ("fragments", ("middleware/cache_middleware.py",)),
    ]


def test_bounded_fulfiller_skips_ast_when_ast_included_in_metadata() -> None:
    try:
        from src.domain.schemas import CodeEntity
        from src.orchestration.context.review_context import BoundedReviewContextFulfiller
    except ImportError as exc:
        pytest.skip(f"review context stack unavailable ({exc})")

    class StubProvider:
        def _ensure_started(self, state: dict) -> None:
            return None

        def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
            return f"body-{file_path}"

        def search_bounded(self, query: str, *, max_hits: int, file_paths=None):
            return []

        def ast_entities_for_file(self, file_path: str, **kwargs):
            return (
                [
                    CodeEntity(
                        name="n",
                        type="def",
                        signature="()",
                        body="",
                        dependencies=[],
                    )
                ],
                [],
            )

    fulfiller = BoundedReviewContextFulfiller(StubProvider())  # type: ignore[arg-type]
    fp = "middleware/cache_middleware.py"
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id="c1",
        requested_by_specialty="general",
        file_paths=[fp],
        symbol_queries=[],
        text_queries=[],
    )
    state: dict = {"run_id": "t", "metadata": {"ast_included_files": [fp]}}
    result = fulfiller.fulfill(state, req)  # type: ignore[arg-type]
    snippet = result.file_snippets.get(fp, "")
    assert "--- ast entities ---" not in snippet
    assert "body-" in snippet


def test_adversarial_cleanup_promotes_on_unanimous_accept() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Issue",
        claim_type="defect",
        failure_mode="The changed branch returns the wrong value.",
        evidence_summary="The diff shows the branch returning False where True is expected.",
        suspected_category="logic",
        recommendation="Return the expected value for this branch.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty=spec,
            verdict="accept",
            rationale="ok",
        )
        for spec in ("security", "logic", "performance", "general")
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {},
        }
    )
    assert len(out["findings"]) == 1
    assert isinstance(out["findings"][0], ReviewFinding)


def test_adversarial_cleanup_drops_on_reject() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Issue",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="reject",
            rationale="no",
        )
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {},
        }
    )
    assert out["findings"] == []


def test_adversarial_cleanup_ignores_off_domain_reject() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-security-1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Regex can catastrophically backtrack on attacker input.",
        claim_type="security_risk",
        failure_mode="Attacker-controlled input can trigger catastrophic regex backtracking.",
        evidence_summary="The changed code applies an unbounded regex to user-provided input.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Validate or bound the regex before executing it.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="accept",
            rationale="Attacker-controlled regex input can cause ReDoS.",
        ),
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="performance",
            verdict="reject",
            rationale="This is a security concern, not a performance concern.",
        ),
    ]

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {
                "ctx": FocusedContextResult(
                    request_id="ctx",
                    candidate_id=cand.candidate_id,
                    file_snippets={"src/x.py": "regex = user_input"},
                )
            },
            "metadata": {},
        }
    )

    assert len(out["findings"]) == 1
    assert out["metadata"]["adversarial_cleanup"]["ignored_off_domain_rejections"] == {
        cand.candidate_id: ["performance"]
    }


def test_adversarial_cleanup_promotes_tier1_security_without_focused_context() -> None:
    """Localized ReDoS-style claims must not be dropped solely for missing repo-wide context."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-security-redos",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=5,
        content="User-controlled pattern compiled without bounds.",
        claim_type="security_risk",
        failure_mode="ReDoS via catastrophic regex backtracking.",
        evidence_summary="Diff applies re.compile(user_input) without timeout.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Bound or validate regex input.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="accept",
            rationale="Tier 1 localized ReDoS risk.",
        )
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {},
            "metadata": {},
        }
    )
    assert len(out["findings"]) == 1


def test_adversarial_cleanup_promotes_needs_verification_with_runtime_verified() -> None:
    """Verifier verified satisfies the revision gate for needs_verification without focused hits."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:nv1",
        patch_task_id="t1",
        file_path="src/nodes.py",
        line_start=10,
        line_end=20,
        content="Possible None dereference in new node.",
        claim_type="defect",
        failure_mode="AttributeError when input string is None.",
        evidence_summary="Diff calls .strip() on input without a guard.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Handle None before calling str methods.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="needs_verification",
            rationale="Runtime repro needed.",
        )
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {},
            "metadata": {
                "verifier_hints": {
                    cand.candidate_id: {
                        "verdict": "verified",
                        "verification_scope": "concrete_behavior",
                        "updated_evidence_summary": "Runtime verifier: verified",
                        "final_rationale": "STATUS: CRASHED",
                        "attempts": 1,
                        "skipped_reason": "",
                    }
                }
            },
        }
    )
    assert len(out["findings"]) == 1
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "promoted"
    assert "verifier_advisory" in life


def test_adversarial_cleanup_drops_tier2_security_without_focused_hits() -> None:
    """Architectural security claims still require gathered context when not Tier 1 localized."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-security-tier2",
        patch_task_id="t1",
        file_path="src/api.py",
        line_start=1,
        line_end=10,
        content="Delete endpoint may not verify resource ownership.",
        claim_type="security_risk",
        failure_mode="Potential IDOR on delete.",
        evidence_summary="Authorization checks are not visible in this handler.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Verify tenant and ownership before delete.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="accept",
            rationale="Risk if no middleware auth.",
        )
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {},
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "required_context_not_gathered"
    )


def test_adversarial_cleanup_promotes_accepted_localized_defect_with_stale_context_request() -> None:
    """A direct logic accept should settle localized defects even if the candidate requested context."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="logic-none-1",
        patch_task_id="logic",
        file_path="src/nodes.py",
        line_start=20,
        line_end=20,
        content="return string[start:end],",
        claim_type="defect",
        failure_mode="None input raises TypeError when sliced.",
        evidence_summary="The changed line slices the input directly with no guard.",
        required_context=["Check whether upstream framework guarantees non-None strings."],
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Handle None or document the non-None invariant before slicing.",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="logic",
        verdict="accept",
        rationale="This is a localized TypeError in the changed code path.",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [report],
            "focused_context_results": {},
            "metadata": {},
        }
    )

    assert len(out["findings"]) == 1
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "promoted"
    assert life["context_requirement_overridden"] == "localized_defect_accepted_by_relevant_reflector"


def test_adversarial_cleanup_drops_when_routed_expert_says_not_applicable() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-security-1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Candidate was routed as security but is actually unrelated.",
        claim_type="defect",
        failure_mode="The issue was routed incorrectly.",
        evidence_summary="The candidate has no security evidence.",
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Route this finding to the correct domain.",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="security",
        verdict="not_applicable",
        rationale="This is not a security issue.",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [report],
            "metadata": {},
        }
    )

    assert out["findings"] == []
    assert out["metadata"]["adversarial_cleanup"]["misrouted_candidate_ids"][cand.candidate_id][0][
        "reflector_specialty"
    ] == "security"


def test_adversarial_cleanup_accept_overrides_stray_relevant_not_applicable() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="logic-data-1",
        patch_task_id="logic",
        file_path="src/parser.py",
        line_start=7,
        line_end=9,
        content="Invalid parse returns an empty string.",
        claim_type="defect",
        failure_mode="Callers cannot distinguish parse failure from a valid empty value.",
        evidence_summary="The changed branch catches the parser error and returns ''.",
        suspected_category="logic",
        reflection_specialties=["logic", "general"],
        recommendation="Return an explicit error state or preserve failure information.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="accept",
            rationale="Ambiguous sentinel value causes a data integrity issue.",
        ),
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="general",
            verdict="not_applicable",
            rationale="This is a logic finding, not a general maintainability finding.",
        ),
    ]

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {},
        }
    )

    assert len(out["findings"]) == 1
    cleanup = out["metadata"]["adversarial_cleanup"]
    assert cleanup["misrouted_candidate_ids"][cand.candidate_id][0]["reflector_specialty"] == "general"
    assert cleanup["candidate_lifecycle"][cand.candidate_id]["decision"] == "promoted"


def test_adversarial_cleanup_promotes_when_routed_expert_times_out_partial_quorum_default() -> None:
    """Missing logic report after timeout: general accepted — default relaxed quorum promotes."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-logic-1",
        patch_task_id="review-logic",
        file_path="src/x.py",
        line_start=10,
        line_end=12,
        content="All-groups extraction can return the wrong match data.",
        claim_type="defect",
        failure_mode="The changed code mishandles regex group extraction.",
        evidence_summary="The candidate was routed to both logic and general reflectors.",
        suspected_category="logic",
        reflection_specialties=["logic", "general"],
        recommendation="Validate the group extraction branch with patterns that have no captures.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="general",
            verdict="accept",
            rationale="The recommendation is readable and actionable.",
        )
    ]

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {
                "adversarial_reflection": {
                    "warnings": ["reflection_failed:logic:APITimeoutError: Request timed out."]
                }
            },
        }
    )

    assert len(out["findings"]) == 1
    cleanup = out["metadata"]["adversarial_cleanup"]
    assert cleanup.get("missing_required_reflections") == {}
    life = cleanup["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "promoted"
    assert life["abstaining_reflectors"] == ["logic"]


def test_adversarial_cleanup_strict_quorum_drops_when_routed_expert_times_out() -> None:
    """With full quorum required, missing logic report still drops (legacy strict behavior)."""
    strict = get_settings().model_copy(update={"reviewer_cleanup_require_full_reflection_quorum": True})
    node = make_adversarial_cleanup_node(settings=strict)
    cand = CandidateFinding(
        candidate_id="review-logic-1",
        patch_task_id="review-logic",
        file_path="src/x.py",
        line_start=10,
        line_end=12,
        content="All-groups extraction can return the wrong match data.",
        claim_type="defect",
        failure_mode="The changed code mishandles regex group extraction.",
        evidence_summary="The candidate was routed to both logic and general reflectors.",
        suspected_category="logic",
        reflection_specialties=["logic", "general"],
        recommendation="Validate the group extraction branch with patterns that have no captures.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="general",
            verdict="accept",
            rationale="The recommendation is readable and actionable.",
        )
    ]

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {
                "adversarial_reflection": {
                    "warnings": ["reflection_failed:logic:APITimeoutError: Request timed out."]
                }
            },
        }
    )

    assert out["findings"] == []
    cleanup = out["metadata"]["adversarial_cleanup"]
    assert cleanup["missing_required_reflections"] == {cand.candidate_id: ["logic"]}
    assert cleanup["candidate_lifecycle"][cand.candidate_id]["reason"] == "missing_required_reflection"


def test_adversarial_cleanup_still_drops_when_only_routed_specialty_never_reports() -> None:
    """Partial quorum cannot invent votes: logic-only route and zero logic reports → drop."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="logic-only-1",
        patch_task_id="t",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="bug",
        claim_type="defect",
        failure_mode="f",
        evidence_summary="e",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="fix it",
    )
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [],
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id][
        "reason"
    ] == "missing_required_reflection"


def test_adversarial_cleanup_drops_positive_observation() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-performance-1",
        patch_task_id="review-performance",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="The new batch delete is faster.",
        claim_type="positive_observation",
        failure_mode="No failure mode.",
        evidence_summary="The diff uses one batch operation.",
        suspected_category="performance",
        reflection_specialties=["performance"],
        recommendation="No action needed.",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="performance",
        verdict="accept",
        rationale="This is more efficient.",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [report],
            "metadata": {},
        }
    )

    assert out["findings"] == []
    assert out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id][
        "reason"
    ] == "non_promotable_claim_type"


def test_auto_focus_request_created_for_security_claim_needing_context() -> None:
    from src.orchestration.routing.critiquer_focus import auto_focus_requests

    task = ReviewTask(
        id="review-security",
        title="Security",
        description="Review auth.",
        target_files=["src/x.py", "src/caller.py"],
        specialty="security",
    )
    cand = CandidateFinding(
        candidate_id="review-security:c1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="delete_by_ids may delete unauthorized records.",
        claim_type="security_risk",
        failure_mode="A caller can pass IDs they are not authorized to delete.",
        evidence_summary="",
        required_context=["Find callers and authorization checks."],
        suspected_category="security",
        reflection_specialties=["security"],
        recommendation="Verify ownership before deletion.",
    )

    requests = auto_focus_requests(task, [cand])

    assert len(requests) == 1
    assert requests[0].candidate_id == cand.candidate_id
    assert "src/x.py" in requests[0].file_paths


def test_reflection_routes_candidates_only_to_declared_domains() -> None:
    node = make_adversarial_reflection_node(use_llm=False)
    security_candidate = CandidateFinding(
        candidate_id="review-security-1",
        patch_task_id="review-security",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Security issue",
        suspected_category="security",
        reflection_specialties=["security"],
    )
    general_candidate = CandidateFinding(
        candidate_id="review-general-1",
        patch_task_id="review-general",
        file_path="src/y.py",
        line_start=1,
        line_end=2,
        content="Missing tests",
        suspected_category="general",
        reflection_specialties=["general"],
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [security_candidate, general_candidate],
            "metadata": {},
        }
    )

    assert out["metadata"]["adversarial_reflection"]["routed_candidate_counts"] == {
        "security": 1,
        "logic": 0,
        "performance": 0,
        "general": 1,
    }
    assert out["metadata"]["adversarial_reflection"]["total_routed_candidate_reviews"] == 2


def test_reflection_retries_active_local_server_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(
        redis_enabled=False,
        reviewer_reflection_retry_backoff_seconds=0,
        reviewer_reflection_timeout_patience_seconds=60,
    )
    calls = {"invoke": 0}

    class FakeLlm:
        def invoke(self, _prompt: str) -> ReflectionBatchOutput:
            calls["invoke"] += 1
            if calls["invoke"] == 1:
                raise TimeoutError("Request timed out.")
            return ReflectionBatchOutput(
                reports=[
                    ReflectionReport(
                        candidate_id="review-logic-1",
                        reflector_specialty="logic",
                        verdict="accept",
                        rationale="The logic claim is valid.",
                    )
                ]
            )

    monkeypatch.setattr(
        "src.orchestration.nodes.application.reflection.Models.worker",
        lambda *_args, **_kwargs: FakeLlm(),
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.reflection.local_llm_server_active",
        lambda _settings: (True, "status ok"),
    )

    node = make_adversarial_reflection_node(settings=settings)
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [
                CandidateFinding(
                    candidate_id="review-logic-1",
                    patch_task_id="review-logic",
                    file_path="src/x.py",
                    line_start=1,
                    line_end=2,
                    content="Logic issue",
                    suspected_category="logic",
                    reflection_specialties=["logic"],
                )
            ],
            "metadata": {},
        }
    )

    assert calls["invoke"] == 2
    assert len(out["reflection_reports"]) == 1
    assert out["metadata"]["adversarial_reflection"]["warnings"] == [
        "reflection_timeout_server_active:logic"
    ]


def test_plan_critique_revision_shards_splits_when_over_budget() -> None:
    from src.orchestration.nodes.application.critique_revision import plan_critique_revision_shards

    cid = "t1:c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="issue",
    )
    results: dict[str, FocusedContextResult] = {}
    for i in range(8):
        rid = f"req{i}"
        results[rid] = FocusedContextResult(
            request_id=rid,
            candidate_id=cid,
            file_snippets={"f.py": "x" * 3000},
        )
    state: dict = {
        "candidate_findings": [cand],
        "focused_context_results": results,
    }
    shards = plan_critique_revision_shards(
        state,
        [cid],
        max_shard_chars=5000,
        max_candidate_chars=8000,
    )
    assert len(shards) >= 2
    assert sum(len(s.focused_results) for s in shards) == 8


def test_critique_revision_digest_dict_reducer_merge() -> None:
    import operator

    a = CritiqueRevisionDigest(
        shard_id="c:0",
        candidate_id="c",
        request_ids=["r1"],
        evidence_bullets=["one"],
        impact="unclear",
    )
    b = CritiqueRevisionDigest(
        shard_id="c:1",
        candidate_id="c",
        request_ids=["r2"],
        evidence_bullets=["two"],
        impact="supports",
    )
    merged = operator.or_({"c:0": a}, {"c:1": b})
    assert set(merged.keys()) == {"c:0", "c:1"}


def test_normalize_revision_rows_dedupes_and_warns() -> None:
    from src.domain.schemas import CritiqueRevisionItem
    from src.orchestration.nodes.application.critique_revision import _normalize_revision_rows

    rows, warns = _normalize_revision_rows(
        [
            CritiqueRevisionItem(candidate_id="x", verdict="accept", updated_evidence_summary="a"),
            CritiqueRevisionItem(candidate_id="x", verdict="reject", updated_evidence_summary="b"),
            CritiqueRevisionItem(candidate_id="y", verdict="accept", updated_evidence_summary="c"),
        ],
        {"x"},
    )
    assert len(rows) == 1
    assert rows[0]["verdict"] == "reject"
    assert any("duplicate" in w for w in warns)
    assert any("unknown" in w for w in warns)


def test_critique_revision_reduce_offline_writes_metadata() -> None:
    from src.orchestration.nodes.application.critique_revision import make_critique_revision_reduce_node

    cid = "t:c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="issue",
        claim_type="defect",
        failure_mode="f",
        evidence_summary="e",
        recommendation="r",
    )
    state: dict = {
        "run_id": "t",
        "repo_path": ".",
        "git_diff": "",
        "candidate_findings": [cand],
        "reflection_reports": [
            ReflectionReport(
                candidate_id=cid,
                reflector_specialty="security",
                verdict="needs_context",
                rationale="more",
            ),
        ],
        "focused_context_results": {
            "r1": FocusedContextResult(
                request_id="r1",
                candidate_id=cid,
                file_snippets={"s.py": "code"},
            ),
        },
        "critique_revision_digests": {
            "c1:0": CritiqueRevisionDigest(
                shard_id="c1:0",
                candidate_id=cid,
                request_ids=["r1"],
                evidence_bullets=["caller checks auth"],
                impact="supports",
            ),
        },
        "metadata": {},
    }
    node = make_critique_revision_reduce_node(use_llm=False)
    out = node(state)
    cr = out["metadata"]["critique_revision"]
    assert cr["digest_count"] == 1
    assert cr["shard_count_planned"] >= 1
    assert cr["revisions"] == []


def test_critique_revision_digest_offline_emits_digest() -> None:
    from src.domain.schemas import CritiqueRevisionShardPayload
    from src.orchestration.nodes.application.critique_revision import make_critique_revision_digest_node

    cid = "t:c1"
    cand = CandidateFinding(
        candidate_id=cid,
        patch_task_id="t",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="issue",
        claim_type="defect",
        failure_mode="f",
        evidence_summary="e",
        recommendation="r",
    )
    shard = CritiqueRevisionShardPayload(
        shard_id="c1:0",
        candidate_id=cid,
        candidate=cand,
        focused_results=[
            FocusedContextResult(request_id="r1", candidate_id=cid, file_snippets={"a.py": "z"}),
        ],
    )
    node = make_critique_revision_digest_node(use_llm=False)
    out = node(
        {
            "run_id": "t",
            "repo_path": ".",
            "git_diff": "",
            "critique_revision_shard": shard.model_dump(mode="json"),
            "metadata": {},
        }
    )
    assert "c1:0" in out["critique_revision_digests"]

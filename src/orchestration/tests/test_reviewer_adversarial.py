"""Tests for adversarial reviewer graph, context caps, and cleanup."""

from __future__ import annotations

import pytest

from src.config import Settings, get_settings
from src.domain.schemas import (
    AuditCoverageRecord,
    CandidateFinding,
    CritiquerOutput,
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
from src.orchestration.nodes.application.cleanup import RevisionSupportAuditOutput, make_adversarial_cleanup_node
from src.orchestration.nodes.application.critiquer import _needs_orthogonal_recall
from src.orchestration.nodes.application.reflection import make_adversarial_reflection_node


def test_merge_graph_metadata_deep_merges_parallel_critiquer_shapes() -> None:
    a = {"general_critiquer": {"by_task": {"t1": {"summary": "s1"}}}}
    b = {"general_critiquer": {"by_task": {"t2": {"summary": "s2"}}}}
    merged = merge_graph_metadata(a, b)
    assert merged["general_critiquer"]["by_task"]["t1"]["summary"] == "s1"
    assert merged["general_critiquer"]["by_task"]["t2"]["summary"] == "s2"


def _recall_task(
    *,
    title: str = "Diff-local correctness",
    description: str = "Broad functional review across the changed handler.",
    specialty: str = "logic",
) -> ReviewTask:
    return ReviewTask(
        id="review-logic",
        title=title,
        description=description,
        target_files=["src/app.py"],
        specialty=specialty,  # type: ignore[arg-type]
    )


def _recall_candidate(
    *,
    claim_type: str = "defect",
    content: str = "The handler lacks a terminal else for an unexpected mode.",
    failure_mode: str = "Missing return for dispatch fall-through.",
    evidence_summary: str = "The branch chain has no fallback.",
    behavioral_symptom: str = "missing_return",
    root_operation: str = "dispatch",
) -> CandidateFinding:
    return CandidateFinding(
        candidate_id="review-logic:c1",
        patch_task_id="review-logic",
        file_path="src/app.py",
        line_start=1,
        line_end=4,
        content=content,
        claim_type=claim_type,  # type: ignore[arg-type]
        failure_mode=failure_mode,
        evidence_summary=evidence_summary,
        recommendation="Check the changed contract and add the missing behavior if confirmed.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom=behavioral_symptom,  # type: ignore[arg-type]
        root_operation=root_operation,  # type: ignore[arg-type]
        evidence_for_contract="The changed handler is expected to return a concrete result.",
        counterexample="Calling the handler with an unexpected mode reaches the fall-through path.",
        rejection_check="The visible code does not show intentional narrowing or an upstream guarantee.",
    )


def test_broad_branch_only_critiquer_response_does_not_trigger_marker_recall() -> None:
    response = CritiquerOutput(candidates=[_recall_candidate()])

    assert _needs_orthogonal_recall(_recall_task(), response) is False


def test_narrow_branch_or_structured_task_does_not_trigger_orthogonal_recall() -> None:
    branch_task = _recall_task(
        title="Branch exhaustiveness",
        description="Audit terminal else handling only.",
    )
    structured_task = _recall_task(
        title="Structured extraction",
        description="Type-tracing structured result slots only.",
    )
    response = CritiquerOutput(candidates=[_recall_candidate()])

    assert _needs_orthogonal_recall(branch_task, response) is False
    assert _needs_orthogonal_recall(structured_task, response) is False


def test_broad_diverse_critiquer_response_does_not_trigger_orthogonal_recall() -> None:
    response = CritiquerOutput(
        candidates=[
            _recall_candidate(
                content="The changed signature no longer matches a call site.",
                failure_mode="API signature mismatch at an existing caller.",
                evidence_summary="The task evidence shows the signature and caller disagree.",
                behavioral_symptom="contract_mismatch",
                root_operation="contract",
            )
        ],
    )

    assert _needs_orthogonal_recall(_recall_task(), response) is False


def test_sparse_response_with_weak_audit_does_not_trigger_marker_recall() -> None:
    weak = CritiquerOutput(
        audit_coverage=[
            AuditCoverageRecord(
                surface="handle",
                dimensions=["branch exhaustiveness"],
                notes="Checked branch fall-through only.",
            )
        ]
    )
    diverse = CritiquerOutput(
        audit_coverage=[
            AuditCoverageRecord(
                surface="handle",
                dimensions=["api/signature compatibility"],
                notes="Checked call-site contract.",
            )
        ]
    )

    assert _needs_orthogonal_recall(_recall_task(), weak) is False
    assert _needs_orthogonal_recall(_recall_task(), diverse) is False


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


def test_critique_subgraph_parent_updates_returns_only_reducer_deltas() -> None:
    from src.orchestration.nodes.application.critique_pipeline import _critique_subgraph_parent_updates

    initial = {
        "token_usage": 5,
        "node_history": ["parent"],
        "review_checks": ["existing"],
        "focused_context_results": {"old": {"value": 1}},
    }
    full = {
        "token_usage": 8,
        "node_history": ["parent", "branch"],
        "review_checks": ["existing", "new"],
        "focused_context_results": {"old": {"value": 1}, "new": {"value": 2}},
    }

    out = _critique_subgraph_parent_updates(full, initial)  # type: ignore[arg-type]

    assert out["token_usage"] == 3
    assert out["node_history"] == ["branch"]
    assert out["review_checks"] == ["new"]
    assert out["focused_context_results"] == {"new": {"value": 2}}


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


def test_bounded_fulfiller_prefers_candidate_line_window() -> None:
    try:
        from src.orchestration.context.review_context import BoundedReviewContextFulfiller
    except ImportError as exc:
        pytest.skip(f"review context stack unavailable ({exc})")

    calls: list[str] = []

    class StubProvider:
        def _ensure_started(self, state: dict) -> None:
            return None

        def read_file_window(
            self,
            file_path: str,
            *,
            line_start: int,
            line_end: int,
            max_chars: int = 20000,
        ) -> str:
            calls.append(f"window:{file_path}:{line_start}-{line_end}")
            return "elif mode == 'All Groups':\n    result = join_delimiter.join(results)"

        def read_file_slice(self, file_path: str, *, max_chars: int = 20000) -> str:
            calls.append(f"slice:{file_path}")
            return "prefix only"

        def search_bounded(self, query: str, *, max_hits: int, file_paths=None):
            return []

        def ast_entities_for_file(self, file_path: str, **kwargs):
            return [], []

    cand = CandidateFinding(
        candidate_id="c1",
        patch_task_id="t",
        file_path="pkg/target.py",
        line_start=280,
        line_end=296,
        content="class Handler",
        claim_type="defect",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    req = FocusedContextRequest(
        request_id="r1",
        candidate_id=cand.candidate_id,
        requested_by_specialty="logic",
        file_paths=["pkg/target.py"],
    )

    result = BoundedReviewContextFulfiller(StubProvider()).fulfill(  # type: ignore[arg-type]
        {"run_id": "t", "metadata": {}, "candidate_findings": [cand]},
        req,
    )

    assert calls == ["window:pkg/target.py:280-296"]
    assert "All Groups" in result.file_snippets["pkg/target.py"]


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
        evidence_for_contract="The changed branch is expected to return the previous true result.",
        counterexample="Calling the accepted branch now returns False.",
        rejection_check="All relevant reflectors accepted the concrete behavior claim.",
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


def test_adversarial_cleanup_does_not_drop_qualified_module_recommendation() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="class PathNode returns the wrong normalized path",
        claim_type="defect",
        failure_mode="The changed branch returns a relative path where callers expect a normalized absolute path.",
        evidence_summary="The diff returns the joined path string without normalizing it first.",
        suspected_category="logic",
        recommendation="Resolve with os.path.realpath and compare against folder_paths.get_input_directory().",
        evidence_for_contract="The node name and callers expect normalized path output.",
        counterexample="A relative path reaches the changed return without normalization.",
        rejection_check="The recommendation names the same module contract and is not a foreign reference.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty=spec,
            verdict="accept",
            rationale="concrete defect",
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
    cleanup = out["metadata"]["adversarial_cleanup"]
    assert cleanup["recommendation_reference_advisories"] == [cand.candidate_id]


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


def test_adversarial_cleanup_revision_accept_overrides_reflector_reject() -> None:
    """Second-pass revision accept promotes when focused context backs the revised claim."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="review-logic-001",
        patch_task_id="review-logic",
        file_path="comfy_extras/nodes_string.py",
        line_start=177,
        line_end=188,
        content="class StringCompare",
        claim_type="defect",
        failure_mode="Missing return for unexpected mode values",
        evidence_summary="execute() lacks else branch for invalid mode.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Add else branch returning a boolean tuple.",
        required_context=["Confirm execute return paths"],
        evidence_for_contract="The execute method contract expects a boolean tuple return.",
        counterexample="An invalid mode reaches the missing fallback path.",
        rejection_check="Focused revision and verifier evidence support the changed behavior.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="reject",
            rationale="Ends With branch has an explicit return; original claim is wrong.",
        ),
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {
                "review-logic-001": FocusedContextResult(
                    request_id="review-logic-001",
                    candidate_id=cand.candidate_id,
                    file_contents_full={
                        "comfy_extras/nodes_string.py": "class StringCompare:\n    def execute(self):\n        pass\n",
                    },
                ),
            },
            "metadata": {
                "critique_revision": {
                    "revisions": [
                        {
                            "candidate_id": cand.candidate_id,
                            "verdict": "accept",
                            "updated_evidence_summary": (
                                "Runtime verifier STATUS: MISMATCH for invalid mode returning None."
                            ),
                        }
                    ],
                },
                "verifier_hints": {
                    cand.candidate_id: {
                        "verdict": "verified",
                        "verification_scope": "concrete_behavior",
                        "harness_error": True,
                        "product_verified": False,
                        "final_rationale": "Verifier script reported STATUS: MISMATCH (wrong output reproduced).",
                    },
                },
            },
        }
    )
    assert len(out["findings"]) == 1
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "promoted"
    assert life["reason"] == "critique_revision_accept_overrides_reject"
    assert life["overridden_rejecting_reflectors"] == ["logic"]


def test_adversarial_cleanup_revision_accept_does_not_override_reject_without_evidence() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:c1",
        patch_task_id="t1",
        file_path="src/x.py",
        line_start=1,
        line_end=2,
        content="Issue",
        claim_type="defect",
        failure_mode="crash",
        evidence_summary="line 1",
        recommendation="fix",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="reject",
            rationale="no",
        ),
    ]
    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {
                "critique_revision": {
                    "revisions": [
                        {
                            "candidate_id": cand.candidate_id,
                            "verdict": "accept",
                            "updated_evidence_summary": "revised claim",
                        }
                    ],
                },
            },
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "relevant_reflector_reject"
    )


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
        evidence_for_contract="The changed code accepts user-provided input at the regex boundary.",
        counterexample="An attacker-controlled input reaches the unbounded regex.",
        rejection_check="Focused context supports the security boundary and no bound is shown.",
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


def test_adversarial_cleanup_promotes_source_local_security_without_focused_context() -> None:
    """Source-local security claims are settled by reflector support scope."""
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
        evidence_for_contract="The changed source-local path compiles user-controlled input.",
        counterexample="A user-controlled pattern reaches compilation without bounds.",
        rejection_check="The security reflector accepted local support for the concrete path.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="accept",
            rationale="The changed code itself shows the resource-amplifying path.",
            support_scope="local",
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


def test_adversarial_cleanup_promotes_verified_with_required_context_localized_regex() -> None:
    """Run 68c1a024f6a8 class: required_context + concrete_behavior verified promotes without focused hits."""
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="logic-string-nodes-comprehensive-6",
        patch_task_id="logic-string-nodes-comprehensive",
        file_path="comfy_extras/nodes_string.py",
        line_start=217,
        line_end=227,
        content="elif mode == 'All Groups':",
        claim_type="defect",
        failure_mode="RegexExtract 'All Groups' skips group_index=0 (full match) when match.groups() is empty.",
        evidence_summary="Checks match.groups() before group_index; group 0 never returned.",
        behavioral_symptom="data_loss",
        root_operation="indexing",
        required_context=[
            "Does group_index include group 0 for full match?",
            "Are there tests for group_index=0 in 'All Groups' mode?",
        ],
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Allow group_index=0 for full match in All Groups mode.",
        evidence_for_contract="The mode and group_index parameter imply group_index=0 is supported.",
        counterexample="All Groups with group_index=0 reaches the wrong selection path.",
        rejection_check="Runtime verifier evidence supports the concrete behavior.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="needs_verification",
            rationale="group_index=0 behavior needs runtime proof.",
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
                        "final_rationale": "STATUS: CRASHED with traceback in target file (product behavior).",
                        "attempts": 1,
                        "skipped_reason": "",
                        "harness_error": False,
                        "product_verified": True,
                    }
                }
            },
        }
    )
    assert len(out["findings"]) == 1
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "promoted"
    assert life.get("context_requirement_overridden") == "runtime_verifier_concrete_behavior"


def test_adversarial_cleanup_drops_needs_context_with_inconclusive_verifier(monkeypatch) -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="security-resource-context",
        patch_task_id="security-resource",
        file_path="src/x.py",
        line_start=1,
        line_end=12,
        content="Changed code may allow unbounded resource use.",
        claim_type="security_risk",
        failure_mode="Resource amplification depends on unresolved caller exposure.",
        evidence_summary="Local code shows the primitive, but exposure is unresolved.",
        suspected_category="security",
        reflection_specialties=["security"],
        required_context=["Confirm whether the input reaches an untrusted boundary."],
        recommendation="Add a concrete bound if the boundary is confirmed.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="security",
            verdict="needs_context",
            rationale="The code primitive is visible, but caller exposure is unresolved.",
            support_scope="needs_context",
        )
    ]
    audit = RevisionSupportAuditOutput(
        verdict="unresolved",
        rationale="The revision restates the concern but does not resolve caller exposure.",
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.cleanup.Models.worker",
        lambda *_args, **_kwargs: type("FakeLLM", (), {"invoke": lambda self, _prompt: {"parsed": audit}})(),
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {
                "ctx": FocusedContextResult(
                    request_id="ctx",
                    candidate_id=cand.candidate_id,
                    file_snippets={"src/x.py": "def changed(value):\n    return value\n"},
                )
            },
            "metadata": {
                "critique_revision": {
                    "revisions": [
                        {
                            "candidate_id": cand.candidate_id,
                            "verdict": "accept",
                            "updated_evidence_summary": "Follow-up still does not resolve caller exposure.",
                        }
                    ]
                },
                "verifier_hints": {
                    cand.candidate_id: {
                        "verdict": "inconclusive",
                        "verification_scope": "concrete_behavior",
                        "harness_error": True,
                        "product_verified": False,
                        "final_rationale": "Harness error before product behavior.",
                    }
                },
            },
        }
    )

    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "needs_context_with_inconclusive_verifier"
    )
    assert out["metadata"]["adversarial_cleanup"]["revision_support_audits"][cand.candidate_id]["verdict"] == "unresolved"


def test_adversarial_cleanup_revision_accept_with_concrete_evidence_promotes_unresolved_reflection(monkeypatch) -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="logic-context-revised",
        patch_task_id="logic-context",
        file_path="src/x.py",
        line_start=3,
        line_end=8,
        content="Changed dispatch may fall through.",
        claim_type="defect",
        failure_mode="Missing fallback return on a changed branch.",
        evidence_summary="The initial check needed follow-up evidence.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        required_context=["Confirm the branch body and return contract."],
        recommendation="Add a fallback return or raise.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
        evidence_for_contract="The changed dispatch branch is expected to return or raise.",
        counterexample="A changed dispatch path falls through without a return.",
        rejection_check="The critique revision cites concrete changed-branch evidence.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="needs_context",
            rationale="Need the changed branch body.",
            support_scope="needs_context",
        )
    ]
    audit = RevisionSupportAuditOutput(
        verdict="resolved",
        rationale="The revision cites the changed dispatch branch and missing return path.",
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.cleanup.Models.worker",
        lambda *_args, **_kwargs: type("FakeLLM", (), {"invoke": lambda self, _prompt: {"parsed": audit}})(),
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "focused_context_results": {},
            "metadata": {
                "critique_revision": {
                    "revisions": [
                        {
                            "candidate_id": cand.candidate_id,
                            "verdict": "accept",
                            "updated_evidence_summary": (
                                "Changed code line shows the dispatch branch can fall through without a return."
                            ),
                        }
                    ]
                },
            },
        }
    )

    assert len(out["findings"]) == 1
    assert out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["decision"] == "promoted"


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
        evidence_for_contract="The changed node accepts the input value used by the string operation.",
        counterexample="A None input reaches the changed .strip() call.",
        rejection_check="Runtime verification supports the concrete crash path.",
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


def test_adversarial_cleanup_labels_unresolved_verification_gap() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:nv-unresolved",
        patch_task_id="t1",
        file_path="src/nodes.py",
        line_start=10,
        line_end=20,
        content="Changed operation may violate its local return contract.",
        claim_type="defect",
        failure_mode="The changed operation can return the wrong value.",
        evidence_summary="The local branch needs executable confirmation.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Preserve the declared local return contract.",
        evidence_for_contract="The changed operation declares a local return contract.",
        counterexample="A concrete branch reaches the wrong value.",
        rejection_check="No local evidence refutes the candidate; verification is unresolved.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="needs_verification",
            rationale="Executable behavior is still unresolved.",
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

    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "dropped"
    assert life["reason"] == "needs_verification_without_supporting_revision"


def test_adversarial_cleanup_product_verified_skips_incomplete_contradiction() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:verified-incomplete",
        patch_task_id="t1",
        file_path="src/handler.py",
        line_start=1,
        line_end=5,
        content="The 'Mode B' branch appears truncated in the provided evidence.",
        claim_type="defect",
        failure_mode="Incomplete branch implementation causes a syntax error.",
        evidence_summary="The branch lacks implementation and needs verification.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Complete the branch body.",
        behavioral_symptom="crash",
        root_operation="contract",
        evidence_for_contract="The changed branch must contain a valid implementation.",
        counterexample="Executing the incomplete branch produces a syntax error.",
        rejection_check="Product verifier evidence supports the source-local crash claim.",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="accept",
            rationale="The source-local claim is concrete.",
        )
    ]
    state = {
        "run_id": "t",
        "candidate_findings": [cand],
        "reflection_reports": reports,
        "metadata": {
            "critique_pipeline": {
                "by_task": {
                    "t1": {
                        "task_evidence": {
                            "file_contents": {
                                "src/handler.py": (
                                    "def execute(mode):\n"
                                    "    if mode == 'Mode B':\n"
                                    "        return 'ok'\n"
                                )
                            }
                        }
                    }
                }
            },
            "verifier_hints": {
                cand.candidate_id: {
                    "verdict": "verified",
                    "verification_scope": "concrete_behavior",
                    "harness_error": False,
                    "product_verified": True,
                    "updated_evidence_summary": "Runtime verifier: verified syntax error.",
                }
            },
        },
    }

    out = node(state)  # type: ignore[arg-type]

    assert len(out["findings"]) == 1
    assert "Runtime verifier evidence" in out["findings"][0].content
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["decision"] == "promoted"


def test_adversarial_cleanup_harness_error_does_not_skip_incomplete_contradiction() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:harness-incomplete",
        patch_task_id="t1",
        file_path="src/handler.py",
        line_start=1,
        line_end=5,
        content="The 'Mode B' branch appears truncated in the provided evidence.",
        claim_type="defect",
        failure_mode="Incomplete branch implementation causes a syntax error.",
        evidence_summary="The branch lacks implementation and needs verification.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Complete the branch body.",
        behavioral_symptom="crash",
        root_operation="contract",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="accept",
            rationale="The source-local claim is concrete.",
        )
    ]

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": reports,
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "t1": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/handler.py": (
                                        "def execute(mode):\n"
                                        "    if mode == 'Mode B':\n"
                                        "        return 'ok'\n"
                                    )
                                }
                            }
                        }
                    }
                },
                "verifier_hints": {
                    cand.candidate_id: {
                        "verdict": "verified",
                        "verification_scope": "concrete_behavior",
                        "harness_error": True,
                        "product_verified": False,
                    }
                },
            },
        }
    )

    assert out["findings"] == []
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["reason"] in {
        "incomplete_claim_contradicted_by_code_evidence",
        "incomplete_evidence_without_followup",
    }


def test_adversarial_cleanup_drops_resource_risk_without_concrete_support() -> None:
    node = make_adversarial_cleanup_node()
    cand = CandidateFinding(
        candidate_id="t1:resource-risk",
        patch_task_id="t1",
        file_path="src/handler.py",
        line_start=1,
        line_end=5,
        content="The changed handler may do unbounded work.",
        claim_type="defect",
        failure_mode="Resource use may grow without a bound.",
        evidence_summary="The operation is potentially expensive but no concrete failure path is shown.",
        required_context=["Confirm a concrete impact path."],
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Consider bounding the work.",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    reports = [
        ReflectionReport(
            candidate_id=cand.candidate_id,
            reflector_specialty="logic",
            verdict="accept",
            rationale="The operation could be expensive.",
        )
    ]

    out = node({"run_id": "t", "candidate_findings": [cand], "reflection_reports": reports})

    assert out["findings"] == []
    life = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]
    assert life["reason"] == "resource_risk_without_concrete_support"


def test_adversarial_cleanup_drops_tier2_security_without_focused_hits() -> None:
    """Architectural security claims still require gathered context when not source-local."""
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
        behavioral_symptom="crash",
        root_operation="indexing",
        required_context=["Check whether upstream framework guarantees non-None strings."],
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Handle None or document the non-None invariant before slicing.",
        evidence_for_contract="The changed slice operation relies on a non-None string contract.",
        counterexample="A None value reaches the direct slice operation.",
        rejection_check="The logic reflector accepted this as a local changed-code failure.",
    )
    report = ReflectionReport(
        candidate_id=cand.candidate_id,
        reflector_specialty="logic",
        verdict="accept",
        rationale="The changed code path itself contains the failure.",
        support_scope="local",
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
        evidence_for_contract="The parse result contract must distinguish failure from valid empty output.",
        counterexample="A parser error returns the same empty string as a valid empty value.",
        rejection_check="The logic reflector accepted the concrete ambiguity despite a general misroute.",
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
        evidence_for_contract="The extraction branch is expected to preserve match data for its mode.",
        counterexample="A pattern with no captures reaches the wrong extraction behavior.",
        rejection_check="The available reflector accepted the actionability after the routed expert timed out.",
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
    reason = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id][
        "reason"
    ]
    assert reason in ("non_promotable_claim_type", "resolution_only_not_promotable")


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


def test_auto_focus_request_created_for_defect_required_context() -> None:
    from src.orchestration.routing.critiquer_focus import auto_focus_requests

    task = ReviewTask(
        id="logic-string",
        title="Logic",
        description="Review string nodes.",
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    cand = CandidateFinding(
        candidate_id="logic-string-nodes-comprehensive-6",
        patch_task_id="logic-string-nodes-comprehensive",
        file_path="comfy_extras/nodes_string.py",
        line_start=217,
        line_end=227,
        content="elif mode == 'All Groups':",
        claim_type="defect",
        failure_mode="group_index=0 not returned when match.groups() empty.",
        evidence_summary="All Groups mode checks match.groups() before index.",
        required_context=["Does group_index include group 0 for full match?"],
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Fix group 0 handling.",
    )
    requests = auto_focus_requests(task, [cand])
    assert len(requests) == 1
    assert "group_index" in " ".join(requests[0].text_queries).lower()


def test_auto_focus_request_adds_subject_symbol_query() -> None:
    from src.orchestration.routing.critiquer_focus import auto_focus_requests

    task = ReviewTask(
        id="logic-string",
        title="Logic",
        description="Review string nodes.",
        target_files=["comfy_extras/nodes_string.py"],
        specialty="logic",
    )
    cand = CandidateFinding(
        candidate_id="logic-string:regex",
        patch_task_id="logic-string",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=296,
        content="class RegexExtract():...",
        claim_type="defect",
        failure_mode="optional group may yield absent values before join aggregation",
        evidence_summary="",
        required_context=["Review complete handler body."],
        suspected_category="logic",
        reflection_specialties=["logic"],
        recommendation="Normalize absent captures before joining.",
    )

    requests = auto_focus_requests(task, [cand])

    assert requests[0].symbol_queries == ["RegexExtract"]


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


def test_reflection_retries_on_length_finish(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings(redis_enabled=False)
    prompts: list[str] = []

    class LengthFinishReasonError(Exception):
        pass

    class FakeLlm:
        def invoke(self, prompt: str) -> dict:
            prompts.append(prompt)
            if len(prompts) == 1:
                raise LengthFinishReasonError("length limit was reached")
            return {
                "parsed": ReflectionBatchOutput(
                    reports=[
                        ReflectionReport(
                            candidate_id="review-logic-1",
                            reflector_specialty="logic",
                            verdict="accept",
                            rationale="Valid defect.",
                        )
                    ]
                )
            }

    monkeypatch.setattr(
        "src.orchestration.nodes.application.reflection.Models.worker",
        lambda *_args, **_kwargs: FakeLlm(),
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

    assert len(prompts) == 2
    assert "retry — required" in prompts[1]
    assert len(out["reflection_reports"]) == 1
    assert "reflection_llm_retry:reason=length" in out["metadata"]["adversarial_reflection"]["warnings"]


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
            f"{cid}:0": CritiqueRevisionDigest(
                shard_id=f"{cid}:0",
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

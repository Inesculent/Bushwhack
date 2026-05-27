"""Tests for semantic finding deduplication and quality gates."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, ReflectionReport, ReviewFinding
from src.orchestration.nodes.application.cleanup import make_adversarial_cleanup_node
from src.orchestration.nodes.application.synthesizer import synthesizer_node
from src.orchestration.routing.finding_dedupe import (
    candidate_signature_key,
    dedupe_candidates_by_signature,
    dedupe_review_findings_by_signature,
    ensure_unique_candidate_ids,
    ensure_unique_finding_ids,
    extract_subject_class,
    extract_subject_class_from_claim,
    is_required_upstream_none_guard_claim,
    is_resolution_only_finding,
    recommendation_cites_foreign_class,
    resolve_repo_file_path,
    revision_summary_conflicts_with_claim,
    review_finding_semantic_key,
    semantic_finding_key,
    split_claim_and_post_context,
)
from src.orchestration.routing.normalize_critiquer_candidates import normalize_critiquer_candidates


def _cand(**kwargs) -> CandidateFinding:
    base = dict(
        candidate_id="t:1",
        patch_task_id="t",
        file_path="comfy_extras/nodes_string.py",
        line_start=165,
        line_end=182,
        content="StringCompare.execute() missing else",
        claim_type="defect",
        failure_mode="implicit None when mode unrecognized",
        evidence_summary="no else after if/elif",
        recommendation="add else",
        reflection_specialties=["logic"],
        suspected_category="logic",
        severity="high",
    )
    base.update(kwargs)
    return CandidateFinding(**base)  # type: ignore[arg-type]


def test_semantic_key_ignores_line_range() -> None:
    a = semantic_finding_key(
        file_path="f.py",
        content="class StringCompare missing else",
        failure_mode="missing else return",
    )
    b = semantic_finding_key(
        file_path="f.py",
        content="class StringCompare():",
        failure_mode="missing else branch",
        evidence_summary="StringCompare.execute",
    )
    assert a == b


def test_findall_and_group_index_are_distinct_semantic_keys() -> None:
    findall_key = semantic_finding_key(
        file_path="pkg/h.py",
        content="class Handler",
        failure_mode="findall returns tuples but loop keeps only m[0]",
    )
    group_key = semantic_finding_key(
        file_path="pkg/h.py",
        content="class Handler",
        failure_mode="group_index=0 skipped when match.groups() is falsy",
    )
    assert findall_key[2] == "structured_slot_truncation"
    assert group_key[2] == "regex_group_index"
    assert findall_key != group_key


def test_dedupe_keeps_findall_and_group_index_candidates() -> None:
    findall_c = _cand(
        candidate_id="t:findall",
        content="class Handler",
        failure_mode="All Matches uses findall; only m[0] kept — data loss",
    )
    group_c = _cand(
        candidate_id="t:group",
        content="class Handler",
        failure_mode="group_index=0 blocked by truthiness on empty groups()",
    )
    out, dups = dedupe_candidates_by_signature([findall_c, group_c])
    assert len(out) == 2
    assert not dups


def test_unique_candidate_ids_prevent_self_duplicate_maps() -> None:
    first = _cand(candidate_id="t:dup")
    second = _cand(candidate_id="t:dup")

    unique = ensure_unique_candidate_ids([first, second])
    out, dups = dedupe_candidates_by_signature(unique)

    assert [c.candidate_id for c in unique] == ["t:dup", "t:dup__2"]
    assert len(out) == 1
    assert dups == {"t:dup": ["t:dup__2"]}


def test_candidate_signature_uses_symptom_and_root_operation() -> None:
    data_loss = _cand(
        candidate_id="t:data",
        content="class Handler",
        failure_mode="Structured row handling loses data",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    crash = _cand(
        candidate_id="t:crash",
        content="class Handler",
        failure_mode="Aggregation crashes when absent values are serialized",
        behavioral_symptom="crash",
        root_operation="aggregation",
    )
    assert candidate_signature_key(data_loss) != candidate_signature_key(crash)
    out, dups = dedupe_candidates_by_signature([data_loss, crash])
    assert len(out) == 2
    assert not dups


def test_normalize_splits_compound_orthogonal_candidate() -> None:
    from src.domain.schemas import ReviewTask

    task = ReviewTask(
        id="logic-structured",
        title="Structured extraction",
        description="Audit structured extraction and aggregation.",
        target_files=["src/x.py"],
        specialty="logic",
    )
    raw = _cand(
        candidate_id="c1",
        patch_task_id="",
        file_path="src/x.py",
        content="class Handler: structured extraction issue",
        failure_mode=(
            "Data loss: only the first slot is retained; also: TypeError when join() "
            "receives an absent element."
        ),
        evidence_summary="One handler has two independent symptoms.",
        recommendation="Preserve fields and normalize elements before aggregation.",
    )
    out, warnings, _ = normalize_critiquer_candidates(task, [raw])
    keys = {(c.behavioral_symptom, c.root_operation) for c in out}
    assert ("data_loss", "indexing") in keys
    assert ("crash", "aggregation") in keys
    assert not any("line_anchor_dropped" in w for w in warnings)
    crash = next(c for c in out if (c.behavioral_symptom, c.root_operation) == ("crash", "aggregation"))
    assert "absent or optional capture" in crash.failure_mode.lower()


def test_normalize_does_not_split_plain_join_as_aggregation_crash() -> None:
    from src.domain.schemas import ReviewTask

    task = ReviewTask(
        id="logic-structured",
        title="Structured extraction",
        description="Audit structured extraction and aggregation.",
        target_files=["src/x.py"],
        specialty="logic",
    )
    raw = _cand(
        candidate_id="c1",
        patch_task_id="",
        file_path="src/x.py",
        content="class Handler: structured extraction issue",
        failure_mode="Data loss: only the first slot is retained before join() output.",
        evidence_summary="The code keeps m[0] from tuple rows.",
        recommendation="Preserve fields before joining results.",
    )
    out, warnings, _ = normalize_critiquer_candidates(task, [raw])

    keys = {(c.behavioral_symptom, c.root_operation) for c in out}
    assert ("data_loss", "indexing") in keys
    assert ("crash", "aggregation") not in keys
    assert not any("line_anchor_dropped" in w for w in warnings)


def test_resolve_repo_file_path_fixes_typo() -> None:
    allowed = {"comfy_extras/nodes_string.py"}
    assert resolve_repo_file_path("comfy_expas/nodes_string.py", allowed) == (
        "comfy_extras/nodes_string.py"
    )


def test_recommendation_foreign_class_detected() -> None:
    assert recommendation_cites_foreign_class(
        content="class StringCompare:",
        failure_mode="StringCompare missing else branch",
        recommendation="Add return to StringConcatenate.execute instead",
    )


def test_redos_and_structured_slot_are_distinct_keys() -> None:
    redos_key = semantic_finding_key(
        file_path="pkg/h.py",
        content="class Handler",
        failure_mode="ReDoS via user-controlled pattern without timeout",
        recommendation="Add pattern length limits",
    )
    slot_key = semantic_finding_key(
        file_path="pkg/h.py",
        content="class Handler",
        failure_mode="findall tuples but loop keeps only m[0]",
        recommendation="Retain all slots per row",
    )
    assert redos_key[2] == "redos"
    assert slot_key[2] == "structured_slot_truncation"
    assert redos_key != slot_key


def test_handler_node_prose_yields_subject_class() -> None:
    assert extract_subject_class("RegexExtract node accepts user-controlled patterns") == "RegexExtract"


def test_duplicate_redos_findings_dedupe_by_handler() -> None:
    a = ReviewFinding(
        id="security-1-001",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="class RegexExtract: ReDoS on user-controlled regex_pattern",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add pattern length validation",
    )
    b = ReviewFinding(
        id="security-1-001",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="RegexExtract node accepts user-controlled regex patterns without limits",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add complexity heuristics or timeout",
    )
    out, dups = dedupe_review_findings_by_signature(ensure_unique_finding_ids([a, b]))
    assert len(out) == 1
    assert len(dups) == 1


def test_review_finding_key_uses_recommendation_for_stub_content() -> None:
    logic = ReviewFinding(
        id="logic-3-001",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="class RegexExtract():",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Use len(match.groups()) >= group_index - 1 for group_index > 0",
    )
    security = ReviewFinding(
        id="security-1-002",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="RegexExtract's All Groups mode has inconsistent group_index validation",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Clarify group_index semantics across modes",
    )
    assert review_finding_semantic_key(logic) == review_finding_semantic_key(security)
    out, _ = dedupe_review_findings_by_signature([logic, security])
    assert len(out) == 1


def test_ensure_unique_finding_ids_suffixes_duplicates() -> None:
    base = ReviewFinding(
        id="security-1-001",
        file_path="f.py",
        line_start=1,
        line_end=2,
        content="x",
        severity="low",
        feedback_type="defect_detection",
    )
    out = ensure_unique_finding_ids([base, base.model_copy()])
    assert [f.id for f in out] == ["security-1-001", "security-1-001__2"]


def test_dedupe_prefers_logic_task_over_general_for_same_family() -> None:
    general = _cand(
        candidate_id="general_1_002",
        patch_task_id="general_1",
        content="class Handler:",
        failure_mode="Data loss: only m[0] kept from structured rows",
        evidence_summary="hedged",
        recommendation="Consider documenting behavior",
        severity="high",
    )
    logic = _cand(
        candidate_id="logic_3_003",
        patch_task_id="logic_3",
        content="class Handler:",
        failure_mode="Data loss: only m[0] kept from structured rows",
        evidence_summary="findall returns tuples",
        recommendation="Retain all required slots per row",
        severity="medium",
    )
    out, dups = dedupe_candidates_by_signature([general, logic])
    assert len(out) == 1
    assert out[0].candidate_id == "logic_3_003"
    assert "general_1_002" in dups.get("logic_3_003", [])


def test_extract_subject_class_from_claim_ignores_recommendation() -> None:
    subject = extract_subject_class_from_claim(
        content="class StringCompare: missing terminal else",
        failure_mode="StringCompare branch exhaustiveness",
        evidence_summary="no else after elif",
    )
    assert subject == "StringCompare"


def test_revision_summary_conflicts_with_claim() -> None:
    assert not revision_summary_conflicts_with_claim(
        content="class StringCompare:",
        failure_mode="missing else",
        revision_summary="Static analysis confirms missing terminal else in StringCompare.execute",
    )
    assert revision_summary_conflicts_with_claim(
        content="class RegexExtract:",
        failure_mode="tuple slot truncation",
        revision_summary="StringCompare.execute missing terminal else branch",
    )


def test_post_context_does_not_merge_distinct_logic_families_in_dedupe() -> None:
    else_finding = ReviewFinding(
        id="logic_b",
        file_path="pkg/nodes.py",
        line_start=160,
        line_end=189,
        content="class StringCompare: missing else after last elif branch",
        severity="high",
        feedback_type="defect_detection",
        recommendation="add terminal else",
    )
    none_finding = ReviewFinding(
        id="structured",
        file_path="pkg/nodes.py",
        line_start=250,
        line_end=280,
        content=(
            "class RegexExtract: nonetype in join path when aggregating results\n\n"
            "Post-context evidence: join() may raise TypeError when None elements appear"
        ),
        severity="high",
        feedback_type="defect_detection",
        recommendation="use match.group(group_index) or ''",
    )
    out, dups = dedupe_review_findings_by_signature([else_finding, none_finding])
    assert len(out) == 2
    assert not dups
    families = {review_finding_semantic_key(f)[2] for f in out}
    assert "missing_branch_return" in families
    assert "aggregation_none_type" in families


def test_dedupe_review_findings_merges_distinct_family_content() -> None:
    f1 = ReviewFinding(
        id="a",
        file_path="pkg/h.py",
        line_start=1,
        line_end=10,
        content="class Handler",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="fix findall indexing",
    )
    f2 = ReviewFinding(
        id="b",
        file_path="pkg/h.py",
        line_start=1,
        line_end=10,
        content="class Handler: group_index",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="fix groups()",
    )
    out, dups = dedupe_review_findings_by_signature([f1, f2])
    assert len(out) == 2
    assert not dups


def test_final_dedupe_preserves_structured_data_and_aggregation_findings() -> None:
    data_loss = ReviewFinding(
        id="data",
        file_path="pkg/h.py",
        line_start=10,
        line_end=20,
        content="class Handler: All Matches keeps only m[0] from tuple rows",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Preserve all captured slots from findall tuple results.",
    )
    aggregation = ReviewFinding(
        id="agg",
        file_path="pkg/h.py",
        line_start=21,
        line_end=30,
        content="class Handler: optional group may produce None before join() aggregation",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Normalize absent elements before joining results.",
    )

    out, dups = dedupe_review_findings_by_signature([data_loss, aggregation])

    assert len(out) == 2
    assert not dups


def test_final_dedupe_preserves_structured_group_index_and_absent_aggregation() -> None:
    data_loss = ReviewFinding(
        id="data",
        file_path="pkg/h.py",
        line_start=10,
        line_end=20,
        content="class Handler: All Matches keeps only m[0] from tuple rows",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Preserve all captured slots from findall tuple results.",
    )
    group_index = ReviewFinding(
        id="group",
        file_path="pkg/h.py",
        line_start=21,
        line_end=30,
        content="class Handler: group_index=0 handling is inconsistent with groups() semantics",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Handle group 0 explicitly.",
    )
    aggregation = ReviewFinding(
        id="agg",
        file_path="pkg/h.py",
        line_start=31,
        line_end=40,
        content="class Handler: optional capture may produce an absent value before join() aggregation",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Normalize absent capture values before joining results.",
    )

    out, dups = dedupe_review_findings_by_signature([data_loss, group_index, aggregation])

    assert {f.id for f in out} == {"data", "group", "agg"}
    assert not dups


def test_dedupe_merges_failure_mode_from_dropped_duplicate() -> None:
    c1 = _cand(
        candidate_id="t:1",
        line_start=58,
        failure_mode="implicit None when mode unrecognized",
        evidence_summary="StringCompare.execute missing else",
    )
    c2 = _cand(
        candidate_id="t:2",
        line_start=176,
        failure_mode="no final return after elif chain",
        evidence_summary="StringCompare.execute branch exhaustiveness",
    )
    out, _ = dedupe_candidates_by_signature([c1, c2])
    assert len(out) == 1
    blob = out[0].failure_mode.lower()
    assert "implicit none" in blob or "no final return" in blob


def test_dedupe_collapses_stringcompare_duplicates() -> None:
    git_diff = "\n".join(
        [
            "@@ -0,0 +165,50 @@",
            "+class StringCompare():",
            "+    def execute(self):",
            "+        if mode == 'Equal':",
            "+            return True",
        ]
    )
    c1 = _cand(candidate_id="t:1", line_start=58, line_end=73, content="class StringCompare():")
    c2 = _cand(candidate_id="t:2", line_start=176, line_end=188)
    c3 = _cand(candidate_id="t:3", line_start=143, line_end=148)
    out, dups = dedupe_candidates_by_signature([c1, c2, c3], git_diff=git_diff)
    assert len(out) == 1
    assert len(dups) >= 1
    assert out[0].line_start == 176


def test_resolution_only_detected() -> None:
    assert is_resolution_only_finding(
        "RegexExtract ok",
        "No action needed - else clause already handles invalid modes",
    )
    assert is_resolution_only_finding(
        "CaseConverter has no critical defect found.",
        "Consider logging conversion results for observability.",
    )


def test_required_param_none_guard_rejected() -> None:
    cand = _cand(
        file_path="comfy_extras/nodes_string.py",
        line_start=24,
        line_end=26,
        content="StringConcatenate.execute",
        failure_mode="TypeError when passing None to required string_a/string_b",
        evidence_summary=(
            "While INPUT_TYPES declares required strings, the in-function contract rule "
            "states schemas don't guarantee runtime types."
        ),
    )
    assert is_required_upstream_none_guard_claim(cand)


def test_cleanup_drops_resolution_and_none_guard() -> None:
    node = make_adversarial_cleanup_node()
    none_cand = _cand(
        candidate_id="none-1",
        line_start=24,
        line_end=26,
        content="StringConcatenate",
        failure_mode="TypeError when passing None to required string_a",
        evidence_summary="missing isinstance guard on required string_a per INPUT_TYPES",
    )
    resolution_cand = _cand(
        candidate_id="res-1",
        line_start=252,
        line_end=268,
        content="RegexExtract.execute()",
        failure_mode="missing else",
        recommendation="No action needed - else already present",
    )
    from src.domain.schemas import ReflectionReport

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [none_cand, resolution_cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=none_cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="bad",
                ),
                ReflectionReport(
                    candidate_id=resolution_cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="bad",
                ),
            ],
            "metadata": {},
        }
    )
    assert out["findings"] == []


def test_cleanup_drops_scope_claim_contradicted_by_code_evidence() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="scope-1",
        patch_task_id="logic-scope",
        file_path="src/x.py",
        line_start=1,
        line_end=8,
        content="class Handler: 'all groups' branch outside try",
        failure_mode="The 'all groups' branch is outside the try block so exceptions are uncaught.",
        evidence_summary="Claim says the branch is not wrapped.",
        recommendation="Move the branch into the wrapper.",
        behavioral_symptom="uncaught_exception",
        root_operation="exception_scope",
    )
    from src.domain.schemas import ReflectionReport

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="accepted",
                )
            ],
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-scope": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/x.py": "\n".join(
                                        [
                                            "class Handler:",
                                            "    def run(self, mode):",
                                            "        try:",
                                            "            if mode == 'first':",
                                            "                return 1",
                                            "            elif mode == 'all groups':",
                                            "                return 2",
                                            "        except Exception:",
                                            "            return 0",
                                        ]
                                    )
                                }
                            }
                        }
                    }
                }
            },
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "scope_claim_contradicted_by_code_evidence"
    )


def test_cleanup_drops_broad_resource_risk_without_impact_path() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="risk-1",
        patch_task_id="security-risk",
        file_path="src/x.py",
        line_start=1,
        line_end=8,
        content="Parser may do expensive matching.",
        claim_type="security_risk",
        failure_mode="Unbounded work without a concrete boundary or caller impact.",
        evidence_summary="Risky primitive exists.",
        recommendation="Add limits.",
        suspected_category="security",
        reflection_specialties=["security"],
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    from src.domain.schemas import ReflectionReport

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="security",
                    verdict="accept",
                    rationale="accepted",
                )
            ],
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "broad_risk_without_concrete_impact_path"
    )


def test_cleanup_drops_resource_defect_without_concrete_impact_path() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="resource-defect",
        patch_task_id="logic-risk",
        file_path="src/x.py",
        line_start=1,
        line_end=8,
        content="Parser may consume excessive memory.",
        claim_type="defect",
        failure_mode="Unbounded work during aggregation.",
        evidence_summary="Large result lists may grow.",
        recommendation="Add limits.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="accepted",
                )
            ],
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "broad_risk_without_concrete_impact_path"
    )


def test_cleanup_keeps_resource_claim_with_concrete_boundary() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="resource-backed",
        patch_task_id="logic-risk",
        file_path="src/x.py",
        line_start=1,
        line_end=8,
        content="Public request handler may consume excessive memory.",
        claim_type="defect",
        failure_mode="Unbounded work on external request input.",
        evidence_summary="The request path forwards user-controlled values into an expensive operation.",
        recommendation="Add a bounded execution path.",
        suspected_category="logic",
        reflection_specialties=["logic"],
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="accepted",
                )
            ],
            "focused_context_results": {
                "r1": {
                    "request_id": "r1",
                    "candidate_id": cand.candidate_id,
                    "file_snippets": {"src/x.py": "public request path"},
                }
            },
            "metadata": {},
        }
    )
    assert [finding.id for finding in out["findings"]] == [cand.candidate_id]


def test_cleanup_drops_reflection_pivot_without_followup() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="regex-pivot",
        patch_task_id="logic-regex",
        file_path="src/x.py",
        line_start=10,
        line_end=40,
        content="class RegexExtract():",
        claim_type="defect",
        failure_mode="All Groups returns empty output when no capture groups exist.",
        evidence_summary="The evidence describes All Groups behavior.",
        recommendation="Handle All Groups output.",
        suspected_category="logic",
        reflection_specialties=["logic"],
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="reject",
                    rationale="The All Groups claim is not supported by the code.",
                ),
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="All Matches uses findall and keeps only m[0], losing tuple data.",
                ),
            ],
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "reflection_family_conflict_without_followup"
    )


def test_cleanup_caps_resource_findings_per_symbol_operation() -> None:
    node = make_adversarial_cleanup_node()
    first = _cand(
        candidate_id="resource-1",
        file_path="src/x.py",
        content="class RegexExtract(): public request path",
        failure_mode="Unbounded work on external input.",
        evidence_summary="Public request can trigger expensive work.",
        recommendation="Add bounded execution.",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    second = first.model_copy(
        update={
            "candidate_id": "resource-2",
            "failure_mode": "Excessive memory use on external input.",
            "evidence_summary": "Public request can trigger memory growth.",
        }
    )
    logic = _cand(
        candidate_id="logic-1",
        file_path="src/x.py",
        content="class RegexExtract():",
        failure_mode="group_index=0 skips the full match.",
        evidence_summary="groups() excludes the full match.",
        recommendation="Handle full-match indexing explicitly.",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [first, second, logic],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=candidate.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="accepted",
                )
                for candidate in (first, second, logic)
            ],
            "focused_context_results": {
                "r1": {
                    "request_id": "r1",
                    "candidate_id": first.candidate_id,
                    "file_snippets": {"src/x.py": "public request path"},
                },
                "r2": {
                    "request_id": "r2",
                    "candidate_id": second.candidate_id,
                    "file_snippets": {"src/x.py": "public request path"},
                },
            },
            "metadata": {},
        }
    )
    ids = {finding.id for finding in out["findings"]}
    assert len(ids & {"resource-1", "resource-2"}) == 1
    assert "logic-1" in ids


def test_final_dedupe_keeps_resource_and_incomplete_implementation_separate() -> None:
    incomplete = ReviewFinding(
        id="logic-incomplete",
        file_path="src/x.py",
        line_start=1,
        line_end=20,
        content="class RegexExtract(): handler implementation appears incomplete.",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Complete the missing handler return path.",
    )
    resource = ReviewFinding(
        id="resource-use",
        file_path="src/x.py",
        line_start=1,
        line_end=20,
        content="class RegexExtract(): external input can cause excessive memory growth.",
        severity="medium",
        feedback_type="optimization",
        recommendation="Bound resource use on the request path.",
    )

    kept, duplicates = dedupe_review_findings_by_signature([incomplete, resource])
    assert {finding.id for finding in kept} == {"logic-incomplete", "resource-use"}
    assert duplicates == {}


def test_synthesizer_drops_resolution_only() -> None:
    finding = ReviewFinding(
        id="x",
        file_path="f.py",
        line_start=1,
        line_end=2,
        content="ok",
        severity="low",
        feedback_type="defect_detection",
        recommendation="No action needed",
    )
    out = synthesizer_node({"findings": [finding], "metadata": {}})
    assert out["final_findings"] == []
    assert "x" in out["metadata"]["review_synthesizer"]["dropped_resolution_only_ids"]


def test_synthesizer_duplicate_map_uses_distinct_ids() -> None:
    base = ReviewFinding(
        id="x",
        file_path="f.py",
        line_start=1,
        line_end=2,
        content="class Handler: missing return",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add a terminal return.",
    )
    out = synthesizer_node(
        {
            "findings": [base, base.model_copy()],
            "metadata": {
                "adversarial_cleanup": {
                    "candidate_lifecycle": {
                        "x": {"decision": "promoted"},
                    }
                }
            },
        }
    )
    dupes = out["metadata"]["review_synthesizer"]["semantic_dedupe_duplicates"]
    assert dupes == {"x": ["x__2"]}
    assert out["metadata"]["review_synthesizer"]["lost_promoted_candidate_ids"] == []


def test_synthesizer_uses_cleanup_duplicate_map_for_lost_promoted_audit() -> None:
    finding = ReviewFinding(
        id="keeper",
        file_path="f.py",
        line_start=1,
        line_end=2,
        content="class Handler: missing return",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add a terminal return.",
    )
    out = synthesizer_node(
        {
            "findings": [finding],
            "candidate_findings": [],
            "metadata": {
                "adversarial_cleanup": {
                    "candidate_lifecycle": {
                        "keeper": {"decision": "promoted"},
                        "dropped": {"decision": "promoted"},
                    },
                    "semantic_dedupe_finding_duplicates": {"keeper": ["dropped"]},
                }
            },
        }
    )

    meta = out["metadata"]["review_synthesizer"]
    assert meta["lost_promoted_candidate_ids"] == []
    assert meta["recall_audit"]["duplicate_equivalents"] == {"dropped": "keeper"}

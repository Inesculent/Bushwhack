"""Tests for semantic finding deduplication and quality gates."""

from __future__ import annotations

from src.domain.schemas import CandidateFinding, ReflectionReport, ReviewFinding
from src.orchestration.nodes.application.cleanup import (
    RevisionSupportAuditOutput,
    SemanticClaimClusterDecision,
    SemanticClaimDuplicateGroup,
    SemanticClaimClusterOutput,
    SemanticEquivalenceAuditItem,
    SemanticEquivalenceAuditOutput,
    _apply_semantic_claim_cluster_audit,
    _apply_semantic_equivalence_audit,
    _semantic_claim_clusters,
    make_adversarial_cleanup_node,
)
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
        evidence_for_contract="The changed execute method is expected to return a result.",
        counterexample="Calling execute with an unrecognized mode falls through.",
        rejection_check="No caller guarantee or intentional narrowing is shown.",
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
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    group_key = semantic_finding_key(
        file_path="pkg/h.py",
        content="class Handler",
        failure_mode="group_index=0 skipped when match.groups() is falsy",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
    )
    assert findall_key[3:] == ("data_loss", "indexing")
    assert group_key[3:] == ("wrong_output", "indexing")
    assert findall_key != group_key


def test_dedupe_keeps_findall_and_group_index_candidates() -> None:
    findall_c = _cand(
        candidate_id="t:findall",
        content="class Handler",
        failure_mode="All Matches uses findall; only m[0] kept — data loss",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    group_c = _cand(
        candidate_id="t:group",
        content="class Handler",
        failure_mode="group_index=0 blocked by truthiness on empty groups()",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
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


def test_candidate_dedupe_ignores_wording_when_structured_signature_matches() -> None:
    first = _cand(
        candidate_id="t:wording-a",
        content="class Handler: keeps the first parsed row slot",
        failure_mode="Only the first slot is retained from structured rows.",
        evidence_summary="row[0] is used when normalizing records.",
        recommendation="Preserve all relevant fields.",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    second = _cand(
        candidate_id="t:wording-b",
        content="class Handler: structured output loses fields",
        failure_mode="Captured row data is silently discarded.",
        evidence_summary="The same handler indexes the first element.",
        recommendation="Keep the full structured value.",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )

    out, dups = dedupe_candidates_by_signature([first, second])

    assert len(out) == 1
    assert {out[0].candidate_id, next(iter(dups.values()))[0]} == {
        "t:wording-a",
        "t:wording-b",
    }


def test_candidate_dedupe_keeps_resource_claim_kinds_separate() -> None:
    security = _cand(
        candidate_id="t:security",
        content="class Handler: user input can cause timeout",
        claim_type="security_risk",
        failure_mode="Attacker-controlled input can force unbounded work.",
        evidence_summary="The request path applies no bound.",
        recommendation="Bound the request path.",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    performance = _cand(
        candidate_id="t:perf",
        content="class Handler: cache misses can cause timeout",
        claim_type="performance_regression",
        failure_mode="Repeated work can become expensive under load.",
        evidence_summary="The same operation is recomputed.",
        recommendation="Cache or bound the work.",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )

    out, dups = dedupe_candidates_by_signature([security, performance])

    assert {c.candidate_id for c in out} == {"t:security", "t:perf"}
    assert not dups


def test_normalize_keeps_structured_metadata_without_text_splitting() -> None:
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
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    out, warnings, _ = normalize_critiquer_candidates(task, [raw])
    keys = {(c.behavioral_symptom, c.root_operation) for c in out}
    assert ("data_loss", "indexing") in keys
    assert ("crash", "aggregation") not in keys
    assert not any("line_anchor_dropped" in w for w in warnings)


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
        behavioral_symptom="data_loss",
        root_operation="indexing",
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
        claim_kind="security_risk",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    slot_key = semantic_finding_key(
        file_path="pkg/h.py",
        content="class Handler",
        failure_mode="findall tuples but loop keeps only m[0]",
        recommendation="Retain all slots per row",
        claim_kind="defect",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    assert redos_key[2:] == ("security_risk", "unbounded_work", "resource_use")
    assert slot_key[2:] == ("defect", "data_loss", "indexing")
    assert redos_key != slot_key


def test_explicit_execute_reference_yields_subject_class() -> None:
    assert extract_subject_class("RegexExtract.execute accepts user-controlled patterns") == "RegexExtract"


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
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    b = ReviewFinding(
        id="security-1-001",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="RegexExtract.execute accepts user-controlled regex patterns without limits",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add complexity heuristics or timeout",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )
    out, dups = dedupe_review_findings_by_signature(ensure_unique_finding_ids([a, b]))
    assert len(out) == 1
    assert len(dups) == 1


def test_review_finding_key_uses_explicit_content_subject() -> None:
    logic = ReviewFinding(
        id="logic-3-001",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="class RegexExtract():",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Use len(match.groups()) >= group_index - 1 for group_index > 0",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
    )
    security = ReviewFinding(
        id="security-1-002",
        file_path="comfy_extras/nodes_string.py",
        line_start=228,
        line_end=323,
        content="RegexExtract.execute All Groups mode has inconsistent group_index validation",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Clarify group_index semantics across modes",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
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
        behavioral_symptom="missing_return",
        root_operation="dispatch",
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
        behavioral_symptom="crash",
        root_operation="aggregation",
    )
    out, dups = dedupe_review_findings_by_signature([else_finding, none_finding])
    assert len(out) == 2
    assert not dups
    behaviors = {review_finding_semantic_key(f)[3:] for f in out}
    assert ("missing_return", "dispatch") in behaviors
    assert ("crash", "aggregation") in behaviors


def test_dedupe_review_findings_preserves_distinct_behavior_metadata() -> None:
    f1 = ReviewFinding(
        id="a",
        file_path="pkg/h.py",
        line_start=1,
        line_end=10,
        content="class Handler",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="fix findall indexing",
        behavioral_symptom="data_loss",
        root_operation="indexing",
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
        behavioral_symptom="wrong_output",
        root_operation="indexing",
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
        behavioral_symptom="data_loss",
        root_operation="indexing",
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
        behavioral_symptom="crash",
        root_operation="aggregation",
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
        behavioral_symptom="data_loss",
        root_operation="indexing",
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
        behavioral_symptom="wrong_output",
        root_operation="indexing",
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
        behavioral_symptom="crash",
        root_operation="aggregation",
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


def test_candidate_dedupe_normalizes_behavior_metadata_before_signature() -> None:
    c1 = _cand(
        candidate_id="t:path-1",
        file_path="pkg/files.py",
        line_start=10,
        line_end=12,
        content="open_user_path joins ../ segments into the base directory",
        failure_mode="path traversal via ../",
        evidence_summary="source is user path, sink is os.path.join",
        behavioral_symptom="other",
        root_operation="other",
    )
    c2 = _cand(
        candidate_id="t:path-2",
        file_path="pkg/files.py",
        line_start=10,
        line_end=12,
        content="open_user_path allows path traversal",
        failure_mode="user-controlled ../ escapes base path",
        evidence_summary="source user input reaches filesystem path sink",
        behavioral_symptom="wrong_output",
        root_operation="contract",
    )

    out, dups = dedupe_candidates_by_signature([c1, c2])

    assert len(out) == 1
    dropped = {item for values in dups.values() for item in values}
    assert len(dropped) == 1
    assert out[0].candidate_id not in dropped
    assert out[0].behavioral_symptom == "contract_mismatch"
    assert out[0].root_operation == "resource_use"
    assert "filesystem path sink" in out[0].evidence_summary


def test_candidate_dedupe_keeps_distinct_symptoms_on_same_surface() -> None:
    crash = _cand(
        candidate_id="t:crash",
        file_path="pkg/files.py",
        line_start=10,
        line_end=12,
        content="open_user_path",
        failure_mode="UnboundLocalError referenced before assignment",
        evidence_summary="local variable only assigned in one branch",
    )
    contract = _cand(
        candidate_id="t:contract",
        file_path="pkg/files.py",
        line_start=10,
        line_end=12,
        content="open_user_path",
        failure_mode="default option is not in the allowed enum",
        evidence_summary="default value violates allowed options",
    )

    out, dups = dedupe_candidates_by_signature([crash, contract])

    assert {item.candidate_id for item in out} == {"t:crash", "t:contract"}
    assert dups == {}


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


def test_cleanup_preserves_structured_behavior_metadata_on_promoted_finding() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="structured-promote",
        file_path="src/x.py",
        line_start=1,
        line_end=20,
        content="class Handler: structured row output drops fields",
        failure_mode="Data loss from first-slot retention.",
        evidence_summary="The handler keeps row[0].",
        recommendation="Preserve all relevant row fields.",
        behavioral_symptom="data_loss",
        root_operation="indexing",
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
                    rationale="The data-loss claim is supported.",
                )
            ],
            "metadata": {},
        }
    )

    assert len(out["findings"]) == 1
    finding = out["findings"][0]
    assert finding.behavioral_symptom == "data_loss"
    assert finding.root_operation == "indexing"


def test_cleanup_drops_promotable_candidate_missing_contract_proof() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(counterexample="")

    out = node(
        {
            "run_id": "t",
            "git_diff": "diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n+++ b/comfy_extras/nodes_string.py\n+pass\n",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="The changed behavior is a concrete defect.",
                )
            ],
            "metadata": {},
        }
    )

    assert out["findings"] == []
    lifecycle = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"]
    assert lifecycle[cand.candidate_id]["reason"] == "missing_contract_proof"
    assert lifecycle[cand.candidate_id]["missing_fields"] == ["counterexample"]
    proof = out["metadata"]["adversarial_cleanup"]["contract_proof_drops"]
    assert proof["missing_contract_proof_candidate_ids"] == [cand.candidate_id]
    assert proof["missing_contract_proof_count"] == 1


def test_cleanup_drops_promotable_candidate_with_self_doubting_contract_proof() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(counterexample="Calling execute with this mode returns None, though this may be intentional.")

    out = node(
        {
            "run_id": "t",
            "git_diff": "diff --git a/comfy_extras/nodes_string.py b/comfy_extras/nodes_string.py\n+++ b/comfy_extras/nodes_string.py\n+pass\n",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="The changed behavior is a concrete defect.",
                )
            ],
            "metadata": {},
        }
    )

    assert out["findings"] == []
    lifecycle = out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"]
    assert lifecycle[cand.candidate_id]["reason"] == "weak_contract_proof"
    proof = out["metadata"]["adversarial_cleanup"]["contract_proof_drops"]
    assert proof["weak_contract_proof_candidate_ids"] == [cand.candidate_id]
    assert proof["weak_contract_proof_count"] == 1


def test_cleanup_llm_equivalence_merges_same_issue_with_different_wording() -> None:
    broad = ReviewFinding(
        id="broad",
        file_path="src/x.py",
        line_start=10,
        line_end=40,
        content="class Handler: structured extraction drops a selected value.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Handle the selected value before aggregating results.",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    specific = ReviewFinding(
        id="specific",
        file_path="src/x.py",
        line_start=20,
        line_end=24,
        content="class Handler: the selected value is skipped before aggregation.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Handle the selected value before aggregating results.",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
    )
    audit = SemanticEquivalenceAuditOutput(
        items=[
            SemanticEquivalenceAuditItem(
                finding_id="broad",
                equivalent_to="specific",
                verdict="same_issue",
                rationale="Same root cause, behavior, and fix.",
            )
        ]
    )

    findings, duplicates = _apply_semantic_equivalence_audit([broad, specific], audit)

    assert [finding.id for finding in findings] == ["specific"]
    assert duplicates == {"specific": ["broad"]}


def test_cleanup_claim_cluster_merges_variants_and_rejects_incoherent() -> None:
    keeper = ReviewFinding(
        id="keeper",
        file_path="src/x.py",
        line_start=10,
        line_end=20,
        content="class Handler: batch mode drops a tuple field.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Preserve every tuple field.",
        behavioral_symptom="data_loss",
        root_operation="aggregation",
        claim_digest="src/x.py::handler::variant=batch::contract=cardinality+preservation::impact=data_loss",
    )
    duplicate = keeper.model_copy(
        update={
            "id": "duplicate",
            "content": "class Handler: batch mode keeps only one tuple slot.",
            "behavioral_symptom": "data_loss",
        }
    )
    incoherent = keeper.model_copy(
        update={
            "id": "incoherent",
            "content": "class Handler: batch mode is maybe fine.",
        }
    )
    distinct = keeper.model_copy(
        update={
            "id": "distinct",
            "content": "class Handler: empty mode falls through.",
            "behavioral_symptom": "missing_return",
            "root_operation": "dispatch",
            "claim_digest": "src/x.py::handler::variant=empty::contract=dispatch::impact=missing_return",
        }
    )
    audit = SemanticClaimClusterOutput(
        clusters=[
            SemanticClaimClusterDecision(
                cluster_id="cluster-1",
                distinct_ids=["distinct"],
                duplicate_groups=[
                    SemanticClaimDuplicateGroup(
                        keeper_id="keeper",
                        absorbed_ids=["duplicate"],
                        rejected_ids=["incoherent"],
                        merged_contract="Batch mode should preserve tuple cardinality.",
                        merged_counterexample="A two-field tuple loses the second field.",
                        merged_impact="data loss",
                        rationale="Duplicate is same root claim; incoherent variant contradicts itself.",
                    )
                ],
                rationale="Duplicate is same root claim; incoherent variant contradicts itself.",
            )
        ]
    )

    findings, duplicates, duplicate_to_keeper, rejected = _apply_semantic_claim_cluster_audit(
        [keeper, duplicate, incoherent, distinct],
        audit,
    )

    assert {finding.id for finding in findings} == {"keeper", "distinct"}
    assert duplicates == {"keeper": ["duplicate"]}
    assert duplicate_to_keeper == {"duplicate": "keeper"}
    assert rejected == {"incoherent": "Duplicate is same root claim; incoherent variant contradicts itself."}
    kept = next(finding for finding in findings if finding.id == "keeper")
    assert "tuple cardinality" in kept.evidence_for_contract


def test_cleanup_claim_cluster_handles_multiple_duplicate_groups_independently() -> None:
    first = ReviewFinding(
        id="first",
        file_path="src/x.py",
        line_start=10,
        line_end=20,
        content="class Handler: batch mode drops a tuple field.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Preserve every tuple field.",
        behavioral_symptom="data_loss",
        root_operation="aggregation",
        claim_digest="model-supplied-noise",
    )
    first_dup = first.model_copy(update={"id": "first-dup", "content": "class Handler: batch mode omits tuple slots."})
    second = first.model_copy(
        update={
            "id": "second",
            "content": "class Handler: empty mode falls through.",
            "behavioral_symptom": "missing_return",
            "root_operation": "dispatch",
        }
    )
    second_dup = second.model_copy(update={"id": "second-dup", "content": "class Handler: empty mode has a missing return."})
    audit = SemanticClaimClusterOutput(
        clusters=[
            SemanticClaimClusterDecision(
                cluster_id="cluster-1",
                duplicate_groups=[
                    SemanticClaimDuplicateGroup(keeper_id="first", absorbed_ids=["first-dup"]),
                    SemanticClaimDuplicateGroup(keeper_id="second", absorbed_ids=["second-dup"]),
                ],
            )
        ]
    )

    findings, duplicates, duplicate_to_keeper, rejected = _apply_semantic_claim_cluster_audit(
        [first, first_dup, second, second_dup],
        audit,
    )

    assert {finding.id for finding in findings} == {"first", "second"}
    assert duplicates == {"first": ["first-dup"], "second": ["second-dup"]}
    assert duplicate_to_keeper == {"first-dup": "first", "second-dup": "second"}
    assert rejected == {}
    assert all("model-supplied-noise" not in finding.claim_digest for finding in findings)


def test_cleanup_claim_cluster_refuses_incompatible_merge_suggestion() -> None:
    data_loss = ReviewFinding(
        id="data-loss",
        file_path="src/x.py",
        line_start=10,
        line_end=20,
        content="class Handler: batch mode drops a tuple field.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Preserve every tuple field.",
        behavioral_symptom="data_loss",
        root_operation="aggregation",
    )
    missing_return = data_loss.model_copy(
        update={
            "id": "missing-return",
            "line_start": 200,
            "line_end": 210,
            "content": "class Handler: empty mode falls through.",
            "behavioral_symptom": "missing_return",
            "root_operation": "dispatch",
        }
    )
    audit = SemanticClaimClusterOutput(
        clusters=[
            SemanticClaimClusterDecision(
                cluster_id="cluster-1",
                duplicate_groups=[
                    SemanticClaimDuplicateGroup(keeper_id="data-loss", absorbed_ids=["missing-return"]),
                ],
            )
        ]
    )

    findings, duplicates, duplicate_to_keeper, rejected = _apply_semantic_claim_cluster_audit(
        [data_loss, missing_return],
        audit,
    )

    assert {finding.id for finding in findings} == {"data-loss", "missing-return"}
    assert duplicates == {}
    assert duplicate_to_keeper == {}
    assert rejected == {"missing-return": "claim_cluster_incompatible_merge"}


def test_cleanup_claim_cluster_preserves_distinct_dimensions(monkeypatch) -> None:
    node = make_adversarial_cleanup_node()
    indexing = _cand(
        candidate_id="indexing",
        file_path="src/x.py",
        line_start=10,
        line_end=20,
        content="class Handler: selected slot is dropped.",
        failure_mode="Slot selection loses data.",
        evidence_summary="The changed path keeps only one slot.",
        recommendation="Preserve the selected slot.",
        behavioral_symptom="data_loss",
        root_operation="indexing",
    )
    aggregation = _cand(
        candidate_id="aggregation",
        file_path="src/x.py",
        line_start=18,
        line_end=28,
        content="class Handler: absent value can reach aggregation.",
        failure_mode="Aggregation can receive an absent value.",
        evidence_summary="The changed path aggregates an absent value.",
        recommendation="Normalize absent values before aggregation.",
        behavioral_symptom="crash",
        root_operation="aggregation",
    )
    audit = SemanticClaimClusterOutput(
        clusters=[
            SemanticClaimClusterDecision(
                cluster_id="cluster-1",
                distinct_ids=["aggregation"],
                rationale="Same surface but different dimensions and fixes.",
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.cleanup.Models.worker",
        lambda *_args, **_kwargs: type("FakeLLM", (), {"invoke": lambda self, _prompt: {"parsed": audit}})(),
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [indexing, aggregation],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=indexing.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="Supported.",
                ),
                ReflectionReport(
                    candidate_id=aggregation.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="Supported.",
                ),
            ],
            "metadata": {},
        }
    )

    assert {finding.id for finding in out["findings"]} == {"indexing", "aggregation"}
    meta = out["metadata"]["adversarial_cleanup"]
    assert "semantic_claim_cluster_duplicates" not in meta
    assert meta["semantic_claim_cluster_audits"][0]["distinct_ids"] == ["aggregation"]


def test_semantic_claim_clusters_include_same_root_sibling_owners() -> None:
    match = ReviewFinding(
        id="regex-match-redos",
        file_path="src/nodes.py",
        line_start=100,
        line_end=130,
        content="RegexMatch.execute runs user regex without timeout validation.",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add timeout or complexity validation.",
        expected_behavior="User regex execution should be bounded.",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
        claim_digest="src/nodes.py::regexmatch_execute::variant=regex::contract=resource::impact=unbounded_work",
    )
    extract = match.model_copy(
        update={
            "id": "regex-extract-redos",
            "line_start": 150,
            "line_end": 190,
            "content": "RegexExtract.execute runs user regex without timeout validation.",
            "claim_digest": "src/nodes.py::regexextract_execute::variant=regex::contract=resource+representation::impact=missing_return",
        }
    )

    clusters = _semantic_claim_clusters([match, extract], {})

    assert clusters
    assert {member["id"] for member in clusters[0]["members"]} == {
        "regex-match-redos",
        "regex-extract-redos",
    }


def test_cleanup_claim_cluster_can_merge_same_root_sibling_owners() -> None:
    match = ReviewFinding(
        id="regex-match-redos",
        file_path="src/nodes.py",
        line_start=100,
        line_end=130,
        content="RegexMatch.execute runs user regex without timeout validation.",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add timeout or complexity validation.",
        expected_behavior="User regex execution should be bounded.",
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
        claim_digest="src/nodes.py::regexmatch_execute::variant=regex::contract=resource::impact=unbounded_work",
    )
    extract = match.model_copy(
        update={
            "id": "regex-extract-redos",
            "line_start": 150,
            "line_end": 190,
            "content": "RegexExtract.execute runs user regex without timeout validation.",
            "claim_digest": "src/nodes.py::regexextract_execute::variant=regex::contract=resource+representation::impact=missing_return",
        }
    )
    audit = SemanticClaimClusterOutput(
        clusters=[
            SemanticClaimClusterDecision(
                cluster_id="cluster-1",
                duplicate_groups=[
                    SemanticClaimDuplicateGroup(
                        keeper_id="regex-match-redos",
                        absorbed_ids=["regex-extract-redos"],
                        rationale="Same root regex resource contract and mitigation.",
                    )
                ],
            )
        ]
    )

    findings, duplicates, duplicate_to_keeper, rejected = _apply_semantic_claim_cluster_audit(
        [match, extract],
        audit,
    )

    assert [finding.id for finding in findings] == ["regex-match-redos"]
    assert duplicates == {"regex-match-redos": ["regex-extract-redos"]}
    assert duplicate_to_keeper == {"regex-extract-redos": "regex-match-redos"}
    assert rejected == {}


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


def test_cleanup_keeps_source_local_fallthrough_when_ast_does_not_prove_all_paths_exit() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="branch-false",
        patch_task_id="logic-task",
        file_path="src/nodes.py",
        line_start=1,
        line_end=20,
        content="class StringCompare():",
        failure_mode="The 'Ends With' branch lacks a return statement.",
        evidence_summary="'Ends With' branch has no return.",
        recommendation="Add return to the 'Ends With' branch.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
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
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-task": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/nodes.py": "\n".join(
                                        [
                                            "def execute(mode):",
                                            "    if mode == 'Equal':",
                                            "        return True",
                                            "    elif mode == 'Ends With':",
                                            "        return False",
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
    assert [finding.id for finding in out["findings"]] == [cand.candidate_id]


def test_cleanup_drops_mode_return_claim_variants_contradicted_by_evidence() -> None:
    variants = [
        (
            "mode-equals",
            "StringCompare.execute mode == 'Ends With': no return statement.",
            "mode == 'Ends With' falls through to implicit None.",
        ),
        (
            "quoted-no-return",
            "StringCompare.execute has a defect in 'Ends With': no return statement.",
            "'Ends With': no return statement.",
        ),
        (
            "does-not-return",
            "StringCompare.execute 'Ends With' branch does not return a value.",
            "The 'Ends With' branch does not return.",
        ),
    ]
    for cid, content, evidence in variants:
        node = make_adversarial_cleanup_node()
        cand = _cand(
            candidate_id=cid,
            patch_task_id="logic-task",
            file_path="src/nodes.py",
            line_start=1,
            line_end=20,
            content=content,
            failure_mode="missing_return",
            evidence_summary=evidence,
            recommendation="Add return to the handled branch.",
            behavioral_symptom="missing_return",
            root_operation="dispatch",
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
                "metadata": {
                    "critique_pipeline": {
                        "by_task": {
                            "logic-task": {
                                "task_evidence": {
                                    "file_contents": {
                                        "src/nodes.py": "\n".join(
                                            [
                                                "def execute(mode):",
                                                "    if mode == 'Equal':",
                                                "        return True",
                                                "    elif mode == 'Ends With':",
                                                "        return False",
                                                "    else:",
                                                "        return False",
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
            == "branch_return_claim_contradicted_by_code_evidence"
        )


def test_cleanup_preserves_concrete_behavioral_missing_test_with_source_evidence() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="missing-test-data-loss",
        patch_task_id="logic-task",
        file_path="src/extract.py",
        line_start=1,
        line_end=5,
        content="extract_values loses non-first tuple fields.",
        claim_type="missing_test",
        failure_mode="Data loss: tuple results keep only the first field.",
        evidence_summary="The changed function joins m[0] for every tuple match.",
        recommendation="Preserve all tuple fields or narrow the extraction contract.",
        behavioral_symptom="data_loss",
        root_operation="aggregation",
        severity="medium",
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
                    rationale="The source-local behavior drops tuple fields.",
                )
            ],
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-task": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/extract.py": (
                                        "def extract_values(matches):\n"
                                        "    return ','.join([m[0] for m in matches])\n"
                                    )
                                }
                            }
                        }
                    }
                }
            },
        }
    )

    assert [finding.id for finding in out["findings"]] == [cand.candidate_id]


def test_cleanup_raw_reject_with_visible_contradiction_beats_consolidated_accept() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="conflicted-branch",
        patch_task_id="logic-task",
        file_path="src/nodes.py",
        line_start=1,
        line_end=20,
        content="StringCompare.execute violates the boolean return contract.",
        failure_mode="missing_return",
        evidence_summary="A handled comparison mode is reported as falling through.",
        recommendation="Add return to the handled branch.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
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
                    rationale=(
                        "The candidate is a false positive: it claims mode == 'Ends With' "
                        "has no return, but code shows that branch returns."
                    ),
                ),
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="The handled mode does not return.",
                ),
            ],
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-task": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/nodes.py": "\n".join(
                                        [
                                            "def execute(mode):",
                                            "    if mode == 'Equal':",
                                            "        return True",
                                            "    elif mode == 'Ends With':",
                                            "        return False",
                                            "    else:",
                                            "        return False",
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
        == "raw_reflection_reject_contradicted_by_code_evidence"
    )


def test_cleanup_keeps_terminal_fallback_return_claim() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="terminal-missing",
        patch_task_id="logic-task",
        file_path="src/nodes.py",
        line_start=1,
        line_end=20,
        content="class StringCompare():",
        failure_mode="Missing terminal else: unexpected mode falls through.",
        evidence_summary="All visible branches return, but no fallback handles unexpected mode.",
        recommendation="Add a terminal else for unexpected mode values.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
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
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-task": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/nodes.py": "\n".join(
                                        [
                                            "def execute(mode):",
                                            "    if mode == 'Equal':",
                                            "        return True",
                                            "    elif mode == 'Ends With':",
                                            "        return False",
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
    assert [finding.id for finding in out["findings"]] == [cand.candidate_id]


def test_cleanup_drops_incomplete_branch_claim_when_body_is_visible() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="truncated-branch",
        patch_task_id="logic-regex",
        file_path="src/nodes.py",
        line_start=1,
        line_end=20,
        content="RegexExtract.execute 'All Groups' mode is incomplete in the diff.",
        failure_mode="Incomplete code: 'All Groups' branch lacks implementation.",
        evidence_summary="The diff is truncated after the branch header.",
        recommendation="Complete the 'All Groups' branch implementation.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="needs_verification",
                    rationale="Needs full context because the diff is truncated.",
                )
            ],
            "focused_context_results": {
                "r1": {
                    "request_id": "r1",
                    "candidate_id": cand.candidate_id,
                    "file_snippets": {"src/nodes.py": "All Groups body visible"},
                }
            },
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-regex": {
                            "task_evidence": {
                                "file_contents": {
                                    "src/nodes.py": "\n".join(
                                        [
                                            "def execute(mode):",
                                            "    if mode == 'All Matches':",
                                            "        result = ''",
                                            "    elif mode == 'All Groups':",
                                            "        results = []",
                                            "        results.append('x')",
                                            "        result = '\\n'.join(results)",
                                            "    return result,",
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
        == "incomplete_claim_contradicted_by_code_evidence"
    )


def test_cleanup_drops_incomplete_branch_claim_when_focused_context_has_body() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="general_registration_003",
        patch_task_id="logic-regex",
        file_path="src/nodes.py",
        line_start=1,
        line_end=20,
        content="RegexExtract.execute has incomplete code for 'All Groups' mode - the branch is cut off in the provided evidence.",
        failure_mode="missing_return",
        evidence_summary="The code evidence shows the 'All Groups' branch starts but is truncated.",
        recommendation="Verify the complete implementation of 'All Groups' mode returns the expected value.",
        behavioral_symptom="crash",
        root_operation="dispatch",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="needs_verification",
                    rationale="The branch is cut off in the provided evidence and needs verification.",
                )
            ],
            "focused_context_results": {
                "r1": {
                    "request_id": "r1",
                    "candidate_id": cand.candidate_id,
                    "file_contents_full": {
                        "src/nodes.py": "\n".join(
                            [
                                "def execute(mode):",
                                "    if mode == 'All Matches':",
                                "        result = ''",
                                "    elif mode == 'All Groups':",
                                "        matches = []",
                                "        results = []",
                                "        for match in matches:",
                                "            results.append(match)",
                                "        result = '\\n'.join(results)",
                                "    else:",
                                "        result = ''",
                                "    return result,",
                            ]
                        )
                    },
                }
            },
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-regex": {
                            "task_evidence": {
                                "file_contents": {"src/nodes.py": "elif mode == 'All Groups':\n"}
                            }
                        }
                    }
                },
                "verifier_hints": {
                    cand.candidate_id: {
                        "verdict": "refuted",
                        "harness_error": True,
                        "product_verified": False,
                    }
                },
            },
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "incomplete_claim_contradicted_by_code_evidence"
    )


def test_cleanup_uses_rendered_units_when_file_contents_are_malformed() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="rendered-unit-body",
        patch_task_id="logic-regex",
        file_path="src/nodes.py",
        line_start=10,
        line_end=20,
        content="Handler.execute is incomplete in the provided evidence; 'All Groups' is cut off.",
        failure_mode="Incomplete code: 'All Groups' branch lacks implementation.",
        evidence_summary="The stored file evidence is truncated after the branch header.",
        recommendation="Complete the 'All Groups' branch implementation.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="needs_verification",
                    rationale="Needs complete body.",
                )
            ],
            "metadata": {
                "critique_pipeline": {
                    "by_task": {
                        "logic-regex": {
                            "task_evidence": {
                                "file_contents": {"src/nodes.py": "Groups\":\n"},
                                "rendered_units": {
                                    "src/nodes.py": "\n".join(
                                        [
                                            "--- src/nodes.py: class Handler (L1-L20) ---",
                                            "class Handler:",
                                            "    def execute(self, mode):",
                                            "        if mode == 'All Groups':",
                                            "            results = []",
                                            "            results.append('x')",
                                            "            result = '\\n'.join(results)",
                                            "        return result,",
                                        ]
                                    )
                                },
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
        == "incomplete_claim_contradicted_by_code_evidence"
    )


def test_revision_evidence_for_same_candidate_is_appended_without_text_classification(monkeypatch) -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="agg",
        file_path="src/nodes.py",
        content="class RegexExtract(): optional capture may reach join().",
        failure_mode="Aggregation safety: absent capture values can enter string joining.",
        evidence_summary="Optional captures are appended before join().",
        recommendation="Normalize absent capture values before joining.",
        behavioral_symptom="crash",
        root_operation="aggregation",
    )

    audit = RevisionSupportAuditOutput(
        verdict="resolved",
        rationale="Focused evidence and revision identify the tuple-row aggregation loss.",
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.cleanup.Models.worker",
        lambda *_args, **_kwargs: type("FakeLLM", (), {"invoke": lambda self, _prompt: {"parsed": audit}})(),
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="needs_verification",
                    rationale="Needs proof.",
                )
            ],
            "focused_context_results": {
                "r1": {
                    "request_id": "r1",
                    "candidate_id": cand.candidate_id,
                    "file_snippets": {"src/nodes.py": "join path"},
                }
            },
            "metadata": {
                "critique_revision": {
                    "revisions": [
                        {
                            "candidate_id": cand.candidate_id,
                            "verdict": "accept",
                            "updated_evidence_summary": "All Matches uses findall and keeps only m[0] from tuple rows.",
                        }
                    ]
                }
            },
        }
    )
    assert len(out["findings"]) == 1
    assert "Post-context evidence" in out["findings"][0].content


def test_cleanup_drops_off_domain_redirect_without_independent_support() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="perf-redirect",
        file_path="src/nodes.py",
        content="class RegexExtract(): external request path",
        failure_mode="Repeated compilation overhead.",
        evidence_summary="Pattern compilation happens on every call.",
        recommendation="Cache compiled patterns.",
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
                    verdict="not_applicable",
                    reclassified_category="performance",
                    rationale="This is a performance optimization concern, not logic.",
                )
            ],
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "off_domain_redirect_without_independent_support"
    )


def test_cleanup_drops_language_defined_behavior_without_contract() -> None:
    node = make_adversarial_cleanup_node()
    cand = _cand(
        candidate_id="slice-pref",
        file_path="src/nodes.py",
        content="StringSubstring.execute allows negative indices.",
        failure_mode="Negative indices may produce unexpected behavior.",
        evidence_summary="Python slicing accepts out-of-range indexes.",
        recommendation="Clamp indexes or document the behavior.",
        behavioral_symptom="wrong_output",
        root_operation="indexing",
    )

    out = node(
        {
            "run_id": "t",
            "candidate_findings": [cand],
            "reflection_reports": [
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="not_applicable",
                    rationale="Python string slicing behavior is well-defined and documented behavior, not a defect.",
                ),
                ReflectionReport(
                    candidate_id=cand.candidate_id,
                    reflector_specialty="logic",
                    verdict="accept",
                    rationale="Could be unexpected to users.",
                ),
            ],
            "metadata": {},
        }
    )
    assert out["findings"] == []
    assert (
        out["metadata"]["adversarial_cleanup"]["candidate_lifecycle"][cand.candidate_id]["reason"]
        == "language_defined_behavior_without_project_contract"
    )


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
        behavioral_symptom="missing_return",
        root_operation="dispatch",
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
        behavioral_symptom="unbounded_work",
        root_operation="resource_use",
    )

    kept, duplicates = dedupe_review_findings_by_signature([incomplete, resource])
    assert {finding.id for finding in kept} == {"logic-incomplete", "resource-use"}
    assert duplicates == {}


def test_final_dedupe_merges_missing_return_root_variants() -> None:
    branch = ReviewFinding(
        id="branch",
        file_path="src/x.py",
        line_start=1,
        line_end=20,
        content="class Handler():",
        severity="high",
        feedback_type="defect_detection",
        recommendation="Add return to handled branch.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
    )
    fallback = ReviewFinding(
        id="fallback",
        file_path="src/x.py",
        line_start=1,
        line_end=20,
        content="class Handler lacks a terminal else for unexpected mode values.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Add a terminal else fallback for unhandled mode.",
        behavioral_symptom="missing_return",
        root_operation="dispatch",
    )

    kept, duplicates = dedupe_review_findings_by_signature([branch, fallback])
    assert [finding.id for finding in kept] == ["fallback"]
    assert duplicates == {"fallback": ["branch"]}


def test_synthesizer_preserves_adjudicated_resolution_like_finding() -> None:
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
    assert [item.id for item in out["final_findings"]] == ["x"]
    assert out["metadata"]["review_synthesizer"]["dropped_resolution_only_ids"] == []


def test_synthesizer_preserves_duplicate_findings_with_unique_ids() -> None:
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
    assert dupes == {}
    assert [item.id for item in out["final_findings"]] == ["x", "x__2"]
    assert out["metadata"]["review_synthesizer"]["lost_promoted_candidate_ids"] == []


def test_synthesizer_reconciles_adjudicator_duplicates_globally(monkeypatch) -> None:
    keeper = ReviewFinding(
        id="keeper",
        file_path="src/x.py",
        line_start=10,
        line_end=20,
        content="Handler batch mode drops a tuple field.",
        severity="medium",
        feedback_type="defect_detection",
        recommendation="Preserve every tuple field.",
        behavioral_symptom="data_loss",
        root_operation="aggregation",
        claim_digest="src/x.py::handler::variant=batch::contract=cardinality::impact=data_loss",
    )
    duplicate = keeper.model_copy(
        update={
            "id": "duplicate",
            "content": "Handler batch mode keeps only one tuple slot.",
        }
    )
    audit = SemanticClaimClusterOutput(
        clusters=[
            SemanticClaimClusterDecision(
                cluster_id="cluster-1",
                duplicate_groups=[
                    SemanticClaimDuplicateGroup(
                        keeper_id="keeper",
                        absorbed_ids=["duplicate"],
                        rationale="Same contract, trigger, operation, and impact.",
                    )
                ],
            )
        ]
    )
    monkeypatch.setattr(
        "src.orchestration.nodes.application.synthesizer.Models.worker",
        lambda *_args, **_kwargs: type("FakeLLM", (), {"invoke": lambda self, _prompt: {"parsed": audit}})(),
    )

    out = synthesizer_node(
        {
            "findings": [keeper, duplicate],
            "metadata": {
                "review_adjudicator": {
                    "candidate_lifecycle": {
                        "keeper": {"decision": "promoted"},
                        "duplicate": {"decision": "promoted"},
                    }
                }
            },
        }
    )

    assert [finding.id for finding in out["final_findings"]] == ["keeper"]
    meta = out["metadata"]["review_synthesizer"]
    assert meta["semantic_dedupe_duplicates"] == {"keeper": ["duplicate"]}
    assert meta["dropped_duplicate_ids"] == ["duplicate"]
    assert meta["claim_cluster_reconciliation"]["claim_cluster_duplicate_to_keeper"] == {"duplicate": "keeper"}
    assert meta["recall_audit"]["duplicate_equivalents"] == {"duplicate": "keeper"}


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


def test_synthesizer_canonicalizes_duplicate_cycles_to_final_keeper() -> None:
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
                        "middle": {"decision": "promoted"},
                        "dropped": {"decision": "promoted"},
                    },
                    "semantic_dedupe_duplicates": {"middle": ["dropped"]},
                    "semantic_dedupe_finding_duplicates": {"keeper": ["middle"], "middle": ["keeper"]},
                }
            },
        }
    )

    meta = out["metadata"]["review_synthesizer"]
    assert meta["lost_promoted_candidate_ids"] == []
    assert meta["recall_audit"]["duplicate_equivalents"] == {
        "dropped": "keeper",
        "middle": "keeper",
    }

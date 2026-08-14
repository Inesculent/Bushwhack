from src.orchestration.prompts.renderer import load_reviewer_prompt, render_reviewer_prompt


def test_reviewer_prompt_files_exist_for_all_roles():
    prompt_paths = [
        "global.md",
        "planner.md",
        "synthesizer.md",
        "critiquer.md",
        "cleanup.md",
        "review_check_executor.md",
        "review_evidence_triage.md",
        "critique_revision.md",
        "critique_revision_digest.md",
        "reflection/security.md",
        "reflection/logic.md",
        "reflection/performance.md",
        "reflection/general.md",
        "workers/security.md",
        "workers/logic.md",
        "workers/performance.md",
        "workers/general.md",
        "mental_model/intent_extractor.md",
        "mental_model/mandate_synthesizer.md",
        "mental_model/owner_contract_questions.md",
        "mental_model/mandate_explorer.md",
        "mental_model/mandate_patch.md",
        "mental_model/joint_plan_critic.md",
        "mental_model/plan_revision.md",
    ]

    for prompt_path in prompt_paths:
        assert load_reviewer_prompt(prompt_path)


def test_global_prompt_declared_input_contracts() -> None:
    text = load_reviewer_prompt("global.md")
    assert "Declared input contracts" in text
    assert "required and non-optional" in text
    assert "Optional" in text or "optional" in text
    assert "Changed behavior contracts" in text
    assert "Contract delta: what promise changed?" in text
    assert "Shape/cardinality: are all intended items" in text
    assert "Ownership/lifecycle: is every acquired resource released" in text
    assert "Mode/variant completeness" in text
    assert "## In-function contracts" not in text


def test_planner_prompt_requires_diff_local_correctness_baseline() -> None:
    text = load_reviewer_prompt("planner.md")
    assert "diff-local" in text.lower()
    assert "general correctness" in text.lower()


def test_all_active_planning_prompts_share_compact_task_budget() -> None:
    for prompt_path in (
        "planner.md",
        "mental_model/plan_revision.md",
        "mental_model/joint_plan_critic.md",
        "mental_model/plan_critic.md",
    ):
        text = load_reviewer_prompt(prompt_path).lower()
        assert "10 tasks" in text
        assert "context" in text


def test_critiquer_prompt_contains_routing_hardcap() -> None:
    text = load_reviewer_prompt("critiquer.md")
    assert "Single-Specialty Hardcap" in text
    assert "Hierarchy of Needs" in text
    assert "Contract Claim Discipline" in text
    assert "Contract before issue class" in text
    assert "Changed behavior contracts" in load_reviewer_prompt("global.md")
    assert "Review KB context" in text


def test_critiquer_prompt_uses_evidence_gated_broad_dimensions() -> None:
    text = load_reviewer_prompt("critiquer.md")
    assert "Evidence-gated scope" in text
    assert "Do not turn the broad dimension list into a generic audit" in text
    assert "Contract-lens pass" in text
    assert "evidence_for_contract" in text
    assert "counterexample" in text
    assert "rejection_check" in text
    assert "claim_type: uncertain" in text
    assert "Diversity before depth on broad tasks" in text
    assert "api/signature compatibility" in text
    assert "dependency/import availability" in text
    assert "state/cache lifecycle" in text
    assert "concurrency/shared-state safety" in text
    assert "security/input boundary" in text


def test_logic_reflection_prompt_cuts_down_broad_uncertain_leads() -> None:
    text = load_reviewer_prompt("reflection/logic.md")
    assert "Changed behavior contracts" in text
    assert "Control-flow is one correctness family" in text
    assert "Cut down lead noise" in text
    assert "promising `uncertain` leads" in text
    assert "reject` generic speculation" in text


def test_active_reviewer_prompts_expose_repository_kb_authority() -> None:
    assert "Repository KB Authority" in load_reviewer_prompt("global.md")
    assert "Repository KB summaries" in load_reviewer_prompt("planner.md")
    assert "Repository KB context" in load_reviewer_prompt("reflection/logic.md")


def test_reflection_prompts_contain_adversarial_two_tier_protocol() -> None:
    for rel in (
        "reflection/security.md",
        "reflection/logic.md",
        "reflection/performance.md",
        "reflection/general.md",
    ):
        text = load_reviewer_prompt(rel)
        assert "ADVERSARIAL REVIEW" in text
        assert "Two-Tier" in text
        assert "Invisible safeguard" in text or "invisible" in text.lower()


def test_mental_model_prompts_guard_data_integrity_and_branch_exhaustiveness() -> None:
    mandate = load_reviewer_prompt("mental_model/mandate_synthesizer.md")
    critic = load_reviewer_prompt("mental_model/joint_plan_critic.md")
    revision = load_reviewer_prompt("mental_model/plan_revision.md")
    explorer = load_reviewer_prompt("mental_model/mandate_explorer.md")

    assert "not asserted bugs" in mandate.lower() or "hypotheses" in mandate.lower()
    assert "aligned=true" in critic.lower()
    assert "exploration_requests" in critic.lower()
    assert "full replacement review plan" in revision.lower()
    assert "bootstrap" in explorer.lower()


_BENCHMARK_SPECIFIC_TOKENS = (
    "nodes_string",
    "RegexExtract",
    "StringCompare",
    "ComfyUI",
)
_ACTIVE_ISSUE_CLASS_TOKENS = (
    "structured extraction",
    "structured extraction and aggregation",
    "match tuples",
    "capture groups",
)
_NAMED_VULNERABILITY_TOKENS = (
    "ReDoS",
    "redos",
    "catastrophic backtracking",
    "regular expression denial",
)


def test_planner_prompt_has_no_benchmark_specific_examples() -> None:
    text = load_reviewer_prompt("planner.md")
    for token in _BENCHMARK_SPECIFIC_TOKENS:
        assert token not in text


def test_intent_extractor_references_pr_context_and_inventory() -> None:
    text = load_reviewer_prompt("mental_model/intent_extractor.md")
    assert "PR context" in text
    assert "Surfaces introduced in diff" in text


def test_joint_plan_critic_scope_completeness_without_benchmark_tokens() -> None:
    text = load_reviewer_prompt("mental_model/joint_plan_critic.md")
    assert "scope completeness" in text.lower() or "Scope completeness" in text
    for token in _BENCHMARK_SPECIFIC_TOKENS:
        assert token not in text


def test_mandate_synthesizer_has_no_regex_specific_examples() -> None:
    text = load_reviewer_prompt("mental_model/mandate_synthesizer.md").lower()
    assert "regex" not in text


def test_active_reviewer_prompts_avoid_named_vulnerability_anchors() -> None:
    prompt_paths = [
        "critiquer.md",
        "workers/security.md",
        "workers/logic.md",
        "workers/performance.md",
        "reflection/security.md",
        "reflection/logic.md",
        "critique_revision.md",
        "mental_model/joint_plan_critic.md",
        "mental_model/plan_critic.md",
    ]
    for prompt_path in prompt_paths:
        text = load_reviewer_prompt(prompt_path)
        for token in _BENCHMARK_SPECIFIC_TOKENS + _NAMED_VULNERABILITY_TOKENS:
            assert token not in text


def test_active_reviewer_prompts_avoid_framework_specific_schema_anchors() -> None:
    prompt_paths = [
        "critiquer.md",
        "reflection/logic.md",
        "critique_revision.md",
    ]
    for prompt_path in prompt_paths:
        text = load_reviewer_prompt(prompt_path)
        assert "COMBO" not in text
        assert "INPUT_TYPES" not in text


def test_planning_prompts_gate_general_practice_as_questions() -> None:
    planner = load_reviewer_prompt("planner.md")
    compiler = load_reviewer_prompt("review_check_compiler.md")
    executor = load_reviewer_prompt("review_check_executor.md")

    assert "General practice may suggest questions" in planner
    assert "not by itself a task-worthy defect hypothesis" in planner
    assert "General practice is a source of questions only" in compiler
    assert "contract, trigger, operation, and impact" in compiler
    assert "compact contract packet" in executor
    assert "It is acceptable to omit an undecidable check" in executor


def test_action_contract_prompts_preserve_value_flow_focus() -> None:
    synthesizer = load_reviewer_prompt("mental_model/mandate_synthesizer.md")
    patch = load_reviewer_prompt("mental_model/mandate_patch.md")
    executor = load_reviewer_prompt("review_check_executor.md")

    assert "expected_behavior` as an action contract" in synthesizer
    assert "expected_behavior` as an action contract" in patch
    assert "produced, selected/transformed" in synthesizer
    assert "produced value, selected/transformed value" in patch
    assert "Treat `expected_behavior` as the action contract" in executor
    assert "use `unsupported`" in executor


def test_lens_driven_prompts_preserve_general_contract_discovery() -> None:
    compiler = load_reviewer_prompt("review_check_compiler.md")
    owner_questions = load_reviewer_prompt("mental_model/owner_contract_questions.md")
    adjudicator = load_reviewer_prompt("review_adjudicator.md")

    assert "question source, not a checklist" in compiler
    assert "One selected lens may produce multiple checks" in compiler
    assert "Preserve lens-card provenance" in compiler
    assert "Through each relevant review lens" in owner_questions
    assert "Use lenses to discover contracts" in owner_questions
    assert "Prefer concrete lens-backed behavioral defects" in adjudicator
    for text in (compiler, owner_questions, adjudicator):
        for token in _BENCHMARK_SPECIFIC_TOKENS + _NAMED_VULNERABILITY_TOKENS:
            assert token not in text


def test_high_volume_structured_prompts_include_output_budget() -> None:
    executor = load_reviewer_prompt("review_check_executor.md")
    triage = load_reviewer_prompt("review_evidence_triage.md")

    for text in (executor, triage):
        assert "Output budget" in text
        assert "Return only schema JSON" in text
        assert "Do not quote long" in text or "Do not repeat long" in text


def test_adjudicator_can_request_bounded_runtime_evidence() -> None:
    text = load_reviewer_prompt("review_adjudicator.md")
    assert "`verify`" in text
    assert "no verifier report is already present" in text
    assert "repository intent" in text


def test_adjudicator_requires_authoritative_source_support() -> None:
    text = load_reviewer_prompt("review_adjudicator.md")
    assert "evidence_card.source_lines" in text
    assert "inconclusive verifier report is neutral" in text
    assert "Missing test coverage alone is not a product defect" in text
    assert "statement that source is truncated" in text


def test_active_planning_prompts_do_not_prescribe_issue_class_tasks() -> None:
    prompt_paths = [
        "planner.md",
        "mental_model/joint_plan_critic.md",
        "mental_model/plan_revision.md",
        "mental_model/mandate_synthesizer.md",
    ]
    for prompt_path in prompt_paths:
        text = load_reviewer_prompt(prompt_path).lower()
        for token in _ACTIVE_ISSUE_CLASS_TOKENS:
            assert token not in text


def test_renderer_combines_global_role_and_runtime_sections():
    rendered = render_reviewer_prompt(
        "workers/security.md",
        {
            "Assigned Task": "Review authentication behavior.",
            "Git Diff Excerpt": "diff --git a/app.py b/app.py",
        },
    )

    assert "Global Reviewer Rules" in rendered
    assert "Security Worker Instructions" in rendered
    assert "Review authentication behavior." in rendered
    assert "diff --git a/app.py b/app.py" in rendered

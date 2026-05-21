from src.orchestration.prompts.renderer import load_reviewer_prompt, render_reviewer_prompt


def test_reviewer_prompt_files_exist_for_all_roles():
    prompt_paths = [
        "global.md",
        "planner.md",
        "synthesizer.md",
        "critiquer.md",
        "cleanup.md",
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
        "mental_model/contract_inspector.md",
        "mental_model/historical_miner.md",
        "mental_model/mandate_synthesizer.md",
        "mental_model/plan_critic.md",
        "mental_model/plan_revision.md",
    ]

    for prompt_path in prompt_paths:
        assert load_reviewer_prompt(prompt_path)


def test_global_prompt_declared_input_contracts() -> None:
    text = load_reviewer_prompt("global.md")
    assert "Declared input contracts" in text
    assert "required and non-optional" in text
    assert "Optional" in text or "optional" in text


def test_planner_prompt_requires_diff_local_correctness_baseline() -> None:
    text = load_reviewer_prompt("planner.md")
    assert "diff-local" in text.lower()
    assert "general correctness" in text.lower()


def test_critiquer_prompt_contains_routing_hardcap() -> None:
    text = load_reviewer_prompt("critiquer.md")
    assert "Single-Specialty Hardcap" in text
    assert "Hierarchy of Needs" in text
    assert "Output budget" in text
    assert "at most 6" in text


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
    critic = load_reviewer_prompt("mental_model/plan_critic.md")
    revision = load_reviewer_prompt("mental_model/plan_revision.md")
    contract = load_reviewer_prompt("mental_model/contract_inspector.md")

    assert "not asserted bugs" in mandate.lower()
    assert "uncertainties explicit" in mandate.lower()
    assert "aligned=true" in critic.lower()
    assert "weak task specificity" in critic.lower()
    assert "full replacement review plan" in revision.lower()
    assert "do **not** invent unsupported paths" in contract.lower()


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

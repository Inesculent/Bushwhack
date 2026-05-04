from src.orchestration.prompts.renderer import load_reviewer_prompt, render_reviewer_prompt


def test_reviewer_prompt_files_exist_for_all_roles():
    prompt_paths = [
        "global.md",
        "planner.md",
        "synthesizer.md",
        "critiquer.md",
        "cleanup.md",
        "critique_revision.md",
        "reflection/security.md",
        "reflection/logic.md",
        "reflection/performance.md",
        "reflection/general.md",
        "workers/security.md",
        "workers/logic.md",
        "workers/performance.md",
        "workers/general.md",
    ]

    for prompt_path in prompt_paths:
        assert load_reviewer_prompt(prompt_path)


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

from src.orchestration.prompts.exploration_prompts import (
    render_explorer_prompt,
    render_unverified_call_resolver_prompt,
)
from src.orchestration.prompts.renderer import load_exploration_prompt


def test_exploration_prompt_files_exist() -> None:
    paths = [
        "explorer.md",
        "community_semantic.md",
        "unverified_call_resolver.md",
        "semantic_merge.md",
    ]
    for relative_path in paths:
        assert load_exploration_prompt(relative_path)


def test_exploration_renderers_substitute_placeholders() -> None:
    explorer = render_explorer_prompt(
        repo_path="/repo",
        user_goals="focus on auth",
        git_diff="diff --git a/x b/x",
    )
    assert "/repo" in explorer
    assert "focus on auth" in explorer
    assert "diff --git" in explorer

    resolver = render_unverified_call_resolver_prompt(
        symbol_node_id="sym:1",
        body_text="def f(): return 1",
    )
    assert "sym:1" in resolver
    assert "def f()" in resolver


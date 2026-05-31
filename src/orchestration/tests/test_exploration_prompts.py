from src.orchestration.prompts.exploration_prompts import (
    render_explorer_prompt,
    render_repository_kb_community_merge_prompt,
    render_repository_kb_community_distill_prompt,
    render_repository_kb_repo_distill_prompt,
    render_repository_kb_shard_distill_prompt,
    render_unverified_call_resolver_prompt,
)
from src.orchestration.prompts.renderer import load_exploration_prompt


def test_exploration_prompt_files_exist() -> None:
    paths = [
        "explorer.md",
        "community_semantic.md",
        "repository_kb_community_distill.md",
        "repository_kb_community_merge.md",
        "repository_kb_repo_distill.md",
        "repository_kb_shard_distill.md",
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
    assert "repository-understanding" in explorer

    resolver = render_unverified_call_resolver_prompt(
        symbol_node_id="sym:1",
        body_text="def f(): return 1",
    )
    community_distill = render_repository_kb_community_distill_prompt(
        pack_json='{"allowed_record_ids": ["community:1"]}',
    )
    repo_distill = render_repository_kb_repo_distill_prompt(
        pack_json='{"allowed_record_ids": ["repo"]}',
    )
    shard_distill = render_repository_kb_shard_distill_prompt(
        pack_json='{"shards": []}',
    )
    community_merge = render_repository_kb_community_merge_prompt(
        pack_json='{"records": []}',
    )
    assert "sym:1" in resolver
    assert "def f()" in resolver
    assert "dependency graph" in resolver
    assert "bounded Repository KB evidence" in community_distill
    assert "repository-understanding brief" in repo_distill
    assert "workflows" in repo_distill
    assert "domain concepts" in repo_distill
    assert "docs_context" in repo_distill
    assert "exact behavior" in repo_distill
    assert "evidence shards" in shard_distill
    assert "shard summaries" in community_merge

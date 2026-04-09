from src.config import Settings
from src.infrastructure.factory import build_repository_understanding_adapters
from src.infrastructure.sandbox import RepoSandbox


def test_build_repository_understanding_adapters_with_ast_disabled() -> None:
    settings = Settings(ast_mcp_enabled=False)

    # We only need an object with the sandbox interface expected by RipgrepSearcher.
    sandbox = RepoSandbox.__new__(RepoSandbox)

    adapters = build_repository_understanding_adapters(
        sandbox=sandbox,
        settings=settings,
    )

    assert adapters.searcher is not None
    assert adapters.ast_parser is None
    assert adapters.ast_enabled is False

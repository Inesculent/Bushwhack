from src.config import Settings
from src.domain.interfaces import IPreflightService
from src.infrastructure.factory import build_preflight_service, build_repository_understanding_adapters
from src.infrastructure.ast.native_parser import NativeASTParser
from src.infrastructure.sandbox import RepoSandbox


def test_build_repository_understanding_adapters_with_ast_disabled() -> None:
    settings = Settings(ast_enabled=False)

    # We only need an object with the sandbox interface expected by RipgrepSearcher.
    sandbox = RepoSandbox.__new__(RepoSandbox)

    adapters = build_repository_understanding_adapters(
        sandbox=sandbox,
        settings=settings,
    )

    assert adapters.searcher is not None
    assert adapters.ast_parser is None
    assert adapters.ast_enabled is False


def test_build_repository_understanding_adapters_uses_native_parser_by_default() -> None:
    settings = Settings(ast_enabled=True, ast_mcp_enabled=False)
    sandbox = RepoSandbox.__new__(RepoSandbox)

    adapters = build_repository_understanding_adapters(
        sandbox=sandbox,
        settings=settings,
    )

    assert isinstance(adapters.ast_parser, NativeASTParser)
    assert adapters.ast_enabled is True


def test_build_preflight_service_returns_domain_port() -> None:
    service = build_preflight_service()
    assert isinstance(service, IPreflightService)

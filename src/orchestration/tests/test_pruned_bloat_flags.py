from unittest.mock import patch

from src.config import Settings
from src.orchestration.review_principles import REVIEW_PRINCIPLES_VERSION, principles_for_specialty
from src.orchestration.reviewer_graph import build_graph


def test_review_principles_are_compact_and_versioned() -> None:
    text = principles_for_specialty("logic")

    assert REVIEW_PRINCIPLES_VERSION in text
    assert "Shoot first" not in text
    assert "terminal else branches" in text
    assert len(text) < 1200


def test_legacy_community_agent_not_built_by_default() -> None:
    settings = Settings(
        redis_enabled=False,
        semantic_legacy_community_agents_enabled=False,
    )

    with patch("src.orchestration.reviewer_graph.get_settings", return_value=settings), patch(
        "src.orchestration.reviewer_graph.make_community_semantic_agent_node"
    ) as make_agent:
        build_graph()

    make_agent.assert_not_called()

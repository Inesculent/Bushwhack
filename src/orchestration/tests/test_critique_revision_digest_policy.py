"""Tests for critique revision digest contradict policy."""

from __future__ import annotations

from src.domain.schemas import CritiqueRevisionDigest
from src.orchestration.nodes.application.critique_revision import _apply_digest_contradict_policy


def test_digest_contradicts_overrides_accept() -> None:
    digests = {
        "c1:0": CritiqueRevisionDigest(
            shard_id="c1:0",
            candidate_id="c1",
            request_ids=[],
            impact="contradicts",
            evidence_bullets=["code shows else branch exists"],
        ),
    }
    rows = [
        {
            "candidate_id": "c1",
            "verdict": "accept",
            "updated_evidence_summary": "confirmed missing return",
        }
    ]
    out, warnings = _apply_digest_contradict_policy(rows, digests)
    assert out[0]["verdict"] == "reject"
    assert any("digest_contradicts" in w for w in warnings)

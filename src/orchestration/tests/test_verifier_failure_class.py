"""Tests for scoped verifier refutation."""

from __future__ import annotations

from src.orchestration.nodes.verifier.failure_class import (
    failure_mode_class,
    verifier_refutation_applies,
)


def test_failure_mode_class_wrong_output() -> None:
    assert (
        failure_mode_class({"failure_mode": "Wrong output loses capturing groups"})
        == "wrong_output"
    )


def test_failure_mode_class_crash() -> None:
    assert failure_mode_class({"failure_mode": "Raises IndexError on bad index"}) == "crash"


def test_verifier_refutation_wrong_output_exit_zero_not_applicable() -> None:
    assert not verifier_refutation_applies(
        {"failure_mode": "data loss from wrong tuple slot"},
        verifier_verdict="refuted",
        verification_scope="concrete_behavior",
        harness_error=False,
    )


def test_verifier_refutation_crash_refuted_applies() -> None:
    assert verifier_refutation_applies(
        {"failure_mode": "IndexError when out of bounds"},
        verifier_verdict="refuted",
        verification_scope="concrete_behavior",
        harness_error=False,
        stdout="STATUS: SAFE",
    )

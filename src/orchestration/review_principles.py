"""Compact cross-cutting review principles injected into task context."""

REVIEW_PRINCIPLES_VERSION = "2026-05-31-pruned-v1"

# Appended to BehavioralSpec.reviewer_guidance and available for context probes.
DECLARED_INPUT_CONTRACT_GUIDANCE = (
    "Declared inputs: assume required, non-optional schema inputs are present. "
    "Do not report generic missing None/null/empty guards unless the diff or contract "
    "makes absence valid."
)

CHANGED_CODE_CAUSALITY_GUIDANCE = (
    "Changed-code causality: a finding must explain the concrete failure newly caused "
    "or exposed by this PR. Source/diff evidence outranks behavioral hypotheses, broad "
    "principles, and repository summaries."
)

CONCRETE_FAILURE_GUIDANCE = (
    "Concrete failure mode: missing tests, validation, terminal else branches, and broad "
    "defensive guards are findings only when tied to a specific changed behavior, project "
    "contract, data loss, crash, security boundary, or wrong output."
)

STRUCTURED_RESULT_GUIDANCE = (
    "Structured data checks: prioritize clear slot/index/branch-order/escaping/import "
    "asymmetries, overwritten accumulators, and aggregation bugs. Avoid checklist findings "
    "that merely ask for more guards."
)
IN_FUNCTION_CONTRACT_GUIDANCE = STRUCTURED_RESULT_GUIDANCE

SECURITY_REVIEW_GUIDANCE = (
    "Security: prioritize attacker-controlled input, authorization boundaries, injection, "
    "secret exposure, unsafe subprocess/eval/deserialization, and session/cookie handling."
)

PERFORMANCE_REVIEW_GUIDANCE = (
    "Performance: prioritize changed-code complexity, unbounded work, blocking hot paths, "
    "missing batching, and retry/timeout regressions. Do not flag style-only micro-optimizations."
)


def principles_for_specialty(specialty: str) -> str:
    """Bounded principle slice for critique probe/critiquer packets."""
    base = "\n".join(
        [
            f"Review principles version: {REVIEW_PRINCIPLES_VERSION}",
            DECLARED_INPUT_CONTRACT_GUIDANCE,
            CHANGED_CODE_CAUSALITY_GUIDANCE,
            CONCRETE_FAILURE_GUIDANCE,
        ]
    )
    spec = (specialty or "general").strip().lower()
    if spec == "logic":
        return f"{base}\n{STRUCTURED_RESULT_GUIDANCE}"
    if spec == "security":
        return f"{base}\n{SECURITY_REVIEW_GUIDANCE}"
    if spec == "performance":
        return f"{base}\n{PERFORMANCE_REVIEW_GUIDANCE}"
    return base

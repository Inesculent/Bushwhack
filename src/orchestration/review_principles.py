"""Cross-cutting review principles injected into early context (not task-specific)."""

# Appended to BehavioralSpec.reviewer_guidance and available for context probes.
DECLARED_INPUT_CONTRACT_GUIDANCE = (
    "Declared input contracts (upstream): Assume runtime inputs satisfy the framework's "
    "declared input schema for each entry point (e.g. plugin/node INPUT_TYPES, public API "
    "parameter types, protobuf/JSON schema required fields, OpenAPI required properties). "
    "Do not report missing null/None/empty guards for parameters that are required and "
    "non-optional in that schema. Treat absent null checks as a defect only when the input "
    "is explicitly optional (Optional, nullable, ANY/wildcard, or documented as accepting "
    "missing/absent values), or when the diff itself introduces nullable handling that "
    "implies such values can arrive."
)

IN_FUNCTION_CONTRACT_GUIDANCE = (
    "In-function contracts (downstream of schema): Declared input schemas do not prove "
    "branch exhaustiveness or return-type correctness inside the handler. Missing else/return, "
    "implicit None when RETURN_TYPES or annotations promise a concrete type, wrong element "
    "from structured returns (regex tuples, ORM rows, JSON nodes), and None in aggregations "
    "(join, serializers) are in scope even when enums or COMBO lists document allowed values. "
    "Do not dismiss those solely because a UI or schema restricts inputs—use needs_verification "
    "only when the failure mode depends on undocumented runtime bypass (manual edits, schema drift). "
    "This rule does NOT override the upstream declared-input rule: do not report missing None/null "
    "guards on required, non-optional parameters solely because runtime might violate types."
)

SECURITY_REVIEW_GUIDANCE = (
    "Security review: prioritize attacker-controlled input, authz boundaries, injection "
    "(SQL, command, deserialization), secrets in logs, unsafe subprocess/eval, and session/cookie "
    "handling. ReDoS and resource exhaustion count when input size or pattern is user-controlled."
)

PERFORMANCE_REVIEW_GUIDANCE = (
    "Performance review: nested loops, unbounded retries/timeouts, missing batching, sync I/O on "
    "hot paths, and accidental quadratic work. Do not flag style-only micro-optimizations."
)


def principles_for_specialty(specialty: str) -> str:
    """Bounded principle slice for critique probe/critiquer packets."""
    spec = (specialty or "general").strip().lower()
    if spec == "logic":
        return f"{DECLARED_INPUT_CONTRACT_GUIDANCE}\n{IN_FUNCTION_CONTRACT_GUIDANCE}"
    if spec == "security":
        return f"{DECLARED_INPUT_CONTRACT_GUIDANCE}\n{SECURITY_REVIEW_GUIDANCE}"
    if spec == "performance":
        return f"{DECLARED_INPUT_CONTRACT_GUIDANCE}\n{PERFORMANCE_REVIEW_GUIDANCE}"
    return DECLARED_INPUT_CONTRACT_GUIDANCE

SECURITY_REVIEW_GUIDANCE = (
    "Security review: prioritize attacker-controlled input, authz boundaries, injection "
    "(SQL, command, deserialization), secrets in logs, unsafe subprocess/eval, and session/cookie "
    "handling. ReDoS and resource exhaustion count when input size or pattern is user-controlled."
)

PERFORMANCE_REVIEW_GUIDANCE = (
    "Performance review: nested loops, unbounded retries/timeouts, missing batching, sync I/O on "
    "hot paths, and accidental quadratic work. Do not flag style-only micro-optimizations."
)


def principles_for_specialty(specialty: str) -> str:
    """Bounded principle slice for critique probe/critiquer packets."""
    spec = (specialty or "general").strip().lower()
    if spec == "logic":
        return f"{DECLARED_INPUT_CONTRACT_GUIDANCE}\n{IN_FUNCTION_CONTRACT_GUIDANCE}"
    if spec == "security":
        return f"{DECLARED_INPUT_CONTRACT_GUIDANCE}\n{SECURITY_REVIEW_GUIDANCE}"
    if spec == "performance":
        return f"{DECLARED_INPUT_CONTRACT_GUIDANCE}\n{PERFORMANCE_REVIEW_GUIDANCE}"
    return DECLARED_INPUT_CONTRACT_GUIDANCE

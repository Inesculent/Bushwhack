"""Cross-cutting review principles injected into early context (not task-specific)."""

# Appended to BehavioralSpec.reviewer_guidance and available for context probes.
DECLARED_INPUT_CONTRACT_GUIDANCE = (
    "Declared input contracts: Assume runtime inputs satisfy the framework's declared "
    "input schema for each entry point (e.g. plugin/node INPUT_TYPES, public API parameter "
    "types, protobuf/JSON schema required fields, OpenAPI required properties). "
    "Do not report missing null/None/empty guards for parameters that are required and "
    "non-optional in that schema. Treat absent null checks as a defect only when the input "
    "is explicitly optional (Optional, nullable, ANY/wildcard, or documented as accepting "
    "missing/absent values), or when the diff itself introduces nullable handling that "
    "implies such values can arrive."
)

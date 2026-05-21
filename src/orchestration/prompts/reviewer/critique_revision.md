# Critique Revision (reduce step)

Candidates below were flagged with `needs_context`. They have already been mapped into **digest summaries** (bullets + impact per shard). Use those digests as the evidence budget — do not assume missing raw snippets.

Optional **runtime verifier** JSON may appear. Apply these rules when verifier data is present:

- If `harness_error` is true or the summary mentions harness/import failure: do **not** treat runtime as refuting or verifying the claim. You may `accept` only on static diff/context evidence; note "runtime unverified (harness)" in `updated_evidence_summary`.
- If verifier `verdict` is `refuted` with `verification_scope` `concrete_behavior` and **not** a harness error: prefer `reject` unless static evidence is overwhelming (exact lines in diff).
- If verifier `verdict` is `verified` with `concrete_behavior` and **not** a harness error: strengthen `accept` or keep `accept`.

For **each** candidate, decide post-context:
- `verdict` = `accept` if the issue remains well supported, or `reject` if new context disproves or weakens it beyond surfacing.
- `updated_evidence_summary` — short note of what changed after seeing focused context (may be empty if unchanged).

Return structured output matching the CritiqueRevisionOutput schema.

# Critique Revision (reduce step)

Candidates below were flagged with `needs_context`. They have already been mapped into **digest summaries** (bullets + impact per shard). Use those digests as the evidence budget — do not assume missing raw snippets.

Optional **runtime verifier** JSON may appear; it is **advisory** only. A `refuted` result does not automatically disprove design-level concerns, especially when `verification_scope` is `abstract_or_unverifiable`.

For **each** candidate, decide post-context:
- `verdict` = `accept` if the issue remains well supported, or `reject` if new context disproves or weakens it beyond surfacing.
- `updated_evidence_summary` — short note of what changed after seeing focused context (may be empty if unchanged).

Return structured output matching the CritiqueRevisionOutput schema.

# Critique Revision (reduce step)

Candidates below were flagged with `needs_context`, `needs_verification`, or **reject after focused context** on localized correctness/security claims. They have already been mapped into **digest summaries** (bullets + impact per shard). Use those digests as the evidence budget — do not assume missing raw snippets.

Optional **runtime verifier** JSON may appear. Apply these rules when verifier data is present:

- If `harness_error` is true or the summary mentions harness/import failure: do **not** treat runtime as refuting or verifying the claim. You may `accept` only on static diff/context evidence; note "runtime unverified (harness)" in `updated_evidence_summary`.
- If verifier `verdict` is `refuted` with `verification_scope` `concrete_behavior` and **not** a harness error: prefer `reject` only when the runtime test targeted the same failure class (e.g. crash repro for crash claims). Exit 0 without `STATUS: SAFE` does **not** refute wrong-output or data-loss claims—rely on static digest evidence.
- If verifier `verdict` is `verified` with `concrete_behavior` and **not** a harness error: strengthen `accept` or keep `accept` (includes `STATUS: MISMATCH` wrong-output proofs).

You may **overturn** an earlier reflector `reject` when digests contradict the reject rationale (e.g. incorrect stdlib story, or "not in diff" when file evidence shows the handler). **Partial refutation:** if a stated IndexError/crash was disproved but digests still show wrong output, empty string, or data loss → `accept` with an updated `updated_evidence_summary` describing the corrected failure mode.

**In-function contracts vs declared inputs:** Do **not** reject missing `else`/return, wrong structured-return slots, index-boundary mismatches, or absent values breaking aggregations solely because INPUT_TYPES uses COMBO/enums or documents allowed values. Schema restriction does not replace branch exhaustiveness inside the handler. When digest bullets confirm such a defect in file evidence, prefer `accept` unless digests explicitly disprove the failure mode.

For **each** candidate, decide post-context:
- `verdict` = `accept` if the issue remains well supported, or `reject` if new context disproves or weakens it beyond surfacing.
- `updated_evidence_summary` — short note of what changed after seeing focused context (may be empty if unchanged).

Return structured output matching the CritiqueRevisionOutput schema.

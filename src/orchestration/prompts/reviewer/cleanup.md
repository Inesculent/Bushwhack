# Cleanup (reference)

Final promotion from candidates to `ReviewFinding` is performed **deterministically** in code:

- Multiple reflection reports for the same candidate and specialty are consolidated in code (accept/reclassify beats reject). A `reject` blocks promotion only when it is the winning verdict from the reflector responsible for the finding's final category. Off-domain rejects are recorded but ignored.
- `needs_context` requires fulfilled focused context plus a revision pass (or non-empty focused hits) before promotion only when it comes from the responsible reflector.
- `reclassify` updates the finding category / feedback type mapping at promotion time.
- `positive_observation` and `uncertain` candidates are never promoted.
- Promoted candidates must include an actionable failure mode, evidence summary, and recommendation.
- Security and performance-regression candidates require focused context support when the claim depends on external code or facts.

This file documents intent for humans tuning prompts; the runtime cleanup node implements the rules.

# Cleanup (reference)

Final promotion from candidates to `ReviewFinding` is performed **deterministically** in code:

- A `reject` blocks promotion only when it comes from the reflector responsible for the finding's final category. Off-domain rejects are recorded but ignored.
- `needs_context` requires fulfilled focused context plus a revision pass (or non-empty focused hits) before promotion only when it comes from the responsible reflector.
- `reclassify` updates the finding category / feedback type mapping at promotion time.

This file documents intent for humans tuning prompts; the runtime cleanup node implements the rules.

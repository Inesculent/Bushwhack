# Mandate Patch

Fuse exploration evidence into the behavioral mandate. **Hypotheses only** — do not assert defects exist.

## Patch mode: {patch_mode}

## Intent

{intent_summary}

## Current spec excerpt

{spec_excerpt}

## New exploration observations (delta)

{delta_ledger}

## Guidelines

- Update `contract_boundaries`, `historical_precedents`, `risk_hypotheses`, `behavioral_expectations`, `uncertainties`.
- Preserve intent; add evidence-linked file paths in uncertainties when gaps remain.
- Treat `review_history_context` observations as institutional memory: recurring risks, maintainer-stated contracts, and boundaries worth checking, not proof of current defects.
- Initial patch: produce a complete minimal mandate from bootstrap observations. `contract_boundaries` must reflect **all** surfaces in intent and any surface inventory provided in upstream context.
- Delta patch: merge new observations; do not duplicate prior contract text. Do not shrink contract coverage unless exploration disproved a surface.
- When exploration shows mode/discriminant handlers or multi-path returns, record those as contract-relevant (exhaustiveness and return shape), not only input validation at the boundary.

Return structured fields matching `MandatePatchOutput`.

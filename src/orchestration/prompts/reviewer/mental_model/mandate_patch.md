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
- Treat `repository_contract_context` as a compact repo-memory aid for contracts and conventions only. Use it to form or sharpen owner-specific questions; do not copy broad repo prose into every surface.
- Treat `owner_contract_scaffold` as the highest-authority factual context when present. It contains owner-local source spans, declarations, companion surfaces, and structural hints. Build concrete owner questions from these facts before relying on broad expectations or exploration prose.
- Initial patch: produce a complete minimal mandate from bootstrap observations. `contract_boundaries` must reflect **all** surfaces in intent and any surface inventory provided in upstream context.
- Delta patch: merge new observations; do not duplicate prior contract text. Do not shrink contract coverage unless exploration disproved a surface.
- When exploration shows mode/discriminant handlers or multi-path returns, record those as contract-relevant (exhaustiveness and return shape), not only input validation at the boundary.
- Emit `contract_questions` only for concrete owner-specific obligations discovered or sharpened by the delta. Keep them compact and use the existing fields: owner, surface_id, dimension, expected_behavior, contract_evidence, trigger_variant, operation, breach_question, direct_suppressor, required_evidence, source_confidence.
- When exploration shows a transformation chain, sharpen questions by operation step using existing fields: producer cardinality, projection/index semantics, aggregation/serialization, and type closure should be separate questions when they are separate obligations.
- Prefer questions over broad invariant prose. If no direct suppressing fact is known, leave `direct_suppressor` empty rather than writing placeholders.

Return structured fields matching `MandatePatchOutput`.

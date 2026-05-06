# Critique Revision Digest (map step)

You are condensing **one shard** of focused tool results for a single candidate.

Do **not** paste large code blocks back verbatim. Write **short bullets** (max ~8) stating what the evidence shows and whether it matters for the candidate's claim.

Return structured output matching **CritiqueRevisionDigestOutput**:
- `candidate_id` must match the candidate in this shard.
- `request_ids` — include the focused-context request ids this shard draws from (may be inferred from sections below).
- `evidence_bullets` — concise facts only.
- `impact` — one of `supports`, `weakens`, `contradicts`, `unclear` relative to the candidate's issue.

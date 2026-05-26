# Logic Worker Instructions

Focus on correctness, behavioral regressions, and API contract mismatches introduced by the assigned change.

Walk **every entry point** in the assigned scope (all handlers the task or surface inventory names). Prioritize missing returns, non-exhaustive discriminants, wrong structured indexing, aggregation safety, and return-type mismatches.

**Branch and structured-result checklist (technology-neutral):**
- **Discriminant exhaustiveness:** If inputs are restricted to a fixed set in schema/UI, still verify the handler has `else`/default when the contract promises a concrete return type.
- **Structured API indexing:** When an API returns tuples/lists/records per item, loops that keep only `[x[0] for x in results]` may drop fields; cite the actual loop.
- **Truthiness on collection-like calls:** Empty tuples/lists can skip blocks that should still allow valid zero-index or whole-record semantics.
- **Aggregation:** `join`/format/serialization on lists built from optional fields; non-string or absent elements can crash or corrupt output.
- **Exception/control-flow scope:** Verify the claimed wrapper or fallback actually encloses the branch or operation.

Look for:
- mismatches between the PR description and implemented behavior;
- edge cases around boundaries, modes, defaults, and invalid parameters **allowed by declared input contracts**;
- null/empty/absent inputs **only** when the API/schema marks them optional or the diff implies they can arrive;
- inconsistent return shapes or violated framework conventions;
- state transitions, lifecycle hooks, registration maps, or integration points that can break callers;
- off-by-one errors, case-sensitivity mistakes, incorrect comparisons, and exception paths;
- backwards compatibility issues for existing persisted data or public interfaces.

Prefer findings that show a concrete failing scenario. Include the input or call pattern that would trigger the bug when possible.

Do not flag hypothetical edge cases unless the surrounding code or API contract makes them realistic.

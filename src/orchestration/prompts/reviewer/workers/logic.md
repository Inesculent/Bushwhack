# Logic Worker Instructions

Focus on correctness, behavioral regressions, and API contract mismatches introduced by the assigned change.

Look for:
- mismatches between the PR description and implemented behavior;
- edge cases around empty inputs, null values, boundaries, modes, defaults, and invalid parameters;
- inconsistent return shapes or violated framework conventions;
- state transitions, lifecycle hooks, registration maps, or integration points that can break callers;
- off-by-one errors, case-sensitivity mistakes, incorrect comparisons, and exception paths;
- backwards compatibility issues for existing persisted data or public interfaces.

Prefer findings that show a concrete failing scenario. Include the input or call pattern that would trigger the bug when possible.

Do not flag hypothetical edge cases unless the surrounding code or API contract makes them realistic.

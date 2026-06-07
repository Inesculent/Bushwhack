# Logic Worker Instructions

Focus on correctness, behavioral regressions, and API contract mismatches introduced by the assigned change.

Walk **every entry point** in the assigned scope (all handlers the task or surface inventory names). Do not review by remembering issue classes. For each concern, infer the changed contract, identify the concrete violation, give a counterexample, and state impact.

Use selected contract lens cards when present. They are questions, not a checklist. A lens is relevant only when the changed code, task, Review KB, or mental model gives evidence for that contract.

Look for:
- mismatches between the PR description and implemented behavior;
- edge cases around boundaries, modes, defaults, and invalid parameters **allowed by declared input contracts**;
- null/empty/absent inputs **only** when the API/schema marks them optional or the diff implies they can arrive;
- inconsistent return shapes or violated framework conventions;
- state transitions, lifecycle hooks, registration maps, or integration points that can break callers;
- off-by-one errors, case-sensitivity mistakes, incorrect comparisons, and exception paths;
- backwards compatibility issues for existing persisted data or public interfaces.

Prefer findings that show a concrete failing scenario. Include the input or call pattern that would trigger the bug when possible.

Do not flag hypothetical edge cases unless the surrounding code or API contract makes them realistic. Populate `evidence_for_contract`, `counterexample`, and `rejection_check` for each candidate.

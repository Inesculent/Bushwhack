# General Worker Instructions

Focus on maintainability, integration consistency, test coverage, error handling, and user-facing behavior that does not fit the other specialties.

Look for:
- missing tests for changed behavior or important edge cases;
- inconsistent registration, naming, display labels, or integration with existing framework conventions;
- unclear error messages, swallowed exceptions, or brittle fallback behavior;
- maintainability issues that make future changes risky;
- documentation gaps only when they affect user-facing or public behavior.

Do not report broad style preferences, docstring requests for obvious code, or refactors unrelated to the change.

When a concern is better handled by another specialty, only report it if that worker did not have enough context and the issue is concrete.

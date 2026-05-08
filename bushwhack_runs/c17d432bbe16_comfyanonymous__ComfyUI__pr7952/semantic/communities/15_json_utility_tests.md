# Community 15: JSON Utility Tests

**Purpose:** This community provides unit tests and implementations for deep merging JSON-like dictionaries in a recursive manner. It supports robust handling of nested structures, mixed types, and edge cases like None values or empty dicts. These utilities are critical for maintaining config integrity across ComfyUI's node management systems.

## Files
- `tests-unit/utils/json_util_test.py`: Contains unit test functions validating the merge_json_recursive behavior across various input scenarios. (confidence 0.95)
- `utils/json_util.py`: Defines the merge_json_recursive function used to deeply merge two JSON-compatible dictionaries while preserving nested structure integrity. (confidence 0.90)

## Symbols
- `0b0c3139e6a19b29`: Core implementation: recursively merges two dictionaries, updating values from the second into the first with nested merging logic. (confidence 0.80)
  - _Rationale:_ Only non-test symbol; likely central utility for handling nested JSON configs across ComfyUI nodes.
- `0fb0af82d593cc95`: Test case: verifies merge_json_recursive handles deeply nested dictionary structures correctly. (confidence 0.85)
  - _Rationale:_ Name suggests nested depth testing; part of suite validating structural integrity.
- `1ca30cd04f64a7d6`: Test case: verifies handling of complex nested structures beyond simple dict nesting. (confidence 0.85)
  - _Rationale:_ Implies testing of non-trivial combinations or types within nested dicts.
- `25b8b5e6b881f0ee`: Test case: checks behavior when merging values of different types (e.g., int + str). (confidence 0.85)
  - _Rationale:_ Important for robustness against malformed or mixed-type JSON inputs.
- `31ddfcd5fe10265f`: Test case: validates merge behavior when input values are of mixed types. (confidence 0.85)
  - _Rationale:_ Suggests testing of type coercion or handling in recursive merge.
- `45234f47c4e8f938`: Test case: validates merge_json_recursive handles nested lists within dictionaries. (confidence 0.85)
  - _Rationale:_ Ensures list values are correctly preserved or merged during recursion.
- `58c9b458b88d6e64`: Test case: verifies handling of None values during merge operations. (confidence 0.85)
  - _Rationale:_ Critical for ensuring nulls don't break recursive merging logic.
- `59fa2fd4e2bfe5a1`: Test case: checks behavior when merging empty dictionaries. (confidence 0.85)
  - _Rationale:_ Edge case validation to prevent errors on trivial inputs.
- `785c4feb8600a79d`: Test case: verifies simple dict merging (non-nested, flat structure). (confidence 0.85)
  - _Rationale:_ Baseline test for core functionality.
- `7cfe129e97c08bed`: Test case: checks behavior when merging non-dict values (e.g., integers or strings). (confidence 0.85)
  - _Rationale:_ Ensures merge handles type mismatches gracefully.
- `ed918369be792522`: Test case: validates list merging behavior (e.g., concatenation or replacement). (confidence 0.85)
  - _Rationale:_ Confirms list values are processed correctly during recursion.

## Cross-community dependencies
5

## Unverified / resolved calls
- unresolved: `CustomNodeManager` from `0b0c3139e6a19b29` — Potentially called during config loading or node registration, but implementation not visible here.

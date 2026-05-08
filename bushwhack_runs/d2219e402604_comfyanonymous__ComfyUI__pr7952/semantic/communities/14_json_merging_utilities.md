# Community 14: JSON Merging Utilities

**Purpose:** Provide utilities and tests for merging JSON objects recursively.

## Files
- `tests-unit/utils/json_util_test.py`: Unit tests for the merge_json_recursive function. (confidence 1.00)
- `utils/json_util.py`: Define the merge_json_recursive function for merging JSON objects. (confidence 1.00)

## Symbols
- `symbol:0b0c3139e6a19b29`: Recursively merges two JSON objects, updating the base with values from the update object. (confidence 1.00)
  - _Rationale:_ The function name and typical use cases suggest it handles merging of JSON objects.
- `symbol:0fb0af82d593cc95`: Test merging of nested dictionaries using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name implies testing a specific scenario of nested dictionaries.
- `symbol:1ca30cd04f64a7d6`: Test merging complex nested structures using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name suggests testing more complex nested scenarios.
- `symbol:25b8b5e6b881f0ee`: Test merging when encountering different data types using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name indicates handling of type differences during merging.
- `symbol:31ddfcd5fe10265f`: Test merging of mixed data types using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name suggests testing merging with mixed types.
- `symbol:45234f47c4e8f938`: Test merging of nested lists using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name indicates handling of nested list structures.
- `symbol:58c9b458b88d6e64`: Test merging with None values using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name suggests handling of None values during merging.
- `symbol:59fa2fd4e2bfe5a1`: Test merging of empty dictionaries using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name indicates testing with empty dictionaries.
- `symbol:785c4feb8600a79d`: Test merging of simple dictionaries using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name suggests testing basic dictionary merging.
- `symbol:7cfe129e97c08bed`: Test overwriting non-dictionary values using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name indicates testing overwriting of non-dictionary values.
- `symbol:ed918369be792522`: Test merging of lists using merge_json_recursive. (confidence 1.00)
  - _Rationale:_ The function name suggests handling of list merging.

## Cross-community dependencies
6

## Unverified / resolved calls
- resolved: `CustomNodeManager` from `symbol:0b0c3139e6a19b29` — Called within merge_json_recursive or related logic.
  - Custom node manager. (Class definition manages custom nodes within the application.)

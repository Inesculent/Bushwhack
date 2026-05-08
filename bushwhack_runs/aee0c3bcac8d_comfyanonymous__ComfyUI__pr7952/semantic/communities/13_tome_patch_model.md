# Community 13: Tome Patch Model

**Purpose:** Implements Tome Patch Model functionalities with specific utility functions.

## Files
- `comfy_extras/nodes_tomesd.py`: Contains utility functions and a class for handling Tome Patch Model operations. (confidence 1.00)

## Symbols
- `symbol:0fde711ccb0fc174:mps_gather_workaround`: A workaround function for MPS gather operations. (confidence 1.00)
  - _Rationale:_ The function name suggests it is used to handle a specific issue or limitation with MPS (Metal Performance Shaders) gather operations.
- `symbol:6204099911b7af70:do_nothing`: A placeholder function that does nothing with the input tensor. (confidence 1.00)
  - _Rationale:_ The function simply returns the input tensor without any modifications, as indicated by its name and implementation.
- `symbol:76ac3e27957b5406:get_functions`: Calculates functions based on given parameters and original shape. (confidence 1.00)
  - _Rationale:_ The function takes three parameters (x, ratio, original_shape) and calculates some functions, likely related to resizing or processing tensors.
- `symbol:840f3a50f9de2c84:bipartite_soft_matching_random2d`: Performs bipartite soft matching in 2D using a given metric tensor. (confidence 1.00)
  - _Rationale:_ The function name and parameter suggest it is used for matching elements in a 2D space using a soft approach based on a provided metric tensor.
- `symbol:9600ceea760a7ede:TomePatchModel`: Defines the TomePatchModel class. (confidence 1.00)
  - _Rationale:_ This symbol represents a class definition, indicating that TomePatchModel is a model or component within the codebase.

## Cross-community dependencies
0, 4

## Unverified / resolved calls
- unresolved: `default` from `Unspecified` — Used as a default value or parameter.
- unresolved: `Image` from `Unspecified` — Used in conjunction with image processing or manipulation.

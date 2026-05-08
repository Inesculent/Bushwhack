# Community 17: Tome Patch Model

**Purpose:** Implements Tome patch model with specific utility functions.

## Files
- `comfy_extras/nodes_tomesd.py`: Provides utility functions and a TomePatchModel class for handling image patches and related operations. (confidence 1.00)

## Symbols
- `symbol:0fde711ccb0fc174:mps_gather_workaround`: A workaround function for MPS gather operations, possibly addressing specific issues in MPS (Metal Performance Shaders). (confidence 0.90)
  - _Rationale:_ The function takes input, dimension, and index as parameters, typical for gather operations.
- `symbol:6204099911b7af70:do_nothing`: A no-operation function that returns the input tensor unchanged, with an optional mode parameter. (confidence 1.00)
  - _Rationale:_ The function name 'do_nothing' clearly indicates its purpose, and the parameters suggest it might be used in conditional operations or as a placeholder.
- `symbol:76ac3e27957b5406:get_functions`: Retrieves functions based on a given ratio and original shape, likely used in processing or transforming data. (confidence 0.90)
  - _Rationale:_ The parameters suggest it involves some form of scaling or adaptation based on the input ratio and shape.
- `symbol:840f3a50f9de2c84:bipartite_soft_matching_random2d`: Performs bipartite soft matching in 2D space using a metric tensor, possibly for tasks like image alignment or correspondence. (confidence 0.90)
  - _Rationale:_ The function name and parameter suggest it deals with matching or alignment tasks involving a metric tensor.
- `symbol:9600ceea760a7ede:TomePatchModel`: Defines a class for a Tome Patch Model, which likely handles operations related to image patches or segments. (confidence 1.00)
  - _Rationale:_ The class name suggests it is central to the functionality of the module, possibly involving patch-based image processing.

## Cross-community dependencies
0, 1

## Unverified / resolved calls
- unresolved: `default` from `Unspecified` — Cross-community callee
- unresolved: `Image` from `Unspecified` — Cross-community callee

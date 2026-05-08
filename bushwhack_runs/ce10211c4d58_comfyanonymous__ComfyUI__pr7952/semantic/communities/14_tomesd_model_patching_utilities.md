# Community 14: TomeSD Model Patching Utilities

**Purpose:** This community implements utility functions and a patch class for Tome (Token Merging) techniques in Stable Diffusion, specifically for accelerating inference. The core responsibility appears to be merging redundant tokens in diffusion models using soft matching algorithms, with helpers for MPS (Metal Performance Shaders) device compatibility. It integrates with the broader ComfyUI system via the TomePatchModel class, likely applied as a patch during model loading or execution.

## Files
- `comfy_extras/nodes_tomesd.py`: Contains the main implementation of TomeSD functionality, including the TomePatchModel class that patches the diffusion model with token merging capabilities, and various helper functions for token merging logic and device workarounds. (confidence 1.00)

## Symbols
- `9600ceea760a7ede:TomePatchModel`: Main class for patching diffusion models with Tome (Token Merging) functionality. Likely wraps or extends model operations to merge redundant tokens during inference, providing acceleration through token reduction. (confidence 1.00)
  - _Rationale:_ Class name suggests model patching capability for the Tome algorithm, which is known for token merging in diffusion models.
- `0fde711ccb0fc174:mps_gather_workaround`: Device-specific workaround for MPS (Metal Performance Shaders) operations involving gather functionality, likely addressing compatibility issues with Apple Silicon hardware. (confidence 1.00)
  - _Rationale:_ Function name indicates a workaround for gather operations on MPS devices, suggesting hardware-specific compatibility handling.
- `6204099911b7af70:do_nothing`: Placeholder/no-op function that returns input unchanged, possibly used as a default or fallback in the token merging logic. (confidence 1.00)
  - _Rationale:_ Function name indicates no operation behavior, likely used for interface consistency or as a default when no merging should occur.
- `76ac3e279575406:get_functions`: Helper function to retrieve merging functions based on configuration parameters like ratio and original_shape. (confidence 1.00)
  - _Rationale:_ Function name suggests dynamic retrieval of functions, likely selecting appropriate token merging strategies based on parameters.
- `840f3a50f9de2c84:bipartite_soft_matching_random2d`: Core algorithm function implementing bipartite soft matching with random 2D selection for token merging, operating on metric tensors. (confidence 1.00)
  - _Rationale:_ Function name describes a specific matching algorithm using bipartite graph matching and soft selection for token merging in 2D spatial dimensions.

## Cross-community dependencies
0, 1

## Unverified / resolved calls
- unresolved: `Image` from `9600ceea760a7ede:TomePatchModel` — Class may interact with Image objects for input/output handling, though body is not visible

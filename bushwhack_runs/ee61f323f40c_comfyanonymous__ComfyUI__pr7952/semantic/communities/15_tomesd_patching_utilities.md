# Community 15: TomeSD Patching Utilities

**Purpose:** This community provides token merging and reduction utilities for diffusion models, specifically implementing the Tome method (Tokens Matter in Diffusion) within ComfyUI. The community integrates with the model patching system via `TomePatchModel` and includes low-level tensor operations like `bipartite_soft_matching_random2d` for token clustering. It appears to be part of the `comfy_extras` extension set, likely used to speed up inference or reduce memory footprint by merging redundant tokens.

## Files
- `comfy_extras/nodes_tomesd.py`: Main module containing the `TomePatchModel` class and associated logic. Likely exposes custom ComfyUI nodes or utility functions for applying token reduction patches to UNet models during inference. (confidence 0.90)

## Symbols
- `0fde711ccb0fc174`: Workaround implementation for Metal Performance Shaders (MPS) on macOS, likely fixing a specific bug in PyTorch's gather operation. Indicates compatibility maintenance for non-CPU/GPU environments. (confidence 0.90)
  - _Rationale:_ Function name suggests a specific hardware acceleration workaround.
- `6204099911b7af70`: No-op placeholder function. Likely used as a default or fallback in a configuration dictionary or function map, possibly for when a matching operation isn't selected. (confidence 0.90)
  - _Rationale:_ Standard naming convention for identity operations in modular architectures.
- `76ac3e27957b5406`: Function to retrieve available matching functions (likely different token reduction strategies) based on input parameters like `ratio`. (confidence 0.80)
  - _Rationale:_ Suggests a factory pattern for selecting specific token merging algorithms.
- `840f3a50f9de2c84`: Core logic for the token merging algorithm, performing soft matching (clustering) on a 2D metric tensor. This is likely the heart of the Tome method implementation. (confidence 0.90)
  - _Rationale:_ Argument names `metric` and signature pattern align with graph-based token reduction literature.
- `9600ceea760a7ede`: Main patch class, likely wrapping model layers to inject token reduction logic during forward passes. (confidence 0.90)
  - _Rationale:_ Class naming convention `*PatchModel` implies integration into the model execution pipeline.

## Cross-community dependencies
0, 2

## Unverified / resolved calls
- unresolved: `default` from `9600ceea760a7ede` — Likely a reference to a global `default` configuration or import. Could be from `diffusers` or internal config.
- unresolved: `Image` from `9600ceea760a7ede` — Likely a PyTorch tensor type alias or import from `torch`. If it refers to a ComfyUI type, it might belong to the Image processing community.

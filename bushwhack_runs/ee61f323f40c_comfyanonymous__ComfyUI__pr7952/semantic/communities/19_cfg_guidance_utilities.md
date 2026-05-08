# Community 19: CFG Guidance Utilities

**Purpose:** This community provides utilities for Classifier-Free Guidance (CFG) in ComfyUI workflows. It contains classes and functions that manage CFG scaling techniques, such as zero-scale operations and optimized scaling methods. It supports the generation pipeline by configuring guidance parameters and enhancing sampling efficiency.

## Files
- `19`: Implements CFG-related functionality for node graphs, including custom scaling and guidance logic used during image generation. (confidence 0.60)

## Symbols
- `5b2575c94d61b255`: A class likely used to handle zero-scale CFG operations in the generation pipeline. (confidence 0.50)
  - _Rationale:_ Name suggests specialized CFG handling, possibly for null or zero-value guidance scenarios.
- `bc02ff741fbd3d11`: Function for optimized CFG scaling between positive and negative prompts. (confidence 0.70)
  - _Rationale:_ Function signature shows it operates on two inputs (positive, negative), typical of CFG logic.

## Cross-community dependencies
(none)

## Unverified / resolved calls

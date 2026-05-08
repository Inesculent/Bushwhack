# Community 19: CFG ZeroStar Sampling Extension

**Purpose:** This community implements a CFG (Classifier-Free Guidance) scaling mechanism for ZeroStar sampling, used to adjust guidance strength in generative models. It operates within the node system, likely supporting advanced sampling strategies by modifying positive/negative embeddings.

## Files
- `comfy_extras/nodes_cfg.py`: Implements CFG scaling utilities and ZeroStar sampling logic for generative workflows. (confidence 1.00)

## Symbols
- `5b2575c94d61b255`: CFGZeroStar class that implements the core logic for ZeroStar sampling, likely handling guidance scaling during generation. (confidence 1.00)
  - _Rationale:_ Class name indicates it implements ZeroStar sampling with CFG, central to this community's purpose.
- `bc02ff741fbd3d11`: Helper function to optimize scale calculations, possibly for guidance adjustments in sampling. (confidence 1.00)
  - _Rationale:_ Function name suggests it optimizes scale computations, likely used by CFGZeroStar for efficient guidance handling.

## Cross-community dependencies
(none)

## Unverified / resolved calls

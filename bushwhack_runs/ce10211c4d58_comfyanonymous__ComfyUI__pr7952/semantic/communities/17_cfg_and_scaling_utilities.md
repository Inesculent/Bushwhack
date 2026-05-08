# Community 17: CFG and Scaling Utilities

**Purpose:** This community provides specialized nodes and utility functions for ControlNet guidance scale (CFG) processing and scaling operations within ComfyUI. It bridges core execution logic with advanced conditioning features, particularly supporting zero-shot controlnet scenarios through the CFGZeroStar class. Key flow involves adapting scaling behaviors for conditioning inputs (positive/negative) without direct dependency on full sampler implementations.

## Files
- `5`: Implements custom nodes for CFG (Classifier-Free Guidance) manipulation, including the CFGZeroStar node that may enable zero-shot controlnet conditioning adjustments. (confidence 0.75)

## Symbols
- `5b2575c94d61b255`: CFGZeroStar: A node class likely used to adjust or disable CFG behavior in specific conditioning contexts, possibly for zero-shot controlnet scenarios. (confidence 0.75)
  - _Rationale:_ Class name suggests it handles 'CFG' logic with 'ZeroStar' (zero-shot/controlnet) semantics, typical for advanced conditioning workflows.
- `bc02ff741fbd3d11`: optimized_scale: A function likely designed to conditionally adjust scale factors for positive and negative conditioning inputs, improving sampling stability or efficiency. (confidence 0.75)
  - _Rationale:_ Name implies optimization logic applied to scale parameters (positive/negative pairs), common in CFG-guided sampling contexts.

## Cross-community dependencies
(none)

## Unverified / resolved calls

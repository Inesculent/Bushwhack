# Community 0: Image and Sampling Operations

**Purpose:** This community implements image manipulation, masking, and sampling utilities for ComfyUI workflows. It provides core node types for blending images, conditioning latents, and applying sampling schedules, often interacting with model loading and conditioning communities.

## Files
- `comfy/sample.py`: Core sampling logic including denoising functions and schedule calculations. (confidence 1.00)
- `comfy_extras/nodes_compositing.py`: Image compositing operations like masking, blending, and alpha handling. (confidence 1.00)
- `comfy_extras/nodes_custom_sampler.py`: Advanced sampler configuration and custom noise schedule applications. (confidence 1.00)

## Symbols
- `272f0d310ad4fccb`: Primary denoising loop that executes the diffusion process given model and noise. (confidence 1.00)
  - _Rationale:_ Explicitly named 'sample' and used in sampling flows.
- `08bf7810ee623f36`: Retrieves generation history by prompt ID from the backend queue. (confidence 1.00)
  - _Rationale:_ Function name and context suggest history management.
- `105bee53f096506a`: Combines two masks using different compositing modes (add, multiply, etc.). (confidence 1.00)
  - _Rationale:_ Class name implies mask composite operations.
- `0aa630bffc38608c`: Euler Ancestral sampler variant for stochastic sampling trajectories. (confidence 1.00)
  - _Rationale:_ Sampler class name indicates specific algorithm type.

## Cross-community dependencies
1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 16

## Unverified / resolved calls
- unresolved: `ComfyClient` from `08bf7810ee623f36` — ComfyClient interface may handle history retrieval from external systems.
- unresolved: `ModelPatcher` from `272f0d310ad4fccb` — ModelPatcher likely wraps model tensors during sampling, requires cross-community verification.

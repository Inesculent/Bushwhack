# Community 18: UNet Type Definitions

**Purpose:** This community provides core type definitions and function signatures for UNet model operations, serving as a typed interface layer for diffusion model components. It appears to define expected input structures and callable protocols used by higher-level UNet implementations, facilitating type safety across the diffusion pipeline. The community likely acts as a dependency for downstream communities implementing actual UNet architectures or applying conditioning logic.

## Files
- `comfy/comfy_types/__init__.py`: Exports type aliases and classes for UNet operations, aggregating definitions that are consumed by other modules in the ComfyUI codebase. It centralizes shared type contracts to ensure consistent data shapes between components like UNet models, condition injectors, and sampling functions. (confidence 0.92)
- `cross_community_calls`: N/A - This entry represents external calls. (confidence 0.75)

## Symbols
- `symbol:5b1931f21044956a`: UnetParams defines the expected configuration or parameter structure for a UNet model, likely used to initialize or pass settings to diffusion networks. Its TypedDict nature suggests it enforces specific keys required by downstream UNet implementations. (confidence 0.90)
  - _Rationale:_ Class inherits TypedDict, indicating a fixed schema for dictionary-like parameters passed to UNet operations.
- `symbol:65ff6df0f17296f7`: UnetApplyFunction defines a callable protocol specifying how UNet models should process inputs, likely enforcing a signature that accepts latent data, timestep, and condition data before returning transformed latents. (confidence 0.90)
  - _Rationale:_ Protocol class indicates it defines a required interface for UNet callables, ensuring consistent method signatures across implementations.
- `symbol:83895300401f404f`: UnetApplyConds appears to define the structure or expected format of conditioning data (e.g., embeddings, attention masks) that the UNet model consumes during diffusion steps. (confidence 0.85)
  - _Rationale:_ TypedDict name suggests it structures condition inputs, aligning with common diffusion architectures requiring explicit conditioning management.

## Cross-community dependencies
0

## Unverified / resolved calls
- unresolved: `BaseModel` from `symbol:5b1931f21044956a` — UnetParams may pass configuration to or inherit from BaseModel, but the relationship is unconfirmed without the BaseModel definition.

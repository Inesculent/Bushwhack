# Community 1: Multi-model DiT and VAE Components

**Purpose:** This community contains foundational architectural components for various diffusion models (DiT, Flux, Wan, HiDream) and VAEs, primarily focusing on attention mechanisms, positional embeddings, and normalization layers. It serves as the core implementation library for different generative model backbones used across ComfyUI's model loading and inference pipelines, integrating with community nodes via model class factories and embedding modules. Key relationships include providing attention blocks to Transformer-based models and VAE structures for image/audio latent processing.

## Files
- `comfy/cldm/dit_embedder.py`: Likely contains DiT-specific embedding logic, though specific symbols not detailed in context. (confidence 0.95)

## Symbols
- `005e65ac45328aa2`: Attention pooling mechanism, likely used in Transformer architectures to reduce sequence length or aggregate context. (confidence 0.90)
  - _Rationale:_ Named AttentionPool, inherits from nn.Module, consistent with Transformer-style pooling.

## Cross-community dependencies
0, 2, 3, 4, 5, 6, 7, 9, 10

## Unverified / resolved calls
- unresolved: `T5LayerFF` from `03b5efd2e39a0f2c` — Likely used for text conditioning in transformer models like HiDream or GLM-based architectures.

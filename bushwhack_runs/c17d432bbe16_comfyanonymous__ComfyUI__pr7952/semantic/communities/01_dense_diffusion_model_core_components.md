# Community 1: Dense Diffusion Model Core Components

**Purpose:** This community provides core building blocks for diffusion and autoregressive video/image generation models. It includes attention mechanisms, position embedding strategies, normalization layers, and model architectures for various model types (Wan, Genmo, HiDream, etc.) that serve as backbone generators in the ComfyUI workflow.

## Files
- `comfy/ldm/cosmos/model.py`: Defines the CosmosModel class and related structures for video generation, integrating position embeddings and transformer blocks. (confidence 0.85)
- `comfy/ldm/genmo/joint_model/asymm_models_joint.py`: Implements the Genmo joint model architecture with asymmetric blocks and RoPE-based position embeddings for video synthesis. (confidence 0.80)
- `comfy/ldm/common_dit.py`: Contains utility functions for diffusion models, including timestep embedding, modulation, and layer normalization. (confidence 0.90)
- `comfy/ldm/cosmos/position_embedding.py`: Defines various position embedding strategies including RoPE and learnable positional encodings for 3D video data. (confidence 0.85)
- `comfy/ldm/genmo/joint_model/temporal_rope.py`: Implements temporal RoPE (Rotary Position Embedding) functions specifically optimized for video transformer models. (confidence 0.80)

## Symbols
- `005e65ac45328aa2`: Pools token embeddings to create global context, often used for condition vectors or classification heads. (confidence 0.90)
  - _Rationale:_ Appears in Transformer architectures to reduce sequence length for downstream conditioning.
- `03a46feaf3a1a578`: HunYuanDiT is a diffusion transformer model implementation, likely supporting Chinese-specific model weights. (confidence 0.85)
  - _Rationale:_ Follows naming convention of major diffusion models (DiT = Diffusion Transformer).
- `03b5efd2e39a0f2c`: Computes 2D sincos position embeddings for image/patch-level spatial encoding. (confidence 0.95)
  - _Rationale:_ Standard technique in transformer architectures for spatial awareness.
- `0699b24cadc24e3f`: Audio-oriented VAE (Variational Autoencoder) likely for music or speech synthesis tasks. (confidence 0.80)
  - _Rationale:_ Named with Audio prefix and VAE suffix.
- `08b09b200c7b6201`: Core attention mechanism used across multiple diffusion models. (confidence 0.95)
  - _Rationale:_ Named 'Attention' and appears in transformer blocks.
- `117dd323f49a624f`: Patches image/video frames into token sequences for transformer processing. (confidence 0.90)
  - _Rationale:_ Standard first layer in DiT architectures.
- `1650bf9de8d24c41`: WanModel is likely the Wan 2.1 video generation backbone implementation. (confidence 0.85)
  - _Rationale:_ Matches the Wan model family in the unverified list.
- `1c6bf52ddf80cea9`: Gligen implementation for localization-guided generation control. (confidence 0.80)
  - _Rationale:_ Named after GLIGEN paper for spatial guidance.
- `1f21cb5849c06a4f`: Root mean square normalization layer, more stable than LayerNorm. (confidence 0.95)
  - _Rationale:_ Common in modern transformer architectures.
- `20cee9fabe9e6eb5`: Second Attention class implementation, possibly optimized for specific hardware or use cases. (confidence 0.70)
  - _Rationale:_ Duplicate name suggests alternative implementation.
- `2182bb463ec54a2a`: Encodes timesteps and class labels into latent vectors for diffusion conditioning. (confidence 0.95)
  - _Rationale:_ Standard DiT conditioning component.
- `2297c09d60802dfa`: Creates position matrices for RoPE or spatial encoding. (confidence 0.85)
  - _Rationale:_ Helper for positional embedding calculation.
- `22f0cf1a84daacbb`: Wan model's self-attention mechanism. (confidence 0.85)
  - _Rationale:_ Part of WanModel implementation.
- `246b4865a2d43a69`: Multi-Layer Perceptron component used in transformer blocks. (confidence 0.95)
  - _Rationale:_ Standard FFN structure in transformers.
- `25f3b93ad49b8841`: SwiGLU feed-forward network with gated activation. (confidence 0.90)
  - _Rationale:_ Modern FFN alternative to ReLU-based layers.
- `27d23e5e2e11e19e`: HiDream image transformer model for high-fidelity generation. (confidence 0.80)
  - _Rationale:_ Matches HiDream model family in unverified list.
- `2b7bd961e4872496`: Final processing layer converting model output to pixel space. (confidence 0.90)
  - _Rationale:_ Standard final layer in generative models.
- `2b96e6cf9535a1cd`: Core Transformer block combining attention and feed-forward layers. (confidence 0.95)
  - _Rationale:_ Fundamental unit of transformer architectures.

## Cross-community dependencies
0, 2, 3, 4, 5, 6, 7, 8, 9, 10

## Unverified / resolved calls
- unresolved: `HiDreamImageTransformer2DModel` from `27d23e5e2e11e19e` — Likely base class or parent architecture reference.
- unresolved: `HunYuanDiT` from `03a46feaf3a1a578` — Likely references model initialization or config loading from external modules.
- unresolved: `WanModel` from `1650bf9de8d24c41` — Self-reference or forward reference to model class definition.

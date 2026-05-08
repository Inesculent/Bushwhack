# Community 4: Model Architecture Building Blocks

**Purpose:** This community provides core neural network modules (ResNets, Attention, Normalization, Downsampling) used across Stable Diffusion, video generation models, and VAE architectures. It acts as a foundational layer for constructing larger models like UNets and Diffusion pipelines. Downstream agents should use these as reference when building or modifying model components.

## Files
- `comfy/cldm/cldm.py`: Defines Conditional Linear Model (CLDM) architecture components, likely used for controlnet-style conditioning in diffusion models. (confidence 1.00)
- `comfy/ldm/ace/vae/autoencoder_dc.py`: Implements a deterministic autoencoder (DC) likely for image or video compression, using DC (Difference Coding) techniques. (confidence 1.00)
- `comfy/ldm/cascade/common.py`: Shared utilities for Stable Cascade architecture, including common layers and helper functions for multi-stage generation. (confidence 1.00)
- `comfy/ldm/cosmos/cosmos_tokenizer/layers3d.py`: Contains 3D convolution layers and tokenization logic for Cosmos video tokenizer models. (confidence 1.00)
- `comfy/ldm/genmo/joint_model/asymm_models_joint.py`: Implements asymmetric joint diffusion models, possibly for text-to-video or multimodal generation tasks. (confidence 1.00)
- `comfy/ldm/lightricks/vae/causal_video_autoencoder.py`: Builds a causal video autoencoder with 3D convolutions for spatiotemporal compression. (confidence 1.00)
- `comfy/ldm/lightricks/vae/dual_conv3d.py`: Implements dual-channel 3D convolutions for efficient video processing. (confidence 1.00)

## Symbols
- `ResBlock`: A standard residual block often used as the fundamental building block in UNets and diffusion models. (confidence 1.00)
  - _Rationale:_ Class inherits nn.Module and is used in conditional modeling pipelines.
- `RMSNorm`: Root Mean Square Layer Normalization module, commonly used in modern transformers for stabilization. (confidence 1.00)
  - _Rationale:_ Inherits ops.RMSNorm, suggesting compatibility with custom neural ops.
- `OptimizedAttention`: Efficient attention mechanism, likely used to accelerate training or inference in diffusion steps. (confidence 1.00)
  - _Rationale:_ Named 'Optimized' suggests performance tuning over standard attention.
- `CausalConv3d`: 3D convolution with causality, ensuring future timesteps do not influence past ones in video generation. (confidence 1.00)
  - _Rationale:_ Inherits ops.Conv3d with 'Causal' modifier, critical for video generation models.
- `DepthToSpaceTime`: Transforms depth/dimension into spatial/temporal dimensions, often used in VAE decoding. (confidence 1.00)
  - _Rationale:_ Class name suggests dimension rearrangement for spatiotemporal data.

## Cross-community dependencies
0, 1, 2, 3, 5, 6, 7, 8, 10, 11

## Unverified / resolved calls
- unresolved: `Attention` from `VideoResBlock` — Video blocks often integrate self-attention for temporal modeling.
- unresolved: `CrossAttention` from `AsymmetricJointBlock` — Joint models often use cross-attention for multimodal conditioning.
- unresolved: `modulate` from `ResBlock` — Modulation is typically used in diffusion timestep embedding or conditional control.
- unresolved: `TimestepEmbedSequential` from `CausalContinuousVideoTokenizer` — Tokenizer may use sequential embedding for timestep conditioning.
- unresolved: `VAE` from `AutoencoderDC` — AutoencoderDC likely implements a VAE (Variational Autoencoder) for compression/decompression.

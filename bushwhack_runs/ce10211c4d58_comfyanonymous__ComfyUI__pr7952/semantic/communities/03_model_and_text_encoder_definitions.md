# Community 3: Model and Text Encoder Definitions

**Purpose:** This community defines core model architectures (e.g., SD3, Flux, HunyuanVideo, PixArtAlpha) and their associated tokenizer and clip implementations. It centralizes the model detection and configuration logic required to initialize different generative backends. This community acts as a registry layer, providing base classes for model types and utility functions for detecting and converting their state dicts.

## Files
- `comfy/model_detection.py`: Contains detection functions for identifying model types from state dicts, including specific logic for Llama, T5, and general UNet configurations. (confidence 0.85)
- `comfy/supported_models_base.py`: Defines the base classes for supported models, serving as the foundation for specific model implementations like SD20, PixArtAlpha, and StableAudio. (confidence 0.90)
- `comfy/supported_models.py`: Aggregates specific model class definitions that extend the base classes, likely acting as the main registry of supported architectures. (confidence 0.88)
- `comfy/sd1_clip.py`: Provides the standard tokenizer and clip model implementations (SDTokenizer, SD1ClipModel) used by most text encoder variants. (confidence 0.92)
- `comfy/text_encoders/ace.py`: Implements specific text encoder architecture for the ACE Step model, likely involving custom T5 layers or similar structures. (confidence 0.80)

## Symbols
- `symbol:044f7a529d8ad3b9:LTXV`: Represents the LTXV model class, inheriting from BaseModel, indicating support for this specific generative architecture. (confidence 0.95)
  - _Rationale:_ Explicitly extends BaseModel, confirming its role as a model definition.
- `symbol:14be8758573f45be:SD3`: Defines the SD3 (Stable Diffusion 3) model structure, inheriting from BaseModel. (confidence 0.95)
  - _Rationale:_ Class name directly corresponds to a major model release, extending the base model structure.
- `symbol:061731c3484e73cd:timestep_embedding`: Utility function to create embeddings for timesteps, crucial for diffusion model conditioning. (confidence 0.88)
  - _Rationale:_ Function name and signature suggest embedding generation for time steps.
- `symbol:1d4155552a2cb761:ACEStep`: Implements the ACE Step generative model, inheriting from BaseModel. (confidence 0.90)
  - _Rationale:_ Class inherits from BaseModel, aligning with other model definitions.
- `symbol:10f1c3d087ecf4b3:model_lora_keys_clip`: Extracts or maps keys specifically for CLIP-based LoRA modifications. (confidence 0.85)
  - _Rationale:_ Function name indicates key mapping logic for LoRA applied to CLIP encoders.

## Cross-community dependencies
0, 1, 2, 4, 5, 6, 7, 8, 10, 12

## Unverified / resolved calls
- unresolved: `sd1_clip.SDTokenizer` from `symbol:023ed91846271f5f:UMT5XXlTokenizer` — Inherits from sd1_clip.SDTokenizer
- unresolved: `T5XXLModel` from `symbol:13b3bf979c81fd27:T5XXLModel` — Inherits from comfy.text_encoders.sd3_clip.T5XXLModel
- unresolved: `torch.nn.Module` from `symbol:18b09443c624d232:FluxClipModel` — Inherits from torch.nn.Module

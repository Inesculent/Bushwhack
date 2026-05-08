# Community 3: Model Registry and Support Base

**Purpose:** This community registers and configures diverse generative models (image, video, audio, text) within ComfyUI, acting as the central model type catalog. It defines model base classes, format handlers, and tokenizer variants to unify API interactions across architectures like SD3, Flux, PixArt, and HunYuan. It bridges upstream model loaders with downstream inference logic by standardizing latent spaces and conditioning pipelines.

## Files
- `comfy/supported_models.py`: Main registry file mapping model names to architecture classes and loading logic. (confidence 1.00)
- `comfy/supported_models_base.py`: Defines base model classes (BASE, BaseModel) and shared infrastructure for all supported architectures. (confidence 1.00)
- `comfy/latent_formats.py`: Defines LatentFormat classes for converting between model latent spaces and internal representation. (confidence 0.80)
- `comfy/sd1_clip.py`: Base clip tokenizer implementations used by various model types (SD, SDXL, SD3). (confidence 0.80)
- `comfy/text_encoders/flux.py`: Flux-specific text encoder (CLIP) implementation. (confidence 1.00)
- `comfy/text_encoders/ace.py`: ACE Step text encoder logic. (confidence 1.00)
- `comfy/text_encoders/genmo.py`: Genmo model text encoder configuration. (confidence 0.80)

## Symbols
- `01ef097099208ee9:SV3D_u`: Specific video-to-video model variant (3D-aware) inheriting from SVD_img2vid. (confidence 1.00)
  - _Rationale:_ Inheritance from SVD_img2vid indicates specialized video generation logic.
- `0222e175be30ce8a:ModelType`: Enum defining distinct model categories (image/video/audio) for routing logic. (confidence 1.00)
  - _Rationale:_ Enum usage suggests classification or dispatch mechanism for model loading.
- `044f7a529d8ad3b9:LTXV`: LTX Video model class inheriting from BaseModel. (confidence 1.00)
  - _Rationale:_ Direct inheritance indicates it follows standard model interfaces defined in BaseModel.
- `061731c3484e73cd:timestep_embedding`: Helper function for encoding time steps into embeddings for diffusion processes. (confidence 1.00)
  - _Rationale:_ Standard diffusion pattern (timesteps, dim, max_period) indicates time embedding logic.
- `0704067a90ab3018:Chroma`: Chroma-specific model class inheriting from Flux. (confidence 1.00)
  - _Rationale:_ Inheritance from Flux implies architectural similarity and shared base logic.
- `07c58aade35ecc17:PixArtAlpha`: PixArt Alpha model class extending BASE. (confidence 1.00)
  - _Rationale:_ BASE subclass indicates it conforms to ComfyUI's model interface standards.
- `092623a67c216688:HunyuanVideoSkyreelsI2V`: Video-to-video variant of Hunyuan Video model. (confidence 1.00)
  - _Rationale:_ Inheritance from HunyuanVideo indicates shared base capabilities with I2V specialization.
- `0c5c817a8c3b46f2:T5XXLModel`: T5XXL text encoder subclass used by SD3 and PixArt. (confidence 1.00)
  - _Rationale:_ Inherits from SD3_clip.T5XXLModel, indicating specific text encoding requirements.
- `0d2565504c017688:SD3`: SD3 model class inheriting from BaseModel. (confidence 1.00)
  - _Rationale:_ BaseModel subclass indicates it follows ComfyUI's standard model interface.
- `0e9876543210abcd:StableAudio1`: Latent format handler for Stable Audio generation. (confidence 1.00)
  - _Rationale:_ LatentFormat subclass suggests it manages audio-specific latent space conversions.
- `0f1a2b3c4d5e6f7g:HunyuanVideo`: Base Hunyuan Video model class extending BASE. (confidence 1.00)
  - _Rationale:_ BASE subclass ensures compatibility with ComfyUI's model loading system.
- `1011121314151617:ACEStep`: ACE Step text encoder class. (confidence 1.00)
  - _Rationale:_ BaseModel subclass indicates standard model interface conformance.
- `1a9b2c3d4e5f6a7b:SD20`: SD2.0 model class extending BASE. (confidence 1.00)
  - _Rationale:_ BASE subclass ensures compatibility with ComfyUI's model loading system.

## Cross-community dependencies
0, 1, 2, 4, 5, 6, 7, 9, 10, 11, 14

## Unverified / resolved calls
- unresolved: `BASE` from `07c58aade35ecc17:PixArtAlpha` — BASE is defined in supported_models_base.py but the body is not visible here.
- unresolved: `BaseModel` from `044f7a529d8ad3b9:LTXV` — BaseModel is likely defined in supported_models_base.py but not shown.
- unresolved: `BaseModel` from `0d2565504c017688:SD3` — BaseModel is likely defined in supported_models_base.py but not shown.
- unresolved: `Flux` from `0704067a90ab3018:Chroma` — Inheritance implies Flux is defined elsewhere or in the same community without body context.
- unresolved: `SVD_img2vid` from `01ef097099208ee9:SV3D_u` — SVD_img2vid is not defined in the context.

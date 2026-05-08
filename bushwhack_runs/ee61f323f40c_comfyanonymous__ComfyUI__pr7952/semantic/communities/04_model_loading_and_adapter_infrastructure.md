# Community 4: Model Loading and Adapter Infrastructure

**Purpose:** This community manages the loading and configuration of diffusion models, checkpoints, and various weight adapters (LoRA, GLIGEN, OFT) within ComfyUI. It serves as a bridge between the core execution engine (ComfyUI's model graph) and external model artifacts, providing standardized interfaces for loading, patching, and adapting model weights. The community coordinates with downstream agents handling image/video I/O and node execution.

## Files
- `comfy/sd.py`: Core model loading infrastructure for checkpoints and checkpoints-only loaders. Provides `load_checkpoint`, `load_checkpoint_only`, and related functions to initialize UNET, CLIP, and VAE components. This file likely orchestrates the main model graph construction. (confidence 0.90)
- `comfy/diffusers_load.py`: Wraps Diffusers pipeline loaders for compatible model formats. Bridges HuggingFace Diffusers models into ComfyUI's execution graph. (confidence 0.85)
- `comfy/weight_adapter/base.py`: Defines base interfaces for weight adapter implementations. Likely includes `WeightAdapterBase` which is extended by specific adapter classes like `LoRAAdapter`, `OFTAdapter`. (confidence 0.90)
- `comfy/weight_adapter/loha.py`: Implements HoRA adapter for LoRA model weight adjustments. (confidence 0.85)
- `comfy/weight_adapter/lokr.py`: Implements LoKr adapter for LoRA model weight adjustments. (confidence 0.85)
- `comfy/weight_adapter/lora.py`: Implements standard LoRA adapter logic for model weight injection. (confidence 0.90)
- `comfy/weight_adapter/oft.py`: Implements OFT adapter logic for model weight adjustments. (confidence 0.85)
- `comfy/weight_adapter/glora.py`: Implements GLoRA adapter logic for model weight adjustments. (confidence 0.85)
- `comfy/weight_adapter/boft.py`: Implements BOFT adapter logic for model weight adjustments. (confidence 0.85)
- `comfy/weight_adapter/lora.py`: Implements standard LoRA adapter logic for model weight injection. (confidence 0.90)
- `comfy/sd.py`: Provides `load_gligen`, `load_style_model`, and other specialized model loaders. (confidence 0.90)
- `comfy/sd.py`: Handles loading of GLIGEN and Style models alongside standard checkpoints. (confidence 0.90)

## Symbols
- `02c6d6a029770ec3`: Loader for ImageOnly checkpoints, likely used when only image generation is needed without audio or video processing. (confidence 0.90)
  - _Rationale:_ Class inheriting from ComfyNodeABC, specialized for image-only checkpoint loading.
- `0ddc585aa5667203`: Node to load and mask images, used to preprocess image inputs before generation. (confidence 0.90)
  - _Rationale:_ Inherits from ComfyNodeABC, implies it is a ComfyUI node for image handling.
- `065abe6a84b4ede6`: Logs warnings during startup, likely for CUDA/memory warnings or missing dependencies. (confidence 0.90)
  - _Rationale:_ Function name suggests it handles initialization warnings.
- `144b5ee6b5f50416`: Cleanup routine for temporary directories, ensures no orphaned files persist. (confidence 0.90)
  - _Rationale:_ Function name suggests it performs cleanup operations.
- `2955fbcdd2f4b657`: Core checkpoint loading function. Handles UNET, CLIP, and VAE initialization, returning model objects. (confidence 0.95)
  - _Rationale:_ Parameters indicate it configures checkpoint loading options (config, ckpt_path, output_vae/clip).
- `23319fcde7f886ac`: Loads LoRA adapters into loaded models, applying weight adjustments. (confidence 0.95)
  - _Rationale:_ Function name and parameters suggest it takes a lora path and applies it to models.
- `213cebe195ebe952`: Loads GLIGEN checkpoints for conditional guidance. (confidence 0.90)
  - _Rationale:_ Function name suggests GLIGEN model loading.
- `154671333245e578`: Logs CUDA memory allocation warnings, likely triggered during model loading. (confidence 0.90)
  - _Rationale:_ Function name implies it handles CUDA memory warnings.
- `07932dfbfd2c230d`: OFT weight adapter implementation, extends base adapter class. (confidence 0.90)
  - _Rationale:_ Inherits from WeightAdapterBase, implements OFT-specific logic.
- `0c32f44c523a9841`: LoRA weight adapter implementation, core mechanism for model weight adaptation. (confidence 0.90)
  - _Rationale:_ Inherits from WeightAdapterBase, likely contains `load_lora` logic.

## Cross-community dependencies
0, 1, 2, 3, 5, 6, 7, 8, 9, 10, 13

## Unverified / resolved calls
- unresolved: `get_save_image_path` from `255f389eafbe2cd2` — Resolves file path for saving generated images
- unresolved: `load_gligen` from `213cebe195ebe952` — GLIGEN model loader
- unresolved: `load_hook_lora_for_models` from `23319fcde7f886ac` — Function to apply LoRA to loaded models
- unresolved: `load_lora` from `23319fcde7f886ac` — Helper function for applying LoRA
- unresolved: `Model` from `2955fbcdd2f4b657` — Expected to construct Model objects from state_dict
- unresolved: `model_manager` from `13de45360d10d514` — Singleton or manager for active model instances
- unresolved: `ModelPatcher` from `2955fbcdd2f4b657` — Expected to wrap loaded model for runtime adjustments
- unresolved: `StyleModel` from `167d58b4c305b83d` — Style transfer model wrapper
- unresolved: `UNETModel` from `2955fbcdd2f4b657` — Expected to be loaded from checkpoint state dict
- unresolved: `weight_dtype` from `23319fcde7f886ac` — Data type used for weights

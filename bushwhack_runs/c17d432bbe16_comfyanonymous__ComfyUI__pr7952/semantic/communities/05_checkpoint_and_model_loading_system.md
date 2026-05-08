# Community 5: Checkpoint and Model Loading System

**Purpose:** This community implements the core infrastructure for loading, managing, and patching deep learning models (checkpoints, LoRAs, style models, GLIGEN, etc.). It serves as a foundational layer for the node execution flow by abstracting file I/O, state dict parsing, and device management for models. It directly feeds into execution nodes by providing preloaded model objects (ModelPatcher, CLIP, VAE) for use in image and video generation workflows.

## Files
- `comfy/sd.py`: Central hub for loading checkpoints (load_checkpoint, load_lora, load_gligen, load_style_model), handling CLIP/VAE separation, and managing model state dictionaries. (confidence 0.95)

## Symbols
- `02c6d6a029770ec3`: Node implementation for loading image-only checkpoints directly into the graph without CLIP/VAE components, used when only U-Net models are required. (confidence 0.95)
  - _Rationale:_ Class name and position in sd.py indicate specialized checkpoint loading logic.

## Cross-community dependencies
0, 1, 2, 3, 4, 6, 7, 8, 9, 10, 11, 12, 15

## Unverified / resolved calls
- unresolved: `model_sampling` from `2955fbcdd2f4b657` — Checkpoint loading likely interacts with model sampling schedulers defined elsewhere.
- unresolved: `ModelPatcher` from `02c6d6a029770ec3` — Checkpoint loaders likely wrap models in ModelPatcher for runtime patching, but body not shown here.

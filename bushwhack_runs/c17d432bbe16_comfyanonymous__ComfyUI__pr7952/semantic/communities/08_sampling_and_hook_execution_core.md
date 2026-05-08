# Community 8: Sampling and Hook Execution Core

**Purpose:** This community orchestrates the core sampling logic (CFG, schedulers, noise) and the hook injection system for dynamic model modification. It integrates conditioning, mask resolution, and timestep management to drive the generation process. It serves as the runtime engine for sampler nodes (e.g., KSampler) and enables advanced features like LoRA, ControlNet, and attention guidance through the Hook interface.

## Files
- `comfy/float.py`: Implements low-level float8 quantization utilities (e.g., manual stochastic rounding) used to optimize memory or precision during sampling. (confidence 1.00)

## Symbols
- `008495785142c8b6:cfg_function`: Core CFG function implementing classifier-free guidance, calculating prediction scaling based on conditional and unconditional predictions. (confidence 1.00)
  - _Rationale:_ Defined directly in the provided context, used internally by guider classes.
- `01b577c91f4423a0:DualCFGGuider`: Extension of CFGGuider allowing for dual-conditioning or alternative guidance logic (likely for Dual CFG techniques). (confidence 1.00)
  - _Rationale:_ Class definition shown, extends CFGGuider, implies specialized sampling flow.
- `035bec0355613b75:resolve_areas_and_cond_masks_multidim`: Computes overlapping areas and masks for multi-dimensional conditioning inputs. (confidence 1.00)
  - _Rationale:_ Function signature shows it takes conditions and dims, likely used before sampling to resolve spatial conditioning.
- `062c1ec21e38b6ab:simple_scheduler`: Generates simple noise schedule steps for diffusion. (confidence 1.00)
  - _Rationale:_ Takes model_sampling and steps, returns scheduler logic.
- `06b1e5dd67eff966:get_mask_aabb`: Calculates Axis-Aligned Bounding Box for masks, likely for optimizing conditioning area calculations. (confidence 1.00)
  - _Rationale:_ Function name implies bounding box computation on mask tensors.
- `09d2d9b5e2c76da6:calculate_start_end_timesteps`: Determines start and end noise timesteps based on conditioning data. (confidence 1.00)
  - _Rationale:_ Takes model and conds, calculates temporal bounds for generation.
- `14a1df553aca88c4:PerturbedAttentionGuidance`: Class implementing Perturbed Attention Guidance (PAG) technique, likely modifying attention maps during generation. (confidence 1.00)
  - _Rationale:_ Specific class name matches the PAG technique.
- `18c554323cf113d8:get_patch_weights_from_model`: Extracts patch weights from a ModelPatcher instance. (confidence 1.00)
  - _Rationale:_ Function extracts weights, used by hook systems or LoRA.
- `2bdf317f8184fafa:preprocess_conds_hooks`: Prepares conditional inputs by applying registered hooks (e.g., scaling, injection) before the main sampling loop. (confidence 1.00)
  - _Rationale:_ Function takes conds and processes them, essential setup step.
- `240eabecdcf3ecfb:combine_with_new_conds`: Merges existing conditioning with new conditioning data. (confidence 1.00)
  - _Rationale:_ Function signature indicates merging lists of conds.
- `281b86abc56ad7dc:CreateHookModelAsLoraModelOnly`: Creates a specific hook type to apply modifications as LoRA-like behavior. (confidence 1.00)
  - _Rationale:_ Class name explicitly ties to LoRA hooking mechanism.
- `294f5a140f482495:KSampler`: Main implementation of the standard sampler node (KSampler), handling the iterative noise reduction. (confidence 1.00)
  - _Rationale:_ Standard class name, likely the entry point for most sampling flows.
- `2decc7d5fca07ff6:TransformerOptionsHook`: Hook designed to inject options or callbacks into the transformer forward pass. (confidence 1.00)
  - _Rationale:_ Class extends Hook, likely wraps transformer execution logic.
- `404722d189a7db0a:encode_model_conds`: Encodes model conditioning data (prompts, etc.) into tensor representations usable by the sampler. (confidence 1.00)
  - _Rationale:_ Takes noise, device, prompt_type; standard preprocessing function.
- `431c2416a0ab6b4d:ConditioningSetDefaultAndCombine`: Handles default conditioning settings and combines them with other conditions. (confidence 1.00)
  - _Rationale:_ Name implies setting defaults and merging condition sets.

## Cross-community dependencies
0, 1, 2, 3, 4, 5, 6, 7, 9, 10

## Unverified / resolved calls
- unresolved: `attention` from `294f5a140f482495:KSampler` — Attention mechanism reference.
- unresolved: `BaseModel` from `294f5a140f482495:KSampler` — Base class for UNet or CLIP models being sampled.
- unresolved: `Boolean` from `294f5a140f482495:KSampler` — Boolean type check.
- unresolved: `calculate_weight` from `294f5a140f482495:KSampler` — Weight calculation for LoRA.
- unresolved: `cast_to_device` from `294f5a140f482495:KSampler` — Device placement utility.
- unresolved: `CFGGuider` from `294f5a140f482495:KSampler` — KSampler likely instantiates or inherits from CFGGuider for the guidance logic.
- unresolved: `CLIP` from `294f5a140f482495:KSampler` — CLIP model reference.
- unresolved: `ClipVisionModel` from `294f5a140f482495:KSampler` — CLIP vision encoder for image prompting.
- unresolved: `conditioning_set_values_with_hooks` from `294f5a140f482495:KSampler` — Setting values with hooks.
- unresolved: `ConditioningSetValues` from `294f5a140f482495:KSampler` — Setting conditioning values.
- unresolved: `ControlBase` from `294f5a140f482495:KSampler` — Base class for ControlNet.
- unresolved: `ControlLora` from `294f5a140f482495:KSampler` — Control LoRA implementation.
- unresolved: `ControlNet` from `294f5a140f482495:KSampler` — ControlNet implementation.
- unresolved: `controlnet_preprocess` from `294f5a140f482495:KSampler` — Preprocessing for ControlNet inputs.
- unresolved: `Conv` from `294f5a140f482495:KSampler` — Convolutional layers in UNet.
- unresolved: `copy_to_param` from `294f5a140f482495:KSampler` — Parameter copying utility.
- unresolved: `default` from `294f5a140f482495:KSampler` — Default values or functions.
- unresolved: `execute` from `294f5a140f482495:KSampler` — Standard execution flow for nodes.
- unresolved: `exists` from `294f5a140f482495:KSampler` — Existence check for models or files.
- unresolved: `free_memory` from `294f5a140f482495:KSampler` — PyTorch memory cleanup.
- unresolved: `get_filename_list` from `294f5a140f482495:KSampler` — Listing model filenames.
- unresolved: `get_folder_paths` from `294f5a140f482495:KSampler` — Retrieving system paths for model directories.
- unresolved: `get_free_memory` from `294f5a140f482495:KSampler` — System memory check.
- unresolved: `get_full_path` from `294f5a140f482495:KSampler` — Getting full file paths.
- unresolved: `KSampler` from `294f5a140f482495:KSampler` — Typo or alias for self.
- unresolved: `load_checkpoint_guess_config` from `294f5a140f482495:KSampler` — Loading models with config guessing.
- unresolved: `load_diffusion_model_state_dict` from `294f5a140f482495:KSampler` — Loading diffusion model weights.
- unresolved: `load_gligen` from `294f5a140f482495:KSampler` — Loading Gligen control modules.
- unresolved: `load_lora` from `294f5a140f482495:KSampler` — Loading LoRA weights.
- unresolved: `load_models_gpu` from `294f5a140f482495:KSampler` — Loading models into GPU memory is a prerequisite for sampling.
- unresolved: `load_state_dict_guess_config` from `294f5a140f482495:KSampler` — Loading state dict with config guess.
- unresolved: `load_torch_file` from `294f5a140f482495:KSampler` — Loading Torch model files.
- resolved: `loaded_models` from `294f5a140f482495:KSampler` — Global registry of loaded models.
  - Retrieves a list of currently loaded models, allowing the system to manage or free memory when necessary. (Directly linked to `cleanup_temp` and `VRAMState` logic for tracking active resources.)
- unresolved: `minimum_inference_memory` from `294f5a140f482495:KSampler` — Memory optimization logic.
- unresolved: `model_lora_keys_clip` from `294f5a140f482495:KSampler` — Keys for CLIP LoRA.
- unresolved: `model_lora_keys_unet` from `294f5a140f482495:KSampler` — Keys for UNet LoRA.
- unresolved: `model_sampling` from `294f5a140f482495:KSampler` — Schedulers and samplers require a model_sampling object to calculate noise schedules.
- unresolved: `module_size` from `294f5a140f482495:KSampler` — Size calculation for model layers.
- unresolved: `prepare_mask` from `294f5a140f482495:KSampler` — Preparation of masks for conditioning is necessary for area-based sampling.
- unresolved: `run` from `294f5a140f482495:KSampler` — Execution method for nodes or loops.
- unresolved: `sampler_object` from `294f5a140f482495:KSampler` — Reference to the sampler implementation instance.
- unresolved: `SamplerEulerCFGpp` from `294f5a140f482495:KSampler` — Specific Euler sampler variant.
- unresolved: `SamplerLCMUpscale` from `294f5a140f482495:KSampler` — Upscaling sampler implementation.
- unresolved: `SelfAttentionGuidance` from `294f5a140f482495:KSampler` — Self-attention based guidance.
- unresolved: `set_model_options_post_cfg_function` from `294f5a140f482495:KSampler` — Callback for modifying model options after CFG calculation.
- unresolved: `SkipLayerGuidanceDiT` from `294f5a140f482495:KSampler` — Guidance technique for DiT architectures.
- unresolved: `UNetModel` from `294f5a140f482495:KSampler` — UNet architecture definition.
- unresolved: `VAE` from `294f5a140f482495:KSampler` — Variational Autoencoder for decoding latent to image.

# Community 7: Sampler & CFG Hooking Infrastructure

**Purpose:** This community manages the execution of sampling schedulers, CFG (Classifier-Free Guidance) logic, and extensible hooking mechanisms for modifying model behavior during inference. It provides core utilities for conditioning combination, patch weight calculation, and orchestrating hooks like WeightHook, InjectionsHook, and TransformerOptionsHook to inject custom logic (e.g., PerturbedAttentionGuidance, PerpNeg) into the diffusion pipeline. It bridges high-level sampler nodes with lower-level model execution flow, relying on external loading and checkpoint management to provide model instances.

## Files
- `comfy/sampler_helpers.py`: Core utilities for sampling logic, including CFG function definition, conditioning combination, mask resolution, and timestep calculation. Contains key functions like calc_cond_batch and resolve_areas_and_cond_masks_multidim. (confidence 1.00)
- `comfy/samplers.py`: Implements KSampler and CFGGuider classes, which serve as the main entry points for sampling operations. Orchestrates the sampling loop and integrates with the patcher system. (confidence 1.00)
- `comfy/model_patcher.py`: Provides the hooking infrastructure for models, allowing dynamic modification of model parameters and execution flow via patcher extensions. Contains the Hook base class and specific implementations like WeightHook. (confidence 1.00)
- `comfy/hooks.py`: Defines various hook classes and utilities (e.g., CombineHooks, ConditionalSetProperties) used to attach custom logic to conditioning inputs and model patches. (confidence 1.00)
- `comfy_extras/nodes_hooks.py`: Provides ComfyUI nodes for advanced hook manipulation, such as CreateHookModelAsLoraModelOnly and handling keyframes for hooks. (confidence 1.00)
- `comfy_extras/nodes_pag.py`: Implements PerturbedAttentionGuidance, an optional enhancement for sampling that perturbs attention mechanisms for specific effects. (confidence 1.00)
- `comfy_extras/nodes_perpneg.py`: Implements PerpNegGuider, which guides generation by using perpendicular negative conditioning to influence latent space directions. (confidence 1.00)
- `comfy/float.py`: Implements float8 precision utilities (manual_stochastic_round_to_float8, calc_mantissa) needed for efficient model operations. (confidence 0.50)

## Symbols
- `008495785142c8b6:cfg_function`: Executes the core Classifier-Free Guidance logic by blending conditional and unconditional predictions based on scale. (confidence 1.00)
  - _Rationale:_ Defined with model, cond_pred, uncond_pred, and cond_scale args, typical of CFG math.
- `294f5a140f482495:KSampler`: Main sampling class driving the iterative denoising process for diffusion models. (confidence 1.00)
  - _Rationale:_ Implements the standard KSampler interface used in most ComfyUI pipelines.
- `1b8f9dade431a9af:CFGGuider`: Guides the sampling process by applying CFG logic (via cfg_function) and managing model patches during sampling. (confidence 1.00)
  - _Rationale:_ Extends guider logic typically associated with CFG sampling.
- `14a1df553aca88c4:PerturbedAttentionGuidance`: Modifies attention maps during sampling to reduce repetition or enhance style via perturbation. (confidence 1.00)
  - _Rationale:_ Named class implementing guidance logic.
- `1ab25ab07a014a3c:PerpNegGuider`: Guides generation by using perpendicular negative conditioning to influence latent space directions. (confidence 1.00)
  - _Rationale:_ Named class implementing guidance logic.
- `3af9eff3dbff749b:Hook`: Base class for extensible patches on models, allowing hooks to intercept and modify model execution flow. (confidence 1.00)
  - _Rationale:_ Used as a base for WeightHook, InjectionsHook, etc.
- `12af667726a3b379:get_area_and_mult`: Computes area multipliers for conditioning masks, ensuring conditional regions are weighted correctly during sampling. (confidence 1.00)
  - _Rationale:_ Handles area and mult logic for conditioning.
- `20e4757a9d6f3fec:HookKeyframe`: Represents time-based or step-based keyframes for dynamic hook behavior changes during sampling. (confidence 1.00)
  - _Rationale:_ Keyframe implies dynamic adjustment over time/steps.

## Cross-community dependencies
0, 1, 2, 3, 4, 5, 6, 8, 10

## Unverified / resolved calls
- unresolved: `attention` from `12af667726a3b379:get_area_and_mult` — Likely refers to attention mechanisms used in diffusion models, potentially imported.
- unresolved: `BaseModel` from `1b8f9dade431a9af:CFGGuider` — Abstract base class for model handling, likely from model wrapper module.
- unresolved: `calc_cond_batch` from `12af667726a3b379:get_area_and_mult` — Internal call within sampling loop to compute condition batches.
- unresolved: `calculate_weight` from `1b8f9dade431a9af:CFGGuider` — Likely used for weighting patches during guidance, but implementation is not visible here.
- unresolved: `CLIP` from `1b8f9dade431a9af:CFGGuider` — Likely the CLIP model class used for text encoding, defined in model loader.
- unresolved: `conditioning_set_values` from `294f5a140f482495:KSampler` — Function to set values in conditioning data, likely imported from conditioning module.
- unresolved: `ControlNet` from `1b8f9dade431a9af:CFGGuider` — Referenced as a type of model or patch, likely defined in controlnet module.
- unresolved: `DualCFGGuider` from `1b8f9dade431a9af:CFGGuider` — Referenced in config or inheritance, but class body is not provided.
- unresolved: `load_lora` from `294f5a140f482495:KSampler` — Function to load LoRA weights, likely imported from model loader module.
- unresolved: `load_models_gpu` from `294f5a140f482495:KSampler` — Function to load models onto GPU, likely from model loader.
- unresolved: `load_state_dict_guess_config` from `12af667726a3b379:get_area_and_mult` — Likely used for loading model weights/config, defined in model loader.
- resolved: `model_lora_keys_clip` from `294f5a140f482495:KSampler` — Likely referenced when applying LoRA during sampling, but definition is external.
  - Extracts or maps keys specifically for CLIP-based LoRA modifications. (Function name indicates key mapping logic for LoRA applied to CLIP encoders.)
- unresolved: `model_sampling` from `294f5a140f482495:KSampler` — Expected object passed to schedulers (ddim, normal, etc.) to handle noise schedules.
- unresolved: `resolve_areas_and_cond_masks_multidim` from `12af667726a3b379:get_area_and_mult` — Function used for resolving conditioning areas, defined in sampler_helpers.
- unresolved: `run` from `294f5a140f482495:KSampler` — Generic method call, likely on a loader or executor.
- unresolved: `sample` from `294f5a140f482495:KSampler` — Likely refers to a specific sampling method or entry point in a different module.
- unresolved: `set_model_options_post_cfg_function` from `1b8f9dade431a9af:CFGGuider` — Called to modify model options after CFG processing, implementation located in sampler_helpers or elsewhere.

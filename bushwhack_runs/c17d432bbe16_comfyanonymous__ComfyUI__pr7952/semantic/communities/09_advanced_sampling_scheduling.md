# Community 9: Advanced Sampling & Scheduling

**Purpose:** This community provides the core noise scheduling, step-size calculation, and adaptive solver implementations for diffusion model sampling. It sits at the intersection of low-level diffusion math (sigmas, noise) and high-level orchestration (KSampler nodes), offering both deterministic and stochastic sampling algorithms (DPM++ variants, UniPC, Euler) as well as utility functions for scheduler selection and configuration.

## Files
- `comfy/extra_samplers/uni_pc.py`: Implements the UniPC solver (Unified Predictor-Corrector), including step-size controllers, predictor/corrector logic, and adaptive order selection. It bridges the gap between fixed-step schedulers and dynamic sampling strategies. (confidence 0.85)
- `comfy/k_diffusion/deis.py`: Provides the Deep Iterative Solver (DEIS) and related coefficient calculations for variable-order sampling. Handles the tabulation and interpolation logic required for fast, high-accuracy sampling. (confidence 0.75)
- `comfy/k_diffusion/sampling.py`: Hosts the main step-function implementations for DPM variants (DPM++, DPM-2, ResMultistep, Euler variants with CFG+PP) and noise scheduling functions (Karras, VP, PolyExponential, Laplace). This is the primary execution hub for sampling. (confidence 0.92)
- `comfy/k_diffusion/utils.py`: Contains low-level utility functions for tensor operations, noise generation (Logistic, Uniform, Split-Log-Normal), and scheduling helpers (append_zero, append_dims). These are prerequisites for the sampling functions above. (confidence 0.88)
- `comfy_extras/nodes_advanced_samplers.py`: Exposes the advanced sampling capabilities to the user via ComfyUI nodes, including KSamplerSelect and conditioning range logic. It acts as the UI entry point for the underlying k_diffusion logic. (confidence 0.90)

## Symbols
- `get_sigmas_karras`: Generates a sequence of sigmas following the Karras distribution, which emphasizes high-sigma (noise) regions for faster convergence. Commonly used in Stable Diffusion sampling. (confidence 0.90)
  - _Rationale:_ Function signature shows n, sigma_min, sigma_max, rho parameters, typical for variance scaling in diffusion.
- `get_sigmas_vp`: Implements VPSDE (Variance Preserving SDE) sigma scheduling, a standard approach for continuous-time diffusion models. (confidence 0.85)
  - _Rationale:_ Parameters beta_d, beta_min, eps_s indicate variance schedule configuration for SDEs.
- `sample_dpmpp_3m_sde`: 3rd-order multistep DPM solver with SDE noise and Euler-Maruyama integration for stochastic sampling. (confidence 0.85)
  - _Rationale:_ Named 'dpmpp_3m_sde' with s_noise and eta parameters indicates stochastic DPM++ 2M variant.
- `sample_euler_cfg_pp`: Euler solver variant that includes classifier-free guidance (CFG) post-processing (PP). (confidence 0.85)
  - _Rationale:_ Suffix 'cfg_pp' explicitly marks guidance correction logic.
- `UniPC`: Main class for Unified Predictor-Corrector algorithm, allowing high-order accuracy with adaptive step sizes. (confidence 0.85)
  - _Rationale:_ Class name and presence of PIDStepSizeController and EMAWarmup in context confirm implementation of predictor-corrector logic.
- `sample_unipc_bh2`: Wrapper for UniPC solver with specific backward-H2 (BH2) integration settings for stability. (confidence 0.80)
  - _Rationale:_ Named 'sample_unipc_bh2' indicates a specific variant configuration of the main UniPC class.
- `ConditioningTimestepsRange`: Utility class to define ranges of timesteps (0 to max_steps) for conditioning inputs or scheduler application. (confidence 0.80)
  - _Rationale:_ Used to slice conditioning data across the denoising trajectory.

## Cross-community dependencies
0, 1, 2, 3, 5, 6, 7, 8, 10, 13

## Unverified / resolved calls
- unresolved: `PIDStepSizeController` from `UniPC` — Internal helper class for controlling step size dynamically during UniPC integration.
- unresolved: `PIDStepSizeController` from `sample_dpm_adaptive` — Used for adaptive step sizing in adaptive DPM solvers.
- unresolved: `predict_eps_sigma` from `sample_dpmpp_3m_sde` — Core utility function to extract epsilon (noise) and sigma (noise level) from model outputs.
- unresolved: `sample_er_sde` from `sample_dpmpp_3m_sde` — Likely a fallback or auxiliary SDE solver used for gradient estimation or specific noise injection scenarios.

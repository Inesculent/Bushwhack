# Community 8: Model Sampling Infrastructure

**Purpose:** This community provides core infrastructure for defining model noise schedules and sampling strategies in diffusion models. It abstracts the mathematical transformations between time steps and latent spaces, supporting various diffusion variants (DDPM, V-prediction, continuous time). The community connects to scheduler factories and sampler functions to enable model-specific sampling workflows.

## Files
- `comfy/model_sampling.py`: Defines base and variant model sampling classes (ModelSamplingSD3, ModelSamplingLTXV, ModelSamplingFlux, etc.) that encapsulate noise schedule logic. These classes determine how sigmas are computed and how timesteps map to noise levels for different architectures. (confidence 0.90)
- `comfy/k_diffusion/sampling.py`: Implements diverse sampling algorithms (e.g., Euler, DPM, IPNDM) as functions that take sigmas and models to generate outputs. These functions rely on ModelSampling classes for sigma computation and are the execution entry point for image/video generation. (confidence 0.90)
- `comfy/k_diffusion/utils.py`: Utility functions for noise generation and scheduling math (get_sigmas_karras, get_sigmas_polyexponential, append_zero, rescale_zero_terminal_snr_sigmas). Supports sigma curve customization and numerical stability. (confidence 0.90)
- `comfy/k_diffusion/deis.py`: Experimental or alternative diffusion integration (DEIS = Denoising Equation Independent Sampling). May provide optimization or specialized sampling paths not covered in core utils. (confidence 0.50)

## Symbols
- `0293a35852276b74`: ModelSamplingSD3 implements noise schedule logic specifically for Stable Diffusion 3 architecture, extending base discrete sampling to handle multi-branch time embeddings. (confidence 0.90)
  - _Rationale:_ Class name indicates SD3-specific adaptation; inherits from ModelSamplingDiscrete.
- `14764c51a30bd3ba`: ModelSamplingLTXV likely handles time sampling for LTXV (Lumina-Tech Video) diffusion models. (confidence 0.80)
  - _Rationale:_ Class name suggests LTXV architecture-specific sampling logic; inherits from ModelSamplingContinuousEDM.
- `1e8ee8f81d84e65e`: ModelSamplingFlux encapsulates sampling behavior for Flux diffusion models, likely involving continuous time embeddings. (confidence 0.90)
  - _Rationale:_ Class name matches Flux model architecture; inherits from ModelSamplingContinuousEDM.
- `39d87c0a3dc6054b`: generic_step_sampler provides a framework for iterative sampling with configurable step functions, enabling custom SDE/ODE solvers. (confidence 0.80)
  - _Rationale:_ Function name indicates generic sampling with step_function callback.
- `315a2c66c40fbc96`: sample_dpmpp_3m_sde implements a 3rd-order DPM++ solver for stochastic differential equations. (confidence 0.90)
  - _Rationale:_ Function name indicates DPM++ 3M SDE solver variant.
- `49a0aab6c57e137a`: sample_dpm_2_ancestral_RF implements ancestral DPM-2 solver with residual feedback (RF) for improved stability. (confidence 0.90)
  - _Rationale:_ Function name indicates DPM-2 ancestral solver with RF modification.

## Cross-community dependencies
0, 1, 2, 3, 4, 5, 6, 7, 10

## Unverified / resolved calls
- unresolved: `ExponentialLR` from `40c49c298f56d68` — ExponentialLR is a PyTorch learning rate scheduler, not a diffusion sampler.
- unresolved: `n_params` from `252eb9387baf294f` — N_params function counts parameters in a module, used in model analysis.
- unresolved: `step_function` from `39d87c0a3dc6054b` — generic_step_sampler takes step_function as a callback for solver logic.

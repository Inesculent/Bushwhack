# Community 5: Diffusion Sampling and Scheduling

**Purpose:** This community manages the core sampling algorithms and noise schedulers for diffusion models within ComfyUI. It bridges the gap between model definitions and user-facing generation by handling sigmas, denoising steps, and various sampler classes like Euler and DPM++.

## Files
- `comfy/samplers.py`: Defines the main `sample` function and sampler classes that orchestrate the denoising process. (confidence 0.95)
- `comfy/sample.py`: Provides high-level utility functions for preparing and executing sampling runs. (confidence 0.90)
- `comfy/model_sampling.py`: Contains classes defining the sigma schedules and noise distributions used by different model architectures (SD, Flux, etc). (confidence 0.90)
- `comfy/k_diffusion/sampling.py`: Implements specific stochastic differential equation (SDE) and ordinary differential equation (ODE) based samplers. (confidence 0.85)
- `comfy/k_diffusion/utils.py`: Contains helper functions for calculating sigmas and basic distribution handling. (confidence 0.85)
- `comfy/k_diffusion/deis.py`: Implements the Diffusion Equation-based Implicit Solver (DEIS) sampler. (confidence 0.80)

## Symbols
- `0272f0d310ad4fccb:sample`: The main entry point for the generation loop, orchestrating the diffusion process through noise injection and model evaluation. (confidence 0.95)
  - _Rationale:_ Visible in comfy/samplers.py and comfy/sample.py, it takes noise, positive/negative conditioning, and samplers as inputs.
- `0294f5a140f482495:KSampler`: A GUI node wrapper class that allows users to configure and run the `sample` function. (confidence 0.90)
  - _Rationale:_ Implements a specific user-facing interface for sampling parameters.
- `01b577c91f4423a0:DualCFGGuider`: A Guider class that handles two separate conditionings, likely for dual-prompt or reference-guided sampling. (confidence 0.85)
  - _Rationale:_ Implements the `CFGGuider` interface but manages two distinct condition inputs.
- `0180aec77ed5e404b:CFGGuider`: Base class for calculating condition strength and applying CFG (Classifier-Free Guidance) during sampling. (confidence 0.90)
  - _Rationale:_ Inherited by DualCFGGuider and PerpNegGuider, defining the standard interface for conditioning application.
- `01ab25ab07a0143c:PerpNegGuider`: Implements Perpendicular Negation guidance, a technique to reduce unwanted artifacts by modifying the negative prompt. (confidence 0.85)
  - _Rationale:_ Specific implementation of the Guider interface for a known enhancement technique.
- `004aa82ec2ada334c:calculate_sigmas`: Generates the sigma tensor based on the chosen scheduler and model parameters. (confidence 0.90)
  - _Rationale:_ Core utility for transforming step counts and model types into the time discretization for diffusion.
- `001de5d05606b13429:rescale_zero_terminal_snr_sigmas`: Adjusts sigmas to support the Zero Terminal Signal-to-Noise Ratio (SNR) scheduling strategy. (confidence 0.85)
  - _Rationale:_ Helper for advanced noise scheduling techniques.
- `029d0d29cf61500e7:calc_cond_batch`: Computes the batch of conditional predictions from the model, handling model hooks and options. (confidence 0.90)
  - _Rationale:_ Critical function called during the `sample` loop to evaluate the model against conditions.
- `00403aa630bffc38608c:SamplerEulerAncestral`: An ancestral version of the Euler sampler that introduces additional noise. (confidence 0.85)
  - _Rationale:_ Implemented in k_diffusion/sampling.py, distinct from standard Euler.
- `00423d353465ee0340f:sample_er_sde`: Executes sampling using the Euler-Riemann method for SDEs. (confidence 0.80)
  - _Rationale:_ Advanced sampler logic found in the k_diffusion module.

## Cross-community dependencies
0, 1, 2, 3, 4, 6, 7, 9, 10

## Unverified / resolved calls
- unresolved: `model_options` from `0272f0d310ad4fccb:sample` — Passes model_options to internal loops.

# Community 21: Diffusion ControlNet Extension

**Purpose:** This community defines the ControlNet extension class for ComfyUI, wrapping the base MMDiT diffusion model to enable conditional image generation control. It serves as a key integration point between ControlNet architectures and ComfyUI's core diffusion pipeline, allowing external conditioners like pose or edge maps to influence generation.

## Files
- `21006bc6c14a35f1`: Core implementation of the ControlNet wrapper class that extends MMDiT functionality with conditional input handling. (confidence 0.85)

## Symbols
- `21006bc6c14a35f1`: Main ControlNet class extending MMDiT, likely responsible for managing diffusion conditions through ControlNet-compatible inputs. (confidence 0.80)
  - _Rationale:_ Direct inheritance from comfy.ldm.modules.diffusionmodules.mmdit.MMDiT indicates specialization of the base diffusion model with ControlNet features.

## Cross-community dependencies
(none)

## Unverified / resolved calls

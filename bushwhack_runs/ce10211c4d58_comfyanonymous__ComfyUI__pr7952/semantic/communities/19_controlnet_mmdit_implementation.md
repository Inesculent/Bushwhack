# Community 19: ControlNet MMDiT Implementation

**Purpose:** This community implements a ControlNet adaptation of the MMDiT (Multi-Modal Diffusion Transformer) architecture. It extends the base MMDiT class to support ControlNet-style conditioning, enabling structured inputs (like poses or edges) to guide image generation. It connects to the broader ComfyUI diffusion pipeline by providing specialized conditioning mechanisms.

## Files
- `comfy/cldm/mmdit.py`: Implements the ControlNet MMDiT class that extends the base MMDiT diffusion transformer with ControlNet conditioning capabilities. (confidence 0.90)

## Symbols
- `symbol:21006bc6c14a35f1`: ControlNet class that inherits from MMDiT and implements ControlNet-specific conditioning logic for the multi-modal diffusion transformer. (confidence 0.95)
  - _Rationale:_ Visible in the class definition as extending MMDiT and following the ControlNet pattern (cldm module)

## Cross-community dependencies
(none)

## Unverified / resolved calls

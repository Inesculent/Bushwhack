# Community 33: CLD Type Definitions

**Purpose:** This community contains the ControlNet type definitions and enums used to control different ControlNet model variants. It establishes the mapping between string identifiers (like 'canny', 'depth', 'openpose') and their corresponding numeric IDs, enabling downstream modules to route ControlNet configurations correctly. This file serves as a foundational lookup table for other ControlNet-related components.

## Files
- `comfy/cldm/control_types.py`: Defines type enumerations and mapping dictionaries for ControlNet preprocessor types, used to parse string inputs into actionable model IDs. (confidence 0.50)

## Symbols
- `comfy/cldm/control_types.py#TypeControlNet`: Enumerates available ControlNet model types. (confidence 0.50)
  - _Rationale:_ Type enum, no method bodies provided
- `comfy/cldm/control_types.py#PreprocessorType`: Enumerates available preprocessor variants. (confidence 0.50)
  - _Rationale:_ Type enum, no method bodies provided
- `comfy/cldm/control_types.py#CONTROLNET_TYPE_MAP`: Maps string names to integer IDs for ControlNet models. (confidence 0.50)
  - _Rationale:_ Dictionary mapping visible in context, no logic bodies provided
- `comfy/cldm/control_types.py#CONTROLNET_PREPROCESSOR_MAP`: Maps string names to integer IDs for preprocessors. (confidence 0.50)
  - _Rationale:_ Dictionary mapping visible in context, no logic bodies provided

## Cross-community dependencies
(none)

## Unverified / resolved calls

# Community 31: CLDm Control Types Configuration

**Purpose:** This community defines type constants and configurations for ControlNet models, specifically managing the ControlLDM (ControlLDM) architecture parameters. It appears to establish the schema and type definitions required for ControlNet conditioning in the broader ComfyUI workflow system.

## Files
- `comfy/cldm/control_types.py`: Contains control type constants and configurations for ControlNet models, defining how conditioning inputs are structured and processed within the ControlLDM architecture. (confidence 0.95)

## Symbols
- `CONTROL_LDM`: Likely a constant or class representing the ControlLDM model type used for ControlNet configurations. (confidence 0.85)
  - _Rationale:_ Name suggests a primary model type identifier for ControlNet conditioning.
- `ControlNetConfig`: Configuration class for ControlNet model parameters and settings. (confidence 0.80)
  - _Rationale:_ Named 'Config' suggests it defines initialization parameters.
- `CldmConfig`: ControlLDM specific configuration settings. (confidence 0.80)
  - _Rationale:_ Abbreviated 'Cldm' indicates ControlLDM variant configuration.
- `ControlNet`: Main class or type representing the ControlNet model implementation. (confidence 0.90)
  - _Rationale:_ Core model class name in the ControlLDM community.

## Cross-community dependencies
(none)

## Unverified / resolved calls

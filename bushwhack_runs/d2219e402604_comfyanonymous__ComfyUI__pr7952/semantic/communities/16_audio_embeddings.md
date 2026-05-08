# Community 16: Audio Embeddings

**Purpose:** Provides audio embedding functionalities using learned positional embeddings, conditioners, and number embedders.

## Files
- `comfy/ldm/audio/embedders.py`: Defines classes and functions for handling audio embeddings including positional and number embeddings along with conditioners. (confidence 0.95)

## Symbols
- `symbol:24b2a03fdfd53cd9`: A neural network module for learned positional embeddings. (confidence 0.99)
  - _Rationale:_ Class definition directly indicates its role in handling positional embeddings.
- `symbol:bce23e2b1280e55e`: A base class for conditioners in the model. (confidence 0.98)
  - _Rationale:_ Class name and inheritance from nn.Module suggest it's a conditioner.
- `symbol:cae81d0b32030ef2`: A function to create a time-based positional embedding module. (confidence 0.98)
  - _Rationale:_ Function definition specifies creation of a positional embedding based on time.
- `symbol:cf3a7880960b82cb`: A neural network module for embedding numbers. (confidence 0.98)
  - _Rationale:_ Class name suggests its role in handling number embeddings.
- `symbol:f9e8ae51bdce6d5e`: A subclass of Conditioner specifically for number conditioning. (confidence 0.98)
  - _Rationale:_ Class name and inheritance indicate it's a specialized conditioner for numbers.

## Cross-community dependencies
4, 7

## Unverified / resolved calls
- unresolved: `manual_cast` from `UnverifiedCallSource` — Possibly used for type casting or conversion.
- resolved: `StableAudio1` from `UnverifiedCallSource` — Used in audio processing or conditioning.
  - A model named StableAudio1, inheriting from LatentFormat, possibly used for audio processing in the Stable Diffusion framework. (Inherits from LatentFormat, suggesting it's a specialized model within the system.)

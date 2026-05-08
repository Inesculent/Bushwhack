# Community 14: Audio Conditioner Embedders

**Purpose:** This community provides modular components for encoding temporal, positional, and numeric signals into condition embeddings suitable for audio generation models. It defines core embedder classes (LearnedPositionalEmbedding, TimePositionalEmbedding, NumberEmbedder) and combines them within Conditioner and NumberConditioner to process conditioning inputs. The code interfaces with external audio models (e.g., StableAudio) via cross-community dependencies, but integration details are not visible here.

## Files
- `comfy/ldm/audio/embedders.py`: Implements embedder classes and conditioner logic for audio diffusion models; contains learned embeddings for time, numbers, and positions; serves as a foundation for conditioning pipelines used by downstream audio generation agents. (confidence 1.00)

## Symbols
- `symbol:24b2a03fdfd53cd9`: A learnable positional embedding module (inherits nn.Module); likely used to inject absolute or relative position information into sequences. Directly inherits from nn.Module, so expected to implement forward() logic not shown here. Supports embedding-based conditioning workflows. (confidence 1.00)
  - _Rationale:_ Class name and inheritance pattern (nn.Module) indicate standard PyTorch embedding behavior; no implementation visible to confirm usage.
- `symbol:bce23e2b1280e55e`: Base conditioner module (inherits nn.Module); aggregates multiple embedders to produce conditioning outputs. Likely used as a parent class for structured conditioning pipelines (e.g., for audio, text, or noise schedules). (confidence 1.00)
  - _Rationale:_ Class name and inheritance indicate a modular aggregator; no forward() body shown, so exact composition unknown.
- `symbol:cae81d0b32030ef2`: Factory or helper function to instantiate TimePositionalEmbedding; takes dimensionality and output feature count. Supports dynamic configuration of time-related embeddings for diffusion steps or audio timelines. (confidence 0.95)
  - _Rationale:_ Function signature indicates configuration-driven embedder creation; no call sites visible to confirm usage frequency or scope.
- `symbol:cf3a7880960b82cb`: Numeric condition embedder (inherits nn.Module); converts numeric scalar/vector inputs (e.g., batch size, timestep, or control parameters) into embeddings for conditioning. (confidence 1.00)
  - _Rationale:_ Class name and inheritance imply embedding of numeric values; forward() body not shown to confirm exact mapping.
- `symbol:f9e8ae51bdce6d5e`: Specialized conditioner for numeric inputs; inherits from Conditioner. Likely extends base conditioning logic to handle numeric-specific preprocessing or routing. (confidence 0.90)
  - _Rationale:_ Inheritance from Conditioner and naming suggest numeric-specific handling; no forward() or method overrides visible to confirm behavior.

## Cross-community dependencies
3, 7

## Unverified / resolved calls
- unresolved: `manual_cast` from `symbol:f9e8ae51bdce6d5e` — NumberConditioner likely applies type conversions (e.g., float to int) for numeric embeddings; manual_cast may be imported from a utils or dtype conversion module.
- unresolved: `StableAudio1` from `symbol:bce23e2b1280e55e` — Conditioner may be used within or to orchestrate StableAudio1 pipelines, but no import or call body is visible here.

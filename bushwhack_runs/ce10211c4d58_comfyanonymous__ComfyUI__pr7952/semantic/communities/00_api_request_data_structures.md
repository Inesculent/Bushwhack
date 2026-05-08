# Community 0: API Request Data Structures

**Purpose:** This community defines data models, request classes, and response handlers for external AI service integrations (Luma, Stability, Pika, Kling, Recraft, BFL, etc.). It acts as the contract layer between ComfyUI nodes and remote API providers, handling serialization, validation, and error handling for API interactions.

## Files
- `comfy_api/input/basic_types.py`: Core data types and enums used across API request/response structures (confidence 0.90)
- `comfy_api/input/video_types.py`: Video-specific type definitions for API interactions (confidence 0.85)
- `comfy_api_nodes/apis/client.py`: Generic API client for making HTTP requests to external services (confidence 0.90)
- `comfy_api_nodes/apis/luma_api.py`: Luma-specific API request/response models and handlers (confidence 0.90)
- `comfy_api_nodes/nodes_bfl.py`: BFL (Black Forest Labs) API integration including Flux model requests (confidence 0.85)
- `comfy_api_nodes/apis/stability_api.py`: Stability AI API integration with request/response models (confidence 0.90)
- `comfy_api_nodes/apis/pixverse_api.py`: Pixverse API integration with request/response structures (confidence 0.85)

## Symbols
- `006580e404eb92c9:BFLFluxProGenerateRequest`: BFL Flux Pro generation request model, likely serialized for API calls to Black Forest Labs (confidence 0.90)
  - _Rationale:_ Named as a Base subclass with FluxPro-specific naming, used in nodes_bfl.py context
- `018f98283d4a3feb:LumaImageReference`: Reference object for Luma image inputs in generation requests (confidence 0.85)
  - _Rationale:_ Appears in video_types context, likely used for tracking image references in Luma API
- `05ba8f21f6bca06f:RecraftTextLayoutItem`: Text layout specification for Recraft text-in-image generation (confidence 0.85)
  - _Rationale:_ Recraft-specific data structure for layout control in image generation
- `05466bfda20e57e8:Model1`: Model type enumeration, likely selecting which AI model to use (confidence 0.85)
  - _Rationale:_ Inherits from str and Enum, suggesting model selection options
- `05fb5f6fd67ae8d3:BFLAsyncResponse`: Asynchronous response structure for BFL API calls (confidence 0.90)
  - _Rationale:_ Named as async response, likely for handling long-running generation tasks
- `0e3dbebdd304dd1b:LumaConceptsNode`: ComfyUI node implementing Luma concept integration (confidence 0.90)
  - _Rationale:_ Extends ComfyNodeABC, part of Luma node implementation
- `1091c925bc474f6a:LumaImageToVideoGenerationNode`: Node for Luma's image-to-video generation functionality (confidence 0.90)
  - _Rationale:_ Named for image-to-video with Luma branding, extends ComfyNodeABC

## Cross-community dependencies
1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 14

## Unverified / resolved calls
- unresolved: `ApiClient` from `0e3dbebdd304dd1b:LumaConceptsNode` — Likely calls ApiClient for making HTTP requests to Luma service
- unresolved: `LumaGenerationRequest` from `1091c925bc474f6a:LumaImageToVideoGenerationNode` — Likely instantiates LumaGenerationRequest for API calls
- unresolved: `PikaBodyGenerate22KeyframeGenerate22PikaframesPost` from `11c811869e30b2f5:PikaSwapsNode` — Likely uses this body model for Pika API request serialization
- unresolved: `StabilityGenerationID` from `04e56c9681f8b6b1:validate_task_creation_response` — Validation function likely checks StabilityGenerationID field in responses

# Community 0: ComfyAPI Third-Party Integration Nodes

**Purpose:** This community implements ComfyUI nodes for external generative AI services (BFL, Luma, Stability, Pixverse, Kling, Minimax, Recraft) via API clients. It provides request/response schemas (Pydantic models), validation logic, and node wrappers to integrate remote workflows into the local graph. Files include API controller interfaces, DTOs, and ComfyUI node definitions that handle async operations, credentials, and image/video outputs.

## Files
- `comfy_api_nodes/apis/PixverseController.py`: Pixverse API client implementation with image generation/upload endpoints. (confidence 0.90)
- `comfy_api_nodes/apis/luma_api.py`: Luma AI API integration for image/video generation with request DTOs and polling. (confidence 0.90)
- `comfy_api_nodes/apis/stability_api.py`: Stability AI client supporting SD3 and upscale operations with response validation. (confidence 0.90)
- `comfy_api_nodes/apis/client.py`: Generic API client base class handling HTTP requests and async polling. (confidence 0.90)
- `comfy_api_nodes/nodes_bfl.py`: BFL (Base Layer Flux) node implementations for Flux Pro generation. (confidence 0.90)
- `comfy_api_nodes/nodes_luma.py`: Luma-specific ComfyUI nodes for generation and concept management. (confidence 0.90)
- `comfy_api_nodes/nodes_kling.py`: Kling AI integration nodes for image-to-video and generation tasks. (confidence 0.90)
- `comfy_api_nodes/nodes_minimax.py`: Minimax API client and node wrappers for generative tasks. (confidence 0.90)
- `comfy_api_nodes/mapper_utils.py`: Utilities for mapping node inputs to API request formats and outputs. (confidence 0.80)
- `comfy_api_nodes/apinode_utils.py`: Helper functions for API nodes (video upload, conversion, validation). (confidence 0.80)

## Symbols
- `006580e404eb92c9:BFLFluxProGenerateRequest`: Pydantic model defining request payload for BFL Flux Pro image generation. (confidence 0.90)
  - _Rationale:_ Class structure suggests request configuration for BFL's Flux model.
- `00c0d2d28d1f6650:PollingOperation`: Generic async polling mechanism for long-running API tasks. (confidence 0.90)
  - _Rationale:_ Named 'PollingOperation' with generic type parameters indicates async task handling.
- `018f98283d4a3feb:LumaImageReference`: Schema for referencing Luma images in generation requests. (confidence 0.80)
  - _Rationale:_ Combined with LumaGenerationReference, indicates asset tracking.
- `01a2b205e9b936c8:StabilityUpscaleFastNode`: ComfyUI node for Stability AI fast upscale operations. (confidence 0.80)
  - _Rationale:_ Name combines Stability, Upscale, Fast indicating specific model endpoint.
- `04e56c9681f8b6b1:validate_task_creation_response`: Validation function for API task creation responses. (confidence 0.80)
  - _Rationale:_ Function name implies error checking for task initiation.
- `05466c9681f8b6b1:ApiClient`: Base HTTP client class for API communication. (confidence 0.90)
  - _Rationale:_ Standard naming for API layer abstractions.
- `05bd382b6bbc2706:Model1`: String enum for selecting model variants. (confidence 0.70)
  - _Rationale:_ Enum naming convention for model selection.
- `06f4ea2859625fca:convert_mask_to_image`: Utility to convert torch tensor masks to image format. (confidence 0.90)
  - _Rationale:_ Takes torch.Tensor as input, returns image format.
- `0e3dbebdd304dd1b:LumaConceptsNode`: Node to manage Luma generation concepts/presets. (confidence 0.90)
  - _Rationale:_ ComfyNodeABC inheritance indicates workflow integration.
- `147a68234a2454b6:LumaImageGenerationRequest`: Request DTO for Luma image generation tasks. (confidence 0.90)
  - _Rationale:_ Pydantic model structure for API payloads.
- `11c811869e30b2f5:PikaSwapsNode`: Node for Pika AI frame swap operations. (confidence 0.80)
  - _Rationale:_ Pika namespace and Swaps indicate video frame manipulation.
- `14d4238de9ae6f18:PixverseImageUploadResponse`: Response schema for Pixverse image uploads. (confidence 0.80)
  - _Rationale:_ Pixverse endpoint response structure.

## Cross-community dependencies
1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 15, 18

## Unverified / resolved calls
- unresolved: `ComfyNodeABC` from `0e3dbebdd304dd1b:LumaConceptsNode` — Inherits from base ComfyUI node class defined elsewhere.
- unresolved: `requests` from `05466c9681f8b6b1:ApiClient` — HttpClient dependency likely imported from standard library.
- unresolved: `torch` from `147a68283d4a3feb:LumaImageGenerationRequest` — Likely uses torch tensors for video/image data in requests.

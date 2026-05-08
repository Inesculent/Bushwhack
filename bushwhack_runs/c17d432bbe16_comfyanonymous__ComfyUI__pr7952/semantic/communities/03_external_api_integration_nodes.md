# Community 3: External API Integration Nodes

**Purpose:** This community provides ComfyUI node implementations for external AI service providers (Luma, Pika, Kling, Ideogram, Minimax, Stability, OpenAI, Pixverse). It bridges internal ComfyUI execution with remote generation APIs through specialized request builders, response validators, and polling mechanisms for async task management.

## Files
- `comfy_api_nodes/apis/luma_api.py`: Luma API client implementations for image/video generation requests and response parsing. (confidence 0.90)
- `comfy_api_nodes/apis/pixverse_api.py`: Pixverse API client for video transitions and content generation. (confidence 0.85)
- `comfy_api_nodes/apis/stability_api.py`: Stability AI API client for image generation and style presets. (confidence 0.90)
- `comfy_api_nodes/nodes_kling.py`: Kling-specific nodes for text-to-video, virtual try-on, and audio features. (confidence 0.85)
- `comfy_api_nodes/nodes_luma.py`: Luma AI node wrappers for image-to-video and reference generation. (confidence 0.90)
- `comfy_api_nodes/nodes_pika.py`: Pika Labs node wrappers for video generation and swaps. (confidence 0.85)
- `comfy_api_nodes/mapper_utils.py`: Utility functions for converting tensor inputs to API-compatible formats. (confidence 0.90)
- `comfy_api_nodes/apis/client.py`: Generic API client for async HTTP requests and polling. (confidence 0.85)

## Symbols
- `symbol:00c0d2d28d1f6650`: PollingOperation class manages asynchronous task status checking with generic return types. (confidence 0.90)
  - _Rationale:_ Used for async API calls where immediate completion is not guaranteed.
- `symbol:0e3dbebdd304dd1b`: LumaConceptsNode handles Luma concept selection for generation. (confidence 0.85)
  - _Rationale:_ Named as Luma-specific concept selection node.
- `symbol:0f9e1925d0ade803`: Test function validates model field to float conversion logic. (confidence 0.80)
  - _Rationale:_ Test utility for node input validation.
- `symbol:1c37992d58007920`: Converts BytesIO image data to tensor format. (confidence 0.85)
  - _Rationale:_ Helper for API response data normalization.
- `symbol:1d08501cbd1d199e`: Converts tensor images to base64 for API payload. (confidence 0.90)
  - _Rationale:_ Standard format for image data transmission.
- `symbol:20de29d1e3d83c96`: Converts tensor to data URI string. (confidence 0.85)
  - _Rationale:_ Utility for embedding images in HTML responses.

## Cross-community dependencies
0, 1, 2, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 16

## Unverified / resolved calls
- unresolved: `convert_image_to_base64` from `symbol:1d08501cbd1d199e` — Internal utility for data encoding
- unresolved: `LumaConceptsNode` from `symbol:0e3dbebdd304dd1b` — Calls to Luma API endpoints
- unresolved: `PollingOperation` from `symbol:00c0d2d28d1f6650` — Likely uses API status checking mechanisms
- unresolved: `tensor_to_data_uri` from `symbol:20de29d1e3d83c96` — Utility for image output formatting

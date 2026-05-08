# Community 16: WebSocket API Examples

**Purpose:** Provides example code for interacting with ComfyUI's websockets API to queue prompts and retrieve generated images. Serves as a practical reference for external clients connecting to the ComfyUI websocket server, demonstrating how to submit prompts and fetch results asynchronously.

## Files
- `script_examples/websockets_api_example.py`: Example script demonstrating WebSocket client implementation for ComfyUI API interactions, including prompting and image retrieval workflows. (confidence 0.70)

## Symbols
- `get_image`: Retrieves an image file from the server given filename metadata, likely used after prompt completion to fetch output images. (confidence 0.60)
  - _Rationale:_ Function signature suggests image retrieval from a server-side file path; common pattern in async generation workflows.
- `get_images`: Fetches all generated images for a given prompt ID through WebSocket connection, likely polling for completion. (confidence 0.60)
  - _Rationale:_ Takes WebSocket object and prompt as parameters; aligns with typical post-generation result retrieval logic.
- `queue_prompt`: Submits a prompt to the ComfyUI server for execution, initiating the generation pipeline. (confidence 0.80)
  - _Rationale:_ Standard entry point for triggering image generation tasks in ComfyUI's API ecosystem.
- `get_history`: Retrieves generation history or status for a specific prompt ID, useful for tracking completion state. (confidence 0.70)
  - _Rationale:_ Typical pattern for checking task status in asynchronous UI workflows.

## Cross-community dependencies
(none)

## Unverified / resolved calls

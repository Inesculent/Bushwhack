# Community 19: Websocket API Example

**Purpose:** Provides examples of using WebSockets for image generation and management.

## Files
- `script_examples/websockets_api_example.py`: Demonstrates WebSocket API usage for image generation, queuing prompts, and retrieving images and history. (confidence 1.00)

## Symbols
- `symbol:2a1e235802956bac:get_image`: Retrieves an image based on filename, subfolder, and folder type. (confidence 1.00)
  - _Rationale:_ Function definition suggests fetching an image from a specified location.
- `symbol:8249c7ec8fc6b8f7:get_images`: Fetches multiple images based on a WebSocket connection and a given prompt. (confidence 1.00)
  - _Rationale:_ Function takes a WebSocket object and a prompt to retrieve images.
- `symbol:977bb20ab0f7627f:queue_prompt`: Queues a prompt for processing. (confidence 1.00)
  - _Rationale:_ Function definition implies adding a prompt to a queue for later processing.
- `symbol:b81790956d42aa62:get_history`: Retrieves the history associated with a specific prompt ID. (confidence 1.00)
  - _Rationale:_ Function definition indicates fetching history data related to a prompt ID.

## Cross-community dependencies
(none)

## Unverified / resolved calls

# Community 17: WebSocket API Client Examples

**Purpose:** This community provides Python script examples demonstrating how to interact with ComfyUI's WebSocket API. It enables programmatic control for prompting image generation, retrieving history, and fetching generated images externally. The scripts serve as reference implementations for integrating ComfyUI into external workflows or tools.

## Files
- `script_examples/websockets_api_example.py`: Main example script that implements a WebSocket client for interacting with ComfyUI. Contains helper functions for common API operations including prompting, history tracking, and image retrieval. (confidence 0.95)

## Symbols
- `symbol:2a1e235802956bac:get_image`: Utility function to retrieve and save generated images from the server after completion. Takes filename and location parameters to construct the proper request path. (confidence 1.00)
  - _Rationale:_ Function signature indicates it handles file retrieval for images stored in subfolders or custom folder types.
- `symbol:8249c7ec8fc6b8f7:get_images`: Batch retrieval function that connects via WebSocket to fetch images associated with a given prompt. Likely processes multiple images from a single generation run. (confidence 1.00)
  - _Rationale:_ Takes WebSocket connection and prompt ID as inputs, suggesting it iterates through results returned from the server for that prompt.
- `symbol:977bb20ab0f7627f:queue_prompt`: Core function for submitting generation prompts to the ComfyUI backend via WebSocket. Accepts a prompt dictionary containing the workflow graph and parameters. (confidence 1.00)
  - _Rationale:_ Standard naming convention for API endpoints that initiate background tasks; receives a prompt object as input.
- `symbol:b81790956d42aa62:get_history`: Retrieves the execution history for a specific prompt ID, including output images and node performance metrics. (confidence 1.00)
  - _Rationale:_ Takes prompt_id parameter and returns historical data about the generation job, likely including status and results.

## Cross-community dependencies
(none)

## Unverified / resolved calls

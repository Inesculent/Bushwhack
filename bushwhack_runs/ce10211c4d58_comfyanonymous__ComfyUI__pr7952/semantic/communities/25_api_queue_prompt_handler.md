# Community 25: API Queue Prompt Handler

**Purpose:** This community centers on demonstrating how to use ComfyUI's programmatic API to queue a prompt for generation. It serves as a minimal example for external clients to understand the data structure required to trigger image/video processing pipelines via HTTP requests. The functionality bridges user code with ComfyUI's internal scheduler and execution engine.

## Files
- `script_examples/basic_api_example.py`: An example script showing the correct request format to queue a prompt. It defines the `queue_prompt` helper function which structures the JSON payload and sends it to the API endpoint. This is a reference implementation for API consumers. (confidence 0.95)

## Symbols
- `64caffea39f61845`: A local helper function that wraps the API call logic. It takes a `prompt` dictionary (containing node definitions and parameters) and serializes it to be sent via the API. This function is critical for downstream agents needing to understand how to format request bodies. (confidence 0.90)
  - _Rationale:_ Visible in the provided symbol context as the core logic for submitting a job to the workflow engine.

## Cross-community dependencies
(none)

## Unverified / resolved calls

# Community 27: API Prompting Interface

**Purpose:** This community exposes a simple API endpoint function that allows external clients to queue ComfyUI workflows for execution. It acts as a bridge between raw API requests and the internal workflow management system, enabling users to trigger graph executions programmatically. This module is primarily consumed by external automation tools and testing scripts rather than by other internal community code.

## Files
- `script_examples/basic_api_example.py`: A minimal example script demonstrating how to use the queue_prompt function to send workflow definitions to the API. It contains basic usage patterns for authentication, workflow construction, and response handling to help users get started quickly. (confidence 1.00)

## Symbols
- `64caffea39f61845`: Top-level entry point function designed to accept a workflow definition (prompt) as a dictionary argument. The function is intended to serialize this data and send it to the internal ComfyUI queue system, triggering graph execution. (confidence 0.90)
  - _Rationale:_ Function name suggests queueing action, parameter indicates data structure input typical of workflow descriptions.

## Cross-community dependencies
(none)

## Unverified / resolved calls

# Community 12: Video API Input Format Handling

**Purpose:** This community handles validation, conversion, and configuration of video input/output formats for the ComfyAPI. It provides utilities for determining output formats and file open parameters, primarily used to bridge API input types with internal video processing flows.

## Files
- `comfy_api/input_impl/video_types.py`: Contains core implementation functions `container_to_output_format` and `get_open_write_kwargs` for converting video container formats to output format strings and configuring file write parameters. (confidence 1.00)
- `tests-unit/comfy_api_test/input_impl_test.py`: Contains comprehensive unit tests covering edge cases and standard paths for format conversion and file open parameter generation. (confidence 1.00)

## Symbols
- `77ce1cb5d31113b8`: Converts a video container format string (or None) to a specific output format string. Handles empty strings and special cases. (confidence 0.90)
  - _Rationale:_ Function signature indicates it takes a container format and returns a format string. Test cases verify handling of None, empty strings, and comma-separated formats.
- `5e4146fb2b8c8a78`: Generates keyword arguments for opening video files, handling formats for BytesIO objects and filepaths. (confidence 0.90)
  - _Rationale:_ Test cases check behavior with BytesIO, specific format lists, auto formats, base options, and filepaths without formats.

## Cross-community dependencies
0, 2

## Unverified / resolved calls
- unresolved: `default` from `unknown` — Could be a default parameter or function call in format conversion logic.
- unresolved: `VideoContainer` from `5e4146fb2b8c8a78` — Used in get_open_write_kwargs, likely to validate container type or access container properties.
- unresolved: `VideoFromFile` from `5e4146fb2b8c8a78` — Likely used for file-based video handling, possibly to check file paths or open file objects.

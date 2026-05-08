# Community 10: Recraft API Integration

**Purpose:** This community implements the Recraft API integration for ComfyUI, providing nodes for text-to-image, image-to-image, upscaling, background removal, and style control operations. It bridges ComfyUI's graph execution with Recraft's generative AI services through API calls and data modeling, serving as an external service adapter for advanced image synthesis and manipulation tasks.

## Files
- `comfy_api_nodes/apis/recraft_api.py`: Contains core API client logic and request/response handlers for Recraft, including multipart parsing and image output processing. (confidence 0.90)
- `comfy_api_nodes/nodes_recraft.py`: Defines ComfyUI node classes for Recraft operations, implementing the interface between user graph nodes and the API layer. (confidence 0.90)
- `custom_nodes/websocket_image_save.py`: Provides websocket-based image saving functionality, likely used by Recraft nodes for immediate output delivery. (confidence 0.70)
- `tests/compare/conftest.py`: Test configuration and fixture setup for image comparison tests in the Recraft test suite. (confidence 0.80)
- `tests/compare/test_quality.py`: Quality testing module with SSIM metrics comparison between generated images. (confidence 0.90)
- `tests/conftest.py`: General test configuration with pytest option setup for the repository. (confidence 0.70)

## Symbols
- `symbol:031cd70978736900`: Data model for Recraft color generation parameters. (confidence 0.90)
  - _Rationale:_ Used in color-related Recraft operations like RecraftColorChain and RecraftColorRGBNode.
- `symbol:081331d2c32f0fbf`: Node class for saving output as SVG format vectors. (confidence 0.80)
  - _Rationale:_ Implements vector output capability for Recraft's vectorization services.
- `symbol:0bb0f5df1a46c5d5`: Computes Structural Similarity Index for image quality comparison testing. (confidence 0.90)
  - _Rationale:_ Used in TestCompareImageMetrics to evaluate generation quality against reference images.
- `symbol:1400361612977df8`: Recraft node for crisp image upscaling using AI enhancement. (confidence 0.90)
  - _Rationale:_ Part of the upscale node hierarchy (RecraftCreativeUpscaleNode inherits from it).
- `symbol:1a943ff3a5c1122f`: Converts raster images to vector graphics using Recraft's vectorization API. (confidence 0.90)
  - _Rationale:_ Enables vector output generation from input images.
- `symbol:1e14afad4356f977`: Resolves Style V3 sub-styles for RecraftStyleV3RealisticImageNode operations. (confidence 0.90)
  - _Rationale:_ Provides style enumeration handling for realistic image generation modes.
- `symbol:261587067c3e024f`: Pydantic model defining Recraft control parameters (likely image conditioning options). (confidence 0.80)
  - _Rationale:_ Used by RecraftControlsNode and RecraftStyleV3 nodes for structured control input.
- `symbol:35c8f74a1c32e6f3`: Pydantic model for Recraft API image generation responses. (confidence 0.80)
  - _Rationale:_ Parses API responses containing generated image data or metadata.
- `symbol:5116632f8125d278`: Recraft node for image-to-image generation with inpainting capabilities. (confidence 0.90)
  - _Rationale:_ Enables modifying existing images through Recraft's generative model.
- `symbol:645de807c97827c4`: Recraft node for text-controlled image inpainting operations. (confidence 0.90)
  - _Rationale:_ Provides masked region editing with text guidance.
- `symbol:70e4dd58cafef3b2`: Pydantic model for Recraft API output data structure. (confidence 0.80)
  - _Rationale:_ Wraps API responses with metadata about generated content.
- `symbol:75a4559f1fbf312d`: Converts text prompts to vector graphics via Recraft API. (confidence 0.90)
  - _Rationale:_ Direct text-to-vector pathway in Recraft workflow.
- `symbol:7aa0ac2d53015461`: Recraft node for text-to-image generation using V3 models. (confidence 0.90)
  - _Rationale:_ Primary generation node, base for style-specific variants.
- `symbol:8f7f338a88585091`: Recraft node for digital illustration output with Style V3 support. (confidence 0.80)
  - _Rationale:_ Specialized style output inheriting from RecraftStyleV3RealisticImageNode.
- `symbol:9ce44ecb49d181e8`: Saves images to disk via websocket connection. (confidence 0.70)
  - _Rationale:_ Provides real-time websocket image delivery for ComfyUI frontend.
- `symbol:a35b1aaab1f177d1`: Recraft I/O abstraction class handling API communication and data parsing. (confidence 0.90)
  - _Rationale:_ Core interface layer between node execution and API calls.
- `symbol:a7eb47a397faeeeb`: Recraft controls interface model for node configuration. (confidence 0.80)
  - _Rationale:_ Defines control parameters like seed, steps, and guidance for generation.
- `symbol:b2f74e35eb16b791`: Enum for Recraft image output size options. (confidence 0.90)
  - _Rationale:_ Provides standardized size selections for image generation requests.
- `symbol:b90c0d303b444702`: Base node class for Recraft control configuration and parameter passing. (confidence 0.80)
  - _Rationale:_ Used by style and generation nodes for control input.
- `symbol:bfcb80a563195d44`: Base class for Recraft Style V3 realistic image generation nodes. (confidence 0.90)
  - _Rationale:_ Parent class for style-specific variants (DigitalIllustration, VectorIllustration, etc.).
- `symbol:c5aa3fbced183368`: AI-powered creative upscaling node for Recraft output enhancement. (confidence 0.90)
  - _Rationale:_ Inherits from RecraftCrispUpscaleNode for style-aware upscaling.
- `symbol:c9dc1046dbd66252`: Pydantic model for Recraft image generation API requests. (confidence 0.90)
  - _Rationale:_ Defines structured input for API calls including prompt, model, and parameters.
- `symbol:d14a4d4e2c9d3f42`: Enum for Recraft Style V3 style categories. (confidence 0.90)
  - _Rationale:_ Provides style selection for style-specific generation nodes.
- `symbol:f190235dfb285d01`: Class for handling Recraft image output processing and conversion. (confidence 0.80)
  - _Rationale:_ Processes API responses and converts image data for ComfyUI output.

## Cross-community dependencies
0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 11

## Unverified / resolved calls
- unresolved: `RecraftIO` from `symbol:a35b1aaab1f177d1` — RecraftIO class methods likely call internal I/O handlers.
- unresolved: `RecraftStyleV3` from `symbol:7aa0ac2d53015461` — RecraftTextToImageNode likely validates or uses RecraftStyleV3 enum values.
- unresolved: `SVG` from `symbol:081331d2c32f0fbf` — SaveSVGNode likely calls SVG class methods for vector output serialization.

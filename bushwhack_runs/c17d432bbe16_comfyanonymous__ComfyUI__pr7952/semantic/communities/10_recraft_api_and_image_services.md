# Community 10: Recraft API and Image Services

**Purpose:** This community handles Recraft API integrations, providing image generation, vectorization, style transfer, and image processing nodes for ComfyUI workflows. It includes both runtime nodes (recraft_api_nodes) for user workflows and test utilities (tests) for validating image quality and metrics, forming a self-contained service layer for external Recraft service consumption.

## Files
- `recraft_api.py`: Core API client and request/response handlers for Recraft services, managing multipart parsing, image generation requests, and HTTP communication. (confidence 0.90)

## Symbols
- `031cd70978736900`: Represents color palette configuration for Recraft image generation, enabling users to specify color schemes in prompts. (confidence 0.95)
  - _Rationale:_ Class defined with color-related fields for image generation parameters.
- `0bb0f5df1a46c5d5`: Computes SSIM (Structural Similarity Index) between two images, likely used in test quality comparisons. (confidence 0.90)
  - _Rationale:_ Function accepting image arrays, returning similarity metrics.
- `12f4cef04e085fb6`: Utility to gather filenames from a directory, used for organizing test assets or image collections. (confidence 0.85)
  - _Rationale:_ Function taking directory path, returning list of basenames.
- `1400361612977df8`: Node for crisp upscaling images using Recraft API service. (confidence 0.90)
  - _Rationale:_ Subclass of RecraftCrispUpscaleNode, handling image processing workflow.
- `1a943ff3a5c1122f`: Node for vectorizing raster images into SVG vectors via Recraft. (confidence 0.90)
  - _Rationale:_ Text-to-vector functionality, likely for graphic design workflows.
- `1e14afad4356f977`: Helper function to extract substyles from Recraft V3 style strings. (confidence 0.85)
  - _Rationale:_ Parses style_v3 parameter, returns list of substyles.
- `261587067c3e024f`: Model class for controlling Recraft image generation parameters. (confidence 0.90)
  - _Rationale:_ BaseModel subclass, likely holds API request configuration.
- `27380382d4258ca7`: Node for generating logo raster images using Recraft Style V3 API. (confidence 0.90)
  - _Rationale:_ Subclass of RecraftStyleV3RealisticImageNode, specialized for logos.
- `2bd4e2f479a09236`: Node for replacing backgrounds in images using Recraft AI. (confidence 0.90)
  - _Rationale:_ Image processing node, likely replaces subject backgrounds.
- `3497410287ca6fd7`: Node for removing backgrounds from images via Recraft API. (confidence 0.90)
  - _Rationale:_ Image processing node, removes background layers.
- `34b58bcee0ef8f37`: Enum for Recraft style selection, defining available artistic styles. (confidence 0.90)
  - _Rationale:_ Class inheriting from str, Enum, controlling style choices.
- `3a78881b16aaae5d`: Color chain structure for managing sequential color palettes. (confidence 0.85)
  - _Rationale:_ Class for color management, likely for image color consistency.
- `3dcf587230151889`: Model representing color object with RGB values and metadata. (confidence 0.90)
  - _Rationale:_ BaseModel with color fields, used in Recraft parameters.
- `5116632f8125d278`: Node for image-to-image transformations using Recraft service. (confidence 0.90)
  - _Rationale:_ Accepts input image, generates modified output via API.
- `5b4cddbacc173b0b`: Enum for selecting Recraft model variants (e.g., V1, V3). (confidence 0.90)
  - _Rationale:_ String enum, controls which Recraft backend model to use.
- `645de807c97827c4`: Node for inpainting images using Recraft AI capabilities. (confidence 0.90)
  - _Rationale:_ Replaces masked regions in images with AI-generated content.
- `70e4dd58cafef3b2`: Model for Recraft API response structure. (confidence 0.90)
  - _Rationale:_ BaseModel containing response fields from Recraft service.
- `75a4559f1fbf312d`: Node for converting text prompts to vector graphics via Recraft. (confidence 0.90)
  - _Rationale:_ Text-to-vector functionality, distinct from image-to-image.
- `7aa0ac2d53015461`: Node for generating images from text prompts using Recraft. (confidence 0.95)
  - _Rationale:_ Primary text-to-image node for workflow generation.
- `7c9d485caf319746`: Model for Recraft controls object, managing generation parameters. (confidence 0.90)
  - _Rationale:_ BaseModel subclass, holds controls for API requests.
- `8d1d4d28d9e8c7d4`: Class for progress bar management during Recraft API operations. (confidence 0.85)
  - _Rationale:_ Utility class for tracking long-running API calls.
- `8f7f338a88585091`: Node for digital illustration generation using Recraft Style V3. (confidence 0.90)
  - _Rationale:_ Subclass of RecraftStyleV3RealisticImageNode, specialized for illustrations.
- `9ce44ecb49d181e8`: Node for saving images via WebSocket, used in Recraft output workflows. (confidence 0.90)
  - _Rationale:_ Utility node for persisting generated images to disk.
- `a35b1aaab1f177d1`: IO utility class for handling Recraft data transfer operations. (confidence 0.85)
  - _Rationale:_ Manages file I/O and memory buffers for API requests/responses.
- `a67c84a047565828`: Model for Recraft image generation response, containing image data and metadata. (confidence 0.90)
  - _Rationale:_ BaseModel with image and generation parameters in response.
- `a7eb47a397faeeeb`: Model for Recraft controls, managing generation settings. (confidence 0.90)
  - _Rationale:_ BaseModel subclass, holds controls for API requests.
- `b037b6ca0f82db32`: Test class for comparing image metrics, likely for quality validation. (confidence 0.85)
  - _Rationale:_ Test infrastructure for verifying Recraft output quality.
- `b2f74e35eb16b791`: Enum for Recraft image size configurations (e.g., small, large). (confidence 0.90)
  - _Rationale:_ String enum, controls output resolution for Recraft.
- `b90c0d303b444702`: Node for managing Recraft controls, allowing dynamic parameter adjustment. (confidence 0.85)
  - _Rationale:_ Controls configuration node, likely for advanced users.
- `bfcb80a563195d44`: Base class for Recraft Style V3 realistic image generation nodes. (confidence 0.90)
  - _Rationale:_ Parent for realistic image variants, defines common logic.
- `c12c626fc1c5d3e5`: Utility for parsing multipart form data from Recraft API requests. (confidence 0.85)
  - _Rationale:_ Handles complex form data parsing for API communication.
- `c4ba8871ab09e7b6`: Node for generating RGB color objects for Recraft parameters. (confidence 0.85)
  - _Rationale:_ Utility for creating color specifications in requests.
- `c5aa3fbced183368`: Node for creative upscaling using Recraft V3 AI capabilities. (confidence 0.90)
  - _Rationale:_ Enhanced upscaling beyond crisp versions, likely with generative details.
- `c9dc1046dbd66252`: Model for Recraft image generation request, specifying parameters. (confidence 0.90)
  - _Rationale:_ BaseModel with prompt, size, style, and other generation params.
- `ce7f5233d35f7c4c`: Node for vector illustration generation using Recraft Style V3. (confidence 0.90)
  - _Rationale:_ Subclass of RecraftStyleV3RealisticImageNode, specialized for vectors.
- `d14a4d4e2c9d3f42`: Enum for Recraft Style V3 variants, defining generation modes. (confidence 0.90)
  - _Rationale:_ String enum, controls V3-specific generation options.
- `eb84db530b3d6080`: Async server runner for Recraft API endpoints. (confidence 0.80)
  - _Rationale:_ Runs server instance for handling API requests.
- `eddfa0a83d53665e`: Style library manager for Recraft V3 infinite style options. (confidence 0.85)
  - _Rationale:_ Manages style selection from large V3 library.
- `f190235dfb285d01`: Handler for processing Recraft image output responses. (confidence 0.85)
  - _Rationale:_ Parses and formats image data from Recraft API responses.
- `f1c04a8bd230b405`: SVG class for vector graphics handling in Recraft workflows. (confidence 0.85)
  - _Rationale:_ Represents vector image data for manipulation or storage.
- `fc7f60cd2283aea6`: Pytest hook for generating test cases dynamically. (confidence 0.85)
  - _Rationale:_ Configures test suite with parameterized inputs.

## Cross-community dependencies
0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 12

## Unverified / resolved calls
- unresolved: `RecraftAPI` from `handle_recraft_file_request` — API client initialization or request dispatching.
- unresolved: `websocket_server` from `run` — Server startup for API endpoints.

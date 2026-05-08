# Community 10: Recraft API Integration and Testing

**Purpose:** This community integrates Recraft's generative AI API nodes into ComfyUI, providing image generation, editing, and style capabilities. It includes both the implementation of API interaction and a test suite to verify image quality and metrics, serving as a bridge between Recraft's backend services and the ComfyUI frontend.

## Files
- `077a03778904271d`: Implements the Recraft API client and helper functions, handling requests, responses, and multipart parsing for image generation and modification. (confidence 0.95)
- `12f4cef04e085fb6`: Defines the actual ComfyUI node implementations for Recraft API operations, including text-to-image, image-to-image, and various style-specific nodes. (confidence 0.95)
- `081331d2c32f0fbf`: Implements a utility node for saving SVG content directly, likely used for vector outputs from Recraft's vectorization features. (confidence 0.85)
- `27380382d4258ca7`: Contains custom logic for saving images via websocket, ensuring image outputs are transmitted to the frontend for real-time updates. (confidence 0.80)
- `3a78881b16aaae5d`: Provides test configuration and fixtures for the pytest suite, setting up the environment for testing Recraft node quality and behavior. (confidence 0.90)
- `5116632f8125d278`: Contains the actual test logic to compare generated images against ground truth using metrics like SSIM, validating node correctness. (confidence 0.95)

## Symbols
- `7aa0ac2d53015461`: Implements the Recraft Text to Image node, exposing API parameters like prompt and style to the ComfyUI graph. (confidence 0.95)
  - _Rationale:_ Named RecraftTextToImageNode, directly maps to the Recraft API's text generation endpoint.
- `5116632f8125d278`: Implements the Recraft Image to Image node, allowing image manipulation and enhancement via Recraft's models. (confidence 0.95)
  - _Rationale:_ Named RecraftImageToImageNode, typical for image-to-image AI workflows.
- `645de807c97827c4`: Implements the Recraft Inpainting node, enabling targeted edits to specific regions of an image. (confidence 0.90)
  - _Rationale:_ Named RecraftImageInpaintingNode, follows standard ComfyUI inpainting patterns.
- `3497410287ca6fd7`: Implements the Recraft Background Removal node, using AI to separate subjects from backgrounds. (confidence 0.95)
  - _Rationale:_ Named RecraftRemoveBackgroundNode, explicitly indicates background removal functionality.
- `b90c0d303b444702`: Implements the Recraft Controls node, allowing users to set parameters for Recraft generation in a structured way. (confidence 0.90)
  - _Rationale:_ Named RecraftControlsNode, suggests configuration and parameter handling.
- `70e4dd53615461`: Defines the response structure for Recraft image generation API calls. (confidence 0.90)
  - _Rationale:_ Named RecraftReturnedObject and RecraftImageGenerationResponse, used to parse API responses.
- `b5defbc1e9f3a060`: Defines a generic data model for test comparisons, likely used to compare input/output image properties. (confidence 0.85)
  - _Rationale:_ Named Datum2, appears in test context for comparing image metrics.
- `0bb0f5df1a46c5d5`: Computes the Structural Similarity Index (SSIM) to quantitatively compare generated images against expected outputs. (confidence 0.95)
  - _Rationale:_ Name ssim_score and return type Tuple[float, np.ndarray] indicate quality metrics calculation.

## Cross-community dependencies
0, 1, 2, 3, 4, 5, 7, 8, 9

## Unverified / resolved calls
- unresolved: `recraft_multipart_parser` from `handle_recraft_image_output` — Likely parses incoming multipart form data from API requests.
- unresolved: `RecraftControls` from `RecraftImageGenerationRequest` — Request object likely contains Controls data for API parameters.

# Community 22: Global Configuration Options

**Purpose:** This community provides global configuration management for the ComfyUI application, handling runtime options and initialization flags. It defines the entry point `enable_args_parsing` to control whether command-line arguments are processed during startup. This module acts as a static configuration layer that other components likely query to determine startup behavior, operating independently of the node execution graph.

## Files
- `comfy/options.py`: Defines global configuration constants and the primary function for toggling command-line argument parsing. It likely stores settings used to initialize the application environment before the main graph logic runs. (confidence 0.90)
- `comfy/__init__.py`: Likely imports options to set up the initial state, though not provided in this prompt. (confidence 0.50)
- `main.py`: Likely imports options to check parsing flags at runtime. (confidence 0.50)
- `server.py`: Likely depends on options for startup configuration. (confidence 0.50)

## Symbols
- `symbol:496c54f7c038dee2`: A public function to programmatically toggle command-line argument parsing on or off. This allows the application to start in a headless mode or bypass CLI initialization logic if `enable` is set to `False`. (confidence 0.95)
  - _Rationale:_ Visible signature shows it accepts a boolean `enable` argument. Given the name `enable_args_parsing`, it logically guards the execution of argument parsing logic.

## Cross-community dependencies
(none)

## Unverified / resolved calls

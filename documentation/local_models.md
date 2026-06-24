# Local model guide (how we ran it)

This is a short, practical description of how we ran a local model in this repo (not a setup tutorial). The key detail is that we bound a local OpenAI-compatible server to a host/port and pointed the app at its base URL.

## What we did

1) Ran a local OpenAI-compatible server (Ollama, LM Studio, vLLM) bound to localhost and an HTTP port. We used the OpenAI-style base URL format and left it on the default path: http://localhost:8000/v1.
2) Configured the app to call that base URL. Requests are standard OpenAI-style calls; the API key is just a placeholder string for local servers.
3) Selected model keys that map to the local provider in the LLM factory (for example an Ollama-specific key if defined there).

## How the app connects

- Base URL points to the local server with a host/port binding (default: http://localhost:8000/v1).
- The client sends Authorization headers with a placeholder key (default: "local").
- Request timeouts and retry counts are tuned for local inference latency.

## Settings we used

These are the environment values we set (loaded by src/config.py with the REVIEW_ prefix):

- REVIEW_LOCAL_LLM_BASE_URL
- REVIEW_LOCAL_LLM_API_KEY
- REVIEW_LOCAL_LLM_TIMEOUT_SECONDS
- REVIEW_LOCAL_LLM_STATUS_TIMEOUT_SECONDS
- REVIEW_LOCAL_LLM_MAX_RETRIES

## Sanity check we ran

We used the manual check script to call /models and confirm the server was reachable:

- src/infrastructure/tests/manual_qwen_llm_check.py

The output lists the model IDs the local server exposes, which we used to verify the server binding and connectivity.

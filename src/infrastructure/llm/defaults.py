"""
Single place to hotswap the default local vLLM stack.

Change DEFAULT_LOCAL_MODEL_KEY and DEFAULT_LOCAL_MODEL_PATH here (or override per-role
via REVIEW_*_MODEL_KEY env vars in config). The path must match what `vllm serve` loads;
the key must have a matching entry in factory.MODELS.
"""

DEFAULT_LOCAL_MODEL_KEY = "qwen3.5-122b"
DEFAULT_LOCAL_MODEL_PATH = "/lustre/fs1/home/dy828490/bushwhack_dev/qwen-3.5-122b/"

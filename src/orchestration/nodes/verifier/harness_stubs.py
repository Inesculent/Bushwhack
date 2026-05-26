"""Snippet builders for verifier scripts (embedded in generated test code)."""

from __future__ import annotations

# Prelude copied into LLM-generated verifier scripts (runs inside Docker).
HEAVY_DEP_STUB_PRELUDE = '''import sys
import types
from unittest.mock import MagicMock

def _install_module_stub(name: str, *, is_package: bool = False) -> types.ModuleType:
    mod = types.ModuleType(name)
    if is_package:
        mod.__path__ = []  # type: ignore[attr-defined]
    sys.modules[name] = mod
    return mod

def _install_heavy_dep_stubs() -> None:
    """Mock torch/PIL/etc. PIL must be a package so PIL.PngImagePlugin imports work."""
    for mod in (
        "torch",
        "torchvision",
        "numpy",
        "pandas",
        "transformers",
        "safetensors",
        "safetensors.torch",
    ):
        if mod not in sys.modules:
            sys.modules[mod] = MagicMock()
    if "PIL" not in sys.modules:
        pil = _install_module_stub("PIL", is_package=True)
        for sub in ("Image", "ImageDraw", "ImageFont", "PngImagePlugin", "JpegImagePlugin"):
            submod = _install_module_stub(f"PIL.{sub}")
            setattr(pil, sub, submod)
    if "Pillow" not in sys.modules:
        sys.modules["Pillow"] = sys.modules.get("PIL") or MagicMock()

_install_heavy_dep_stubs()
'''

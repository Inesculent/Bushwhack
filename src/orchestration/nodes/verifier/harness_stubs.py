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

def _install_package_tree(name: str) -> types.ModuleType:
    parts = name.split(".")
    full = ""
    parent = None
    current = None
    for part in parts:
        full = part if not full else f"{full}.{part}"
        current = sys.modules.get(full)
        if current is None:
            current = _install_module_stub(full, is_package=True)
        if parent is not None:
            setattr(parent, part, current)
        parent = current
    return current or _install_module_stub(name, is_package=True)

def _install_heavy_dep_stubs() -> None:
    """Mock torch/PIL/etc. PIL must be a package so PIL.PngImagePlugin imports work."""
    for pkg in (
        "torch",
        "torch.utils",
        "torch.utils.data",
        "torch.nn",
        "torchvision",
        "numpy",
        "pandas",
        "transformers",
        "safetensors",
        "safetensors.torch",
    ):
        mod = _install_package_tree(pkg)
        mod.__getattr__ = lambda _name: MagicMock()  # type: ignore[attr-defined]
    pil = sys.modules.get("PIL") or _install_module_stub("PIL", is_package=True)
    if not hasattr(pil, "__path__"):
        pil.__path__ = []  # type: ignore[attr-defined]
    for sub in ("Image", "ImageDraw", "ImageFont", "PngImagePlugin", "JpegImagePlugin"):
        submod = sys.modules.get(f"PIL.{sub}") or _install_module_stub(f"PIL.{sub}")
        setattr(pil, sub, submod)
        submod.__getattr__ = lambda _name: MagicMock()  # type: ignore[attr-defined]
    sys.modules["PIL.PngImagePlugin"].PngInfo = type("PngInfo", (), {})
    if "Pillow" not in sys.modules:
        sys.modules["Pillow"] = sys.modules.get("PIL") or MagicMock()

_install_heavy_dep_stubs()
'''

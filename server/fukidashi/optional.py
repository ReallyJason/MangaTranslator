"""Guarded imports for the heavy, optional dependencies.

Nothing here raises: each probe logs a clear message and records whether the
feature is available, so the server always starts.
"""

from __future__ import annotations

import importlib
import logging
import threading

log = logging.getLogger("fukidashi.optional")

_lock = threading.Lock()
_modules: dict[str, object | None] = {}
_errors: dict[str, str] = {}

HINTS = {
    "torch": "LaMa inpainting and GPU support are disabled (OpenCV inpainting is used). "
    "Install with: pip install torch  (see README for CUDA builds)",
    "easyocr": "Text detection is disabled, so /translate cannot work. "
    "Install with: pip install easyocr",
    "manga_ocr": "Japanese OCR will use EasyOCR instead (less accurate on manga). "
    "Install with: pip install manga-ocr",
    "argostranslate": "The offline translation engine is disabled. "
    "Install with: pip install argostranslate",
}


def get(name: str):
    """Import `name` once; return the module or None if unavailable."""
    with _lock:
        if name in _modules:
            return _modules[name]
        try:
            mod = importlib.import_module(name)
        except Exception as e:  # ImportError, but also broken native libs (OSError etc.)
            _modules[name] = None
            _errors[name] = f"{type(e).__name__}: {e}"
            log.warning("Optional dependency '%s' unavailable (%s). %s", name, _errors[name], HINTS.get(name, ""))
            return None
        _modules[name] = mod
        return mod


def available(name: str) -> bool:
    return get(name) is not None


def error(name: str) -> str:
    return _errors.get(name, "")


def probe_all() -> dict[str, bool]:
    result = {}
    for name in ("torch", "easyocr", "manga_ocr", "argostranslate"):
        result[name] = available(name)
        if result[name]:
            log.info("Optional dependency '%s' available", name)
    return result


_gpu_cache: tuple[bool, str] | None = None


def gpu_info(use_gpu: str = "auto") -> tuple[bool, str]:
    """(gpu_enabled, device_name). Only CUDA is used for acceleration."""
    global _gpu_cache
    if use_gpu == "false":
        return False, "disabled by USE_GPU=false"
    if _gpu_cache is not None:
        return _gpu_cache
    torch = get("torch")
    if torch is None:
        _gpu_cache = (False, "torch not installed")
    else:
        try:
            if torch.cuda.is_available():
                _gpu_cache = (True, torch.cuda.get_device_name(0))
            else:
                _gpu_cache = (False, "no CUDA device")
        except Exception as e:
            _gpu_cache = (False, f"CUDA check failed: {e}")
    if use_gpu == "true" and not _gpu_cache[0]:
        log.warning("USE_GPU=true but no usable GPU (%s); running on CPU", _gpu_cache[1])
    return _gpu_cache

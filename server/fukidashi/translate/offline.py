"""Offline translation with Argos Translate.

Language packages are downloaded automatically the first time a pair is used.
If there is no direct package, translation pivots through English.
"""

from __future__ import annotations

import logging
import threading

from .. import optional
from .errors import EngineError

log = logging.getLogger("fukidashi.offline")

_lock = threading.Lock()
_index_updated = False


def code(lang: str) -> str:
    l = (lang or "").lower()
    return {"zh-tw": "zt", "zh-hant": "zt", "zh-cn": "zh", "zh-hans": "zh", "pt-br": "pt", "pt-pt": "pt"}.get(
        l, l.split("-")[0]
    )


def available() -> bool:
    return optional.available("argostranslate")


def _modules():
    if optional.get("argostranslate") is None:
        raise EngineError("Argos Translate is not installed (pip install argostranslate)")
    import argostranslate.package as package
    import argostranslate.translate as translate

    return package, translate


def _installed_translation(translate_mod, src: str, tgt: str):
    langs = {l.code: l for l in translate_mod.get_installed_languages()}
    if src in langs and tgt in langs:
        return langs[src].get_translation(langs[tgt])
    return None


def _install(package_mod, src: str, tgt: str) -> bool:
    global _index_updated
    if not _index_updated:
        log.info("Updating Argos package index")
        package_mod.update_package_index()
        _index_updated = True
    for p in package_mod.get_available_packages():
        if p.from_code == src and p.to_code == tgt:
            log.info("Downloading Argos package %s -> %s", src, tgt)
            package_mod.install_from_path(p.download())
            return True
    return False


def _chain(src: str, tgt: str) -> list:
    """Translations to apply in order: direct, or src->en->tgt."""
    package_mod, translate_mod = _modules()
    with _lock:
        direct = _installed_translation(translate_mod, src, tgt)
        if direct is not None:
            return [direct]
        try:
            if _install(package_mod, src, tgt):
                t = _installed_translation(translate_mod, src, tgt)
                if t is not None:
                    return [t]
            if "en" in (src, tgt):
                raise EngineError(f"No offline language package for {src} -> {tgt}")
            steps = []
            for a, b in ((src, "en"), ("en", tgt)):
                t = _installed_translation(translate_mod, a, b)
                if t is None:
                    if not _install(package_mod, a, b):
                        raise EngineError(f"No offline language package for {a} -> {b}")
                    t = _installed_translation(translate_mod, a, b)
                if t is None:
                    raise EngineError(f"Offline package {a} -> {b} failed to load")
                steps.append(t)
            return steps
        except EngineError:
            raise
        except Exception as e:
            raise EngineError(f"Offline package download failed: {e}") from e


def translate(texts: list[str], source: str, target: str) -> list[str]:
    src, tgt = code(source), code(target)
    if src == tgt:
        return list(texts)
    steps = _chain(src, tgt)
    out = []
    for text in texts:
        for step in steps:
            text = step.translate(text)
        out.append(text)
    return out

"""Translation dispatch: context mode (LLM + story memory) or fast mode (per-block engines)."""

from __future__ import annotations

import logging
from typing import Callable, Optional

import httpx

from ..config import Settings
from ..models import TranslateOptions, TranslationOutcome
from ..series import SeriesStore, series_key
from . import deepl, google, offline
from .errors import EngineError
from .llm import LLMError, translate_with_llm

log = logging.getLogger("fukidashi.translate")

AUTO_ORDER = ("deepl", "google", "offline")
ENGINE_LABELS = {"deepl": "DeepL", "google": "Google", "offline": "offline (Argos)"}


class Translator:
    def __init__(self, settings: Settings, store: SeriesStore):
        self.s = settings
        self.store = store
        self.http = httpx.Client(timeout=httpx.Timeout(30, connect=10))
        # Overridable for tests.
        self.llm_fn: Callable = translate_with_llm

    # -- fast mode -----------------------------------------------------------

    def engines(self) -> dict[str, bool]:
        return {
            "deepl": bool(self.s.deepl_api_key),
            "google": bool(self.s.google_api_key),
            "offline": offline.available(),
        }

    def _run_engine(self, name: str, texts: list[str], source: str, target: str) -> list[str]:
        if name == "deepl":
            if not self.s.deepl_api_key:
                raise EngineError("DEEPL_API_KEY is not set")
            return deepl.translate(self.s.deepl_api_key, texts, source, target, self.http)
        if name == "google":
            if not self.s.google_api_key:
                raise EngineError("GOOGLE_API_KEY is not set")
            return google.translate(self.s.google_api_key, texts, source, target, self.http)
        if name == "offline":
            return offline.translate(texts, source, target)
        raise EngineError(f"unknown engine '{name}'")

    def fast(self, texts: list[str], opts: TranslateOptions) -> tuple[list[str], str, list[str]]:
        """Translate each block independently. Returns (translations, engine, notes).

        An explicitly chosen engine that fails falls back to the other available
        engines (with a note); "auto" tries DeepL > Google > offline.
        """
        avail = self.engines()
        if opts.engine == "auto":
            order = [e for e in AUTO_ORDER if avail[e]]
        else:
            order = [opts.engine] + [e for e in AUTO_ORDER if e != opts.engine and avail[e]]
        if not order:
            raise EngineError(
                "No translation engine available: set DEEPL_API_KEY or GOOGLE_API_KEY in server/.env, "
                "or install argostranslate for offline translation"
            )
        notes, errors = [], []
        for name in order:
            try:
                result = self._run_engine(name, texts, opts.source_lang, opts.target_lang)
            except EngineError as e:
                errors.append(str(e))
                log.warning("Engine %s failed: %s", name, e)
                continue
            if errors:
                notes.append(f"{'; '.join(errors)}; used {ENGINE_LABELS[name]} instead")
            return result, name, notes
        raise EngineError("All translation engines failed: " + "; ".join(errors))

    # -- dispatch ------------------------------------------------------------

    def ensure_available(self, opts: TranslateOptions) -> None:
        """Fail fast (before OCR work) when nothing could translate this request."""
        if opts.mode == "context" and self.s.llm_status()[0]:
            return
        if any(self.engines().values()):
            return
        why = self.s.llm_status()[1]
        raise EngineError(
            ("Context mode needs an LLM (" + why + ") and there is no fast-mode fallback: " if opts.mode == "context" else "")
            + "no translation engine available. Set DEEPL_API_KEY or GOOGLE_API_KEY in server/.env, "
            "install argostranslate for offline translation, or configure an LLM"
        )

    def translate(self, texts: list[str], opts: TranslateOptions) -> TranslationOutcome:
        if opts.mode == "context":
            return self._context(texts, opts)
        result, engine, notes = self.fast(texts, opts)
        return TranslationOutcome(result, "fast", engine, notes, degraded=bool(notes))

    def _context(self, texts: list[str], opts: TranslateOptions) -> TranslationOutcome:
        key = series_key(opts.session)
        label = f"llm:{self.s.llm_provider}/{self.s.effective_model}"
        with self.store.lock(key):
            ctx = self.store.load(key)
            try:
                res = self.llm_fn(self.s, texts, opts.source_lang, opts.target_lang, ctx.summary, ctx.glossary)
            except (LLMError, httpx.HTTPError) as e:
                log.warning("Context mode failed (%s); falling back to fast mode", e)
                try:
                    result, engine, notes = self.fast(texts, opts)
                except EngineError as fe:
                    raise EngineError(f"Context mode failed ({e}) and the fast-mode fallback failed too: {fe}") from fe
                note = f"Context mode unavailable: {e}. Used fast mode ({ENGINE_LABELS[engine]})."
                return TranslationOutcome(result, "fast", engine, [note] + notes, degraded=True)
            self.store.update(ctx, res.summary, res.glossary)

        translations: list[Optional[str]] = list(res.translations)
        notes = list(res.problems)
        missing = [i for i, t in enumerate(translations) if t is None]
        if missing:
            try:
                filled, engine, _ = self.fast([texts[i] for i in missing], opts)
                for i, t in zip(missing, filled):
                    translations[i] = t
                notes.append(f"{len(missing)} block(s) filled in with {ENGINE_LABELS[engine]}")
            except EngineError as e:
                notes.append(f"{len(missing)} block(s) left untranslated ({e})")
        return TranslationOutcome(translations, "context", label, notes, degraded=bool(notes))


__all__ = ["Translator", "EngineError", "LLMError"]

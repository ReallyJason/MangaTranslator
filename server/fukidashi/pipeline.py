"""detect -> OCR -> translate -> erase -> typeset, with caching.

Model work (detection, OCR, inpainting) is serialised with one lock; network
translation runs outside it so another request can use the models meanwhile.
"""

from __future__ import annotations

import io
import logging
import threading
import time
from typing import Callable

import numpy as np
from PIL import Image, ImageOps

from . import optional
from .cache import ResultCache, cache_key
from .config import Settings
from .detect import detect_blocks, reading_order
from .erase import LamaInpainter, erase_blocks
from .models import Block, PipelineResult, TranslateOptions, base_lang
from .ocr import OcrEngines, recognize_blocks
from .series import SeriesStore
from .translate import Translator
from .typeset import render_blocks

log = logging.getLogger("fukidashi.pipeline")

Image.MAX_IMAGE_PIXELS = 400_000_000  # long webtoon strips are legitimately huge


def header_safe(text: str, limit: int = 600) -> str:
    """HTTP headers are latin-1; keep notes ASCII and on one line."""
    flat = " ".join(str(text).split())
    return flat.encode("ascii", "replace").decode("ascii")[:limit]


def decode_image(data: bytes) -> tuple[np.ndarray, np.ndarray | None]:
    """Return (RGB uint8 array, alpha channel or None). Applies EXIF rotation like browsers do."""
    img = Image.open(io.BytesIO(data))
    img.seek(0)  # first frame of animated GIF/WebP
    img = ImageOps.exif_transpose(img)
    alpha = None
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        a = np.asarray(rgba.getchannel("A"))
        if a.min() < 255:
            alpha = a.copy()
            white = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
            img = Image.alpha_composite(white, rgba)
    rgb = np.array(img.convert("RGB"))
    return rgb, alpha


def encode_png(image: Image.Image, alpha: np.ndarray | None) -> bytes:
    if alpha is not None:
        image = image.convert("RGBA")
        image.putalpha(Image.fromarray(alpha))
    buf = io.BytesIO()
    image.save(buf, format="PNG", compress_level=3)
    return buf.getvalue()


class Pipeline:
    def __init__(self, settings: Settings):
        self.s = settings
        self.gpu, self.device_name = optional.gpu_info(settings.use_gpu)
        self.model_lock = threading.Lock()
        self.ocr = OcrEngines(self.gpu, settings.use_manga_ocr)
        self.lama = LamaInpainter(settings.models_dir, settings.lama_url, self.gpu) if settings.inpainter == "lama" else None
        self.store = SeriesStore(settings.series_dir)
        self.translator = Translator(settings, self.store)
        self.cache = ResultCache(settings.cache_dir, settings.cache_max_mb)
        # Stage hook, replaceable in tests: (rgb image, options) -> blocks with .text, in reading order.
        self.detect_fn: Callable[[np.ndarray, TranslateOptions], list[Block]] = self.detect_and_read

    def detect_and_read(self, image: np.ndarray, opts: TranslateOptions) -> list[Block]:
        reader = self.ocr.reader(opts.source_lang)
        blocks = detect_blocks(reader, image)
        blocks = recognize_blocks(self.ocr, image, blocks, opts.source_lang)
        return reading_order(blocks, right_to_left=base_lang(opts.source_lang) == "ja")

    def process(self, data: bytes, opts: TranslateOptions) -> PipelineResult:
        t0 = time.perf_counter()
        key = cache_key(data, opts.cache_dict())
        hit, png, meta = self.cache.get(key)
        if hit:
            headers = {k: v for k, v in meta.get("headers", {}).items()}
            headers["X-Fukidashi-Cache"] = "hit"
            return PipelineResult(png, headers)

        self.translator.ensure_available(opts)
        image, alpha = decode_image(data)
        with self.model_lock:
            t_lock = time.perf_counter()
            blocks = self.detect_fn(image, opts)
            t_ocr = time.perf_counter() - t_lock

        headers = {"X-Fukidashi-Cache": "miss", "X-Fukidashi-Blocks": str(len(blocks))}
        if not blocks:
            self.cache.put(key, None, {"headers": headers})
            headers["X-Fukidashi-Time"] = f"{(time.perf_counter() - t0) * 1000:.0f}"
            return PipelineResult(None, headers)

        t1 = time.perf_counter()
        outcome = self.translator.translate([b.text for b in blocks], opts)
        t_tr = time.perf_counter() - t1
        for b, t in zip(blocks, outcome.translations):
            b.translation = t
        todo = [b for b in blocks if b.translation and b.translation.strip() and b.translation.strip() != b.text]
        headers.update(
            {
                "X-Fukidashi-Mode": outcome.mode,
                "X-Fukidashi-Engine": header_safe(outcome.engine, 120),
                "X-Fukidashi-Blocks": str(len(todo)),
            }
        )
        if outcome.notes:
            headers["X-Fukidashi-Note"] = header_safe(" | ".join(outcome.notes))
        if not todo:
            headers["X-Fukidashi-Time"] = f"{(time.perf_counter() - t0) * 1000:.0f}"
            return PipelineResult(None, headers)

        t2 = time.perf_counter()
        with self.model_lock:
            infos = erase_blocks(image, todo, self.lama)
        pil = Image.fromarray(image)
        render_blocks(pil, todo, infos, opts.target_lang, self.s.fonts_dir, opts.font)
        png = encode_png(pil, alpha)
        t_draw = time.perf_counter() - t2

        log.info(
            "Translated %dx%d: %d blocks, %s via %s (ocr %.1fs, translate %.1fs, render %.1fs)%s",
            image.shape[1], image.shape[0], len(todo), outcome.mode, outcome.engine, t_ocr, t_tr, t_draw,
            f" note: {'; '.join(outcome.notes)}" if outcome.notes else "",
        )
        if not outcome.degraded:
            self.cache.put(key, png, {"headers": headers})
        headers["X-Fukidashi-Time"] = f"{(time.perf_counter() - t0) * 1000:.0f}"
        return PipelineResult(png, headers)

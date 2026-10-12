"""OCR: manga-ocr for Japanese, EasyOCR for everything else (and as fallback)."""

from __future__ import annotations

import logging
import threading

import numpy as np
from PIL import Image

from . import optional
from .models import Block, base_lang

log = logging.getLogger("fukidashi.ocr")

# Source language -> EasyOCR language list. Several scripts must be paired with English.
EASYOCR_LANGS = {
    "ja": ["ja", "en"],
    "zh": ["ch_sim", "en"],
    "zh-cn": ["ch_sim", "en"],
    "zh-hans": ["ch_sim", "en"],
    "zh-tw": ["ch_tra", "en"],
    "zh-hant": ["ch_tra", "en"],
    "ko": ["ko", "en"],
    "th": ["th", "en"],
    "en": ["en"],
}


def easyocr_langs(source_lang: str) -> list[str]:
    code = (source_lang or "ja").lower()
    if code in EASYOCR_LANGS:
        return EASYOCR_LANGS[code]
    b = base_lang(code)
    if b in EASYOCR_LANGS:
        return EASYOCR_LANGS[b]
    return [b, "en"] if b != "en" else ["en"]


def has_text(s: str) -> bool:
    return any(c.isalnum() for c in s or "")


class OcrEngines:
    """Lazily constructed, cached OCR models. Callers hold the pipeline model lock."""

    def __init__(self, gpu: bool, use_manga_ocr: bool):
        self.gpu = gpu
        self.use_manga_ocr = use_manga_ocr
        self._readers: dict[tuple, object] = {}
        self._mocr = None
        self._mocr_failed = False
        self._lock = threading.Lock()

    def reader(self, source_lang: str):
        easyocr = optional.get("easyocr")
        if easyocr is None:
            raise RuntimeError(
                "EasyOCR is not installed, so text cannot be detected. "
                "Install the full requirements (pip install -r requirements-full.txt)."
            )
        langs = tuple(easyocr_langs(source_lang))
        with self._lock:
            if langs not in self._readers:
                log.info("Loading EasyOCR reader %s (gpu=%s); first use downloads models", list(langs), self.gpu)
                try:
                    self._readers[langs] = easyocr.Reader(list(langs), gpu=self.gpu, verbose=False)
                except ValueError as e:
                    raise RuntimeError(f"EasyOCR does not support source language '{source_lang}': {e}") from e
            return self._readers[langs]

    @property
    def manga_ocr_failed(self) -> bool:
        return self._mocr_failed

    def manga_ocr(self):
        if not self.use_manga_ocr or self._mocr_failed:
            return None
        if self._mocr is not None:
            return self._mocr
        mod = optional.get("manga_ocr")
        if mod is None:
            self._mocr_failed = True
            return None
        with self._lock:
            if self._mocr is None:
                try:
                    log.info("Loading manga-ocr model; first use downloads it (~450 MB)")
                    self._mocr = mod.MangaOcr(force_cpu=not self.gpu)
                except Exception as e:
                    log.warning("manga-ocr failed to load (%s); using EasyOCR for Japanese", e)
                    self._mocr_failed = True
                    return None
        return self._mocr


def _crop(image: np.ndarray, block: Block, pad_ratio: float = 0.1) -> np.ndarray:
    h, w = image.shape[:2]
    pad = int(max(2, block.char_size * pad_ratio))
    return image[max(0, block.y0 - pad) : min(h, block.y1 + pad), max(0, block.x0 - pad) : min(w, block.x1 + pad)]


def column_to_line(crop: np.ndarray) -> np.ndarray:
    """Re-lay a vertical text column as a horizontal line of character cells.

    EasyOCR only reads horizontal lines; cutting the column at the blank rows
    between glyphs and placing the glyphs side by side lets it read vertical
    Japanese/Chinese reasonably well.
    """
    gray = crop.mean(axis=2) if crop.ndim == 3 else crop.astype(float)
    h, w = gray.shape
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    bg = float(np.median(border))
    ink_rows = (np.abs(gray - bg) > 60).any(axis=1)

    segs: list[list[int]] = []
    start = None
    for y, ink in enumerate(ink_rows):
        if ink and start is None:
            start = y
        elif not ink and start is not None:
            segs.append([start, y])
            start = None
    if start is not None:
        segs.append([start, h])
    if not segs:
        return crop

    # Glyphs are roughly square: join pieces of one glyph (e.g. 二, 三, ⋮) ...
    merged: list[list[int]] = []
    for s in segs:
        if merged and s[1] - merged[-1][0] <= w * 1.05:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    # ... and split runs of touching glyphs.
    cells: list[tuple[int, int]] = []
    for a, b in merged:
        n = max(1, round((b - a) / max(1, w)))
        step = (b - a) / n
        cells.extend((int(a + i * step), int(a + (i + 1) * step)) for i in range(n))

    pad = max(2, w // 4)
    fill = np.uint8(bg)
    line = np.full((w + 2 * pad, len(cells) * w + 2 * pad) + crop.shape[2:], fill, dtype=crop.dtype)
    for i, (a, b) in enumerate(cells):
        glyph = crop[a:b]
        off = pad + (w - glyph.shape[0]) // 2
        line[off : off + glyph.shape[0], pad + i * w : pad + (i + 1) * w] = glyph
    return line


def _recognize(reader, img: np.ndarray) -> str:
    h, w = img.shape[:2]
    if h < 4 or w < 4:
        return ""
    try:
        res = reader.recognize(img, horizontal_list=[[0, w, 0, h]], free_list=[], detail=0, paragraph=False)
    except Exception as e:
        log.debug("EasyOCR recognize failed: %s", e)
        return ""
    return " ".join(r for r in res if r).strip()


def split_columns(crop: np.ndarray) -> list[tuple[int, int]]:
    """Split a block of vertical text into columns via the ink x-profile.

    Returns x ranges, right to left. Empty if the block doesn't look like clean
    vertical columns (busy background, horizontal text).
    """
    gray = crop.mean(axis=2) if crop.ndim == 3 else crop.astype(float)
    h, w = gray.shape
    border = np.concatenate([gray[0], gray[-1], gray[:, 0], gray[:, -1]])
    bg = float(np.median(border))
    if (np.abs(border - bg) > 60).mean() > 0.2:
        return []  # busy surroundings: projection would be meaningless
    ink_cols = (np.abs(gray - bg) > 60).any(axis=0)
    runs, start = [], None
    for x, ink in enumerate(ink_cols):
        if ink and start is None:
            start = x
        elif not ink and start is not None:
            runs.append([start, x])
            start = None
    if start is not None:
        runs.append([start, w])
    if not runs:
        return []
    # Bridge tiny gaps inside one glyph column.
    widest = max(b - a for a, b in runs)
    merged = [runs[0]]
    for a, b in runs[1:]:
        if a - merged[-1][1] < widest * 0.15 and b - merged[-1][0] <= widest * 1.3:
            merged[-1][1] = b
        else:
            merged.append([a, b])
    widths = sorted(b - a for a, b in merged)
    med = widths[len(widths) // 2]
    cols = [(a, b) for a, b in merged if b - a >= 0.5 * med]  # drop furigana / specks
    if not cols or h < 1.5 * med:
        return []
    return sorted(cols, key=lambda c: -c[0])


def _easyocr_block(reader, image: np.ndarray, block: Block, lang: str) -> str:
    """Recognise a block line by line (or column by column) and join in reading order."""
    vertical_script = lang in ("ja", "zh")
    if vertical_script and block.h > 1.2 * block.char_size:
        pad = max(2, int(block.char_size * 0.15))
        y0, x0 = max(0, block.y0 - pad), max(0, block.x0 - pad)
        crop = image[y0 : block.y1 + pad, x0 : block.x1 + pad]
        cols = split_columns(crop)
        if cols:
            parts = []
            for a, b in cols:
                col = crop[:, a:b]
                rows = np.nonzero((np.abs(col.mean(axis=2) - np.median(col)) > 60).any(axis=1))[0]
                if len(rows):
                    col = col[rows[0] : rows[-1] + 1]
                parts.append(_recognize(reader, column_to_line(col)))
            return "".join(p for p in parts if p).strip()

    boxes = block.boxes or [block.rect]
    row = max(1.0, block.char_size * 0.6)
    order = sorted(boxes, key=lambda b: (round(b[1] / row), b[0]))
    parts = [_recognize(reader, image[b[1] : b[3], b[0] : b[2]]) for b in order]
    return ("" if vertical_script else " ").join(p for p in parts if p).strip()


def recognize_blocks(engines: OcrEngines, image: np.ndarray, blocks: list[Block], source_lang: str) -> list[Block]:
    """Fill block.text; return only blocks that contain letters or digits."""
    lang = base_lang(source_lang)
    mocr = engines.manga_ocr() if lang == "ja" else None
    reader = None
    for b in blocks:
        text = ""
        if mocr is not None:
            try:
                text = mocr(Image.fromarray(_crop(image, b)))
            except Exception as e:
                log.debug("manga-ocr failed on block %s: %s", b.rect, e)
        if not text:
            if reader is None:
                reader = engines.reader(source_lang)
            text = _easyocr_block(reader, image, b, lang)
        b.text = " ".join(text.split()) if lang not in ("ja", "zh") else "".join(text.split())
        if lang in ("ja", "zh", "ko"):
            # CJK glyphs are roughly square: area / glyph count ~ glyph size squared.
            n = sum(1 for c in b.text if not c.isspace())
            if n:
                b.size_hint = max(6.0, (b.w * b.h / n) ** 0.5)
    return [b for b in blocks if has_text(b.text)]

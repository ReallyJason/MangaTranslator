"""Draw translated text into the erased blocks with Pillow."""

from __future__ import annotations

import logging
import re
from pathlib import Path

from PIL import Image, ImageDraw

from . import fonts
from .erase import EraseInfo
from .models import Block, Rect, base_lang

log = logging.getLogger("fukidashi.typeset")

MIN_SIZE = 7
# Characters that must not start a line in Japanese/Chinese (simple kinsoku shori).
NO_LINE_START = set("、。，．・：；！？!?,.:;」』）)】〉》〕］}ー～…‥ゃゅょっャュョッァィゥェォぁぃぅぇぉ")


def _len(font, s: str) -> float:
    return font.getlength(s)


MIN_PIECE = 3  # never leave fewer than this many letters on either side of a hyphen


def _break_word(word: str, font, max_w: float, hyphen: bool) -> list[str]:
    """Split a word that is wider than max_w into pieces, hyphenating if asked."""
    pieces, cur = [], ""
    mark = "-" if hyphen else ""
    for ch in word:
        if not cur or _len(font, cur + ch + mark) <= max_w:
            cur += ch
        else:
            pieces.append(cur)
            cur = ch
    pieces.append(cur)
    # Rebalance so the last piece isn't a stub like "y!" or "ss.".
    if hyphen and len(pieces) > 1:
        letters = sum(c.isalnum() for c in pieces[-1])
        if letters < MIN_PIECE and len(pieces[-2]) > MIN_PIECE * 2:
            take = MIN_PIECE - letters
            pieces[-1] = pieces[-2][-take:] + pieces[-1]
            pieces[-2] = pieces[-2][:-take]
    return [p + mark for p in pieces[:-1]] + [pieces[-1]]


def _tokens(para: str) -> list[tuple[str, bool]]:
    """Words split after existing hyphens; (token, preceded_by_space)."""
    out = []
    for word in para.split():
        parts = re.findall(r"[^-]+-*|-+", word) or [word]
        out.extend((p, i == 0) for i, p in enumerate(parts))
    return out


def wrap_words(text: str, font, max_w: float, hyphen: bool = True) -> tuple[list[str], bool]:
    """Greedy word wrap. Returns (lines, broke_a_word)."""
    lines: list[str] = []
    broke = False
    for para in text.split("\n"):
        cur = ""
        for tok, space in _tokens(para):
            cand = f"{cur}{' ' if space and cur else ''}{tok}"
            if _len(font, cand) <= max_w:
                cur = cand
                continue
            if cur:
                lines.append(cur)
                cur = ""
            if _len(font, tok) <= max_w:
                cur = tok
            else:
                broke = True
                pieces = _break_word(tok, font, max_w, hyphen)
                lines.extend(pieces[:-1])
                cur = pieces[-1]
        if cur:
            lines.append(cur)
    return lines, broke


def wrap_chars(text: str, font, max_w: float) -> list[str]:
    lines: list[str] = []
    for para in text.split("\n"):
        cur = ""
        for ch in para.strip():
            if not cur or _len(font, cur + ch) <= max_w:
                cur += ch
                continue
            if ch in NO_LINE_START and len(cur) > 1:
                lines.append(cur[:-1])
                cur = cur[-1] + ch
            else:
                lines.append(cur)
                cur = ch.lstrip()
        if cur:
            lines.append(cur)
    return lines


def wrap(text: str, font, max_w: float, lang: str) -> tuple[list[str], bool]:
    """Returns (lines, broke_a_word)."""
    b = base_lang(lang)
    if b in ("ja", "zh"):
        return wrap_chars(text, font, max_w), False
    # Korean separates words with spaces; break overlong words without a hyphen.
    return wrap_words(text, font, max_w, hyphen=b != "ko")


def line_height(font) -> int:
    try:
        ascent, descent = font.getmetrics()
        return int(ascent + descent)
    except Exception:
        return int(font.size * 1.2)


def fit_text(
    text: str, font_path: str | None, max_w: int, max_h: int, lang: str, max_size: int, min_size: int = MIN_SIZE
) -> tuple[int, list[str]]:
    """Largest font size (binary search) whose wrapped text fits in max_w x max_h.

    Layouts that keep every word whole are preferred: a hyphenated layout is
    only used if it allows a clearly larger font.
    """

    def attempt(size: int, allow_break: bool):
        font = fonts.load(font_path, size)
        lines, broke = wrap(text, font, max_w, lang)
        lh = line_height(font)
        ok = len(lines) * lh <= max_h and all(_len(font, ln) <= max_w for ln in lines)
        return ok and (allow_break or not broke), lines

    def search(allow_break: bool):
        lo, hi = min_size, max(min_size, max_size)
        best = None
        while lo <= hi:
            mid = (lo + hi) // 2
            ok, lines = attempt(mid, allow_break)
            if ok:
                best = (mid, lines)
                lo = mid + 1
            else:
                hi = mid - 1
        return best

    whole = search(allow_break=False)
    broken = search(allow_break=True)
    if whole and (not broken or whole[0] >= max(min_size + 3, broken[0] * 0.6)):
        size, lines = whole
    elif broken:
        size, lines = broken
    else:
        size, lines = min_size, attempt(min_size, True)[1]  # doesn't fit; draw at minimum size
    return size, balance(text, font_path, size, lines, max_w, lang)


def balance(text: str, font_path: str | None, size: int, lines: list[str], max_w: int, lang: str) -> list[str]:
    """Narrow the wrap width while the line count stays the same, for evener lines."""
    if len(lines) < 2:
        return lines
    font = fonts.load(font_path, size)
    _, broke_before = wrap(text, font, max_w, lang)
    best = lines
    w = max_w
    while w > max_w * 0.5:
        w = int(w * 0.94)
        trial, broke = wrap(text, font, w, lang)
        if len(trial) != len(lines) or (broke and not broke_before):
            break
        best = trial
    return best


def text_rect(block: Block, info: EraseInfo, img_w: int, img_h: int) -> Rect:
    """Area to typeset into: the bubble interior if known, else the block, widened if narrow."""
    x0, y0, x1, y1 = info.region
    if not info.bubble:
        w, h = x1 - x0, y1 - y0
        # Vertical CJK columns are tall and narrow; horizontal text needs width.
        if w < 0.6 * h:
            new_w = int(min(0.75 * h, w * 3))
            cx = (x0 + x1) // 2
            x0, x1 = cx - new_w // 2, cx + new_w - new_w // 2
            if x0 < 0:
                x1, x0 = x1 - x0, 0
            if x1 > img_w:
                x0, x1 = max(0, x0 - (x1 - img_w)), img_w
    return (max(0, x0), max(0, y0), min(img_w, x1), min(img_h, y1))


def _colors(bg: tuple[int, int, int]) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    lum = 0.299 * bg[0] + 0.587 * bg[1] + 0.114 * bg[2]
    return ((0, 0, 0), (255, 255, 255)) if lum >= 128 else ((255, 255, 255), (0, 0, 0))


def render_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    rect: Rect,
    info: EraseInfo,
    char_size: float,
    lang: str,
    font_path: str | None,
) -> int:
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    pad_x, pad_y = max(1, int(w * 0.04)), max(1, int(h * 0.04))
    max_w, max_h = max(4, w - 2 * pad_x), max(4, h - 2 * pad_y)
    # Cap the size relative to the original glyphs so short lines don't balloon.
    max_size = int(max(MIN_SIZE + 1, min(max_h, char_size * 1.05, 160)))

    size, lines = fit_text(text, font_path, max_w, max_h, lang, max_size)
    font = fonts.load(font_path, size)
    fg, outline = _colors(info.bg)
    stroke = max(1, round(size * (0.07 if info.flat else 0.13)))
    lh = line_height(font)
    total = lh * len(lines)
    cx = (x0 + x1) / 2
    y = (y0 + y1) / 2 - total / 2
    for ln in lines:
        lw = _len(font, ln)
        draw.text((cx - lw / 2, y), ln, font=font, fill=fg, stroke_width=stroke, stroke_fill=outline)
        y += lh
    return size


def render_blocks(
    image: Image.Image,
    blocks: list[Block],
    infos: list[EraseInfo],
    target_lang: str,
    fonts_dir: Path,
    font_name: str = "",
) -> None:
    """Typeset block.translation for each block in-place on a PIL RGB image."""
    draw = ImageDraw.Draw(image)
    all_text = "".join(b.translation or "" for b in blocks)
    font_path = fonts.font_for(fonts_dir, target_lang, font_name, all_text)
    for b, info in zip(blocks, infos):
        if not b.translation:
            continue
        rect = text_rect(b, info, image.width, image.height)
        render_block(draw, b.translation.strip(), rect, info, b.char_size, target_lang, font_path)

"""Synthetic manga page for tests: gradient + screentone, speech bubbles, text over art.

Returns the image and ground-truth text blocks, so the erase/typeset stages can
be tested with mocked detection.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from fukidashi import fonts
from fukidashi.models import Block


@dataclass
class SyntheticPage:
    image: Image.Image
    blocks: list[Block]
    translations: list[str]
    kinds: list[str]  # "bubble" | "dark-bubble" | "art"


def _cjk_font(size: int):
    path = fonts.resolve_font("/nonexistent", "ja", "", "あ漢")
    return ImageFont.truetype(path, size) if path else None


def _draw_vertical(draw: ImageDraw.ImageDraw, columns: list[str], right_x: int, top_y: int, font, size: int, fill):
    """Japanese-style vertical text: columns right to left. Returns member boxes."""
    boxes = []
    col_gap = int(size * 0.35)
    x = right_x - size
    for col in columns:
        y = top_y
        for ch in col:
            draw.text((x, y), ch, font=font, fill=fill)
            y += int(size * 1.05)
        bbox = (x, top_y, x + size, y)
        boxes.append(bbox)
        x -= size + col_gap
    return boxes


def _draw_horizontal(draw, lines, left_x, top_y, font, size, fill, stroke=0, stroke_fill=None):
    boxes = []
    y = top_y
    for line in lines:
        draw.text((left_x, y), line, font=font, fill=fill, stroke_width=stroke, stroke_fill=stroke_fill)
        l, t, r, b = draw.textbbox((left_x, y), line, font=font, stroke_width=stroke)
        boxes.append((int(l), int(t), int(r), int(b)))
        y += int(size * 1.25)
    return boxes


def _block(boxes) -> Block:
    xs0, ys0, xs1, ys1 = zip(*boxes)
    return Block(min(xs0), min(ys0), max(xs1), max(ys1), boxes=[tuple(map(int, b)) for b in boxes])


def make_page(width: int = 900, height: int = 1300, seed: int = 7, with_text: bool = True) -> SyntheticPage:
    """with_text=False renders the identical page minus the text (ground truth for erasing)."""
    rng = np.random.default_rng(seed)
    # Background: vertical gradient + diagonal screentone hatching + noise.
    yy, xx = np.mgrid[0:height, 0:width]
    base = 235 - 120 * (yy / height)
    hatch = ((xx + yy) // 6 % 3 == 0) * 35
    dots = ((xx % 9 < 3) & (yy % 9 < 3)) * 25 * (yy > height * 0.55)
    gray = np.clip(base - hatch - dots + rng.normal(0, 6, (height, width)), 0, 255).astype(np.uint8)
    rgb = np.stack([gray, (gray * 0.97).astype(np.uint8), (gray * 0.92).astype(np.uint8)], axis=2)
    img = Image.fromarray(rgb)
    d = ImageDraw.Draw(img)
    # Text goes to a scratch canvas when with_text=False, so boxes are still computed.
    t = d if with_text else ImageDraw.Draw(Image.new("RGB", img.size))

    # Panel borders.
    for box in [(20, 20, 880, 640), (20, 660, 430, 1280), (450, 660, 880, 1280)]:
        d.rectangle(box, outline=(0, 0, 0), width=5)

    blocks, kinds, translations = [], [], []
    size = 30
    jp = _cjk_font(size)

    # Bubble 1: large white ellipse, vertical Japanese.
    d.ellipse((520, 60, 820, 400), fill=(255, 255, 255), outline=(0, 0, 0), width=4)
    if jp:
        boxes = _draw_vertical(t, ["おい、待てよ！", "どこへ行く", "つもりだ？"], 720, 120, jp, size, (0, 0, 0))
    else:
        boxes = _draw_horizontal(t, ["HEY, WAIT!", "WHERE ARE", "YOU GOING?"], 580, 180, fonts.load(None, size), size, (0, 0, 0))
    blocks.append(_block(boxes))
    kinds.append("bubble")
    translations.append("Hey, wait! Where do you think you're going?")

    # Bubble 2: small, tall bubble, two short columns.
    d.ellipse((90, 90, 300, 430), fill=(255, 255, 255), outline=(0, 0, 0), width=4)
    if jp:
        boxes = _draw_vertical(t, ["関係", "ないだろ"], 225, 150, jp, size, (0, 0, 0))
    else:
        boxes = _draw_horizontal(t, ["NONE OF", "YOUR", "BUSINESS"], 130, 200, fonts.load(None, size), size, (0, 0, 0))
    blocks.append(_block(boxes))
    kinds.append("bubble")
    translations.append("That's none of your business.")

    # Narration over busy art (no bubble) -> inpainting path.
    if jp:
        boxes = _draw_vertical(t, ["その日", "全てが変わった"], 380, 380, jp, 26, (255, 255, 255))
    else:
        boxes = _draw_horizontal(t, ["THAT DAY", "EVERYTHING CHANGED"], 260, 460, fonts.load(None, 26), 26, (255, 255, 255))
    blocks.append(_block(boxes))
    kinds.append("art")
    translations.append("That day, everything changed.")

    # Dark bubble with white text (thought / flashback style).
    d.ellipse((520, 760, 840, 1060), fill=(20, 20, 20), outline=(255, 255, 255), width=3)
    if jp:
        boxes = _draw_vertical(t, ["もう二度と", "負けない"], 715, 830, jp, size, (255, 255, 255))
    else:
        boxes = _draw_horizontal(t, ["NEVER", "AGAIN"], 600, 860, fonts.load(None, size), size, (255, 255, 255))
    blocks.append(_block(boxes))
    kinds.append("dark-bubble")
    translations.append("I'll never lose again.")

    # A long one with an overlong word, to exercise wrapping/hyphenation.
    d.rounded_rectangle((60, 760, 390, 980), radius=40, fill=(250, 250, 250), outline=(0, 0, 0), width=4)
    if jp:
        boxes = _draw_vertical(t, ["超電磁砲の", "研究所だ"], 270, 790, jp, 26, (0, 0, 0))
    else:
        boxes = _draw_horizontal(t, ["SUPER", "LAB"], 150, 820, fonts.load(None, 26), 26, (0, 0, 0))
    blocks.append(_block(boxes))
    kinds.append("bubble")
    translations.append("It's the Electromagnetic-Railgun-Supercollider Laboratory!")

    return SyntheticPage(img, blocks, translations, kinds)

"""Erase + typeset on a synthetic manga page with mocked detection and translation.

Writes PNGs to tests/output/: before.png, erased_<inpainter>.png, after_<inpainter>.png
and comparison_<inpainter>.png (before | after).
"""

import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from fukidashi import fonts, optional
from fukidashi.config import SERVER_DIR
from fukidashi.erase import LamaInpainter, erase_blocks
from fukidashi.typeset import fit_text, line_height, render_blocks, text_rect, wrap
from synthetic import make_page

LAMA_PATH = SERVER_DIR / "models" / "big-lama.pt"
FONTS_DIR = SERVER_DIR / "fonts"


def _inpainters():
    params = [pytest.param(None, id="opencv")]
    lama_ok = optional.available("torch") and (LAMA_PATH.exists() or os.environ.get("FUKIDASHI_TEST_LAMA") == "1")
    params.append(
        pytest.param(
            "lama", id="lama", marks=pytest.mark.skipif(not lama_ok, reason="torch or models/big-lama.pt not available")
        )
    )
    return params


def _side_by_side(a: Image.Image, b: Image.Image) -> Image.Image:
    out = Image.new("RGB", (a.width + b.width + 20, max(a.height, b.height)), "white")
    out.paste(a, (0, 0))
    out.paste(b, (a.width + 20, 0))
    return out


@pytest.mark.parametrize("inpainter", _inpainters())
def test_erase_and_typeset_synthetic_page(inpainter, output_dir: Path):
    page = make_page()
    clean = np.asarray(make_page(with_text=False).image).astype(int)
    page.image.save(output_dir / "before.png")

    lama = LamaInpainter(SERVER_DIR / "models", "https://github.com/enesmsahin/simple-lama-inpainting/releases/download/v0.1.0/big-lama.pt", gpu=False) if inpainter else None
    name = inpainter or "opencv"

    arr = np.array(page.image)
    for b, t in zip(page.blocks, page.translations):
        b.translation = t  # mocked translation
    infos = erase_blocks(arr, page.blocks, lama)  # mocked detection: ground-truth blocks
    Image.fromarray(arr).save(output_dir / f"erased_{name}.png")
    erased = arr.astype(int).copy()

    for b, info, kind in zip(page.blocks, infos, page.kinds):
        x0, y0, x1, y1 = b.rect
        region_err = np.abs(erased[y0:y1, x0:x1] - clean[y0:y1, x0:x1]).mean()
        if kind == "bubble":
            assert info.flat and info.bubble, f"bubble not recognised at {b.rect}"
            assert info.bg[0] > 240
            # Text strokes fully removed: the block now matches the clean bubble.
            assert region_err < 1.0, f"text left in bubble {b.rect}: err {region_err:.2f}"
        elif kind == "dark-bubble":
            assert info.flat and info.bg[0] < 40
            assert region_err < 1.0
        else:  # text over art -> inpainted
            assert not info.flat
            limit = 12 if inpainter == "lama" else 30
            assert region_err < limit, f"{name} inpaint error {region_err:.1f} at {b.rect}"
            if inpainter is None:
                print(f"opencv inpaint error {region_err:.1f}")
            else:
                print(f"lama inpaint error {region_err:.1f}")

    # Bubble outlines and panel borders must survive erasing (outside the blocks only).
    outside = np.ones(arr.shape[:2], bool)
    for b in page.blocks:
        pad = 12
        outside[max(0, b.y0 - pad) : b.y1 + pad, max(0, b.x0 - pad) : b.x1 + pad] = False
    art = next(b for b, k in zip(page.blocks, page.kinds) if k == "art")
    ctx = 220  # inpainting only writes inside the padded block, but be generous around it
    outside[max(0, art.y0 - ctx) : art.y1 + ctx, max(0, art.x0 - ctx) : art.x1 + ctx] = False
    assert np.abs(erased - np.asarray(page.image).astype(int))[outside].max() == 0

    # Typeset.
    pil = Image.fromarray(arr)
    render_blocks(pil, page.blocks, infos, "en", FONTS_DIR)
    pil.save(output_dir / f"after_{name}.png")
    _side_by_side(page.image, pil).save(output_dir / f"comparison_{name}.png")
    final = np.asarray(pil).astype(int)

    changed = np.abs(final - erased).sum(axis=2) > 0
    for b, info in zip(page.blocks, infos):
        rect = text_rect(b, info, pil.width, pil.height)
        x0, y0, x1, y1 = rect
        inside = np.zeros_like(changed)
        inside[y0:y1, x0:x1] = True
        assert changed[inside].any(), f"no text drawn for {b.rect}"
        if info.bubble:
            # Text stays inside the bubble's text area (stroke may add a pixel or two).
            grow = np.zeros_like(changed)
            grow[max(0, y0 - 3) : y1 + 3, max(0, x0 - 3) : x1 + 3] = True
            others = np.zeros_like(changed)
            for ob, oi in zip(page.blocks, infos):
                if ob is not b:
                    r = text_rect(ob, oi, pil.width, pil.height)
                    others[max(0, r[1] - 3) : r[3] + 3, max(0, r[0] - 3) : r[2] + 3] = True
            stray = changed & ~grow & ~others
            assert not stray.any()

    # Light bubbles get dark text, the dark bubble gets light text.
    for b, info, kind in zip(page.blocks, infos, page.kinds):
        x0, y0, x1, y1 = text_rect(b, info, pil.width, pil.height)
        region = final[y0:y1, x0:x1].mean(axis=2)
        if kind == "bubble":
            assert (region < 60).sum() > 50
        if kind == "dark-bubble":
            assert (region > 200).sum() > 50


# --- typesetting units --------------------------------------------------------


def _font(size=20, lang="en", text="Ag"):
    return fonts.load(fonts.font_for(FONTS_DIR, lang, "", text), size)


def test_fit_text_respects_box():
    path = fonts.font_for(FONTS_DIR, "en")
    for text in ["Hi!", "Where do you think you're going at this hour?", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"]:
        size, lines = fit_text(text, path, 120, 90, "en", max_size=60)
        font = fonts.load(path, size)
        if size > 7:
            assert len(lines) * line_height(font) <= 90
            assert all(font.getlength(ln) <= 120 for ln in lines)


def test_short_text_does_not_balloon():
    path = fonts.font_for(FONTS_DIR, "en")
    size, _ = fit_text("Eh?", path, 300, 300, "en", max_size=32)
    assert size == 32


def test_prefers_whole_words_over_hyphenation():
    path = fonts.font_for(FONTS_DIR, "en")
    size, lines = fit_text("That's none of your business.", path, 80, 160, "en", max_size=30)
    assert not any(ln.endswith("-") for ln in lines), lines


def test_hyphenation_leaves_no_stub():
    font = _font(20)
    lines, broke = wrap("Supercalifragilisticexpialidocious!", font, 90, "en")
    assert broke
    assert all(len(ln.rstrip("-")) >= 3 for ln in lines), lines
    assert all(ln.endswith("-") for ln in lines[:-1])


def test_breaks_at_existing_hyphens_first():
    font = _font(20)
    width = font.getlength("Supercollider") + 2  # fits alone, not with "Railgun-"
    lines, broke = wrap("Railgun-Supercollider", font, width, "en")
    assert lines == ["Railgun-", "Supercollider"] and not broke


def test_cjk_char_wrap_and_kinsoku():
    font = _font(20, "ja", "おい待てよ")
    lines, _ = wrap("おい、待てよ。どこへ行くつもりだ？", font, 60, "ja")
    assert len(lines) > 1
    assert all(ln[0] not in "、。？" for ln in lines), lines


def test_cjk_target_font_has_glyphs():
    path = fonts.font_for(FONTS_DIR, "ja", "", "日本語")
    if path is None:
        pytest.skip("no CJK font installed on this machine")
    assert fonts.supports(path, "あ漢")


def test_narrow_vertical_box_is_widened():
    from fukidashi.erase import EraseInfo
    from fukidashi.models import Block

    b = Block(500, 100, 540, 400)
    info = EraseInfo(flat=False, bg=(128, 128, 128), region=(495, 95, 545, 405))
    x0, y0, x1, y1 = text_rect(b, info, 1000, 1000)
    assert x1 - x0 > 3 * 40 * 0.9 and (y0, y1) == (95, 405)
    assert x0 < 520 < x1  # still centred on the column


def test_render_on_page_edge_does_not_crash():
    from fukidashi.erase import EraseInfo
    from fukidashi.models import Block

    img = Image.new("RGB", (200, 300), "white")
    b = Block(0, 0, 30, 280, translation="Edge case text that is long")
    info = EraseInfo(flat=True, bg=(255, 255, 255), region=(0, 0, 30, 280))
    render_blocks(img, [b], [info], "en", FONTS_DIR)
    assert ImageDraw.Draw(img)  # rendered without exceptions


def test_jpeg_page_leaves_no_ghost_text(output_dir):
    """Real manga pages are JPEGs: compression ringing around glyphs must be erased too."""
    import io

    page = make_page()
    buf = io.BytesIO()
    page.image.save(buf, format="JPEG", quality=90)
    arr = np.array(Image.open(io.BytesIO(buf.getvalue())).convert("RGB"))
    infos = erase_blocks(arr, page.blocks, None)
    Image.fromarray(arr).save(output_dir / "erased_jpeg.png")
    for b, info, kind in zip(page.blocks, infos, page.kinds):
        if kind != "bubble":
            continue
        x0, y0, x1, y1 = b.rect
        region = arr[y0:y1, x0:x1].astype(int)
        dev = np.abs(region - np.array(info.bg)).max(axis=2)
        # Even faint leftovers a few levels off a white bubble read as a ghost of the old text.
        assert (dev > 6).sum() == 0, f"ghost pixels left in {b.rect}: {(dev > 6).sum()}"
    # The bubble outline must still be there (left edge of the first bubble at y=230).
    assert arr[230, 518:526].min() < 80

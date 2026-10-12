"""Font resolution: server/fonts first, then system fonts, then Pillow's default."""

from __future__ import annotations

import functools
import logging
import os
import sys
from pathlib import Path

from PIL import ImageFont

from .models import base_lang

log = logging.getLogger("fukidashi.fonts")

FONT_EXTS = (".ttf", ".otf", ".ttc")

_WIN = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
_WIN_USER = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Windows/Fonts"
_MAC = [Path("/System/Library/Fonts"), Path("/System/Library/Fonts/Supplemental"), Path("/Library/Fonts"), Path.home() / "Library/Fonts"]
_LINUX = [Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".local/share/fonts", Path.home() / ".fonts"]

# File names tried in order. Bold, legible sans faces read best in bubbles.
LATIN_FONTS = [
    "arialbd.ttf", "segoeuib.ttf", "verdanab.ttf", "tahomabd.ttf",  # Windows
    "Arial Bold.ttf", "Verdana Bold.ttf", "Helvetica.ttc", "Arial.ttf",  # macOS
    "NotoSans-Bold.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf",  # Linux
    "Ubuntu-B.ttf", "OpenSans-Bold.ttf", "DejaVuSans.ttf", "arial.ttf",
]

CJK_FONTS = {
    "ja": [
        "YuGothB.ttc", "meiryob.ttc", "meiryo.ttc", "msgothic.ttc", "YuGothM.ttc",
        "ヒラギノ角ゴシック W6.ttc", "ヒラギノ角ゴシック W3.ttc", "Hiragino Sans GB.ttc",
        "NotoSansCJK-Bold.ttc", "NotoSansCJKjp-Bold.otf", "NotoSansJP-Bold.ttf", "NotoSansCJK-Regular.ttc",
        "NotoSansJP-Regular.ttf", "ipagp.ttf", "ipag.ttf", "TakaoPGothic.ttf", "VL-PGothic-Regular.ttf",
        "wqy-zenhei.ttc", "DroidSansFallbackFull.ttf",
    ],
    "zh": [
        "msyhbd.ttc", "msyh.ttc", "simhei.ttf", "simsun.ttc",
        "PingFang.ttc", "STHeiti Medium.ttc", "Hiragino Sans GB.ttc",
        "NotoSansCJK-Bold.ttc", "NotoSansCJKsc-Bold.otf", "NotoSansSC-Bold.ttf", "NotoSansCJK-Regular.ttc",
        "wqy-zenhei.ttc", "wqy-microhei.ttc", "DroidSansFallbackFull.ttf",
    ],
    "ko": [
        "malgunbd.ttf", "malgun.ttf", "gulim.ttc",
        "AppleSDGothicNeo.ttc", "AppleGothic.ttf",
        "NotoSansCJK-Bold.ttc", "NotoSansCJKkr-Bold.otf", "NotoSansKR-Bold.ttf", "NotoSansCJK-Regular.ttc",
        "NanumGothicBold.ttf", "NanumGothic.ttf", "UnDotum.ttf", "wqy-zenhei.ttc", "DroidSansFallbackFull.ttf",
    ],
}

# Characters used to check that a font really covers the target script.
SAMPLES = {"ja": "あア漢", "zh": "汉語的", "ko": "한국어", "ru": "Жд", "uk": "Їж", "el": "Ωλ", "vi": "ệơ"}


def _search_dirs() -> list[Path]:
    if sys.platform.startswith("win"):
        return [_WIN, _WIN_USER]
    if sys.platform == "darwin":
        return _MAC
    return _LINUX


@functools.lru_cache(maxsize=1)
def _system_index() -> dict[str, Path]:
    """Map lowercase file name -> path for every font file in the system font dirs."""
    index: dict[str, Path] = {}
    for d in _search_dirs():
        if not d.is_dir():
            continue
        for root, _dirs, files in os.walk(d):
            for f in files:
                if f.lower().endswith(FONT_EXTS):
                    index.setdefault(f.lower(), Path(root) / f)
    return index


def user_fonts(fonts_dir: Path) -> list[Path]:
    if not fonts_dir.is_dir():
        return []
    return sorted(p for p in fonts_dir.iterdir() if p.suffix.lower() in FONT_EXTS)


def _glyph_signature(font, ch: str) -> bytes:
    mask = font.getmask(ch)
    return bytes(mask.size) + bytes(mask)


@functools.lru_cache(maxsize=4096)
def _has_glyph(path: str, ch: str) -> bool:
    font = _probe_font(path)
    if font is None:
        return False
    try:
        if font.getmask(ch).getbbox() is None:
            return False
        tofu = {_glyph_signature(font, "\uffff"), _glyph_signature(font, "\U0010fffd")}
        return _glyph_signature(font, ch) not in tofu
    except Exception:
        return False


@functools.lru_cache(maxsize=128)
def _probe_font(path: str):
    try:
        return ImageFont.truetype(path, 32)
    except Exception:
        return None


def supports(path: str, sample: str) -> bool:
    """True if every sample character renders as a real glyph (not .notdef/tofu)."""
    return all(_has_glyph(path, ch) for ch in sample)


def _sample_for(lang: str, text: str = "") -> str:
    extra = "".join(sorted({c for c in text if ord(c) > 0x2FF and not c.isspace()}))[:8]
    return SAMPLES.get(base_lang(lang), "") + extra


def candidates(fonts_dir: Path, target_lang: str, requested: str = "") -> list[Path]:
    lang = base_lang(target_lang)
    user = user_fonts(fonts_dir)
    if requested:
        req = requested.lower()
        user = sorted(user, key=lambda p: (req not in p.stem.lower(), p.name.lower()))
    index = _system_index()
    names = CJK_FONTS.get(lang, []) + LATIN_FONTS if lang in CJK_FONTS else LATIN_FONTS
    system = [index[n.lower()] for n in names if n.lower() in index]
    # Any other Noto font as a last resort before Pillow's default.
    system += [p for n, p in sorted(index.items()) if n.startswith("notosans") and p not in system]
    return user + system


@functools.lru_cache(maxsize=64)
def resolve_font(fonts_dir: str, target_lang: str, requested: str = "", sample: str = "") -> str | None:
    """Return the path of the first candidate that covers `sample` (or None for Pillow default)."""
    for path in candidates(Path(fonts_dir), target_lang, requested):
        if supports(str(path), sample or "Ag"):
            return str(path)
    log.warning("No font found covering %r for target %s; using Pillow default", sample, target_lang)
    return None


def font_for(fonts_dir: Path, target_lang: str, requested: str = "", text: str = "") -> str | None:
    return resolve_font(str(fonts_dir), target_lang, requested, _sample_for(target_lang, text))


@functools.lru_cache(maxsize=512)
def load(path: str | None, size: int):
    if path:
        try:
            return ImageFont.truetype(path, size)
        except Exception as e:
            log.warning("Could not load font %s (%s)", path, e)
    try:
        return ImageFont.load_default(size=size)
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def font_names(fonts_dir: Path) -> list[str]:
    return [p.stem for p in user_fonts(fonts_dir)]

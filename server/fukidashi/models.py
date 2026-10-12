"""Plain data types shared across the pipeline."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# (x0, y0, x1, y1) in image pixels, x1/y1 exclusive.
Rect = tuple[int, int, int, int]

MODES = ("context", "fast")
ENGINES = ("auto", "deepl", "google", "offline")
CJK_LANGS = ("ja", "zh", "zh-CN", "zh-TW", "ko")


def base_lang(code: str) -> str:
    return (code or "").split("-")[0].split("_")[0].lower()


def is_cjk(code: str) -> bool:
    return base_lang(code) in ("ja", "zh", "ko")


@dataclass
class Block:
    """A group of nearby detected text boxes, translated and typeset as a unit."""

    x0: int
    y0: int
    x1: int
    y1: int
    boxes: list[Rect] = field(default_factory=list)
    text: str = ""
    translation: Optional[str] = None
    size_hint: float = 0.0  # glyph size estimated from OCR text, if known

    @property
    def rect(self) -> Rect:
        return (self.x0, self.y0, self.x1, self.y1)

    @property
    def w(self) -> int:
        return self.x1 - self.x0

    @property
    def h(self) -> int:
        return self.y1 - self.y0

    @property
    def vertical(self) -> bool:
        return self.h > 1.5 * self.w

    @property
    def char_size(self) -> float:
        """Approximate glyph size: the median short side of the member boxes."""
        sizes = sorted(min(b[2] - b[0], b[3] - b[1]) for b in (self.boxes or [self.rect]))
        size = float(sizes[len(sizes) // 2]) if sizes else float(min(self.w, self.h))
        # Detectors sometimes merge neighbouring columns into one box; the text-based
        # estimate catches that.
        return min(size, self.size_hint) if self.size_hint > 0 else size


@dataclass
class TranslateOptions:
    mode: str = "context"
    engine: str = "auto"
    source_lang: str = "ja"
    target_lang: str = "en"
    session: str = ""
    font: str = ""

    @classmethod
    def from_dict(cls, d: dict | None) -> "TranslateOptions":
        d = d or {}
        o = cls(
            mode=str(d.get("mode") or "context").lower(),
            engine=str(d.get("engine") or "auto").lower(),
            source_lang=str(d.get("source_lang") or "ja"),
            target_lang=str(d.get("target_lang") or "en"),
            session=str(d.get("session") or ""),
            font=str(d.get("font") or ""),
        )
        if o.mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        if o.engine not in ENGINES:
            raise ValueError(f"engine must be one of {ENGINES}")
        return o

    def cache_dict(self) -> dict:
        """Options that affect the output image (session excluded)."""
        return {
            "mode": self.mode,
            "engine": self.engine,
            "source_lang": self.source_lang,
            "target_lang": self.target_lang,
            "font": self.font,
        }


@dataclass
class TranslationOutcome:
    translations: list[Optional[str]]
    mode: str  # mode actually used
    engine: str  # e.g. "anthropic:claude-haiku-5-5", "deepl"
    notes: list[str] = field(default_factory=list)
    degraded: bool = False  # fell back / partial; do not cache


@dataclass
class PipelineResult:
    png: Optional[bytes]  # None -> no text found (HTTP 204)
    headers: dict[str, str] = field(default_factory=dict)

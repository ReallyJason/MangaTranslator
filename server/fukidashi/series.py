"""Per-series story memory (running summary + name glossary) for context mode."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("fukidashi.series")

MAX_SUMMARY_CHARS = 2000
MAX_GLOSSARY = 300


def series_key(url: str) -> str:
    """hostname + path minus the last segment, e.g.
    https://site.com/manga/foo/chapter-12?p=1 -> site.com/manga/foo
    """
    if not url:
        return ""
    parts = urlsplit(url if "://" in url else f"http://{url}")
    host = (parts.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    segments = [s for s in parts.path.split("/") if s]
    if segments:
        segments = segments[:-1]
    return "/".join([host] + segments)


@dataclass
class SeriesContext:
    key: str
    summary: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    pages: int = 0
    updated: float = 0.0


class SeriesStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha1(key.encode("utf-8")).hexdigest()[:20] + ".json")

    def lock(self, key: str) -> threading.Lock:
        """Per-series lock so pages of one series update the context in order."""
        with self._guard:
            return self._locks.setdefault(key, threading.Lock())

    def load(self, key: str) -> SeriesContext:
        if not key:
            return SeriesContext(key="")
        path = self._path(key)
        try:
            data = json.loads(path.read_text("utf-8"))
            return SeriesContext(
                key=key,
                summary=str(data.get("summary", "")),
                glossary={str(k): str(v) for k, v in (data.get("glossary") or {}).items()},
                pages=int(data.get("pages", 0)),
                updated=float(data.get("updated", 0)),
            )
        except FileNotFoundError:
            return SeriesContext(key=key)
        except Exception as e:
            log.warning("Corrupt series context %s (%s); starting fresh", path, e)
            return SeriesContext(key=key)

    def save(self, ctx: SeriesContext) -> None:
        if not ctx.key:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        ctx.summary = ctx.summary[:MAX_SUMMARY_CHARS]
        if len(ctx.glossary) > MAX_GLOSSARY:
            ctx.glossary = dict(list(ctx.glossary.items())[-MAX_GLOSSARY:])
        ctx.updated = time.time()
        path = self._path(ctx.key)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(ctx), ensure_ascii=False, indent=1), "utf-8")
        tmp.replace(path)

    def update(self, ctx: SeriesContext, summary: str | None, glossary: dict[str, str] | None) -> None:
        if summary:
            ctx.summary = summary.strip()
        if glossary:
            for k, v in glossary.items():
                ctx.glossary.pop(k, None)  # re-insert so recent names survive trimming
                ctx.glossary[k] = v
        ctx.pages += 1
        self.save(ctx)

    def reset(self, key: str) -> bool:
        try:
            self._path(key).unlink()
            return True
        except FileNotFoundError:
            return False

    def reset_all(self) -> int:
        n = 0
        if self.dir.is_dir():
            for p in self.dir.glob("*.json"):
                p.unlink(missing_ok=True)
                n += 1
        return n

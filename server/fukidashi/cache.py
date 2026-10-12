"""On-disk result cache keyed by hash(image bytes + options minus session)."""

from __future__ import annotations

import hashlib
import json
import logging
import threading
from pathlib import Path

log = logging.getLogger("fukidashi.cache")

# Bump when pipeline output changes so stale renders are not served.
PIPELINE_VERSION = "1"


def cache_key(image_bytes: bytes, options: dict) -> str:
    h = hashlib.sha256()
    h.update(PIPELINE_VERSION.encode())
    h.update(json.dumps(options, sort_keys=True).encode())
    h.update(image_bytes)
    return h.hexdigest()


class ResultCache:
    def __init__(self, directory: Path, max_mb: int = 1024):
        self.dir = Path(directory)
        self.max_bytes = max(0, max_mb) * 1024 * 1024
        self._lock = threading.Lock()
        self._writes = 0

    def _paths(self, key: str) -> tuple[Path, Path]:
        sub = self.dir / key[:2]
        return sub / f"{key}.png", sub / f"{key}.json"

    def get(self, key: str) -> tuple[bool, bytes | None, dict]:
        """(hit, png_or_None_for_no_text, meta)."""
        png, meta = self._paths(key)
        if not meta.exists():
            return False, None, {}
        try:
            info = json.loads(meta.read_text("utf-8"))
            if info.get("empty"):
                return True, None, info
            data = png.read_bytes()
            meta.touch()
            return True, data, info
        except Exception:
            return False, None, {}

    def put(self, key: str, png: bytes | None, meta: dict) -> None:
        if self.max_bytes == 0:
            return
        png_path, meta_path = self._paths(key)
        try:
            png_path.parent.mkdir(parents=True, exist_ok=True)
            if png is not None:
                png_path.write_bytes(png)
            meta_path.write_text(json.dumps({**meta, "empty": png is None}), "utf-8")
        except OSError as e:
            log.warning("Could not write cache entry: %s", e)
            return
        with self._lock:
            self._writes += 1
            if self._writes % 25 == 0:
                self.prune()

    def prune(self) -> None:
        """Delete least-recently-used entries until under the size limit."""
        if not self.dir.is_dir():
            return
        entries = []
        total = 0
        for meta in self.dir.glob("*/*.json"):
            png = meta.with_suffix(".png")
            size = meta.stat().st_size + (png.stat().st_size if png.exists() else 0)
            total += size
            entries.append((meta.stat().st_mtime, size, meta, png))
        if total <= self.max_bytes:
            return
        entries.sort()
        for _mtime, size, meta, png in entries:
            meta.unlink(missing_ok=True)
            png.unlink(missing_ok=True)
            total -= size
            if total <= self.max_bytes * 0.9:
                break
        log.info("Cache pruned to %.1f MB", total / 1e6)

    def clear(self) -> int:
        n = 0
        if self.dir.is_dir():
            for f in self.dir.glob("*/*"):
                f.unlink(missing_ok=True)
                n += 1
        return n

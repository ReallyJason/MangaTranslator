"""Text detection (EasyOCR's CRAFT detector) and grouping of boxes into blocks."""

from __future__ import annotations

import logging
from typing import Callable, Iterable

import numpy as np

from .models import Block, Rect

log = logging.getLogger("fukidashi.detect")

# Detector input is resized so its long side is at most CANVAS; a 800x20000
# webtoon strip would shrink to ~100px wide, so tall images are tiled first.
CANVAS = 2560


def tile_spans(height: int, width: int) -> list[tuple[int, int, int, int]]:
    """Vertical tiles for tall images.

    Returns (start, end, core_start, core_end) per tile. A box is kept from a
    tile only if its vertical center lies inside the tile's core, so boxes in
    overlapping regions are reported once, by the tile that sees them whole.
    """
    tile_h = int(min(CANVAS, max(1024, width * 1.6)))
    if height <= tile_h * 1.15:
        return [(0, height, 0, height)]
    overlap = max(256, tile_h // 4)
    step = tile_h - overlap
    starts = list(range(0, height - tile_h, step)) + [height - tile_h]
    spans = [[s, s + tile_h, s, s + tile_h] for s in starts]
    spans[0][2] = 0
    spans[-1][3] = height
    # Cores meet in the middle of each overlap, so together they cover the image once.
    for cur, nxt in zip(spans, spans[1:]):
        mid = (nxt[0] + cur[1]) // 2
        cur[3] = mid
        nxt[2] = mid
    return [tuple(s) for s in spans]


def _to_rect(item) -> Rect | None:
    """EasyOCR returns [x_min, x_max, y_min, y_max] or 4 corner points."""
    try:
        arr = np.asarray(item, dtype=float)
    except Exception:
        return None
    if arr.shape == (4,):
        x0, x1, y0, y1 = arr
    elif arr.ndim == 2 and arr.shape[1] == 2:
        x0, y0 = arr.min(axis=0)
        x1, y1 = arr.max(axis=0)
    else:
        return None
    return (int(x0), int(y0), int(np.ceil(x1)), int(np.ceil(y1)))


def _flatten(result) -> list:
    """Normalise reader.detect output across EasyOCR versions (batched or not)."""
    if result is None:
        return []
    if len(result) and isinstance(result[0], (list, tuple)) and result[0] and _to_rect(result[0]) is None:
        # Batched: one list per image; we only send one image.
        return list(result[0])
    return list(result)


def detect_boxes(reader, image_rgb: np.ndarray) -> list[Rect]:
    """Run EasyOCR detection, tiling tall strips. Returns axis-aligned boxes."""
    h, w = image_rgb.shape[:2]
    mag = 1.5 if w < 700 else 1.0
    boxes: list[Rect] = []
    for start, end, core_s, core_e in tile_spans(h, w):
        tile = np.ascontiguousarray(image_rgb[start:end])
        horizontal, free = reader.detect(
            tile,
            text_threshold=0.6,
            low_text=0.35,
            link_threshold=0.4,
            canvas_size=CANVAS,
            mag_ratio=mag,
            add_margin=0.08,
            min_size=8,
        )
        for item in _flatten(horizontal) + _flatten(free):
            r = _to_rect(item)
            if r is None:
                continue
            x0, y0, x1, y1 = r
            cy = (y0 + y1) / 2
            if not (core_s - start <= cy < core_e - start):
                continue
            x0, x1 = max(0, x0), min(w, x1)
            y0, y1 = max(0, y0 + start), min(h, y1 + start)
            if x1 - x0 >= 3 and y1 - y0 >= 3:
                boxes.append((x0, y0, x1, y1))
    return boxes


class _UnionFind:
    def __init__(self, n: int):
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, a: int, b: int) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def merge_boxes(boxes: Iterable[Rect], gap_factor: float = 0.6) -> list[Block]:
    """Union boxes whose gap (in both axes) is within gap_factor x median box size."""
    boxes = [tuple(map(int, b)) for b in boxes]
    if not boxes:
        return []
    sizes = sorted(min(b[2] - b[0], b[3] - b[1]) for b in boxes)
    median = sizes[len(sizes) // 2]
    gap = max(2.0, gap_factor * median)

    arr = np.array(boxes, dtype=float)
    uf = _UnionFind(len(boxes))
    for i in range(len(boxes)):
        # Vectorised gap test of box i against all later boxes.
        rest = arr[i + 1 :]
        if not len(rest):
            break
        dx = np.maximum(0, np.maximum(arr[i, 0], rest[:, 0]) - np.minimum(arr[i, 2], rest[:, 2]))
        dy = np.maximum(0, np.maximum(arr[i, 1], rest[:, 1]) - np.minimum(arr[i, 3], rest[:, 3]))
        for j in np.nonzero((dx <= gap) & (dy <= gap))[0]:
            uf.union(i, i + 1 + int(j))

    groups: dict[int, list[Rect]] = {}
    for i, b in enumerate(boxes):
        groups.setdefault(uf.find(i), []).append(b)

    blocks = []
    for members in groups.values():
        xs0, ys0, xs1, ys1 = zip(*members)
        blocks.append(Block(min(xs0), min(ys0), max(xs1), max(ys1), boxes=list(members)))
    return blocks


def reading_order(blocks: list[Block], right_to_left: bool) -> list[Block]:
    """Sort blocks into rows (top to bottom), each row right-to-left or left-to-right."""
    remaining = sorted(blocks, key=lambda b: b.y0)
    rows: list[list[Block]] = []
    for b in remaining:
        cy = (b.y0 + b.y1) / 2
        for row in rows:
            ry0 = min(r.y0 for r in row)
            ry1 = max(r.y1 for r in row)
            # Same row if the block's center falls within the row's vertical span
            # and the row isn't far taller than the block (avoids chaining).
            if ry0 <= cy <= ry1 and b.y0 < ry0 + (ry1 - ry0) * 0.6:
                row.append(b)
                break
        else:
            rows.append([b])
    ordered = []
    for row in rows:
        row.sort(key=lambda b: -(b.x0 + b.x1) if right_to_left else (b.x0 + b.x1))
        ordered.extend(row)
    return ordered


def detect_blocks(
    reader,
    image_rgb: np.ndarray,
    box_fn: Callable[[object, np.ndarray], list[Rect]] = detect_boxes,
) -> list[Block]:
    boxes = box_fn(reader, image_rgb)
    blocks = merge_boxes(boxes)
    # Drop specks: blocks far smaller than a typical glyph are almost always noise.
    if blocks:
        sizes = sorted(b.char_size for b in blocks)
        med = sizes[len(sizes) // 2]
        blocks = [b for b in blocks if max(b.w, b.h) >= max(6, 0.35 * med)]
    log.debug("Detected %d boxes -> %d blocks", len(boxes), len(blocks))
    return blocks

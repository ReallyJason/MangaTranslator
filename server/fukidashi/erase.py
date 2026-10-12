"""Remove the original text from each block.

Flat background (speech bubble): only the text strokes are repainted with the
sampled bubble colour. Busy background: the block is inpainted with LaMa
(TorchScript big-lama.pt), or OpenCV TELEA if torch/LaMa are unavailable.
"""

from __future__ import annotations

import logging
import threading
import warnings
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from . import optional
from .models import Block, Rect

log = logging.getLogger("fukidashi.erase")

FLAT_COLOR_DIST = 40.0  # RGB distance for "same as background"
FLAT_FRACTION = 0.82  # share of border pixels that must match for a flat background


@dataclass
class EraseInfo:
    """What the typesetter needs to know about the area after erasing."""

    flat: bool
    bg: tuple[int, int, int]  # background colour (RGB)
    region: Rect  # area available for text (bubble interior or the padded block)
    bubble: bool = False  # region is a detected bubble interior


def _clip(r: Rect, w: int, h: int) -> Rect:
    return (max(0, r[0]), max(0, r[1]), min(w, r[2]), min(h, r[3]))


def _pad(block: Block) -> int:
    return int(max(3, round(block.char_size * 0.2)))


def sample_background(image: np.ndarray, rect: Rect, ring: int) -> tuple[bool, np.ndarray]:
    """Median colour of a ring around `rect`, and whether the ring is (nearly) uniform."""
    h, w = image.shape[:2]
    x0, y0, x1, y1 = rect
    ox0, oy0, ox1, oy1 = _clip((x0 - ring, y0 - ring, x1 + ring, y1 + ring), w, h)
    outer = image[oy0:oy1, ox0:ox1].reshape(-1, 3)
    mask = np.ones((oy1 - oy0, ox1 - ox0), bool)
    ix0, iy0, ix1, iy1 = _clip(rect, w, h)
    mask[iy0 - oy0 : iy1 - oy0, ix0 - ox0 : ix1 - ox0] = False
    pixels = outer[mask.reshape(-1)].astype(np.float32)
    if len(pixels) < 8:
        pixels = image[iy0:iy1, ix0:ix1].reshape(-1, 3).astype(np.float32)
    bg = np.median(pixels, axis=0)
    dist = np.linalg.norm(pixels - bg, axis=1)
    return bool((dist < FLAT_COLOR_DIST).mean() >= FLAT_FRACTION), bg


def _bubble_region(image: np.ndarray, block: Block, bg: np.ndarray) -> tuple[Rect, bool]:
    """Bounding box of the bg-coloured area connected to the (erased) block.

    Used to give the typesetter the whole bubble interior. Returns the padded
    block rect (and False) if the flood leaks (broken outline / page background).
    """
    h, w = image.shape[:2]
    pad = _pad(block)
    fallback = _clip((block.x0 - pad, block.y0 - pad, block.x1 + pad, block.y1 + pad), w, h)
    span = int(max(block.w, block.h) * 1.5 + 2 * block.char_size)
    win = _clip((block.x0 - span, block.y0 - span, block.x1 + span, block.y1 + span), w, h)
    crop = image[win[1] : win[3], win[0] : win[2]].astype(np.float32)
    similar = (np.linalg.norm(crop - bg, axis=2) < FLAT_COLOR_DIST).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(similar, connectivity=4)
    cx, cy = (block.x0 + block.x1) // 2 - win[0], (block.y0 + block.y1) // 2 - win[1]
    label = labels[min(cy, labels.shape[0] - 1), min(cx, labels.shape[1] - 1)]
    if label == 0:
        return fallback, False
    x, y, bw, bh, _area = stats[label]
    # Reaching the window (or image) edge means the flood escaped: no closed bubble.
    touches = x == 0 or y == 0 or x + bw >= crop.shape[1] or y + bh >= crop.shape[0]
    if touches or bw * bh > 25 * max(1, block.w * block.h):
        return fallback, False
    # Inset so text stays clear of the outline (an ellipse's inscribed box is ~70%).
    ix, iy = int(bw * 0.12), int(bh * 0.12)
    region = (win[0] + x + ix, win[1] + y + iy, win[0] + x + bw - ix, win[1] + y + bh - iy)
    if region[2] - region[0] < block.w * 0.8 or region[3] - region[1] < block.h * 0.8:
        return fallback, False
    return region, True


def _flat_fill(image: np.ndarray, block: Block, bg: np.ndarray) -> None:
    """Repaint text strokes (non-bg components that live inside the block) with bg."""
    h, w = image.shape[:2]
    pad = _pad(block)
    inner = _clip((block.x0 - pad, block.y0 - pad, block.x1 + pad, block.y1 + pad), w, h)
    ring = pad * 2
    outer = _clip((inner[0] - ring, inner[1] - ring, inner[2] + ring, inner[3] + ring), w, h)
    crop = image[outer[1] : outer[3], outer[0] : outer[2]]
    dist = np.linalg.norm(crop.astype(np.float32) - bg, axis=2)
    diff = (dist > FLAT_COLOR_DIST).astype(np.uint8)

    inside = np.zeros_like(diff)
    inside[inner[1] - outer[1] : inner[3] - outer[1], inner[0] - outer[0] : inner[2] - outer[0]] = 1

    n, labels, stats, _ = cv2.connectedComponentsWithStats(diff, connectivity=8)
    if n <= 1:
        return
    in_count = np.bincount(labels[inside.astype(bool)], minlength=n)
    total = stats[:, cv2.CC_STAT_AREA]
    # Keep components mostly inside the block: text. Skip ones that extend far
    # outside (bubble outlines, panel borders, neighbouring art).
    keep = (in_count >= 0.85 * total) & (np.arange(n) != 0)
    mask = keep[labels].astype(np.uint8)
    if not mask.any():
        return
    k = max(1, int(round(block.char_size * 0.06)))
    strokes = cv2.dilate(mask, np.ones((2 * k + 1, 2 * k + 1), np.uint8))
    # JPEG ringing leaves faint halos around glyphs that the threshold above
    # misses; near the strokes, also take any pixel that is off-background,
    # but stay clear of the components we kept (outlines, art).
    r = max(3, int(round(block.char_size * 0.25)))
    near = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))
    other = ((diff > 0) & ~keep[labels]).astype(np.uint8)
    other = cv2.dilate(other, np.ones((3, 3), np.uint8))
    halo = (near > 0) & (dist > 10) & (other == 0)
    crop[(strokes > 0) | halo] = bg.astype(np.uint8)


class LamaInpainter:
    """TorchScript LaMa, downloaded to server/models/ on first use."""

    MAX_SIDE = 1536

    def __init__(self, models_dir: Path, url: str, gpu: bool):
        self.path = Path(models_dir) / "big-lama.pt"
        self.url = url
        self.gpu = gpu
        self.model = None
        self.device = None
        self.failed = ""
        self._lock = threading.Lock()

    @property
    def ready(self) -> bool:
        return self.model is not None

    def _download(self) -> None:
        import httpx

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".part")
        log.info("Downloading LaMa model (~200 MB) from %s", self.url)
        with httpx.stream("GET", self.url, follow_redirects=True, timeout=httpx.Timeout(30, read=300)) as r:
            r.raise_for_status()
            total = int(r.headers.get("content-length", 0))
            done, last = 0, 0
            with open(tmp, "wb") as f:
                for chunk in r.iter_bytes(1 << 20):
                    f.write(chunk)
                    done += len(chunk)
                    if total and done * 10 // total > last:
                        last = done * 10 // total
                        log.info("LaMa download %d%%", last * 10)
        tmp.replace(self.path)
        log.info("Saved LaMa model to %s", self.path)

    def load(self) -> bool:
        if self.model is not None:
            return True
        if self.failed:
            return False
        torch = optional.get("torch")
        if torch is None:
            self.failed = "torch not installed"
            return False
        with self._lock:
            if self.model is not None:
                return True
            try:
                if not self.path.exists():
                    self._download()
                self.device = torch.device("cuda" if self.gpu else "cpu")
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore")  # torch.jit deprecation notice
                    model = torch.jit.load(str(self.path), map_location=self.device)
                model.eval()
                self.model = model
                log.info("LaMa loaded on %s", self.device)
                return True
            except Exception as e:
                self.failed = f"{type(e).__name__}: {e}"
                log.warning("LaMa unavailable (%s); using OpenCV inpainting", self.failed)
                return False

    def inpaint(self, crop_rgb: np.ndarray, mask: np.ndarray) -> np.ndarray:
        torch = optional.get("torch")
        h, w = crop_rgb.shape[:2]
        scale = min(1.0, self.MAX_SIDE / max(h, w))
        img, m = crop_rgb, mask
        if scale < 1.0:
            size = (max(8, int(w * scale)), max(8, int(h * scale)))
            img = cv2.resize(crop_rgb, size, interpolation=cv2.INTER_AREA)
            m = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
        ph, pw = img.shape[:2]
        pad_h, pad_w = (-ph) % 8, (-pw) % 8
        img_p = np.pad(img, ((0, pad_h), (0, pad_w), (0, 0)), mode="reflect" if min(ph, pw) > 1 else "edge")
        m_p = np.pad(m, ((0, pad_h), (0, pad_w)), mode="constant")
        with torch.inference_mode():
            t_img = torch.from_numpy(img_p).permute(2, 0, 1).unsqueeze(0).float().div(255).to(self.device)
            t_mask = torch.from_numpy((m_p > 0).astype(np.float32)).unsqueeze(0).unsqueeze(0).to(self.device)
            out = self.model(t_img, t_mask)
            out = out[0].permute(1, 2, 0).clamp(0, 1).mul(255).byte().cpu().numpy()
        out = out[:ph, :pw]
        if scale < 1.0:
            out = cv2.resize(out, (w, h), interpolation=cv2.INTER_CUBIC)
        return out


def _opencv_inpaint(crop_rgb: np.ndarray, mask: np.ndarray, radius: int) -> np.ndarray:
    bgr = cv2.cvtColor(crop_rgb, cv2.COLOR_RGB2BGR)
    out = cv2.inpaint(bgr, mask, radius, cv2.INPAINT_TELEA)
    return cv2.cvtColor(out, cv2.COLOR_BGR2RGB)


def _inpaint_block(image: np.ndarray, block: Block, lama: LamaInpainter | None) -> None:
    h, w = image.shape[:2]
    pad = _pad(block)
    inner = _clip((block.x0 - pad, block.y0 - pad, block.x1 + pad, block.y1 + pad), w, h)
    ctx = int(max(64, 0.75 * max(block.w, block.h)))
    outer = _clip((inner[0] - ctx, inner[1] - ctx, inner[2] + ctx, inner[3] + ctx), w, h)
    crop = image[outer[1] : outer[3], outer[0] : outer[2]].copy()
    mask = np.zeros(crop.shape[:2], np.uint8)
    mask[inner[1] - outer[1] : inner[3] - outer[1], inner[0] - outer[0] : inner[2] - outer[0]] = 255

    result = None
    if lama is not None and lama.load():
        try:
            result = lama.inpaint(crop, mask)
        except Exception as e:
            log.warning("LaMa inpaint failed on block %s (%s); using OpenCV", block.rect, e)
    if result is None:
        result = _opencv_inpaint(crop, mask, radius=max(3, pad))
    sel = mask.astype(bool)
    image[outer[1] : outer[3], outer[0] : outer[2]][sel] = result[sel]


def erase_blocks(image: np.ndarray, blocks: list[Block], lama: LamaInpainter | None = None) -> list[EraseInfo]:
    """Erase text in-place on an RGB uint8 image. Returns per-block info for typesetting."""
    infos = []
    h, w = image.shape[:2]
    for b in blocks:
        pad = _pad(b)
        padded = _clip((b.x0 - pad, b.y0 - pad, b.x1 + pad, b.y1 + pad), w, h)
        flat, bg = sample_background(image, padded, ring=max(3, pad))
        if flat:
            _flat_fill(image, b, bg)
            region, bubble = _bubble_region(image, b, bg)
        else:
            bubble = False
            _inpaint_block(image, b, lama)
            x0, y0, x1, y1 = padded
            bg = np.median(image[y0:y1, x0:x1].reshape(-1, 3), axis=0)
            region = padded
        infos.append(EraseInfo(flat=flat, bg=tuple(int(c) for c in bg), region=region, bubble=bubble))
    return infos

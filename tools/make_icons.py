"""Generate the Fukidashi toolbar icons (extension/icons/icon{16,32,48,128}.png).

A white speech bubble with three text lines on a warm orange rounded square.
Drawn at 8x and downsampled for clean anti-aliasing. Usage: python tools/make_icons.py
"""

from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parent.parent / "extension" / "icons"
ORANGE = (232, 100, 60, 255)
INK = (40, 32, 44, 255)
WHITE = (255, 255, 255, 255)


def draw(size: int) -> Image.Image:
    s = 8
    S = size * s
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((0, 0, S - 1, S - 1), radius=int(S * 0.22), fill=ORANGE)

    # Bubble body + tail.
    bx0, by0, bx1, by1 = S * 0.14, S * 0.17, S * 0.86, S * 0.70
    d.ellipse((bx0, by0, bx1, by1), fill=WHITE)
    d.polygon([(S * 0.30, S * 0.60), (S * 0.46, S * 0.66), (S * 0.22, S * 0.86)], fill=WHITE)

    # Text lines (fewer at tiny sizes so they stay readable).
    lines = [(0.33, 0.67), (0.27, 0.73), (0.36, 0.60)] if size >= 32 else [(0.30, 0.70), (0.34, 0.62)]
    lh = S * (0.055 if size >= 32 else 0.075)
    top = S * (0.32 if size >= 32 else 0.35)
    gap = S * (0.11 if size >= 32 else 0.14)
    for i, (a, b) in enumerate(lines):
        y = top + i * gap
        d.rounded_rectangle((S * a, y, S * b, y + lh), radius=lh / 2, fill=INK)
    return img.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for size in (16, 32, 48, 128):
        draw(size).save(OUT / f"icon{size}.png")
        print("wrote", OUT / f"icon{size}.png")

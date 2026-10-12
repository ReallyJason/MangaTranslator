"""Local test site for the Fukidashi extension.

    python tools/test_page/serve.py        (use the server's venv: needs Pillow + numpy)

Serves two origins so hotlink protection is realistic:
  http://localhost:8080/        the "manga reader" pages (index.html, csp.html)
  http://127.0.0.1:8081/img/    the "CDN": images, 403 unless the Referer is the reader
                                (/img/open/... is served without the check)

The page images are synthetic manga pages generated on startup.
"""

from __future__ import annotations

import io
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent.parent
sys.path.insert(0, str(ROOT / "server"))
sys.path.insert(0, str(ROOT / "server" / "tests"))

from synthetic import make_page  # noqa: E402

PAGE_PORT, CDN_PORT = 8080, 8081
READER_ORIGIN = f"http://localhost:{PAGE_PORT}"

IMAGES: dict[str, tuple[bytes, str]] = {}
REFERERS: list[tuple[str, str]] = []  # (path, referer) seen by the CDN, for the e2e check


def _jpeg(img: Image.Image, quality: int = 90) -> bytes:
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=quality)
    return buf.getvalue()


def build_images() -> None:
    base = make_page().image
    for n in range(1, 9):
        page = base.copy()
        d = ImageDraw.Draw(page)
        d.rectangle((830, 1240, 890, 1290), fill=(255, 255, 255))
        d.text((842, 1252), f"p{n}", fill=(0, 0, 0))
        IMAGES[f"page{n}.jpg"] = (_jpeg(page), "image/jpeg")
    # Webtoon strip: three pages stacked.
    strip = Image.new("RGB", (900, 4100), "white")
    for i in range(3):
        strip.paste(base, (0, i * 1400))
    IMAGES["strip.jpg"] = (_jpeg(strip, 85), "image/jpeg")
    icon = Image.new("RGB", (48, 48), (230, 100, 60))
    IMAGES["icon.jpg"] = (_jpeg(icon), "image/jpeg")
    IMAGES["placeholder.gif"] = (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\xff\xff\x00\x00\x00!\xf9\x04\x01\x00\x00\x00\x00,"
        b"\x00\x00\x00\x00\x01\x00\x01\x00\x00\x02\x02D\x01\x00;",
        "image/gif",
    )


class PageHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        name = {"/": "index.html", "/index.html": "index.html", "/csp.html": "csp.html"}.get(path)
        if not name:
            self.send_error(404)
            return
        body = (HERE / name).read_bytes()
        self.send_response(200)
        self.send_header("content-type", "text/html; charset=utf-8")
        self.send_header("content-length", str(len(body)))
        if name == "csp.html":
            # Like some readers: images allowed from anywhere, but not from blob: URLs.
            self.send_header("content-security-policy", "img-src * data:")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


class CdnHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/referers":
            body = "\n".join(f"{p} {r}" for p, r in REFERERS).encode()
            self.send_response(200)
            self.send_header("content-type", "text/plain")
            self.end_headers()
            self.wfile.write(body)
            return
        protected = not path.startswith("/img/open/")
        name = path.rsplit("/", 1)[-1]
        if not path.startswith("/img/") or name not in IMAGES:
            self.send_error(404)
            return
        referer = self.headers.get("referer", "")
        REFERERS.append((path, referer))
        if protected and not referer.startswith(READER_ORIGIN):
            self.send_error(403, "Hotlinking not allowed")
            return
        body, ctype = IMAGES[name]
        self.send_response(200)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(body)))
        self.send_header("cache-control", "no-store")
        self.send_header("access-control-allow-origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def main() -> None:
    build_images()
    servers = [ThreadingHTTPServer(("127.0.0.1", PAGE_PORT), PageHandler), ThreadingHTTPServer(("127.0.0.1", CDN_PORT), CdnHandler)]
    for s in servers[1:]:
        threading.Thread(target=s.serve_forever, daemon=True).start()
    print(f"Reader: {READER_ORIGIN}/   CDN: http://127.0.0.1:{CDN_PORT}/img/ (hotlink-protected)")
    servers[0].serve_forever()


if __name__ == "__main__":
    main()

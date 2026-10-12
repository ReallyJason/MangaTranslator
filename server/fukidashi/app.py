"""FastAPI app: GET /health, POST /translate, POST /context/reset (+ small extras)."""

from __future__ import annotations

import json
import logging
import re
from contextlib import asynccontextmanager

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from PIL import UnidentifiedImageError

from . import __version__, fonts, optional
from .config import Settings, load_settings
from .models import TranslateOptions
from .pipeline import Pipeline, header_safe
from .series import series_key
from .translate import EngineError

log = logging.getLogger("fukidashi")

EXPOSED_HEADERS = [
    "X-Fukidashi-Mode",
    "X-Fukidashi-Engine",
    "X-Fukidashi-Note",
    "X-Fukidashi-Blocks",
    "X-Fukidashi-Cache",
    "X-Fukidashi-Time",
    "X-Fukidashi-Error",
]
EXTENSION_ORIGIN = re.compile(r"^(chrome|moz)-extension://[A-Za-z0-9._-]+$")
LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}


def create_app(settings: Settings | None = None, pipeline: Pipeline | None = None) -> FastAPI:
    settings = settings or load_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if app.state.pipeline is None:
            optional.probe_all()
            app.state.pipeline = Pipeline(settings)
        p: Pipeline = app.state.pipeline
        configured, why = settings.llm_status()
        log.info(
            "Fukidashi %s ready on http://%s:%s | GPU: %s | LLM: %s | engines: %s",
            __version__, settings.host, settings.port,
            p.device_name if p.gpu else f"no ({p.device_name})",
            f"{settings.llm_provider}/{settings.effective_model}" if configured else f"off ({why})",
            ", ".join(k for k, v in p.translator.engines().items() if v) or "none",
        )
        yield

    app = FastAPI(title="Fukidashi", version=__version__, lifespan=lifespan)
    app.state.settings = settings
    app.state.pipeline = pipeline

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=EXTENSION_ORIGIN.pattern,
        allow_methods=["GET", "POST"],
        allow_headers=["*"],
        expose_headers=EXPOSED_HEADERS,
    )

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # Block DNS-rebinding (Host) and web pages (Origin) from driving the API;
        # the extension's service worker sends a chrome-extension:// origin.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0].lower()
        if host and host not in LOCAL_HOSTS and settings.host in ("127.0.0.1", "localhost"):
            return JSONResponse({"error": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if request.method == "POST" and origin and not EXTENSION_ORIGIN.match(origin):
            return JSONResponse({"error": "requests from web pages are not allowed"}, status_code=403)
        return await call_next(request)

    def pipe() -> Pipeline:
        return app.state.pipeline

    @app.get("/health")
    def health():
        p = pipe()
        configured, why = settings.llm_status()
        lama = p.lama
        return {
            "ok": True,
            "name": "Fukidashi",
            "version": __version__,
            "gpu": p.gpu,
            "device": p.device_name,
            "ocr": {"easyocr": optional.available("easyocr"), "manga_ocr": optional.available("manga_ocr") and settings.use_manga_ocr and not p.ocr.manga_ocr_failed},
            "inpainter": "lama" if lama is not None and not lama.failed and optional.available("torch") else "opencv",
            "lama": {
                "enabled": lama is not None,
                "downloaded": bool(lama and lama.path.exists()),
                "loaded": bool(lama and lama.ready),
                "error": lama.failed if lama else "",
            },
            "engines": p.translator.engines(),
            "llm": {
                "provider": settings.llm_provider,
                "model": settings.effective_model,
                "configured": configured,
                "reason": why,
            },
            "fonts": fonts.font_names(settings.fonts_dir),
        }

    def error(status: int, msg: str) -> JSONResponse:
        return JSONResponse({"error": msg}, status_code=status, headers={"X-Fukidashi-Error": header_safe(msg)})

    @app.post("/translate")
    async def translate(image: UploadFile = File(...), options: str = Form("{}")):
        data = await image.read()
        if not data:
            return error(400, "empty image upload")
        try:
            opts = TranslateOptions.from_dict(json.loads(options or "{}"))
        except (ValueError, TypeError) as e:
            return error(400, f"invalid options: {e}")
        try:
            result = await run_in_threadpool(pipe().process, data, opts)
        except UnidentifiedImageError:
            return error(415, "not a supported image format")
        except EngineError as e:
            return error(503, str(e))
        except RuntimeError as e:  # e.g. EasyOCR missing
            log.error("%s", e)
            return error(503, str(e))
        except Exception as e:
            log.exception("Translation failed")
            return error(500, f"{type(e).__name__}: {e}")
        if result.png is None:
            return Response(status_code=204, headers=result.headers)
        return Response(result.png, media_type="image/png", headers=result.headers)

    @app.post("/context/reset")
    async def context_reset(request: Request):
        try:
            body = await request.json()
        except Exception:
            body = {}
        store = pipe().store
        if body.get("all"):
            return {"ok": True, "cleared": store.reset_all()}
        key = body.get("series") or series_key(body.get("session", ""))
        if not key:
            return error(400, "pass 'session' (page URL), 'series', or 'all': true")
        return {"ok": True, "series": key, "existed": store.reset(key)}

    @app.get("/context")
    def context_get(session: str = "", series: str = ""):
        key = series or series_key(session)
        ctx = pipe().store.load(key)
        return {"series": key, "summary": ctx.summary, "glossary": ctx.glossary, "pages": ctx.pages}

    @app.post("/cache/clear")
    def cache_clear():
        return {"ok": True, "deleted_files": pipe().cache.clear()}

    return app


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S")
app = create_app()

"""HTTP API and translation dispatch, with detection and remote services mocked."""

import io
import json

import numpy as np
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from fukidashi.app import create_app
from fukidashi.config import Settings
from fukidashi.models import TranslateOptions
from fukidashi.pipeline import Pipeline
from fukidashi.translate import Translator, deepl
from fukidashi.translate.llm import LLMError, LLMResult
from synthetic import make_page

EXT = {"Origin": "chrome-extension://abcdefghijklmnopabcdefghijklmnop"}


@pytest.fixture
def settings(tmp_path):
    return Settings(
        llm_provider="anthropic",
        anthropic_api_key="sk-test",
        deepl_api_key="dk-test:fx",
        inpainter="opencv",
        data_dir=tmp_path / "data",
    )


@pytest.fixture
def fake_deepl(monkeypatch):
    calls = []

    def translate(key, texts, source, target, client):
        calls.append(list(texts))
        return [f"[deepl] {t}" for t in texts]

    monkeypatch.setattr(deepl, "translate", translate)
    return calls


class FakeLLM:
    def __init__(self, fail=None, drop_last=False):
        self.fail = fail
        self.drop_last = drop_last
        self.calls = []

    def __call__(self, s, texts, source, target, summary, glossary):
        self.calls.append({"texts": texts, "summary": summary, "glossary": dict(glossary)})
        if self.fail:
            raise LLMError(self.fail)
        out = [f"[llm] {t}" for t in texts]
        if self.drop_last:
            out[-1] = None
        n = len(self.calls)
        return LLMResult(out, summary=f"summary after page {n}", glossary={f"名前{n}": f"Name{n}"})


def _page_bytes():
    page = make_page()
    buf = io.BytesIO()
    page.image.save(buf, format="PNG")
    return buf.getvalue(), page


@pytest.fixture
def client(settings, fake_deepl):
    pipe = Pipeline(settings)
    data, page = _page_bytes()

    def detect(image, opts):
        if image.mean() > 250:  # blank image: no text
            return []
        blocks = make_page().blocks
        for b, t in zip(blocks, ["おい待てよ", "関係ないだろ", "その日", "負けない", "研究所だ"]):
            b.text = t
        return blocks

    pipe.detect_fn = detect
    pipe.translator.llm_fn = FakeLLM()
    app = create_app(settings, pipe)
    with TestClient(app, base_url="http://127.0.0.1:5003") as c:
        c.page_bytes = data
        c.pipe = pipe
        yield c


def _post(client, data, **opts):
    return client.post(
        "/translate",
        files={"image": ("page.png", data, "image/png")},
        data={"options": json.dumps(opts)},
        headers=EXT,
    )


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    h = r.json()
    assert h["ok"] and h["name"] == "Fukidashi"
    assert h["engines"]["deepl"] is True and h["engines"]["google"] is False
    assert h["llm"] == {"provider": "anthropic", "model": "claude-haiku-5-5", "configured": True, "reason": ""}
    assert isinstance(h["gpu"], bool)
    assert h["inpainter"] == "opencv"


def test_translate_context_mode_returns_png_and_caches(client):
    r = _post(client, client.page_bytes, mode="context", session="https://site.com/manga/foo/ch-1")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "image/png"
    assert r.headers["x-fukidashi-mode"] == "context"
    assert r.headers["x-fukidashi-engine"] == "llm:anthropic/claude-haiku-5-5"
    assert r.headers["x-fukidashi-cache"] == "miss"
    assert r.headers["x-fukidashi-blocks"] == "5"
    assert "x-fukidashi-note" not in r.headers
    img = Image.open(io.BytesIO(r.content))
    assert img.size == (900, 1300)

    # Same image + options from another page of the series: served from cache.
    r2 = _post(client, client.page_bytes, mode="context", session="https://site.com/manga/foo/ch-2")
    assert r2.headers["x-fukidashi-cache"] == "hit"
    assert r2.content == r.content
    assert len(client.pipe.translator.llm_fn.calls) == 1


def test_story_memory_is_passed_between_pages_and_reset(client):
    llm = client.pipe.translator.llm_fn
    _post(client, client.page_bytes, mode="context", session="https://site.com/manga/foo/ch-1")
    _post(client, client.page_bytes, mode="context", target_lang="fr", session="https://site.com/manga/foo/ch-2")
    assert llm.calls[0]["summary"] == ""
    assert llm.calls[1]["summary"] == "summary after page 1"
    assert llm.calls[1]["glossary"] == {"名前1": "Name1"}

    ctx = client.get("/context", params={"session": "https://site.com/manga/foo/ch-9"}).json()
    assert ctx["series"] == "site.com/manga/foo" and ctx["pages"] == 2

    r = client.post("/context/reset", json={"session": "https://site.com/manga/foo/ch-9"}, headers=EXT)
    assert r.json() == {"ok": True, "series": "site.com/manga/foo", "existed": True}
    _post(client, client.page_bytes, mode="context", target_lang="de", session="https://site.com/manga/foo/ch-3")
    assert llm.calls[2]["summary"] == ""


def test_llm_failure_falls_back_to_fast_with_note(client, fake_deepl):
    client.pipe.translator.llm_fn = FakeLLM(fail="LLM HTTP 529: overloaded")
    r = _post(client, client.page_bytes, mode="context", session="https://s.com/a/b")
    assert r.status_code == 200
    assert r.headers["x-fukidashi-mode"] == "fast"
    assert r.headers["x-fukidashi-engine"] == "deepl"
    assert "Context mode unavailable" in r.headers["x-fukidashi-note"]
    assert "overloaded" in r.headers["x-fukidashi-note"]
    # Degraded results are not cached, so the next request retries the LLM.
    client.pipe.translator.llm_fn = FakeLLM()
    r2 = _post(client, client.page_bytes, mode="context", session="https://s.com/a/b")
    assert r2.headers["x-fukidashi-cache"] == "miss" and r2.headers["x-fukidashi-mode"] == "context"


def test_llm_not_configured_falls_back(settings, fake_deepl, tmp_path):
    settings.anthropic_api_key = ""
    t = Translator(settings, Pipeline(settings).store)
    out = t.translate(["おい"], TranslateOptions(mode="context"))
    assert out.mode == "fast" and out.translations == ["[deepl] おい"]
    assert "ANTHROPIC_API_KEY" in out.notes[0]


def test_missing_llm_entries_filled_by_fast_engine(settings, fake_deepl):
    t = Translator(settings, Pipeline(settings).store)
    t.llm_fn = FakeLLM(drop_last=True)
    out = t.translate(["a", "b"], TranslateOptions(mode="context", session="https://x.com/s/1"))
    assert out.translations == ["[llm] a", "[deepl] b"]
    assert out.mode == "context" and out.degraded
    assert fake_deepl == [["b"]]


def test_fast_mode(client, fake_deepl):
    r = _post(client, client.page_bytes, mode="fast", engine="auto")
    assert r.status_code == 200
    assert r.headers["x-fukidashi-mode"] == "fast" and r.headers["x-fukidashi-engine"] == "deepl"
    assert len(fake_deepl[-1]) == 5


def test_explicit_engine_falls_back_with_note(client):
    r = _post(client, client.page_bytes, mode="fast", engine="google")
    assert r.status_code == 200
    assert r.headers["x-fukidashi-engine"] == "deepl"
    assert "GOOGLE_API_KEY is not set" in r.headers["x-fukidashi-note"]


def test_no_engine_available_is_503(settings, monkeypatch):
    settings.deepl_api_key = ""
    monkeypatch.setattr("fukidashi.translate.offline.available", lambda: False)
    t = Translator(settings, Pipeline(settings).store)
    from fukidashi.translate import EngineError

    with pytest.raises(EngineError, match="No translation engine"):
        t.translate(["a"], TranslateOptions(mode="fast"))


def test_no_text_returns_204(client):
    buf = io.BytesIO()
    Image.new("RGB", (400, 600), "white").save(buf, format="PNG")
    r = _post(client, buf.getvalue(), mode="fast")
    assert r.status_code == 204
    assert r.headers["x-fukidashi-blocks"] == "0"
    r2 = _post(client, buf.getvalue(), mode="fast")
    assert r2.status_code == 204 and r2.headers["x-fukidashi-cache"] == "hit"


def test_bad_requests(client):
    assert _post(client, b"not an image", mode="fast").status_code == 415
    r = client.post("/translate", files={"image": ("x.png", client.page_bytes)}, data={"options": "{bad"}, headers=EXT)
    assert r.status_code == 400 and "invalid options" in r.json()["error"]
    r = _post(client, client.page_bytes, mode="turbo")
    assert r.status_code == 400


def test_web_pages_cannot_call_api(client):
    r = client.post(
        "/translate",
        files={"image": ("p.png", client.page_bytes)},
        data={"options": "{}"},
        headers={"Origin": "https://evil.example"},
    )
    assert r.status_code == 403
    r = client.post("/context/reset", json={"all": True}, headers={"Origin": "null"})
    assert r.status_code == 403


def test_dns_rebinding_host_rejected(client):
    r = client.get("/health", headers={"Host": "attacker.example:5003"})
    assert r.status_code == 403


def test_cors_exposes_headers_to_extension(client):
    r = client.get("/health", headers=EXT)
    assert r.headers["access-control-allow-origin"] == EXT["Origin"]
    assert "X-Fukidashi-Note" in r.headers["access-control-expose-headers"]


def test_transparent_png_keeps_alpha(client):
    img = make_page().image.convert("RGBA")
    a = np.array(img)
    a[:50, :50, 3] = 0
    buf = io.BytesIO()
    Image.fromarray(a).save(buf, format="PNG")
    r = _post(client, buf.getvalue(), mode="fast")
    out = np.array(Image.open(io.BytesIO(r.content)))
    assert out.shape[2] == 4 and out[0, 0, 3] == 0 and out[600, 600, 3] == 255


def test_cache_clear(client):
    _post(client, client.page_bytes, mode="fast")
    r = client.post("/cache/clear", headers=EXT)
    assert r.json()["deleted_files"] >= 2
    assert _post(client, client.page_bytes, mode="fast").headers["x-fukidashi-cache"] == "miss"


def test_fails_fast_without_any_engine(client, monkeypatch):
    pipe = client.pipe
    pipe.s.deepl_api_key = ""
    pipe.s.anthropic_api_key = ""
    monkeypatch.setattr("fukidashi.translate.offline.available", lambda: False)
    called = []
    pipe.detect_fn = lambda image, opts: called.append(1) or []
    r = _post(client, client.page_bytes, mode="context")
    assert r.status_code == 503
    assert "ANTHROPIC_API_KEY" in r.json()["error"] and "DEEPL_API_KEY" in r.json()["error"]
    assert called == []  # no OCR work was done


def test_refresh_bypasses_cache(client):
    _post(client, client.page_bytes, mode="fast")
    assert _post(client, client.page_bytes, mode="fast").headers["x-fukidashi-cache"] == "hit"
    assert _post(client, client.page_bytes, mode="fast", refresh=True).headers["x-fukidashi-cache"] == "miss"


def test_llm_and_fallback_both_failing_reports_both(settings, monkeypatch):
    settings.deepl_api_key = ""
    monkeypatch.setattr("fukidashi.translate.offline.available", lambda: False)
    t = Translator(settings, Pipeline(settings).store)
    t.llm_fn = FakeLLM(fail="cannot reach LLM endpoint")
    from fukidashi.translate import EngineError

    with pytest.raises(EngineError, match="cannot reach LLM endpoint.*No translation engine"):
        t.translate(["a"], TranslateOptions(mode="context"))

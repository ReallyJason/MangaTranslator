"""Google Cloud Translation API v2 (Basic) with an API key."""

from __future__ import annotations

import html

import httpx

from .errors import EngineError

URL = "https://translation.googleapis.com/language/translate/v2"
BATCH = 100


def code(lang: str) -> str:
    l = lang.lower()
    return {"zh": "zh-CN", "zh-cn": "zh-CN", "zh-hans": "zh-CN", "zh-tw": "zh-TW", "zh-hant": "zh-TW"}.get(
        l, l.split("-")[0] if l not in ("pt-pt", "pt-br") else l
    )


def translate(key: str, texts: list[str], source: str, target: str, client: httpx.Client) -> list[str]:
    out: list[str] = []
    for i in range(0, len(texts), BATCH):
        body = {"q": texts[i : i + BATCH], "target": code(target), "format": "text"}
        if source and source.lower() != "auto":
            body["source"] = code(source)
        try:
            r = client.post(URL, params={"key": key}, json=body)
        except httpx.HTTPError as e:
            raise EngineError(f"Google Translate unreachable: {e}") from e
        if r.status_code != 200:
            try:
                msg = r.json()["error"]["message"]
            except Exception:
                msg = r.text[:200]
            raise EngineError(f"Google Translate HTTP {r.status_code}: {msg}")
        out.extend(html.unescape(t["translatedText"]) for t in r.json()["data"]["translations"])
    return out

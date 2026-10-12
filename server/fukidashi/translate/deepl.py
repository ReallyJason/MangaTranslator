"""DeepL REST API. Free keys end in ':fx' and use api-free.deepl.com."""

from __future__ import annotations

import httpx

from .errors import EngineError

BATCH = 50


def endpoint(key: str) -> str:
    host = "api-free.deepl.com" if key.strip().endswith(":fx") else "api.deepl.com"
    return f"https://{host}/v2/translate"


def target_code(lang: str) -> str:
    l = lang.lower()
    special = {
        "en": "EN-US", "en-gb": "EN-GB", "en-us": "EN-US",
        "pt": "PT-BR", "pt-br": "PT-BR", "pt-pt": "PT-PT",
        "zh": "ZH-HANS", "zh-cn": "ZH-HANS", "zh-hans": "ZH-HANS", "zh-tw": "ZH-HANT", "zh-hant": "ZH-HANT",
        "nb": "NB", "no": "NB",
    }
    return special.get(l, l.split("-")[0].upper())


def source_code(lang: str) -> str | None:
    l = (lang or "").lower()
    if not l or l == "auto":
        return None
    return l.split("-")[0].upper()


def translate(key: str, texts: list[str], source: str, target: str, client: httpx.Client) -> list[str]:
    out: list[str] = []
    headers = {"Authorization": f"DeepL-Auth-Key {key}"}
    for i in range(0, len(texts), BATCH):
        body = {"text": texts[i : i + BATCH], "target_lang": target_code(target)}
        src = source_code(source)
        if src:
            body["source_lang"] = src
        try:
            r = client.post(endpoint(key), headers=headers, json=body)
        except httpx.HTTPError as e:
            raise EngineError(f"DeepL unreachable: {e}") from e
        if r.status_code == 403:
            raise EngineError("DeepL rejected the API key (403)")
        if r.status_code == 456:
            raise EngineError("DeepL quota exceeded (456)")
        if r.status_code != 200:
            raise EngineError(f"DeepL HTTP {r.status_code}: {r.text[:200]}")
        out.extend(t["text"] for t in r.json()["translations"])
    return out

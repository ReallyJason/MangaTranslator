"""Context-mode translation with an LLM over plain HTTP (no SDKs).

Providers: Anthropic Messages API, OpenAI Chat Completions, and any
OpenAI-compatible endpoint (Ollama, LM Studio, OpenRouter, ...).
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Optional

import httpx

from ..config import Settings

log = logging.getLogger("fukidashi.llm")

LANG_NAMES = {
    "ja": "Japanese", "zh": "Chinese", "zh-cn": "Simplified Chinese", "zh-tw": "Traditional Chinese",
    "ko": "Korean", "en": "English", "es": "Spanish", "fr": "French", "de": "German", "it": "Italian",
    "pt": "Portuguese", "pt-br": "Brazilian Portuguese", "ru": "Russian", "pl": "Polish", "uk": "Ukrainian",
    "nl": "Dutch", "tr": "Turkish", "vi": "Vietnamese", "th": "Thai", "id": "Indonesian", "ar": "Arabic",
    "sv": "Swedish", "cs": "Czech", "hu": "Hungarian", "ro": "Romanian", "fi": "Finnish", "da": "Danish",
    "nb": "Norwegian", "el": "Greek", "he": "Hebrew", "hi": "Hindi", "ms": "Malay", "tl": "Filipino",
}


def lang_name(code: str) -> str:
    c = (code or "").lower()
    return LANG_NAMES.get(c) or LANG_NAMES.get(c.split("-")[0]) or code


class LLMError(Exception):
    pass


@dataclass
class LLMResult:
    translations: list[Optional[str]]
    summary: str = ""
    glossary: dict[str, str] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Response parsing
# ---------------------------------------------------------------------------

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*(.*?)```", re.S)
_TRAILING_COMMA_RE = re.compile(r",\s*([}\]])")


def _candidates(text: str) -> list[str]:
    """Substrings of the model output that may be the JSON payload, best first."""
    text = _THINK_RE.sub("", text).strip()
    out = []
    for m in _FENCE_RE.finditer(text):
        out.append(m.group(1).strip())
    out.append(text)
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        out.append(text[start : end + 1])
    start, end = text.find("["), text.rfind("]")
    if start != -1 and end > start:
        out.append(text[start : end + 1])
    return out


def _loads_lenient(s: str):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass
    fixed = _TRAILING_COMMA_RE.sub(r"\1", s)
    fixed = fixed.replace("“", '"').replace("”", '"')
    try:
        return json.loads(fixed)
    except json.JSONDecodeError:
        pass
    # Truncated output: close open strings/brackets and try again.
    return json.loads(_close_truncated(fixed))


def _close_truncated(s: str) -> str:
    stack, in_str, esc = [], False, False
    for ch in s:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append("}" if ch == "{" else "]")
        elif ch in "}]" and stack:
            stack.pop()
    out = s + ('"' if in_str else "")
    out = re.sub(r",\s*$", "", out.rstrip())
    out = re.sub(r':\s*$', ': null', out)
    return out + "".join(reversed(stack))


def _extract_translations_array(text: str) -> list | None:
    """Last resort: pull the "translations" array out of otherwise broken JSON."""
    m = re.search(r'"translations"\s*:\s*\[', text)
    if not m:
        return None
    try:
        arr = _loads_lenient(text[m.end() - 1 :].split("]", 1)[0] + "]")
        return arr if isinstance(arr, list) else None
    except Exception:
        # Grab quoted strings between the brackets.
        body = text[m.end() :].split("]", 1)[0]
        items = re.findall(r'"((?:[^"\\]|\\.)*)"', body)
        try:
            return [json.loads(f'"{x}"') for x in items] if items else None
        except Exception:
            return None


def _as_text(item) -> Optional[str]:
    if item is None:
        return None
    if isinstance(item, str):
        return item.strip()
    if isinstance(item, dict):
        for k in ("text", "translation", "translated", "t"):
            if isinstance(item.get(k), str):
                return item[k].strip()
        return None
    return str(item).strip()


def _item_id(item, n: int) -> Optional[int]:
    if isinstance(item, dict):
        for k in ("id", "index", "i", "n"):
            v = item.get(k)
            if isinstance(v, (int, float)) or (isinstance(v, str) and v.strip().isdigit()):
                v = int(v)
                if 1 <= v <= n:
                    return v
    return None


def parse_llm_response(text: str, n: int) -> LLMResult:
    """Parse the model's JSON. Raises LLMError if no translations can be recovered.

    Tolerates code fences, prose around the JSON, <think> blocks, trailing commas,
    truncation, a bare array, objects with ids, and wrong array lengths (missing
    entries become None so the caller can fill them).
    """
    data = None
    for cand in _candidates(text or ""):
        try:
            data = _loads_lenient(cand)
            if isinstance(data, (dict, list)):
                break
        except Exception:
            data = None
    problems: list[str] = []

    if isinstance(data, list):
        data = {"translations": data}
    if not isinstance(data, dict):
        arr = _extract_translations_array(text or "")
        if arr is None:
            raise LLMError("LLM returned no parseable JSON")
        problems.append("LLM JSON was malformed; recovered translations only")
        data = {"translations": arr}

    raw = data.get("translations")
    if raw is None:
        # Some models rename the key.
        for k in ("translation", "lines", "texts", "results", "blocks"):
            if isinstance(data.get(k), list):
                raw = data[k]
                break
    if not isinstance(raw, list):
        raise LLMError("LLM JSON has no 'translations' array")

    out: list[Optional[str]] = [None] * n
    ids = [_item_id(it, n) for it in raw]
    if raw and None not in ids and len(set(ids)) == len(ids):
        # Objects carrying block ids: place by id, which survives skipped/reordered items.
        for idx, it in zip(ids, raw):
            out[idx - 1] = _as_text(it)
    else:
        for i, it in enumerate(raw[:n]):
            out[i] = _as_text(it)
    if len(raw) != n:
        problems.append(f"LLM returned {len(raw)} translations for {n} text blocks")
    # "" = the model judged the block to be noise (skip it); None = missing (caller may fill).
    if all(t is None for t in out):
        raise LLMError("LLM returned no usable translations")

    summary = data.get("summary")
    glossary = data.get("glossary")
    if isinstance(glossary, list):  # [{"source": .., "target": ..}] variants
        g = {}
        for it in glossary:
            if isinstance(it, dict):
                vals = list(it.values())
                if len(vals) >= 2:
                    g[str(vals[0])] = str(vals[1])
        glossary = g
    return LLMResult(
        translations=out,
        summary=summary.strip() if isinstance(summary, str) else "",
        glossary={str(k).strip(): str(v).strip() for k, v in glossary.items() if str(k).strip()}
        if isinstance(glossary, dict)
        else {},
        problems=problems,
    )


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------


def build_prompt(texts: list[str], source: str, target: str, summary: str, glossary: dict[str, str]) -> tuple[str, str]:
    src, tgt = lang_name(source), lang_name(target)
    system = (
        f"You are an expert comics translator localising {src} manga, manhwa and webtoons into natural, "
        f"fluent {tgt}. You receive the OCR'd text blocks of one page in reading order, plus a running "
        "summary of the story so far and a glossary of character and place names.\n"
        "Rules:\n"
        "- Translate every block, in order, as dialogue/narration that fits a speech bubble: concise and natural, "
        "matching each character's tone. Do not add notes or explanations.\n"
        "- OCR can be noisy: infer the intended text from context. If a block is meaningless noise, return an "
        "empty string for it. Sound effects get a short equivalent.\n"
        "- Use the glossary spellings for names consistently.\n"
        "- Reply with ONLY a JSON object, no prose and no code fences, with exactly these keys:\n"
        '  "translations": array of strings, exactly one per input block, same order;\n'
        '  "summary": the updated story summary covering everything so far including this page, at most 120 words;\n'
        '  "glossary": object mapping original names (as written in the source) to their translated spelling, '
        "for characters, places and recurring terms (include existing entries you still use)."
    )
    payload = {
        "story_summary": summary or "(this is the first page)",
        "glossary": glossary or {},
        "block_count": len(texts),
        "blocks": [{"id": i + 1, "text": t} for i, t in enumerate(texts)],
    }
    user = (
        f"Translate these {len(texts)} text blocks from {src} to {tgt}.\n"
        + json.dumps(payload, ensure_ascii=False, indent=1)
        + f'\nReturn JSON: {{"translations": [{len(texts)} strings], "summary": "...", "glossary": {{...}}}}'
    )
    return system, user


# ---------------------------------------------------------------------------
# HTTP providers
# ---------------------------------------------------------------------------


def _err_detail(r: httpx.Response) -> str:
    try:
        j = r.json()
        e = j.get("error", j)
        if isinstance(e, dict):
            return str(e.get("message") or e)[:300]
        return str(e)[:300]
    except Exception:
        return r.text[:300]


def _post(client: httpx.Client, url: str, headers: dict, body: dict) -> dict:
    last = None
    for attempt in range(2):
        try:
            r = client.post(url, headers=headers, json=body)
        except httpx.HTTPError as e:
            last = LLMError(f"cannot reach LLM endpoint {url}: {e}")
        else:
            if r.status_code == 200:
                try:
                    return r.json()
                except ValueError as e:
                    raise LLMError(f"LLM endpoint returned non-JSON: {r.text[:200]}") from e
            last = LLMError(f"LLM HTTP {r.status_code}: {_err_detail(r)}")
            if r.status_code not in (429, 500, 502, 503, 504, 529):
                break
        time.sleep(1.5 * (attempt + 1))
    raise last


def call_anthropic(s: Settings, system: str, user: str, client: httpx.Client) -> str:
    body = {
        "model": s.effective_model,
        "max_tokens": 4096,
        "temperature": 0.3,
        "system": system,
        "messages": [{"role": "user", "content": user}],
    }
    headers = {
        "x-api-key": s.anthropic_api_key,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    data = _post(client, f"{s.anthropic_base_url}/v1/messages", headers, body)
    parts = [c.get("text", "") for c in data.get("content", []) if c.get("type") == "text"]
    if not parts:
        raise LLMError(f"Anthropic returned no text (stop_reason={data.get('stop_reason')})")
    return "".join(parts)


def call_openai(s: Settings, system: str, user: str, client: httpx.Client) -> str:
    body = {
        "model": s.effective_model,
        "temperature": 0.3,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
    }
    if s.llm_provider == "openai" and not s.openai_base_url:
        body["response_format"] = {"type": "json_object"}
    headers = {"content-type": "application/json"}
    if s.openai_api_key:
        headers["authorization"] = f"Bearer {s.openai_api_key}"
    data = _post(client, f"{s.effective_openai_base_url}/chat/completions", headers, body)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as e:
        raise LLMError(f"unexpected chat completion response: {str(data)[:200]}") from e
    if isinstance(content, list):  # some servers return content parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content or ""


def translate_with_llm(
    s: Settings,
    texts: list[str],
    source: str,
    target: str,
    summary: str,
    glossary: dict[str, str],
    client: httpx.Client | None = None,
) -> LLMResult:
    configured, why = s.llm_status()
    if not configured:
        raise LLMError(f"LLM not configured ({why})")
    system, user = build_prompt(texts, source, target, summary, glossary)
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(s.llm_timeout, connect=10))
    try:
        if s.llm_provider == "anthropic":
            raw = call_anthropic(s, system, user, client)
        else:
            raw = call_openai(s, system, user, client)
    finally:
        if own:
            client.close()
    return parse_llm_response(raw, len(texts))

import json

import httpx
import pytest

from fukidashi.config import Settings
from fukidashi.translate.llm import (
    LLMError,
    build_prompt,
    call_anthropic,
    call_openai,
    parse_llm_response,
    translate_with_llm,
)

GOOD = {
    "translations": ["Hey, wait!", "None of your business.", "That day, everything changed."],
    "summary": "Kenta confronts Aoi, who is leaving town.",
    "glossary": {"ケンタ": "Kenta", "アオイ": "Aoi"},
}


def test_clean_json():
    r = parse_llm_response(json.dumps(GOOD, ensure_ascii=False), 3)
    assert r.translations == GOOD["translations"]
    assert r.summary == GOOD["summary"]
    assert r.glossary == GOOD["glossary"]
    assert r.problems == []


def test_code_fence_and_prose():
    text = "Sure! Here is the translation:\n```json\n" + json.dumps(GOOD) + "\n```\nLet me know if you need more."
    r = parse_llm_response(text, 3)
    assert r.translations == GOOD["translations"]
    assert r.problems == []


def test_prose_without_fence():
    text = "Here you go: " + json.dumps(GOOD) + " Hope that helps!"
    assert parse_llm_response(text, 3).translations == GOOD["translations"]


def test_think_block_is_ignored():
    text = '<think>The user wants {"translations": ["wrong"]}...</think>\n' + json.dumps(GOOD)
    assert parse_llm_response(text, 3).translations == GOOD["translations"]


def test_trailing_commas_and_smart_quotes():
    text = '{"translations": ["A", "B", "C",], "summary": “S”, "glossary": {"x": "y",},}'
    r = parse_llm_response(text, 3)
    assert r.translations == ["A", "B", "C"]
    assert r.summary == "S"
    assert r.glossary == {"x": "y"}


def test_too_few_translations_pads_with_none():
    r = parse_llm_response(json.dumps({"translations": ["A", "B"], "summary": "", "glossary": {}}), 4)
    assert r.translations == ["A", "B", None, None]
    assert any("2 translations for 4" in p for p in r.problems)


def test_too_many_translations_truncated():
    r = parse_llm_response(json.dumps({"translations": ["A", "B", "C", "D"]}), 2)
    assert r.translations == ["A", "B"]
    assert any("4 translations for 2" in p for p in r.problems)


def test_objects_with_ids_are_placed_by_id():
    payload = {"translations": [{"id": 3, "text": "C"}, {"id": 1, "text": "A"}]}
    r = parse_llm_response(json.dumps(payload), 3)
    assert r.translations == ["A", None, "C"]
    assert r.problems  # wrong length reported


def test_objects_without_ids_in_order():
    payload = {"translations": [{"translation": "A"}, {"text": "B"}]}
    assert parse_llm_response(json.dumps(payload), 2).translations == ["A", "B"]


def test_bare_array():
    assert parse_llm_response('["A", "B"]', 2).translations == ["A", "B"]


def test_renamed_key():
    assert parse_llm_response('{"lines": ["A", "B"]}', 2).translations == ["A", "B"]


def test_empty_string_means_skip_not_missing():
    r = parse_llm_response('{"translations": ["A", "", "C"]}', 3)
    assert r.translations == ["A", "", "C"]
    assert r.problems == []


def test_truncated_output_recovers_translations():
    full = json.dumps(GOOD)
    cut = full[: full.index('"summary"') + 25]  # cut off mid-summary
    r = parse_llm_response(cut, 3)
    assert r.translations == GOOD["translations"]


def test_broken_json_recovers_translations_array():
    text = '{"translations": ["A", "B"], "summary": "unterminated, "glossary": {oops}}'
    r = parse_llm_response(text, 2)
    assert r.translations == ["A", "B"]


def test_glossary_as_list_of_pairs():
    text = json.dumps({"translations": ["A"], "glossary": [{"source": "ケンタ", "target": "Kenta"}]})
    assert parse_llm_response(text, 1).glossary == {"ケンタ": "Kenta"}


@pytest.mark.parametrize(
    "text",
    [
        "",
        "I'm sorry, I can't help with that.",
        '{"summary": "no translations here"}',
        '{"translations": "not a list"}',
        '{"translations": [null, null]}',
    ],
)
def test_unusable_output_raises(text):
    with pytest.raises(LLMError):
        parse_llm_response(text, 2)


def test_prompt_contains_context():
    system, user = build_prompt(["おい", "待て"], "ja", "en", "Story so far.", {"ケンタ": "Kenta"})
    assert "Japanese" in system and "English" in system
    assert "Story so far." in user
    assert "ケンタ" in user and '"id": 2' in user


# --- HTTP request shape (mocked transport) -----------------------------------


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_anthropic_request_and_response():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["headers"] = req.headers
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"content": [{"type": "text", "text": json.dumps(GOOD)}], "stop_reason": "end_turn"})

    s = Settings(llm_provider="anthropic", anthropic_api_key="sk-test")
    r = translate_with_llm(s, ["a", "b", "c"], "ja", "en", "", {}, client=_client(handler))
    assert r.translations == GOOD["translations"]
    assert seen["url"] == "https://api.anthropic.com/v1/messages"
    assert seen["headers"]["x-api-key"] == "sk-test"
    assert seen["headers"]["anthropic-version"] == "2023-06-01"
    assert seen["body"]["model"] == "claude-haiku-5-5"
    assert seen["body"]["messages"][0]["role"] == "user"


def test_openai_compatible_request_and_response():
    seen = {}

    def handler(req: httpx.Request):
        seen["url"] = str(req.url)
        seen["auth"] = req.headers.get("authorization")
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(GOOD)}}]})

    s = Settings(llm_provider="compatible", openai_base_url="http://localhost:11434/v1/", llm_model="qwen2.5:14b")
    r = translate_with_llm(s, ["a", "b", "c"], "ja", "en", "", {}, client=_client(handler))
    assert r.translations == GOOD["translations"]
    assert seen["url"] == "http://localhost:11434/v1/chat/completions"
    assert seen["auth"] is None  # no key needed for Ollama
    assert seen["body"]["model"] == "qwen2.5:14b"
    assert "response_format" not in seen["body"]


def test_openai_uses_json_mode():
    seen = {}

    def handler(req):
        seen["body"] = json.loads(req.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": '{"translations": ["x"]}'}}]})

    s = Settings(llm_provider="openai", openai_api_key="k")
    call_openai(s, "sys", "user", _client(handler))
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_http_error_is_llm_error():
    s = Settings(llm_provider="anthropic", anthropic_api_key="bad")
    client = _client(lambda req: httpx.Response(401, json={"error": {"message": "invalid x-api-key"}}))
    with pytest.raises(LLMError, match="invalid x-api-key"):
        call_anthropic(s, "sys", "user", client)


def test_not_configured_raises():
    with pytest.raises(LLMError, match="ANTHROPIC_API_KEY"):
        translate_with_llm(Settings(llm_provider="anthropic"), ["a"], "ja", "en", "", {})

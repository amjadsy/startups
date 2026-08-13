"""Locks for the baseline runner's request shapes and key hygiene.

The request builders are pure (no network), so the provider API shapes and the
never-key-in-URL rule are pinned here without mocking urllib.
"""
import os
import pathlib

import source_baseline as sb


def _with_keys(**keys):
    for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY"):
        os.environ.pop(k, None)
    os.environ.update(keys)


def test_openai_shape_uses_max_completion_tokens():
    _with_keys(OPENAI_API_KEY="sk-test")
    url, headers, body = sb.build_openai_request("gpt-4o", "sys", "hi")
    assert url == "https://api.openai.com/v1/chat/completions"
    assert body["max_completion_tokens"] == sb.MAX_TOKENS
    assert "max_tokens" not in body, "gpt-5.x rejects max_tokens (HTTP 400)"
    assert body["messages"][0] == {"role": "system", "content": "sys"}


def test_openai_no_system_message_when_empty():
    _with_keys(OPENAI_API_KEY="sk-test")
    _, _, body = sb.build_openai_request("gpt-4o", "", "hi")
    assert [m["role"] for m in body["messages"]] == ["user"]


def test_anthropic_shape_uses_top_level_system():
    _with_keys(ANTHROPIC_API_KEY="sk-ant")
    url, headers, body = sb.build_anthropic_request("claude-3-5-sonnet", "sys", "hi")
    assert url == "https://api.anthropic.com/v1/messages"
    assert body["system"] == "sys"
    assert headers["anthropic-version"] == "2023-06-01"
    assert body["max_tokens"] == sb.MAX_TOKENS


def test_gemini_key_in_header_never_in_url():
    _with_keys(GEMINI_API_KEY="AIza-test")
    url, headers, body = sb.build_gemini_request("gemini-1.5-pro", "sys", "hi")
    assert "AIza-test" not in url, "key must never be a URL query parameter"
    assert "key=" not in url
    assert headers["x-goog-api-key"] == "AIza-test"
    assert body["systemInstruction"] == {"parts": [{"text": "sys"}]}
    assert body["generationConfig"]["maxOutputTokens"] == sb.MAX_TOKENS


def test_all_provider_urls_are_official_hosts():
    # Security note in the skill: the key only ever travels to its own
    # provider's official endpoint.
    _with_keys(OPENAI_API_KEY="a", ANTHROPIC_API_KEY="b", GEMINI_API_KEY="c")
    urls = [
        sb.build_openai_request("m", "", "x")[0],
        sb.build_anthropic_request("m", "", "x")[0],
        sb.build_gemini_request("m", "", "x")[0],
    ]
    allowed = ("https://api.openai.com/", "https://api.anthropic.com/",
               "https://generativelanguage.googleapis.com/")
    for u in urls:
        assert u.startswith(allowed), u


def test_env_file_loader_ignores_blank_and_malformed_lines(tmp_path: pathlib.Path):
    p = tmp_path / ".source-provider-env"
    p.write_text("\n# comment-ish\nOPENAI_API_KEY=sk-live\n", encoding="utf-8")
    os.environ.pop("OPENAI_API_KEY", None)
    sb.load_env_file(str(p))
    assert os.environ["OPENAI_API_KEY"] == "sk-live"

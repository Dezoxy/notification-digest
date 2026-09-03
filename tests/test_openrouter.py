import json
import logging
import urllib.error

import pytest

import digest.openrouter as openrouter_mod
from digest.openrouter import OpenRouterError, run_openrouter, validate_model_id


class _FakeHTTPResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _response_json(**overrides) -> bytes:
    payload = {"choices": [{"message": {"content": "hello from openrouter"}}]}
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8")


# --- run_openrouter: success ---


def test_run_openrouter_success_returns_content(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json())

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    result = run_openrouter("hi", "openai/gpt-5.6-sol", 60, "sk-test-key")

    assert result == "hello from openrouter"


def test_run_openrouter_sends_expected_model_and_reasoning_effort(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _FakeHTTPResponse(_response_json())

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    run_openrouter("the prompt text", "z-ai/glm-5.3", 42, "sk-test-key")

    assert captured["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert captured["method"] == "POST"
    assert captured["timeout"] == 42
    assert captured["headers"]["authorization"] == "Bearer sk-test-key"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["user-agent"] == "notification-digest/1.0"
    assert captured["body"] == {
        "model": "z-ai/glm-5.3",
        "messages": [{"role": "user", "content": "the prompt text"}],
        "reasoning": {"effort": "high"},
    }


def test_run_openrouter_strips_returned_content(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json(choices=[{"message": {"content": "  hi  \n"}}]))

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    assert run_openrouter("p", "openai/gpt-5.6-sol", 60, "key") == "hi"


# --- run_openrouter: usage logging ---


def test_run_openrouter_logs_usage_including_reasoning_tokens(monkeypatch, caplog):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(
            _response_json(
                usage={
                    "prompt_tokens": 1000,
                    "completion_tokens": 200,
                    "completion_tokens_details": {"reasoning_tokens": 150},
                }
            )
        )

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with caplog.at_level(logging.INFO):
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "key")

    assert any(
        "prompt_tokens=1000" in r.message
        and "completion_tokens=200" in r.message
        and "reasoning_tokens=150" in r.message
        for r in caplog.records
    )


def test_run_openrouter_missing_usage_does_not_raise(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json())

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    # No `usage` key in the response at all -- must not raise, usage
    # accounting is a nicety layered on top of a successful call.
    assert run_openrouter("p", "openai/gpt-5.6-sol", 60, "key") == "hello from openrouter"


def test_run_openrouter_malformed_usage_shape_does_not_raise(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json(usage="not a dict"))

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    assert run_openrouter("p", "openai/gpt-5.6-sol", 60, "key") == "hello from openrouter"


# --- run_openrouter: failure shapes ---


def test_run_openrouter_http_error_raises_with_only_the_status(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            request.full_url,
            401,
            "Unauthorized: your prompt said SECRET-PLANTED-TEXT",
            {},
            None,
        )

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError) as exc_info:
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "sk-test-key")

    message = str(exc_info.value)
    assert "401" in message
    assert "SECRET-PLANTED-TEXT" not in message
    assert "sk-test-key" not in message


def test_run_openrouter_non_http_exception_raises_with_only_the_type_name(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise TimeoutError("SECRET-PLANTED-TEXT: connection timed out to sk-test-key")

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError) as exc_info:
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "sk-test-key")

    message = str(exc_info.value)
    assert "TimeoutError" in message
    assert "SECRET-PLANTED-TEXT" not in message
    assert "sk-test-key" not in message


def test_run_openrouter_malformed_json_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(b"not json at all {{{")

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError):
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "key")


def test_run_openrouter_empty_content_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json(choices=[{"message": {"content": "   "}}]))

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError):
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "key")


def test_run_openrouter_missing_content_field_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(json.dumps({"choices": [{"message": {}}]}).encode("utf-8"))

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError):
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "key")


def test_run_openrouter_non_string_content_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json(choices=[{"message": {"content": 42}}]))

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError):
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "key")


def test_run_openrouter_empty_choices_raises(monkeypatch):
    def fake_urlopen(request, timeout=None):
        return _FakeHTTPResponse(_response_json(choices=[]))

    monkeypatch.setattr(openrouter_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(OpenRouterError):
        run_openrouter("p", "openai/gpt-5.6-sol", 60, "key")


# --- validate_model_id ---


@pytest.mark.parametrize(
    "model",
    [
        "openai/gpt-5.6-sol",
        "z-ai/glm-5.3",
        "openai/gpt-5.6-terra",
        "deepseek/deepseek-v4-flash",
    ],
)
def test_validate_model_id_accepts_real_ids(model):
    assert validate_model_id(model) is True


@pytest.mark.parametrize("model", ["sol", "a/b c", "UPPER/case"])
def test_validate_model_id_rejects_malformed_ids(model):
    assert validate_model_id(model) is False

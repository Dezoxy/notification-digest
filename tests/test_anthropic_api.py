import io
import json
import logging
import urllib.error

import pytest

import digest.anthropic_api as api_mod
from digest.anthropic_api import (
    AnthropicApiError,
    FederationConfig,
    run_anthropic,
    validate_model_id,
)

_TOKEN_URL = "https://api.anthropic.com/v1/oauth/token"
_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

_ENTRA_JWT = "entra.jwt.dummy-assertion"
_ACCESS_TOKEN = "sk-ant-oat01-dummy-access-token"
_PROMPT = "PROMPT-TEXT-FROM-SCRAPED-CHATS"

_FEDERATION = FederationConfig(
    rule_id="fdrl_test",
    organization_id="11111111-2222-3333-4444-555555555555",
    service_account_id="svac_test",
    audience="api://11111111-2222-3333-4444-555555555555",
    tenant_id="99999999-8888-7777-6666-555555555555",
)


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def _json(payload) -> bytes:
    return json.dumps(payload).encode("utf-8")


def _messages_reply(**overrides) -> dict:
    payload = {
        "content": [{"type": "text", "text": "hello from claude"}],
        "stop_reason": "end_turn",
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }
    payload.update(overrides)
    return payload


class _FakeApi:
    """Routes urlopen by URL and records every Request it sees."""

    def __init__(self, token_reply=None, messages_reply=None):
        self.token_reply = (
            token_reply if token_reply is not None else {"access_token": _ACCESS_TOKEN}
        )
        self.messages_reply = messages_reply if messages_reply is not None else _messages_reply()
        self.requests: list = []

    def urlopen(self, request, timeout=None):
        self.requests.append(request)
        reply = self.token_reply if request.full_url == _TOKEN_URL else self.messages_reply
        if isinstance(reply, Exception):
            raise reply
        return _FakeHTTPResponse(reply if isinstance(reply, bytes) else _json(reply))

    def request_to(self, url):
        return next(r for r in self.requests if r.full_url == url)


@pytest.fixture
def fake_api(monkeypatch) -> _FakeApi:
    api = _FakeApi()
    monkeypatch.setattr(api_mod.urllib.request, "urlopen", api.urlopen)
    monkeypatch.setattr(api_mod, "_entra_token", lambda federation: _ENTRA_JWT)
    return api


def _headers(request) -> dict[str, str]:
    return {k.lower(): v for k, v in request.headers.items()}


def _http_error(url: str, code: int = 500) -> urllib.error.HTTPError:
    # The body echoes the prompt and a token, as a real API error body might.
    body = f"error echoing {_PROMPT} and {_ACCESS_TOKEN}".encode()
    return urllib.error.HTTPError(url, code, "boom", {}, io.BytesIO(body))


def _assert_nothing_sensitive(text: str) -> None:
    for secret in (_PROMPT, _ACCESS_TOKEN, _ENTRA_JWT, "error echoing"):
        assert secret not in text


# --- run_anthropic: success ---


def test_success_returns_stripped_text(fake_api):
    fake_api.messages_reply = _messages_reply(content=[{"type": "text", "text": "  hi there \n"}])

    assert run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION) == "hi there"


def test_thinking_blocks_are_dropped_and_text_blocks_concatenated(fake_api):
    fake_api.messages_reply = _messages_reply(
        content=[
            {"type": "thinking", "thinking": "SECRET REASONING", "signature": "sig"},
            {"type": "text", "text": "part one, "},
            {"type": "text", "text": "part two"},
        ]
    )

    result = run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)

    assert result == "part one, part two"
    assert "SECRET REASONING" not in result


# --- run_anthropic: request shape ---


def test_token_exchange_request_body_and_headers(fake_api):
    run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)

    request = fake_api.request_to(_TOKEN_URL)
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {
        "grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
        "assertion": _ENTRA_JWT,
        "federation_rule_id": "fdrl_test",
        "organization_id": "11111111-2222-3333-4444-555555555555",
        "service_account_id": "svac_test",
    }
    assert "authorization" not in _headers(request)


def test_token_exchange_includes_workspace_id_only_when_configured(fake_api):
    federation = FederationConfig(**{**_FEDERATION.__dict__, "workspace_id": "wrkspc_abc"})

    run_anthropic("p", "claude-opus-5-5", 60, federation)

    assert json.loads(fake_api.request_to(_TOKEN_URL).data)["workspace_id"] == "wrkspc_abc"


def test_token_exchange_omits_workspace_id_when_not_configured(fake_api):
    run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)

    assert "workspace_id" not in json.loads(fake_api.request_to(_TOKEN_URL).data)


def test_messages_request_uses_exchanged_bearer_token_and_expected_body(fake_api):
    run_anthropic(_PROMPT, "claude-sonnet-5-5", 60, _FEDERATION)

    request = fake_api.request_to(_MESSAGES_URL)
    headers = _headers(request)
    assert headers["authorization"] == f"Bearer {_ACCESS_TOKEN}"
    assert headers["anthropic-version"] == "2023-06-01"
    body = json.loads(request.data)
    assert body["model"] == "claude-sonnet-5-5"
    assert isinstance(body["max_tokens"], int) and body["max_tokens"] > 0
    assert body["messages"] == [{"role": "user", "content": _PROMPT}]
    assert body["output_config"] == {"effort": "high"}


def test_no_request_carries_an_api_key_header(fake_api):
    run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)

    assert len(fake_api.requests) == 2
    assert all("x-api-key" not in _headers(r) for r in fake_api.requests)


# --- run_anthropic: unusable replies ---


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_unusable_stop_reason_raises(fake_api, stop_reason):
    fake_api.messages_reply = _messages_reply(stop_reason=stop_reason)

    with pytest.raises(AnthropicApiError, match=stop_reason):
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)


@pytest.mark.parametrize("text", ["", "   \n\t "])
def test_empty_or_whitespace_text_raises(fake_api, text):
    fake_api.messages_reply = _messages_reply(content=[{"type": "text", "text": text}])

    with pytest.raises(AnthropicApiError, match="no text"):
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)


def test_only_thinking_blocks_raises(fake_api):
    fake_api.messages_reply = _messages_reply(content=[{"type": "thinking", "thinking": "hmm"}])

    with pytest.raises(AnthropicApiError, match="no text"):
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)


def test_non_list_content_raises(fake_api):
    fake_api.messages_reply = _messages_reply(content="just a string")

    with pytest.raises(AnthropicApiError, match="unexpected shape"):
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)


@pytest.mark.parametrize("reply", [{}, {"access_token": ""}, {"access_token": 123}])
def test_exchange_response_without_access_token_raises(fake_api, reply):
    fake_api.token_reply = reply

    with pytest.raises(AnthropicApiError, match="no access token"):
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)

    assert [r.full_url for r in fake_api.requests] == [_TOKEN_URL]


# --- run_anthropic: failures are scrubbed ---


@pytest.mark.parametrize("failing_url", [_TOKEN_URL, _MESSAGES_URL])
def test_http_error_names_the_status_only(fake_api, caplog, failing_url):
    error = _http_error(failing_url, 429)
    if failing_url == _TOKEN_URL:
        fake_api.token_reply = error
    else:
        fake_api.messages_reply = error

    with caplog.at_level(logging.DEBUG), pytest.raises(AnthropicApiError) as exc_info:
        run_anthropic(_PROMPT, "claude-opus-5-5", 60, _FEDERATION)

    message = str(exc_info.value)
    assert "429" in message
    _assert_nothing_sensitive(message)
    _assert_nothing_sensitive(caplog.text)
    # The scrubbed error must not chain to the exception that holds the body.
    assert exc_info.value.__cause__ is None


@pytest.mark.parametrize("failing_url", [_TOKEN_URL, _MESSAGES_URL])
def test_network_error_names_only_the_exception_type(fake_api, caplog, failing_url):
    error = urllib.error.URLError(f"unreachable {_PROMPT} {_ACCESS_TOKEN}")
    if failing_url == _TOKEN_URL:
        fake_api.token_reply = error
    else:
        fake_api.messages_reply = error

    with caplog.at_level(logging.DEBUG), pytest.raises(AnthropicApiError) as exc_info:
        run_anthropic(_PROMPT, "claude-opus-5-5", 60, _FEDERATION)

    assert "URLError" in str(exc_info.value)
    _assert_nothing_sensitive(str(exc_info.value))
    _assert_nothing_sensitive(caplog.text)
    assert "unreachable" not in str(exc_info.value)


@pytest.mark.parametrize("failing_url", [_TOKEN_URL, _MESSAGES_URL])
def test_non_json_body_names_only_the_shape(fake_api, failing_url):
    body = f"<html>{_PROMPT} {_ACCESS_TOKEN}</html>".encode()
    if failing_url == _TOKEN_URL:
        fake_api.token_reply = body
    else:
        fake_api.messages_reply = body

    with pytest.raises(AnthropicApiError, match="not valid JSON") as exc_info:
        run_anthropic(_PROMPT, "claude-opus-5-5", 60, _FEDERATION)

    _assert_nothing_sensitive(str(exc_info.value))


def test_json_that_is_not_an_object_raises(fake_api):
    fake_api.messages_reply = b'["a", "list"]'

    with pytest.raises(AnthropicApiError, match="not a JSON object"):
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)


class _FakeAssertionCredential:
    """Stands in for ClientAssertionCredential: asks for the assertion, returns an app token."""

    seen: dict = {}

    def __init__(self, tenant_id, client_id, func):
        type(self).seen = {"tenant_id": tenant_id, "client_id": client_id}
        self._func = func

    def get_token(self, *scopes):
        type(self).seen["assertion"] = self._func()
        type(self).seen["scopes"] = scopes
        return type("Token", (), {"token": _ENTRA_JWT})()


def test_entra_token_failure_names_the_type_not_the_text(monkeypatch, caplog):
    import azure.identity

    class _BrokenIdentity:
        def __init__(self, client_id=None):
            pass

        def get_token(self, *scopes):
            raise RuntimeError("secret-looking text")

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", _BrokenIdentity)
    monkeypatch.setattr(azure.identity, "ClientAssertionCredential", _FakeAssertionCredential)
    sent = []
    monkeypatch.setattr(api_mod.urllib.request, "urlopen", lambda *a, **k: sent.append(a))

    with caplog.at_level(logging.DEBUG), pytest.raises(AnthropicApiError) as exc_info:
        run_anthropic("p", "claude-opus-5-5", 60, _FEDERATION)

    assert "RuntimeError" in str(exc_info.value)
    assert "secret-looking text" not in str(exc_info.value)
    assert "secret-looking text" not in caplog.text
    assert exc_info.value.__cause__ is None
    assert sent == []


def test_entra_token_trades_the_identity_token_for_an_app_token(monkeypatch):
    import azure.identity

    identity_seen = {}

    class _Identity:
        def __init__(self, client_id=None):
            identity_seen["client_id"] = client_id

        def get_token(self, *scopes):
            identity_seen["scopes"] = scopes
            return type("Token", (), {"token": "managed-identity-assertion"})()

    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", _Identity)
    monkeypatch.setattr(azure.identity, "ClientAssertionCredential", _FakeAssertionCredential)
    federation = FederationConfig(**{**_FEDERATION.__dict__, "identity_client_id": "client-123"})

    # The app token is returned, never the managed identity's own (24 hour) token.
    assert api_mod._entra_token(federation) == _ENTRA_JWT
    # Hop 1: the managed identity is asked for Entra's token-exchange audience.
    assert identity_seen == {
        "client_id": "client-123",
        "scopes": ("api://AzureADTokenExchange/.default",),
    }
    # Hop 2: that token is the client assertion for the audience app in this tenant.
    assert _FakeAssertionCredential.seen == {
        "tenant_id": _FEDERATION.tenant_id,
        "client_id": "11111111-2222-3333-4444-555555555555",
        "assertion": "managed-identity-assertion",
        "scopes": (f"{_FEDERATION.audience}/.default",),
    }


# --- run_anthropic: budget ---


def test_exhausted_budget_raises_before_any_request(monkeypatch):
    def fail(*args, **kwargs):
        raise AssertionError("no request or token fetch may happen with no budget")

    monkeypatch.setattr(api_mod.urllib.request, "urlopen", fail)
    monkeypatch.setattr(api_mod, "_entra_token", fail)

    with pytest.raises(AnthropicApiError, match="budget exhausted"):
        run_anthropic("p", "claude-opus-5-5", 0, _FEDERATION)


# --- validate_model_id ---


@pytest.mark.parametrize("model", ["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"])
def test_validate_model_id_accepts_claude_ids(model):
    assert validate_model_id(model) is True


@pytest.mark.parametrize(
    "model",
    ["openai/gpt-5.6-sol", "claude-", "Claude-opus", "", "claude opus", "claude-opus 5"],
)
def test_validate_model_id_rejects_everything_else(model):
    assert validate_model_id(model) is False

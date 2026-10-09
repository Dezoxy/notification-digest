"""One Claude API call over workload identity federation -- the fallback chain's HTTP leg.

WHY THIS EXISTS. `digest/summarize.py`'s `run_claude` drives the `claude -p`
CLI on the owner's subscription, and it fails in ways a retry cannot fix: the
subscription's usage limit can run out mid-run, the model's safety classifier
can refuse an ordinary security-heavy digest (`SafeguardsRefusalError`), or the
CLI can time out. `run_with_fallbacks` then retries the same prompt through
this module: the Claude API, billed to the owner's prepaid API credits, one
HTTP request per leg.

NO API KEY, NO SDK. The Azure job already holds a managed identity. This module
trades that identity's token with Entra for a short-lived token of a dedicated
app registration (`_entra_token`), exchanges it at `/v1/oauth/token` (RFC 7523
jwt-bearer, Anthropic's Workload Identity Federation) for a short-lived
`sk-ant-oat01-...` access token, and sends that as a Bearer token. Nothing
static is stored anywhere, so there is nothing to rotate or leak. The exchange
runs once per call, never cached: fallbacks are rare. Plain `urllib` for the
two Anthropic POSTs, like every other outbound call in this codebase
(`publish.py`, the collectors) -- they do not justify the `anthropic` SDK as a
dependency.

SECRETS POSTURE. The request body is the pipeline's own prompt, built from
scraped Telegram/X text, and an HTTP error body can echo request content back.
`AnthropicApiError` therefore never carries a response body, `str(exc)`, a URL
or a token: only an HTTP status or an exception type name.
"""

from __future__ import annotations

import json
import logging
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

_API_BASE = "https://api.anthropic.com"
_TOKEN_URL = f"{_API_BASE}/v1/oauth/token"
_MESSAGES_URL = f"{_API_BASE}/v1/messages"
_API_VERSION = "2023-06-01"
_JWT_BEARER_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"

# Same product identifier the other outbound calls send (duplicated, not
# imported: there is no shared HTTP-client module to hang one constant off).
_USER_AGENT = "notification-digest/1.0"

# Fixed, not threaded from Config, for the reason the old fallback leg gave: a
# leg only runs after the primary failed, so it is already the exceptional path
# for every tier, and the owner chose its best attempt over a cheaper second
# failure. Matches production's CLAUDE_EFFORT. Public because
# `run_with_fallbacks` stamps it on the leg's `ModelRun.effort`.
EFFORT = "high"

# A hard cap on thinking PLUS reply (thinking counts toward it). Sized well
# above a digest so adaptive thinking cannot starve the visible text; a reply
# that still hits it comes back as stop_reason "max_tokens" and is rejected.
_MAX_TOKENS = 32000

_MODEL_ID_RE = re.compile(r"^claude-[a-z0-9][a-z0-9.-]*$")

# Per-step cap, so a hung token endpoint cannot eat the whole shared budget
# before the Messages call even starts.
_MAX_EXCHANGE_SECONDS = 30


class AnthropicApiError(Exception):
    """Raised when a Claude API fallback call fails, for any reason.

    The message names a step and an HTTP status or exception type, never a
    response body, token, URL or any prompt content -- it is exactly what gets
    logged, so anything unsafe to log is unsafe to put here.
    """


@dataclass(frozen=True)
class FederationConfig:
    """Everything needed to federate, none of it secret.

    Identifiers only: the rule, organization, service account and workspace ids
    from the Claude Console, the Entra tenant and the Entra audience
    (`api://<app id>`). The credential is the managed identity itself, which
    never appears in config. `identity_client_id` selects one of several
    user-assigned identities and is the same value the Blob state client
    already uses.
    """

    rule_id: str
    organization_id: str
    service_account_id: str
    audience: str
    tenant_id: str
    workspace_id: str | None = None
    identity_client_id: str | None = None


def validate_model_id(model: str) -> bool:
    """True when `model` has the shape of a Claude API model id (`claude-...`)."""
    return bool(_MODEL_ID_RE.match(model))


# The audience Entra requires on a token that is presented as a client
# assertion through a federated identity credential.
_ENTRA_EXCHANGE_SCOPE = "api://AzureADTokenExchange/.default"


def _entra_token(federation: FederationConfig) -> str:
    """Trade the managed identity's token for a short-lived token of the audience app.

    Two hops inside Entra, not one. Asking the managed identity for the
    audience directly returns a token that lives about 24 hours (86,700 s
    between `iat` and `exp`, measured), and Anthropic rejects an assertion
    longer than the issuer's limit (`jwt_lifetime_too_long`). The Claude
    Console caps that limit at 86,400 s and the Admin API that could raise it
    is not available to individual organizations, so that token can never be
    accepted. Instead the managed identity's token is presented to Entra as a
    client assertion, through a federated identity credential on the audience
    app registration, and Entra returns an ordinary app token that lives about
    an hour (3,900 s, measured). The identity Anthropic sees is therefore the
    app's service principal; the managed identity is trusted only by Entra.
    """
    from azure.identity import ClientAssertionCredential, ManagedIdentityCredential

    try:
        identity = ManagedIdentityCredential(client_id=federation.identity_client_id)
        credential = ClientAssertionCredential(
            federation.tenant_id,
            federation.audience.removeprefix("api://"),
            lambda: identity.get_token(_ENTRA_EXCHANGE_SCOPE).token,
        )
        return credential.get_token(f"{federation.audience}/.default").token
    except Exception as exc:
        logger.warning("claude api: entra token request failed: %s", type(exc).__name__)
        raise AnthropicApiError(
            f"claude api entra token request failed: {type(exc).__name__}"
        ) from None


def _post_json(
    url: str,
    payload: dict,
    headers: dict[str, str],
    timeout_seconds: float,
    step: str,
) -> dict:
    """POST JSON and return the parsed object, with every failure secrets-scrubbed."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"content-type": "application/json", "user-agent": _USER_AGENT, **headers},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        request_id = exc.headers.get("request-id") if exc.headers else None
        logger.warning(
            "claude api %s failed with status %d (request-id %s)", step, exc.code, request_id
        )
        raise AnthropicApiError(f"claude api {step} failed with status {exc.code}") from None
    except Exception as exc:
        logger.warning("claude api %s failed: %s", step, type(exc).__name__)
        raise AnthropicApiError(f"claude api {step} failed: {type(exc).__name__}") from None
    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise AnthropicApiError(f"claude api {step} response was not valid JSON") from None
    if not isinstance(parsed, dict):
        raise AnthropicApiError(f"claude api {step} response was not a JSON object")
    return parsed


def _exchange(federation: FederationConfig, entra_jwt: str, timeout_seconds: float) -> str:
    """Trade the Entra JWT for a short-lived Anthropic access token."""
    payload = {
        "grant_type": _JWT_BEARER_GRANT,
        "assertion": entra_jwt,
        "federation_rule_id": federation.rule_id,
        "organization_id": federation.organization_id,
        "service_account_id": federation.service_account_id,
    }
    if federation.workspace_id is not None:
        payload["workspace_id"] = federation.workspace_id
    parsed = _post_json(_TOKEN_URL, payload, {}, timeout_seconds, "token exchange")
    token = parsed.get("access_token")
    if not isinstance(token, str) or not token:
        raise AnthropicApiError("claude api token exchange response had no access token")
    return token


def run_anthropic(
    prompt: str, model: str, timeout_seconds: int, federation: FederationConfig
) -> str:
    """Send one prompt to the Claude Messages API and return the reply text.

    `timeout_seconds` is this leg's whole remaining budget (Entra token,
    exchange and completion together), never per step. Only `text` blocks are
    returned -- Opus and Sonnet 5.5 always think, so `content` also carries
    thinking blocks that must not reach a digest. A reply that was truncated
    (`max_tokens`) or declined (`refusal`) is a failed leg, exactly like a
    leg that raised: `run_with_fallbacks` moves on to the next one instead of
    accepting unusable text.

    Logs one INFO line with the model and token counts so a fallback's cost is
    visible in the log record, not only on a bill. Returned text is stripped,
    matching `run_claude`'s contract: the legs are interchangeable.
    """
    deadline = time.monotonic() + timeout_seconds

    def remaining(cap: float | None = None) -> float:
        left = deadline - time.monotonic()
        if left < 1:
            raise AnthropicApiError("claude api budget exhausted")
        return min(left, cap) if cap is not None else left

    remaining()
    entra_jwt = _entra_token(federation)
    access_token = _exchange(federation, entra_jwt, remaining(_MAX_EXCHANGE_SECONDS))
    parsed = _post_json(
        _MESSAGES_URL,
        {
            "model": model,
            "max_tokens": _MAX_TOKENS,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": EFFORT},
        },
        {"authorization": f"Bearer {access_token}", "anthropic-version": _API_VERSION},
        remaining(),
        "messages call",
    )

    usage = parsed.get("usage")
    if isinstance(usage, dict):
        logger.info(
            "claude api usage: model=%s input_tokens=%s output_tokens=%s",
            model,
            usage.get("input_tokens"),
            usage.get("output_tokens"),
        )

    stop_reason = parsed.get("stop_reason")
    if stop_reason in ("max_tokens", "refusal"):
        raise AnthropicApiError(f"claude api reply ended with stop_reason {stop_reason}")
    content = parsed.get("content")
    if not isinstance(content, list):
        raise AnthropicApiError("claude api response had an unexpected shape: content")
    texts = [
        block["text"]
        for block in content
        if isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
    ]
    text = "".join(texts).strip()
    if not text:
        raise AnthropicApiError("claude api response contained no text")
    return text

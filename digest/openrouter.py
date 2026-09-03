"""Builds and sends one OpenRouter chat-completion request -- the fallback chain's HTTP leg.

WHY THIS EXISTS. `digest/summarize.py`'s `run_claude` is the primary path for
every model call in this codebase, and it fails in ways that have nothing to
do with content quality: the owner's Max-subscription usage limits can be hit
mid-run, the target model's real-time safety classifier can flag an
otherwise-ordinary security-heavy digest (`SafeguardsRefusalError`), or the
CLI itself can time out or exit non-zero for a transient reason. None of
those are recoverable by retrying the same model against the same prompt.
`digest/summarize.py`'s `run_with_fallbacks` is the retry chain that reaches
for a DIFFERENT model when that happens, and this module is what actually
calls one: an OpenRouter model id, one HTTP request, one prompt in, one
completion out.

WHY PLAIN URLLIB, NOT AN SDK. Matches this codebase's established
convention for hitting an external HTTP API: `digest/publish.py`'s
`_send_message`, `digest/collectors/rss.py`, and `digest/collectors/
polymarket.py` all use the stdlib `urllib.request`/`urllib.error` pair
rather than pulling in `requests` or a provider SDK. OpenRouter exposes a
plain REST endpoint (OpenAI-compatible chat completions) -- there is nothing
here an SDK would buy beyond what three stdlib calls already do, and every
other HTTP call in this codebase already pays the same small urllib
boilerplate rather than adding a dependency for it.

WHY A SEPARATE MODULE FROM summarize.py, NOT FOLDED IN. `digest/summarize.py`
already carries `run_claude`, the primary call, and will carry
`run_with_fallbacks`, the chain orchestrator that calls BOTH `run_claude`
(via the caller's own closure) and this module's `run_openrouter`. If this
function lived inside summarize.py instead, summarize.py would have to
import nothing extra -- but every OTHER module that wants OpenRouter-shaped
validation at config-load time (`digest/config.py`'s `_optional_model_id_tuple`,
which needs `validate_model_id` to check FALLBACK_MODELS/FALLBACK_LIGHT_MODELS
entries at startup) would then have to import summarize.py just to reach it,
pulling in the CLI-subprocess machinery, the prompt-building helpers, and
every other summarize.py symbol along with it. Keeping this as its own small
module means the dependency runs in exactly one direction: summarize.py
imports `run_openrouter`/`OpenRouterError` FROM here, config.py imports
`validate_model_id` FROM here, and this module imports nothing back from
either -- there is no import cycle to worry about, ever, by construction.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_API_URL = "https://openrouter.ai/api/v1/chat/completions"

# Mirrors digest/publish.py's own _USER_AGENT constant (duplicated, not
# imported -- the same call digest/publish.py's own comment makes about
# duplicating digest/collectors/polymarket.py's copy: there is no shared
# "HTTP client" abstraction across these otherwise-unrelated modules to hang
# one constant off of, and a stable, honest product identifier costs nothing
# to state three times over). OpenRouter has no documented edge-level
# User-Agent rejection the way Cloudflare's Browser Integrity Check does,
# but there is no reason to send the stdlib default "Python-urllib/3.x"
# signature here either when every other outbound call in this codebase
# already identifies itself.
_USER_AGENT = "notification-digest/1.0"

# Fixed, NOT threaded from Config -- deliberately, and unlike the primary's
# own CLAUDE_EFFORT knob. digest/patreon.py's _PATREON_EFFORT,
# digest/translate.py's _TRANSLATE_EFFORT, and digest/context.py's
# _CONTEXT_EFFORT are all fixed, cheaper-than-primary module constants for
# their own LIGHT-tier work (translation is a mechanical rewrite, a context
# primer is factual background -- neither needs frontier-level editorial
# judgment). A fallback leg, by contrast, only ever runs after the PRIMARY
# has already failed -- it is already the exceptional, low-volume path, for
# EVERY tier this codebase has, light included -- so the owner chose to pay
# for the fallback model's best attempt at "high" reasoning rather than risk
# a second, cheaper-effort failure on content the primary already couldn't
# handle. Only CLAUDE_EFFORT (the primary's own effort, Config.claude_effort)
# stays a configurable knob; every fallback leg, at every tier, always asks
# OpenRouter for "high".
_REASONING_EFFORT = "high"

# OpenRouter's own model-id shape: "<provider>/<model>", e.g.
# "openai/gpt-5.6-sol" or "z-ai/glm-5.3". The provider segment is always
# lowercase in OpenRouter's own catalog; the model segment additionally
# allows ":" for OpenRouter's own variant suffixes (e.g. some listings carry
# a ":free" or ":online" tag). digest/config.py's `_optional_model_id_tuple`
# imports `validate_model_id` (below), which is built on this single
# pattern, rather than each module keeping its own copy -- one home for the
# shape, checked at Config-load time so a typo'd model id is a startup
# ConfigError, not a fallback leg silently discovered as broken only during
# a live outage.
_MODEL_ID_RE = re.compile(r"^[a-z0-9._-]+/[a-z0-9._:-]+$")


class OpenRouterError(Exception):
    """Raised when an OpenRouter chat-completion call fails, for any reason.

    The message NEVER carries a response body, `str(exc)`, `exc.url`, or the
    API key: mirrors digest/publish.py's `_send_message`/`TelegramSendError`
    secrets posture exactly, and for the identical reason. The request here
    carries a Bearer API key in a header, and the request BODY is this
    pipeline's own prompt -- built (see digest/summarize.py's `build_prompt`
    and friends) from scraped Telegram/X message text. An HTTPError's body
    can echo request content back (a validation error naming the offending
    field, a rate-limit message quoting a snippet), so only `exc.code` (an
    HTTP status, never a secret) crosses into the raised exception for an
    `HTTPError`; every other exception shape (network error, timeout,
    malformed response) carries only `type(exc).__name__`. Neither the key
    nor the prompt nor a response body ever reaches a log line either --
    this exception's message is exactly what gets logged and shipped to
    Loki, so anything unsafe to log is unsafe to put here.
    """


def validate_model_id(model: str) -> bool:
    """True when `model` matches OpenRouter's own "<provider>/<model>" id shape.

    Used by digest/config.py's `_optional_model_id_tuple` to validate every
    entry of FALLBACK_MODELS/FALLBACK_LIGHT_MODELS at Config-load time --
    see `_MODEL_ID_RE`'s own comment for why this lives here rather than a
    copy in config.py. A public model id (e.g. "openai/gpt-5.6-sol") carries
    no secret -- there is nothing sensitive about validating or logging one.
    """
    return bool(_MODEL_ID_RE.match(model))


def run_openrouter(prompt: str, model: str, timeout_seconds: int, api_key: str) -> str:
    """POST one prompt to OpenRouter's chat-completions endpoint and return the reply text.

    Sends `{"model": model, "messages": [{"role": "user", "content": prompt}],
    "reasoning": {"effort": _REASONING_EFFORT}}` as the JSON body, with the
    API key carried as a Bearer `authorization` header (never in the body or
    the URL, where it would be far more likely to end up copied into a log
    line or an error message by accident). `reasoning.effort` is OpenRouter's
    own analogue of `claude -p`'s `--effort` flag -- see `_REASONING_EFFORT`'s
    own comment for why it is fixed at "high" for every leg rather than
    threaded from Config the way the primary's CLAUDE_EFFORT is.

    Raises `OpenRouterError` on every failure shape, secrets-scrubbed per
    that exception's own docstring: an `HTTPError` carries only the status
    code (also logged, at WARNING, alongside the model name -- a public
    model id, not a secret); any other exception (network error, a socket
    timeout from the `timeout=timeout_seconds` passed to `urlopen` included,
    malformed/non-JSON response body, an unexpected response shape) carries
    only `type(exc).__name__`. `choices[0].message.content` is parsed
    defensively: a missing key, a non-string value, or a value that is empty
    or whitespace-only after stripping all raise `OpenRouterError` with a
    message that describes the SHAPE of the problem (e.g. which field/type
    was wrong), never the content itself -- the content is exactly the
    untrusted, prompt-injection-reachable text this function must never let
    leak into a raised exception's message.

    Logs one INFO line with the model name and, when the response carries a
    `usage` object, `prompt_tokens`, `completion_tokens`, and
    `usage.completion_tokens_details.reasoning_tokens` -- reasoning tokens
    are billed as OUTPUT tokens by OpenRouter's own pricing and are the main
    cost unknown of a fallback leg (a model can "think" for an
    unpredictable, and non-trivial, number of tokens before it ever writes
    the visible reply), so surfacing them here is what makes that cost
    visible in the log/Loki record rather than only showing up as a bill
    later. `usage` (and every field inside it) is parsed defensively and a
    missing/malformed shape is silently skipped, never raised over: usage
    accounting is an operational nicety layered on top of a successful call,
    not part of the call's own success/failure contract.

    Returned content is stripped, matching `run_claude`'s own stripped-stdout
    contract -- the two are interchangeable legs of the same
    `run_with_fallbacks` chain (digest/summarize.py), and every downstream
    consumer (`validate_output`, `enforce_link_allowlist`, and friends)
    already expects that shape from a successful call.
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "reasoning": {"effort": _REASONING_EFFORT},
    }
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        _API_URL,
        data=data,
        headers={
            "authorization": f"Bearer {api_key}",
            "content-type": "application/json",
            "user-agent": _USER_AGENT,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        logger.warning("openrouter call failed with status %d (model %s)", exc.code, model)
        raise OpenRouterError(f"openrouter call failed with status {exc.code}") from None
    except Exception as exc:
        logger.warning("openrouter call failed: %s (model %s)", type(exc).__name__, model)
        raise OpenRouterError(f"openrouter call failed: {type(exc).__name__}") from None

    try:
        parsed = json.loads(body)
    except json.JSONDecodeError:
        raise OpenRouterError("openrouter response was not valid JSON") from None

    # Usage/cost logging -- see this function's own docstring for why
    # reasoning_tokens specifically is called out. Entirely defensive: any
    # shape surprise here (a non-dict `usage`, a missing nested field) is
    # silently skipped rather than raised, since a malformed usage block
    # says nothing about whether the completion itself succeeded.
    usage = parsed.get("usage") if isinstance(parsed, dict) else None
    if isinstance(usage, dict):
        details = usage.get("completion_tokens_details")
        reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
        logger.info(
            "openrouter usage: model=%s prompt_tokens=%s completion_tokens=%s reasoning_tokens=%s",
            model,
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
            reasoning_tokens,
        )

    try:
        content = parsed["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise OpenRouterError(
            f"openrouter response had an unexpected shape: {type(exc).__name__}"
        ) from None
    if not isinstance(content, str) or not content.strip():
        raise OpenRouterError("openrouter response content was empty or not a string")

    return content.strip()

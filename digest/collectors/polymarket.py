"""Polymarket prediction-market collector: swing-detection over the top-N markets by volume.

See CLAUDE.md and the feature spec this module implements for the full
rationale. This module is synchronous (urllib), matching rss.py -- the
caller (digest/main.py) runs collectors sequentially, one at a time, so
there is nothing else in flight to block on.

Money-weighted odds are a salience signal: a probability that moves sharply
overnight usually means something happened. This collector does not report
absolute probabilities -- it reports MOVEMENT, comparing each market's
current probability against the last probability THIS module actually
reported (its stored "anchor"), never against the last probability it merely
observed.

The anchor rule (read this before touching the swing logic below)
----------------------------------------------------------------------
The stored probability per market (digest/state.py's `polymarket_probs`
table, `probability` column) is the last REPORTED probability, not the last
OBSERVED one:

- A market never seen before stores its current probability as a baseline
  and emits NO item -- a first sighting is not a swing, there is nothing to
  compare it against yet.
- `abs(current - stored) >= threshold` emits an item AND moves the anchor to
  `current`.
- Below threshold: NO item, and the anchor is deliberately left UNCHANGED.

That last point is the crux of the design, not an oversight: if the anchor
moved to `current` on every observation regardless of threshold, a slow,
steady drift (say 5 percentage points per 6-hour run, forever) would never
trip the threshold -- each step is individually too small, and the
comparison base keeps sliding right along with the drift, so the gap between
consecutive observations never grows. Anchoring at the last REPORTED value
instead means an undetected drift keeps accumulating against a fixed
reference point across runs until it finally crosses the threshold -- at
which point it is reported exactly once, with the full accumulated
magnitude. This is what lets the collector catch "a slow-burn process nobody
was watching" instead of only ever catching single-run shocks.

Network context (shapes the config, digest/config.py)
----------------------------------------------------------------------
polymarket.com is ISP-blocked in Hungary. In production,
`POLYMARKET_API_BASE` points at a small Cloudflare Worker proxy (built
separately) rather than `https://gamma-api.polymarket.com` directly, and
`POLYMARKET_PROXY_KEY` (optional, a secret -- never logged) authenticates to
that proxy via an `x-proxy-key` header. Neither of those facts changes
anything about how this module talks to "the API" -- it's still exactly one
GET per run to `{base}/markets`.

Gamma API response quirk (verified against the live API)
----------------------------------------------------------------------
`outcomes` and `outcomePrices` are each a JSON-ENCODED STRING containing an
array (e.g. `outcomes='["Yes", "No"]'`, `outcomePrices='["0.69", "0.31"]')`,
not actual JSON arrays -- they must be `json.loads`'d a second time.
`_parse_binary_market` does this defensively: any market whose fields don't
parse to exactly the two-element `["Yes", "No"]` shape (case-insensitively)
is skipped and counted, never fatal to the run. `volume24hr` is a plain
float (e.g. `2203333.017`) -- no quirk there. Market objects also carry an
`events` array, which this module ignores entirely.

Ranking: volume24hr, not lifetime volume (live-verified correction)
----------------------------------------------------------------------
The request orders by `volume24hr` (money moving in the last 24 hours), NOT
`volumeNum` (lifetime volume). A live check ordering by `volumeNum` showed
the top of that ranking dominated by stale, ancient markets (e.g. "Will
Jesus Christ return before 2027?") that have accumulated huge lifetime
volume but no current activity -- the opposite of the salience signal this
feature exists to surface. `volume24hr` is also the figure quoted in each
swing item's text ("24h volume $X"), for the same reason: lifetime volume
says nothing about whether this market is active RIGHT NOW.

Sports/esports exclusion (live-verified)
----------------------------------------------------------------------
Ranking by `volume24hr` surfaces a flood of live sports/esports markets
(tennis matchups, single Dota games, ...) whose in-game odds swing wildly
and constantly -- exactly the kind of noise this collector must not report
as a "swing worth attention". Live-verified discriminator: a sports/esports
market object carries a `sportsMarketType` key; a real-world event market
does not. Any market where that key is present with a truthy (non-null,
non-empty) value is excluded -- see `_is_sports_market` -- counted in the
same debug-level skip tally as non-binary/malformed markets, never treated
as a failure.

Because this exclusion (and the binary-market filter below it) can each
remove a meaningful fraction of the ranked list, `_fetch_markets` always
requests `_OVERFETCH_LIMIT` (100) markets regardless of `top_n` -- ordering
is volume24hr-descending, so taking the first `top_n` SURVIVORS after
filtering (not the first `top_n` raw entries before filtering) is what
actually yields the top `top_n` genuine, reportable markets. `top_n` itself
governs only this post-filter cut; it never appears in the request's
`limit` query parameter.

v1 scope: binary markets only. A multi-outcome market, or any market whose
`outcomes`/`outcomePrices`/`id`/`question`/`slug` fields don't parse
cleanly, is skipped with a debug-level count -- never logged at warning+
level with the market's own question text, keeping collected content out of
Loki (matching this codebase's logging discipline for scraped content).

Failure semantics -- diverges from rss.py, matches telegram.py/x.py
----------------------------------------------------------------------
There is exactly one "feed" here (`{base}/markets`), unlike rss.py's
per-feed list where one flaky publisher among many is routine and must not
trip the failure banner. Any failure of THIS single request or its
top-level JSON-array parse sets `failed=True` -- the same posture as a
single Telegram chat or the one X notifications timeline failing being the
whole signal (see rss.py's own module docstring for the contrast). Only one
attempt is made per run; there is no retry loop, matching the "gentleness"
posture x.py documents for hitting an unofficial/rate-limited API.
"""

from __future__ import annotations

import json
import logging
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from digest.state import Item

logger = logging.getLogger(__name__)

# Single-request timeout, in seconds. Matches rss.py's _FEED_TIMEOUT_SECONDS
# constant pattern -- a hung proxy/API must not wedge the whole run, it just
# means this collector contributes nothing (and the run is marked failed,
# see module docstring's "Failure semantics") this time.
_REQUEST_TIMEOUT_SECONDS = 15

# Only these two outcome labels, in exactly this order (case-insensitive),
# are treated as a binary Yes/No market -- see module docstring's "v1
# scope". probability is always float(outcomePrices[0]), the "Yes" price.
_BINARY_OUTCOMES = ("yes", "no")

# Sent on every request. Live-verified requirement, not a nicety: Cloudflare's
# edge (Browser Integrity Check) rejects urllib's default
# "Python-urllib/3.x" User-Agent signature with error 1010 BEFORE the
# request ever reaches the owner's proxy Worker -- the same call with this
# UA returns 200. Any stable, honest product identifier passes; what fails
# is specifically the default python-urllib signature.
_USER_AGENT = "notification-digest/1.0"

# Always requested regardless of `top_n` -- see module docstring's "Ranking:
# volume24hr, not lifetime volume" / "Sports/esports exclusion" sections.
# Sports/esports markets and non-binary/malformed entries are filtered out
# of a volume24hr-descending list AFTER the fetch, so getting `top_n`
# genuine candidates requires overfetching a larger raw page than `top_n`
# itself -- 100 is a fixed, generous page size for this purpose (if fewer
# than `top_n` genuine markets survive filtering even out of 100, the run
# simply reports fewer candidates this time; that's not a failure).
_OVERFETCH_LIMIT = 100


@dataclass
class PolymarketCollectResult:
    """Sibling of digest.collectors.base.CollectResult, but for Polymarket.

    Deliberately its own type rather than reusing CollectResult: this
    collector has no cursor axis at all (see module docstring -- its state
    is the `polymarket_probs` anchor table, not a per-scope cursor), and it
    carries `prob_updates` instead, keyed by market_id -> (probability to
    store, question). This is exactly the shape
    digest/state.py's `commit_new_items(..., polymarket_prob_updates=...)`
    keyword parameter expects, so `digest/main.py` can pass this field
    straight through without reshaping it.
    """

    items: list[Item] = field(default_factory=list)
    prob_updates: dict[str, tuple[float, str]] = field(default_factory=dict)
    failed: bool = False


@dataclass(frozen=True)
class _ParsedMarket:
    """One binary market's fields, extracted from one raw Gamma API market object.

    Deliberately flat -- the only shape `collect`'s swing logic consumes
    from `_parse_binary_market`. `end_date` is the raw `endDate` string
    verbatim (or None if absent/malformed) -- `_format_end_date` extracts
    just the date part of it at item-text-building time, not here.
    """

    market_id: str
    question: str
    slug: str
    probability: float
    volume_24hr: float
    end_date: str | None


def _is_sports_market(raw: Any) -> bool:
    """True iff `raw` carries a truthy `sportsMarketType` key (live-verified discriminator).

    See module docstring's "Sports/esports exclusion" section: a real-world
    event market never sets this key at all, while a sports/esports market
    always does, with some non-empty value. `raw.get(...)` being falsy
    (missing, None, `""`, `0`) is treated as "not sports" -- deliberately
    lenient (fails open toward INCLUDING a market) since a false negative
    here just means one extra market goes through the normal binary/anchor
    pipeline, whereas a false positive would silently drop a genuine
    real-world market.
    """
    if not isinstance(raw, dict):
        return False
    return bool(raw.get("sportsMarketType"))


def _parse_binary_market(raw: Any) -> _ParsedMarket | None:
    """Parse one raw Gamma market object into a `_ParsedMarket`, or None.

    Returns None (never raises) for anything that isn't a clean binary
    Yes/No market: a non-dict entry, a missing/blank id, question, or slug,
    an `outcomes`/`outcomePrices` pair that isn't a JSON-encoded string
    array (module docstring's "Gamma API response quirk"), an outcomes list
    that isn't exactly `["Yes", "No"]` case-insensitively, or a first price
    that isn't a number in [0, 1]. Every check below fails open into `None`
    rather than propagating -- `collect`'s caller counts skips, it never
    lets a single malformed market abort the run (module docstring's
    "Failure semantics"). Does NOT itself check `sportsMarketType` --
    `collect` applies `_is_sports_market` as a separate, earlier filter (see
    its own docstring), so this function only ever needs to worry about
    market SHAPE, not market CATEGORY.

    `volume24hr` is defensively coalesced to 0.0 on anything unparsable --
    volume is display-only (the item text's "24h volume $..." clause), not a
    filtering/ordering criterion this module itself applies (the API's own
    `order=volume24hr` query parameter already did that ordering), so a
    missing/malformed volume must not disqualify an otherwise-valid market.
    """
    if not isinstance(raw, dict):
        return None

    market_id = raw.get("id")
    if not isinstance(market_id, (str, int)):
        return None
    market_id = str(market_id)

    question = raw.get("question")
    if not isinstance(question, str) or not question:
        return None

    slug = raw.get("slug")
    if not isinstance(slug, str) or not slug:
        return None

    outcomes_raw = raw.get("outcomes")
    prices_raw = raw.get("outcomePrices")
    if not isinstance(outcomes_raw, str) or not isinstance(prices_raw, str):
        return None
    try:
        outcomes = json.loads(outcomes_raw)
        prices = json.loads(prices_raw)
    except (json.JSONDecodeError, TypeError):
        return None

    if not isinstance(outcomes, list) or not isinstance(prices, list):
        return None
    if len(outcomes) != 2 or len(prices) != 2:
        return None
    normalized_outcomes = tuple(
        o.strip().lower() if isinstance(o, str) else None for o in outcomes
    )
    if normalized_outcomes != _BINARY_OUTCOMES:
        return None

    try:
        probability = float(prices[0])
    except (TypeError, ValueError):
        return None
    if not (0.0 <= probability <= 1.0):
        return None

    try:
        volume_24hr = float(raw.get("volume24hr"))
    except (TypeError, ValueError):
        volume_24hr = 0.0

    end_date = raw.get("endDate")
    end_date = end_date if isinstance(end_date, str) and end_date else None

    return _ParsedMarket(
        market_id=market_id,
        question=question,
        slug=slug,
        probability=probability,
        volume_24hr=volume_24hr,
        end_date=end_date,
    )


def _format_end_date(end_date: str | None) -> str:
    """The date-only portion of a raw `endDate` string, or "unknown".

    Gamma's `endDate` is an ISO8601 timestamp (e.g.
    "2026-12-31T00:00:00Z") -- the item text only wants the date, per the
    feature spec's "(24h volume $..., resolves {endDate date part})"
    wording. Splitting on the first "T" is intentionally cheap: a full
    datetime.fromisoformat round-trip buys nothing here since the text is
    display-only, and this still degrades gracefully (returns the whole
    string) for a value with no "T" at all rather than raising.
    """
    if not end_date:
        return "unknown"
    return end_date.split("T", 1)[0]


def _build_item(market: _ParsedMarket, prior_probability: float, fetched_at: str) -> Item:
    """Build the digest Item for one confirmed swing.

    `source_id` embeds `fetched_at` (this run's own timestamp) alongside
    the market id -- per the feature spec, this is deliberate: successive
    swings on the SAME market across different runs must each get their own
    row (a market can swing more than once across its lifetime), and
    UNIQUE(source, source_id) (digest/state.py) remains the idempotency
    backstop against this exact function somehow running twice for the same
    run.
    """
    text = (
        f'Prediction market swing: "{market.question}" moved '
        f"{prior_probability:.0%} -> {market.probability:.0%} "
        f"(24h volume ${market.volume_24hr:,.0f}, resolves {_format_end_date(market.end_date)})."
    )
    return Item(
        source="polymarket",
        source_id=f"{market.market_id}:{fetched_at}",
        chat_id=None,
        chat_title="Polymarket",
        author=None,
        text=text,
        url=f"https://polymarket.com/market/{market.slug}",
        fetched_at=fetched_at,
    )


def _fetch_markets(api_base: str, proxy_key: str | None) -> list[Any]:
    """One GET to `{api_base}/markets`, returning the raw parsed JSON array.

    Always requests `_OVERFETCH_LIMIT` (100) markets, ordered by
    `volume24hr` descending -- never `top_n`, and never `volumeNum` (see
    module docstring's "Ranking" / "Sports/esports exclusion" sections for
    why both of those would be wrong here). `top_n` is applied later, by
    `collect`, as a post-filter cut over the parsed/kept survivors.

    Raises on any failure (network error, non-2xx status, non-JSON body, a
    JSON body that isn't a list) -- deliberately unguarded here, matching
    rss.py's `_fetch_and_parse_one_feed`: the caller (`collect`) wraps this
    single call in the try/except that sets `failed=True` (module
    docstring's "Failure semantics" -- there is only one "feed" in this
    collector, so any failure here is the whole run's signal).

    Sends `accept: application/json` always, and `x-proxy-key: {proxy_key}`
    only when one is configured (module docstring's "Network context" --
    the header authenticates to the owner's own Cloudflare Worker proxy,
    not to Polymarket itself, and is never logged).
    """
    url = (
        f"{api_base}/markets?closed=false&active=true"
        f"&order=volume24hr&ascending=false&limit={_OVERFETCH_LIMIT}"
    )
    headers = {"accept": "application/json", "user-agent": _USER_AGENT}
    if proxy_key:
        headers["x-proxy-key"] = proxy_key
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=_REQUEST_TIMEOUT_SECONDS) as response:
        raw_bytes = response.read()
    data = json.loads(raw_bytes)
    if not isinstance(data, list):
        raise ValueError("polymarket /markets response was not a JSON array")
    return data


def collect(
    api_base: str,
    proxy_key: str | None,
    top_n: int,
    threshold: float,
    get_stored_probs: Callable[[Sequence[str]], dict[str, float]],
) -> PolymarketCollectResult:
    """Fetch the top-N active markets by volume and emit an Item for each one that swung.

    `get_stored_probs` is injected rather than a live `sqlite3.Connection`
    passed straight through -- this collector, like rss.py/telegram.py/x.py,
    must stay testable purely against a fake urllib.request.urlopen, with no
    real database in the loop. The caller (digest/main.py's
    `_run_polymarket_collector`) wires this to
    `lambda market_ids: get_polymarket_probs(conn, market_ids)`. It is
    called with exactly the market ids this run actually fetched (not the
    full historical table) -- there is no way to know which markets are
    "this run's top N" before fetching them, so the stored-anchor lookup
    necessarily happens AFTER the request, not alongside `cursors` the way
    telegram.py/x.py pre-fetch their cursor dict before any network call.

    One GET per run (`_fetch_markets`, always overfetching `_OVERFETCH_LIMIT`
    markets ordered by volume24hr -- see that function's and the module
    docstring's "Ranking"/"Sports/esports exclusion" sections); ANY failure
    of it sets `result.failed = True` and returns immediately with no
    items/updates -- see module docstring's "Failure semantics". A
    malformed top-level response (not a JSON array) is treated identically,
    since `_fetch_markets` itself raises for that case too.

    Each raw market is first checked with `_is_sports_market` (excluded,
    counted, never fatal) and then parsed via `_parse_binary_market`
    (anything that isn't a clean binary Yes/No market is likewise skipped
    and counted) -- both skip categories share one debug-level tally, never
    logged with the market's own question text (module docstring's "v1
    scope"). Survivors are kept in the API's own volume24hr-descending
    order and truncated to the first `top_n` -- `top_n` is a POST-filter
    cut, never part of the request itself (module docstring's "Sports/
    esports exclusion", overfetch rationale). An empty survivor set (e.g.
    every market this run happened to be sports or non-binary) returns a
    fresh, unfailed result -- that is not itself a failure, just a quiet
    run.

    For each successfully parsed market, compares its current probability
    against `get_stored_probs`' anchor (module docstring, "The anchor
    rule"):

    - No prior anchor at all (first sighting) -> record `(current,
      question)` as the new baseline in `prob_updates`, emit NO item.
    - `abs(current - prior) >= threshold` -> emit an Item (`_build_item`)
      AND record `(current, question)` in `prob_updates` -- the anchor
      moves to what was just reported.
    - Otherwise -> record `(prior, question)` in `prob_updates` -- the
      PRIOR value, unchanged, not `current`. This looks like a no-op on
      `probability` but is not: digest/state.py's `commit_new_items` upserts
      every entry in `prob_updates` unconditionally, including
      `updated_at`, so this still refreshes "last observed" (used for the
      30-day prune) even though "last reported" (the anchor value itself)
      correctly stays put. See digest/state.py's `polymarket_probs`
      schema comment for this same "probability = last reported, updated_at
      = last observed" split stated from the storage side.
    """
    fetched_at = datetime.now(UTC).isoformat()

    try:
        raw_markets = _fetch_markets(api_base, proxy_key)
    except Exception as exc:
        logger.warning("polymarket fetch failed: %s", type(exc).__name__)
        return PolymarketCollectResult(failed=True)

    parsed: list[_ParsedMarket] = []
    skipped = 0
    for raw in raw_markets:
        if len(parsed) >= top_n:
            break
        if _is_sports_market(raw):
            skipped += 1
            continue
        market = _parse_binary_market(raw)
        if market is None:
            skipped += 1
            continue
        parsed.append(market)
    if skipped:
        logger.debug("polymarket: skipped %d sports/non-binary/malformed markets", skipped)

    if not parsed:
        return PolymarketCollectResult()

    stored_probs = get_stored_probs([market.market_id for market in parsed])

    result = PolymarketCollectResult()
    for market in parsed:
        prior = stored_probs.get(market.market_id)
        if prior is None:
            result.prob_updates[market.market_id] = (market.probability, market.question)
            continue
        if abs(market.probability - prior) >= threshold:
            result.items.append(_build_item(market, prior, fetched_at))
            result.prob_updates[market.market_id] = (market.probability, market.question)
        else:
            result.prob_updates[market.market_id] = (prior, market.question)

    return result

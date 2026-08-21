"""Tests for digest/collectors/polymarket.py -- no network, ever.

`urllib.request.urlopen` is monkeypatched to a fake that resolves to
pre-built JSON bytes or an exception to raise, mirroring
tests/test_rss_collector.py's mocking discipline. `get_stored_probs` (the
collector's injected anchor-lookup callable) is a plain lambda/closure over
an in-memory dict in every test -- no real sqlite3.Connection is ever
involved, matching the collector's own design (see its module docstring).
"""

from __future__ import annotations

import json
import urllib.error

import pytest

import digest.collectors.polymarket as polymarket_module
from digest.collectors.polymarket import PolymarketCollectResult, collect


class _FakeResponse:
    def __init__(self, data: bytes) -> None:
        self._data = data

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._data


def _patch_urlopen(
    monkeypatch: pytest.MonkeyPatch,
    response_body: bytes | Exception,
    captured: dict | None = None,
) -> None:
    """Fake urlopen returning `response_body` (or raising it, if an Exception).

    When `captured` is given, the raw `urllib.request.Request` object built
    by the collector is stashed under `captured["request"]` -- lets tests
    inspect the exact URL/headers sent without needing a real HTTP stack.
    """

    def fake_urlopen(request: object, timeout: float | None = None) -> _FakeResponse:
        if captured is not None:
            captured["request"] = request
        if isinstance(response_body, Exception):
            raise response_body
        return _FakeResponse(response_body)

    monkeypatch.setattr(polymarket_module.urllib.request, "urlopen", fake_urlopen)


def _market(
    market_id: str = "m1",
    question: str = "Will X happen?",
    slug: str = "will-x-happen",
    outcomes: tuple[str, str] = ("Yes", "No"),
    prices: tuple[str, str] = ("0.60", "0.40"),
    volume_24hr: float = 100000.0,
    end_date: str | None = "2026-12-31T00:00:00Z",
    sports_market_type: str | None = None,
) -> dict:
    """One raw Gamma market object, matching the live API's field shapes.

    `outcomes`/`outcomePrices` are JSON-ENCODED STRINGS on the real API
    (module docstring's "Gamma API response quirk") -- this fixture
    reproduces that quirk faithfully via json.dumps, rather than handing the
    collector actual list objects, so tests exercise the real double-decode
    path.
    """
    raw: dict = {
        "id": market_id,
        "question": question,
        "slug": slug,
        "outcomes": json.dumps(list(outcomes)),
        "outcomePrices": json.dumps(list(prices)),
        "volume24hr": volume_24hr,
        "endDate": end_date,
        "events": [{"id": "irrelevant"}],  # present on real markets, always ignored
    }
    if sports_market_type is not None:
        raw["sportsMarketType"] = sports_market_type
    return raw


def _no_stored_probs(market_ids):
    return {}


# --- binary market parsing / item text ---


def test_binary_market_swing_emits_item_with_expected_text_and_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(
        market_id="m1",
        question="Will X happen?",
        slug="will-x-happen",
        prices=("0.60", "0.40"),
        volume_24hr=2203333.017,
        end_date="2026-12-31T00:00:00Z",
    )
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://gamma-api.polymarket.com", None, 30, 0.15, lambda ids: {"m1": 0.40})

    assert result.failed is False
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source == "polymarket"
    assert item.chat_id is None
    assert item.chat_title == "Polymarket"
    assert item.author is None
    assert item.url == "https://polymarket.com/market/will-x-happen"
    assert item.source_id.startswith("m1:")
    assert "moved 40% -> 60%" in item.text
    assert "24h volume $2,203,333" in item.text
    assert "resolves 2026-12-31" in item.text
    assert result.prob_updates == {"m1": (0.60, "Will X happen?")}


def test_outcomes_and_prices_are_double_decoded_json_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Explicit regression for the Gamma quirk: outcomes/outcomePrices must be
    # JSON-encoded STRINGS, not actual arrays -- a market shipping them as
    # real lists (the "naive" shape) is malformed by this API's own contract
    # and must be skipped, not silently accepted.
    market = _market(market_id="m1")
    market["outcomes"] = ["Yes", "No"]  # real list, not a JSON-encoded string
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result.items == []
    assert result.prob_updates == {}


def test_missing_end_date_formats_as_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    market = _market(market_id="m1", prices=("0.60", "0.40"), end_date=None)
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://api.example", None, 30, 0.15, lambda ids: {"m1": 0.40})

    assert "resolves unknown" in result.items[0].text


# --- non-binary / malformed skipped, never fatal ---


def test_non_binary_and_malformed_markets_are_skipped(monkeypatch: pytest.MonkeyPatch) -> None:
    multi_outcome = _market(market_id="multi", outcomes=("Yes", "No", "Maybe"))
    missing_question = _market(market_id="noq")
    del missing_question["question"]
    bad_price = _market(market_id="badprice", prices=("not-a-number", "0.5"))
    good = _market(market_id="good")

    _patch_urlopen(
        monkeypatch,
        json.dumps([multi_outcome, missing_question, bad_price, good]).encode(),
    )

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result.failed is False
    assert list(result.prob_updates.keys()) == ["good"]


def test_sports_market_is_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    sports_market = _market(market_id="sport1", sports_market_type="tennis")
    real_market = _market(market_id="real1")
    _patch_urlopen(monkeypatch, json.dumps([sports_market, real_market]).encode())

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert list(result.prob_updates.keys()) == ["real1"]


def test_sports_market_type_falsy_value_is_not_excluded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Presence with an empty/falsy value must NOT be treated as sports (see
    # _is_sports_market's fail-open-toward-inclusion rationale).
    market = _market(market_id="m1")
    market["sportsMarketType"] = ""
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert list(result.prob_updates.keys()) == ["m1"]


def test_all_markets_filtered_out_returns_fresh_unfailed_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sports_market = _market(market_id="sport1", sports_market_type="tennis")
    _patch_urlopen(monkeypatch, json.dumps([sports_market]).encode())

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result == PolymarketCollectResult()


# --- anchor rule: first sighting, swing, below-threshold drift ---


def test_first_sighting_records_baseline_and_emits_no_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(market_id="m1", prices=("0.55", "0.45"))
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result.items == []
    assert result.prob_updates == {"m1": (0.55, "Will X happen?")}


def test_swing_exactly_at_threshold_emits_item_and_moves_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(market_id="m1", prices=("0.55", "0.45"))
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://api.example", None, 30, 0.15, lambda ids: {"m1": 0.40})

    assert len(result.items) == 1
    assert result.prob_updates == {"m1": (0.55, "Will X happen?")}


def test_swing_below_threshold_emits_nothing_and_leaves_anchor_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    market = _market(market_id="m1", prices=("0.48", "0.52"))
    _patch_urlopen(monkeypatch, json.dumps([market]).encode())

    result = collect("https://api.example", None, 30, 0.15, lambda ids: {"m1": 0.40})

    assert result.items == []
    # The PRIOR value (0.40), not current (0.48) -- the anchor must not move.
    assert result.prob_updates == {"m1": (0.40, "Will X happen?")}


def test_drift_accumulates_against_fixed_anchor_across_three_runs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """0.40 (baseline) -> 0.48 (no item, anchor stays 0.40) -> 0.57 (item, vs the 0.40 anchor)."""
    stored_probs: dict[str, float] = {}

    def get_stored_probs(market_ids):
        return {mid: stored_probs[mid] for mid in market_ids if mid in stored_probs}

    def _run(price_yes: str, price_no: str) -> PolymarketCollectResult:
        market = _market(market_id="m1", prices=(price_yes, price_no))
        _patch_urlopen(monkeypatch, json.dumps([market]).encode())
        result = collect("https://api.example", None, 30, 0.15, get_stored_probs)
        for mid, (prob, _question) in result.prob_updates.items():
            stored_probs[mid] = prob
        return result

    step1 = _run("0.40", "0.60")
    assert step1.items == []
    assert stored_probs == {"m1": 0.40}

    step2 = _run("0.48", "0.52")  # drift of 0.08 -- below the 0.15 threshold
    assert step2.items == []
    assert stored_probs == {"m1": 0.40}  # anchor did NOT move

    step3 = _run("0.57", "0.43")  # 0.57 - 0.40 = 0.17 >= 0.15, vs the ORIGINAL anchor
    assert len(step3.items) == 1
    assert "moved 40% -> 57%" in step3.items[0].text
    assert stored_probs == {"m1": 0.57}  # anchor now moves to the reported value


# --- request shape: proxy header, overfetch limit, top_n post-filter cut ---


def test_proxy_header_present_only_when_key_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _patch_urlopen(monkeypatch, json.dumps([]).encode(), captured=captured)
    collect("https://api.example", "secret123", 30, 0.15, _no_stored_probs)
    assert captured["request"].get_header("X-proxy-key") == "secret123"

    captured.clear()
    _patch_urlopen(monkeypatch, json.dumps([]).encode(), captured=captured)
    collect("https://api.example", None, 30, 0.15, _no_stored_probs)
    assert captured["request"].get_header("X-proxy-key") is None


def test_request_always_overfetches_limit_100_ordered_by_volume24hr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict = {}
    _patch_urlopen(monkeypatch, json.dumps([]).encode(), captured=captured)

    collect("https://api.example", None, 5, 0.15, _no_stored_probs)

    url = captured["request"].full_url
    assert "limit=100" in url
    assert "order=volume24hr" in url
    assert "ascending=false" in url
    assert "limit=5" not in url


def test_top_n_governs_post_filter_cut_not_the_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    markets = [_market(market_id=f"m{i}", prices=("0.5", "0.5")) for i in range(5)]
    _patch_urlopen(monkeypatch, json.dumps(markets).encode())

    result = collect("https://api.example", None, 2, 0.15, _no_stored_probs)

    assert len(result.prob_updates) == 2


# --- failure semantics: single endpoint, any failure marks the run failed ---


def test_request_failure_marks_result_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, urllib.error.URLError("connection refused"))

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result.failed is True
    assert result.items == []
    assert result.prob_updates == {}


def test_non_json_array_top_level_response_marks_result_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_urlopen(monkeypatch, json.dumps({"not": "a list"}).encode())

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result.failed is True


def test_non_json_body_marks_result_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_urlopen(monkeypatch, b"not json at all")

    result = collect("https://api.example", None, 30, 0.15, _no_stored_probs)

    assert result.failed is True

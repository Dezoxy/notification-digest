"""Tests for the Patreon collector.

Every fixture below mirrors a real response observed on 2026-08-21 -- the
authorized shape, and the unauthenticated control (200 with posts listed
but `current_user_can_view` false and empty bodies), which is the case the
collector exists to distinguish. No network is touched: `_fetch_page` is
the module's only external edge and is monkeypatched, mirroring how
CLAUDE.md requires the Telegram and X collectors to be mocked.
"""

from __future__ import annotations

import json

import pytest

from digest.collectors import patreon
from digest.collectors.patreon import (
    PatreonUnavailable,
    _authorization_state,
    _item_from_post,
    _prosemirror_text,
    collect,
)

BODY_DOC = json.dumps(
    {
        "type": "doc",
        "content": [
            {
                "type": "paragraph",
                "content": [
                    {"type": "text", "marks": [{"type": "bold"}], "text": "Kedves VIP Közösség,"}
                ],
            },
            {"type": "paragraph", "content": [{"type": "text", "text": "A piac ma emelkedett."}]},
        ],
    }
)


def make_post(
    post_id="167226458", can_view=True, body=BODY_DOC, title="Megszólalt a kripto harang"
):
    return {
        "id": post_id,
        "type": "post",
        "attributes": {
            "title": title,
            "content_json_string": body,
            "published_at": "2026-08-20T20:46:04.000+00:00",
            "url": f"https://www.patreon.com/kriptovadasz/posts/megszolalt-{post_id}",
            "post_type": "poll",
            "current_user_can_view": can_view,
        },
    }


class TestAuthorizationState:
    def test_viewable_posts_are_ok(self):
        assert _authorization_state([make_post()]) == "ok"

    def test_the_unauthenticated_control_shape_is_unauthorized(self):
        # Verified live: no cookie still returns 200 with posts listed.
        # This must never read as "nothing new".
        assert _authorization_state([make_post(can_view=False)]) == "unauthorized"

    def test_no_posts_at_all_is_a_trustworthy_empty(self):
        # Unambiguous precisely because an unauthorized request still
        # LISTS posts -- an empty page cannot be a masked auth failure.
        assert _authorization_state([]) == "empty"

    def test_a_mixed_page_is_ok(self):
        # One higher-tier post the pledge doesn't cover alongside one it
        # does: the run is healthy, the locked post gets dropped later.
        posts = [make_post("1", can_view=False), make_post("2", can_view=True)]
        assert _authorization_state(posts) == "ok"

    def test_missing_attributes_do_not_count_as_viewable(self):
        assert _authorization_state([{"id": "1"}]) == "unauthorized"


class TestProsemirrorText:
    def test_flattens_paragraphs_with_breaks(self):
        out = _prosemirror_text(BODY_DOC)
        assert "Kedves VIP Közösség," in out
        assert "A piac ma emelkedett." in out
        assert "\n\n" in out

    def test_preserves_hungarian_characters(self):
        assert "Közösség" in _prosemirror_text(BODY_DOC)

    def test_unparseable_body_degrades_to_empty_not_an_exception(self):
        # A single post's formatting must never fail the run.
        assert _prosemirror_text("not json") == ""
        assert _prosemirror_text("") == ""

    def test_nested_marks_and_lists_are_walked(self):
        doc = json.dumps(
            {
                "type": "doc",
                "content": [
                    {
                        "type": "bullet_list",
                        "content": [
                            {
                                "type": "list_item",
                                "content": [
                                    {
                                        "type": "paragraph",
                                        "content": [{"type": "text", "text": "BTC"}],
                                    }
                                ],
                            }
                        ],
                    }
                ],
            }
        )
        assert "BTC" in _prosemirror_text(doc)


class TestItemFromPost:
    def test_builds_an_item_keyed_on_the_patreon_post_id(self):
        item = _item_from_post(make_post(), "2026-08-21T16:00:00+00:00")
        assert item is not None
        assert item.source == "patreon"
        assert item.source_id == "167226458"
        assert "Kedves VIP" in item.text
        assert item.url.startswith("https://www.patreon.com/")

    def test_unviewable_post_is_dropped_rather_than_delivered_as_a_teaser(self):
        assert _item_from_post(make_post(can_view=False), "t") is None

    def test_title_only_post_is_still_usable(self):
        item = _item_from_post(make_post(body="not json"), "t")
        assert item is not None
        assert item.text == "Megszólalt a kripto harang"

    def test_post_with_neither_title_nor_body_is_dropped(self):
        assert _item_from_post(make_post(title="", body="not json"), "t") is None

    def test_non_patreon_url_is_dropped(self):
        post = make_post()
        post["attributes"]["url"] = "https://evil.example/x"
        assert _item_from_post(post, "t") is None

    def test_body_is_truncated_to_the_prompt_budget(self):
        huge = json.dumps(
            {
                "type": "doc",
                "content": [
                    {"type": "paragraph", "content": [{"type": "text", "text": "x" * 50_000}]}
                ],
            }
        )
        item = _item_from_post(make_post(body=huge), "t")
        assert item is not None
        assert len(item.text) <= patreon._MAX_BODY_CHARS + 200


class TestCollect:
    @staticmethod
    def _patch(monkeypatch, result):
        def fake(campaign_id, session_cookie):
            if isinstance(result, Exception):
                raise result
            return result

        monkeypatch.setattr(patreon, "_fetch_page", fake)

    def test_returns_only_posts_not_already_stored(self, monkeypatch):
        self._patch(monkeypatch, [make_post("1"), make_post("2"), make_post("3")])
        out = collect("7095842", "cookie", lambda ids: {"1", "3"})
        assert [i.source_id for i in out.items] == ["2"]
        assert out.failed is False

    def test_quiet_run_is_not_a_failure(self, monkeypatch):
        self._patch(monkeypatch, [make_post("1")])
        out = collect("7095842", "cookie", lambda ids: {"1"})
        assert out.items == []
        assert out.failed is False

    def test_unauthorized_page_fails_the_run(self, monkeypatch):
        self._patch(monkeypatch, [make_post("1", can_view=False)])
        out = collect("7095842", "cookie", lambda ids: set())
        assert out.failed is True
        assert out.items == []

    def test_transport_failure_fails_the_run(self, monkeypatch):
        self._patch(monkeypatch, PatreonUnavailable("patreon api returned HTTP 403"))
        out = collect("7095842", "cookie", lambda ids: set())
        assert out.failed is True

    def test_no_cursor_updates_are_ever_emitted(self, monkeypatch):
        self._patch(monkeypatch, [make_post("1")])
        assert collect("7095842", "cookie", lambda ids: set()).cursor_updates == {}

    def test_lookup_is_asked_only_about_ids_actually_listed(self, monkeypatch):
        seen = {}
        self._patch(monkeypatch, [make_post("1"), make_post("2")])

        def lookup(ids):
            seen["ids"] = list(ids)
            return set()

        collect("7095842", "cookie", lookup)
        assert seen["ids"] == ["1", "2"]


class TestFetchPageErrorHandling:
    def test_http_error_message_carries_status_but_never_the_body(self, monkeypatch):
        import urllib.error

        def boom(request, timeout):
            raise urllib.error.HTTPError(
                "https://www.patreon.com/api/posts", 403, "Forbidden", {}, None
            )

        monkeypatch.setattr(patreon.urllib.request, "urlopen", boom)
        with pytest.raises(PatreonUnavailable) as exc:
            patreon._fetch_page("7095842", "secret-cookie-value")
        message = str(exc.value)
        assert "403" in message
        assert "secret-cookie-value" not in message

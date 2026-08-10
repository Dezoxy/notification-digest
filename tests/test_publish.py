import json
import re
import urllib.error
from pathlib import Path

import pytest

import digest.publish as publish_mod
from digest.publish import (
    TelegramSendError,
    count_sections,
    derive_topics,
    extract_tldr,
    has_needs_attention,
    map_deltas_to_slugs,
    parse_failed_sources,
    publish_to_site,
    section_link_targets,
    send_telegram_tldr,
)

# --- extract_tldr ---


def test_extract_tldr_normal_case_strips_marker_and_links():
    body_md = (
        "**TL;DR:** Big thing happened [¹](https://t.me/c/1/1) and it matters.\n\n"
        "## Worth knowing\n\nmore detail here.\n"
    )

    assert extract_tldr(body_md) == "Big thing happened and it matters."


def test_extract_tldr_banner_prefixed_still_finds_tldr():
    body_md = (
        "⚠ telegram collection failed this run\n\n"
        "**TL;DR:** Everything is fine.\n\n"
        "## Worth knowing\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "Everything is fine."


def test_extract_tldr_multiple_banner_lines_are_all_skipped():
    body_md = (
        "⚠ telegram collection failed this run\n"
        "⚠ x collection failed this run\n\n"
        "**TL;DR:** All good.\n\n## Section\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "All good."


def test_extract_tldr_alternate_colon_placement_is_handled():
    body_md = "**TL;DR**: Alternate colon placement.\n\n## Section\n\nmore.\n"

    assert extract_tldr(body_md) == "Alternate colon placement."


def test_extract_tldr_drops_superscript_citation_chips():
    body_md = "**TL;DR:** Thing happened[¹](https://a) and also[²³](https://b).\n\n## S\n\nx\n"

    result = extract_tldr(body_md)

    assert "¹" not in result
    assert "²" not in result
    assert "³" not in result
    assert "happened" in result
    assert "also" in result


def test_extract_tldr_strips_bold_emphasis_inside_the_tldr_sentence():
    # Production finding (live site index, digest #54): the model can put
    # bold markdown INSIDE the TL;DR paragraph itself, not just as the
    # `**TL;DR:**` marker -- that inner `**...**` must not reach the site
    # excerpt or Telegram message as literal asterisks.
    body_md = (
        "**TL;DR:** Revenue could hit **$100B ARR by year end**, analysts say.\n\n## S\n\nx\n"
    )

    result = extract_tldr(body_md)

    assert "*" not in result
    assert result == "Revenue could hit $100B ARR by year end, analysts say."


def test_extract_tldr_preserves_underscore_handles():
    # A lone `_` inside a Telegram/X handle or snake_case identifier must
    # survive untouched -- only the bold `__...__` form and single `*...*`
    # emphasis are stripped, never a single underscore.
    body_md = "**TL;DR:** Big update from @user_name on the platform.\n\n## S\n\nx\n"

    assert extract_tldr(body_md) == "Big update from @user_name on the platform."


def test_extract_tldr_strips_single_asterisk_emphasis():
    body_md = "**TL;DR:** The *actual* number surprised everyone.\n\n## S\n\nx\n"

    result = extract_tldr(body_md)

    assert "*" not in result
    assert result == "The actual number surprised everyone."


def test_extract_tldr_needs_attention_section_before_tldr_is_skipped():
    # The prompt contract places "## Needs attention" ABOVE the TL;DR
    # paragraph -- extract_tldr must search past it, not stop at the first
    # paragraph found.
    body_md = (
        "## Needs attention\n\nSomething urgent needs a reply.\n\n"
        "**TL;DR:** The actual summary.\n\n## Worth knowing\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "The actual summary."


def test_extract_tldr_missing_tldr_falls_back_to_first_paragraph_capped_at_300(monkeypatch):
    long_para = "x" * 400
    body_md = f"## Worth knowing\n\n{long_para}\n\n## Other\n\nmore.\n"

    result = extract_tldr(body_md)

    assert result == "x" * 300


def test_extract_tldr_fallback_skips_headings_and_banners():
    body_md = (
        "⚠ telegram collection failed this run\n\n"
        "## Worth knowing\n\nThe real first paragraph.\n\n## Other\n\nmore.\n"
    )

    assert extract_tldr(body_md) == "The real first paragraph."


def test_extract_tldr_empty_body_returns_empty_string():
    assert extract_tldr("") == ""


def test_extract_tldr_never_raises_on_pathological_input():
    assert extract_tldr("#" * 10000) == ""
    assert extract_tldr("\n\n\n") == ""


# --- count_sections / has_needs_attention ---


def test_count_sections_counts_real_h2_headings_excluding_needs_attention():
    body_md = (
        "**TL;DR:** hi\n\n"
        "## Needs attention\n\nurgent\n\n"
        "## Story one\n\ntext\n\n"
        "## Story two\n\ntext\n"
    )

    assert count_sections(body_md) == 2
    assert has_needs_attention(body_md) is True


def test_count_sections_zero_when_no_headings():
    assert count_sections("just some prose, no headings at all") == 0
    assert has_needs_attention("just some prose") is False


def test_count_sections_ignores_headings_inside_fenced_code_blocks():
    # Reuses summarize._real_heading_lines, which is fence-aware -- a ##
    # heading-shaped line inside a fenced block must not count as a real
    # section (see digest/summarize.py's validate_output docstring).
    body_md = "## Real section\n\n```\n## Not a real heading\n```\n\n## Another real one\n"

    assert count_sections(body_md) == 2


def test_has_needs_attention_case_insensitive():
    assert has_needs_attention("## NEEDS ATTENTION\n\ntext\n") is True


# --- section_link_targets ---


def test_section_link_targets_eight_sections_returns_first_three_with_correct_numbers():
    body_md = "\n\n".join(f"## Section {i}\n\ntext" for i in range(1, 9))

    assert section_link_targets(body_md) == [
        ("Section 1", "s1"),
        ("Section 2", "s2"),
        ("Section 3", "s3"),
    ]


def test_section_link_targets_needs_attention_first_consumes_no_number():
    # "Needs attention" sits FIRST but must not consume s1 -- the site's
    # worker.js splits it into its own div.attention before assigning
    # ids to the remaining h2s, so the first real content section is s1.
    body_md = (
        "## Needs attention\n\nurgent\n\n"
        "## Story one\n\ntext\n\n"
        "## Story two\n\ntext\n\n"
        "## Story three\n\ntext\n\n"
        "## Story four\n\ntext\n"
    )

    assert section_link_targets(body_md) == [
        ("Story one", "s1"),
        ("Story two", "s2"),
        ("Story three", "s3"),
    ]


def test_section_link_targets_backticked_heading_anywhere_returns_empty():
    # A heading containing inline-markup-shaped punctuation renders on the
    # site with nested tags (no id, no number assigned) -- ANY such heading
    # anywhere desyncs every later anchor, so the fail-safe guard drops
    # section links for the whole digest, not just the offending heading.
    body_md = (
        "## Story one\n\ntext\n\n"
        "## `Story two`\n\ntext\n\n"
        "## Story three\n\ntext\n"
    )

    assert section_link_targets(body_md) == []


def test_section_link_targets_fenced_fake_heading_ignored():
    # Mirrors test_summarize's fence-awareness style: a heading-shaped line
    # (backticks and all) inside a fenced code block is not a real heading
    # at all, so it neither counts toward numbering nor trips the inline-
    # markup fail-safe guard.
    body_md = "## Real section\n\n```\n## `Not a real heading`\n```\n\n## Another real one\n"

    assert section_link_targets(body_md) == [
        ("Real section", "s1"),
        ("Another real one", "s2"),
    ]


def test_section_link_targets_no_headings_returns_empty():
    assert section_link_targets("just some prose, no headings at all") == []


# --- parse_failed_sources ---


def test_parse_failed_sources_no_banner_returns_empty_list():
    body_md = "**TL;DR:** hi\n\n## Worth knowing\n\nstuff\n"

    assert parse_failed_sources(body_md) == []


def test_parse_failed_sources_one_banner_line():
    body_md = "⚠ telegram collection failed this run\n\n**TL;DR:** hi\n\n## S\n\nx\n"

    assert parse_failed_sources(body_md) == ["telegram"]


def test_parse_failed_sources_two_banner_lines_preserve_order():
    body_md = (
        "⚠ telegram collection failed this run\n"
        "⚠ x collection failed this run\n\n"
        "**TL;DR:** hi\n\n## S\n\nx\n"
    )

    assert parse_failed_sources(body_md) == ["telegram", "x"]


def test_parse_failed_sources_lookalike_not_at_head_is_ignored():
    # A banner-shaped line that shows up later in the body (e.g. inside the
    # model's own output) must never be picked up -- only the genuine,
    # code-generated block at the very START of body_md counts.
    body_md = (
        "**TL;DR:** hi\n\n"
        "## Worth knowing\n\n"
        "⚠ x collection failed this run\n"
    )

    assert parse_failed_sources(body_md) == []


def test_parse_failed_sources_empty_body_returns_empty_list():
    assert parse_failed_sources("") == []


# --- derive_topics ---


def test_derive_topics_basic_multi_section_digest():
    body_md = (
        "**TL;DR:** hi\n\n"
        "## Story one\n\ntext\n\n"
        "## Story two\n\ntext\n"
    )

    assert derive_topics(body_md) == [
        {"slug": "story-one", "label": "Story one"},
        {"slug": "story-two", "label": "Story two"},
    ]


def test_derive_topics_ignores_headings_inside_fenced_code_blocks():
    # Mirrors test_count_sections_ignores_headings_inside_fenced_code_blocks
    # and test_section_link_targets_fenced_fake_heading_ignored -- reuses
    # _real_heading_lines, so a ## line inside a fence is not a real
    # heading and must not become a topic.
    body_md = "## Real section\n\n```\n## Not a real heading\n```\n\n## Another real one\n"

    assert derive_topics(body_md) == [
        {"slug": "real-section", "label": "Real section"},
        {"slug": "another-real-one", "label": "Another real one"},
    ]


def test_derive_topics_excludes_needs_attention():
    body_md = (
        "## Needs attention\n\nurgent\n\n"
        "## Story one\n\ntext\n"
    )

    assert derive_topics(body_md) == [{"slug": "story-one", "label": "Story one"}]


def test_derive_topics_excludes_structural_rubric_headings():
    # Every fixed, prompt-mandated section across all three briefing
    # prompts -- "Also this window" (prompts/digest.md), "Also today"
    # (prompts/daily.md), "Also this week" and "Watching next week"
    # (prompts/weekly.md), and the "Hungary" standing rule (all three) --
    # is prompt-mandated, not model-chosen, so none may become a topic,
    # mirroring the Needs attention exclusion above. A single body mixing
    # all five is not a shape any one prompt produces; it is deliberately
    # the union, so this test fails if any single entry is dropped.
    body_md = (
        "## Story one\n\ntext\n\n"
        "## Also this window\n\nsecond tier\n\n"
        "## Also today\n\nminor items\n\n"
        "## Also this week\n\nweekly second tier\n\n"
        "## Hungary\n\nquiet day in Hungarian threads\n\n"
        "## Story two\n\nmore text\n\n"
        "## Watching next week\n\nwatchlist\n"
    )

    assert derive_topics(body_md) == [
        {"slug": "story-one", "label": "Story one"},
        {"slug": "story-two", "label": "Story two"},
    ]


def test_derive_topics_excludes_also_this_window_the_window_prompt_rubric():
    # Regression, owner-reported 2026-08-10: the site rendered a live
    # "ALSO THIS WINDOW ×5 THIS WEEK" story-arc chip. Window digests run
    # every 3 hours, so this rubric recurs faster than any real story and
    # was the most visible false arc of the set. Called out on its own
    # (rather than only inside the union above) because the window prompt
    # is the highest-frequency producer in the system.
    body_md = "## Also this window\n\nsecond-tier prose\n\n## Real story\n\ntext\n"

    assert derive_topics(body_md) == [{"slug": "real-story", "label": "Real story"}]


def test_structural_rubric_headings_cover_every_prompt_mandated_heading():
    # The guard for publish.py's MAINTENANCE COUPLING note, which has now
    # been missed twice by hand. Reads the prompt files themselves rather
    # than restating their contents, so adding a fixed rubric section to a
    # prompt without adding it to _STRUCTURAL_RUBRIC_HEADINGS fails here
    # instead of surfacing as a live false arc on the site.
    #
    # Discriminator: the prompts state every MANDATED section inside a bold
    # run (`- **`## Also today`** takes...`, `- **Standing rule --
    # `## Hungary`:**`), while EXAMPLE story headings ("## Missile strike
    # in Poland") only ever appear in plain prose. That is the whole
    # difference between "the prompt requires this section" and "the prompt
    # is illustrating what a story heading looks like" -- verified to yield
    # exactly the five mandated headings and zero example headings across
    # all four prompt files.
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    assert prompts_dir.is_dir(), f"prompts/ not found at {prompts_dir}"

    bold_run = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
    heading_ref = re.compile(r"`##\s+([^`]+)`")

    mandated: dict[str, str] = {}
    for prompt_path in sorted(prompts_dir.glob("*.md")):
        for run in bold_run.finditer(prompt_path.read_text(encoding="utf-8")):
            for ref in heading_ref.finditer(run.group(1)):
                # Prompt files wrap, so a heading reference can straddle a
                # newline -- collapse whitespace before comparing.
                mandated[" ".join(ref.group(1).split()).casefold()] = prompt_path.name

    # Sanity: the scan must actually find something. A prompt-format change
    # that silently matched nothing would make this test vacuously pass.
    assert len(mandated) >= 5, f"heading scan found too little: {mandated}"

    allowed = publish_mod._STRUCTURAL_RUBRIC_HEADINGS | {publish_mod._NEEDS_ATTENTION_HEADING}
    missing = {h: src for h, src in mandated.items() if h not in allowed}
    assert not missing, (
        "prompt-mandated rubric headings missing from "
        f"_STRUCTURAL_RUBRIC_HEADINGS: {missing}"
    )


def test_structural_rubric_headings_has_no_stale_entries():
    # The other direction: every entry in the set must still be mandated by
    # some prompt. Catches typos and entries left behind when a prompt drops
    # a section -- a stale entry silently suppresses a real story heading
    # that happens to match it.
    prompts_dir = Path(__file__).resolve().parent.parent / "prompts"
    corpus = "\n".join(
        p.read_text(encoding="utf-8") for p in sorted(prompts_dir.glob("*.md"))
    )
    collapsed = " ".join(corpus.split()).casefold()

    stale = [h for h in publish_mod._STRUCTURAL_RUBRIC_HEADINGS if f"## {h}" not in collapsed]
    assert not stale, f"_STRUCTURAL_RUBRIC_HEADINGS entries no prompt mandates: {stale}"


def test_derive_topics_structural_rubric_headings_case_insensitive():
    body_md = "## ALSO TODAY\n\nminor items\n\n## Real story\n\ntext\n"

    assert derive_topics(body_md) == [{"slug": "real-story", "label": "Real story"}]


def test_derive_topics_diacritics_fold_to_ascii_slug():
    body_md = "## Középső árfolyam\n\ntext\n"

    assert derive_topics(body_md) == [
        {"slug": "kozepso-arfolyam", "label": "Középső árfolyam"}
    ]


def test_derive_topics_punctuation_and_spacing_collapse():
    body_md = "## Fed — Watch & Rates!\n\ntext\n"

    assert derive_topics(body_md) == [
        {"slug": "fed-watch-rates", "label": "Fed — Watch & Rates!"}
    ]


def test_derive_topics_slug_collision_dedupes_first_occurrence_wins():
    # "Fed Watch!" and "Fed Watch?" both fold to "fed-watch" -- the second
    # must not produce a duplicate-slug entry (the site's ingest validator
    # 400s on that), and the FIRST heading's own label is the one kept.
    body_md = "## Fed Watch!\n\ntext\n\n## Fed Watch?\n\nmore text\n"

    assert derive_topics(body_md) == [{"slug": "fed-watch", "label": "Fed Watch!"}]


def test_derive_topics_more_than_twelve_sections_capped_at_twelve():
    body_md = "\n\n".join(f"## Section {i}\n\ntext" for i in range(1, 21))

    topics = derive_topics(body_md)

    assert len(topics) == 12
    assert topics[0] == {"slug": "section-1", "label": "Section 1"}
    assert topics[-1] == {"slug": "section-12", "label": "Section 12"}


def test_derive_topics_heading_that_slugifies_to_nothing_is_skipped():
    # A heading that is entirely punctuation/CJK folds to "" -- must be
    # skipped outright, never sent with an empty slug.
    body_md = "## !!!\n\ntext\n\n## 中文标题\n\nmore\n\n## Real section\n\ntext\n"

    assert derive_topics(body_md) == [{"slug": "real-section", "label": "Real section"}]


def test_derive_topics_empty_body_returns_empty_list():
    assert derive_topics("") == []
    assert derive_topics("just some prose, no headings at all") == []


# --- publish_to_site ---


class _FakeHTTPResponse:
    def __init__(self, body: bytes = b"{}"):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_publish_to_site_sends_expected_payload_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        captured["timeout"] = timeout
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        "<p>hi</p>",
        "2026-07-29T10:00:00+00:00",
        3,
        "https://news-site.example.workers.dev",
        "ingest-secret",
    )

    assert captured["url"] == "https://news-site.example.workers.dev/ingest/42"
    assert captured["method"] == "PUT"
    assert captured["headers"]["x-ingest-key"] == "ingest-secret"
    assert captured["headers"]["content-type"] == "application/json"
    assert captured["headers"]["user-agent"] == "notification-digest/1.0"
    assert captured["body"] == {
        "created_at": "2026-07-29T10:00:00+00:00",
        "tldr": "hi",
        "item_count": 3,
        "section_count": 1,
        "has_attention": False,
        "body_html": "<p>hi</p>",
        "body_md": "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        "kind": "window",
    }


def test_publish_to_site_raises_on_http_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 401, "Unauthorized", {}, None)

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.HTTPError):
        publish_to_site(
            1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
            "https://news-site.example.workers.dev", "key",
        )


def test_publish_to_site_raises_on_network_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(urllib.error.URLError):
        publish_to_site(
            1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
            "https://news-site.example.workers.dev", "key",
        )


# --- publish_to_site: optional Hungarian fields (all three or none) ---


def test_publish_to_site_includes_hu_fields_when_both_hu_args_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "**TL;DR:** hi\n\n## Worth knowing\n\nstuff",
        "<p>hi</p>",
        "2026-07-29T10:00:00+00:00",
        3,
        "https://news-site.example.workers.dev",
        "ingest-secret",
        body_md_hu="**TL;DR:** szia\n\n## Érdemes tudni\n\ndolog",
        body_html_hu="<p>szia</p>",
    )

    body = captured["body"]
    assert body["tldr_hu"] == "szia"
    assert body["body_html_hu"] == "<p>szia</p>"
    assert body["body_md_hu"] == "**TL;DR:** szia\n\n## Érdemes tudni\n\ndolog"
    # No duplicated structural fields for the Hungarian body -- translation
    # cannot change section count or attention-flag, so the English-derived
    # values already describe it.
    assert "section_count_hu" not in body
    assert "has_attention_hu" not in body


def test_publish_to_site_omits_hu_fields_when_neither_hu_arg_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "body",
        "<p>body</p>",
        "2026-07-29T10:00:00+00:00",
        1,
        "https://news-site.example.workers.dev",
        "key",
    )

    body = captured["body"]
    assert "tldr_hu" not in body
    assert "body_html_hu" not in body
    assert "body_md_hu" not in body


def test_publish_to_site_omits_hu_fields_when_only_body_md_hu_given(monkeypatch):
    # Guards the "all three or none" contract at the boundary: a caller bug
    # that supplies only one of the pair must not leak a partial set into
    # the payload (the Worker 400s a partial set).
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "body",
        "<p>body</p>",
        "2026-07-29T10:00:00+00:00",
        1,
        "https://news-site.example.workers.dev",
        "key",
        body_md_hu="magyar szöveg",
    )

    body = captured["body"]
    assert "tldr_hu" not in body
    assert "body_html_hu" not in body
    assert "body_md_hu" not in body


def test_publish_to_site_hu_tldr_falls_back_to_no_summary_placeholder(monkeypatch):
    # Mirrors the English tldr's own "(no summary)" fallback -- a
    # pathological Hungarian body that extract_tldr can't find a TL;DR line
    # in must not fail the Worker's ingest validator.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        42,
        "body",
        "<p>body</p>",
        "2026-07-29T10:00:00+00:00",
        1,
        "https://news-site.example.workers.dev",
        "key",
        body_md_hu="\n\n\n",
        body_html_hu="<p></p>",
    )

    assert captured["body"]["tldr_hu"] == "(no summary)"


# --- publish_to_site: kind field (daily-brief feature) ---


def test_publish_to_site_defaults_kind_to_window(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
    )

    assert captured["body"]["kind"] == "window"


def test_publish_to_site_forwards_explicit_daily_kind(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
        kind="daily",
    )

    assert captured["body"]["kind"] == "daily"


# --- publish_to_site: source_counts / failed_sources (both truthy-only) ---


def test_publish_to_site_includes_source_counts_and_failed_sources_when_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 3,
        "https://news-site.example.workers.dev", "key",
        source_counts={"telegram": 2, "x": 1},
        failed_sources=["news"],
    )

    assert captured["body"]["source_counts"] == {"telegram": 2, "x": 1}
    assert captured["body"]["failed_sources"] == ["news"]


def test_publish_to_site_omits_source_counts_and_failed_sources_when_none(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
    )

    assert "source_counts" not in captured["body"]
    assert "failed_sources" not in captured["body"]


def test_publish_to_site_omits_source_counts_and_failed_sources_when_empty(monkeypatch):
    # Truthy-only inclusion: an explicit empty dict/list must be omitted
    # exactly like None, matching the site's normalize-empty-to-NULL
    # ingest contract.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
        source_counts={},
        failed_sources=[],
    )

    assert "source_counts" not in captured["body"]
    assert "failed_sources" not in captured["body"]


# --- publish_to_site: topics (story-arcs feature, truthy-only) ---


def test_publish_to_site_includes_topics_when_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
        topics=[{"slug": "story-one", "label": "Story one"}],
    )

    assert captured["body"]["topics"] == [{"slug": "story-one", "label": "Story one"}]


def test_publish_to_site_omits_topics_when_none(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
    )

    assert "topics" not in captured["body"]


def test_publish_to_site_omits_topics_when_empty(monkeypatch):
    # Truthy-only inclusion, matching source_counts/failed_sources above: an
    # explicit empty list must be omitted exactly like None -- the site
    # itself treats an empty `topics` array the same as a missing field.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
        topics=[],
    )

    assert "topics" not in captured["body"]


def test_publish_to_site_includes_deltas_when_given(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
        deltas=[{"slug": "story-one", "previously": "old", "now": "new"}],
    )

    assert captured["body"]["deltas"] == [
        {"slug": "story-one", "previously": "old", "now": "new"}
    ]


def test_publish_to_site_omits_deltas_when_none(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
    )

    assert "deltas" not in captured["body"]


def test_publish_to_site_omits_deltas_when_empty(monkeypatch):
    # Truthy-only inclusion, matching topics/source_counts/failed_sources
    # above: an explicit empty list must be omitted exactly like None.
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    publish_to_site(
        1, "body", "<p>body</p>", "2026-07-29T10:00:00+00:00", 1,
        "https://news-site.example.workers.dev", "key",
        deltas=[],
    )

    assert "deltas" not in captured["body"]


# --- map_deltas_to_slugs ---


def test_map_deltas_to_slugs_matching_heading_mapped_to_derive_topics_slug():
    body_md = "**TL;DR:** hi\n\n## Fed rate decision\n\ntext\n"
    deltas = [{"heading": "Fed rate decision", "previously": "old", "now": "new"}]

    assert map_deltas_to_slugs(body_md, deltas) == [
        {"slug": "fed-rate-decision", "previously": "old", "now": "new"}
    ]


def test_map_deltas_to_slugs_no_matching_section_is_dropped(caplog):
    import logging

    body_md = "**TL;DR:** hi\n\n## Real section\n\ntext\n"
    deltas = [{"heading": "A heading that does not exist", "previously": "old", "now": "new"}]

    with caplog.at_level(logging.WARNING):
        result = map_deltas_to_slugs(body_md, deltas)

    assert result == []
    assert any("dropped 1 delta" in r.message for r in caplog.records)


def test_map_deltas_to_slugs_structural_rubric_heading_is_dropped():
    # "Also this window" is a structural rubric derive_topics always excludes
    # (see _STRUCTURAL_RUBRIC_HEADINGS) -- a delta citing it must be dropped
    # too, automatically, with no separate exclusion list to maintain.
    body_md = "**TL;DR:** hi\n\n## Also this window\n\ntext\n"
    deltas = [{"heading": "Also this window", "previously": "old", "now": "new"}]

    assert map_deltas_to_slugs(body_md, deltas) == []


def test_map_deltas_to_slugs_needs_attention_heading_is_dropped():
    body_md = "**TL;DR:** hi\n\n## Needs attention\n\ntext\n"
    deltas = [{"heading": "Needs attention", "previously": "old", "now": "new"}]

    assert map_deltas_to_slugs(body_md, deltas) == []


def test_map_deltas_to_slugs_preserves_order_and_keeps_only_matches():
    body_md = "**TL;DR:** hi\n\n## Story one\n\ntext\n\n## Story two\n\ntext\n"
    deltas = [
        {"heading": "Story one", "previously": "a", "now": "b"},
        {"heading": "No match", "previously": "c", "now": "d"},
        {"heading": "Story two", "previously": "e", "now": "f"},
    ]

    assert map_deltas_to_slugs(body_md, deltas) == [
        {"slug": "story-one", "previously": "a", "now": "b"},
        {"slug": "story-two", "previously": "e", "now": "f"},
    ]


def test_map_deltas_to_slugs_empty_deltas_returns_empty_list():
    assert map_deltas_to_slugs("**TL;DR:** hi\n\n## Section\n\ntext\n", []) == []


# --- send_telegram_tldr ---


def test_send_telegram_tldr_sends_expected_payload_and_headers(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["method"] = request.get_method()
        captured["headers"] = {k.lower(): v for k, v in request.headers.items()}
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    send_telegram_tldr(
        7,
        "**TL;DR:** big news\n\n## Section\n\nmore.\n",
        "2026-07-29T10:00:00+00:00",
        "12345:bot-token-value",
        "-100123",
        99,
        "https://news.example.com/t/tok",
    )

    assert captured["url"] == "https://api.telegram.org/bot12345:bot-token-value/sendMessage"
    assert captured["method"] == "POST"
    assert captured["headers"]["user-agent"] == "notification-digest/1.0"
    body = captured["body"]
    assert body["chat_id"] == "-100123"
    assert body["disable_web_page_preview"] is True
    assert body["message_thread_id"] == 99
    assert "big news" in body["text"]
    # The link is an inline-keyboard BUTTON, never raw text: the URL embeds
    # the site's capability token and reads as noise in the topic, and a
    # button needs no parse_mode.
    assert "https://news.example.com/t/tok/d/7" not in body["text"]
    button = body["reply_markup"]["inline_keyboard"][0][0]
    assert button["url"] == "https://news.example.com/t/tok/d/7"
    assert button["text"] == "Open the digest →"
    # PLAIN TEXT only -- no parse_mode, ever.
    assert "parse_mode" not in body


def test_send_telegram_tldr_omits_thread_id_when_zero(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    send_telegram_tldr(
        7,
        "**TL;DR:** hi\n\n## S\n\nx\n",
        "2026-07-29T10:00:00+00:00",
        "bot-token",
        "-100123",
        0,
        "https://news.example.com/t/tok",
    )

    assert "message_thread_id" not in captured["body"]


def test_send_telegram_tldr_http_error_raises_sanitized_error_without_url_or_body(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(
            "https://api.telegram.org/botSECRET-TOKEN/sendMessage",
            401,
            "Unauthorized: SECRET-TOKEN leaked in body",
            {},
            None,
        )

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TelegramSendError) as exc_info:
        send_telegram_tldr(
            7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
            "SECRET-TOKEN", "-100123", 0, "https://news.example.com/t/tok",
        )

    message = str(exc_info.value)
    assert "SECRET-TOKEN" not in message
    assert "401" in message
    assert exc_info.value.status == 401


def test_send_telegram_tldr_network_error_raises_sanitized_error(monkeypatch):
    def fake_urlopen(request, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TelegramSendError) as exc_info:
        send_telegram_tldr(
            7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
            "SECRET-TOKEN", "-100123", 0, "https://news.example.com/t/tok",
        )

    assert "SECRET-TOKEN" not in str(exc_info.value)
    assert exc_info.value.status is None


def test_send_telegram_tldr_status_matches_http_error_code_for_429_specifically(monkeypatch):
    # digest/main.py's per-run circuit breaker checks exactly `status == 429`
    # to decide whether to trip -- this pins the attribute for the specific
    # code that guard cares about, not just "some status was set."
    def fake_urlopen(request, timeout=None):
        raise urllib.error.HTTPError(request.full_url, 429, "Too Many Requests", {}, None)

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(TelegramSendError) as exc_info:
        send_telegram_tldr(
            7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
            "bot-token", "-100123", 0, "https://news.example.com/t/tok",
        )

    assert exc_info.value.status == 429


def test_send_telegram_tldr_header_uses_europe_budapest_local_time(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    # 2026-07-29T10:00:00+00:00 UTC is 12:00 in Europe/Budapest (CEST, UTC+2).
    send_telegram_tldr(
        7, "**TL;DR:** hi\n\n## S\n\nx\n", "2026-07-29T10:00:00+00:00",
        "bot-token", "-100123", 0, "https://news.example.com/t/tok",
    )

    assert "12:00" in captured["body"]["text"]


# --- send_telegram_tldr: section link button rows ---


def test_send_telegram_tldr_keyboard_has_open_digest_plus_three_section_rows(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    body_md = (
        "**TL;DR:** hi\n\n"
        "## Story one\n\ntext\n\n"
        "## Story two\n\ntext\n\n"
        "## Story three\n\ntext\n\n"
        "## Story four\n\ntext\n"
    )

    send_telegram_tldr(
        7, body_md, "2026-07-29T10:00:00+00:00",
        "bot-token", "-100123", 0, "https://news.example.com/t/tok",
    )

    rows = captured["body"]["reply_markup"]["inline_keyboard"]
    assert len(rows) == 4
    assert rows[0][0] == {"text": "Open the digest →", "url": "https://news.example.com/t/tok/d/7"}
    assert rows[1][0] == {
        "text": "→ Story one",
        "url": "https://news.example.com/t/tok/d/7#s1",
    }
    assert rows[2][0] == {
        "text": "→ Story two",
        "url": "https://news.example.com/t/tok/d/7#s2",
    }
    assert rows[3][0] == {
        "text": "→ Story three",
        "url": "https://news.example.com/t/tok/d/7#s3",
    }


def test_send_telegram_tldr_long_section_title_truncated_to_30_with_ellipsis(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    long_title = "A" * 40
    body_md = f"**TL;DR:** hi\n\n## {long_title}\n\ntext\n"

    send_telegram_tldr(
        7, body_md, "2026-07-29T10:00:00+00:00",
        "bot-token", "-100123", 0, "https://news.example.com/t/tok",
    )

    rows = captured["body"]["reply_markup"]["inline_keyboard"]
    button_text = rows[1][0]["text"]
    assert len(button_text) == 30
    assert button_text.endswith("…")
    assert button_text == f"→ {long_title}"[:29] + "…"


def test_send_telegram_tldr_zero_targets_keyboard_identical_to_today(monkeypatch):
    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["body"] = json.loads(request.data)
        return _FakeHTTPResponse()

    monkeypatch.setattr(publish_mod.urllib.request, "urlopen", fake_urlopen)

    # No real heading at all -- section_link_targets returns [], so the
    # keyboard must stay exactly the single "Open the digest" row it had
    # before this feature existed.
    body_md = "**TL;DR:** hi\n\njust some prose, no headings at all\n"

    send_telegram_tldr(
        7, body_md, "2026-07-29T10:00:00+00:00",
        "bot-token", "-100123", 0, "https://news.example.com/t/tok",
    )

    rows = captured["body"]["reply_markup"]["inline_keyboard"]
    assert rows == [[{"text": "Open the digest →", "url": "https://news.example.com/t/tok/d/7"}]]

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — the reference builder, and above all ITS FENCES.

Every fence here exists because R1 measured the failure it prevents, on a live
run, and wrote the number down. The tests are named after the failures rather
than after the functions, so a future reader deleting one can see what they are
re-admitting:

  * cited a URL it never fetched — 11 of 13 developments in run 1;
  * dated a November-2024 article off a September-2026 masthead — run 2 shipped
    it as an in-window leadership transition;
  * quoted a search snippet and called it a page span — run 1's 7.7%
    verification rate;
  * anchored a reference on ``londondaily.com`` and tiered it 2;
  * burned 12 of 17 fetches on one host that 401'd every time.

The loop is exercised end to end against a scripted model and a scripted pack
binding: no network, no core plane, no money, and the REAL protocol
(``stack.llm.tool_rounds``) carrying it. The handler is exercised through
``deterministic.run_method`` — the real binding path the runtime uses — and
never by calling the module function directly.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic_handlers import _reference_store as STORE
from legba.data.analysts.deterministic_handlers import reference_builder as RB
from legba.data.analysts.deterministic_handlers._reference_fences import (
    REJECT_NOT_FETCHED,
    REJECT_NO_DATE,
    REJECT_OUT_OF_WINDOW,
    REJECT_SPAN_UNVERIFIED,
    REJECT_TIER3_NOT_ADMITTED,
    DomainBlocklist,
    NoteGate,
    apply_fences,
    date_gate,
    dimension_counts,
    thin_from_counts,
    is_unfetchable,
    tier3_dimensions_after_first_pass,
    tier_of,
)
from legba.data.analysts.deterministic_handlers import _reference_notes as NOTES
from legba.data.analysts.deterministic_handlers._reference_loop import (
    BUILD_MAX_SECONDS_DEFAULT,
    BUILD_MAX_TOKENS_DEFAULT,
    COMMIT_AT,
    SEARCH_DEGRADED_STRIKES,
    ToolPlane,
    manifest,
    parse_reference_json,
    run_loop,
)
from legba.data.analysts.deterministic_handlers._reference_page import (
    ArchivedPage,
    ReferenceArchive,
    coerce_page_text,
    extract_publish_date,
    html_to_text,
    norm_span,
)
from legba.data.postgres import PostgresConfig
from legba.data.provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
from legba.runtime.analyst_method import AnalystMethodResult

_TARGET = "country_watch_il"
_T0 = datetime(2026, 9, 16, 19, 30, tzinfo=timezone.utc)
_WINDOW_START = _T0 - timedelta(days=14)
_DIMS = ("escalation", "internal_stability", "energy_security")

_SPAN = (
    "The Supreme Court struck down the moratorium on arresting Haredi draft "
    "evaders, the court said on Thursday."
)
#: Long enough to clear ``THIN_PAGE_CHARS``. That floor is a fence in its own
#: right — below it a 200 is almost certainly a consent wall, a paywall stub or
#: a challenge interstitial, and quoting one is how a BLOCK gets recorded as
#: "the web had nothing" (the false-absence defect the S3 review found).
_PAGE_TEXT = (
    "Jerusalem — in a ruling handed down this morning.\n"
    + _SPAN
    + "\nThe decision takes effect immediately.\n"
    + ("The court's reasoning is set out at length in the judgment. " * 30)
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _archive_with(
    url: str,
    text: str = _PAGE_TEXT,
    *,
    publish_date: str | None = "2026-09-04",
    status: int = 200,
) -> ReferenceArchive:
    archive = ReferenceArchive(None)
    page = ArchivedPage(
        url=url, final_url=url, host=url.split("/")[2].removeprefix("www."),
        status=status, chars=len(text), publish_date=publish_date,
        date_source="json-ld" if publish_date else "none",
        fetch_ts="2026-09-16T19:00:00+00:00",
    )
    archive.put(page, text)
    return archive


def _dev(**over: Any) -> dict[str, Any]:
    base = {
        "item_id": "RD-IL-C-1",
        "summary": "The court struck down the arrest moratorium.",
        "decisive_span": _SPAN,
        "outlet": "BBC",
        "publish_date": "2026-09-04",
        "source_url": "https://www.bbc.com/news/israel-haredi-ruling",
        "source_tier": 2,
        "significance": "major",
        "dimension": ["internal_stability"],
    }
    base.update(over)
    return base


def _fence(devs, archive, **kw):
    return apply_fences(
        devs,
        archive=archive,
        window_start=_WINDOW_START.date(),
        window_end=_T0.date(),
        **kw,
    )


# ---------------------------------------------------------------------------
# F4 — the DATE GATE. R1 run 2 shipped a FALSE development without it.
# ---------------------------------------------------------------------------


def test_date_gate_accepts_an_in_window_machine_readable_date():
    ok, reason = date_gate("2026-09-04", _WINDOW_START.date(), _T0.date())
    assert ok and reason == ""


def test_date_gate_rejects_a_page_with_no_machine_readable_date():
    """THE Israel-Katz item. Span verbatim, URL really fetched, page undated —
    the model read the site's MASTHEAD. A reference carrying it marks a correct
    live read as contradicted."""
    ok, reason = date_gate(None, _WINDOW_START.date(), _T0.date())
    assert not ok and reason == REJECT_NO_DATE


def test_date_gate_rejects_a_pre_window_date():
    ok, reason = date_gate("2026-09-01", _WINDOW_START.date(), _T0.date())
    assert not ok and reason == REJECT_OUT_OF_WINDOW


def test_date_gate_rejects_a_post_t0_date():
    ok, reason = date_gate("2026-09-20", _WINDOW_START.date(), _T0.date())
    assert not ok and reason == REJECT_OUT_OF_WINDOW


def test_an_undated_page_drops_its_development_however_good_the_span():
    archive = _archive_with(
        "https://www.bbc.com/news/x", publish_date=None
    )
    kept, stats = _fence(
        [_dev(source_url="https://www.bbc.com/news/x")], archive
    )
    assert kept == []
    assert stats.rejected[REJECT_NO_DATE] == 1
    assert stats.span_verified_rate == 0.0


def test_the_page_date_overrides_what_the_model_claimed():
    """The date is the PAGE's, never the model's. A model asserting an in-window
    date over a page that declares a pre-window one loses."""
    archive = _archive_with(
        "https://www.bbc.com/news/x", publish_date="2026-08-01"
    )
    kept, stats = _fence(
        [_dev(source_url="https://www.bbc.com/news/x",
              publish_date="2026-09-10")],
        archive,
    )
    assert kept == []
    assert stats.rejected[REJECT_OUT_OF_WINDOW] == 1


def test_an_accepted_development_carries_the_RUNG_that_dated_its_page():
    """o2 — ``date_source`` was always stamped; what it can now say is the rung
    off a six-rung ladder, so a reader can re-argue the date against the exact
    thing that produced it."""
    archive = _archive_with("https://www.bbc.com/news/x")
    archive.lookup_exact("https://www.bbc.com/news/x").date_source = "dateline:de"
    kept, _ = _fence([_dev(source_url="https://www.bbc.com/news/x")], archive)
    assert kept[0]["date_source"] == "dateline:de"
    assert "date_disagreement" not in kept[0]


def test_a_page_that_argued_with_itself_carries_the_dissent_onto_the_row():
    """The strongest rung still wins — that is the precedence — and the weaker
    rungs that answered differently ride the development so a reader can see
    the page contradicted itself rather than having it silently dropped."""
    archive = _archive_with("https://www.bbc.com/news/x")
    page = archive.lookup_exact("https://www.bbc.com/news/x")
    page.date_source = "json-ld"
    page.date_disagreement = ("http_last_modified=2026-09-16",)
    kept, _ = _fence([_dev(source_url="https://www.bbc.com/news/x")], archive)
    assert kept[0]["date_disagreement"] == ["http_last_modified=2026-09-16"]


def test_extract_publish_date_reads_json_ld_meta_and_time_in_that_order():
    ld = '<script type="application/ld+json">{"datePublished":"2026-09-04T08:00:00Z"}</script>'
    assert extract_publish_date(ld, ['{"datePublished":"2026-09-04T08:00:00Z"}']) == (
        "2026-09-04", "json-ld"
    )
    meta = '<meta property="article:published_time" content="2026-09-05T10:00:00Z">'
    assert extract_publish_date(meta, []) == ("2026-09-05", "meta:article:published_time")
    tag = '<time datetime="2026-09-06T11:00:00Z">Sept 6</time>'
    assert extract_publish_date(tag, []) == ("2026-09-06", "time-datetime")


def test_extract_publish_date_never_reads_a_masthead_out_of_visible_prose():
    """The exact page shape that produced R1's false development."""
    page = (
        "<html><body><header>Wednesday, Sep 16, 2026</header>"
        "<p>Israel Katz has been named the new Defence Minister.</p>"
        "</body></html>"
    )
    assert extract_publish_date(page, []) == (None, "none")


# ---------------------------------------------------------------------------
# F1 — the fetched-URL manifest. 11 invented citations -> 0.
# ---------------------------------------------------------------------------


def test_a_url_the_run_never_fetched_is_rejected_as_fabrication_not_as_a_bad_span():
    archive = _archive_with("https://www.bbc.com/news/real")
    kept, stats = _fence(
        [_dev(source_url="https://www.reuters.com/world/middle-east/.../")],
        archive,
    )
    assert kept == []
    assert stats.rejected[REJECT_NOT_FETCHED] == 1
    assert stats.rejected.get(REJECT_SPAN_UNVERIFIED, 0) == 0


def test_a_mistyped_url_is_admitted_against_the_page_it_really_read_and_records_it():
    """A loose (host+path) match locates BYTES for a URL already admitted; it
    never admits one. The correction is recorded, not silently applied."""
    archive = _archive_with("https://www.bbc.com/news/israel-haredi-ruling")
    kept, _ = _fence(
        [_dev(source_url="http://bbc.com/news/israel-haredi-ruling/")], archive
    )
    assert len(kept) == 1
    assert kept[0]["source_url_as_written"] == (
        "http://bbc.com/news/israel-haredi-ruling/"
    )
    assert kept[0]["source_url"] == (
        "https://www.bbc.com/news/israel-haredi-ruling"
    )


def test_the_manifest_lists_only_usable_pages_and_marks_the_undated_ones():
    archive = _archive_with("https://www.bbc.com/a", publish_date=None)
    archive.put(
        ArchivedPage(url="https://www.bbc.com/b", final_url="https://www.bbc.com/b",
                     host="bbc.com", status=200, chars=len(_PAGE_TEXT),
                     publish_date="2026-09-05"),
        _PAGE_TEXT,
    )
    archive.put(
        ArchivedPage(url="https://blocked.example/c",
                     final_url="https://blocked.example/c", host="blocked.example",
                     status=403, chars=12),
        "tiny stub",
    )
    text = manifest(archive)
    assert "UNDATED" in text
    assert "https://www.bbc.com/b" in text
    assert "blocked.example" not in text


# ---------------------------------------------------------------------------
# F6 — span verification. 7.7% -> 78.9%.
# ---------------------------------------------------------------------------


def test_a_span_that_is_not_in_the_page_drops_the_development():
    archive = _archive_with("https://www.bbc.com/news/x")
    kept, stats = _fence(
        [_dev(source_url="https://www.bbc.com/news/x",
              decisive_span="The court upheld the moratorium.")],
        archive,
    )
    assert kept == []
    assert stats.rejected[REJECT_SPAN_UNVERIFIED] == 1


def test_a_span_quoted_from_a_search_snippet_is_dropped_and_NAMED_as_such():
    """Three different defects must not be conflated: an invented URL, a quote
    from the wrong page, and a quote from a SNIPPET. Only the third gets this
    trace."""
    archive = _archive_with("https://www.bbc.com/news/x")
    snippet = "Court ends Haredi arrest freeze in landmark ruling"
    kept, stats = _fence(
        [_dev(source_url="https://www.bbc.com/news/x", decisive_span=snippet)],
        archive,
        snippet_blob=snippet,
    )
    assert kept == []
    assert stats.snippet_spans == 1


def test_span_normalisation_folds_curly_quotes_and_whitespace_and_nothing_else():
    assert norm_span("the  court’s\nruling") == "the court's ruling"
    assert norm_span("a — b") == "a - b"
    # never lowercases, never strips punctuation
    assert norm_span("The Court.") == "The Court."


def test_a_verified_development_carries_its_archive_address_for_re_argument():
    archive = _archive_with("https://www.bbc.com/news/israel-haredi-ruling")
    kept, stats = _fence([_dev()], archive)
    assert len(kept) == 1
    assert kept[0]["span_verified"] is True
    assert kept[0]["archive_sha256"]
    assert kept[0]["archive_path"].startswith("reference/")
    assert kept[0]["span_source"] == "archived_page"
    assert stats.span_verified_rate == 1.0


def test_span_verified_rate_is_a_real_number_not_null_when_anything_was_tried():
    archive = _archive_with("https://www.bbc.com/news/israel-haredi-ruling")
    _, stats = _fence([_dev(), _dev(source_url="https://nope.example/x")], archive)
    assert stats.candidates == 2
    assert stats.span_verified_rate == 0.5


def test_span_verified_rate_is_null_only_on_an_empty_denominator():
    """Zero candidates is undefined, not 0.0 — 0.0 would read as 'every span
    failed'."""
    _, stats = _fence([], ReferenceArchive(None))
    assert stats.span_verified_rate is None


# ---------------------------------------------------------------------------
# F5 — the Tier 1-2 discovery allowlist.
# ---------------------------------------------------------------------------


def test_tiering_is_by_host_suffix_on_label_boundaries():
    assert tier_of("https://www.iaea.org/newscentre/x") == 1
    assert tier_of("https://mfa.gov.il/statement") == 1
    assert tier_of("https://www.bbc.com/news/x") == 2
    assert tier_of("https://www.londondaily.com/x") == 3
    assert tier_of("https://notgov.il/x") == 3


def test_a_tier3_source_is_refused_unless_its_dimension_was_opened():
    archive = _archive_with("https://www.londondaily.com/news/x")
    dev = _dev(source_url="https://www.londondaily.com/news/x")
    kept, stats = _fence([dev], archive)
    assert kept == []
    assert stats.rejected[REJECT_TIER3_NOT_ADMITTED] == 1

    kept, stats = _fence([dev], archive, tier3_dimensions=["internal_stability"])
    assert len(kept) == 1
    assert kept[0]["tier3_admitted"] is True
    assert kept[0]["tier3_admitted_for"] == ["internal_stability"]
    assert stats.tier3_admitted == 1


def test_tier3_opens_only_for_dimensions_under_two_tier12_developments():
    noted = [
        {"source_url": "https://www.bbc.com/a", "dimension": ["escalation"]},
        {"source_url": "https://www.bbc.com/b", "dimension": ["escalation"]},
        {"source_url": "https://www.bbc.com/c", "dimension": ["energy_security"]},
    ]
    opened = tier3_dimensions_after_first_pass(noted, _DIMS)
    assert "escalation" not in opened
    assert "energy_security" in opened
    assert "internal_stability" in opened


def test_an_unrecognised_host_fails_SAFE_to_tier3():
    assert tier_of("https://brand-new-outlet.example/x") == 3


# ---------------------------------------------------------------------------
# F2 — the domain blocklist. 12 Reuters fetches -> 2.
# ---------------------------------------------------------------------------


def test_a_host_is_refused_after_two_failures_and_dropped_from_search_results():
    blocklist = DomainBlocklist(block_after=2)
    url = "https://www.reuters.com/world/x"
    assert not blocklist.blocked(url)
    blocklist.record_failure(url)
    assert not blocklist.blocked(url)
    blocklist.record_failure(url)
    assert blocklist.blocked(url)
    assert "will not spend another call on it" in blocklist.refuse(url)

    kept, dropped = blocklist.filter_results([
        {"url": "https://www.reuters.com/world/y"},
        {"url": "https://www.bbc.com/news/z"},
    ])
    assert [r["url"] for r in kept] == ["https://www.bbc.com/news/z"]
    assert dropped == ["reuters.com"]


def test_block_after_zero_disables_the_fence_so_its_absence_can_be_measured():
    blocklist = DomainBlocklist(block_after=0)
    for _ in range(5):
        blocklist.record_failure("https://www.reuters.com/x")
    assert not blocklist.blocked("https://www.reuters.com/x")


# ---------------------------------------------------------------------------
# F3 — the NOTE turn. 21 refused calls in run 2; 7.7% verification without it.
# ---------------------------------------------------------------------------


def test_the_note_gate_refuses_a_tool_call_that_skips_the_note():
    gate = NoteGate()
    gate.after_fetch(ok=True)
    assert gate.should_refuse()
    assert "NOTE" in gate.refuse()
    gate.note_written()
    assert not gate.should_refuse()


def test_a_failed_fetch_does_not_arm_the_note_gate():
    gate = NoteGate()
    gate.after_fetch(ok=False)
    assert not gate.should_refuse()


def test_the_note_gate_grants_an_exception_rather_than_spinning_forever():
    """A model that cannot produce a NOTE must not deadlock the build. The
    exception is COUNTED, because a silently different protocol is worse than a
    measured exception to one."""
    gate = NoteGate(max_consecutive=2)
    gate.after_fetch(ok=True)
    assert gate.should_refuse()
    gate.refuse()
    assert gate.should_refuse()
    gate.refuse()
    assert not gate.should_refuse()
    assert gate.as_record()["exceptions_granted"] == 1
    assert gate.as_record()["refusals"] == 2


# ---------------------------------------------------------------------------
# thin — the consequence fence the grader reads
# ---------------------------------------------------------------------------


def test_a_dimension_under_two_verified_developments_is_thin():
    devs = [
        _dev(dimension=["escalation"]),
        _dev(dimension=["escalation"]),
        _dev(dimension=["energy_security"]),
    ]
    counts = dimension_counts(devs, _DIMS)
    assert counts == {
        "escalation": 2, "internal_stability": 0, "energy_security": 1
    }
    assert thin_from_counts(counts, 2) == ["energy_security", "internal_stability"]


def test_the_store_counts_thin_over_the_references_OWN_band_table():
    """Not a platform constant: a reference that banded seven dimensions is thin
    on the seven it banded, whatever today's roster is."""
    reference = {
        "ref_bands": {"escalation": "high", "energy_security": "watch"},
        "ref_developments": [
            _dev(dimension=["escalation"]),
            _dev(dimension=["escalation"]),
            _dev(dimension=["military_posture"]),
        ],
    }
    assert STORE.thin_dimensions(reference, 2) == ["energy_security"]


def test_an_unverified_development_does_not_count_toward_depth():
    reference = {
        "ref_bands": {"escalation": "high"},
        "ref_developments": [
            _dev(dimension=["escalation"], span_verified=True),
            _dev(dimension=["escalation"], span_verified=False),
        ],
    }
    assert STORE.thin_dimensions(reference, 2) == ["escalation"]


def test_a_reference_never_span_checked_counts_every_development():
    """Loading ``ref_IL_A.json`` must compute the thin set it always did."""
    reference = {
        "ref_bands": {"escalation": "high"},
        "ref_developments": [
            _dev(dimension=["escalation"]),
            _dev(dimension=["escalation"]),
        ],
    }
    assert STORE.thin_dimensions(reference, 2) == []


# ---------------------------------------------------------------------------
# the store: the ONE writer's derivations
# ---------------------------------------------------------------------------


def test_span_verified_rate_is_the_builders_number_and_is_never_re_derived():
    assert STORE.span_verified_rate({"header": {"span_verified_rate": 0.789}}) == 0.789
    assert STORE.span_verified_rate({"header": {}}) is None
    assert STORE.span_verified_rate(
        {"header": {"span_verification": {"candidates": 19, "verified": 15}}}
    ) == pytest.approx(15 / 19)


def test_a_window_nobody_stated_is_refused_rather_than_invented():
    with pytest.raises(ValueError):
        STORE.parse_window({"header": {}}, None, None)
    start, end = STORE.parse_window(
        {"header": {"window": "2026-09-02T19:30:00+00:00 -> "
                              "2026-09-16T19:30:00+00:00"}}, None, None
    )
    assert start.day == 2 and end.day == 16


def test_the_canonical_sha_is_stable_under_key_order():
    a = {"b": 1, "a": [1, 2]}
    b = {"a": [1, 2], "b": 1}
    assert STORE.canonical_sha256(a) == STORE.canonical_sha256(b)


def test_the_topup_merge_unions_by_matter_and_collapses_a_repeat():
    base = [_dev(item_id="RD-IL-C-1")]
    addition = [
        _dev(item_id="X-1"),                                   # same url+date
        _dev(item_id="X-2", source_url="https://www.iaea.org/n",
             decisive_span="Iran was referred to the Security Council."),
    ]
    merged, stats = STORE.merge_developments(base, addition)
    assert stats == {"base": 1, "added": 1, "duplicates_dropped": 1}
    renumbered = STORE.renumber(merged, "RD-IL-C")
    assert [d["item_id"] for d in renumbered] == ["RD-IL-C-1", "RD-IL-C-2"]


def test_two_outlets_on_one_matter_are_corroboration_and_are_NOT_collapsed():
    base = [_dev(source_url="https://www.bbc.com/a")]
    addition = [_dev(source_url="https://www.iaea.org/b",
                     decisive_span="A different sentence entirely.")]
    merged, stats = STORE.merge_developments(base, addition)
    assert stats["added"] == 1 and stats["duplicates_dropped"] == 0
    assert len(merged) == 2


# ---------------------------------------------------------------------------
# the page module
# ---------------------------------------------------------------------------


def test_html_to_text_drops_chrome_and_keeps_the_article_sentence():
    html = (
        "<html><head><title>t</title></head><body><nav>menu</nav>"
        f"<article><p>{_SPAN}</p></article><footer>foot</footer></body></html>"
    )
    text, _ = html_to_text(html)
    assert _SPAN in text
    assert "menu" not in text and "foot" not in text


def test_a_json_payload_is_passed_through_so_an_official_feed_still_verifies():
    body = '{"headline": "Rate held at 4.5%", "date": "2026-09-10"}'
    text, ld = coerce_page_text(body)
    assert text == body and ld == []


def test_a_teaser_depth_page_verifies_its_span_but_stores_no_body():
    """The licence rule: read, check, do not keep. The development survives and
    says ``span_source: snippet`` so no reader thinks the bytes are re-servable."""
    archive = ReferenceArchive(None)
    page = ArchivedPage(
        url="https://www.haaretz.com/x", final_url="https://www.haaretz.com/x",
        host="haaretz.com", status=200, chars=len(_PAGE_TEXT),
        publish_date="2026-09-04", date_source="json-ld",
        depth="teaser", depth_reason="license_unreviewed",
    )
    archive.index_unstored(page, _PAGE_TEXT)
    kept, stats = _fence(
        [_dev(source_url="https://www.haaretz.com/x")], archive
    )
    assert len(kept) == 1
    assert stats.accepted == 1
    assert kept[0]["span_source"] == "snippet"
    assert kept[0]["archive_path"] == ""
    assert kept[0]["licence_depth"] == "teaser"


# ---------------------------------------------------------------------------
# the loop, end to end, against a scripted model and a scripted pack binding
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 1200
    completion_tokens = 80
    reasoning_tokens = 0
    total_tokens = 1280


class _Call:
    def __init__(self, name: str, args: dict[str, Any], call_id: str) -> None:
        self.id = call_id
        self.name = name
        self.arguments = dict(args)


class _Reply:
    def __init__(self, content: str = "", calls=()) -> None:
        self.content = content
        self.tool_calls = list(calls)
        self.finish_reason = "tool_calls" if calls else "stop"
        self.usage = _Usage()
        self.raw_response = None


class _ScriptedLLM:
    """Replays a fixed list of replies. Records the tools it was offered."""

    subprovider = "vllm"
    model_name = "pytest-core"

    def __init__(
        self, replies, *, fallback: str = "REFERENCE COMPLETE",
        delay: float = 0.0,
    ) -> None:
        self._replies = list(replies)
        self._fallback = fallback
        self._delay = float(delay)
        self.calls = 0
        self.tools_seen: list[Any] = []
        self.kwargs_seen: list[dict[str, Any]] = []
        self.messages_seen: list[list[dict[str, Any]]] = []
        self.last_messages: list[Any] = []

    def user_turns(self) -> list[str]:
        """Every user message the loop has ever put in front of the model."""
        seen: list[str] = []
        for turn in self.messages_seen:
            for message in turn:
                if message.get("role") == "user":
                    content = str(message.get("content") or "")
                    if content not in seen:
                        seen.append(content)
        return seen

    async def chat_complete(self, messages, **kwargs):
        self.calls += 1
        self.last_messages = list(messages)
        self.tools_seen.append(kwargs.get("tools"))
        self.kwargs_seen.append(dict(kwargs))
        self.messages_seen.append([dict(m) for m in messages])
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._replies:
            return self._replies.pop(0)
        return _Reply(self._fallback)


class _Result:
    def __init__(self, output, status="completed", error="") -> None:
        self.output = output
        self.status = status
        self.error = error


class _Outcome:
    def __init__(self, result, admitted=True, block_cause=None) -> None:
        self.tool_result = result
        self.admitted = admitted
        self.block_cause = block_cause


class _ScriptedBinding:
    """A pack binding double. Counts invocations, like the real governor does."""

    def __init__(self, pages=None, results=None) -> None:
        self.pages = dict(pages or {})
        self.results = list(results or [])
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name, args, **kwargs):
        self.invocations.append((tool_name, dict(args)))
        if tool_name == "web_search":
            return _Outcome(_Result({"results": self.results}))
        url = str(args.get("url"))
        body = self.pages.get(url)
        if body is None:
            return _Outcome(_Result(None, status="failed", error="404"))
        return _Outcome(_Result({
            "url": url, "status_code": 200, "body": body,
            "content_type": "text/html",
        }))


_GOOD_PAGE = (
    '<html><head><script type="application/ld+json">'
    '{"datePublished":"2026-09-04T08:00:00Z"}</script></head>'
    f"<body><article><p>{_SPAN}</p>"
     "<p>" + ("filler sentence. " * 120) + "</p></article></body></html>"
)

_FINAL_JSON = json.dumps({
    "header": {"country": "IL"},
    "ref_bands": {d: "elevated" for d in _DIMS},
    "ref_developments": [_dev(source_url="https://www.bbc.com/news/haredi")],
    "gaps": ["searched for Iranian fire on Israel; nothing dated in window"],
    "ref_direction": {"direction": "rising", "why": "x"},
})


@pytest.mark.asyncio
async def test_the_loop_searches_fetches_notes_and_commits_through_the_real_protocol():
    binding = _ScriptedBinding(
        pages={"https://www.bbc.com/news/haredi": _GOOD_PAGE},
        results=[{"url": "https://www.bbc.com/news/haredi", "title": "Ruling",
                  "snippet": "court", "engine": "bing"}],
    )
    archive = ReferenceArchive(None)
    plane = ToolPlane(binding=binding, archive=archive,
                      blocklist=DomainBlocklist())
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("web_search", {"query": "israel haredi"}, "c1")]),
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c2")]),
        _Reply("NOTE: | court struck down moratorium | SPAN: x | OUTLET: BBC "
               "| DATE: 2026-09-04 | URL: https://www.bbc.com/news/haredi "
               "| TIER: 2 | DIMS: internal_stability | SIG: major"),
        _Reply("REFERENCE COMPLETE"),
        _Reply(_FINAL_JSON),
    ])

    result = await run_loop(
        llm=llm, plane=plane, instruction="sys", first_user="go",
        dimensions=_DIMS, tool_call_cap=80, note_gate=NoteGate(),
        min_noted=1,
    )
    assert result.stop_reason == "committed"
    assert result.reference is not None
    assert result.tool_calls == 2
    # the MODEL's tool is `fetch_page` (R1's name, kept so the instruction and
    # the measured runs still describe the same thing); the PACK's tool is
    # `web_fetch`. The plane is the translation, and this asserts it.
    assert [name for name, _ in binding.invocations] == [
        "web_search", "web_fetch"
    ]
    # native tool specs were offered while gathering and WITHDRAWN at commit
    assert llm.tools_seen[0] is not None
    assert llm.tools_seen[-1] is None
    # the page really was archived, so the fences have ground truth
    assert archive.lookup_exact("https://www.bbc.com/news/haredi") is not None
    assert result.usage["prompt_tokens"] == 1200 * llm.calls


@pytest.mark.asyncio
async def test_the_loop_refuses_a_tool_call_that_skipped_its_note_and_spends_no_budget():
    binding = _ScriptedBinding(pages={"https://www.bbc.com/news/haredi": _GOOD_PAGE})
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c1")]),
        # straight back for another fetch, no NOTE — must be refused
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c2")]),
        _Reply("NOTE: | a | SPAN: b | URL: c"),
        _Reply("REFERENCE COMPLETE"),
        _Reply(_FINAL_JSON),
    ])
    gate = NoteGate()
    result = await run_loop(
        llm=llm, plane=plane, instruction="s", first_user="g",
        dimensions=_DIMS, tool_call_cap=80, note_gate=gate, min_noted=1,
    )
    assert result.note_gate["refusals"] == 1
    # ONE fetch reached the binding; the refused call cost no budget
    assert len(binding.invocations) == 1
    assert result.tool_calls == 1


@pytest.mark.asyncio
async def test_a_search_result_off_the_allowlist_never_reaches_the_model():
    binding = _ScriptedBinding(results=[
        {"url": "https://www.londondaily.com/x", "title": "a", "snippet": "s"},
        {"url": "https://www.bbc.com/news/y", "title": "b", "snippet": "t"},
    ])
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    payload = await plane.web_search("israel")
    assert [r["url"] for r in payload["results"]] == ["https://www.bbc.com/news/y"]
    assert payload["dropped_off_allowlist"] == ["londondaily.com"]


@pytest.mark.asyncio
async def test_a_blocked_host_is_refused_without_reaching_the_binding():
    # NOT reuters.com any more: D6 refuses a MEASURED-unreadable host at the
    # fetch call itself, so reuters would never reach the blocklist and this
    # test would measure the wrong fence. The host here is one that is perfectly
    # fetchable in principle and simply keeps failing THIS run.
    binding = _ScriptedBinding()
    blocklist = DomainBlocklist(block_after=2)
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=blocklist)
    url = "https://www.bbc.com/news/dead-x"
    await plane.fetch_page(url)   # 404 -> failure 1
    await plane.fetch_page(url)   # 404 -> failure 2
    assert len(binding.invocations) == 2
    payload = await plane.fetch_page(url)
    assert payload["status"] == "refused"
    assert len(binding.invocations) == 2  # the third never reached the pack


@pytest.mark.asyncio
async def test_a_licence_forbidden_host_is_never_fetched_at_all():
    binding = _ScriptedBinding(pages={"https://www.example.com/x": _GOOD_PAGE})

    async def lookup(_url):
        return "forbidden", "license_forbids", "publisher_forbids"

    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist(), licence_lookup=lookup)
    payload = await plane.fetch_page("https://www.example.com/x")
    assert payload["status"] == "refused"
    assert binding.invocations == []


@pytest.mark.asyncio
async def test_a_refused_call_costs_no_tool_budget():
    """The fences exist to SAVE budget; charging for a refusal spends exactly
    what they were built to protect. Measured: the first live R2 build burned 11
    of its 80 calls being told, eleven times, that it could not read a host it
    had already failed twice."""
    binding = _ScriptedBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist(block_after=1))
    url = "https://www.bbc.com/news/dead"
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page", {"url": url}, "c1")]),   # 404, charged
        _Reply("NOTE: | a |"),
        _Reply(calls=[_Call("fetch_page", {"url": url}, "c2")]),   # refused, free
        _Reply(calls=[_Call("fetch_page", {"url": url}, "c3")]),   # refused, free
        _Reply("REFERENCE COMPLETE"),
        _Reply(_FINAL_JSON),
    ])
    result = await run_loop(
        llm=llm, plane=plane, instruction="s", first_user="g",
        dimensions=_DIMS, tool_call_cap=80, note_gate=NoteGate(), min_noted=1,
    )
    assert result.tool_calls == 1
    assert len(binding.invocations) == 1


@pytest.mark.asyncio
async def test_a_measured_unreadable_host_leaves_discovery_but_keeps_its_tier():
    """Reuters is a wire and saying otherwise would be a lie about the source
    ladder. It is also measured to serve this lane nothing, repeatedly, so a
    result pointing at it is not a lead — it is a budget sink."""
    assert tier_of("https://www.reuters.com/world/x") == 2      # tier UNCHANGED
    assert is_unfetchable("https://www.reuters.com/world/x")
    assert not is_unfetchable("https://www.bbc.com/news/x")

    binding = _ScriptedBinding(results=[
        {"url": "https://www.reuters.com/world/x", "title": "a", "snippet": "s"},
        {"url": "https://www.bbc.com/news/y", "title": "b", "snippet": "t"},
    ])
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    payload = await plane.web_search("israel")
    assert [r["url"] for r in payload["results"]] == ["https://www.bbc.com/news/y"]
    assert payload["dropped_unreadable_hosts"] == ["reuters.com"]


@pytest.mark.asyncio
async def test_tier3_opens_under_budget_pressure_not_only_on_early_completion():
    """The escape hatch used to fire only when the model said REFERENCE COMPLETE
    too early — so a run that spent its whole budget gathering never opened at
    all, which is exactly the run that most needed it. The first live R2 build
    ended with tier3_opened == [] and seven empty dimensions."""
    binding = _ScriptedBinding(
        pages={"https://www.bbc.com/news/haredi": _GOOD_PAGE},
        results=[{"url": "https://www.bbc.com/news/haredi", "title": "a",
                  "snippet": "s"}],
    )
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    assert plane.tier12_only is True
    replies = [
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c1")]),
        _Reply("NOTE: | a | SPAN: b | URL: https://www.bbc.com/news/haredi "
               "| DIMS: escalation"),
        _Reply(calls=[_Call("web_search", {"query": "x"}, "c2")]),
        _Reply("NOTE: | b |"),
        _Reply(calls=[_Call("web_search", {"query": "y"}, "c3")]),
        _Reply("NOTE: | c |"),
        _Reply(calls=[_Call("web_search", {"query": "z"}, "c4")]),
        _Reply("NOTE: | d |"),
        _Reply(_FINAL_JSON),
    ]
    result = await run_loop(
        llm=_ScriptedLLM(replies), plane=plane, instruction="s", first_user="g",
        dimensions=_DIMS, tool_call_cap=4, note_gate=NoteGate(), min_noted=1,
    )
    # the budget ran out; the hatch fired on its way there, not at completion
    assert result.tier3_opened, "Tier 3 never opened despite empty dimensions"
    assert plane.tier12_only is False
    assert "energy_security" in result.tier3_opened


def test_the_json_parser_survives_a_fence_and_a_trailing_comma():
    assert parse_reference_json("```json\n{\"a\": 1,}\n```") == {"a": 1}
    assert parse_reference_json("no json here") is None


# ---------------------------------------------------------------------------
# registration, and the handler through the REAL binding path
# ---------------------------------------------------------------------------


def test_the_sub_handler_is_registered_in_the_dispatch_table():
    assert "reference_builder" in deterministic.SUB_HANDLERS
    assert deterministic.SUB_HANDLERS["reference_builder"] is RB.handle
    assert deterministic.OUTPUT_KIND_BY_SUB_HANDLER["reference_builder"].value == (
        "finding"
    )


def test_the_builder_is_verify_exempt_like_every_other_instrument():
    assert "reference_builder" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


def test_the_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(RB.ENABLED_ENV, raising=False)
    assert RB.builder_enabled() is False
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    assert RB.builder_enabled() is True


def test_t0_reads_the_graders_own_option_key_and_accepts_the_headers_alias():
    assert RB.resolve_t0({"as_of": "2026-09-16T19:30:00+00:00"}) == _T0
    assert RB.resolve_t0({"t0": "2026-09-16T19:30:00+00:00"}) == _T0
    # unparseable degrades to now rather than raising
    assert RB.resolve_t0({"as_of": "not a date"}).tzinfo is not None


# ---------------------------------------------------------------------------
# where the BUILD cadence meets the GRADE currency window
# ---------------------------------------------------------------------------


def test_a_cadence_longer_than_the_graders_grace_is_NAMED_not_left_to_be_found(
    monkeypatch
):
    """Each job would otherwise be correct and silent about the other: the
    builder reporting healthy builds, the grader reporting stale references, and
    nothing joining them. A reference built at T is current until T+grace; the
    next lands at T+cadence; live, that is cadence-grace ungraded days a cycle."""
    from legba.data.analysts.deterministic_handlers import correctness_grader as CG

    monkeypatch.setattr(CG, "REFERENCE_GRACE_ENV", "LEGBA_TEST_GRACE",
                        raising=False)
    monkeypatch.setattr(CG, "DEFAULT_REFERENCE_GRACE_DAYS", 7, raising=False)
    monkeypatch.delenv("LEGBA_TEST_GRACE", raising=False)

    assert RB.grader_grace_days() == (7, "LEGBA_TEST_GRACE")

    gap = RB.coverage_warning(14)
    assert gap is not None
    assert "7 day(s) per cycle" in gap
    assert "LEGBA_TEST_GRACE" in gap
    # both remedies named, and neither taken here
    assert "lowering cadence_days" in gap and "raising" in gap

    # a cadence inside the grace window has no gap and says nothing
    assert RB.coverage_warning(7) is None
    assert RB.coverage_warning(3) is None

    # the operator raising the grace closes it without a code change
    monkeypatch.setenv("LEGBA_TEST_GRACE", "14")
    assert RB.grader_grace_days() == (14, "LEGBA_TEST_GRACE")
    assert RB.coverage_warning(14) is None


def test_the_coverage_check_degrades_to_SILENCE_when_it_cannot_tell(monkeypatch):
    """The currency rule is a sibling lane's. On a tree where it has not landed,
    'cannot tell' must read as nothing at all rather than as a wrong number."""
    from legba.data.analysts.deterministic_handlers import correctness_grader as CG

    monkeypatch.setattr(CG, "REFERENCE_GRACE_ENV", "", raising=False)
    assert RB.grader_grace_days() == (-1, "")
    assert RB.coverage_warning(14) is None


def test_a_garbled_grace_env_falls_back_to_the_default_rather_than_raising(
    monkeypatch
):
    from legba.data.analysts.deterministic_handlers import correctness_grader as CG

    monkeypatch.setattr(CG, "REFERENCE_GRACE_ENV", "LEGBA_TEST_GRACE",
                        raising=False)
    monkeypatch.setattr(CG, "DEFAULT_REFERENCE_GRACE_DAYS", 7, raising=False)
    monkeypatch.setenv("LEGBA_TEST_GRACE", "not-a-number")
    assert RB.grader_grace_days() == (7, "LEGBA_TEST_GRACE")


class _Deps:
    def __init__(self, pool, extras=None) -> None:
        self.pg_pool = pool
        self.extras = dict(extras or {})


async def _run(pool, extras=None, **opts) -> AnalystMethodResult:
    """Drive the handler through ``deterministic.run_method`` — the REAL binding
    path the runtime uses, never a direct module call."""
    options = {
        "sub_handler": "reference_builder",
        "analyst_id": "reference_builder",
        "run_id": str(uuid4()),
        **opts,
    }
    result = await deterministic.run_method([], options, _Deps(pool, extras))
    assert isinstance(result, AnalystMethodResult)
    return result


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


#: A host whose licence class these tests flip. It is an OPERATOR-OWNED
#: `system.seed` row in `source_credibility` — 125 of them ship — so the tests
#: SAVE AND RESTORE its `license_class` and never delete the row. An earlier
#: version of this file deleted it, which silently destroyed seed data that
#: `test_source_credibility_api` and `test_seed` assert on: a test that mutates
#: shared seed state fails OTHER suites, in another file, for reasons invisible
#: from there.
_LICENCE_HOST = "bbc.com"


async def _clear_desk_findings(conn) -> None:
    """H5d (2026-09-23): the roster is DERIVED from every dimension desk's
    findings in the window, so a desk finding any earlier module left behind
    (for any target, or for none) joins the roster and displaces the seeded
    one. The container runner shuffles module order, so which module runs
    before this file is a coin toss. Clear the whole dimension set, not a few
    named targets."""
    from legba.data.analysts.deterministic_handlers import scorecard_banding as _banding
    from legba.data.analysts.deterministic_handlers.correctness_grader import PROLIFERATION as _prolif
    await conn.execute(
        "DELETE FROM analyst_outputs WHERE kind = 'finding' AND analyst_id = ANY($1::text[])",
        list(_banding.DIMENSIONS) + [_prolif],
    )


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async def _wipe(conn):
        await _clear_desk_findings(conn)
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = $1", _TARGET
        )

    async with pg_pool.acquire() as conn:
        original = await conn.fetchval(
            "SELECT license_class FROM source_credibility WHERE source_host = $1",
            _LICENCE_HOST,
        )
        await _wipe(conn)
    try:
        yield
    finally:
        async with pg_pool.acquire() as conn:
            await _wipe(conn)
            # Put the ledger back exactly as it was found, whatever a test did.
            await conn.execute(
                "UPDATE source_credibility SET license_class = $2 "
                "WHERE source_host = $1",
                _LICENCE_HOST, original,
            )


async def _seed_desk(conn, analyst_id: str, produced_at: datetime) -> None:
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, analyst_id, analyst_version, run_id, target_id, kind, title,
             body, confidence, produced_at, created_at, schema_uri)
        VALUES ($1,$2,$3,$4,$5,'finding',$6,'body',1.0,$7,$7,$8)
        """,
        uuid4(), analyst_id, "0" * 16, uuid4(), _TARGET,
        f"{analyst_id} read", produced_at,
        "iglu:legba/finding/jsonschema/1-0-0",
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_flag_off_reads_nothing_fetches_nothing_and_writes_nothing(
    pg_pool, clean_slate, monkeypatch
):
    monkeypatch.delenv(RB.ENABLED_ENV, raising=False)
    binding = _ScriptedBinding()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: _ScriptedLLM([]),
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
    )
    data = result.finding.data
    assert data["enabled"] is False
    assert data["n_references_written"] == 0
    assert binding.invocations == []
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_references WHERE target_id = $1", _TARGET
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_missing_web_binding_REFUSES_rather_than_degrading(
    pg_pool, clean_slate, monkeypatch
):
    """A reference built without the open web would not be independent of the
    reads it grades, so this is a refusal and not a degradation."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
    result = await _run(
        pg_pool, extras={RB.LLM_DEPS_EXTRA_KEY: _ScriptedLLM([])},
        as_of=_T0.isoformat(), reference_targets=[_TARGET],
    )
    statuses = [t["status"] for t in result.finding.data["per_target"]]
    assert statuses == ["no_web"]
    assert result.finding.data["n_references_written"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_one_build_end_to_end_writes_a_fenced_reference_the_grader_can_read(
    pg_pool, clean_slate, monkeypatch
):
    """The whole path: roster -> loop -> fences -> unit_references, through the
    real dispatch, with a scripted model and a scripted pack binding."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
        await _seed_desk(conn, "internal_stability", _T0 - timedelta(hours=2))
        # A REVIEWED, cleared licence class, so the body may be archived and the
        # span stays re-verifiable. Without one the host is teaser depth — the
        # sibling test covers that path. UPDATE only: the row is operator-owned
        # seed data and the fixture restores its original value.
        await conn.execute(
            "UPDATE source_credibility SET license_class = 'cc_by' "
            "WHERE source_host = $1",
            _LICENCE_HOST,
        )

    binding = _ScriptedBinding(
        pages={"https://www.bbc.com/news/haredi": _GOOD_PAGE},
        results=[{"url": "https://www.bbc.com/news/haredi", "title": "Ruling",
                  "snippet": "court"}],
    )
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c1")]),
        _Reply("NOTE: | a | SPAN: b | URL: https://www.bbc.com/news/haredi"),
        _Reply("REFERENCE COMPLETE"),
        # TWO developments committed: one real, one citing a page never fetched
        _Reply(json.dumps({
            "header": {"country": "IL"},
            "ref_bands": {"escalation": "high", "internal_stability": "elevated"},
            "ref_developments": [
                _dev(source_url="https://www.bbc.com/news/haredi"),
                _dev(item_id="RD-IL-C-2",
                     source_url="https://www.reuters.com/invented/..."),
            ],
            "gaps": ["checked for Iranian fire; nothing dated in window"],
        })),
    ])

    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET], min_developments=1,
    )
    data = result.finding.data
    assert data["n_references_written"] == 1
    target = data["per_target"][0]
    assert target["status"] == "built"
    # the invented citation was dropped by F1, the real one kept by F4+F6
    assert target["n_developments"] == 1
    assert target["fences"]["candidates"] == 2
    assert target["fences"]["rejected"][REJECT_NOT_FETCHED] == 1
    assert target["span_verified_rate"] == 0.5
    # cost travels on the receipt and is $0 by construction
    assert data["cost"]["paid_api_usd"] == 0.0
    assert data["cost"]["prompt_tokens"] > 0

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM unit_references WHERE target_id = $1", _TARGET
        )
    assert row is not None
    assert row["builder"] == RB.BUILDER_LABEL
    assert float(row["span_verified_rate"]) == 0.5
    # BOTH banded dimensions are thin, and the second one is the instructive
    # case: internal_stability carries the ONE surviving development, and one
    # development is still an anecdote. `thin_dimensions` is the column the
    # correctness grader reads, so this reference under-claims on both rather
    # than letting a single item stand as a dimension's reference.
    assert set(row["thin_dimensions"]) == {"escalation", "internal_stability"}
    stored = json.loads(row["ref_json"]) if isinstance(row["ref_json"], str) \
        else dict(row["ref_json"])
    assert stored["header"]["cost"]["paid_api_usd"] == 0.0
    assert stored["header"]["builder"] == RB.BUILDER_LABEL
    # bbc.com carries a CLEARED license_class in this test's ledger, so its body
    # was archived and the span stays re-verifiable after the process exits.
    assert stored["ref_developments"][0]["archive_sha256"]
    assert stored["ref_developments"][0]["span_source"] == "archived_page"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_unreviewed_licence_host_keeps_its_development_but_stores_no_body(
    pg_pool, clean_slate, monkeypatch
):
    """The ledger's own posture, and its cost, made visible.

    ``depth_for_license`` puts every host without a REVIEWED licence class at
    teaser depth. The span is still checked — against the page held in memory,
    before those bytes are dropped — so the development survives and is
    correctly fenced. What it is not is re-verifiable later, and the receipt
    says so rather than letting a whole roster quietly become un-re-arguable.
    """
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        # NULL, not DELETE — an unreviewed licence class is the row's natural
        # state and is what `depth_for_license` reads as teaser depth. The
        # fixture restores whatever was there before.
        await conn.execute(
            "UPDATE source_credibility SET license_class = NULL "
            "WHERE source_host = $1",
            _LICENCE_HOST,
        )
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
    binding = _ScriptedBinding(
        pages={"https://www.bbc.com/news/haredi": _GOOD_PAGE}
    )
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c1")]),
        _Reply("NOTE: | a | SPAN: b"),
        _Reply("REFERENCE COMPLETE"),
        _Reply(json.dumps({
            "header": {}, "ref_bands": {"escalation": "high"},
            "ref_developments": [
                _dev(source_url="https://www.bbc.com/news/haredi",
                     dimension=["escalation"]),
            ],
        })),
    ])
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET],
        reference_dry_run=True, min_developments=1,
    )
    target = result.finding.data["per_target"][0]
    dev = target["reference"]["ref_developments"][0]
    # the development SURVIVES — the fence is about evidence, not about licence
    assert dev["span_verified"] is True
    assert dev["span_source"] == "snippet"
    assert dev["archive_path"] == ""
    assert dev["licence_depth"] == "teaser"
    assert target["snippet_sourced"] == 1
    assert any("no reviewed license_class" in w
               for w in result.finding.data["warnings"])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_dry_run_builds_and_fences_but_writes_no_row(
    pg_pool, clean_slate, monkeypatch
):
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
    binding = _ScriptedBinding(
        pages={"https://www.bbc.com/news/haredi": _GOOD_PAGE}
    )
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page",
                            {"url": "https://www.bbc.com/news/haredi"}, "c1")]),
        _Reply("NOTE: | a | SPAN: b"),
        _Reply("REFERENCE COMPLETE"),
        _Reply(json.dumps({
            "header": {}, "ref_bands": {"escalation": "high"},
            "ref_developments": [
                _dev(source_url="https://www.bbc.com/news/haredi",
                     dimension=["escalation"]),
            ],
        })),
    ])
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET],
        reference_dry_run=True, min_developments=1,
    )
    target = result.finding.data["per_target"][0]
    assert target["status"] == "built_not_written"
    assert target["reference"]["ref_developments"][0]["span_verified"] is True
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_references WHERE target_id = $1", _TARGET
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_cadence_stagger_takes_the_most_overdue_target_and_only_one(
    pg_pool, clean_slate
):
    """The whole scheduler, and the reason there is no stagger table."""
    other = "country_g20_us"
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = ANY($1::text[])",
            [_TARGET, other],
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = ANY($1::text[])",
            [_TARGET, other],
        )
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
        for analyst in ("escalation",):
            await conn.execute(
                """
                INSERT INTO analyst_outputs
                    (id, analyst_id, analyst_version, run_id, target_id, kind,
                     title, body, confidence, produced_at, created_at,
                     schema_uri)
                VALUES ($1,$2,$3,$4,$5,'finding',$6,'b',1.0,$7,$7,$8)
                """,
                uuid4(), analyst, "0" * 16, uuid4(), other, "r",
                _T0 - timedelta(hours=2),
                "iglu:legba/finding/jsonschema/1-0-0",
            )
        # IL built 20 days ago (overdue by 6), US built 30 days ago (overdue 16)
        for target, days in ((_TARGET, 20), (other, 30)):
            await conn.execute(
                """
                INSERT INTO unit_references (
                    id, target_id, window_start, window_end, built_at, builder,
                    ref_json, thin_dimensions, sha256
                ) VALUES ($1,$2,$3,$4,$5,'pytest','{}'::jsonb,'{}'::text[],$6)
                """,
                uuid4(), target, _T0 - timedelta(days=days + 14),
                _T0 - timedelta(days=days), _T0 - timedelta(days=days),
                f"{days:064d}",
            )
        roster = await RB.resolve_roster(
            conn, t0=_T0, dimensions=("escalation",), roster_window_days=30
        )
        due = await RB.due_targets(conn, roster, t0=_T0, cadence_days=14)

        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = ANY($1::text[])",
            [_TARGET, other],
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = $1", other
        )

    assert {t["target_id"] for t in due} == {_TARGET, other}
    # most overdue FIRST — that ordering IS the stagger
    assert due[0]["target_id"] == other
    assert due[0]["age_days"] > due[1]["age_days"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_target_inside_its_cadence_is_not_due(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
        await conn.execute(
            """
            INSERT INTO unit_references (
                id, target_id, window_start, window_end, built_at, builder,
                ref_json, thin_dimensions, sha256
            ) VALUES ($1,$2,$3,$4,$5,'pytest','{}'::jsonb,'{}'::text[],$6)
            """,
            uuid4(), _TARGET, _T0 - timedelta(days=16), _T0 - timedelta(days=2),
            _T0 - timedelta(days=2), "f" * 64,
        )
        roster = await RB.resolve_roster(
            conn, t0=_T0, dimensions=("escalation",), roster_window_days=30
        )
        due = await RB.due_targets(conn, roster, t0=_T0, cadence_days=14)
    assert [t["target_id"] for t in due if t["target_id"] == _TARGET] == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_store_write_is_idempotent_on_target_and_sha(
    pg_pool, clean_slate
):
    reference = {"header": {}, "ref_bands": {"escalation": "high"},
                 "ref_developments": [_dev(dimension=["escalation"])]}
    async with pg_pool.acquire() as conn:
        first, derived = await STORE.write_reference(
            conn, target_id=_TARGET, reference=reference,
            window_start=_WINDOW_START, window_end=_T0,
            built_at=_T0, builder="pytest", rate=0.9,
        )
        second, _ = await STORE.write_reference(
            conn, target_id=_TARGET, reference=reference,
            window_start=_WINDOW_START, window_end=_T0,
            built_at=_T0, builder="pytest", rate=0.9,
        )
    assert first is not None
    assert second is None
    assert derived["sha256"] == STORE.canonical_sha256(reference)


# ---------------------------------------------------------------------------
# D3 — THE COMMIT WALL (ops 2026-09-17)
#
# The 00:43Z autonomous tick picked Argentina, ran 927.9 s for 4,510,885 prompt
# tokens over 67 tool calls and committed NOTHING. The web_access pack's
# invocation governor was already over `max_invocations_per_hour: 120` when the
# tick fired, so 63 of those calls came back "not admitted by the pack" — and
# the loop had no opinion about any of it, because its only exit toward a commit
# was the TOOL BUDGET running out or the model volunteering REFERENCE COMPLETE.
# The 4.5 M tokens then exhausted the descriptor's own `budget_tokens_per_day`,
# which stamped a one-hour global cooldown on the actor; the next tick no-op'd
# on it and the liveness watchdog raised `cadence_stall`. The receipt said
# `all_rejected` with "0 verified of 0 committed" and a rejection table of seven
# zeros — a sentence that cannot be true.
#
# Each test below is named after the part of that run it makes impossible.
# ---------------------------------------------------------------------------


_ALLOWED_URL = "https://www.bbc.com/news/haredi"


def _usable_result(url: str = _ALLOWED_URL) -> dict[str, Any]:
    return {"url": url, "title": "Ruling", "snippet": "court", "engine": "bing"}


def _search_calls(n: int) -> list[_Reply]:
    return [
        _Reply(calls=[_Call("web_search", {"query": f"q{i}"}, f"s{i}")])
        for i in range(n)
    ]


async def _loop(llm, binding, **kw):
    plane = ToolPlane(
        binding=binding, archive=ReferenceArchive(None),
        blocklist=DomainBlocklist(),
    )
    params: dict[str, Any] = dict(
        llm=llm, plane=plane, instruction="i", first_user="u",
        dimensions=_DIMS, tool_call_cap=10, note_gate=NoteGate(),
        min_noted=1,
    )
    params.update(kw)
    return await run_loop(**params), plane


@pytest.mark.asyncio
async def test_the_forced_commit_turn_fires_at_seventy_percent_of_the_tool_budget():
    """R1 run 1 spent 80 of 80 calls and verified 1 span of 13; the live
    Argentina build spent 67 and committed nothing. A loop whose only road to a
    commit is the budget running out has no road to a commit at all when the
    model keeps finding one more thing to look at. At 70% the tools come off the
    table — which is where the one GOOD live build (Israel, 55 of 80) already
    stopped on its own, so this changes the behaviour of a build that is not
    converging and nothing else."""
    binding = _ScriptedBinding(results=[_usable_result()])
    llm = _ScriptedLLM(_search_calls(20), fallback="")
    result, _plane = await _loop(llm, binding, tool_call_cap=10)

    assert result.commit_trigger == "tool_budget"
    # 70% of 10, and not one call more
    assert result.tool_calls == int(10 * COMMIT_AT)
    assert result.tool_calls < 10


@pytest.mark.asyncio
async def test_the_forced_commit_turn_closes_the_tools_and_says_the_sentence():
    """D3(b) in its own words. A model told only 'emit the JSON' while it still
    believes more gathering is coming asks for more gathering; this one is told
    the tools are closed and what happens to what it could not anchor. The tool
    SPECS are withheld (the binding half) and `tool_choice: none` is sent
    alongside (the explicit half), so a model that ignores one still meets the
    other."""
    binding = _ScriptedBinding(results=[_usable_result()])
    llm = _ScriptedLLM(_search_calls(20), fallback="")
    result, _plane = await _loop(llm, binding, tool_call_cap=10)

    # D6 REWROTE THE SENTENCE. "Commit the reference now from the notes you
    # have" was said to a model that had no notes — the 06:43Z Australia build
    # ended `no_notes` and answered it with an empty reference in fifteen
    # seconds. The forced turn now hands over the PAGES.
    forced = [
        turn for turn in llm.user_turns()
        if "BUILD YOUR DEVELOPMENTS ONLY FROM THE PAGES BELOW" in turn
    ]
    assert len(forced) == 1, llm.user_turns()
    assert "The tools are CLOSED for the rest of this build" in forced[0]
    assert "THE PAGES THIS RUN READ:" in forced[0]
    assert "TOOL BUDGET SPENT" in forced[0]

    # the round that carried the commit prompt was offered NO tools and was
    # sent tool_choice=none
    commit_rounds = [
        (tools, kwargs)
        for tools, kwargs in zip(llm.tools_seen, llm.kwargs_seen)
        if kwargs.get("tool_choice") is not None
    ]
    assert commit_rounds, "tool_choice was never sent on the commit turn"
    for tools, kwargs in commit_rounds:
        assert tools is None
        assert kwargs["tool_choice"] == "none"
    # and nothing BEFORE the commit turn carried it
    assert llm.kwargs_seen[0].get("tool_choice") is None
    assert result.commit_trigger == "tool_budget"


@pytest.mark.asyncio
async def test_a_handler_that_refuses_tool_choice_still_gets_its_commit():
    """The explicit half must never cost us the commit — that would trade the
    defect this wall exists to fix for a narrower one."""

    class _NoToolChoice(_ScriptedLLM):
        async def chat_complete(self, messages, *, tools=None, temperature=None,
                                system=None):
            return await super().chat_complete(
                messages, tools=tools, temperature=temperature, system=system,
            )

    binding = _ScriptedBinding(results=[_usable_result()])
    llm = _NoToolChoice(_search_calls(20), fallback="")
    result, _plane = await _loop(llm, binding, tool_call_cap=10)

    assert all(k.get("tool_choice") is None for k in llm.kwargs_seen)
    assert result.commit_trigger == "tool_budget"
    assert any(
        "BUILD YOUR DEVELOPMENTS ONLY FROM THE PAGES BELOW" in turn
        for turn in llm.user_turns()
    )


@pytest.mark.asyncio
async def test_two_dead_searches_running_commit_early_rather_than_burning_budget():
    """D3(d). The Argentina build's searches came back 'not admitted by the
    pack' sixty-three times; each one cost a full re-send of the conversation to
    learn the same fact. Two running is the whole signal — the third is not
    evidence of absence, it is evidence of the search plane."""
    binding = _ScriptedBinding(results=[])       # the plane answers, with nothing
    llm = _ScriptedLLM(_search_calls(20), fallback="")
    result, _plane = await _loop(llm, binding, tool_call_cap=10)

    assert result.search_degraded is True
    assert result.commit_trigger == "search_degraded"
    assert result.tool_calls == SEARCH_DEGRADED_STRIKES
    assert any("SEARCH DEGRADED" in turn for turn in llm.user_turns())


@pytest.mark.asyncio
async def test_a_pack_refusal_counts_as_a_dead_search_because_it_is_one():
    """THE LIVE SHAPE. `admitted=False` is what the governor returns when the
    web_access account is over `max_invocations_per_hour`, and the loop sees it
    as `{"error": ...}` — no results, no lead, and no amount of budget will
    change it inside the hour."""

    class _Refusing(_ScriptedBinding):
        async def run_tool(self, tool_name, args, **kwargs):
            self.invocations.append((tool_name, dict(args)))
            return _Outcome(None, admitted=False, block_cause="over_rate")

    llm = _ScriptedLLM(_search_calls(20), fallback="")
    result, _plane = await _loop(llm, _Refusing(), tool_call_cap=10)

    assert result.search_degraded is True
    assert result.commit_trigger == "search_degraded"
    assert result.tool_calls <= SEARCH_DEGRADED_STRIKES


@pytest.mark.asyncio
async def test_one_usable_search_between_two_dead_ones_is_not_a_degraded_plane():
    """The counter RESETS. A single narrow query that finds nothing is an
    ordinary event in a 14-day window; treating it as an outage would cut every
    build short for the most normal reason there is."""

    class _Alternating(_ScriptedBinding):
        def __init__(self) -> None:
            super().__init__()
            self.n = 0

        async def run_tool(self, tool_name, args, **kwargs):
            self.invocations.append((tool_name, dict(args)))
            self.n += 1
            hits = [] if self.n % 2 else [_usable_result()]
            return _Outcome(_Result({"results": hits}))

    llm = _ScriptedLLM(_search_calls(20), fallback="")
    result, _plane = await _loop(llm, _Alternating(), tool_call_cap=10)

    assert result.search_degraded is False
    assert result.commit_trigger == "tool_budget"


@pytest.mark.asyncio
async def test_the_wall_clock_cap_forces_the_commit_and_then_ends_the_build():
    """(a) and the reason for it. 927.9 s of an actor's turn is a held turn: the
    reconciler's 20 s ENSURE_ACTIVE heal blew twice against this actor while the
    build ran (`actor_turn.budget_exceeded op=reconcile.activate`), which is the
    condition `runtime/actor_turn.py` exists to make impossible. The cap is HARD
    — past it the loop stops whether or not anything was committed — and the
    forced turn at 70% is what gives it something to stop WITH."""
    binding = _ScriptedBinding(results=[_usable_result()])
    # A model that never volunteers, never emits JSON, and costs real time.
    llm = _ScriptedLLM([], fallback="still thinking about it", delay=0.02)
    result, _plane = await _loop(
        llm, binding, tool_call_cap=1000, max_seconds=0.6,
    )

    assert result.commit_trigger == "wall_clock"
    assert result.reference is None
    assert result.wall_seconds <= 2.0, result.wall_seconds
    assert result.build_max_seconds == 0.6
    assert any("WALL-CLOCK CAP" in turn for turn in llm.user_turns())


@pytest.mark.asyncio
async def test_the_token_ceiling_forces_the_commit_before_the_day_bucket_dies():
    """THE WALL THAT ACTUALLY STOPPED THE LANE. 4,510,885 tokens in one run
    against `budget_tokens_per_day: 4000000` exhausted the bucket, so the NEXT
    tick's budget precheck returned `exhausted` and stamped a one-hour
    BUDGET_THROTTLED cooldown — the analyst went dark for two hours because of
    one run. A ceiling under a third of the day bucket means no single build can
    do that, whatever else goes wrong."""
    binding = _ScriptedBinding(results=[_usable_result()])
    llm = _ScriptedLLM(_search_calls(50), fallback="")
    # _Usage reports 1,280 total per round; 3,000 forces the wall at ~1.6 rounds
    result, _plane = await _loop(
        llm, binding, tool_call_cap=1000, max_tokens=3_000,
    )

    assert result.commit_trigger == "token_ceiling"
    assert result.total_tokens() <= 3_000 * 2
    assert any("TOKEN CEILING" in turn for turn in llm.user_turns())


@pytest.mark.asyncio
async def test_a_garbled_cap_env_falls_back_rather_than_disabling_the_wall(
    monkeypatch
):
    """A cap that silently becomes 0 because of a typo is a cap that has been
    disabled without anyone deciding to."""
    from legba.data.analysts.deterministic_handlers import _reference_loop as LOOP

    monkeypatch.setenv(LOOP.BUILD_MAX_SECONDS_ENV, "not-a-number")
    assert LOOP.build_max_seconds() == BUILD_MAX_SECONDS_DEFAULT
    monkeypatch.setenv(LOOP.BUILD_MAX_SECONDS_ENV, "0")
    assert LOOP.build_max_seconds() == BUILD_MAX_SECONDS_DEFAULT
    monkeypatch.setenv(LOOP.BUILD_MAX_SECONDS_ENV, "120")
    assert LOOP.build_max_seconds() == 120.0

    monkeypatch.setenv(LOOP.BUILD_MAX_TOKENS_ENV, "-5")
    assert LOOP.build_max_tokens() == BUILD_MAX_TOKENS_DEFAULT


# ---------------------------------------------------------------------------
# D3(c) — the notes a failed build leaves behind
# ---------------------------------------------------------------------------


@pytest.fixture
def notes_root(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path))
    return tmp_path


def test_a_carry_keeps_the_newest_whole_note_lines_and_nothing_else(notes_root):
    """Newest-first is the right end to keep: the model works forward through a
    window, so its LATER notes are the ones it had not yet turned into
    developments. Whole lines only — half a NOTE line is a URL the next build
    would chase to nowhere."""
    lines = [f"NOTE: | matter {i} | URL: https://example.org/{i}" for i in range(50)]
    text = NOTES.bound(["prose that is not a note"] + lines, max_chars=200)
    kept = text.splitlines()
    assert kept, text
    assert all(line.startswith("NOTE:") for line in kept)
    assert kept[-1] == lines[-1]                 # the newest survives
    assert lines[0] not in kept                  # the oldest was dropped
    assert len(text) <= 200


def test_a_build_that_committed_nothing_leaves_its_notes_for_the_next_one(
    notes_root
):
    saved = NOTES.save(
        _TARGET, ["NOTE: | a matter | URL: https://www.bbc.com/x"],
        window_end=_T0.isoformat(), stop_reason="max_rounds",
        commit_trigger="wall_clock",
    )
    assert saved is not None
    loaded = NOTES.load(_TARGET)
    assert loaded is not None
    assert "https://www.bbc.com/x" in loaded["notes"]
    assert loaded["stop_reason"] == "max_rounds"

    block = NOTES.preamble(loaded)
    # LEADS, not evidence — a model that reads a carried URL as already-verified
    # commits a development the manifest fence then drops for no stated reason.
    assert "THESE ARE LEADS, NOT EVIDENCE" in block
    assert "FETCH AGAIN" in block

    assert NOTES.clear(_TARGET) is True
    assert NOTES.load(_TARGET) is None


def test_a_carry_older_than_its_window_is_dropped_unread(notes_root):
    NOTES.save(_TARGET, ["NOTE: | a | URL: https://www.bbc.com/x"])
    assert NOTES.load(_TARGET, max_age_hours=0.0) is None
    # and it was DELETED, not left to be re-read and re-rejected every tick
    assert NOTES.load(_TARGET) is None


def test_a_target_id_that_is_not_a_path_segment_is_refused(notes_root):
    assert NOTES.save("../../etc/passwd", ["NOTE: | a | URL: https://x/y"]) is None
    assert NOTES.load("../../etc/passwd") is None


def test_nothing_worth_carrying_carries_nothing(notes_root):
    assert NOTES.save(_TARGET, ["just prose, no NOTE lines"]) is None
    assert NOTES.load(_TARGET) is None


# ---------------------------------------------------------------------------
# D3(c) — `no_commit` and `all_rejected` are OPPOSITE diagnoses
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_model_that_never_commits_yields_no_commit_within_the_cap(
    pg_pool, clean_slate, monkeypatch, notes_root
):
    """THE ARGENTINA RUN, through the real dispatch, with the walls in.

    A model that neither volunteers REFERENCE COMPLETE nor emits JSON used to
    run to `max_rounds` — 320 rounds at the shipped cap, each one a full re-send
    of the conversation. Now the wall clock forces the commit turn, the reply is
    still not a reference, and the run ends INSIDE ITS CAP with a named outcome
    and the notes kept. Nothing is written and nothing pretends to be."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))

    binding = _ScriptedBinding(
        pages={_ALLOWED_URL: _GOOD_PAGE}, results=[_usable_result()],
    )
    llm = _ScriptedLLM(
        [], delay=0.02,
        fallback="NOTE: | a matter | URL: https://www.bbc.com/news/haredi",
    )
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET],
        min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    target = data["per_target"][0]

    assert target["status"] == "no_commit"
    assert data["n_no_commit"] == 1
    assert data["n_all_rejected"] == 0
    assert data["n_references_written"] == 0
    # INSIDE the cap — the whole point of it
    assert target["loop"]["wall_seconds"] <= 4.0, target["loop"]
    assert target["loop"]["build_max_seconds"] == 1
    assert target["commit_trigger"] == "wall_clock"
    assert data["n_forced_commit"] == 1
    # the receipt READS no_commit, and says what stopped the build
    assert "built no reference (no_commit)" in result.finding.title
    body = result.finding.body
    assert f"- [no_commit] {_TARGET}" in body
    assert "no_commit: stop_reason=" in body
    assert "forced_commit=wall_clock" in body
    # and it is explicitly NOT the other diagnosis
    assert any("NOT `all_rejected`" in w for w in data["warnings"])
    # the notes it did accumulate are kept for the next attempt
    assert target["notes_carried"] is True
    carried = NOTES.load(_TARGET)
    assert carried is not None and "bbc.com" in carried["notes"]

    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_references WHERE target_id = $1", _TARGET
        ) == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reference_with_no_development_is_no_commit_not_all_rejected(
    pg_pool, clean_slate, monkeypatch, notes_root
):
    """THE LINE THAT COULD NOT BE TRUE. The 00:43Z receipt read `all_rejected`
    with '0 verified of 0 committed' and a rejection table of seven zeros —
    nothing was rejected, because nothing was committed. They point at opposite
    things: `all_rejected` at the fences, `no_commit` at the plane, the budget
    and the loop. An operator reading the wrong one looks in the wrong place."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))

    binding = _ScriptedBinding(pages={_ALLOWED_URL: _GOOD_PAGE})
    llm = _ScriptedLLM([
        _Reply("NOTE: | a matter | URL: " + _ALLOWED_URL),
        _Reply("REFERENCE COMPLETE"),
        _Reply(json.dumps({
            "header": {}, "ref_bands": {"escalation": "high"},
            "ref_developments": [],          # parseable, and carrying nothing
        })),
    ])
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET], min_developments=1,
    )
    target = result.finding.data["per_target"][0]
    assert target["status"] == "no_commit"
    # the model DID commit a parseable object — the loop is not what failed
    assert target["no_commit_reason"] == "committed"
    assert "fences" not in target, (
        "a build that committed nothing must not report a rejection table — "
        "seven zeros is what sent the last reader to the fences"
    )
    assert result.finding.data["n_no_commit"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_all_rejected_still_names_the_case_it_was_always_for(
    pg_pool, clean_slate, monkeypatch, notes_root
):
    """The converse, so the split is a split and not a rename: a model that
    COMMITS developments and has every one thrown out by a fence is still
    `all_rejected`, with the rejection table that makes it actionable."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))

    binding = _ScriptedBinding(pages={_ALLOWED_URL: _GOOD_PAGE})
    llm = _ScriptedLLM([
        _Reply("NOTE: | a matter | URL: " + _ALLOWED_URL),
        _Reply("REFERENCE COMPLETE"),
        _Reply(json.dumps({
            "header": {}, "ref_bands": {"escalation": "high"},
            # a page this run never fetched — F1, the fabrication fence
            "ref_developments": [
                _dev(source_url="https://www.reuters.com/invented/...",
                     dimension=["escalation"]),
            ],
        })),
    ])
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET], min_developments=1,
    )
    target = result.finding.data["per_target"][0]
    assert target["status"] == "all_rejected"
    assert target["fences"]["candidates"] == 1
    assert target["fences"]["rejected"][REJECT_NOT_FETCHED] == 1
    assert result.finding.data["n_all_rejected"] == 1
    assert result.finding.data["n_no_commit"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_next_attempt_starts_from_the_carried_notes_and_clears_them(
    pg_pool, clean_slate, monkeypatch, notes_root
):
    """The carry is only worth having if it REACHES the next build — and only
    safe if a build that succeeds drops it, or the next window would be seeded
    from the last one's leads."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk(conn, "escalation", _T0 - timedelta(hours=2))
        await conn.execute(
            "UPDATE source_credibility SET license_class = 'cc_by' "
            "WHERE source_host = $1", _LICENCE_HOST,
        )
    NOTES.save(
        _TARGET, [f"NOTE: | a prior matter | URL: {_ALLOWED_URL}"],
        window_end=_T0.isoformat(), stop_reason="max_rounds",
    )

    binding = _ScriptedBinding(pages={_ALLOWED_URL: _GOOD_PAGE})
    llm = _ScriptedLLM([
        _Reply(calls=[_Call("fetch_page", {"url": _ALLOWED_URL}, "c1")]),
        _Reply("NOTE: | a | SPAN: b"),
        _Reply("REFERENCE COMPLETE"),
        _Reply(json.dumps({
            "header": {}, "ref_bands": {"escalation": "high"},
            "ref_developments": [
                _dev(source_url=_ALLOWED_URL, dimension=["escalation"]),
            ],
        })),
    ])
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET], min_developments=1,
    )
    target = result.finding.data["per_target"][0]
    assert target["status"] == "built"
    assert target["carried_notes"]["note_lines"] == 1
    # it reached the model, labelled as a lead rather than as evidence
    first_user = llm.messages_seen[0][0]["content"]
    assert "NOTES CARRIED FROM YOUR LAST ATTEMPT" in first_user
    assert "a prior matter" in first_user
    # and a committed reference supersedes it
    assert NOTES.load(_TARGET) is None


@pytest.mark.asyncio
async def test_an_allowlist_that_drops_everything_is_not_a_degraded_plane():
    """THE FIRST LIVE RUN OF THIS WALL got this wrong and committed 43 s into an
    Argentina build whose engines had answered fine. `results` is what survived
    the Tier 1-2 allowlist, the domain blocklist and the unreadable-host drop —
    every one of them a FENCE of ours. A query that found thirty items and had
    all thirty dropped for being off-allowlist is a STRICT build, not a dead
    search plane. The budget-pressure Tier-3 opening is what handles an
    over-filtered build; this signal is only about whether anything answered."""

    class _OffAllowlistOnly(_ScriptedBinding):
        async def run_tool(self, tool_name, args, **kwargs):
            self.invocations.append((tool_name, dict(args)))
            return _Outcome(_Result({"results": [
                {"url": f"https://londondaily.example/{i}", "title": "t",
                 "snippet": "s"} for i in range(30)
            ]}))

    llm = _ScriptedLLM(_search_calls(20), fallback="")
    result, plane = await _loop(llm, _OffAllowlistOnly(), tool_call_cap=10)

    # the allowlist really did empty the result sets, right through the window
    # where the strike counter would have fired (it needs two RUNNING, and the
    # Tier-3 budget-pressure opening only lifts the allowlist at 50% of the cap)
    assert all(entry["results"] == 30 for entry in plane.searches)
    assert [e["returned"] for e in plane.searches[:3]] == [0, 0, 0]
    # … and the plane was never called degraded for it
    assert result.search_degraded is False
    assert result.commit_trigger == "tool_budget"


# ---------------------------------------------------------------------------
# R2-FIX(2) — THE ORDER, AND THE TARGET THAT COULD NOT YIELD THE HOUR
#
# Live evidence, 2026-09-17. Two consecutive builds went to country_g20_ar and
# produced nothing: 00:43Z ran 928 s to `all_rejected` with 0 committed (before
# the D3 fix) and the 05:20Z dry run stopped correctly at 304 s with
# `no_commit` (after it, working as designed). Argentina is largely unfetchable
# to this lane — 3 of 16 pages usable. Because the queue was ordered by each
# target's newest SUCCESSFUL reference, and a failed build writes no row,
# Argentina stayed permanently the most overdue target on a 32-country roster:
# every hourly tick would have picked it again, and 29 other due targets would
# never have been built at all.
#
# The tests below are named after that failure. The ordering function is pure
# and is argued with a fixture roster; the fence is then exercised END TO END
# through `deterministic.run_method` against real Postgres, because the thing
# that has to be true is not "the sort is right" but "the tick spends its hour
# somewhere else".
# ---------------------------------------------------------------------------

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _reference_roster as ROSTER,
)

_TARGET_B = "country_g20_ar"


def _entry(target_id: str, *, age_days: float | None = None) -> dict[str, Any]:
    """One cadence-due roster entry, as ``due_targets`` builds them."""
    return {
        "target_id": target_id,
        "dimensions": list(_DIMS),
        "last_built_at": (
            None if age_days is None
            else (_T0 - timedelta(days=age_days)).isoformat()
        ),
        "age_days": age_days,
    }


def _attempt(hours_ago: float, status: str, failures: int = 1) -> ROSTER.Attempt:
    return ROSTER.Attempt(
        last_attempt_at=_T0 - timedelta(hours=hours_ago),
        last_status=status,
        consecutive_failures=failures if status in ROSTER.FAILED_STATUSES else 0,
    )


# ---------------------------------------------------------------------------
# the ordering function, pure, over a fixture roster
# ---------------------------------------------------------------------------


def test_a_target_never_attempted_goes_ahead_of_one_attempted_an_hour_ago():
    """NULLS FIRST is the whole of "give everyone a turn". Without it a roster
    where one country has been tried and thirty have not would keep re-trying
    the one, because its REFERENCE age is what the old key read."""
    order = ROSTER.order_due(
        [_entry("country_a", age_days=40.0), _entry("country_b")],
        {"country_a": _attempt(200, "built")},
        t0=_T0,
    )
    assert [e["target_id"] for e in order] == ["country_b", "country_a"]
    assert order[0]["reason"] == ROSTER.REASON_NEVER_ATTEMPTED
    assert order[1]["reason"] == ROSTER.REASON_OLDEST_ATTEMPT


def test_most_overdue_still_breaks_the_tie_between_two_never_attempted_targets():
    """The old rule is not gone, it is DEMOTED to the tie-break. On a fresh
    roster nothing has been attempted, so the behaviour is exactly the old one
    and the R2 stagger is unchanged."""
    order = ROSTER.order_due(
        [_entry("country_a", age_days=20.0), _entry("country_b", age_days=30.0),
         _entry("country_c")],
        {},
        t0=_T0,
    )
    # never built (age None) first, then oldest reference first
    assert [e["target_id"] for e in order] == [
        "country_c", "country_b", "country_a",
    ]


def test_the_target_that_just_failed_yields_the_hour_and_says_why():
    """THE ARGENTINA CASE. It is the most overdue target by a mile — it has
    never built a reference at all — and it is still not the one that gets the
    hour, because it failed forty minutes ago."""
    order = ROSTER.order_due(
        [_entry(_TARGET_B), _entry("country_watch_il", age_days=9.0)],
        {_TARGET_B: _attempt(0.7, "no_commit")},
        t0=_T0, backoff_hours=24,
    )
    assert [e["target_id"] for e in order] == ["country_watch_il", _TARGET_B]
    chosen, waiting = order[0], order[1]
    assert chosen["target_id"] == "country_watch_il"
    assert chosen["eligible"] is True
    assert waiting["target_id"] == _TARGET_B
    assert waiting["eligible"] is False
    assert waiting["reason"] == ROSTER.REASON_RETRY_BACKOFF
    assert waiting["retry_after"] == (
        _T0 - timedelta(hours=0.7) + timedelta(hours=24)
    ).isoformat()
    # and it is NOT dropped — the receipt can still say it is overdue
    assert waiting["age_days"] is None


def test_a_failed_target_is_eligible_again_once_its_backoff_expires():
    order = ROSTER.order_due(
        [_entry(_TARGET_B), _entry("country_watch_il", age_days=9.0)],
        {_TARGET_B: _attempt(25, "all_rejected"),
         "country_watch_il": _attempt(2, "built")},
        t0=_T0, backoff_hours=24,
    )
    assert order[0]["target_id"] == _TARGET_B
    assert order[0]["eligible"] is True
    assert order[0]["reason"] == ROSTER.REASON_OLDEST_ATTEMPT
    assert order[0]["retry_after"] is None


def test_backoff_zero_disables_the_fence_so_its_absence_can_be_measured():
    """The same rule the domain blocklist honours: a fence nobody can turn off
    is a fence nobody can measure."""
    order = ROSTER.order_due(
        [_entry(_TARGET_B)], {_TARGET_B: _attempt(0.1, "no_commit")},
        t0=_T0, backoff_hours=0,
    )
    assert order[0]["eligible"] is True
    assert order[0]["reason"] == ROSTER.REASON_OLDEST_ATTEMPT


def test_a_successful_build_ends_the_backoff_immediately():
    """Only the two FAILURE statuses arm it. A target that built an hour ago is
    not due anyway, but if the cadence says it is, nothing holds it back."""
    order = ROSTER.order_due(
        [_entry(_TARGET_B)], {_TARGET_B: _attempt(0.1, "built")},
        t0=_T0, backoff_hours=24,
    )
    assert order[0]["eligible"] is True


def test_three_consecutive_failures_flag_the_target_and_never_drop_it():
    attempts = {_TARGET_B: ROSTER.Attempt(
        last_attempt_at=_T0 - timedelta(hours=30),
        last_status="no_commit", consecutive_failures=3,
    )}
    assert ROSTER.unbuildable_targets(attempts) == [_TARGET_B]
    order = ROSTER.order_due([_entry(_TARGET_B)], attempts, t0=_T0,
                             backoff_hours=24)
    # flagged AND still in the queue AND eligible (its backoff has lapsed)
    assert order[0]["unbuildable_by_lane"] is True
    assert order[0]["eligible"] is True
    assert order[0]["consecutive_failures"] == 3


def test_two_failures_are_not_yet_a_flag():
    attempts = {_TARGET_B: ROSTER.Attempt(
        last_attempt_at=_T0, last_status="all_rejected",
        consecutive_failures=2,
    )}
    assert ROSTER.unbuildable_targets(attempts) == []


# ---------------------------------------------------------------------------
# the ledger: what counts as an attempt, and how the strikes are counted
# ---------------------------------------------------------------------------


def test_only_a_build_that_reached_the_plane_counts_as_an_attempt():
    """`no_web`/`no_model` are refusals BEFORE the build: nothing searched,
    nothing fetched, nothing spent. Recording them would rotate the queue on a
    misconfiguration that hits every target equally, and would push a perfectly
    buildable country to the back for a reason that is not about it."""
    records = ROSTER.attempt_records(
        [
            {"target_id": "a", "status": "no_web"},
            {"target_id": "b", "status": "no_model"},
            {"target_id": "c", "status": "no_commit"},
            {"target_id": "d", "status": "built"},
            {"target_id": "e", "status": "built_not_written"},
            {"target_id": "f", "status": "duplicate"},
            {"target_id": "g", "status": "all_rejected"},
        ],
        at=_T0,
    )
    assert set(records) == {"c", "d", "e", "f", "g"}
    assert records["c"] == {"at": _T0.isoformat(), "status": "no_commit",
                            "ok": False}
    assert records["g"]["ok"] is False
    assert records["d"]["ok"] is True
    assert records["e"]["ok"] is True   # a dry run still produced a reference
    assert records["f"]["ok"] is True


def test_the_strike_count_stops_at_the_first_success_walking_backwards():
    """Two failures, a build, then one failure is ONE strike, not three."""
    rows = [   # newest first, as the SQL returns them
        {"produced_at": _T0, "attempts": {"x": {"at": _T0.isoformat(),
                                                "status": "no_commit"}}},
        {"produced_at": _T0 - timedelta(hours=1),
         "attempts": {"x": {"at": (_T0 - timedelta(hours=1)).isoformat(),
                            "status": "built"}}},
        {"produced_at": _T0 - timedelta(hours=2),
         "attempts": {"x": {"at": (_T0 - timedelta(hours=2)).isoformat(),
                            "status": "all_rejected"}}},
        {"produced_at": _T0 - timedelta(hours=3),
         "attempts": {"x": {"at": (_T0 - timedelta(hours=3)).isoformat(),
                            "status": "no_commit"}}},
    ]
    folded = ROSTER.fold_attempts(rows)
    assert folded["x"].consecutive_failures == 1
    assert folded["x"].last_status == "no_commit"
    assert folded["x"].last_attempt_at == _T0
    assert folded["x"].unbuildable is False


def test_three_failures_in_a_row_fold_to_the_flag():
    rows = [
        {"produced_at": _T0 - timedelta(hours=h),
         "attempts": {"x": {"at": (_T0 - timedelta(hours=h)).isoformat(),
                            "status": "no_commit"}}}
        for h in (0, 24, 48)
    ]
    folded = ROSTER.fold_attempts(rows)
    assert folded["x"].consecutive_failures == 3
    assert folded["x"].unbuildable is True


def test_the_ledger_survives_a_jsonb_string_and_ignores_garbage_entries():
    """asyncpg hands jsonb back as a str on some codecs, and a receipt written
    by an older build carries no `attempts` at all. Neither may raise."""
    rows = [
        {"produced_at": _T0, "attempts": json.dumps(
            {"x": {"at": _T0.isoformat(), "status": "built"}})},
        {"produced_at": _T0 - timedelta(hours=1), "attempts": None},
        {"produced_at": _T0 - timedelta(hours=2), "attempts": {"y": "nonsense"}},
    ]
    folded = ROSTER.fold_attempts(rows)
    assert set(folded) == {"x"}


def test_the_outcome_of_this_run_lands_on_the_ledger_before_the_receipt_reads_it():
    """The third strike has to be named on the tick that earns it, not one tick
    later — an operator seeing "built no reference" for the third time should
    not have to count back through three receipts themselves."""
    prior = {_TARGET_B: ROSTER.Attempt(
        last_attempt_at=_T0 - timedelta(hours=24),
        last_status="no_commit", consecutive_failures=2,
    )}
    after = ROSTER.apply_outcomes(
        prior, [{"target_id": _TARGET_B, "status": "no_commit"}], at=_T0
    )
    assert after[_TARGET_B].consecutive_failures == 3
    assert ROSTER.unbuildable_targets(after) == [_TARGET_B]
    # and a build clears it outright
    cleared = ROSTER.apply_outcomes(
        after, [{"target_id": _TARGET_B, "status": "built"}], at=_T0
    )
    assert cleared[_TARGET_B].consecutive_failures == 0
    assert ROSTER.unbuildable_targets(cleared) == []


# ---------------------------------------------------------------------------
# the backoff value: env over knob, and a garbled value that must not raise
# ---------------------------------------------------------------------------


def test_the_env_wins_over_the_descriptor_knob(monkeypatch):
    """THE INVERSE of the two build walls, deliberately. This is the lever an
    operator reaches for while the lane is stuck on one country, when a
    descriptor PUT plus a re-register is the ceremony they cannot afford."""
    monkeypatch.setenv(ROSTER.RETRY_BACKOFF_ENV, "6")
    assert ROSTER.retry_backoff_hours(24) == (
        6.0, ROSTER.RETRY_BACKOFF_ENV
    )


def test_the_knob_is_read_when_the_env_is_unset(monkeypatch):
    monkeypatch.delenv(ROSTER.RETRY_BACKOFF_ENV, raising=False)
    assert ROSTER.retry_backoff_hours(3) == (
        3.0, "retry_backoff_hours"
    )
    assert ROSTER.retry_backoff_hours(None) == (
        float(ROSTER.DEFAULT_RETRY_BACKOFF_HOURS), "default"
    )


@pytest.mark.parametrize("raw", ["not-a-number", "-4", ""])
def test_a_garbled_backoff_env_falls_back_rather_than_taking_the_lane_down(
    monkeypatch, raw
):
    monkeypatch.setenv(ROSTER.RETRY_BACKOFF_ENV, raw)
    hours, source = ROSTER.retry_backoff_hours(None)
    assert hours == float(ROSTER.DEFAULT_RETRY_BACKOFF_HOURS)
    assert source == "default"


def test_zero_is_a_legal_backoff_and_is_not_confused_with_unset(monkeypatch):
    monkeypatch.setenv(ROSTER.RETRY_BACKOFF_ENV, "0")
    assert ROSTER.retry_backoff_hours(24) == (
        0.0, ROSTER.RETRY_BACKOFF_ENV
    )


# ---------------------------------------------------------------------------
# THE SAME PROPERTY, END TO END: real dispatch, real Postgres, real receipts
#
# The ordering tests above argue a pure function. What has to be true of the
# LANE is narrower and less forgiving: that the hour goes somewhere else, and
# that the reason is on a row a human can read. So every test below drives
# `deterministic.run_method` — the binding path the runtime uses — and seeds its
# history by writing REAL receipts through `write_finding`, the writer the
# actor uses. That is deliberate: the attempt ledger lives at
# `data -> 'data' -> 'attempts'` only because `writes._insert_analyst_output`
# dumps the whole FindingPayload, and a test that hand-rolled that envelope
# would keep passing on the day the platform changed it.
# ---------------------------------------------------------------------------


async def _seed_desk_for(conn, analyst_id: str, target_id: str, at: datetime):
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, analyst_id, analyst_version, run_id, target_id, kind, title,
             body, confidence, produced_at, created_at, schema_uri)
        VALUES ($1,$2,$3,$4,$5,'finding',$6,'body',1.0,$7,$7,$8)
        """,
        uuid4(), analyst_id, "0" * 16, uuid4(), target_id,
        f"{analyst_id} read", at, "iglu:legba/finding/jsonschema/1-0-0",
    )


async def _seed_attempt(conn, *, target_id: str, status: str, at: datetime):
    """Persist a REAL builder receipt recording one attempt, stamped at ``at``.

    The payload comes from ``RB.build_receipt`` and goes through
    ``write_finding``, so the row this leaves behind is byte-for-byte the shape
    a live tick leaves behind. ``produced_at`` is then moved back, because the
    writer stamps now() and the fixture needs a past tick.
    """
    from legba.data.provenance._core import AnalystContext
    from legba.data.provenance.writes import write_finding

    outcome = [{"target_id": target_id, "status": status}]
    payload = RB.build_receipt(
        t0=at, enabled=True, roster_size=2, due=[], per_target=outcome,
        warnings=[], cadence_days=7, window_days=14, dry_run=False,
        selected=[], attempts_now=ROSTER.attempt_records(outcome, at=at),
        unbuildable=[], backoff_hours=24, backoff_source="default",
    )
    row, dlq = await write_finding(
        conn,
        analyst_ctx=AnalystContext(
            analyst_id=ROSTER.RECEIPT_ANALYST_ID,
            analyst_version="0" * 16,
            run_id=uuid4(),
        ),
        payload=payload,
        derived_from=[],
    )
    assert dlq is None and row is not None, dlq
    await conn.execute(
        "UPDATE analyst_outputs SET produced_at = $1, created_at = $1 "
        "WHERE id = $2",
        at, row.id,
    )
    return row.id


@pytest_asyncio.fixture
async def roster_slate(pg_pool):
    """Clears both roster targets AND the builder's own receipt history."""
    targets = [_TARGET, _TARGET_B, "country_g20_us"]

    async def _wipe(conn):
        await conn.execute(
            "DELETE FROM unit_references WHERE target_id = ANY($1::text[])",
            targets,
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = ANY($1::text[])",
            targets,
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = $1",
            ROSTER.RECEIPT_ANALYST_ID,
        )
        await _clear_desk_findings(conn)

    async with pg_pool.acquire() as conn:
        await _wipe(conn)
    try:
        yield
    finally:
        async with pg_pool.acquire() as conn:
            await _wipe(conn)


def _failing_build():
    """A model that notes and never commits — the Argentina shape, in 1 s."""
    return (
        _ScriptedLLM(
            [], fallback="NOTE: | a matter | URL: https://www.bbc.com/news/haredi"
        ),
        _ScriptedBinding(pages={_ALLOWED_URL: _GOOD_PAGE},
                         results=[_usable_result()]),
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_tick_passes_over_the_most_overdue_target_when_it_just_failed(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """THE DEFECT, exactly. Both targets are due and country_g20_ar is the more
    overdue of the two — it has never built a reference at all. Under the old
    key it won every tick for ever. It failed forty minutes ago, so this tick
    goes to Israel and Argentina is told when it may come back."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    monkeypatch.delenv(ROSTER.RETRY_BACKOFF_ENV, raising=False)
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET, _T0 - timedelta(hours=2))
        await _seed_desk_for(conn, "escalation", _TARGET_B, _T0 - timedelta(hours=2))
        await _seed_attempt(
            conn, target_id=_TARGET_B, status="no_commit",
            at=_T0 - timedelta(minutes=40),
        )

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data

    # THE HOUR WENT SOMEWHERE ELSE
    assert [t["target_id"] for t in data["per_target"]] == [_TARGET]
    assert data["n_due"] == 2
    assert data["n_eligible"] == 1
    assert data["n_retry_backoff"] == 1

    queue = {d["target_id"]: d for d in data["due_queue"]}
    assert queue[_TARGET]["reason"] == ROSTER.REASON_NEVER_ATTEMPTED
    assert queue[_TARGET]["eligible"] is True
    assert queue[_TARGET_B]["reason"] == ROSTER.REASON_RETRY_BACKOFF
    assert queue[_TARGET_B]["eligible"] is False
    assert queue[_TARGET_B]["retry_after"] == (
        _T0 - timedelta(minutes=40) + timedelta(hours=24)
    ).isoformat()
    assert queue[_TARGET_B]["consecutive_failures"] == 1
    # NOT dropped: it is still counted as due and still on the row
    assert queue[_TARGET_B]["last_built_at"] is None

    # the receipt says what was chosen and why, and who is behind it
    body = result.finding.body
    assert f"CHOSE {_TARGET}: {ROSTER.REASON_NEVER_ATTEMPTED}" in body
    assert f"next  {_TARGET_B}: {ROSTER.REASON_RETRY_BACKOFF}" in body
    assert "1 consecutive failure(s)" in body

    # and this tick's own outcome is on the ledger for the next one
    assert data["attempts"] == {
        _TARGET: {"at": _T0.isoformat(), "status": "no_commit", "ok": False}
    }
    assert data["retry_backoff_hours"] == 24.0
    assert data["retry_backoff_source"] == "default"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_roster_with_no_attempts_at_all_orders_exactly_as_it_used_to(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """The R2 stagger is DEMOTED, not deleted. With an empty ledger every
    target sorts NULL-first and most-overdue breaks the tie, which is the
    behaviour that shipped — so the fix cannot have changed a fresh roster."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        for target in (_TARGET, _TARGET_B):
            await _seed_desk_for(conn, "escalation", target,
                                 _T0 - timedelta(hours=2))
        # IL carries a 9-day-old reference; AR has none, so AR is more overdue
        await conn.execute(
            """
            INSERT INTO unit_references (
                id, target_id, window_start, window_end, built_at, builder,
                ref_json, thin_dimensions, sha256
            ) VALUES ($1,$2,$3,$4,$5,'pytest','{}'::jsonb,'{}'::text[],$6)
            """,
            uuid4(), _TARGET, _T0 - timedelta(days=23), _T0 - timedelta(days=9),
            _T0 - timedelta(days=9), "a" * 64,
        )

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    assert [t["target_id"] for t in data["per_target"]] == [_TARGET_B]
    assert [d["target_id"] for d in data["due_queue"]] == [_TARGET_B, _TARGET]
    assert all(
        d["reason"] == ROSTER.REASON_NEVER_ATTEMPTED for d in data["due_queue"]
    )
    assert data["n_retry_backoff"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_every_due_target_backed_off_builds_nothing_and_spends_nothing(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """A receipt that said "no target due" here would send its reader to the
    cadence, which is not where the fault is. And the tick must cost NOTHING:
    no search, no fetch, no model round."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET_B,
                             _T0 - timedelta(hours=2))
        await _seed_attempt(conn, target_id=_TARGET_B, status="all_rejected",
                            at=_T0 - timedelta(hours=1))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    assert data["per_target"] == []
    assert data["n_due"] == 1 and data["n_eligible"] == 0
    assert "retry backoff" in result.finding.title
    assert binding.invocations == []
    assert llm.calls == 0
    assert any("retry backoff" in w for w in data["warnings"])
    # and nothing was recorded as an attempt, because nothing was attempted
    assert data["attempts"] == {}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_failed_target_comes_back_once_its_backoff_has_lapsed(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """The fence is a REST, not a removal. Twenty-five hours after the failure
    the same roster picks the same target."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET_B,
                             _T0 - timedelta(hours=2))
        await _seed_attempt(conn, target_id=_TARGET_B, status="no_commit",
                            at=_T0 - timedelta(hours=25))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    assert [t["target_id"] for t in data["per_target"]] == [_TARGET_B]
    queue = {d["target_id"]: d for d in data["due_queue"]}
    assert queue[_TARGET_B]["reason"] == ROSTER.REASON_OLDEST_ATTEMPT
    assert queue[_TARGET_B]["eligible"] is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_third_failure_in_a_row_flags_the_target_on_that_same_receipt(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """Three strikes is a FLAG and never a deletion: the target stays on the
    roster, keeps being retried at the backoff, and is named as the candidate
    for the operator top-up that is the only route left to it."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET_B,
                             _T0 - timedelta(hours=2))
        for hours in (73, 49, 25):
            await _seed_attempt(conn, target_id=_TARGET_B, status="no_commit",
                                at=_T0 - timedelta(hours=hours))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    assert [t["status"] for t in data["per_target"]] == ["no_commit"]
    assert data["unbuildable_by_lane"] == [_TARGET_B]
    body = result.finding.body
    assert "UNBUILDABLE BY THIS LANE after 3 consecutive failures" in body
    assert "reference_topup_packet.py --candidates" in body
    assert any("UNBUILDABLE BY THIS LANE" in w for w in data["warnings"])
    assert any("NOT dropped" in w for w in data["warnings"])
    # it is still on the roster and still due — nothing was removed
    assert data["n_due"] == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_receipt_this_tick_writes_is_the_ledger_the_next_tick_reads(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """THE ENVELOPE GUARD, closed end to end. A real receipt from
    ``RB.build_receipt`` goes through ``write_finding`` — the platform writer,
    which dumps the whole payload one level down — and comes back through
    ``ATTEMPTS_SQL``, the statement the scheduler runs. Nothing here hand-rolls
    the `data -> 'data' -> 'attempts'` path, so a change to how findings are
    persisted turns this red instead of silently emptying the ledger."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET_B,
                             _T0 - timedelta(hours=2))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    assert result.finding.data["attempts"][_TARGET_B]["status"] == "no_commit"

    from legba.data.provenance._core import AnalystContext
    from legba.data.provenance.writes import write_finding

    async with pg_pool.acquire() as conn:
        row, dlq = await write_finding(
            conn,
            analyst_ctx=AnalystContext(
                analyst_id=ROSTER.RECEIPT_ANALYST_ID,
                analyst_version="0" * 16, run_id=uuid4(),
            ),
            payload=result.finding,
            derived_from=[],
        )
        assert dlq is None and row is not None
        await conn.execute(
            "UPDATE analyst_outputs SET produced_at = $1 WHERE id = $2",
            _T0, row.id,
        )
        # the statement the scheduler runs, against the row the writer wrote
        ledger = await ROSTER.load_attempts(conn, t0=_T0)

    assert set(ledger) == {_TARGET_B}
    assert ledger[_TARGET_B].last_status == "no_commit"
    assert ledger[_TARGET_B].last_attempt_at == _T0
    assert ledger[_TARGET_B].consecutive_failures == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_operator_named_target_is_built_however_many_times_it_failed(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """Naming a target is a person saying "build this one now". A lane that
    answered "not for another nineteen hours" would be obeying its own
    scheduler over the operator running it. The strike history still travels on
    the receipt, so they can see what they are asking for."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET_B,
                             _T0 - timedelta(hours=2))
        for hours in (49, 25, 1):
            await _seed_attempt(conn, target_id=_TARGET_B, status="no_commit",
                                at=_T0 - timedelta(hours=hours))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), reference_targets=[_TARGET_B],
        min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    assert [t["target_id"] for t in data["per_target"]] == [_TARGET_B]
    queue = {d["target_id"]: d for d in data["due_queue"]}
    assert queue[_TARGET_B]["reason"] == ROSTER.REASON_NAMED
    assert queue[_TARGET_B]["consecutive_failures"] == 3
    assert queue[_TARGET_B]["unbuildable_by_lane"] is True
    assert f"CHOSE {_TARGET_B}: {ROSTER.REASON_NAMED}" in result.finding.body


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_backoff_of_zero_restores_the_old_relentless_behaviour(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """The env is the incident lever and it wins over the descriptor knob. At 0
    the fence is off and the failing target is picked again immediately — which
    is what the lane did before this fix, and is now something an operator has
    to ask for."""
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    monkeypatch.setenv(ROSTER.RETRY_BACKOFF_ENV, "0")
    async with pg_pool.acquire() as conn:
        await _seed_desk_for(conn, "escalation", _TARGET, _T0 - timedelta(hours=2))
        await _seed_desk_for(conn, "escalation", _TARGET_B, _T0 - timedelta(hours=2))
        await _seed_attempt(conn, target_id=_TARGET_B, status="no_commit",
                            at=_T0 - timedelta(minutes=40))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), retry_backoff_hours=24,
        min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    assert data["retry_backoff_hours"] == 0.0
    assert data["retry_backoff_source"] == ROSTER.RETRY_BACKOFF_ENV
    assert data["n_retry_backoff"] == 0
    # IL still goes first — never attempted beats attempted, backoff or no
    assert [t["target_id"] for t in data["per_target"]] == [_TARGET]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_flag_is_standing_state_and_the_warning_is_an_event(
    pg_pool, roster_slate, monkeypatch, notes_root
):
    """`unbuildable_by_lane` stays on every receipt — any single row should show
    which countries this lane cannot build. The paragraph that names the top-up
    route is written only for a target THIS RUN attempted, so the one warning
    that tells a reader what to DO does not become the line they learn to skip.
    """
    monkeypatch.setenv(RB.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        for target in (_TARGET, _TARGET_B):
            await _seed_desk_for(conn, "escalation", target,
                                 _T0 - timedelta(hours=2))
        for hours in (49, 25, 1):     # AR: three strikes, the last one recent
            await _seed_attempt(conn, target_id=_TARGET_B, status="no_commit",
                                at=_T0 - timedelta(hours=hours))

    llm, binding = _failing_build()
    result = await _run(
        pg_pool,
        extras={RB.LLM_DEPS_EXTRA_KEY: llm,
                RB.WEB_BINDING_DEPS_EXTRA_KEY: binding},
        as_of=_T0.isoformat(), min_developments=1, build_max_seconds=1,
    )
    data = result.finding.data
    # the hour went to Israel; Argentina is backed off AND flagged
    assert [t["target_id"] for t in data["per_target"]] == [_TARGET]
    assert data["unbuildable_by_lane"] == [_TARGET_B]
    assert "UNBUILDABLE BY THIS LANE after 3 consecutive failures" in (
        result.finding.body
    )
    queue = {d["target_id"]: d for d in data["due_queue"]}
    assert queue[_TARGET_B]["unbuildable_by_lane"] is True
    # ...and NOT the paragraph, because nothing was attempted against it here
    assert not [w for w in data["warnings"] if _TARGET_B in w and "NOT dropped" in w]

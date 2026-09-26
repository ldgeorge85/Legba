# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2/D6 — THE YIELD FIXES, measured against the run that motivated them.

THE RUN. ``analyst_traces`` row ``14e81d75-4bc8-49eb-a3ec-b2d079cd7c37``,
2026-09-17 06:43Z, ``country_g20_au``: 36 searches, 8 admitted fetches, 71 model
rounds, 306 s, 479 k core-plane tokens, **one usable page** and **zero
developments**. The receipt said ``no_commit`` with ``no_notes``.

WHAT THE TRACE ACTUALLY SHOWS, read call by call — and it is not "the model
would not take notes":

  1. The model was SHOWN
     ``https://www.theguardian.com/australia-news/2026/sep/17/imf-downgrades-…``
     and fetched ``https://theguardian.com/australia-news/…``. The second is a
     404 of 202 bytes; the first is 6,538 characters dated ``2026-09-17`` in
     JSON-LD. It did it twice. The run's only in-window page, lost to ``www.``.
  2. Four of the eight fetches went to ``reuters.com`` and ``bloomberg.com``,
     both measured unreadable to this lane. They were dropped from search
     RESULTS and fetched anyway, from URLs written out of the model's head.
  3. Thirteen queries named an outlet this lane cannot read, six carried a
     ``site:`` operator, eight carried a date literal.
  4. The model wrote one span-carrying NOTE (round 66) and the loop discarded
     it, because it arrived beside a tool call.
  5. At round 58, 230 s in, the model emitted the complete final JSON as a
     plain-text turn. It lacked the words REFERENCE COMPLETE, so the loop
     replied "Noted. Continue." and bought thirteen more calls.
  6. The forced commit turn said "commit the reference now from the notes you
     have" to a model with no notes, and got an empty reference in 15 seconds.

THE FIXTURE. The trace's URLs, hosts, dates and query shapes are the REAL ones.
The page BODIES are synthesised — the run archived nothing (every host was at
teaser depth, so ``archive/reference`` is empty) and republishing publisher prose
into a public repo to fix that would trade one problem for a worse one. Each
fixture page carries the real machine-readable date the live re-fetch found:
Guardian ``2026-09-17`` (in window), ABS ``2026-08-26`` (out), The Bulletin
``2020-06-22`` (out). That date mix is the whole point of half these tests.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

import pytest

from legba.data.analysts.deterministic_handlers import _reference_manifest as M
from legba.data.analysts.deterministic_handlers import _reference_notes as NOTES
from legba.data.analysts.deterministic_handlers._reference_fences import (
    DomainBlocklist,
    NoteGate,
    is_unfetchable,
)
from legba.data.analysts.deterministic_handlers._reference_loop import (
    ToolPlane,
    run_loop,
)
from legba.data.analysts.deterministic_handlers._reference_page import (
    ReferenceArchive,
)

from .test_reference_builder import (  # the shipped doubles, reused on purpose
    _Call,
    _Outcome,
    _Reply,
    _Result,
    _ScriptedLLM,
)

# ---------------------------------------------------------------------------
# The AU fixture — real URLs, real dates, synthesised bodies
# ---------------------------------------------------------------------------

GUARDIAN_URL = (
    "https://www.theguardian.com/australia-news/2026/sep/17/"
    "imf-downgrades-australian-economic-forecast-amid-fears-of-interest-rate-hike"
)
#: What the 06:43Z model actually asked for. Four characters short, 404.
GUARDIAN_URL_AS_TYPED = GUARDIAN_URL.replace("https://www.", "https://")
ABS_URL = (
    "https://www.abs.gov.au/statistics/economy/price-indexes-and-inflation/"
    "consumer-price-index-australia/latest-release"
)
REUTERS_URL = (
    "https://www.reuters.com/world/asia-pacific/"
    "imf-urges-australia-rba-stay-hawkish-warns-inflation-upside-2026-09-16/"
)
BLOOMBERG_URL = (
    "https://www.bloomberg.com/news/articles/2026-09-16/"
    "imf-urges-rba-to-stay-hawkish-warns-of-inflation-upside-risks"
)

GUARDIAN_SPAN = (
    "The IMF cut its forecast for Australian growth and urged the government "
    "to restrain spending as another interest rate rise came into view."
)
ABS_SPAN = (
    "The monthly consumer price index indicator rose over the twelve months to "
    "the reference period."
)

_DIMS = ("economic_coercion", "escalation", "leadership_transition")
WINDOW_START = date(2026, 9, 3)
WINDOW_END = date(2026, 9, 17)


def _page(span: str, published: str) -> str:
    return (
        '<html><head><script type="application/ld+json">'
        f'{{"datePublished":"{published}T05:00:00Z"}}</script></head>'
        f"<body><article><p>{span}</p><p>"
        + ("Context sentence that is not decisive. " * 60)
        + "</p></article></body></html>"
    )


AU_PAGES: dict[str, str] = {
    GUARDIAN_URL: _page(GUARDIAN_SPAN, "2026-09-17"),
    ABS_URL: _page(ABS_SPAN, "2026-08-26"),
}

AU_RESULTS = [
    {"url": GUARDIAN_URL, "title": "IMF downgrades Australian economic forecast",
     "snippet": "The IMF cut its forecast", "engine": "brave"},
    {"url": ABS_URL, "title": "Consumer Price Index, Australia",
     "snippet": "monthly CPI indicator", "engine": "google"},
    {"url": REUTERS_URL, "title": "IMF urges Australia's RBA to stay hawkish",
     "snippet": "wire copy", "engine": "brave"},
    {"url": BLOOMBERG_URL, "title": "IMF urges RBA to stay hawkish",
     "snippet": "wire copy", "engine": "brave"},
]


class _ReplayBinding:
    """The pack binding, serving the AU fixture and recording every query."""

    def __init__(self, results=None, pages=None) -> None:
        self.results = list(AU_RESULTS if results is None else results)
        self.pages = dict(AU_PAGES if pages is None else pages)
        self.queries: list[str] = []
        self.fetched: list[str] = []
        self.invocations: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name, args, **kwargs):
        self.invocations.append((tool_name, dict(args)))
        if tool_name == "web_search":
            self.queries.append(str(args.get("query")))
            return _Outcome(_Result({"results": self.results}))
        url = str(args.get("url"))
        self.fetched.append(url)
        body = self.pages.get(url)
        if body is None:
            # Exactly what the www-less Guardian URL served the live run.
            return _Outcome(_Result(
                {"url": url, "status_code": 404, "body": "<html>Not found</html>"}
            ))
        return _Outcome(_Result({
            "url": url, "status_code": 200, "body": body,
            "content_type": "text/html",
        }))


class _ManifestReadingLLM(_ScriptedLLM):
    """A stub that COMMITS FROM THE MANIFEST it is handed, and nothing else.

    It parses the commit turn the loop rendered, takes the first page marked IN
    WINDOW and the text quoted under it, and writes one development from that.
    It knows nothing about the fixture: if the commit prompt does not carry a
    quotable in-window page, this model commits nothing — which is what makes it
    a test of the prompt rather than of the stub.
    """

    def __init__(self, replies, *, refuse_to_commit: bool = False) -> None:
        super().__init__(replies, fallback="")
        self.refuse_to_commit = refuse_to_commit
        self.commit_turn = ""

    async def chat_complete(self, messages, **kwargs):
        if kwargs.get("tools") is None:  # the commit turn withholds the specs
            self.calls += 1
            self.messages_seen.append([dict(m) for m in messages])
            self.tools_seen.append(None)
            self.kwargs_seen.append(dict(kwargs))
            self.commit_turn = str(messages[-1].get("content") or "")
            return _Reply(self._commit(self.commit_turn))
        return await super().chat_complete(messages, **kwargs)

    def _commit(self, prompt: str) -> str:
        developments = []
        if not self.refuse_to_commit:
            for url, span in _in_window_pages(prompt):
                developments.append({
                    "item_id": "RD-AU-C-1",
                    "summary": "From the manifest.",
                    "decisive_span": span,
                    "outlet": "manifest",
                    "publish_date": "2026-09-17",
                    "source_url": url,
                    "source_tier": 2,
                    "significance": "major",
                    "hindsight": False,
                    "dimension": ["economic_coercion"],
                })
                break
        return json.dumps({
            "header": {"country": "AU"},
            "ref_bands": {d: "watch" for d in _DIMS},
            "ref_developments": developments,
            "gaps": ["searched for escalation; nothing dated in window"],
            "ref_direction": {"direction": "stable", "why": "x"},
        })


def _in_window_pages(prompt: str) -> list[tuple[str, str]]:
    """``(url, first sentence of the quotable text)`` for each IN WINDOW page."""
    out: list[tuple[str, str]] = []
    lines = prompt.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not (stripped.startswith("[") and "] http" in stripped):
            continue
        url = stripped.split("] ", 1)[1].strip()
        block = "\n".join(lines[index + 1: index + 8])
        if "IN WINDOW" not in block:
            continue
        for candidate in block.splitlines():
            candidate = candidate.strip()
            if candidate.startswith('"'):
                text = candidate.strip('"')
                out.append((url, text.split(". ")[0] + "."))
                break
    return out


def _au_gathering_replies() -> list[Any]:
    """The 06:43Z model's own habits, replayed: site:, dates, outlet names,
    the www-less URL, and fetches of two hosts this lane cannot read."""
    return [
        _Reply(calls=[_Call(
            "web_search",
            {"query": "site:reuters.com Australia IMF downgrade September 2026"},
            "c1")]),
        _Reply(calls=[_Call(
            "fetch_page", {"url": GUARDIAN_URL_AS_TYPED}, "c2")]),
        _Reply(calls=[_Call("web_search", {"query": "2026-09-05 Australia"}, "c3")]),
        _Reply(calls=[_Call("fetch_page", {"url": REUTERS_URL}, "c4")]),
        _Reply(calls=[_Call("fetch_page", {"url": BLOOMBERG_URL}, "c5")]),
        _Reply(calls=[_Call("fetch_page", {"url": ABS_URL}, "c6")]),
        _Reply("REFERENCE COMPLETE"),
    ]


async def _replay(llm, binding, **kwargs):
    plane = ToolPlane(
        binding=binding, archive=ReferenceArchive(None),
        blocklist=DomainBlocklist(),
    )
    result = await run_loop(
        llm=llm, plane=plane, instruction="s", first_user="Begin.",
        dimensions=_DIMS, tool_call_cap=kwargs.pop("tool_call_cap", 40),
        note_gate=NoteGate(), min_noted=kwargs.pop("min_noted", 0),
        window_start=WINDOW_START, window_end=WINDOW_END, **kwargs,
    )
    return result, plane


# ---------------------------------------------------------------------------
# 1. The headline: the same run, replayed, now commits
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_au_trace_replayed_commits_a_development_from_the_guardian_page():
    """THE WHOLE TRACK, in one test. Same model habits, same URLs, same dates —
    and the build now ends with a development anchored to the in-window Guardian
    page, whose URL the model never typed correctly once."""
    binding = _ReplayBinding()
    llm = _ManifestReadingLLM(_au_gathering_replies())
    result, plane = await _replay(llm, binding)

    assert result.reference is not None
    developments = result.reference["ref_developments"]
    assert len(developments) >= 1, result.reference
    assert developments[0]["source_url"] == GUARDIAN_URL
    assert developments[0]["decisive_span"] in GUARDIAN_SPAN

    # and the span is really in the archived page, which is what the fence asks
    page = plane.archive.lookup_exact(GUARDIAN_URL)
    assert page is not None
    assert developments[0]["decisive_span"] in plane.archive.text_for(page)


@pytest.mark.asyncio
async def test_the_same_replay_with_a_refusing_model_still_has_the_material():
    """When the stub refuses to use what it was handed, the LOOP's record is
    what says so. ``has_material`` is the test the builder runs to name
    ``empty_commit_with_material``, and it reads the manifest, never the model."""
    binding = _ReplayBinding()
    llm = _ManifestReadingLLM(_au_gathering_replies(), refuse_to_commit=True)
    result, _plane = await _replay(llm, binding)

    assert result.reference is not None
    assert result.reference["ref_developments"] == []
    assert M.has_material(result.manifest_entries, WINDOW_START, WINDOW_END)
    assert [e.url for e in M.in_window_entries(
        result.manifest_entries, WINDOW_START, WINDOW_END
    )] == [GUARDIAN_URL]


# ---------------------------------------------------------------------------
# 2. The manifest is the loop's, not the model's
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_manifest_is_populated_without_a_single_model_note():
    """The 06:43Z receipt read ``no_notes``. The manifest must not care."""
    binding = _ReplayBinding()
    llm = _ManifestReadingLLM([
        _Reply(calls=[_Call("web_search", {"query": "australia imf"}, "c1")]),
        _Reply(calls=[_Call("fetch_page", {"url": GUARDIAN_URL}, "c2")]),
        # prose, not the NOTE form — no pipe, so nothing the carry would keep
        _Reply("Read it. Moving on."),
        _Reply(calls=[_Call("fetch_page", {"url": ABS_URL}, "c3")]),
        _Reply("REFERENCE COMPLETE"),
    ])
    result, _plane = await _replay(llm, binding)

    assert [n for n in result.notes if "|" in n] == [], (
        "this model wrote no NOTE line at all — that is the point"
    )
    assert len(result.manifest_entries) == 2
    guardian = result.manifest_entries[0]
    assert guardian.url == GUARDIAN_URL
    assert guardian.publish_date == "2026-09-17"
    assert guardian.date_source == "json-ld"
    assert GUARDIAN_SPAN[:40] in guardian.excerpt
    assert guardian.archive_sha256
    assert guardian.title == "IMF downgrades Australian economic forecast"


def test_the_commit_turn_marks_the_window_verdict_on_every_page():
    entries = [
        M.ManifestEntry(url=GUARDIAN_URL, host="theguardian.com",
                        publish_date="2026-09-17", date_source="json-ld",
                        excerpt=GUARDIAN_SPAN),
        M.ManifestEntry(url=ABS_URL, host="abs.gov.au",
                        publish_date="2026-08-26", date_source="time-datetime",
                        excerpt=ABS_SPAN),
        M.ManifestEntry(url="https://x.example/undated", host="x.example",
                        excerpt="something"),
    ]
    rendered = M.commit_prompt(
        entries, "Good.", window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert "BUILD YOUR DEVELOPMENTS ONLY FROM THE PAGES BELOW" in rendered
    assert f"[1] {GUARDIAN_URL}" in rendered
    assert "IN WINDOW" in rendered
    assert GUARDIAN_SPAN in rendered
    # the two it may NOT use are named with the reason, not silently dropped
    assert "OUTSIDE THE WINDOW" in rendered
    assert ABS_URL in rendered
    assert "UNDATED — cannot carry a development" in rendered
    # ...and their text is never offered for quoting
    assert ABS_SPAN not in rendered


def test_a_manifest_with_no_in_window_page_says_so_rather_than_going_quiet():
    entries = [M.ManifestEntry(
        url=ABS_URL, host="abs.gov.au", publish_date="2026-08-26",
        excerpt=ABS_SPAN,
    )]
    rendered = M.render_manifest(entries, WINDOW_START, WINDOW_END)
    assert "NO PAGE THIS RUN READ CARRIES A MACHINE-READABLE PUBLISH DATE" in rendered
    assert not M.has_material(entries, WINDOW_START, WINDOW_END)


def test_the_manifest_never_calls_a_page_in_window_when_no_window_was_stated():
    """An unstated window admits nothing. The date gate would reject it anyway,
    and a manifest that disagreed with the gate is the one lie it cannot tell."""
    entry = M.ManifestEntry(url=GUARDIAN_URL, host="theguardian.com",
                            publish_date="2026-09-17", excerpt="x")
    assert not entry.in_window(None, None)
    assert not M.has_material([entry], None, None)


def test_the_manifest_respects_its_total_size_cap():
    entries = [
        M.ManifestEntry(
            url=f"https://ex.example/{i}", host="ex.example",
            publish_date="2026-09-10", excerpt="q" * M.MANIFEST_EXCERPT_CHARS,
        )
        for i in range(40)
    ]
    rendered = M.render_manifest(entries, WINDOW_START, WINDOW_END)
    assert len(rendered) < M.MANIFEST_TOTAL_CHARS * 1.5
    assert "manifest at its size cap" in rendered
    assert "https://ex.example/39" in rendered, "every page is still NAMED"


# ---------------------------------------------------------------------------
# 3. Query hygiene
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "query,expected_absent",
    [
        ("site:reuters.com Australia IMF downgrade September 2026", "site:"),
        ("2026-09-05 Australia inflation", "2026-09-05"),
        ("IMF downgrades Australian economy September 2026 Reuters", "Reuters"),
        ("Australia IMF outlook September 2026 FT.com", "FT.com"),
    ],
)
def test_a_query_that_pre_filters_is_rewritten_not_sent(query, expected_absent):
    cleaned, reasons = M.clean_query(query)
    assert reasons
    assert expected_absent.lower() not in cleaned.lower()
    assert cleaned.strip()


def test_stacked_exact_match_quotes_are_unquoted():
    cleaned, reasons = M.clean_query('"2026-09" "Australia" "Reuters"')
    assert reasons
    assert '"' not in cleaned


def test_an_ordinary_query_is_left_exactly_alone():
    for query in (
        "Australia tariffs imposed imports",
        "IMF urges Australia to restrain spending",
        "Australia cabinet reshuffle minister appointed",
    ):
        assert M.clean_query(query) == (query, [])


def test_a_query_the_rules_would_erase_is_sent_UNCHANGED():
    """A hygiene rule that can turn a real query into no query is worse than the
    habit it corrects."""
    assert M.clean_query("site:gov.au")[0] == "site:gov.au"
    assert M.clean_query("Reuters")[0] == "Reuters"


@pytest.mark.asyncio
async def test_the_pack_never_sees_the_site_operator():
    binding = _ReplayBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    payload = await plane.web_search(
        "site:treasury.gov.au \"2026\" \"September\" \"press release\""
    )
    assert "site:" not in binding.queries[0]
    assert payload["query_rewritten"]
    assert plane.queries.as_record()["queries_rewritten"] == 1


def test_the_instruction_hands_the_model_patterns_for_its_own_dimensions():
    guidance = M.query_guidance("Australia", ["economic_coercion", "escalation"])
    assert "Australia tariffs imposed imports" in guidance
    assert "Australia border incident troops clash" in guidance
    assert "site:" in guidance and "Never a `site:` operator" in guidance
    for dimension in M.QUERY_PATTERNS:
        assert 3 <= len(M.QUERY_PATTERNS[dimension]) <= 5


def test_an_unknown_dimension_still_gets_patterns():
    guidance = M.query_guidance("Australia", ["some_new_desk"])
    assert "some new desk" in guidance


# ---------------------------------------------------------------------------
# 4. Unfetchable hosts, and the URL the model was shown
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_an_unfetchable_host_never_reaches_a_fetch_call():
    """The discovery drop had no fetch fence behind it, so the 06:43Z build
    spent four of its eight fetch calls on hosts this lane cannot read."""
    assert is_unfetchable(BLOOMBERG_URL), "D6 added bloomberg.com, measured"
    binding = _ReplayBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())

    for url in (REUTERS_URL, BLOOMBERG_URL):
        payload = await plane.fetch_page(url)
        assert payload["status"] == "refused"
        assert "MEASURED unreadable" in payload["error"]
    assert binding.fetched == [], "not one invocation reached the pack"
    assert sorted(set(plane.unfetchable_refused)) == [
        "bloomberg.com", "reuters.com",
    ]


@pytest.mark.asyncio
async def test_a_refused_unfetchable_fetch_costs_no_tool_budget():
    binding = _ReplayBinding()
    llm = _ManifestReadingLLM([
        _Reply(calls=[_Call("web_search", {"query": "australia imf"}, "c1")]),
        _Reply(calls=[_Call("fetch_page", {"url": REUTERS_URL}, "c2")]),
        _Reply(calls=[_Call("fetch_page", {"url": BLOOMBERG_URL}, "c3")]),
        _Reply(calls=[_Call("fetch_page", {"url": GUARDIAN_URL}, "c4")]),
        _Reply("REFERENCE COMPLETE"),
    ])
    result, _plane = await _replay(llm, binding)
    assert result.tool_calls == 2, "one search and one fetch; two refusals free"


@pytest.mark.asyncio
async def test_the_www_defect_is_repaired_to_the_url_the_model_was_shown():
    """The single highest-yield line in this track. 4 characters, 2 fetch calls,
    and the only in-window page of the 06:43Z build."""
    binding = _ReplayBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    await plane.web_search("australia imf")

    payload = await plane.fetch_page(GUARDIAN_URL_AS_TYPED)
    assert binding.fetched == [GUARDIAN_URL], "the www. was put back"
    assert payload["status"] == 200
    assert payload["publish_date"] == "2026-09-17"
    assert "URL REPAIRED" in payload["repair_note"]
    assert plane.url_repairs == [
        {"asked": GUARDIAN_URL_AS_TYPED, "fetched": GUARDIAN_URL}
    ]


@pytest.mark.asyncio
async def test_a_url_no_search_ever_offered_is_fetched_exactly_as_written():
    """The repair never INVENTS a target: carried notes and re-fetches name
    pages no search in THIS run returned."""
    unseen = "https://www.bbc.com/news/world-australia-99"
    binding = _ReplayBinding(pages={unseen: _page("A sentence.", "2026-09-10")})
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    await plane.fetch_page(unseen)
    assert binding.fetched == [unseen]
    assert plane.url_repairs == []


def test_repair_url_is_a_pure_function_of_what_was_offered():
    offered = {M.offered_key(GUARDIAN_URL): GUARDIAN_URL}
    assert M.repair_url(GUARDIAN_URL_AS_TYPED, offered)[0] == GUARDIAN_URL
    assert M.repair_url(GUARDIAN_URL, offered) == (GUARDIAN_URL, "")
    assert M.repair_url("https://elsewhere.example/x", offered) == (
        "https://elsewhere.example/x", ""
    )


# ---------------------------------------------------------------------------
# 5. The fetch-first rule and the ratio guard
# ---------------------------------------------------------------------------


def test_the_ratio_guard_is_silent_early_and_fires_on_the_shape_of_the_au_build():
    # the 06:43Z build's own numbers
    assert M.ratio_breached(searches=36, fetches=8, tool_calls=44)
    # a healthy build (the UA run that built 5 developments: 59 searches, 21
    # fetches) is left alone
    assert not M.ratio_breached(searches=59, fetches=21, tool_calls=80)
    # and nothing is judged before the build has opened its subject
    assert not M.ratio_breached(searches=11, fetches=0, tool_calls=11)


@pytest.mark.asyncio
async def test_the_ratio_guard_fires_in_a_real_loop_and_is_counted():
    binding = _ReplayBinding()
    searches = [
        _Reply(calls=[_Call("web_search", {"query": f"australia q{i}"}, f"c{i}")])
        for i in range(30)
    ]
    llm = _ManifestReadingLLM(searches + [_Reply("REFERENCE COMPLETE")])
    result, _plane = await _replay(llm, binding, tool_call_cap=60)

    assert result.ratio_nudges >= 1
    nudges = [t for t in llm.user_turns() if "STOP SEARCHING" in t]
    assert nudges, llm.user_turns()


@pytest.mark.asyncio
async def test_two_searches_without_a_fetch_inject_the_fetch_first_rule():
    binding = _ReplayBinding()
    llm = _ManifestReadingLLM([
        _Reply(calls=[_Call("web_search", {"query": "australia a"}, "c1")]),
        _Reply(calls=[_Call("web_search", {"query": "australia b"}, "c2")]),
        _Reply(calls=[_Call("fetch_page", {"url": GUARDIAN_URL}, "c3")]),
        _Reply("REFERENCE COMPLETE"),
    ])
    _result, _plane = await _replay(llm, binding)
    rule = [t for t in llm.user_turns() if "2 searches without fetching" in t]
    assert rule, llm.user_turns()
    assert "including any `www.`" in rule[0]


# ---------------------------------------------------------------------------
# 6. The NOTE turn, and the two loop defects the trace exposed
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_note_beside_a_tool_call_is_recorded_and_satisfies_the_gate():
    """Round 66 of the 06:43Z build wrote a real span-carrying NOTE beside a
    tool call and the loop discarded it, then refused the call for skipping the
    note it had just written."""
    binding = _ReplayBinding()
    gate = NoteGate()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    llm = _ManifestReadingLLM([
        _Reply(calls=[_Call("fetch_page", {"url": GUARDIAN_URL}, "c1")]),
        _Reply("NOTE: | IMF cut the forecast | URL: x | DIMS: economic_coercion",
               calls=[_Call("web_search", {"query": "australia rba"}, "c2")]),
        _Reply("REFERENCE COMPLETE"),
    ])
    result = await run_loop(
        llm=llm, plane=plane, instruction="s", first_user="Begin.",
        dimensions=_DIMS, tool_call_cap=40, note_gate=gate, min_noted=0,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert any("IMF cut the forecast" in n for n in result.notes)
    assert result.note_gate["refusals"] == 0


def test_the_note_gate_refuses_at_most_once_per_fetch():
    gate = NoteGate()
    gate.after_fetch(ok=True)
    assert gate.should_refuse() is True
    gate.refuse()
    assert gate.should_refuse() is False, "the second attempt is let through"
    assert gate.as_record()["exceptions_granted"] == 1
    assert gate.as_record()["refusals"] == 1


@pytest.mark.asyncio
async def test_a_reference_emitted_without_the_phrase_enters_the_commit_turn():
    """Round 58: the complete final JSON, as prose, answered with 'Noted.
    Continue.' and thirteen more calls."""
    binding = _ReplayBinding()
    empty = json.dumps({
        "header": {"country": "AU"},
        "ref_bands": {d: "insufficient-basis" for d in _DIMS},
        "ref_developments": [],
        "gaps": ["nothing dated"],
    })
    llm = _ManifestReadingLLM([
        _Reply(calls=[_Call("web_search", {"query": "australia imf"}, "c1")]),
        _Reply(calls=[_Call("fetch_page", {"url": GUARDIAN_URL}, "c2")]),
        _Reply("NOTE: | read it |"),
        _Reply(empty),
    ])
    result, _plane = await _replay(llm, binding)

    assert result.commit_trigger == "model_emitted_json"
    assert "BUILD YOUR DEVELOPMENTS ONLY FROM THE PAGES BELOW" in llm.commit_turn
    # and the SECOND ask, with the manifest in front of it, is not empty
    assert result.reference["ref_developments"], result.reference


def test_prose_and_a_note_are_never_mistaken_for_the_final_reference():
    from legba.data.analysts.deterministic_handlers._reference_loop import (
        _looks_like_reference,
    )

    assert not _looks_like_reference("NOTE: | a | URL: b |")
    assert not _looks_like_reference("I will keep searching.")
    assert not _looks_like_reference('{"datePublished": "2026-09-17"}')
    assert _looks_like_reference(
        '{"header": {"country": "AU"}, "ref_developments": []}'
    )


# ---------------------------------------------------------------------------
# 7. The carry, and the named outcome
# ---------------------------------------------------------------------------


def test_a_failed_build_carries_its_pages_even_with_no_model_notes(tmp_path,
                                                                   monkeypatch):
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path))
    entries = [
        M.ManifestEntry(url=GUARDIAN_URL, host="theguardian.com",
                        title="IMF downgrades", publish_date="2026-09-17",
                        excerpt=GUARDIAN_SPAN),
        M.ManifestEntry(url=ABS_URL, host="abs.gov.au",
                        publish_date="2026-08-26", excerpt=ABS_SPAN),
        M.ManifestEntry(url="https://dead.example/x", host="dead.example"),
    ]
    record = NOTES.save(
        "country_g20_au", [], stop_reason="committed",
        pages=M.carry_lines(entries),
    )
    assert record is not None, "the 06:43Z build carried NOTHING; this must not"
    assert record["note_lines"] == 0
    assert record["page_lines"] == 2, "the page that served nothing is not a lead"

    loaded = NOTES.load("country_g20_au")
    assert loaded is not None
    preamble = NOTES.preamble(loaded)
    assert GUARDIAN_URL in preamble
    assert "PAGES THAT ATTEMPT ACTUALLY READ" in preamble
    assert "LEADS, NOT EVIDENCE" in preamble


def test_empty_commit_with_material_is_a_failed_attempt_like_the_others():
    from legba.data.analysts.deterministic_handlers import _reference_roster as R

    assert M.EMPTY_COMMIT_WITH_MATERIAL in R.FAILED_STATUSES
    assert M.EMPTY_COMMIT_WITH_MATERIAL in R.ATTEMPTED_STATUSES
    records = R.attempt_records(
        [{"target_id": "country_g20_au",
          "status": M.EMPTY_COMMIT_WITH_MATERIAL}],
        at=__import__("datetime").datetime(2026, 9, 17, tzinfo=None).replace(
            tzinfo=__import__("datetime").timezone.utc),
    )
    assert records["country_g20_au"]["ok"] is False


# ---------------------------------------------------------------------------
# 8. A fetch is of an ADMITTED result, or it is not a fetch
# ---------------------------------------------------------------------------
#
# THE MEASURED CASE is the 06:43Z Guardian URL: four characters off the one the
# model was shown, twice, for the run's only in-window page. The repair above
# catches that shape. This fence catches the one it cannot — a URL with no
# offered near-match at all.
#
# NOT claimed: that every unoffered URL is fabricated. The 07:23Z live dry run's
# ``dfat.gov.au`` / ``defence.gov.au`` fetches looked composed and are in fact
# REAL pages (one dated inside the window) that intermittently hang for this
# lane. The fence rests on the design rule — the model finds through search, the
# harness decides what is fetchable — and on being cheap: no tool budget, the
# real URLs named back, and stood down entirely before anything is offered.


@pytest.mark.asyncio
async def test_an_unoffered_url_is_refused_and_the_real_ones_are_named():
    binding = _ReplayBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    await plane.web_search("australia imf")

    payload = await plane.fetch_page(
        "https://www.abs.gov.au/statistics/invented/path"
    )
    assert payload["status"] == "refused"
    assert "composed rather than copied" in payload["error"]
    assert payload["urls_on_offer_for_this_host"] == [ABS_URL]
    assert binding.fetched == [], "nothing reached the pack"
    assert plane.unoffered_refused == ["abs.gov.au"]


@pytest.mark.asyncio
async def test_the_fence_stands_down_before_anything_has_been_offered():
    """A build whose first act is a carried lead must still be able to fetch."""
    binding = _ReplayBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    await plane.fetch_page(GUARDIAN_URL)
    assert binding.fetched == [GUARDIAN_URL]
    assert plane.unoffered_refused == []


@pytest.mark.asyncio
async def test_a_carried_page_is_admitted_without_a_search():
    """The loop recorded that URL after a successful fetch last time, so it is
    known to exist — the one thing the offered set stands for."""
    binding = _ReplayBinding()
    plane = ToolPlane(binding=binding, archive=ReferenceArchive(None),
                      blocklist=DomainBlocklist())
    await plane.web_search("australia imf")          # offers something ELSE
    other = "https://www.bbc.com/news/world-australia-77"
    binding.pages[other] = _page("Carried sentence.", "2026-09-09")

    assert (await plane.fetch_page(other))["status"] == "refused"
    assert plane.admit([other]) == 1
    payload = await plane.fetch_page(other)
    assert payload["status"] == 200
    assert binding.fetched == [other]


def test_only_the_harness_written_PAGE_lines_are_admitted_from_a_carry():
    """A URL the MODEL wrote in a NOTE is a claim about a page. Admitting it
    would let a hallucinated URL survive a build by being written down once."""
    record = {
        "notes": "NOTE: | a | URL: https://invented.example/model-wrote-this |",
        "pages": (
            f"PAGE: | URL: {GUARDIAN_URL} | OUTLET: theguardian.com | "
            "DATE: 2026-09-17 | TITLE: IMF\n"
            "PAGE: | URL: not-a-url | OUTLET: x | DATE: 2026-09-17 | TITLE: y"
        ),
    }
    assert NOTES.carried_urls(record) == [GUARDIAN_URL]


@pytest.mark.asyncio
async def test_a_refused_composed_url_costs_no_tool_budget():
    binding = _ReplayBinding()
    llm = _ManifestReadingLLM([
        _Reply(calls=[_Call("web_search", {"query": "australia imf"}, "c1")]),
        _Reply(calls=[_Call(
            "fetch_page", {"url": "https://www.dfat.gov.au/invented"}, "c2")]),
        _Reply(calls=[_Call(
            "fetch_page", {"url": "https://www.defence.gov.au/invented"}, "c3")]),
        _Reply(calls=[_Call("fetch_page", {"url": GUARDIAN_URL}, "c4")]),
        _Reply("REFERENCE COMPLETE"),
    ])
    result, _plane = await _replay(llm, binding)
    assert result.tool_calls == 2, "one search and one real fetch"
    assert sorted(result.unoffered_refused) == ["defence.gov.au", "dfat.gov.au"]
    assert result.reference["ref_developments"], "and it still commits"

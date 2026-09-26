# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 7a — the ``contrary_evidence_pass`` deterministic sub-handler.

Three layers, deliberately.

**Pure** — the properties that must hold with no database and no network, and
they are the properties the whole lane rests on: the counter-query is the
OPPOSITE of the claim rather than a restatement of it; the paraphrase gate
refuses a restatement BEFORE a search is spent; the stance derivation is a
closed table over a calibrated vocabulary; and nothing anywhere on this path
renders a verdict.

**The migration, on the real driver** — the vocabularies, the evidence CHECK
(a row may not claim a contradiction it cannot show), and the unique key that
makes a re-run inside a day a no-op.

**End-to-end through the REAL binding path** — a live migrated Postgres, the
REAL ``wire_standing_auditor_web_pack`` production wiring under this pass's own
extras key, the REAL ``Agency.run_pack_tool`` three-way gate, the REAL
``web_access`` ActionPack off the registry, the REAL ``SearxngSearchHandler``
and the REAL ``web_search`` / ``web_fetch`` tool handlers. The only doubles are
SOCKETS: the LLM, the registry GET, the search provider's HTTP GET, the fetch
transport and the robots verdict. Nothing in the handler is monkeypatched, so if
the pass stopped routing through the pack — an ad-hoc httpx call, say — the
sockets would go unconsulted AND no ``action_pack_invocations`` ledger rows
would land. That ledger assertion is what makes this a binding-path test rather
than a shape test, and #85 is why it is written that way: the standing
auditor's search leg was dead for five weeks while its suite stayed green,
because the double sat at the seam under test instead of at the socket.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from legba.data.analysts import claim_contradiction as r2
from legba.data.analysts import contrary_tension as ct
from legba.data.analysts.agency import Agency, AgencyToolBinding
from legba.data.analysts.assembly_render import render_assembly_body
from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers._external_audit_claims import (
    claim_key,
)
from legba.data.analysts.deterministic_handlers import _contrary_fences as cf
from legba.data.analysts.deterministic_handlers import _contrary_query as cq
from legba.data.analysts.deterministic_handlers import _contrary_stance as cs
from legba.data.analysts.deterministic_handlers import _contrary_store as store
from legba.data.analysts.deterministic_handlers import contrary_evidence_pass as cep
from legba.data.analysts.handler_options import HANDLER_OPTIONS
from legba.data.provenance.kinds import TRACE_ONLY

_DESCRIPTORS = Path(__file__).resolve().parents[2] / "descriptors"
_MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src" / "legba" / "data" / "migrations"
)
_MIGRATION = _MIGRATIONS_DIR / "0221_claim_contentions.sql"
#: 0222 adds the four FENCE columns and their checks. Applied here beside 0221
#: because the two are one table and a test that re-ran only the first would
#: prove idempotency of half a schema.
_MIGRATION_FENCES = _MIGRATIONS_DIR / "0222_contrary_fences.sql"

_CLAIM_SHUT = "The Strait of Hormuz remains effectively shut to commercial traffic."
_CLAIM_RESIGNED = "Minister Kovac resigned on Monday after the budget vote."


# ---------------------------------------------------------------------------
# 1) ONE vocabulary, not two
# ---------------------------------------------------------------------------


def test_counter_headwords_are_drawn_from_the_r2_vocabulary() -> None:
    """The counter headwords ORDER an existing vocabulary; they never add one.

    R2's polarity table earned its precision with a live sweep — 57 candidate
    pairs across 24 of 32 desks, almost all false, pruned to zero. A second
    table of "opposite words" maintained beside it would drift away from that
    calibration within a release and nothing would say so. This test is what
    says so: an edit to ``_POLARITY_GROUPS`` that drops a term turns
    ``_contrary_query`` red instead of quietly emitting a word the detector no
    longer knows.
    """
    assert cq.headwords_are_in_vocabulary() == []
    # ...and every group R2 knows has a counter for BOTH signs, so a claim that
    # takes a side can always be negated deterministically.
    for group in r2.POLARITY_GROUPS:
        for sign in (1, -1):
            assert cq.COUNTER_HEADWORDS.get((group, sign)), (group, sign)


def test_qualifiers_are_disjoint_from_the_polarity_vocabulary() -> None:
    """A qualifier narrows a state; it may never BE one.

    Overlap would make "temporarily" both the thing being qualified and the
    qualification, and the stance rules would read one sentence two ways.
    """
    assert cs.qualifiers_are_disjoint() == []
    assert not (cs.QUALIFIERS & r2.NEGATORS)


# ---------------------------------------------------------------------------
# 2) The counter-query is the OPPOSITE, not a restatement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claim,group,sign,must_contain",
    [
        (_CLAIM_SHUT, "closure", 1, "reopened"),
        ("The Bab el-Mandeb crossing reopened to shipping on Tuesday.",
         "closure", -1, "closed"),
        ("Heavy fighting continued around Rafah through the weekend.",
         "hostilities", 1, "ceasefire"),
        ("A ceasefire in northern Gaza has held since Friday.",
         "hostilities", -1, "fighting"),
        ("A fuel shortage continued across Karachi this week.",
         "supply", 1, "restored"),
        ("Russian forces captured the town of Vuhledar on Sunday.",
         "control", 1, "retreated"),
        ("Washington sanctioned four Iranian shipping firms on Thursday.",
         "sanctions", 1, "lifted"),
    ],
)
def test_the_polarity_negation_asks_for_the_opposite_state(
    claim: str, group: str, sign: int, must_contain: str,
) -> None:
    """Table-driven over the calibrated vocabulary.

    Three properties per row, and the third is the one the capture worries
    about: the query names the same subject, it carries the OPPOSITE state's
    words, and it carries none of the claim's own polarity words — so it cannot
    retrieve the corroboration the standing auditor already retrieves.
    """
    counter = cq.counter_query_from_polarity(claim)
    assert counter is not None and counter.issued, claim
    assert counter.polarity_group == group
    assert counter.polarity_sign == sign
    assert must_contain in counter.query
    own_side = r2.polarity_side_terms(group, sign)
    assert not (set(counter.query.split()) & set(own_side)), counter.query
    assert counter.novel_tokens > 0
    # REPLAYABLE: the same claim yields the same string forever. That is the
    # whole argument for preferring this leg over a model call.
    assert cq.counter_query_from_polarity(claim).query == counter.query


def test_a_compound_claim_gets_no_deterministic_negation() -> None:
    """Two polarity groups in one sentence have no single negation.

    Countering one clause while recording a stance on the whole claim is the
    shape that manufactures disagreement, so the deterministic leg declines and
    the model leg takes it (where the paraphrase gate applies).
    """
    compound = (
        "The strait remains shut to tankers while a ceasefire holds ashore."
    )
    assert len(r2.polarity_of(compound)) > 1
    assert cq.counter_query_from_polarity(compound) is None


def test_a_contrastive_sentence_gets_no_deterministic_negation() -> None:
    """THE LANE'S OWN SAMPLED AUDIT PUT THIS RULE HERE, and the row is worth
    keeping in the test:

        "Australia's high-level military posture remains unchanged, BUT the
         initial operational capability of new MQ-4C Triton UAVs ... adds a
         modest boost to its long-range surveillance."

    ``operational`` sits on the ``closure`` group's negative side — it is there
    for "the strait is operational" — so the claim read as a CHOKEPOINT
    statement and the counter-query came back "triton uavs peregrine isr
    australia high closed shut suspended", which is about nothing. One sentence
    making two claims cannot take one negation, and the contrastive conjunction
    is the cheapest signal of that shape. The claim is not lost: it goes to the
    model leg, behind the paraphrase gate.
    """
    row = (
        "Australia's high-level military posture remains unchanged, but the "
        "initial operational capability of new MQ-4C Triton UAVs adds a modest "
        "boost to its long-range surveillance."
    )
    # The polarity read that would have fired — it is real, and it is wrong.
    assert "closure" in r2.polarity_of(row)
    assert cq.counter_query_from_polarity(row) is None
    # The vocabulary is closed and every term is a genuine contrastive.
    assert "but" in cq.contrastive_terms()
    assert not (cq.contrastive_terms() & set(r2.POLARITY_GROUPS))


def test_a_claim_past_the_single_proposition_ceiling_is_not_negated() -> None:
    long_claim = ("The strait remains shut, " * 30).strip()
    assert len(long_claim) > r2.MAX_CLAIM_CHARS
    assert cq.counter_query_from_polarity(long_claim) is None


# ---------------------------------------------------------------------------
# 3) The paraphrase gate — the risk the capture names, as a mechanism
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "reply,expect_query,expect_reason",
    [
        # A restatement of the claim: refused before any search is spent.
        ('{"counter_query": "Strait of Hormuz remains shut commercial"}',
         False, cq.REASON_PARAPHRASE),
        # A real opposing proposition: admitted, and counted.
        ('{"counter_query": "Hormuz transits resumed tanker convoys"}',
         True, ""),
        # The model's own legitimate decline. NOT a failure and NOT a defect.
        ('{"counter_query": ""}', False, cq.REASON_MODEL_EMPTY),
        # A VERDICT instead of a query — the one thing the prompt forbids.
        # It never becomes a query, because it is not JSON with the key.
        ("The claim is false; reporting contradicts it.",
         False, cq.REASON_MODEL_FAILED),
        ('{"verdict": "CONTRADICTED"}', False, cq.REASON_MODEL_FAILED),
    ],
)
def test_the_paraphrase_gate_runs_in_code_before_any_search(
    reply: str, expect_query: bool, expect_reason: str,
) -> None:
    counter = cq.validate_model_query(_CLAIM_SHUT, reply)
    assert counter.issued is expect_query, reply
    if not expect_query:
        assert counter.reason == expect_reason
        assert counter.query == ""


def test_novel_tokens_is_the_definition_of_not_a_paraphrase() -> None:
    """The gate's arithmetic, stated as a number a row carries.

    ``query_novel_tokens`` is on every ``claim_contentions`` row precisely so
    counter-query quality is something to average rather than something to
    assert — the capture names query quality as this lane's risk, and a column
    is the only honest answer to a named risk.
    """
    assert cq.novel_tokens(_CLAIM_SHUT, _CLAIM_SHUT) == 0
    assert cq.is_paraphrase(_CLAIM_SHUT, "the strait remains shut")
    assert cq.novel_tokens(_CLAIM_SHUT, "Hormuz reopened operating") >= 2


# ---------------------------------------------------------------------------
# 4) The stance is a RETRIEVAL outcome, derived in code
# ---------------------------------------------------------------------------

_PAGE_OPPOSITE = (
    "Shipping sources said the Strait of Hormuz reopened to commercial "
    "traffic on Tuesday."
)
_PAGE_NARROWS = (
    "The Strait of Hormuz remains temporarily shut to commercial traffic, "
    "officials said."
)
_PAGE_UNRELATED = "Officials in Brussels discussed energy prices on Thursday."
_PAGE_DENIES = (
    "Minister Kovac has not resigned and remains in post, the ministry said "
    "on Monday after the budget vote."
)

#: Second-outlet counterparts. Deliberately NOT near-verbatim restatements: 7d's
#: fold would collapse two mastheads running one dispatch into one source, which
#: is the correct answer and would make a two-page fixture secretly a one-page
#: one. Different words, same proposition — which is what two independent
#: outlets reporting one event actually looks like.
_PAGE_OPPOSITE_SECOND = (
    "Port authorities in Muscat confirmed that the Strait of Hormuz has "
    "reopened, with convoy escorts stood down and tanker traffic back at "
    "pre-closure levels according to two port agents."
)
_PAGE_DENIES_SECOND = (
    "A spokesperson rejected reports that Kovac had resigned, telling "
    "reporters the minister chaired Tuesday's cabinet committee and will "
    "present the finance bill next week."
)

#: The claim's admissible counter-evidence window for the pure tests. Explicit
#: rather than derived, so a fixture never silently depends on today's date.
_WINDOW = cf.ClaimWindow(date(2026, 9, 1), date(2026, 10, 2))


@pytest.mark.parametrize(
    "claim,group,sign,page,stance,derivation",
    [
        # ONE admissible page is `qualifies`, never `contradicts` — F4. The
        # sentence says the opposite and the page's own ref records that; what
        # one retrieval cannot do is establish a disagreement.
        (_CLAIM_SHUT, "closure", 1, _PAGE_OPPOSITE,
         cs.STANCE_QUALIFIES, cs.DERIVATION_POLARITY),
        (_CLAIM_SHUT, "closure", 1, _PAGE_NARROWS,
         cs.STANCE_QUALIFIES, cs.DERIVATION_POLARITY),
        (_CLAIM_SHUT, "closure", 1, _PAGE_UNRELATED,
         cs.STANCE_NONE_FOUND, cs.DERIVATION_NONE),
        # No polarity group ⇒ the uncalibrated negation fallback.
        (_CLAIM_RESIGNED, None, None, _PAGE_DENIES,
         cs.STANCE_QUALIFIES, cs.DERIVATION_NEGATION),
        (_CLAIM_RESIGNED, None, None, _PAGE_UNRELATED,
         cs.STANCE_NONE_FOUND, cs.DERIVATION_NONE),
    ],
)
def test_stance_derivation_is_a_closed_table(
    claim: str, group: str | None, sign: int | None, page: str,
    stance: str, derivation: str,
) -> None:
    outcome = cs.derive_stance(
        claim, [_page(page)], group=group, sign=sign, window=_WINDOW,
    )
    assert (outcome.stance, outcome.derivation) == (stance, derivation)
    assert outcome.stance in cs.STANCES
    # EVERY fetched page becomes a ref, including the silent ones. A record
    # showing which pages were read and found silent is what makes
    # ``none_found`` believable rather than merely asserted.
    assert len(outcome.refs) == 1


@pytest.mark.parametrize(
    "claim,group,sign,pages,stance,derivation",
    [
        (_CLAIM_SHUT, "closure", 1,
         (_PAGE_OPPOSITE, _PAGE_OPPOSITE_SECOND),
         cs.STANCE_CONTRADICTS, cs.DERIVATION_POLARITY),
        (_CLAIM_RESIGNED, None, None,
         (_PAGE_DENIES, _PAGE_DENIES_SECOND),
         cs.STANCE_CONTRADICTS, cs.DERIVATION_NEGATION),
    ],
)
def test_two_independent_pages_are_what_contradicts_costs(
    claim: str, group: str | None, sign: int | None, pages: tuple[str, ...],
    stance: str, derivation: str,
) -> None:
    """F4's other half — the fence refuses a lone page, it does not refuse
    everything. A pass that could only ever emit ``qualifies`` would not be a
    fenced contrary pass, it would be a broken one."""
    outcome = cs.derive_stance(
        claim,
        [_page(pages[0]), _page(pages[1], url="https://other.example/b")],
        group=group, sign=sign, window=_WINDOW,
    )
    assert (outcome.stance, outcome.derivation) == (stance, derivation)
    assert outcome.independent_pages == 2
    assert outcome.fences == ()


def test_a_page_that_shares_too_little_subject_settles_nothing() -> None:
    """R2's precision rules, inherited: a polarity word somewhere on a page is
    not a position on THIS claim."""
    stance, derivation = cs.stance_of_sentence(
        _CLAIM_SHUT, "A canal in Panama reopened to shipping.",
        group="closure", sign=1,
    )
    assert (stance, derivation) == (cs.STANCE_NONE_FOUND, cs.DERIVATION_NONE)


def test_the_fallback_is_stricter_than_r2_on_the_dial_it_shares() -> None:
    """The negation rule needs THREE shared content tokens against R2's two.

    R2's two is defensible over a desk's verified claims — one register, one
    unit. A page from the open web is not that, and the extra token is the
    cheapest available precision on a rule with no live calibration behind it.
    """
    assert cs.NEGATION_MIN_OVERLAP > r2.MIN_SUBJECT_OVERLAP


def test_the_statement_declares_and_never_resolves() -> None:
    """The hedged line, pinned. It may say THAT a page takes a position and
    WHICH page; it may not weigh the two or name a winner."""
    outcome = cs.derive_stance(
        _CLAIM_SHUT, [_page(_PAGE_OPPOSITE)], group="closure", sign=1,
    )
    line = outcome.statement
    assert "Not adjudicated" in line
    assert "https://counter.example/a" in line
    lowered = line.lower()
    for banned in (" is false", " is wrong", "therefore", "we conclude",
                   "the claim is"):
        assert banned not in lowered, line


def test_no_stance_value_is_a_verdict_vocabulary() -> None:
    """The four stances describe a RETRIEVAL. None of them is the audit's or the
    judge's vocabulary, so a row can never be pooled into either population."""
    assert set(cs.STANCES) == {
        "contradicts", "qualifies", "none_found", "search_failed",
    }
    for verdict in ("SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNCHECKED"):
        assert verdict.lower() not in cs.STANCES


# ---------------------------------------------------------------------------
# 4b) THE FOUR FENCES, and the three live rows they were written against
# ---------------------------------------------------------------------------
#
# Every fixture below is a row the pass ACTUALLY WROTE on 2026-09-25 (run
# abfd43d6-77ce-41ef-945a-ce7f94d2b8eb, 60 claims, $0) — the URL, the page's own
# metadata date and the matched sentence are copied verbatim off
# `claim_contentions`. All three were `contradicts` and all three were false.
# Nothing here is invented: if a fence is ever relaxed, these three tests are
# what turn red, with the live evidence in the failure message.

#: The three live false positives, as (name, claim, group, sign, pages).
#: ``pages`` carries the real url / date / decisive sentence per ref.
_LIVE_FALSE_POSITIVES = [
    (
        # 1. energy_security / country_g20_in — an encyclopedia sentence about
        #    Gulf shipping, matched on polarity-side terms, against a claim
        #    about INDIA. Subject overlap: 2 tokens (`route`, `supply`), which
        #    clears R2's floor — F3 alone would NOT have caught this one, and
        #    the test says so rather than pretending otherwise.
        "energy_security/country_g20_in",
        "India faces high energy‑security pressure, driven by record "
        "oil‑price spikes, a tightening Hormuz shipping route and falling "
        "Russian crude imports, with no new supply‑side outages reported "
        "in this window [46][103][110].",
        "supply", -1,
        [
            ("https://en.wikipedia.org/wiki/Strait_of_Hormuz", "2018-12-10",
             "It is also the only maritime route for several Gulf countries "
             "including the UAE, Qatar, Bahrain, Kuwait, and Iraq, and "
             "disruption to the strait can cause severe supply shortages."),
            ("https://hormuz.data-tracking.net/", "2026-03-11",
             "Daily transit counts are published here."),
        ],
    ),
    (
        # 2. escalation / country_g20_kr — the DEFINITION of the DMZ, undated
        #    for this window, against a claim about a land-mine explosion in it.
        "escalation/country_g20_kr",
        "A DMZ land‑mine explosion that wounded two South Korean "
        "soldiers, together with North Korea’s test‑fire of upgraded "
        "240 mm rockets, raises escalation risk.",
        "hostilities", 1,
        [
            ("https://en.wikipedia.org/wiki/Korean_Demilitarized_Zone",
             "2007-05-17",
             "It was established to serve as a demilitarized zone between the "
             "sovereign states of the Democratic People's Republic of Korea "
             "(North Korea) and the Republic of Korea (South Korea) under the "
             "provisions of the Korean Armistice Agreement in 1953."),
            ("https://en.wikipedia.org/wiki/DMZ_(computing)", "2003-02-05",
             "In computer security, a DMZ is a perimeter network."),
        ],
    ),
    (
        # 3. military_posture / country_g20_ru — the uncalibrated negation leg.
        #    One encyclopedia page (384 chars extracted) and ONE think-tank
        #    article from another month quoting "in mid-May".
        "military_posture/country_g20_ru",
        "Russia’s high‑intensity war posture remains unchanged in "
        "this window, with no new weapons, deployments, exercises or readiness "
        "adjustments observed.",
        None, None,
        [
            ("https://en.wikipedia.org/wiki/Russia", "2020-03-17",
             "Russia is the largest country in the world by area."),
            ("https://www.fpri.org/article/2026/07/russia-belarus-nuclear-"
             "exercise/", "2026-08-04",
             "In mid-May, Belarus and Russia announced that they would conduct "
             "military exercises involving the delivery of nuclear weapons to "
             "units in the field."),
        ],
    ),
]

#: The window the three claims were published into — the September 2026 read
#: window widened by the pass's default 168h shelf life.
_LIVE_WINDOW = cf.ClaimWindow(date(2026, 9, 11), date(2026, 10, 2))


def _live_pages(pages: list[tuple[str, str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "url": url,
            "text": sentence,
            "sha256": f"{i:02x}" * 32,
            "chars": len(sentence),
            "published_at": published,
            "extracted": True,
            "fetched_at": "2026-09-25T22:26:54.288878+00:00",
            "status_code": 200,
        }
        for i, (url, published, sentence) in enumerate(pages)
    ]


@pytest.mark.parametrize(
    "name,claim,group,sign,pages",
    _LIVE_FALSE_POSITIVES,
    ids=[row[0] for row in _LIVE_FALSE_POSITIVES],
)
def test_the_three_live_false_contradictions_no_longer_derive(
    name: str, claim: str, group: str | None, sign: int | None,
    pages: list[tuple[str, str, str]],
) -> None:
    """THE LANE'S REASON, as three assertions.

    Each of these produced ``contradicts`` live. Past the fences each must
    derive ``none_found`` or at most ``qualifies`` — never ``contradicts``, in
    any derivation. The first two are polarity-derived, which means the
    composition tension leg would have rendered them as counter-evidence in the
    next India and Korea compositions.
    """
    fetched = _live_pages(pages)
    # The sentence rules ALONE still find the match — the fences are what
    # refuse it, and proving the unfenced derivation is unchanged is what makes
    # this a fence test rather than a coincidence.
    unfenced = [
        cs.stance_of_page(claim, p["text"], group=group, sign=sign)[0]
        for p in fetched
    ]
    assert cs.STANCE_CONTRADICTS in unfenced, name

    outcome = cs.derive_stance(
        claim, fetched, group=group, sign=sign, window=_LIVE_WINDOW,
    )
    assert outcome.stance != cs.STANCE_CONTRADICTS, (
        f"{name}: the live false positive still derives contradicts"
    )
    assert outcome.stance in (cs.STANCE_NONE_FOUND, cs.STANCE_QUALIFIES), name
    assert outcome.independent_pages < cf.MIN_INDEPENDENT_PAGES, name
    assert outcome.fences, f"{name}: no fence recorded the refusal"
    # ...and the refusal is legible on the refs, not only in the aggregate.
    assert all(r.host_class for r in outcome.refs), name


def test_the_first_two_live_rows_are_killed_by_the_host_class_fence() -> None:
    """F1, named. Both counter pages were encyclopedia articles, which is what
    the free rung returns when the real answer is absent — the run reported
    ``unresponsive_engines`` five times and its hits were dominated by
    wikipedia.org and merriam-webster.com."""
    for name, claim, group, sign, pages in _LIVE_FALSE_POSITIVES[:2]:
        outcome = cs.derive_stance(
            claim, _live_pages(pages), group=group, sign=sign,
            window=_LIVE_WINDOW,
        )
        assert outcome.stance == cs.STANCE_NONE_FOUND, name
        assert cf.FENCE_HOST_CLASS in outcome.fences, name
        assert outcome.refs[0].host_class == cf.HOST_CLASS_REFERENCE, name
        assert outcome.refs[0].fence == cf.FENCE_HOST_CLASS, name


def test_the_third_live_row_is_killed_by_the_date_and_independence_fences(
) -> None:
    """F2 and F4 on the negation leg's row. The FPRI page is real reporting from
    a host nothing refuses — it is out of window (2026-08 against a 2026-09
    claim) and it is alone."""
    name, claim, group, sign, pages = _LIVE_FALSE_POSITIVES[2]
    outcome = cs.derive_stance(
        claim, _live_pages(pages), group=group, sign=sign, window=_LIVE_WINDOW,
    )
    assert outcome.stance == cs.STANCE_QUALIFIES
    assert outcome.derivation == cs.DERIVATION_NEGATION
    assert cf.FENCE_PAGE_DATE in outcome.fences
    fpri = outcome.refs[1]
    assert fpri.host_class == cf.HOST_CLASS_UNKNOWN
    assert fpri.page_published_at == "2026-08-04"
    assert fpri.fence == cf.FENCE_PAGE_DATE
    assert outcome.independent_pages == 0


# --- the fences individually ------------------------------------------------


def test_a_reference_host_can_never_carry_a_contradiction() -> None:
    """F1. Not reporting, no publication date for anyone's window, and what a
    degraded search rung hands back. Mirrors count: a fence a mirror walks
    around is not a fence."""
    for host in ("en.wikipedia.org", "simple.wikipedia.org", "wikiwand.com",
                 "www.britannica.com", "merriam-webster.com",
                 "www.investopedia.com", "dbpedia.org"):
        url = f"https://{host}/x"
        assert cf.is_reference_host(url), host
        assert cf.host_class_of(url) == cf.HOST_CLASS_REFERENCE, host
        outcome = cs.derive_stance(
            _CLAIM_SHUT, [_page(_PAGE_OPPOSITE, url=url)],
            group="closure", sign=1, window=_WINDOW,
        )
        assert outcome.stance == cs.STANCE_NONE_FOUND, host
    # An UNKNOWN host passes — refusing the unknown would refuse the whole
    # point of a contrary pass.
    assert cf.host_class_of("https://news.example/a") == cf.HOST_CLASS_UNKNOWN


def test_a_known_host_keeps_the_source_class_it_was_registered_under() -> None:
    """One vocabulary, not two. A host the platform already ingests is labelled
    with ``source_descriptors``' own class rather than a word invented here —
    and the reference list still wins, because a registered encyclopedia feed
    would still not be reporting."""
    catalog = {"bbc.co.uk": "reporting", "un.org": "official",
               "irna.ir": "state_media", "38north.org": "analysis"}
    assert cf.host_class_of("https://www.bbc.co.uk/news/x", catalog) == "reporting"
    assert cf.host_class_of("https://news.un.org/en/x", catalog) == "official"
    assert cf.host_class_of("https://en.irna.ir/news/1", catalog) == "state_media"
    assert cf.host_class_of("https://www.38north.org/p", catalog) == "analysis"
    assert set(catalog.values()) <= set(cf.HOST_CLASSES)
    # The reference list is not overridable by the catalog.
    assert cf.host_class_of(
        "https://en.wikipedia.org/wiki/X", {"wikipedia.org": "reporting"},
    ) == cf.HOST_CLASS_REFERENCE


def test_an_undated_page_cannot_contradict() -> None:
    """F2's first half, and it holds even when the claim's window is UNMEASURED:
    "no date" is a property of the page, not of the claim."""
    undated = _page(_PAGE_OPPOSITE, published_at=None)
    for window in (_WINDOW, None):
        outcome = cs.derive_stance(
            _CLAIM_SHUT, [undated], group="closure", sign=1, window=window,
        )
        assert outcome.stance == cs.STANCE_QUALIFIES
        assert cf.FENCE_PAGE_DATE in outcome.fences
        assert outcome.page_published_at is None
    admitted, reason = cf.date_admits(None, _WINDOW)
    assert (admitted, reason) == (False, cf.REJECT_NO_DATE)


def test_a_dated_url_answers_when_the_page_metadata_does_not() -> None:
    """F2 reuses the reference builder's THIRD dating mechanism rather than
    re-implementing one: a date the URL itself embeds, validated through
    ``datetime.date`` so a Feb-30-shaped id reads as no date."""
    assert cf.page_date_of(
        "https://news.example/2026/09/20/hormuz-reopens", None,
    ) == (date(2026, 9, 20), cf.DATE_SOURCE_URL)
    assert cf.page_date_of("https://news.example/x", "2026-09-20") == (
        date(2026, 9, 20), cf.DATE_SOURCE_PAGE
    )
    assert cf.page_date_of("https://news.example/2026/02/30/x", None) == (
        None, cf.DATE_SOURCE_NONE
    )


def test_a_page_from_another_year_qualifies_at_most() -> None:
    """F2's second half. A real counter-report from outside the window narrows
    the picture; it does not contradict a claim about this window — the exact
    defect the reference builder's date gate was written for."""
    stale = _page(_PAGE_OPPOSITE, published_at="2018-12-10")
    outcome = cs.derive_stance(
        _CLAIM_SHUT, [stale], group="closure", sign=1, window=_WINDOW,
    )
    assert outcome.stance == cs.STANCE_QUALIFIES
    assert cf.FENCE_PAGE_DATE in outcome.fences
    assert outcome.page_published_at == "2018-12-10"
    admitted, reason = cf.date_admits(date(2018, 12, 10), _WINDOW)
    assert (admitted, reason) == (False, cf.REJECT_OUT_OF_WINDOW)


def test_the_claims_window_is_the_audits_own_stamp_widened_by_the_ttl() -> None:
    """F2's window is not a new concept: it is the read's own evidence window
    (``evidence_window_bounds``, plus the read's produced_at as the upper
    bound) widened by ``contention_ttl_hours``, the record's shelf life."""
    claim = SimpleNamespace(
        read_evidence_window={"oldest": "2026-09-18T00:00:00+00:00",
                              "newest": "2026-09-25T00:00:00+00:00"},
        produced_at="2026-09-25T06:00:00+00:00",
    )
    window = cf.claim_window(claim, ttl_hours=168)
    assert window.measured
    assert window.start == date(2026, 9, 11)
    assert window.end == date(2026, 10, 2)
    # An UNMEASURED window is not an out-of-window page — the audit's own
    # ruling, inherited rather than restated.
    blank = cf.claim_window(
        SimpleNamespace(read_evidence_window={}, produced_at=""), ttl_hours=168,
    )
    assert not blank.measured
    assert cf.date_admits(date(2018, 1, 1), blank) == (True, "")


def test_a_polarity_term_in_a_sentence_about_something_else_is_refused() -> None:
    """F3. The floors are the rules' own — R2's two for the calibrated leg,
    three for the uncalibrated one — and the count lands on the row beside
    ``query_novel_tokens``."""
    assert cs.overlap_floor(cs.DERIVATION_POLARITY) == r2.MIN_SUBJECT_OVERLAP
    assert cs.overlap_floor(cs.DERIVATION_NEGATION) == cs.NEGATION_MIN_OVERLAP
    assert cf.subject_overlap_of(_CLAIM_SHUT, _PAGE_OPPOSITE) >= 2
    assert cf.subject_overlap_of(_CLAIM_SHUT, _PAGE_UNRELATED) < 2
    verdict = cf.judge_page(
        _CLAIM_SHUT, url="https://news.example/a", published_at="2026-09-20",
        sentence="A canal in Panama reopened to shipping.",
        window=_WINDOW, min_overlap=r2.MIN_SUBJECT_OVERLAP,
    )
    assert verdict.fence == cf.FENCE_SUBJECT_OVERLAP
    assert verdict.rejected
    # ...and a passing page records the count rather than a boolean.
    ok = cf.judge_page(
        _CLAIM_SHUT, url="https://news.example/a", published_at="2026-09-20",
        sentence=_PAGE_OPPOSITE, window=_WINDOW,
        min_overlap=r2.MIN_SUBJECT_OVERLAP,
    )
    assert ok.admissible and ok.subject_overlap >= 2 and ok.fence == ""


def test_two_mastheads_running_one_dispatch_are_one_page() -> None:
    """F4 counts OUTLETS, and the outlet notion is 7d's — imported, not
    re-spelled. Two hosts carrying the same wire copy fold to one, which is what
    stops a syndicated dispatch from looking like corroboration."""
    long_copy = _PAGE_OPPOSITE + " " + _PAGE_OPPOSITE
    assert cf.independent_pages([
        {"url": "https://a.example/x", "text": long_copy, "sha256": "1" * 64},
        {"url": "https://b.example/y", "text": long_copy, "sha256": "2" * 64},
    ]) == 1
    # Two feeds of ONE host are one outlet before any content test runs.
    assert cf.independent_pages([
        {"url": "https://a.example/x", "text": _PAGE_OPPOSITE, "sha256": "1" * 64},
        {"url": "https://a.example/y", "text": _PAGE_OPPOSITE_SECOND,
         "sha256": "2" * 64},
    ]) == 1
    # Two outlets, two dispatches.
    assert cf.independent_pages([
        {"url": "https://a.example/x", "text": _PAGE_OPPOSITE, "sha256": "1" * 64},
        {"url": "https://b.example/y", "text": _PAGE_OPPOSITE_SECOND,
         "sha256": "2" * 64},
    ]) == 2
    assert not cf.contradiction_admitted(1)
    assert cf.contradiction_admitted(2)


def test_one_admissible_page_is_qualifies_and_records_why() -> None:
    """F4 at the claim level: the page's OWN ref keeps saying the page states
    the opposite — that is true — while the claim's stance is capped, and
    ``independent_pages`` on the row is the number that says which rule did it."""
    outcome = cs.derive_stance(
        _CLAIM_SHUT, [_page(_PAGE_OPPOSITE)], group="closure", sign=1,
        window=_WINDOW,
    )
    assert outcome.stance == cs.STANCE_QUALIFIES
    assert outcome.refs[0].stance == cs.STANCE_CONTRADICTS
    assert outcome.refs[0].fence == ""
    assert outcome.independent_pages == 1
    assert outcome.fences == (cf.FENCE_INDEPENDENT_PAGES,)


def test_every_fence_and_host_class_is_a_closed_vocabulary() -> None:
    """A receipt reports zeros rather than omitting a key, so both vocabularies
    have to be enumerable — and ``host_class`` may not grow a synonym of a
    source class that already exists."""
    from typing import get_args

    from legba.data.schemas.source import SourceClass

    assert set(cf.FENCES) == {
        cf.FENCE_HOST_CLASS, cf.FENCE_PAGE_DATE, cf.FENCE_SUBJECT_OVERLAP,
        cf.FENCE_INDEPENDENT_PAGES,
    }
    declared = set(get_args(SourceClass))
    assert declared < set(cf.HOST_CLASSES), (
        "host_class must REUSE the source-class taxonomy, not shadow it"
    )
    assert set(cf.HOST_CLASSES) - declared == {
        cf.HOST_CLASS_REFERENCE, cf.HOST_CLASS_UNKNOWN,
    }



# ---------------------------------------------------------------------------
# 5) Selection — the audit's bar, verbatim
# ---------------------------------------------------------------------------


def test_the_selection_is_the_audits_top_layer_and_nothing_wider() -> None:
    """Imported, never re-listed. The brief's hard line is "do not widen the
    audit's claim selection", and the only way to keep that true across a later
    edit to the auditor is to hold the SAME object."""
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_width as width,
    )

    assert store.MATERIAL_ANALYST_IDS is width.WIDTH_READ_ANALYST_IDS
    assert "region_rollup" not in store.MATERIAL_ANALYST_IDS


def test_material_claims_drops_uncheckable_and_compound_and_counts_both(
) -> None:
    rows = [_assembled_read(
        spans=[
            _CLAIM_SHUT,                       # checkable, single proposition
            "This read covers 4 of 7 declared units.",   # scope-bounded
            ("The strait remains shut, " * 30).strip(),  # compound/too long
        ],
    )]
    claims, counts = store.material_claims(
        rows, cap=60, max_claim_chars=r2.MAX_CLAIM_CHARS
    )
    assert counts["claims_enumerated"] == 3
    assert counts["claims_uncheckable"] >= 1
    assert counts["claims_compound"] == 1
    assert [c.claim_text for c in claims] == [_CLAIM_SHUT]
    # The cap binds AFTER the priority sort, so it drops the LEAST material
    # claims — never the last ones by name.
    _, capped = store.material_claims(
        rows, cap=0, max_claim_chars=r2.MAX_CLAIM_CHARS
    )
    assert capped["claims_over_cap"] == 1


# ---------------------------------------------------------------------------
# 6) Registration, descriptor and knobs
# ---------------------------------------------------------------------------


def test_the_sub_handler_is_registered_and_trace_only() -> None:
    assert SUB_HANDLERS["contrary_evidence_pass"] is cep.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["contrary_evidence_pass"] is TRACE_ONLY


def test_the_descriptor_is_draft_zero_budget_and_temperature_one() -> None:
    """The standing rules, as a file assertion: no token budget anywhere, 1.0
    wherever an LLM route is declared, and NO max_tokens on the core plane's
    own control (the handler's cap is the live one)."""
    doc = yaml.safe_load(
        (_DESCRIPTORS / "analyst_contrary_evidence_pass.yaml").read_text()
    )
    assert doc["identity"]["state"] == "draft"
    assert doc["identity"]["kind"] == "deterministic"
    assert doc["method"]["sub_handler"] == "contrary_evidence_pass"
    assert doc["method"]["budget_tokens_per_day"] == 0
    assert doc["method"]["llm"]["temperature"] == 1.0
    # NO GRADER REF. The organ the design refuses to have must not be wirable
    # by a descriptor PUT.
    assert "grader" not in doc["method"]["llm"]
    assert "audit_rater" not in doc["method"]["llm"]
    # The metered rung ships OFF, and the ladder ships with rung 0 alone.
    assert doc["method"]["options"]["paid_rung"] is False
    assert doc["method"]["options"]["serp_provider_order"] == ["searxng"]
    assert doc["action_packs"] == [{"pack_id": "web_access"}]


def test_every_declared_knob_is_read_by_the_handler() -> None:
    """The X-1 reachability property, asserted here too because a knob an
    operator can set and the code never reads is config that lies."""
    declared = {s.name for s in HANDLER_OPTIONS["contrary_evidence_pass"]}
    source = (
        Path(cep.__file__).read_text()
    )
    for name in declared:
        assert f'options.get("{name}")' in source, name
    doc = yaml.safe_load(
        (_DESCRIPTORS / "analyst_contrary_evidence_pass.yaml").read_text()
    )
    assert set(doc["method"]["options"]) <= declared


@pytest.mark.asyncio
async def test_missing_pool_raises_rather_than_reporting_a_clean_pass() -> None:
    class _NoPool:
        pg_pool = None
        extras: dict[str, Any] = {}

    with pytest.raises(RuntimeError, match="deps.pg_pool"):
        await cep.handle(None, {}, _NoPool())


# ---------------------------------------------------------------------------
# 7) The composition tension rule
# ---------------------------------------------------------------------------


def test_only_a_polarity_contradiction_reaches_a_composition() -> None:
    """The admission rule, in one place and pinned.

    A ``qualifies`` record is not a tension; a ``negation``-derived record is
    the UNCALIBRATED fallback and is human-surfaces-only. R2 shipped ZERO pairs
    rather than render 57 false ones into a composition, and this is the same
    call in the same place.
    """
    assert ct.composition_admits(
        {"stance": "contradicts", "derivation": "polarity",
         "independent_pages": 2}
    )
    for record in (
        {"stance": "contradicts", "derivation": "negation",
         "independent_pages": 2},
        {"stance": "qualifies", "derivation": "polarity",
         "independent_pages": 2},
        {"stance": "none_found", "derivation": "none", "independent_pages": 2},
        {"stance": "search_failed", "derivation": "none",
         "independent_pages": 2},
        # F4 at the composition floor, belt and braces with the stance: ONE
        # admissible page is a retrieval, not a disagreement...
        {"stance": "contradicts", "derivation": "polarity",
         "independent_pages": 1},
        # ...and a row that measured NO independence at all — every row written
        # before migration 0222, including the first live run's three false
        # contradictions — is "not measured", which is not two.
        {"stance": "contradicts", "derivation": "polarity",
         "independent_pages": None},
        {"stance": "contradicts", "derivation": "polarity"},
    ):
        assert not ct.composition_admits(record), record


def test_a_payload_with_no_records_renders_byte_identically() -> None:
    payload = _payload()
    before = render_assembly_body(payload)
    merged = ct.merge_entries(payload, [])
    assert render_assembly_body(merged) == before
    assert "## Counter-evidence" not in before


def test_a_contradicts_record_renders_a_hedged_line_and_a_counter_ref() -> None:
    """The rendered body, end to end from a record.

    Four properties: the bullet cites the carried block's own ``[[ref:N]]``; the
    other side is a plain ``[counter:1]`` and NOT a ``[[ref:…]]`` (the
    ``|blocks| == |citations| == |distinct markers|`` invariant is load-bearing
    and a marker with no block behind it would break it); the foot carries the
    URL, the date, the hash and the counter-query; and nothing adjudicates.
    """
    payload = _payload()
    merged = ct.merge_entries(
        payload, ct.tension_entries([_record()], ct.ordinals_by_claim(payload))
    )
    body = render_assembly_body(merged)
    assert "[[ref:1]] / [counter:1]" in body
    assert "## Counter-evidence" in body
    assert "https://counter.example/a" in body
    assert "published 2026-09-20" in body
    assert "counter-query: hormuz reopened operating" in body
    assert "Not adjudicated" in body
    assert "[[ref:2]]" not in body.split("## Counter-evidence")[1]
    # The counters MOVED with the entry — the Assessment voice is graded
    # against this arithmetic, so a grown list beside a frozen count would put
    # it in the position of narrating a tension the record says was never found.
    assert merged["tension_checked"]["pairs_found"] == 1
    assert merged["tension_checked"]["pairs_found_uncarried"] == 1
    assert merged["tension_checked"]["contrary_retrieval"] == 1
    # ...and the PAIRS denominator did not move: this pass compared a claim to
    # the open web, not two shown blocks to each other.
    assert merged["tension_checked"]["pairs_examined"] == (
        _payload()["tension_checked"]["pairs_examined"]
    )


def test_a_record_for_a_claim_this_composition_does_not_carry_is_dropped(
) -> None:
    """The join is the CLAIM KEY, not the read id — a contention written
    against yesterday's composition still applies to today's, as long as today
    is quoting the same desk-head span. A record for a span nobody carries
    renders nothing rather than an ordinal with no block behind it."""
    payload = _payload()
    stray = dict(_record(), claim_id=claim_key("a different claim", uuid4(), 0, 5))
    assert ct.tension_entries(
        [stray], ct.ordinals_by_claim(payload)
    ) == []


def test_a_missing_publication_date_renders_as_absence_never_as_today(
) -> None:
    """A number is never invented; absence renders as absence. The fetch leg
    DISCOVERS a date from the document and a page that states none gets no
    date — not today's, and not a blank that reads as though nobody looked."""
    payload = _payload()
    record = _record()
    record["refs"][0]["published_at"] = None
    # BOTH date fields: the raw discovery AND the one the date gate parsed. A
    # page that stated no date has neither, and the renderer must not reach
    # past an absent one to a stale one.
    record["refs"][0]["page_published_at"] = None
    merged = ct.merge_entries(
        payload, ct.tension_entries([record], ct.ordinals_by_claim(payload))
    )
    foot = render_assembly_body(merged).split("## Counter-evidence", 1)[1]
    assert "no publication date stated" in foot
    # ...and NO date at the published slot. Scoped to the foot because the
    # record's own as-of legitimately appears in the header; what must never
    # happen is a publication date appearing for a page that stated none.
    assert "published " not in foot


# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


def _page(
    text: str,
    url: str = "https://counter.example/a",
    published_at: str | None = "2026-09-20",
) -> dict[str, Any]:
    return {
        "url": url,
        "text": text,
        "sha256": "ab" * 32,
        "chars": len(text),
        "published_at": published_at,
        "extracted": True,
        "fetched_at": "2026-09-25T05:37:00+00:00",
        "status_code": 200,
    }


_FINDING_ID = str(uuid4())

#: The key the payload's own block-1 span mints. Computed with the AUDITOR's
#: function, which is the whole point of the join: the pass stored this key and
#: the composition recomputes it from the span it is carrying.
_CARRIED_CLAIM_ID = claim_key(
    _CLAIM_SHUT, _FINDING_ID, 0, len(_CLAIM_SHUT.encode("utf-8")),
)


def _record(**over: Any) -> dict[str, Any]:
    record = {
        "claim_id": _CARRIED_CLAIM_ID,
        "claim_text": _CLAIM_SHUT,
        "finding_id": _FINDING_ID,
        "target_id": "country_watch_ir",
        "desk_key": "country_watch_ir",
        "analyst_id": "country_composition",
        "query": "hormuz reopened operating",
        "statement": cs.statement_for(
            _CLAIM_SHUT, cs.STANCE_CONTRADICTS, cs.DERIVATION_POLARITY,
            cs.PageRef(
                url="https://counter.example/a", sha256="ab" * 32, chars=120,
                published_at="2026-09-20",
                fetched_at="2026-09-25T05:37:00+00:00",
                stance=cs.STANCE_CONTRADICTS, quote=_PAGE_OPPOSITE,
            ),
        ),
        "stance": "contradicts",
        "derivation": "polarity",
        "pipeline_version": store.CONTRARY_PIPELINE_VERSION,
        "as_of": "2026-09-25T05:37:00+00:00",
        # The four fences' numbers, as a fenced row carries them (0222). Two
        # independent pages is what a `contradicts` row costs now, so a fixture
        # that omitted it would be testing a row the pass can no longer write.
        "host_class": cf.HOST_CLASS_UNKNOWN,
        "page_published_at": "2026-09-20",
        "subject_overlap": 4,
        "independent_pages": 2,
        "refs": [{
            "url": "https://counter.example/a",
            "sha256": "ab" * 32,
            "published_at": "2026-09-20",
            "page_published_at": "2026-09-20",
            "host_class": cf.HOST_CLASS_UNKNOWN,
            "subject_overlap": 4,
            "fence": "",
            "stance": "contradicts",
            "quote": _PAGE_OPPOSITE,
        }],
    }
    record.update(over)
    return record


def _payload() -> dict[str, Any]:
    """A two-block assembly payload, minimal but real in every field the
    renderer reads."""
    def _block(ordinal: int, finding_id: str, text: str) -> dict[str, Any]:
        return {
            "ordinal": ordinal,
            "finding_id": finding_id,
            "desk": "country_composition",
            "target_id": "country_watch_ir",
            "target_name": "Iran",
            "question": "What changed?",
            "spans": [{
                "text": text,
                "role": "bluf",
                "origin": {"head_id": finding_id, "start": 0,
                           "end": len(text.encode("utf-8"))},
            }],
            "attribution": {},
        }

    return {
        "schema": "assembly.v1",
        "tier": "country",
        "as_of": "2026-09-25T05:00:00+00:00",
        "blocks": [
            _block(1, _FINDING_ID, _CLAIM_SHUT),
            _block(2, str(uuid4()), "Port throughput held steady last week."),
        ],
        "lead": {"kind": "none", "block_ordinals": [], "test": {}},
        "drops": {"counts": {"shown_not_carried": 0}},
        "tensions": [],
        "tension_checked": {
            "blocks": 2, "pairs_examined": 1, "pairs_found": 0,
            "pairs_found_carried": 0, "pairs_found_uncarried": 0,
            "scope": "carried", "scope_note": "carried pairs only",
        },
        "coverage": [],
    }


def _assembled_read(*, spans: list[str]) -> dict[str, Any]:
    """An ``analyst_outputs`` row shaped the way ``claims_from_read`` reads it."""
    head_id = str(uuid4())
    return {
        "id": uuid4(),
        "analyst_id": "country_composition",
        "target_id": "country_watch_ir",
        "title": "Iran read",
        "body": "body",
        "produced_at": datetime.now(timezone.utc),
        "data": {
            "tags": ["severity:high"],
            "data": {
                "assembly": {
                    "schema": "assembly.v1",
                    "regime": "assembly",
                    "tier": "country",
                    "blocks": [{
                        "ordinal": i + 1,
                        "finding_id": head_id,
                        "desk": "country_composition",
                        "target_id": "country_watch_ir",
                        "spans": [{
                            "text": text,
                            "role": "bluf",
                            "origin": {
                                "head_id": head_id, "start": 0,
                                "end": len(text.encode("utf-8")),
                            },
                        }],
                    } for i, text in enumerate(spans)],
                },
            },
        },
    }


# ---------------------------------------------------------------------------
# 8) The migration, on the REAL driver
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg):
    p = await asyncpg.create_pool(
        host=migrated_pg.host, port=migrated_pg.port, user=migrated_pg.user,
        password=migrated_pg.password, database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    async with p.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('claim_contentions')")
        assert await conn.fetchval(
            "SELECT to_regclass('action_pack_invocations')"
        )
        assert await conn.fetchval(
            "SELECT to_regclass('alert_trigger_watermarks')"
        )
    yield p
    await p.close()


def _row(**over: Any) -> dict[str, Any]:
    """A ``claim_contentions`` row, built by the REAL builder.

    Going through ``store.build_row`` rather than hand-assembling a dict means
    a column the builder forgets is a failure here, not a silent NULL live.
    """
    now = over.pop("as_of", None) or datetime.now(timezone.utc)
    claim = SimpleNamespace(
        key=over.pop("claim_id", "c" * 64),
        claim_text=_CLAIM_SHUT,
        graded_output_id=over.pop("finding_id", uuid4()),
        target_id="country_watch_ir",
        desk_key="country_watch_ir",
        analyst_id="country_composition",
    )
    counter = cq.CounterQuery(
        query="hormuz reopened operating", source=cq.QUERY_SOURCE_POLARITY,
        polarity_group="closure", polarity_sign=1, novel_tokens=3,
    )
    outcome = cs.StanceOutcome(
        stance="contradicts", derivation="polarity",
        statement="retrieved counter-evidence; Not adjudicated.",
        refs=(cs.PageRef(
            url="https://counter.example/a", sha256="ab" * 32, chars=120,
            published_at="2026-09-20", page_published_at="2026-09-20",
            fetched_at="2026-09-25T05:37:00+00:00",
            stance="contradicts", quote=_PAGE_OPPOSITE,
            host_class=cf.HOST_CLASS_UNKNOWN, subject_overlap=4,
        ),),
        # A `contradicts` row is only writable with TWO independent pages —
        # the schema says so since 0222, so the fixture says so too.
        host_class=cf.HOST_CLASS_UNKNOWN, page_published_at="2026-09-20",
        subject_overlap=4, independent_pages=2,
    )
    row = store.build_row(
        claim, counter, outcome, rung="searxng", reason="", as_of=now,
        ttl_hours=168, receipt_id=uuid4(),
    )
    if "refs" in over:
        row["refs"] = over.pop("refs")
    row.update(over)
    return row


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migrations_0221_and_0222_are_idempotent_on_the_real_driver(
    pool,
) -> None:
    """CREATE-only and re-runnable — the house rule for every migration here.

    0222 adds four NULLABLE columns and three NOT VALID checks behind
    ``pg_constraint`` guards, so a second application is a no-op on both.
    """
    for path in (_MIGRATION, _MIGRATION_FENCES):
        sql = path.read_text()
        async with pool.acquire() as conn:
            await conn.execute(sql)
            await conn.execute(sql)
    async with pool.acquire() as conn:
        cols = {
            r["column_name"] for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'claim_contentions'"
            )
        }
        idx = {
            r["indexname"] for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes "
                "WHERE tablename = 'claim_contentions'"
            )
        }
        checks = {
            r["conname"] for r in await conn.fetch(
                "SELECT conname FROM pg_constraint "
                "WHERE conrelid = 'public.claim_contentions'::regclass"
            )
        }
    for required in (
        "claim_id", "finding_id", "target_id", "query", "rung", "stance",
        "refs", "retrieved_at", "as_of", "expires_at", "receipt_id",
        # 0222 — the four fences' numbers.
        "host_class", "page_published_at", "subject_overlap",
        "independent_pages",
    ):
        assert required in cols, required
    # The partial index the composition read depends on. Without it the
    # per-cycle question walks the none_found bulk, which is by design most of
    # the table.
    assert "claim_contentions_contradicts_idx" in idx
    assert "claim_contentions_claim_day_unique" in idx
    # `scope=<target>` on the route now filters target_id, so the OR can be a
    # BitmapOr of two index scans rather than a sequential scan.
    assert "claim_contentions_target_idx" in idx
    for constraint in (
        "ck_claim_contentions_host_class_vocab",
        "ck_claim_contentions_fence_counts",
        "ck_claim_contentions_contradicts_independent",
    ):
        assert constraint in checks, constraint


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_fence_columns_are_null_not_zero_when_unmeasured(
    pool,
) -> None:
    """NULLABLE AND UNBACKFILLED is the design. A row written before 0222
    measured none of these four, and "not measured" is not zero — the
    ``reference_age_days`` ruling in 0197, one table over. The three false
    ``contradicts`` rows the first live run left behind stay exactly as they
    are, which is why this column may not default."""
    claim_id = f"unmeasured{uuid4().hex}"
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO claim_contentions (
                claim_id, claim_text, query, query_source, stance, derivation,
                pipeline_version, as_of, as_of_day, expires_at
            ) VALUES ($1, 'x', 'q', 'polarity', 'none_found', 'none',
                      '2026-09/7a.1', now(), now()::date, now())
            """,
            claim_id,
        )
        row = await conn.fetchrow(
            "SELECT host_class, page_published_at, subject_overlap, "
            "independent_pages FROM claim_contentions WHERE claim_id = $1",
            claim_id,
        )
    assert dict(row) == {
        "host_class": None, "page_published_at": None,
        "subject_overlap": None, "independent_pages": None,
    }


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_contradicts_row_may_not_be_written_without_two_pages(
    pool,
) -> None:
    """F4, at the schema. The code caps a lone contradiction at ``qualifies``
    before the row is built; this is the layer that catches a write path that
    forgot to. Two independent pages, or it is not a contradiction."""
    async with pool.acquire() as conn:
        assert await store.write_contention(
            conn, _row(claim_id=f"f4a{uuid4().hex}", independent_pages=1)
        ) is False
        assert await store.write_contention(
            conn, _row(claim_id=f"f4b{uuid4().hex}", independent_pages=None)
        ) is False
        # ...and the same row with two lands.
        assert await store.write_contention(
            conn, _row(claim_id=f"f4c{uuid4().hex}")
        ) is True
        # A NON-contradicting stance is unconstrained by F4: `qualifies` on one
        # page is exactly what the fence produces, and refusing it here would
        # refuse the fence's own output.
        assert await store.write_contention(
            conn, _row(claim_id=f"f4d{uuid4().hex}", stance="qualifies",
                       independent_pages=1)
        ) is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_host_class_vocabulary_is_enforced_by_the_schema(
    pool,
) -> None:
    """One vocabulary, not two: ``host_class`` is the source-class taxonomy plus
    ``reference`` and ``unknown``, and the schema is what keeps a fourth
    spelling of "what kind of source is this" out of the table."""
    async with pool.acquire() as conn:
        assert await store.write_contention(
            conn, _row(claim_id=f"hc1{uuid4().hex}", host_class="encyclopedia")
        ) is False
        assert await store.write_contention(
            conn, _row(claim_id=f"hc2{uuid4().hex}", subject_overlap=-1)
        ) is False
        for klass in cf.HOST_CLASSES:
            assert await store.write_contention(
                conn, _row(claim_id=f"hc{klass}{uuid4().hex}",
                           host_class=klass)
            ) is True, klass


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_row_may_not_claim_a_contradiction_it_cannot_show(pool) -> None:
    """The schema-level half of "a counter-ref the platform cites is a page it
    fetched": a stance that names evidence MUST carry it."""
    async with pool.acquire() as conn:
        assert await store.write_contention(
            conn, _row(claim_id=f"noev{uuid4().hex}", refs=[])
        ) is False


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_closed_vocabularies_are_enforced_by_the_schema(
    pool,
) -> None:
    """The audit's verdict words are not this table's words, and the schema —
    not a convention — is what keeps the two populations apart."""
    async with pool.acquire() as conn:
        for bad in (
            _row(claim_id=f"v1{uuid4().hex}", stance="CONTRADICTED"),
            # a stance that found nothing may not carry a derivation
            _row(claim_id=f"v2{uuid4().hex}", stance="none_found"),
            _row(claim_id=f"v3{uuid4().hex}", query_source="guessed"),
            _row(claim_id=f"v4{uuid4().hex}", derivation="vibes"),
        ):
            assert await store.write_contention(conn, bad) is False, bad["stance"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_rerun_inside_a_day_is_a_no_op(pool) -> None:
    """``(claim_id, pipeline_version, as_of_day)`` is the replay key: a second
    pass over the same claim on the same day writes nothing and says so."""
    row = _row(claim_id=f"dup{uuid4().hex}")
    async with pool.acquire() as conn:
        assert await store.write_contention(conn, row) is True
        assert await store.write_contention(conn, row) is False
        n = await conn.fetchval(
            "SELECT count(*) FROM claim_contentions WHERE claim_id = $1",
            row["claim_id"],
        )
    assert n == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_expired_or_uncalibrated_record_is_not_a_live_tension(
    pool,
) -> None:
    """The composition's three predicates, proved from the QUERY rather than
    from a caller remembering to filter."""
    finding = uuid4()
    now = datetime.now(timezone.utc)
    live = _row(claim_id=f"live{uuid4().hex}", finding_id=finding)
    dead = _row(
        claim_id=f"dead{uuid4().hex}", finding_id=finding,
        as_of=now - timedelta(days=30),
        expires_at=now - timedelta(days=23),
        as_of_day=(now - timedelta(days=30)).date(),
    )
    negation = _row(
        claim_id=f"neg{uuid4().hex}", finding_id=finding, derivation="negation",
    )
    async with pool.acquire() as conn:
        for row in (live, dead, negation):
            assert await store.write_contention(conn, row) is True, row["claim_id"]
        found = await ct.contradictions_for_claims(
            conn,
            [live["claim_id"], dead["claim_id"], negation["claim_id"]],
            now=now,
        )
    keys = {r["claim_id"] for r in found}
    assert live["claim_id"] in keys
    assert dead["claim_id"] not in keys
    assert negation["claim_id"] not in keys
    # ...and the live one carries the fences' numbers back to the reader, so a
    # rendered tension line can be re-argued without re-reading the table.
    admitted = next(r for r in found if r["claim_id"] == live["claim_id"])
    assert admitted["independent_pages"] == 2
    assert admitted["host_class"] == cf.HOST_CLASS_UNKNOWN
    assert admitted["subject_overlap"] == 4
    assert ct.composition_admits(admitted)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_pre_fence_contradiction_never_reaches_a_composition(
    pool,
) -> None:
    """F4 THROUGH THE REAL SQL, on the shape the live table actually holds.

    The first live run left three `contradicts` rows that measured no
    independence at all. They are expired, so the clock predicate already
    excludes them — but a row like that with a live `expires_at` must be
    excluded too, and by the QUERY rather than by a caller remembering. NULL is
    "not measured", and `NULL >= 2` is NULL, which this comparison drops.

    The row is inserted with the 0222 CHECK temporarily dropped because that is
    the only way this shape exists: the check is NOT VALID, so it grandfathers
    the rows already in the table and refuses every new one. Dropping and
    restoring it here reproduces a grandfathered row exactly.
    """
    claim_id = f"prefence{uuid4().hex}"
    now = datetime.now(timezone.utc)
    async with pool.acquire() as conn:
        await conn.execute(
            "ALTER TABLE claim_contentions "
            "DROP CONSTRAINT ck_claim_contentions_contradicts_independent"
        )
        try:
            row = _row(claim_id=claim_id, independent_pages=None,
                       host_class=None, subject_overlap=None)
            assert await store.write_contention(conn, row) is True
        finally:
            await conn.execute(
                "ALTER TABLE claim_contentions ADD CONSTRAINT "
                "ck_claim_contentions_contradicts_independent CHECK ("
                "stance <> 'contradicts' OR (independent_pages IS NOT NULL "
                "AND independent_pages >= 2)) NOT VALID"
            )
        stored = await conn.fetchrow(
            "SELECT stance, independent_pages FROM claim_contentions "
            "WHERE claim_id = $1", claim_id,
        )
        found = await ct.contradictions_for_claims(conn, [claim_id], now=now)
    assert stored["stance"] == "contradicts"
    assert stored["independent_pages"] is None, (
        "the grandfathered shape is the point of this test"
    )
    assert found == [], "an unmeasured contradiction reached a composition"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_linking_a_fetched_page_never_writes_a_signal(pool) -> None:
    """``ref_signal_ids`` LINKS and never creates.

    The pages this pass fetches are chosen ADVERSARIALLY; landing them in the
    corpus would move freshness, source health, salience and calibration for
    every desk that reads them, silently and in one direction. So the link is a
    read, and this asserts the corpus did not grow.
    """
    from legba.data.research_evidence import content_hash_for

    digest = content_hash_for(_PAGE_OPPOSITE)
    async with pool.acquire() as conn:
        before = await conn.fetchval("SELECT count(*) FROM signals")
        linked = await store.link_signals(conn, [digest])
        after = await conn.fetchval("SELECT count(*) FROM signals")
    assert after == before, "the contrary pass must never write a signal"
    assert isinstance(linked, list)


# ---------------------------------------------------------------------------
# 9) END-TO-END through the REAL binding path
# ---------------------------------------------------------------------------
#
# #85 — THE DOUBLE SITS AT THE SOCKET, NOT AT THE SEAM. The standing auditor's
# search leg was dead for five weeks while its suite stayed green, because that
# suite injected a resolved provider straight into `ToolContext(search=...)` —
# which can only ever prove the code DOWNSTREAM of the seam under test. So here
# the binding is built by the REAL `wire_standing_auditor_web_pack` (route
# resolution, component fetch, family assertion, handler configuration all
# real), and the fakes are the provider's HTTP GET, the fetch transport, the
# registry GET, the robots verdict and the LLM. The `action_pack_invocations`
# assertions are what make this a binding-path test: an ad-hoc httpx call
# anywhere in the handler would leave the ledger empty.

from tests.runtime.test_external_audit_binding import (  # noqa: E402
    _FakeRegistryClient,
    _FakeSearchHTTP,
)

_COUNTER_URL = "https://news.example/hormuz-reopens"
_COUNTER_HTML = (
    "<html><head><title>Hormuz reopens</title>"
    '<meta property="article:published_time" content="2026-09-20T09:00:00Z"/>'
    "</head><body><article><p>Shipping sources said the Strait of Hormuz "
    "reopened to commercial traffic on Tuesday, with transits resumed at "
    "normal volumes after a week of disruption.</p></article></body></html>"
)

#: A SECOND outlet, on a different host, carrying a DIFFERENT dispatch about
#: the same event. Not a restatement: 7d's near-verbatim fold would collapse two
#: mastheads running one wire story into one source — correctly — and a fixture
#: that ignored that would be secretly testing one page twice.
_COUNTER_URL_SECOND = "https://portwire.example/muscat-convoys-stood-down"
_COUNTER_HTML_SECOND = (
    "<html><head><title>Muscat stands down convoy escorts</title>"
    '<meta property="article:published_time" content="2026-09-21T14:30:00Z"/>'
    "</head><body><article><p>Port authorities in Muscat confirmed that the "
    "Strait of Hormuz has reopened, with convoy escorts stood down and tanker "
    "traffic back at pre-closure levels, according to two port agents briefed "
    "on the decision. Bunkering schedules were restored the same "
    "afternoon.</p></article></body></html>"
)

_SEARXNG_PAYLOAD = {
    "results": [
        {"url": _COUNTER_URL, "title": "Hormuz reopens",
         "content": "Transits resumed at normal volumes.",
         "engine": "duckduckgo", "score": 1.0},
        {"url": _COUNTER_URL_SECOND, "title": "Muscat stands down escorts",
         "content": "Convoy escorts stood down.",
         "engine": "duckduckgo", "score": 0.9},
    ],
    "unresponsive_engines": [],
}

#: What the fetch socket serves PER URL. ``max_refs_per_claim`` decides how many
#: of these are actually fetched, so the one-page e2e is unaffected by the
#: second entry existing.
_COUNTER_BODIES = {
    _COUNTER_URL: _COUNTER_HTML,
    _COUNTER_URL_SECOND: _COUNTER_HTML_SECOND,
}


@dataclass
class _Deps:
    """A dataclass because the REAL wiring does ``dataclasses.replace(deps, …)``
    — the production StandardDeps is one, and a plain object could not traverse
    the path this suite exercises."""

    pg_pool: Any
    extras: dict
    secrets_resolve: Any = None


@dataclass
class _PassDescriptor:
    """Just the two fields the wiring reads: identity + the GRANT leg."""

    identity: Any = field(default_factory=lambda: SimpleNamespace(
        id="contrary_evidence_pass",
    ))
    action_packs: list = field(
        default_factory=lambda: [{"pack_id": "web_access"}],
    )


class _ScriptedLLM:
    """The one sanctioned model double. Records every call.

    It is scripted with a VALID counter-query so a claim that needs the model
    leg still works — and the e2e asserts that the seeded claim did NOT reach
    it, because its negation is deterministic and the deterministic leg is
    preferred for replayability rather than for cost.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_complete(self, messages, **kwargs):
        self.calls.append(str(messages[0]["content"]))
        content = json.dumps({"counter_query": "hormuz transits resumed"})
        usage = type("_U", (), {"model": "gpt-oss-120b-test"})()
        return type("_R", (), {"content": content, "usage": usage})()


class _FakeResponse:
    def __init__(self, url: str, body: str, status_code: int = 200):
        self.url = url
        self.text = body
        self.status_code = status_code
        self.headers = {"content-type": "text/html; charset=utf-8"}


class _FakeFetchClient:
    """SOCKET — the page fetch. Records the URLs the tool actually GET s.

    Serves a body PER URL, so two counter pages are two documents rather than
    one document twice — otherwise 7d's near-verbatim fold would (rightly) count
    them as one outlet and F4 could never be satisfied in a test.
    """

    def __init__(self, recorder: list[str],
                 bodies: dict[str, str] | None = None,
                 status_code: int = 200):
        self._recorder = recorder
        self._bodies = dict(bodies or _COUNTER_BODIES)
        self._status = status_code

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url: str):
        self._recorder.append(url)
        return _FakeResponse(
            url, self._bodies.get(url, _COUNTER_HTML), self._status,
        )


@pytest.fixture
def fetch_socket(monkeypatch) -> list[str]:
    """Patch ONLY the guarded client factory web_fetch opens, plus the robots
    verdict. Everything above both stays real: the agency gate, the governor,
    the ledger row, the non-2xx refusal, the trafilatura extraction and the
    date discovery."""
    from legba.data.analysts.agency import web_tools
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_fetch as fetch_leg,
    )

    urls: list[str] = []

    def _client(**kwargs):
        return _FakeFetchClient(urls)

    async def _robots(url, **kwargs):
        return "allowed"

    monkeypatch.setattr(web_tools, "guarded_async_client", _client)
    monkeypatch.setattr(fetch_leg, "robots_decision", _robots)
    return urls


@pytest.fixture
def search_socket(monkeypatch) -> _FakeSearchHTTP:
    """Patch ONLY the provider's HTTP GET. The endpoint validation, the param
    build, ``parse_searxng_payload`` and the degradation read all stay real."""
    from legba.data.stack.search.base import SearchProviderHandler
    from legba.data.stack.search.liveness import DEFAULT_LIVENESS_CACHE
    from legba.runtime import search_handler_factory as shf

    shf.clear_search_handler_cache()
    DEFAULT_LIVENESS_CACHE.reset()
    rec = _FakeSearchHTTP(payload=_SEARXNG_PAYLOAD)
    monkeypatch.setattr(SearchProviderHandler, "_get_json", rec)
    yield rec
    shf.clear_search_handler_cache()
    DEFAULT_LIVENESS_CACHE.reset()


async def _binding(pool) -> AgencyToolBinding:
    """The pass's web binding, built by the REAL production wiring.

    Not a reconstruction — ``wire_standing_auditor_web_pack`` itself, over the
    shipped ``web_access`` descriptor and a registry serving the shipped
    ``search.searxng.local`` row, under THIS pass's own extras key.
    """
    from legba.data.analysts.agency import ToolContext as _TC
    from legba.runtime.external_audit_binding import (
        wire_standing_auditor_web_pack,
    )
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    AGENCY_HOLDER["agency"] = Agency()
    AGENCY_HOLDER["tool_context"] = _TC(queue=None, emit=None)

    async def _secrets(_sid: str) -> bytes:
        return b""

    deps = await wire_standing_auditor_web_pack(
        _PassDescriptor(),
        _Deps(pool, {}, secrets_resolve=_secrets),
        registry_client=_FakeRegistryClient(),
        extra_key=cep.WEB_BINDING_DEPS_EXTRA_KEY,
    )
    binding = deps.extras[cep.WEB_BINDING_DEPS_EXTRA_KEY]
    assert binding.tool_context.search is not None, (
        "the real wiring bound no provider — #85 has regressed"
    )
    # The ONE cap this suite cannot honour: `api_rate_per_minute: 20` counted
    # in a trailing wall-clock minute, against a file that runs several real
    # ticks in seconds. Nothing here asserts the rate cap and `governor.py` has
    # its own tests for it; every other cap, the gate, the pack resolution and
    # the ledger row all still run.
    binding.pack.governor.api_rate_per_minute = None
    return binding


async def _seed_read(conn) -> tuple[UUID, str, datetime]:
    """One assembled top-layer read carrying ONE claim with a deterministic
    negation, plus the watermark that makes this run see only it.

    NOTHING IS DELETED. Isolation comes from two handles the run carries: a
    desk key no other row can hold, and a watermark set to the instant before
    the insert — so the cold-start 48h reach cannot sweep in what an earlier
    test in the session left behind.
    """
    before = await conn.fetchval("SELECT now()")
    read_id, head_id = uuid4(), uuid4()
    desk_key = f"aaa_contrary_{uuid4().hex[:8]}"
    payload = {
        "tags": ["severity:high"],
        "data": {"assembly": {
            "schema": "assembly.v1",
            "regime": "assembly",
            "tier": "country",
            "blocks": [{
                "ordinal": 1,
                "finding_id": str(head_id),
                "desk": "country_composition",
                "target_id": desk_key,
                "spans": [{
                    "text": _CLAIM_SHUT,
                    "role": "bluf",
                    "origin": {
                        "head_id": str(head_id), "start": 0,
                        "end": len(_CLAIM_SHUT.encode("utf-8")),
                    },
                }],
            }],
        }},
    }
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, kind, title, body, confidence, data, target_id,
             analyst_id, analyst_version, schema_uri, produced_at)
        VALUES ($1, 'finding', $2, $3, 0.8, $4::jsonb, $5, $6, $7, $8, now())
        """,
        read_id, "Contrary desk read", _CLAIM_SHUT, json.dumps(payload),
        desk_key, "country_composition", "c" * 16,
        "iglu:legba/finding/jsonschema/1-0-0",
    )
    await conn.execute(
        "DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1",
        store.TRIGGER_CLASS,
    )
    await store.save_state(
        conn, {"watermark": before.isoformat()}, key=store.WATERMARK_KEY,
    )
    return read_id, desk_key, before


@pytest.mark.integration
@pytest.mark.asyncio
async def test_end_to_end_contends_a_claim_through_the_real_pack(
    pool, search_socket, fetch_socket,
) -> None:
    """The whole organ, through the real gate, the real search and the real
    fetch.

    Proves in one run: the binding RESOLVED the shipped ``config.provider``
    stack_ref; the counter-query reached that provider THROUGH the pack; the
    counter page was fetched THROUGH the pack; both spent a settled
    ``action_pack_invocations`` row; the query asked for the OPPOSITE state;
    the stance was derived from the fetched text; the ref carries the hash of
    the bytes we hold and the date the page itself states; and the heartbeat
    records what the run actually did.
    """
    async with pool.acquire() as conn:
        read_id, desk_key, before = await _seed_read(conn)
        ledger_before = await conn.fetchval(
            "SELECT count(*) FROM action_pack_invocations "
            "WHERE pack_id = 'web_access' AND occurred_at > $1", before,
        )

    llm = _ScriptedLLM()
    binding = await _binding(pool)
    deps = _Deps(pool, {
        cep.LLM_DEPS_EXTRA_KEY: llm,
        cep.WEB_BINDING_DEPS_EXTRA_KEY: binding,
    })
    run_id = uuid4()
    result = await cep.handle(
        None,
        {"run_id": run_id, "claims_per_run": 60, "max_refs_per_claim": 1},
        deps,
    )

    # --- the record ----------------------------------------------------
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM claim_contentions WHERE target_id = $1", desk_key,
        )
        ledger = await conn.fetch(
            "SELECT tool_name, outcome FROM action_pack_invocations "
            "WHERE pack_id = 'web_access' AND occurred_at > $1", before,
        )
    assert row is not None, "no contention record for the seeded read"
    # ONE fetched page is ONE retrieval. The page says the opposite and its own
    # ref records that; F4 is why the CLAIM's stance is `qualifies` — see
    # ``test_end_to_end_two_independent_pages_contradict`` for the other half.
    assert row["stance"] == "qualifies"
    assert row["independent_pages"] == 1
    assert row["host_class"] == cf.HOST_CLASS_UNKNOWN
    assert str(row["page_published_at"]) == "2026-09-20"
    assert row["subject_overlap"] >= r2.MIN_SUBJECT_OVERLAP
    assert row["derivation"] == "polarity"
    assert row["query_source"] == "polarity"
    assert row["query_novel_tokens"] > 0
    assert row["polarity_group"] == "closure"
    assert row["rung"]
    assert row["receipt_id"] == run_id
    assert row["expires_at"] > row["as_of"]

    # --- the counter-query asked for the OPPOSITE ----------------------
    assert "reopened" in row["query"]
    assert "shut" not in row["query"]
    assert search_socket.queries, "the provider socket was never consulted"
    assert any("reopened" in q for q in search_socket.queries)

    # --- the ref is a page we HOLD -------------------------------------
    refs = json.loads(row["refs"]) if isinstance(row["refs"], str) else row["refs"]
    assert len(refs) == 1
    ref = refs[0]
    assert ref["url"] == _COUNTER_URL
    assert ref["stance"] == "contradicts"
    assert ref["published_at"], "the page states a date and it was discovered"
    assert "2026-09-20" in str(ref["published_at"])
    from legba.data.research_evidence import content_hash_for

    assert len(ref["sha256"]) == 64
    assert ref["sha256"] != content_hash_for("")
    assert "reopened" in ref["quote"]
    assert _COUNTER_URL in fetch_socket, "the page was not fetched"

    # --- THE BINDING PATH ----------------------------------------------
    # Both egress acts went through the pack. An ad-hoc httpx call anywhere in
    # the handler would leave these rows unwritten.
    assert len(ledger) > ledger_before
    tools = {r["tool_name"] for r in ledger}
    assert "web_search" in tools
    assert "web_fetch" in tools
    # THE EXACT EGRESS BUDGET, not merely "some rows landed": ONE search and
    # ONE fetch per claim at `max_refs_per_claim=1`. A regression that
    # re-queried, retried a fetch, or escalated a rung would show up here as a
    # third row rather than as a bill nobody reads until the month turns.
    assert sum(1 for r in ledger if r["tool_name"] == "web_search") == 1
    assert sum(1 for r in ledger if r["tool_name"] == "web_fetch") == 1
    assert {r["outcome"] for r in ledger} == {"completed"}

    # --- the deterministic leg was preferred ---------------------------
    for call in llm.calls:
        assert _CLAIM_SHUT not in call, (
            "a claim with a deterministic negation must not reach the model"
        )

    # --- the heartbeat --------------------------------------------------
    async with pool.acquire() as conn:
        state = await store.load_state(conn, key=store.HEARTBEAT_KEY)
        watermark = await store.load_state(conn, key=store.WATERMARK_KEY)
    assert state["sub_handler"] == "contrary_evidence_pass"
    assert state["claims_contended"] >= 1
    assert state["stances"].get("qualifies", 0) >= 1
    # The fence and host-class mixes report ZEROS rather than omitting keys, so
    # a week of heartbeats says which rule is doing the work and what the free
    # rung is actually returning — the run that produced the three false rows
    # would have read `{reference: 3, ...}` here.
    assert set(state["fences"]) == set(cf.FENCES)
    assert state["fences"][cf.FENCE_INDEPENDENT_PAGES] >= 1
    assert set(state["host_classes"]) == set(cf.HOST_CLASSES)
    assert state["host_classes"][cf.HOST_CLASS_UNKNOWN] >= 1
    assert state["searches"] >= 1
    assert state["pages_fetched"] >= 1
    assert state["rows_written"] >= 1
    assert state["paid_escalations"] == 0, "the metered rung must stay unspent"
    assert watermark["watermark"] > before.isoformat()

    # --- THE COMPOSITION CAN FIND IT AGAIN ------------------------------
    # The key the pass stored, recomputed from the span a composition carries.
    # Joining on `finding_id` here would be the natural mistake and would match
    # nothing the next cycle: the composition being built is a NEW row.
    carried = {
        "blocks": [{
            "ordinal": 1,
            "finding_id": row["origin_head_id"],
            "spans": [{
                "text": _CLAIM_SHUT,
                "origin": {
                    "head_id": str(row["origin_head_id"]),
                    "start": 0, "end": len(_CLAIM_SHUT.encode("utf-8")),
                },
            }],
        }],
        "tensions": [],
        "tension_checked": {"pairs_examined": 0, "pairs_found": 0},
    }
    assert ct.ordinals_by_claim(carried) == {row["claim_id"]: 1}
    merged = await ct.merge_contrary_tension(carried, pool)
    # ...and finds NOTHING, because a `qualifies` row is not a tension. The
    # composition renders exactly as it does today — which is the point of the
    # fence: the India and Korea compositions would otherwise have carried the
    # first live run's two polarity-derived false contradictions.
    assert merged["tensions"] == []

    # --- the receipt is a receipt, not a finding ------------------------
    assert result.finding is not None
    assert "Contrary-evidence pass" in result.finding.title
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }



@pytest.mark.integration
@pytest.mark.asyncio
async def test_end_to_end_two_independent_pages_contradict(
    pool, search_socket, fetch_socket,
) -> None:
    """F4's other half, END TO END: the fences refuse a lone page, they do not
    refuse everything.

    Same organ, same real gate, same real search and fetch — and
    ``max_refs_per_claim=2`` against TWO counter pages on two hosts carrying two
    different dispatches. That is what ``contradicts`` costs now, and this is
    the test that would fail if the fences were tightened into a pass that can
    only ever emit ``qualifies``. The composition finds it and renders the
    hedged line, which is the behaviour the lane is protecting rather than
    removing.
    """
    async with pool.acquire() as conn:
        read_id, desk_key, before = await _seed_read(conn)

    deps = _Deps(pool, {
        cep.LLM_DEPS_EXTRA_KEY: _ScriptedLLM(),
        cep.WEB_BINDING_DEPS_EXTRA_KEY: await _binding(pool),
    })
    await cep.handle(
        None, {"claims_per_run": 60, "max_refs_per_claim": 2}, deps,
    )

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM claim_contentions WHERE target_id = $1", desk_key,
        )
        state = await store.load_state(conn, key=store.HEARTBEAT_KEY)
    assert row is not None, "no contention record for the seeded read"
    assert row["stance"] == "contradicts"
    assert row["derivation"] == "polarity"
    assert row["independent_pages"] == 2
    assert row["host_class"] == cf.HOST_CLASS_UNKNOWN
    assert row["subject_overlap"] >= r2.MIN_SUBJECT_OVERLAP
    # The DATE the gate parsed, off the page's own metadata — never invented.
    assert str(row["page_published_at"]) in ("2026-09-20", "2026-09-21")

    refs = json.loads(row["refs"]) if isinstance(row["refs"], str) else row["refs"]
    assert {r["url"] for r in refs} == {_COUNTER_URL, _COUNTER_URL_SECOND}
    assert all(r["fence"] == "" for r in refs), "a fence refused an admissible page"
    assert all(r["host_class"] == cf.HOST_CLASS_UNKNOWN for r in refs)
    assert _COUNTER_URL in fetch_socket and _COUNTER_URL_SECOND in fetch_socket
    # No fence fired on this claim at all — the count that says so is on the
    # heartbeat, beside the ones that did fire on other runs.
    assert sum(state["fences"].values()) == 0

    # --- and the COMPOSITION renders it --------------------------------
    carried = {
        "blocks": [{
            "ordinal": 1,
            "finding_id": row["origin_head_id"],
            "spans": [{
                "text": _CLAIM_SHUT,
                "origin": {
                    "head_id": str(row["origin_head_id"]),
                    "start": 0, "end": len(_CLAIM_SHUT.encode("utf-8")),
                },
            }],
        }],
        "tensions": [],
        "tension_checked": {"pairs_examined": 0, "pairs_found": 0},
    }
    merged = await ct.merge_contrary_tension(carried, pool)
    assert len(merged["tensions"]) == 1
    entry = merged["tensions"][0]
    assert entry["kind"] == ct.TENSION_KIND_CONTRARY
    assert entry["b_ref"]["counter_url"] in (_COUNTER_URL, _COUNTER_URL_SECOND)
    assert entry["detail"]["independent_pages"] == 2
    assert "Not adjudicated" in entry["statement"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_no_web_binding_records_search_failed_and_says_so(pool) -> None:
    """The 08-12 lesson, in this instrument. A run with a dead search plane
    still ends status='success', so the HEARTBEAT is what has to tell a quiet
    web from a broken pass — and ``claims_contended`` deliberately excludes
    ``search_failed`` so a dead plane cannot look busy."""
    async with pool.acquire() as conn:
        read_id, desk_key, before = await _seed_read(conn)

    deps = _Deps(pool, {cep.LLM_DEPS_EXTRA_KEY: _ScriptedLLM()})
    await cep.handle(None, {"claims_per_run": 5}, deps)

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT stance, reason, derivation, refs, host_class, "
            "page_published_at, subject_overlap, independent_pages "
            "FROM claim_contentions WHERE target_id = $1", desk_key,
        )
        state = await store.load_state(conn, key=store.HEARTBEAT_KEY)
    assert row is not None
    assert row["stance"] == "search_failed"
    # The fences never ran, so all four of their numbers are NULL. A zero
    # ``independent_pages`` here would be a default wearing a measurement's
    # clothes — "we looked and found no second page" against "we could not
    # look", which is the split this stance exists to keep.
    assert row["host_class"] is None
    assert row["page_published_at"] is None
    assert row["subject_overlap"] is None
    assert row["independent_pages"] is None
    assert row["reason"] == cep.REASON_NO_BINDING
    assert row["derivation"] == "none"
    assert state["claims_contended"] == 0
    assert "no web_access binding wired" in state["degraded_reason"]
    assert state["healthy"] is False

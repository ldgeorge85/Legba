# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""V-B (2026-07-31) — SCOPED-ABSENCE claims judged against the input SLICE.

The readout's 6x hard-fail enrichment class, and the dominant false-alarm source
in BOTH panels. "No NEW / LARGE-SCALE / TIGHTENED X" is a claim about the WHOLE
input slice; the judge sees only the CITATION subset, reads topical term
PRESENCE as contradiction, and hard-fails. The measured artifact (dossier
``pick 1``, ``country_watch_ht`` / economic_coercion, hard_fail
``judge_contradicted``):

    "None of the 25 recent signals report new or tightened sanctions
     designations, asset freezes, export-control blacklists, or de-listings
     affecting Haiti or its entities [1]…[25]."

The slice IS retained — ``analyst_traces.input_row_refs``, one row per run —
and was never consulted. Surfaces:

  1. CLASSIFY (B1) — absence grammar + scope-qualifier extraction, deterministic
     and gated on the SAME ``_is_absence_claim`` the floor and the V3 route use.
  2. STAGE 1 (B2) — a lexical screen of the slice TITLES; no collision ⇒
     SUPPORTED (``absence_slice_verified``), no LLM call at all. The DESK'S OWN
     country tokens are excluded from the screen (every Haiti title says Haiti).
  3. STAGE 2 (B2) — ONE bounded call on a collision. A violation must NAME the
     violating title, resolved against the shown set (the V-D earned-severity
     rule); anything unresolvable decides nothing.
  4. HONESTY (B3) — an unreadable slice degrades to today's behavior, counted
     ``absence_slice_unavailable``. Never a fabricated pass.
  5. W31 COEXISTENCE — a claim already flagged ``unscoped_absence_claim`` is
     skipped, so a content pass can never erase the phrasing flag.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import uuid4

import legba.data.provenance.verify as V
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    VERDICT_SUPPORTED,
    absence_scope_qualifier,
    load_absence_slice_titles,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# Doubles
# ---------------------------------------------------------------------------


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None


class _StubJudge:
    """Returns a canned payload per SYSTEM prompt, so the slice call is isolated."""

    subprovider = "stub-judge"

    def __init__(self, *, slice_payload: dict[str, Any] | None = None) -> None:
        self._slice = slice_payload
        self.slice_calls = 0
        self.other_calls = 0
        self.slice_prompts: list[str] = []

    async def chat_complete(
        self, messages, *, max_tokens=None, temperature=None, system=None, **kw
    ):
        if system == V._ABSENCE_SLICE_JUDGE_SYSTEM:
            self.slice_calls += 1
            self.slice_prompts.append(messages[0]["content"])
            return _Response(json.dumps(self._slice or {}))
        self.other_calls += 1
        # Every other partition: mark everything supported so the V-B verdict is
        # the only thing under test.
        n = messages[0]["content"].count("\n1. ") or 1
        claims = messages[0]["content"].split("CLAIMS:\n")[-1].strip().splitlines()
        n = max(len(claims), 1)
        return _Response(json.dumps({"verdicts": ["supported"] * n}))


class _FakeConn:
    """Minimal asyncpg-shaped double over ``analyst_traces`` + the title tables."""

    def __init__(self, titles: list[str] | None, *, refs: int = 3) -> None:
        self._titles = titles
        self._refs = refs
        self.fetch_calls = 0

    async def fetchrow(self, sql: str, *args):
        if "analyst_traces" in sql:
            if self._titles is None:
                return None  # trace pruned / never written
            return {"input_row_refs": [uuid4() for _ in range(self._refs)]}
        return None

    async def fetch(self, sql: str, *args):
        self.fetch_calls += 1
        return [{"title": t} for t in (self._titles or [])]


# The live artifact, lightly shortened (the marker run is what mattered there).
_HAITI_CLAIM = (
    "- None of the recent signals report new or tightened sanctions "
    "designations, asset freezes, export-control blacklists, or de-listings "
    "affecting Haiti or its entities [1][2][3]."
)
_HAITI_BODY = (
    "Gang control of the capital's port districts persisted this week [1].\n"
    + _HAITI_CLAIM
    + "\n"
)


def _citations() -> list[dict[str, Any]]:
    return [
        {"marker": f"[{n}]", "signal_id": str(uuid4()), "title": f"cited row {n}"}
        for n in (1, 2, 3)
    ]


# Titles that MENTION Haiti and sanctions but report no NEW designation — the
# exact collision that produced the false hard-fail.
_BENIGN_TITLES = [
    "Haiti: humanitarian corridors reopen in the capital",
    "Analysis: how sanctioned Haitian gang leaders launder fuel money",
    "Haiti's transitional council names a new interior minister",
]
_VIOLATING_TITLE = (
    "US Treasury adds three Haitian gang financiers to the sanctions list"
)


def _claim_verdict(report, needle: str):
    return next((cv for cv in report.claim_verdicts if needle in cv.text), None)


# ---------------------------------------------------------------------------
# 1. CLASSIFY (B1)
# ---------------------------------------------------------------------------


def test_scope_qualifier_extraction() -> None:
    assert absence_scope_qualifier(_HAITI_CLAIM) == "new"
    assert (
        absence_scope_qualifier("No large-scale exercise was observed.")
        == "large-scale"
    )
    assert absence_scope_qualifier("No announcements of major procurement.") == "major"


def test_non_qualified_and_non_absence_claims_are_not_this_branch() -> None:
    """A plain negative keeps today's route; a positive claim is never absence."""
    assert absence_scope_qualifier("No strikes were reported.") is None
    assert absence_scope_qualifier("The bank raised rates by fifty points.") is None
    # "new" inside another word must not trip it.
    assert absence_scope_qualifier("No newspaper coverage was found.") is None


# ---------------------------------------------------------------------------
# 2. STAGE 1 — the deterministic screen
# ---------------------------------------------------------------------------


def test_content_terms_are_topical_and_stemmed() -> None:
    terms = V._absence_content_terms(_HAITI_CLAIM, target_id=None)
    assert "sanction" in terms  # singular-stemmed, so it screens "sanctioned"
    assert "designation" in terms
    # Function words, the absence/scope vocabulary and the reporting verbs carry
    # no topical signal and never enter the screen.
    for noise in ("new", "recent", "signal", "report", "none", "entities"):
        assert noise not in terms


def test_ubiquitous_terms_are_dropped_from_the_screen() -> None:
    """A term present in most of the slice discriminates NOTHING — on a country
    desk every title names the country. Data-driven, so it needs no gazetteer."""
    terms = {"haiti", "sanction"}
    titles = [
        "Haiti: humanitarian corridors reopen in the capital",
        "Analysis: how sanctioned Haitian gang leaders launder fuel money",
        "Haiti's transitional council names an interior minister",
    ]
    hits, discriminated = V._absence_slice_candidates(terms, titles)
    assert discriminated is True
    assert hits == [titles[1]]  # only the SANCTION collision, not all three


def test_a_saturated_vocabulary_is_never_read_as_verified() -> None:
    """When the filter leaves nothing to screen with, the screen did not run —
    the caller must not treat that as a clean slice."""
    titles = ["Sanctions on Haiti tightened", "Haiti sanctions review", "Haiti sanctions"]
    hits, discriminated = V._absence_slice_candidates({"haiti", "sanction"}, titles)
    assert discriminated is False
    assert hits  # everything collides — the decision goes to stage 2


async def test_no_term_collision_verifies_the_absence(monkeypatch) -> None:
    """Nothing in the slice is even topically about the thing said to be absent
    ⇒ SUPPORTED, deterministically, with no LLM call."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge()
    conn = _FakeConn(
        [
            "Haiti: humanitarian corridors reopen in the capital",
            "Haiti's transitional council names an interior minister",
        ]
    )
    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=judge,
        target_id="country_watch_ht",
        slice_conn=conn,
        run_id=uuid4(),
    )
    cv = _claim_verdict(report, "tightened sanctions")
    assert cv is not None and cv.verdict == VERDICT_SUPPORTED
    assert "verified against" in (cv.detail or "")
    assert report.counters["absence_slice_verified"] == 1
    assert judge.slice_calls == 0  # stage 2 never fired


async def test_collision_triggers_exactly_one_bounded_stage_two_call(
    monkeypatch,
) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(slice_payload={"verdicts": ["supported"], "quotes": [""]})
    conn = _FakeConn(_BENIGN_TITLES)
    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=judge,
        target_id="country_watch_ht",
        slice_conn=conn,
        run_id=uuid4(),
    )
    assert judge.slice_calls == 1
    assert report.counters["absence_slice_candidates"] == 1
    # The collision was benign — the absence still verifies.
    assert _claim_verdict(report, "tightened sanctions").verdict == VERDICT_SUPPORTED
    assert report.counters["absence_slice_verified"] == 1
    # Only the colliding titles are shown, and the claim rides along.
    prompt = judge.slice_prompts[0]
    assert "sanctioned Haitian gang leaders" in prompt
    assert "humanitarian corridors" not in prompt


# ---------------------------------------------------------------------------
# 3. STAGE 2 — a violation must NAME a resolvable title
# ---------------------------------------------------------------------------


async def test_named_violating_title_is_a_hard_fail(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        slice_payload={"verdicts": ["contradicted"], "quotes": [_VIOLATING_TITLE]}
    )
    conn = _FakeConn([*_BENIGN_TITLES, _VIOLATING_TITLE])
    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=judge,
        target_id="country_watch_ht",
        slice_conn=conn,
        run_id=uuid4(),
    )
    cv = _claim_verdict(report, "tightened sanctions")
    assert cv.verdict == FAIL_CLASS_HARD
    assert cv.reason == "absence_slice_contradicted"
    assert "Treasury adds three Haitian gang financiers" in (cv.detail or "")
    assert report.counters["absence_slice_contradicted"] == 1
    span = next(
        s for s in report.unsupported_spans if s.reason == "absence_slice_contradicted"
    )
    assert span.as_dict()["fail_class"] == FAIL_CLASS_HARD


async def test_unresolvable_violation_decides_nothing(monkeypatch) -> None:
    """A 'contradicted' whose named title is not in the shown set is noise — it
    can never manufacture a hard fail; today's verdict stands."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    judge = _StubJudge(
        slice_payload={
            "verdicts": ["contradicted"],
            "quotes": ["EU announces a fresh Haiti arms embargo"],  # not shown
        }
    )
    conn = _FakeConn([*_BENIGN_TITLES, _VIOLATING_TITLE])
    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=judge,
        target_id="country_watch_ht",
        slice_conn=conn,
        run_id=uuid4(),
    )
    assert "absence_slice_contradicted" not in report.counters
    assert report.counters["absence_slice_unresolved"] == 1


async def test_stage_two_transport_failure_decides_nothing(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")

    class _Boom(_StubJudge):
        async def chat_complete(self, messages, *, system=None, **kw):
            if system == V._ABSENCE_SLICE_JUDGE_SYSTEM:
                raise RuntimeError("transport down")
            return await super().chat_complete(messages, system=system, **kw)

    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=_Boom(),
        target_id="country_watch_ht",
        slice_conn=_FakeConn([*_BENIGN_TITLES, _VIOLATING_TITLE]),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_unresolved"] == 1
    assert "absence_slice_verified" not in report.counters


# ---------------------------------------------------------------------------
# 4. HONESTY (B3) — an unreadable slice never fabricates a pass
# ---------------------------------------------------------------------------


async def test_pruned_trace_degrades_to_todays_behavior(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_watch_ht",
        slice_conn=_FakeConn(None),  # no trace row
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_unavailable"] == 1
    assert "absence_slice_verified" not in report.counters


async def test_no_slice_conn_is_a_total_no_op(monkeypatch) -> None:
    """Every existing caller passes neither slice_conn nor run_id — byte-identical."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    a = await verify_finding_faithfulness(
        body=_HAITI_BODY, citations=_citations(), judge_llm=_StubJudge()
    )
    assert not any(k.startswith("absence_slice") for k in a.counters)


async def test_read_error_degrades(monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")

    class _BadConn:
        async def fetchrow(self, *a, **kw):
            raise RuntimeError("pg gone")

    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        judge_llm=_StubJudge(),
        target_id="country_watch_ht",
        slice_conn=_BadConn(),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_unavailable"] == 1


async def test_loader_distinguishes_missing_from_empty() -> None:
    """``None`` = unreadable (the honest unavailable answer); ``[]`` = a real
    empty slice."""
    assert await load_absence_slice_titles(_FakeConn(None), uuid4()) is None
    assert await load_absence_slice_titles(None, uuid4()) is None
    assert await load_absence_slice_titles(_FakeConn(_BENIGN_TITLES), None) is None
    # A trace whose refs resolve to no titles is a REAL empty slice, not unknown.
    assert await load_absence_slice_titles(_FakeConn([]), uuid4()) == []
    assert await load_absence_slice_titles(_FakeConn(_BENIGN_TITLES), uuid4()) == (
        _BENIGN_TITLES
    )


# ---------------------------------------------------------------------------
# 5. W31 coexistence + the judge-off path
# ---------------------------------------------------------------------------


async def test_w31_scope_flag_is_never_erased_by_a_content_pass(monkeypatch) -> None:
    """An UNSCOPED world-negative keeps its W31 phrasing flag even when the slice
    verifies its content — the two defects are orthogonal."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    body = "No new large-scale outages were reported.\n"
    report = await verify_finding_faithfulness(
        body=body,
        citations=_citations(),
        target_id="country_watch_ht",
        slice_conn=_FakeConn(["Haiti: council names an interior minister"]),
        run_id=uuid4(),
    )
    flagged = [
        cv for cv in report.claim_verdicts if cv.reason == "unscoped_absence_claim"
    ]
    assert flagged, "the W31 backstop must still own this claim"
    assert report.counters["absence_slice_scope_flagged"] == 1
    assert "absence_slice_verified" not in report.counters


async def test_stage_one_runs_with_the_judge_off(monkeypatch) -> None:
    """Stage 1 is deterministic — it verifies scoped negatives on the floor path
    too; stage 2 simply cannot run, and says so."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=_HAITI_BODY,
        citations=_citations(),
        target_id="country_watch_ht",
        slice_conn=_FakeConn([*_BENIGN_TITLES, _VIOLATING_TITLE]),
        run_id=uuid4(),
    )
    assert report.counters["absence_slice_candidates"] == 1
    assert report.counters["absence_slice_unresolved"] == 1

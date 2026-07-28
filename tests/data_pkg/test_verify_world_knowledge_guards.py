# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""M13 / M14 / M15 (2026-07-06 live audit) — write/verify-time correctness guards.

Deterministic: the optional LLM judge is OFF by default (floor) or a canned stub.

  * M13 — the stale-cutoff current-leader guard (``stale_leader_spans``): calling
    the SITTING US president "former" is FLAGGED (demotes effective_confidence),
    a correct "former President Biden" is NOT.
  * M14 — honest NULL-RESULT / survey findings: the RANGE citation ``[1-92]``
    resolves, a ``[no citation]`` line is floor-exempt, and a corpus-negative
    routes to the survey judge rubric (an absence scores healthy; a fabrication
    still scores low).
  * M15 — the cross-target guard (``cross_target_leak_span``): a per-country
    finding naming ONLY other countries than its desk target is FLAGGED.
"""

from __future__ import annotations

import pytest

from legba.data.provenance.verify import (
    _NULL_RESULT_JUDGE_SYSTEM,
    _deterministic_floor,
    _is_fact_asserting,
    _is_null_result_finding,
    _range_markers,
    cross_target_leak_span,
    stale_leader_spans,
    verify_finding_faithfulness,
)


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _CapturingJudge:
    """Judge stub that returns canned verdicts AND records the system prompt used."""

    subprovider = "vllm:stub"

    def __init__(self, verdicts_json: str) -> None:
        self._json = verdicts_json
        self.systems: list[str] = []
        self.prompts: list[str] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        self.systems.append(system or "")
        self.prompts.append(messages[0]["content"] if messages else "")
        return _Response(self._json)


# ---------------------------------------------------------------------------
# M13 — stale-cutoff current-leader guard
# ---------------------------------------------------------------------------


def test_stale_leader_flags_sitting_president_called_former():
    spans = stale_leader_spans(
        "renewed US-Russia cooperation via former President Trump [1]"
    )
    assert len(spans) == 1
    assert spans[0].reason == "stale_leader"


def test_stale_leader_flags_reversed_shape_and_wrong_current_holder():
    assert stale_leader_spans("Trump, the former president, brokered the deal")
    assert stale_leader_spans("the current US president Joe Biden signed the order")


def test_stale_leader_flags_hyphenated_ex_president_trump():
    # FIX #3: the hyphenated "ex-President Trump" (no space after 'ex-') must flag.
    assert stale_leader_spans("brokered by ex-President Trump last week")
    assert stale_leader_spans("past President Trump weighed in")


def test_stale_leader_ignores_correct_references():
    # Biden IS a former president — correct, must NOT flag.
    assert stale_leader_spans("under former President Biden, policy shifted") == []
    # FIX #3: "President Biden, now a private citizen" CORRECTLY says he is out of
    # office — the dropped bare now/today proximity must no longer false-flag it.
    assert stale_leader_spans("President Biden, now a private citizen, spoke out") == []
    assert stale_leader_spans("President Biden today defended his record") == []
    # A different former president named plainly — must NOT flag.
    assert stale_leader_spans("President Obama attended the summit") == []
    # The sitting president named correctly — must NOT flag.
    assert stale_leader_spans("President Trump announced new sanctions today") == []
    assert stale_leader_spans("") == []


@pytest.mark.asyncio
async def test_verify_demotes_stale_leader_finding():
    """A cited clause naming the sitting president 'former' still demotes: the
    stale-leader guard folds an extra unsupported checkable claim (flag, not delete)."""
    body = "US-Russia cooperation is being renewed via former President Trump [1]."
    citations = [{"marker": "[1]", "signal_id": "sig-1"}]
    report = await verify_finding_faithfulness(body=body, citations=citations)
    # 1 supported cited clause + 1 stale_leader guard span → 1/2 = 0.5.
    assert report.faithfulness_score == pytest.approx(0.5)
    assert any(s.reason == "stale_leader" for s in report.unsupported_spans)


# ---------------------------------------------------------------------------
# M14 — range parser, [no citation] exemption, null-result rubric
# ---------------------------------------------------------------------------


def test_range_markers_expands_and_caps():
    assert _range_markers("survey [3-6]") == {3, 4, 5, 6}
    assert _range_markers("en-dash [1–3]") == {1, 2, 3}
    assert _range_markers("no range here [5]") == set()
    # Pathological width is ignored (guarded).
    assert _range_markers("[1-100000]") == set()


def test_range_citation_resolves_survey_clause():
    """``[1-92]`` cites the whole enumerated corpus; a member signal resolves it.
    Without the range parser the clause would floor as ``no_citation`` (0.0)."""
    body = "The signals concern floods, sports fixtures, and trade logistics [1-92]."
    citations = [{"marker": "[5]", "signal_id": "sig-5"}]
    report = _deterministic_floor(body, citations)
    assert report.faithfulness_score == pytest.approx(1.0)
    assert report.supported_claims == 1


def test_no_citation_marker_is_floor_exempt():
    line = "Tehran's posture is best read as steady in our synthesis [no citation]"
    assert _is_fact_asserting(line) is False
    report = _deterministic_floor(line, [])
    assert report.checkable_claims == 0
    assert report.faithfulness_score == pytest.approx(1.0)


def test_null_result_finding_detected_and_positive_not():
    null_body = (
        "None of the 78 signals reference political unrest concerning Romania's "
        "leadership. The signals focus on floods, sports, and trade."
    )
    assert _is_null_result_finding(null_body) is True
    positive_body = (
        "Iran resumed uranium enrichment at Natanz [1]. Centrifuges were "
        "reinstalled at Fordow [2]. The IAEA confirmed the breakout [3]."
    )
    assert _is_null_result_finding(positive_body) is False


@pytest.mark.asyncio
async def test_null_result_uses_survey_rubric_and_scores_healthy(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = (
        "No proliferation activity was observed. The 51 signals focus on floods, "
        "sports, and trade."
    )
    judge = _CapturingJudge('{"verdicts": ["supported", "supported"]}')
    report = await verify_finding_faithfulness(body=body, citations=[], judge_llm=judge)
    # The survey rubric was selected...
    assert any("NULL-RESULT" in s for s in judge.systems)
    assert judge.systems[0] == _NULL_RESULT_JUDGE_SYSTEM
    # ...and an honest null scores healthy rather than collapsing to ~0.
    assert report.judge_status == "llm"
    assert report.faithfulness_score >= 0.9


@pytest.mark.asyncio
async def test_fabrication_still_scores_low_with_standard_rubric(monkeypatch):
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    body = "Iran launched a nuclear warhead at Tel Aviv this morning [1]."
    citations = [{"marker": "[1]", "signal_id": "sig-1", "title": "IRNA sports roundup"}]
    judge = _CapturingJudge('{"verdicts": ["contradicted"]}')
    report = await verify_finding_faithfulness(body=body, citations=citations, judge_llm=judge)
    # A positive fabrication is NOT a null-result → standard rubric, scores low.
    assert judge.systems[0] != _NULL_RESULT_JUDGE_SYSTEM
    assert report.faithfulness_score < 0.5


# ---------------------------------------------------------------------------
# M15 — cross-target leak guard
# ---------------------------------------------------------------------------


def test_cross_target_leak_flags_wrong_country():
    span = cross_target_leak_span(
        title="Romania – Leadership transition risk low",
        body="Near-term change in Romania's head of government is unlikely.",
        target_id="country_g20_tr",
    )
    assert span is not None
    assert span.reason == "cross_target_leak"
    assert "romania" in span.text.lower()


def test_cross_target_leak_common_word_slug_desks_are_not_inert():
    """FIX #1: desks whose ISO-2 slug is a common English word (India='in',
    Italy='it', US='us', Indonesia='id') must still flag — the slug must NOT be in
    the on-target mention set, or the word 'in'/'it'/'us' in normal prose fires the
    early-return on every finding and silently disables the guard."""
    # India desk, finding entirely about Pakistan, never says "India" → FLAGGED.
    span = cross_target_leak_span(
        title="Leadership steady",
        body="Pakistan's coalition faces no near-term instability in the capital.",
        target_id="country_g20_in",
    )
    assert span is not None and span.reason == "cross_target_leak"
    assert "pakistan" in span.text.lower()
    # A legitimate India finding that names India → NOT flagged (even though the
    # word 'in' appears throughout the prose).
    assert cross_target_leak_span(
        title="India leadership steady",
        body="India's ruling coalition remains stable in the interim.",
        target_id="country_g20_in",
    ) is None


def test_cross_target_leak_none_when_on_target_or_generic():
    # Mentions its own country (Turkey) → on-target.
    assert cross_target_leak_span(
        title="Turkey leadership steady",
        body="President Erdogan faces no near-term challenge in Turkey.",
        target_id="country_g20_tr",
    ) is None
    # Names no country at all → generic/thin, not flagged.
    assert cross_target_leak_span(
        title="Leadership transition risk low",
        body="No drivers of leadership change are observed in the signals.",
        target_id="country_g20_tr",
    ) is None
    # Non-country / meta target → never flagged.
    assert cross_target_leak_span(
        title="World assessment", body="Romania and Poland see elections.",
        target_id=None,
    ) is None


@pytest.mark.asyncio
async def test_verify_demotes_cross_target_finding():
    """The live M15 shape: a Turkey desk head entirely about Romania → flagged +
    demoted (the guard adds an unsupported checkable claim), not deleted."""
    body = (
        "**BLUF:** Near-term change in Romania's head of government or head of "
        "state is unlikely.\nRomania's current leaders are not under pressure."
    )
    report = await verify_finding_faithfulness(
        body=body, citations=[], title="Romania – Leadership transition risk low",
        target_id="country_g20_tr",
    )
    assert any(s.reason == "cross_target_leak" for s in report.unsupported_spans)
    assert report.faithfulness_score < 1.0


# ---------------------------------------------------------------------------
# E-1 (2026-07-27 sweep rec #2) — the FACTS-RECONCILED officeholder guard.
# The M13 heuristic above is curated-regex world knowledge; this sibling probes
# the CURRENT facts-table officeholder row and flags a mismatch under the
# DISTINCT reason ``stale_leader_vs_facts`` (hard_fail — same entity-scramble
# class, separable in calibration). HONESTY: the seed facts can THEMSELVES be
# stale (known live: the DRC PM upstream), so a mismatch only ever
# DEMOTES/flags — never auto-corrects; every ambiguity fails OPEN; a facts
# read failure degrades to no flag.
# ---------------------------------------------------------------------------

from legba.data.provenance.verify import (  # noqa: E402 — E-1 section imports
    FAIL_CLASS_HARD,
    extract_officeholder_claims,
    stale_leader_vs_facts_spans,
)


class _FactsConn:
    """Stub conn: canned current-officeholder rows; records the probe calls."""

    def __init__(self, rows: list[dict] | None = None) -> None:
        self.rows = rows or []
        self.calls: list[tuple] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return list(self.rows)


class _FailingConn:
    async def fetch(self, sql, *args):
        raise RuntimeError("facts table unavailable")


def _office_row(subject: str, predicate: str, value: str) -> dict:
    return {"subject": subject, "predicate": predicate, "value": value}


# --- pure extraction --------------------------------------------------------


def test_extract_officeholder_claims_recognizes_the_explicit_shapes():
    country_first = extract_officeholder_claims(
        "Slovenia's Prime Minister Janez Jansa met the delegation."
    )
    assert len(country_first) == 1
    assert country_first[0].role == "prime minister"
    assert country_first[0].person == "Janez Jansa"
    # acronym surface (uppercase-only) expands to the full alias group
    acronym = extract_officeholder_claims(
        "DRC Prime Minister Sama Lukonde announced the budget."
    )
    assert acronym and "democratic republic of the congo" in acronym[0].country_aliases
    role_first = extract_officeholder_claims(
        "President Nicolas Maduro of Venezuela spoke on Tuesday."
    )
    assert role_first and role_first[0].country_aliases == ("venezuela",)
    # repeated claim de-duplicates
    dedup = extract_officeholder_claims(
        "US President Trump signed it. US President Trump praised it."
    )
    assert len(dedup) == 1


def test_extract_officeholder_claims_skips_qualified_and_noise():
    # correct prose about a PREDECESSOR must never enter the probe
    assert extract_officeholder_claims("former President Biden attended.") == []
    assert extract_officeholder_claims("ex-Prime Minister Hamdok of Sudan spoke.") == []
    # a DIFFERENT office (vice/deputy) is not the officeholder claim
    assert extract_officeholder_claims(
        "Vice President Delcy Rodriguez of Venezuela chaired the session."
    ) == []
    # lowercase 'us' is an English word, not the US acronym
    assert extract_officeholder_claims("Tell us president material is scarce.") == []
    # role without a following capitalized name — no claim
    assert extract_officeholder_claims("the President of France arrived") == []
    assert extract_officeholder_claims("") == []


# --- the probe (stub conn) --------------------------------------------------


@pytest.mark.asyncio
async def test_vs_facts_flags_mismatch_with_distinct_reason():
    """The exact sweep shape: the facts table carries the (stale) seeded DRC PM;
    a finding names a different person in that office → flag, DISTINCT reason,
    hard_fail class, and the span says flag-only (never a correction)."""
    conn = _FactsConn([
        _office_row(
            "Democratic Republic of the Congo", "head of government",
            "Sylvestre Ilunga Ilunkamba",
        ),
    ])
    spans = await stale_leader_vs_facts_spans(
        conn, "Prime Minister Judith Suminwa Tuluka of the DRC visited Goma.",
    )
    assert len(spans) == 1
    assert spans[0].reason == "stale_leader_vs_facts"
    assert spans[0].as_dict()["fail_class"] == FAIL_CLASS_HARD
    assert "never auto-corrected" in spans[0].text
    assert conn.calls  # the facts table was actually probed


@pytest.mark.asyncio
async def test_vs_facts_no_flag_when_person_matches_current_holder():
    conn = _FactsConn([
        _office_row("Slovenia", "head of government", "Janez Janša"),
    ])
    # diacritic-folded surname match: prose 'Jansa' vs fact 'Janša'
    assert await stale_leader_vs_facts_spans(
        conn, "Slovenia's Prime Minister Jansa Cabinet survived the vote.",
    ) == []


@pytest.mark.asyncio
async def test_vs_facts_fails_open_without_a_current_office_fact():
    # No rows at all → nothing to reconcile → no flag.
    assert await stale_leader_vs_facts_spans(
        _FactsConn([]), "DRC Prime Minister Judith Suminwa Tuluka spoke.",
    ) == []
    # A current fact exists only for the OTHER office family member → the
    # claimed office has no basis row → fail-open, no flag.
    conn = _FactsConn([
        _office_row(
            "Democratic Republic of the Congo", "head of state",
            "Felix Tshisekedi",
        ),
    ])
    assert await stale_leader_vs_facts_spans(
        conn, "DRC Prime Minister Judith Suminwa Tuluka spoke.",
    ) == []


@pytest.mark.asyncio
async def test_vs_facts_role_confusion_is_not_a_mismatch():
    """Prose calling the head of STATE 'prime minister' matches a current
    family officeholder — role confusion, not a stale leader → no flag."""
    conn = _FactsConn([
        _office_row(
            "Democratic Republic of the Congo", "head of government",
            "Sylvestre Ilunga Ilunkamba",
        ),
        _office_row(
            "Democratic Republic of the Congo", "head of state",
            "Felix Tshisekedi",
        ),
    ])
    assert await stale_leader_vs_facts_spans(
        conn, "DRC Prime Minister Felix Tshisekedi addressed the nation.",
    ) == []


@pytest.mark.asyncio
async def test_vs_facts_leader_of_row_counts_as_current_holder():
    """A person-subject 'leader of' row is part of the family: matching it is
    consistent even when the country-subject office row names someone else
    (two seed producers can disagree; consistency with EITHER fails open)."""
    conn = _FactsConn([
        _office_row("Venezuela", "head of state", "Delcy Rodriguez"),
        {"subject": "Nicolas Maduro", "predicate": "leader of",
         "value": "Venezuela"},
    ])
    assert await stale_leader_vs_facts_spans(
        conn, "President Nicolas Maduro of Venezuela spoke.",
    ) == []


@pytest.mark.asyncio
async def test_vs_facts_read_failure_degrades_to_no_flag():
    assert await stale_leader_vs_facts_spans(
        _FailingConn(), "DRC Prime Minister Judith Suminwa Tuluka spoke.",
    ) == []


@pytest.mark.asyncio
async def test_vs_facts_no_claim_makes_no_query():
    conn = _FactsConn([])
    assert await stale_leader_vs_facts_spans(
        conn, "A quiet week; nothing about officeholders.",
    ) == []
    assert conn.calls == []


# --- integration through verify_finding_faithfulness ------------------------


@pytest.mark.asyncio
async def test_verify_folds_vs_facts_span_and_demotes():
    body = (
        "Prime Minister Judith Suminwa Tuluka of the DRC announced the "
        "reshuffle [1]."
    )
    citations = [{"marker": "[1]", "signal_id": "sig-1"}]
    conn = _FactsConn([
        _office_row(
            "Democratic Republic of the Congo", "head of government",
            "Sylvestre Ilunga Ilunkamba",
        ),
    ])
    report = await verify_finding_faithfulness(
        body=body, citations=citations, facts_conn=conn,
    )
    # 1 supported cited clause + 1 vs-facts guard span → 1/2 = 0.5.
    assert report.faithfulness_score == pytest.approx(0.5)
    assert any(
        s.reason == "stale_leader_vs_facts" for s in report.unsupported_spans
    )
    # DISTINCT from the heuristic reason — calibration can tell them apart.
    assert not any(s.reason == "stale_leader" for s in report.unsupported_spans)


@pytest.mark.asyncio
async def test_verify_without_facts_conn_never_probes():
    """Default facts_conn=None → byte-identical no-op for existing callers."""
    body = "Prime Minister Judith Suminwa Tuluka of the DRC announced it [1]."
    report = await verify_finding_faithfulness(
        body=body, citations=[{"marker": "[1]", "signal_id": "sig-1"}],
    )
    assert not any(
        s.reason == "stale_leader_vs_facts" for s in report.unsupported_spans
    )


# --- ephemeral DB — the probe SQL against the real facts schema -------------


@pytest.mark.asyncio
async def test_vs_facts_probe_live_schema_honors_supersession(migrated_pg):
    """Against the real ``facts`` table: only OPEN rows (superseded_by IS NULL
    AND valid_until IS NULL) are the reconciliation basis; a superseded prior
    holder neither flags a correct current claim nor shields a stale one."""
    import asyncpg as _asyncpg
    from uuid import uuid4 as _uuid4

    conn = await _asyncpg.connect(migrated_pg.dsn)
    try:
        current_id, prior_id = _uuid4(), _uuid4()
        await conn.execute(
            "INSERT INTO facts (id, subject, predicate, value, source_type, "
            "confidence, produced_at, valid_from) VALUES "
            "($1, 'Democratic Republic of the Congo', 'head of government', "
            "'Sylvestre Ilunga Ilunkamba', 'seed', 0.9, now(), now())",
            current_id,
        )
        # A CLOSED prior holder — must never be part of the basis.
        await conn.execute(
            "INSERT INTO facts (id, subject, predicate, value, source_type, "
            "confidence, produced_at, valid_from, valid_until, superseded_by) "
            "VALUES ($1, 'Democratic Republic of the Congo', "
            "'head of government', 'Bruno Tshibala', 'seed', 0.9, now(), "
            "now() - interval '2 years', now() - interval '1 year', $2)",
            prior_id, current_id,
        )
        # Naming the CLOSED prior holder as current PM → mismatch vs the OPEN row.
        spans = await stale_leader_vs_facts_spans(
            conn, "DRC Prime Minister Bruno Tshibala spoke in Kinshasa.",
        )
        assert len(spans) == 1
        assert spans[0].reason == "stale_leader_vs_facts"
        # Naming the OPEN row's holder → consistent, no flag.
        assert await stale_leader_vs_facts_spans(
            conn, "DRC Prime Minister Sylvestre Ilunga Ilunkamba spoke.",
        ) == []
    finally:
        await conn.execute(
            "DELETE FROM facts WHERE subject = 'Democratic Republic of the Congo'"
        )
        await conn.close()

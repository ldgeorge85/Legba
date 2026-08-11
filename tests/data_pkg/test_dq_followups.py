# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""DQ documented FOLLOW-UPS (FU1–FU6) — forward code fixes completing loose ends
from the shipped 7-phase data-quality program. Each fix EXTENDS an already-shipped
seam and must not regress it; the shipped-phase test files stay green alongside.

  * FU1 — verify._is_forward_looking: a present fact with a no-comma conditional
    tail whose verb is a present-tense EVENT verb outside the past-tense list is
    graded, not hidden (H1 residual).
  * FU2 — binding.escalation_gate_decision: a title-heuristic absence lead only
    gags a SUB-MODERATE finding, never a moderate+/high negation-framed event.
  * FU3 — office-keyed functional-role supersession (a re-seeded new office-holder
    closes the prior holder's open row, keyed on the country side).
  * FU4 — reifier sports gate over the UNION of all source-signal texts.
  * FU5 — fact_contention_arbiter role-keyed clustering + junk-gated surfacing +
    credibility-weighted quorum.
  * FU6 — world-composition stable situation_signature + live head fold.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig


# ===========================================================================
# FU1 — verify.py: shrink the forward-looking no-comma residual (P7 minor)
# ===========================================================================


def test_fu1_present_event_verb_conditional_tail_is_graded_not_hidden():
    """A present fact with a NO-COMMA conditional tail whose verb is a present-tense
    EVENT verb outside the past-tense list ('conducts', 'enriches') is NOT
    forward-looking: the floor counts it and the judge grades it (H1 residual)."""
    from legba.data.provenance.verify import (
        _is_fact_asserting,
        _is_forward_looking,
        _is_judgeable_claim,
    )

    for claim in (
        "Beijing conducts live-fire drills that would confirm intent",
        "Iran enriches uranium to 90% which would confirm breakout",
    ):
        low = claim.lower()
        assert _is_forward_looking(low) is False, claim
        # floor counts it (a checkable present fact) ...
        assert _is_fact_asserting(claim) is True, claim
        # ... and the judge grades it (never exempt).
        assert _is_judgeable_claim(claim) is True, claim


def test_fu1_pure_modal_prediction_stays_forward_looking_floor_exempt():
    """The -s ending is the disambiguator: a genuine modal prediction ('Iran WOULD
    enrich …', base stem 'enrich', not 'enriches') stays forward-looking and
    floor-exempt — the widened verb list must not swallow real predictions."""
    from legba.data.provenance.verify import _is_fact_asserting, _is_forward_looking

    pred = "iran would enrich uranium to 90% which would confirm breakout"
    assert _is_forward_looking(pred) is True
    assert _is_fact_asserting(pred) is False  # floor-exempt (pure prediction)


def test_fu1_noun_homograph_prediction_not_misgraded():
    """A prediction whose subject is a noun-homograph -s form deliberately EXCLUDED
    from the verb list ('air strikes would confirm …') stays forward-looking —
    the widening never adds ambiguous nouns-as-verbs."""
    from legba.data.provenance.verify import _is_forward_looking

    assert _is_forward_looking("air strikes would confirm the offensive") is True


def test_fu1_trimmed_noun_homograph_verbs_removed():
    """FU1 round 2 (nit) — the noun-homograph -s forms the exclusion policy meant
    to avoid ('halts'/'resumes'/'captures'/'annexes') are trimmed from
    _PRESENT_FACT_VERB_RE; the unambiguous present-tense EVENT verbs stay."""
    from legba.data.provenance.verify import _PRESENT_FACT_VERB_RE

    for noun_form in ("halts", "resumes", "captures", "annexes"):
        assert _PRESENT_FACT_VERB_RE.search(noun_form) is None, noun_form
    for verb in (
        "conducts", "enriches", "deploys", "seizes", "imposes",
        "withdraws", "invades", "expels", "ratifies", "mobilizes",
    ):
        assert _PRESENT_FACT_VERB_RE.search(verb) is not None, verb


def test_fu1_trimmed_homograph_prediction_stays_forward_looking():
    """A prediction whose subject is one of the newly-trimmed homographs ('captures
    would confirm the offensive') is no longer flipped to a present fact — it stays
    forward-looking and floor-exempt."""
    from legba.data.provenance.verify import _is_fact_asserting, _is_forward_looking

    pred = "further captures would confirm the offensive if the front collapses"
    assert _is_forward_looking(pred) is True
    assert _is_fact_asserting(pred) is False


# ===========================================================================
# FU2 — binding.py: absence-title suppression gated on sub-moderate severity
# ===========================================================================


def test_fu2_high_severity_negation_framed_event_pages():
    """A high-severity negation-framed EVENT ('No confirmed casualties as fighting
    intensifies') describes an ongoing situation and MUST page — a title heuristic
    never gags a moderate+/high finding."""
    from legba.data.analysts.agency.binding import (
        escalation_gate_decision,
        is_absence_or_negative_title,
    )

    title = "No confirmed casualties as fighting intensifies"
    # The pure title classifier still reads it as an absence LEAD ('No confirmed …').
    assert is_absence_or_negative_title(title) is True
    # But at high severity it is NOT suppressed — it pages.
    assert escalation_gate_decision(
        severity="high", confidence=0.9, title=title
    ) is True


def test_fu2_low_and_info_absence_verdicts_still_suppressed():
    """A genuinely low/info absence verdict is still title-gagged (the shipped
    P7-F4 win is preserved for sub-moderate severity)."""
    from legba.data.analysts.agency.binding import escalation_gate_decision

    assert escalation_gate_decision(
        severity="low", confidence=0.9,
        title="Argentina – Low leadership transition risk",
    ) is False
    assert escalation_gate_decision(
        severity="info", confidence=0.99,
        title="United States – No observable WMD proliferation activity",
    ) is False


def test_fu2_moderate_absence_lead_is_not_gagged():
    """The boundary: a MODERATE finding is at/above the gate, so a title heuristic
    no longer suppresses it (moderate is a legitimate page)."""
    from legba.data.analysts.agency.binding import escalation_gate_decision

    # moderate weight is 1.0, so 0.9 >= 0.85 gate ⇒ pages despite the absence title.
    assert escalation_gate_decision(
        severity="moderate", confidence=0.9,
        title="No material escalation observed",
    ) is True


# ===========================================================================
# Shared DB fixture (fresh migrated test DB — session-scoped)
# ===========================================================================


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


# ===========================================================================
# FU3 — seed-refresh office-keyed functional-role supersession (P5)
# ===========================================================================


def _seed_ctx():
    from legba.data.provenance import AnalystContext

    return AnalystContext(
        analyst_id="seed.wikidata_leaders",
        analyst_version="v1",
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


async def _write_leader_fact(pool, *, person: str, country: str, role: str):
    """Write one person-subject 'leader of <country>' seed fact (the shape the
    wikidata_leaders adapter emits) carrying its office role in ``data``."""
    from legba.data.provenance import FactPayload, write_fact

    async with pool.acquire() as conn:
        out, dlq = await write_fact(
            conn,
            analyst_ctx=_seed_ctx(),
            payload=FactPayload(
                subject=person,
                predicate="LeaderOf",  # normalize_predicate -> 'leader of'
                value=country,
                confidence=0.92,
                source_type="seed",
                valid_from=datetime.now(tz=timezone.utc),
                data={
                    "seed_adapter": "wikidata_leaders",
                    "relation": "leader_of",
                    "role": role,
                },
            ),
            derived_from=[],
            source_type="seed",
        )
        return out, dlq


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu3_reseeding_new_leader_closes_prior_office_holder(pg_pool):
    """A re-seed of a NEW office-holder closes the PRIOR holder's open 'leader of
    <country>' row — keyed on the COUNTRY (value) + office (role), regardless of
    the person subject (the P5 stale-leader class migration 0064 fixed by hand)."""
    country = f"Testlandia{uuid4().hex[:8]}"
    out_old, _ = await _write_leader_fact(
        pg_pool, person=f"OldLeader{uuid4().hex[:6]}", country=country,
        role="head_of_government",
    )
    assert out_old is not None
    out_new, _ = await _write_leader_fact(
        pg_pool, person=f"NewLeader{uuid4().hex[:6]}", country=country,
        role="head_of_government",
    )
    assert out_new is not None

    async with pg_pool.acquire() as conn:
        old_row = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out_old.id
        )
        new_row = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out_new.id
        )
    # Prior office-holder is CLOSED and pointed at the successor.
    assert old_row["valid_until"] is not None, "prior leader row must be closed"
    assert old_row["superseded_by"] == out_new.id
    # The new holder stays open (the one canonical current leader).
    assert new_row["valid_until"] is None and new_row["superseded_by"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu3_dual_office_country_not_collapsed(pg_pool):
    """A dual-office country (Iran supreme leader vs president — both 'leader of
    Iran' but DIFFERENT office roles) is NOT collapsed: the office key is
    role-aware, so neither co-leader closes the other."""
    country = f"Dualstan{uuid4().hex[:8]}"
    out_hog, _ = await _write_leader_fact(
        pg_pool, person=f"President{uuid4().hex[:6]}", country=country,
        role="head_of_government",
    )
    out_hos, _ = await _write_leader_fact(
        pg_pool, person=f"Supreme{uuid4().hex[:6]}", country=country,
        role="head_of_state",
    )
    async with pg_pool.acquire() as conn:
        hog = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out_hog.id
        )
        hos = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out_hos.id
        )
    assert hog["valid_until"] is None and hog["superseded_by"] is None
    assert hos["valid_until"] is None and hos["superseded_by"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu3_role_casing_drift_still_closes_prior_holder(pg_pool):
    """FU3 round 2 (minor) — the office key is matched CASE-INSENSITIVELY: a prior
    holder seeded with role 'President' is still closed by a re-seed carrying
    'president' (casing drift between adapter runs no longer skips the auto-close)."""
    country = f"Caselandia{uuid4().hex[:8]}"
    out_old, _ = await _write_leader_fact(
        pg_pool, person=f"OldLeader{uuid4().hex[:6]}", country=country,
        role="President",
    )
    assert out_old is not None
    out_new, _ = await _write_leader_fact(
        pg_pool, person=f"NewLeader{uuid4().hex[:6]}", country=country,
        role="president",  # same office, different casing
    )
    assert out_new is not None

    async with pg_pool.acquire() as conn:
        old_row = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out_old.id
        )
        new_row = await conn.fetchrow(
            "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out_new.id
        )
    assert old_row["valid_until"] is not None, "casing drift must not skip the close"
    assert old_row["superseded_by"] == out_new.id
    assert new_row["valid_until"] is None and new_row["superseded_by"] is None


# ===========================================================================
# FU4 — reifier sports gate over the UNION of source-signal texts (P5)
# ===========================================================================


def test_fu4_sports_frame_in_union_downgrades_hostile():
    """A hostile typing whose sports frame ('World Cup') is in a CO-SOURCE signal,
    NOT the excerpt, is downgraded to a neutral co-occurrence once the gate runs
    over the UNION of the excerpt + all source-signal texts."""
    from legba.data.analysts.relationship_reifier import (
        _coerce_typing,
        _sports_gate_text,
    )

    cand = {
        "evidence_text": "Spain and Morocco met on Tuesday",  # no sports frame
        "source_signal_text": "World Cup last-16: Spain face Morocco in a knockout tie",
    }
    gate = _sports_gate_text(cand)
    payload = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Spain",
         "object": "Morocco", "intent": "hostile"},
        fallback_subject="Spain", fallback_object="Morocco",
        evidence_text=gate,
    )
    assert payload is not None
    assert payload.polarity == 0, "a fixture must be downgraded to co-occurrence"
    assert payload.intent == "neutral"


def test_fu4_real_conflict_dyad_not_downgraded_by_union():
    """A genuine interstate hostility whose co-source carries dual-use 'clashes'
    with NO sports anchor stays HOSTILE — the union never mis-gates a real war."""
    from legba.data.analysts.relationship_reifier import (
        _coerce_typing,
        _sports_gate_text,
    )

    cand = {
        "evidence_text": "Russian forces shelled Ukrainian positions",
        "source_signal_text": "225 clashes reported along the front line near Kharkiv",
    }
    gate = _sports_gate_text(cand)
    payload = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Russia",
         "object": "Ukraine", "intent": "hostile"},
        fallback_subject="Russia", fallback_object="Ukraine",
        evidence_text=gate,
    )
    assert payload is not None
    assert payload.polarity == -1, "a real conflict dyad must NOT be downgraded"
    assert payload.intent == "hostile"


def test_fu4_real_hostility_with_stray_sports_signal_not_downgraded():
    """FU4 round 2 (BLOCKING) — a genuinely HOSTILE dyad whose lineage union
    contains BOTH real conflict signals AND a stray UNAMBIGUOUS sports signal
    ('World Cup') stays HOSTILE: the conflict/casualty vocab in the union BLOCKS
    the sports downgrade (a real Gaza/Israel dyad is never erased because it once
    co-occurred with a sports fixture in the same source pool)."""
    from legba.data.analysts.relationship_reifier import (
        _coerce_typing,
        _has_conflict_context,
        _is_sports_context,
        _sports_gate_text,
    )

    cand = {
        "evidence_text": "Israeli forces launched airstrikes on Gaza killing dozens",
        # a stray World-Cup signal shares the source pool with the strike report
        "source_signal_text": (
            "World Cup 2026 qualifiers draw announced; "
            "Israel bombards Gaza as casualties mount along the front line"
        ),
    }
    gate = _sports_gate_text(cand)
    # the union DOES read as a sports frame (unambiguous 'World Cup') ...
    assert _is_sports_context(gate) is True
    # ... but it ALSO carries conflict/casualty vocab, so the downgrade is blocked.
    assert _has_conflict_context(gate) is True
    payload = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "Israel",
         "object": "Gaza", "intent": "hostile"},
        fallback_subject="Israel", fallback_object="Gaza",
        evidence_text=gate,
    )
    assert payload is not None
    assert payload.polarity == -1, "a real hostility must NOT be downgraded"
    assert payload.intent == "hostile"


def test_fu4_pure_worldcup_fixture_still_downgraded():
    """FU4 round 2 — the round-1 gain is preserved: a PURE World-Cup fixture
    (DR Congo face England, no conflict vocab anywhere in the union) is STILL
    downgraded to a neutral co-occurrence."""
    from legba.data.analysts.relationship_reifier import (
        _coerce_typing,
        _has_conflict_context,
        _is_sports_context,
        _sports_gate_text,
    )

    cand = {
        "evidence_text": "DR Congo and England were drawn together on Tuesday",
        "source_signal_text": (
            "World Cup last-16: DR Congo face England with nothing to lose in the tie"
        ),
    }
    gate = _sports_gate_text(cand)
    assert _is_sports_context(gate) is True
    assert _has_conflict_context(gate) is False  # no conflict vocab -> gate fires
    payload = _coerce_typing(
        {"related": True, "rel_type": "HostileTo", "subject": "DR Congo",
         "object": "England", "intent": "hostile"},
        fallback_subject="DR Congo", fallback_object="England",
        evidence_text=gate,
    )
    assert payload is not None
    assert payload.polarity == 0, "a pure sports fixture must be downgraded"
    assert payload.intent == "neutral"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu4_read_candidates_unions_cosource_signal_text(pg_pool):
    """The candidate read joins the edge's derived_from lineage → signals and
    surfaces the UNION of their title+summary, so the gate sees a sports frame
    that lives ONLY in a co-source signal (not the excerpt)."""
    import json

    from legba.data.analysts.relationship_reifier import (
        _is_sports_context,
        _read_candidates,
        _sports_gate_text,
    )

    src = f"SpainFU4{uuid4().hex[:6]}"
    tgt = f"MoroccoFU4{uuid4().hex[:6]}"
    # K-G2 put a qualification bar on selection: THREE independent publishers is
    # what carries this pair over it (a single-sourced co-mention scores 0.0 and
    # is correctly invisible). The sports frame lives in ONE of them, which is
    # exactly the condition this test exists to check.
    tag = uuid4().hex[:6]
    sigs = []
    async with pg_pool.acquire() as conn:
        for i in range(3):
            sid = uuid4()
            await conn.execute(
                "INSERT INTO signals (id, source_id, owner_tenant, modality, "
                "payload, content_hash, fetched_at) "
                "VALUES ($1,$2,'default','text',$3::jsonb,$4, now())",
                sid,
                f"source.pub{tag}{i}.feed",
                json.dumps({
                    "title": (
                        f"World Cup last-16: {src} face {tgt}" if i == 0
                        else f"Report {i}: {src} and {tgt}"
                    ),
                    "summary": (
                        "A knockout fixture at the tournament." if i == 0 else ""
                    ),
                }),
                f"hash-{sid}",
            )
            sigs.append(sid)
        await conn.execute(
            "INSERT INTO proposed_edges (source_entity, target_entity, "
            "relationship_type, confidence, evidence_text, status, derived_from) "
            "VALUES ($1,$2,'co_occurs',0.7,$3,'pending',$4::uuid[])",
            src, tgt, f"{src} and {tgt} met on Tuesday", sigs,
        )
        cands = await _read_candidates(conn, limit=50)

    cand = next((c for c in cands if c["source_entity"] == src), None)
    assert cand is not None, "the seeded edge must be a candidate"
    # The co-source signal's sports frame is unioned in ...
    assert "World Cup" in (cand["source_signal_text"] or "")
    # ... the excerpt ALONE has no sports frame, but the UNION does.
    assert _is_sports_context(str(cand["evidence_text"])) is False
    assert _is_sports_context(_sports_gate_text(cand)) is True


# ===========================================================================
# FU5 — fact_contention_arbiter role-keyed clustering + junk-gated surfacing +
# credibility-weighted quorum (P5, detect-only)
# ===========================================================================

_ARB_NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


def _arb_agg(value_key, *, distinct, cred, conf_mean, rep=None):
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    a = arb._ValueAgg(value_key)
    a.representative_fact_id = uuid4()
    a.representative_value = rep if rep is not None else value_key
    a.distinct_lineage = {f"src{i}" for i in range(distinct)}
    a.cred_sum = cred
    a.confidence_sum = conf_mean
    a.row_count = 1
    a.confidence_max = conf_mean
    a.latest_asserted_at = _ARB_NOW
    a.supporting_fact_ids = [a.representative_fact_id]
    return a


def _leader_row(person, country, role, *, cred=0.9, days=0):
    return {
        "id": uuid4(), "subject": person, "predicate": "leader of",
        "value": country, "confidence": 0.9, "source_type": "seed",
        "source_credibility": cred, "role_key": role,
        "produced_at": _ARB_NOW - timedelta(days=days), "derived_from": [uuid4()],
    }


def test_fu5a_leader_of_rekeyed_clusters_cross_person():
    """Two DISTINCT people both 'leader of United States' of the SAME office —
    re-keyed on (country, office), they cluster into ONE dispute the normal
    (subject, predicate) grouping could never see."""
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    rows = [
        _leader_row("Joe Biden", "United States", "head_of_government", days=30),
        _leader_row("Donald Trump", "United States", "head_of_government", days=0),
    ]
    buckets = arb._bucket_rows([arb._rekey_role_row(r) for r in rows])
    assert len(buckets) == 1, "one (country, office) group"
    (_skey, _pkey), grp = next(iter(buckets.items()))
    non_junk, junk = arb._aggregate_group(grp)
    assert len(non_junk) == 2, "Biden vs Trump = a genuine two-value dispute"
    assert {a.representative_value for a in non_junk} == {"Joe Biden", "Donald Trump"}


def test_fu5a_dual_office_country_stays_separate_groups():
    """A dual-office country (Iran supreme leader vs president — both 'leader of
    Iran' but DIFFERENT offices) is NOT a contradiction: role-aware re-keying keeps
    them in SEPARATE single-holder groups."""
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    rows = [
        _leader_row("Ali Khamenei", "Iran", "head_of_state"),
        _leader_row("Masoud Pezeshkian", "Iran", "head_of_government"),
    ]
    buckets = arb._bucket_rows([arb._rekey_role_row(r) for r in rows])
    assert len(buckets) == 2, "two offices -> two distinct groups"
    for _key, grp in buckets.items():
        non_junk, _ = arb._aggregate_group(grp)
        assert len(non_junk) == 1, "one holder per office -> no dispute"


def test_fu5b_junk_value_never_surfaced():
    """A value that clears the score gates but is a possessive-fragment / byline is
    NEVER surfaced as the winner — the arbiter abstains instead."""
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    for junk_rep in ("Donald Trump 's", "Reuters", "the"):
        winner = _arb_agg("w", distinct=5, cred=3.0, conf_mean=0.9, rep=junk_rep)
        loser = _arb_agg("y", distinct=1, cred=0.3, conf_mean=0.5, rep="Jane Doe")
        aggs = [winner, loser]
        scores = arb._score_group(aggs, _ARB_NOW)
        assert arb._select_winner(aggs, scores) is None, junk_rep


def test_fu5b_clean_value_still_surfaces():
    """A clean, dominating value still surfaces (the junk gate never blocks a real
    winner)."""
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    winner = _arb_agg("de-escalating", distinct=5, cred=3.0, conf_mean=0.9)
    loser = _arb_agg("clashes ongoing", distinct=1, cred=0.3, conf_mean=0.5)
    aggs = [winner, loser]
    scores = arb._score_group(aggs, _ARB_NOW)
    assert arb._select_winner(aggs, scores) is winner


def test_fu5c_quorum_weighted_by_credibility_not_raw_count():
    """The quorum vote is weighted by source credibility: ten low-credibility
    syndicated copies no longer out-count one authoritative source on raw volume;
    an unknown-credibility source votes at the bounded machine nominal."""
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    synd = arb._ValueAgg("syndicated")
    for _ in range(10):
        synd.add({
            "id": uuid4(), "value": "syndicated", "source_type": "ingestion",
            "source_credibility": 0.2, "produced_at": _ARB_NOW,
            "derived_from": [uuid4()], "confidence": 0.6,
        })
    authoritative = arb._ValueAgg("authoritative")
    authoritative.add({
        "id": uuid4(), "value": "authoritative", "source_type": "seed",
        "source_credibility": 0.95, "produced_at": _ARB_NOW,
        "derived_from": [uuid4()], "confidence": 0.9,
    })
    # Raw distinct count says syndication wins 10:1 ...
    assert synd.distinct_source_count == 10
    assert authoritative.distinct_source_count == 1
    # ... but the credibility-weighted vote bounds each source by its credibility.
    assert synd.credibility_weighted_source_count == pytest.approx(2.0)
    assert authoritative.credibility_weighted_source_count == pytest.approx(0.95)

    unknown = arb._ValueAgg("unknown")
    unknown.add({
        "id": uuid4(), "value": "unknown", "source_type": "ingestion",
        "source_credibility": None, "produced_at": _ARB_NOW,
        "derived_from": [uuid4()], "confidence": 0.6,
    })
    assert unknown.credibility_weighted_source_count == pytest.approx(
        arb._UNKNOWN_SOURCE_CRED
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu5a_run_arbiter_opens_cross_person_leader_dispute(pg_pool):
    """End-to-end: two open 'leader of <country>' facts (different persons, same
    office) — directly inserted to simulate an unresolved dispute — cause a full
    arbiter pass to OPEN a contention group keyed on the country side (role-keyed
    fetch + rekey + detect-only surfacing)."""
    from legba.data.analysts.deterministic_handlers import fact_contention_arbiter as arb

    country = f"Arbstan{uuid4().hex[:8]}"
    p1, p2 = f"LeaderA{uuid4().hex[:6]}", f"LeaderB{uuid4().hex[:6]}"
    try:
        async with pg_pool.acquire() as conn:
            for person, cred, days in ((p1, 0.9, 30), (p2, 0.9, 0)):
                await conn.execute(
                    "INSERT INTO facts (id, subject, predicate, value, source_type, "
                    "source_credibility, confidence, produced_at, valid_from, data, "
                    "derived_from) VALUES ($1,$2,'leader of',$3,'seed',$4,0.9, now(), "
                    "now() - ($5||' days')::interval, "
                    "'{\"role\":\"head_of_government\"}'::jsonb, $6::uuid[])",
                    uuid4(), person, country, cred, str(days), [uuid4()],
                )
            counts = await arb._run_arbiter(pg_pool)
            # A contention group keyed on the COUNTRY side (bracketed with the
            # office) opened for this dispute.
            grp = await conn.fetchrow(
                "SELECT subject_key, predicate_key, value_count FROM fact_contention "
                "WHERE subject_key LIKE $1 AND status <> 'collapsed'",
                f"{country.lower()}%",
            )
            # Both facts got the detect-only contested marker.
            marked = await conn.fetchval(
                "SELECT count(*) FROM facts WHERE lower(value)=lower($1) AND contested",
                country,
            )
    finally:
        # CLOSE the fixture's disputed pair (own rows only; close, never
        # delete — the contention group's supporting_fact_ids still point
        # here). Left OPEN, these two rows are a standing same-office
        # two-holder dispute in the SHARED seed slice, and they are the pair
        # the 2026-08-09 shuffled nightly (seed 277595060) collapsed inside
        # test_substrate_export_import's round trip: the whole-slice export
        # carried both, and the re-home's office-keyed supersession closed
        # them against each other (2 closed, 1 reopened — of1 == of0 failed
        # 48 != 49). File order never showed it because
        # test_fact_contention_surfacing_db's own fixture wipe used to level
        # the facts table in between — masking, not hygiene.
        async with pg_pool.acquire() as conn:
            await conn.execute(
                "UPDATE facts SET valid_until = now(), updated_at = now() "
                "WHERE lower(value) = lower($1) AND predicate = 'leader of' "
                "AND valid_until IS NULL",
                country,
            )
    assert counts["groups_open"] >= 1
    assert grp is not None, "a country-keyed leader contention group must open"
    assert grp["value_count"] == 2
    assert marked == 2, "both disputed leader facts marked contested (detect-only)"


# ===========================================================================
# FU6 — world-composition stable situation_signature + live head fold (P6)
# ===========================================================================


def test_fu6_world_composition_signature_is_stable_and_nonempty():
    """Two successive world runs derive the IDENTICAL, non-empty signature (the
    supersession key), so heads cluster instead of forking."""
    from legba.data.analysts.meta_findings_synthesizer import _composition_signature

    sig_a = _composition_signature("world_assessor", None)
    sig_b = _composition_signature("world_assessor", None)
    assert sig_a == sig_b == "composition:world_assessor:world"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu6_later_world_head_supersedes_earlier_via_fold(pg_pool):
    """Two successive world composition heads (same stable signature, EMPTY column
    at write time — the live symptom) fold cleanly: the later head stamps the
    canonical 'sit:' column signature and supersedes the earlier, with a mirrored
    audit edge."""
    from legba.data.analysts.deterministic_handlers.finding_supersession import (
        fold_prior_composition_heads,
    )
    from legba.data.analysts.meta_findings_synthesizer import _composition_signature
    from legba.data.provenance import AnalystContext, FindingPayload
    from legba.data.provenance.kinds import OutputKind
    from legba.data.provenance.writes import write_analyst_output

    sig = _composition_signature("world_assessor", None)
    aid = f"world_assessor_fu6_{uuid4().hex[:8]}"  # isolate from any live rows

    async def _write_world_head(conn):
        ctx = AnalystContext(
            analyst_id=aid, analyst_version="v1", run_id=uuid4(),
            target_id=None, target_version=None,
        )
        payload = FindingPayload(
            title="World read", body="body", confidence=0.7,
            data={"situation_signature": sig, "meta": True},
        )
        out, _dlq = await write_analyst_output(
            conn, analyst_ctx=ctx, kind=OutputKind.FINDING,
            output_payload=payload, derived_from=[],
        )
        return out

    async with pg_pool.acquire() as conn:
        head1 = await _write_world_head(conn)
        head2 = await _write_world_head(conn)
        # The write path leaves the column NULL (the live world-head symptom) ...
        col1 = await conn.fetchval(
            "SELECT situation_signature FROM analyst_outputs WHERE id=$1", head1.id
        )
        assert col1 is None, "the write path leaves the column empty (repro)"
        # ... the fold on the LATER head stamps the column + supersedes the earlier.
        closed = await fold_prior_composition_heads(
            conn, analyst_id=aid, raw_signature=sig, new_head_id=head2.id,
        )
        first = await conn.fetchrow(
            "SELECT superseded_by, situation_signature FROM analyst_outputs WHERE id=$1",
            head1.id,
        )
        second = await conn.fetchrow(
            "SELECT superseded_by, situation_signature FROM analyst_outputs WHERE id=$1",
            head2.id,
        )
        edge = await conn.fetchval(
            "SELECT count(*) FROM finding_supersessions "
            "WHERE superseded_finding_id=$1 AND superseding_finding_id=$2",
            head1.id, head2.id,
        )
    assert closed == 1
    assert first["superseded_by"] == head2.id, "earlier head superseded by later"
    assert first["situation_signature"] == f"sit:{sig}"
    assert second["superseded_by"] is None, "the later head is the one live head"
    assert second["situation_signature"] == f"sit:{sig}"
    assert edge == 1, "a mirrored finding_supersessions audit edge is written"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_fu6_fold_is_idempotent_and_ignores_noncomposition(pg_pool):
    """The fold is idempotent (re-run closes nothing new) and never touches a
    first-order (non-composition) finding signature."""
    from legba.data.analysts.deterministic_handlers.finding_supersession import (
        fold_prior_composition_heads,
    )

    # A non-composition signature is ignored outright (defensive guard).
    async with pg_pool.acquire() as conn:
        assert await fold_prior_composition_heads(
            conn, analyst_id="whatever", raw_signature="sig:some_topic",
            new_head_id=uuid4(),
        ) == 0

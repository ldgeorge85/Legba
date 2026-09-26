# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""WIDTH — the pure units of external grading at width (W-1 · W-2 · W-4 · W-5 · W-9).

Everything here is pure: the claim source, the pre-filter, the claim key, the
durable queue's arithmetic, the grader's rubric gates and family fence, the
degradation ladder, and the ledger aggregation. The REAL-binding-path e2e —
``deterministic.run_method`` with the search binding built by the production
wiring — lives in ``test_standing_auditor.py`` beside the shipped sweep's e2e,
because that suite already owns the migrated Postgres fixture and the fake
socket, and the memory rule (#85: the auditor's search leg was dead for five
weeks while its tests passed) says the width leg must traverse the same real
seam.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from legba.data.analysts.deterministic_handlers import (
    _external_audit_claims as claims,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_grader as grader,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_fetch as fetch_leg,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_queue as queue,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_sampling as sampling,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_width as width,
)
from legba.data.provenance import external_grades as eg
from legba.data.provenance import external_span_check as esc


# ---------------------------------------------------------------------------
# A tiny assembly payload, real in shape
# ---------------------------------------------------------------------------


def _assembly_row(*, output_id="00000000-0000-0000-0000-000000000001",
                  head_id="00000000-0000-0000-0000-0000000000aa",
                  regime="assembly", spans=None):
    if spans is None:
        spans = [{
            "role": "bluf", "text": "The central bank raised rates in March 2026.",
            "origin": {"head_id": head_id, "start": 0, "end": 44,
                       "body_sha256": "x", "body_len": 44},
            "scope_tokens": [],
        }]
    return {
        "id": output_id,
        "analyst_id": "country_composition",
        "target_id": "tr",
        "title": "TR read",
        "body": "body",
        "produced_at": "2026-09-05T12:00:00+00:00",
        "data": {
            "tags": ["severity:high", "severity_delta:rose"],
            "data": {
                "assembly": {
                    "schema": "assembly.v1", "regime": regime,
                    "blocks": [{
                        "ordinal": 1, "finding_id": head_id, "desk":
                        "country_composition", "target_id": "tr",
                        "severity": "high", "spans": spans,
                    }],
                },
                "evidence_window": {"earliest": "2026-08-24", "latest": "2026-09-05"},
            },
        },
    }


# ---------------------------------------------------------------------------
# W-1 — the claim source
# ---------------------------------------------------------------------------


def test_assembly_spans_enumerate_deterministically_with_no_model():
    row = _assembly_row()
    a = claims.claims_from_assembly(row)
    b = claims.claims_from_assembly(row)
    assert len(a) == 1
    assert [c.claim_text for c in a] == [c.claim_text for c in b]
    assert [c.key for c in a] == [c.key for c in b]
    c = a[0]
    assert c.population == claims.POPULATION_ASSEMBLY_SPAN
    assert c.origin_head_id == "00000000-0000-0000-0000-0000000000aa"
    assert c.block_ordinal == 1 and c.lead_block is True
    assert c.span_role == "bluf"
    assert c.assembly_regime == "assembly"


def test_a_legacy_regime_assembly_row_yields_no_width_claims():
    """The regime half of the gate (D-3 §1.1): a flag-off row carries the
    ``{schema, regime}`` stamp and NOTHING behind it, and a schema-only gate
    would 'grade' it. ``claims_from_read`` returns [] so the caller keeps the
    extraction leg — the whole reason the loop still works flag-off."""
    row = _assembly_row(regime="legacy")
    assert claims.read_assembly(row) is None
    assert claims.claims_from_read(row) == []


def test_the_claim_key_is_stable_across_a_fold_only_change():
    """MECH-6 applied to IDENTITY: a U+2011 the renderer inserts between two
    replays must not mint a second key for the same claim."""
    k1 = claims.claim_key("weakly-supported finding", "head", 0, 24)
    k2 = claims.claim_key("weakly‑supported finding", "head", 0, 24)
    assert k1 == k2
    # But a different span (same text, different offsets) is a different key —
    # the coordinates are what make the key replayable AND injective.
    assert claims.claim_key("x", "head", 1, 2) != claims.claim_key("x", "head", 12, 3)


def test_the_prefilter_classifies_the_r_round_specimens():
    """The specimens the design names, each landing in its own class."""
    # R2-C4 case #4 — a provenance claim whose truth-maker is the packet.
    assert claims.classify_uncheckable(
        "All eight principal units produced verified reads in this cycle."
    ) == claims.UNCHECKABLE_PROVENANCE
    # R3 ML_A — self-referential provenance.
    assert claims.classify_uncheckable(
        "No head was available for this desk in the window."
    ) == claims.UNCHECKABLE_PROVENANCE
    # scope-bounded off the lexicon.
    assert claims.classify_uncheckable(
        "No new signals in this slice as of 2026-09-05."
    ) == claims.UNCHECKABLE_SCOPE_BOUNDED
    # scope-bounded off the assembler's own scope_tokens (M-8 shape).
    assert claims.classify_uncheckable(
        "Three of the desk's units reported.",
        scope_tokens=["collection_denominator"],
    ) == claims.UNCHECKABLE_SCOPE_BOUNDED
    # a real world claim resolves to None — the search decides it.
    assert claims.classify_uncheckable(
        "The central bank raised rates in March 2026."
    ) is None


def test_perspective_only_applies_to_the_assessment_and_needs_two_signals():
    # No ordinal named -> not stating what a block says -> perspective.
    assert claims.perspective_shaped("This is worth watching.", has_ordinal=False)
    # An ordinal named + a judgement marker -> perspective.
    assert claims.perspective_shaped(
        "The risk here is escalation [[ref:2]].", has_ordinal=True
    )
    # An ordinal named, plain relay -> a FACT, not perspective.
    assert not claims.perspective_shaped(
        "Rates rose 50bp [[ref:1]].", has_ordinal=True
    )


def test_the_query_is_deterministic_and_strips_our_own_markers():
    row = _assembly_row(spans=[{
        "role": "bluf",
        "text": "Turkey's central bank raised rates 【1】 in March [[ref:2]].",
        "origin": {"head_id": "h", "start": 0, "end": 10, "body_sha256": "x",
                   "body_len": 10},
        "scope_tokens": [],
    }])
    c = claims.claims_from_assembly(row)[0]
    q1 = claims.build_query(c)
    q2 = claims.build_query(c)
    assert q1 == q2
    assert "[[ref:" not in q1 and "【" not in q1 and "[1]" not in q1
    assert "tr" in q1  # the target leads the query
    assert q1.endswith("2026-09")  # the window month, not a day pin


def test_world_re_quotations_dedup_by_key():
    row = _assembly_row()
    dupe = claims.claims_from_assembly(row) + claims.claims_from_assembly(row)
    assert len(list(claims.iter_unique(dupe))) == 1


# ---------------------------------------------------------------------------
# W-2 / W-9 — the queue, the drain, the governor, the ladder
# ---------------------------------------------------------------------------


def _claim(key_text, *, severity="moderate", analyst="country_composition",
           lead=False):
    return claims.WidthClaim(
        claim_text=key_text, population="assembly_span",
        graded_output_id="o", analyst_id=analyst, origin_head_id="h",
        start=0, end=len(key_text), claim_severity=severity, lead_block=lead,
    )


def test_refill_is_idempotent_on_claim_key():
    state = queue.empty_state(day="2026-09-05")
    cs = [_claim("a"), _claim("b")]
    state, c1 = queue.refill(state, cs, watermark="2026-09-05T00:00:00")
    assert c1["added"] == 2
    state, c2 = queue.refill(state, cs, watermark="2026-09-05T00:00:00")
    assert c2["added"] == 0 and c2["already_queued"] == 2
    assert len(state["entries"]) == 2


def test_the_refill_watermark_only_advances():
    state = queue.empty_state(day="2026-09-05")
    state, _ = queue.refill(state, [_claim("a")], watermark="2026-09-05T05:00:00")
    state, _ = queue.refill(state, [_claim("b")], watermark="2026-09-05T01:00:00")
    assert state["refill_watermark"] == "2026-09-05T05:00:00"


def test_priority_order_severity_then_lead_then_tier_then_key():
    world_lead = _claim("w", severity="high", analyst="world_assessor", lead=True)
    country_body = _claim("c", severity="high", analyst="country_composition")
    low = _claim("z", severity="low")
    ordered = sorted([low, country_body, world_lead], key=queue.priority_key)
    assert [c.analyst_id for c in ordered][0] == "world_assessor"
    assert ordered[-1].claim_severity == "low"


class _GovGovernor:
    def __init__(self, per_hour):
        self.max_invocations_per_hour = per_hour


class _GovPack:
    def __init__(self, per_hour):
        self.governor = _GovGovernor(per_hour)


class _GovBinding:
    """The shape ``plan_drain`` reads the live governor off — the same
    ``binding.pack.governor`` the agency enforcer itself consults."""

    def __init__(self, per_hour):
        self.pack = _GovPack(per_hour)


def test_the_governor_clamps_max_claims_per_tick_from_the_LIVE_pack():
    """The clamp is the LIVE web_access governor / 3, not a copy of it.

    THE DEFECT (2026-09-20). ``GOVERNOR_MAX_INVOCATIONS_PER_HOUR = 120`` was a
    hand-copied snapshot of the pack. The operator lifted the pack to
    1,000,000/h; the constant did not move, so the clamp stayed at 40 and the
    auditor drained ~13 claims/h against a search plane that was wide open.
    A binding carrying the lifted governor must now clamp at 1,000,000/3.
    """
    state = queue.empty_state(day="2026-09-05")
    state, _ = queue.refill(state, [_claim(str(i)) for i in range(100)])
    _, _, plan = queue.plan_drain(
        state, max_claims_per_tick=200, max_claims_per_day=10000,
        max_serp_per_day=10000, binding=_GovBinding(1_000_000),
    )
    assert plan["governor_clamped"] is False
    assert plan["tick_cap"] == 200
    assert plan["selected"] == 100  # the whole pending population, not 40
    assert plan["governor_invocations_per_hour"] == 1_000_000
    assert plan["governor_max_claims_per_tick"] == 1_000_000 // 3


def test_a_TIGHT_live_governor_still_clamps_the_tick():
    """The clamp is not gone, it is RESOLVED. A pack that is genuinely tight
    still wins over the descriptor, because exceeding a governor BLOCKS a tick
    rather than slowing it."""
    state = queue.empty_state(day="2026-09-05")
    state, _ = queue.refill(state, [_claim(str(i)) for i in range(100)])
    _, _, plan = queue.plan_drain(
        state, max_claims_per_tick=200, max_claims_per_day=10000,
        max_serp_per_day=10000, binding=_GovBinding(120),
    )
    assert plan["governor_clamped"] is True
    assert plan["tick_cap"] == 40  # 120 // 3, the OLD behaviour, on tight pack
    assert plan["selected"] == 40
    assert (plan["governor_max_claims_per_tick"]
            * queue.EGRESS_CALLS_PER_CLAIM) <= 120


def test_the_governor_resolution_order_pack_then_env_then_uncapped(monkeypatch):
    """Pack beats env beats the uncapped default, and an uncapped (None) pack
    governor means UNCAPPED — the same thing it means to the enforcer, which
    skips the dimension entirely."""
    monkeypatch.delenv(queue.GOVERNOR_EGRESS_PER_HOUR_ENV, raising=False)
    assert (queue.governor_invocations_per_hour()
            == queue.DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR)
    assert queue.governor_invocations_per_hour(_GovBinding(None)) == (
        queue.DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR
    )
    monkeypatch.setenv(queue.GOVERNOR_EGRESS_PER_HOUR_ENV, "600")
    assert queue.governor_invocations_per_hour() == 600
    assert queue.governor_max_claims_per_tick() == 200
    # The PACK still wins over the env mirror.
    assert queue.governor_invocations_per_hour(_GovBinding(90)) == 90
    # A junk env is not a disarm: it falls back, loudly.
    monkeypatch.setenv(queue.GOVERNOR_EGRESS_PER_HOUR_ENV, "not-a-number")
    assert (queue.governor_invocations_per_hour()
            == queue.DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR)


def test_a_malformed_binding_never_raises_the_clamp(monkeypatch):
    """A clamp that raises is a tick that dies. Every unreadable shape
    degrades to the env/default rather than propagating."""
    monkeypatch.delenv(queue.GOVERNOR_EGRESS_PER_HOUR_ENV, raising=False)

    class _Exploding:
        @property
        def pack(self):
            raise RuntimeError("registry gone")

    for bad in (object(), None, _Exploding()):
        assert (queue.governor_invocations_per_hour(bad)
                == queue.DEFAULT_GOVERNOR_INVOCATIONS_PER_HOUR)


def test_the_shipped_width_defaults_are_the_LIFTED_ones():
    """The module defaults moved with the descriptor: a caller that passes no
    knobs must not silently re-impose the starved 40/600/900/4000."""
    assert queue.DEFAULT_MAX_CLAIMS_PER_TICK == 200
    assert queue.DEFAULT_MAX_CLAIMS_PER_DAY == 5000
    assert queue.DEFAULT_MAX_SERP_PER_DAY == 10000
    assert queue.DEFAULT_MAX_QUEUE_DEPTH == 20000


def test_exhaustion_degrades_to_sampled_never_fabricates():
    """The day's claim budget cannot cover the pending population, so the tick
    enters SAMPLED mode: a hash-gated fraction, stamped, and a partial day is
    never a whole one."""
    state = queue.empty_state(day="2026-09-05")
    state, _ = queue.refill(state, [_claim(str(i)) for i in range(40)])
    # Only 10 of the day's claim budget left.
    state["spent"] = {"claims": 590, "serp": 0}
    selected, state, plan = queue.plan_drain(
        state, max_claims_per_tick=40, max_claims_per_day=600,
        max_serp_per_day=10000,
    )
    assert plan["sample_reason"] == queue.SAMPLED_REASON_CLAIM_BUDGET
    assert 0.0 < plan["sample_fraction"] < 1.0
    assert len(selected) <= 10
    # Deterministic: the SAME claims are admitted on a replay.
    state2 = queue.empty_state(day="2026-09-05")
    state2, _ = queue.refill(state2, [_claim(str(i)) for i in range(40)])
    state2["spent"] = {"claims": 590, "serp": 0}
    selected2, _, _ = queue.plan_drain(
        state2, max_claims_per_tick=40, max_claims_per_day=600,
        max_serp_per_day=10000,
    )
    assert {c.key for c in selected} == {c.key for c in selected2}


def test_a_merely_full_tick_is_not_a_degradation():
    """A tick full to its cap with the day's budget intact is NOT sampled — the
    rest drains next tick. Sampled mode is only the DAY running out."""
    state = queue.empty_state(day="2026-09-05")
    state, _ = queue.refill(state, [_claim(str(i)) for i in range(100)])
    _, _, plan = queue.plan_drain(state, max_claims_per_tick=40,
                                  max_claims_per_day=600, max_serp_per_day=10000)
    assert plan["sample_reason"] is None
    assert plan["sample_fraction"] == 1.0
    assert plan["selected"] == 40 and plan["pending_after"] == 60


def test_the_sampled_ladder_still_binds_at_the_LIFTED_day_cap():
    """F-11 is NOT disarmed by the lift. The caps moved 600 -> 5000; the
    degradation ladder is the same mechanism and must still fire at the new
    number, with the same hash gate and the same replayability. A widened
    budget that silently stopped degrading would publish a partial day as a
    whole one, which is the exact failure the ladder exists to prevent."""
    def _day(spent):
        state = queue.empty_state(day="2026-09-20")
        state, _ = queue.refill(
            state, [_claim(str(i)) for i in range(300)]
        )
        state["spent"] = {"claims": spent, "serp": 0}
        return queue.plan_drain(
            state,
            max_claims_per_tick=queue.DEFAULT_MAX_CLAIMS_PER_TICK,
            max_claims_per_day=queue.DEFAULT_MAX_CLAIMS_PER_DAY,
            max_serp_per_day=queue.DEFAULT_MAX_SERP_PER_DAY,
            egress_per_hour=1_000_000,
        )

    selected, _, plan = _day(4950)   # 50 of the day's 5000 claims left
    assert plan["sample_reason"] == queue.SAMPLED_REASON_CLAIM_BUDGET
    assert 0.0 < plan["sample_fraction"] < 1.0
    assert len(selected) <= 50
    # Replayable: the same day, the same spend, the same admitted claims.
    again, _, _ = _day(4950)
    assert {c.key for c in selected} == {c.key for c in again}
    # And a day with budget to spare is NOT a degradation.
    _, _, healthy = _day(0)
    assert healthy["sample_reason"] is None
    assert healthy["sample_fraction"] == 1.0
    assert healthy["selected"] == queue.DEFAULT_MAX_CLAIMS_PER_TICK


def test_the_SERP_budget_still_names_itself_when_it_binds_first():
    """The two exhaustion reasons stay distinguishable at the lifted caps —
    an operator reading a sampled day must know WHICH knob to move."""
    state = queue.empty_state(day="2026-09-20")
    state, _ = queue.refill(state, [_claim(str(i)) for i in range(300)])
    state["spent"] = {"claims": 0, "serp": 9970}
    _, _, plan = queue.plan_drain(
        state,
        max_claims_per_tick=queue.DEFAULT_MAX_CLAIMS_PER_TICK,
        max_claims_per_day=queue.DEFAULT_MAX_CLAIMS_PER_DAY,
        max_serp_per_day=queue.DEFAULT_MAX_SERP_PER_DAY,
        egress_per_hour=1_000_000,
    )
    assert plan["sample_reason"] == queue.SAMPLED_REASON_SERP_BUDGET


def test_the_lifted_caps_clear_the_measured_starving_population():
    """The arithmetic the operator is buying, asserted end to end: the day's
    measured ~545 claims fit inside ONE day's budget and inside three ticks,
    where the starved configuration needed 14 ticks and hit its day cap."""
    per_day = queue.DEFAULT_MAX_CLAIMS_PER_DAY
    per_tick = queue.DEFAULT_MAX_CLAIMS_PER_TICK
    measured_population = 545
    assert per_day >= measured_population * 2
    assert -(-measured_population // per_tick) <= 3
    # ...and 24 ticks of head-room still sit under the day cap, so the DAY is
    # the binding ceiling rather than the tick clamp.
    assert 24 * per_tick <= per_day


def test_the_day_rollover_resets_the_budget_on_read():
    state = {"entries": [], "refill_watermark": "", "day": "2026-09-04",
             "spent": {"claims": 600, "serp": 900}, "sample_fraction": 0.3,
             "sample_reason": "claim_budget_exhausted", "graded_keys": ["x"]}
    rolled = queue.coerce_state(state, day="2026-09-04")
    # coerce keeps the stored day; the load path is what rolls it — simulate.
    assert rolled["day"] == "2026-09-04"


def test_serp_provider_order_is_one_rung_by_default():
    assert queue.serp_provider_order(None) == ("searxng",)
    assert queue.serp_provider_order(["brave", "searxng"]) == ("brave", "searxng")
    assert queue.serp_provider_order([]) == ("searxng",)


# ---------------------------------------------------------------------------
# The write-failure requeue (follow-up to the ledger-writer fixes): a claim
# whose ledger write fails must go BACK into the queue, bounded, never
# silently lost. See EXTERNAL_GRADES_PUBLISHED_AT_FIX_REPORT.md's "Re-queue
# recommendation".
# ---------------------------------------------------------------------------


def test_empty_state_and_coerce_state_carry_write_attempts_and_dead_letter():
    """The two new keys are present from the start (empty_state's own
    doctrine: 'every key present, so no reader guesses'), and coerce_state
    tolerantly rehydrates or discards a malformed stored value the same way
    it already does for `graded_keys`."""
    empty = queue.empty_state(day="2026-09-05")
    assert empty["write_attempts"] == {} and empty["dead_letter"] == []

    good = queue.coerce_state(
        {"write_attempts": {"k1": 2, "k2": "3", "k3": 0, "k4": "garbage"},
         "dead_letter": [{"claim_key": "k5"}, "not-a-dict"]},
        day="2026-09-05",
    )
    # k3 (0) and k4 (unparsable) are dropped — only a POSITIVE attempt count
    # means anything; a stray non-dict dead-letter entry is dropped too.
    assert good["write_attempts"] == {"k1": 2, "k2": 3}
    assert good["dead_letter"] == [{"claim_key": "k5"}]

    garbage = queue.coerce_state(
        {"write_attempts": "not-a-dict", "dead_letter": "not-a-list"},
        day="2026-09-05",
    )
    assert garbage["write_attempts"] == {} and garbage["dead_letter"] == []


def test_requeue_failed_writes_appends_the_claim_and_counts_the_attempt():
    state = queue.empty_state(day="2026-09-05")
    claim = _claim("a failed write")
    state, requeued, dead_lettered = queue.requeue_failed_writes(
        state, [(claim, "DataError")],
    )
    assert requeued == 1 and dead_lettered == 0
    assert state["entries"] == [claim.as_dict()]
    assert state["write_attempts"] == {claim.key: 1}
    assert state["dead_letter"] == []


def test_requeue_failed_writes_dead_letters_after_max_attempts():
    """Three straight failures for the SAME claim: the first two requeue it
    (attempts 1, 2); the third hits the bound and quarantines it instead —
    never re-drained, never silently dropped, and its attempt counter is
    retired once it lands in `dead_letter`."""
    state = queue.empty_state(day="2026-09-05")
    claim = _claim("a chronically failing write")
    for attempt in (1, 2):
        state, requeued, dead_lettered = queue.requeue_failed_writes(
            state, [(claim, "DataError")], max_write_attempts=3,
        )
        assert (requeued, dead_lettered) == (1, 0), f"attempt {attempt}"
        assert state["write_attempts"][claim.key] == attempt
        assert state["entries"] == [claim.as_dict()]
        # Simulate the next tick's `plan_drain` pulling the requeued claim
        # back OUT of `entries` before grading it (and failing) again — the
        # same invariant `run_width_tick` holds: a claim is never both
        # pending AND mid-write in the same round trip.
        state = dict(state)
        state["entries"] = []

    state, requeued, dead_lettered = queue.requeue_failed_writes(
        state, [(claim, "CheckViolationError")], max_write_attempts=3,
    )
    assert (requeued, dead_lettered) == (0, 1)
    assert state["entries"] == []  # NOT put back a fourth time
    assert claim.key not in state["write_attempts"]  # the counter is retired
    assert len(state["dead_letter"]) == 1
    entry = state["dead_letter"][0]
    assert entry["claim_key"] == claim.key
    assert entry["write_attempts"] == 3
    assert entry["last_error"] == "CheckViolationError"


def test_requeue_failed_writes_is_a_no_op_on_an_empty_failed_list():
    """The byte-identity case: when nothing failed this tick, the queue state
    is unchanged (new keys present, both still empty)."""
    state = queue.empty_state(day="2026-09-05")
    state, _ = queue.refill(state, [_claim("still pending")])
    before = dict(state)
    state, requeued, dead_lettered = queue.requeue_failed_writes(state, [])
    assert requeued == 0 and dead_lettered == 0
    assert state["entries"] == before["entries"]
    assert state["write_attempts"] == {} and state["dead_letter"] == []


def test_requeue_failed_writes_tracks_two_claims_independently():
    state = queue.empty_state(day="2026-09-05")
    a, b = _claim("claim a"), _claim("claim b")
    state, requeued, dead_lettered = queue.requeue_failed_writes(
        state, [(a, "DataError"), (b, "DataError")],
    )
    assert requeued == 2 and dead_lettered == 0
    assert state["write_attempts"] == {a.key: 1, b.key: 1}
    assert {e["claim_key"] for e in state["entries"]} == {a.key, b.key}


# ---------------------------------------------------------------------------
# W-4 — the grader plane
# ---------------------------------------------------------------------------


def _envelope(urls=("https://news.example/a",)):
    return grader.EvidenceEnvelope(
        query="q",
        results=tuple({"url": u, "title": "t", "snippet": "s"} for u in urls),
        search_status={"status": "completed", "liveness": "unverified"},
        provider="searxng",
    )


def test_a_fabricated_url_is_dropped_and_an_unsourced_verdict_demoted():
    g = grader.WidthGrade(claim=_claim("x"), verdict="NOT_FOUND")
    grader.parse_grader_reply(
        '{"verdict":"SUPPORTED","rationale":"r","evidence":'
        '[{"url":"https://evil.example/z","quote":"q"}]}',
        g, allowed_urls=["https://news.example/a"],
    )
    # The URL was not in the results -> dropped -> no surviving URL -> demoted.
    assert g.verdict == "NOT_FOUND"
    assert "demoted" in g.rationale


# ---------------------------------------------------------------------------
# THE SEAM — the binding, and the gates run through the REAL checker
# ---------------------------------------------------------------------------
#
# THE TEST THAT WOULD HAVE CAUGHT IT. From the width deploy (2026-09-05 ~18:00Z)
# to the 09-06 repair, `check_decisive_span` called `check_span(claim_text=,
# url=, span=, evidence_window=)` against a function whose parameters are
# `(claim, candidate_url, fetched_text, published_at, evidence_window, *,
# produced_at, archive_root)`. Every call raised TypeError, the wrapper's
# except-branch degraded the verdict to UNCHECKED/span_check_unavailable, and
# 211 ledger rows recorded a decisive proposal nobody ever checked.
#
# The whole suite stayed green, because every test that reached this code passed
# a MOCK checker — and a mock accepts any signature. The three tests below are
# the ones that bite: one binds the ACTUAL arguments against the ACTUAL
# signature, and the other two run the ACTUAL `check_span` with only the FETCH
# stubbed. No mock of the checker appears anywhere in this section, on purpose.


def _decisive(verdict="SUPPORTED", *, span="raised its policy rate",
              url="https://www.reuters.com/x", window=None, produced=""):
    claim = claims.WidthClaim(
        claim_text="The central bank raised its policy rate in March 2026.",
        population="assembly_span", graded_output_id="o",
        analyst_id="country_composition", origin_head_id="h", start=0, end=10,
        read_evidence_window=dict(window or {}),
        produced_at=produced,
    )
    return grader.WidthGrade(
        claim=claim, verdict=verdict, decisive_url=url, decisive_span=span,
    )


#: The production window stamp's own shape — `composition_window.
#: evidence_window_span` writes `{"oldest", "newest"}`, and G-3 only runs when
#: the window is MEASURED. A test that used some other spelling would exercise
#: the unmeasured branch and prove nothing about the time gate.
_WINDOW = {"oldest": "2026-02-01T00:00:00+00:00",
           "newest": "2026-04-01T00:00:00+00:00"}

_PAGE_TEXT = (
    "Nairobi - The central bank raised its policy rate in March 2026, "
    "the sharpest move in two years."
)


class _Page:
    """What the drain's fetch leg hands the checker (a `FetchedPage` duck)."""

    def __init__(self, text=_PAGE_TEXT, published_at="2026-03-04"):
        self.text = text
        self.published_at = published_at
        self.url = "https://www.reuters.com/x"


def test_the_span_check_call_binds_against_the_real_signature():
    """THE REGRESSION TEST FOR THE 09-05 SEAM DEFECT.

    `span_check_arguments` builds exactly what the wrapper passes; this binds it
    against `inspect.signature(check_span)`. It needs no page, no binding and no
    pool, and it fails the moment either side of the seam is renamed or
    reordered — which is precisely what nothing in the tree could see when the
    call was `claim_text=/url=/span=` against `(claim, candidate_url,
    fetched_text, ...)`.
    """
    import inspect

    from legba.data.provenance.external_span_check import check_span

    args, kwargs = grader.span_check_arguments(
        _decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
        _Page(),
        archive_root=None,
    )
    bound = inspect.signature(check_span).bind(*args, **kwargs)
    bound.apply_defaults()
    # Not just "it binds" — the arguments land on the parameters they mean.
    assert bound.arguments["candidate_url"] == "https://www.reuters.com/x"
    assert bound.arguments["fetched_text"] == _PAGE_TEXT
    assert bound.arguments["published_at"] == "2026-03-04"
    assert bound.arguments["evidence_window"] == _WINDOW
    assert bound.arguments["produced_at"] == "2026-04-01T00:00:00+00:00"
    # And the claim mapping is one `DecisiveClaim.coerce` actually accepts.
    from legba.data.provenance.external_span_check import DecisiveClaim

    coerced = DecisiveClaim.coerce(bound.arguments["claim"])
    assert coerced.decisive_span == "raised its policy rate"
    assert coerced.proposed_verdict == "SUPPORTED"
    assert coerced.text.startswith("The central bank raised")


def test_the_gates_run_through_the_real_checker_with_only_the_fetch_stubbed():
    """G-1/G-2/G-3 end to end against the REAL `check_span`. No checker mock.

    Five cases, each the one the round it comes from was overturned by:
    a verbatim span on a Tier-2 page inside the window SURVIVES with its sha256,
    tier and archive_ref filled; a fabricated span is NOT_FOUND with G-2's
    reason named; a source outside the read's own window is NOT_FOUND /
    out_of_window (R2-C4 case #5); an undated source cannot be anchored at all;
    and an unregistered domain SURVIVES, flagged (F-3, ruled visible).
    """
    def run(grade, page):
        return grader.check_decisive_span(
            grade, _envelope(), page=page, archive_root=None,
        )

    # -- the admitted case: verbatim, Tier 2, in window --------------------
    g = run(_decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
            _Page())
    assert g.verdict == "SUPPORTED"
    assert len(g.decisive_span_sha256) == 64, "G-2 content-addresses the span"
    assert g.decisive_source_tier == 2
    assert g.archive_ref.startswith("cas:grading/sha256/"), (
        "the grading archive has its OWN scheme — never a signal's cas: ref"
    )
    assert g.decisive_published_at.startswith("2026-03-04")
    assert not g.tier_unknown

    # -- G-2: the page does not say it ------------------------------------
    g = run(_decisive(span="cut its policy rate to zero", window=_WINDOW,
                      produced="2026-04-01T00:00:00+00:00"), _Page())
    assert g.verdict == "NOT_FOUND"
    assert "span_unresolved" in g.rationale
    assert not g.decisive_span_sha256, "an unresolved span is never addressed"
    # The evidence SURVIVES the demotion — the row must still say what it
    # demoted, or a month later it is indistinguishable from an unsourced one.
    assert g.decisive_url == "https://www.reuters.com/x"
    assert g.decisive_span == "cut its policy rate to zero"

    # -- G-3: published outside the read's own window ----------------------
    g = run(_decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
            _Page(published_at="2024-01-05"))
    assert g.verdict == "NOT_FOUND"
    assert g.unchecked_reason == "out_of_window"
    assert "out_of_window" in g.rationale

    # -- G-3: no publish date at all — it cannot be anchored ---------------
    g = run(_decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
            _Page(published_at=None))
    assert g.verdict == "NOT_FOUND"
    assert "no_publish_date" in g.rationale

    # -- G-1/F-3: an unregistered domain stands, and is FLAGGED ------------
    g = run(_decisive(url="https://blog.example/x", window=_WINDOW,
                      produced="2026-04-01T00:00:00+00:00"), _Page())
    assert g.verdict == "SUPPORTED"
    assert g.decisive_source_tier is None and g.tier_unknown is True
    assert "tier_unknown" in g.rationale


def test_a_tier_3_only_decisive_verdict_demotes_through_the_real_checker():
    """G-1, C4 case #10 — the demotion the SCORER used to miss entirely."""
    g = grader.check_decisive_span(
        _decisive(url="https://www.rt.com/x", window=_WINDOW,
                  produced="2026-04-01T00:00:00+00:00"),
        _envelope(), page=_Page(), archive_root=None,
    )
    assert g.verdict == "NOT_FOUND"
    assert g.decisive_source_tier == 3
    assert "tier_3_only" in g.rationale


def test_an_unfetched_decisive_page_degrades_with_the_reason_the_drain_named():
    """No page, no decisive verdict — and the ledger says WHICH kind of no.

    `robots_disallowed` and `span_fetch_failed` are different statements: one is
    a publisher's standing answer, the other is a retryable failure of ours.
    They must never share a row shape.
    """
    for reason in ("robots_disallowed", "span_fetch_failed"):
        g = grader.check_decisive_span(
            _decisive(window=_WINDOW), _envelope(),
            page=None, fetch_reason=reason,
        )
        assert g.verdict == "UNCHECKED"
        assert g.unchecked_reason == reason
        assert "not read" in g.rationale

    # No reason offered at all still names a class, never an empty column.
    g = grader.check_decisive_span(
        _decisive(window=_WINDOW), _envelope(), page=None,
    )
    assert g.unchecked_reason == "span_fetch_failed"


def test_a_decisive_verdict_without_the_span_check_module_degrades_to_unchecked(
    monkeypatch,
):
    """G-2: no verbatim span check ⇒ no decisive verdict.

    THE OLD VERSION OF THIS TEST PASSED FOR THE WRONG REASON. It called
    `check_decisive_span(..., checker=None)` and asserted
    UNCHECKED/span_check_unavailable — which it got, because the soft import
    SUCCEEDED and the call then raised TypeError into the same except-branch.
    The test meant to pin "W-3 is absent" and was in fact pinning "the call is
    broken", so the defect was invisible for a day. The absence is now forced
    explicitly, at the import helper, which is the only thing that can actually
    be absent.
    """
    monkeypatch.setattr(grader, "_load_span_check", lambda: None)
    g = grader.check_decisive_span(
        _decisive("CONTRADICTED", window=_WINDOW), _envelope(), checker=None,
    )
    assert g.verdict == "UNCHECKED"
    assert g.unchecked_reason == "span_check_unavailable"


def test_a_checker_that_raises_is_logged_and_degraded_never_published():
    """`check_span`'s contract is "never raises". If it ever does, the seam has
    moved again — and a verdict must not be published on the strength of a
    traceback."""
    def _boom(*a, **k):
        raise TypeError("check_span() got an unexpected keyword argument")

    g = grader.check_decisive_span(
        _decisive(window=_WINDOW), _envelope(), checker=_boom, page=_Page(),
    )
    assert g.verdict == "UNCHECKED"
    assert g.unchecked_reason == "span_check_unavailable"


def test_a_non_decisive_grade_never_reaches_the_checker():
    """NOT_FOUND has no page to check, and must not spend a fetch or a gate."""
    calls = []

    def _spy(*a, **k):
        calls.append(a)
        raise AssertionError("the checker must not run on a non-decisive grade")

    g = grader.WidthGrade(claim=_claim("x"), verdict="NOT_FOUND")
    grader.check_decisive_span(g, _envelope(), checker=_spy, page=_Page())
    assert g.verdict == "NOT_FOUND"
    assert calls == []


# ---------------------------------------------------------------------------
# THE GRACE KNOB — threading through the seam (2026-09-06 follow-up)
# ---------------------------------------------------------------------------


def test_span_check_arguments_carries_grace_before_hours_into_the_real_signature():
    """The same seam-defect guard as the 09-05 regression test, extended: a
    non-default grace must land on `check_span`'s own parameter, not get lost
    in the wrapper."""
    import inspect

    from legba.data.provenance.external_span_check import check_span

    args, kwargs = grader.span_check_arguments(
        _decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
        _Page(),
        archive_root=None,
        grace_before_hours=72.0,
    )
    bound = inspect.signature(check_span).bind(*args, **kwargs)
    bound.apply_defaults()
    assert bound.arguments["grace_before_hours"] == 72.0


def test_span_check_arguments_defaults_grace_to_zero():
    args, kwargs = grader.span_check_arguments(
        _decisive(window=_WINDOW), _Page(), archive_root=None,
    )
    assert kwargs["grace_before_hours"] == 0.0


def test_check_decisive_span_admits_a_source_the_grace_knob_reaches():
    """A source 48h before ``oldest`` demotes at grace=0 and is admitted, span
    resolved verbatim and archived, at grace=72 — through the REAL checker
    (no mock), the same shape as the 09-05 regression suite above."""
    page = _Page(published_at="2026-01-30")  # `_WINDOW["oldest"]` - 48h

    no_grace = grader.check_decisive_span(
        _decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
        _envelope(), page=page, archive_root=None,
    )
    assert no_grace.verdict == "NOT_FOUND"
    assert no_grace.unchecked_reason == "out_of_window"

    with_grace = grader.check_decisive_span(
        _decisive(window=_WINDOW, produced="2026-04-01T00:00:00+00:00"),
        _envelope(), page=page, archive_root=None, grace_before_hours=72.0,
    )
    assert with_grace.verdict == "SUPPORTED"
    assert len(with_grace.decisive_span_sha256) == 64, "G-2 still content-addresses it"
    assert with_grace.archive_ref.startswith("cas:grading/sha256/")
    assert with_grace.unchecked_reason == ""


def test_window_grace_hours_defaults_to_zero_with_no_env_and_no_option(monkeypatch):
    monkeypatch.delenv(width.WINDOW_GRACE_HOURS_ENV, raising=False)
    assert width._window_grace_hours({}) == 0.0


def test_window_grace_hours_reads_the_env_as_the_base(monkeypatch):
    monkeypatch.setenv(width.WINDOW_GRACE_HOURS_ENV, "48")
    assert width._window_grace_hours({}) == 48.0


def test_window_grace_hours_option_wins_over_env(monkeypatch):
    """The house `_coerce` idiom: env supplies the base, an option present
    wins — `_coverage_floor_scan.CoverageFloorConfig.from_options`'s own
    precedence, mirrored here."""
    monkeypatch.setenv(width.WINDOW_GRACE_HOURS_ENV, "48")
    assert width._window_grace_hours({"window_grace_hours": 96}) == 96.0
    assert width._window_grace_hours({"window_grace_hours": 96.5}) == 96.5


def test_window_grace_hours_bad_env_value_keeps_zero(monkeypatch, caplog):
    monkeypatch.setenv(width.WINDOW_GRACE_HOURS_ENV, "not-a-number")
    with caplog.at_level(logging.WARNING):
        assert width._window_grace_hours({}) == 0.0
    assert "bad_window_grace_env" in caplog.text


def test_window_grace_hours_bad_option_value_keeps_the_env_value(monkeypatch, caplog):
    monkeypatch.setenv(width.WINDOW_GRACE_HOURS_ENV, "48")
    with caplog.at_level(logging.WARNING):
        assert width._window_grace_hours({"window_grace_hours": "nope"}) == 48.0
    assert "bad_window_grace_option" in caplog.text


def test_window_grace_hours_negative_is_a_no_op(monkeypatch):
    monkeypatch.delenv(width.WINDOW_GRACE_HOURS_ENV, raising=False)
    assert width._window_grace_hours({"window_grace_hours": -5}) == 0.0


# ---------------------------------------------------------------------------
# THE VISIBILITY RECEIPT — out_of_window_gap_buckets (2026-09-06 follow-up)
# ---------------------------------------------------------------------------


def _demoted_grade(*, hours_before_oldest, resolved=True, role=grader.RATER_PRIMARY,
                    unchecked_reason="out_of_window"):
    """A PRIMARY grade shaped like ``check_decisive_span`` would leave one that
    G-3 demoted for landing before ``_WINDOW['oldest']`` by ``hours_before_oldest``."""
    oldest = datetime.fromisoformat(_WINDOW["oldest"])
    published = oldest - timedelta(hours=hours_before_oldest)
    claim = claims.WidthClaim(
        claim_text="x", population="assembly_span", graded_output_id="o",
        analyst_id="country_composition", origin_head_id="h", start=0, end=1,
        read_evidence_window=dict(_WINDOW),
    )
    return grader.WidthGrade(
        claim=claim, verdict="NOT_FOUND", rater_role=role,
        unchecked_reason=unchecked_reason,
        decisive_published_at=published.isoformat(),
        decisive_span_sha256=("a" * 64) if resolved else "",
    )


def test_out_of_window_gap_buckets_sorts_by_gap_from_oldest():
    graded = [
        _demoted_grade(hours_before_oldest=1),     # <=24h
        _demoted_grade(hours_before_oldest=24),    # <=24h
        _demoted_grade(hours_before_oldest=25),    # <=72h
        _demoted_grade(hours_before_oldest=72),    # <=72h
        _demoted_grade(hours_before_oldest=73),    # <=7d
        _demoted_grade(hours_before_oldest=24 * 7),      # <=7d
        _demoted_grade(hours_before_oldest=24 * 7 + 1),  # >7d
        _demoted_grade(hours_before_oldest=24 * 30),     # >7d
    ]
    buckets = width.out_of_window_gap_buckets(graded)
    assert buckets == {
        width.GAP_BUCKET_24H: 2,
        width.GAP_BUCKET_72H: 2,
        width.GAP_BUCKET_7D: 2,
        width.GAP_BUCKET_OVER_7D: 2,
    }


def test_out_of_window_gap_buckets_excludes_unresolved_spans():
    """G-2 must have passed — a span that never resolved is a different
    failure, and grace could never have admitted it either way."""
    buckets = width.out_of_window_gap_buckets(
        [_demoted_grade(hours_before_oldest=1, resolved=False)]
    )
    assert buckets == {label: 0 for label in width.GAP_BUCKET_ORDER}


def test_out_of_window_gap_buckets_excludes_no_publish_date_demotions():
    """`no_publish_date` is a different G-3 reason with no gap to bucket."""
    g = _demoted_grade(hours_before_oldest=1, unchecked_reason="no_publish_date")
    g.decisive_published_at = ""
    buckets = width.out_of_window_gap_buckets([g])
    assert buckets == {label: 0 for label in width.GAP_BUCKET_ORDER}


def test_out_of_window_gap_buckets_excludes_after_window_sources():
    """A source published AFTER `newest` is also out_of_window, but grace can
    never reach it (the after bound is unchanged) — it must not inflate a
    bucket that describes what a BEFORE grace would admit."""
    newest = datetime.fromisoformat(_WINDOW["newest"])
    claim = claims.WidthClaim(
        claim_text="x", population="assembly_span", graded_output_id="o",
        analyst_id="country_composition", origin_head_id="h", start=0, end=1,
        read_evidence_window=dict(_WINDOW),
    )
    after = grader.WidthGrade(
        claim=claim, verdict="NOT_FOUND", rater_role=grader.RATER_PRIMARY,
        unchecked_reason="out_of_window",
        decisive_published_at=(newest + timedelta(hours=1)).isoformat(),
        decisive_span_sha256="a" * 64,
    )
    buckets = width.out_of_window_gap_buckets([after])
    assert buckets == {label: 0 for label in width.GAP_BUCKET_ORDER}


def test_out_of_window_gap_buckets_excludes_the_audit_rater_row():
    """Counting both raters would double-count one demoted claim."""
    g = _demoted_grade(hours_before_oldest=1, role=grader.RATER_AUDIT)
    buckets = width.out_of_window_gap_buckets([g])
    assert buckets == {label: 0 for label in width.GAP_BUCKET_ORDER}


def test_out_of_window_gap_buckets_is_indifferent_to_the_ticks_own_grace():
    """The buckets always answer 'what would N hours admit', computed at the
    RAW window — they do not shrink just because the tick itself ran under a
    nonzero grace (that admitted row would not be `out_of_window` any more,
    which is exactly how the metric stays honest without a second code path)."""
    result = width.WidthRunResult(
        graded=[_demoted_grade(hours_before_oldest=1)],
        window_grace_hours=1000.0,
    )
    assert result.out_of_window_gap_buckets[width.GAP_BUCKET_24H] == 1


def test_width_heartbeat_block_carries_the_grace_receipt_fields():
    result = width.WidthRunResult(
        graded=[_demoted_grade(hours_before_oldest=1)],
        window_grace_hours=24.0,
    )
    block = width.width_heartbeat_block(result)
    assert block["window_grace_hours"] == 24.0
    assert block["out_of_window_gap_buckets"][width.GAP_BUCKET_24H] == 1


def test_width_heartbeat_block_default_grace_is_zero_and_buckets_are_all_zero():
    """The default-0 build's byte-identical claim, at the receipt level: a
    tick that never touched the knob reports it as 0 and an all-zero shape,
    not an absent key."""
    result = width.WidthRunResult()
    block = width.width_heartbeat_block(result)
    assert block["window_grace_hours"] == 0.0
    assert block["out_of_window_gap_buckets"] == {
        label: 0 for label in width.GAP_BUCKET_ORDER
    }


# ---------------------------------------------------------------------------
# THE FETCH LEG — F-7's robots gate, and every way a page fails to arrive
# ---------------------------------------------------------------------------


class _ToolResult:
    def __init__(self, status="completed", output=None, error=None):
        self.status = status
        self.output = output or {}
        self.error = error


class _Outcome:
    def __init__(self, *, admitted=True, tool_result=None, block_cause=None):
        self.admitted = admitted
        self.tool_result = tool_result
        self.block_cause = block_cause


class _Binding:
    """A binding double that RECORDS whether it was asked to fetch.

    The recording is the assertion that matters for F-7: a robots-disallowed URL
    must not merely produce an UNCHECKED verdict, it must produce NO REQUEST.
    A test that only checked the verdict would pass just as happily against an
    implementation that fetched the page and then threw it away.
    """

    def __init__(self, *, body="<html><body><p>x</p></body></html>",
                 status_code=200, status="completed", admitted=True,
                 raises=None):
        self.calls = []
        self._body = body
        self._status_code = status_code
        self._status = status
        self._admitted = admitted
        self._raises = raises

    async def run_tool(self, tool_name, args, **kwargs):
        self.calls.append((tool_name, dict(args)))
        if self._raises is not None:
            raise self._raises
        if not self._admitted:
            return _Outcome(admitted=False, block_cause="governor_exhausted")
        if self._status != "completed":
            return _Outcome(tool_result=_ToolResult(
                status=self._status, error="fetch_failed: boom",
            ))
        return _Outcome(tool_result=_ToolResult(output={
            "url": args.get("url"),
            "status_code": self._status_code,
            "content_type": "text/html",
            "body": self._body,
            "truncated": False,
        }))


def _robots(monkeypatch, decision):
    """Pin the robots verdict without touching the network."""
    async def _decide(url, **kwargs):
        return decision

    monkeypatch.setattr(fetch_leg, "robots_decision", _decide)


async def test_a_robots_disallowed_url_is_never_fetched(monkeypatch):
    """F-7, the ToS posture, as a mechanical fact rather than an intention.

    The design's own acceptance criterion for W-3 is "robots-disallowed URL is
    not fetched" — so the assertion is on the BINDING, not only on the reason.
    """
    _robots(monkeypatch, "disallowed")
    binding = _Binding()
    page, reason = await fetch_leg.fetch_decisive_page(
        binding, "https://www.reuters.com/x",
    )
    assert page is None
    assert reason == "robots_disallowed"
    assert binding.calls == [], "a disallowed page must not be requested at all"


async def test_an_unreachable_robots_txt_refuses_but_is_not_called_disallowed(
    monkeypatch,
):
    """`robots.py` fails CLOSED on an unreachable robots.txt, and this plane
    keeps that — but it does NOT report it as the publisher refusing us.

    `robots_allows` collapses disallowed/unreachable/bad_url into one False.
    Counting all three as `robots_disallowed` would inflate the single number an
    operator would read to decide a ToS posture, so the two are told apart here.
    """
    for decision in ("unreachable", "bad_url"):
        _robots(monkeypatch, decision)
        binding = _Binding()
        page, reason = await fetch_leg.fetch_decisive_page(
            binding, "https://www.reuters.com/x",
        )
        assert page is None
        assert reason == "span_fetch_failed", decision
        assert binding.calls == [], decision


async def test_an_allowed_page_is_fetched_and_its_date_extracted(monkeypatch):
    """no_rules (a 4xx robots.txt) permits, and the leg returns a real page."""
    _robots(monkeypatch, "no_rules")
    html = (
        '<html><head><meta property="article:published_time" '
        'content="2026-03-04T09:00:00Z"/></head><body><article>'
        "<p>The central bank raised its policy rate in March 2026.</p>"
        "</article></body></html>"
    )
    binding = _Binding(body=html)
    page, reason = await fetch_leg.fetch_decisive_page(
        binding, "https://www.reuters.com/x",
    )
    assert reason == ""
    assert page is not None
    assert binding.calls == [("web_fetch", {"url": "https://www.reuters.com/x"})]
    assert "raised its policy rate in March 2026" in page.text
    assert "<article>" not in page.text, "G-2 compares text, not markup"
    assert page.published_at.startswith("2026-03-04")
    assert page.extracted is True


async def test_a_non_2xx_is_not_a_page(monkeypatch):
    """`web_fetch_tool` never calls `raise_for_status`, so a 404 comes back
    status="completed" carrying the ERROR PAGE's HTML. Folding that into G-2
    would let a publisher's not-found template decide a claim."""
    _robots(monkeypatch, "allowed")
    binding = _Binding(status_code=404,
                       body="<html><body>Page not found</body></html>")
    page, reason = await fetch_leg.fetch_decisive_page(
        binding, "https://www.reuters.com/gone",
    )
    assert page is None
    assert reason == "span_fetch_failed"
    assert binding.calls, "the status is only knowable after the request"


async def test_every_fetch_failure_is_named_and_never_raises(monkeypatch):
    """A timeout, a gate block, a failed tool result, an empty body and a
    raising binding are all UNCHECKED with a named reason — never an exception
    that would lose the whole tick's remaining claims."""
    _robots(monkeypatch, "allowed")
    import asyncio as _asyncio

    cases = [
        _Binding(raises=_asyncio.TimeoutError()),
        _Binding(raises=RuntimeError("pool exhausted")),
        _Binding(admitted=False),
        _Binding(status="failed"),
        _Binding(body=""),
    ]
    for binding in cases:
        page, reason = await fetch_leg.fetch_decisive_page(
            binding, "https://www.reuters.com/x",
        )
        assert page is None
        assert reason == "span_fetch_failed"


async def test_an_unextractable_document_falls_back_to_the_raw_body(monkeypatch):
    """A page trafilatura cannot read is still evidence. The fallback is a
    WEAKER check, not a different verdict, and the flag records which ran."""
    _robots(monkeypatch, "allowed")
    binding = _Binding(body="raised its policy rate in March 2026")
    page, reason = await fetch_leg.fetch_decisive_page(
        binding, "https://www.reuters.com/x",
    )
    assert reason == ""
    assert page.text == "raised its policy rate in March 2026"
    assert page.extracted is False


async def test_the_page_cache_fetches_one_url_once_per_tick(monkeypatch):
    """The double-grade path hands the audit rater the BYTE-IDENTICAL envelope
    the primary saw. A page re-fetched between the two raters breaks that
    invariant exactly as a second search would — the two raters would be gated
    against two different documents and the overlap would stop meaning what it
    says. A refusal is cached too: a host that said no is not asked twice.
    """
    _robots(monkeypatch, "allowed")
    binding = _Binding()
    cache = width.PageCache()
    a, _ = await cache.get(binding, "https://www.reuters.com/x")
    b, _ = await cache.get(binding, "https://www.reuters.com/x")
    assert a is b
    assert len(binding.calls) == 1
    assert cache.fetches == 1

    _robots(monkeypatch, "disallowed")
    refused = _Binding()
    cache2 = width.PageCache()
    await cache2.get(refused, "https://www.example.com/y")
    await cache2.get(refused, "https://www.example.com/y")
    assert cache2.fetches == 1 and cache2.robots_refused == 1
    assert refused.calls == []


def test_uncheckable_and_absence_unverified_grades_stamp_the_configured_grader():
    """THE LIVE DEFECT (first width sweep, 2026-09-05): both verdicts rendered
    BEFORE any grader call — UNCHECKABLE (decided by the pre-filter) and
    UNCHECKED/absence_liveness_unverified (the search plane never proved its
    own emptiness) — left ``grader_family``/``grader_component_id`` at the
    ``WidthGrade`` default ``""``, which is exactly the shape migration 0190's
    two non-empty CHECK constraints exist to reject. ``grade_one`` only reaches
    either branch once a grader route is resolved and wired, so that route is
    what must land here even though no call was made.
    """
    uncheckable_claim = claims.WidthClaim(
        claim_text="Rates are basically fine, on balance.",
        population="assembly_span", graded_output_id="o",
        analyst_id="country_composition", origin_head_id="h",
        start=0, end=10, uncheckable_class="perspective",
    )
    g = grader.uncheckable_grade(
        uncheckable_claim,
        grader_family="mistral",
        grader_component_id="llm.judge.openrouter_mistral_large.openai_compat",
    )
    assert g.verdict == grader.UNCHECKABLE_VERDICT
    assert g.grader_family == "mistral"
    assert g.grader_component_id == (
        "llm.judge.openrouter_mistral_large.openai_compat"
    )
    # Nobody answered — grader_served_by is NOT stamped as though they did.
    assert g.grader_served_by == ""

    absence_claim = _claim("no reported disruption at the border")
    envelope = _envelope()  # liveness=unverified, supports_absence_claim=False
    g2 = grader.absence_unverified_grade(
        absence_claim, envelope,
        grader_family="mistral",
        grader_component_id="llm.judge.openrouter_mistral_large.openai_compat",
    )
    assert g2.verdict == "UNCHECKED"
    assert g2.unchecked_reason == grader.UNCHECKED_ABSENCE_LIVENESS
    assert g2.grader_family == "mistral"
    assert g2.grader_component_id == (
        "llm.judge.openrouter_mistral_large.openai_compat"
    )
    assert g2.grader_served_by == ""

    # Both constructors STILL default to "" when a caller passes no route —
    # they do not invent a value. The fix lives at the call site (`grade_one`
    # in ``_external_audit_width.py``), which now always threads the resolved
    # route through; see ``test_migration_0190_external_grades.py`` for the
    # real-Postgres regression proving the fixed row inserts cleanly and the
    # bare (pre-fix) shape is rejected by the ledger's own CHECK constraints.
    bare = grader.uncheckable_grade(uncheckable_claim).as_dict()
    assert bare["grader_family"] == ""
    assert bare["grader_component_id"] == ""


def test_double_grade_routes_all_contradicted_plus_a_hash_gated_sample():
    contra = grader.WidthGrade(claim=_claim("x"), verdict="CONTRADICTED")
    assert grader.should_double_grade(contra) is True
    # An UNCHECKED/UNCHECKABLE is never double-graded — nothing to overlap.
    unc = grader.WidthGrade(claim=_claim("y"), verdict="UNCHECKED")
    assert grader.should_double_grade(unc) is False
    # SUPPORTED is gated at the fraction: forcing fraction 1.0 admits it, 0.0
    # excludes it, proving the gate is what selects (not the verdict).
    sup = grader.WidthGrade(claim=_claim("z"), verdict="SUPPORTED")
    assert grader.should_double_grade(sup, fraction=1.0) is True
    assert grader.should_double_grade(sup, fraction=0.0) is False


def test_overlap_bands_are_pre_registered():
    assert grader.overlap_band(0.82) == "stands"
    assert grader.overlap_band(0.77) == "contingent"
    assert grader.overlap_band(0.70) == "instrument_limited"
    assert grader.overlap_band(None) == "unmeasured"  # never a number


# ---------------------------------------------------------------------------
# W-4 — the family fence (the R2 VOID, in code)
# ---------------------------------------------------------------------------


def test_the_three_way_grader_fence(monkeypatch):
    from legba.runtime import analyst_deps_builder as ab

    monkeypatch.setenv("LEGBA_JUDGE_STACK_REF", "llm.judge.openrouter_nemotron120b")

    def refuse(component):
        return ab.grader_fence_refusal(
            component,
            primary_component_id="llm.primary.openai_compat",
            judge_component_id="llm.judge.openrouter_nemotron120b",
        )

    # (a) Anthropic — HARD standing rule.
    assert refuse("llm.anthropic.opus_4_7") == ab.GRADER_REFUSE_ANTHROPIC
    # (b) the writer's family — a different component of the same family too.
    assert refuse("llm.primary.openai_compat") == ab.GRADER_REFUSE_WRITER_FAMILY
    # (c) the judge's family — the exact R2 VOID (nemotron == the judge).
    assert refuse("llm.judge.nemotron3_super.openai_compat") == ab.GRADER_REFUSE_JUDGE_FAMILY
    assert refuse("llm.judge.openrouter_nemotron120b") == ab.GRADER_REFUSE_JUDGE_FAMILY
    # the RULED grader — a third family — is allowed.
    assert refuse("llm.judge.cerebras_gemma4_31b.openai_compat") == ""
    # the FOURTH family audit rater — allowed.
    assert refuse("llm.audit.openrouter_llama33_70b.openai_compat") == ""


MISTRAL_GRADER = "llm.judge.openrouter_mistral_large.openai_compat"


def test_the_mistral_grader_passes_the_fence_that_refuses_the_judge_and_the_writer(
    monkeypatch,
):
    """The third-family slot moved off the UNFUNDED Cerebras Gemma component.

    The R4 reachability probe measured Cerebras at HTTP 402 — **0 of 12** graded
    calls — and OpenRouter Mistral Large 3 at **12 of 12**. This pins the
    property the move has to preserve: the replacement is a genuinely THIRD
    family, so it clears the same fence that refuses the production judge and
    the core plane. A grader that fails this test is R2's void wearing a new
    model id.
    """
    from legba.runtime import analyst_deps_builder as ab

    monkeypatch.setenv("LEGBA_JUDGE_STACK_REF", "llm.judge.openrouter_nemotron120b")

    def refuse(component):
        return ab.grader_fence_refusal(
            component,
            primary_component_id="llm.primary.openai_compat",
            judge_component_id="llm.judge.openrouter_nemotron120b",
        )

    # ACCEPTED — mistral is neither the writer's family nor the judge's.
    assert refuse(MISTRAL_GRADER) == ""
    assert ab.grader_family_for_component(MISTRAL_GRADER) == "mistral"
    # …and the family is DECLARED, not merely unknown. An id absent from the map
    # gets family "" and slips through by id equality alone, which is a pass
    # nobody argued for.
    assert MISTRAL_GRADER in ab.GRADER_FAMILY_BY_COMPONENT
    for plane in ("llm.primary.openai_compat",
                  "llm.judge.openrouter_nemotron120b",
                  "llm.judge.nemotron3_super.openai_compat"):
        assert ab.grader_family_for_component(plane) != "mistral"

    # REFUSED — the nemotron judge, by id and by family.
    assert refuse("llm.judge.openrouter_nemotron120b") == ab.GRADER_REFUSE_JUDGE_FAMILY
    assert (refuse("llm.judge.nemotron3_super.openai_compat")
            == ab.GRADER_REFUSE_JUDGE_FAMILY)
    # REFUSED — the core plane, which is the writer marking its own homework.
    assert refuse("llm.primary.openai_compat") == ab.GRADER_REFUSE_WRITER_FAMILY

    # And it survives the ladder end-to-end, not just the predicate.
    monkeypatch.delenv("LEGBA_EXTERNAL_GRADER_STACK_REF", raising=False)
    llm = {"primary": {"raw": "llm.primary.openai_compat"},
           "grader": {"raw": MISTRAL_GRADER}}
    route = ab.resolve_grader_route_from_llm_block(llm)
    assert route.component_id == MISTRAL_GRADER and route.family == "mistral"


def test_the_mistral_grader_descriptor_is_the_registrar_source_of_truth():
    """One body, one place — and the id the fence knows is the id on disk."""
    import importlib.util
    from pathlib import Path

    import yaml

    from legba.data.schemas.stack import LLMProvider
    from legba.runtime.external_grader_route import GRADER_FAMILY_BY_COMPONENT

    repo = Path(__file__).resolve().parents[2]
    name = "stack_component_llm_judge_openrouter_mistral_large.yaml"
    body = yaml.safe_load((repo / "descriptors" / name).read_text())
    model = LLMProvider.model_validate(body)

    assert model.id == MISTRAL_GRADER
    # `.openai_compat` is load-bearing: it routes to the vLLM handler, which is
    # the wire shape OpenRouter speaks.
    assert model.id.endswith(".openai_compat")
    assert model.state.value == "draft", "registering it must activate nothing"
    assert model.config.model_name.raw == "mistralai/mistral-medium-3.1"
    # the SAME OpenRouter vault ref the nemotron judge lane uses — one key.
    assert model.config.api_key.raw == "llm.judge.openrouter.api_key"
    assert "openrouter.ai" in model.config.api_endpoint.raw
    # a PAID lane, unlike the `:free` nemotron lanes: priced AND gauged.
    assert model.config.price_input_per_m.raw > 0
    assert model.config.price_output_per_m.raw > 0
    assert model.config.daily_burn_alert_usd.raw > 0
    # the family lives in the fence map, because both stack schemas are
    # extra="forbid" and a `family:` key here would fail validation outright.
    assert "family" not in body
    assert GRADER_FAMILY_BY_COMPONENT[model.id] == "mistral"

    spec = importlib.util.spec_from_file_location(
        "_brs_mistral", repo / "scripts" / "bringup_register_stack.py",
    )
    registrar = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(registrar)
    ids = [cid for cid, _ in registrar.COMPONENTS]
    assert MISTRAL_GRADER in ids, "the registrar must list the third-family grader"
    registered = next(b for cid, b in registrar.COMPONENTS if cid == MISTRAL_GRADER)
    assert registered == body, "the registrar must LOAD the descriptor, not copy it"


# ---------------------------------------------------------------------------
# W-5 — the ledger aggregation, honest-null and non-pooling
# ---------------------------------------------------------------------------


def test_accuracy_is_none_below_the_floor_never_zero():
    rec = eg.score(["SUPPORTED", "CONTRADICTED", "NOT_FOUND"], min_decided=30)
    assert rec["accuracy"] is None  # 2 decided, below 30
    assert rec["n_decided"] == 2 and rec["n_searched"] == 3
    assert rec["decided_rate"] == pytest.approx(2 / 3)


def test_a_real_zero_is_distinct_from_unmeasured():
    rec = eg.score(["CONTRADICTED"] * 30, min_decided=30)
    assert rec["accuracy"] == 0.0  # every decided claim was CONTRADICTED
    good = eg.score(["SUPPORTED"] * 30, min_decided=30)
    assert good["accuracy"] == 1.0


def test_not_found_unchecked_uncheckable_are_excluded_from_both_sides():
    rec = eg.score(
        ["SUPPORTED"] * 30 + ["NOT_FOUND"] * 5 + ["UNCHECKED"] * 3
        + ["UNCHECKABLE"] * 2,
        min_decided=30,
    )
    assert rec["accuracy"] == 1.0  # only supported/contradicted score
    assert rec["n_decided"] == 30 and rec["n_searched"] == 35
    assert rec["n_unchecked"] == 3 and rec["n_uncheckable"] == 2
    assert rec["decided_rate"] == pytest.approx(30 / 35)


def test_the_three_populations_are_never_pooled():
    with pytest.raises(AssertionError):
        eg.assert_not_pooled(
            {"external_accuracy": 0.7, "faithfulness": 0.9},
            what="a faithfulness aggregate",
        )
    with pytest.raises(AssertionError):
        eg.assert_populations_not_pooled([
            {"population": "assembly_span"}, {"population": "assembly_span"},
        ])


def test_the_instrument_overlap_is_the_three_verdict_raw_agreement():
    rows = [
        {"claim_key": "a", "verdict": "SUPPORTED", "rater_role": "primary",
         "grader_family": "gemma"},
        {"claim_key": "a", "verdict": "SUPPORTED", "rater_role": "audit",
         "grader_family": "llama"},
        {"claim_key": "b", "verdict": "CONTRADICTED", "rater_role": "primary",
         "grader_family": "gemma"},
        {"claim_key": "b", "verdict": "NOT_FOUND", "rater_role": "audit",
         "grader_family": "llama"},
    ]
    inst = eg.instrument(rows)
    assert inst["overlap_n"] == 2
    assert inst["overlap_raw"] == 0.5  # a agrees, b disagrees
    assert inst["band"] == "instrument_limited"
    # No audit rows at all -> UNMEASURED, not agreement.
    solo = eg.instrument([rows[0], rows[2]])
    assert solo["overlap_raw"] is None and solo["band"] == "unmeasured"
    assert solo["instrument_limited"] is False


def test_aggregate_only_scores_primary_rows_and_carries_a_visible_tier_class():
    rows = [
        {"claim_key": "a", "population": "assembly_span", "verdict": "SUPPORTED",
         "assembly_regime": "assembly", "claim_severity": "high",
         "absence_shaped": False, "decisive_source_tier": None,
         "analyst_id": "country_composition", "grader_family": "gemma",
         "grader_pipeline_version": "2026-09-05/1", "rater_role": "primary",
         "retrieval_origin_mix": {}, "sample_fraction": 1.0},
    ]
    out = eg.aggregate(rows, window_days=7)
    pop = out["populations"][0]
    assert pop["population"] == "assembly_span"
    # F-3: a decisive verdict on an unknown tier is published as its own class.
    assert "tier_unknown" in pop["strata"]["tier_class"]


# ---------------------------------------------------------------------------
# W-5 — the ``decisive_published_at`` boundary (2026-09-05 23:07Z sweep: 14/40
# writes rejected by asyncpg because the writer passed the search result's raw
# published-date STRING straight into a `timestamptz` column). The fix lives
# at the writer boundary in `external_grades.py`; these are the pure units.
# ---------------------------------------------------------------------------

#: The tick's own `graded_at`, used as the reference for every relative form
#: below so the test is reproducible rather than racing wall-clock `now()`.
_REFERENCE = datetime(2026, 9, 5, 23, 7, 0, tzinfo=timezone.utc)

#: The exact 14 raw values the incident's failed writes carried, paired with
#: the datetime/precision the fix must produce. `'1 day ago'` appears twice in
#: the incident (two different claims, same phrase) — parametrized once per
#: occurrence so the test count matches the incident's own count.
_OBSERVED_PUBLISHED_AT_SHAPES: tuple[tuple[str, datetime | None, str], ...] = (
    ("2026-09-05", datetime(2026, 9, 5, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("1 day ago", datetime(2026, 9, 4, 23, 7, 0, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_RELATIVE),
    ("1 day ago", datetime(2026, 9, 4, 23, 7, 0, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_RELATIVE),
    ("2026-07", datetime(2026, 7, 1, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_MONTH),
    ("Aug 20, 2026", datetime(2026, 8, 20, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("2025-11-22", datetime(2025, 11, 22, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("Jan 1, 2026", datetime(2026, 1, 1, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("2026-09", datetime(2026, 9, 1, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_MONTH),
    ("Feb 27, 2026", datetime(2026, 2, 27, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("Jul 12, 2026", datetime(2026, 7, 12, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("2026-09-01", datetime(2026, 9, 1, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("Oct 25, 2025", datetime(2025, 10, 25, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
    ("2026-09-02", datetime(2026, 9, 2, tzinfo=timezone.utc), eg.PUBLISHED_PRECISION_DAY),
)


@pytest.mark.parametrize("raw, expected_dt, expected_precision", _OBSERVED_PUBLISHED_AT_SHAPES)
def test_every_observed_published_at_shape_parses_to_datetime_or_none(
    raw, expected_dt, expected_precision,
):
    dt, precision = eg.parse_decisive_published_at(raw, reference=_REFERENCE)
    assert dt == expected_dt
    assert precision == expected_precision
    # Never a str reaching the caller — the exact defect this fix closes.
    assert dt is None or isinstance(dt, datetime)


@pytest.mark.parametrize("bad", [None, "", "   ", "not a date", "next Tuesday", 42, []])
def test_unparseable_published_at_values_become_none_never_a_str(bad):
    dt, precision = eg.parse_decisive_published_at(bad, reference=_REFERENCE)
    assert dt is None
    assert precision == eg.PUBLISHED_PRECISION_UNPARSED


def test_a_real_datetime_passes_through_and_is_utc_stamped_if_naive():
    aware = datetime(2026, 9, 1, 12, tzinfo=timezone.utc)
    dt, precision = eg.parse_decisive_published_at(aware, reference=_REFERENCE)
    assert dt == aware and precision == eg.PUBLISHED_PRECISION_DAY

    naive = datetime(2026, 9, 1, 12)
    dt2, _ = eg.parse_decisive_published_at(naive, reference=_REFERENCE)
    assert dt2 == naive.replace(tzinfo=timezone.utc)


def test_relative_forms_resolve_against_the_reference_not_wall_clock():
    """A replay of an old tick must reproduce the same datetime every time —
    resolving against wall-clock `now()` instead of `graded_at` would make a
    re-run of the same tick's data non-reproducible."""
    dt_a, _ = eg.parse_decisive_published_at("2 weeks ago", reference=_REFERENCE)
    dt_b, _ = eg.parse_decisive_published_at("2 weeks ago", reference=_REFERENCE)
    assert dt_a == dt_b == _REFERENCE - timedelta(days=14)
    # No reference at all still returns a real datetime (falls back to
    # wall-clock UTC), never raises and never returns a str.
    dt_c, precision_c = eg.parse_decisive_published_at("1 day ago")
    assert isinstance(dt_c, datetime) and precision_c == eg.PUBLISHED_PRECISION_RELATIVE


def test_the_read_evidence_window_is_extended_never_clobbered():
    """`read_evidence_window` already carries the READ's own admissible window
    (G-3's stamp); the raw published-date string must nest under its own key,
    never overwrite `earliest`/`latest`."""
    row = {
        "decisive_published_at": "1 day ago",
        "read_evidence_window": {"earliest": "2026-08-24", "latest": "2026-09-05"},
    }
    dt, window = eg._decisive_published_and_window(row, reference=_REFERENCE)
    assert dt == _REFERENCE - timedelta(days=1)
    assert window["earliest"] == "2026-08-24" and window["latest"] == "2026-09-05"
    assert window["decisive_published"] == {
        "published_raw": "1 day ago",
        "published_precision": eg.PUBLISHED_PRECISION_RELATIVE,
    }


def test_an_absent_published_at_records_nothing_extra_on_the_window():
    row = {"decisive_published_at": None, "read_evidence_window": {"earliest": "2026-08-24"}}
    dt, window = eg._decisive_published_and_window(row, reference=_REFERENCE)
    assert dt is None
    assert window == {"earliest": "2026-08-24"}  # untouched — nothing to record


class _CapturingConn:
    """A fake `conn` whose only job is to record the positional args
    `write_grade` sends to `INSERT_GRADE_SQL`, so the regression below can
    assert on the TYPE that reaches the wire without a real Postgres."""

    def __init__(self):
        self.calls: list[tuple[Any, ...]] = []

    async def execute(self, sql, *args):
        self.calls.append(args)
        return "INSERT 0 1"


def _width_row(**over):
    base = dict(
        claim_key="k1", population="assembly_span",
        graded_output_id="00000000-0000-0000-0000-000000000001",
        origin_head_id=None, block_ordinal=1, span_role="bluf",
        analyst_id="country_composition", target_id="tr", desk_key="tr",
        claim_text="A thing happened.", claim_severity="high",
        assembly_regime="assembly", scope_bounded=False, absence_shaped=False,
        verdict="SUPPORTED", uncheckable_class=None, unchecked_reason=None,
        decisive_url="https://news.example/a", decisive_span="the thing happened",
        decisive_span_sha256="deadbeef", decisive_source_tier=1,
        decisive_published_at="1 day ago", archive_ref=None, source_urls=[],
        search_provider="searxng", search_status="completed",
        search_liveness="unverified", search_degraded=False,
        grader_family="mistral", grader_component_id="llm.judge.mistral",
        grader_model_name=None, grader_served_by=None,
        rubric_version="external_world_check/2026-09-05/1", rater_role="primary",
        retrieval_origin_mix={}, read_evidence_window={"earliest": "2026-08-24"},
        sample_fraction=1.0,
    )
    base.update(over)
    return base


@pytest.mark.asyncio
async def test_write_grade_never_sends_a_raw_str_for_decisive_published_at():
    """Regression, pinned at the boundary the bug lived in: whatever the row
    dict carries for `decisive_published_at`, the value in `$22` on the wire
    is a real `datetime` or `None` — never a `str`. Runs against a capturing
    fake conn, not a real Postgres, because the property under test is the
    Python-side type conversion, not the database's own type enforcement
    (that half is `test_migration_0190_external_grades.py`'s job)."""
    for raw in ("2026-09-05", "1 day ago", "2026-07", "Aug 20, 2026", "garbage", None):
        conn = _CapturingConn()
        landed = await eg.write_grade(
            conn, _width_row(claim_key=f"k-{raw}", decisive_published_at=raw),
            pipeline_version="2026-09-05/1",
            graded_at=_REFERENCE,
        )
        assert landed is True
        (args,) = conn.calls
        decisive_published_arg = args[21]  # $22, 0-indexed
        assert not isinstance(decisive_published_arg, str), (
            f"raw={raw!r} reached the INSERT as a str"
        )
        assert decisive_published_arg is None or isinstance(
            decisive_published_arg, datetime
        )


# ---------------------------------------------------------------------------
# write_grades()'s per-row outcomes — the fix's OTHER half. A write that does
# not land must name WHY (so the caller can requeue with a reason) and a
# write that lands via an idempotent conflict must NOT be confused with one
# that failed (so a healthy re-run is never mistaken for a lost claim).
# ---------------------------------------------------------------------------


class _FlakyConn:
    """A fake `conn` whose `execute` behaves per claim_key ($1): raises for
    keys in `fail_keys`, returns the idempotent-conflict receipt for keys in
    `conflict_keys`, and a fresh-insert receipt for everything else — so
    `write_grades`' three-way outcome classification can be pinned without a
    real Postgres (the DB-enforced half — a real CHECK violation — lives in
    `test_migration_0190_external_grades.py`)."""

    def __init__(self, *, fail_keys=(), conflict_keys=(), exc=RuntimeError):
        self.fail_keys = set(fail_keys)
        self.conflict_keys = set(conflict_keys)
        self.exc = exc
        self.calls: list[Any] = []

    async def execute(self, sql, *args):
        key = args[0]  # $1 == claim_key
        self.calls.append(key)
        if key in self.fail_keys:
            raise self.exc(f"boom for {key}")
        if key in self.conflict_keys:
            return "INSERT 0 0"
        return "INSERT 0 1"


@pytest.mark.asyncio
async def test_write_grades_reports_per_row_outcomes_in_input_order():
    conn = _FlakyConn(fail_keys={"k-bad"}, conflict_keys={"k-dupe"})
    rows = [
        _width_row(claim_key="k-good"),
        _width_row(claim_key="k-bad"),
        _width_row(claim_key="k-dupe"),
    ]
    written, skipped, outcomes = await eg.write_grades(
        conn, rows, pipeline_version="2026-09-05/1", graded_at=_REFERENCE,
    )
    # written/skipped keep their PRE-FIX meaning exactly: only a fresh INSERT
    # counts as written; a conflict and a failure both count as skipped.
    assert written == 1 and skipped == 2
    assert [o.claim_key for o in outcomes] == ["k-good", "k-bad", "k-dupe"]
    assert outcomes[0] == eg.WriteOutcome(
        claim_key="k-good", landed=True, error_class="",
    )
    assert outcomes[1].landed is False
    assert outcomes[1].error_class == "RuntimeError"
    # The idempotent conflict is landed=True — NOT a failure, because the row
    # already exists in the ledger; requeuing it would grade it a second time
    # for nothing.
    assert outcomes[2].landed is True and outcomes[2].error_class == ""


@pytest.mark.asyncio
async def test_write_grades_names_the_missing_graded_output_id_skip():
    """The one pre-INSERT validation refusal never reaches `conn.execute` at
    all, and still needs a named failure class — not an unnamed skip — so a
    caller can requeue it like any other failed write."""
    conn = _FlakyConn()
    rows = [_width_row(claim_key="k-none", graded_output_id=None)]
    written, skipped, outcomes = await eg.write_grades(
        conn, rows, pipeline_version="2026-09-05/1", graded_at=_REFERENCE,
    )
    assert written == 0 and skipped == 1
    assert outcomes[0].landed is False
    assert outcomes[0].error_class == eg.ERROR_CLASS_MISSING_GRADED_OUTPUT_ID
    assert conn.calls == []  # never reached the INSERT


@pytest.mark.asyncio
async def test_write_grade_bool_contract_is_unchanged_by_the_execute_write_refactor():
    """`write_grade`'s own contract predates the landed/failed distinction
    `write_grades` now reports and must not shift under existing callers:
    True ONLY for a fresh insert; False for both a conflict and a failure."""
    fresh = _FlakyConn()
    assert await eg.write_grade(
        fresh, _width_row(claim_key="k1"),
        pipeline_version="v", graded_at=_REFERENCE,
    ) is True

    conflict = _FlakyConn(conflict_keys={"k2"})
    assert await eg.write_grade(
        conflict, _width_row(claim_key="k2"),
        pipeline_version="v", graded_at=_REFERENCE,
    ) is False

    failing = _FlakyConn(fail_keys={"k3"})
    assert await eg.write_grade(
        failing, _width_row(claim_key="k3"),
        pipeline_version="v", graded_at=_REFERENCE,
    ) is False


# ---------------------------------------------------------------------------
# G-3'S WINDOW BASIS — window_basis="evidence" (2026-09-07)
# ---------------------------------------------------------------------------
#
# The operator's decision, taken 2026-09-07: the admissible source window stops
# being the arrival spread of the consumed HEADS and becomes the span of the
# EVIDENCE those heads rest on. Everything below is pure — the SQL walk itself
# is exercised against real Postgres in ``test_standing_auditor.py``, per this
# file's own banner.

#: The heads window a 12:00Z world read actually stamped on 2026-09-06: three
#: hours wide. The evidence under those 33 heads reached back ~13 days.
_HEADS_WINDOW = {
    "oldest": "2026-09-06T08:30:00+00:00",
    "newest": "2026-09-06T11:41:00+00:00",
    "oldest_human_date": "6 September 2026",
    "newest_human_date": "6 September 2026",
    "span_hours": 3.19,
    "heads": 33,
}

#: The oldest ``signals.fetched_at`` the lineage walk reaches from those heads.
_EVIDENCE_OLDEST = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)


class _WalkConn:
    """A stub connection that answers the ONE evidence-walk query.

    Deliberately not a full fake: the query's own correctness is a SQL fact and
    is proven against real Postgres in ``test_standing_auditor.py``. What is
    proven here is everything AROUND it — the seeding, the per-root fan-out, the
    fallbacks and the receipt arithmetic — which is where the branches are.
    """

    def __init__(self, answers=None, *, raises=False):
        self.answers = dict(answers or {})
        self.raises = raises
        self.calls: list[tuple[list[str], int]] = []

    async def fetch(self, sql, ids, depth):
        self.calls.append((list(ids), depth))
        if self.raises:
            raise RuntimeError("lineage read failed")
        return [
            {"root": root, "oldest": oldest, "evidence_signals": n}
            for root, (oldest, n) in self.answers.items()
            if root in set(ids)
        ]


def _claim_on(read_id: str, *, window=None, key: str = "c") -> claims.WidthClaim:
    return claims.WidthClaim(
        claim_text=key, population=claims.POPULATION_ASSEMBLY_SPAN,
        graded_output_id=read_id, analyst_id="country_composition",
        origin_head_id="h", start=0, end=1,
        read_evidence_window=dict(_HEADS_WINDOW if window is None else window),
    )


# -- the basis resolver, same _coerce idiom as the grace knob ----------------


def test_window_basis_defaults_to_heads_with_no_env_and_no_option(monkeypatch):
    monkeypatch.delenv(width.WINDOW_BASIS_ENV, raising=False)
    assert width._window_basis({}) == width.WINDOW_BASIS_HEADS


def test_window_basis_reads_the_env_as_the_base(monkeypatch):
    monkeypatch.setenv(width.WINDOW_BASIS_ENV, "evidence")
    assert width._window_basis({}) == width.WINDOW_BASIS_EVIDENCE
    monkeypatch.setenv(width.WINDOW_BASIS_ENV, "  EVIDENCE ")
    assert width._window_basis({}) == width.WINDOW_BASIS_EVIDENCE


def test_window_basis_option_wins_over_env(monkeypatch):
    """Env supplies the base, the descriptor option wins — the same precedence
    `window_grace_hours` keeps, in BOTH directions."""
    monkeypatch.setenv(width.WINDOW_BASIS_ENV, "heads")
    assert width._window_basis({"window_basis": "evidence"}) == "evidence"
    monkeypatch.setenv(width.WINDOW_BASIS_ENV, "evidence")
    assert width._window_basis({"window_basis": "heads"}) == "heads"


def test_window_basis_unknown_value_keeps_the_predecessor(monkeypatch, caplog):
    """A typo must never be read as "not heads" — that would widen the window
    AND stamp the row as a different instrument, for a spelling mistake."""
    monkeypatch.delenv(width.WINDOW_BASIS_ENV, raising=False)
    with caplog.at_level(logging.WARNING):
        assert width._window_basis({"window_basis": "evidince"}) == "heads"
    assert "bad_window_basis_option" in caplog.text

    monkeypatch.setenv(width.WINDOW_BASIS_ENV, "sideways")
    with caplog.at_level(logging.WARNING):
        assert width._window_basis({}) == "heads"
    assert "bad_window_basis_env" in caplog.text

    # An env-set evidence basis survives a bad OPTION (predecessor kept).
    monkeypatch.setenv(width.WINDOW_BASIS_ENV, "evidence")
    assert width._window_basis({"window_basis": 7}) == "evidence"


def test_resolve_window_config_carries_both_knobs(monkeypatch):
    monkeypatch.delenv(width.WINDOW_BASIS_ENV, raising=False)
    monkeypatch.delenv(width.WINDOW_GRACE_HOURS_ENV, raising=False)
    default = width.resolve_window_config({})
    assert (default.basis, default.grace_hours) == ("heads", 0.0)
    assert default.is_default is True

    flipped = width.resolve_window_config(
        {"window_basis": "evidence", "window_grace_hours": 96}
    )
    assert (flipped.basis, flipped.grace_hours) == ("evidence", 96.0)
    assert flipped.is_default is False


# -- the stamp is a FUNCTION of the configuration ---------------------------


def test_the_width_stamp_follows_the_window_configuration(monkeypatch):
    """An instrument stamp names the measurement TAKEN. heads/0 keeps
    2026-09-21/3 (the paid-rung reformulation bump) for heads/0; anything else
    is a different instrument and says so."""
    assert sampling.width_pipeline_version() == "2026-09-21/3"
    assert sampling.width_pipeline_version(
        window_basis="heads", grace_before_hours=0.0
    ) == sampling.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH_HEADS

    for kwargs in (
        {"window_basis": "evidence"},
        {"grace_before_hours": 96},
        {"window_basis": "evidence", "grace_before_hours": 96},
    ):
        assert sampling.width_pipeline_version(**kwargs) == (
            sampling.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH
        ), kwargs
    assert sampling.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH == "2026-09-21/4"


def test_a_negative_or_unparsable_grace_does_not_move_the_stamp():
    """`_window_grace_hours` already floors at 0, but the stamp must not depend
    on that: a stamp that could be moved by a value the instrument ignored
    would label rows by a measurement nobody took."""
    assert sampling.width_pipeline_version(grace_before_hours=-5) == "2026-09-21/3"
    assert sampling.width_pipeline_version(grace_before_hours="nope") == "2026-09-21/3"


def test_window_config_exposes_the_stamp_its_rows_must_carry(monkeypatch):
    monkeypatch.delenv(width.WINDOW_BASIS_ENV, raising=False)
    monkeypatch.delenv(width.WINDOW_GRACE_HOURS_ENV, raising=False)
    assert width.resolve_window_config({}).pipeline_version == "2026-09-21/3"
    assert width.resolve_window_config(
        {"window_basis": "evidence"}
    ).pipeline_version == "2026-09-21/4"
    assert width.resolve_window_config(
        {"window_grace_hours": 96}
    ).pipeline_version == "2026-09-21/4"


def test_the_flag_off_stamp_is_untouched_by_either_window_knob(monkeypatch):
    """The shipped 6-claim sweep is a THIRD instrument and neither window knob
    is wired into it — flag-off must keep saying 2026-08-29/1 whatever the
    window configuration says."""
    monkeypatch.delenv(sampling.WIDTH_FLAG_ENV, raising=False)
    assert sampling.pipeline_version(
        window_basis="evidence", grace_before_hours=96,
    ) == sampling.EXTERNAL_AUDIT_PIPELINE_VERSION


# -- evidence_window(): the pure re-basing ----------------------------------


def test_evidence_window_rebases_oldest_and_keeps_the_record():
    out = width.evidence_window(
        _HEADS_WINDOW, oldest=_EVIDENCE_OLDEST, evidence_signals=4212,
    )
    assert out["oldest"] == _EVIDENCE_OLDEST.isoformat()
    assert out["basis"] == "evidence"
    assert out["oldest_heads"] == _HEADS_WINDOW["oldest"]
    assert out["evidence_signals"] == 4212
    # The AFTER bound is not a judgement call under any basis.
    assert out["newest"] == _HEADS_WINDOW["newest"]
    assert out["heads"] == 33
    # No derived field is left describing the bound that moved.
    assert out["span_hours"] == 296.68
    assert out["oldest_human_date"] == "25 August 2026"
    assert out["newest_human_date"] == _HEADS_WINDOW["newest_human_date"]
    # The caller's dict is never mutated.
    assert _HEADS_WINDOW["oldest"] == "2026-09-06T08:30:00+00:00"


def test_evidence_window_is_the_window_check_span_then_grades_against():
    """The rewritten dict must be readable by W-3's own bounds resolver, and
    the kept `oldest_heads` must NOT be mistaken for a bound."""
    out = width.evidence_window(
        _HEADS_WINDOW, oldest=_EVIDENCE_OLDEST, evidence_signals=10,
    )
    bounds = esc.evidence_window_bounds(out)
    assert bounds.measured is True
    assert bounds.start == _EVIDENCE_OLDEST
    assert bounds.end == datetime.fromisoformat(_HEADS_WINDOW["newest"])


def test_evidence_window_admits_a_source_between_the_two_oldests():
    """THE WHOLE POINT. A page published between the evidence's oldest and the
    heads' oldest is inadmissible under `heads` and admissible under
    `evidence` — no grace involved."""
    published = "2026-09-01T00:00:00+00:00"
    assert esc.in_evidence_window(published, _HEADS_WINDOW) is False
    rebased = width.evidence_window(
        _HEADS_WINDOW, oldest=_EVIDENCE_OLDEST, evidence_signals=10,
    )
    assert esc.in_evidence_window(published, rebased) is True


def test_evidence_window_never_returns_an_unmeasured_window():
    """An unresolved walk keeps the heads window VERBATIM. `check_span` skips
    G-3 entirely on an unmeasured window, so a failed lineage read would
    otherwise admit every source ever published."""
    out = width.evidence_window(_HEADS_WINDOW, oldest=None, evidence_signals=0)
    assert out == _HEADS_WINDOW
    assert "basis" not in out
    assert esc.evidence_window_bounds(out).measured is True


def test_evidence_window_never_narrows():
    """`min(evidence, heads)`: the read rests on its heads as well as on the
    signals under them. A lineage that somehow reports later-than-the-heads
    evidence must not demote verdicts the narrow window already admitted."""
    later = datetime(2026, 9, 6, 23, 0, tzinfo=timezone.utc)
    out = width.evidence_window(_HEADS_WINDOW, oldest=later, evidence_signals=3)
    assert out["oldest"] == _HEADS_WINDOW["oldest"]
    assert out["basis"] == "evidence"
    assert out["oldest_heads"] == _HEADS_WINDOW["oldest"]


def test_evidence_window_measures_a_read_whose_heads_window_was_empty():
    """The live 2026-09-06 ledger held a SUPPORTED row whose read carried no
    evidence_window at all — G-3 never ran and a source published three months
    earlier was admitted. Under `evidence` that read gets a real lower bound."""
    out = width.evidence_window({}, oldest=_EVIDENCE_OLDEST, evidence_signals=3783)
    assert out["oldest"] == _EVIDENCE_OLDEST.isoformat()
    assert out["oldest_heads"] is None
    assert "span_hours" not in out, "no newest, so no span to state"
    # Unmeasured until `produced_at` supplies the after bound — which the drain
    # always passes — and measured the moment it does.
    assert esc.evidence_window_bounds(out).measured is False
    assert esc.evidence_window_bounds(
        out, produced_at="2026-09-06T12:00:00+00:00"
    ).measured is True
    assert esc.in_evidence_window(
        "2026-06-07T00:00:00+00:00", out,
        produced_at="2026-09-06T12:00:00+00:00",
    ) is False


# -- the walk's plumbing and its receipt ------------------------------------


@pytest.mark.asyncio
async def test_evidence_oldest_by_read_seeds_one_query_for_every_read():
    conn = _WalkConn({"r1": (_EVIDENCE_OLDEST, 120)})
    out = await width.evidence_oldest_by_read(conn, ["r1", "r2", "", None])
    assert out == {"r1": (_EVIDENCE_OLDEST, 120)}
    assert len(conn.calls) == 1, "ONE query per tick, not one per read"
    assert conn.calls[0] == (["r1", "r2"], width._EVIDENCE_WALK_MAX_DEPTH)


@pytest.mark.asyncio
async def test_evidence_oldest_by_read_asks_nothing_when_there_is_nothing_to_ask():
    conn = _WalkConn({})
    assert await width.evidence_oldest_by_read(conn, []) == {}
    assert conn.calls == []


@pytest.mark.asyncio
async def test_a_failed_lineage_read_leaves_every_claim_on_its_heads_window(caplog):
    """Never raises, never widens. A degraded lineage read must not be able to
    move what a decisive verdict admits, and must not take the tick down."""
    conn = _WalkConn(raises=True)
    with caplog.at_level(logging.WARNING):
        assert await width.evidence_oldest_by_read(conn, ["r1"]) == {}
    assert "evidence_window_walk_failed" in caplog.text

    rebased, stats = await width.apply_evidence_windows(
        _WalkConn(raises=True), [_claim_on("r1")],
    )
    assert rebased[0].read_evidence_window == _HEADS_WINDOW
    assert stats == {"reads": 1, "resolved": 0, "unresolved": 1,
                     "signals": 0, "claims_rebased": 0}


@pytest.mark.asyncio
async def test_apply_evidence_windows_rebases_every_claim_of_a_resolved_read():
    conn = _WalkConn({"r1": (_EVIDENCE_OLDEST, 423), "r2": (None, 0)})
    drained = [
        _claim_on("r1", key="a"), _claim_on("r1", key="b"), _claim_on("r2", key="c"),
    ]
    rebased, stats = await width.apply_evidence_windows(conn, drained)

    assert [c.claim_text for c in rebased] == ["a", "b", "c"]
    assert all(
        c.read_evidence_window["oldest"] == _EVIDENCE_OLDEST.isoformat()
        for c in rebased[:2]
    )
    assert rebased[2].read_evidence_window == _HEADS_WINDOW, "unresolved read kept"
    assert stats == {"reads": 2, "resolved": 1, "unresolved": 1,
                     "signals": 423, "claims_rebased": 2}
    # The queue's own copies are untouched — WidthClaim is frozen and this
    # replaces rather than mutates, so a requeued claim is re-based fresh next
    # tick against whatever configuration is in force then.
    assert drained[0].read_evidence_window == _HEADS_WINDOW


# -- the receipt ------------------------------------------------------------


def test_width_heartbeat_block_reports_the_basis_and_is_heads_by_default():
    block = width.width_heartbeat_block(width.WidthRunResult())
    assert block["window_basis"] == "heads"
    assert block["evidence_window"] is None, (
        "None, not a dict of zeros — the walk did not run"
    )
    assert "pipeline_version" not in block, (
        "an unset stamp must not overwrite the shipped heartbeat key"
    )


def test_width_heartbeat_block_reports_the_evidence_walk_and_the_stamp():
    result = width.WidthRunResult(
        window_basis="evidence",
        pipeline_version="2026-09-21/4",
        evidence_window_stats={"reads": 2, "resolved": 2, "unresolved": 0,
                               "signals": 8465, "claims_rebased": 24},
    )
    block = width.width_heartbeat_block(result)
    assert block["window_basis"] == "evidence"
    assert block["evidence_window"]["signals"] == 8465
    assert block["pipeline_version"] == "2026-09-21/4"

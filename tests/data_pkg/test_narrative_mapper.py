# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-1 + P4-2 — the ``narrative_mapper`` deterministic analyst (A11).

Pure tests (no DB): the registry wiring + structural-exempt badge, refuse-loud,
published_at parsing, narrative reification (a contested family -> carrier
sources + per-source echo lags + variants), the echo-graph lead/follow
computation (co-carriage, window, systematic flag, directional asymmetry), the
publish-dated-only honesty rule (fetch-time-only carriage NEVER mints an echo
edge), and the zero-state summary honesty. Ephemeral-DB coverage lives in
``test_narrative_mapper_db.py``.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts.deterministic_handlers import narrative_mapper as nm
from legba.data.provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from legba.runtime.analyst_method import AnalystMethodResult

NOW = datetime(2026, 7, 24, 12, tzinfo=timezone.utc)
T0 = datetime(2026, 7, 1, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_finding_sub_handler_and_structural_exempt():
    """The summary is a genuine FINDING (the measurement product), so the
    handler sits in the STRUCTURAL_VERIFY_EXEMPT registry and the drift guard's
    FINDING-set equality holds."""
    assert SUB_HANDLERS["narrative_mapper"] is nm.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["narrative_mapper"] is OutputKind.FINDING
    assert "narrative_mapper" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await nm.handle([], {"sub_handler": "narrative_mapper"}, None)


# ---------------------------------------------------------------------------
# published_at parsing
# ---------------------------------------------------------------------------


def test_parse_published_at_variants():
    assert nm.parse_published_at("2026-07-01T00:00:00+00:00") == T0
    # Trailing Z honored.
    assert nm.parse_published_at("2026-07-01T00:00:00Z") == T0
    # Naive assumed UTC.
    assert nm.parse_published_at("2026-07-01T00:00:00") == T0
    # Junk / absent -> None (never a fabricated time).
    assert nm.parse_published_at(None) is None
    assert nm.parse_published_at("") is None
    assert nm.parse_published_at("not-a-date") is None
    assert nm.parse_published_at(12345) is None


def test_effective_ts_prefers_published_falls_back_to_fetched():
    fetched = T0 + timedelta(hours=5)
    ts, dated = nm.effective_ts("2026-07-01T00:00:00Z", fetched)
    assert ts == T0 and dated is True
    ts2, dated2 = nm.effective_ts(None, fetched)
    assert ts2 == fetched and dated2 is False


# ---------------------------------------------------------------------------
# Reification helpers
# ---------------------------------------------------------------------------


def _meta(cid: str, subject: str, predicate: str, status: str = "contested",
          surfaced_value: str | None = None) -> dict:
    return {
        "contention_id": cid,
        "subject_key": subject,
        "predicate_key": predicate,
        "status": status,
        "surfaced_value": surfaced_value,
        "opened_at": T0,
        "surfaced_at": None,
    }


def _member(cid: str, value_key: str, is_winner: bool, source_id: str,
            *, dsc: int = 1, published_at, fetched_at=NOW,
            fact_id: str | None = None, signal_id: str | None = None) -> dict:
    fact_id = fact_id or f"fact-{source_id}-{value_key}"
    signal_id = signal_id or f"sig-{source_id}-{value_key}"
    return {
        "contention_id": cid,
        "value_key": value_key,
        "is_winner": is_winner,
        "distinct_source_count": dsc,
        "fact_id": fact_id,
        "signal_id": signal_id,
        "source_id": source_id,
        "published_at": published_at,
        "fetched_at": fetched_at,
    }


CID1 = "11111111-1111-1111-1111-111111111111"


def test_reify_contested_family_carriers_and_lags():
    """A contested family -> carrier sources, the publish-dated lead, per-source
    echo lag vs the lead, and the winner/loser variants."""
    meta = [_meta(CID1, "gaza", "ceasefire", surfaced_value="holding")]
    members = [
        _member(CID1, "holding", True, "A", dsc=2, published_at=T0.isoformat()),
        _member(CID1, "holding", True, "B", dsc=2,
                published_at=(T0 + timedelta(hours=2)).isoformat()),
        _member(CID1, "collapsed", False, "C", dsc=1,
                published_at=(T0 + timedelta(hours=50)).isoformat()),
    ]
    [nar] = nm.reify_narratives(meta, members, {"A": "Alpha", "B": "Bravo"}, now=NOW)

    assert nar.contention_id == CID1
    assert nar.carrier_source_count == 3
    assert nar.publish_dated_source_count == 3
    assert nar.variant_count == 2
    # A published first -> the lead.
    assert nar.lead_source_id == "A"
    assert nar.lead_first_seen_at == T0
    assert nar.first_seen_at == T0
    assert nar.last_seen_at == T0 + timedelta(hours=50)
    assert nar.span_hours == pytest.approx(50.0)
    assert nar.max_echo_lag_hours == pytest.approx(50.0)

    by_src = {c["source_id"]: c for c in nar.carriers}
    assert by_src["A"]["role"] == "lead" and by_src["A"]["echo_lag_hours"] == 0.0
    assert by_src["B"]["role"] == "echo" and by_src["B"]["echo_lag_hours"] == pytest.approx(2.0)
    assert by_src["C"]["role"] == "echo" and by_src["C"]["echo_lag_hours"] == pytest.approx(50.0)
    assert by_src["A"]["source_name"] == "Alpha"
    assert by_src["A"]["on_winning_side"] is True
    assert by_src["C"]["on_winning_side"] is False
    # Winner variant sorts first.
    assert nar.variants[0]["value_key"] == "holding"
    assert nar.variants[0]["is_winner"] is True


def test_reify_fetch_only_carriage_has_no_lead():
    """Carriers with NO published_at are datable via fetched_at (first/last-seen
    populated) but yield no publish-dated lead — the honest 'we cannot say who
    published first' state."""
    meta = [_meta(CID1, "syria", "govt control")]
    members = [
        _member(CID1, "consolidating", True, "A", published_at=None,
                fetched_at=T0),
        _member(CID1, "fragmenting", False, "B", published_at=None,
                fetched_at=T0 + timedelta(hours=3)),
    ]
    [nar] = nm.reify_narratives(meta, members, now=NOW)
    assert nar.carrier_source_count == 2
    assert nar.publish_dated_source_count == 0
    assert nar.lead_source_id is None
    assert nar.first_seen_at == T0            # effective (fetched) still datable
    assert nar.pub_first == {}                # nothing feeds the echo graph
    for c in nar.carriers:
        assert c["publish_dated"] is False
        assert c["echo_lag_hours"] is None


# ---------------------------------------------------------------------------
# Echo graph — lead/follow, window, systematic, asymmetry, honesty
# ---------------------------------------------------------------------------


def _nar_with_pub(cid: str, pub: dict[str, datetime]) -> nm.Narrative:
    """A minimal Narrative carrying only the publish-dated map the echo builder
    reads (isolates build_echo_edges from reification)."""
    return nm.Narrative(
        contention_id=cid, subject_key="s", predicate_key="p", status="contested",
        surfaced_value=None, variant_count=1, carrier_source_count=len(pub),
        publish_dated_source_count=len(pub), signal_count=len(pub), fact_count=len(pub),
        first_seen_at=None, last_seen_at=None, span_hours=None, lead_source_id=None,
        lead_first_seen_at=None, max_echo_lag_hours=None, carriers=[], variants=[],
        opened_at=None, contention_surfaced_at=None, computed_at=NOW, pub_first=pub,
    )


def test_echo_lead_follow_and_systematic():
    """A leads B within-window across 3 shared narratives -> A->B systematic;
    the reverse edge B->A is not stored (B never led)."""
    nars = [
        _nar_with_pub(f"c{i}", {"A": T0 + timedelta(days=i),
                                "B": T0 + timedelta(days=i, hours=2)})
        for i in range(3)
    ]
    edges = nm.build_echo_edges(
        nars, window_hours=48, min_co_carriage=2, systematic_floor=3,
        ratio_floor=0.6, now=NOW,
    )
    by = {(e.leader_source_id, e.follower_source_id): e for e in edges}
    assert ("A", "B") in by
    ab = by[("A", "B")]
    assert ab.co_carried == 3 and ab.lead_count == 3 and ab.follow_within_count == 3
    assert ab.echo_ratio == pytest.approx(1.0)
    assert ab.median_lag_hours == pytest.approx(2.0)
    assert ab.systematic is True
    # B never led A -> no reverse edge stored (lead_count 0).
    assert ("B", "A") not in by


def test_echo_window_excludes_late_follower():
    """A leads C but always OUTSIDE the 48h window -> the edge exists (A led)
    but follow_within/echo_ratio are 0 and it is NOT systematic."""
    nars = [
        _nar_with_pub(f"c{i}", {"A": T0 + timedelta(days=i),
                                "C": T0 + timedelta(days=i, hours=50)})
        for i in range(2)
    ]
    edges = nm.build_echo_edges(
        nars, window_hours=48, min_co_carriage=2, systematic_floor=2,
        ratio_floor=0.6, now=NOW,
    )
    by = {(e.leader_source_id, e.follower_source_id): e for e in edges}
    ac = by[("A", "C")]
    assert ac.co_carried == 2 and ac.lead_count == 2
    assert ac.follow_within_count == 0
    assert ac.echo_ratio == pytest.approx(0.0)
    assert ac.systematic is False


def test_echo_min_co_carriage_floor():
    """A single shared narrative is below the co-carriage floor -> no edge."""
    nars = [_nar_with_pub("c0", {"A": T0, "B": T0 + timedelta(hours=1)})]
    edges = nm.build_echo_edges(nars, window_hours=48, min_co_carriage=2, now=NOW)
    assert edges == []


def test_echo_ignores_fetch_only_narratives():
    """A narrative with no publish-dated carriers (empty pub_first) contributes
    NOTHING to the echo graph — the fetch-order-is-not-publish-order rule."""
    nars = [_nar_with_pub(f"c{i}", {}) for i in range(5)]
    assert nm.build_echo_edges(nars, min_co_carriage=1, now=NOW) == []


def test_echo_tie_publish_times_no_lead():
    """Equal publish times co-carry but determine no lead (order undetermined)."""
    nars = [_nar_with_pub(f"c{i}", {"A": T0, "B": T0}) for i in range(3)]
    edges = nm.build_echo_edges(nars, window_hours=48, min_co_carriage=2, now=NOW)
    # co_carried accrues but lead_count is 0 for both directions -> no edge.
    assert edges == []


# ---------------------------------------------------------------------------
# Summary honesty
# ---------------------------------------------------------------------------


def test_summary_zero_state_is_honest():
    finding = nm.build_summary(
        [], [], window_hours=48, min_co_carriage=2, systematic_floor=3,
        ratio_floor=0.6,
    )
    assert "0 contested-claim families" in finding.title
    assert finding.confidence == 1.0
    assert finding.data["detect_only"] is True
    assert finding.data["descriptive_not_causal"] is True
    assert finding.data["narratives_total"] == 0
    assert "not a causal or coordination claim" in finding.data["honesty_note"].lower()


def test_summary_populated_reports_counts_and_honesty():
    meta = [_meta(CID1, "gaza", "ceasefire")]
    members = [
        _member(CID1, "holding", True, "A", published_at=T0.isoformat()),
        _member(CID1, "holding", True, "B",
                published_at=(T0 + timedelta(hours=2)).isoformat()),
    ]
    nars = nm.reify_narratives(meta, members, now=NOW)
    edges = nm.build_echo_edges(nars, min_co_carriage=1, now=NOW)
    finding = nm.build_summary(
        nars, edges, window_hours=48, min_co_carriage=1, systematic_floor=3,
        ratio_floor=0.6,
    )
    assert finding.data["narratives_total"] == 1
    assert isinstance(finding, type(nm.build_summary([], [], window_hours=1,
                      min_co_carriage=1, systematic_floor=1, ratio_floor=0.5)))
    assert "narrative" in finding.title.lower()

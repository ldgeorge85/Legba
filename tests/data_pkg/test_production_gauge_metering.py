# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The two METERING gauges (#21 headroom / #22 spend, 2026-08-15).

llm_latency exists because the timeout cliff announces itself: the primary
component's client timeout is 240s, and a fleet p95 crossing HALF that budget
is visible hours before the first analyst run dies at the full 240 — measured
live on 2026-08-15 the 24h p95 was 102.4s, under the bar and not by much.
Its second leg exists because ``finish_reason='length'`` means the server
truncated a finding mid-sentence inside a run that reported success.

llm_daily_burn exists because quota death is blind: every hosted PAYG call's
receipt carried ``cost_estimate_usd: 0.0`` (the vllm handler's price table is
empty — correct self-hosted, silently wrong for Cerebras), and the 2026-08-03
judge outage was a ``402 payment_required`` nothing had been counting toward.

The load-bearing assertions: a p95 over the ceiling PAGES; ONE truncated
completion PAGES regardless of sample size; a component crossing its own
daily ceiling PAGES; and money moving on an un-thresholded component is
VISIBLE but never a page.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from legba.data.analysts.handler_options import HANDLER_OPTIONS
from legba.data.registry.production_gauge import (
    LOOP_CLASSES,
    LOOP_LLM_DAILY_BURN,
    LOOP_LLM_LATENCY,
    GaugeConfig,
)
from legba.data.registry.production_gauge_metering import (
    LATENCY_COMPONENT,
    llm_burn_gauges,
    llm_latency_gauge,
    read_llm_burn_loops,
    read_llm_latency_loops,
)

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
CFG = GaugeConfig()


# ---------------------------------------------------------------------------
# llm_latency — the p95 leg
# ---------------------------------------------------------------------------


def test_p95_over_the_ceiling_is_a_deficit_and_pages():
    """THE #21 assertion: the plane burning half its timeout budget per
    typical-worst call must go red BEFORE budgets rise onto it."""
    gauge = llm_latency_gauge(
        {"calls": 60, "p95_ms": 150_000.0, "max_ms": 200_000.0, "length_hits": 0},
        now=NOW,
        cfg=CFG,
    )
    assert gauge.state == "deficit"
    assert gauge.pages is True
    assert gauge.severity == "medium"
    assert "150.0s" in gauge.actual


def test_p95_at_the_full_timeout_is_high():
    gauge = llm_latency_gauge(
        {"calls": 60, "p95_ms": 240_000.0, "length_hits": 0}, now=NOW, cfg=CFG
    )
    assert gauge.state == "deficit"
    assert gauge.severity == "high"


def test_p95_under_the_ceiling_is_ok_and_the_ratio_is_honest():
    """Measured live 2026-08-15: 24h p95 102.4s. Under the bar, and the ratio
    says how close — 0.85, not a rounded-away zero."""
    gauge = llm_latency_gauge(
        {"calls": 1527, "p95_ms": 102_420.0, "length_hits": 0}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ok"
    assert gauge.pages is False
    assert gauge.ratio == pytest.approx(0.854, abs=0.001)


# ---------------------------------------------------------------------------
# llm_latency — the truncation leg
# ---------------------------------------------------------------------------


def test_one_truncated_completion_pages_at_any_sample_size():
    """The min-calls floor guards the PERCENTILE; a finish_reason='length'
    receipt is a discrete defect — a finding the server cut off inside a run
    that reported success — and one is enough."""
    gauge = llm_latency_gauge(
        {"calls": 2, "p95_ms": 5_000.0, "length_hits": 1}, now=NOW, cfg=CFG
    )
    assert gauge.state == "deficit"
    assert gauge.pages is True
    assert gauge.severity == "medium"
    assert "truncated" in gauge.actual


def test_truncations_escalate_on_the_shared_ramp():
    gauge = llm_latency_gauge(
        {"calls": 100, "p95_ms": 5_000.0, "length_hits": 4}, now=NOW, cfg=CFG
    )
    assert gauge.severity == "critical"


def test_both_legs_report_together():
    gauge = llm_latency_gauge(
        {"calls": 60, "p95_ms": 150_000.0, "length_hits": 2}, now=NOW, cfg=CFG
    )
    assert gauge.state == "deficit"
    assert "p95" in gauge.actual and "truncated" in gauge.actual
    # The worse leg drives severity: 2 truncations (2.0) > p95 ratio (1.25).
    assert gauge.ratio == pytest.approx(2.0)
    assert gauge.severity == "high"


# ---------------------------------------------------------------------------
# llm_latency — quiet-by-design
# ---------------------------------------------------------------------------


def test_no_calls_defers_to_the_silence_alarm():
    """A quiet hour is host_llm_heartbeat's condition. One fault, one page."""
    gauge = llm_latency_gauge({"calls": 0}, now=NOW, cfg=CFG)
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "no_calls_in_window"


def test_a_few_slow_calls_are_an_anecdote_not_a_percentile():
    gauge = llm_latency_gauge(
        {"calls": 3, "p95_ms": 200_000.0, "length_hits": 0}, now=NOW, cfg=CFG
    )
    assert gauge.state == "ungauged"
    assert gauge.quiet_reason == "insufficient_history"


async def test_latency_read_degrades_loud():
    class _Boom:
        async def fetchrow(self, *a, **k):
            raise RuntimeError("connection reset")

    loops = await read_llm_latency_loops(_Boom(), now=NOW, cfg=CFG)
    assert len(loops) == 1
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == "latency_query_failed"
    assert "connection reset" in loops[0].evidence["error"]


# ---------------------------------------------------------------------------
# llm_daily_burn
# ---------------------------------------------------------------------------


def _spend(component_id: str, burn: float, calls: int = 10, tokens: int = 1000):
    return {
        "component_id": component_id,
        "burn_usd": burn,
        "calls": calls,
        "tokens": tokens,
    }


def _threshold(component_id: str, ceiling, priced: bool = True):
    return {
        "component_id": component_id,
        "state": "active",
        "daily_burn_alert_usd": ceiling,
        "priced": priced,
    }


def test_burn_over_the_ceiling_is_a_deficit_and_pages():
    """THE #22 assertion: the condition that ended in a 402 nobody saw coming
    must page while it is still money, not yet an outage."""
    gauges = llm_burn_gauges(
        [_spend("llm.judge.cerebras_gemma4_31b.openai_compat", 12.5)],
        [_threshold("llm.judge.cerebras_gemma4_31b.openai_compat", 10.0)],
        now=NOW,
        cfg=CFG,
    )
    assert len(gauges) == 1
    g = gauges[0]
    assert g.state == "deficit"
    assert g.pages is True
    assert g.severity == "medium"
    assert "$12.50" in g.actual


def test_runaway_burn_is_critical():
    gauges = llm_burn_gauges(
        [_spend("c", 40.0)], [_threshold("c", 10.0)], now=NOW, cfg=CFG
    )
    assert gauges[0].severity == "critical"


def test_burn_under_the_ceiling_is_ok():
    gauges = llm_burn_gauges(
        [_spend("c", 3.0)], [_threshold("c", 10.0)], now=NOW, cfg=CFG
    )
    assert gauges[0].state == "ok"
    assert gauges[0].pages is False
    assert gauges[0].ratio == pytest.approx(0.3)


def test_a_ceiling_with_no_spend_today_is_ok_at_zero():
    gauges = llm_burn_gauges([], [_threshold("c", 10.0)], now=NOW, cfg=CFG)
    assert gauges[0].state == "ok"
    assert gauges[0].evidence["burn_usd_today"] == 0.0


def test_spend_without_a_ceiling_is_visible_but_never_pages():
    """Money moving with nobody watching is a fact worth showing — on the
    route, not the operator's phone (absent = never pages, per contract)."""
    gauges = llm_burn_gauges([_spend("c", 7.0)], [], now=NOW, cfg=CFG)
    assert len(gauges) == 1
    assert gauges[0].state == "ungauged"
    assert gauges[0].quiet_reason == "no_burn_threshold"
    assert gauges[0].pages is False
    assert gauges[0].evidence["burn_usd_today"] == 7.0


def test_no_spend_and_no_ceiling_is_no_row_at_all():
    """An unpriced self-hosted component's non-burn is not a fact worth a
    line per read."""
    gauges = llm_burn_gauges(
        [], [_threshold("c", None)], now=NOW, cfg=CFG
    )
    assert gauges == []


def test_a_zero_ceiling_cannot_divide_and_degrades_honestly():
    gauges = llm_burn_gauges(
        [_spend("c", 1.0)], [_threshold("c", 0.0)], now=NOW, cfg=CFG
    )
    assert gauges[0].state == "ungauged"
    assert gauges[0].quiet_reason == "no_burn_threshold"


def test_an_unpriced_component_with_a_ceiling_says_it_cannot_see():
    """A ceiling on a component whose receipts cost 0.0 by construction gauges
    a fiction — the evidence must say so rather than reading healthy."""
    gauges = llm_burn_gauges(
        [], [_threshold("c", 10.0, priced=False)], now=NOW, cfg=CFG
    )
    assert gauges[0].state == "ok"
    assert "cannot see real spend" in gauges[0].evidence["note"]


async def test_burn_read_degrades_loud():
    class _Boom:
        async def fetch(self, *a, **k):
            raise RuntimeError("permission denied")

    loops = await read_llm_burn_loops(_Boom(), now=NOW, cfg=CFG)
    assert len(loops) == 1
    assert loops[0].state == "ungauged"
    assert loops[0].quiet_reason == "burn_query_failed"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_every_metering_loop_is_registered_in_the_enumeration():
    assert LOOP_LLM_LATENCY in LOOP_CLASSES
    assert LOOP_LLM_DAILY_BURN in LOOP_CLASSES


def test_the_watched_component_is_the_primary_plane():
    """The ceiling default (120s) is calibrated to THIS component's 240s
    client timeout; repointing the loop means recalibrating the ceiling."""
    assert LATENCY_COMPONENT == "llm.primary.openai_compat"


@pytest.mark.parametrize(
    "field",
    [
        "llm_latency_window_minutes",
        "llm_latency_p95_ceiling_ms",
        "llm_latency_min_calls",
    ],
)
def test_every_new_threshold_is_an_operator_knob(field):
    """A GaugeConfig field with no matching ``gauge_``-prefixed OptionSpec is
    silently un-tunable: a descriptor setting it is REJECTED."""
    assert hasattr(GaugeConfig(), field)
    names = {spec.name for spec in HANDLER_OPTIONS["alert_trigger_scan"]}
    assert f"gauge_{field}" in names

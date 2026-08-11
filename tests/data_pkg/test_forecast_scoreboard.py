# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P4-T7 — the acute-forecast scoreboard producer (deterministic META driver).

Forecasting returns as a MEASURED number, NEVER a free-text claim. These tests
pin the honesty invariants:

  (1) forecast_scoreboard is mapped TRACE_ONLY in the deterministic dispatch — its
      per-run receipt is NEVER persisted as a finding / prediction / claim; its
      only persisted product is acute_forecasts rows + the analyst_traces receipt.
  (2) the producer NEVER bypasses the D9 degeneracy ABSTAIN — fed a geography-
      dominated (near-{0,1}) p-vector, issue_weekly_forecasts issues ZERO rows
      (no INSERT) and the producer's receipt reports issued=0, minting nothing.
  (3) the three legs are best-effort + isolated (one leg failing never aborts the
      others); a no-pool run degrades to an HONEST empty receipt, writes nothing.

The forecasting math + the exogeneity / clamp / abstain LOGIC are covered by
tests/data_pkg/test_forecast_acute.py; this module covers only the T7 WIRING +
the driver's honest receipt.
"""
from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import forecast_acute as fa
from legba.data.analysts.deterministic_handlers import forecast_scoreboard as fs


# ---------------------------------------------------------------------------
# Invariant (1) — TRACE_ONLY wiring: no finding / claim on a trust surface
# ---------------------------------------------------------------------------


def test_dispatch_wiring_is_trace_only():
    # Import here so a wiring regression surfaces as a test failure, not a
    # collection error for the pure-logic tests below.
    from legba.data.analysts import deterministic as det
    from legba.data.provenance.kinds import TRACE_ONLY

    # Dispatchable under its sub_handler name.
    assert det.SUB_HANDLERS["forecast_scoreboard"] is fs.handle
    # TRACE_ONLY => the actor SKIPS the analyst_outputs INSERT: the receipt is
    # never a persisted finding / prediction / claim on any trust surface.
    assert det.OUTPUT_KIND_BY_SUB_HANDLER["forecast_scoreboard"] is TRACE_ONLY


# ---------------------------------------------------------------------------
# The receipt is counts-only (no forecast values / probabilities / claim text)
# ---------------------------------------------------------------------------


def test_receipt_is_counts_only():
    r = fs.build_receipt(issued=3, resolved=2, resolved_total=19, warnings=[])
    assert r.kind_marker == "finding"
    assert r.confidence == 1.0
    assert r.data["sub_handler"] == "forecast_scoreboard"
    assert r.data["issued"] == 3
    assert r.data["resolved"] == 2
    assert r.data["resolved_total"] == 19
    assert r.data["event_class"] == fa.EVENT_CLASS
    # It carries the class + counts only — no probability / p / p_base / region.
    for banned in ("p_base", "region", "probability"):
        assert banned not in r.data


# ---------------------------------------------------------------------------
# Invariant (3) — no-pool degrades to an HONEST empty receipt, writes nothing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_pool_is_honest_empty_receipt_and_calls_no_writer(monkeypatch):
    calls: list[str] = []

    async def _boom(*a, **k):  # any writer touched on the no-pool path = a bug
        calls.append("writer")
        raise AssertionError("no forecast_acute writer should run without a pool")

    monkeypatch.setattr(fa, "issue_weekly_forecasts", _boom)
    monkeypatch.setattr(fa, "resolve_open_acute_forecasts", _boom)
    monkeypatch.setattr(fa, "pull_resolved_acute_forecasts", _boom)

    result = await fs.handle([], {"sub_handler": "forecast_scoreboard"}, None)

    assert calls == []
    assert result.finding.data["issued"] == 0
    assert result.finding.data["resolved"] == 0
    assert result.finding.data["resolved_total"] == 0
    assert "forecast_scoreboard.no_pool" in result.finding.data["warnings"]
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


# ---------------------------------------------------------------------------
# The happy path — the driver reports what the writers DID, verbatim
# ---------------------------------------------------------------------------


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool


@pytest.mark.asyncio
async def test_handle_reports_writer_counts_verbatim(monkeypatch):
    seen: list[str] = []

    async def _issue(deps, options, *, receipt=None):
        seen.append("issue")
        if receipt is not None:
            receipt["reason"] = "issued"
        return 4

    async def _resolve(deps, options, *, receipt=None):
        seen.append("resolve")
        return 2

    async def _pull(deps, options):
        seen.append("pull")
        return [{"claim_id": "a"}, {"claim_id": "b"}, {"claim_id": "c"}]

    monkeypatch.setattr(fa, "issue_weekly_forecasts", _issue)
    monkeypatch.setattr(fa, "resolve_open_acute_forecasts", _resolve)
    monkeypatch.setattr(fa, "pull_resolved_acute_forecasts", _pull)

    result = await fs.handle(
        [],
        {"sub_handler": "forecast_scoreboard", "run_id": str(uuid4())},
        _Deps(object()),  # any non-None pool
    )

    # issue BEFORE resolve BEFORE pull (fresh grades land before the count).
    assert seen == ["issue", "resolve", "pull"]
    assert result.finding.data["issued"] == 4
    assert result.finding.data["resolved"] == 2
    assert result.finding.data["resolved_total"] == 3
    assert result.finding.data["warnings"] == []


# ---------------------------------------------------------------------------
# Invariant (3) — one leg failing never aborts the others
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_leg_failure_is_isolated(monkeypatch):
    async def _issue_boom(deps, options, *, receipt=None):
        raise RuntimeError("issue exploded")

    async def _resolve(deps, options, *, receipt=None):
        return 5

    async def _pull(deps, options):
        return [{"claim_id": "x"}]

    monkeypatch.setattr(fa, "issue_weekly_forecasts", _issue_boom)
    monkeypatch.setattr(fa, "resolve_open_acute_forecasts", _resolve)
    monkeypatch.setattr(fa, "pull_resolved_acute_forecasts", _pull)

    result = await fs.handle([], {"sub_handler": "forecast_scoreboard"}, _Deps(object()))

    # The issue leg failing is recorded but resolve + pull still ran.
    assert result.finding.data["issued"] == 0
    assert "forecast_scoreboard.issue_failed" in result.finding.data["warnings"]
    assert result.finding.data["resolved"] == 5
    assert result.finding.data["resolved_total"] == 1


# ---------------------------------------------------------------------------
# Invariant (2) — the producer NEVER bypasses the D9 degeneracy ABSTAIN
# ---------------------------------------------------------------------------


class _AcquireCtx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _AbstainConn:
    """A fake conn that drives forecast_acute.issue_weekly_forecasts into a
    GEOGRAPHY-DOMINATED (all-near-0) p-vector so the real D9 guard ABSTAINS —
    every G20 country has zero recent class-K events AND zero climatology weeks,
    so p clamps to ~P_EPSILON for all, uncertain-share 0 < 0.2 → abstain."""

    def __init__(self):
        self.inserts: list[str] = []

    async def fetch(self, sql, *args):
        if "FROM target_descriptors" in sql:
            # Two active G20 regions with resolvable geo.
            return [
                {"descriptor_id": "country_g20_us", "geo": ["US"]},
                {"descriptor_id": "country_g20_br", "geo": ["BR"]},
            ]
        # resolve_open_acute_forecasts open-row scan (nothing open).
        if "resolved_outcome IS NULL" in sql:
            return []
        # pull_resolved_acute_forecasts (nothing resolved yet).
        if "resolved_outcome IS NOT NULL" in sql:
            return []
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        # The resolver's backlog SELF-CHECK — no gradeable row is overdue here.
        if "AS oldest_days" in sql:
            return {"n": 0, "oldest_days": 0}
        # _count_class_k → recent-rate = 0 events for every country.
        if "AS cnt" in sql:
            return {"cnt": 0}
        # _climatology_base (has the geo filter) → 0 weeks with an event → p_base 0.
        if "AS wk" in sql and "geo &&" in sql:
            return {"wk": 0}
        # _total_observed_weeks (no geo filter) → a non-zero denominator.
        if "AS wk" in sql:
            return {"wk": 52}
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        # The ONLY execute the issue path can reach is the acute_forecasts INSERT
        # — which must NEVER run on an abstaining (degenerate) batch.
        if "INSERT INTO acute_forecasts" in sql:
            self.inserts.append(sql)
        return "INSERT 0 0"


class _AbstainPool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _AcquireCtx(self._conn)


@pytest.mark.asyncio
async def test_geography_dominated_vector_abstains_no_rows_minted():
    conn = _AbstainConn()
    result = await fs.handle(
        [], {"sub_handler": "forecast_scoreboard"}, _Deps(_AbstainPool(conn))
    )

    # The real forecast_acute D9 guard fired: ZERO acute_forecasts rows minted.
    assert conn.inserts == []
    # The producer reports the abstain HONESTLY as issued=0 — it does NOT mint a
    # certainty vector to make the scoreboard look populated.
    assert result.finding.data["issued"] == 0
    assert result.finding.data["resolved"] == 0
    assert result.finding.data["resolved_total"] == 0
    # DQ P6 — the receipt now ATTRIBUTES the issued=0 to a D9 degeneracy abstain
    # (distinct from a window-already-issued no-op) via a real receipt roundtrip.
    assert any(
        "abstained_degenerate_p" in w for w in result.finding.data["warnings"]
    )


# ---------------------------------------------------------------------------
# DQ P6 — issued=0 is ATTRIBUTED: abstain vs already-issued vs no-targets each
# emit a DISTINCT warnings[] entry (they used to be indistinguishable).
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reason,expect",
    [
        ("abstained_degenerate", "forecast_scoreboard.abstained_degenerate_p"),
        ("window_already_issued", "forecast_scoreboard.window_already_issued"),
        ("no_regions", "forecast_scoreboard.issued_0_no_targets"),
    ],
)
async def test_issued_zero_reason_maps_to_distinct_warning(monkeypatch, reason, expect):
    async def _issue(deps, options, *, receipt=None):
        if receipt is not None:
            receipt["reason"] = reason
            receipt["staged"] = 19
            receipt["uncertain"] = 0
        return 0

    async def _noop_resolve(deps, options, *, receipt=None):
        return 0

    async def _noop_pull(deps, options):
        return []

    monkeypatch.setattr(fa, "issue_weekly_forecasts", _issue)
    monkeypatch.setattr(fa, "resolve_open_acute_forecasts", _noop_resolve)
    monkeypatch.setattr(fa, "pull_resolved_acute_forecasts", _noop_pull)

    result = await fs.handle([], {"sub_handler": "forecast_scoreboard"}, _Deps(object()))
    warnings = result.finding.data["warnings"]
    assert any(w.startswith(expect) for w in warnings), (reason, warnings)

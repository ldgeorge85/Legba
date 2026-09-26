# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``inquiry_yield`` — the H12 weekly instrument (Program 5 lane 1,
planning/PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §4).

Mirrors ``test_sealed_forecast_ledger.py``'s own two-layer proof:

  * the ARITHMETIC (:func:`compute_yield`) is exercised directly against a
    hand-built fixture ledger — no DB, no container;
  * :func:`handle` is exercised against a fake ``asyncpg``-shaped connection
    (the ``receipt_anchor`` precedent's ``_AnchorConn`` idiom) so the SQL
    routing + degrade paths are proven without a live pool.

Dispatch-wiring + descriptor checks close the loop: the sub-handler is
registered, emits a genuine FINDING (never TRACE_ONLY — the receipt IS the
product), and is exempted from the faithfulness verify pass (pure
arithmetic, no model prose to grade).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import inquiry_yield as iy

_NOW = datetime(2026, 9, 24, 6, 0, 0, tzinfo=timezone.utc)
_WEEK_START = _NOW - timedelta(days=7)
_WEEK_END = _NOW


def _row(
    descriptor_id: str,
    kind: str,
    *,
    status: str = "open",
    created_at: datetime = _WEEK_START + timedelta(days=1),
    closed_at: datetime | None = None,
    dispatched_to: str | None = None,
    cited_refs: list[str] | None = None,
) -> dict:
    return {
        "descriptor_id": descriptor_id,
        "kind": kind,
        "status": status,
        "created_at": created_at,
        "closed_at": closed_at,
        "dispatched_to": dispatched_to,
        "cited_refs": cited_refs or [],
    }


# ---------------------------------------------------------------------------
# The pure arithmetic
# ---------------------------------------------------------------------------


def test_hypothesis_opened_counts_only_inside_the_window():
    inside = _row("d1", "hypothesis", created_at=_WEEK_START + timedelta(hours=1))
    before = _row("d1", "hypothesis", created_at=_WEEK_START - timedelta(days=1))
    at_end = _row("d1", "hypothesis", created_at=_WEEK_END)  # half-open: excluded
    out = iy.compute_yield([inside, before, at_end], week_start=_WEEK_START, week_end=_WEEK_END)
    assert out["d1"].hypotheses_opened == 1


def test_hypothesis_confirmed_refuted_expired_split_by_status_at_close():
    rows = [
        _row("d1", "hypothesis", status="confirmed",
             closed_at=_WEEK_START + timedelta(days=2)),
        _row("d1", "hypothesis", status="refuted",
             closed_at=_WEEK_START + timedelta(days=3)),
        _row("d1", "hypothesis", status="expired",
             closed_at=_WEEK_START + timedelta(days=4)),
        # closed OUTSIDE the window — not counted this week.
        _row("d1", "hypothesis", status="confirmed",
             closed_at=_WEEK_START - timedelta(days=1)),
        # withdrawn is not one of the three counters at all.
        _row("d1", "hypothesis", status="withdrawn",
             closed_at=_WEEK_START + timedelta(days=1)),
    ]
    out = iy.compute_yield(rows, week_start=_WEEK_START, week_end=_WEEK_END)
    d = out["d1"]
    assert (d.hypotheses_confirmed, d.hypotheses_refuted, d.hypotheses_expired) == (1, 1, 1)


def test_questions_dispatched_requires_a_target():
    dispatched = _row("d1", "question", dispatched_to="desk_x")
    undispatched = _row("d1", "question", dispatched_to=None)
    out = iy.compute_yield(
        [dispatched, undispatched], week_start=_WEEK_START, week_end=_WEEK_END,
    )
    assert out["d1"].questions_dispatched == 1


def test_questions_answered_counts_closed_answered_in_window():
    answered = _row(
        "d1", "question", status="answered", dispatched_to="desk_x",
        closed_at=_WEEK_START + timedelta(days=2),
    )
    still_open = _row("d1", "question", status="open", dispatched_to="desk_y")
    out = iy.compute_yield(
        [answered, still_open], week_start=_WEEK_START, week_end=_WEEK_END,
    )
    assert out["d1"].questions_answered == 1


def test_observation_anticipated_requires_a_strictly_later_finding_sharing_a_ref():
    ref = str(uuid4())
    closed_at = _WEEK_START + timedelta(days=2)
    observed = _row(
        "d1", "observation", status="confirmed", closed_at=closed_at,
        cited_refs=[ref],
    )
    later_finding = (closed_at + timedelta(hours=1), frozenset({ref}))
    out = iy.compute_yield(
        [observed], week_start=_WEEK_START, week_end=_WEEK_END,
        finding_refs=[later_finding],
    )
    assert out["d1"].observations_anticipated == 1


def test_observation_not_anticipated_when_finding_predates_the_close():
    ref = str(uuid4())
    closed_at = _WEEK_START + timedelta(days=2)
    observed = _row(
        "d1", "observation", status="confirmed", closed_at=closed_at,
        cited_refs=[ref],
    )
    earlier_finding = (closed_at - timedelta(hours=1), frozenset({ref}))
    out = iy.compute_yield(
        [observed], week_start=_WEEK_START, week_end=_WEEK_END,
        finding_refs=[earlier_finding],
    )
    assert out["d1"].observations_anticipated == 0


def test_observation_not_anticipated_when_no_ref_overlaps():
    closed_at = _WEEK_START + timedelta(days=2)
    observed = _row(
        "d1", "observation", status="confirmed", closed_at=closed_at,
        cited_refs=[str(uuid4())],
    )
    later_finding = (closed_at + timedelta(hours=1), frozenset({str(uuid4())}))
    out = iy.compute_yield(
        [observed], week_start=_WEEK_START, week_end=_WEEK_END,
        finding_refs=[later_finding],
    )
    assert out["d1"].observations_anticipated == 0


def test_observation_with_no_cited_refs_is_never_anticipated():
    """cited_refs is set ONLY by ledger_close — an observation closed without
    any (a plain 'confirmed', no warrant) can never count as anticipated,
    even against a finding that would otherwise overlap an empty set."""
    closed_at = _WEEK_START + timedelta(days=2)
    observed = _row("d1", "observation", status="confirmed", closed_at=closed_at, cited_refs=[])
    out = iy.compute_yield(
        [observed], week_start=_WEEK_START, week_end=_WEEK_END,
        finding_refs=[(closed_at + timedelta(hours=1), frozenset())],
    )
    assert out["d1"].observations_anticipated == 0


def test_blind_spot_is_an_open_dispatched_question_with_no_read_this_cycle():
    live = _row("d1", "question", status="open", dispatched_to="desk_silent",
                created_at=_WEEK_START + timedelta(days=1))
    read_this_cycle = _row("d1", "question", status="open", dispatched_to="desk_active",
                            created_at=_WEEK_START + timedelta(days=1))
    out = iy.compute_yield(
        [live, read_this_cycle], week_start=_WEEK_START, week_end=_WEEK_END,
        read_targets=frozenset({"desk_active"}),
    )
    assert out["d1"].blind_spots == ("desk_silent",)


def test_blind_spot_requires_the_question_to_still_be_open():
    answered = _row(
        "d1", "question", status="answered", dispatched_to="desk_silent",
        created_at=_WEEK_START + timedelta(days=1),
        closed_at=_WEEK_START + timedelta(days=2),
    )
    out = iy.compute_yield(
        [answered], week_start=_WEEK_START, week_end=_WEEK_END, read_targets=frozenset(),
    )
    assert out["d1"].blind_spots == ()


def test_blind_spot_requires_the_question_to_overlap_the_window():
    """A question dispatched and CLOSED entirely before the window opened is
    not a live blind spot this week, even if its target never read."""
    stale = _row(
        "d1", "question", status="open", dispatched_to="desk_silent",
        created_at=_WEEK_START - timedelta(days=30),
        closed_at=_WEEK_START - timedelta(days=20),
    )
    out = iy.compute_yield(
        [stale], week_start=_WEEK_START, week_end=_WEEK_END, read_targets=frozenset(),
    )
    assert out["d1"].blind_spots == ()


def test_multiple_descriptors_are_scored_independently():
    rows = [
        _row("d1", "hypothesis", created_at=_WEEK_START + timedelta(days=1)),
        _row("d2", "hypothesis", created_at=_WEEK_START + timedelta(days=1)),
        _row("d2", "hypothesis", created_at=_WEEK_START + timedelta(days=2)),
    ]
    out = iy.compute_yield(rows, week_start=_WEEK_START, week_end=_WEEK_END)
    assert set(out) == {"d1", "d2"}
    assert out["d1"].hypotheses_opened == 1
    assert out["d2"].hypotheses_opened == 2


def test_a_descriptor_with_no_rows_never_appears():
    out = iy.compute_yield([], week_start=_WEEK_START, week_end=_WEEK_END)
    assert out == {}


# ---------------------------------------------------------------------------
# build_finding — the receipt
# ---------------------------------------------------------------------------


def test_build_finding_carries_the_method_version_and_breakdown():
    counts = iy.YieldCounts(descriptor_id="d1", hypotheses_opened=3)
    finding = iy.build_finding(
        week_start=_WEEK_START, week_end=_WEEK_END,
        by_descriptor={"d1": counts}, warnings=["x"],
    )
    assert finding.data["method_version"] == iy.METHOD_VERSION
    assert finding.data["sub_handler"] == "inquiry_yield"
    assert finding.data["descriptors_scored"] == 1
    assert finding.data["by_descriptor"]["d1"]["hypotheses_opened"] == 3
    assert finding.data["warnings"] == ["x"]
    assert finding.confidence == 1.0


def test_yield_counts_as_dict_reports_blind_spot_count():
    counts = iy.YieldCounts(descriptor_id="d1", blind_spots=("a", "b"))
    d = counts.as_dict()
    assert d["blind_spots"] == ["a", "b"]
    assert d["blind_spot_count"] == 2


# ---------------------------------------------------------------------------
# handle() — the I/O shell, against a fake asyncpg-shaped connection
# ---------------------------------------------------------------------------


class _Ctx:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        return _Ctx(self._conn)


class _Deps:
    def __init__(self, pool):
        self.pg_pool = pool


class _YieldConn:
    """Three canned result sets: the ledger scan, the in-window finding refs,
    and the desks/targets with activity this cycle."""

    def __init__(self, *, ledger=(), findings=(), reads=()):
        self._ledger = list(ledger)
        self._findings = list(findings)
        self._reads = list(reads)

    async def fetch(self, sql, *args):
        if "FROM inquiry_ledger" in sql:
            return self._ledger
        if "FROM analyst_outputs" in sql and "derived_from" in sql:
            return self._findings
        if "DISTINCT analyst_id, target_id" in sql:
            return self._reads
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")


@pytest.mark.asyncio
async def test_handle_degrades_to_an_honest_empty_receipt_with_no_pool():
    result = await iy.handle([], {}, _Deps(None))
    d = result.finding.data
    assert d["descriptors_scored"] == 0
    assert d["by_descriptor"] == {}
    assert "inquiry_yield.no_pool" in d["warnings"]
    assert result.usage == {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


@pytest.mark.asyncio
async def test_handle_wires_ledger_rows_findings_and_reads_into_the_arithmetic():
    ref = uuid4()
    now = datetime.now(timezone.utc)
    ledger_row = {
        "descriptor_id": "pilot",
        "kind": "observation",
        "status": "confirmed",
        "dispatched_to": None,
        "cited_refs": [str(ref)],
        "created_at": now - timedelta(days=1),
        "closed_at": now - timedelta(hours=12),
    }
    finding_row = {
        "produced_at": now - timedelta(hours=1),
        "derived_from": [ref],
    }
    conn = _YieldConn(ledger=[ledger_row], findings=[finding_row], reads=[])
    result = await iy.handle([], {}, _Deps(_Pool(conn)))
    by_desc = result.finding.data["by_descriptor"]
    assert by_desc["pilot"]["observations_anticipated"] == 1


@pytest.mark.asyncio
async def test_handle_reports_capped_warnings():
    now = datetime.now(timezone.utc)
    ledger_rows = [
        {
            "descriptor_id": "pilot", "kind": "expectation", "status": "open",
            "dispatched_to": None, "cited_refs": [],
            "created_at": now - timedelta(days=1), "closed_at": None,
        }
    ] * iy._MAX_LEDGER_ROWS
    conn = _YieldConn(ledger=ledger_rows, findings=[], reads=[])
    result = await iy.handle([], {}, _Deps(_Pool(conn)))
    assert "inquiry_yield.ledger_capped" in result.finding.data["warnings"]


# ---------------------------------------------------------------------------
# Dispatch wiring + descriptor
# ---------------------------------------------------------------------------


def test_dispatch_wiring_emits_a_genuine_finding_and_is_verify_exempt():
    from legba.data.analysts import deterministic as det
    from legba.data.provenance.kinds import (
        STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
        OutputKind,
        TRACE_ONLY,
        verify_exempt_reason,
    )

    assert det.SUB_HANDLERS["inquiry_yield"] is iy.handle
    assert det.OUTPUT_KIND_BY_SUB_HANDLER["inquiry_yield"] is OutputKind.FINDING
    assert det.OUTPUT_KIND_BY_SUB_HANDLER["inquiry_yield"] is not TRACE_ONLY
    assert "inquiry_yield" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert verify_exempt_reason("inquiry_yield") == "structural"


def test_descriptor_is_draft_weekly_and_budgeted_zero():
    from pathlib import Path

    import yaml

    repo_root = Path(__file__).resolve().parents[2]
    text = (repo_root / "descriptors/analyst_inquiry_yield.yaml").read_text()
    doc = yaml.safe_load(text)
    assert doc["identity"]["id"] == "inquiry_yield"
    assert doc["identity"]["state"] == "draft"
    assert doc["identity"]["kind"] == "deterministic"
    assert doc["method"]["sub_handler"] == "inquiry_yield"
    assert doc["method"]["budget_tokens_per_day"] == 0
    assert doc["cadence"]["fallback_schedule"] == "0 6 * * 1"
    assert doc["cadence"]["cooldown_seconds"] < 7 * 24 * 3600

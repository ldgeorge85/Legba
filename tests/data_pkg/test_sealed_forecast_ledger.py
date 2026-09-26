# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H13 — the sealed forecast ledger.

Three properties, none of which changes a forecast or claims skill:

  (1) the RESOLUTION TEST is frozen as text at mint —
      ``acute_forecasts.resolution_test`` carries the event class, the
      exogenous column/join, the threshold and the window; pre-existing rows
      are backfilled ``retro:``-prefixed by migration 0212;
  (2) the honest DENOMINATOR — a due-but-unresolved row is marked
      ``resolved_by='unresolved:expired'`` (still retried by the resolver,
      never conflated with ``voided:``) and the scoreboard publishes
      ``brier_answered`` / ``brier_all`` (expired at maximum penalty) /
      ``expired_count`` side by side, plus per-minter hypothesis open/resolved
      counts;
  (3) the EXTERNAL TIMESTAMP — ``receipt_anchor`` Merkle-roots the chain heads
      daily and POSTs the digest to two OpenTimestamps calendars, landing a
      ``receipt_anchors`` row ('submitted' with proof, or 'pending' retried
      next tick); ``GET /v3/system/receipt-anchors`` lists them.
"""
from __future__ import annotations

import hashlib
import inspect
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic_handlers import forecast_acute as fa
from legba.data.analysts.deterministic_handlers import forecast_scoreboard as fs
from legba.data.analysts.deterministic_handlers import receipt_anchor as ra

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATION = (
    REPO_ROOT / "src/legba/data/migrations/0212_sealed_forecast_ledger.sql"
)


# ---------------------------------------------------------------------------
# (1) the frozen resolution test
# ---------------------------------------------------------------------------


def test_resolution_test_text_freezes_the_rule():
    """The minted text names the class, the exogenous sources, the upstream
    event-time join, the threshold and the window — enough to re-grade the row
    without reading the code."""
    t = fa.RESOLUTION_TEST
    assert f"class={fa.EVENT_CLASS}" in t
    for src in fa.HAZARD_SEVERE_SOURCES:
        assert src in t
    assert ">=1" in t                       # the threshold
    assert "[window_start, window_end)" in t  # the window
    assert f"grace={fa.RESOLUTION_GRACE_DAYS}d" in t
    # Frozen means FROZEN — built from the constants, so it cannot drift.
    assert fa.RESOLUTION_TEST == (
        f"class={fa.EVENT_CLASS}; "
        f"o=1 iff >=1 signal with source_id IN "
        f"({', '.join(fa.HAZARD_SEVERE_SOURCES)}) "
        "geo-overlapping the region's geo codes and timed by the UPSTREAM "
        "event stamp (usgs origin ms / eonet event date / fetched_at "
        "fallback) inside [window_start, window_end); "
        f"window=7d weekly; resolver grace={fa.RESOLUTION_GRACE_DAYS}d"
    )


def test_migration_retro_backfill_matches_the_constant():
    """The 0212 backfill writes 'retro: ' + the CURRENT rule — pinned here so
    neither side can drift apart silently."""
    import re

    text = MIGRATION.read_text(encoding="utf-8")
    seg = text.split("SET resolution_test =", 1)[1].split("WHERE", 1)[0]
    # Rebuild the SQL string literal: 'quoted' chunks joined by ||, with ''
    # escapes unescaped.
    protected = seg.replace("''", "\x00")
    sql_text = "".join(re.findall(r"'([^']*)'", protected)).replace("\x00", "'")
    assert sql_text == "retro: " + fa.RESOLUTION_TEST


class _IssueConn:
    """Drives the REAL issuer: one region, mild counts → a non-degenerate
    vector of interior probabilities, so the INSERT path actually runs."""

    def __init__(self):
        self.inserts: list[tuple] = []

    async def fetch(self, sql, *args):
        if "FROM target_descriptors" in sql:          # _g20_regions
            return [
                {"descriptor_id": "country_g20_us",
                 "geo": ["US"]},
                {"descriptor_id": "country_g20_fr",
                 "geo": ["FR"]},
                {"descriptor_id": "country_g20_jp",
                 "geo": ["JP"]},
            ]
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        if "AS wk" in sql and "geo &&" in sql:
            return {"wk": 2}    # _climatology_base: 2/10 weeks → p_base 0.2
        if "AS wk" in sql:      # _total_observed_weeks
            return {"wk": 10}
        if "AS cnt" in sql:     # _count_class_k → mild recent rate
            return {"cnt": 2}
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        if "INSERT INTO acute_forecasts" in sql:
            self.inserts.append(args)
            return "INSERT 0 1"
        raise AssertionError(f"unexpected execute SQL: {sql[:80]}")


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


@pytest.mark.asyncio
async def test_mint_stamps_resolution_test_and_method_version():
    conn = _IssueConn()
    n = await fa.issue_weekly_forecasts(_Deps(_Pool(conn)), {})
    assert n == 3
    for args in conn.inserts:
        # ($1..$11) — $8 method_version, $9 scale_version (K3), $11 the frozen
        # resolution test. The scale slotted in beside the revision it is not:
        # one says which code ran, the other whether two p's are comparable.
        assert args[7] == fa.METHOD_VERSION
        assert args[8] == fa.SCALE_VERSION
        assert args[10] == fa.RESOLUTION_TEST


# ---------------------------------------------------------------------------
# (2) the honest denominator
# ---------------------------------------------------------------------------


class _ResolveConn:
    """Due rows come back from the open-row scan; counts may raise."""

    def __init__(self, *, due=(), count=1, geo=("US",)):
        self._due = list(due)
        self._count = count
        self._geo = list(geo)
        self.updates: list[tuple] = []

    async def fetch(self, sql, *args):
        if "resolved_outcome IS NULL" in sql:
            return self._due
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")

    async def fetchrow(self, sql, *args):
        if "AS oldest_days" in sql:
            return {"n": 0, "oldest_days": 0}
        if "FROM target_descriptors" in sql:
            return {"geo": self._geo}
        if "AS cnt" in sql:
            if isinstance(self._count, Exception):
                raise self._count
            return {"cnt": self._count}
        raise AssertionError(f"unexpected fetchrow SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        self.updates.append((sql, args))
        return "UPDATE 1"


def _due_row(region="country_g20_us"):
    t1 = datetime.now(timezone.utc) - timedelta(days=10)
    return {
        "id": str(uuid4()),
        "region": region,
        "window_start": t1 - timedelta(days=7),
        "window_end": t1,
    }


@pytest.mark.asyncio
async def test_unresolvable_due_row_is_marked_expired():
    """A row whose window_end + grace passed but could not be graded is marked
    'unresolved:expired' — in the denominator, not silently dropped."""
    conn = _ResolveConn(due=[_due_row()], count=RuntimeError("nope"))
    receipt: dict = {}
    n = await fa.resolve_open_acute_forecasts(
        _Deps(_Pool(conn)), {}, receipt=receipt
    )
    assert n == 0
    # The ONLY write is the expiry mark — never a grade on a failed count.
    assert len(conn.updates) == 1
    sql, args = conn.updates[0]
    assert "UPDATE acute_forecasts" in sql
    assert args[1] == fa.UNRESOLVED_EXPIRED
    assert receipt["newly_expired"] == 1


@pytest.mark.asyncio
async def test_expired_marking_does_not_run_on_an_idle_tick():
    """due == 0 ⇒ nothing matches the marking predicate ⇒ zero writes (the
    idle tick stays a zero-write tick)."""
    conn = _ResolveConn(due=[])
    receipt: dict = {}
    await fa.resolve_open_acute_forecasts(_Deps(_Pool(conn)), {}, receipt=receipt)
    assert conn.updates == []
    assert receipt["newly_expired"] == 0


def test_expired_rows_stay_retryable():
    """The mark is NOT voided:* — the open-row scan must still pick the row up
    next tick so a transient failure can resolve late. Pin the SELECT's only
    resolved_by filter: voided-only."""
    src = inspect.getsource(fa.resolve_open_acute_forecasts)
    scan = src.split("SELECT id::text AS id")[1].split("LIMIT 500")[0]
    assert "resolved_by IS NULL" in scan
    assert "NOT LIKE 'voided:%'" in scan
    assert "unresolved" not in scan
    # And it is distinct from the void sentinel.
    assert fa.UNRESOLVED_EXPIRED.startswith("unresolved:")
    assert not fa.UNRESOLVED_EXPIRED.startswith(fa.VOID_PREFIX)


def test_brier_answered_and_brier_all():
    rows = [
        {"claimed_confidence": 0.2, "outcome": 0},   # 0.04
        {"claimed_confidence": 0.8, "outcome": 1},   # 0.04
    ]
    assert fs.brier_of(rows) == pytest.approx(0.04)
    # Two answered + one expired at max penalty (1.0): (0.08 + 1.0) / 3.
    assert fs.brier_all_of(rows, 1) == pytest.approx((0.08 + 1.0) / 3)
    # Honest-null: empty population is None, never a fabricated 0.
    assert fs.brier_of([]) is None
    assert fs.brier_all_of([], 0) is None
    assert fs.brier_all_of([], 2) == pytest.approx(1.0)


def test_receipt_publishes_the_denominator_trio_and_minters():
    r = fs.build_receipt(
        issued=1, resolved=1, resolved_total=5, warnings=[],
        brier_answered=0.04, brier_all=0.36, expired_count=1,
        hypothesis_minters=[{"analyst_id": "desk", "open": 12, "resolved": 3}],
    )
    d = r.data
    assert d["brier_answered"] == 0.04
    assert d["brier_all"] == 0.36
    assert d["expired_count"] == 1
    assert d["hypothesis_minters"] == [
        {"analyst_id": "desk", "open": 12, "resolved": 3}
    ]


def test_minter_sql_groups_hypotheses_by_analyst():
    """The selection-bias meter reads hypotheses.resolved_outcome split by
    minter — open vs resolved."""
    sql = fs._HYPOTHESIS_MINTER_SQL
    assert "FROM hypotheses" in sql
    assert "resolved_outcome IS NULL" in sql
    assert "GROUP BY analyst_id" in sql


# ---------------------------------------------------------------------------
# (3) the external timestamp — Merkle fold, calendar POSTs, ledger rows
# ---------------------------------------------------------------------------


def test_leaf_recipe_is_sha256_of_analyst_plus_receipt():
    leaf = ra.leaf_for("analyst_a", "deadbeef")
    assert leaf == hashlib.sha256(b"analyst_adeadbeef").digest()


def test_merkle_root_is_deterministic_and_sensitive():
    heads = [("a", "aa" * 32), ("b", "bb" * 32), ("c", "cc" * 32)]
    leaves = [ra.leaf_for(a, h) for a, h in heads]
    r1 = ra.merkle_root(leaves)
    r2 = ra.merkle_root([ra.leaf_for(a, h) for a, h in heads])
    assert r1 == r2                          # deterministic for a fixed set
    assert len(r1) == 32
    # One head changes → the root changes (tamper evidence).
    heads[1] = ("b", "dd" * 32)
    r3 = ra.merkle_root([ra.leaf_for(a, h) for a, h in heads])
    assert r3 != r1
    # Reordering the SAME set changes the root — leaf order is part of the
    # contract (sorted by analyst_id).
    rev = [ra.leaf_for(a, h) for a, h in reversed(heads)]
    assert ra.merkle_root(rev) != r3
    # Empty set anchors honestly.
    assert ra.merkle_root([]) == hashlib.sha256(b"").digest()


def test_two_calendars_and_version_stamp():
    assert len(ra.CALENDARS) == 2
    assert all("opentimestamps" in c for c in ra.CALENDARS)
    assert ra.RECEIPT_ANCHOR_VERSION == "2026-09/h13"


class _AnchorConn:
    """Heads from the scan; pending rows from the retry pull."""

    def __init__(self, *, heads=(), pending=()):
        self._heads = list(heads)
        self._pending = list(pending)
        self.execs: list[tuple] = []

    async def fetch(self, sql, *args):
        if "DISTINCT ON" in sql:
            return self._heads
        if "status = 'pending'" in sql:
            return self._pending
        raise AssertionError(f"unexpected fetch SQL: {sql[:80]}")

    async def execute(self, sql, *args):
        self.execs.append((sql, args))
        # The (day, calendar) ensure lands a pending row the retry pull then
        # sees — mirror the real table's behaviour.
        if "INSERT INTO receipt_anchors" in sql:
            self._pending.append({
                "id": str(uuid4()), "day": args[0],
                "root_hash": args[1], "calendar": args[3],
            })
        return "UPDATE 1"


def _head(a="analyst_a", h="aa" * 32):
    return {"analyst_id": a, "receipt_hash": h}


@pytest.mark.asyncio
async def test_anchor_run_submits_both_calendars(monkeypatch):
    """A reachable calendar pair: two (day, calendar) rows ensured 'pending',
    both POSTed, both flipped 'submitted' with the proof bytes."""
    sent: list[tuple] = []

    async def _ok(calendar, digest):
        sent.append((calendar, digest))
        return b"\x00proof\x01"

    monkeypatch.setattr(ra, "_submit_digest", _ok)
    conn = _AnchorConn(heads=[_head()])
    result = await ra.handle([], {}, _Deps(_Pool(conn)))
    d = result.finding.data
    assert d["sub_handler"] == "receipt_anchor"
    assert d["leaf_count"] == 1
    assert d["anchors_submitted"] == 2
    assert d["anchors_pending"] == 0
    # The digest POSTed is the computed 32-byte root.
    root = ra.merkle_root([ra.leaf_for("analyst_a", "aa" * 32)])
    assert all(digest == root for _, digest in sent)
    assert d["root_hash"] == root.hex()
    # Ledger writes: 2 ensures + 2 submit updates (proof bytea).
    updates = [a for s, a in conn.execs if "SET status = 'submitted'" in s]
    assert len(updates) == 2
    assert updates[0][1] == b"\x00proof\x01"


@pytest.mark.asyncio
async def test_anchor_ensure_binds_the_day_as_a_date_not_a_string(monkeypatch):
    """The first live tick (2026-09-25 00:10Z) hard-failed with asyncpg's
    "invalid input for query argument $1: '2026-09-25' ('str' object has no
    attribute 'toordinal')": the handler bound the day's ISO STRING to
    ``$1::date``. The fake connection above accepted it; the driver never
    will. The wire argument is a ``datetime.date``; the receipt keeps the
    string."""
    import datetime as _dt

    async def _ok(calendar, digest):
        return b"\x00proof\x01"

    monkeypatch.setattr(ra, "_submit_digest", _ok)
    conn = _AnchorConn(heads=[_head()])
    result = await ra.handle([], {}, _Deps(_Pool(conn)))
    ensures = [a for s, a in conn.execs if "INSERT INTO receipt_anchors" in s]
    assert len(ensures) == 2
    for args in ensures:
        assert isinstance(args[0], _dt.date) and not isinstance(args[0], str), type(args[0])
    assert result.finding.data["day"] == ensures[0][0].isoformat()


@pytest.mark.asyncio
async def test_anchor_calendar_failure_is_pending_never_an_error(monkeypatch):
    """An unreachable calendar → 'pending', retried next tick — and the OTHER
    calendar still submits."""
    async def _fail(calendar, digest):
        if "a.pool" in calendar:
            raise OSError("unreachable")
        return b"proof"

    monkeypatch.setattr(ra, "_submit_digest", _fail)
    conn = _AnchorConn(heads=[_head()])
    result = await ra.handle([], {}, _Deps(_Pool(conn)))
    d = result.finding.data
    assert d["anchors_submitted"] == 1
    assert d["anchors_pending"] == 1


@pytest.mark.asyncio
async def test_anchor_retries_older_pending_rows(monkeypatch):
    """A pending row from a PRIOR day resubmits the root it stored — the day's
    attestation is pinned at mint, not recomputed."""
    sent: list[bytes] = []

    async def _ok(calendar, digest):
        sent.append(digest)
        return b"proof"

    monkeypatch.setattr(ra, "_submit_digest", _ok)
    old_root = ra.merkle_root([ra.leaf_for("old", "11" * 32)]).hex()
    conn = _AnchorConn(
        heads=[_head()],
        pending=[{
            "id": str(uuid4()), "day": "2026-09-20",
            "root_hash": old_root, "calendar": ra.CALENDARS[0],
        }],
    )
    await ra.handle([], {}, _Deps(_Pool(conn)))
    assert bytes.fromhex(old_root) in sent


def test_dispatch_wiring_is_trace_only():
    """The anchor's only persisted product is receipt_anchors rows + the
    analyst_traces receipt — never a finding on a trust surface."""
    from legba.data.analysts import deterministic as det
    from legba.data.provenance.kinds import TRACE_ONLY

    assert det.SUB_HANDLERS["receipt_anchor"] is ra.handle
    assert det.OUTPUT_KIND_BY_SUB_HANDLER["receipt_anchor"] is TRACE_ONLY


def test_descriptor_is_draft_and_budgeted_zero():
    """The descriptor ships inert: state draft (registration + activate are
    deploy steps) and a zero token budget."""
    import yaml

    text = (REPO_ROOT / "descriptors/analyst_receipt_anchor.yaml").read_text()
    doc = yaml.safe_load(text)
    assert doc["identity"]["id"] == "receipt_anchor"
    assert doc["identity"]["state"] == "draft"
    assert doc["method"]["sub_handler"] == "receipt_anchor"
    assert doc["method"]["budget_tokens_per_day"] == 0
    assert doc["cadence"]["fallback_schedule"] == "10 0 * * *"


@pytest.mark.asyncio
async def test_anchor_ensure_sql_on_the_real_driver_takes_a_date_and_refuses_a_string(migrated_pg):
    """The proof the fake connection cannot give: against the real driver and
    the real ``receipt_anchors`` table, ``_ENSURE_SQL`` lands a row for a
    ``datetime.date`` and raises ``DataError`` for the ISO string the first
    live tick sent."""
    import datetime as _dt

    import asyncpg

    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        day = _dt.date(2001, 1, 1)
        await conn.execute("DELETE FROM receipt_anchors WHERE day = $1", day)
        await conn.execute(ra._ENSURE_SQL, day, "ab" * 32, 1, "test-calendar")  # noqa: SLF001
        row = await conn.fetchrow(
            "SELECT day, status FROM receipt_anchors WHERE day = $1 AND calendar = $2",
            day, "test-calendar",
        )
        assert row is not None and row["status"] == "pending" and row["day"] == day
        with pytest.raises(asyncpg.DataError):
            await conn.execute(ra._ENSURE_SQL, day.isoformat(), "ab" * 32, 1, "test-calendar-2")  # noqa: SLF001
    finally:
        try:
            await conn.execute("DELETE FROM receipt_anchors WHERE day = $1", _dt.date(2001, 1, 1))
        finally:
            await conn.close()


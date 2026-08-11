# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the ``corpus_retention`` sub-handler — the OpenSearch corpus DELETE
path (W2-C).

The corpus had no delete path of ANY kind until 2026-08-03: ``OpenSearchStore``
exposed index/search/get and nothing else, so every signals purge in the
platform's history left its documents behind (measured: 75,871 of 182,648 docs,
41.5%, orphaned). These tests are DETERMINISTIC and need no live substrate /
OpenSearch — they cover:

  * **Registration** — a first-class registered deterministic sub-handler with a
    declared options catalog entry and a TRACE_ONLY output kind.
  * **Synthetic** (``deps=None``) — no substrate → a zeroed, well-formed run.
  * **The no-store guard** — a wired pool but no OpenSearchStore no-ops LOUDLY
    and leaves the queue intact, rather than silently reporting success.
  * **The safety gate** — the load-bearing invariant of the whole design: a
    tombstone whose ``signals`` row is STILL ALIVE is never deleted. If this
    regresses, a bad queue row destroys live documents.
  * **Idempotency** — an already-absent doc counts as deleted (a 404 IS the
    desired end state), which is what makes the retry free.
  * **The SQL contract** — the drain, the gauge and the tombstone writers must
    agree on the predicate; a divergence silently zeroes the gauge or the drain.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    run_method,
)
from legba.data.analysts.deterministic_handlers import corpus_retention
from legba.data.analysts.handler_options import HANDLER_OPTIONS
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult

SUB = "corpus_retention"


# ---------------------------------------------------------------------------
# Fakes — a pool and a store with the surfaces the sweep actually touches
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self, rows: list[dict[str, Any]], pending: int) -> None:
        self._rows = rows
        self._pending = pending
        self.executed: list[tuple[str, tuple[Any, ...]]] = []

    async def fetch(self, sql: str, *args: Any) -> list[dict[str, Any]]:
        return self._rows

    async def fetchval(self, sql: str, *args: Any) -> int:
        return self._pending

    async def execute(self, sql: str, *args: Any) -> str:
        self.executed.append((sql, args))
        return "UPDATE 0"


class _FakePool:
    def __init__(self, conn: _FakeConn) -> None:
        self._conn = conn

    def acquire(self) -> Any:
        conn = self._conn

        class _Ctx:
            async def __aenter__(self) -> _FakeConn:
                return conn

            async def __aexit__(self, *exc: Any) -> bool:
                return False

        return _Ctx()


class _FakeStore:
    """Deletes everything asked of it, recording the ids."""

    class _Cfg:
        index = "legba_signals_corpus"

    cfg = _Cfg()

    def __init__(self) -> None:
        self.deleted: list[str] = []

    async def bulk_delete(self, index: str, doc_ids: Any) -> int:
        ids = list(doc_ids)
        self.deleted.extend(ids)
        return len(ids)


class _Deps:
    def __init__(self, pool: Any, extras: dict[str, Any]) -> None:
        self.pg_pool = pool
        self.extras = extras


def _tombstone(doc_id: Any, *, alive: bool = False, attempts: int = 0) -> dict[str, Any]:
    return {
        "doc_id": doc_id,
        "index_name": "legba_signals_corpus",
        "attempts": attempts,
        "row_alive": alive,
    }


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_corpus_retention_registered():
    assert SUB in SUB_HANDLERS, "corpus_retention missing from SUB_HANDLERS"
    assert SUB in OUTPUT_KIND_BY_SUB_HANDLER
    assert SUB_HANDLERS[SUB] is corpus_retention.handle


def test_corpus_retention_has_a_declared_options_catalog_entry():
    """Every registered sub-handler must declare its knobs (X-1 contract)."""
    assert SUB in HANDLER_OPTIONS
    assert {o.name for o in HANDLER_OPTIONS[SUB]} == {"batch_limit"}


def test_output_kind_is_trace_only():
    """Its real product is the OpenSearch delete, not an analytical finding."""
    from legba.data.analysts.deterministic import TRACE_ONLY

    assert OUTPUT_KIND_BY_SUB_HANDLER[SUB] is TRACE_ONLY


# ---------------------------------------------------------------------------
# Synthetic path — no substrate, zeroed run, never spends tokens
# ---------------------------------------------------------------------------


async def test_synthetic_no_deps_zeroed_run():
    result = await run_method(
        [], {"sub_handler": SUB, "analyst_id": "cr", "run_id": uuid4()}, None,
    )
    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)
    data = result.finding.data
    assert data["sub_handler"] == SUB
    assert data["examined"] == 0
    assert data["deleted"] == 0
    assert data["pending"] == 0
    assert result.usage == {
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
    }


async def test_no_store_wired_noops_and_says_so():
    """A wired pool but no store must NOT report a successful empty drain."""
    conn = _FakeConn([], 0)
    deps = _Deps(_FakePool(conn), extras={})
    result = await corpus_retention.handle([], {}, deps)
    data = result.finding.data
    assert data["skipped_no_store"] == 1
    assert data["deleted"] == 0
    assert not conn.executed, "no-store path must not touch the queue"


# ---------------------------------------------------------------------------
# THE SAFETY GATE — the invariant the whole design rests on
# ---------------------------------------------------------------------------


async def test_a_tombstone_whose_row_is_alive_is_never_deleted():
    """A tombstone is a CLAIM that a row is gone; the drain re-verifies it.

    If this regresses, a mistaken or stale queue row destroys a LIVE document —
    the one genuinely unrecoverable outcome in this subsystem.
    """
    alive_id, dead_id = uuid4(), uuid4()
    conn = _FakeConn(
        [_tombstone(alive_id, alive=True), _tombstone(dead_id, alive=False)], 0
    )
    store = _FakeStore()
    deps = _Deps(_FakePool(conn), {corpus_retention.OS_DEPS_EXTRA_KEY: store})

    result = await corpus_retention.handle([], {}, deps)
    data = result.finding.data

    assert store.deleted == [str(dead_id)], "the live row's doc must survive"
    assert data["skipped_row_alive"] == 1
    assert data["deleted"] == 1
    assert data["examined"] == 2


async def test_all_alive_deletes_nothing_at_all():
    conn = _FakeConn([_tombstone(uuid4(), alive=True) for _ in range(3)], 0)
    store = _FakeStore()
    deps = _Deps(_FakePool(conn), {corpus_retention.OS_DEPS_EXTRA_KEY: store})

    result = await corpus_retention.handle([], {}, deps)
    assert store.deleted == []
    assert result.finding.data["skipped_row_alive"] == 3
    assert result.finding.data["deleted"] == 0


# ---------------------------------------------------------------------------
# Draining
# ---------------------------------------------------------------------------


async def test_drains_and_stamps_the_confirmed_ids():
    ids = [uuid4() for _ in range(4)]
    conn = _FakeConn([_tombstone(i) for i in ids], 0)
    store = _FakeStore()
    deps = _Deps(_FakePool(conn), {corpus_retention.OS_DEPS_EXTRA_KEY: store})

    result = await corpus_retention.handle([], {}, deps)
    data = result.finding.data

    assert sorted(store.deleted) == sorted(str(i) for i in ids)
    assert data["deleted"] == 4
    assert data["failures"] == 0
    stamped = [sql for sql, _ in conn.executed if "purged_at = now()" in sql]
    assert len(stamped) == 1, "confirmed ids must be stamped exactly once"


async def test_a_short_delete_count_requeues_rather_than_stamping():
    """We cannot tell WHICH id failed, so the batch stays queued. The retry is
    free — deleting an already-absent doc succeeds."""

    class _ShortStore(_FakeStore):
        async def bulk_delete(self, index: str, doc_ids: Any) -> int:
            ids = list(doc_ids)
            self.deleted.extend(ids)
            return len(ids) - 1

    conn = _FakeConn([_tombstone(uuid4()) for _ in range(3)], 3)
    store = _ShortStore()
    deps = _Deps(_FakePool(conn), {corpus_retention.OS_DEPS_EXTRA_KEY: store})

    result = await corpus_retention.handle([], {}, deps)
    assert result.finding.data["failures"] == 1
    assert not [sql for sql, _ in conn.executed if "purged_at = now()" in sql]
    assert [sql for sql, _ in conn.executed if "attempts = attempts + 1" in sql]


async def test_a_transport_failure_leaves_the_queue_untouched():
    class _BoomStore(_FakeStore):
        async def bulk_delete(self, index: str, doc_ids: Any) -> int:
            raise RuntimeError("connection reset")

    conn = _FakeConn([_tombstone(uuid4())], 1)
    deps = _Deps(_FakePool(conn), {corpus_retention.OS_DEPS_EXTRA_KEY: _BoomStore()})

    result = await corpus_retention.handle([], {}, deps)
    # Degrades to a zeroed run; nothing stamped, so the batch retries next tick.
    assert result.finding.data["deleted"] == 0
    assert not [sql for sql, _ in conn.executed if "purged_at = now()" in sql]


async def test_batch_limit_option_is_honored():
    conn = _FakeConn([], 0)
    seen: dict[str, Any] = {}

    async def _fetch(sql: str, *args: Any) -> list[dict[str, Any]]:
        seen["limit"] = args[0]
        return []

    conn.fetch = _fetch  # type: ignore[assignment]
    deps = _Deps(_FakePool(conn), {corpus_retention.OS_DEPS_EXTRA_KEY: _FakeStore()})
    await corpus_retention.handle([], {"batch_limit": 77}, deps)
    assert seen["limit"] == 77


# ---------------------------------------------------------------------------
# The SQL contract — the drain, the gauge and the writers must agree
# ---------------------------------------------------------------------------


def test_drain_select_anti_joins_signals():
    """The safety gate lives in SQL as well as in Python; both must hold."""
    sql = corpus_retention._SELECT_DRAINABLE_SQL
    assert "EXISTS" in sql and "FROM signals" in sql, (
        "the drain SELECT must report whether the signals row is still alive"
    )
    assert "purged_at IS NULL" in sql
    assert "attempts <" in sql


def test_gauge_backlog_matches_the_drain_predicate():
    """The declared backlog and the drain must measure the SAME queue.

    If the gauge counts rows the drain will never attempt (or vice versa), the
    S-1 loop either shows a permanent deficit nobody can clear or reads zero
    while the queue grows — which is precisely how the orphan population went
    unnoticed for a month.
    """
    from legba.data.registry.production_gauge import BACKLOG_DRAINS

    drain = next(
        (d for d in BACKLOG_DRAINS if d.backlog_id == "corpus_tombstone_drain"), None
    )
    assert drain is not None, "corpus_tombstone_drain must be a declared backlog"
    assert drain.owner_analyst_id == SUB
    assert "corpus_tombstones" in drain.overdue_sql
    assert "purged_at IS NULL" in drain.overdue_sql
    assert "NOT EXISTS" in drain.overdue_sql, (
        "the gauge must exclude live-row tombstones exactly as the drain does"
    )
    assert f"attempts < {corpus_retention._MAX_ATTEMPTS}" in drain.overdue_sql, (
        "the gauge's give-up threshold must track corpus_retention._MAX_ATTEMPTS"
    )


def test_retention_sweep_tombstones_in_the_same_transaction_as_the_delete():
    """The purge and the tombstone must land together or not at all."""
    import inspect

    from legba.data.analysts.deterministic_handlers import _retention_sweep

    src = inspect.getsource(_retention_sweep._purge_signals)
    assert "corpus_tombstones" in src, (
        "the signals purge must record a corpus tombstone for every deleted row"
    )
    delete_at = src.index("DELETE FROM signals")
    insert_at = src.index("INSERT INTO corpus_tombstones")
    txn_at = src.index("conn.transaction()")
    assert txn_at < delete_at < insert_at, (
        "the tombstone INSERT must sit inside the same transaction as the DELETE"
    )


@pytest.mark.parametrize(
    "reason", ["signals_retention", "intrasource_collapse", "orphan_backfill"]
)
def test_every_tombstone_writer_stamps_a_reason(reason: str) -> None:
    """The audit trail — 'which docs did we drop, when, and why' is a SELECT."""
    import inspect
    from pathlib import Path

    from legba.data.analysts.deterministic_handlers import _retention_sweep

    haystack = inspect.getsource(_retention_sweep)
    root = Path(__file__).resolve().parents[2]
    for rel in (
        "scripts/collapse_intrasource_dupes.py",
        "scripts/seed_corpus_orphan_tombstones.py",
    ):
        haystack += (root / rel).read_text(encoding="utf-8")
    assert f"'{reason}'" in haystack

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D11 — deterministic, fork-safe receipt-chain head derivation (unit).

The production wiring tests in ``tests/runtime/test_receipt_chain_wiring.py``
are ``@pytest.mark.integration`` (real Postgres). These are PURE-PYTHON unit
tests over :meth:`RuntimeReceiptChain._head_locked` / ``head`` / ``head_tip_count``
using an in-memory fake connection, so they run with no external services.

What D11 fixed
--------------
The old head rule was ``ORDER BY run_started_at DESC LIMIT 1`` — non-deterministic
once a chain FORKS (two rows sharing a ``prev_receipt_hash``, which happens across
a process recreate that lost the in-memory head, or two concurrent same-analyst
runs racing the prev pointer). "Most recent overall row" can pick a NON-tip and
deepen the fork on the next ``record()``.

The new rule derives the head as a TIP: a ``receipt_hash`` that no other row in
the chain references as its ``prev_receipt_hash``. A linear chain has exactly one
tip; a forked chain has several, and the writer picks one DETERMINISTICALLY
(newest by run_started_at, ties broken by run_id text) so the choice is STABLE
across recreate and future runs extend the same tip.

The fake below emulates ONLY the two SQL shapes ``_head_locked`` issues (the tip
SELECT, the COUNT, the fallback SELECT) + the ``head_tip_count`` COUNT, applying
the exact tip semantics in Python — so the test asserts the orchestration logic,
not the SQL engine.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID, uuid4

import pytest

from legba.data.provenance._core import ZERO_HASH
from legba.data.provenance.receipts import RuntimeReceiptChain


# ---------------------------------------------------------------------------
# In-memory fake pool / connection
# ---------------------------------------------------------------------------


@dataclass
class _Row:
    run_id: UUID
    receipt_hash: str
    prev_receipt_hash: str | None
    run_started_at: float  # monotonically-increasing float stands in for ts


class _FakeConn:
    """Emulates the handful of queries ``_head_locked`` / ``head_tip_count`` run.

    Routes on stable keywords in the SQL text. The semantics mirror the real
    queries: a TIP is a receipt_hash that is not any row's prev_receipt_hash.
    """

    def __init__(self, rows: list[_Row]):
        self._rows = rows

    def _tips(self) -> list[_Row]:
        prevs = {
            r.prev_receipt_hash
            for r in self._rows
            if r.prev_receipt_hash is not None
        }
        return [r for r in self._rows if r.receipt_hash not in prevs]

    @staticmethod
    def _order_key(r: _Row) -> tuple[float, str]:
        # ORDER BY run_started_at DESC, run_id::text DESC → max() over this key.
        return (r.run_started_at, str(r.run_id))

    async def fetchrow(self, sql: str, analyst_id: str):
        rows = [r for r in self._rows if True]  # single-analyst fake
        if "NOT IN" in sql:
            # The deterministic tip SELECT.
            tips = self._tips()
            if not tips:
                return None
            winner = max(tips, key=self._order_key)
            return {"receipt_hash": winner.receipt_hash}
        # Fallback "latest row" SELECT (no NOT IN).
        if not rows:
            return None
        winner = max(rows, key=self._order_key)
        return {"receipt_hash": winner.receipt_hash}

    async def fetchval(self, sql: str, analyst_id: str):
        if "NOT IN" in sql:
            # head_tip_count COUNT over tips.
            return len(self._tips())
        # COUNT(*) over all rows.
        return len(self._rows)


class _FakeAcquire:
    def __init__(self, conn: _FakeConn):
        self._conn = conn

    async def __aenter__(self) -> _FakeConn:
        return self._conn

    async def __aexit__(self, *exc) -> None:
        return None


class _FakePool:
    def __init__(self, rows: list[_Row]):
        self._conn = _FakeConn(rows)

    def acquire(self) -> _FakeAcquire:
        return _FakeAcquire(self._conn)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


async def test_head_empty_chain_is_zero_hash() -> None:
    chain = RuntimeReceiptChain(_FakePool([]))  # type: ignore[arg-type]
    assert await chain.head("a") == ZERO_HASH
    assert await chain.count("a") == 0


async def test_head_linear_chain_returns_unique_tip() -> None:
    """A clean linear chain (each row chains the previous) → the single tip,
    NOT necessarily the most-recently-started row."""
    h1, h2, h3 = "1" * 64, "2" * 64, "3" * 64
    rows = [
        _Row(uuid4(), h1, ZERO_HASH, run_started_at=10.0),
        _Row(uuid4(), h2, h1, run_started_at=11.0),
        _Row(uuid4(), h3, h2, run_started_at=12.0),  # the tip
    ]
    chain = RuntimeReceiptChain(_FakePool(rows))  # type: ignore[arg-type]
    assert await chain.head("a") == h3
    assert await chain.head_tip_count("a") == 1


async def test_head_ignores_most_recent_when_it_is_not_the_tip() -> None:
    """REGRESSION for the old rule: the newest row by run_started_at is a
    NON-tip (some other row chains off it). The deterministic rule must return
    the actual tip, not the newest row."""
    h1, h2, h3 = "a" * 64, "b" * 64, "c" * 64
    # h2 is the genuine tip (no row has prev=h2). h3 was started LATER but is an
    # interior node because h-something... here we make h1 the latest-started
    # interior node to model "newest row is not the tip".
    rows = [
        _Row(uuid4(), h1, ZERO_HASH, run_started_at=99.0),  # newest, interior
        _Row(uuid4(), h3, h1, run_started_at=50.0),         # chains off h1
        _Row(uuid4(), h2, h3, run_started_at=51.0),         # the real tip
    ]
    chain = RuntimeReceiptChain(_FakePool(rows))  # type: ignore[arg-type]
    # Old rule would return h1 (run_started_at=99). New rule returns the tip h2.
    assert await chain.head("a") == h2
    assert await chain.head_tip_count("a") == 1


async def test_forked_chain_head_is_deterministic_and_stable() -> None:
    """Two rows share prev_receipt_hash → a fork with TWO tips. The head pick is
    deterministic (newest tip, ties broken by run_id) and STABLE across repeated
    derivation (modelling a recreate that drops the in-memory head)."""
    base = "0" * 63 + "f"
    tip_a, tip_b = "a" * 64, "b" * 64
    ra = _Row(uuid4(), tip_a, base, run_started_at=20.0)
    rb = _Row(uuid4(), tip_b, base, run_started_at=21.0)  # newer → wins
    root = _Row(uuid4(), base, ZERO_HASH, run_started_at=10.0)
    rows = [root, ra, rb]

    # Two tips exist (the fork is observable).
    chain1 = RuntimeReceiptChain(_FakePool(rows))  # type: ignore[arg-type]
    assert await chain1.head_tip_count("a") == 2
    head1 = await chain1.head("a")
    assert head1 == tip_b  # newest tip by run_started_at

    # A FRESH chain (recreate; in-memory head lost) derives the SAME head.
    chain2 = RuntimeReceiptChain(_FakePool(rows))  # type: ignore[arg-type]
    assert await chain2.head("a") == head1


async def test_forked_chain_tie_broken_by_run_id_text() -> None:
    """When two tips share run_started_at, the tie breaks on run_id::text DESC —
    fully deterministic."""
    base = "0" * 63 + "e"
    tip_a, tip_b = "a" * 64, "b" * 64
    # Force run_ids whose text ordering is known.
    rid_low = UUID(int=1)
    rid_high = UUID(int=2)
    rows = [
        _Row(rid_low, tip_a, base, run_started_at=30.0),
        _Row(rid_high, tip_b, base, run_started_at=30.0),  # same ts
        _Row(uuid4(), base, ZERO_HASH, run_started_at=10.0),
    ]
    chain = RuntimeReceiptChain(_FakePool(rows))  # type: ignore[arg-type]
    # run_id::text DESC → "00000000-...-0002" > "...-0001" → tip_b wins.
    assert await chain.head("a") == tip_b


async def test_head_is_cached_after_first_derivation() -> None:
    """Once derived, head() returns the in-memory pointer without re-querying."""
    h1 = "1" * 64
    rows = [_Row(uuid4(), h1, ZERO_HASH, run_started_at=10.0)]
    chain = RuntimeReceiptChain(_FakePool(rows))  # type: ignore[arg-type]
    assert await chain.head("a") == h1
    # Mutate the underlying fake; cached head must not change.
    chain._heads["a"] = "cached-sentinel"  # noqa: SLF001
    assert await chain.head("a") == "cached-sentinel"


async def test_head_tip_count_defaults_to_bound_analyst() -> None:
    """A chain constructed with a bound analyst_id can omit it on the
    diagnostic call."""
    h1 = "1" * 64
    rows = [_Row(uuid4(), h1, ZERO_HASH, run_started_at=10.0)]
    chain = RuntimeReceiptChain(_FakePool(rows), analyst_id="bound")  # type: ignore[arg-type]
    assert await chain.head_tip_count() == 1


async def test_head_tip_count_requires_an_analyst_id_when_unbound() -> None:
    chain = RuntimeReceiptChain(_FakePool([]))  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        await chain.head_tip_count()

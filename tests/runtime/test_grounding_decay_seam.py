# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C4 — the fact-decay CONSUMPTION seam in grounding (flag
``LEGBA_FACT_DECAY_WEIGHTING``, default OFF).

The load-bearing invariant: with the flag UNSET the grounding fact read (SQL,
bound params, and the rendered preamble) is BYTE-IDENTICAL to the pre-C4
behavior — no join, no revoke exclusion, no decay annotation. Flag ON: the
sidecar is joined, revoke candidates are excluded in SQL, and aging/stale
lines are annotated. Pure (the recording ``_StubPool``); no DB.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from legba.runtime.grounding import (
    GroundingFact,
    SubstrateGroundingResolver,
    build_grounding_preamble,
)


# ---------------------------------------------------------------------------
# Recording stub pool (mirror of tests/runtime/test_grounding.py).
# ---------------------------------------------------------------------------


class _StubConn:
    def __init__(self, rows: dict[str, list[dict[str, Any]]], log: list):
        self._rows = rows
        self._log = log

    async def fetch(self, sql: str, *params: Any) -> list[dict[str, Any]]:
        self._log.append((sql, params))
        if "FROM facts" in sql:
            return self._rows.get("facts", [])
        if "FROM nexuses" in sql:
            return self._rows.get("nexuses", [])
        return []


class _StubAcquire:
    def __init__(self, conn: _StubConn):
        self._conn = conn

    async def __aenter__(self) -> _StubConn:
        return self._conn

    async def __aexit__(self, *exc: Any) -> None:
        return None


class _StubPool:
    def __init__(self, rows: dict[str, list[dict[str, Any]]] | None = None):
        self.log: list = []
        self._conn = _StubConn(rows or {}, self.log)

    def acquire(self) -> _StubAcquire:
        return _StubAcquire(self._conn)


# The frozen pre-C4 fact SQL — the exact string _query_facts must emit when the
# flag is unset. If C4 ever changes this branch, THIS assertion fails loud.
_BASELINE_FACTS_SQL = """
            SELECT f.subject, f.predicate, f.value, f.valid_from,
                   f.source_type, f.confidence,
                   (f.contested AND fc.status IN ('contested','surfaced'))
                       AS contested,
                   COALESCE(f.surfaced_winner, false) AS surfaced_winner,
                   fc.value_count AS contention_value_count
            FROM facts f
            LEFT JOIN fact_contention fc ON fc.id = f.contention_id
            WHERE lower(f.subject) = ANY($1::text[])
              AND f.source_type = ANY($2::text[])
              AND f.superseded_by IS NULL
              AND (f.valid_until IS NULL OR f.valid_until > now())
              AND f.value !~ '^Q[0-9]+$'
            ORDER BY
              (f.source_type IN ('seed','curated')) DESC,
              f.confidence DESC NULLS LAST,
              f.valid_from DESC NULLS LAST
            LIMIT $3
        """


def _facts_sql(pool: _StubPool) -> str:
    return next(sql for sql, _ in pool.log if "FROM facts" in sql)


# ---------------------------------------------------------------------------
# OFF invariant (the shipped default)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_facts_sql_is_byte_identical_to_pre_c4(monkeypatch):
    monkeypatch.delenv("LEGBA_FACT_DECAY_WEIGHTING", raising=False)
    pool = _StubPool(rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["United States"], max_facts=30)
    sql = _facts_sql(pool)
    assert sql == _BASELINE_FACTS_SQL
    # None of the C4 surfaces leak into the OFF read.
    assert "fact_decay_states" not in sql
    assert "revoke_candidate" not in sql
    assert "decayed_confidence" not in sql


@pytest.mark.asyncio
async def test_flag_off_render_is_byte_identical(monkeypatch):
    """A GroundingFact with no decay readout renders EXACTLY the pre-C4 line."""
    monkeypatch.delenv("LEGBA_FACT_DECAY_WEIGHTING", raising=False)
    f = GroundingFact(
        subject="United States", predicate="leader of", value="Donald Trump",
        valid_from=datetime(2025, 1, 20, tzinfo=timezone.utc),
        source_type="seed", confidence=0.95,
    )
    assert f.render() == "United States — leader of: Donald Trump (since 2025-01-20)"
    assert f.decayed_confidence is None and f.decay_state is None


@pytest.mark.asyncio
async def test_flag_off_row_without_decay_columns_is_accepted(monkeypatch):
    """The OFF SQL never SELECTs the decay columns; the resolver must still
    build facts from such rows (backward-compatible _row_get degrade)."""
    monkeypatch.delenv("LEGBA_FACT_DECAY_WEIGHTING", raising=False)
    rows = {
        "facts": [
            {
                "subject": "United States", "predicate": "leader of",
                "value": "Donald Trump",
                "valid_from": datetime(2025, 1, 20, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.95,
                "contested": False, "surfaced_winner": False,
                "contention_value_count": None,
            }
        ],
        "nexuses": [],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(rows=rows))
    facts, _ = await resolver.resolve(["United States"], max_facts=30)
    assert len(facts) == 1
    assert facts[0].decay_state is None
    preamble = build_grounding_preamble(facts, [])
    assert preamble is not None
    assert "DECAYED" not in preamble and "AGING" not in preamble


# ---------------------------------------------------------------------------
# ON behavior
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_on_joins_sidecar_and_excludes_revoke_candidates(monkeypatch):
    monkeypatch.setenv("LEGBA_FACT_DECAY_WEIGHTING", "1")
    pool = _StubPool(rows={"facts": [], "nexuses": []})
    resolver = SubstrateGroundingResolver(pg_pool=pool)
    await resolver.resolve(["United States"], max_facts=30)
    sql = _facts_sql(pool)
    assert "LEFT JOIN fact_decay_states fds ON fds.fact_id = f.id" in sql
    assert "fds.decay_state <> 'revoke_candidate'" in sql
    assert "fds.decayed_confidence AS decayed_confidence" in sql
    # The provenance + QID gates from the baseline still hold under ON.
    assert "source_type = ANY($2::text[])" in sql
    assert "value !~ '^Q[0-9]+$'" in sql


@pytest.mark.asyncio
async def test_flag_on_threads_decay_readout_and_annotates(monkeypatch):
    monkeypatch.setenv("LEGBA_FACT_DECAY_WEIGHTING", "1")
    rows = {
        "facts": [
            {
                "subject": "Iran", "predicate": "hostile to",
                "value": "Israel",
                "valid_from": datetime(2024, 1, 1, tzinfo=timezone.utc),
                "source_type": "seed", "confidence": 0.9,
                "contested": False, "surfaced_winner": False,
                "contention_value_count": None,
                "decayed_confidence": 0.42, "decay_state": "aging",
            }
        ],
        "nexuses": [],
    }
    resolver = SubstrateGroundingResolver(pg_pool=_StubPool(rows=rows))
    facts, _ = await resolver.resolve(["Iran"], max_facts=30)
    assert len(facts) == 1
    assert facts[0].decay_state == "aging"
    assert facts[0].decayed_confidence == pytest.approx(0.42)
    line = facts[0].render()
    assert "[AGING: not recently re-observed; decayed confidence 0.42]" in line


@pytest.mark.parametrize(
    "state,should_annotate",
    [("fresh", False), ("aging", True), ("stale", True)],
)
def test_decay_suffix_only_for_aging_and_stale(state, should_annotate):
    f = GroundingFact(
        subject="X", predicate="member of", value="Y",
        valid_from=None, source_type="seed", confidence=0.8,
        decayed_confidence=0.3, decay_state=state,
    )
    annotated = "not recently re-observed" in f.render()
    assert annotated is should_annotate

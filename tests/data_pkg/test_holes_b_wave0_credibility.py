# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Holes-B Wave 0 — facts.source_credibility + the two A-leg fixes.

Pure-logic (no-DB) tests for the fact-write-path foundation of the
contested-claims arbiter (#101):

  * facts.source_credibility resolution — backing-signal MAX overrides the
    source-tier nominal; tier nominal (seed/curated 0.9, agent/ingestion 0.5)
    is the fallback when there is no signal lineage / every signal is unscored.
  * the ingestion producer threads incoming_source_type='ingestion' into
    supersede_prior_facts so the A1 tier guard ENGAGES (an ingestion fact can no
    longer close a seed/curated open row).
  * both producers stamp source_credibility on the INSERT and merge it (max) on
    a same-value re-assert.
  * the ingestion same-value confidence merge uses the bounded noisy-OR
    (capped 0.99), matching the analyst path (was GREATEST/max).

These run WITHOUT a live Postgres: a ``RecordingConn`` captures the SQL + params
each helper issues and returns scripted results. The DB round-trips
(credibility actually populated from a backing signal's score; an ingestion fact
NOT closing a seed row) live in tests/data_pkg/test_writes_fact.py and
tests/data_pkg/test_filter_fact_extractor.py (those need a migrated DB).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.filters.fact_extractor import _insert_ingestion_fact
from legba.data.provenance import AnalystContext
from legba.data.provenance._core import from_analyst
from legba.data.provenance.models import FactPayload
from legba.data.provenance.writes import (
    _insert_fact,
    max_signal_credibility,
    resolve_fact_source_credibility,
    source_tier_credibility,
)


NOW = datetime(2026, 6, 29, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def _pin_fact_contention_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """This file tests the Holes-B **Wave 0** A1/A2 contract (tier-nominal
    credibility stamping, noisy-OR merge math, the ingestion rank guard) — a
    layer that predates and is orthogonal to the Wave 4 contention-coexist
    feature. The live deploy's ``.env`` sets ``LEGBA_FACT_CONTENTION=1``
    (loaded eagerly at ``legba.data.config`` import time), which would route
    every ``supersede_prior_facts()`` call here through
    ``_supersede_prior_facts_coexist()`` — a FETCH-then-decide path that calls
    ``conn.fetch(...)``, a method ``RecordingConn`` below does not implement
    (none of these tests exercise multi-row coexistence; that is
    ``test_writes_contention_coexist.py``'s job). Pin the flag OFF so this
    file always exercises the OFF-path contract it is named for, regardless
    of the host's ``.env``.
    """
    monkeypatch.setenv("LEGBA_FACT_CONTENTION", "0")


# ---------------------------------------------------------------------------
# Fake conn — records every execute/fetchval and returns scripted results.
# (Same shape as tests/data_pkg/test_writes_d15_d17_d18.py::RecordingConn.)
# ---------------------------------------------------------------------------


class RecordingConn:
    """Minimal asyncpg.Connection stand-in.

    ``fetchval_results`` is popped FIFO for each fetchval call. The credibility
    resolver issues a fetchval (the signals MAX) BEFORE the collapse/dedup
    probe, so a caller scripting both must order them: [credibility, collapse].
    A credibility resolution over an EMPTY derived_from issues NO fetchval (it
    short-circuits to the tier nominal), so those callers script only the
    collapse probe.
    """

    def __init__(self, fetchval_results: list[Any] | None = None) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchvals: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchval_results = list(fetchval_results or [])

    async def execute(self, sql: str, *params: Any) -> str:
        self.executes.append((sql, params))
        return "UPDATE 0"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self.fetchvals.append((sql, params))
        if self._fetchval_results:
            return self._fetchval_results.pop(0)
        return None

    def insert_into(self, table: str) -> tuple[str, tuple[Any, ...]] | None:
        for sql, params in self.executes:
            if f"INSERT INTO {table}" in sql:
                return sql, params
        return None

    def supersede_exec(self) -> tuple[str, tuple[Any, ...]] | None:
        for sql, params in self.executes:
            if "UPDATE facts" in sql and "superseded_by" in sql:
                return sql, params
        return None


def _ctx(target_id: str | None = None) -> AnalystContext:
    return AnalystContext(
        analyst_id="analyst.holes_b_test",
        analyst_version="vdeadbeef",
        run_id=uuid4(),
        target_id=target_id,
        target_version="abc123" if target_id else None,
    )


# ===========================================================================
# source_tier_credibility — the per-tier nominal prior
# ===========================================================================


def test_tier_credibility_nominals():
    """seed/curated -> 0.9, agent/ingestion -> 0.5 (Lewis 2026-06-29)."""
    assert source_tier_credibility("seed") == 0.9
    assert source_tier_credibility("curated") == 0.9
    assert source_tier_credibility("agent") == 0.5
    assert source_tier_credibility("ingestion") == 0.5
    # case / whitespace tolerant
    assert source_tier_credibility("  Seed ") == 0.9
    assert source_tier_credibility("CURATED") == 0.9
    # unknown / None -> machine-extracted nominal (0.5), never authoritative
    assert source_tier_credibility("frobnicator") == 0.5
    assert source_tier_credibility("") == 0.5
    assert source_tier_credibility(None) == 0.5


# ===========================================================================
# resolve_fact_source_credibility — backing-signal MAX overrides the nominal
# ===========================================================================


def test_resolve_credibility_empty_lineage_falls_back_to_nominal():
    """No signal lineage -> NO signals query is issued, the tier nominal wins."""
    conn = RecordingConn()
    val = asyncio.run(
        resolve_fact_source_credibility(conn, source_type="seed", derived_from=[])
    )
    assert val == 0.9
    assert conn.fetchvals == [], "empty lineage must not query signals"


def test_resolve_credibility_unscored_signals_fall_back_to_nominal():
    """Backing signals all NULL (max -> NULL) -> fall back to the tier nominal."""
    sig = uuid4()
    conn = RecordingConn(fetchval_results=[None])  # signals MAX is NULL
    val = asyncio.run(
        resolve_fact_source_credibility(
            conn, source_type="ingestion", derived_from=[sig]
        )
    )
    assert val == 0.5  # ingestion nominal
    # the signals MAX query ran and keyed on the backing ids
    assert len(conn.fetchvals) == 1
    sql, params = conn.fetchvals[0]
    assert "max(source_credibility)" in sql and "FROM signals" in sql
    assert params[0] == [sig]


def test_resolve_credibility_backing_signal_score_overrides_nominal():
    """A scored backing signal (0.9) OVERRIDES the ingestion nominal (0.5)."""
    sig = uuid4()
    conn = RecordingConn(fetchval_results=[0.9])
    val = asyncio.run(
        resolve_fact_source_credibility(
            conn, source_type="ingestion", derived_from=[sig]
        )
    )
    assert val == pytest.approx(0.9)


def test_resolve_credibility_lower_signal_score_still_overrides_nominal():
    """The backing-signal MAX wins even when it is BELOW the tier nominal —
    the real per-host score is authoritative over the coarse prior."""
    sig = uuid4()
    conn = RecordingConn(fetchval_results=[0.3])
    val = asyncio.run(
        resolve_fact_source_credibility(
            conn, source_type="seed", derived_from=[sig]
        )
    )
    assert val == pytest.approx(0.3)


def test_max_signal_credibility_empty_is_none_no_query():
    conn = RecordingConn()
    val = asyncio.run(max_signal_credibility(conn, []))
    assert val is None
    assert conn.fetchvals == []


# ===========================================================================
# Analyst producer (_insert_fact) — stamps source_credibility on the INSERT
# ===========================================================================


def test_insert_fact_stamps_tier_nominal_when_no_lineage():
    """An analyst FactPayload with NO signal lineage lands the tier nominal:
    'agent' default -> 0.5; the INSERT binds it on the source_credibility
    column."""
    conn = RecordingConn(fetchval_results=[None])  # collapse miss -> insert
    ctx = _ctx()
    payload = FactPayload(
        subject="A", predicate="p", value="v", confidence=0.6, valid_from=NOW
    )
    asyncio.run(
        _insert_fact(
            conn,
            row_id=uuid4(),
            payload=payload,
            prov=from_analyst(
                ctx, schema_uri="iglu:legba/fact/jsonschema/2-0-0", derived_from=[]
            ),
            produced_at=NOW,
            effective_schema_uri="iglu:legba/fact/jsonschema/2-0-0",
        )
    )
    ins = conn.insert_into("facts")
    assert ins is not None
    sql, params = ins
    assert "source_credibility" in sql
    # source_credibility is the LAST bind param ($23) on the analyst INSERT.
    assert params[-1] == pytest.approx(0.5), params


def test_insert_fact_seed_override_stamps_authoritative_nominal():
    """The curated-seeding source_type='seed' override drives the nominal to
    0.9 (no signal lineage)."""
    conn = RecordingConn(fetchval_results=[None])
    ctx = _ctx()
    payload = FactPayload(
        subject="A", predicate="p", value="v", confidence=0.9, valid_from=NOW
    )
    asyncio.run(
        _insert_fact(
            conn,
            row_id=uuid4(),
            payload=payload,
            prov=from_analyst(
                ctx, schema_uri="iglu:legba/fact/jsonschema/2-0-0", derived_from=[]
            ),
            produced_at=NOW,
            effective_schema_uri="iglu:legba/fact/jsonschema/2-0-0",
            source_type="seed",
        )
    )
    _, params = conn.insert_into("facts")
    assert params[-1] == pytest.approx(0.9), params


def test_insert_fact_backing_signal_overrides_nominal():
    """An analyst fact WITH signal lineage takes the backing-signal MAX (0.85)
    over the 'agent' nominal (0.5)."""
    sig = uuid4()
    # fetchval order: [signals MAX (credibility), collapse probe (miss)]
    conn = RecordingConn(fetchval_results=[0.85, None])
    ctx = _ctx()
    payload = FactPayload(
        subject="A", predicate="p", value="v", confidence=0.6, valid_from=NOW
    )
    asyncio.run(
        _insert_fact(
            conn,
            row_id=uuid4(),
            payload=payload,
            prov=from_analyst(
                ctx,
                schema_uri="iglu:legba/fact/jsonschema/2-0-0",
                derived_from=[sig],
            ),
            produced_at=NOW,
            effective_schema_uri="iglu:legba/fact/jsonschema/2-0-0",
        )
    )
    _, params = conn.insert_into("facts")
    assert params[-1] == pytest.approx(0.85), params


def test_insert_fact_on_conflict_merges_credibility_max():
    """The analyst ON CONFLICT keeps the MOST credible backing source
    (GREATEST over source_credibility), unscored sides never lowering it."""
    conn = RecordingConn(fetchval_results=[None])
    ctx = _ctx()
    payload = FactPayload(
        subject="A", predicate="p", value="v", confidence=0.6, valid_from=NOW
    )
    asyncio.run(
        _insert_fact(
            conn,
            row_id=uuid4(),
            payload=payload,
            prov=from_analyst(
                ctx, schema_uri="iglu:legba/fact/jsonschema/2-0-0", derived_from=[]
            ),
            produced_at=NOW,
            effective_schema_uri="iglu:legba/fact/jsonschema/2-0-0",
        )
    )
    sql, _ = conn.insert_into("facts")
    assert "GREATEST(facts.source_credibility" in sql
    assert "EXCLUDED.source_credibility" in sql


# ===========================================================================
# Ingestion producer (_insert_ingestion_fact)
#   A-leg fix 1: A1 tier guard ENGAGES (binds the machine rank into supersede)
#   A-leg fix 2: same-value merge is noisy-OR, not GREATEST
#   + stamps source_credibility
# ===========================================================================


#: 0-based index of the ``source_credibility`` bind param ($12) on the
#: ingestion INSERT. DQ R1 appended $13 (the corroboration source id) after it.
_SOURCE_CREDIBILITY_PARAM = 11


def _run_ingestion(conn: RecordingConn, *, derived_from: list[UUID]) -> None:
    asyncio.run(
        _insert_ingestion_fact(
            conn,
            fact_id=uuid4(),
            subject="Iran",
            predicate="leader of",
            value="Some Value",
            confidence=0.6,
            valid_from=NOW,
            geo_lat=None,
            geo_lon=None,
            data={},
            evidence_set={},
            derived_from=derived_from,
        )
    )


def test_ingestion_threads_machine_rank_into_supersede_guard():
    """A-leg fix 1 (the real bug): the ingestion producer now passes
    incoming_source_type='ingestion' into supersede_prior_facts, so the A1 tier
    guard binds rank 1 ($5) and the CASE/`<= $5` filter EXCLUDES a prior
    seed/curated row (rank 2) — an ingestion fact can no longer close an
    authoritative open row."""
    # fetchval order with NO lineage: only the dedup probe (credibility
    # short-circuits to the nominal, issuing no fetchval). Dedup miss -> insert.
    conn = RecordingConn(fetchval_results=[None])
    _run_ingestion(conn, derived_from=[])
    sup = conn.supersede_exec()
    assert sup is not None, "supersede must run before insert"
    sql, params = sup
    # The guard SQL is present and the incoming machine rank (1) is bound on $5.
    assert "$5::int IS NULL" in sql
    assert "WHEN 'seed'      THEN 2" in sql
    assert "<= $5::int" in sql
    assert params[4] == 1, params  # 'ingestion' -> machine rank 1 (was None=bypass)


def test_ingestion_same_value_dedup_uses_noisy_or_not_greatest():
    """A-leg fix 2: the same-value dedup UPDATE combines confidence with the
    bounded noisy-OR (capped 0.99), matching the analyst path — the old
    GREATEST(facts.confidence, $4) max is gone."""
    existing = uuid4()
    # credibility short-circuits (no lineage); the dedup probe HITS.
    conn = RecordingConn(fetchval_results=[existing])
    _run_ingestion(conn, derived_from=[])
    # The dedup UPDATE is the fetchval probe SQL.
    dedup_sql = conn.fetchvals[-1][0]
    assert "1.0 - (1.0 - facts.confidence) * (1.0 - $4)" in dedup_sql
    assert "LEAST(" in dedup_sql and "0.99" in dedup_sql
    assert "GREATEST(facts.confidence, $4)" not in dedup_sql
    # A dedup HIT skips the INSERT entirely.
    assert conn.insert_into("facts") is None


def test_ingestion_insert_on_conflict_uses_noisy_or():
    """The ingestion INSERT ... ON CONFLICT also combines via noisy-OR (was
    GREATEST), so a same-triple+valid_from re-ingest corroborates rather than
    taking the plain max."""
    conn = RecordingConn(fetchval_results=[None])  # dedup miss -> insert
    _run_ingestion(conn, derived_from=[])
    ins = conn.insert_into("facts")
    assert ins is not None
    sql = ins[0]
    assert "1.0 - (1.0 - facts.confidence) * (1.0 - EXCLUDED.confidence)" in sql
    assert "LEAST(" in sql and "0.99" in sql
    assert "GREATEST(facts.confidence, EXCLUDED.confidence)" not in sql


def test_ingestion_stamps_tier_nominal_when_signals_unscored():
    """No backing-signal score -> ingestion fact lands the 0.5 ingestion
    nominal on the source_credibility INSERT column."""
    sig = uuid4()
    # fetchval order: [signals MAX = NULL, dedup probe = miss]
    conn = RecordingConn(fetchval_results=[None, None])
    _run_ingestion(conn, derived_from=[sig])
    ins = conn.insert_into("facts")
    assert ins is not None
    sql, params = ins
    assert "source_credibility" in sql
    # source_credibility binds $12; DQ R1 appended $13 (the corroboration
    # source id), so index by position rather than off the end.
    assert params[_SOURCE_CREDIBILITY_PARAM] == pytest.approx(0.5), params


def test_ingestion_stamps_backing_signal_score():
    """A scored backing signal (0.9) is stamped on the ingestion fact's
    source_credibility (MAX over the backing signals)."""
    sig = uuid4()
    conn = RecordingConn(fetchval_results=[0.9, None])  # credibility, dedup miss
    _run_ingestion(conn, derived_from=[sig])
    _, params = conn.insert_into("facts")
    assert params[_SOURCE_CREDIBILITY_PARAM] == pytest.approx(0.9), params


def test_ingestion_lift_requires_a_distinct_source():
    """DQ R1: the noisy-OR lift on BOTH re-assert paths is gated on the
    incoming source not already being in the fact's corroboration ledger, so a
    source repeating itself unions lineage without raising belief."""
    conn = RecordingConn(fetchval_results=[None])
    _run_ingestion(conn, derived_from=[])
    ins = conn.insert_into("facts")
    assert ins is not None
    sql = ins[0]
    assert "source_ids" in sql
    assert "$13::text" in sql
    # The lift is inside a CASE that short-circuits to the existing confidence.
    assert "THEN facts.confidence" in sql


def test_ingestion_on_conflict_merges_credibility_max():
    """The ingestion ON CONFLICT keeps the MOST credible backing source."""
    conn = RecordingConn(fetchval_results=[None])
    _run_ingestion(conn, derived_from=[])
    sql, _ = conn.insert_into("facts")
    assert "GREATEST(facts.source_credibility" in sql
    assert "EXCLUDED.source_credibility" in sql


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

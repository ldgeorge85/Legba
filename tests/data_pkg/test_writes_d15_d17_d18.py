# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-logic (no-DB) tests for the W2 write-path remediation owned by
AGENT D-writes-owner: D17 (collapse_open_triple), D15 (nexus provenance —
source_signal_ids populates BOTH columns), and D18 (rel_type canonicalization
to ONE vocabulary so the open-triple unique index + supersession bite).

These run WITHOUT a live Postgres: a hand-rolled ``RecordingConn`` captures the
SQL + params each helper issues and returns scripted results, so we assert the
control flow + the values bound onto each column. The integration round-trips
live in tests/data_pkg/test_writes_nexus.py / test_writes_fact.py (those need a
migrated DB).

Anchored on the VERBATIM garbage strings from
planning/PLATFORM_HEALTH_RESULTS.md:
  * D18: "CoOccursWith" vs "co-occurs-with" (mixed-case defeats the index).
  * D14/D18: "Spain hostile to Saudi Arabia" (sports fixture typed hostile).
  * D17/D6: "Russian located in UK" ×8 (per-cycle valid_from drift dup-leak).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any
from uuid import UUID, uuid4

import pytest

from legba.data.provenance import AnalystContext
from legba.data.provenance.writes import (
    _canonical_rel_type,
    _insert_fact,
    _insert_nexus,
    collapse_open_triple,
    write_nexus,
)
from legba.data.provenance._core import from_analyst
from legba.data.provenance.models import FactPayload, NexusPayload


# ---------------------------------------------------------------------------
# Fake conn — records every execute/fetchval and returns scripted results.
# ---------------------------------------------------------------------------


class RecordingConn:
    """Minimal asyncpg.Connection stand-in.

    ``fetchval_results`` is a list popped FIFO for each fetchval call (the
    helpers call fetchval for the collapse/lock probes). ``execute`` records the
    SQL+params and returns a benign 'UPDATE 0' / 'INSERT 0 1' string.
    """

    def __init__(self, fetchval_results: list[Any] | None = None) -> None:
        self.executes: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchvals: list[tuple[str, tuple[Any, ...]]] = []
        self.fetchrows: list[tuple[str, tuple[Any, ...]]] = []
        self._fetchval_results = list(fetchval_results or [])

    async def execute(self, sql: str, *params: Any) -> str:
        self.executes.append((sql, params))
        # supersede_* parse the trailing int; give them a 0-rows reply.
        return "UPDATE 0"

    async def fetchval(self, sql: str, *params: Any) -> Any:
        self.fetchvals.append((sql, params))
        if self._fetchval_results:
            return self._fetchval_results.pop(0)
        return None

    async def fetchrow(self, sql: str, *params: Any) -> Any:
        self.fetchrows.append((sql, params))
        return None

    # --- assertion helpers -------------------------------------------------

    def insert_into(self, table: str) -> tuple[str, tuple[Any, ...]] | None:
        for sql, params in self.executes:
            if f"INSERT INTO {table}" in sql:
                return sql, params
        return None

    @property
    def n_facts_inserts(self) -> int:
        return sum(1 for sql, _ in self.executes if "INSERT INTO facts" in sql)

    @property
    def n_nexuses_inserts(self) -> int:
        return sum(1 for sql, _ in self.executes if "INSERT INTO nexuses" in sql)


def _ctx(target_id: str | None = "br_energy_test") -> AnalystContext:
    return AnalystContext(
        analyst_id="analyst.reifier_test",
        analyst_version="vdeadbeef",
        run_id=uuid4(),
        target_id=target_id,
        target_version="abc123" if target_id else None,
    )


def _prov(ctx: AnalystContext, derived_from: list[UUID]):
    return from_analyst(
        ctx,
        schema_uri="iglu:legba/nexus/jsonschema/1-0-0",
        derived_from=derived_from,
    )


NOW = datetime(2026, 6, 25, tzinfo=timezone.utc)


# ===========================================================================
# D18 — rel_type canonicalization (pure logic over verbatim garbage strings)
# ===========================================================================


def test_d18_co_occurs_variants_collapse_to_one_form():
    """The VERBATIM live divergence: "CoOccursWith" (proposed_edge_governance /
    seed CamelCase) vs "co-occurs-with" (a hyphen surface) must canonicalize to
    ONE form so the lower(rel_type) open-triple index dedups across producers."""
    variants = [
        "CoOccursWith",
        "co-occurs-with",
        "co occurs with",
        "CO_OCCURS_WITH",
        "Co Occurs With",
        "  CoOccursWith  ",
        "co-occurs with",
    ]
    forms = {_canonical_rel_type(v) for v in variants}
    assert forms == {"co occurs with"}, forms


def test_d18_hostile_to_variants_collapse():
    """D14 garbage: "Spain hostile to Saudi Arabia" — the rel_type "hostile to"
    must equal the CamelCase "HostileTo" the seed emits."""
    forms = {
        _canonical_rel_type(v)
        for v in ["HostileTo", "hostile to", "hostile-to", "HOSTILE_TO", "Hostile To"]
    }
    assert forms == {"hostile to"}, forms


def test_d18_unmapped_predicate_still_converges_lowercased():
    """An UNMAPPED predicate is never sent to the canonical map, but two
    separator variants of it still collapse to one lowercased spaced key so the
    index never splits — conservative: novel predicates are not invented, just
    separator-normalized + lowercased."""
    a = _canonical_rel_type("FrenemyOf")
    b = _canonical_rel_type("frenemy-of")
    c = _canonical_rel_type("frenemy_of")
    assert a == b == c == "frenemy of"


def test_d18_empty_passthrough():
    assert _canonical_rel_type("") == ""
    assert _canonical_rel_type("   ") == "   "


def test_d18_insert_nexus_writes_canonical_rel_type_and_supersedes_on_it():
    """_insert_nexus must (a) write the CANONICAL rel_type onto the row and
    (b) run the supersession probe with the SAME canonical rel_type — the D18
    bug was supersession keying on the raw mixed-case form so it never matched
    the prior open row (0 superseded)."""
    conn = RecordingConn()
    ctx = _ctx()
    payload = NexusPayload(
        subject="Spain", object="Saudi Arabia", rel_type="co-occurs-with",
        polarity=0, label="Spain CoOccursWith Saudi Arabia",
    )
    row_id = uuid4()
    asyncio.run(
        _insert_nexus(
            conn,
            row_id=row_id,
            payload=payload,
            prov=_prov(ctx, []),
            produced_at=NOW,
            effective_schema_uri="iglu:legba/nexus/jsonschema/1-0-0",
        )
    )
    # supersede_prior_nexuses runs (one execute) with the canonical rel_type.
    super_sql = next(
        (p for sql, p in conn.executes if "UPDATE nexuses" in sql and "superseded_by" in sql),
        None,
    )
    assert super_sql is not None, "supersede_prior_nexuses must run before insert"
    # supersede params: $1 subject, $2 intermediary, $3 object, $4 rel_type ...
    assert super_sql[3] == "co occurs with", super_sql
    # The INSERT writes the canonical rel_type ($5 in the nexuses insert).
    ins = conn.insert_into("nexuses")
    assert ins is not None
    assert ins[1][4] == "co occurs with", ins[1]


# ===========================================================================
# D15 — nexus provenance: source_signal_ids populates BOTH columns
# ===========================================================================


def _nexus_insert_params(conn: RecordingConn) -> tuple[Any, ...]:
    ins = conn.insert_into("nexuses")
    assert ins is not None, "expected an INSERT INTO nexuses"
    return ins[1]


# Column ordinals in the nexuses INSERT (1-indexed → 0-indexed here):
#   $13 derived_from = idx 12 ; $14 source_signal_ids = idx 13
_DERIVED_FROM_IDX = 12
_SSID_IDX = 13


def test_d15_source_signal_ids_param_populates_both_columns():
    """The D15 fix: write_nexus(source_signal_ids=...) must land in BOTH the
    derived_from lineage array AND the source_signal_ids slice — 100% of agent
    nexuses were carrying empty provenance because the call site set neither."""
    conn = RecordingConn()
    ctx = _ctx()
    s1, s2 = uuid4(), uuid4()
    payload = NexusPayload(
        subject="Iran", object="Israel", rel_type="SuppliesWeaponsTo",
        polarity=-1, intent="hostile",
    )
    out_box: dict[str, Any] = {}

    async def _go():
        out, dlq = await write_nexus(
            conn,
            analyst_ctx=ctx,
            payload=payload,
            derived_from=[],            # empty — the historical bug
            source_signal_ids=[s1, s2],  # the new explicit channel
        )
        out_box["out"], out_box["dlq"] = out, dlq

    asyncio.run(_go())
    assert out_box["dlq"] is None and out_box["out"] is not None
    params = _nexus_insert_params(conn)
    derived = list(params[_DERIVED_FROM_IDX])
    ssids = list(params[_SSID_IDX])
    assert set(derived) == {s1, s2}, derived
    assert set(ssids) == {s1, s2}, ssids


def test_d15_derived_from_alone_backfills_source_signal_ids():
    """Backward-compat: a producer that only passes derived_from (the existing
    reifier/proposed_edge_governance call shape) still gets a populated
    source_signal_ids slice (never emptier than the lineage)."""
    conn = RecordingConn()
    ctx = _ctx()
    d1, d2 = uuid4(), uuid4()
    payload = NexusPayload(subject="A", object="B", rel_type="AlliedWith", polarity=1)

    async def _go():
        return await write_nexus(
            conn, analyst_ctx=ctx, payload=payload, derived_from=[d1, d2],
        )

    out, dlq = asyncio.run(_go())
    assert dlq is None and out is not None
    params = _nexus_insert_params(conn)
    assert set(params[_DERIVED_FROM_IDX]) == {d1, d2}
    assert set(params[_SSID_IDX]) == {d1, d2}


def test_d15_payload_ssids_union_with_param_and_lineage():
    """All three id channels — explicit param, payload.source_signal_ids, and
    derived_from — are UNIONED into both columns (dedup-preserving)."""
    conn = RecordingConn()
    ctx = _ctx()
    p_id, e_id, d_id, dup = uuid4(), uuid4(), uuid4(), uuid4()
    payload = NexusPayload(
        subject="A", object="B", rel_type="MemberOf", polarity=0,
        source_signal_ids=[p_id, dup],
    )

    async def _go():
        return await write_nexus(
            conn, analyst_ctx=ctx, payload=payload,
            derived_from=[d_id, dup], source_signal_ids=[e_id, dup],
        )

    out, dlq = asyncio.run(_go())
    assert dlq is None and out is not None
    params = _nexus_insert_params(conn)
    expected = {p_id, e_id, d_id, dup}
    assert set(params[_DERIVED_FROM_IDX]) == expected
    assert set(params[_SSID_IDX]) == expected
    # dup appears exactly once in each column.
    assert list(params[_SSID_IDX]).count(dup) == 1
    assert list(params[_DERIVED_FROM_IDX]).count(dup) == 1


def test_d15_empty_everywhere_stays_empty_not_none():
    """The pathological no-provenance write still produces empty *arrays* (not
    NULL) so the NOT NULL columns are satisfied — it just can't enter the tier,
    which is the honest signal."""
    conn = RecordingConn()
    ctx = _ctx()
    payload = NexusPayload(subject="A", object="B", rel_type="PartOf", polarity=0)

    out, dlq = asyncio.run(
        write_nexus(conn, analyst_ctx=ctx, payload=payload, derived_from=[])
    )
    assert dlq is None and out is not None
    params = _nexus_insert_params(conn)
    assert list(params[_DERIVED_FROM_IDX]) == []
    assert list(params[_SSID_IDX]) == []


# ===========================================================================
# D17 — collapse_open_triple: a standing triple keeps ONE open row regardless
#        of valid_from drift ("Russian located in UK" ×8).
# ===========================================================================


def test_d17_collapse_returns_existing_id_when_open_row_found():
    """When an open row for the triple already exists (any valid_from), the
    helper refreshes it in place and returns its id — the caller skips the
    insert. This is the "Russian located in UK" ×8 collapse."""
    existing = uuid4()
    conn = RecordingConn(fetchval_results=[existing])
    new_id = uuid4()
    got = asyncio.run(
        collapse_open_triple(
            conn,
            subject="Russian",
            predicate="located in",
            value="UK",
            new_fact_id=new_id,
            confidence=0.8,
            derived_from=[uuid4()],
            valid_from=NOW,
        )
    )
    assert got == existing
    # It issued exactly one UPDATE-by-id probe.
    assert len(conn.fetchvals) == 1
    sql, params = conn.fetchvals[0]
    assert "UPDATE facts" in sql
    # Same-value match (lower(value) = lower($3)) + open-only + excludes self.
    assert "lower(value)     = lower($3)" in sql
    assert "valid_until IS NULL" in sql and "superseded_by IS NULL" in sql
    assert params[6] == new_id  # id <> $7 self-exclusion


def test_d17_collapse_returns_none_when_no_open_row():
    conn = RecordingConn(fetchval_results=[None])
    got = asyncio.run(
        collapse_open_triple(
            conn, subject="Russian", predicate="located in", value="UK",
            new_fact_id=uuid4(), confidence=0.9, derived_from=[], valid_from=NOW,
        )
    )
    assert got is None


def test_d17_insert_fact_skips_insert_when_collapse_hits():
    """_insert_fact must run supersede → collapse → (skip insert) when an open
    row already carries the triple. Two distinct valid_from drifts for the SAME
    "Russian located in UK" must NOT accumulate two open rows."""
    existing = uuid4()
    # supersede issues an execute (UPDATE), collapse issues a fetchval that HITS.
    conn = RecordingConn(fetchval_results=[existing])
    ctx = _ctx(target_id=None)
    payload = FactPayload(
        subject="Russian", predicate="located in", value="UK",
        confidence=0.7, valid_from=NOW,
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
    assert conn.n_facts_inserts == 0, "collapse hit must SKIP the INSERT (D17)"
    # supersede_prior_facts still ran (the differing-value guard).
    assert any("UPDATE facts" in sql and "superseded_by" in sql for sql, _ in conn.executes)


def test_d17_insert_fact_inserts_when_no_open_row():
    """When collapse finds no open row, the fresh open row is inserted."""
    conn = RecordingConn(fetchval_results=[None])
    ctx = _ctx(target_id=None)
    payload = FactPayload(
        subject="Russian", predicate="located in", value="UK",
        confidence=0.7, valid_from=NOW,
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
    assert conn.n_facts_inserts == 1, "no open row → insert the fresh row"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

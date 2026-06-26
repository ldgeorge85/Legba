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
    _source_tier_rank,
    collapse_open_triple,
    noisy_or_confidence,
    supersede_prior_facts,
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


# ===========================================================================
# Holes-A A1 — source-tier-aware supersession: an ingestion/agent fact must
#        NOT close an open seed/curated fact; same-tier recency still wins.
# ===========================================================================


def test_a1_source_tier_total_order():
    """The authority total order: seed == curated (2) outrank ingestion ==
    agent (1); an unknown/None class is the machine-extracted rank (1) so it can
    never masquerade as authoritative."""
    assert _source_tier_rank("seed") == 2
    assert _source_tier_rank("curated") == 2
    assert _source_tier_rank("ingestion") == 1
    assert _source_tier_rank("agent") == 1
    # case / whitespace tolerant
    assert _source_tier_rank("  Seed ") == 2
    assert _source_tier_rank("CURATED") == 2
    # unknown / None -> machine-extracted (1), never authoritative
    assert _source_tier_rank("frobnicator") == 1
    assert _source_tier_rank("") == 1
    assert _source_tier_rank(None) == 1
    # seed and curated are EQUAL (neither outranks the other), as are
    # ingestion and agent — only the cross-tier comparison gates.
    assert _source_tier_rank("seed") == _source_tier_rank("curated")
    assert _source_tier_rank("ingestion") == _source_tier_rank("agent")


def test_a1_ingestion_passes_machine_rank_into_guard():
    """An incoming ingestion fact binds rank 1 as the guard param and the UPDATE
    carries the CASE tier ladder + `<= $5` filter, so a prior seed/curated row
    (rank 2) is excluded from the close set (2 <= 1 is false)."""
    conn = RecordingConn()
    asyncio.run(
        supersede_prior_facts(
            conn,
            subject="Iran",
            predicate="leader of",
            value="Some Wrong Value",
            new_fact_id=uuid4(),
            incoming_source_type="ingestion",
        )
    )
    assert len(conn.executes) == 1
    sql, params = conn.executes[0]
    assert "UPDATE facts" in sql and "superseded_by" in sql
    # The source-tier guard is present and keyed on the rank bind param.
    assert "$5::int IS NULL" in sql
    assert "WHEN 'seed'      THEN 2" in sql
    assert "WHEN 'curated'   THEN 2" in sql
    assert "<= $5::int" in sql
    # $5 (1-indexed) -> params[4] is the incoming machine rank = 1.
    assert params[4] == 1, params


def test_a1_agent_is_also_machine_rank():
    """An analyst ('agent') emission binds the same machine rank 1 — it likewise
    cannot close a seed/curated row."""
    conn = RecordingConn()
    asyncio.run(
        supersede_prior_facts(
            conn, subject="A", predicate="p", value="v",
            new_fact_id=uuid4(), incoming_source_type="agent",
        )
    )
    _, params = conn.executes[0]
    assert params[4] == 1, params


def test_a1_seed_incoming_binds_authoritative_rank():
    """An incoming seed/curated fact binds rank 2, so the `tier <= 2` filter
    admits prior rows of EVERY tier — an operator-blessed value supersedes
    anything below or equal to it (authoritative writes are never gated out)."""
    for st, expect in (("seed", 2), ("curated", 2)):
        conn = RecordingConn()
        asyncio.run(
            supersede_prior_facts(
                conn, subject="A", predicate="p", value="v",
                new_fact_id=uuid4(), incoming_source_type=st,
            )
        )
        _, params = conn.executes[0]
        assert params[4] == expect, (st, params)


def test_a1_none_source_type_disables_guard_backcompat():
    """The historical / operator journal-correction caller passes no source_type
    — the guard param binds NULL so `$5::int IS NULL` short-circuits the CASE and
    NO tier filtering happens (unconditional close, exactly as before A1)."""
    conn = RecordingConn()
    asyncio.run(
        supersede_prior_facts(
            conn, subject="A", predicate="p", value="v", new_fact_id=uuid4(),
        )
    )
    _, params = conn.executes[0]
    assert params[4] is None, params


def test_a1_same_tier_leader_change_still_supersedes():
    """REGRESSION GUARD (the prompt's leader-change case): a NEW seed leader fact
    superseding an OLD seed leader fact is a SAME-tier (2 vs 2) update — the
    guard's `2 <= 2` admits the prior row, so legitimate recency-wins
    supersession is NOT blocked. We assert the bound rank is 2 and the filter is
    `<=` (inclusive), which is what lets equal tiers through.

    The DB round-trip (an old seed 'Iran leader of X' actually closing when a new
    seed 'Iran leader of Y' arrives) is exercised in the orchestrator's serial
    integration run against a migrated Postgres; here we assert the SQL contract
    that makes same-tier closes possible (inclusive `<=`, not strict `<`)."""
    conn = RecordingConn()
    asyncio.run(
        supersede_prior_facts(
            conn,
            subject="Iran",
            predicate="leader of",
            value="New Leader",   # differs from the prior open seed value
            new_fact_id=uuid4(),
            incoming_source_type="seed",
        )
    )
    sql, params = conn.executes[0]
    # Inclusive `<=` is what admits an equal-tier prior row (2 <= 2 -> close);
    # a strict `<` would WRONGLY block same-tier leader updates.
    assert "<= $5::int" in sql
    assert params[4] == 2, params
    # The value-differs predicate is intact (a same-value re-assert never closes).
    assert "lower(value)    <> lower($3)" in sql


def test_a1_insert_fact_threads_payload_source_type_into_guard():
    """_insert_fact must resolve the incoming fact's source_type and PASS IT to
    supersede_prior_facts so the tier guard runs. An analyst FactPayload defaults
    to 'agent' (rank 1) — the supersede UPDATE must bind rank 1, proving the
    analyst path can't retire a seed/curated row."""
    conn = RecordingConn(fetchval_results=[None])  # collapse misses -> insert
    ctx = _ctx(target_id=None)
    payload = FactPayload(
        subject="Iran", predicate="leader of", value="Wrong",
        confidence=0.6, valid_from=NOW,
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
    super_exec = next(
        (p for sql, p in conn.executes if "UPDATE facts" in sql and "superseded_by" in sql),
        None,
    )
    assert super_exec is not None, "supersede must run before insert"
    assert super_exec[4] == 1, super_exec  # 'agent' default -> machine rank


def test_a1_insert_fact_seed_override_binds_authoritative_rank():
    """The curated-seeding path passes source_type='seed' to _insert_fact; that
    override (not the payload) must drive the tier rank bound into supersede ->
    rank 2 (authoritative), so a seed re-assert can supersede lower tiers."""
    conn = RecordingConn(fetchval_results=[None])
    ctx = _ctx(target_id=None)
    payload = FactPayload(
        subject="Iran", predicate="leader of", value="Correct",
        confidence=0.9, valid_from=NOW,
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
    super_exec = next(
        (p for sql, p in conn.executes if "UPDATE facts" in sql and "superseded_by" in sql),
        None,
    )
    assert super_exec is not None
    assert super_exec[4] == 2, super_exec  # 'seed' override -> authoritative rank


# ===========================================================================
# Holes-A A2 — confidence aggregation on agreement: bounded noisy-OR combine
#        so N corroborating sources raise confidence above any single one,
#        capped below certainty.
# ===========================================================================


def test_a2_noisy_or_raises_above_either_input():
    """Two agreeing sources at 0.6 and 0.7 combine ABOVE both (and above the old
    max=0.7): 1 - 0.4*0.3 = 0.88."""
    combined = noisy_or_confidence(0.6, 0.7)
    assert combined == pytest.approx(0.88)
    assert combined > 0.7  # strictly above the plain max


def test_a2_noisy_or_capped_below_certainty():
    """Even two near-certain sources never reach 1.0 — clamped to the 0.99 cap so
    corroboration asymptotes just below certainty."""
    assert noisy_or_confidence(0.999, 0.999) == 0.99
    assert noisy_or_confidence(1.0, 1.0) == 0.99
    assert noisy_or_confidence(1.0, 0.5) == 0.99  # 1-(0)*(.5)=1.0 -> capped


def test_a2_noisy_or_monotone_and_repeated_agreement_climbs():
    """N repeated corroborations climb monotonically toward (never past) the
    cap — the whole point of aggregation: more agreement -> more belief."""
    c = 0.5
    prev = c
    for _ in range(20):
        c = noisy_or_confidence(c, 0.5)
        assert c >= prev          # never decreases
        assert c <= 0.99          # never exceeds the cap
        prev = c
    assert c == pytest.approx(0.99)  # asymptotes to the cap


def test_a2_noisy_or_clamps_malformed_inputs():
    """Out-of-range inputs are clamped to [0,1] first so monotonicity holds even
    on a malformed/overshooting confidence."""
    assert noisy_or_confidence(-0.5, 0.5) == pytest.approx(0.5)   # -0.5 -> 0
    assert noisy_or_confidence(0.5, 1.5) == 0.99                  # 1.5 -> 1 -> capped
    assert 0.0 <= noisy_or_confidence(0.0, 0.0) <= 0.99


def test_a2_collapse_open_triple_uses_noisy_or_in_sql():
    """collapse_open_triple's UPDATE must combine confidence with the bounded
    noisy-OR (capped at 0.99), NOT the old GREATEST(max). We assert the SQL shape
    on the probe it issues."""
    existing = uuid4()
    conn = RecordingConn(fetchval_results=[existing])
    asyncio.run(
        collapse_open_triple(
            conn, subject="A", predicate="p", value="v",
            new_fact_id=uuid4(), confidence=0.8, derived_from=[], valid_from=NOW,
        )
    )
    sql, _ = conn.fetchvals[0]
    assert "1.0 - (1.0 - facts.confidence) * (1.0 - $4)" in sql
    assert "LEAST(" in sql and "0.99" in sql
    # The old max combine must be GONE from the confidence assignment.
    assert "GREATEST(facts.confidence, $4)" not in sql


def test_a2_insert_fact_on_conflict_uses_noisy_or_in_sql():
    """_insert_fact's ON CONFLICT DO UPDATE must combine confidence with the
    bounded noisy-OR over EXCLUDED.confidence, capped at 0.99 (was GREATEST)."""
    conn = RecordingConn(fetchval_results=[None])  # collapse misses -> reach insert
    ctx = _ctx(target_id=None)
    payload = FactPayload(
        subject="A", predicate="p", value="v", confidence=0.7, valid_from=NOW,
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
    sql = ins[0]
    assert "1.0 - (1.0 - facts.confidence) * (1.0 - EXCLUDED.confidence)" in sql
    assert "LEAST(" in sql and "0.99" in sql
    assert "GREATEST(facts.confidence, EXCLUDED.confidence)" not in sql


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

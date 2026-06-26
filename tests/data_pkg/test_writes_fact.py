# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for OutputKind.FACT + write_fact + _insert_fact (anchor §5 PIECE 2).

Mirrors the hypothesis write tests (``test_writes``). Uses the shared
``migrated_pg`` fixture so it runs against the same fresh test DB.

Covers (plan §6.2-3):
  * OutputKind.FACT resolves via spec_for_kind; KIND_REGISTRY[FACT].table=='facts'.
  * FactPayload validation (required subject/predicate/value).
  * write_fact happy path → row in `facts` with analyst_id set.
  * bad payload → output_dead_letter (mirror the hypothesis write test).
  * _insert_fact ON CONFLICT upsert (confidence=noisy-OR agreement combine,
    lineage unioned).
  * PIECE B supersession: a new value for an existing (subject, predicate)
    closes the prior open row (valid_until + superseded_by); same-value
    re-assert does NOT supersede.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    FactPayload,
    KIND_REGISTRY,
    OutputKind,
    spec_for_kind,
    write_fact,
)
from legba.data.sources._contract import Signal
from legba.runtime.source_actor import write_canonical_signal


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _analyst_ctx(target_id: str | None = "br_energy_test") -> AnalystContext:
    return AnalystContext(
        analyst_id=f"analyst.test_{uuid4().hex[:8]}",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id=target_id,
        target_version="abc123def456" if target_id else None,
    )


async def _seed_signal(conn, *, title: str = "root") -> UUID:
    signal = Signal(
        source_id="rss_main",
        modality="text",
        payload={"title": title},
        content_hash=f"hash-{uuid4().hex}",
        fetched_at=datetime.now(tz=timezone.utc),
    )
    return await write_canonical_signal(
        conn, signal, source_version="v1", owner_tenant="default"
    )


# ---------------------------------------------------------------------------
# Registry / payload unit tests (no DB)
# ---------------------------------------------------------------------------


def test_output_kind_fact_registered():
    assert OutputKind.FACT in KIND_REGISTRY
    spec = spec_for_kind("fact")
    assert spec is spec_for_kind(OutputKind.FACT)
    assert spec.table == "facts"
    assert spec.payload_model is FactPayload
    assert spec.schema_uri == "iglu:legba/fact/jsonschema/2-0-0"
    assert spec.nats_subject_pattern == "analyst.{analyst_id}.fact"


def test_fact_payload_validates_required():
    with pytest.raises(Exception):
        FactPayload(subject="x", predicate="y")  # value required
    p = FactPayload(subject="Apple", predicate="located in", value="Cupertino")
    assert p.kind_marker == "fact"
    assert p.confidence == 1.0
    assert p.source_type == "agent"


def test_fact_payload_carries_valid_until():
    """Phase B item 1: FactPayload now carries a creation-time valid_until
    (forward TTL / curated expiry), mirroring valid_from. Defaults None."""
    assert FactPayload(subject="a", predicate="b", value="c").valid_until is None
    vu = datetime(2027, 1, 1, tzinfo=timezone.utc)
    p = FactPayload(subject="a", predicate="b", value="c", valid_until=vu)
    assert p.valid_until == vu


# ---------------------------------------------------------------------------
# write_fact round-trip (DB)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_routes_to_facts_table(pg_conn):
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="root")
    output, dlq = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="OPEC",
            predicate="includes",
            value="Saudi Arabia",
            confidence=0.7,
            source_type="agent",
        ),
        derived_from=[s1],
    )
    assert dlq is None and output is not None
    row = await pg_conn.fetchrow(
        "SELECT subject, predicate, value, confidence, analyst_id, "
        "source_type, derived_from FROM facts WHERE id = $1",
        output.id,
    )
    assert row["subject"] == "OPEC"
    assert row["predicate"] == "includes"
    assert row["value"] == "Saudi Arabia"
    assert row["confidence"] == pytest.approx(0.7)
    assert row["analyst_id"] == actx.analyst_id
    assert s1 in row["derived_from"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_bad_payload_dead_letters(pg_conn):
    actx = _analyst_ctx()
    # Empty subject violates min_length=1 → DLQ route (mirror hypothesis test).
    output, dlq = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload={"subject": "", "predicate": "p", "value": "v"},
        derived_from=[],
    )
    assert output is None
    assert dlq is not None
    found = await pg_conn.fetchval(
        "SELECT count(*) FROM output_dead_letter WHERE id = $1", dlq.id
    )
    assert found == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_on_conflict_upserts(pg_conn):
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="a")
    s2 = await _seed_signal(pg_conn, title="b")
    vf = datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc)

    out1, _ = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="Berlin", predicate="capital of", value="Germany",
            confidence=0.4, valid_from=vf,
        ),
        derived_from=[s1],
    )
    out2, _ = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="berlin", predicate="CAPITAL OF", value="germany",
            confidence=0.9, valid_from=vf,
        ),
        derived_from=[s2],
    )
    assert out1 is not None and out2 is not None
    rows = await pg_conn.fetch(
        "SELECT id, confidence, derived_from FROM facts "
        "WHERE lower(subject)='berlin' AND lower(predicate)='capital of' "
        "AND lower(value)='germany' AND valid_from=$1",
        vf,
    )
    assert len(rows) == 1, "case-insensitive triple+valid_from must upsert to one row"
    # Holes-A A2: agreement now combines via bounded noisy-OR, not MAX —
    # 1-(1-0.4)(1-0.9) = 0.94 (two sources corroborating raise confidence).
    assert rows[0]["confidence"] == pytest.approx(0.94)
    lineage = rows[0]["derived_from"]
    assert s1 in lineage and s2 in lineage


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_value_change_supersedes_prior(pg_conn):
    """PIECE B auto-supersession on the analyst write path: a new VALUE for an
    existing (subject, predicate) closes the prior open row (valid_until +
    superseded_by → new id) and opens the new one as the single canonical row.
    A same-value re-assert (different valid_from) does NOT supersede."""
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="a")
    s2 = await _seed_signal(pg_conn, title="b")

    # First: Acmestan_wf led_by Alice.
    out1, _ = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="Acmestan_wf", predicate="led by", value="Alice",
            confidence=0.8,
            valid_from=datetime(2026, 6, 1, 0, 0, tzinfo=timezone.utc),
        ),
        derived_from=[s1],
    )
    # Second: same subject+predicate, DIFFERENT value → supersede the prior.
    out2, _ = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject="Acmestan_wf", predicate="led by", value="Bob",
            confidence=0.8,
            valid_from=datetime(2026, 6, 2, 0, 0, tzinfo=timezone.utc),
        ),
        derived_from=[s2],
    )
    assert out1 is not None and out2 is not None

    prior = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out1.id
    )
    assert prior["valid_until"] is not None, "prior must be closed"
    assert prior["superseded_by"] == out2.id, "prior must chain to the new row"

    new = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out2.id
    )
    assert new["valid_until"] is None and new["superseded_by"] is None, "new row is open"

    open_count = await pg_conn.fetchval(
        "SELECT count(*) FROM facts "
        "WHERE lower(subject)='acmestan_wf' AND lower(predicate)='led by' "
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert open_count == 1, "exactly one canonical (open) row per subject+predicate"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_replay_of_closed_value_does_not_dangle(pg_conn):
    """PIECE B hardening regression: re-asserting a value that was previously
    CLOSED (superseded) must open a NEW canonical row — NOT upsert into the old
    closed row via ON CONFLICT.

    Sequence (all same subject+predicate, same valid_from so the FULL triple
    index would have collided on the closed row):
      1. assert V1  → row A open
      2. assert V2  → A closed (superseded_by=B), B open
      3. assert V1  → B closed (superseded_by=C), and C must be a FRESH open row
         (the partial-on-open unique index hides closed A from conflict
         inference), so B.superseded_by points at a row that actually exists and
         A stays untouched/closed.
    """
    actx = _analyst_ctx()
    s1 = await _seed_signal(pg_conn, title="a")
    vf = datetime(2026, 6, 5, 0, 0, tzinfo=timezone.utc)

    def _payload(value: str) -> FactPayload:
        return FactPayload(
            subject="Borduria", predicate="ruled by", value=value,
            confidence=0.6, valid_from=vf,
        )

    a, _ = await write_fact(pg_conn, analyst_ctx=actx, payload=_payload("Alice"), derived_from=[s1])
    b, _ = await write_fact(pg_conn, analyst_ctx=actx, payload=_payload("Bob"), derived_from=[s1])
    c, _ = await write_fact(pg_conn, analyst_ctx=actx, payload=_payload("Alice"), derived_from=[s1])
    assert a is not None and b is not None and c is not None

    # Three DISTINCT rows must exist (no upsert collapsed the replay into A).
    ids = {a.id, b.id, c.id}
    assert len(ids) == 3
    present = await pg_conn.fetch(
        "SELECT id, valid_until, superseded_by FROM facts WHERE id = ANY($1::uuid[])",
        list(ids),
    )
    assert len(present) == 3, "the replayed row C must be a real, distinct INSERT"

    by_id = {r["id"]: r for r in present}
    # A is closed by B and never touched again.
    assert by_id[a.id]["superseded_by"] == b.id
    assert by_id[a.id]["valid_until"] is not None
    # B is closed by C — and C exists, so the pointer is NOT dangling.
    assert by_id[b.id]["superseded_by"] == c.id
    assert by_id[b.id]["valid_until"] is not None
    # C is the single open canonical row.
    assert by_id[c.id]["superseded_by"] is None
    assert by_id[c.id]["valid_until"] is None

    # No dangling supersession pointers anywhere for this subject+predicate.
    dangling = await pg_conn.fetchval(
        "SELECT count(*) FROM facts f "
        "WHERE lower(f.subject)='borduria' AND lower(f.predicate)='ruled by' "
        "AND f.superseded_by IS NOT NULL "
        "AND NOT EXISTS (SELECT 1 FROM facts g WHERE g.id = f.superseded_by)"
    )
    assert dangling == 0, "every superseded_by must point at an existing row"

    open_count = await pg_conn.fetchval(
        "SELECT count(*) FROM facts "
        "WHERE lower(subject)='borduria' AND lower(predicate)='ruled by' "
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert open_count == 1, "exactly one canonical (open) row survives the replay"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_persists_creation_valid_until(pg_conn):
    """Phase B item 1 ACCEPTANCE: a payload carrying a creation-time valid_until
    (a curated forward TTL) lands with valid_until NON-NULL — and that TTL is
    NOT the supersession close (the row is OPEN: superseded_by IS NULL)."""
    actx = _analyst_ctx()
    vf = datetime(2020, 1, 1, tzinfo=timezone.utc)
    vu = datetime(2025, 12, 31, tzinfo=timezone.utc)
    out, dlq = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject=f"TtlLand_{uuid4().hex[:8]}", predicate="leader of",
            value="Atlantis", confidence=0.9, valid_from=vf, valid_until=vu,
        ),
        derived_from=[],
    )
    assert dlq is None and out is not None
    row = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM facts WHERE id=$1", out.id
    )
    assert row["valid_until"] == vu, "curated forward TTL must persist non-NULL"
    assert row["superseded_by"] is None, "a creation-time TTL is NOT a supersession close"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_normalizes_camelcase_predicate(pg_conn):
    """Phase B item 5: the seed/analyst write path converges a CamelCase
    predicate ('LeaderOf') onto the canonical lowercase-spaced form
    ('leader of'), so the lower(predicate) key agrees with the ingest path."""
    actx = _analyst_ctx()
    subj = f"Camelot_{uuid4().hex[:8]}"
    out, _ = await write_fact(
        pg_conn,
        analyst_ctx=actx,
        payload=FactPayload(
            subject=subj, predicate="LeaderOf", value="Arthur", confidence=0.9,
        ),
        derived_from=[],
    )
    assert out is not None
    pred = await pg_conn.fetchval(
        "SELECT predicate FROM facts WHERE id=$1", out.id
    )
    assert pred == "leader of", "CamelCase predicate must normalize at write"


@pytest.mark.integration
@pytest.mark.asyncio
async def test_write_fact_camelcase_and_spaced_dedupe_to_one_open_row(pg_conn):
    """Phase B item 5 (convergence): the SAME relation written once as CamelCase
    ('LeaderOf') and once as lowercase-spaced ('leader of') must land on ONE
    open row — the normalizer makes the lower(predicate) supersession/upsert key
    agree across the seed and ingest producers."""
    actx = _analyst_ctx()
    subj = f"Converge_{uuid4().hex[:8]}"
    vf = datetime(2026, 6, 1, tzinfo=timezone.utc)
    a, _ = await write_fact(
        pg_conn, analyst_ctx=actx,
        payload=FactPayload(subject=subj, predicate="LeaderOf", value="Zog",
                            confidence=0.4, valid_from=vf),
        derived_from=[],
    )
    b, _ = await write_fact(
        pg_conn, analyst_ctx=actx,
        payload=FactPayload(subject=subj, predicate="leader of", value="Zog",
                            confidence=0.9, valid_from=vf),
        derived_from=[],
    )
    assert a is not None and b is not None
    rows = await pg_conn.fetch(
        "SELECT id, confidence FROM facts "
        "WHERE lower(subject)=lower($1) AND predicate='leader of' "
        "AND valid_until IS NULL AND superseded_by IS NULL",
        subj,
    )
    assert len(rows) == 1, "CamelCase + spaced forms of one relation must upsert to ONE open row"
    # Holes-A A2: agreement combines via bounded noisy-OR, not MAX —
    # 1-(1-0.4)(1-0.9) = 0.94.
    assert rows[0]["confidence"] == pytest.approx(0.94)

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""KW-1 — forward-consumption index + review-flag schema (migrations 0106/0107).

Covered here (ephemeral-DB via ``migrated_pg`` — the real migration runner,
never the live DB — plus pure kind-level stamp tests):

  * migration objects — ``output_consumption`` / ``review_flags`` /
    ``bearing_edges`` tables, the forward index, the never-delete trigger;
  * the writer — ``record_output_consumption`` materializes edges, folds
    in-batch duplicates via the (consumer, consumed, context) PK, and NEVER
    raises (degrade-not-break: a consumption-write failure must never fail
    the compose it sidecars — the actor guard mirrors the same discipline);
  * composition consumption points — ``meta_findings_synthesizer._run``
    stamps BASIS vs PERIPHERY edges exactly where the orient/periphery split
    decides them (incl. the honest-empty basis path and the untiered
    per-country read); the LEGACY global meta stamps nothing;
  * journal consumption point — ``journal_assessor.run_method`` stamps the
    RENDERED slice (``_select_journal_slice``) as ``journal_slice`` edges,
    while ``derived_from`` stays empty (the journal remains off-chain);
  * review_flags close-by-supersession — one OPEN flag per pair, closed by
    stamping ``closed_by``/``closed_at`` (paired CHECK), DELETE fails loud
    (schema-enforced never-deleted posture);
  * bearing_edges — an edge without dates/provenance/planes/matcher is
    UNREPRESENTABLE (NOT NULL + CHECK rejections), (src, dst, kind) unique.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest

from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.inline_target import InlineTargetDeps
from legba.data.analysts.journal_assessor import run_method as journal_run_method
from legba.data.config import PostgresConfig
from legba.data.provenance.consumption import (
    CONSUMPTION_CONTEXT_BASIS,
    CONSUMPTION_CONTEXT_JOURNAL,
    CONSUMPTION_CONTEXT_PERIPHERY,
    record_output_consumption,
)


# ---------------------------------------------------------------------------
# helpers (mirroring test_composition_tiered_evidence.py conventions)
# ---------------------------------------------------------------------------


class _CannedLLM:
    """LLM double returning a caller-supplied payload."""

    subprovider = "consumption_test_double"

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    async def chat_complete(self, *a: Any, **k: Any) -> Any:
        class _Usage:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = json.dumps(self._payload)
        resp.usage = _Usage()
        return resp


class _ScriptedLLM:
    """Pops scripted plain-text responses in order (journal arc double)."""

    subprovider = "consumption_test_double"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)

    async def chat_complete(self, *a: Any, **k: Any) -> Any:
        class _Usage:
            prompt_tokens = 10
            completion_tokens = 20
            reasoning_tokens = 0

        resp = SimpleNamespace()
        resp.content = (
            self._scripted.pop(0) if self._scripted else '{"done": true}'
        )
        resp.usage = _Usage()
        return resp


def _row(
    *,
    analyst_id: str = "leadership_transition",
    uid: UUID | None = None,
    periphery: bool = False,
    floor: float | None = None,
) -> dict[str, Any]:
    r: dict[str, Any] = {
        "id": uid or uuid4(),
        "kind": "finding",
        "title": "sub-claim title",
        "body": "sub-claim body",
        "confidence": 0.7,
        "effective_confidence": 0.7,
        "faithfulness_score": 0.9,
        "severity": None,
        "data": {"tags": [], "evidence": []},
        "evidence": [],
        "target_id": "country_g20_in",
        "target_version": None,
        "analyst_id": analyst_id,
        "analyst_version": "vtest",
        "produced_at": "2026-06-30T00:00:00+00:00",
        "derived_from": [],
        "schema_uri": "iglu:legba/finding/jsonschema/1-0-0",
        "run_id": uuid4(),
    }
    if periphery:
        r["_evidence_tier"] = synth.PERIPHERY_TIER
    if floor is not None:
        r["_evidence_floor"] = floor
    return r


def _canned_payload(body: str) -> dict[str, Any]:
    return {
        "title": "Composed read",
        "body": body,
        "confidence": 0.6,
        "evidence": [],
        "tags": [],
    }


_COMPOSE_OPTIONS = {
    "target_id": "country_g20_in",
    "analyst_id": "country_composition",
}


class _BrokenConn:
    """Connection double whose writes always fail (degrade-path probe)."""

    async def executemany(self, *a: Any, **k: Any) -> None:
        raise asyncpg.PostgresError("simulated write failure")


# ---------------------------------------------------------------------------
# 1. Migration objects (0106 + 0107)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_migration_objects_exist(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        tables = {
            r["table_name"]
            for r in await conn.fetch(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public'"
            )
        }
        assert {"output_consumption", "review_flags", "bearing_edges"} <= tables

        indexes = {
            r["indexname"]
            for r in await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
            )
        }
        # The FORWARD-walk index (consumed_id first) + the open-pair guard.
        assert "idx_output_consumption_forward" in indexes
        assert "uq_review_flags_open_pair" in indexes
        assert "idx_bearing_edges_dst" in indexes

        # The never-delete posture is a TRIGGER, not a convention.
        trig = await conn.fetchval(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgname = 'trg_review_flags_forbid_delete'"
        )
        assert trig == "trg_review_flags_forbid_delete"

        # Every output_consumption column is NOT NULL (an edge without a
        # consumer/consumed/time/kind/context is unrepresentable).
        nullable = {
            r["column_name"]
            for r in await conn.fetch(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'output_consumption' "
                "AND is_nullable = 'YES'"
            )
        }
        assert nullable == set()
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 2. The writer — materialization + degrade-not-break
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_output_consumption_writes_edges(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    consumer = uuid4()
    basis_a, basis_b, peri = uuid4(), uuid4(), uuid4()
    try:
        n = await record_output_consumption(
            conn,
            consumer_id=consumer,
            consumer_kind="meta_findings_synthesizer",
            edges=[
                (basis_a, CONSUMPTION_CONTEXT_BASIS),
                (basis_b, CONSUMPTION_CONTEXT_BASIS),
                (peri, CONSUMPTION_CONTEXT_PERIPHERY),
                # in-batch duplicate folds via the PK (ON CONFLICT DO NOTHING)
                (basis_a, CONSUMPTION_CONTEXT_BASIS),
            ],
        )
        assert n == 4  # attempted; the duplicate folded at the schema layer

        rows = await conn.fetch(
            "SELECT consumed_id, consumer_kind, context, consumed_at "
            "FROM output_consumption WHERE consumer_id = $1 "
            "ORDER BY context, consumed_id",
            consumer,
        )
        assert len(rows) == 3
        assert {(r["consumed_id"], r["context"]) for r in rows} == {
            (basis_a, CONSUMPTION_CONTEXT_BASIS),
            (basis_b, CONSUMPTION_CONTEXT_BASIS),
            (peri, CONSUMPTION_CONTEXT_PERIPHERY),
        }
        assert all(
            r["consumer_kind"] == "meta_findings_synthesizer" for r in rows
        )
        assert all(r["consumed_at"] is not None for r in rows)

        # FORWARD walk: one indexed probe answers "who consumed basis_a?".
        consumers = await conn.fetch(
            "SELECT consumer_id FROM output_consumption WHERE consumed_id = $1",
            basis_a,
        )
        assert [r["consumer_id"] for r in consumers] == [consumer]
    finally:
        await conn.close()


@pytest.mark.asyncio
async def test_record_output_consumption_never_raises():
    """DEGRADE-NOT-BREAK: a consumption-write failure returns 0, raises
    nothing — the compose/journal write it sidecars must never fail on it."""
    n = await record_output_consumption(
        _BrokenConn(),
        consumer_id=uuid4(),
        consumer_kind="meta_findings_synthesizer",
        edges=[(uuid4(), CONSUMPTION_CONTEXT_BASIS)],
    )
    assert n == 0


@pytest.mark.asyncio
async def test_record_output_consumption_empty_edges_is_noop():
    n = await record_output_consumption(
        _BrokenConn(),  # never reached — empty short-circuits before the conn
        consumer_id=uuid4(),
        consumer_kind="meta_findings_synthesizer",
        edges=[],
    )
    assert n == 0


# ---------------------------------------------------------------------------
# 3. Composition consumption points (basis vs periphery contexts distinct)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_composition_run_stamps_basis_and_periphery_edges():
    basis_uid, peri_uid = uuid4(), uuid4()
    basis = _row(uid=basis_uid, floor=0.5)
    peri = _row(
        analyst_id="escalation", uid=peri_uid, periphery=True, floor=0.5
    )
    result = await synth._run(
        [basis, peri],
        _COMPOSE_OPTIONS,
        llm=_CannedLLM(
            _canned_payload("Contested [[ref:1]]. Weak convoy [[ref:2]].")
        ),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    # BASIS and PERIPHERY are DISTINCT contexts; basis edges lead.
    assert result.consumed_edges == [
        (basis_uid, CONSUMPTION_CONTEXT_BASIS),
        (peri_uid, CONSUMPTION_CONTEXT_PERIPHERY),
    ]
    # The stamp is a SIDECAR: derived_from is unchanged by it (basis + kept
    # periphery, the pre-existing lineage contract).
    assert result.derived_from == [basis_uid, peri_uid]


@pytest.mark.asyncio
async def test_untiered_composition_stamps_basis_only():
    uid = uuid4()
    result = await synth._run(
        [_row(uid=uid)],
        _COMPOSE_OPTIONS,
        llm=_CannedLLM(_canned_payload("Contested [[ref:1]].")),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    assert result.consumed_edges == [(uid, CONSUMPTION_CONTEXT_BASIS)]


@pytest.mark.asyncio
async def test_empty_basis_with_periphery_stamps_periphery_only():
    """The honest-empty composition head still CONSUMED the periphery it
    recorded — the forward index must know, so a mover among those rows can
    flag this head."""
    peri_uid = uuid4()
    peri = _row(
        analyst_id="escalation", uid=peri_uid, periphery=True, floor=0.5
    )
    result = await synth._run(
        [peri],
        _COMPOSE_OPTIONS,
        llm=_CannedLLM(_canned_payload("never called")),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    assert "empty_slice" in result.finding.tags
    assert result.consumed_edges == [
        (peri_uid, CONSUMPTION_CONTEXT_PERIPHERY)
    ]


@pytest.mark.asyncio
async def test_legacy_global_meta_stamps_no_edges():
    """The legacy global meta (no target_id / composition / thematic option)
    stays unstamped — no consumption rows, per the standing legacy-read-
    unchanged discipline."""
    result = await synth._run(
        [_row()],
        {"analyst_id": "meta_synth"},  # no target_id, no composition flag
        llm=_CannedLLM(_canned_payload("Legacy synthesis.")),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    assert result.consumed_edges == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_composed_output_edges_materialize_distinct_contexts(
    migrated_pg: PostgresConfig,
):
    """End-to-end over the real DB: a composed run's stamped edges land as
    output_consumption rows with basis vs periphery contexts DISTINCT — the
    same (writer, edges) call the actor host makes alongside the output
    write."""
    basis_uid, peri_uid = uuid4(), uuid4()
    result = await synth._run(
        [
            _row(uid=basis_uid, floor=0.5),
            _row(
                analyst_id="escalation",
                uid=peri_uid,
                periphery=True,
                floor=0.5,
            ),
        ],
        _COMPOSE_OPTIONS,
        llm=_CannedLLM(_canned_payload("A [[ref:1]]. B [[ref:2]].")),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    consumer = uuid4()  # stands in for the written analyst_outputs row id
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        await record_output_consumption(
            conn,
            consumer_id=consumer,
            consumer_kind="meta_findings_synthesizer",
            edges=result.consumed_edges,
        )
        ctx_by_id = {
            r["consumed_id"]: r["context"]
            for r in await conn.fetch(
                "SELECT consumed_id, context FROM output_consumption "
                "WHERE consumer_id = $1",
                consumer,
            )
        }
        assert ctx_by_id == {
            basis_uid: CONSUMPTION_CONTEXT_BASIS,
            peri_uid: CONSUMPTION_CONTEXT_PERIPHERY,
        }
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 4. Journal consumption point (the rendered slice)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_journal_run_stamps_rendered_slice_as_journal_edges():
    sig_a, sig_b = uuid4(), uuid4()
    inputs = [
        {"id": sig_a, "title": "signal A", "source_id": "src1"},
        {"id": sig_b, "title": "signal B", "source_id": "src2"},
        {"id": "not-a-uuid", "title": "malformed id — tolerated"},
        {"title": "no id at all — skipped"},
    ]
    # No binding wired → GATHER no-ops; scripted: field-notes, then the entry.
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(["field notes", "A quiet window entry."]),
        system_prompt="PERSONA",
        max_rounds=1,
    )
    result = await journal_run_method(
        inputs, {"analyst_id": "journal_assessor"}, deps
    )
    assert set(result.consumed_edges) == {
        (sig_a, CONSUMPTION_CONTEXT_JOURNAL),
        (sig_b, CONSUMPTION_CONTEXT_JOURNAL),
    }
    # The journal stays OFF the lineage chain — consumption is the sidecar,
    # never derived_from.
    assert result.derived_from == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_journal_slice_consumption_materializes(
    migrated_pg: PostgresConfig,
):
    sig = uuid4()
    deps = InlineTargetDeps(
        llm=_ScriptedLLM(["field notes", "Entry body."]),
        system_prompt="PERSONA",
        max_rounds=1,
    )
    result = await journal_run_method(
        [{"id": sig, "title": "signal"}],
        {"analyst_id": "journal_assessor"},
        deps,
    )
    consumer = uuid4()  # stands in for the written journal row id
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        await record_output_consumption(
            conn,
            consumer_id=consumer,
            consumer_kind="journal_assessor",
            edges=result.consumed_edges,
        )
        row = await conn.fetchrow(
            "SELECT consumed_id, consumer_kind, context "
            "FROM output_consumption WHERE consumer_id = $1",
            consumer,
        )
        assert row is not None
        assert row["consumed_id"] == sig
        assert row["consumer_kind"] == "journal_assessor"
        assert row["context"] == CONSUMPTION_CONTEXT_JOURNAL
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 5. Degrade path — a consumption-write failure never breaks the compose
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_consumption_write_failure_does_not_break_compose():
    """The full degrade shape: the compose succeeds, its stamped edges hit a
    FAILING writer conn, nothing raises, and the compose result stands."""
    uid = uuid4()
    result = await synth._run(
        [_row(uid=uid)],
        _COMPOSE_OPTIONS,
        llm=_CannedLLM(_canned_payload("Composed fine [[ref:1]].")),
        max_tokens=512,
        temperature=0.2,
        system_prompt="unused",
    )
    assert result.consumed_edges == [(uid, CONSUMPTION_CONTEXT_BASIS)]
    n = await record_output_consumption(
        _BrokenConn(),
        consumer_id=uuid4(),
        consumer_kind="meta_findings_synthesizer",
        edges=result.consumed_edges,
    )
    assert n == 0  # logged + degraded — the composed finding is untouched
    assert result.finding.title == "Composed read"


# ---------------------------------------------------------------------------
# 6. review_flags — close by supersession, never deleted
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.asyncio
async def test_review_flags_close_by_supersession_never_deleted(
    migrated_pg: PostgresConfig,
):
    conn = await asyncpg.connect(migrated_pg.dsn)
    output_id, founded_on = uuid4(), uuid4()
    moved_at = datetime.now(timezone.utc) - timedelta(hours=1)
    try:
        flag_id = await conn.fetchval(
            "INSERT INTO review_flags (output_id, founded_on_id, moved_at, "
            "reason) VALUES ($1, $2, $3, 'foundation_superseded') "
            "RETURNING id",
            output_id, founded_on, moved_at,
        )

        # One OPEN flag per (consumer, foundation) pair — a re-scan cannot
        # stack a duplicate while the first is open.
        with pytest.raises(asyncpg.UniqueViolationError):
            await conn.execute(
                "INSERT INTO review_flags (output_id, founded_on_id, "
                "moved_at, reason) VALUES ($1, $2, $3, 'foundation_decayed')",
                output_id, founded_on, moved_at,
            )

        # Half-closed rows are unrepresentable (paired CHECK).
        with pytest.raises(asyncpg.CheckViolationError):
            await conn.execute(
                "UPDATE review_flags SET closed_by = $1 WHERE id = $2",
                uuid4(), flag_id,
            )

        # CLOSE BY SUPERSESSION: the later output stamps both fields.
        superseder = uuid4()
        await conn.execute(
            "UPDATE review_flags SET closed_by = $1, closed_at = now() "
            "WHERE id = $2",
            superseder, flag_id,
        )
        closed = await conn.fetchrow(
            "SELECT closed_by, closed_at FROM review_flags WHERE id = $1",
            flag_id,
        )
        assert closed["closed_by"] == superseder
        assert closed["closed_at"] is not None

        # A NEW episode for the same pair is representable again post-close.
        await conn.execute(
            "INSERT INTO review_flags (output_id, founded_on_id, moved_at, "
            "reason) VALUES ($1, $2, now(), 'foundation_moved_again')",
            output_id, founded_on,
        )

        # NEVER-DELETED posture is schema-enforced: DELETE fails loud on
        # open AND closed rows alike; the rows survive.
        with pytest.raises(asyncpg.PostgresError, match="never deleted"):
            await conn.execute(
                "DELETE FROM review_flags WHERE id = $1", flag_id
            )
        with pytest.raises(asyncpg.PostgresError, match="never deleted"):
            await conn.execute(
                "DELETE FROM review_flags WHERE output_id = $1", output_id
            )
        n = await conn.fetchval(
            "SELECT count(*) FROM review_flags WHERE output_id = $1",
            output_id,
        )
        assert n == 2  # both episodes durable — closed, not gone
    finally:
        await conn.close()


# ---------------------------------------------------------------------------
# 7. bearing_edges — undated/unprovenanced edges are unrepresentable
# ---------------------------------------------------------------------------


def _bearing_row(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "src_kind": "finding",
        "src_id": uuid4(),
        "src_as_of": datetime.now(timezone.utc),
        "dst_kind": "finding",
        "dst_id": uuid4(),
        "dst_as_of": datetime.now(timezone.utc) - timedelta(days=3),
        "weight": 0.8,
        "planes": ["vector", "entity"],
        "provenance_class": "live",
        "matcher_version": "kw-test-0.1",
    }
    row.update(overrides)
    return row


_BEARING_INSERT = (
    "INSERT INTO bearing_edges (src_kind, src_id, src_as_of, dst_kind, "
    "dst_id, dst_as_of, weight, planes, provenance_class, matcher_version) "
    "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)"
)


async def _insert_bearing(conn: asyncpg.Connection, row: dict[str, Any]) -> None:
    await conn.execute(
        _BEARING_INSERT,
        row["src_kind"], row["src_id"], row["src_as_of"], row["dst_kind"],
        row["dst_id"], row["dst_as_of"], row["weight"], row["planes"],
        row["provenance_class"], row["matcher_version"],
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_bearing_edges_constraints(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    try:
        # A fully-specified LIVE edge lands, defaulting edge_kind='bears_on'.
        ok = _bearing_row()
        await _insert_bearing(conn, ok)
        stored = await conn.fetchrow(
            "SELECT edge_kind, planes, provenance_class, created_at "
            "FROM bearing_edges WHERE src_id = $1 AND dst_id = $2",
            ok["src_id"], ok["dst_id"],
        )
        assert stored["edge_kind"] == "bears_on"
        assert list(stored["planes"]) == ["vector", "entity"]
        assert stored["provenance_class"] == "live"
        assert stored["created_at"] is not None

        # (src, dst, edge_kind) is unique — a re-match cannot stack.
        with pytest.raises(asyncpg.UniqueViolationError):
            await _insert_bearing(conn, ok)

        # Missing PROVENANCE is unrepresentable.
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_bearing(conn, _bearing_row(matcher_version=None))
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_bearing(conn, _bearing_row(provenance_class=None))
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_bearing(conn, _bearing_row(provenance_class="gold"))
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_bearing(conn, _bearing_row(planes=None))
        with pytest.raises(asyncpg.CheckViolationError):
            await _insert_bearing(conn, _bearing_row(planes=[]))

        # Missing DATES are unrepresentable — an undated bearing edge cannot
        # be audited or decayed.
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_bearing(conn, _bearing_row(src_as_of=None))
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_bearing(conn, _bearing_row(dst_as_of=None))

        # Weight is part of the edge, not optional garnish.
        with pytest.raises(asyncpg.NotNullViolationError):
            await _insert_bearing(conn, _bearing_row(weight=None))

        # The EXEMPLAR class is representable (never-pool-gold needs the
        # distinction at the schema layer, not a side convention).
        await _insert_bearing(conn, _bearing_row(provenance_class="exemplar"))
    finally:
        await conn.close()

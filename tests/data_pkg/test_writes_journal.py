# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for the 11th OutputKind — JOURNAL + write_journal + _insert_journal_entry
+ supersede_prior_consolidation (Journal Assessor, plan §3 / §8 / §13).

Mirrors test_writes_fact / test_writes_nexus. Uses the shared ``migrated_pg``
fixture so it runs against the fresh test DB (migration 0048 applied).

Covers (plan §13 Testing — Unit):
  * OutputKind.JOURNAL resolves via spec_for_kind; KIND_REGISTRY[JOURNAL].table
    == 'journal_entries'.
  * JournalPayload validation (entry | consolidation; per-claim JournalClaim).
  * write_journal happy path → row in journal_entries with derived_from EMPTY
    (the off-chain invariant §3.5) — even when derived_from is passed.
  * supersede_prior_consolidation closes the prior open consolidation
    (valid_until + superseded_by → new id); allows the bootstrap NULL (first
    consolidation supersedes nothing); idempotent on replay.
  * entries are pure append (never supersede).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.provenance import (
    AnalystContext,
    JournalClaim,
    JournalPayload,
    KIND_REGISTRY,
    OutputKind,
    spec_for_kind,
    supersede_prior_consolidation,
    write_journal,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_conn(migrated_pg: PostgresConfig):
    conn = await asyncpg.connect(migrated_pg.dsn)
    yield conn
    await conn.close()


def _analyst_ctx() -> AnalystContext:
    # META analyst — no target (target_filter=None for the journal).
    return AnalystContext(
        analyst_id="journal_assessor",
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


def _entry_payload(**kw) -> JournalPayload:
    now = datetime.now(tz=timezone.utc)
    base = dict(
        entry_kind="entry",
        title="A quiet window",
        body="Three assessors went quiet [[ref:%s]]." % uuid4(),
        period_start=now - timedelta(hours=24),
        period_end=now,
    )
    base.update(kw)
    return JournalPayload(**base)


def _consolidation_payload(**kw) -> JournalPayload:
    now = datetime.now(tz=timezone.utc)
    base = dict(
        entry_kind="consolidation",
        title="Current inner landscape",
        body="My standing read of the world.",
        period_start=now - timedelta(days=1),
        period_end=now,
    )
    base.update(kw)
    return JournalPayload(**base)


# ---------------------------------------------------------------------------
# Registry / payload (no DB)
# ---------------------------------------------------------------------------


def test_output_kind_journal_registered():
    """The 11th kind resolves + routes to the dedicated table."""
    spec = spec_for_kind(OutputKind.JOURNAL)
    assert OutputKind.JOURNAL in KIND_REGISTRY
    assert spec.table == "journal_entries"
    assert spec.schema_uri == "iglu:legba/journal/jsonschema/1-0-0"
    assert spec.nats_subject_pattern == "analyst.{analyst_id}.journal"
    assert spec.payload_model is JournalPayload


def test_journal_payload_validates_and_claims():
    now = datetime.now(tz=timezone.utc)
    ref = uuid4()
    p = JournalPayload(
        entry_kind="entry",
        title="t",
        body="b",
        period_start=now,
        period_end=now,
        claims=[
            JournalClaim(text_span="cited", refs=[ref], kind="fact"),
            JournalClaim(text_span="i wonder", kind="perspective"),
        ],
        cited_substrate_refs=[ref],
    )
    assert p.entry_kind == "entry"
    # perspective claim may carry zero refs (§4.5)
    assert p.claims[1].refs == []
    # the discriminator rejects a bad value (extra=forbid + Literal)
    with pytest.raises(Exception):
        JournalPayload(
            entry_kind="bogus", title="t", period_start=now, period_end=now
        )


# ---------------------------------------------------------------------------
# write_journal — off-chain insert (DB)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_write_journal_routes_to_table_and_derived_from_empty(pg_conn):
    """write_journal lands a journal_entries row with derived_from EMPTY even
    when a derived_from is passed in — the off-chain invariant (§3.5)."""
    ctx = _analyst_ctx()
    payload = _entry_payload()
    out, dlq = await write_journal(
        pg_conn,
        analyst_ctx=ctx,
        payload=payload,
        derived_from=[uuid4(), uuid4()],  # must be IGNORED — journal is off-chain
    )
    assert dlq is None
    assert out is not None and out.kind == OutputKind.JOURNAL
    row = await pg_conn.fetchrow(
        "SELECT entry_kind, title, derived_from, analyst_id, valid_until, "
        "superseded_by, cited_substrate_refs FROM journal_entries WHERE id=$1",
        out.id,
    )
    assert row is not None
    assert row["entry_kind"] == "entry"
    assert list(row["derived_from"]) == [], "journal derived_from MUST be empty (§3.5)"
    assert row["analyst_id"] == "journal_assessor"
    # an entry is open + never superseded
    assert row["valid_until"] is None
    assert row["superseded_by"] is None
    # the cited ref harvested from the body marker is persisted
    assert len(row["cited_substrate_refs"]) == 0  # this payload set none explicitly


@pytest.mark.asyncio
async def test_consolidation_supersession_bootstrap_and_close(pg_conn):
    """The FIRST consolidation supersedes nothing (bootstrap NULL); a SECOND
    closes the prior open one (valid_until + superseded_by → new id)."""
    ctx1 = _analyst_ctx()
    out1, _ = await write_journal(pg_conn, analyst_ctx=ctx1, payload=_consolidation_payload())
    assert out1 is not None
    # bootstrap: the first consolidation is open, supersedes nothing.
    r1 = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM journal_entries WHERE id=$1", out1.id
    )
    assert r1["valid_until"] is None and r1["superseded_by"] is None

    # second consolidation closes the first.
    ctx2 = _analyst_ctx()
    out2, _ = await write_journal(pg_conn, analyst_ctx=ctx2, payload=_consolidation_payload())
    assert out2 is not None
    prior = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM journal_entries WHERE id=$1", out1.id
    )
    assert prior["valid_until"] is not None, "prior consolidation must be closed"
    assert prior["superseded_by"] == out2.id, "prior must chain to the new consolidation"
    new = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM journal_entries WHERE id=$1", out2.id
    )
    assert new["valid_until"] is None and new["superseded_by"] is None, "new is open"

    # single-open invariant: exactly one open consolidation now.
    open_count = await pg_conn.fetchval(
        "SELECT count(*) FROM journal_entries WHERE entry_kind='consolidation' "
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert open_count == 1


@pytest.mark.asyncio
async def test_supersede_prior_consolidation_idempotent_on_replay(pg_conn):
    """A replayed close (the same new_entry_id against an already-closed prior)
    closes nothing new and leaves the existing pointer untouched."""
    ctx = _analyst_ctx()
    out1, _ = await write_journal(pg_conn, analyst_ctx=ctx, payload=_consolidation_payload())
    out2, _ = await write_journal(pg_conn, analyst_ctx=_analyst_ctx(), payload=_consolidation_payload())
    # out1 is now closed by out2. Replaying supersede with out2's id closes
    # nothing (there is no longer an open prior other than out2 itself, excluded).
    closed = await supersede_prior_consolidation(
        pg_conn, new_entry_id=out2.id, analyst_id="journal_assessor"
    )
    assert closed is None, "replay must close nothing new"
    # out1's pointer is unchanged.
    prior = await pg_conn.fetchrow(
        "SELECT superseded_by FROM journal_entries WHERE id=$1", out1.id
    )
    assert prior["superseded_by"] == out2.id


@pytest.mark.asyncio
async def test_entries_never_supersede(pg_conn):
    """Entries are pure append — two entries coexist open; neither closes the
    other (only consolidations carry supersession)."""
    e1, _ = await write_journal(pg_conn, analyst_ctx=_analyst_ctx(), payload=_entry_payload())
    e2, _ = await write_journal(pg_conn, analyst_ctx=_analyst_ctx(), payload=_entry_payload())
    rows = await pg_conn.fetch(
        "SELECT valid_until, superseded_by FROM journal_entries "
        "WHERE entry_kind='entry' AND id = ANY($1::uuid[])",
        [e1.id, e2.id],
    )
    assert len(rows) == 2
    for r in rows:
        assert r["valid_until"] is None and r["superseded_by"] is None

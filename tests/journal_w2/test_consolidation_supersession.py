# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Journal Wave 2 — the consolidation tier's supersession, end-to-end on the
DISPOSABLE container (plan §8 / §12 Wave 2).

Wave 1 already proved ``supersede_prior_consolidation`` at the helper level
(bootstrap NULL, single-open, idempotent). This module proves the SAME machinery
fires when the row is shaped the way the consolidator's ``run_method`` shapes it —
the tier discriminator (``_entry_kind_for_analyst('journal_consolidator')`` →
``'consolidation'``) is what makes the write path supersede — and adds the §12
"what did it believe, and when" history-retention assertion (the closed row is
RETAINED, not deleted, and the believed-at window is queryable).

NEVER touches the live ``legba`` / ``legba-postgres-1`` (the journal_w2 conftest
reuses the Wave-1 disposable 5544 harness).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.analysts.journal_assessor import (
    CONSOLIDATOR_ANALYST_ID,
    _entry_kind_for_analyst,
)
from legba.data.provenance import AnalystContext, JournalPayload, write_journal


def _consolidator_ctx() -> AnalystContext:
    """A META context for the consolidation tier (target_filter=None), running as
    the consolidator id so the tier discriminator selects ``consolidation``."""
    return AnalystContext(
        analyst_id=CONSOLIDATOR_ANALYST_ID,
        analyst_version="v" + uuid4().hex[:8],
        run_id=uuid4(),
        target_id=None,
        target_version=None,
    )


def _consolidation_payload_as_run_method_would(body: str) -> JournalPayload:
    """Shape a payload exactly as the consolidator ``run_method`` does: entry_kind
    derived from the consolidator id (NOT hardcoded), supersedes=None (the write
    path decides the link), a 7-day-ish reflection window."""
    entry_kind = _entry_kind_for_analyst(CONSOLIDATOR_ANALYST_ID)
    assert entry_kind == "consolidation"  # the tier discriminator drives this
    now = datetime.now(tz=timezone.utc)
    return JournalPayload(
        entry_kind=entry_kind,
        title=body[:60],
        body=body,
        period_start=now - timedelta(days=7),
        period_end=now,
        supersedes=None,
        honesty_flags=["forecast_unproven", "calibration_thin"],
    )


@pytest.mark.asyncio
async def test_two_consolidations_in_sequence_close_retain_and_remain_queryable(pg_conn):
    """Two consolidations written in sequence (as the consolidator emits them):
      * the FIRST supersedes nothing (bootstrap NULL);
      * the SECOND closes the first (valid_until set, superseded_by → the newer);
      * the newer is the single OPEN "current inner landscape";
      * the closed row is RETAINED, not deleted → "what did it believe, and when"
        is queryable (the believed-at window survives).
    """
    # --- first consolidation: bootstrap (supersedes nothing) ---
    out1, dlq1 = await write_journal(
        pg_conn,
        analyst_ctx=_consolidator_ctx(),
        payload=_consolidation_payload_as_run_method_would(
            "Inner landscape #1: the world feels quiet; three assessors went dark."
        ),
    )
    assert dlq1 is None and out1 is not None
    r1 = await pg_conn.fetchrow(
        "SELECT entry_kind, valid_until, superseded_by FROM journal_entries WHERE id=$1",
        out1.id,
    )
    assert r1["entry_kind"] == "consolidation"
    assert r1["valid_until"] is None and r1["superseded_by"] is None, "bootstrap: open, no supersede"

    # --- second consolidation: closes the first ---
    out2, dlq2 = await write_journal(
        pg_conn,
        analyst_ctx=_consolidator_ctx(),
        payload=_consolidation_payload_as_run_method_would(
            "Inner landscape #2: the quiet broke; a nexus flipped antagonistic."
        ),
    )
    assert dlq2 is None and out2 is not None

    prior = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by, body, period_start, period_end "
        "FROM journal_entries WHERE id=$1",
        out1.id,
    )
    assert prior["valid_until"] is not None, "the older consolidation is CLOSED"
    assert prior["superseded_by"] == out2.id, "the older forward-points to the newer"

    new = await pg_conn.fetchrow(
        "SELECT valid_until, superseded_by FROM journal_entries WHERE id=$1", out2.id
    )
    assert new["valid_until"] is None and new["superseded_by"] is None, "the newer is OPEN"

    # the single open consolidation == the "current inner landscape".
    open_rows = await pg_conn.fetch(
        "SELECT id FROM journal_entries WHERE entry_kind='consolidation' "
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert [r["id"] for r in open_rows] == [out2.id]

    # --- "what did it believe, and when" — the closed row is RETAINED, queryable ---
    history = await pg_conn.fetch(
        "SELECT id, body, produced_at, valid_until FROM journal_entries "
        "WHERE entry_kind='consolidation' ORDER BY produced_at ASC"
    )
    assert len(history) == 2, "no row was deleted — history accrues"
    assert history[0]["id"] == out1.id and "#1" in history[0]["body"]
    assert history[1]["id"] == out2.id and "#2" in history[1]["body"]
    # the believed-at window of the retired belief is intact (produced → closed).
    assert history[0]["valid_until"] is not None
    assert history[0]["produced_at"] <= history[0]["valid_until"]


@pytest.mark.asyncio
async def test_third_consolidation_chains_the_history(pg_conn):
    """A THIRD consolidation closes the second; the full chain is walkable
    (#1 → #2 → #3), proving the believed-history is a linked, retained record."""
    out1, _ = await write_journal(
        pg_conn, analyst_ctx=_consolidator_ctx(),
        payload=_consolidation_payload_as_run_method_would("Inner landscape #1"),
    )
    out2, _ = await write_journal(
        pg_conn, analyst_ctx=_consolidator_ctx(),
        payload=_consolidation_payload_as_run_method_would("Inner landscape #2"),
    )
    out3, _ = await write_journal(
        pg_conn, analyst_ctx=_consolidator_ctx(),
        payload=_consolidation_payload_as_run_method_would("Inner landscape #3"),
    )
    # exactly one open; the chain pointers are 1→2→3.
    open_count = await pg_conn.fetchval(
        "SELECT count(*) FROM journal_entries WHERE entry_kind='consolidation' "
        "AND valid_until IS NULL AND superseded_by IS NULL"
    )
    assert open_count == 1
    s1 = await pg_conn.fetchval("SELECT superseded_by FROM journal_entries WHERE id=$1", out1.id)
    s2 = await pg_conn.fetchval("SELECT superseded_by FROM journal_entries WHERE id=$1", out2.id)
    s3 = await pg_conn.fetchval("SELECT superseded_by FROM journal_entries WHERE id=$1", out3.id)
    assert s1 == out2.id
    assert s2 == out3.id
    assert s3 is None  # the head is open
    # all three retained.
    total = await pg_conn.fetchval(
        "SELECT count(*) FROM journal_entries WHERE entry_kind='consolidation'"
    )
    assert total == 3

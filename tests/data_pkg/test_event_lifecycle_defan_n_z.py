# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-Z — the lifecycle scan without the signal-links × entity-links fan-out
returns exactly the rows the fanned query returned (live: 718 rows identical,
31.5 s -> 0.9 s; here on the isolated test database)."""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_event_clustering_p1 import (  # noqa: E402
    _entity,
    _link_signal_entity,
    _options,
    _signal,
    pg_pool,  # noqa: F401 — the module's fixture, reused
)

from legba.data.analysts.deterministic_handlers import (  # noqa: E402
    _event_lifecycle,
    event_clustering,
)


@pytest.mark.asyncio
async def test_the_defanned_scan_equals_the_fanned_scan(pg_pool, monkeypatch) -> None:
    monkeypatch.setenv("LEGBA_EVENTS", "1")
    opts = _options(analyst_id=f"analyst.nz_defan_{uuid4().hex[:6]}")
    async with pg_pool.acquire() as conn:
        for story in range(3):
            ents = [await _entity(conn, f"NZ {story} {k} {uuid4().hex[:6]}")
                    for k in range(4)]
            for i in range(5):
                sid = await _signal(conn, title=f"Story {story} strike hits port",
                                    source_id=f"rss_{story}_{i}", credibility=0.8)
                for e in ents:
                    await _link_signal_entity(conn, sid, e)
    deps = SimpleNamespace(pg_pool=pg_pool, extras={})
    await event_clustering.handle([], opts, deps)
    await event_clustering.handle([], opts, deps)
    at = datetime.now(timezone.utc)
    touched = [r["id"] for r in await pg_pool.fetch(
        "SELECT id FROM events WHERE analyst_id=$1", opts["analyst_id"])]
    assert touched
    for ids in ([], touched):
        for limit in (5000, 2):
            old = await pg_pool.fetch(
                _event_lifecycle._OPEN_EVENT_STATS_SQL_FANNED, at, ids, limit)
            new = await pg_pool.fetch(
                _event_lifecycle._OPEN_EVENT_STATS_SQL, at, ids, limit)
            assert [tuple(r.values()) for r in new] == [
                tuple(r.values()) for r in old]
            assert [list(r.keys()) for r in new[:1]] == [list(r.keys()) for r in old[:1]]

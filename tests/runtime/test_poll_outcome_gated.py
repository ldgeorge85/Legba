# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Migration 0226 (W3-F8) — a poll whose every entry the keyword gate dropped
records ``outcome='gated'`` and the gate's per-poll drop count.

Driven through ``SourceCore.pull_once`` against the capturing substrate doubles
of ``test_source_poll_outcomes`` (the INSERT is decoded column by column); the
real-driver read-back lives in
``tests/data_pkg/test_migration_0226_poll_outcome_gated.py``.

Covered:
  * every entry dropped, none written -> ``gated`` + ``keyword_filtered=N``;
  * some dropped, some admitted -> ``success`` (productivity wins) + the count;
  * an ungated source's empty poll -> ``empty`` and ``keyword_filtered`` None
    (NULL = not gated, never a 0);
  * a gated source whose feed is empty -> ``empty`` and ``keyword_filtered=0``;
  * a failing poll that also dropped entries -> ``error`` (precedence) with the
    count kept;
  * the pure rollup's precedence table.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import uuid4

import pytest

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.properties import Cron
from legba.data.schemas.source import (
    CadenceBlock,
    FilterStage,
    SourceDescriptor,
    SourceIdentity,
    SourcePipeline,
    SourceScope,
)
from legba.data.sources._contract import InMemoryStateStore, Signal, SourceContext
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps, _RawConfig
from legba.runtime.source_poll_outcome import classify_poll_outcome
from tests.runtime.test_source_poll_outcomes import _CapturingPool, _StubHandler

_VERSION = "b" * 16
_BASE = datetime(2026, 10, 3, 9, 0, tzinfo=timezone.utc)
_GATE = [FilterStage(kind="keyword_gate", config={"include": ["sanctions"]})]


def _descriptor(source_id: str, *, gated: bool) -> SourceDescriptor:
    return SourceDescriptor(
        identity=SourceIdentity(
            id=source_id, name="gated-test", kind="rss",
            schema_uri="legba/source/3.0.0", version=_VERSION,
            owner="test:gated", created=datetime.now(tz=timezone.utc),
            state=LifecycleState.ACTIVE,
        ),
        scope=SourceScope(owner_tenant="acme", languages=["en"]),
        acquisition="poll",
        config={"url": "http://unused"},
        cadence=CadenceBlock(schedule=Cron(raw="*/5 * * * *")),
        pipeline=SourcePipeline(
            media="reference", ingestion_filters=list(_GATE) if gated else [],
        ),
    )


def _entry(title: str, minute: int) -> Signal:
    return Signal(
        source_id="source.test.gated", modality="text",
        payload={"title": title, "_published_at_dt": _BASE + timedelta(minutes=minute)},
        content_hash=uuid4().hex,
    )


def _build(handler, *, gated: bool = True):
    sid = f"source.test.gated_{uuid4().hex[:8]}"
    sd = _descriptor(sid, gated=gated)
    pool = _CapturingPool()
    core = SourceCore(
        f"source::{sid}::gt",
        SourceDeps(descriptor=sd, deps=StandardDeps(pg_pool=pool, nats_publish=None)),
    )
    core.sd.handler = handler
    store = InMemoryStateStore()

    def _ctx():
        return SourceContext(
            target_id=sid, target_version=_VERSION, source_id=sid,
            config=_RawConfig(**(sd.config or {})), state_store=store,
            scope_geo=[], scope_languages=["en"],
        )

    core._make_context = _ctx  # type: ignore[method-assign]
    return core, pool


@pytest.mark.asyncio
async def test_every_entry_dropped_records_gated_with_the_count() -> None:
    core, pool = _build(_StubHandler([_entry("Weather", 0), _entry("Football", 1), _entry("Fair", 2)]))
    result = await core.pull_once()
    assert result["signals_written"] == 0 and result["keyword_filtered"] == 3
    (row,) = pool.outcome_writes
    assert row["outcome"] == "gated"
    assert row["keyword_filtered"] == 3
    assert row["signals_written"] == 0 and row["error"] is None


@pytest.mark.asyncio
async def test_a_partly_admitted_poll_is_a_success_and_keeps_the_count() -> None:
    core, pool = _build(_StubHandler([_entry("New sanctions listed", 0), _entry("Weather", 1)]))
    await core.pull_once()
    (row,) = pool.outcome_writes
    assert row["outcome"] == "success"
    assert row["signals_written"] == 1 and row["keyword_filtered"] == 1


@pytest.mark.asyncio
async def test_an_ungated_empty_poll_is_empty_with_a_null_count() -> None:
    core, pool = _build(_StubHandler([]), gated=False)
    await core.pull_once()
    (row,) = pool.outcome_writes
    assert row["outcome"] == "empty"
    assert row["keyword_filtered"] is None        # NULL = not gated, never 0


@pytest.mark.asyncio
async def test_a_gated_source_with_an_empty_feed_is_empty_not_gated() -> None:
    core, pool = _build(_StubHandler([]))
    await core.pull_once()
    (row,) = pool.outcome_writes
    assert row["outcome"] == "empty"
    assert row["keyword_filtered"] == 0           # gated, dropped nothing


@pytest.mark.asyncio
async def test_a_failing_poll_stays_an_error_even_with_drops() -> None:
    handler = _StubHandler([_entry("Weather", 0)], raise_exc=RuntimeError("boom"))
    core, pool = _build(handler)
    await core.pull_once()
    (row,) = pool.outcome_writes
    assert row["outcome"] == "error"
    assert row["keyword_filtered"] == 1


@pytest.mark.parametrize(
    "written,reserve,errored,health,dropped,expected",
    [
        (1, 0, None, None, 5, "success"),
        (0, 2, None, None, 5, "success"),
        (0, 0, "boom", None, 5, "error"),
        (0, 0, None, "degraded", 5, "error"),
        (0, 0, None, "unhealthy", 5, "error"),
        (0, 0, None, "healthy", 5, "gated"),
        (0, 0, None, None, 1, "gated"),
        (0, 0, None, None, 0, "empty"),
        (0, 0, None, None, None, "empty"),
    ],
)
def test_classify_poll_outcome_precedence(written, reserve, errored, health, dropped, expected) -> None:
    assert classify_poll_outcome(
        signals_written=written, reserve_unchanged=reserve, errored=errored,
        health_state=health, keyword_filtered=dropped,
    ) == expected

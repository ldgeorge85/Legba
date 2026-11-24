# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-4: the subscribe connector against a LOCAL fake SSE server.

A real TCP server on 127.0.0.1 (``tests/runtime/_sse_fake_server.py``) and the
REAL guarded HTTP client (the egress guard admits 127.0.0.1 only through the
operator allowlist, set here), the real :class:`StreamCore` on the spine, and
the recording pool from the ingestion tests standing in for Postgres. Covers:
connect + records (signals, receipts at ``<stream url>#event=<id>``, cursor),
reconnect WITH resume (``Last-Event-ID``, no gap), reconnect WITHOUT resume (a
declared ``no_resume`` gap), queue overflow (a declared, counted gap — every
received event is either landed or counted dropped), a stall (no bytes for
the idle timeout → reconnect, an ``error``/``unhealthy`` window), a malformed
event (counted, its bytes still kept), shutdown (queued events landed or
declared), the first-connect age cap, and the refusals (licence ledger,
undeclarable credential, a non-stream response)."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.schemas.source import SourceDescriptor
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceDeps
from legba.runtime.stream_connector import SubscribeConnector
from legba.runtime.stream_ingest import StreamCore

from tests.runtime import _ingest_fakes as F
from tests.runtime._sse_fake_server import FakeSseServer, Plan, item

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ingestion" / "source_test_subscribe_sse.yaml"
NOW = datetime.now(tz=timezone.utc)
FRESH = (NOW - timedelta(minutes=5)).isoformat()


@pytest.fixture(autouse=True)
def _rig(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGBA_EGRESS_ALLOW_HOSTS", "127.0.0.1")
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")


class _Lease:
    def __init__(self) -> None:
        self.released = 0

    async def acquire(self) -> bool:
        return True

    async def alive(self) -> bool:
        return True

    async def release(self) -> None:
        self.released += 1


class _LedgerPool(F.RecordingPool):
    """The recording pool, plus a licence-ledger row for one host."""

    def __init__(self, ledger: dict[str, str]) -> None:
        super().__init__()
        self.ledger = ledger

    def acquire(self) -> Any:
        cm = super().acquire()
        pool = self

        class _A:
            async def __aenter__(self_a):
                conn = await cm.__aenter__()
                real = conn.fetch

                async def fetch(q, *a):
                    if "FROM source_credibility" in q:
                        return [{"source_host": h, "license_class": c} for h, c in pool.ledger.items()
                                if h in a[0]]
                    return await real(q, *a)
                conn.fetch = fetch
                return conn

            async def __aexit__(self_a, *e):
                return False
        return _A()


def _desc(url: str, **subscribe: Any) -> SourceDescriptor:
    body = yaml.safe_load(FIXTURE.read_text())
    body["subscribe"]["url"] = url
    body["subscribe"].update(subscribe)
    return SourceDescriptor.model_validate(body, strict=False)


def _connector(desc: SourceDescriptor, pool: Any = None, *, fast: dict | None = None,
               secrets: Any = None, drain_seconds: float = 2.0) -> tuple[SubscribeConnector, Any, Any]:
    pool = pool if pool is not None else F.RecordingPool()
    nats = F.NatsRecorder()
    sd = SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats,
                                                       secrets_resolve=secrets))
    core = StreamCore(f"source::{desc.identity.id}::{desc.identity.version[:16]}", sd)
    # Test-speed bounds (below the save-time minimums, set on the parsed copy).
    core.subscribe = core.subscribe.model_copy(update={
        "idle_timeout_seconds": 0.6, "backoff_initial_seconds": 0.05, "backoff_max_seconds": 0.2,
        "outcome_window_seconds": 3600, **(fast or {})})
    return SubscribeConnector(core, lease=_Lease(), drain_seconds=drain_seconds), pool, nats


async def _until(pred, timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not pred():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.02)


def _sql(pool: F.RecordingPool, prefix: str) -> list[tuple]:
    return [c for c in pool.calls if c[1].startswith(prefix)]


def _gaps(pool) -> list[tuple]:
    return [c[2] for c in _sql(pool, "INSERT INTO public.source_stream_gaps")]


def _windows(pool) -> list[dict]:
    out = []
    for _m, q, args in _sql(pool, "INSERT INTO public.source_poll_outcomes"):
        cols = q.split("(", 1)[1].split(")", 1)[0].replace(" ", "").split(",")
        row = dict(zip(cols, args))
        if isinstance(row.get("stream"), str):
            row["stream"] = json.loads(row["stream"])
        out.append(row)
    return out


def _events(n: int, start: int = 1, *, published: str = FRESH) -> list[tuple[str, str]]:
    return [(str(i), item(i, published=published)) for i in range(start, start + n)]


# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_connect_and_records_land_with_receipts_and_cursor() -> None:
    async with FakeSseServer([Plan(events=_events(3), end="beat")]) as srv:
        conn, pool, nats = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: len(pool.signals) == 3)
        await _until(lambda: conn.core.cursor.get("last_event_id") == "3")
        await conn.stop()
    assert srv.requests[0]["accept"] == "text/event-stream"
    assert "last-event-id" not in srv.requests[0]        # first connect: nothing to resume
    assert len(nats.messages) == 3
    versions = _sql(pool, "INSERT INTO fetch_versions")
    addresses = sorted(c[2][0] for c in versions)
    assert addresses == [f"http://127.0.0.1:{srv.port}/v1/stream#event={i}" for i in (1, 2, 3)]
    assert all(c[2][7] == "text/event-stream" and c[2][4] == "response" for c in versions)
    links = [c for c in pool.calls if "fetch_version_records" in c[1]]
    assert len(links) == 3 and all(c[2][0] == "signal" and c[2][2] == "response" for c in links)
    final = _windows(pool)[-1]
    assert final["outcome"] == "success" and final["stream"]["state"] == "stopped"
    assert final["stream"]["events"] == 3 and final["signals_written"] == 3


@pytest.mark.asyncio
async def test_reconnect_resumes_from_the_last_event_id_with_no_gap() -> None:
    plans = [Plan(events=_events(2), end="close"), Plan(events=_events(2, start=3), end="beat")]
    async with FakeSseServer(plans) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: len(pool.signals) == 4)
        await conn.stop()
    assert srv.requests[1].get("last-event-id") == "2"
    assert _gaps(pool) == []
    assert sum(w["stream"]["reconnects"] for w in _windows(pool)) == 1
    assert _windows(pool)[-1]["stream"]["resumed"] is True


@pytest.mark.asyncio
async def test_a_restart_resumes_from_the_persisted_cursor() -> None:
    plans = [Plan(events=_events(2), end="beat")]
    async with FakeSseServer(plans) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: conn.core.cursor.get("last_event_id") == "2")
        await conn.stop()
        srv.plans = [Plan(events=_events(1, start=3), end="beat")]
        # a NEW connector (a runtime restart) on the same pool = the same cursor row
        conn2, _, _ = _connector(_desc(srv.url), pool)
        conn2.start()
        await _until(lambda: len(pool.signals) == 3)
        await conn2.stop()
    assert srv.requests[-1].get("last-event-id") == "2"
    assert _gaps(pool) == []


@pytest.mark.asyncio
async def test_a_lost_lease_ends_the_session_and_the_next_resumes_from_the_landed_cursor() -> None:
    class _Flaky(_Lease):
        def __init__(self) -> None:
            super().__init__()
            self.checks = 0

        async def alive(self) -> bool:
            self.checks += 1
            return self.checks > 1          # the first check finds the lease gone

    async with FakeSseServer([Plan(events=_events(2), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url), fast={"outcome_window_seconds": 0.5})
        conn.lease = _Flaky()
        conn._lease_retry_s = 0.05
        conn.start()
        await _until(lambda: conn.lease.released >= 1 and len(srv.requests) >= 2)
        await conn.stop()
    assert "last-event-id" not in srv.requests[0]
    assert srv.requests[1].get("last-event-id") == "2"      # what landed, persisted
    assert len(pool.signals) == 2


@pytest.mark.asyncio
async def test_reconnect_without_resume_declares_a_gap() -> None:
    plans = [Plan(events=_events(2), end="close"), Plan(events=_events(1, start=3), end="beat")]
    async with FakeSseServer(plans) as srv:
        conn, pool, _ = _connector(_desc(srv.url, resume="none"))
        conn.start()
        await _until(lambda: len(pool.signals) == 3)
        await conn.stop()
    assert "last-event-id" not in srv.requests[1]
    gaps = _gaps(pool)
    assert len(gaps) == 1
    (source_id, _v, _t, transport, kind, opened, closed, after, _f, _l, dropped, detail) = gaps[0]
    assert (source_id, transport, kind, after, dropped) == ("source.test.subscribe_sse", "sse", "no_resume", "2", None)
    assert closed >= opened and "resume=none" in json.loads(detail)["reason"]
    assert sum(w["stream"]["gaps"] for w in _windows(pool)) == 1


@pytest.mark.asyncio
async def test_overflow_drops_are_declared_and_counted_never_silent() -> None:
    plans = [Plan(events=_events(12) + [("99", item(99, published=FRESH))], end="beat")]
    # The last event arrives after a pause, once the queue has room again.
    plans[0].gap_s = 0.0
    async with FakeSseServer(plans) as srv:
        conn, pool, _ = _connector(_desc(srv.url), fast={"queue_max": 2, "max_events_per_second": 4.0,
                                                         "batch_max": 1})
        conn.start()
        await _until(lambda: conn.stats.dropped > 0 or _gaps(pool), timeout=8)
        await _until(lambda: conn.queue.empty() and not conn._inflight, timeout=10)
        await conn.stop()
    windows = _windows(pool)
    received = sum(w["stream"]["events"] for w in windows)
    dropped = sum(w["stream"]["dropped"] for w in windows)
    assert received == 13 and dropped >= 1
    assert len(pool.signals) + dropped == received          # every event: landed or counted
    gaps = _gaps(pool)
    assert gaps and all(g[4] == "overflow" for g in gaps)
    assert sum(g[10] for g in gaps) == dropped
    assert json.loads(gaps[0][11]) == {"queue_max": 2}


@pytest.mark.asyncio
async def test_a_stall_reconnects_and_reports_an_unhealthy_window() -> None:
    plans = [Plan(events=_events(1), end="hang"), Plan(events=_events(1, start=2), end="beat")]
    async with FakeSseServer(plans) as srv:
        conn, pool, _ = _connector(_desc(srv.url), fast={"backoff_initial_seconds": 1.5,
                                                         "backoff_max_seconds": 1.5})
        conn.start()
        await _until(lambda: conn.state == "stalled", timeout=6)
        await conn._write_window()            # the window that saw the stall
        await conn._write_window()            # a whole window down
        await _until(lambda: len(pool.signals) == 2, timeout=8)
        await conn.stop()
    w = _windows(pool)
    assert w[0]["stream"]["stalls"] == 1 and w[0]["outcome"] == "success"   # it landed ev 1 first
    assert w[1]["outcome"] == "error" and w[1]["health_state"] == "unhealthy"
    assert w[1]["stream"]["state"] == "stalled" and w[1]["error"].startswith("stream_stalled")
    assert srv.requests[1].get("last-event-id") == "1"


@pytest.mark.asyncio
async def test_a_quiet_connection_is_an_honest_quiet_window() -> None:
    async with FakeSseServer([Plan(events=_events(1), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: len(pool.signals) == 1)
        await conn._write_window()
        await asyncio.sleep(0.2)              # keep-alives only
        await conn._write_window()
        await conn.stop()
    quiet = _windows(pool)[1]
    assert quiet["outcome"] == "empty" and quiet["health_state"] == "healthy"
    assert quiet["stream"]["state"] == "quiet" and quiet["stream"]["heartbeats"] > 0
    assert quiet["newest_entry_ts"] is not None          # the watchdog's honest-quiet evidence


@pytest.mark.asyncio
async def test_a_malformed_event_is_counted_and_still_kept_as_a_receipt() -> None:
    events = [("1", "this is not json"), ("2", item(2, published=FRESH)), ("3", "[1, 2]")]
    async with FakeSseServer([Plan(events=events, end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: conn.core.cursor.get("last_event_id") == "3")
        await conn.stop()
    assert len(pool.signals) == 1
    final = _windows(pool)[-1]
    assert final["stream"]["malformed"] == 2 and final["stream"]["events"] == 3
    assert len(_sql(pool, "INSERT INTO fetch_versions")) == 3        # all three received, all kept


@pytest.mark.asyncio
async def test_shutdown_declares_queued_events_on_a_stream_that_cannot_resume() -> None:
    async with FakeSseServer([Plan(events=_events(6), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url, resume="none"),
                                   fast={"max_events_per_second": 0.5, "batch_max": 1}, drain_seconds=0.1)
        conn.start()
        await _until(lambda: conn.stats.events == 6)
        await conn.stop()
    gaps = [g for g in _gaps(pool) if g[4] == "shutdown"]
    assert len(gaps) == 1 and gaps[0][10] >= 1
    assert len(pool.signals) + gaps[0][10] == 6
    assert _windows(pool)[-1]["stream"]["state"] == "stopped"
    assert conn.lease.released == 1


@pytest.mark.asyncio
async def test_shutdown_on_a_resumable_stream_leaves_queued_events_to_the_replay() -> None:
    async with FakeSseServer([Plan(events=_events(6), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url), fast={"max_events_per_second": 0.5, "batch_max": 1},
                                   drain_seconds=0.1)
        conn.start()
        await _until(lambda: conn.stats.events == 6 and conn.core.cursor.get("last_event_id"))
        await conn.stop()
    assert _gaps(pool) == []
    assert int(conn.core.cursor["last_event_id"]) == len(pool.signals) < 6


@pytest.mark.asyncio
async def test_a_first_connect_honours_the_first_poll_age_cap_and_counts_skips() -> None:
    old = (NOW - timedelta(days=90)).isoformat()
    events = _events(3, published=old) + _events(2, start=4)
    async with FakeSseServer([Plan(events=events, end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: conn.core.cursor.get("last_event_id") == "5")
        await conn.stop()
    assert len(pool.signals) == 2
    final = _windows(pool)[-1]
    assert final["age_skipped"] == 3


@pytest.mark.asyncio
async def test_a_resumed_connect_is_not_age_capped() -> None:
    old = (NOW - timedelta(days=90)).isoformat()
    async with FakeSseServer([Plan(events=_events(1), end="close"),
                              Plan(events=_events(2, start=2, published=old), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: len(pool.signals) == 3)
        await conn.stop()
    assert all(not w.get("age_skipped") for w in _windows(pool))


@pytest.mark.asyncio
async def test_a_host_the_licence_ledger_marks_restrictive_is_refused() -> None:
    async with FakeSseServer([Plan(events=_events(1), end="beat")]) as srv:
        pool = _LedgerPool({"127.0.0.1": "tos_restrictive"})
        conn, pool, _ = _connector(_desc(srv.url), pool)
        conn.start()
        await _until(lambda: conn.state == "refused")
        await conn._write_window()
        await conn.stop()
    assert srv.requests == []                     # never connected
    w = _windows(pool)[0]
    assert w["outcome"] == "error" and w["stream"]["state"] == "refused"
    assert "licence ledger" in w["error"]


@pytest.mark.asyncio
async def test_a_declared_credential_with_no_resolver_refuses_to_connect() -> None:
    async with FakeSseServer([Plan(events=_events(1), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url, auth={"secret_ref": "source.test.stream_token",
                                                         "value_template": "Bearer {secret}"}))
        conn.start()
        await _until(lambda: conn.state == "refused")
        await conn._write_window()
        await conn.stop()
    assert srv.requests == []
    assert "no secrets resolver" in _windows(pool)[0]["error"]
    assert "unauthenticated" in _windows(pool)[0]["error"]


@pytest.mark.asyncio
async def test_a_resolved_credential_is_sent_as_declared() -> None:
    async def _secrets(ref: str) -> bytes:
        assert ref == "source.test.stream_token"
        return b"s3cret-value"

    async with FakeSseServer([Plan(events=_events(1), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url, auth={"secret_ref": "source.test.stream_token",
                                                         "header": "X-Api-Key"}), secrets=_secrets)
        conn.start()
        await _until(lambda: len(pool.signals) == 1)
        await conn.stop()
    assert srv.requests[0]["x-api-key"] == "s3cret-value"


@pytest.mark.asyncio
async def test_a_non_stream_response_is_a_refusal() -> None:
    async with FakeSseServer([Plan(events=[], content_type="application/json", end="close")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: conn.state == "refused")
        await conn._write_window()
        await conn.stop()
    assert "not an event stream" in _windows(pool)[0]["error"]


@pytest.mark.asyncio
async def test_an_http_error_is_reconnecting_not_refused_when_it_can_heal() -> None:
    async with FakeSseServer([Plan(status=503), Plan(events=_events(1), end="beat")]) as srv:
        conn, pool, _ = _connector(_desc(srv.url))
        conn.start()
        await _until(lambda: len(pool.signals) == 1)
        await conn.stop()
    assert len(srv.requests) >= 2


@pytest.mark.asyncio
async def test_a_kind_that_cannot_parse_events_is_refused_loud() -> None:
    body = yaml.safe_load(FIXTURE.read_text())
    body["identity"]["kind"] = "rss"
    body["config"] = {"url": {"factory_kind": "text", "raw": "https://news.example.org/feed"}}
    async with FakeSseServer([Plan(events=_events(1), end="beat")]) as srv:
        body["subscribe"]["url"] = srv.url
        conn, pool, _ = _connector(SourceDescriptor.model_validate(body, strict=False))
        conn.start()
        await _until(lambda: _windows(pool))
        await conn.stop()
    assert srv.requests == []
    assert "cannot serve acquisition='subscribe'" in _windows(pool)[0]["error"]

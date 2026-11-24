# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-4 — a record arriving by SUBSCRIBE lands exactly the sink rows the same
record lands by POLL (no second write path).

The way the sink and intake lanes proved theirs (``test_signal_sink_
equivalence.py``, ``test_intake_equivalence.py``): the same items, the same
source id and version, two runs, each on its own recording pool and NATS
recorder —

* POLL: a ``json_api`` source whose URL returns the items, through
  ``SourceCore.pull_once``;
* SUBSCRIBE: a ``stream_json`` source receiving the same items as SSE events
  (one item per event), through ``StreamCore.ingest_batch``.

Asserted identical after normalising run-specific uuids and clock reads: every
statement on ``signals`` and ``signal_aliases`` with its arguments, in order,
and every published NATS message — EXCEPT ``raw_provenance``, which records
how the record arrived (``fetch_kind: json_api`` + the poll window vs.
``fetch_kind: stream`` + the event id) and is asserted to differ in exactly
that way. The receipts differ by design too: the poll keeps one response
version at the URL, the stream one version per event at ``<url>#event=<id>``.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest

from legba.data.schemas.source import SourceDescriptor
from legba.data.sources.stream_event import StreamEvent
from legba.runtime.deps import StandardDeps
from legba.runtime.source_actor import SourceCore, SourceDeps
from legba.runtime.source_first_poll_age import FirstPollAgeCap
from legba.runtime.stream_ingest import StreamCore, WindowStats

from tests.runtime import _ingest_fakes as F

URL = "https://who.example.org/api/don/stream"
SID = "source.test.i4_equivalence"
_MAPPING = {"id_path": "Id", "title_path": "Title", "timestamp_path": "PublicationDateAndTime",
            "body_path": "Summary", "url_path": "Url", "language": "en"}


@pytest.fixture(autouse=True)
def _rig(tmp_path, monkeypatch):
    monkeypatch.setenv("LEGBA_ARCHIVE_ROOT", str(tmp_path / "archive"))
    monkeypatch.setenv("LEGBA_FETCH_RECEIPTS", "1")


def _items(now: datetime) -> list[dict[str, Any]]:
    return [{"Id": f"don-{i}", "Title": f"Disease outbreak {i}", "Summary": f"Summary {i} of an outbreak.",
             "PublicationDateAndTime": (now - timedelta(hours=i)).isoformat(),
             "Url": f"https://who.example.org/don/{i}"} for i in range(1, 4)]


def _poll_desc() -> SourceDescriptor:
    return F.descriptor(SID, "json_api", {"url_template": URL, "items_path": "value", **_MAPPING})


def _subscribe_desc() -> SourceDescriptor:
    body = _poll_desc().model_dump(mode="json")
    body["identity"]["kind"] = "stream_json"
    body["acquisition"] = "subscribe"
    body["cadence"] = None
    body["config"] = dict(_MAPPING)
    body["subscribe"] = {"transport": "sse", "url": URL}
    return SourceDescriptor.model_validate(body, strict=False)


async def _poll(now: datetime) -> tuple[F.RecordingPool, F.NatsRecorder, dict]:
    from legba.data.sources.json_api import JsonApiConfig, JsonApiSourceHandler

    body = json.dumps({"value": _items(now)}).encode()

    def _serve(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    desc = _poll_desc()
    handler = JsonApiSourceHandler(JsonApiConfig(url_template=URL, items_path="value", **_MAPPING),
                                   http_client=F.client_for(_serve))
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    core = SourceCore(f"source::{SID}::{desc.identity.version[:16]}",
                      SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats),
                                 handler=handler))
    return pool, nats, await core.pull_once()


async def _subscribe(now: datetime) -> tuple[F.RecordingPool, F.NatsRecorder, WindowStats]:
    desc = _subscribe_desc()
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    core = StreamCore(f"source::{SID}::{desc.identity.version[:16]}",
                      SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats)))
    await core.load_cursor()
    events = []
    for i, it in enumerate(_items(now), start=1):
        data = json.dumps(it)
        events.append(StreamEvent(id=str(i), event="message", data=data,
                                  raw=f"id: {i}\ndata: {data}\n".encode(), received_at=now))
    stats = WindowStats(opened_at=now)
    retry = await core.ingest_batch(events, stats=stats,
                                    age_cap=FirstPollAgeCap(core.parser(), first_poll=False))
    assert retry == []
    return pool, nats, stats


def _rows(pool: F.RecordingPool) -> list:
    out = []
    for method, q, args in pool.calls:
        if not (q.startswith("INSERT INTO signals") or "signal_aliases" in q or q.startswith("UPDATE signals")):
            continue
        args = list(args)
        if q.startswith("INSERT INTO signals"):
            args[17] = "<raw_provenance>"
        out.append((method, q, args))
    return out


def _messages(nats: F.NatsRecorder) -> list:
    out = []
    for subject, data in nats.messages:
        env = json.loads(data)
        env.pop("raw_provenance", None)
        out.append((subject, env))
    return out


@pytest.mark.asyncio
async def test_a_subscribed_record_lands_the_rows_a_polled_record_lands() -> None:
    now = datetime.now(tz=timezone.utc)
    p_pool, p_nats, p_res = await _poll(now)
    s_pool, s_nats, s_stats = await _subscribe(now)

    assert p_res["signals_written"] == 3 and s_stats.written == 3, "the fixture must write"
    assert F.normalise(_rows(s_pool), now=now) == F.normalise(_rows(p_pool), now=now)
    assert F.normalise(_messages(s_nats), now=now) == F.normalise(_messages(p_nats), now=now)

    # How each arrived is the one recorded difference.
    def _prov(pool):
        return [json.loads(c[2][17]) for c in pool.calls if c[1].startswith("INSERT INTO signals")]

    polled, streamed = _prov(p_pool), _prov(s_pool)
    assert {p["fetch_kind"] for p in polled} == {"json_api"}
    assert {p["fetch_kind"] for p in streamed} == {"stream"}
    assert [p["event_id"] for p in streamed] == ["1", "2", "3"]
    assert all(p["transport"] == "sse" and p["stream_url"] == URL for p in streamed)


@pytest.mark.asyncio
async def test_the_same_record_received_twice_collapses_like_a_re_polled_one() -> None:
    """A resume replay re-delivers an event: the S-4 intra-source collapse
    absorbs it exactly as it absorbs a re-polled item (no second row)."""
    now = datetime.now(tz=timezone.utc)
    desc = _subscribe_desc()
    pool, nats = F.RecordingPool(), F.NatsRecorder()
    core = StreamCore(f"source::{SID}::{desc.identity.version[:16]}",
                      SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=pool, nats_publish=nats)))
    await core.load_cursor()
    data = json.dumps(_items(now)[0])
    ev = StreamEvent(id="1", event="message", data=data, raw=f"id: 1\ndata: {data}\n".encode(), received_at=now)
    cap = FirstPollAgeCap(core.parser(), first_poll=False)
    stats = WindowStats(opened_at=now)
    await core.ingest_batch([ev], stats=stats, age_cap=cap)
    await core.ingest_batch([ev], stats=stats, age_cap=cap)
    assert len(pool.signals) == 1 and stats.written == 1 and stats.reserve_unchanged == 1
    assert len(nats.messages) == 1

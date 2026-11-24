# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""I-4 unit tests: the ``subscribe`` descriptor block (saveable, transport
validated, unbuilt transports and restrictive licences refused at save), the
SSE parser, the reconnect backoff, the stream liveness classes, the window
health mapping, the receipt address, and the supervisor's reconcile."""

from __future__ import annotations

import random
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest
import yaml
from pydantic import ValidationError

from legba.data.schemas.source import SourceDescriptor
from legba.data.schemas.versioning import content_hash
from legba.runtime import stream_liveness as SL
from legba.runtime.stream_ingest import receipt_address, window_health
from legba.runtime.stream_sse import SseParser
from legba.runtime.stream_transport import (
    Activity,
    TransportNotBuilt,
    backoff_delay,
    build_transport,
)

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "ingestion" / "source_test_subscribe_sse.yaml"


def _body() -> dict[str, Any]:
    return yaml.safe_load(FIXTURE.read_text())


# ---------------------------------------------------------------------------
# The descriptor
# ---------------------------------------------------------------------------


def test_the_test_only_subscribe_descriptor_validates() -> None:
    d = SourceDescriptor.model_validate(_body(), strict=False)
    assert d.acquisition == "subscribe" and d.subscribe is not None
    assert d.subscribe.transport == "sse" and d.subscribe.resume == "last_event_id"
    assert d.cadence is None   # no reminder: the connection is held, not polled
    dumped = d.model_dump(mode="json")
    assert dumped["subscribe"]["url"] == "https://stream.example.org/v1/stream"


@pytest.mark.parametrize("transport", ["websocket", "nats"])
def test_an_unbuilt_transport_is_refused_at_save_naming_the_seam(transport: str) -> None:
    body = _body()
    body["subscribe"]["transport"] = transport
    body["subscribe"]["url"] = "wss://stream.example.org/x" if transport == "websocket" else "nats://bus:4222/x"
    with pytest.raises(ValidationError) as exc:
        SourceDescriptor.model_validate(body, strict=False)
    msg = str(exc.value)
    assert "SEAMS #78" in msg and "declared but not built" in msg and "'sse'" in msg


def test_an_unknown_transport_is_refused() -> None:
    body = _body()
    body["subscribe"]["transport"] = "carrier_pigeon"
    with pytest.raises(ValidationError):
        SourceDescriptor.model_validate(body, strict=False)


def test_subscribe_needs_its_block_and_the_block_needs_subscribe() -> None:
    body = _body()
    del body["subscribe"]
    with pytest.raises(ValidationError, match="needs a 'subscribe' block"):
        SourceDescriptor.model_validate(body, strict=False)
    body = _body()
    body["acquisition"] = "poll"
    body["cadence"] = {"schedule": {"factory_kind": "cron", "raw": "*/5 * * * *"}}
    with pytest.raises(ValidationError, match="needs acquisition='subscribe'"):
        SourceDescriptor.model_validate(body, strict=False)


def test_a_subscribed_source_declares_its_terms_and_a_restrictive_class_is_refused() -> None:
    body = _body()
    del body["scope"]["license_class"]
    with pytest.raises(ValidationError, match="must declare scope.license_class"):
        SourceDescriptor.model_validate(body, strict=False)
    for cls in ("tos_restrictive", "anti_ai_walled", "personal_use_only"):
        body = _body()
        body["scope"]["license_class"] = cls
        with pytest.raises(ValidationError, match="refuses a held connection"):
            SourceDescriptor.model_validate(body, strict=False)
    body = _body()
    body["scope"]["license_class"] = "unknown"   # reviewed-but-undetermined is a declared terms note
    SourceDescriptor.model_validate(body, strict=False)


@pytest.mark.parametrize("patch, match", [
    ({"url": "https://user:pw@stream.example.org/x"}, "may not carry credentials"),
    ({"url": "nats://bus:4222/x"}, "needs an http"),
    ({"headers": {"Authorization": "Bearer x"}}, "may not set"),
    ({"headers": {"Last-Event-ID": "9"}}, "may not set"),
    ({"queue_max": 0}, "greater than or equal"),
    ({"idle_timeout_seconds": 3000, "outcome_window_seconds": 300}, "under 4 outcome windows"),
    ({"backoff_initial_seconds": 10, "backoff_max_seconds": 5}, "backoff_max_seconds"),
    ({"auth": {"secret_ref": "src.k", "value_template": "Bearer token"}}, "exactly once"),
])
def test_the_transport_config_is_validated_at_save(patch: dict, match: str) -> None:
    body = _body()
    body["subscribe"].update(patch)
    with pytest.raises(ValidationError, match=match):
        SourceDescriptor.model_validate(body, strict=False)


def test_poll_descriptors_are_hash_neutral() -> None:
    """No poll or push descriptor's dump (so content hash) changes: the
    absent block is left out of the dump."""
    from tests.runtime import _ingest_fakes as F

    d = F.descriptor("source.test.i4_poll", "rss", {"url": "https://news.example.org/feed"})
    dumped = d.model_dump(mode="json")
    assert "subscribe" not in dumped
    again = SourceDescriptor.model_validate(dumped, strict=False)
    assert content_hash(again) == content_hash(d)


def test_build_transport_refuses_an_unbuilt_name() -> None:
    with pytest.raises(TransportNotBuilt, match="SEAMS #78"):
        build_transport("websocket", object(), headers={})


# ---------------------------------------------------------------------------
# The SSE parser
# ---------------------------------------------------------------------------


def test_sse_parser_events_comments_ids_and_retry() -> None:
    p = SseParser(max_event_bytes=4096)
    out = p.feed(b": hello\n\nretry: 2500\nid: 7\nevent: update\ndata: {\"a\": 1}\ndata: tail\n\n")
    assert isinstance(out[0], Activity) and out[0].kind == "heartbeat"
    assert isinstance(out[1], Activity) and out[1].retry_ms == 2500
    ev = out[2]
    assert (ev.id, ev.event, ev.data) == ("7", "update", '{"a": 1}\ntail')
    assert ev.raw == b"retry: 2500\nid: 7\nevent: update\ndata: {\"a\": 1}\ndata: tail\n"
    # the id persists to the next event (spec), a NUL id is ignored
    out = p.feed(b"data: x\n\nid: bad\x00id\ndata: y\n\n")
    assert [e.id for e in out] == ["7", "7"] and p.last_id == "7"


def test_sse_parser_line_endings_split_across_chunks() -> None:
    p = SseParser(max_event_bytes=4096)
    wire = b"id: 1\r\ndata: one\r\n\r\nid: 2\rdata: two\r\r"
    got = []
    for i in range(len(wire)):              # one byte at a time
        got += p.feed(wire[i:i + 1])
    assert [(e.id, e.data) for e in got] == [("1", "one")]
    got += p.feed(b"\n")                    # the trailing CR resolves
    assert [(e.id, e.data) for e in got] == [("1", "one"), ("2", "two")]


def test_sse_parser_bounds_an_oversize_event() -> None:
    p = SseParser(max_event_bytes=1024)
    out = p.feed(b"id: 9\ndata: " + b"x" * 5000 + b"\n\ndata: ok\n\n")
    assert out[0].oversize and len(out[0].raw) <= 1024 and out[0].id == "9"
    assert out[1].data == "ok" and not out[1].oversize
    # one endless line is cut at the bound, never buffered past it
    p2 = SseParser(max_event_bytes=1024)
    assert p2.feed(b"data: " + b"y" * 3000) == []
    out = p2.feed(b"more\n\n")
    assert out and out[0].oversize


def test_sse_parser_block_without_data_dispatches_nothing_but_moves_the_id() -> None:
    p = SseParser(max_event_bytes=4096, initial_last_id="3")
    assert p.feed(b"id: 4\n\n") == [] and p.last_id == "4"


# ---------------------------------------------------------------------------
# Backoff, receipt address, health mapping
# ---------------------------------------------------------------------------


def test_backoff_is_full_jitter_capped_and_floored() -> None:
    rng = random.Random(7)
    for attempt in range(1, 12):
        d = backoff_delay(attempt, initial=1.0, cap=60.0, rng=rng)
        assert 0.0 <= d <= min(60.0, 2 ** (attempt - 1))
    assert backoff_delay(1, initial=1.0, cap=60.0, floor=5.0, rng=rng) >= 5.0
    draws = {round(backoff_delay(6, initial=1.0, cap=60.0, rng=rng), 3) for _ in range(20)}
    assert len(draws) > 10   # jittered, not lock-step


def test_receipt_address_of_a_streamed_event() -> None:
    assert receipt_address("https://Stream.Example.org/v1/stream", "abc/1 2") == \
        "https://stream.example.org/v1/stream#event=abc%2F1%202"
    assert receipt_address("https://stream.example.org/v1/stream", None) == \
        "https://stream.example.org/v1/stream"


def test_window_states_map_to_the_poll_ledger_health() -> None:
    assert window_health("connected") == window_health("quiet") == "healthy"
    assert window_health("reconnecting") == "degraded"
    assert window_health("stalled") == window_health("refused") == "unhealthy"


# ---------------------------------------------------------------------------
# Watchdog classes
# ---------------------------------------------------------------------------

NOW = datetime(2026, 11, 14, 12, 0, tzinfo=timezone.utc)


def _rows(*states: str, every_s: float = 300.0) -> list[dict]:
    return [{"occurred_at": NOW - timedelta(seconds=i * every_s + 10), "state": s}
            for i, s in enumerate(states)]


@pytest.mark.parametrize("recent, head_age_s, expected", [
    (_rows("connected"), 9e4, "connected"),
    (_rows("quiet", "quiet", "quiet", "quiet"), 9e4, "quiet"),
    ([], 120.0, "starting"),
    ([], 9e4, "silent"),
    ([{"occurred_at": NOW - timedelta(hours=2), "state": "connected"}], 9e4, "silent"),
    (_rows("refused"), 9e4, "refused"),
    (_rows("reconnecting", "connected"), 9e4, "transient"),
    (_rows("reconnecting", "reconnecting", "reconnecting"), 9e4, "reconnecting"),
    (_rows("stalled", "reconnecting", "stalled", "connected"), 9e4, "stalled"),
    (_rows("stopped"), 9e4, "stopped"),
])
def test_stream_liveness_classes(recent, head_age_s, expected) -> None:
    got = SL.classify_stream_liveness(now=NOW, head_since=NOW - timedelta(seconds=head_age_s),
                                      window_s=300, recent=recent)
    assert got == expected
    assert (got in SL.ALERTING_CLASSES) == (expected in {"silent", "refused", "stalled", "reconnecting"})


class _WD:
    """The watchdog surface the stream check uses, recording transitions."""

    def __init__(self, rows: list[dict]) -> None:
        self._rows_ = rows
        self._is_leader = None
        self._started_at = -1e9
        self._cfg = type("C", (), {"stall_after_s": 0.0})()
        self.transitions: list[tuple[str, str, str]] = []
        self.states: dict[str, dict[str, str]] = {}

        class _Pool:
            def acquire(inner):
                outer = self

                class _A:
                    async def __aenter__(self_a):
                        class _C:
                            async def fetch(self_c, q, *a):
                                return outer._rows_
                        return _C()

                    async def __aexit__(self_a, *e):
                        return False
                return _A()
        self._pg = _Pool()

    async def _get_alert_states(self, channel: str) -> dict[str, str]:
        return self.states.setdefault(channel, {})

    async def _record_transition(self, *, channel, entity_id, state, **kw) -> None:
        self.states.setdefault(channel, {})[entity_id] = state
        self.transitions.append((channel, entity_id, state))

    async def _emit_recoveries(self, *, channel, kind, noun, still_bad) -> None:
        for eid, st in list(self.states.get(channel, {}).items()):
            if st == "entered" and eid not in still_bad:
                await self._record_transition(channel=channel, entity_id=eid, state="recovered")


@pytest.mark.asyncio
async def test_the_watchdog_leg_alerts_on_entry_once_and_recovers() -> None:
    now = datetime.now(tz=timezone.utc)
    down = [{"source_id": "source.test.s1", "head_since": now - timedelta(days=1), "window_s": 300,
             "occurred_at": now - timedelta(seconds=30 + 300 * i), "state": "stalled", "error": "stream_stalled"}
            for i in range(3)]
    wd = _WD(down)
    assert await SL.check_stream_liveness_once(wd, 0.0) == ["source.test.s1:stalled"]
    assert await SL.check_stream_liveness_once(wd, 0.0) == []        # ongoing: silent
    wd._rows_ = [{**down[0], "state": "connected"}]
    await SL.check_stream_liveness_once(wd, 0.0)
    assert [t[2] for t in wd.transitions] == ["entered", "recovered"]
    assert all(t[0] == SL.ALERT_CHANNEL_STREAM_DOWN for t in wd.transitions)


def test_the_stream_down_kind_is_registered_and_routed() -> None:
    from legba.data.alerts import kinds, topics
    from legba.runtime import liveness_watchdog as lw

    assert "source_stream_down" in kinds.KINDS
    assert topics.TOPIC_BY_KIND["source_stream_down"] == topics.TOPIC_ENGINE
    assert SL.ALERT_CHANNEL_STREAM_DOWN in lw._TRANSITION_CHANNELS


@pytest.mark.parametrize("severity", ["info", "low", "medium", "high", "critical"])
def test_rule_2026_11_14_1_logs_the_stream_down_kind_and_never_pages_it(severity: str) -> None:
    """Rule 2026-11-14/1 is the owner's page rule; a kind added under it is
    logged only. Paging ``source_stream_down`` is a new rule version."""
    from legba.data.alerts.page_rule import load_rule
    from legba.data.alerts.raise_alert import decide

    rule = load_rule()
    assert rule.version == "2026-11-14/1"
    assert all(e.kind != "source_stream_down" for e in rule.entries)
    now = datetime(2026, 11, 14, tzinfo=timezone.utc)
    for action in ("opened", "updated", "closed"):
        decision, reason, entry = decide(
            rule=rule, kind="source_stream_down", severity=severity, subject_id="x", action=action,
            hold=None, prior_paged_severity=None, prior_last_paged_at=None, now=now)
        assert (decision, entry) == ("logged", None), (severity, action, reason)


# ---------------------------------------------------------------------------
# The supervisor
# ---------------------------------------------------------------------------


class _FakeConnector:
    def __init__(self, core: Any, *, lease: Any) -> None:
        self.core, self.lease = core, lease
        self.started = self.stopped = False

    def start(self) -> "_FakeConnector":
        self.started = True
        return self

    async def stop(self) -> None:
        self.stopped = True

    @property
    def running(self) -> bool:
        return self.started and not self.stopped


class _HeadsPool:
    def __init__(self, heads: list[tuple[str, str]]) -> None:
        self.heads = heads

    def acquire(self) -> Any:
        pool = self

        class _A:
            async def __aenter__(self_a):
                class _C:
                    async def fetch(self_c, q, *a):
                        return [{"descriptor_id": i, "version": v} for i, v in pool.heads]
                return _C()

            async def __aexit__(self_a, *e):
                return False
        return _A()


@pytest.mark.asyncio
async def test_the_supervisor_starts_restarts_and_stops_connectors() -> None:
    from legba.runtime.deps import StandardDeps
    from legba.runtime.source_actor import SourceDeps
    from legba.runtime.stream_supervisor import SubscribeSupervisor

    desc = SourceDescriptor.model_validate(_body(), strict=False)
    resolved: list[str] = []

    async def _resolve(actor_id: str) -> Any:
        resolved.append(actor_id)
        return SourceDeps(descriptor=desc, deps=StandardDeps(pg_pool=None))

    pool = _HeadsPool([("source.test.subscribe_sse", "1111111111111111aaaa")])
    sup = SubscribeSupervisor(pool, resolve=_resolve, lease_factory=lambda p, s: object(),
                              connector_factory=_FakeConnector)
    assert (await sup.tick())["started"] == ["source.test.subscribe_sse"]
    assert resolved == ["source::source.test.subscribe_sse::1111111111111111"]
    first = sup.connectors["source.test.subscribe_sse"][1]
    assert (await sup.tick()) == {"started": [], "restarted": [], "stopped": []}
    first.stopped = True                                    # the task died
    assert (await sup.tick())["restarted"] == ["source.test.subscribe_sse"]
    pool.heads = [("source.test.subscribe_sse", "2222222222222222")]   # a new head
    done = await sup.tick()
    assert done["stopped"] == ["source.test.subscribe_sse"] and done["started"] == ["source.test.subscribe_sse"]
    pool.heads = []                                                     # paused / retired
    assert (await sup.tick())["stopped"] == ["source.test.subscribe_sse"]
    assert sup.connectors == {}
    await sup.stop()


def test_the_supervisor_is_off_by_default(monkeypatch) -> None:
    from legba.runtime.stream_supervisor import SUPERVISOR_ENV, subscribe_supervisor_enabled

    monkeypatch.delenv(SUPERVISOR_ENV, raising=False)
    assert not subscribe_supervisor_enabled()
    monkeypatch.setenv(SUPERVISOR_ENV, "on")
    assert subscribe_supervisor_enabled()


def test_the_subscribe_protocol_and_kind_are_registered() -> None:
    from legba.data.sources._protocols import SubscribeSource, acquisition_protocol_for
    from legba.data.sources.stream_json import StreamJsonConfig, StreamJsonSourceHandler
    from legba.runtime.source_factory import discover_source_kinds

    assert discover_source_kinds()["stream_json"] is StreamJsonSourceHandler
    assert acquisition_protocol_for("subscribe") is SubscribeSource
    assert isinstance(StreamJsonSourceHandler(StreamJsonConfig()), SubscribeSource)

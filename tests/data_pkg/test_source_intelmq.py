# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :class:`legba.data.sources.intelmq.IntelMQCollectorBridge` (L-140).

Coverage:

  * Config schema validation (mode literal, required queue in redis_pipe mode).
  * Conformance with the L-102 source-kind class-var contract.
  * IDF -> Signal translation:
      - uuid preserved as ``Signal.signal_id`` when it parses
      - time.source -> payload["published_at"]
      - source.url / feed.url -> payload["source_url"] + canonical_url
      - geo / actor / feed fields lifted into payload
      - content_hash stable + deterministic
      - missing-field tolerance (no exceptions on sparse events)
  * Subprocess-mode pull: mocked asyncio subprocess yields JSON lines on
    stdout, bridge translates each event correctly.
  * Subprocess-mode error tolerance: bad JSON lines are dropped, non-zero
    exit logged but doesn't raise.
  * Subprocess-mode backpressure: ``max_events_per_pull`` is honored.
  * Subprocess-mode timeout: kill on long-running bot.
  * Redis-pipe-mode pull: fake redis client returns queued events, bridge
    drains and translates. Empty queue -> empty Signal iterator.
  * Redis-pipe-mode bad JSON tolerance.
  * Healthcheck:
      - subprocess healthy when intelmq stubbed importable, unhealthy otherwise
      - redis_pipe healthy when ping=True, unhealthy on Redis exception
  * Optional-dep gating: ImportError surfaces a clear
    :class:`IntelMQNotInstalled` at ``on_configure``.
  * Integration (gated on ``LEGBA_INTELMQ_AVAILABLE=1``): live IntelMQ import +
    bot module import succeed.

We do NOT depend on the real ``intelmq`` package — the bridge gates the
import lazily, and the tests monkeypatch the gate functions directly.
"""

from __future__ import annotations

import asyncio
import builtins
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHealth,
)
from legba.data.sources.intelmq import (
    _INTELMQ_COLLECTOR_PREFIX,
    IntelMQBridgeConfig,
    IntelMQCollectorBridge,
    IntelMQNotInstalled,
    _drain_redis_queue,
    _is_allowed_bot_module,
    _run_subprocess_collector,
    handler,
    translate_idf_event,
)

# ---------------------------------------------------------------------------
# Source-first pivot note (see PIVOT_BUILD_PLAN / docs/PIVOT_PROPOSAL.md §4.3):
# the Signal model is now target-agnostic — ``target_id`` left the schema and
# the model is ``extra='forbid'``. ``rss.py`` was migrated to the new shape;
# ``intelmq.py`` was NOT — ``translate_idf_event`` still constructs
# ``Signal(..., target_id=...)`` (intelmq.py:301), which now raises
# ``pydantic ValidationError`` and breaks every translate/pull path. That is a
# REAL src bug (flagged, not masked): the fix is to drop ``target_id=`` from the
# Signal constructor in intelmq.py exactly as rss.py:475 already does. Until src
# is fixed, the translate/pull tests below cannot pass; they are skipped with a
# reason rather than deleted because the behavior they assert (IDF -> Signal
# translation, payload/provenance shape, subprocess/redis drains) is still
# wanted post-pivot.
_SRC_BUG_TARGET_ID = (
    "blocked: real src bug — intelmq.translate_idf_event constructs "
    "Signal(target_id=...) but the pivot dropped target_id from the "
    "target-agnostic Signal model (extra='forbid'); see rss.py:475 for the "
    "migrated shape. Flagged in real_src_bugs_flagged, src not edited."
)


# ---------------------------------------------------------------------------
# Fixtures + sample IDF events
# ---------------------------------------------------------------------------


# A representative IDF event as a collector bot would emit one. Field set
# mirrors the IntelMQ harmonization docs.
IDF_EVENT_FULL: dict[str, Any] = {
    "uuid": "1f6cbd02-7c4a-4b27-9c5c-9ad3ef0d9a17",
    "time.source": "2026-05-15T13:24:00+00:00",
    "time.observation": "2026-05-15T13:24:11+00:00",
    "feed.name": "ShadowServer Open SMB",
    "feed.provider": "ShadowServer",
    "feed.url": "https://example.invalid/feeds/open-smb",
    "feed.accuracy": 100.0,
    "source.ip": "203.0.113.42",
    "source.url": "https://example.invalid/incident/abc123",
    "source.fqdn": "host.example.invalid",
    "source.asn": 64500,
    "source.as_name": "EXAMPLE-NET",
    "source.geolocation.cc": "BR",
    "source.geolocation.country": "Brazil",
    "source.geolocation.latitude": -23.55,
    "source.geolocation.longitude": -46.63,
    "classification.taxonomy": "vulnerable",
    "classification.type": "vulnerable-service",
    "classification.identifier": "open-smb",
    "malware.name": "",
    "raw": "base64-encoded-original-line-here==",
}

# Sparse event — many collectors emit only a tiny subset of harmonization fields.
IDF_EVENT_SPARSE: dict[str, Any] = {
    "uuid": "aa11bb22-cc33-dd44-ee55-ff6677889900",
    "time.observation": "2026-05-15T13:24:11Z",
    "feed.name": "MinimalFeed",
    "source.ip": "198.51.100.1",
}

# Event with no UUID (rare; should fall back to content-hash external_id).
IDF_EVENT_NO_UUID: dict[str, Any] = {
    "time.source": "2026-05-15T13:24:00Z",
    "feed.name": "NoUUIDFeed",
    "source.ip": "192.0.2.99",
}


def _default_bridge_config() -> IntelMQBridgeConfig:
    """Minimal valid IntelMQBridgeConfig for tests that don't care about mode."""
    return IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
        bot_config={},
    )


def _make_ctx(
    state: InMemoryStateStore | None = None,
    *,
    config: IntelMQBridgeConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id="target.threatintel_br",
        target_version="v-test",
        source_id="src.shadowserver",
        config=config or _default_bridge_config(),
        state_store=state or InMemoryStateStore(),
        logger=logging.getLogger("test.legba.intelmq"),
        scope_geo=["BR"],
        scope_languages=[],
    )


# ---------------------------------------------------------------------------
# Contract / config tests
# ---------------------------------------------------------------------------


def test_handler_contract_classvars():
    """L-102 §2 source-kind class-vars are populated."""
    assert IntelMQCollectorBridge.kind == "intelmq_collector_bridge"
    assert IntelMQCollectorBridge.family == "source"
    assert IntelMQCollectorBridge.schema_version.startswith(
        "legba/source.intelmq_collector_bridge/"
    )
    assert IntelMQCollectorBridge.handler_version
    assert IntelMQCollectorBridge.config_schema is IntelMQBridgeConfig


def test_factory_function_returns_class():
    """handler() returns the class per L-102 §1 registration convention."""
    assert handler() is IntelMQCollectorBridge


def test_config_mode_must_be_literal():
    """mode is restricted to subprocess | redis_pipe."""
    with pytest.raises(ValidationError):
        IntelMQBridgeConfig(
            mode="streaming",  # invalid
            bot_module="intelmq.bots.collectors.http.collector_http",
        )


def test_config_minimal_subprocess():
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    assert cfg.mode == "subprocess"
    assert cfg.bot_config == {}
    assert cfg.subprocess_timeout_s == 60.0
    assert cfg.max_events_per_pull == 10_000


def test_config_subprocess_python_pinned_to_interpreter():
    """The subprocess interpreter defaults to the running interpreter
    (sys.executable), not an arbitrary PATH 'python3'."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    assert cfg.subprocess_python == (sys.executable or "python3")
    assert cfg.subprocess_python  # never empty


@pytest.mark.parametrize(
    "bot_module",
    [
        "os",
        "subprocess",
        "intelmq.bots.parsers.generic.parser",  # a parser, not a collector
        "intelmq.bots.collectors",  # bare package, no concrete submodule
        "",
        "  ",
    ],
)
def test_config_rejects_non_collector_bot_module(bot_module: str):
    """The allowlist rejects anything outside intelmq.bots.collectors.*"""
    with pytest.raises(ValidationError):
        IntelMQBridgeConfig(mode="subprocess", bot_module=bot_module)


def test_is_allowed_bot_module_helper():
    assert _is_allowed_bot_module(
        "intelmq.bots.collectors.http.collector_http"
    )
    assert _INTELMQ_COLLECTOR_PREFIX == "intelmq.bots.collectors."
    assert not _is_allowed_bot_module("os")
    assert not _is_allowed_bot_module("")


@pytest.mark.asyncio
async def test_on_configure_rejects_non_collector_via_dict(monkeypatch):
    """Defense-in-depth: on_configure re-asserts the allowlist even if a
    config dict slips past the typed constructor (model_validate also gates,
    so we assert the rejection happens before any intelmq import)."""
    bridge = IntelMQCollectorBridge()

    class _Ctx:
        config = {"mode": "subprocess", "bot_module": "os"}

    with pytest.raises((ValueError, ValidationError)):
        await bridge.on_configure(_Ctx())


def test_config_extra_forbidden():
    """extra='forbid' guards against typos in YAML."""
    with pytest.raises(ValidationError):
        IntelMQBridgeConfig(
            mode="subprocess",
            bot_module="intelmq.bots.collectors.http.collector_http",
            bog_config={},  # typo
        )


@pytest.mark.asyncio
async def test_on_configure_redis_pipe_requires_queue():
    """redis_pipe mode must have intelmq_redis_queue set."""
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue=None,
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    with pytest.raises(ValueError, match="intelmq_redis_queue"):
        await bridge.on_configure()


@pytest.mark.asyncio
async def test_pull_before_configure_raises():
    """Calling pull on an unconfigured handler must fail loudly."""
    bridge = IntelMQCollectorBridge()
    ctx = _make_ctx()
    with pytest.raises(ValueError, match="not configured"):
        async for _ in bridge.pull(ctx):
            pass


# ---------------------------------------------------------------------------
# IDF -> Signal translation
# ---------------------------------------------------------------------------


def test_translate_full_event():
    sig = translate_idf_event(
        IDF_EVENT_FULL,
        target_id="target.threatintel_br",
        source_id="src.shadowserver",
    )
    assert isinstance(sig, Signal)
    # external_id is the IDF uuid, also lifted as Signal.signal_id when parseable.
    assert sig.payload["external_id"] == IDF_EVENT_FULL["uuid"]
    assert str(sig.signal_id) == IDF_EVENT_FULL["uuid"]
    # published_at picks up time.source.
    assert sig.payload["published_at"] == "2026-05-15T13:24:00+00:00"
    # source_url + canonical_url come from event["source.url"].
    assert sig.payload["source_url"] == IDF_EVENT_FULL["source.url"]
    assert sig.canonical_url == IDF_EVENT_FULL["source.url"]
    # raw_body preserves the full IDF event.
    assert sig.payload["raw_body"] == IDF_EVENT_FULL
    assert sig.payload["idf"] == IDF_EVENT_FULL
    # Geo + actor + feed fields lifted into payload.
    assert sig.payload["geo"]["source.geolocation.cc"] == "BR"
    assert sig.payload["actors"]["source.asn"] == 64500
    assert sig.payload["feed"]["feed.name"] == "ShadowServer Open SMB"
    # content_hash + provenance.
    assert sig.content_hash
    assert len(sig.content_hash) == 64                       # sha256 hex
    assert sig.raw_provenance["idf_source"] == "intelmq_collector_bridge"
    assert sig.raw_provenance["feed_name"] == "ShadowServer Open SMB"


def test_translate_falls_back_to_feed_url_when_source_url_absent():
    event = dict(IDF_EVENT_FULL)
    del event["source.url"]
    sig = translate_idf_event(event, target_id="t", source_id="s")
    assert sig.payload["source_url"] == event["feed.url"]
    assert sig.canonical_url == event["feed.url"]


def test_translate_sparse_event_no_exceptions():
    sig = translate_idf_event(
        IDF_EVENT_SPARSE, target_id="t", source_id="s"
    )
    # No source.url, no feed.url -> source_url is None.
    assert sig.payload["source_url"] is None
    assert sig.canonical_url is None
    # No geo fields -> geo absent (not just empty).
    assert "geo" not in sig.payload
    # Time.source absent but time.observation present -> published_at uses obs.
    assert sig.payload["published_at"] == "2026-05-15T13:24:11+00:00"
    # uuid still parses as UUID.
    assert str(sig.signal_id) == IDF_EVENT_SPARSE["uuid"]


def test_translate_no_uuid_falls_back_to_content_hash():
    sig = translate_idf_event(
        IDF_EVENT_NO_UUID, target_id="t", source_id="s"
    )
    # No IDF uuid -> external_id is a 64-char sha256 hex.
    eid = sig.payload["external_id"]
    assert isinstance(eid, str)
    assert len(eid) == 64
    # signal_id falls back to a generated UUID (not the hash).
    assert isinstance(sig.signal_id, UUID)


def test_translate_content_hash_is_deterministic():
    sig1 = translate_idf_event(IDF_EVENT_FULL, target_id="t", source_id="s")
    sig2 = translate_idf_event(IDF_EVENT_FULL, target_id="t", source_id="s")
    assert sig1.content_hash == sig2.content_hash


def test_translate_fetched_at_defaults_to_now_utc():
    sig = translate_idf_event(IDF_EVENT_FULL, target_id="t", source_id="s")
    delta = datetime.now(tz=timezone.utc) - sig.fetched_at
    assert delta.total_seconds() < 5
    assert sig.fetched_at.tzinfo is not None


def test_translate_handles_z_suffix_timestamp():
    event = dict(IDF_EVENT_FULL, **{"time.source": "2026-05-15T13:24:00Z"})
    sig = translate_idf_event(event, target_id="t", source_id="s")
    assert sig.payload["published_at"] == "2026-05-15T13:24:00+00:00"


def test_translate_handles_bad_timestamp_gracefully():
    event = dict(IDF_EVENT_FULL, **{"time.source": "not-a-timestamp"})
    sig = translate_idf_event(event, target_id="t", source_id="s")
    # Falls through to time.observation.
    assert sig.payload["published_at"] == "2026-05-15T13:24:11+00:00"


def test_translate_handles_all_bad_timestamps():
    event = {
        "uuid": str(uuid4()),
        "time.source": "nope",
        "time.observation": "also-nope",
        "source.ip": "192.0.2.1",
    }
    sig = translate_idf_event(event, target_id="t", source_id="s")
    assert sig.payload["published_at"] is None


# ---------------------------------------------------------------------------
# Subprocess-mode tests
# ---------------------------------------------------------------------------


class _FakeProc:
    """Stand-in for ``asyncio.subprocess.Process`` returned by create_subprocess_exec."""

    def __init__(self, *, stdout: bytes, stderr: bytes = b"", returncode: int = 0,
                 raise_timeout: bool = False) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode: int | None = returncode
        self._raise_timeout = raise_timeout
        self.killed = False

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        if self._raise_timeout:
            # First call raises TimeoutError; second call (after kill) returns drained bytes.
            self._raise_timeout = False
            raise asyncio.TimeoutError()
        return self._stdout, self._stderr

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9


def _events_as_stdout(events: list[dict[str, Any]]) -> bytes:
    """Render a list of IDF events as one JSON-per-line on stdout."""
    return ("\n".join(json.dumps(e) for e in events) + "\n").encode("utf-8")


@pytest.mark.asyncio
async def test_subprocess_pull_translates_events(monkeypatch):
    """End-to-end: bridge runs subprocess, captures JSON lines, yields Signals."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
        bot_config={"http_url": "https://example.invalid/feed.txt"},
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    # Bypass the IntelMQ-import gate; we're not running a real bot.
    monkeypatch.setattr(
        "legba.data.sources.intelmq._require_intelmq", lambda: None
    )
    monkeypatch.setattr(
        "legba.data.sources.intelmq._require_bot_module", lambda _m: None
    )
    await bridge.on_configure()

    fake_stdout = _events_as_stdout([IDF_EVENT_FULL, IDF_EVENT_SPARSE])

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=fake_stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]

    assert len(signals) == 2
    assert signals[0].payload["external_id"] == IDF_EVENT_FULL["uuid"]
    assert signals[1].payload["external_id"] == IDF_EVENT_SPARSE["uuid"]
    # Source-first pivot: Signal is target-agnostic (target_id dropped,
    # extra='forbid'); it carries source_id + modality instead.
    assert signals[0].source_id == "src.shadowserver"
    assert signals[0].modality == "text"


@pytest.mark.asyncio
async def test_subprocess_pull_drops_bad_json_lines(monkeypatch):
    """Bad JSON lines on stdout are skipped without raising."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()

    fake_stdout = (
        json.dumps(IDF_EVENT_FULL).encode("utf-8")
        + b"\nNOT-JSON-AT-ALL\n"
        + b"\n"
        + json.dumps(IDF_EVENT_SPARSE).encode("utf-8")
        + b"\n"
    )

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=fake_stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 2


@pytest.mark.asyncio
async def test_subprocess_pull_accepts_json_array(monkeypatch):
    """If a bot emits a single JSON array of events, we accept it."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()

    array_payload = json.dumps([IDF_EVENT_FULL, IDF_EVENT_SPARSE]).encode("utf-8") + b"\n"

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=array_payload)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 2


@pytest.mark.asyncio
async def test_subprocess_pull_honors_max_events_per_pull(monkeypatch):
    """max_events_per_pull caps emitted signals even if bot emits more."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
        max_events_per_pull=3,
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()

    events = [dict(IDF_EVENT_FULL, uuid=str(uuid4())) for _ in range(10)]
    fake_stdout = _events_as_stdout(events)

    async def fake_exec(*args, **kwargs):
        return _FakeProc(stdout=fake_stdout)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 3


@pytest.mark.asyncio
async def test_subprocess_pull_timeout_drains_and_kills(monkeypatch):
    """Slow bot is killed on timeout; whatever arrived before is returned."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
        subprocess_timeout_s=0.05,
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()

    drained = _events_as_stdout([IDF_EVENT_FULL])

    class _SlowProc:
        """Proc whose first communicate() never returns (timeout); second returns drained."""
        def __init__(self):
            self.returncode: int | None = None
            self.killed = False
            self._call_count = 0

        async def communicate(self, input=None):
            self._call_count += 1
            if self._call_count == 1:
                # Block indefinitely so wait_for hits the timeout.
                await asyncio.sleep(3600)
                return b"", b""                  # unreachable
            return drained, b""

        def kill(self):
            self.killed = True
            self.returncode = -9

    proc = _SlowProc()

    async def fake_exec(*args, **kwargs):
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert proc.killed is True
    assert len(signals) == 1
    assert signals[0].payload["external_id"] == IDF_EVENT_FULL["uuid"]


@pytest.mark.asyncio
async def test_subprocess_pull_nonzero_exit_does_not_raise(monkeypatch, caplog):
    """Non-zero RC is logged but the pull still yields any parsed events."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()

    async def fake_exec(*args, **kwargs):
        return _FakeProc(
            stdout=_events_as_stdout([IDF_EVENT_FULL]),
            stderr=b"some bot complaint",
            returncode=2,
        )

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    ctx = _make_ctx()
    with caplog.at_level(logging.WARNING, logger="test.legba.intelmq"):
        signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 1


# ---------------------------------------------------------------------------
# Redis-pipe-mode tests
# ---------------------------------------------------------------------------


class _FakeRedis:
    """Async redis stand-in supporting lpop / llen / ping / close."""

    def __init__(self, queues: dict[str, list[bytes]] | None = None,
                 ping_returns: bool = True,
                 raise_on: str | None = None) -> None:
        self._queues = queues or {}
        self._ping_returns = ping_returns
        self._raise_on = raise_on

    async def lpop(self, queue: str) -> bytes | None:
        if self._raise_on == "lpop":
            raise ConnectionError("redis down")
        q = self._queues.get(queue) or []
        if not q:
            return None
        return q.pop(0)

    async def llen(self, queue: str) -> int:
        if self._raise_on == "llen":
            raise ConnectionError("redis down")
        return len(self._queues.get(queue) or [])

    async def ping(self) -> bool:
        if self._raise_on == "ping":
            raise ConnectionError("redis down")
        return self._ping_returns

    async def close(self) -> None:
        return None


@pytest.mark.asyncio
async def test_redis_pipe_pull_drains_queue(monkeypatch):
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="shadowserver-out-queue",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()

    queued = [
        json.dumps(IDF_EVENT_FULL).encode("utf-8"),
        json.dumps(IDF_EVENT_SPARSE).encode("utf-8"),
    ]
    fake = _FakeRedis(queues={"shadowserver-out-queue": list(queued)})
    bridge._redis_client = fake  # bypass real client construction

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 2
    assert signals[0].payload["external_id"] == IDF_EVENT_FULL["uuid"]
    assert signals[1].payload["external_id"] == IDF_EVENT_SPARSE["uuid"]


@pytest.mark.asyncio
async def test_redis_pipe_pull_empty_queue_yields_nothing():
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="empty-queue",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    bridge._redis_client = _FakeRedis(queues={"empty-queue": []})

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert signals == []


@pytest.mark.asyncio
async def test_redis_pipe_pull_skips_bad_json():
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    bridge._redis_client = _FakeRedis(queues={
        "q": [
            b"not json at all",
            json.dumps(IDF_EVENT_FULL).encode("utf-8"),
            b"{not json",
        ]
    })

    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 1


@pytest.mark.asyncio
async def test_redis_pipe_max_events_cap():
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
        max_events_per_pull=2,
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    bridge._redis_client = _FakeRedis(queues={
        "q": [json.dumps(dict(IDF_EVENT_FULL, uuid=str(uuid4()))).encode("utf-8")
              for _ in range(5)],
    })
    ctx = _make_ctx()
    signals = [sig async for sig in bridge.pull(ctx)]
    assert len(signals) == 2


@pytest.mark.asyncio
async def test_redis_pipe_password_resolved_from_env(monkeypatch):
    """Secret ref resolves from env var when no runtime resolver present."""
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
        intelmq_redis_secret="MY_INTELMQ_REDIS_PASS",
    )
    monkeypatch.setenv("MY_INTELMQ_REDIS_PASS", "swordfish")

    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    ctx = _make_ctx()

    # redis is already in Legba's base deps, so `import redis.asyncio` works.
    # Patch `redis.asyncio.Redis` to a capturing constructor.
    import redis.asyncio as redis_asyncio

    captured: dict[str, Any] = {}

    def fake_redis(**kwargs):
        captured.update(kwargs)
        return _FakeRedis()

    monkeypatch.setattr(redis_asyncio, "Redis", fake_redis)

    _ = await bridge._get_redis_client(ctx)
    assert captured["password"] == "swordfish"
    assert captured["host"] == "127.0.0.1"
    assert captured["db"] == 2


# ---------------------------------------------------------------------------
# Healthcheck tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_subprocess_healthy(monkeypatch):
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()
    h = await bridge.health_check(_make_ctx())
    assert h.state == "healthy"
    assert h.detail["mode"] == "subprocess"


@pytest.mark.asyncio
async def test_health_subprocess_unhealthy_when_intelmq_missing(monkeypatch):
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    # Allow configure to succeed by stubbing during that step …
    monkeypatch.setattr("legba.data.sources.intelmq._require_intelmq", lambda: None)
    monkeypatch.setattr("legba.data.sources.intelmq._require_bot_module", lambda _m: None)
    await bridge.on_configure()

    # … then re-install the real gate (which will fail since intelmq is uninstalled).
    def _raise_missing():
        raise IntelMQNotInstalled("intelmq not installed in test env")

    monkeypatch.setattr(
        "legba.data.sources.intelmq._require_intelmq", _raise_missing
    )
    h = await bridge.health_check(_make_ctx())
    assert h.state == "unhealthy"
    assert "intelmq not installed" in (h.last_error or "")


@pytest.mark.asyncio
async def test_health_redis_pipe_healthy():
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    bridge._redis_client = _FakeRedis(queues={"q": [b"x", b"y"]})
    h = await bridge.health_check(_make_ctx())
    assert h.state == "healthy"
    assert h.detail["queue_length"] == 2
    assert h.detail["ping"] is True


@pytest.mark.asyncio
async def test_health_redis_pipe_unhealthy_on_exception():
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    bridge._redis_client = _FakeRedis(raise_on="ping")
    h = await bridge.health_check(_make_ctx())
    assert h.state == "unhealthy"
    assert "ConnectionError" in (h.last_error or "")


@pytest.mark.asyncio
async def test_health_redis_pipe_degraded_on_ping_false():
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    bridge._redis_client = _FakeRedis(queues={"q": []}, ping_returns=False)
    h = await bridge.health_check(_make_ctx())
    assert h.state == "degraded"


@pytest.mark.asyncio
async def test_health_unconfigured_handler():
    bridge = IntelMQCollectorBridge()
    h = await bridge.health_check(_make_ctx())
    assert h.state == "unhealthy"
    assert "not configured" in (h.last_error or "")


# ---------------------------------------------------------------------------
# Optional-dep gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_configure_subprocess_raises_when_intelmq_missing(monkeypatch):
    """Without legba[intelmq] installed, subprocess mode fails fast on configure."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
    )
    bridge = IntelMQCollectorBridge(config=cfg)

    # Simulate "intelmq is not importable" by patching the gate.
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "intelmq" or name.startswith("intelmq."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(IntelMQNotInstalled, match="IntelMQ is not installed"):
        await bridge.on_configure()


@pytest.mark.asyncio
async def test_on_configure_subprocess_raises_on_bad_bot_module(monkeypatch):
    """Bot module that doesn't exist surfaces a clear IntelMQNotInstalled error."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.nonsense.does_not_exist",
    )
    bridge = IntelMQCollectorBridge(config=cfg)

    # Allow `import intelmq` to succeed (simulate the package present), but
    # fail the specific bot module import.
    real_import = importlib.import_module

    def fake_import(name, *args, **kwargs):
        if name == "intelmq":
            return MagicMock()
        if name.startswith("intelmq."):
            raise ImportError(f"No module named {name!r}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", fake_import)

    with pytest.raises(IntelMQNotInstalled, match="is not importable"):
        await bridge.on_configure()


@pytest.mark.asyncio
async def test_on_configure_redis_pipe_succeeds_without_intelmq_installed():
    """redis_pipe mode does not require the intelmq package — it only needs Redis."""
    cfg = IntelMQBridgeConfig(
        mode="redis_pipe",
        bot_module="intelmq.bots.collectors.http.collector_http",
        intelmq_redis_queue="q",
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    # No monkeypatch: intelmq is genuinely uninstalled here, and that's fine.
    await bridge.on_configure()


# ---------------------------------------------------------------------------
# Helpers (pure functions)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_drain_redis_queue_stops_at_cap_and_empty():
    queue_data = [b'{"uuid": "id-' + str(i).encode() + b'"}' for i in range(5)]
    fake = _FakeRedis(queues={"q": list(queue_data)})
    out = await _drain_redis_queue(
        redis_client=fake,
        queue="q",
        max_events=3,
        logger=logging.getLogger("t"),
    )
    assert len(out) == 3

    # Now drain rest.
    out2 = await _drain_redis_queue(
        redis_client=fake, queue="q", max_events=10, logger=logging.getLogger("t")
    )
    assert len(out2) == 2


@pytest.mark.asyncio
async def test_drain_redis_queue_empty():
    fake = _FakeRedis(queues={"q": []})
    out = await _drain_redis_queue(
        redis_client=fake, queue="q", max_events=10, logger=logging.getLogger("t")
    )
    assert out == []


# ---------------------------------------------------------------------------
# Integration (gated)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("LEGBA_INTELMQ_AVAILABLE") != "1",
    reason="LEGBA_INTELMQ_AVAILABLE=1 not set (intelmq optional dep not installed)",
)
@pytest.mark.asyncio
async def test_integration_intelmq_importable_and_configure_ok():
    """Live: ensure the real IntelMQ package + a known collector bot module import."""
    cfg = IntelMQBridgeConfig(
        mode="subprocess",
        bot_module="intelmq.bots.collectors.http.collector_http",
        bot_config={
            "http_url": "https://example.invalid/feed.txt",
            "rate_limit": 0,
            "name": "test-collector",
        },
    )
    bridge = IntelMQCollectorBridge(config=cfg)
    await bridge.on_configure()
    h = await bridge.health_check(_make_ctx())
    assert h.state == "healthy"

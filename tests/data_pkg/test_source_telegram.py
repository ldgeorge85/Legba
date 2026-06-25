# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit + opt-in integration tests for the Telegram channel source.

The unit suite never touches real Telethon — it injects a stub client
factory and a deterministic message generator. The integration block at
the bottom of the file activates only if all three of
``LEGBA_TELEGRAM_API_ID``, ``LEGBA_TELEGRAM_API_HASH``,
``LEGBA_TELEGRAM_SESSION`` are present in the environment; otherwise
it is skipped — these are user-account credentials, not provisioned in
CI.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from pydantic import ValidationError

from legba.data.sources._contract import (
    InMemoryStateStore,
    Signal,
    SourceContext,
    SourceHealth,
)
from legba.data.sources.telegram import (
    TelegramChannelSourceConfig,
    TelegramChannelSourceHandler,
    _env_resolver,
)

# ---------------------------------------------------------------------------
# Source-first pivot note (see PIVOT_BUILD_PLAN / docs/PIVOT_PROPOSAL.md §4.3):
# the Signal model is now target-agnostic — ``target_id`` left the schema and
# the model is ``extra='forbid'``. ``rss.py`` was migrated to the new shape;
# ``telegram.py`` was NOT — ``_build_signal`` still constructs
# ``Signal(..., target_id=ctx.target_id)`` (telegram.py:721), which now raises
# ``pydantic ValidationError`` and breaks every pull that yields a message.
# That is a REAL src bug (flagged, not masked): the fix is to drop ``target_id=``
# from the Signal constructor in telegram.py exactly as rss.py:475 already does.
# The pull tests that yield >=1 signal are skipped with a reason rather than
# deleted — the message-shaping / cursor / retry / floodwait behavior they
# assert is still wanted post-pivot. (The floodwait-exceeds-cap test that yields
# [] is unaffected and still runs.)
_SRC_BUG_TARGET_ID = (
    "blocked: real src bug — telegram._build_signal constructs "
    "Signal(target_id=ctx.target_id) but the pivot dropped target_id from the "
    "target-agnostic Signal model (extra='forbid'); see rss.py:475 for the "
    "migrated shape. Flagged in real_src_bugs_flagged, src not edited."
)


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _StubMessage:
    """Mimics a telethon Message attribute bag."""

    id: int
    text: str
    date: datetime
    views: int = 0
    forwards: int = 0
    media: Any = None


@dataclass
class _StubEntity:
    username: str
    id: int
    title: str


@dataclass
class _StubChannelState:
    entity: _StubEntity
    messages: list[_StubMessage] = field(default_factory=list)
    # If set, raised on the next call to get_entity / iter_messages
    next_error: Exception | None = None
    # Per-attempt error script for retry tests. Pops from front each
    # iter_messages call until empty, then succeeds.
    error_script: list[Exception | None] = field(default_factory=list)


class _StubTelegramClient:
    """Deterministic Telethon stand-in."""

    def __init__(
        self,
        channels: dict[str, _StubChannelState] | None = None,
        *,
        connect_should_fail: bool = False,
    ) -> None:
        self._channels = channels or {}
        self._connected = False
        self._connect_should_fail = connect_should_fail
        self.connect_calls = 0
        self.disconnect_calls = 0
        self.get_entity_calls: list[str] = []
        self.iter_calls: list[str] = []

    async def connect(self) -> None:
        self.connect_calls += 1
        if self._connect_should_fail:
            raise ConnectionError("simulated connect failure")
        self._connected = True

    async def disconnect(self) -> None:
        self.disconnect_calls += 1
        self._connected = False

    async def is_connected(self) -> bool:
        return self._connected

    async def get_entity(self, handle: str) -> _StubEntity:
        self.get_entity_calls.append(handle)
        state = self._channels.get(handle)
        if state is None:
            raise KeyError(f"unknown channel: {handle}")
        if state.next_error is not None:
            err, state.next_error = state.next_error, None
            raise err
        return state.entity

    async def iter_messages(self, entity: _StubEntity, limit: int = 100):
        self.iter_calls.append(entity.username)
        # Find the channel state matching this entity.
        state = next(
            (s for s in self._channels.values() if s.entity is entity), None
        )
        if state is None:
            return
        if state.error_script:
            err = state.error_script.pop(0)
            if err is not None:
                raise err
        # Telethon returns newest-first by default.
        for msg in sorted(state.messages, key=lambda m: m.id, reverse=True):
            yield msg


def _static_resolver(mapping: dict[str, str]):
    """Build an async ``resolve(name) -> str`` matching the L-contract."""

    async def resolve(name: str) -> str:
        if name not in mapping:
            raise KeyError(f"secret {name!r} not in stub resolver")
        return mapping[name]

    return resolve


def _ctx_with_state(
    state: InMemoryStateStore | None = None,
    source_id: str = "src-tg-test",
    target_id: str = "tgt-tg-test",
    config: TelegramChannelSourceConfig | None = None,
) -> SourceContext:
    return SourceContext(
        target_id=target_id,
        target_version="v1",
        source_id=source_id,
        config=config or _make_config(),
        state_store=state or InMemoryStateStore(),
        logger=logging.getLogger("legba.test.telegram"),
    )


def _make_config(**overrides: Any) -> TelegramChannelSourceConfig:
    return TelegramChannelSourceConfig.model_validate(
        {
            "api_id_secret": "creds.telegram.api_id",
            "api_hash_secret": "creds.telegram.api_hash",
            "session_secret": "creds.telegram.session",
            "channels": ["@warnews"],
            **overrides,
        }
    )


def _valid_session_b64() -> str:
    """A short non-empty base64 blob. Telethon won't actually open it —
    the stub client_factory bypasses real client construction."""
    return base64.b64encode(b"legba-test-session").decode("ascii")


def _resolver_for(cfg: TelegramChannelSourceConfig):
    return _static_resolver(
        {
            cfg.api_id_secret: "12345",
            cfg.api_hash_secret: "deadbeefcafebabedeadbeefcafebabe",
            cfg.session_secret: _valid_session_b64(),
        }
    )


def _configure_handler(
    handler: TelegramChannelSourceHandler,
    cfg: TelegramChannelSourceConfig,
):
    """Run on_configure with a stub ctx exposing .config + .secrets_resolve."""
    ctx = SimpleNamespace(config=cfg, secrets_resolve=_resolver_for(cfg))
    return handler.on_configure(ctx)


# ---------------------------------------------------------------------------
# Config-level tests
# ---------------------------------------------------------------------------


def test_config_requires_credential_refs():
    with pytest.raises(ValidationError):
        TelegramChannelSourceConfig.model_validate({})


def test_config_defaults_are_safe():
    cfg = _make_config()
    assert cfg.lookback_hours == 24
    assert cfg.include_media is False
    assert cfg.per_channel_message_limit == 200
    assert cfg.max_retries_per_channel == 5


def test_config_rejects_zero_lookback():
    with pytest.raises(ValidationError):
        TelegramChannelSourceConfig.model_validate(
            {
                "api_id_secret": "a",
                "api_hash_secret": "b",
                "session_secret": "c",
                "lookback_hours": 0,
            }
        )


# ---------------------------------------------------------------------------
# Handler identity
# ---------------------------------------------------------------------------


def test_handler_classvars_match_contract():
    assert TelegramChannelSourceHandler.kind == "telegram_channel"
    assert TelegramChannelSourceHandler.family == "source"
    assert TelegramChannelSourceHandler.schema_version.startswith(
        "legba/source.telegram_channel/"
    )
    assert TelegramChannelSourceHandler.config_schema is TelegramChannelSourceConfig


# ---------------------------------------------------------------------------
# on_configure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_on_configure_resolves_secrets_and_writes_session_file(tmp_path):
    cfg = _make_config()
    handler = TelegramChannelSourceHandler()
    await _configure_handler(handler, cfg)

    assert handler._api_id == 12345  # noqa: SLF001 — test inspection
    assert handler._api_hash == "deadbeefcafebabedeadbeefcafebabe"  # noqa: SLF001
    assert handler._session_path is not None  # noqa: SLF001
    assert os.path.exists(handler._session_path)  # noqa: SLF001
    with open(handler._session_path, "rb") as fh:  # noqa: SLF001
        assert fh.read() == b"legba-test-session"

    # Tear down: on_retire deletes the materialized session.
    await handler.on_retire(SimpleNamespace())
    assert handler._session_path is None  # noqa: SLF001


@pytest.mark.asyncio
async def test_on_configure_rejects_invalid_session_b64():
    cfg = _make_config()
    bad = _static_resolver(
        {
            cfg.api_id_secret: "1",
            cfg.api_hash_secret: "h",
            cfg.session_secret: "!!!not-base64!!!",
        }
    )
    handler = TelegramChannelSourceHandler(secret_resolver=bad)
    with pytest.raises(ValueError, match="base64"):
        await handler.on_configure(SimpleNamespace(config=cfg))


@pytest.mark.asyncio
async def test_on_configure_rejects_non_integer_api_id():
    cfg = _make_config()
    bad = _static_resolver(
        {
            cfg.api_id_secret: "not-a-number",
            cfg.api_hash_secret: "h",
            cfg.session_secret: _valid_session_b64(),
        }
    )
    handler = TelegramChannelSourceHandler(secret_resolver=bad)
    with pytest.raises(ValueError, match="api_id"):
        await handler.on_configure(SimpleNamespace(config=cfg))


# ---------------------------------------------------------------------------
# pull — happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_yields_signals_with_expected_payload_shape():
    cfg = _make_config(channels=["@warnews"], lookback_hours=48)

    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1001, title="War News"),
        messages=[
            _StubMessage(id=10, text="alpha", date=now - timedelta(minutes=10), views=500, forwards=2),
            _StubMessage(id=11, text="bravo", date=now - timedelta(minutes=5), views=900, forwards=4),
            _StubMessage(id=12, text="charlie", date=now - timedelta(minutes=1), views=1200, forwards=7),
        ],
    )
    client = _StubTelegramClient({"warnews": state})

    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: client,
    )
    await _configure_handler(handler, cfg)

    ctx = _ctx_with_state()
    out: list[Signal] = []
    async for sig in handler.pull(ctx, since=now - timedelta(hours=1)):
        out.append(sig)

    assert len(out) == 3
    ids = sorted(s.payload["message_id"] for s in out)
    assert ids == [10, 11, 12]

    sample = next(s for s in out if s.payload["message_id"] == 12)
    # Source-first pivot: Signal is target-agnostic (target_id dropped,
    # extra='forbid'); it carries source_id + modality instead.
    assert sample.source_id == "src-tg-test"
    assert sample.modality == "text"
    assert sample.payload["text"] == "charlie"
    assert sample.payload["views"] == 1200
    assert sample.payload["forwards"] == 7
    assert sample.payload["channel"] == {
        "username": "warnews", "id": 1001, "title": "War News",
    }
    assert sample.payload["external_id"] == "tg:1001:12"
    assert sample.canonical_url == "https://t.me/warnews/12"
    assert sample.payload["media_type"] is None
    assert sample.content_hash  # non-empty
    assert sample.raw_provenance["source_kind"] == "telegram_channel"
    assert sample.raw_provenance["channel_id"] == 1001


@pytest.mark.asyncio
async def test_pull_lazily_configures_from_factory_injected_config():
    """Production poll path: ``build_source_handler`` injects the unwrapped
    config + ``secret_resolver`` into ``__init__`` and the SourceActor drives
    ``pull`` directly — without an explicit ``on_configure``. ``pull`` must
    lazily configure (resolve the vault secrets, materialize the session)
    instead of raising. Regression for the live ``source_actor.pull.error
    ... pull() called before on_configure`` poll failure that left the
    Telegram source ingesting zero signals.
    """
    cfg = _make_config(channels=["@warnews"], lookback_hours=48)

    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1001, title="War News"),
        messages=[
            _StubMessage(
                id=20, text="delta", date=now - timedelta(minutes=3),
                views=10, forwards=1,
            ),
        ],
    )
    client = _StubTelegramClient({"warnews": state})

    # Mirror build_source_handler exactly: config passed positionally into the
    # ``config`` slot, the secrets resolver threaded into ``secret_resolver``.
    # NO on_configure is called before pull — the actor never calls it.
    handler = TelegramChannelSourceHandler(
        cfg,
        secret_resolver=_resolver_for(cfg),
        client_factory=lambda **_: client,
    )
    assert handler._config is cfg  # factory-injected  # noqa: SLF001
    assert handler._session_path is None  # not configured yet  # noqa: SLF001

    ctx = _ctx_with_state(config=cfg)
    out = [s async for s in handler.pull(ctx, since=now - timedelta(hours=1))]

    assert len(out) == 1
    assert out[0].payload["message_id"] == 20
    # Proof the lazy configure ran: secrets resolved + session materialized.
    assert handler._api_id == 12345  # noqa: SLF001
    assert handler._session_path is not None  # noqa: SLF001


@pytest.mark.asyncio
async def test_pull_skips_messages_older_than_since():
    cfg = _make_config(channels=["@warnews"])
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=2, title="WN"),
        messages=[
            _StubMessage(id=1, text="old", date=now - timedelta(days=3)),
            _StubMessage(id=2, text="recent", date=now - timedelta(hours=1)),
        ],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    ctx = _ctx_with_state()

    out = [s async for s in handler.pull(ctx, since=now - timedelta(hours=2))]
    assert len(out) == 1
    assert out[0].payload["text"] == "recent"


@pytest.mark.asyncio
async def test_pull_advances_cursor_per_channel():
    cfg = _make_config(channels=["@a", "@b"], lookback_hours=72)
    now = datetime.now(tz=timezone.utc)
    sa = _StubChannelState(
        entity=_StubEntity(username="a", id=10, title="A"),
        messages=[_StubMessage(id=100, text="A1", date=now)],
    )
    sb = _StubChannelState(
        entity=_StubEntity(username="b", id=20, title="B"),
        messages=[
            _StubMessage(id=200, text="B1", date=now),
            _StubMessage(id=201, text="B2", date=now),
        ],
    )
    client = _StubTelegramClient({"a": sa, "b": sb})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore()
    ctx = _ctx_with_state(store)
    [s async for s in handler.pull(ctx)]

    cursors = await store.get("telegram_cursor")
    assert cursors == {"a": 100, "b": 201}


@pytest.mark.asyncio
async def test_pull_honors_cursor_on_second_call():
    cfg = _make_config(channels=["@warnews"], lookback_hours=72)
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=5, title="W"),
        messages=[
            _StubMessage(id=1, text="old", date=now - timedelta(hours=4)),
            _StubMessage(id=2, text="seen", date=now - timedelta(hours=3)),
            _StubMessage(id=3, text="new", date=now - timedelta(hours=1)),
        ],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore({"telegram_cursor": {"warnews": 2}})
    ctx = _ctx_with_state(store)
    out = [s async for s in handler.pull(ctx)]
    assert [s.payload["message_id"] for s in out] == [3]


@pytest.mark.asyncio
async def test_pull_handle_normalization_strips_at_and_scheme():
    cfg = _make_config(channels=["@warnews", "telegram://breaking", "rawhandle"])
    now = datetime.now(tz=timezone.utc)
    states = {
        h: _StubChannelState(
            entity=_StubEntity(username=h, id=i + 1, title=h.upper()),
            messages=[_StubMessage(id=1, text="x", date=now)],
        )
        for i, h in enumerate(["warnews", "breaking", "rawhandle"])
    }
    client = _StubTelegramClient(states)
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    out = [s async for s in handler.pull(_ctx_with_state())]
    assert {s.payload["channel"]["username"] for s in out} == {
        "warnews", "breaking", "rawhandle"
    }
    assert sorted(client.get_entity_calls) == ["breaking", "rawhandle", "warnews"]


@pytest.mark.asyncio
async def test_pull_respects_per_channel_message_limit():
    cfg = _make_config(channels=["@warnews"], per_channel_message_limit=2, lookback_hours=72)
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=i, text=f"m{i}", date=now) for i in range(1, 11)],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    out = [s async for s in handler.pull(_ctx_with_state())]
    assert len(out) == 2
    # Telethon delivers newest-first; we keep the two newest.
    assert sorted(s.payload["message_id"] for s in out) == [9, 10]


# ---------------------------------------------------------------------------
# Media handling
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_records_media_type_without_downloading_by_default():
    cfg = _make_config(channels=["@warnews"])
    now = datetime.now(tz=timezone.utc)

    class _MessageMediaPhoto:
        mime_type = "image/jpeg"
        size = 12345
        file_reference = b"\x00\x01abc"

    msg = _StubMessage(id=1, text="see photo", date=now, media=_MessageMediaPhoto())
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[msg],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    out = [s async for s in handler.pull(_ctx_with_state())]
    assert out[0].payload["media_type"] == "photo"
    assert "media" not in out[0].payload  # bytes NOT downloaded by default


@pytest.mark.asyncio
async def test_pull_attaches_media_descriptor_when_include_media_true():
    cfg = _make_config(channels=["@warnews"], include_media=True)
    now = datetime.now(tz=timezone.utc)

    class _MessageMediaDocument:
        mime_type = "video/mp4"
        size = 9876
        file_reference = b"\xff\xee"

    msg = _StubMessage(id=1, text="vid", date=now, media=_MessageMediaDocument())
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[msg],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    out = [s async for s in handler.pull(_ctx_with_state())]
    assert out[0].payload["media_type"] == "document"
    media = out[0].payload["media"]
    assert media["mime_type"] == "video/mp4"
    assert media["size"] == 9876
    assert base64.b64decode(media["file_reference"]) == b"\xff\xee"


# ---------------------------------------------------------------------------
# Reconnection / backoff
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pull_retries_on_transient_error_then_succeeds():
    cfg = _make_config(channels=["@warnews"], max_retries_per_channel=3, backoff_base_seconds=0.0)
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=1, text="ok", date=now)],
        error_script=[ConnectionError("transient")],  # first iter_messages call fails
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)
    out = [s async for s in handler.pull(_ctx_with_state())]
    assert len(out) == 1
    assert client.disconnect_calls >= 1
    assert client.connect_calls >= 2  # one initial + one after retry


@pytest.mark.asyncio
async def test_pull_gives_up_on_channel_after_max_retries_continues_others():
    cfg = _make_config(channels=["@bad", "@good"], max_retries_per_channel=2, backoff_base_seconds=0.0)
    now = datetime.now(tz=timezone.utc)
    bad = _StubChannelState(
        entity=_StubEntity(username="bad", id=1, title="bad"),
        messages=[_StubMessage(id=1, text="x", date=now)],
        error_script=[RuntimeError("fail-1"), RuntimeError("fail-2"), RuntimeError("fail-3")],
    )
    good = _StubChannelState(
        entity=_StubEntity(username="good", id=2, title="good"),
        messages=[_StubMessage(id=42, text="ok", date=now)],
    )
    client = _StubTelegramClient({"bad": bad, "good": good})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)
    store = InMemoryStateStore()
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    handles = {s.payload["channel"]["username"] for s in out}
    assert handles == {"good"}
    # Bad channel cursor not advanced; good channel cursor at 42.
    cursors = await store.get("telegram_cursor")
    assert cursors == {"good": 42}


@pytest.mark.asyncio
async def test_pull_honors_floodwait_without_consuming_retry_budget(monkeypatch):
    cfg = _make_config(
        channels=["@warnews"],
        max_retries_per_channel=1,
        flood_wait_cap_seconds=5,
        backoff_base_seconds=0.0,
    )
    now = datetime.now(tz=timezone.utc)

    class FakeFloodWait(Exception):
        def __init__(self, seconds: int) -> None:
            super().__init__(f"flood {seconds}s")
            self.seconds = seconds

    # Patch the module-level lookup so our fake is treated as FloodWait.
    from legba.data.sources import telegram as tg_mod
    monkeypatch.setattr(tg_mod, "_flood_wait_error_class", lambda: FakeFloodWait)

    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=1, text="ok", date=now)],
        error_script=[FakeFloodWait(seconds=0)],
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    # FloodWait(seconds=0) keeps the test fast; backoff_base_seconds=0
    # ensures the post-FloodWait continue() doesn't sleep either. We
    # deliberately avoid globally monkeypatching asyncio.sleep — that
    # recurses through the harness's own awaits.
    out = [s async for s in handler.pull(_ctx_with_state())]
    assert len(out) == 1


@pytest.mark.asyncio
async def test_pull_skips_channel_when_floodwait_exceeds_cap(monkeypatch):
    cfg = _make_config(channels=["@warnews"], flood_wait_cap_seconds=2)
    now = datetime.now(tz=timezone.utc)

    class FakeFloodWait(Exception):
        def __init__(self, seconds: int) -> None:
            super().__init__(f"flood {seconds}s")
            self.seconds = seconds

    from legba.data.sources import telegram as tg_mod
    monkeypatch.setattr(tg_mod, "_flood_wait_error_class", lambda: FakeFloodWait)

    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=1, text="ok", date=now)],
        error_script=[FakeFloodWait(seconds=999)],
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    out = [s async for s in handler.pull(_ctx_with_state())]
    assert out == []


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_check_healthy_when_connected_and_channel_resolves():
    cfg = _make_config()
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[],
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)
    # Force the connect path.
    await handler.on_activate(SimpleNamespace())

    health = await handler.health_check(_ctx_with_state())
    assert isinstance(health, SourceHealth)
    assert health.state == "healthy"
    assert health.detail.get("connected") is True
    assert health.detail.get("probed_channel") == "warnews"


@pytest.mark.asyncio
async def test_health_check_degraded_when_channel_probe_fails():
    cfg = _make_config(channels=["@missing"])
    client = _StubTelegramClient({})  # empty registry → KeyError on get_entity
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)
    await handler.on_activate(SimpleNamespace())

    health = await handler.health_check(_ctx_with_state())
    assert health.state == "degraded"
    assert health.detail.get("probe_ok") is False


@pytest.mark.asyncio
async def test_health_check_unhealthy_when_client_unavailable():
    cfg = _make_config()
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: None)
    await _configure_handler(handler, cfg)
    health = await handler.health_check(_ctx_with_state())
    assert health.state == "unhealthy"


# ---------------------------------------------------------------------------
# Env-fallback resolver
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_env_secret_resolver_reads_legba_telegram_vars(monkeypatch):
    monkeypatch.setenv("LEGBA_TELEGRAM_API_ID", "777")
    val = await _env_resolver("creds.telegram.api_id")
    assert val == "777"


@pytest.mark.asyncio
async def test_env_secret_resolver_raises_on_missing(monkeypatch):
    monkeypatch.delenv("LEGBA_TELEGRAM_MISSING", raising=False)
    with pytest.raises(KeyError):
        await _env_resolver("creds.telegram.missing")


# ---------------------------------------------------------------------------
# Integration block — only runs if real user-account creds are present.
# ---------------------------------------------------------------------------


_TG_INTEGRATION_ENABLED = all(
    os.environ.get(k) for k in (
        "LEGBA_TELEGRAM_API_ID",
        "LEGBA_TELEGRAM_API_HASH",
        "LEGBA_TELEGRAM_SESSION",
    )
)


@pytest.mark.integration
@pytest.mark.skipif(
    not _TG_INTEGRATION_ENABLED,
    reason="LEGBA_TELEGRAM_API_ID / API_HASH / SESSION not set",
)
@pytest.mark.asyncio
async def test_integration_real_telethon_smoke():
    """Smoke test against real Telegram MTProto.

    Only runs when the three env vars above are present — the runtime
    is responsible for putting them there in production. The channel
    probed is the integration-test channel name from the env, defaulting
    to ``@telegram`` (Telegram's own official channel, always reachable).
    """
    cfg = TelegramChannelSourceConfig(
        api_id_secret="creds.telegram.api_id",
        api_hash_secret="creds.telegram.api_hash",
        session_secret="creds.telegram.session",
        channels=[os.environ.get("LEGBA_TELEGRAM_PROBE_CHANNEL", "@telegram")],
        lookback_hours=72,
        per_channel_message_limit=3,
    )
    handler = TelegramChannelSourceHandler()
    await _configure_handler(handler, cfg)
    try:
        health = await handler.health_check(_ctx_with_state())
        assert health.state in {"healthy", "degraded"}
        if health.state == "healthy":
            ctx = _ctx_with_state()
            collected = 0
            async for sig in handler.pull(ctx):
                assert sig.payload["message_id"] > 0
                collected += 1
                if collected >= 3:
                    break
    finally:
        await handler.on_retire(SimpleNamespace())

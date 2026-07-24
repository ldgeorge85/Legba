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

    async def iter_messages(
        self,
        entity: _StubEntity,
        limit: int = 100,
        *,
        min_id: int = 0,
        reverse: bool = False,
    ):
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
        # Task #206 — real Telethon min_id + reverse semantics (verified
        # against the installed telethon package's iter_messages docstring):
        # min_id EXCLUDES messages with id <= min_id; with reverse=True the
        # walk is oldest-to-newest ("min_id becomes equivalent to offset_id
        # ... since messages are returned in ascending order"). Without
        # reverse, Telethon's default is newest-first (unchanged from the
        # pre-#206 stub behavior).
        pool = [m for m in state.messages if m.id > min_id]
        ordered = sorted(pool, key=lambda m: m.id, reverse=not reverse)
        for msg in ordered:
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
# FIX 1 — honest session-revoked health (fail-loud, no silent 'empty')
#
# A revoked / expired MTProto session fails AUTH on every channel. The
# per-channel try/except isolation would otherwise return a clean 0-yield pull
# that the source actor records as 'empty', hiding the dead session. The
# handler now records a health state under ``health_state_key`` that
# SourceActor._record_poll_outcome turns into an honest 'error' outcome. These
# tests assert the RECORDED HEALTH STATE (the observable input to that mapping);
# 'degraded'/'unhealthy' → 'error', 'healthy' → not-an-error.
# ---------------------------------------------------------------------------


_HEALTH_KEY = TelegramChannelSourceHandler.health_state_key


@pytest.mark.asyncio
async def test_pull_session_revoked_records_unhealthy_health(monkeypatch):
    """All channels auth-fail (session revoked) → 'unhealthy' health state.

    The source actor maps 'unhealthy' → an 'error' poll outcome, so the dead
    session is finally visible in source_poll_outcomes + the liveness watchdog
    instead of masquerading as a silent 'empty'. No signals are fabricated.
    """
    from legba.data.sources import telegram as tg_mod

    class FakeAuthError(Exception):
        """Stand-in for telethon's AuthKeyUnregisteredError / SessionRevokedError."""

    # Mirror the FloodWait pattern: monkeypatch the module-level class lookup so
    # our fake is treated as a session-level auth error (no telethon needed).
    monkeypatch.setattr(tg_mod, "_auth_error_classes", lambda: (FakeAuthError,))

    cfg = _make_config(
        channels=["@a", "@b"],
        max_retries_per_channel=5,
        backoff_base_seconds=0.0,
    )
    now = datetime.now(tz=timezone.utc)
    sa = _StubChannelState(
        entity=_StubEntity(username="a", id=1, title="A"),
        messages=[_StubMessage(id=1, text="x", date=now)],
        next_error=FakeAuthError("AUTH_KEY_UNREGISTERED"),
    )
    sb = _StubChannelState(
        entity=_StubEntity(username="b", id=2, title="B"),
        messages=[_StubMessage(id=2, text="y", date=now)],
        next_error=FakeAuthError("AUTH_KEY_UNREGISTERED"),
    )
    client = _StubTelegramClient({"a": sa, "b": sb})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore()
    out = [s async for s in handler.pull(_ctx_with_state(store))]

    # A dead session yields NOTHING (no fabrication).
    assert out == []
    # Auth failure is NON-retryable: exactly one get_entity call per channel —
    # no backoff/retry hammering (the retry storm is what got the account
    # bot-flagged and the session revoked in the first place).
    assert sorted(client.get_entity_calls) == ["a", "b"]

    health = await store.get(_HEALTH_KEY)
    assert health is not None, "handler must record health, not stay silent"
    assert health["state"] == "unhealthy"
    assert health["detail"]["reason"] == "session_revoked"
    assert sorted(health["detail"]["auth_failed_channels"]) == ["a", "b"]
    # This is exactly what SourceActor._record_poll_outcome maps to 'error'.
    assert health["state"] in ("degraded", "unhealthy")


@pytest.mark.asyncio
async def test_pull_single_channel_floodwait_is_not_a_source_error(monkeypatch):
    """A single-channel flood-wait (even one exceeding the cap → give-up) is a
    TRANSIENT per-channel condition, NOT a revoked session. Health stays
    'healthy' so the poll is not falsely reported as a source error."""
    from legba.data.sources import telegram as tg_mod

    class FakeFloodWait(Exception):
        def __init__(self, seconds: int) -> None:
            super().__init__(f"flood {seconds}s")
            self.seconds = seconds

    monkeypatch.setattr(tg_mod, "_flood_wait_error_class", lambda: FakeFloodWait)

    cfg = _make_config(channels=["@warnews"], flood_wait_cap_seconds=2)
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=1, text="ok", date=now)],
        error_script=[FakeFloodWait(seconds=999)],  # exceeds cap → give-up
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore()
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    assert out == []

    health = await store.get(_HEALTH_KEY)
    assert health is not None
    assert health["state"] == "healthy"
    # NOT one of the states the source actor turns into an 'error' outcome.
    assert health["state"] not in ("degraded", "unhealthy")


@pytest.mark.asyncio
async def test_pull_empty_channel_is_not_a_source_error():
    """A channel that resolves but has no new messages is a legitimate empty
    poll — health stays 'healthy', never an error."""
    cfg = _make_config(channels=["@warnews"])
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[],  # resolves fine, nothing to yield
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore()
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    assert out == []

    health = await store.get(_HEALTH_KEY)
    assert health is not None
    assert health["state"] == "healthy"
    assert health["state"] not in ("degraded", "unhealthy")


@pytest.mark.asyncio
async def test_pull_partial_auth_failure_records_degraded_not_full_revoke(monkeypatch):
    """One channel auth-fails while another succeeds — NOT a confirmed full
    revocation. Recorded 'degraded' (visible) but NOT the systemic 'unhealthy';
    the good channel's signals still flow (degrade-not-break), so the actor's
    poll outcome is driven by those written signals rather than the health key."""
    from legba.data.sources import telegram as tg_mod

    class FakeAuthError(Exception):
        pass

    monkeypatch.setattr(tg_mod, "_auth_error_classes", lambda: (FakeAuthError,))

    cfg = _make_config(channels=["@dead", "@live"], lookback_hours=48)
    now = datetime.now(tz=timezone.utc)
    dead = _StubChannelState(
        entity=_StubEntity(username="dead", id=1, title="D"),
        messages=[_StubMessage(id=1, text="x", date=now)],
        next_error=FakeAuthError("AUTH_KEY_UNREGISTERED"),
    )
    live = _StubChannelState(
        entity=_StubEntity(username="live", id=2, title="L"),
        messages=[_StubMessage(id=99, text="ok", date=now)],
    )
    client = _StubTelegramClient({"dead": dead, "live": live})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore()
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    # The reachable channel still delivers.
    assert [s.payload["channel"]["username"] for s in out] == ["live"]

    health = await store.get(_HEALTH_KEY)
    assert health is not None
    assert health["state"] == "degraded"
    assert "dead" in health["detail"]["auth_failed_channels"]


# ---------------------------------------------------------------------------
# FIX 2 — descriptor footprint softening (anti-bot-flag)
# ---------------------------------------------------------------------------


def test_descriptor_cadence_and_limit_soften_footprint():
    """The committed descriptor widens the cadence + jitter (the real
    anti-bot-flag levers: request FREQUENCY + jitter) to reduce the flagging
    that revokes the session — while KEEPING per_channel_message_limit at 50
    (the message cap is a runaway-pull safety belt, not a footprint lever:
    Telethon fetches ≤100 msgs per getHistory request, so 25 vs 50 is the same
    single API call — lowering it would only cost war-beat coverage). The
    softened config still parses through the production unwrap → config-schema
    path."""
    from pathlib import Path

    import yaml
    from legba.runtime.source_factory import _unwrap_factory_dict

    repo_root = Path(__file__).resolve().parents[2]
    body = yaml.safe_load(
        (repo_root / "descriptors" / "source_telegram_monitor.yaml").read_text()
    )

    # Cadence widened */15 → */30 (poll rate halved); jitter widened 60 → 120.
    assert body["cadence"]["schedule"]["raw"] == "*/30 * * * *"
    assert body["cadence"]["jitter_seconds"] == 120

    # per_channel_message_limit kept at 50 (NOT a footprint lever — see docstring).
    assert body["config"]["per_channel_message_limit"]["raw"] == 50

    # The softened config still parses through the production unwrap + schema.
    cfg = TelegramChannelSourceConfig(**_unwrap_factory_dict(body["config"]))
    assert cfg.per_channel_message_limit == 50
    # Lookback stays FAR wider than the 30-min cadence gap → nothing missed on
    # the cursor-less first pull (steady-state is bounded by the id cursor).
    assert cfg.lookback_hours == 12
    assert len(cfg.channels) >= 1
    # #206 rides the SAME softened descriptor — no explicit override needed,
    # the field's own default (10 pages/poll) applies; asserting it here so a
    # future descriptor edit can't silently regress cadence-soften's intent
    # (a huge catchup_max_pages_per_channel would defeat the point of a
    # halved poll rate by flooding right back on resume).
    assert cfg.catchup_max_pages_per_channel == 10


# ---------------------------------------------------------------------------
# #206 — bounded backlog catch-up (min_id + reverse)
# ---------------------------------------------------------------------------
#
# The bug: a channel that produced MORE than per_channel_message_limit
# messages since the last cursor used to silently DROP the excess — a single
# newest-first page only ever returns the latest per_channel_message_limit
# messages, so anything OLDER than that (but still newer than the cursor)
# was skipped, and the cursor then advanced past the gap forever (permanent,
# unrecoverable loss). test_pull_respects_per_channel_message_limit (above,
# unmodified) demonstrates the PRE-#206 behavior on a COLD START (no cursor)
# — that case is intentionally UNCHANGED (bounded by lookback_hours, not a
# gap). These tests exercise the RESUMED-poll path (last_seen_id > 0), where
# #206 now pages forward instead of dropping.


@pytest.mark.asyncio
async def test_catchup_small_gap_single_short_page_matches_old_semantics():
    """A gap smaller than one page (the everyday, non-catch-up case) still
    yields exactly the new messages in one short page — the SAME set the
    pre-#206 single-page code would have produced, just via the new
    min_id+reverse walk instead of a newest-first walk. No behavior change
    for the common case."""
    cfg = _make_config(channels=["@warnews"], per_channel_message_limit=200)
    now = datetime.now(tz=timezone.utc)
    # cursor at id=2; 3 new messages (ids 3,4,5) — well under the 200 cap.
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[
            _StubMessage(id=1, text="old", date=now - timedelta(hours=5)),
            _StubMessage(id=2, text="seen", date=now - timedelta(hours=4)),
            _StubMessage(id=3, text="new1", date=now - timedelta(hours=3)),
            _StubMessage(id=4, text="new2", date=now - timedelta(hours=2)),
            _StubMessage(id=5, text="new3", date=now - timedelta(hours=1)),
        ],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    store = InMemoryStateStore({"telegram_cursor": {"warnews": 2}})
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    assert sorted(s.payload["message_id"] for s in out) == [3, 4, 5]
    cursors = await store.get("telegram_cursor")
    assert cursors == {"warnews": 5}


@pytest.mark.asyncio
async def test_catchup_gap_exceeding_one_page_drains_across_pages_no_skip():
    """THE #206 regression case: a channel produced MORE than
    per_channel_message_limit messages since the cursor. The pre-#206 code
    would return only the newest `limit` (dropping the rest permanently);
    #206 pages forward and returns EVERY message in the gap, in order, none
    skipped."""
    cfg = _make_config(channels=["@warnews"], per_channel_message_limit=5)
    now = datetime.now(tz=timezone.utc)
    # cursor at id=0 (simulated as if some earlier state existed) — 23
    # messages total in the backlog, cap=5/page → needs 5 pages (5*4=20, +3
    # on the 5th, short page) to fully drain.
    messages = [
        _StubMessage(id=i, text=f"m{i}", date=now - timedelta(minutes=(23 - i)))
        for i in range(1, 24)
    ]
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=messages,
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    # last_seen_id=0 (dispatch check is `<= 0`) would take the cold-start
    # path (unchanged pre-#206 behavior, bounded by lookback_hours) — a
    # DIFFERENT scenario than what #206 fixes. Seed cursor=1 to simulate a
    # RESUMED poll (message id 1 already seen; ids start at 1 in Telethon,
    # so this is the smallest realistic positive cursor) and exercise the
    # catch-up path specifically.
    store = InMemoryStateStore({"telegram_cursor": {"warnews": 1}})
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    # Cursor was 1 → messages 2..23 are the gap (22 messages), ALL must be
    # present, in ascending id order across the page boundaries, none
    # skipped and none duplicated.
    ids = [s.payload["message_id"] for s in out]
    assert ids == list(range(2, 24))
    cursors = await store.get("telegram_cursor")
    assert cursors == {"warnews": 23}


@pytest.mark.asyncio
async def test_catchup_flood_cap_bounds_pages_per_poll_and_resumes_next_poll():
    """A CATASTROPHIC backlog (far exceeding catchup_max_pages_per_channel *
    per_channel_message_limit) must NEVER flood one poll — it hard-stops at
    the page cap, and the cursor lands exactly at the boundary the SECOND
    poll resumes from (no gap, no re-emission of already-drained pages)."""
    cfg = _make_config(
        channels=["@warnews"],
        per_channel_message_limit=3,
        catchup_max_pages_per_channel=2,  # cap: at most 2*3=6 msgs/poll
    )
    now = datetime.now(tz=timezone.utc)
    # 50 messages in backlog — WAY more than the 6/poll the cap allows.
    messages = [
        _StubMessage(id=i, text=f"m{i}", date=now - timedelta(minutes=(50 - i)))
        for i in range(1, 51)
    ]
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=messages,
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    # last_seen_id=0 (dispatch check `<= 0`) would take the cold-start path
    # (unchanged pre-#206 behavior, bounded by lookback_hours) — seed
    # cursor=1 to simulate a RESUMED poll and exercise catch-up specifically.
    poll1_store = InMemoryStateStore({"telegram_cursor": {"warnews": 1}})
    out1 = [s async for s in handler.pull(_ctx_with_state(poll1_store))]
    ids1 = [s.payload["message_id"] for s in out1]
    # Exactly the flood-cap worth of messages (2 pages * 3/page = 6),
    # starting right after the cursor, in ascending order.
    assert ids1 == [2, 3, 4, 5, 6, 7]
    cursor_after_poll1 = (await poll1_store.get("telegram_cursor"))["warnews"]
    assert cursor_after_poll1 == 7

    # Second poll resumes exactly where the first left off — no gap, no
    # re-emission of ids 2-7.
    out2 = [s async for s in handler.pull(_ctx_with_state(poll1_store))]
    ids2 = [s.payload["message_id"] for s in out2]
    assert ids2 == [8, 9, 10, 11, 12, 13]
    assert ids2[0] == ids1[-1] + 1  # perfectly contiguous, no gap


@pytest.mark.asyncio
async def test_catchup_persists_cursor_after_each_page_not_just_at_the_end():
    """Durability: the cursor must be written to state_store incrementally,
    per page — not only once after the whole multi-page walk finishes. This
    proves a crash mid-catch-up would lose at most the CURRENT in-flight
    page's messages already yielded to the consumer (still safe: dedupe /
    re-yield is the documented invariant), never a page already fully
    drained and persisted."""
    cfg = _make_config(
        channels=["@warnews"], per_channel_message_limit=3,
        catchup_max_pages_per_channel=5,
    )
    now = datetime.now(tz=timezone.utc)
    messages = [
        _StubMessage(id=i, text=f"m{i}", date=now - timedelta(minutes=(12 - i)))
        for i in range(1, 13)  # 12 messages, cap 3/page -> 4 pages exactly
    ]
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=messages,
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)

    store = InMemoryStateStore({"telegram_cursor": {"warnews": 1}})
    cursor_snapshots: list[int] = []
    orig_set = store.set

    async def _tracking_set(key, value):
        await orig_set(key, value)
        if key == "telegram_cursor" and "warnews" in value:
            cursor_snapshots.append(value["warnews"])

    store.set = _tracking_set  # type: ignore[method-assign]

    out = [s async for s in handler.pull(_ctx_with_state(store))]
    assert len(out) == 11  # ids 2..12
    # The cursor was persisted MULTIPLE times during the walk (once per
    # completed page), not just once at pull()'s very end — proving
    # per-page durability, not end-of-pull-only durability.
    assert len(cursor_snapshots) >= 3
    # Snapshots are monotonically non-decreasing (never regresses mid-walk).
    assert cursor_snapshots == sorted(cursor_snapshots)
    assert cursor_snapshots[-1] == 12


@pytest.mark.asyncio
async def test_catchup_respects_max_retries_and_gives_up_cleanly():
    """A channel that errors on every catch-up page attempt still gives up
    per the existing max_retries_per_channel policy (shared via
    _apply_backoff_or_giveup) — #206 must not create an infinite retry loop
    on a persistently-failing catch-up page."""
    cfg = _make_config(
        channels=["@warnews"], per_channel_message_limit=3,
        max_retries_per_channel=2, backoff_base_seconds=0.0,
    )
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=i, text=f"m{i}", date=now) for i in range(1, 6)],
        error_script=[RuntimeError("boom1"), RuntimeError("boom2"), RuntimeError("boom3")],
    )
    handler = TelegramChannelSourceHandler(
        client_factory=lambda **_: _StubTelegramClient({"warnews": state}),
    )
    await _configure_handler(handler, cfg)
    store = InMemoryStateStore({"telegram_cursor": {"warnews": 1}})
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    # Gives up cleanly — no signals, no cursor advance, no unhandled raise.
    assert out == []
    cursors = await store.get("telegram_cursor")
    assert cursors.get("warnews", 1) == 1  # unchanged — give-up leaves cursor alone


@pytest.mark.asyncio
async def test_catchup_auth_failure_is_not_retried_even_mid_catchup(monkeypatch):
    """#204's systemic-auth-failure fast path must still short-circuit a
    catch-up page immediately (no wasted retries hammering a revoked
    session), exactly as it does for the cold-start path."""
    from legba.data.sources import telegram as tg_mod

    class FakeAuthError(Exception):
        pass

    monkeypatch.setattr(tg_mod, "_auth_error_classes", lambda: (FakeAuthError,))

    cfg = _make_config(
        channels=["@warnews"], per_channel_message_limit=3,
        max_retries_per_channel=5, backoff_base_seconds=0.0,
    )
    now = datetime.now(tz=timezone.utc)
    state = _StubChannelState(
        entity=_StubEntity(username="warnews", id=1, title="W"),
        messages=[_StubMessage(id=i, text=f"m{i}", date=now) for i in range(1, 6)],
        error_script=[FakeAuthError("AUTH_KEY_UNREGISTERED")],
    )
    client = _StubTelegramClient({"warnews": state})
    handler = TelegramChannelSourceHandler(client_factory=lambda **_: client)
    await _configure_handler(handler, cfg)
    store = InMemoryStateStore({"telegram_cursor": {"warnews": 1}})
    out = [s async for s in handler.pull(_ctx_with_state(store))]
    assert out == []
    # Exactly ONE iter_messages attempt — the auth failure short-circuits
    # immediately, never entering the backoff/retry loop.
    assert client.iter_calls == ["warnews"]

    health = await store.get(_HEALTH_KEY)
    assert health["state"] == "unhealthy"
    assert health["detail"]["reason"] == "session_revoked"


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

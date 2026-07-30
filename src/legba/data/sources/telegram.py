# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Telegram channel source handler (L-136, Phase 3).

Pulls public-channel messages over MTProto via Telethon, yielding one
:class:`Signal` per message. OSINT-relevant for monitoring channels in
target countries (war reporting, government press, opposition voices,
local-language commentary) where RSS / news APIs miss.

Contract: implements the L-102 §2 source-kind protocol via the trimmed
:class:`SourceHandler` in :mod:`legba.data.sources._contract`. Like the
RSS handler (L-130) this is the only first-party source handler that
needs a persistent client connection — Telethon's MTProto client is
stateful and benefits from being held open across `pull` calls.

Credentials (Telegram API ID, API hash, base64-encoded session blob)
live in the credentials vault and are referenced by name from the
config; the handler resolves them at `on_configure` time and never
caches the raw values. The session blob is materialized to a temp
file at activate time and torn down at retire (Telethon needs a file
on disk).

Cursor: max-seen `message_id` per channel. Telethon's `iter_messages`
returns newest-first; we walk until we hit either the stored cursor or
the `since` lower bound, whichever is earlier — re-emission of
overlapping windows is allowed (dedupe is downstream, per KC-3 / L-151).

Reconnection: Telethon raises `FloodWaitError(seconds=N)` on rate
limit; we honor it. Connection drops fall through to exponential
backoff (1s, 2s, 4s, 8s, capped at 30s; max 5 attempts per channel
per pull). Channels that exhaust retries are skipped with a structured
log; subsequent pulls retry.

Media handling: by default we skip downloading media. Each message's
``media_type`` (photo / video / document / poll / webpage / none) is
recorded as metadata so downstream OCR / transcription analysts can
fan back out and pull selectively. Setting ``include_media=True``
attaches the media descriptor (file_reference, size, mime) to the
payload but still does not download bytes — bulk media pull is a
separate task (deferred).
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import logging
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..schemas.source import SourceClass
from ._contract import Signal, SourceContext, SourceHealth


# ---------------------------------------------------------------------------
# Optional telethon dependency — lazy-imported so test environments and
# health probes don't require it to load the module.
# ---------------------------------------------------------------------------


def _telethon():
    """Return the telethon module or None if unavailable."""
    try:
        import telethon  # noqa: F401
        return __import__("telethon")
    except ImportError:
        return None


def _flood_wait_error_class():
    try:
        from telethon.errors import FloodWaitError
        return FloodWaitError
    except ImportError:
        return None


def _auth_error_classes() -> tuple[type[BaseException], ...]:
    """Return telethon's SESSION-level auth error classes (or ``()``).

    A revoked / expired / deauthorized MTProto session raises one of these on
    EVERY request — they are systemic (the whole session is dead), NOT a
    per-channel access problem (``ChannelPrivateError`` / ``ChannelInvalidError``
    are deliberately excluded — those are one bad handle, not a dead session).
    ``UnauthorizedError`` is telethon's base class for the 401 family
    (AuthKeyUnregistered / SessionRevoked / SessionExpired / UserDeactivated /
    ...); we also name the concrete leaves so a telethon layout that doesn't
    re-export the base still matches. Returns ``()`` when telethon is
    unavailable so callers degrade to a no-op.

    Module-level (like :func:`_flood_wait_error_class`) so tests can
    monkeypatch it to inject a stand-in auth error without importing telethon.
    """
    try:
        from telethon import errors as _errors
    except ImportError:
        return ()
    classes: list[type[BaseException]] = []
    for name in (
        "UnauthorizedError",        # base for the 401 family
        "AuthKeyUnregisteredError",
        "AuthKeyError",
        "SessionRevokedError",
        "SessionExpiredError",
        "UserDeactivatedError",
        "UserDeactivatedBanError",
    ):
        cls = getattr(_errors, name, None)
        if isinstance(cls, type) and issubclass(cls, BaseException):
            classes.append(cls)
    return tuple(classes)


_TELETHON_LOGGERS_TAMED = False

# A7 poller guards — process-start marker for the startup-delay guard.
# Captured at MODULE import (process start for the runtime container): after a
# container swap the OLD container's MTProto connection can linger for tens of
# seconds; connecting a fresh client with the SAME session while the old one is
# alive is what triggers Telegram's AUTH_KEY_DUPLICATED (which KILLS the
# session — a re-auth, not a retry). The guard skips polls until the process
# has been up ``startup_delay_seconds`` (worldmonitor-proven mitigation).
_PROCESS_STARTED_AT_MONOTONIC: float = time.monotonic()


def _tame_telethon_loggers() -> None:
    """Raise telethon's chatty transport loggers to WARNING (H1).

    A dead / expired / IP-blocked MTProto session drops telethon into a
    transport-level reconnect loop that emits ``Connecting to …`` /
    ``Connection … complete!`` / ``Server closed the connection`` at INFO
    every few ms — a stuck session was measured at ~150 lines/sec, 95% of
    the whole runtime log. We only ever want WARNING+ from telethon's
    network layer; real errors still surface. Idempotent."""
    global _TELETHON_LOGGERS_TAMED
    if _TELETHON_LOGGERS_TAMED:
        return
    for name in (
        "telethon",
        "telethon.network",
        "telethon.network.mtprotosender",
        "telethon.network.connection",
        "telethon.network.connection.connection",
    ):
        logging.getLogger(name).setLevel(logging.WARNING)
    _TELETHON_LOGGERS_TAMED = True


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------
#
# The L-102 §7 contract resolves credentials via the runtime context. In
# this code path, :class:`SourceContext` exposes an async callable
# ``secrets_resolve(vault_id) -> str`` — exactly what we need. When the
# context omits the resolver (unit tests, dev bootstrap), we fall back to
# (a) an explicit constructor argument, (b) ``LEGBA_TELEGRAM_<NAME>`` env
# vars (matches the integration-test envvars in the L-136 brief). Plain
# string literals on the config are treated as the credential itself only
# when no resolver path matches — this is the "config-as-secret" escape
# hatch the contract docstring calls out.


async def _env_resolver(name: str) -> str:
    """Resolve a vault ref to ``LEGBA_TELEGRAM_<segment>``."""
    key_segment = name.rsplit(".", 1)[-1].upper()
    env_key = f"LEGBA_TELEGRAM_{key_segment}"
    val = os.environ.get(env_key)
    if val is None:
        raise KeyError(f"secret {name!r} not found (no env {env_key})")
    return val


async def _resolve_secret(
    name: str,
    *,
    ctx_resolver: Callable[[str], Awaitable[str]] | None,
    handler_resolver: Callable[[str], Awaitable[str]] | None,
) -> str:
    """Pick a resolver from the available options.

    Preference: explicit handler-side resolver → context-side resolver →
    env-var fallback. The handler-side resolver is set by tests and
    bootstrap scripts; the context-side is the runtime's preferred path.
    """
    for resolver in (handler_resolver, ctx_resolver):
        if resolver is None:
            continue
        return await resolver(name)
    return await _env_resolver(name)


def _strip_channel_prefix(channel_ref: str) -> str:
    """Strip '@' / 'telegram://' the same way everywhere a channel handle is
    compared: the ``channels`` list (:meth:`TelegramChannelSourceHandler.
    _normalize_handle`), the ``classes`` per-channel override map's keys
    (below), and — on the read side — a signal's own
    ``payload.channel.username`` (:mod:`legba.data.analysts.signal_salience`'s
    stamping-path lookup). One normalization rule, three call sites, so an
    override keyed ``"@Almasirah_En"`` matches a channel configured as
    ``"Almasirah_En"`` (or vice versa) instead of silently never firing."""
    s = channel_ref.strip()
    if s.startswith("telegram://"):
        s = s[len("telegram://"):]
    return s.lstrip("@")


# ---------------------------------------------------------------------------
# Config schema
# ---------------------------------------------------------------------------


class TelegramChannelSourceConfig(BaseModel):
    """Configuration for the ``telegram_channel`` source kind."""

    model_config = ConfigDict(extra="forbid")

    api_id_secret: str = Field(
        ...,
        description="Vault reference to the Telegram API ID (integer, "
                    "obtained from https://my.telegram.org). Resolved "
                    "per-call; never cached past a single pull.",
    )
    api_hash_secret: str = Field(
        ...,
        description="Vault reference to the Telegram API hash.",
    )
    session_secret: str = Field(
        ...,
        description="Vault reference to a base64-encoded Telethon session "
                    "file. The session was generated out-of-band by "
                    "running telethon_auth.py against a real Telegram "
                    "account; the handler decodes + materializes it to a "
                    "tempfile per pull.",
    )
    channels: list[str] = Field(
        default_factory=list,
        description="Channel handles or numeric IDs to monitor. Handles "
                    "may be prefixed with '@' or 'telegram://' — both are "
                    "stripped at resolve time.",
    )
    classes: dict[str, SourceClass] = Field(
        default_factory=dict,
        description="OPTIONAL per-channel source_class override (2026-07-29 "
                    "Ansar Allah decision). source_class is otherwise a "
                    "whole-descriptor field (SourceScope.source_class) — one "
                    "class for the entire batch of channels this descriptor "
                    "polls. A channel that needs a DIFFERENT editorial class "
                    "than the batch default — e.g. a Houthi/Ansar Allah "
                    "state-media voice riding the SAME session/account as a "
                    "batch classed `reporting` (a second concurrent "
                    "TelegramClient on one session triggers Telegram's "
                    "AUTH_KEY_DUPLICATED session-kill — see the module "
                    "docstring) — is listed here instead of forking a new "
                    "descriptor + account. Keyed by channel handle (same "
                    "normalization as `channels` — '@' / 'telegram://' "
                    "stripped, see `_strip_channel_prefix`); values are "
                    "choice-locked to the S1-T8 source_class vocabulary "
                    "(`legba.data.schemas.source.SourceClass`) — an "
                    "off-vocabulary value fails LOUD right here at config "
                    "validation (registration/activation), never silently "
                    "at signal-write time. A channel absent from this map "
                    "keeps the descriptor's `scope.source_class` default. "
                    "Read at the S-1 authority-stamping path — "
                    "`legba.data.analysts.signal_salience."
                    "_channel_class_override` — which prefers this override "
                    "over the descriptor default per-signal, keyed by the "
                    "message's own `payload.channel.username`.",
    )
    lookback_hours: int = Field(
        default=24,
        ge=1,
        description="When no cursor exists for a channel, pull messages "
                    "from the last N hours. Bounded so a misconfigured "
                    "lookback doesn't kick off a backfill storm.",
    )
    include_media: bool = Field(
        default=False,
        description="If True, attach media descriptor (type / mime / size) "
                    "to the signal payload. Bytes are NEVER downloaded by "
                    "this handler — downstream analysts fan out for OCR / "
                    "transcription as needed.",
    )
    per_channel_message_limit: int = Field(
        default=200,
        ge=1,
        le=1000,
        description="Hard cap on messages pulled per channel per pull in "
                    "the NORMAL (no-backlog) case, and the per-PAGE size "
                    "during bounded catch-up (see "
                    "catchup_max_pages_per_channel). Telethon's "
                    "iter_messages is paginated; this is a safety belt "
                    "against runaway pulls on a busy channel.",
    )
    catchup_max_pages_per_channel: int = Field(
        default=10,
        ge=1,
        le=100,
        description="Task #206 — bounded backlog catch-up. When a channel's "
                    "stored cursor is more than one page "
                    "(per_channel_message_limit) behind the newest message "
                    "(the source dropped backlog because the gap exceeded "
                    "the per-poll limit — e.g. the channel was unreachable "
                    "for a while), the handler walks FORWARD from the "
                    "cursor with min_id + reverse=True instead of a single "
                    "newest-first page, so no message in the gap is "
                    "skipped. Each page is still capped at "
                    "per_channel_message_limit; this field is the HARD cap "
                    "on how many such pages one channel may consume in ONE "
                    "poll, so a catastrophic backlog (e.g. a channel down "
                    "for weeks) can never flood a single poll — it drains "
                    "over successive polls instead, at up to "
                    "per_channel_message_limit * catchup_max_pages_per_"
                    "channel messages per poll. The cursor advances (and "
                    "persists) after every page, so a poll that hits this "
                    "cap resumes exactly where it left off next time — no "
                    "gap, no re-emission of the pages already drained.",
    )
    max_retries_per_channel: int = Field(
        default=5,
        ge=1,
        description="Exponential-backoff retry cap per channel per pull.",
    )
    backoff_base_seconds: float = Field(
        default=1.0,
        ge=0.0,
        description="Base for exponential backoff (1, 2, 4, 8, ...) "
                    "capped at 30s. 0 disables backoff (test-only).",
    )
    flood_wait_cap_seconds: int = Field(
        default=300,
        ge=1,
        description="Maximum FloodWait the handler will honor inline. "
                    "Longer waits are treated as a transient failure for "
                    "the channel and skipped to the next pull.",
    )
    auto_reconnect: bool = Field(
        default=False,
        description="Telethon transport auto-reconnect (H1). Default False: "
                    "a dropped/dead session fails the pull fast and our "
                    "per-channel retry loop reconnects deliberately, instead "
                    "of telethon spawning a background hot-loop that floods "
                    "the log at ~150 lines/sec on an expired session.",
    )
    connection_retries: int = Field(
        default=3,
        ge=0,
        description="Bounded telethon transport connect/request retries. "
                    "0 = a single attempt; a dead session then errors out "
                    "cleanly rather than looping.",
    )
    connect_retry_delay_seconds: float = Field(
        default=5.0,
        ge=0.0,
        description="Seconds between telethon transport connect retries "
                    "(never 0 in prod → no busy-loop).",
    )
    connect_timeout_seconds: float = Field(
        default=15.0,
        ge=1.0,
        description="Per-connect timeout handed to the telethon client.",
    )
    # --- A7 poller guards (worldmonitor parity) --------------------------
    # The five guards below are ADDITIVE hard stops around the existing
    # retry/backoff machinery. Every one of them logs when it trips.
    startup_delay_seconds: float = Field(
        default=60.0,
        ge=0.0,
        description="A7 guard — polls are SKIPPED until the process has been "
                    "up this long. On a container swap the old container's "
                    "MTProto connection can linger; a fresh client connecting "
                    "with the same session while the old one is alive trips "
                    "Telegram's AUTH_KEY_DUPLICATED, which permanently kills "
                    "the session. 0 disables (unit tests).",
    )
    per_channel_timeout_seconds: float = Field(
        default=15.0,
        gt=0.0,
        description="A7 guard — wall-clock budget for ONE channel within one "
                    "poll. A hung telethon request (no exception, socket "
                    "stalled) or an over-long walk trips the budget: the "
                    "channel is abandoned for THIS poll (cursor progress "
                    "already yielded/persisted stands) and the poll moves on. "
                    "Backoff/flood sleeps that cannot fit in the remaining "
                    "budget trip it early instead of sleeping past it.",
    )
    cycle_timeout_seconds: float = Field(
        default=180.0,
        gt=0.0,
        description="A7 guard — wall-clock cap for the WHOLE poll cycle "
                    "(all channels). When it expires, remaining channels are "
                    "deferred to the next poll (logged). Each channel's "
                    "deadline is additionally clamped to the cycle deadline.",
    )
    flood_wait_abort_seconds: int = Field(
        default=30,
        ge=0,
        description="A7 guard — FLOOD_WAIT early-abort. When Telegram's "
                    "FloodWaitError demands a wait LONGER than this, the "
                    "whole poll cycle aborts immediately (no inline sleep, "
                    "no further channels hammered) and the server's wait "
                    "deadline is persisted; subsequent polls are skipped "
                    "until it passes — the server's wait is honored ACROSS "
                    "polls instead of inside one. Waits <= this are still "
                    "honored inline (pre-existing behavior).",
    )
    poll_lock_stale_seconds: float = Field(
        default=300.0,
        gt=0.0,
        description="A7 guard — single-flight poll lock staleness. A poll "
                    "records a lock in the state store and clears it when "
                    "done; a second poll arriving while a FRESH lock is held "
                    "skips (overlap). A lock OLDER than this is presumed "
                    "left by a crashed poll and is force-cleared (logged). "
                    "Keep it comfortably above cycle_timeout_seconds.",
    )

    @field_validator("classes", mode="after")
    @classmethod
    def _normalize_classes_keys(
        cls, v: dict[str, SourceClass]
    ) -> dict[str, SourceClass]:
        """Normalize override keys the same way ``channels`` handles are
        normalized, so ``"@Almasirah_En"`` and ``"Almasirah_En"`` collide on
        the same entry instead of silently coexisting as two dead keys."""
        return {_strip_channel_prefix(k): c for k, c in v.items()}

    @model_validator(mode="after")
    def _classes_keys_must_reference_a_configured_channel(
        self,
    ) -> "TelegramChannelSourceConfig":
        """Fail loud at registration when an override names a channel that
        isn't in ``channels`` — a typo (or a channel later removed from
        ``channels`` without cleaning up its override) must not silently
        vanish; it should refuse validation instead."""
        known = {_strip_channel_prefix(c) for c in self.channels}
        unknown = sorted(set(self.classes) - known)
        if unknown:
            raise ValueError(
                "config.classes references channel(s) not present "
                f"in config.channels: {unknown}"
            )
        return self


# ---------------------------------------------------------------------------
# State + per-pull bookkeeping
# ---------------------------------------------------------------------------


_STATE_KEY = "telegram_cursor"  # value: { channel_handle: int (max message_id) }
# DQ-H5b (#88) — state-store key under which this handler records its poll
# health, so SourceActor._record_poll_outcome can read the WHY for a
# non-productive poll and turn a SWALLOWED systemic failure (a revoked session)
# into an honest 'error' outcome instead of a silent 'empty'. Mirrors the RSS
# handler's ``_RSS_HEALTH_KEY`` + ``health_state_key`` mechanism.
_HEALTH_KEY = "telegram_health"
# A7 guards — state-store keys. The poll lock makes polls single-flight per
# source actor (with stale force-clear); the floodwait key persists a server
# FLOOD_WAIT deadline so it is honored ACROSS polls after a cycle abort.
_LOCK_KEY = "telegram_poll_lock"            # value: {"acquired_at": epoch_s}
_FLOODWAIT_KEY = "telegram_floodwait_until"  # value: {"until": epoch_s}


@dataclass
class _PullStats:
    yielded: int = 0
    channels_ok: list[str] = field(default_factory=list)
    channels_failed: dict[str, str] = field(default_factory=dict)
    # Channels whose pull failed with a SESSION-level auth error (revoked /
    # expired session), tracked apart from transient give-ups so pull() can
    # tell a systemic dead session from a per-channel hiccup.
    channels_auth_failed: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None
    # A7 guards — channels not attempted this poll (cycle cap / flood abort)
    # and, when a FLOOD_WAIT cycle abort fired, the server-demanded wait.
    channels_deferred: list[str] = field(default_factory=list)
    flood_wait_abort_seconds: int | None = None


@dataclass
class _Deadline:
    """A7 guards — a monotonic wall-clock budget (per channel / per cycle)."""

    expires_at: float   # time.monotonic() basis
    label: str = ""

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

    def expired(self) -> bool:
        return self.remaining() <= 0.0


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class TelegramChannelSourceHandler:
    """Telegram channel source — L-102 §2 + L-136.

    Lifecycle:

      * ``on_configure`` parses config, resolves credentials, decodes the
        session blob to a tempfile.
      * ``on_activate`` connects the Telethon client and verifies
        authorization. Lazy by default — the smoke harness can also call
        ``pull`` directly with the client lazily constructed inside.
      * ``pull`` iterates configured channels, yielding Signals.
      * ``on_pause`` / ``on_retire`` disconnect cleanly and delete the
        materialized session file.

    Tests inject a ``client_factory`` to substitute a mock Telethon
    client; production omits it and the handler builds a real client.
    """

    # --- L-102 KindHandler identity --------------------------------------
    kind: ClassVar[str] = "telegram_channel"
    family: ClassVar[str] = "source"
    schema_version: ClassVar[str] = "legba/source.telegram_channel/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = TelegramChannelSourceConfig
    # DQ-H5b (#88) — state-store key the source actor reads to surface the WHY
    # of a non-productive poll (a revoked session → 'error', not silent 'empty').
    health_state_key: ClassVar[str] = _HEALTH_KEY

    # --- Construction ---------------------------------------------------
    def __init__(
        self,
        config: TelegramChannelSourceConfig | None = None,
        *,
        secret_resolver: Callable[[str], Awaitable[str]] | None = None,
        client_factory: Callable[..., Any] | None = None,
    ) -> None:
        # ``config`` is injected by the source factory (build_source_handler
        # unwraps the descriptor's property-factory shapes and validates the
        # config_schema, then passes the parsed instance to the ``config``
        # __init__ slot — the same proven path RSS/JSON/GeoJSON take). It is
        # optional so the smoke harness / unit tests can still construct the
        # handler bare and let ``on_configure`` parse ``ctx.config``.
        self._secret_resolver = secret_resolver
        self._client_factory = client_factory
        self._config: TelegramChannelSourceConfig | None = config
        self._client: Any = None
        self._session_path: str | None = None
        self._api_id: int | None = None
        self._api_hash: str | None = None
        self._last_success_at: datetime | None = None
        self._rows_pulled_24h: int = 0
        self._last_error: str | None = None

    # --- Lifecycle hooks (L-102 §1) -------------------------------------

    async def on_configure(self, ctx: Any) -> None:
        """Resolve config + credentials. Build session file on disk.

        Config precedence: a factory-injected ``self._config`` (the typed,
        already-unwrapped config from build_source_handler) wins. Only when
        it is absent — the bare-construct / direct-call path — do we parse
        ``ctx.config``. The runtime's ``ctx.config`` is a raw passthrough
        (``_RawConfig``, property-factory shapes NOT unwrapped), so parsing
        it for the factory path would fail validation; the guard below skips
        that parse entirely when the factory already supplied the config.
        Credentials are (re)resolved every call so a fresh per-pull handler
        always pulls live secrets — never cached past a single pull.
        """
        if self._config is None:
            cfg_raw = getattr(ctx, "config", None)
            if cfg_raw is None:
                raise ValueError("on_configure: ctx.config required")
            if isinstance(cfg_raw, TelegramChannelSourceConfig):
                cfg = cfg_raw
            else:
                cfg = TelegramChannelSourceConfig.model_validate(
                    cfg_raw.model_dump() if isinstance(cfg_raw, BaseModel) else cfg_raw
                )
            self._config = cfg

        cfg = self._config
        ctx_resolver = getattr(ctx, "secrets_resolve", None)
        api_id_raw = await _resolve_secret(
            cfg.api_id_secret,
            ctx_resolver=ctx_resolver,
            handler_resolver=self._secret_resolver,
        )
        api_hash = await _resolve_secret(
            cfg.api_hash_secret,
            ctx_resolver=ctx_resolver,
            handler_resolver=self._secret_resolver,
        )
        session_b64 = await _resolve_secret(
            cfg.session_secret,
            ctx_resolver=ctx_resolver,
            handler_resolver=self._secret_resolver,
        )

        try:
            self._api_id = int(api_id_raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"telegram api_id (vault {cfg.api_id_secret!r}) must be an integer"
            ) from exc
        self._api_hash = api_hash
        self._session_path = self._materialize_session(session_b64)

    async def on_activate(self, ctx: Any) -> None:
        """Establish (or lazily defer) the Telethon connection."""
        if self._client is not None:
            return  # idempotent
        self._client = await self._build_client()
        if self._client is None:
            return  # telethon missing — pull will no-op
        with suppress(Exception):
            await self._client.connect()

    async def on_pause(self, ctx: Any) -> None:
        await self._disconnect()

    async def on_resume(self, ctx: Any) -> None:
        await self.on_activate(ctx)

    async def on_retire(self, ctx: Any) -> None:
        await self._disconnect()
        self._tear_down_session()

    # --- Pull -----------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,
    ) -> AsyncIterator[Signal]:
        """Yield Signals from configured Telegram channels.

        Cursor handling is per-channel: we use
        ``max(stored_cursor[channel], since_lower_bound)`` to decide what
        to emit. Re-emission of overlapping windows is permitted.
        """
        # Lazy configure — the SourceActor poll path builds a fresh handler
        # per pull and drives ``pull`` directly without an explicit
        # ``on_configure``/``on_activate`` (mirrors scraper.py / discord.py,
        # which self-configure on first use). ``on_configure`` resolves the
        # vault secrets + materializes the session file; with config injected
        # by the factory it only needs to do the credential resolution here.
        # ``self._session_path is None`` is the "secrets not yet resolved"
        # signal (a fresh handler always starts None).
        if self._config is None or self._session_path is None:
            await self.on_configure(ctx)

        cfg = self._config
        if cfg is None:  # on_configure could not source a config — nothing to do
            raise RuntimeError("pull() called before on_configure")

        # A7 GUARD 1 — startup delay (AUTH_KEY_DUPLICATED mitigation). Skip the
        # poll entirely (no client connect) until the process has been up
        # ``startup_delay_seconds``, so a fresh client never connects with the
        # same session while a just-swapped-out container's connection lingers.
        if await self._guard_startup_delay(ctx, cfg):
            return

        # A7 GUARD 2 — cross-poll FLOOD_WAIT honoring. A prior cycle may have
        # aborted on a server FLOOD_WAIT and persisted a wait deadline; skip
        # polls (do not even connect) until it passes.
        if await self._guard_floodwait_skip(ctx):
            return

        # A7 GUARD 3 — single-flight poll lock with stale force-clear. A fresh
        # lock held by a still-running poll → skip (overlap). A lock older than
        # ``poll_lock_stale_seconds`` is presumed crashed and force-cleared.
        if not await self._acquire_poll_lock(ctx, cfg):
            return
        lock_held = True

        try:
            # Lazy client construction — the smoke harness can call pull
            # directly without an explicit on_activate.
            if self._client is None:
                self._client = await self._build_client()
                if self._client is not None:
                    with suppress(Exception):
                        await self._client.connect()

            if self._client is None:
                # Telethon unavailable — log once and yield nothing. This
                # mirrors the legacy ingestion path (`telegram.py`).
                ctx.logger.warning(
                    "telegram_channel: telethon not installed; pull is a no-op"
                )
                return

            cursors: dict[str, int] = dict(
                await ctx.state_store.get(_STATE_KEY) or {}
            )

            lower_bound = since or (
                datetime.now(tz=timezone.utc) - timedelta(hours=cfg.lookback_hours)
            )
            if lower_bound.tzinfo is None:
                lower_bound = lower_bound.replace(tzinfo=timezone.utc)

            stats = _PullStats()

            # A7 GUARD 4 — cycle-wide wall-clock cap. Each channel's own budget
            # (GUARD 5) is additionally clamped to this cycle deadline.
            cycle_deadline = _Deadline(
                expires_at=time.monotonic() + cfg.cycle_timeout_seconds,
                label="cycle",
            )

            for channel_ref in cfg.channels:
                handle = self._normalize_handle(channel_ref)

                # A7 GUARD 4 — cycle cap reached: defer the rest to next poll.
                if cycle_deadline.expired():
                    remaining = [
                        self._normalize_handle(c)
                        for c in cfg.channels[cfg.channels.index(channel_ref):]
                    ]
                    stats.channels_deferred.extend(remaining)
                    ctx.logger.warning(
                        "telegram_channel: cycle timeout %.0fs reached — "
                        "deferring %d channel(s) to next poll: %s",
                        cfg.cycle_timeout_seconds, len(remaining), remaining,
                    )
                    break

                # Task #206 — durable-across-pages cursor persistence. A
                # catch-up walk (see _pull_channel) can span MULTIPLE
                # Telethon pages within one `pull` call; this closure lets
                # it persist the cursor to the (crash-safe, Postgres-backed
                # in production) state_store after EACH page, not just once
                # at the very end of the whole multi-channel loop below. If
                # the process dies mid-catch-up, the next poll resumes from
                # the last-persisted page boundary — no drained page is
                # re-walked, no pending backlog message is skipped.
                #
                # Deliberately UNCONDITIONAL (no ">" guard against the outer
                # loop's own `cursors[handle] = mid` tracking below): by the
                # time a page's own persist_cursor() call runs, every signal
                # from that page has already been yielded UP to this outer
                # `async for sig in self._pull_channel(...)` loop and its
                # per-signal cursor bump has ALREADY executed (each `yield`
                # inside the catch-up walk suspends into this exact loop
                # body first) — so `new_cursor > cursors.get(_handle, 0)`
                # would always read False here and silently skip every
                # write. Persisting is idempotent (re-writing the current
                # value is harmless); the whole point is the WRITE ITSELF
                # landing durably once per page, not a redundant-write guard.
                async def _persist_page_cursor(new_cursor: int, *, _handle: str = handle) -> None:
                    if new_cursor > cursors.get(_handle, 0):
                        cursors[_handle] = new_cursor
                    await ctx.state_store.set(_STATE_KEY, dict(cursors))

                # A7 GUARD 5 — per-channel wall-clock budget, clamped to the
                # remaining cycle budget so one hung/slow channel can neither
                # overrun its own slice nor blow the whole cycle. Enforced by
                # ``_iter_with_deadline`` (times out each generator step) plus
                # the backoff/flood early-trip in ``_apply_backoff_or_giveup``.
                channel_budget = min(
                    cfg.per_channel_timeout_seconds, cycle_deadline.remaining()
                )
                channel_deadline = _Deadline(
                    expires_at=time.monotonic() + channel_budget,
                    label=f"channel:{handle}",
                )

                try:
                    async for sig in self._iter_with_deadline(
                        self._pull_channel(
                            ctx=ctx,
                            handle=handle,
                            since=lower_bound,
                            last_seen_id=int(cursors.get(handle, 0)),
                            cfg=cfg,
                            persist_cursor=_persist_page_cursor,
                        ),
                        deadline=channel_deadline,
                        handle=handle,
                    ):
                        stats.yielded += 1
                        # Track max message_id per channel for cursor advance.
                        mid = int(sig.payload.get("message_id", 0))
                        if mid > cursors.get(handle, 0):
                            cursors[handle] = mid
                        yield sig
                    stats.channels_ok.append(handle)
                except _CycleAbort as ab:
                    # A7 GUARD — abort the WHOLE cycle (cycle cap hit mid-walk,
                    # or a FLOOD_WAIT exceeding the abort threshold). Remaining
                    # channels are deferred; a flood abort persists the server
                    # wait deadline (below) so subsequent polls honor it.
                    idx = cfg.channels.index(channel_ref)
                    remaining = [self._normalize_handle(c) for c in cfg.channels[idx + 1:]]
                    stats.channels_deferred.extend(remaining)
                    stats.last_error = str(ab)
                    if ab.flood_wait_seconds is not None:
                        stats.flood_wait_abort_seconds = ab.flood_wait_seconds
                    ctx.logger.warning(
                        "telegram_channel: cycle abort on %s (%s) — deferring "
                        "%d channel(s) to next poll",
                        handle, ab, len(remaining),
                    )
                    break
                except _ChannelDeadlineExceeded as dl:
                    # A7 GUARD 5 — this channel exhausted its time budget. Treat
                    # like a per-channel give-up: abandon it for THIS poll (any
                    # already-yielded/persisted cursor progress stands), move on.
                    stats.channels_failed[handle] = str(dl)
                    stats.last_error = str(dl)
                    ctx.logger.warning(
                        "telegram_channel: %s per-channel timeout %.0fs — "
                        "abandoning for this poll: %s",
                        handle, channel_budget, dl,
                    )
                    continue
                except _ChannelAuthFailure as af:
                    # SESSION-level auth failure (revoked / expired session).
                    # Systemic — tracked apart from a transient give-up so the
                    # post-loop health decision can distinguish a dead session
                    # (every channel auth-fails) from a one-off channel hiccup.
                    stats.channels_auth_failed[handle] = str(af)
                    stats.last_error = str(af)
                    ctx.logger.warning(
                        "telegram_channel: %s SESSION auth failure "
                        "(session likely revoked/expired): %s",
                        handle, af,
                    )
                    # do not advance cursor for this channel
                    continue
                except _ChannelGiveUp as gu:
                    stats.channels_failed[handle] = str(gu)
                    stats.last_error = str(gu)
                    ctx.logger.warning(
                        "telegram_channel: %s give-up after retries: %s",
                        handle, gu,
                    )
                    # do not advance cursor for this channel
                    continue
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001 — last-ditch isolation
                    stats.channels_failed[handle] = repr(exc)
                    stats.last_error = repr(exc)
                    ctx.logger.warning(
                        "telegram_channel: %s unexpected failure: %r",
                        handle, exc,
                    )
                    continue

            await ctx.state_store.set(_STATE_KEY, cursors)
            # A7 GUARD 2 — persist the server FLOOD_WAIT deadline so the NEXT
            # poll (and any until it passes) is skipped, honoring the server's
            # wait ACROSS polls instead of sleeping it inside one.
            if stats.flood_wait_abort_seconds is not None:
                await self._persist_floodwait_deadline(
                    ctx, stats.flood_wait_abort_seconds
                )
            if stats.yielded > 0 or stats.channels_ok:
                self._last_success_at = datetime.now(tz=timezone.utc)
                self._rows_pulled_24h = stats.yielded  # last-pull approximation
            self._last_error = stats.last_error
            # FIX 1 (fail-loud) — record this pull's health under
            # ``health_state_key`` so a SWALLOWED systemic auth failure surfaces
            # HONESTLY as a source 'error'. Reached whenever the channel loop
            # runs to a natural conclusion; a revoked session yields zero
            # signals (no cap), so the generator never suspends at a yield and
            # this line always runs for that case.
            await self._record_pull_health(ctx, stats)
        finally:
            # A7 GUARD 3 — always release the single-flight poll lock we took
            # so a finished/aborted/closed poll never leaves a stale lock that
            # blocks the next one until the stale-clear window.
            if lock_held:
                await self._release_poll_lock(ctx)
            # H1: the SourceActor builds a FRESH handler per pull, so the
            # "held open across pulls" optimization never actually applies in
            # production — and leaving the client connected leaks a telethon
            # background reconnect task that hot-loops forever on a dead /
            # expired session (the H1 flood). Tear it down whenever the pull
            # generator finishes or is closed.
            await self._disconnect()

    async def _pull_channel(
        self,
        *,
        ctx: SourceContext,
        handle: str,
        since: datetime,
        last_seen_id: int,
        cfg: TelegramChannelSourceConfig,
        persist_cursor: Callable[[int], Awaitable[None]] | None = None,
    ) -> AsyncIterator[Signal]:
        """Pull one channel — dispatches cold-start vs. bounded catch-up.

        ``last_seen_id == 0`` (no stored cursor — first-ever pull for this
        channel) takes the ORIGINAL newest-first, ``since``-bounded, single
        page path UNCHANGED: that case is governed by ``lookback_hours``
        (how far back to backfill on first activation), not a dropped-gap —
        there is no cursor floor below which anything could have been lost.

        ``last_seen_id > 0`` (a cursor exists — this is a RESUMED poll) takes
        the #206 bounded catch-up path: page FORWARD from the cursor with
        ``min_id=last_seen_id, reverse=True`` (Telethon returns oldest-first
        starting just after ``min_id`` in this mode — see
        ``_pull_channel_catchup``) instead of a single newest-first page.
        This single mode correctly covers BOTH the everyday case (the gap
        since the last poll is smaller than one page — the walk yields one
        short page and stops, identical rows to the old code) AND the
        genuine catch-up case (the gap exceeds one page — the walk continues
        across bounded pages, capped by ``catchup_max_pages_per_channel``, so
        no message between the cursor and "now" is ever skipped).

        Both branches share the SAME retry/backoff/FloodWait/auth-failure
        policy (``_apply_backoff_or_giveup`` — a straight extraction of the
        pre-#206 inline logic, including the ``_ChannelAuthFailure`` fast
        path checked BEFORE FloodWait, unchanged)."""
        if last_seen_id <= 0:
            async for sig in self._pull_channel_cold_start(
                ctx=ctx, handle=handle, since=since, cfg=cfg,
            ):
                yield sig
            return

        async for sig in self._pull_channel_catchup(
            ctx=ctx, handle=handle, last_seen_id=last_seen_id, cfg=cfg,
            persist_cursor=persist_cursor,
        ):
            yield sig

    async def _pull_channel_cold_start(
        self,
        *,
        ctx: SourceContext,
        handle: str,
        since: datetime,
        cfg: TelegramChannelSourceConfig,
    ) -> AsyncIterator[Signal]:
        """First-ever pull for a channel — original newest-first walk,
        UNCHANGED from pre-#206 behavior. Bounded by ``lookback_hours`` /
        ``since``, one page of ``per_channel_message_limit`` messages."""
        entity = await self._resolve_entity_with_retry(ctx=ctx, handle=handle, cfg=cfg)
        channel_info = self._extract_channel_info(entity, handle)

        async def _fetch() -> AsyncIterator[Signal]:
            count = 0
            async for msg in self._client.iter_messages(
                entity, limit=cfg.per_channel_message_limit,
            ):
                if count >= cfg.per_channel_message_limit:
                    break
                # Older than lower bound → stop walking (newest-first).
                msg_date = self._normalize_msg_date(msg)
                if msg_date is not None and msg_date < since:
                    break
                msg_id = getattr(msg, "id", None)
                if msg_id is None:
                    continue
                count += 1
                yield self._to_signal(
                    msg=msg, channel_info=channel_info, ctx=ctx, cfg=cfg,
                )

        async for sig in self._run_with_retry(
            ctx=ctx, handle=handle, cfg=cfg, fetch=_fetch,
        ):
            yield sig

    async def _pull_channel_catchup(
        self,
        *,
        ctx: SourceContext,
        handle: str,
        last_seen_id: int,
        cfg: TelegramChannelSourceConfig,
        persist_cursor: Callable[[int], Awaitable[None]] | None,
    ) -> AsyncIterator[Signal]:
        """Task #206 — bounded forward catch-up: page with min_id + reverse.

        Walks OLDEST-to-newest starting just after ``last_seen_id``.
        Telethon's ``iter_messages(..., min_id=N, reverse=True)`` excludes
        messages with id <= N and returns ascending order (its docstring:
        "min_id becomes equivalent to offset_id ... since messages are
        returned in ascending order") — exactly the semantics needed to walk
        forward from a cursor without skipping anything.

        One page (one ``iter_messages`` call, retried independently via
        ``_run_with_retry``) at a time, each capped at
        ``per_channel_message_limit``. After every page that yields at least
        one message, the cursor advances to that page's max message id and
        is persisted immediately via ``persist_cursor`` — so a crash between
        pages loses no progress, and a poll that hits the page cap below
        resumes on the NEXT poll exactly where it left off (the persisted
        cursor already reflects every page fully drained this poll).

        Flood-cap: hard-stops after ``catchup_max_pages_per_channel`` pages
        regardless of remaining backlog — a catastrophic gap (a channel
        unreachable for weeks) can never flood a single poll. A short page
        (fewer messages than ``per_channel_message_limit``) means the walk
        has reached "now" — no need to burn the remaining page budget."""
        cursor = last_seen_id
        for page_num in range(1, cfg.catchup_max_pages_per_channel + 1):
            entity = await self._resolve_entity_with_retry(
                ctx=ctx, handle=handle, cfg=cfg,
            )
            channel_info = self._extract_channel_info(entity, handle)
            page_cursor = cursor

            async def _fetch(_cursor: int = cursor) -> AsyncIterator[Signal]:
                # Defensive explicit count guard — mirrors the cold-start
                # walk's own ``count >= limit: break`` (never trust
                # ``iter_messages(limit=...)`` alone to bound the yielded
                # count; the cold-start path never has, and a client whose
                # ``limit`` handling differs — e.g. a lazily-truncating or
                # simply non-conforming client — must not be able to smuggle
                # more than one page's worth past the flood-cap accounting
                # below, which counts on ``len(page_msgs)`` being an honest
                # per-page size).
                count = 0
                async for msg in self._client.iter_messages(
                    entity,
                    limit=cfg.per_channel_message_limit,
                    min_id=_cursor,
                    reverse=True,
                ):
                    if count >= cfg.per_channel_message_limit:
                        break
                    msg_id = getattr(msg, "id", None)
                    if msg_id is None:
                        continue
                    count += 1
                    yield self._to_signal(
                        msg=msg, channel_info=channel_info, ctx=ctx, cfg=cfg,
                    )

            page_msgs: list[Signal] = [
                sig async for sig in self._run_with_retry(
                    ctx=ctx, handle=handle, cfg=cfg, fetch=_fetch,
                )
            ]

            for sig in page_msgs:
                mid = int(sig.payload.get("message_id", 0))
                if mid > page_cursor:
                    page_cursor = mid
                yield sig

            if page_cursor > cursor:
                cursor = page_cursor
                if persist_cursor is not None:
                    # Unconditional call (not gated on comparing against the
                    # OUTER pull()-loop's own cursors[handle] tracking) —
                    # pull()'s per-signal `cursors[handle] = mid` update has
                    # ALREADY run for every message in this page by the time
                    # control returns here (each `yield sig` above suspends
                    # into that outer consuming loop, which updates its
                    # local `cursors` dict before resuming this generator).
                    # A gate comparing against that same dict would always
                    # see "already caught up" and silently never persist —
                    # persist_cursor's OWN internal state_store.set() is what
                    # must fire once per page; it is idempotent (re-writing
                    # the same already-current value is harmless).
                    await persist_cursor(cursor)

            if len(page_msgs) < cfg.per_channel_message_limit:
                return  # short page — caught up to "now"

            ctx.logger.info(
                "telegram_channel: %s catchup page %d/%d full (cursor now "
                "%d) — continuing",
                handle, page_num, cfg.catchup_max_pages_per_channel, cursor,
            )

        ctx.logger.warning(
            "telegram_channel: %s catchup hit the %d-page flood-cap "
            "(cursor at %d) — remaining backlog resumes next poll",
            handle, cfg.catchup_max_pages_per_channel, cursor,
        )

    async def _resolve_entity_with_retry(
        self, *, ctx: SourceContext, handle: str, cfg: TelegramChannelSourceConfig,
    ) -> Any:
        """Resolve the channel entity, retrying transient failures with the
        same backoff policy ``_run_with_retry`` applies to a page fetch."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await self._client.get_entity(handle)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._apply_backoff_or_giveup(
                    ctx=ctx, handle=handle, cfg=cfg, attempt=attempt, exc=exc,
                )

    async def _run_with_retry(
        self,
        *,
        ctx: SourceContext,
        handle: str,
        cfg: TelegramChannelSourceConfig,
        fetch: Callable[[], AsyncIterator[Signal]],
    ) -> AsyncIterator[Signal]:
        """Run ``fetch()`` (one bounded ``iter_messages`` walk) with the
        SAME exponential-backoff + FloodWait + auth-failure retry policy the
        pre-#206 code applied inline — extracted so both the cold-start walk
        and each catch-up page share one policy instead of two copies
        drifting apart. A retry re-runs ``fetch()`` from scratch (Telethon
        has no partial-page resume) — this matches the pre-#206 handler's own
        documented invariant ("re-emission of overlapping windows is
        allowed (dedupe is downstream, per KC-3 / L-151)"), not a new
        behavior; ``fetch`` closures are cheap to re-invoke — they open no
        state of their own beyond the bound ``min_id``/``limit``, which are
        unaffected by a mid-page retry (the caller only advances its cursor
        on a page that fully returns from this generator)."""
        attempt = 0
        while True:
            attempt += 1
            try:
                async for sig in fetch():
                    yield sig
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._apply_backoff_or_giveup(
                    ctx=ctx, handle=handle, cfg=cfg, attempt=attempt, exc=exc,
                )

    async def _apply_backoff_or_giveup(
        self,
        *,
        ctx: SourceContext,
        handle: str,
        cfg: TelegramChannelSourceConfig,
        attempt: int,
        exc: Exception,
    ) -> None:
        """Shared auth-failure/FloodWait/backoff policy — pre-#206 logic,
        UNCHANGED (same check ORDER: auth failure first, then FloodWait,
        then attempt-exhaustion backoff), extracted so the cold-start and
        catch-up retry loops share ONE copy. Returns normally to signal
        "retry"; raises :class:`_ChannelAuthFailure` (systemic,
        non-retryable — the whole session is revoked/expired) or
        :class:`_ChannelGiveUp` (per-channel, retries exhausted) to signal
        "abandon this channel for this poll" — any cursor/page progress
        already made (and, for catch-up, already persisted) stands; nothing
        already-yielded is undone."""
        auth_cls = _auth_error_classes()
        if auth_cls and isinstance(exc, auth_cls):
            # SESSION-level auth failure — the MTProto session is revoked /
            # expired / deauthorized. Retrying cannot fix it and only hammers
            # the API further (which is exactly what gets the account
            # bot-flagged). Fail this channel fast with a distinct,
            # NON-retryable signal so pull() can surface a dead session as an
            # honest error rather than a silent empty.
            raise _ChannelAuthFailure(f"session auth failed: {exc!r}") from exc
        fw_cls = _flood_wait_error_class()
        if fw_cls is not None and isinstance(exc, fw_cls):
            wait = getattr(exc, "seconds", 0) or 0
            # A7 GUARD — FLOOD_WAIT early-abort. When the server demands a wait
            # LONGER than the abort threshold, do NOT sleep it inline and do NOT
            # keep hammering the remaining channels with the same session (which
            # escalates the flood/ban risk). Abort the whole cycle; ``pull``
            # persists the server's wait deadline and skips polls until it
            # passes — honoring the server's wait ACROSS polls.
            if wait > cfg.flood_wait_abort_seconds:
                ctx.logger.warning(
                    "telegram_channel: %s FLOOD_WAIT %ds exceeds abort "
                    "threshold %ds — aborting cycle, honoring wait across polls",
                    handle, wait, cfg.flood_wait_abort_seconds,
                )
                raise _CycleAbort(
                    f"FLOOD_WAIT {wait}s exceeds abort threshold "
                    f"{cfg.flood_wait_abort_seconds}s",
                    flood_wait_seconds=wait,
                ) from exc
            if wait > cfg.flood_wait_cap_seconds:
                raise _ChannelGiveUp(
                    f"FloodWait {wait}s exceeds cap "
                    f"{cfg.flood_wait_cap_seconds}s"
                ) from exc
            ctx.logger.info(
                "telegram_channel: %s flood-wait %ds (attempt %d)",
                handle, wait, attempt,
            )
            await asyncio.sleep(wait)
            # FloodWait does not consume a retry budget — the API told us
            # exactly how long to wait, not that we failed.
            return
        if attempt >= cfg.max_retries_per_channel:
            raise _ChannelGiveUp(
                f"max retries ({cfg.max_retries_per_channel}) "
                f"exhausted: {exc!r}"
            ) from exc
        backoff = min(cfg.backoff_base_seconds * (2 ** (attempt - 1)), 30.0)
        ctx.logger.info(
            "telegram_channel: %s attempt %d failed (%r); backing off %.1fs",
            handle, attempt, exc, backoff,
        )
        await asyncio.sleep(backoff)
        # Drop and rebuild the client connection between retries on the
        # chance the underlying transport is unhealthy.
        with suppress(Exception):
            await self._client.disconnect()
        with suppress(Exception):
            await self._client.connect()

    # --- Health ---------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Connectivity + one-channel reachability probe.

        Healthy: client is connected (or able to connect) AND at least
        one configured channel resolves.
        Degraded: client connected but channel resolution failed (e.g.,
        Telegram returned ChannelPrivateError on the probed channel).
        Unhealthy: client cannot connect at all.
        """
        cfg = self._config
        detail: dict[str, Any] = {}
        try:
            if self._client is None:
                self._client = await self._build_client()
                if self._client is None:
                    return SourceHealth(
                        state="unhealthy",
                        last_success_at=self._last_success_at,
                        last_error="telethon not installed",
                        rows_pulled_24h=self._rows_pulled_24h,
                        detail={"telethon": False},
                    )
                with suppress(Exception):
                    await self._client.connect()

            connected = False
            with suppress(Exception):
                connected = bool(await self._client.is_connected())  # type: ignore[func-returns-value]
            if not connected:
                # Some telethon stubs expose `is_connected` as sync.
                with suppress(Exception):
                    connected = bool(self._client.is_connected())

            if not connected:
                return SourceHealth(
                    state="unhealthy",
                    last_success_at=self._last_success_at,
                    last_error="client not connected",
                    rows_pulled_24h=self._rows_pulled_24h,
                    detail={"connected": False},
                )
            detail["connected"] = True

            # Probe the first configured channel.
            if cfg and cfg.channels:
                probe = self._normalize_handle(cfg.channels[0])
                try:
                    await self._client.get_entity(probe)
                    detail["probed_channel"] = probe
                    detail["probe_ok"] = True
                except Exception as exc:  # noqa: BLE001
                    return SourceHealth(
                        state="degraded",
                        last_success_at=self._last_success_at,
                        last_error=f"channel probe failed: {exc!r}",
                        rows_pulled_24h=self._rows_pulled_24h,
                        detail={"probed_channel": probe, "probe_ok": False},
                    )

            return SourceHealth(
                state="healthy",
                last_success_at=self._last_success_at,
                last_error=self._last_error,
                rows_pulled_24h=self._rows_pulled_24h,
                detail=detail,
            )
        except Exception as exc:  # noqa: BLE001
            return SourceHealth(
                state="unhealthy",
                last_success_at=self._last_success_at,
                last_error=repr(exc),
                rows_pulled_24h=self._rows_pulled_24h,
                detail=detail,
            )

    async def _record_pull_health(
        self, ctx: SourceContext, stats: _PullStats,
    ) -> None:
        """Persist this pull's health under ``health_state_key`` (FIX 1).

        The source actor (``_record_poll_outcome``) reads this record and, for a
        NON-productive poll, turns ``degraded`` / ``unhealthy`` into an
        ``error`` outcome — the ONLY way a handler-SWALLOWED systemic failure
        (a revoked session, which the per-channel isolation above would
        otherwise return as a clean 0-yield 'empty') becomes visible to
        ``source_poll_outcomes`` + the liveness watchdog.

        Decision (SYSTEMIC vs per-channel):

          * ``unhealthy`` — auth failed AND no channel succeeded AND EVERY
            failure was an auth failure: the session is revoked/expired. Honest
            error, degrade-not-break (we still don't fabricate signals).
          * ``degraded`` — auth failures alongside successes / other failures:
            visible without over-claiming a full revocation.
          * ``healthy`` — no auth failures. A per-channel flood-wait, give-up,
            or genuinely empty channel is NOT a source-level error.
        """
        auth_failed = len(stats.channels_auth_failed)
        other_failed = len(stats.channels_failed)
        ok = len(stats.channels_ok)
        now = datetime.now(tz=timezone.utc)

        if auth_failed and ok == 0 and other_failed == 0:
            await self._record_health(
                ctx,
                state="unhealthy",
                last_error=(
                    f"telegram session auth failed on all {auth_failed} "
                    f"channel(s) — session likely revoked/expired: "
                    f"{stats.last_error}"
                ),
                detail={
                    "reason": "session_revoked",
                    "auth_failed_channels": sorted(stats.channels_auth_failed),
                },
            )
            return

        if auth_failed:
            await self._record_health(
                ctx,
                state="degraded",
                last_success_at=now if ok else None,
                last_error=f"telegram partial auth failures: {stats.last_error}",
                detail={
                    "reason": "partial_auth_failure",
                    "auth_failed_channels": sorted(stats.channels_auth_failed),
                    "channels_ok": ok,
                },
            )
            return

        await self._record_health(
            ctx,
            state="healthy",
            last_success_at=now if (stats.yielded or ok) else self._last_success_at,
            last_error=None,
            detail={
                "channels_ok": ok,
                "channels_failed": sorted(stats.channels_failed),
                "yielded": stats.yielded,
            },
        )

    async def _record_health(
        self,
        ctx: SourceContext,
        *,
        state: str,
        last_success_at: datetime | None = None,
        last_error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """Write a health record to the state store (mirror of rss._record_health)."""
        record = {
            "state": state,
            "last_success_at": (
                last_success_at.astimezone(timezone.utc).isoformat()
                if last_success_at is not None
                else None
            ),
            "last_error": last_error,
            "detail": detail or {},
        }
        try:
            await ctx.state_store.set(_HEALTH_KEY, record)
        except Exception:  # pragma: no cover
            ctx.logger.warning("telegram.health.persist_failed", exc_info=True)

    # --- A7 poller guards ----------------------------------------------

    async def _iter_with_deadline(
        self,
        agen: AsyncIterator[Signal],
        *,
        deadline: _Deadline,
        handle: str,
    ) -> AsyncIterator[Signal]:
        """A7 GUARD 5 — consume ``agen`` bounding EACH step by the remaining
        channel budget.

        Wrapping each ``__anext__`` in :func:`asyncio.wait_for` bounds BOTH a
        genuinely hung telethon request (a stalled socket that never returns —
        the boundary checks below could not catch it) AND an over-long walk /
        an inline backoff-or-flood sleep that overruns the budget (wait_for
        cancels the in-flight step). A timeout raises
        :class:`_ChannelDeadlineExceeded`; the caller abandons this channel for
        the poll. Control-flow signals (``_CycleAbort`` / ``_ChannelAuthFailure``
        / ``_ChannelGiveUp``) propagate UNCHANGED — only timeouts convert.
        """
        it = agen.__aiter__()
        try:
            while True:
                remaining = deadline.remaining()
                if remaining <= 0.0:
                    raise _ChannelDeadlineExceeded(
                        f"{handle}: channel budget exhausted before next message"
                    )
                try:
                    sig = await asyncio.wait_for(it.__anext__(), timeout=remaining)
                except StopAsyncIteration:
                    return
                except asyncio.TimeoutError as exc:
                    raise _ChannelDeadlineExceeded(
                        f"{handle}: channel step exceeded {deadline.label} budget"
                    ) from exc
                yield sig
        finally:
            # Close the wrapped generator so a mid-walk timeout/abort never
            # leaks a suspended telethon iterator (unclosed-generator warning).
            with suppress(Exception):
                await agen.aclose()

    async def _guard_startup_delay(
        self, ctx: SourceContext, cfg: TelegramChannelSourceConfig,
    ) -> bool:
        """A7 GUARD 1 — True (skip poll) when the process is still inside the
        startup-delay window (AUTH_KEY_DUPLICATED mitigation on container swap)."""
        delay = getattr(cfg, "startup_delay_seconds", 0.0) or 0.0
        if delay <= 0.0:
            return False
        elapsed = time.monotonic() - _PROCESS_STARTED_AT_MONOTONIC
        if elapsed < delay:
            ctx.logger.info(
                "telegram_channel: startup delay active (%.0fs of %.0fs "
                "elapsed) — skipping poll to avoid AUTH_KEY_DUPLICATED on a "
                "fresh session connect during container swap",
                elapsed, delay,
            )
            return True
        return False

    async def _guard_floodwait_skip(self, ctx: SourceContext) -> bool:
        """A7 GUARD 2 — True (skip poll) when a prior cycle persisted a server
        FLOOD_WAIT deadline that has not yet passed. Honors the server's wait
        ACROSS polls instead of sleeping it inline."""
        try:
            record = await ctx.state_store.get(_FLOODWAIT_KEY)
        except Exception:  # pragma: no cover — state store hiccup, fail open
            return False
        if not record:
            return False
        until = float(record.get("until", 0.0) or 0.0)
        now = time.time()
        if until > now:
            ctx.logger.warning(
                "telegram_channel: honoring persisted FLOOD_WAIT — %.0fs "
                "remaining before polls resume",
                until - now,
            )
            return True
        return False

    async def _persist_floodwait_deadline(
        self, ctx: SourceContext, wait_seconds: int,
    ) -> None:
        """A7 GUARD 2 — persist ``now + wait_seconds`` as the deadline until
        which polls are skipped (server FLOOD_WAIT honored across polls)."""
        with suppress(Exception):
            await ctx.state_store.set(
                _FLOODWAIT_KEY, {"until": time.time() + float(wait_seconds)}
            )

    async def _acquire_poll_lock(
        self, ctx: SourceContext, cfg: TelegramChannelSourceConfig,
    ) -> bool:
        """A7 GUARD 3 — single-flight poll lock with stale force-clear.

        Returns True when the lock is acquired (caller proceeds), False when a
        FRESH lock is already held (overlapping poll → caller skips). A lock
        OLDER than ``poll_lock_stale_seconds`` is presumed left by a crashed
        poll and force-cleared before acquiring (logged)."""
        try:
            record = await ctx.state_store.get(_LOCK_KEY)
        except Exception:  # pragma: no cover — fail open (acquire)
            record = None
        now = time.time()
        if record:
            acquired_at = float(record.get("acquired_at", 0.0) or 0.0)
            age = now - acquired_at
            if age < cfg.poll_lock_stale_seconds:
                ctx.logger.warning(
                    "telegram_channel: poll lock held (%.0fs old) — skipping "
                    "overlapping poll",
                    age,
                )
                return False
            ctx.logger.warning(
                "telegram_channel: force-clearing STALE poll lock (%.0fs old, "
                "threshold %.0fs) — prior poll presumed crashed",
                age, cfg.poll_lock_stale_seconds,
            )
        with suppress(Exception):
            await ctx.state_store.set(_LOCK_KEY, {"acquired_at": now})
        return True

    async def _release_poll_lock(self, ctx: SourceContext) -> None:
        """A7 GUARD 3 — clear the single-flight poll lock (best-effort)."""
        with suppress(Exception):
            await ctx.state_store.set(_LOCK_KEY, None)

    # --- Helpers --------------------------------------------------------

    async def _build_client(self) -> Any:
        """Build a Telethon client (or test factory's substitute)."""
        if self._client_factory is not None:
            client = self._client_factory(
                session_path=self._session_path,
                api_id=self._api_id,
                api_hash=self._api_hash,
            )
            if asyncio.iscoroutine(client):
                client = await client
            return client
        telethon = _telethon()
        if telethon is None:
            return None
        if self._session_path is None:
            raise RuntimeError(
                "_build_client called before session materialized"
            )
        _tame_telethon_loggers()
        cfg = self._config
        # H1: bound the transport reconnect so a dead/expired session cannot
        # busy-loop. `auto_reconnect=False` stops telethon's background
        # reconnect task from hot-looping; our per-channel retry (with
        # deliberate disconnect+reconnect) remains the reconnection path.
        auto_reconnect = getattr(cfg, "auto_reconnect", False) if cfg else False
        connection_retries = (
            getattr(cfg, "connection_retries", 3) if cfg else 3
        )
        retry_delay = (
            getattr(cfg, "connect_retry_delay_seconds", 5.0) if cfg else 5.0
        )
        connect_timeout = (
            getattr(cfg, "connect_timeout_seconds", 15.0) if cfg else 15.0
        )
        return telethon.TelegramClient(
            self._session_path,
            self._api_id,
            self._api_hash,
            auto_reconnect=auto_reconnect,
            connection_retries=connection_retries,
            request_retries=connection_retries,
            retry_delay=retry_delay,
            timeout=connect_timeout,
        )

    async def _disconnect(self) -> None:
        if self._client is None:
            return
        try:
            disc = self._client.disconnect
            result = disc() if not asyncio.iscoroutinefunction(disc) else await disc()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._client = None

    def _materialize_session(self, session_b64: str) -> str:
        """Decode the base64 session blob to a temp file.

        Telethon needs a session file on disk (it opens it as SQLite). We
        write to a private tempdir owned by this handler instance so two
        concurrent instances don't clobber each other.
        """
        tmp_dir = tempfile.mkdtemp(prefix="legba-tg-")
        path = os.path.join(tmp_dir, "telegram.session")
        # Empty session string is allowed — Telethon will create a fresh
        # one (useful for the new-account auth flow run out-of-band).
        if session_b64.strip():
            try:
                blob = base64.b64decode(session_b64, validate=True)
            except (ValueError, TypeError) as exc:
                raise ValueError(
                    "session_secret value is not valid base64"
                ) from exc
            with open(path, "wb") as fh:
                fh.write(blob)
            os.chmod(path, 0o600)
        return path

    def _tear_down_session(self) -> None:
        if not self._session_path:
            return
        with suppress(Exception):
            os.unlink(self._session_path)
        with suppress(Exception):
            os.rmdir(os.path.dirname(self._session_path))
        self._session_path = None

    @staticmethod
    def _normalize_handle(channel_ref: str) -> str:
        """Strip @-prefix and telegram:// scheme; keep numeric IDs intact."""
        return _strip_channel_prefix(channel_ref)

    @staticmethod
    def _normalize_msg_date(msg: Any) -> Optional[datetime]:
        d = getattr(msg, "date", None)
        if d is None:
            return None
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d

    def _extract_channel_info(self, entity: Any, fallback_handle: str) -> dict[str, Any]:
        """Best-effort extraction of (username, id, title) from a Telethon entity."""
        return {
            "username": getattr(entity, "username", None) or fallback_handle,
            "id": getattr(entity, "id", None),
            "title": getattr(entity, "title", None)
                or getattr(entity, "first_name", None)
                or fallback_handle,
        }

    def _to_signal(
        self,
        *,
        msg: Any,
        channel_info: dict[str, Any],
        ctx: SourceContext,
        cfg: TelegramChannelSourceConfig,
    ) -> Signal:
        text = getattr(msg, "text", None) or getattr(msg, "message", None) or ""
        msg_id = getattr(msg, "id", 0)
        published = self._normalize_msg_date(msg)
        views = getattr(msg, "views", None) or 0
        forwards = getattr(msg, "forwards", None) or 0

        media_type, media_descriptor = self._classify_media(msg, cfg)

        channel_id = channel_info.get("id")
        external_id = f"tg:{channel_id or channel_info['username']}:{msg_id}"
        canonical_url = (
            f"https://t.me/{channel_info['username']}/{msg_id}"
            if channel_info.get("username")
            else None
        )
        content_basis = f"{external_id}\n{text}".encode("utf-8", errors="replace")
        content_hash = hashlib.sha256(content_basis).hexdigest()

        payload: dict[str, Any] = {
            "external_id": external_id,
            "message_id": int(msg_id),
            "text": text,
            "published_at": published.isoformat() if published else None,
            "views": int(views),
            "forwards": int(forwards),
            "channel": {
                "username": channel_info.get("username"),
                "id": channel_info.get("id"),
                "title": channel_info.get("title"),
            },
            "media_type": media_type,
            # D5: a telegram message has NO inherent country. The old geo
            # enrichment derived one from the canonical URL's TLD — but that
            # host is `t.me` (Montenegro, `.me`), which tagged EVERY channel
            # message {ME}. Geo for a telegram signal must come from the
            # message BODY: `text` (above) feeds language_detect + the
            # ner_multilingual filter, whose place entities the geocode ladder
            # reads. This flag makes the "publisher origin is not story geo"
            # contract explicit for any downstream consumer.
            "publisher_origin_nongeo": True,
        }
        if cfg.include_media and media_descriptor is not None:
            payload["media"] = media_descriptor

        return Signal(
            signal_id=uuid4(),
            source_id=ctx.source_id,
            modality="text",
            fetched_at=datetime.now(tz=timezone.utc),
            payload=payload,
            content_hash=content_hash,
            canonical_url=canonical_url,
            language_hint=None,
            raw_provenance={
                "source_kind": "telegram_channel",
                "schema_version": self.schema_version,
                "channel_username": channel_info.get("username"),
                "channel_id": channel_info.get("id"),
                "message_id": int(msg_id),
            },
        )

    @staticmethod
    def _classify_media(
        msg: Any,
        cfg: TelegramChannelSourceConfig,
    ) -> tuple[str | None, dict[str, Any] | None]:
        """Return (media_type_name, descriptor_or_None).

        We never download bytes here — the descriptor is purely metadata
        (file_reference, mime, approx size) so downstream analysts can
        decide what to fetch.
        """
        media = getattr(msg, "media", None)
        if media is None:
            return None, None
        # Telethon exposes MessageMediaPhoto / MessageMediaDocument / Poll /
        # Web / Geo / Contact / Venue / Unsupported / ... Map the class name
        # to a short token; strip any leading punctuation and the
        # ``MessageMedia`` prefix wherever it appears.
        raw = type(media).__name__
        idx = raw.find("MessageMedia")
        if idx >= 0:
            stripped = raw[idx + len("MessageMedia"):]
        else:
            stripped = raw.lstrip("_")
        type_name = stripped.lower() or "unknown"
        if not cfg.include_media:
            return type_name, None

        descriptor: dict[str, Any] = {"type": type_name}
        # Best-effort introspection — works against both real telethon
        # objects and mock attribute bags.
        for attr in ("mime_type", "size", "file_reference", "duration", "w", "h"):
            val = getattr(media, attr, None)
            if val is None:
                # Some descriptors nest the doc one level down.
                doc = getattr(media, "document", None) or getattr(media, "photo", None)
                if doc is not None:
                    val = getattr(doc, attr, None)
            if val is not None:
                # bytes (file_reference) are not JSON-safe — base64.
                if isinstance(val, (bytes, bytearray)):
                    descriptor[attr] = base64.b64encode(bytes(val)).decode("ascii")
                else:
                    descriptor[attr] = val
        return type_name, descriptor


class _ChannelGiveUp(Exception):
    """Per-channel give-up signal — caught inside ``pull``."""


class _ChannelDeadlineExceeded(Exception):
    """A7 guard — a channel exhausted its per-channel/cycle time budget.

    Raised by :meth:`_apply_backoff_or_giveup` (a backoff/flood sleep that
    cannot fit the remaining budget) and by the per-channel deadline check in
    ``pull``. Caught per-channel like :class:`_ChannelGiveUp`: the channel is
    abandoned for THIS poll (already-yielded/persisted cursor progress stands)
    and the loop moves to the next channel.
    """


class _CycleAbort(Exception):
    """A7 guard — abort the ENTIRE poll cycle immediately.

    Two triggers: (a) the cycle-wide wall-clock cap expired, or (b) a
    FLOOD_WAIT longer than ``flood_wait_abort_seconds`` — we do NOT sleep it
    inline; the server's wait deadline is persisted and honored across polls.
    ``flood_wait_seconds`` is set for the FLOOD_WAIT trigger so ``pull`` can
    persist the cross-poll skip deadline.
    """

    def __init__(self, message: str, *, flood_wait_seconds: int | None = None) -> None:
        super().__init__(message)
        self.flood_wait_seconds = flood_wait_seconds


class _ChannelAuthFailure(Exception):
    """Per-channel SESSION-level auth failure — caught inside ``pull``.

    Distinct from :class:`_ChannelGiveUp`: an auth failure is NON-retryable and
    systemic (the whole session is revoked/expired), so pull() aggregates these
    separately and, when they cover every channel, records an ``unhealthy``
    health state → an honest ``error`` poll outcome (never a silent ``empty``).
    """


__all__ = [
    "TelegramChannelSourceConfig",
    "TelegramChannelSourceHandler",
]

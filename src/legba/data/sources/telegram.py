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
import os
import tempfile
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncIterator, Awaitable, Callable, ClassVar, Optional
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

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
        description="Hard cap on messages pulled per channel per pull. "
                    "Telethon's iter_messages is paginated; this is a "
                    "safety belt against runaway pulls on a busy channel.",
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


# ---------------------------------------------------------------------------
# State + per-pull bookkeeping
# ---------------------------------------------------------------------------


_STATE_KEY = "telegram_cursor"  # value: { channel_handle: int (max message_id) }


@dataclass
class _PullStats:
    yielded: int = 0
    channels_ok: list[str] = field(default_factory=list)
    channels_failed: dict[str, str] = field(default_factory=dict)
    last_error: str | None = None


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

        for channel_ref in cfg.channels:
            handle = self._normalize_handle(channel_ref)
            try:
                async for sig in self._pull_channel(
                    ctx=ctx,
                    handle=handle,
                    since=lower_bound,
                    last_seen_id=int(cursors.get(handle, 0)),
                    cfg=cfg,
                ):
                    stats.yielded += 1
                    # Track max message_id per channel for cursor advance.
                    mid = int(sig.payload.get("message_id", 0))
                    if mid > cursors.get(handle, 0):
                        cursors[handle] = mid
                    yield sig
                stats.channels_ok.append(handle)
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
        if stats.yielded > 0 or stats.channels_ok:
            self._last_success_at = datetime.now(tz=timezone.utc)
            self._rows_pulled_24h = stats.yielded  # last-pull approximation
        self._last_error = stats.last_error

    async def _pull_channel(
        self,
        *,
        ctx: SourceContext,
        handle: str,
        since: datetime,
        last_seen_id: int,
        cfg: TelegramChannelSourceConfig,
    ) -> AsyncIterator[Signal]:
        """Pull one channel with exponential-backoff retry."""
        attempt = 0
        while True:
            attempt += 1
            try:
                entity = await self._client.get_entity(handle)
                channel_info = self._extract_channel_info(entity, handle)

                count = 0
                async for msg in self._client.iter_messages(
                    entity,
                    limit=cfg.per_channel_message_limit,
                ):
                    if count >= cfg.per_channel_message_limit:
                        break
                    # Older than lower bound → stop walking (newest-first).
                    msg_date = self._normalize_msg_date(msg)
                    if msg_date is not None and msg_date < since:
                        break
                    # Cursor short-circuit: messages strictly newer than
                    # last_seen_id only.
                    msg_id = getattr(msg, "id", None)
                    if msg_id is None:
                        continue
                    if msg_id <= last_seen_id:
                        continue
                    count += 1
                    yield self._to_signal(
                        msg=msg,
                        channel_info=channel_info,
                        ctx=ctx,
                        cfg=cfg,
                    )
                return  # done with this channel
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                fw_cls = _flood_wait_error_class()
                if fw_cls is not None and isinstance(exc, fw_cls):
                    wait = getattr(exc, "seconds", 0) or 0
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
                    # FloodWait does not consume a retry budget — the API
                    # told us exactly how long to wait, not that we failed.
                    continue
                if attempt >= cfg.max_retries_per_channel:
                    raise _ChannelGiveUp(
                        f"max retries ({cfg.max_retries_per_channel}) "
                        f"exhausted: {exc!r}"
                    ) from exc
                backoff = min(
                    cfg.backoff_base_seconds * (2 ** (attempt - 1)),
                    30.0,
                )
                ctx.logger.info(
                    "telegram_channel: %s attempt %d failed (%r); "
                    "backing off %.1fs",
                    handle, attempt, exc, backoff,
                )
                await asyncio.sleep(backoff)
                # Drop and rebuild the client connection between retries
                # on the chance the underlying transport is unhealthy.
                with suppress(Exception):
                    await self._client.disconnect()
                with suppress(Exception):
                    await self._client.connect()
                continue

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
        return telethon.TelegramClient(
            self._session_path, self._api_id, self._api_hash
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
        s = channel_ref.strip()
        if s.startswith("telegram://"):
            s = s[len("telegram://"):]
        s = s.lstrip("@")
        return s

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


__all__ = [
    "TelegramChannelSourceConfig",
    "TelegramChannelSourceHandler",
]

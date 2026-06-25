# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATSClusterHandler — Phase-2 stack-component handler for NATS JetStream.

Implements the L-102 §1 `KindHandler` shape for the `nats` family. Wraps the
L-001 `legba.data.nats.NatsStore` async connection wrapper and exposes the
JetStream operations the runtime needs:

  * Stream management — `ensure_stream` (idempotent create/update).
  * Consumer management — `ensure_consumer` (idempotent durable consumer).
  * Publish / subscribe — `publish`, `subscribe`, `pull_subscribe`.
  * Key/value bucket access — `kv_get` / `kv_put` / `kv_delete` (with
    `ensure_kv_bucket`).
  * Lifecycle hooks — `on_configure`, `on_activate`, `on_pause`, `on_resume`,
    `on_retire` per L-102. Pause drains in-flight subscriptions; retire
    closes the underlying connection.
  * Healthcheck — `streams_info()` + `account_info()` returning a
    `HandlerHealth` per L-102.

Per-stream naming convention (see `legba_topology_redesign.md` §3 and L-101
output binding):

  * `legba.target.<target_id>.signals`  — target signal streams
  * `legba.analyst.<analyst_id>.findings` — analyst finding streams

Helpers `target_stream_name()` and `analyst_stream_name()` enforce the
convention; callers that need the raw nats subject pass a stream name
through directly.

Per L-101, the typed config is `NATSClusterConfig` (servers, optional
credentials Secret, jetstream enabled flag). The handler resolves the
credentials Secret (when present) at `on_configure` via the supplied
secret resolver; it never caches the resolved value across calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, ClassVar, Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field

try:
    import nats  # noqa: F401  (presence-guard mirroring data/nats.py)
    from nats.aio.client import Client as NATSClient  # noqa: F401
    from nats.aio.msg import Msg
    from nats.aio.subscription import Subscription as PushSubscription
    from nats.js import JetStreamContext
    from nats.js.api import (
        AckPolicy,
        ConsumerConfig,
        DeliverPolicy,
        KeyValueConfig,
        PubAck,
        RetentionPolicy,
        StorageType,
        StreamConfig,
    )
    from nats.js.errors import BucketNotFoundError, KeyNotFoundError, NotFoundError
    from nats.js.kv import KeyValue
except Exception:  # pragma: no cover — soft-fail when nats-py is missing
    nats = None  # type: ignore[assignment]
    Msg = Any  # type: ignore[assignment,misc]
    PushSubscription = Any  # type: ignore[assignment,misc]
    JetStreamContext = Any  # type: ignore[assignment,misc]
    KeyValue = Any  # type: ignore[assignment,misc]
    AckPolicy = None  # type: ignore[assignment]
    ConsumerConfig = None  # type: ignore[assignment]
    DeliverPolicy = None  # type: ignore[assignment]
    KeyValueConfig = None  # type: ignore[assignment]
    PubAck = Any  # type: ignore[assignment,misc]
    RetentionPolicy = None  # type: ignore[assignment]
    StorageType = None  # type: ignore[assignment]
    StreamConfig = None  # type: ignore[assignment]
    BucketNotFoundError = Exception  # type: ignore[assignment,misc]
    KeyNotFoundError = Exception  # type: ignore[assignment,misc]
    NotFoundError = Exception  # type: ignore[assignment,misc]

from ...config import NatsConfig
from ...nats import NatsStore
from ...schemas.stack import NATSClusterConfig

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Naming convention helpers
# ---------------------------------------------------------------------------
#
# Two parallel conventions:
#
#   - **subject pattern** (uses '.' as token separator, per NATS subject rules)
#     `legba.target.<target_id>.signals.>`
#     `legba.analyst.<analyst_id>.findings.>`
#
#   - **stream name** (NO '.', '*', '>', whitespace, or path separators allowed
#     in JetStream stream names) — same parts joined with '_':
#     `legba_target_<target_id>_signals`
#     `legba_analyst_<analyst_id>_findings`
#
# Callers ensuring a per-target stream pass the *stream name* and the
# *subject pattern* together — they're derived from the same target_id.

_TARGET_STREAM_NAME_TEMPLATE = "legba_target_{target_id}_signals"
_TARGET_SUBJECT_PREFIX_TEMPLATE = "legba.target.{target_id}.signals"
_ANALYST_STREAM_NAME_TEMPLATE = "legba_analyst_{analyst_id}_findings"
_ANALYST_SUBJECT_PREFIX_TEMPLATE = "legba.analyst.{analyst_id}.findings"


def target_stream_name(target_id: str) -> str:
    """Canonical per-target JetStream stream name (NATS-valid, no '.').

    Returns `legba_target_<target_id>_signals`. Use
    `target_subject_prefix()` for the subject pattern (which uses '.').
    """
    _validate_id_segment("target_id", target_id)
    return _TARGET_STREAM_NAME_TEMPLATE.format(target_id=target_id)


def target_subject_prefix(target_id: str) -> str:
    """Canonical per-target subject prefix (`legba.target.<id>.signals`).

    Append `.>` for a wildcard catch-all (`legba.target.<id>.signals.>`).
    """
    _validate_id_segment("target_id", target_id)
    return _TARGET_SUBJECT_PREFIX_TEMPLATE.format(target_id=target_id)


def analyst_stream_name(analyst_id: str) -> str:
    """Canonical per-analyst JetStream stream name (NATS-valid, no '.').

    Returns `legba_analyst_<analyst_id>_findings`.
    """
    _validate_id_segment("analyst_id", analyst_id)
    return _ANALYST_STREAM_NAME_TEMPLATE.format(analyst_id=analyst_id)


def analyst_subject_prefix(analyst_id: str) -> str:
    """Canonical per-analyst subject prefix (`legba.analyst.<id>.findings`)."""
    _validate_id_segment("analyst_id", analyst_id)
    return _ANALYST_SUBJECT_PREFIX_TEMPLATE.format(analyst_id=analyst_id)


def _validate_id_segment(field_name: str, value: str) -> None:
    if not value or not isinstance(value, str):
        raise ValueError(f"{field_name!r} must be a non-empty string")
    # Subject + stream tokens must not contain '.', '*', '>', whitespace,
    # or path separators per NATS subject and JetStream stream-name rules.
    disallowed = (" ", "\t", "\n", ".", "*", ">", "/", "\\")
    if any(c in value for c in disallowed):
        raise ValueError(
            f"{field_name!r} contains disallowed characters: {value!r}"
        )


# ---------------------------------------------------------------------------
# Local context shapes (L-102 §1; full runtime context lives in L-103).
# ---------------------------------------------------------------------------


@dataclass
class ConfigureContext:
    """Minimal slice of L-102 `ConfigureContext` the handler needs at
    `on_configure` time. The Phase-5 runtime context is a strict superset."""

    instance_id: str
    instance_version: str = "unversioned"
    config: NATSClusterConfig | None = None
    # Optional secret resolver — `(secret_id) -> bytes | None`. Used to
    # dereference the optional credentials Secret on the config.
    resolve_secret: Callable[[str], Awaitable[bytes | None]] | None = None
    logger: logging.Logger = field(default_factory=lambda: logger)


@dataclass
class RuntimeContext(ConfigureContext):
    """Runtime-phase context. Extended by Phase 5 with state-store, NATS
    handles, budget, tracer fields per L-103 — this slice is forward-
    compatible (every new field is added with a default)."""

    # Forward-compatible no-ops; Phase-5 runtime context will populate these.
    state_store: Any = None
    tracer: Any = None


# ---------------------------------------------------------------------------
# HandlerHealth — mirror of L-102 §1 shape.
# ---------------------------------------------------------------------------


@dataclass
class HandlerHealth:
    """L-102 `HandlerHealth` shape, repeated locally so the handler doesn't
    need to import the (future) runtime contract package."""

    state: Literal["healthy", "degraded", "unhealthy"]
    last_success_at: datetime | None
    last_error: str | None = None
    detail: Mapping[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Subscription bookkeeping
# ---------------------------------------------------------------------------


@dataclass
class SubscriptionHandle:
    """Tracker for one push or pull subscription owned by the handler.

    The handler keeps a list of these so `on_pause` / `on_retire` can drain
    them cleanly without the caller having to track every handle. Callers
    still receive the underlying subscription object for `fetch()` /
    `unsubscribe()` calls; the handler observes ownership only.
    """

    name: str
    kind: Literal["push", "pull"]
    subject: str | None
    stream: str | None
    durable: str | None
    sub: Any  # nats Subscription | PullSubscription
    created_at: datetime


# ---------------------------------------------------------------------------
# Handler config wrapper — links L-101 NATSClusterConfig + ad-hoc runtime opts.
# ---------------------------------------------------------------------------


class NATSClusterHandlerConfig(BaseModel):
    """Wrapper that pairs the L-101 typed `NATSClusterConfig` with handler-
    level runtime knobs not part of the persisted descriptor.

    Keeping the persisted descriptor (`NATSClusterConfig`) untouched preserves
    schema_uri stability — operator-facing config edits land in the descriptor
    registry, runtime-only knobs land here.
    """

    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    cluster_config: NATSClusterConfig
    # Optional secret resolver factory passed in via ConfigureContext.
    connect_timeout_seconds: int = Field(default=10, ge=1, le=120)
    # When the handler pauses, how long to wait for in-flight subs to drain.
    pause_drain_timeout_seconds: float = Field(default=5.0, ge=0.0, le=60.0)


# ---------------------------------------------------------------------------
# NATSClusterHandler
# ---------------------------------------------------------------------------


class NATSClusterHandler:
    """Stack-component handler for NATS clusters with JetStream.

    Conforms to L-102 §1 `KindHandler` (class-vars + lifecycle hooks +
    `health_check`). The handler does NOT subclass an abstract base — L-102
    uses structural `Protocol` typing so plugins authored outside this tree
    don't need to import a base class.
    """

    # --- L-102 §1 identity / registration ClassVars. ---
    kind: ClassVar[str] = "nats"
    family: ClassVar[str] = "stack"
    schema_version: ClassVar[str] = "legba/stack/nats/1.0.0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = NATSClusterConfig

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    def __init__(
        self,
        *,
        store: NatsStore | None = None,
        connect_timeout_seconds: int = 10,
        pause_drain_timeout_seconds: float = 5.0,
    ) -> None:
        if nats is None:  # pragma: no cover
            raise RuntimeError("nats-py is not installed")
        self._instance_id: str | None = None
        self._instance_version: str | None = None
        self._config: NATSClusterConfig | None = None
        self._store: NatsStore | None = store
        self._owns_store = store is None
        self._state: Literal[
            "draft", "configured", "active", "paused", "retired"
        ] = "draft"
        self._last_success_at: datetime | None = None
        self._last_error: str | None = None
        self._connect_timeout_seconds = connect_timeout_seconds
        self._pause_drain_timeout_seconds = pause_drain_timeout_seconds
        self._subscriptions: list[SubscriptionHandle] = []

    # ------------------------------------------------------------------
    # Accessors
    # ------------------------------------------------------------------

    @property
    def instance_id(self) -> str:
        if self._instance_id is None:
            raise RuntimeError("NATSClusterHandler not configured")
        return self._instance_id

    @property
    def lifecycle_state(self) -> str:
        return self._state

    @property
    def store(self) -> NatsStore:
        if self._store is None:
            raise RuntimeError("NATSClusterHandler not connected")
        return self._store

    @property
    def js(self) -> JetStreamContext:
        return self.store.js

    @property
    def subscriptions(self) -> list[SubscriptionHandle]:
        return list(self._subscriptions)

    # ------------------------------------------------------------------
    # Lifecycle hooks (L-102 §1)
    # ------------------------------------------------------------------

    async def on_configure(self, ctx: ConfigureContext) -> None:
        """Validate config, optionally resolve credentials, and verify the
        JetStream account is reachable.

        Raises RuntimeError if the JetStream account is unreachable so the
        descriptor lifecycle can refuse the configured→active transition.
        """
        if ctx.config is None:
            raise ValueError("ConfigureContext.config is required")
        self._instance_id = ctx.instance_id
        self._instance_version = ctx.instance_version
        self._config = ctx.config

        # Optionally resolve the credentials secret. We deliberately do NOT
        # cache the resolved bytes — only used to bootstrap the NatsStore
        # config below if creds_file isn't already set via env.
        creds_payload: bytes | None = None
        if (
            ctx.config.credentials is not None
            and ctx.config.credentials.raw
            and ctx.resolve_secret is not None
        ):
            try:
                creds_payload = await ctx.resolve_secret(ctx.config.credentials.raw)
            except Exception as exc:
                self._last_error = f"credential resolve failed: {exc}"
                raise RuntimeError(self._last_error) from exc

        # Build a NatsConfig from the descriptor servers list, unless the
        # caller already gave us a `store` in __init__ (test/inject pattern).
        if self._store is None:
            servers = [
                s for s in ctx.config.servers.raw if isinstance(s, str) and s
            ]
            url = servers[0] if servers else "nats://localhost:4222"
            nats_cfg = NatsConfig(
                url=url,
                connect_timeout=self._connect_timeout_seconds,
                creds_file=creds_payload.decode("utf-8") if creds_payload else None,
            )
            self._store = NatsStore(nats_cfg)

        # Connect and verify JetStream account.
        await self._store.connect()
        try:
            await self._store.js.account_info()
        except Exception as exc:
            self._last_error = f"jetstream account_info failed: {exc}"
            raise RuntimeError(self._last_error) from exc

        self._state = "configured"
        self._last_success_at = _now()
        self._last_error = None
        ctx.logger.info(
            "NATSClusterHandler configured instance_id=%s", self._instance_id
        )

    async def on_activate(self, ctx: RuntimeContext) -> None:
        """Transition to active. The connection is already open from
        `on_configure`; this is a marker for the lifecycle machine."""
        if self._state not in ("configured", "paused"):
            raise RuntimeError(
                f"on_activate from invalid state {self._state!r}"
            )
        if self._store is None or not self._store.nc.is_connected:
            # Reconnect on resume-after-disconnect.
            await self.store.connect()
        self._state = "active"
        self._last_success_at = _now()
        ctx.logger.info(
            "NATSClusterHandler activated instance_id=%s", self._instance_id
        )

    async def on_pause(self, ctx: RuntimeContext) -> None:
        """Gracefully drain in-flight subscriptions, then mark paused.

        Push subscriptions are drained via `Subscription.drain()` (waits for
        the dispatch queue to empty). Pull subscriptions are simply
        unsubscribed since they have no background dispatcher.
        """
        deadline = (
            asyncio.get_event_loop().time() + self._pause_drain_timeout_seconds
        )
        for handle in list(self._subscriptions):
            remaining = max(0.0, deadline - asyncio.get_event_loop().time())
            try:
                if handle.kind == "push":
                    await asyncio.wait_for(
                        handle.sub.drain(), timeout=remaining or 0.01
                    )
                else:
                    await asyncio.wait_for(
                        handle.sub.unsubscribe(), timeout=remaining or 0.01
                    )
            except (asyncio.TimeoutError, Exception) as exc:
                # Best-effort drain — log and keep going so one stuck sub
                # doesn't block the whole pause.
                logger.warning(
                    "pause-drain failed for sub %s: %s", handle.name, exc
                )
        self._subscriptions.clear()
        self._state = "paused"
        ctx.logger.info(
            "NATSClusterHandler paused instance_id=%s", self._instance_id
        )

    async def on_resume(self, ctx: RuntimeContext) -> None:
        """Resume from paused: re-activate. Existing subscriptions are not
        restored — the caller is expected to re-issue subscribe calls."""
        if self._state != "paused":
            raise RuntimeError(
                f"on_resume from invalid state {self._state!r}"
            )
        await self.on_activate(ctx)

    async def on_retire(self, ctx: RuntimeContext) -> None:
        """Terminal: drop subscriptions and close the connection."""
        # Best-effort drain remaining handles, regardless of state.
        if self._subscriptions:
            try:
                await self.on_pause(ctx)
            except Exception:
                logger.exception("error draining subs during retire")
            self._subscriptions.clear()
        if self._store is not None and self._owns_store:
            try:
                await self._store.close()
            except Exception:
                logger.exception("error closing NatsStore during retire")
        self._state = "retired"
        ctx.logger.info(
            "NATSClusterHandler retired instance_id=%s", self._instance_id
        )

    # ------------------------------------------------------------------
    # Healthcheck (L-102 §1 + §4 stack components per L-091/L-111 patterns)
    # ------------------------------------------------------------------

    async def health_check(self, ctx: RuntimeContext | None = None) -> HandlerHealth:
        """Probe the cluster via `streams_info()` + `account_info()`.

        - `unhealthy`: any underlying exception (connect, account_info fail).
        - `degraded`: connected + account_info ok, but `streams_info()` empty.
        - `healthy`: connected + account_info + at least one stream visible.

        Returns the L-102 `HandlerHealth` shape.
        """
        if self._store is None or self._store._nc is None:  # type: ignore[attr-defined]
            return HandlerHealth(
                state="unhealthy",
                last_success_at=self._last_success_at,
                last_error="not connected",
                detail={"connected": False},
            )
        try:
            account = await self._store.js.account_info()
            # streams_info() returns a list[StreamInfo]; iterator variant returns
            # an iterable (sync) from an async call.
            infos = await self._store.js.streams_info()
            streams: list[str] = [s.config.name for s in infos]
            state: Literal["healthy", "degraded", "unhealthy"] = (
                "healthy" if streams else "degraded"
            )
            self._last_success_at = _now()
            self._last_error = None
            return HandlerHealth(
                state=state,
                last_success_at=self._last_success_at,
                last_error=None,
                detail={
                    "streams_count": len(streams),
                    "streams": streams,
                    "memory": getattr(account, "memory", None),
                    "storage": getattr(account, "storage", None),
                    "domain": getattr(account, "domain", None),
                },
            )
        except Exception as exc:
            self._last_error = str(exc)
            return HandlerHealth(
                state="unhealthy",
                last_success_at=self._last_success_at,
                last_error=str(exc),
                detail={},
            )

    # ------------------------------------------------------------------
    # Stream / consumer management
    # ------------------------------------------------------------------

    async def ensure_stream(
        self,
        name: str,
        subjects: list[str],
        *,
        retention: Literal["limits", "interest", "workqueue"] = "limits",
        max_age_seconds: int | None = None,
        replicas: int = 1,
        storage: Literal["file", "memory"] = "file",
    ) -> bool:
        """Idempotently create/update a JetStream stream.

        Returns True if the stream was newly created, False if it already
        existed (and was updated when subjects/retention/replicas drifted).
        """
        if not name or not subjects:
            raise ValueError("ensure_stream requires non-empty name and subjects")
        retention_map = {
            "limits": RetentionPolicy.LIMITS,
            "interest": RetentionPolicy.INTEREST,
            "workqueue": RetentionPolicy.WORK_QUEUE,
        }
        storage_map = {
            "file": StorageType.FILE,
            "memory": StorageType.MEMORY,
        }
        # nats-py StreamConfig.max_age is in seconds (float); server stores ns.
        max_age_val = float(max_age_seconds) if max_age_seconds else 0.0
        cfg = StreamConfig(
            name=name,
            subjects=list(subjects),
            retention=retention_map[retention],
            max_age=max_age_val if max_age_val > 0 else None,
            num_replicas=int(replicas),
            storage=storage_map[storage],
        )
        try:
            existing = await self.js.stream_info(name)
        except NotFoundError:
            existing = None
        except Exception:
            # NotFoundError shape varies across nats-py versions; treat any
            # exception as "stream does not exist" and let add_stream surface
            # the real error if there's something else wrong.
            existing = None
        if existing is None:
            await self.js.add_stream(cfg)
            return True
        # Update if subjects / retention / replicas drifted.
        needs_update = (
            sorted(existing.config.subjects or []) != sorted(subjects)
            or existing.config.retention != retention_map[retention]
            or int(getattr(existing.config, "num_replicas", 1) or 1) != int(replicas)
            # max_age comes back from the server in nanoseconds (int);
            # compare against seconds*1e9 to detect drift.
            or int(getattr(existing.config, "max_age", 0) or 0)
            != int(max_age_val * 1_000_000_000)
        )
        if needs_update:
            await self.js.update_stream(cfg)
        return False

    async def ensure_consumer(
        self,
        stream: str,
        durable_name: str,
        *,
        filter_subject: str | None = None,
        ack_wait_seconds: float = 30.0,
        max_deliver: int = 5,
        ack_policy: Literal["explicit", "all", "none"] = "explicit",
        deliver_policy: Literal["all", "last", "new"] = "all",
    ) -> bool:
        """Idempotently create/update a durable consumer on `stream`.

        Returns True if newly created, False if it already existed (and was
        updated when ack_wait/max_deliver/filter drifted).
        """
        if not stream or not durable_name:
            raise ValueError("ensure_consumer requires non-empty stream and durable_name")
        ack_policy_map = {
            "explicit": AckPolicy.EXPLICIT,
            "all": AckPolicy.ALL,
            "none": AckPolicy.NONE,
        }
        deliver_policy_map = {
            "all": DeliverPolicy.ALL,
            "last": DeliverPolicy.LAST,
            "new": DeliverPolicy.NEW,
        }
        cfg = ConsumerConfig(
            durable_name=durable_name,
            filter_subject=filter_subject,
            ack_wait=float(ack_wait_seconds),
            max_deliver=int(max_deliver),
            ack_policy=ack_policy_map[ack_policy],
            deliver_policy=deliver_policy_map[deliver_policy],
        )
        try:
            existing = await self.js.consumer_info(stream, durable_name)
        except NotFoundError:
            existing = None
        except Exception:
            existing = None
        if existing is None:
            await self.js.add_consumer(stream=stream, config=cfg)
            return True
        # Update on drift.
        existing_cfg = existing.config
        needs_update = (
            getattr(existing_cfg, "filter_subject", None) != filter_subject
            or float(getattr(existing_cfg, "ack_wait", 0) or 0)
            != float(ack_wait_seconds)
            or int(getattr(existing_cfg, "max_deliver", 0) or 0) != int(max_deliver)
            or getattr(existing_cfg, "ack_policy", None) != ack_policy_map[ack_policy]
        )
        if needs_update:
            # nats-py supports update_consumer via add_consumer with the same
            # durable name (it upserts). Catch and ignore "no change" errors.
            with suppress(Exception):
                await self.js.add_consumer(stream=stream, config=cfg)
        return False

    # ------------------------------------------------------------------
    # Publish / subscribe
    # ------------------------------------------------------------------

    async def publish(
        self,
        subject: str,
        payload: bytes | str | Mapping[str, Any],
        *,
        headers: Mapping[str, str] | None = None,
        timeout: float = 10.0,
    ) -> PubAck:
        """JetStream publish-with-ack. Returns the `PubAck` from nats-py.

        `payload` may be bytes, str, or a JSON-serializable mapping. Mappings
        and strings are encoded to UTF-8 JSON; bytes are passed through.
        """
        if not subject:
            raise ValueError("publish requires a non-empty subject")
        if isinstance(payload, (bytes, bytearray)):
            data = bytes(payload)
        elif isinstance(payload, str):
            data = payload.encode("utf-8")
        else:
            data = json.dumps(dict(payload)).encode("utf-8")
        kwargs: dict[str, Any] = {"timeout": timeout}
        if headers:
            kwargs["headers"] = dict(headers)
        return await self.js.publish(subject, data, **kwargs)

    async def subscribe(
        self,
        subject: str,
        callback: Callable[[Msg], Awaitable[None]],
        *,
        durable: str | None = None,
        stream: str | None = None,
        queue: str | None = None,
        manual_ack: bool = True,
    ) -> SubscriptionHandle:
        """Push-based JetStream subscription. Each message invokes `callback`.

        The returned `SubscriptionHandle` keeps a reference so `on_pause` can
        drain. Callers that need to unsubscribe early may call
        `await handle.sub.unsubscribe()` themselves.
        """
        if not subject:
            raise ValueError("subscribe requires a non-empty subject")
        sub = await self.js.subscribe(
            subject,
            cb=callback,
            durable=durable,
            stream=stream,
            queue=queue,
            manual_ack=manual_ack,
        )
        handle = SubscriptionHandle(
            name=durable or f"push-{subject}",
            kind="push",
            subject=subject,
            stream=stream,
            durable=durable,
            sub=sub,
            created_at=_now(),
        )
        self._subscriptions.append(handle)
        return handle

    async def pull_subscribe(
        self,
        stream: str,
        consumer: str,
        *,
        subject: str | None = None,
    ) -> SubscriptionHandle:
        """Pull-based subscription bound to an existing durable consumer.

        The caller drives delivery via `await handle.sub.fetch(batch, timeout)`.
        The handle is tracked for `on_pause` drain.
        """
        if not stream or not consumer:
            raise ValueError(
                "pull_subscribe requires non-empty stream and consumer"
            )
        sub = await self.js.pull_subscribe(
            subject=subject or "",
            durable=consumer,
            stream=stream,
        )
        handle = SubscriptionHandle(
            name=consumer,
            kind="pull",
            subject=subject,
            stream=stream,
            durable=consumer,
            sub=sub,
            created_at=_now(),
        )
        self._subscriptions.append(handle)
        return handle

    # ------------------------------------------------------------------
    # KV operations
    # ------------------------------------------------------------------

    async def ensure_kv_bucket(
        self,
        bucket: str,
        *,
        history: int = 1,
        ttl_seconds: float | None = None,
        max_value_size: int | None = None,
        replicas: int = 1,
    ) -> bool:
        """Idempotently create a JetStream KV bucket. Returns True if created."""
        if not bucket:
            raise ValueError("ensure_kv_bucket requires a non-empty bucket name")
        try:
            await self.js.key_value(bucket)
            return False
        except BucketNotFoundError:
            pass
        except Exception:
            # Older nats-py raises NotFoundError too — same handling.
            pass
        cfg = KeyValueConfig(
            bucket=bucket,
            history=int(history),
            ttl=float(ttl_seconds) if ttl_seconds else None,
            max_value_size=int(max_value_size) if max_value_size else None,
            replicas=int(replicas),
        )
        await self.js.create_key_value(cfg)
        return True

    async def kv_get(self, bucket: str, key: str) -> bytes | None:
        """Fetch a value from `bucket` by `key`. Returns None if not found."""
        kv = await self._open_kv(bucket)
        try:
            entry = await kv.get(key)
        except KeyNotFoundError:
            return None
        except Exception:
            # Newer nats-py raises a generic NotFoundError shape.
            return None
        return entry.value

    async def kv_put(
        self, bucket: str, key: str, value: bytes | str | Mapping[str, Any]
    ) -> int:
        """Set `key` -> `value` in `bucket`. Returns the new revision."""
        kv = await self._open_kv(bucket)
        if isinstance(value, (bytes, bytearray)):
            payload = bytes(value)
        elif isinstance(value, str):
            payload = value.encode("utf-8")
        else:
            payload = json.dumps(dict(value)).encode("utf-8")
        return await kv.put(key, payload)

    async def kv_delete(self, bucket: str, key: str) -> bool:
        """Delete a key. Returns True if the operation succeeded
        (idempotent — deleting a missing key still returns True)."""
        kv = await self._open_kv(bucket)
        try:
            await kv.delete(key)
            return True
        except KeyNotFoundError:
            return True
        except Exception:
            return True

    async def _open_kv(self, bucket: str) -> KeyValue:
        return await self.js.key_value(bucket)

    # ------------------------------------------------------------------
    # Convenience inspection
    # ------------------------------------------------------------------

    async def streams_info(self) -> list[dict[str, Any]]:
        """Return a list of stream descriptors as dicts (name + subjects)."""
        infos = await self.js.streams_info()
        out: list[dict[str, Any]] = []
        for s in infos:
            out.append(
                {
                    "name": s.config.name,
                    "subjects": list(s.config.subjects or []),
                    "messages": getattr(s.state, "messages", None),
                    "bytes": getattr(s.state, "bytes", None),
                }
            )
        return out

    async def account_info(self) -> dict[str, Any]:
        """Return the current JetStream account info as a dict."""
        a = await self.js.account_info()
        return {
            "memory": getattr(a, "memory", None),
            "storage": getattr(a, "storage", None),
            "streams": getattr(a, "streams", None),
            "consumers": getattr(a, "consumers", None),
            "domain": getattr(a, "domain", None),
        }


# ---------------------------------------------------------------------------
# L-102 §8 factory function
# ---------------------------------------------------------------------------


def handler() -> type[NATSClusterHandler]:
    """Module-level factory — returns the handler class for entry-point
    registration per L-102 §8 (Path 1, `legba.handlers` group)."""
    return NATSClusterHandler


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)

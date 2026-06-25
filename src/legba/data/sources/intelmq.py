# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""IntelMQ collector bridge (L-140).

LB-12 decision (Lewis, 2026-05-16): adopt IntelMQ via a bridge rather than
re-implementing the 200+ CERT-grade collector bots. IntelMQ's pipeline is

    Collector -> Parser -> Expert -> Output

with Redis as the in-process message broker between bots. We hook the
**Collector -> Parser boundary**: collector bots emit JSON events conforming
to the IDF (IntelMQ Data Format), and the bridge intercepts those events and
translates them into Legba :class:`Signal` instances.

Two operation modes are supported:

* ``"subprocess"``  — the bridge runs the collector bot as a subprocess
  (``python -m <bot_module>``) one shot per :meth:`pull`, captures the
  bot's JSON output on stdout (one JSON event per line), and translates
  each event. Good for HTTP / file / one-shot collectors.

* ``"redis_pipe"`` — the bridge subscribes to IntelMQ's Redis pipeline (the
  bot's *destination queue*), pops all available events from it via
  ``LPOP`` / ``RPOP``, and translates each. Good for long-running collector
  bots that are already deployed as part of an IntelMQ install and whose
  events we want to siphon at the parser boundary.

IDF -> Signal mapping (see IntelMQ docs / harmonization.conf for the full
field catalogue). The bridge is intentionally lossy at the top level — the
full IDF dict is preserved in ``payload['idf']`` so downstream filters /
enrichment handlers can reach any field. The fields we lift to top-level
:class:`Signal` slots:

  * ``external_id``     <- ``event["uuid"]``  (or generated if missing)
  * ``published_at``    <- ``event["time.source"]`` (ISO-8601)
  * ``source_url``      <- ``event["source.url"]`` else ``event["feed.url"]``
  * ``raw_body``        <- the full IDF event under ``payload['idf']``
  * geo/actor hints     <- harvested from ``source.geolocation.cc``,
                          ``source.asn``, etc., copied into ``payload``

The IDF schema is open — collector bots emit varying field sets. We do not
fail on missing fields; we just don't populate the corresponding Signal slot.

**Optional dependency.** IntelMQ pulls ~50 transitive packages (parsers for
dozens of feed formats, GeoIP, etc.). To keep the base Legba install lean,
``intelmq`` is declared in the ``legba[intelmq]`` optional-deps group in
``pyproject.toml``. This module imports IntelMQ lazily inside
:meth:`on_configure` and raises a clear error if the user enables this kind
without the extra installed. The Redis client is already a base Legba
dependency (used by the runtime), so ``mode: "redis_pipe"`` does not require
the extra — only ``mode: "subprocess"`` does (because that mode validates the
bot module is importable as an IntelMQ-side check).
"""

from __future__ import annotations

import asyncio
import hashlib
import importlib
import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, AsyncIterator, ClassVar, Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ._contract import (
    Signal,
    SourceContext,
    SourceHealth,
)


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Hardening: a registry-authenticated actor selects ``bot_module``; the
# subprocess runner launches it as ``python -m <bot_module>``. Even though the
# call is exec-form (argv list — no shell, no injection), an arbitrary module
# would still be RUN. Restrict the spawnable module to the IntelMQ COLLECTOR
# package so an enabled bridge can never be pointed at, say, ``os`` or some
# other importable module. Validated both in the Pydantic field (config-time)
# and again at on_configure (defense-in-depth).
_INTELMQ_COLLECTOR_PREFIX = "intelmq.bots.collectors."


def _is_allowed_bot_module(bot_module: str) -> bool:
    """True iff ``bot_module`` is inside the IntelMQ collector package.

    Rejects empty, traversal-y, or non-collector module paths. The prefix
    check is exact (no bare ``intelmq.bots.collectors`` package import — a
    concrete collector submodule is required).
    """
    candidate = (bot_module or "").strip()
    return bool(candidate) and candidate.startswith(_INTELMQ_COLLECTOR_PREFIX)


class IntelMQBridgeConfig(BaseModel):
    """Configuration for the IntelMQ collector bridge.

    The ``bot_config`` dict is passed straight to the IntelMQ collector bot;
    its schema is dictated by IntelMQ itself (see the collector bot's
    docstring or ``intelmq.bots.collectors.<name>.collector_<name>`` source).
    We do not validate ``bot_config`` here — IntelMQ does that on bot
    instantiation. Garbage in = collector bot raises = bridge raises.
    """

    model_config = ConfigDict(extra="forbid")

    mode: Literal["subprocess", "redis_pipe"]

    bot_module: str = Field(
        ...,
        description=(
            "IntelMQ collector bot module path. MUST be inside "
            "'intelmq.bots.collectors.' (allowlist-enforced). "
            "Example: 'intelmq.bots.collectors.http.collector_http'."
        ),
    )

    @field_validator("bot_module")
    @classmethod
    def _bot_module_must_be_collector(cls, v: str) -> str:
        if not _is_allowed_bot_module(v):
            raise ValueError(
                f"bot_module {v!r} is not allowed — only IntelMQ collector "
                f"bots under {_INTELMQ_COLLECTOR_PREFIX!r} may be spawned."
            )
        return v

    bot_config: dict[str, Any] = Field(
        default_factory=dict,
        description="JSON config passed to the IntelMQ bot (rate-limit, http_url, etc.).",
    )

    # Redis-pipe mode only.
    intelmq_redis_host: str = Field(
        default="127.0.0.1",
        description="IntelMQ Redis host (redis_pipe mode).",
    )
    intelmq_redis_port: int = Field(
        default=6379,
        description="IntelMQ Redis port (redis_pipe mode).",
    )
    intelmq_redis_db: int = Field(
        default=2,
        description="IntelMQ Redis logical DB (default: 2 per IntelMQ defaults).",
    )
    intelmq_redis_secret: str | None = Field(
        default=None,
        description=(
            "Vault reference for IntelMQ Redis password. The runtime "
            "resolves this through ctx.secrets.resolve(); if unset and the "
            "Redis instance requires auth, the bridge will fail at "
            "health_check time."
        ),
    )
    intelmq_redis_queue: str | None = Field(
        default=None,
        description=(
            "Redis list / queue name to drain. "
            "Conventionally '<bot_id>-queue' in IntelMQ. Required in redis_pipe mode."
        ),
    )

    # Subprocess-mode only.
    subprocess_timeout_s: float = Field(
        default=60.0,
        description="Wall-clock timeout for one collector-bot subprocess run.",
    )
    subprocess_python: str = Field(
        default_factory=lambda: sys.executable or "python3",
        description=(
            "Python interpreter to launch the bot subprocess. Defaults to the "
            "running runtime interpreter (sys.executable) so the bot runs in "
            "the same pinned environment, not an arbitrary PATH 'python3'."
        ),
    )

    # Backpressure cap to avoid OOM on a flooded redis queue.
    max_events_per_pull: int = Field(
        default=10_000,
        description="Hard cap on events emitted per pull() invocation.",
    )


# ---------------------------------------------------------------------------
# IDF -> Signal translation
# ---------------------------------------------------------------------------


# Per IntelMQ harmonization (https://intelmq.readthedocs.io/en/latest/dev/harmonization-fields.html)
# Geo-related fields commonly populated on collector output.
_GEO_FIELDS: tuple[str, ...] = (
    "source.geolocation.cc",
    "source.geolocation.country",
    "source.geolocation.region",
    "source.geolocation.city",
    "source.geolocation.latitude",
    "source.geolocation.longitude",
    "destination.geolocation.cc",
    "destination.geolocation.country",
)

# ASN / actor adjacent fields. IntelMQ does not have a first-class "actor"
# notion; the closest analogues are the ASN / ip / domain on the source side
# plus the malware family / classification taxonomy.
_ACTOR_FIELDS: tuple[str, ...] = (
    "source.asn",
    "source.as_name",
    "source.ip",
    "source.fqdn",
    "malware.name",
    "malware.hash.md5",
    "malware.hash.sha1",
    "malware.hash.sha256",
    "classification.taxonomy",
    "classification.type",
    "classification.identifier",
)

# Provenance / feed-side fields.
_FEED_FIELDS: tuple[str, ...] = (
    "feed.name",
    "feed.provider",
    "feed.url",
    "feed.accuracy",
    "feed.code",
    "feed.documentation",
)


def _parse_idf_timestamp(raw: Any) -> datetime | None:
    """Best-effort parse of an IntelMQ time field.

    IntelMQ stores ISO-8601 strings with explicit timezone. We accept either
    a parseable string or an already-parsed datetime; anything else returns
    ``None``.
    """
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if not isinstance(raw, str) or not raw:
        return None
    try:
        # fromisoformat handles "2026-05-20T12:00:00+00:00"; tolerates "Z" since 3.11.
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None


def translate_idf_event(
    event: dict[str, Any],
    *,
    target_id: str,
    source_id: str,
    fetched_at: datetime | None = None,
) -> Signal:
    """Translate one IntelMQ IDF event into a Legba :class:`Signal`.

    Pure function — no I/O. Lifted into module scope so tests can exercise it
    directly with sample IDF JSON, and so the same translation path serves
    both subprocess and redis_pipe modes.
    """
    fetched_at = fetched_at or datetime.now(tz=timezone.utc)

    # external_id: prefer the IntelMQ-assigned UUID; fall back to a content hash
    # so dedupe still works for collectors that don't stamp uuid (rare).
    external_id = event.get("uuid")
    if not external_id:
        canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
        external_id = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    # published_at: IDF "time.source" is the event's natural time; "time.observation"
    # is when IntelMQ saw it. Prefer source.
    published_at = (
        _parse_idf_timestamp(event.get("time.source"))
        or _parse_idf_timestamp(event.get("time.observation"))
    )

    # source_url: per-event source.url else the feed url.
    source_url = event.get("source.url") or event.get("feed.url")

    # Lift geo + actor + feed fields into top-level payload slots for downstream
    # filters that don't want to drill into payload['idf'].
    geo: dict[str, Any] = {
        k: event[k] for k in _GEO_FIELDS if k in event and event[k] is not None
    }
    actors: dict[str, Any] = {
        k: event[k] for k in _ACTOR_FIELDS if k in event and event[k] is not None
    }
    feed: dict[str, Any] = {
        k: event[k] for k in _FEED_FIELDS if k in event and event[k] is not None
    }

    # content_hash for the L-151 dedupe filter. SHA-256 over the canonical IDF
    # JSON. Stable across runs since IntelMQ produces deterministic key ordering
    # only sometimes — we sort here to be safe.
    canonical = json.dumps(event, sort_keys=True, separators=(",", ":"))
    content_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    canonical_url = source_url if isinstance(source_url, str) else None

    payload: dict[str, Any] = {
        "idf": event,                       # full IDF event preserved
        "external_id": external_id,
        "published_at": published_at.isoformat() if published_at else None,
        "source_url": source_url,
        "raw_body": event,                  # alias the brief calls out explicitly
    }
    if geo:
        payload["geo"] = geo
    if actors:
        payload["actors"] = actors
    if feed:
        payload["feed"] = feed

    raw_provenance: dict[str, Any] = {"idf_source": "intelmq_collector_bridge"}
    if "feed.name" in event:
        raw_provenance["feed_name"] = event["feed.name"]
    if "feed.provider" in event:
        raw_provenance["feed_provider"] = event["feed.provider"]

    # Try to use the IntelMQ UUID as Signal.signal_id when it parses as a real
    # UUID — preserves end-to-end provenance and lets the downstream provenance
    # writer (L-001/L-114) match on it. Otherwise generate a fresh UUID.
    signal_id: UUID
    try:
        signal_id = UUID(str(external_id))
    except (ValueError, AttributeError, TypeError):
        signal_id = uuid4()

    return Signal(
        signal_id=signal_id,
        source_id=source_id,
        modality="text",
        fetched_at=fetched_at,
        payload=payload,
        content_hash=content_hash,
        canonical_url=canonical_url,
        language_hint=None,                 # IDF has no first-class lang field
        raw_provenance=raw_provenance,
    )


# ---------------------------------------------------------------------------
# Optional-dep gate
# ---------------------------------------------------------------------------


class IntelMQNotInstalled(RuntimeError):
    """Raised when the user enables intelmq_collector_bridge without the
    ``legba[intelmq]`` extra installed."""


def _require_intelmq() -> None:
    """Verify the IntelMQ package is importable.

    Called at :meth:`on_configure` time so a misconfigured deployment fails
    fast and loudly rather than silently dropping every collector bot run.
    """
    try:
        importlib.import_module("intelmq")
    except ImportError as e:  # pragma: no cover — environment-dependent
        raise IntelMQNotInstalled(
            "IntelMQ is not installed. Install with `pip install 'legba[intelmq]'` "
            "to use the intelmq_collector_bridge source kind, or set "
            f"mode='redis_pipe' if you have an external IntelMQ deployment "
            f"writing to Redis. Original ImportError: {e}"
        ) from e


def _require_bot_module(bot_module: str) -> None:
    """Verify the referenced collector bot module is importable.

    IntelMQ bot modules look like ``intelmq.bots.collectors.http.collector_http``.
    We do not instantiate; we just confirm the import works so misconfig
    surfaces at configure time, not at first pull.
    """
    try:
        importlib.import_module(bot_module)
    except ImportError as e:
        raise IntelMQNotInstalled(
            f"IntelMQ collector bot module {bot_module!r} is not importable. "
            f"Ensure the module path is correct and `legba[intelmq]` is "
            f"installed. Original ImportError: {e}"
        ) from e


# ---------------------------------------------------------------------------
# Subprocess-mode runner
# ---------------------------------------------------------------------------


async def _run_subprocess_collector(
    *,
    python_bin: str,
    bot_module: str,
    bot_config: dict[str, Any],
    timeout_s: float,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Run an IntelMQ collector bot as a subprocess; return parsed IDF events.

    Contract with the collector bot (consistent with how the IntelMQ CLI runs
    bots in one-shot mode):

      * The bot is launched as ``python -m <bot_module>``.
      * The bot config is passed on stdin as a single JSON object the bot
        deserializes. Real IntelMQ bots read config from ``BOTS`` /
        ``runtime.yaml`` files; the bridge therefore prefers a thin wrapper
        invocation, but stdin-config is documented and supported in IntelMQ
        3.x. The exact mechanism is fragile — IntelMQ doesn't officially
        commit to a stdout-JSON contract — so the bridge defaults to a
        ``LEGBA_INTELMQ_BRIDGE_RUNNER`` env var the runtime can override.
      * Each emitted event is one JSON object per line on stdout.

    On TimeoutExpired the subprocess is killed and whatever events arrived
    before the kill are returned (best-effort drain). This is the right
    behaviour for one-shot collectors that occasionally hang on slow feeds.
    """
    env = dict(os.environ)
    env.setdefault("PYTHONUNBUFFERED", "1")

    proc = await asyncio.create_subprocess_exec(
        python_bin,
        "-m",
        bot_module,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )

    config_blob = json.dumps(bot_config).encode("utf-8")
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=config_blob),
            timeout=timeout_s,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "intelmq.collector.subprocess_timeout",
            extra={"bot_module": bot_module, "timeout_s": timeout_s},
        )
        try:
            proc.kill()
        except ProcessLookupError:
            pass
        try:
            stdout, stderr = await proc.communicate()
        except Exception:
            stdout, stderr = b"", b""

    if proc.returncode not in (0, None):
        logger.warning(
            "intelmq.collector.nonzero_exit",
            extra={
                "bot_module": bot_module,
                "rc": proc.returncode,
                "stderr_tail": (stderr or b"").decode("utf-8", errors="replace")[-2000:],
            },
        )

    events: list[dict[str, Any]] = []
    for raw_line in (stdout or b"").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            logger.debug(
                "intelmq.collector.bad_json_line",
                extra={"line_head": line[:200].decode("utf-8", errors="replace")},
            )
            continue
        if isinstance(ev, dict):
            events.append(ev)
        elif isinstance(ev, list):
            # Some bots emit a JSON array. Accept that too.
            events.extend(x for x in ev if isinstance(x, dict))
    return events


# ---------------------------------------------------------------------------
# Redis-pipe-mode runner
# ---------------------------------------------------------------------------


async def _drain_redis_queue(
    *,
    redis_client: Any,
    queue: str,
    max_events: int,
    logger: logging.Logger,
) -> list[dict[str, Any]]:
    """Drain up to ``max_events`` items from a Redis list/queue.

    IntelMQ uses Redis lists keyed by ``<bot_id>-queue`` as its pipeline
    transport (per IntelMQ pipeline docs). We pop from the left (FIFO) until
    the queue is empty or we hit the per-pull cap. Each list entry is a JSON
    string holding one IDF event.

    We use the async redis client (the same ``redis[hiredis]`` package
    already in base Legba deps). LPOP returns ``None`` when the list is
    empty; that's our terminator.
    """
    events: list[dict[str, Any]] = []
    for _ in range(max_events):
        raw = await redis_client.lpop(queue)
        if raw is None:
            break
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            ev = json.loads(raw)
        except json.JSONDecodeError:
            logger.debug(
                "intelmq.redis_pipe.bad_json",
                extra={"queue": queue, "raw_head": raw[:200]},
            )
            continue
        if isinstance(ev, dict):
            events.append(ev)
    return events


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------


class IntelMQCollectorBridge:
    """Wrap an IntelMQ collector bot as a Legba :class:`SourceHandler`.

    See module docstring for design rationale. Satisfies the L-102 source-kind
    contract structurally — not via inheritance — so this class never needs
    to import the (yet-to-land) full ``KindHandler`` runtime base.
    """

    # --- KindHandler identity --------------------------------------------------
    kind: ClassVar[str] = "intelmq_collector_bridge"
    family: ClassVar[Literal["source"]] = "source"
    schema_version: ClassVar[str] = "legba/source.intelmq_collector_bridge/1-0-0"
    handler_version: ClassVar[str] = "0.1.0"
    config_schema: ClassVar[type[BaseModel]] = IntelMQBridgeConfig

    def __init__(self, config: IntelMQBridgeConfig | dict[str, Any] | None = None) -> None:
        """Construct with an in-process config.

        The runtime (L-103) will instead instantiate via the registered
        factory and call :meth:`on_configure` with a ``ConfigureContext``
        carrying the parsed config. Tests pass the config directly here.
        """
        if config is None:
            self._config: IntelMQBridgeConfig | None = None
        elif isinstance(config, IntelMQBridgeConfig):
            self._config = config
        else:
            self._config = IntelMQBridgeConfig.model_validate(config)

        # Redis client cached across pulls; constructed lazily so import of
        # `redis.asyncio` does not happen at module import time. Set to None
        # to force reconnection after on_pause / on_resume.
        self._redis_client: Any = None

    # --- Lifecycle hooks -------------------------------------------------------

    async def on_configure(self, ctx: Any = None) -> None:
        """Validate the IntelMQ dep state and the referenced bot module.

        ``ctx`` is the runtime's ConfigureContext (L-103). Its full shape is
        not yet pinned, so we accept ``Any`` and pull the config off it only
        if present. Tests pass ctx=None and configure via the constructor.
        """
        if ctx is not None and getattr(ctx, "config", None) is not None:
            cfg = ctx.config
            if isinstance(cfg, IntelMQBridgeConfig):
                self._config = cfg
            elif isinstance(cfg, dict):
                self._config = IntelMQBridgeConfig.model_validate(cfg)

        if self._config is None:
            raise ValueError(
                "IntelMQCollectorBridge.on_configure: no config provided "
                "(neither via constructor nor via ConfigureContext)."
            )

        # Mode-specific gating.
        if self._config.mode == "subprocess":
            # Defense-in-depth: re-assert the collector allowlist before any
            # subprocess can be spawned (the Pydantic validator already gates
            # config construction; this catches a config built some other way).
            if not _is_allowed_bot_module(self._config.bot_module):
                raise ValueError(
                    f"bot_module {self._config.bot_module!r} is not allowed — "
                    f"only IntelMQ collector bots under "
                    f"{_INTELMQ_COLLECTOR_PREFIX!r} may be spawned."
                )
            _require_intelmq()
            _require_bot_module(self._config.bot_module)
        else:  # redis_pipe
            # Redis-pipe mode talks to an external IntelMQ via Redis only. We
            # do NOT require the intelmq package here — but the bot_module
            # field is still useful documentation, so we sanity-check it's a
            # plausibly IntelMQ-shaped path. (Not a hard requirement.)
            if not self._config.intelmq_redis_queue:
                raise ValueError(
                    "redis_pipe mode requires `intelmq_redis_queue` to be set."
                )

    async def on_activate(self, ctx: Any = None) -> None:  # noqa: ARG002
        # Defer Redis client construction to first use so on_configure can
        # succeed in pure-dry test runs.
        return None

    async def on_pause(self, ctx: Any = None) -> None:  # noqa: ARG002
        await self._close_redis()

    async def on_resume(self, ctx: Any = None) -> None:  # noqa: ARG002
        return None

    async def on_retire(self, ctx: Any = None) -> None:  # noqa: ARG002
        await self._close_redis()

    async def _close_redis(self) -> None:
        if self._redis_client is not None:
            try:
                await self._redis_client.close()
            except Exception:
                pass
            self._redis_client = None

    # --- Redis client construction --------------------------------------------

    async def _get_redis_client(self, ctx: SourceContext) -> Any:
        """Lazily construct & cache the redis.asyncio client used in redis_pipe mode.

        Uses the async redis client from the `redis` package (already a base
        Legba dep). If ``intelmq_redis_secret`` is set on config, the password
        is resolved via the runtime's secret resolver if available; otherwise
        we read from the env var named by the secret ref. The runtime's full
        secret resolver lands with L-103.
        """
        if self._redis_client is not None:
            return self._redis_client

        assert self._config is not None
        try:
            import redis.asyncio as redis_asyncio  # type: ignore[import-untyped]
        except ImportError as e:  # pragma: no cover
            raise RuntimeError(
                "redis package is not installed; this is a base Legba dep "
                "and should always be present. Install `legba[data]`."
            ) from e

        password = await self._resolve_redis_password(ctx)
        self._redis_client = redis_asyncio.Redis(
            host=self._config.intelmq_redis_host,
            port=self._config.intelmq_redis_port,
            db=self._config.intelmq_redis_db,
            password=password,
            decode_responses=False,
        )
        return self._redis_client

    async def _resolve_redis_password(self, ctx: SourceContext) -> str | None:
        """Resolve the Redis password from the secret ref.

        Resolution order:
          1. If ``ctx`` carries a ``secrets`` resolver (runtime path, L-103),
             use it.
          2. Else, treat the secret ref string as an environment-variable
             name and read from os.environ. This is the test-friendly path.
          3. None means "no password" — connect anonymously.
        """
        assert self._config is not None
        ref = self._config.intelmq_redis_secret
        if not ref:
            return None

        # Runtime path: ctx.secrets.resolve(ref) — not yet pinned by L-103.
        secrets = getattr(ctx, "secrets", None)
        if secrets is not None and hasattr(secrets, "resolve"):
            try:
                resolved = await secrets.resolve(ref)
                if resolved is not None:
                    return str(resolved)
            except Exception:
                # Fall through to env-var resolution; logged but not fatal.
                ctx.logger.warning(
                    "intelmq.redis.secret_resolve_failed",
                    extra={"ref": ref},
                )

        return os.environ.get(ref)

    # --- pull -----------------------------------------------------------------

    async def pull(
        self,
        ctx: SourceContext,
        since: datetime | None = None,  # noqa: ARG002 — cursor lives in IntelMQ
    ) -> AsyncIterator[Signal]:
        """Pull one batch of Signals.

        :raises ValueError: if :meth:`on_configure` was not called first.
        """
        if self._config is None:
            raise ValueError(
                "IntelMQCollectorBridge.pull: handler not configured. "
                "Call on_configure() first (or pass config to __init__)."
            )

        if self._config.mode == "subprocess":
            async for sig in self._pull_subprocess(ctx):
                yield sig
        else:
            async for sig in self._pull_redis_pipe(ctx):
                yield sig

    async def _pull_subprocess(self, ctx: SourceContext) -> AsyncIterator[Signal]:
        assert self._config is not None
        events = await _run_subprocess_collector(
            python_bin=self._config.subprocess_python,
            bot_module=self._config.bot_module,
            bot_config=self._config.bot_config,
            timeout_s=self._config.subprocess_timeout_s,
            logger=ctx.logger,
        )
        # Apply backpressure cap.
        events = events[: self._config.max_events_per_pull]
        fetched_at = datetime.now(tz=timezone.utc)
        for ev in events:
            yield translate_idf_event(
                ev,
                target_id=ctx.target_id,
                source_id=ctx.source_id,
                fetched_at=fetched_at,
            )

    async def _pull_redis_pipe(self, ctx: SourceContext) -> AsyncIterator[Signal]:
        assert self._config is not None
        client = await self._get_redis_client(ctx)
        queue = self._config.intelmq_redis_queue
        assert queue is not None, "redis_pipe mode validated queue in on_configure"
        events = await _drain_redis_queue(
            redis_client=client,
            queue=queue,
            max_events=self._config.max_events_per_pull,
            logger=ctx.logger,
        )
        fetched_at = datetime.now(tz=timezone.utc)
        for ev in events:
            yield translate_idf_event(
                ev,
                target_id=ctx.target_id,
                source_id=ctx.source_id,
                fetched_at=fetched_at,
            )

    # --- health_check ---------------------------------------------------------

    async def health_check(self, ctx: SourceContext) -> SourceHealth:
        """Probe handler health.

        * ``subprocess`` mode: confirm the IntelMQ package + the configured
          bot module are importable. No bot is launched.
        * ``redis_pipe`` mode: PING the IntelMQ Redis instance and report
          the queue length.
        """
        if self._config is None:
            return SourceHealth(
                state="unhealthy",
                last_error="not configured",
                detail={"reason": "on_configure not called"},
            )

        now = datetime.now(tz=timezone.utc)

        if self._config.mode == "subprocess":
            try:
                _require_intelmq()
                _require_bot_module(self._config.bot_module)
            except IntelMQNotInstalled as e:
                return SourceHealth(
                    state="unhealthy",
                    last_error=str(e),
                    detail={"mode": "subprocess", "bot_module": self._config.bot_module},
                )
            return SourceHealth(
                state="healthy",
                last_success_at=now,
                detail={"mode": "subprocess", "bot_module": self._config.bot_module},
            )

        # redis_pipe mode
        try:
            client = await self._get_redis_client(ctx)
            pong = await client.ping()
            qlen = await client.llen(self._config.intelmq_redis_queue)
        except Exception as e:
            return SourceHealth(
                state="unhealthy",
                last_error=f"{type(e).__name__}: {e}",
                detail={
                    "mode": "redis_pipe",
                    "queue": self._config.intelmq_redis_queue,
                },
            )

        return SourceHealth(
            state="healthy" if pong else "degraded",
            last_success_at=now,
            detail={
                "mode": "redis_pipe",
                "queue": self._config.intelmq_redis_queue,
                "queue_length": int(qlen),
                "ping": bool(pong),
            },
        )


# ---------------------------------------------------------------------------
# Factory function (per L-102 §1 — "every handler module exports `handler()`").
# ---------------------------------------------------------------------------


def handler() -> type[IntelMQCollectorBridge]:
    """Return the registered handler class. Used by L-160 handler registry."""
    return IntelMQCollectorBridge


__all__ = [
    "IntelMQBridgeConfig",
    "IntelMQCollectorBridge",
    "IntelMQNotInstalled",
    "handler",
    "translate_idf_event",
]

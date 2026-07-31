# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Healthcheck dispatch for stack components.

Each stack component kind maps to a probe coroutine that takes the typed
config object (post-credential-resolution) and returns a `StackComponentHealth`.
The dispatcher invokes the right probe by kind, caches results in memory,
runs a background loop on a configurable cadence, and publishes
`stack.component.health_changed.<kind>.<id>` whenever the latched state
changes.

Per L-102 §1 the analyst/source/filter contract uses `HandlerHealth` —
stack components carry the same shape (state + last_success_at + detail).
This module provides the kind-keyed probes themselves; the dispatcher is
not a `KindHandler` because stack components aren't "handlers" in the L-102
family sense (they're substrate dependencies the handlers use).

Per-kind probes:
  * `llm_provider`: 1-token completion ping.
  * `vector_store`: query collection info.
  * `embedding`: encode a 1-word string + verify dim.
  * `postgres`: SELECT 1.
  * `redis`: PING.
  * `nats`: streams_info().
  * `proxy_pool`: outbound test through the proxy (skipped when no
    credentials configured — returns 'unknown').

Probes are intentionally lightweight (no payload-shape checks). Phase 2
handlers (L-120, L-121, …) layer richer probes once they need them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Awaitable, Callable, Mapping, Protocol, runtime_checkable

from ..schemas.stack import (
    EmbeddingServiceConfig,
    LLMProviderConfig,
    NATSClusterConfig,
    NLPServiceConfig,
    PostgresClusterConfig,
    ProxyPoolConfig,
    RedisClusterConfig,
    SearchProviderConfig,
    VectorStoreConfig,
)
from .credentials import CredentialResolverProtocol, MissingSecretError
from .emitter import RegistryEventEmitter
from .stack_events import stack_event_payload, stack_health_subject

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class HealthState(str, Enum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNHEALTHY = "unhealthy"
    UNKNOWN = "unknown"


@dataclass
class StackComponentHealth:
    """Result of one health probe. Shape mirrors L-102 `HandlerHealth`."""

    component_id: str
    kind: str
    state: HealthState
    checked_at: datetime
    detail: str = ""
    last_success_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Resolved-config wrapper passed to each checker
# ---------------------------------------------------------------------------


@dataclass
class ResolvedConfig:
    """Typed config + a credential resolver for the probe to dereference
    `Property.Secret` references at probe time. Secrets remain in the vault;
    the probe asks for them only when it needs to make a network call."""

    config: Any
    resolver: CredentialResolverProtocol

    async def secret(self, factory_value) -> bytes | None:
        """Resolve a `Secret` factory value (or None) into raw bytes."""
        if factory_value is None:
            return None
        secret_id = getattr(factory_value, "raw", None)
        if not secret_id:
            return None
        try:
            return await self.resolver.resolve(secret_id)
        except MissingSecretError:
            return None


# ---------------------------------------------------------------------------
# Checker protocol + registry
# ---------------------------------------------------------------------------


@runtime_checkable
class StackHealthChecker(Protocol):
    """Single-kind probe surface."""

    kind: str

    async def check(
        self,
        component_id: str,
        resolved: ResolvedConfig,
    ) -> StackComponentHealth: ...


HEALTH_CHECKERS: dict[str, StackHealthChecker] = {}


def register_health_checker(checker: StackHealthChecker) -> None:
    """Register a checker for a kind. Last-write-wins (Phase-2 handlers can
    override the in-tree default by re-registering with the same kind)."""
    HEALTH_CHECKERS[checker.kind] = checker


# ---------------------------------------------------------------------------
# Concrete checkers — Phase-1 lightweight defaults.
# ---------------------------------------------------------------------------


def _now() -> datetime:
    return datetime.now(tz=timezone.utc)


def _tcp_reachable(host: str, port: int, timeout: float = 2.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _missing_auth_detail(
    cfg: LLMProviderConfig,
    *,
    key_ok: bool,
    user_ok: bool,
    pass_ok: bool,
) -> str:
    """Name the field that is ACTUALLY missing, not the one we probed first.

    The old text was a flat ``"api_key not in vault"``, which for a Basic-only
    component (``llm.verify.slm_8b``: no ``api_key`` field at all, authing with
    ``api_user``/``api_pass``) reported a healthy component unhealthy over a
    field it is not supposed to carry — and pointed the operator at the wrong
    vault entry. So the message is built from what the component DECLARES."""
    declares_bearer = cfg.api_key is not None
    declares_basic = cfg.api_user is not None and cfg.api_pass is not None
    missing: list[str] = []
    if declares_bearer and not key_ok:
        missing.append("api_key")
    if declares_basic:
        if not user_ok:
            missing.append("api_user")
        if not pass_ok:
            missing.append("api_pass")
    if missing:
        return f"{', '.join(missing)} not in vault"
    # Neither mode declared, or a half-declared Basic pair. The schema
    # validator forbids both shapes, so reaching here means the row predates
    # the validator or was written around it — say exactly that rather than
    # blaming a field the operator never configured.
    if cfg.api_user is not None or cfg.api_pass is not None:
        return (
            "incomplete basic-auth config: set BOTH api_user and api_pass "
            "(or an api_key)"
        )
    return "no auth field configured: set api_key (Bearer) or api_user+api_pass"


class LLMProviderChecker:
    kind = "llm_provider"

    async def check(self, component_id, resolved):
        cfg: LLMProviderConfig = resolved.config
        endpoint = cfg.api_endpoint.raw
        # Phase-1 probe: TCP reachability on the endpoint host:port. We do
        # NOT make a real completion call here because that consumes budget
        # against the real provider; the dedicated Phase-2 L-120 handler
        # owns the model-specific 1-token ping.
        host, port = _split_endpoint(endpoint, default_port=443)
        if not host:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"unparseable endpoint {endpoint!r}",
            )
        # Verify credential lookup works (don't dereference plaintext into logs).
        #
        # Auth is a SWITCH on this config (see LLMProviderConfig): a bearer
        # ``api_key`` OR the HTTP Basic ``api_user``/``api_pass`` pair, with at
        # least one required by the schema validator. This probe used to try
        # ONLY api_key, so every Basic-authing component reported UNHEALTHY
        # "api_key not in vault" while serving traffic perfectly — the probe
        # was wrong about the component, not the component about itself. Try
        # BOTH modes; either one resolving is a pass. ``resolved.secret``
        # already maps an absent field and a MissingSecretError to None.
        try:
            key_bytes = await resolved.secret(cfg.api_key)
            user_bytes = await resolved.secret(cfg.api_user)
            pass_bytes = await resolved.secret(cfg.api_pass)
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"credential resolve failed: {exc}",
            )
        has_bearer = key_bytes is not None
        has_basic = user_bytes is not None and pass_bytes is not None
        if not (has_bearer or has_basic):
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=_missing_auth_detail(
                    cfg,
                    key_ok=has_bearer,
                    user_ok=user_bytes is not None,
                    pass_ok=pass_bytes is not None,
                ),
            )
        # Basic wins when both resolve — mirrors LLMProviderHandler._auth_headers,
        # so the probe reports the mode the real calls will actually use.
        auth_mode = "basic" if has_basic else "bearer"
        reachable = _tcp_reachable(host, port)
        return StackComponentHealth(
            component_id=component_id, kind=self.kind,
            state=HealthState.HEALTHY if reachable else HealthState.UNHEALTHY,
            checked_at=_now(),
            detail=f"tcp {host}:{port} reachable={reachable} auth={auth_mode}",
            last_success_at=_now() if reachable else None,
            extra={
                "endpoint": endpoint,
                "model": cfg.model_name.raw,
                "auth": auth_mode,
            },
        )


class VectorStoreChecker:
    kind = "vector_store"

    async def check(self, component_id, resolved):
        cfg: VectorStoreConfig = resolved.config
        from qdrant_client import AsyncQdrantClient
        endpoint = cfg.endpoint.raw
        host, port = _split_endpoint(endpoint, default_port=6333)
        client = AsyncQdrantClient(host=host, port=port)
        try:
            result = await client.get_collections()
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.HEALTHY, checked_at=_now(),
                detail=f"{len(result.collections)} collection(s)",
                last_success_at=_now(),
                extra={"collections": [c.name for c in result.collections]},
            )
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"get_collections failed: {exc}",
            )
        finally:
            try:
                await client.close()
            except Exception:
                pass


class EmbeddingChecker:
    kind = "embedding"

    async def check(self, component_id, resolved):
        cfg: EmbeddingServiceConfig = resolved.config
        endpoint = cfg.endpoint.raw
        host, port = _split_endpoint(endpoint, default_port=80)
        # Phase-1: TCP reachability + verify dim is in [64, 8192].
        reachable = _tcp_reachable(host, port) if host else False
        dim = int(cfg.dim.raw)
        ok_dim = 64 <= dim <= 8192
        state = HealthState.HEALTHY if reachable and ok_dim else (
            HealthState.UNHEALTHY if not reachable else HealthState.DEGRADED
        )
        return StackComponentHealth(
            component_id=component_id, kind=self.kind, state=state,
            checked_at=_now(),
            detail=f"tcp {host}:{port} reachable={reachable} dim={dim}",
            last_success_at=_now() if reachable else None,
            extra={"model_name": cfg.model_name.raw, "dim": dim},
        )


class PostgresChecker:
    kind = "postgres"

    async def check(self, component_id, resolved):
        import asyncpg
        cfg: PostgresClusterConfig = resolved.config
        password_bytes = await resolved.secret(cfg.password)
        if password_bytes is None:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail="vault missing password secret",
            )
        password = password_bytes.decode("utf-8")
        dsn = (
            f"postgresql://{cfg.user.raw}:{password}@"
            f"{cfg.host.raw}:{int(cfg.port.raw)}/{cfg.database.raw}"
        )
        try:
            conn = await asyncpg.connect(dsn, timeout=5.0)
            try:
                val = await conn.fetchval("SELECT 1")
                return StackComponentHealth(
                    component_id=component_id, kind=self.kind,
                    state=HealthState.HEALTHY, checked_at=_now(),
                    detail=f"SELECT 1 -> {val}", last_success_at=_now(),
                )
            finally:
                await conn.close()
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"connect/select failed: {exc}",
            )


class RedisChecker:
    kind = "redis"

    async def check(self, component_id, resolved):
        from redis.asyncio import Redis
        cfg: RedisClusterConfig = resolved.config
        password = None
        if cfg.password is not None:
            secret_bytes = await resolved.secret(cfg.password)
            password = secret_bytes.decode("utf-8") if secret_bytes else None
        # B-1: fall back to the substrate-wide requirepass (LEGBA_REDIS_PASSWORD)
        # when the descriptor carries no per-cluster secret.
        if password is None:
            password = (
                os.getenv("LEGBA_DATA_REDIS_PASSWORD")
                or os.getenv("LEGBA_REDIS_PASSWORD")
                or None
            )
        client = Redis(
            host=cfg.host.raw,
            port=int(cfg.port.raw),
            password=password,
            socket_connect_timeout=3.0,
            decode_responses=False,
        )
        try:
            pong = await client.ping()
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.HEALTHY if pong else HealthState.UNHEALTHY,
                checked_at=_now(), detail=f"PING -> {pong}",
                last_success_at=_now() if pong else None,
            )
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"ping failed: {exc}",
            )
        finally:
            try:
                await client.aclose()
            except Exception:
                try:
                    await client.close()
                except Exception:
                    pass


class NATSChecker:
    kind = "nats"

    async def check(self, component_id, resolved):
        import nats
        cfg: NATSClusterConfig = resolved.config
        servers = [s for s in cfg.servers.raw if isinstance(s, str)] or ["nats://localhost:4222"]
        # B-1: server-wide token authorization (LEGBA_NATS_TOKEN); empty/unset
        # connects unauthenticated (pre-cutover behaviour).
        connect_kwargs: dict[str, Any] = {"servers": servers, "connect_timeout": 3}
        token = os.getenv("LEGBA_NATS_TOKEN") or None
        if token:
            connect_kwargs["token"] = token
        try:
            nc = await nats.connect(**connect_kwargs)
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"connect failed: {exc}",
            )
        try:
            js = nc.jetstream()
            try:
                streams = await js.streams_info()
                state = HealthState.HEALTHY
                detail = f"jetstream {len(streams)} stream(s)"
            except Exception as exc:
                # JetStream might be disabled; that's degraded, not unhealthy.
                state = HealthState.DEGRADED
                detail = f"jetstream unavailable: {exc}"
            return StackComponentHealth(
                component_id=component_id, kind=self.kind, state=state,
                checked_at=_now(), detail=detail,
                last_success_at=_now() if state == HealthState.HEALTHY else None,
            )
        finally:
            try:
                await nc.drain()
            except Exception:
                pass
            try:
                await nc.close()
            except Exception:
                pass


class ProxyPoolChecker:
    kind = "proxy_pool"

    async def check(self, component_id, resolved):
        cfg: ProxyPoolConfig = resolved.config
        provider = cfg.provider.raw
        if provider == "none":
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.HEALTHY, checked_at=_now(),
                detail="provider=none (no-op)", last_success_at=_now(),
            )
        # For real provider integrations the Phase-2 L-123 handler owns the
        # outbound test. Phase-1 just verifies the credential is present.
        if cfg.credentials is None:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.DEGRADED, checked_at=_now(),
                detail=f"provider={provider} but no credentials configured",
            )
        secret_bytes = await resolved.secret(cfg.credentials)
        if secret_bytes is None:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail="vault missing proxy credentials",
            )
        return StackComponentHealth(
            component_id=component_id, kind=self.kind,
            state=HealthState.HEALTHY, checked_at=_now(),
            detail=f"provider={provider}, credentials resolved",
            last_success_at=_now(),
            extra={"provider": provider, "rotation": cfg.rotation.raw},
        )


class NLPServiceChecker:
    """Healthcheck for the hosted Legba-models NLP service.

    Issues ``GET /health`` over HTTPS with Basic Auth (when credentials are
    configured). The service returns ``{"status": "ok", ...}`` when up.
    A 401 means the service is reachable but our credentials are wrong;
    we treat that as ``degraded`` rather than ``unhealthy`` so the operator
    sees a distinct failure mode.
    """

    kind = "nlp_service"

    async def check(self, component_id, resolved):
        import httpx

        cfg: NLPServiceConfig = resolved.config
        endpoint = cfg.endpoint.raw.rstrip("/")
        health_path = getattr(cfg, "health_path", None)
        path = health_path.raw if health_path is not None else "/health"
        url = f"{endpoint}{path}"

        # Resolve creds (best-effort; internal docker path may be anon).
        auth = None
        try:
            user_bytes = await resolved.secret(cfg.api_user)
            pass_bytes = await resolved.secret(cfg.api_pass)
            if user_bytes and pass_bytes:
                auth = httpx.BasicAuth(
                    user_bytes.decode("utf-8"),
                    pass_bytes.decode("utf-8"),
                )
        except Exception as exc:                                # pragma: no cover
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"credential resolve failed: {exc}",
            )

        timeout = float(getattr(cfg.timeout_seconds, "raw", 60))
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, auth=auth)
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"GET {url} failed: {exc}",
            )

        if resp.status_code == 401:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.DEGRADED, checked_at=_now(),
                detail=f"GET {url} -> 401 (auth required / wrong creds)",
                extra={"endpoint": endpoint},
            )
        if resp.status_code != 200:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"GET {url} -> HTTP {resp.status_code}",
            )
        try:
            data = resp.json()
        except Exception:
            data = {}
        status = data.get("status", "?")
        ok = status == "ok"
        return StackComponentHealth(
            component_id=component_id, kind=self.kind,
            state=HealthState.HEALTHY if ok else HealthState.DEGRADED,
            checked_at=_now(),
            detail=f"GET {url} -> {status}",
            last_success_at=_now() if ok else None,
            extra={"endpoint": endpoint, "models_loaded": data.get("models_loaded")},
        )


# ---------------------------------------------------------------------------
# Endpoint parsing helpers
# ---------------------------------------------------------------------------


def _split_endpoint(endpoint: str, default_port: int) -> tuple[str | None, int]:
    """Parse `host:port` or `scheme://host:port[/path]` into `(host, port)`."""
    scheme, host, port = _split_full_endpoint(endpoint, default_port)
    return host, port


def _split_full_endpoint(
    endpoint: str, default_port: int
) -> tuple[str, str | None, int]:
    """Parse into `(scheme, host, port)`. Scheme defaults to 'http'."""
    if not endpoint:
        return "http", None, default_port
    scheme = "http"
    rest = endpoint
    if "://" in endpoint:
        scheme, rest = endpoint.split("://", 1)
    # Strip path / query
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if ":" in rest:
        host, _, port_str = rest.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        host = rest
        port = default_port
    return scheme, host or None, port


class SearchProviderChecker:
    """TCP reachability of the search endpoint. NEVER a real query.

    Rationale mirrors ``LLMProviderChecker``'s no-token-burn rule, with a
    different currency: a meta-search provider forwards every query to upstream
    engines that ban it for looking like a bot, so a query-per-poll healthcheck
    would spend the exact resource (upstream goodwill) that keeps the instance
    usable.

    HONEST LIMIT, recorded in ``extra['caveat']``: this probe reports HEALTHY
    while every upstream engine is banned, because the service genuinely IS up.
    The state that actually matters — "is it still returning results?" — is
    detectable only by a separate LOW-CADENCE canary (a fixed control query
    with a known-nonzero expected count, alerting on zero) wired into the
    watchdog cron, and by the per-response ``degraded`` flag the handler sets
    from ``unresponsive_engines``. Do not read HEALTHY here as "search works".
    """

    kind = "search_provider"

    async def check(self, component_id, resolved):
        cfg: SearchProviderConfig = resolved.config
        endpoint = cfg.endpoint.raw
        subprovider = cfg.subprovider.raw
        scheme, host, port = _split_endpoint_scheme(endpoint, default_port=8080)
        if not host:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"unparseable endpoint {endpoint!r}",
                extra={"subprovider": subprovider},
            )
        try:
            # Optional: keyless subproviders (searxng) resolve nothing here.
            await resolved.secret(cfg.api_key)
        except Exception as exc:
            return StackComponentHealth(
                component_id=component_id, kind=self.kind,
                state=HealthState.UNHEALTHY, checked_at=_now(),
                detail=f"credential resolve failed: {exc}",
                extra={"subprovider": subprovider},
            )
        reachable = _tcp_reachable(host, port)
        return StackComponentHealth(
            component_id=component_id, kind=self.kind,
            state=HealthState.HEALTHY if reachable else HealthState.UNHEALTHY,
            checked_at=_now(),
            detail=f"tcp {host}:{port} reachable={reachable}",
            last_success_at=_now() if reachable else None,
            extra={
                "endpoint": endpoint,
                "subprovider": subprovider,
                "scheme": scheme,
                "probe": "tcp_only",
                "caveat": (
                    "reachable != serving results; upstream engines can all be "
                    "banned while this reports healthy — see the control-query "
                    "canary, not this check"
                ),
            },
        )


def _split_endpoint_scheme(
    endpoint: str, default_port: int,
) -> tuple[str, str | None, int]:
    """``(scheme, host, port)`` — the 3-tuple variant this checker needs.

    ``_split_endpoint`` above returns ``(host, port)`` and defaults the port to
    ``default_port`` regardless of scheme; the search endpoint is commonly a
    plain ``http://host:8080/search``, so parse the scheme explicitly.
    """
    if not endpoint:
        return "https", None, default_port
    scheme = "https"
    rest = endpoint
    if "://" in endpoint:
        scheme, rest = endpoint.split("://", 1)
    rest = rest.split("/", 1)[0].split("?", 1)[0]
    if ":" in rest:
        host, _, port_str = rest.partition(":")
        try:
            port = int(port_str)
        except ValueError:
            port = default_port
    else:
        host = rest
        port = 80 if scheme == "http" else 443 if scheme == "https" else default_port
    return scheme, (host or None), port


# Register the in-tree default checkers eagerly.
for _checker in (
    LLMProviderChecker(), VectorStoreChecker(), EmbeddingChecker(),
    NLPServiceChecker(),
    PostgresChecker(), RedisChecker(),
    NATSChecker(), ProxyPoolChecker(),
    SearchProviderChecker(),
):
    register_health_checker(_checker)


# ---------------------------------------------------------------------------
# Dispatcher + background loop
# ---------------------------------------------------------------------------


@dataclass
class _CachedHealth:
    health: StackComponentHealth
    cached_at: float


class StackHealthDispatcher:
    """Holds a kind-keyed registry of checkers, runs them on demand, caches
    results in memory, and optionally drives a background poll loop that
    emits state-change events on a NATS emitter.

    The registry's per-component config is provided by the caller via
    `check_component(component_id, kind, typed_config, resolver)`; the
    dispatcher does not own the registry's PG connection.
    """

    def __init__(
        self,
        emitter: RegistryEventEmitter | None = None,
        *,
        poll_interval_seconds: int = 60,
    ):
        self._emitter = emitter
        self._poll_interval = poll_interval_seconds
        self._cache: dict[str, _CachedHealth] = {}
        self._loop_task: asyncio.Task | None = None

    async def check_component(
        self,
        component_id: str,
        kind: str,
        typed_config: Any,
        resolver: CredentialResolverProtocol,
    ) -> StackComponentHealth:
        """Run the kind's probe; cache + maybe emit state-change event."""
        checker = HEALTH_CHECKERS.get(kind)
        if checker is None:
            health = StackComponentHealth(
                component_id=component_id, kind=kind,
                state=HealthState.UNKNOWN, checked_at=_now(),
                detail=f"no checker registered for kind={kind!r}",
            )
        else:
            resolved = ResolvedConfig(config=typed_config, resolver=resolver)
            try:
                health = await checker.check(component_id, resolved)
            except Exception as exc:
                logger.exception("checker for %s raised", component_id)
                health = StackComponentHealth(
                    component_id=component_id, kind=kind,
                    state=HealthState.UNHEALTHY, checked_at=_now(),
                    detail=f"checker raised: {exc}",
                )

        prior = self._cache.get(component_id)
        self._cache[component_id] = _CachedHealth(health=health, cached_at=time.time())

        if self._emitter and (prior is None or prior.health.state != health.state):
            await self._emit_change(prior.health if prior else None, health)
        return health

    def cached(self, component_id: str) -> StackComponentHealth | None:
        c = self._cache.get(component_id)
        return c.health if c else None

    def all_cached(self) -> dict[str, StackComponentHealth]:
        return {k: v.health for k, v in self._cache.items()}

    async def _emit_change(
        self,
        prior: StackComponentHealth | None,
        current: StackComponentHealth,
    ) -> None:
        payload = stack_event_payload(
            action="health_changed",
            kind=current.kind,
            component_id=current.component_id,
            actor="system:health-loop",
            extra={
                "from_state": prior.state.value if prior else None,
                "to_state": current.state.value,
                "detail": current.detail,
            },
        )
        await self._emitter.publish(  # type: ignore[union-attr]
            stack_health_subject(current.kind, current.component_id), payload,
        )

    # ------------------------------------------------------------------
    # Background loop
    # ------------------------------------------------------------------

    async def run_loop(
        self,
        provider: Callable[[], Awaitable[list[tuple[str, str, Any, CredentialResolverProtocol]]]],
    ) -> None:
        """Poll loop. `provider` returns the *current* set of components to
        check on each tick — list of `(component_id, kind, typed_config, resolver)`.

        Cancel via `stop()` or task cancellation.
        """
        while True:
            try:
                components = await provider()
            except Exception:
                logger.exception("health-loop provider raised")
                components = []
            for cid, kind, cfg, resolver in components:
                try:
                    await self.check_component(cid, kind, cfg, resolver)
                except Exception:
                    logger.exception("health check failed for %s", cid)
            try:
                await asyncio.sleep(self._poll_interval)
            except asyncio.CancelledError:
                raise

    def start_loop(
        self,
        provider: Callable[[], Awaitable[list[tuple[str, str, Any, CredentialResolverProtocol]]]],
    ) -> asyncio.Task:
        if self._loop_task and not self._loop_task.done():
            return self._loop_task
        self._loop_task = asyncio.create_task(self.run_loop(provider))
        return self._loop_task

    async def stop(self) -> None:
        if self._loop_task and not self._loop_task.done():
            self._loop_task.cancel()
            try:
                await self._loop_task
            except asyncio.CancelledError:
                pass
        self._loop_task = None

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async factory for the hosted-NLP :class:`NlpServiceClient`.

Background
----------

The ``ner_multilingual`` + ``classify`` pipeline filters consume the
:class:`~legba.data.stack.nlp_service.NlpServiceClient` — an httpx-based
client to the hosted Legba-models service.  Construction needs three
things the filter handler can't do on its own:

  1. Fetch the ``nlp.local.legba_models`` stack-component row from
     the registry (carries the endpoint + per-path overrides + secret
     refs for Basic Auth).
  2. Resolve the ``api_user`` / ``api_pass`` secret refs through the
     :class:`~legba.data.registry.credentials.CredentialVault`.
  3. Instantiate the client and hand it to ``build_filter_handler`` via
     the ``nlp_client_factory`` closure.

The Dapr host's ``bring_up_production_runtime`` previously inlined this
as a lazy ``_build_nlp_client`` coroutine that the sync
``_nlp_client_factory()`` closure tried to look up out of an instance
dict.  That pattern raced — filter handler activation calls the
sync factory before the async builder has run, surfacing as
``RuntimeError("NlpServiceClient not yet built — call _build_nlp_client()
in an async context first")``.  Pre-awaiting at bootstrap fixes the race;
extracting the construction to a module makes it testable in isolation.

Construction is intentionally side-effect-light — we do NOT call
:meth:`NlpServiceClient.health` here.  Health is the actor's lifecycle
responsibility per L-102 §1 and a bootstrap-time probe would burn an
HTTP request before the filter is even wired.  The underlying
``httpx.AsyncClient`` is constructed lazily inside the client itself
(see :meth:`NlpServiceClient._ensure_client`) — the constructor doesn't
open a socket.

Config shape (registry side)
----------------------------

The stack-component body matches :class:`legba.data.schemas.stack.NLPService`::

    {
      "id": "nlp.local.legba_models",
      "version": "<sha256>",
      "body": {
        "schema_uri": "legba/stack/nlp_service/1.0.0",
        "config": {
          "endpoint":         {"factory_kind": "text",   "raw": "https://..."},
          "api_user":         {"factory_kind": "secret", "raw": "nlp.user"},
          "api_pass":         {"factory_kind": "secret", "raw": "nlp.pass"},
          "timeout_seconds":  {"factory_kind": "number", "raw": 60},
          "translate_path":   {"factory_kind": "text",   "raw": "/translate"},
          "classify_path":    {"factory_kind": "text",   "raw": "/classify"},
          "extract_path":     {"factory_kind": "text",   "raw": "/extract"},
          "summarize_path":   {"factory_kind": "text",   "raw": "/summarize"},
          "health_path":      {"factory_kind": "text",   "raw": "/health"}
        }
      }
    }

``api_user`` / ``api_pass`` may be absent (internal docker-network
deployments hit ``http://legba-models:8700`` with no auth).  Absent or
``raw: null`` skips the auth-resolve step and the client constructs
without a BasicAuth wrapper — matching the
:class:`NlpServiceClient` contract.

Errors
------

:class:`NlpClientFactoryError` is raised for every failure mode the
caller can act on:

  * 404 stack-component → "component not found in registry".
  * Missing / non-mapping ``body.config`` → schema-shape error.
  * One of ``api_user`` / ``api_pass`` present but missing its ``raw``
    secret id (asymmetric refs imply operator error).
  * ``endpoint`` missing or non-string after unwrap.
  * Underlying :class:`RegistryClientError` → wrapped with context.
  * Underlying vault resolve failure → wrapped with the secret id.

Construction errors propagate at bootstrap; the runtime fails loud
rather than silently degrading the filter set (per Lewis's "fail loud,
not silent" guidance).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from .registry_client import RegistryClientError, RegistryHTTPClient
from .source_factory import _unwrap_factory_dict

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..data.stack.nlp_service import NlpServiceClient


logger = logging.getLogger(__name__)


DEFAULT_NLP_COMPONENT_ID = "nlp.local.legba_models"


class LazyNlpClient:
    """Lazy, re-resolving holder for the hosted-NLP :class:`NlpServiceClient`.

    #91 §2.3: the client used to be built ONCE at host bootstrap and cached
    as a possibly-permanent ``None``. Boot-before-seed (the
    ``nlp.local.legba_models`` stack row not yet registered) or a transient
    models-host outage at boot pinned every later filter build to the
    degraded / unbuildable path for the whole process lifetime — recoverable
    only by a restart.

    This holder resolves on FIRST use and RE-resolves on every subsequent
    call while no client is cached, so a late seed or a recovered host heals
    the filter set on the next build attempt. A successful build is cached
    (one configured client reused across runs). A failed build is NEVER
    cached as a sticky ``None`` — the next :meth:`get` retries — and the
    underlying :class:`NlpClientFactoryError` is RAISED so the caller decides
    whether to degrade (best-effort enrichment) or fail loud (a descriptor
    that genuinely requires NER). The lock serialises concurrent first-use
    builds so a burst of resolutions doesn't fan out N registry round-trips.

    The wire contract is unchanged: :meth:`get` returns the same
    :class:`NlpServiceClient` the eager factory returned.
    """

    def __init__(
        self,
        *,
        registry_client: RegistryHTTPClient,
        secrets_resolve: Callable[[str], Awaitable[bytes]],
        component_id: str = DEFAULT_NLP_COMPONENT_ID,
    ) -> None:
        self._registry_client = registry_client
        self._secrets_resolve = secrets_resolve
        self._component_id = component_id
        self._client: "NlpServiceClient | None" = None
        self._lock = asyncio.Lock()

    @property
    def cached(self) -> "NlpServiceClient | None":
        """The currently-cached client, or ``None`` if not yet resolved."""
        return self._client

    async def get(self) -> "NlpServiceClient":
        """Return the cached client, building (or re-building) it on demand.

        Raises :class:`NlpClientFactoryError` on a build failure WITHOUT
        caching the failure — the next call retries.
        """
        cached = self._client
        if cached is not None:
            return cached
        async with self._lock:
            # Double-check under the lock — a concurrent waiter may have built it.
            cached = self._client
            if cached is not None:
                return cached
            client = await build_nlp_client_from_stack_component(
                self._component_id,
                registry_client=self._registry_client,
                secrets_resolve=self._secrets_resolve,
            )
            self._client = client
            logger.info(
                "nlp_client_factory.lazy_built component_id=%s",
                self._component_id,
            )
            return client


class NlpClientFactoryError(RuntimeError):
    """Surface for nlp-client construction failures.

    Raised by :func:`build_nlp_client_from_stack_component` when the
    registry lookup, config shape, secret resolution, or client
    instantiation fails.  Distinct from :class:`RegistryClientError` so
    callers at the host bootstrap can distinguish "registry is down"
    from "the stack component is malformed."
    """


async def build_nlp_client_from_stack_component(
    component_id: str = DEFAULT_NLP_COMPONENT_ID,
    *,
    registry_client: RegistryHTTPClient,
    secrets_resolve: Callable[[str], Awaitable[bytes]],
) -> "NlpServiceClient":
    """Build a configured :class:`NlpServiceClient` from registry data.

    Steps:

      1. Fetch the stack-component row via
         :meth:`RegistryHTTPClient.get_stack_component` if available,
         falling back to a direct httpx GET against
         ``LEGBA_REGISTRY_API_URL`` so older client instances still work.
      2. Re-shape the ``body.config`` blob through
         :func:`legba.runtime.source_factory._unwrap_factory_dict` to
         flatten the FactoryValue dicts (``{"raw": ..., "factory_kind":
         ...}`` → ``...``).
      3. Resolve the Basic-Auth secrets via ``secrets_resolve``.  Both
         refs are optional; if either is absent the client is built
         without BasicAuth (matches the internal-docker-path contract).
         If only one of the pair is present the call raises — asymmetry
         is operator error.
      4. Construct and return the client.  ``health()`` is NOT called
         here — that's the actor's lifecycle responsibility.

    Raises :class:`NlpClientFactoryError` on every failure mode.
    """
    # ---- 1. Fetch the stack-component row.
    row = await _fetch_stack_component(registry_client, component_id)
    if row is None:
        raise NlpClientFactoryError(
            f"nlp stack-component {component_id!r} not found in registry"
        )

    # ---- 2. Extract + unwrap config.
    body = row.get("body") if isinstance(row, Mapping) else None
    if not isinstance(body, Mapping):
        raise NlpClientFactoryError(
            f"nlp stack-component {component_id!r}: row is missing 'body' "
            f"(got {type(body).__name__})"
        )
    raw_config = body.get("config")
    if not isinstance(raw_config, Mapping):
        raise NlpClientFactoryError(
            f"nlp stack-component {component_id!r}: body.config is missing "
            f"or non-mapping (got {type(raw_config).__name__})"
        )
    cfg = _unwrap_factory_dict(raw_config)

    # ---- 3a. Endpoint (required).
    endpoint = cfg.get("endpoint")
    if not isinstance(endpoint, str) or not endpoint:
        raise NlpClientFactoryError(
            f"nlp stack-component {component_id!r}: config.endpoint is "
            f"missing or non-string after unwrap (got {endpoint!r})"
        )

    # ---- 3b. Secret refs (both-or-neither).
    user_secret_id = _coerce_secret_ref(raw_config.get("api_user"))
    pass_secret_id = _coerce_secret_ref(raw_config.get("api_pass"))
    if bool(user_secret_id) != bool(pass_secret_id):
        # Asymmetry → operator error.  The schema allows either or both
        # to be null but partial wiring breaks Basic Auth in surprising
        # ways; fail loud at bootstrap.
        raise NlpClientFactoryError(
            f"nlp stack-component {component_id!r}: api_user and api_pass "
            f"must be either both set or both absent "
            f"(got user_ref={user_secret_id!r}, pass_ref={pass_secret_id!r})"
        )

    api_user: str | None = None
    api_pass: str | None = None
    if user_secret_id and pass_secret_id:
        api_user = await _resolve_secret_text(
            secrets_resolve, user_secret_id, field="api_user",
        )
        api_pass = await _resolve_secret_text(
            secrets_resolve, pass_secret_id, field="api_pass",
        )

    # ---- 3c. Optional config fields with defaults.
    timeout_seconds = _coerce_timeout(cfg.get("timeout_seconds"))
    translate_path = _coerce_path(cfg.get("translate_path"), "/translate")
    classify_path = _coerce_path(cfg.get("classify_path"), "/classify")
    extract_path = _coerce_path(cfg.get("extract_path"), "/extract")
    summarize_path = _coerce_path(cfg.get("summarize_path"), "/summarize")
    health_path = _coerce_path(cfg.get("health_path"), "/health")

    # ---- 4. Instantiate (no health probe).
    from ..data.stack.nlp_service import NlpServiceClient

    client = NlpServiceClient(
        endpoint=endpoint,
        api_user=api_user,
        api_pass=api_pass,
        timeout_seconds=timeout_seconds,
        translate_path=translate_path,
        classify_path=classify_path,
        extract_path=extract_path,
        summarize_path=summarize_path,
        health_path=health_path,
    )
    logger.info(
        "nlp_client_factory.built component_id=%s endpoint=%s auth=%s",
        component_id, endpoint, "basic" if api_user else "none",
    )
    return client


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


async def _fetch_stack_component(
    registry_client: RegistryHTTPClient,
    component_id: str,
) -> dict[str, Any] | None:
    """Fetch the stack-component row, preferring the typed helper.

    Path resolution:

      1. If ``registry_client`` exposes :meth:`get_stack_component` (the
         clean path), use it.
      2. Otherwise fall back to a direct httpx GET against
         ``LEGBA_REGISTRY_API_URL`` — older client instances handed in
         from long-running processes won't have the method.

    Both paths return the JSON body on 2xx, ``None`` on 404, and raise
    :class:`NlpClientFactoryError` on transport / 5xx.
    """
    getter = getattr(registry_client, "get_stack_component", None)
    if callable(getter):
        try:
            return await getter(component_id)
        except RegistryClientError as exc:
            raise NlpClientFactoryError(
                f"registry lookup failed for {component_id!r}: {exc}"
            ) from exc

    # Fallback path — direct httpx.  Kept import-local per the runtime
    # sandbox-cascade rule (see commit 79176c3): top-level httpx imports
    # in runtime modules drag the dependency into the Temporal worker's
    # workflow-validation re-import path.
    import httpx

    base_url = os.environ.get(
        "LEGBA_REGISTRY_API_URL", "http://localhost:8090",
    ).rstrip("/")
    token = (os.environ.get("LEGBA_REGISTRY_API_TOKEN") or "").strip() or None
    timeout = float(
        os.environ.get("LEGBA_REGISTRY_API_TIMEOUT", "10")
    )
    headers: dict[str, str] = {}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    url = f"{base_url}/api/v1/registry/stack/{component_id}"
    try:
        async with httpx.AsyncClient(timeout=timeout) as c:
            resp = await c.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise NlpClientFactoryError(
            f"registry GET {url} failed: {type(exc).__name__}: {exc}"
        ) from exc
    if resp.status_code == 404:
        return None
    if resp.status_code >= 400:
        raise NlpClientFactoryError(
            f"registry GET {url} returned {resp.status_code}: "
            f"{resp.text[:512]}"
        )
    try:
        return resp.json()
    except ValueError as exc:
        raise NlpClientFactoryError(
            f"registry GET {url} returned non-JSON body: {exc}"
        ) from exc


def _coerce_secret_ref(value: Any) -> str | None:
    """Pull the secret id out of a Property.Secret factory dump.

    The Property.Secret dump shape is ``{"raw": "<vault_id>",
    "factory_kind": "secret", ...}``.  A bare string is accepted for
    forward-compat with operator-authored YAML.  ``None`` (or a dict
    with ``raw=None``) means the operator left the slot empty.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return value or None
    if isinstance(value, Mapping):
        raw = value.get("raw")
        if raw is None:
            return None
        if isinstance(raw, str):
            return raw or None
    return None


async def _resolve_secret_text(
    secrets_resolve: Callable[[str], Awaitable[bytes]],
    secret_id: str,
    *,
    field: str,
) -> str:
    """Resolve ``secret_id`` through the vault and decode UTF-8."""
    try:
        raw = await secrets_resolve(secret_id)
    except Exception as exc:
        raise NlpClientFactoryError(
            f"vault resolve failed for {field}={secret_id!r}: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(raw, (bytes, bytearray)):
        raise NlpClientFactoryError(
            f"vault resolve for {field}={secret_id!r} returned "
            f"{type(raw).__name__}, expected bytes"
        )
    try:
        return bytes(raw).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise NlpClientFactoryError(
            f"vault secret {field}={secret_id!r} is not valid UTF-8: {exc}"
        ) from exc


def _coerce_timeout(value: Any) -> float:
    """Best-effort cast of the timeout field to a positive float.

    The schema enforces ``Number(minimum=1, maximum=600)`` so a sane
    config will pass through; we still defend against missing /
    malformed values by falling back to the client's 60-second default.
    """
    if value is None:
        return 60.0
    try:
        f = float(value)
    except (TypeError, ValueError):
        return 60.0
    if f <= 0:
        return 60.0
    return f


def _coerce_path(value: Any, default: str) -> str:
    """Coerce a per-endpoint path override to a non-empty string."""
    if isinstance(value, str) and value:
        return value
    return default


__all__ = [
    "DEFAULT_NLP_COMPONENT_ID",
    "LazyNlpClient",
    "NlpClientFactoryError",
    "build_nlp_client_from_stack_component",
]

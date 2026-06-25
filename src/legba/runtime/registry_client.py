# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Minimal HTTP client for the legba-registry surface.

The runtime needs to fetch descriptor bodies from the registry to
reconstruct actor deps on cache miss (Phase 5 hardening item 6). This
client wraps the registry's L-113 ``/api/v1/registry/descriptors/...``
routes with just enough surface to support the deps-fallback path.

Configuration (env-first; explicit kwargs override):

  * ``LEGBA_REGISTRY_API_URL`` — base URL, default ``http://localhost:8090``
  * ``LEGBA_REGISTRY_API_TOKEN`` — bearer token. Empty / unset → no
    ``Authorization`` header (matches the registry's dev-mode auth gate).
  * ``LEGBA_REGISTRY_API_TIMEOUT`` — per-request timeout in seconds,
    default 10.

The client raises :class:`RegistryClientError` on transport / 5xx
failures so the caller can surface a hard-fail (per Lewis's "fail loud,
not silent" guidance). A 404 returns ``None`` from the fetch helper so
the cache-miss path can degrade gracefully.
"""

from __future__ import annotations

import logging
import os
from typing import Any

import httpx

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://localhost:8090"
DEFAULT_API_PREFIX = "/api/v1/registry"
DEFAULT_TIMEOUT_SECONDS = 10.0


class RegistryClientError(RuntimeError):
    """Surface for non-404 registry-client failures.

    Raised on:
      * connection / DNS / TCP errors,
      * non-2xx / non-404 HTTP responses,
      * malformed JSON in a 2xx body.

    The caller is expected to fail the actor invocation loudly when this
    fires — silently returning ``hard_fail`` masks the underlying outage.
    """


class RegistryHTTPClient:
    """Async HTTP client for the legba-registry descriptor surface.

    Single-instance-per-process; the underlying ``httpx.AsyncClient`` is
    lazily created so test fixtures can construct the client without
    eagerly opening sockets.
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_prefix: str = DEFAULT_API_PREFIX,
        token: str | None = None,
        timeout_seconds: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._base_url = (base_url or os.environ.get(
            "LEGBA_REGISTRY_API_URL", DEFAULT_BASE_URL,
        )).rstrip("/")
        self._api_prefix = api_prefix
        # An explicit `token=""` should still fall through to env. The
        # caller can pass a sentinel via env-clear if they really want no
        # auth (development mode on the server side accepts anonymous).
        if token is None:
            token = os.environ.get("LEGBA_REGISTRY_API_TOKEN", "").strip() or None
        self._token = token
        self._timeout = float(
            timeout_seconds
            if timeout_seconds is not None
            else os.environ.get("LEGBA_REGISTRY_API_TIMEOUT", DEFAULT_TIMEOUT_SECONDS)
        )
        self._client = client
        self._owns_client = client is None

    @property
    def base_url(self) -> str:
        return self._base_url

    def _headers(self) -> dict[str, str]:
        if self._token:
            return {"Authorization": f"Bearer {self._token}"}
        return {}

    async def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._base_url,
                timeout=self._timeout,
            )
        return self._client

    async def close(self) -> None:
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                pass
            self._client = None

    async def get_descriptor(
        self,
        descriptor_id: str,
        *,
        family: str,
        version: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch the raw descriptor row at ``/descriptors/{family}/{id}``.

        Returns the JSON body (a ``DescriptorRowOut`` shape — ``body`` is
        the descriptor blob). Returns ``None`` when the registry replies
        404. Raises :class:`RegistryClientError` for transport / 5xx.
        """
        path = f"{self._api_prefix}/descriptors/{family}/{descriptor_id}"
        params: dict[str, str] = {}
        if version:
            params["version"] = version
        client = await self._ensure_client()
        try:
            resp = await client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise RegistryClientError(
                f"registry GET {path} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 500 or resp.status_code >= 400:
            raise RegistryClientError(
                f"registry GET {path} returned {resp.status_code}: {resp.text[:512]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise RegistryClientError(
                f"registry GET {path} returned non-JSON body: {exc}"
            ) from exc

    async def get_stack_component(
        self,
        component_id: str,
    ) -> dict[str, Any] | None:
        """Fetch a stack-component row at ``/stack/{component_id}``.

        Stack components (LLM providers, NLP services, NATS / Postgres /
        Redis clusters, ...) live under a sibling registry surface
        from descriptors.  Mirrors :meth:`get_descriptor` semantics:

          * 2xx JSON body → returned as-is (a ``StackComponentRowOut``
            shape — ``body`` carries the config blob with FactoryValue
            dicts).
          * 404 → ``None`` (caller decides whether to fall back or fail).
          * 5xx / transport error / non-JSON → :class:`RegistryClientError`.
        """
        path = f"{self._api_prefix}/stack/{component_id}"
        client = await self._ensure_client()
        try:
            resp = await client.get(path, headers=self._headers())
        except httpx.HTTPError as exc:
            raise RegistryClientError(
                f"registry GET {path} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RegistryClientError(
                f"registry GET {path} returned {resp.status_code}: "
                f"{resp.text[:512]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise RegistryClientError(
                f"registry GET {path} returned non-JSON body: {exc}"
            ) from exc

    async def get_descriptor_typed(
        self,
        descriptor_id: str,
        *,
        family: str,
        version: str | None = None,
    ) -> dict[str, Any] | None:
        """Fetch the typed (re-parsed) descriptor dict at ``/.../typed``.

        Returns the typed model dump (pydantic ``model_dump(mode="json")``
        output). Returns ``None`` on 404; raises
        :class:`RegistryClientError` for transport / 5xx.
        """
        path = f"{self._api_prefix}/descriptors/{family}/{descriptor_id}/typed"
        params: dict[str, str] = {}
        if version:
            params["version"] = version
        client = await self._ensure_client()
        try:
            resp = await client.get(path, params=params, headers=self._headers())
        except httpx.HTTPError as exc:
            raise RegistryClientError(
                f"registry GET {path} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if resp.status_code == 404:
            return None
        if resp.status_code >= 400:
            raise RegistryClientError(
                f"registry GET {path} returned {resp.status_code}: {resp.text[:512]}"
            )
        try:
            return resp.json()
        except ValueError as exc:
            raise RegistryClientError(
                f"registry GET {path} returned non-JSON body: {exc}"
            ) from exc


__all__ = [
    "DEFAULT_API_PREFIX",
    "DEFAULT_BASE_URL",
    "DEFAULT_TIMEOUT_SECONDS",
    "RegistryClientError",
    "RegistryHTTPClient",
]

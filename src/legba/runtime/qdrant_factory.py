# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Qdrant async client factory (built from a registered stack component).

Call from :func:`legba.runtime.dapr_host.bring_up_production_runtime`;
cache the result for the process lifetime; thread into pipeline_factory
via the ``qdrant_client`` kwarg of
:func:`legba.runtime.pipeline.build_filter_handler`.

Today :mod:`legba.runtime.dapr_host` passes ``qdrant_client=None`` into
``build_filter_handler``, which fails activation of the
``dedupe_tier_3`` filter with a clear ValueError. This factory closes
that gap by:

  1. Fetching the vector-store stack component row from the registry
     (``/stack/{component_id}``) via :class:`RegistryHTTPClient`.
  2. Re-parsing its ``body.config`` against the
     :class:`legba.data.schemas.stack.VectorStoreConfig` shape (which
     exposes ``endpoint`` / ``collection_prefix`` / ``default_dim`` /
     ``default_metric``).
  3. Constructing a single ``qdrant_client.AsyncQdrantClient`` against
     the configured endpoint (the legba deploy is auth-less today; the
     ``api_key`` field exists on the schema but is left ``None``).
  4. Validating reachability by issuing ``await get_collections()``.
     If the call fails or returns a shape we don't recognise, the
     factory raises :class:`QdrantFactoryError` so the host fails loud
     rather than handing the pipeline a half-wired client.

Per Lewis's no-stubs rule, every failure surfaces a typed error rather
than degrading silently. Per the Temporal-sandbox precedent (see
commit 79176c3 and the ``runtime/budget.py`` TYPE_CHECKING guard) we
hold the ``qdrant_client`` import inside the function body so importing
this module never pulls the qdrant client (and its transitive httpx /
urllib closure) through.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Mapping

from ..data.schemas.stack import VectorStoreConfig
from .registry_client import RegistryClientError, RegistryHTTPClient

if TYPE_CHECKING:  # pragma: no cover
    from qdrant_client import AsyncQdrantClient

logger = logging.getLogger(__name__)


__all__ = [
    "DEFAULT_QDRANT_ENDPOINT",
    "QdrantFactoryError",
    "build_qdrant_client_from_stack_component",
]


DEFAULT_QDRANT_ENDPOINT = "http://qdrant:6333"


class QdrantFactoryError(RuntimeError):
    """Surface for non-recoverable Qdrant factory failures.

    Raised on:
      * registry lookup failure (transport / 5xx),
      * registry 404 (component_id not registered),
      * malformed ``body.config`` (missing fields, wrong types),
      * Qdrant connectivity probe failure (``get_collections`` raised
        or returned an unexpected shape).

    The host treats this as a hard fail — the runtime must not start
    in a half-wired state where the dedupe tier-3 filter would later
    blow up at activation time.
    """


async def build_qdrant_client_from_stack_component(
    component_id: str,
    *,
    registry_client: RegistryHTTPClient,
) -> "AsyncQdrantClient":
    """Build + validate an :class:`AsyncQdrantClient` from registry data.

    Parameters
    ----------
    component_id:
        The vector-store stack-component id (e.g.
        ``vec.local.qdrant_primary``). The registry row is fetched at
        ``/stack/{component_id}`` via the supplied client.
    registry_client:
        The shared :class:`RegistryHTTPClient` instance — same one used
        by the LLM-handler factory. Owned by the caller; this factory
        does not close it.

    Returns
    -------
    AsyncQdrantClient
        A ready-to-use Qdrant client whose ``get_collections()`` probe
        has succeeded.

    Raises
    ------
    QdrantFactoryError
        On any failure path (registry lookup, config validation,
        connectivity probe).
    """
    # 1. Registry fetch.
    try:
        row = await _fetch_stack_component(registry_client, component_id)
    except RegistryClientError as exc:
        raise QdrantFactoryError(
            f"stack-component lookup failed for {component_id!r}: {exc}"
        ) from exc
    if row is None:
        raise QdrantFactoryError(
            f"stack-component {component_id!r} not found in registry"
        )

    # 2. Re-parse config. The body carries the JSON dump (factory values
    # as dicts); parse non-strictly so FactoryValue subclasses coerce
    # from their dict shapes (matches the LLM-handler factory pattern).
    body = row.get("body") or {}
    raw_config = body.get("config") if isinstance(body, dict) else None
    if not isinstance(raw_config, Mapping):
        raise QdrantFactoryError(
            f"stack-component {component_id!r}: body.config is missing "
            f"or non-mapping (got {type(raw_config).__name__})"
        )
    try:
        cfg = VectorStoreConfig.model_validate(dict(raw_config), strict=False)
    except Exception as exc:
        raise QdrantFactoryError(
            f"stack-component {component_id!r}: VectorStoreConfig "
            f"validation failed: {exc}"
        ) from exc

    endpoint = (cfg.endpoint.raw or "").strip() or DEFAULT_QDRANT_ENDPOINT

    # 3. Construct the client. Import qdrant_client lazily — keeping it
    # off the module-import path matches the budget.py TYPE_CHECKING
    # precedent (commit 79176c3) for staying out of the Temporal-
    # sandbox urllib cascade.
    from qdrant_client import AsyncQdrantClient
    from qdrant_client.http.exceptions import (  # noqa: F401  (alias below)
        UnexpectedResponse,
    )

    client = AsyncQdrantClient(url=endpoint)

    # 4. Connectivity probe. Any failure here means downstream
    # dedupe-tier-3 activation would also fail at the worst possible
    # time; surface it now.
    try:
        result = await client.get_collections()
    except Exception as exc:
        # Be defensive about close() so we don't mask the original error.
        try:
            await client.close()
        except Exception:  # pragma: no cover
            pass
        raise QdrantFactoryError(
            f"qdrant connectivity probe failed for {component_id!r} "
            f"at {endpoint!r}: {type(exc).__name__}: {exc}"
        ) from exc

    if not _is_collections_response(result):
        try:
            await client.close()
        except Exception:  # pragma: no cover
            pass
        raise QdrantFactoryError(
            f"qdrant get_collections() at {endpoint!r} returned an "
            f"unexpected shape: {type(result).__name__}"
        )

    logger.info(
        "qdrant_factory.client_built component_id=%s endpoint=%s "
        "collection_prefix=%s default_dim=%s default_metric=%s",
        component_id,
        endpoint,
        cfg.collection_prefix.raw,
        cfg.default_dim.raw,
        cfg.default_metric.raw,
    )
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_collections_response(result: Any) -> bool:
    """Verify a ``get_collections()`` result is the expected list-bearing shape.

    The qdrant-client ``CollectionsResponse`` exposes a ``collections``
    attribute that's a ``list`` of items (each with a ``.name``). Tests
    may pass a plain ``list`` from a fake client; accept both.
    """
    if isinstance(result, list):
        return True
    collections = getattr(result, "collections", None)
    return isinstance(collections, list)


async def _fetch_stack_component(
    registry_client: RegistryHTTPClient,
    component_id: str,
) -> dict[str, Any] | None:
    """GET ``/stack/{component_id}`` via the registry HTTP client.

    Returns the JSON body on 2xx, ``None`` on 404, raises
    :class:`RegistryClientError` for transport / 5xx. Mirrors the
    helper in :mod:`legba.runtime.analyst_deps_builder` — we can't
    share it directly without creating a circular dependency, so the
    body is duplicated (the shape is intentionally simple).
    """
    import httpx

    client = await registry_client._ensure_client()  # noqa: SLF001
    path = f"{registry_client._api_prefix}/stack/{component_id}"  # noqa: SLF001
    try:
        resp = await client.get(
            path, headers=registry_client._headers(),  # noqa: SLF001
        )
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

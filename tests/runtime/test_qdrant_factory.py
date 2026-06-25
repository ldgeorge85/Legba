# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for :mod:`legba.runtime.qdrant_factory`.

Coverage:

  * Happy path against the live ``legba-qdrant`` container, gated on
    ``LEGBA_TEST_QDRANT=1`` so the test only runs when the docker
    network gives us ``qdrant:6333``. The probe call returns a
    qdrant-client ``CollectionsResponse`` whose ``.collections`` is
    a list (possibly empty).
  * Registry 404 path: a stack-component miss surfaces a clear
    :class:`QdrantFactoryError` (no silent stub).
  * Transport-failure / unreachable-endpoint path: the connectivity
    probe failing on a closed port raises
    :class:`QdrantFactoryError` so the runtime fails loud rather than
    handing the dedupe-tier-3 filter a half-wired client.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import pytest

from legba.runtime.qdrant_factory import (
    QdrantFactoryError,
    build_qdrant_client_from_stack_component,
)
from legba.runtime.registry_client import RegistryHTTPClient


_QDRANT_GATE = os.environ.get("LEGBA_TEST_QDRANT") == "1"

skip_unless_qdrant_live = pytest.mark.skipif(
    not _QDRANT_GATE,
    reason="LEGBA_TEST_QDRANT=1 not set; skipping live Qdrant tests",
)


# ---------------------------------------------------------------------------
# Registry fixtures
# ---------------------------------------------------------------------------


_COMPONENT_ID = "vec.local.qdrant_primary"
_VERSION = "0" * 64


def _vector_store_body(
    endpoint: str,
    *,
    component_id: str = _COMPONENT_ID,
) -> dict[str, Any]:
    return {
        "id": component_id,
        "version": _VERSION,
        "body": {
            "id": component_id,
            "name": "Local Qdrant Primary",
            "schema_uri": "legba/stack/vector_store/1.0.0",
            "version": _VERSION,
            "owner": "test",
            "state": "active",
            "config": {
                "endpoint": {"factory_kind": "text", "raw": endpoint},
                "collection_prefix": {
                    "factory_kind": "text",
                    "raw": "legba_dedup",
                },
                "default_dim": {"factory_kind": "number", "raw": 1024},
                "default_metric": {
                    "factory_kind": "dropdown_static",
                    "raw": "cosine",
                    "options": ["cosine", "dot", "euclid"],
                },
            },
        },
    }


def _registry_client(
    *,
    response_factory,
    component_id: str = _COMPONENT_ID,
) -> RegistryHTTPClient:
    """Build a RegistryHTTPClient backed by an httpx MockTransport.

    ``response_factory`` is invoked with the incoming request and must
    return an ``httpx.Response``.
    """

    def _handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(f"/stack/{component_id}"), (
            f"unexpected path: {request.url.path}"
        )
        return response_factory(request)

    transport = httpx.MockTransport(_handler)
    inner = httpx.AsyncClient(
        transport=transport, base_url="http://registry.test",
    )
    return RegistryHTTPClient(
        base_url="http://registry.test", token=None, client=inner,
    )


# ---------------------------------------------------------------------------
# Live happy path
# ---------------------------------------------------------------------------


@skip_unless_qdrant_live
@pytest.mark.asyncio
async def test_build_qdrant_client_happy_live() -> None:
    """LEGBA_TEST_QDRANT=1 — hit the live legba-qdrant container.

    Validates the probe call succeeds and the returned client is a
    qdrant-client ``AsyncQdrantClient`` instance whose collections
    surface is reachable.
    """
    from qdrant_client import AsyncQdrantClient

    body = _vector_store_body("http://qdrant:6333")
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=body),
    )
    try:
        qdrant = await build_qdrant_client_from_stack_component(
            _COMPONENT_ID, registry_client=client,
        )
    finally:
        await client.close()
    assert isinstance(qdrant, AsyncQdrantClient)
    try:
        # Sanity: re-probe to confirm the returned client is functional.
        result = await qdrant.get_collections()
        # The CollectionsResponse exposes ``.collections``; some local
        # forks expose a plain list. Both should pass the factory's
        # shape check, so re-probe here as a fast sanity rail.
        assert hasattr(result, "collections") or isinstance(result, list)
    finally:
        await qdrant.close()


# ---------------------------------------------------------------------------
# Registry 404 path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_qdrant_client_404_raises() -> None:
    """Registry 404 on stack-component lookup surfaces a typed error."""
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(404),
    )
    try:
        with pytest.raises(QdrantFactoryError, match="not found"):
            await build_qdrant_client_from_stack_component(
                _COMPONENT_ID, registry_client=client,
            )
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Transport failure / unreachable endpoint path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_qdrant_client_unreachable_endpoint_raises() -> None:
    """Probe failure (closed port / DNS miss) surfaces a typed error.

    Uses an obviously-unreachable endpoint so the qdrant client's
    ``get_collections()`` raises during the probe; the factory wraps
    that into :class:`QdrantFactoryError` and closes the half-built
    client.
    """
    # ``127.0.0.1:1`` — port 1 is reserved and refuses TCP connects
    # on any sane host. Short timeout is implicit (qdrant-client's
    # default connect timeout fires fast on a refused TCP).
    body = _vector_store_body("http://127.0.0.1:1")
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=body),
    )
    try:
        with pytest.raises(QdrantFactoryError, match="connectivity probe"):
            await build_qdrant_client_from_stack_component(
                _COMPONENT_ID, registry_client=client,
            )
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# Config malformed path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_build_qdrant_client_missing_config_raises() -> None:
    """A registry row without ``body.config`` raises a typed error."""
    bad_body = {
        "id": _COMPONENT_ID,
        "version": _VERSION,
        "body": {"id": _COMPONENT_ID, "name": "broken"},  # no .config
    }
    client = _registry_client(
        response_factory=lambda _req: httpx.Response(200, json=bad_body),
    )
    try:
        with pytest.raises(QdrantFactoryError, match="config"):
            await build_qdrant_client_from_stack_component(
                _COMPONENT_ID, registry_client=client,
            )
    finally:
        await client.close()

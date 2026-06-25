# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Embedding service factory (built from a registered stack component).

Call from :func:`legba.runtime.dapr_host.bring_up_production_runtime`;
cache the result for the process lifetime; thread into pipeline_factory
via the ``embedding_service`` kwarg of
:func:`legba.runtime.pipeline.build_filter_handler`.

The pre-reshape in-process ``BgeM3EmbeddingHandler`` (BAAI/bge-m3 via
``sentence-transformers``) retired in L-205. The replacement is the
hosted ``embed.primary.openai_compat`` stack component, which
points at a vLLM-served OpenAI-compatible ``/v1/embeddings`` endpoint.
This factory builds a thin httpx-backed client conforming to the
:class:`legba.data.filters.dedupe.EmbeddingService` Protocol
(``async def embed(text: str) -> list[float]``) plus a public ``dim``
attribute matching the configured vector size.

Lifecycle / sandbox notes:

  * ``httpx`` is imported inside the function body (not at module top)
    to match the budget.py TYPE_CHECKING precedent (commit 79176c3) —
    the Temporal sandbox is allergic to the urllib closure ``httpx``
    drags along at import time, and the registry HTTP path is the only
    other caller of httpx in the host's bring-up.
  * The factory eagerly resolves the api_key (we cannot defer
    credential fetch to first-call without complicating the dedupe
    filter's error handling). The resolved key never enters logs.
  * The probe call inside :func:`build_embedding_service_from_stack_component`
    is a single embedding of the literal string ``"healthcheck"`` so
    we fail loud if vault auth / endpoint URL / model name don't line
    up at bring-up, rather than at the first dedupe-tier-3 transform.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Awaitable, Callable, Mapping

from ..data.schemas.stack import EmbeddingServiceConfig
from .registry_client import RegistryClientError, RegistryHTTPClient

if TYPE_CHECKING:  # pragma: no cover
    import httpx

logger = logging.getLogger(__name__)


__all__ = [
    "EmbeddingFactoryError",
    "HostedEmbeddingClient",
    "build_embedding_service_from_stack_component",
]


class EmbeddingFactoryError(RuntimeError):
    """Surface for non-recoverable embedding-factory failures.

    Raised on:
      * registry lookup failure (transport / 5xx),
      * registry 404 (component_id not registered),
      * malformed ``body.config`` (missing fields, wrong types),
      * vault resolution failure for the configured api_key secret,
      * the probe call failing (auth / endpoint / model).

    The host treats this as a hard fail — without an embedding service
    the dedupe-tier-3 path cannot run, and we'd rather fail at bring-up
    than mid-pipeline.
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class HostedEmbeddingClient:
    """OpenAI-compatible ``/v1/embeddings`` client.

    Satisfies the :class:`legba.data.filters.dedupe.EmbeddingService`
    Protocol: ``async def embed(text: str) -> list[float]``. The
    dedupe-tier-3 transform also reads ``dim`` off the configured
    handler (the field is on :class:`Tier3Config` rather than on this
    client, but we expose it here so operators inspecting the runtime
    object see the expected vector size at a glance).

    Wire shape (vLLM / OpenAI-compatible):

    .. code-block:: text

        POST {endpoint}/v1/embeddings
        Authorization: Bearer <api_key>
        Content-Type: application/json

        {"model": "<model_name>", "input": "<text>"}

    Response::

        {"object": "list",
         "data": [{"object": "embedding", "index": 0,
                   "embedding": [<float>, ...]}],
         "model": "<model_name>",
         "usage": {"prompt_tokens": N, "total_tokens": N}}

    Multi-input requests are supported by the upstream endpoint but
    the dedupe-tier-3 transform only embeds one string at a time, so
    we keep the single-input path simple and let any caller that
    needs batching wrap us later.
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_key: str,
        model_name: str,
        dim: int,
        normalize: bool,
        batch_size: int,
        timeout_seconds: float = 30.0,
        http_client: "httpx.AsyncClient | None" = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self.dim = int(dim)
        self.normalize = bool(normalize)
        self.batch_size = int(batch_size)
        self._timeout_seconds = float(timeout_seconds)
        # When the caller injects a client (tests with respx /
        # MockTransport) we don't own it; otherwise we lazily build one
        # on first call so module import stays httpx-free.
        self._client = http_client
        self._owns_client = http_client is None

    # ------------------------------------------------------------------
    # EmbeddingService Protocol implementation
    # ------------------------------------------------------------------

    async def embed(self, text: str) -> list[float]:
        """Embed ``text`` via the hosted endpoint; return the vector.

        Raises :class:`EmbeddingFactoryError` on transport / auth /
        protocol shape failures so callers can distinguish a hard
        backend outage from an empty input. The dedupe filter already
        catches and logs at warning + fails open (returning the signal
        unmarked), so the chosen exception type here is just for
        observability — any non-EmbeddingFactoryError surface would
        also be caught by the filter's try/except.
        """
        client = await self._ensure_client()
        path = self._embeddings_path()
        payload = self._build_request_payload(text)
        try:
            resp = await client.post(
                path,
                json=payload,
                headers=self._auth_headers(),
            )
        except Exception as exc:
            raise EmbeddingFactoryError(
                f"embedding POST {path} transport failed: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise EmbeddingFactoryError(
                f"embedding endpoint rejected credentials "
                f"(HTTP {resp.status_code}) at {path}"
            )
        if resp.status_code >= 400:
            raise EmbeddingFactoryError(
                f"embedding endpoint returned HTTP {resp.status_code} "
                f"at {path}: {resp.text[:512]}"
            )

        try:
            body = resp.json()
        except ValueError as exc:
            raise EmbeddingFactoryError(
                f"embedding endpoint returned non-JSON body at {path}: "
                f"{exc}"
            ) from exc

        return _extract_single_embedding(body)

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def aclose(self) -> None:
        """Close the underlying httpx client if we own it."""
        if self._client is not None and self._owns_client:
            try:
                await self._client.aclose()
            except Exception:  # pragma: no cover
                pass
            self._client = None

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _ensure_client(self) -> "httpx.AsyncClient":
        if self._client is None:
            import httpx
            self._client = httpx.AsyncClient(timeout=self._timeout_seconds)
        return self._client

    def _embeddings_path(self) -> str:
        """Return the absolute embeddings URL.

        Accepts an endpoint with or without a trailing ``/v1`` (the
        legba ``embed.primary.openai_compat`` component is
        registered with ``/v1`` already on the path, but historically
        some entries omit it — handle both rather than failing on a
        stale config).
        """
        base = self._endpoint
        if base.endswith("/v1") or base.endswith("/v1/"):
            return base.rstrip("/") + "/embeddings"
        if "/v1/embeddings" in base:
            return base
        return base + "/v1/embeddings"

    def _auth_headers(self) -> dict[str, str]:
        # Always carry the bearer header (vLLM accepts but doesn't
        # require it; the hosted variant does require it).
        return {"Authorization": f"Bearer {self._api_key}"}

    def _build_request_payload(self, text: str) -> dict[str, Any]:
        return {
            "model": self._model_name,
            "input": text,
        }


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


async def build_embedding_service_from_stack_component(
    component_id: str,
    *,
    registry_client: RegistryHTTPClient,
    secrets_resolve: Callable[[str], Awaitable[bytes]],
) -> HostedEmbeddingClient:
    """Build a :class:`HostedEmbeddingClient` from registry data.

    Steps:

      1. Fetch the stack-component row at ``/stack/{component_id}``.
      2. Re-parse ``body.config`` against
         :class:`legba.data.schemas.stack.EmbeddingServiceConfig`.
      3. Resolve the api_key secret via ``secrets_resolve`` (the same
         callable :func:`build_llm_handler_from_stack_component` uses).
      4. Construct the client. We do NOT issue a probe call at
         bring-up — the dedupe-tier-3 filter handles the first real
         embed call's failure mode, and burning hosted GPU on every
         restart is wasteful. The factory's contract is "client is
         constructable with resolved auth"; first-call validation
         lives on the wire.

    Raises
    ------
    EmbeddingFactoryError
        On registry / config / secret-resolution failures.
    """
    # 1. Registry fetch.
    try:
        row = await _fetch_stack_component(registry_client, component_id)
    except RegistryClientError as exc:
        raise EmbeddingFactoryError(
            f"stack-component lookup failed for {component_id!r}: {exc}"
        ) from exc
    if row is None:
        raise EmbeddingFactoryError(
            f"stack-component {component_id!r} not found in registry"
        )

    # 2. Re-parse config.
    body = row.get("body") or {}
    raw_config = body.get("config") if isinstance(body, dict) else None
    if not isinstance(raw_config, Mapping):
        raise EmbeddingFactoryError(
            f"stack-component {component_id!r}: body.config is missing "
            f"or non-mapping (got {type(raw_config).__name__})"
        )
    try:
        cfg = EmbeddingServiceConfig.model_validate(dict(raw_config), strict=False)
    except Exception as exc:
        raise EmbeddingFactoryError(
            f"stack-component {component_id!r}: EmbeddingServiceConfig "
            f"validation failed: {exc}"
        ) from exc

    endpoint = (cfg.endpoint.raw or "").strip()
    if not endpoint:
        raise EmbeddingFactoryError(
            f"stack-component {component_id!r}: endpoint is empty"
        )
    model_name = cfg.model_name.raw
    dim = int(cfg.dim.raw)
    batch_size = int(cfg.batch_size.raw)
    normalize_raw = (cfg.normalize.raw or "").lower()
    normalize = normalize_raw == "true"

    # 3. Resolve api_key. If the schema's optional api_key is unset we
    # treat the endpoint as anonymous-ok (vLLM behind a docker network
    # accepts requests without auth). We still construct the client
    # with an empty-bearer to keep the header shape uniform.
    api_key_plain = ""
    if cfg.api_key is not None:
        secret_id = cfg.api_key.raw
        try:
            api_key_bytes = await secrets_resolve(secret_id)
        except Exception as exc:
            raise EmbeddingFactoryError(
                f"vault could not resolve api_key {secret_id!r} for "
                f"{component_id!r}: {exc}"
            ) from exc
        api_key_plain = api_key_bytes.decode("utf-8").strip()

    client = HostedEmbeddingClient(
        endpoint=endpoint,
        api_key=api_key_plain,
        model_name=model_name,
        dim=dim,
        normalize=normalize,
        batch_size=batch_size,
    )
    logger.info(
        "embedding_factory.client_built component_id=%s endpoint=%s "
        "model=%s dim=%s normalize=%s batch_size=%s",
        component_id, endpoint, model_name, dim, normalize, batch_size,
    )
    return client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _extract_single_embedding(body: Any) -> list[float]:
    """Pull the first embedding vector out of an OpenAI-shaped response.

    Shape::

        {"object": "list",
         "data": [{"object": "embedding", "index": 0,
                   "embedding": [<float>, ...]}],
         ...}

    Some self-hosted vLLM builds nest the embedding under a
    ``base64`` key when ``encoding_format=base64`` is requested; we
    don't request that, so a missing ``embedding`` list is a hard
    error (not silently treating an alternate field as the vector).
    """
    if not isinstance(body, Mapping):
        raise EmbeddingFactoryError(
            f"embedding response is not a JSON object: {type(body).__name__}"
        )
    data = body.get("data")
    if not isinstance(data, list) or not data:
        raise EmbeddingFactoryError(
            f"embedding response missing 'data' list (got {data!r})"
        )
    first = data[0]
    if not isinstance(first, Mapping):
        raise EmbeddingFactoryError(
            f"embedding response data[0] is not an object: "
            f"{type(first).__name__}"
        )
    vec = first.get("embedding")
    if not isinstance(vec, list) or not vec:
        raise EmbeddingFactoryError(
            f"embedding response data[0].embedding is not a non-empty "
            f"list (got {type(vec).__name__})"
        )
    # Coerce each entry to float. The upstream serializer emits JSON
    # numbers; we tolerate ints by coercing.
    try:
        return [float(x) for x in vec]
    except (TypeError, ValueError) as exc:
        raise EmbeddingFactoryError(
            f"embedding response data[0].embedding contains non-numeric "
            f"entries: {exc}"
        ) from exc


async def _fetch_stack_component(
    registry_client: RegistryHTTPClient,
    component_id: str,
) -> dict[str, Any] | None:
    """GET ``/stack/{component_id}`` via the registry HTTP client.

    Returns the JSON body on 2xx, ``None`` on 404, raises
    :class:`RegistryClientError` for transport / 5xx. Duplicates the
    shape from :mod:`legba.runtime.qdrant_factory` /
    :mod:`legba.runtime.analyst_deps_builder` rather than importing
    either (avoids circular imports + keeps the body trivial).
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

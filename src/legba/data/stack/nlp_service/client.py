# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Async HTTP client for the hosted Legba-models NLP service.

Service contract: ``legba-models/USAGE.md`` + ``docs/AI_MODELS.md``.

Three endpoints used by the Phase-4 filter handlers:

  * ``POST /classify`` — DeBERTa-v3 zero-shot classification.
  * ``POST /extract``  — GLiREL zero-shot relation extraction (S-P-O triples).
  * ``POST /translate`` — NLLB-200 translation.
  * ``POST /summarize`` — T5-small summarization (kept for parity but
    consumed by cluster code, not filters).

Auth: HTTPS + Basic Auth via vault-resolved ``MODELS_API_USER`` /
``MODELS_API_PASS``. The internal ``http://legba-models:8700`` docker path
has no auth; passing ``api_user=None`` skips the BasicAuth wrapper.

Graceful degradation: every method returns a typed result OR raises a
typed exception (``NlpServiceUnavailable`` / ``NlpServiceAuthError``).
The filter handlers convert these into no-op signal pass-throughs +
record the failure in their health state. Network-level failures
(connection refused, timeout) raise ``NlpServiceUnavailable``; HTTP 401
raises ``NlpServiceAuthError`` so the operator gets a distinct signal.

This client is independent of the legacy
:class:`legba.ingestion.models_client.ModelsClient` — that one reads env
vars at construction time and is consumed by the legacy subconscious +
ingestion modules (L-205 retires them). The Phase-4 filter handlers
construct this client from vault-resolved credentials.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class NlpServiceError(RuntimeError):
    """Base class for nlp_service errors."""


class NlpServiceUnavailable(NlpServiceError):
    """The service is unreachable / returning 5xx / timing out.

    Filter handlers convert this into a no-op pass-through: the signal
    flows downstream un-enriched and the handler's health flips to
    ``degraded``.
    """


class NlpServiceAuthError(NlpServiceError):
    """The service returned 401 — bad / missing credentials.

    A distinct exception from :class:`NlpServiceUnavailable` because
    operator action (vault rotation) is required.
    """


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class NlpServiceClient:
    """Async HTTP client for the hosted Legba-models service.

    Construct via ``__init__`` (descriptor handlers pass vault-resolved
    creds) or via :meth:`from_env` (legacy ingestion path; reads
    ``MODELS_API_URL``/``MODELS_API_USER``/``MODELS_API_PASS``).
    """

    def __init__(
        self,
        *,
        endpoint: str,
        api_user: str | None = None,
        api_pass: str | None = None,
        api_secret: str | None = None,
        timeout_seconds: float = 60.0,
        translate_path: str = "/translate",
        classify_path: str = "/classify",
        extract_path: str = "/extract",
        summarize_path: str = "/summarize",
        health_path: str = "/health",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not endpoint:
            raise ValueError("endpoint is required")
        self._endpoint = endpoint.rstrip("/")
        self._timeout = timeout_seconds
        self._translate_path = translate_path
        self._classify_path = classify_path
        self._extract_path = extract_path
        self._summarize_path = summarize_path
        self._health_path = health_path

        auth: httpx.BasicAuth | None = None
        if api_user and api_pass is not None:
            auth = httpx.BasicAuth(api_user, api_pass)
        self._auth = auth

        # Defense-in-depth shared secret presented to legba-models when it
        # enforces one (X-Models-Secret). Absent = no header (dev default).
        self._default_headers: dict[str, str] = {}
        if api_secret:
            self._default_headers["X-Models-Secret"] = api_secret

        # Pre-built client (injected for tests via ``httpx.MockTransport``)
        # or constructed lazily.
        self._client = client
        self._owned_client = client is None

    @classmethod
    def from_env(cls) -> "NlpServiceClient | None":
        """Build a client from the legacy env vars. Returns ``None`` if
        ``MODELS_API_URL`` is unset — lets the caller decide between
        no-op and required wiring."""
        import os
        url = os.getenv("MODELS_API_URL", "").strip()
        if not url:
            return None
        return cls(
            endpoint=url,
            api_user=os.getenv("MODELS_API_USER") or None,
            api_pass=os.getenv("MODELS_API_PASS") or None,
            api_secret=os.getenv("MODELS_API_SECRET")
            or os.getenv("LEGBA_MODELS_API_SECRET")
            or None,
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self._endpoint,
                auth=self._auth,
                timeout=httpx.Timeout(self._timeout),
                headers=self._default_headers or None,
            )
            self._owned_client = True
        return self._client

    async def aclose(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "NlpServiceClient":
        self._ensure_client()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        client = self._ensure_client()
        try:
            resp = await client.post(
                path,
                json=body,
                auth=self._auth,
                headers=self._default_headers or None,
            )
        except httpx.HTTPError as exc:
            raise NlpServiceUnavailable(f"POST {path} network error: {exc}") from exc
        if resp.status_code == 401:
            raise NlpServiceAuthError(f"POST {path} -> 401 (bad credentials)")
        if resp.status_code >= 500:
            raise NlpServiceUnavailable(
                f"POST {path} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        if resp.status_code >= 400:
            # 4xx (other than 401) means the request body was bad — surface
            # as unavailable so the caller treats it like a service failure
            # rather than crashing the pipeline.
            raise NlpServiceUnavailable(
                f"POST {path} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except Exception as exc:
            raise NlpServiceUnavailable(f"POST {path} non-JSON response: {exc}") from exc

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """GET /health. Returns the raw dict.

        Raises :class:`NlpServiceUnavailable` on network failure / non-200.
        :class:`NlpServiceAuthError` on 401.
        """
        client = self._ensure_client()
        try:
            resp = await client.get(self._health_path, auth=self._auth)
        except httpx.HTTPError as exc:
            raise NlpServiceUnavailable(f"GET {self._health_path}: {exc}") from exc
        if resp.status_code == 401:
            raise NlpServiceAuthError(f"GET {self._health_path} -> 401")
        if resp.status_code != 200:
            raise NlpServiceUnavailable(
                f"GET {self._health_path} -> HTTP {resp.status_code}"
            )
        try:
            return resp.json()
        except Exception as exc:
            raise NlpServiceUnavailable(f"non-JSON health response: {exc}") from exc

    async def classify(
        self,
        text: str,
        *,
        labels: list[str] | None = None,
    ) -> dict[str, Any]:
        """POST /classify. Returns the raw dict::

            {"category": str, "confidence": float, "scores": {label: float}, "ms": float}

        ``labels`` is optional; when omitted the server uses its built-in
        9-category default set (see ``legba-models/USAGE.md``).
        """
        body: dict[str, Any] = {"text": text}
        if labels is not None:
            body["labels"] = list(labels)
        return await self._post(self._classify_path, body)

    async def extract(self, text: str) -> dict[str, Any]:
        """POST /extract. Returns the raw dict::

            {"triples": [{"subject": str, "predicate": str, "object": str}, ...],
             "ms": float}
        """
        return await self._post(self._extract_path, {"text": text})

    async def translate(
        self,
        text: str,
        source_lang: str,
        target_lang: str = "en",
    ) -> dict[str, Any]:
        """POST /translate. Returns the raw dict::

            {"translated": str, "source_lang": str, "target_lang": str, "ms": float}
        """
        return await self._post(
            self._translate_path,
            {"text": text, "source_lang": source_lang, "target_lang": target_lang},
        )

    async def summarize(
        self,
        texts: list[str],
        max_length: int = 64,
    ) -> dict[str, Any]:
        """POST /summarize. Returns the raw dict::

            {"summary": str, "ms": float}
        """
        return await self._post(
            self._summarize_path,
            {"texts": list(texts), "max_length": int(max_length)},
        )


__all__ = [
    "NlpServiceAuthError",
    "NlpServiceClient",
    "NlpServiceError",
    "NlpServiceUnavailable",
]

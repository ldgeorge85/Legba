# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Thin hosted media-extraction client (P-07 / PIVOT §4.6).

Heavy media extraction (transcribe / caption / OCR / detect) runs on
**hosted endpoints** (Whisper / VLM / OCR) — the same hosted-model pattern
as the existing :class:`legba.data.stack.nlp_service.NlpServiceClient`. This
client is the worker-pool's edge to those endpoints: it POSTs a ``media_ref``
to the configured path and returns a :class:`MediaExtractionResult`.

External-model edge — NO stub (A-2 / G3 close)
----------------------------------------------

There is NO local fallback. The former "clearly-marked stub edge" fabricated
``[stub transcript of <ref>]`` rows into the SHARED signal pool whenever
``LEGBA_MEDIA_API_URL`` was unset — provenance poison. Now:

  * No endpoint configured → :class:`MediaEndpointNotConfiguredError`
    (typed, permanent — the job plane refuses the work; nothing lands).
  * Endpoint configured but unreachable / 5xx →
    :class:`MediaEndpointUnreachable` (transient — the worker lets the
    delivery redeliver and the job retries, failing terminally only after
    ``max_deliver``).
  * Reachable endpoint answers 4xx / non-JSON →
    :class:`MediaExtractionError` (a real, terminal refusal).

When the hosted media endpoints come online, set ``LEGBA_MEDIA_API_URL`` (+ the
existing ``MODELS_API_USER`` / ``MODELS_API_PASS`` for Basic Auth) and the
same client makes the real call with zero plumbing changes.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ...data.jobs.media import (
    MediaEndpointNotConfiguredError,
    MediaExtraction,
    MediaExtractionResult,
    configured_media_endpoint,
)

logger = logging.getLogger(__name__)


# Per-extraction default endpoint path on the hosted media service.
EXTRACTION_PATHS: dict[str, str] = {
    "transcribe": "/transcribe",
    "caption": "/caption",
    "ocr": "/ocr",
    "detect": "/detect",
}

# Model labels recorded in provenance (informational — the real model name
# comes back from a live endpoint's response when present).
EXTRACTION_MODELS: dict[str, str] = {
    "transcribe": "whisper-large-v3",
    "caption": "vlm-caption",
    "ocr": "ocr-engine",
    "detect": "object-detect",
}


class MediaClient:
    """Async client for the hosted media-extraction endpoints.

    Construct with an explicit ``endpoint`` (descriptor / factory path) or via
    :meth:`from_env`. A ``None`` endpoint means "no media service configured"
    → every :meth:`extract` call raises
    :class:`MediaEndpointNotConfiguredError` (typed refusal, never a stub).
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        api_user: str | None = None,
        api_pass: str | None = None,
        # The hosted-media HTTP timeout. The job queue's ``ack_wait`` MUST stay
        # above this (C-3 / 2.6) or JetStream redelivers a job whose handler is
        # still mid-extraction — see JobQueue(ack_wait_seconds=...).
        timeout_seconds: float = 120.0,
        client: Any | None = None,
    ) -> None:
        self._endpoint = endpoint.rstrip("/") if endpoint else None
        self._api_user = api_user
        self._api_pass = api_pass
        self._timeout = timeout_seconds
        self._client = client  # injectable httpx.AsyncClient for tests
        self._owned_client = client is None

    @classmethod
    def from_env(cls) -> "MediaClient":
        """Build from env. ``LEGBA_MEDIA_API_URL`` unset → refusal-only client."""
        import os

        return cls(
            endpoint=configured_media_endpoint(),
            api_user=os.getenv("MODELS_API_USER") or None,
            api_pass=os.getenv("MODELS_API_PASS") or None,
            timeout_seconds=float(os.getenv("LEGBA_MEDIA_API_TIMEOUT", "120")),
        )

    @property
    def has_endpoint(self) -> bool:
        return self._endpoint is not None

    # ------------------------------------------------------------------
    # Public extraction
    # ------------------------------------------------------------------

    async def extract(
        self,
        *,
        media_ref: str,
        extraction: MediaExtraction,
        modality: str = "audio",
        mime_type: str | None = None,
        language_hint: str | None = None,
    ) -> MediaExtractionResult:
        """Run one extraction against ``media_ref``.

        Real HTTP call only. No endpoint configured →
        :class:`MediaEndpointNotConfiguredError`; endpoint down →
        :class:`MediaEndpointUnreachable` (caller retries). Never a stub.
        """
        if self._endpoint is None:
            logger.error(
                "media_client.refused reason=endpoint_not_configured "
                "env=LEGBA_MEDIA_API_URL extraction=%s media_ref=%s",
                extraction, media_ref,
            )
            raise MediaEndpointNotConfiguredError(
                "no hosted media endpoint configured (LEGBA_MEDIA_API_URL "
                f"unset) — refusing {extraction!r} of {media_ref!r}; no stub "
                "output may land in the signal pool"
            )

        path = EXTRACTION_PATHS.get(extraction, f"/{extraction}")
        body: dict[str, Any] = {
            "media_ref": media_ref,
            "extraction": extraction,
            "modality": modality,
        }
        if mime_type:
            body["mime_type"] = mime_type
        if language_hint:
            body["language_hint"] = language_hint

        started = time.monotonic()
        data = await self._post(path, body)

        latency_ms = (time.monotonic() - started) * 1000.0
        # hosted response contract (mirrors the nlp_service shape): a primary
        # text field + optional detail. Be liberal in what we accept.
        text = (
            data.get("text")
            or data.get("transcript")
            or data.get("caption")
            or data.get("ocr_text")
            or ""
        )
        detail = data.get("detail") if isinstance(data.get("detail"), dict) else {}
        if not detail:
            detail = {k: v for k, v in data.items() if k not in {"text", "ms"}}
        return MediaExtractionResult(
            extraction=extraction,
            text=str(text),
            detail=detail,
            model=str(data.get("model") or EXTRACTION_MODELS.get(extraction, "")),
            source="hosted",
            latency_ms=latency_ms,
        )

    # ------------------------------------------------------------------
    # HTTP internals
    # ------------------------------------------------------------------

    def _ensure_client(self):
        if self._client is None:
            import httpx

            auth = None
            if self._api_user and self._api_pass is not None:
                auth = httpx.BasicAuth(self._api_user, self._api_pass)
            self._client = httpx.AsyncClient(
                base_url=self._endpoint,
                auth=auth,
                timeout=httpx.Timeout(self._timeout),
            )
            self._owned_client = True
        return self._client

    async def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        import httpx

        client = self._ensure_client()
        try:
            resp = await client.post(path, json=body)
        except httpx.HTTPError as exc:
            raise MediaEndpointUnreachable(f"POST {path}: {exc}") from exc
        if resp.status_code >= 500:
            raise MediaEndpointUnreachable(
                f"POST {path} -> HTTP {resp.status_code}"
            )
        if resp.status_code >= 400:
            # A 4xx is a real, deterministic answer from a reachable endpoint
            # (bad request / unsupported media) — surface it, terminal.
            raise MediaExtractionError(
                f"POST {path} -> HTTP {resp.status_code}: {resp.text[:200]}"
            )
        try:
            return resp.json()
        except Exception as exc:
            raise MediaExtractionError(
                f"POST {path} non-JSON response: {exc}"
            ) from exc

    async def aclose(self) -> None:
        if self._client is not None and self._owned_client:
            await self._client.aclose()
            self._client = None

    async def __aenter__(self) -> "MediaClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.aclose()


class MediaExtractionError(RuntimeError):
    """A reachable endpoint refused / mangled the request (4xx / non-JSON).

    A real answer the worker surfaces as a terminal job failure.
    """


class MediaEndpointUnreachable(RuntimeError):
    """The configured endpoint is down / 5xx — transient.

    Propagates out of the handler so the worker releases the claim and lets
    JetStream redeliver (retry), failing terminally only at ``max_deliver``.
    """


__all__ = [
    "MediaClient",
    "EXTRACTION_PATHS",
    "MediaEndpointNotConfiguredError",
    "MediaEndpointUnreachable",
    "MediaExtractionError",
]

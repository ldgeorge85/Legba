# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Legba Media — hosted media-extraction service (Whisper / VLM / OCR).

The deployable sibling of ``legba-models``. It exposes the exact HTTP contract
the Legba runtime's :class:`legba.runtime.jobs.media_client.MediaClient` POSTs to:

  * ``POST /transcribe`` — audio/video → transcript (Whisper)
  * ``POST /caption``    — image → caption / objects (a VLM)
  * ``POST /ocr``        — image/blob → text (an OCR engine)
  * ``POST /detect``     — image → object detections (a detector)
  * ``GET  /health``     — liveness + which backends are loaded (open, no auth)

Request body (matches ``MediaClient.extract``)::

    {"media_ref": "<url-or-uri>", "extraction": "transcribe",
     "modality": "audio", "mime_type": "audio/mpeg", "language_hint": "en"}

Response body (matches ``MediaClient._post`` parsing)::

    {"text": "<extracted text>", "model": "<model id>", "detail": {...},
     "ms": <latency>}


D2 PREP-not-PROVISION posture — NO fabricated output
----------------------------------------------------

Per locked decision D2 (`planning/FEATURE_COMPLETE_PLAN.md`) this service is
built to be **deployed + tested easily**, but the live model endpoint is held
as the stated seam (`docs/SEAMS.md` #1). That means: a media backend must be
**registered** before the matching extraction endpoint will answer. With no
backend loaded the endpoint returns **HTTP 503** (fail-loud), exactly like
`MediaEndpointUnreachable` on the client — it NEVER returns a fabricated
transcript/caption. A fabricated result landing in the shared signal pool is
the provenance-poison failure A-2/G3 removed on the client side; this service
upholds the same invariant on the server side.

Wiring a real model is a small, contained edit: implement an
:class:`ExtractionBackend` and register it in :func:`load_backends` (guarded by
its optional heavy dependency, the same lazy-import pattern `legba-models`
uses for torch/transformers). Until then the service deploys + health-checks +
serves the seam loudly.

Auth (defense-in-depth) reuses the `legba-models` shared-secret pattern: when
``LEGBA_MEDIA_API_SECRET`` is set every extraction endpoint requires a matching
``X-Models-Secret`` header (constant-time compare); unset = no in-app auth
(the service is loopback / network-internal only). The Legba runtime presents
its `MODELS_API_PASS` as that header via the env wiring in `docker-compose.yml`.
"""

from __future__ import annotations

import hmac
import logging
import os
import time
from contextlib import asynccontextmanager
from typing import Any, Optional, Protocol

from fastapi import Depends, FastAPI, Header, HTTPException, status
from pydantic import BaseModel, Field

logger = logging.getLogger("legba.media")

# The four extraction kinds the runtime's MediaClient dispatches (one HTTP path
# each). Keep in lockstep with ``EXTRACTION_PATHS`` on the client.
EXTRACTIONS = ("transcribe", "caption", "ocr", "detect")

# ---------------------------------------------------------------------------
# Auth (mirrors legba-models/app/main.py exactly)
# ---------------------------------------------------------------------------
_SECRET_ENV = "LEGBA_MEDIA_API_SECRET"
_SECRET_HEADER = "X-Models-Secret"


def require_secret(
    x_models_secret: Optional[str] = Header(default=None, alias=_SECRET_HEADER),
) -> None:
    """Enforce the shared-secret header when one is configured (no-op if unset)."""
    configured = (os.getenv(_SECRET_ENV) or "").strip()
    if not configured:
        return
    presented = (x_models_secret or "").strip()
    if not presented or not hmac.compare_digest(presented, configured):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"missing or invalid {_SECRET_HEADER} header",
        )


# ---------------------------------------------------------------------------
# Backend contract + registry
# ---------------------------------------------------------------------------


class ExtractionBackend(Protocol):
    """One media-extraction backend (a real model behind one extraction kind).

    A live backend loads its model in ``__init__`` / a factory and returns a
    real extraction in :meth:`run`. ``model`` is the id recorded in Legba's
    provenance (``raw_provenance.model``).
    """

    model: str

    def run(self, *, media_ref: str, mime_type: str | None,
            language_hint: str | None) -> dict[str, Any]:
        """Return ``{"text": str, "detail": dict}`` for ``media_ref``."""
        ...


# extraction-kind -> loaded backend. Empty until a real model is registered in
# load_backends(); an empty slot ⇒ 503 (fail-loud, never a fabricated result).
BACKENDS: dict[str, ExtractionBackend] = {}


def load_backends() -> None:
    """Register the available real model backends.

    DEPLOY HOOK (the D2 seam boundary). This is intentionally empty in the
    shipped tree: no hosted media model is provisioned yet (SEAMS #1). To bring
    a backend online, lazily import its heavy dependency (so the import failing
    on a box without the model degrades to "endpoint 503" rather than crashing
    the whole service) and register it, e.g.::

        try:
            from .backends.whisper import WhisperBackend
            BACKENDS["transcribe"] = WhisperBackend(
                model_id=os.getenv("LEGBA_MEDIA_WHISPER_MODEL",
                                   "openai/whisper-large-v3"),
            )
        except Exception as exc:  # missing weights / no GPU / bad config
            logger.warning("transcribe backend unavailable: %s", exc)

    Each registered kind flips its endpoint from 503 to a real extraction with
    zero other changes — the HTTP contract here already matches the runtime
    client.
    """
    enabled = [e.strip() for e in
               (os.getenv("LEGBA_MEDIA_BACKENDS") or "").split(",") if e.strip()]
    if not enabled:
        logger.warning(
            "no media backends configured (LEGBA_MEDIA_BACKENDS empty) — every "
            "extraction endpoint will return 503 (the declared seam, fail-loud); "
            "no fabricated output is ever returned. See load_backends() to wire "
            "a real Whisper/VLM/OCR model."
        )
        return
    # When a deployment lists a backend it has NOT actually wired in-tree, fail
    # loud at startup rather than silently serving 503 on a kind the operator
    # believes is live.
    unknown = [e for e in enabled if e not in EXTRACTIONS]
    if unknown:
        raise RuntimeError(
            f"LEGBA_MEDIA_BACKENDS lists unknown extraction kinds {unknown}; "
            f"valid kinds: {list(EXTRACTIONS)}"
        )
    missing = [e for e in enabled if e not in BACKENDS]
    if missing:
        raise RuntimeError(
            f"LEGBA_MEDIA_BACKENDS requests {missing} but no backend is wired "
            f"in load_backends() for them — refusing to start half-configured "
            f"(would serve a 503 the operator thinks is live). Implement + "
            f"register the backend or remove it from LEGBA_MEDIA_BACKENDS."
        )


@asynccontextmanager
async def lifespan(app: FastAPI):
    load_backends()
    logger.info(
        "legba-media up — loaded backends: %s",
        sorted(BACKENDS.keys()) or "(none — serving the seam, 503 on extract)",
    )
    yield
    BACKENDS.clear()


app = FastAPI(title="Legba Media", version="0.1.0", lifespan=lifespan)


# ---------------------------------------------------------------------------
# Schemas (mirror MediaClient.extract's request + _post's response parsing)
# ---------------------------------------------------------------------------


class ExtractRequest(BaseModel):
    media_ref: str
    extraction: str
    modality: str = "audio"
    mime_type: Optional[str] = None
    language_hint: Optional[str] = None


class ExtractResponse(BaseModel):
    text: str = ""
    model: str = ""
    detail: dict[str, Any] = Field(default_factory=dict)
    ms: float = 0.0


def _run_extraction(kind: str, req: ExtractRequest) -> ExtractResponse:
    """Dispatch one extraction to its backend, or 503 if none is loaded.

    The 503 is the server-side mirror of the client's ``MediaEndpointUnreachable``
    — a reachable service that has not loaded a model for ``kind``. It is NEVER
    a fabricated transcript: an absent backend fails loud so no synthetic row
    can land in the signal pool.
    """
    backend = BACKENDS.get(kind)
    if backend is None:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=(
                f"no '{kind}' media backend loaded (declared seam #1: the live "
                f"media model is not provisioned). Wire one in "
                f"app.main.load_backends() and set LEGBA_MEDIA_BACKENDS. The "
                f"service refuses to fabricate output."
            ),
        )
    t0 = time.perf_counter()
    out = backend.run(
        media_ref=req.media_ref,
        mime_type=req.mime_type,
        language_hint=req.language_hint,
    )
    ms = (time.perf_counter() - t0) * 1000.0
    return ExtractResponse(
        text=str(out.get("text", "")),
        model=backend.model,
        detail=out.get("detail") or {},
        ms=round(ms, 1),
    )


@app.get("/health")
def health() -> dict[str, Any]:
    """Liveness + which extraction backends are loaded. Open (no auth)."""
    loaded = sorted(BACKENDS.keys())
    return {
        # 'ok' once the process is up; 'seam' makes the unprovisioned state
        # visible to an operator hitting /health (NOT an error — the service
        # is up and correctly serving the declared seam).
        "status": "ok" if loaded else "seam",
        "backends_loaded": loaded,
        "backends_available": list(EXTRACTIONS),
        "seam": "media model endpoint not provisioned (SEAMS #1)" if not loaded
        else None,
    }


@app.post("/transcribe", response_model=ExtractResponse,
          dependencies=[Depends(require_secret)])
def transcribe(req: ExtractRequest) -> ExtractResponse:
    return _run_extraction("transcribe", req)


@app.post("/caption", response_model=ExtractResponse,
          dependencies=[Depends(require_secret)])
def caption(req: ExtractRequest) -> ExtractResponse:
    return _run_extraction("caption", req)


@app.post("/ocr", response_model=ExtractResponse,
          dependencies=[Depends(require_secret)])
def ocr(req: ExtractRequest) -> ExtractResponse:
    return _run_extraction("ocr", req)


@app.post("/detect", response_model=ExtractResponse,
          dependencies=[Depends(require_secret)])
def detect(req: ExtractRequest) -> ExtractResponse:
    return _run_extraction("detect", req)

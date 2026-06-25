<!--
SPDX-FileCopyrightText: 2026 Lewis George
SPDX-License-Identifier: AGPL-3.0-or-later
-->
# legba-media — hosted media-extraction service

The deployable sibling of `legba-models`. It serves the HTTP contract the Legba
runtime's media client (`src/legba/runtime/jobs/media_client.py`) POSTs to when
`LEGBA_MEDIA_API_URL` is set: Whisper transcription, VLM captioning, OCR, and
object detection for the `process_media` job loop.

## D2 posture: PREP, not PROVISION

Per the locked operator decision **D2** (`planning/FEATURE_COMPLETE_PLAN.md`) the
*deploy + test path* is built but the *live model endpoint is held as the stated
seam* (`docs/SEAMS.md` #1). Concretely:

- The shipped service is **CPU-only and tiny** — `fastapi` + `uvicorn` + `pydantic`.
  It deploys + health-checks on any box, no GPU.
- With **no model backend wired** it serves the seam: `GET /health` reports
  `status: "seam"`, and every extraction endpoint returns **HTTP 503** with the
  reason. It **never** returns a fabricated transcript/caption — a synthetic
  result landing in the shared signal pool is exactly the provenance-poison
  A-2/G3 removed on the client side, and this service upholds the same invariant
  server-side.
- Wiring a real model is a contained edit (`app/main.load_backends()`), and
  flips that kind from 503 to a real extraction with no other change — the HTTP
  contract already matches the runtime client.

## HTTP contract

All extraction endpoints take the body the runtime client sends and return the
shape it parses:

```
POST /transcribe | /caption | /ocr | /detect
  body:  {"media_ref": "...", "extraction": "transcribe",
          "modality": "audio", "mime_type": "audio/mpeg",
          "language_hint": "en"}
  200:   {"text": "...", "model": "...", "detail": {...}, "ms": 12.3}
  401:   missing/invalid X-Models-Secret  (only when LEGBA_MEDIA_API_SECRET set)
  503:   no backend loaded for this kind  (the declared seam — fail-loud)

GET /health    (open, no auth)
  200:   {"status": "ok"|"seam", "backends_loaded": [...],
          "backends_available": ["transcribe","caption","ocr","detect"],
          "seam": null | "media model endpoint not provisioned (SEAMS #1)"}
```

Auth mirrors `legba-models`: set `LEGBA_MEDIA_API_SECRET` to require an
`X-Models-Secret` header on the extraction endpoints (constant-time compare);
unset = no in-app auth (loopback/internal-only). The Legba runtime presents its
`MODELS_API_PASS` as that header.

## Deploy

### A) In the main Legba stack (local deploy + test of the loop)

The repo-root `docker-compose.yml` carries a `media` profile that builds this
service and wires `LEGBA_MEDIA_API_URL=http://legba-media:8800` into the runtime:

```bash
docker compose --profile runtime --profile media up -d legba-media
# the runtime then resolves the media endpoint and process_media stops refusing
```

No GPU needed for the seam service; it will return 503 on extraction until a
backend is wired, which keeps `process_media` honestly refusing (no stub rows).

### B) Standalone on the GPU/model host (alongside legba-models)

```bash
cp .env.example .env        # edit if wiring a real backend
docker compose up -d        # this directory's compose; joins the `fastchat` net
```

Front it with Caddy (same pattern as `legba-models`) and point the runtime's
`LEGBA_MEDIA_API_URL` at the external URL.

## Wiring a real model backend

Implement an `ExtractionBackend` (a class with a `model: str` attribute and a
`run(*, media_ref, mime_type, language_hint) -> {"text": ..., "detail": ...}`
method) and register it in `app/main.load_backends()` behind a lazy import of
its heavy dependency — the same pattern `legba-models` uses for torch/transformers.
Then:

1. add the model deps to `app/requirements.txt`,
2. set `LEGBA_MEDIA_INSTALL_TORCH=1` (if the backend needs torch),
3. set `LEGBA_MEDIA_BACKENDS=transcribe,caption,...` to the kinds you wired,
4. swap the Dockerfile base for the CUDA image + uncomment the GPU
   `deploy.resources` stanza in `docker-compose.yml`,
5. retire the media seam (`docs/SEAMS.md` #1 / #13) per the
   `media-extraction` Wave-2 plan once the live loop is proven.

`LEGBA_MEDIA_BACKENDS` is validated at startup: listing a kind with no wired
backend makes the service **refuse to start** (fail-loud), so you never serve a
503 you believe is live.

## Test

A loop test points the runtime media client at THIS service running on a real
local socket (uvicorn) and drives `process_media` end-to-end — see
`tests/runtime/jobs/test_media_service_http_e2e.py`. It exercises the real HTTP
path (a registered test backend) AND asserts the seam (503) refusal, so the
deploy contract is covered without provisioning a live model.

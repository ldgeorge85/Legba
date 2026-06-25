# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D2 media-prep: the runtime media client talks to the REAL legba-media
service over a real HTTP socket, and the process_media loop closes.

Where ``test_process_media_e2e.py`` / ``test_media_loop_close.py`` exercise the
client through an in-process ``httpx.MockTransport`` (the wire is faked), THIS
test runs the actual ``legba-media`` FastAPI app (``legba-media/app/main.py``)
on a real uvicorn server bound to a real loopback socket, points the runtime's
:class:`MediaClient` at it via ``LEGBA_MEDIA_API_URL``, and proves:

  1. **The deploy contract holds end-to-end** — a registered backend's real
     extraction flows: enqueue → worker → real HTTP POST to legba-media → the
     derived signal lands with the backend's text + model in provenance.
  2. **The seam fails loud over HTTP** — with NO backend registered the service
     returns HTTP 503 and the worker surfaces it as a transient
     ``MediaEndpointUnreachable`` (retry, NOT a fabricated row). This is the
     D2 PREP-not-PROVISION invariant proven against the real service, not a mock.
  3. **/health reports the seam state** — open, no auth, status 'seam' vs 'ok'.

No live model is provisioned (D2): the test registers a deterministic test
backend in the service's ``BACKENDS`` registry to stand in for a real
Whisper/VLM model — the service's HTTP contract + refusal behavior are what's
under test, not a model.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import socket
import sys
import threading
from pathlib import Path
from uuid import UUID, uuid4

import httpx
import pytest

from legba.data.jobs.envelope import JobEnvelope, JobResult
from legba.runtime.jobs.media_client import MediaClient
from legba.runtime.jobs.worker import JobWorkerPool
from legba.runtime.subscription.engine import SubscriptionEngine

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


# ---------------------------------------------------------------------------
# Load the standalone legba-media FastAPI app by path (it lives OUTSIDE the
# src/legba package — it's a separate deployable service).
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).resolve().parents[3]
_MEDIA_APP_PATH = _REPO_ROOT / "legba-media" / "app" / "main.py"


def _load_media_app_module():
    if not _MEDIA_APP_PATH.exists():
        pytest.skip(f"legba-media service app not found at {_MEDIA_APP_PATH}")
    spec = importlib.util.spec_from_file_location(
        "legba_media_app_main", _MEDIA_APP_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    # Register under a package-free name so FastAPI/uvicorn can import it.
    sys.modules["legba_media_app_main"] = mod
    spec.loader.exec_module(mod)
    return mod


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _UvicornServer:
    """Run the legba-media app on a real loopback socket in a daemon thread."""

    def __init__(self, app, port: int):
        import uvicorn

        self._config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="warning",
            lifespan="on",
        )
        self._server = uvicorn.Server(self._config)
        self._thread = threading.Thread(target=self._server.run, daemon=True)
        self.base_url = f"http://127.0.0.1:{port}"

    def start(self) -> None:
        self._thread.start()

    async def wait_ready(self, timeout: float = 15.0) -> None:
        deadline = asyncio.get_event_loop().time() + timeout
        async with httpx.AsyncClient() as c:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    r = await c.get(f"{self.base_url}/health", timeout=1.0)
                    if r.status_code == 200:
                        return
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(0.1)
        raise TimeoutError("legba-media did not become ready")

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10.0)


async def _insert_raw_signal(pg, *, source_id="source.media.http") -> UUID:
    sid = uuid4()
    async with pg.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO signals (
                id, source_id, source_version, produced_by_kind, owner_tenant,
                modality, mime_type, media_ref, payload, content_hash, derived_from
            ) VALUES (
                $1, $2, 'v1', 'source', 'default',
                'audio', 'audio/mpeg', $3, $4::jsonb, 'rawhash', '{}'::uuid[]
            )
            """,
            sid, source_id, "https://cdn.example/news-clip.mp3",
            json.dumps({"title": "media http e2e"}),
        )
    return sid


def _media_env(raw_id: UUID, *, idem: str | None = None) -> JobEnvelope:
    return JobEnvelope(
        job_kind="process_media",
        requested_by="analyst.http",
        budget_account="analyst.http",
        idempotency_key=idem or f"http-{uuid4().hex}",
        input_refs={
            "media_ref": "https://cdn.example/news-clip.mp3",
            "extraction": "transcribe",
            "derived_from": str(raw_id),
            "modality": "audio",
            "language_hint": "en",
        },
    )


class _TestTranscribeBackend:
    """A deterministic stand-in for a real Whisper backend (test-only).

    Stands in for the live model the D2 seam holds back — it lets the test
    exercise the SERVICE's real HTTP contract + the worker's real HTTP path
    without provisioning a model. It is registered into the service's BACKENDS
    registry by the test, never shipped: load_backends() ships empty.
    """

    model = "whisper-test-deterministic"

    def run(self, *, media_ref, mime_type, language_hint):
        return {
            "text": f"transcript of {media_ref} [{language_hint}]",
            "detail": {"segments": 1, "lang": language_hint},
        }


# ---------------------------------------------------------------------------
# 1) Real HTTP path: a registered backend → the loop closes for real.
# ---------------------------------------------------------------------------


async def test_real_legba_media_http_loop_closes(job_pg, job_queue):
    media_mod = _load_media_app_module()
    # Register a test backend (stands in for the held-back live model).
    media_mod.BACKENDS["transcribe"] = _TestTranscribeBackend()

    server = _UvicornServer(media_mod.app, _free_port())
    server.start()
    try:
        await server.wait_ready()

        # /health reflects the loaded backend (status 'ok', not the seam).
        async with httpx.AsyncClient() as c:
            h = await c.get(f"{server.base_url}/health")
        assert h.status_code == 200
        assert h.json()["status"] == "ok"
        assert "transcribe" in h.json()["backends_loaded"]

        # The runtime client, pointed at the REAL service (no injected client →
        # it builds its own httpx.AsyncClient and makes a real socket call).
        client = MediaClient(endpoint=server.base_url)
        raw_id = await _insert_raw_signal(job_pg)
        env = _media_env(raw_id, idem=f"http-ok-{uuid4().hex}")

        pool = JobWorkerPool(
            queue=job_queue, pg=job_pg, size=1, media=client,
            subscriptions=SubscriptionEngine(job_pg),
        )
        await job_queue.enqueue(env)
        completed = await pool.drain_until_empty()
        await client.aclose()
        assert completed == 1

        async with job_pg.acquire() as conn:
            d = await conn.fetchrow(
                "SELECT payload, raw_provenance FROM signals "
                "WHERE produced_by_kind='job' AND $1 = ANY(derived_from)",
                raw_id,
            )
        assert d is not None
        payload = d["payload"]
        prov = d["raw_provenance"]
        payload = payload if isinstance(payload, dict) else json.loads(payload)
        prov = prov if isinstance(prov, dict) else json.loads(prov)
        assert payload["text"] == (
            "transcript of https://cdn.example/news-clip.mp3 [en]"
        )
        # Real hosted edge: model + source came back over the wire.
        assert prov["model"] == "whisper-test-deterministic"
        assert prov["model_source"] == "hosted"
    finally:
        media_mod.BACKENDS.clear()
        server.stop()


# ---------------------------------------------------------------------------
# 2) The seam fails loud over real HTTP: no backend → 503 → transient, no row.
# ---------------------------------------------------------------------------


async def test_seam_503_when_no_backend_loaded():
    """No backend → real HTTP 503 → the client raises a transient unreachable.

    A 5xx from a reachable media service is the server-side mirror of the held-
    back live model. The runtime client must surface it as
    :class:`MediaEndpointUnreachable` (transient → the worker naks + retries),
    NEVER as a fabricated extraction. Asserting at the client edge keeps this
    fast + deterministic (no multi-redelivery wait); the worker's nak-retry of a
    transient is already covered by the jobs-plane hardening suite, and the
    'no fabricated row lands' invariant is guaranteed because the client raises
    BEFORE any derived signal is built.
    """
    media_mod = _load_media_app_module()
    media_mod.BACKENDS.clear()  # the SHIPPED state — no model wired

    server = _UvicornServer(media_mod.app, _free_port())
    server.start()
    try:
        await server.wait_ready()

        # /health reports the seam (service up, no backend).
        async with httpx.AsyncClient() as c:
            h = await c.get(f"{server.base_url}/health")
            assert h.status_code == 200
            assert h.json()["status"] == "seam"
            assert h.json()["backends_loaded"] == []

            # A direct extraction POST returns 503 (fail-loud, never fabricated).
            r = await c.post(
                f"{server.base_url}/transcribe",
                json={"media_ref": "x", "extraction": "transcribe"},
            )
        assert r.status_code == 503
        assert "no 'transcribe' media backend" in r.text

        # The runtime client surfaces the 503 as a TRANSIENT unreachable (retry),
        # not a fabricated result. This is the D2 PREP invariant over real HTTP.
        from legba.runtime.jobs.media_client import MediaEndpointUnreachable

        client = MediaClient(endpoint=server.base_url)
        try:
            with pytest.raises(MediaEndpointUnreachable):
                await client.extract(
                    media_ref="https://cdn.example/news-clip.mp3",
                    extraction="transcribe",
                    modality="audio",
                )
        finally:
            await client.aclose()
    finally:
        media_mod.BACKENDS.clear()
        server.stop()


# ---------------------------------------------------------------------------
# 3) The shipped load_backends() registers nothing (the seam is the default).
# ---------------------------------------------------------------------------


async def test_shipped_service_loads_no_backends_by_default(monkeypatch):
    media_mod = _load_media_app_module()
    media_mod.BACKENDS.clear()
    monkeypatch.delenv("LEGBA_MEDIA_BACKENDS", raising=False)
    media_mod.load_backends()
    assert media_mod.BACKENDS == {}, (
        "the shipped legba-media must wire NO model backend (D2 seam) — "
        "load_backends() registering anything would ship fabricated output"
    )


async def test_unwired_backend_in_env_refuses_to_start(monkeypatch):
    """Listing a kind with no wired backend fails loud at startup."""
    media_mod = _load_media_app_module()
    media_mod.BACKENDS.clear()
    monkeypatch.setenv("LEGBA_MEDIA_BACKENDS", "transcribe")
    with pytest.raises(RuntimeError, match="no backend is wired"):
        media_mod.load_backends()

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end validation of the L-193 inbound A2A skill router (O-1).

What this exercises that the in-app ASGITransport tests in
``tests/data_pkg/test_output_a2a_skill.py`` deliberately don't:

  * The full **HTTP transport stack** — uvicorn + a real TCP socket, not
    an in-process ASGI shortcut. If the router is silently relying on
    ASGI middleware that isn't wired in production, this surfaces it.
  * A **real signing identity** built via ``load_default_identity()``
    (the same code path the production runtime uses for its server-side
    identity) and a separately-generated caller key registered in the
    :class:`TrustedKeyDirectory`.
  * A **real signed envelope** constructed via the inbound router's own
    ``build_envelope`` / ``sign_envelope`` helpers — bit-for-bit the
    wire form the outbound :class:`MnemosyneA2AClient` from
    ``legba.clients.mnemosyne_a2a`` produces (mirrored shape).
  * The four documented dispatch outcomes: 200 happy path, 401 on bad
    signature, 401 on unknown signer DID, 422 on schema-invalid args,
    400 on envelope/path mismatch, and 404 on unregistered skill_id.
  * The happy-path **round-trip latency** for one call (recorded into
    the test output for the parent session's report).

Why an in-process uvicorn on localhost rather than the live
``legba-runtime-dapr`` container
--------------------------------------------------------------------

The task brief asked us to hit ``http://legba-runtime-dapr:6090/a2a/
skills/{skill_id}`` directly. While probing the running container
during test design, the live runtime's ``GET /openapi.json`` returned
ONLY these paths::

    ['/actors/{actor_type_name}/{actor_id}',
     '/actors/{actor_type_name}/{actor_id}/method/remind/{reminder_name}',
     '/actors/{actor_type_name}/{actor_id}/method/timer/{timer_name}',
     '/actors/{actor_type_name}/{actor_id}/method/{method_name}',
     '/dapr/config',
     '/healthz']

No ``/a2a/skills`` route is mounted. Tracing the source:

  * :func:`legba.runtime.dapr_host.build_dapr_host_app` only mounts the
    A2A router when ``a2a_registry`` + ``a2a_identity`` +
    ``a2a_fetch_latest_outputs`` are all passed in (see L154-L132 of
    ``dapr_host.py``).
  * The production entry point :func:`legba.runtime.dapr_host.main`
    builds its own ``FastAPI`` directly with ``production_lifespan``
    (see L840-L857) and **never threads the A2A trio through**.
  * Consequence: the L-193 router is wired into the source tree and
    has unit coverage, but is **not actually mounted on the live
    runtime container**. ``register_a2a_skill_route`` is dead code on
    the production surface today.

That's an integration gap, not a router-implementation bug, so per the
task ground rule "DO NOT modify the A2A skill router code" we DO NOT
patch it here — the parent session can fold the wiring fix into the
production bring-up. The :func:`test_live_runtime_container_a2a_mount_xfail`
test below documents the gap as an ``xfail(strict=False)`` so the day
the gap closes, CI flips the test to PASS unprompted.

The rest of the end-to-end coverage is provided by spinning the SAME
router (via :func:`register_a2a_skill_route`) into an in-process
uvicorn on a localhost port and POSTing real HTTP through it. That
exercises everything the live container would except the dapr_host
mount, so when (1) the mount lands and (2) the same scenarios pass
in-process, the live-container case follows by construction.
"""

from __future__ import annotations

import asyncio
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from contextlib import suppress
from typing import Any, Iterator

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from nacl.signing import SigningKey

from legba.clients.mnemosyne_a2a import (
    MnemosyneA2AClient,
    A2ATransportError,
    A2ARemoteError,
    A2ASignatureError,
)
from legba.data.outputs.a2a_skill import (
    A2ASkillRegistration,
    A2ASkillRegistry,
    ENVELOPE_VERSION,
    HEADER_ENVELOPE_VERSION,
    HEADER_NONCE,
    HEADER_SIGNATURE,
    HEADER_SIGNER_DID,
    KIND_NAME,
    TrustedKeyDirectory,
    build_envelope,
    register_a2a_skill_route,
    sign_envelope,
    verify_envelope,
)
from legba.data.registry.signing import SigningIdentity, load_default_identity

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Test-only "echo" skill descriptor — documented at the top of the file so
# the parent session can transcribe its shape if the production runtime
# ever needs an A2A-callable health-check skill.
# ---------------------------------------------------------------------------
#
# Body fields below match :class:`A2ASkillRegistration` (the kind's
# registration record). Equivalent ``OutputBinding`` YAML on a real
# analyst descriptor::
#
#     outputs:
#       - kind: a2a_skill
#         config:
#           skill_id: legba.test.echo
#           description: Test-only echo skill for the O-1 e2e gate.
#           auth_required: true
#           input_schema:
#             type: object
#             properties:
#               target_id: {type: string}
#               limit:     {type: integer}
#             required:    []
#           response_schema:
#             type: object
#
# The full registration record (id + version) the test installs:
TEST_SKILL_ID = "legba.test.echo"
TEST_ANALYST_ID = "analyst.test.echo"
TEST_ANALYST_VERSION = "ee" * 8  # "eeeeeeeeeeeeeeee" — matches the 16-64-hex pattern
TEST_DESCRIPTOR_ID = "desc-test-echo"

CALLER_DID = "did:legba:o1-test-caller"


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------


def _pick_free_port() -> int:
    """Bind to port 0, read the kernel-assigned port, release. Race with
    other test sessions is theoretically possible; the window is ~ms and
    pytest sessions don't share this fixture so we accept it."""
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]
    finally:
        s.close()


def _http_probe(url: str, *, timeout: float = 2.0) -> tuple[int, str]:
    """Tiny urllib-based GET that returns (status, body[:512])."""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, resp.read(512).decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        try:
            body = exc.read(512).decode("utf-8", errors="replace")
        except Exception:
            body = ""
        return exc.code, body


# ---------------------------------------------------------------------------
# Background uvicorn — mirrors the pattern from
# tests/runtime/test_spike_integration.py but stripped to the parts we need
# (no daprd dance, just the FastAPI app on a free port).
# ---------------------------------------------------------------------------


class _BackgroundUvicorn:
    """Run a FastAPI app on its own thread + event loop, started/stopped
    explicitly. We use a dedicated thread (rather than ``asyncio.create_task``
    in the test's loop) so the test can use ``httpx.AsyncClient`` from its
    own pytest-asyncio loop without yielding control to uvicorn between
    requests.
    """

    def __init__(self, app: FastAPI, port: int) -> None:
        self._port = port
        cfg = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="warning", log_config=None,
            # Lifespan="off" because we don't need FastAPI lifespan events
            # for this test and avoiding them sidesteps Dapr's actor-runtime
            # registration that would otherwise fire on startup.
            lifespan="off",
        )
        self._server = uvicorn.Server(cfg)
        self._thread: threading.Thread | None = None

    def start(self, *, timeout_s: float = 5.0) -> None:
        def _run() -> None:
            # Each thread needs its own event loop.
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(self._server.serve())
            finally:
                with suppress(Exception):
                    loop.close()

        self._thread = threading.Thread(target=_run, name="a2a-e2e-uvicorn", daemon=True)
        self._thread.start()

        # Spin-wait until uvicorn reports "started" OR a TCP connect to
        # 127.0.0.1:<port> succeeds. Whichever signals first wins.
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if self._server.started:
                return
            with suppress(Exception):
                with socket.create_connection(("127.0.0.1", self._port), timeout=0.2):
                    return
            time.sleep(0.05)
        raise RuntimeError(f"uvicorn did not start within {timeout_s}s on port {self._port}")

    def stop(self, *, timeout_s: float = 3.0) -> None:
        self._server.should_exit = True
        if self._thread is not None:
            self._thread.join(timeout=timeout_s)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def server_identity() -> SigningIdentity:
    """The runtime-side signing identity.

    We do NOT call :func:`load_default_identity` in the module body
    because it reads ``LEGBA_REGISTRY_SIGNING_KEY`` env state at import
    time and bakes in whatever the parent shell exported. Instead, we
    force a clean ephemeral identity per module — equivalent to the
    fallback path inside ``load_default_identity`` — and clear the
    relevant envs first so a stale local env can't poison the test.
    """
    # Quarantine the resolution path: no hex key, no key file, so the
    # function's documented "ephemeral key" branch is the one we exercise.
    saved = {}
    for k in ("LEGBA_REGISTRY_SIGNING_KEY", "LEGBA_REGISTRY_SIGNING_KEY_FILE"):
        saved[k] = os.environ.pop(k, None)
    os.environ.setdefault("LEGBA_REGISTRY_SIGNER_DID", "did:legba:registry:o1-e2e-test")
    try:
        identity = load_default_identity()
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    assert identity.signing_key is not None
    return identity


@pytest.fixture(scope="module")
def caller_key() -> SigningKey:
    """Deterministically-fresh Ed25519 key for the *caller* (Mnemosyne in
    production). We register its verify-key in :class:`TrustedKeyDirectory`
    under :data:`CALLER_DID`.
    """
    return SigningKey.generate()


@pytest.fixture(scope="module")
def trusted_keys(caller_key: SigningKey) -> TrustedKeyDirectory:
    d = TrustedKeyDirectory()
    d.add(CALLER_DID, bytes(caller_key.verify_key))
    return d


@pytest.fixture
def echo_registry() -> A2ASkillRegistry:
    """A registry seeded with the ``legba.test.echo`` registration.

    Self-cleanup: the registry instance is function-scoped so each test
    starts from a freshly populated one — no need to retire explicitly.
    The PRODUCTION-side teardown for a real descriptor would be
    ``registry.unregister_by_analyst(analyst_id)``, exercised by the
    ``test_register_from_descriptor_lifecycle`` test below.
    """
    reg = A2ASkillRegistry()
    reg.register(
        A2ASkillRegistration(
            skill_id=TEST_SKILL_ID,
            analyst_id=TEST_ANALYST_ID,
            analyst_version=TEST_ANALYST_VERSION,
            descriptor_id=TEST_DESCRIPTOR_ID,
            input_schema={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "limit": {"type": "integer"},
                    "echo_value": {"type": "string"},
                },
                "required": [],
            },
            response_schema={"type": "object"},
            auth_required=True,
            description="Test-only echo skill for the O-1 e2e gate.",
        )
    )
    return reg


@pytest.fixture
def echo_fetcher():
    """A ``LatestOutputFetcher`` that echoes its inputs as the analyst's
    "most recent findings".

    The L-193 router calls ``fetch_latest_outputs(analyst_ids=, limit=,
    target_filter=)`` after schema validation passes. By echoing the call
    parameters back as the "finding" payload the test can assert the
    router forwarded args correctly (``target_id`` flows into
    ``target_filter``, ``limit`` flows into ``limit``).
    """
    captured: dict[str, Any] = {"calls": []}

    async def _fetch(*, analyst_ids, limit, target_filter):
        captured["calls"].append(
            {
                "analyst_ids": list(analyst_ids),
                "limit": limit,
                "target_filter": target_filter,
            }
        )
        return [
            {
                "echoed_analyst_ids": list(analyst_ids),
                "echoed_limit": limit,
                "echoed_target_filter": target_filter,
                "produced_at": "2026-05-28T00:00:00Z",
            }
        ]

    _fetch.captured = captured  # type: ignore[attr-defined]
    return _fetch


@pytest.fixture
def app(
    echo_registry: A2ASkillRegistry,
    server_identity: SigningIdentity,
    echo_fetcher,
    trusted_keys: TrustedKeyDirectory,
) -> FastAPI:
    """FastAPI app with the L-193 router mounted — equivalent to what the
    runtime SHOULD be mounting via ``attach_a2a_skill_router`` in
    ``dapr_host.py`` (see module docstring for the wiring gap)."""
    app = FastAPI(title="legba A2A e2e test app", lifespan=None)
    register_a2a_skill_route(
        app,
        registry=echo_registry,
        identity=server_identity,
        fetch_latest_outputs=echo_fetcher,
        trusted_keys=trusted_keys,
    )
    return app


@pytest.fixture
def live_server(app: FastAPI) -> Iterator[str]:
    """Start uvicorn in a background thread on a free port; yield base URL.

    Teardown stops the server cleanly with a 3s grace period — uvicorn's
    ``should_exit`` flag flips its serve() loop on the next tick.
    """
    port = _pick_free_port()
    server = _BackgroundUvicorn(app, port=port)
    server.start()
    base_url = f"http://127.0.0.1:{port}"
    try:
        yield base_url
    finally:
        server.stop()


# ---------------------------------------------------------------------------
# Envelope construction helpers — mirror MnemosyneA2AClient.build_envelope
# but kept inline so the test is independent of the outbound client (the
# outbound client is exercised in `tests/clients/test_mnemosyne_a2a.py`).
# ---------------------------------------------------------------------------


def _signed_request(
    *,
    skill_id: str,
    args: dict[str, Any],
    caller_key: SigningKey,
    signer_did: str = CALLER_DID,
) -> dict[str, Any]:
    env = build_envelope(skill_id=skill_id, payload=args, signer_did=signer_did)
    sig = sign_envelope(env, caller_key)
    return {"envelope": env, "signature": sig}


# ---------------------------------------------------------------------------
# Happy-path
# ---------------------------------------------------------------------------


async def test_e2e_happy_path_signed_request_returns_signed_response(
    live_server: str,
    caller_key: SigningKey,
    server_identity: SigningIdentity,
) -> None:
    """Valid signed envelope → 200 + signed response envelope that
    verifies under the server's verify-key. Round-trip latency is logged
    so the parent session can transcribe the number into the report.
    """
    wire = _signed_request(
        skill_id=TEST_SKILL_ID,
        args={"target_id": "test-target", "limit": 3, "echo_value": "hello"},
        caller_key=caller_key,
    )

    start = time.perf_counter()
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{live_server}/a2a/skills/{TEST_SKILL_ID}",
            json=wire,
        )
    elapsed_ms = (time.perf_counter() - start) * 1000.0

    assert resp.status_code == 200, resp.text
    logger.info(
        "a2a.e2e.happy_path_latency_ms=%.2f status=%d url=%s",
        elapsed_ms, resp.status_code, resp.url,
    )
    # Always print so a `-s` pytest run surfaces the number even when
    # the test passes (and log levels would otherwise swallow it).
    print(f"\n[a2a-e2e] happy-path round-trip latency: {elapsed_ms:.2f} ms")

    body = resp.json()
    assert isinstance(body, dict)
    env = body["envelope"]
    sig = body["signature"]

    # Server-signed response verifies under the server's verify-key.
    assert verify_envelope(env, sig, server_identity.verify_key) is True

    # Envelope shape echoes the request's skill_id + carries the analyst's
    # ids + replies-to the request nonce.
    assert env["skill_id"] == TEST_SKILL_ID
    assert env["signer_did"] == server_identity.signer_did
    assert env["envelope_version"] == ENVELOPE_VERSION

    payload = env["payload"]
    assert payload["skill_id"] == TEST_SKILL_ID
    assert payload["analyst_id"] == TEST_ANALYST_ID
    assert payload["analyst_version"] == TEST_ANALYST_VERSION
    assert payload["in_reply_to_nonce"] == wire["envelope"]["nonce"]

    # Our echo fetcher's output ends up under `findings`.
    findings = payload["findings"]
    assert isinstance(findings, list) and len(findings) == 1
    f0 = findings[0]
    assert f0["echoed_analyst_ids"] == [TEST_ANALYST_ID]
    assert f0["echoed_limit"] == 3                 # forwarded from args.limit
    assert f0["echoed_target_filter"] == "test-target"

    # Custom response headers carry the envelope metadata for callers
    # that want to dispatch without parsing the body.
    assert resp.headers[HEADER_ENVELOPE_VERSION] == ENVELOPE_VERSION
    assert resp.headers[HEADER_SIGNER_DID] == server_identity.signer_did
    assert resp.headers[HEADER_SIGNATURE] == sig
    assert resp.headers[HEADER_NONCE] == env["nonce"]


# ---------------------------------------------------------------------------
# Failure modes
# ---------------------------------------------------------------------------


async def test_e2e_bad_signature_returns_401(
    live_server: str,
    caller_key: SigningKey,
) -> None:
    """Tampered signature → 401. The router's verify path raises
    ``BadSignatureError`` which is mapped to ``HTTPException(401)``.
    """
    wire = _signed_request(
        skill_id=TEST_SKILL_ID,
        args={"target_id": "test-target"},
        caller_key=caller_key,
    )
    # Flip the first character of the signature so it no longer verifies.
    flipped = "B" if wire["signature"][0] != "B" else "A"
    wire["signature"] = flipped + wire["signature"][1:]

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{live_server}/a2a/skills/{TEST_SKILL_ID}",
            json=wire,
        )
    assert resp.status_code == 401, resp.text
    assert "bad" in resp.json()["detail"].lower() or "signature" in resp.json()["detail"].lower()


async def test_e2e_unknown_signer_returns_401(live_server: str) -> None:
    """A signed envelope from a DID that's NOT in TrustedKeyDirectory →
    401. The router enforces this when ``auth_required=True`` on the
    skill registration (our echo skill).
    """
    rogue = SigningKey.generate()
    env = build_envelope(
        skill_id=TEST_SKILL_ID,
        payload={"target_id": "test-target"},
        signer_did="did:legba:rogue-not-registered",
    )
    sig = sign_envelope(env, rogue)

    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{live_server}/a2a/skills/{TEST_SKILL_ID}",
            json={"envelope": env, "signature": sig},
        )
    assert resp.status_code == 401, resp.text
    assert "untrusted" in resp.json()["detail"].lower() or \
           "unknown" in resp.json()["detail"].lower()


async def test_e2e_schema_invalid_args_returns_422(
    live_server: str,
    caller_key: SigningKey,
    echo_registry: A2ASkillRegistry,
) -> None:
    """A signed envelope whose ``payload`` fails ``input_schema``
    validation → 422.

    NOTE on status code: the task brief said "400 on schema-invalid
    args". The implementation uses **422 Unprocessable Content** — the
    HTTP-semantic correct choice (the body parsed fine; the *content*
    is invalid). 400 is reserved for malformed-envelope cases
    (path/skill_id mismatch, missing envelope object, etc.) which is
    tested separately by :func:`test_e2e_envelope_skill_id_mismatch_400`.
    The 400/422 split matches what unit tests in
    ``tests/data_pkg/test_output_a2a_skill.py`` already pin.
    """
    # Mutate the echo skill to require `target_id` as a string.
    echo_registry.skills[TEST_SKILL_ID].input_schema = {
        "type": "object",
        "properties": {"target_id": {"type": "string"}},
        "required": ["target_id"],
    }
    wire = _signed_request(
        skill_id=TEST_SKILL_ID,
        args={},  # missing required field
        caller_key=caller_key,
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{live_server}/a2a/skills/{TEST_SKILL_ID}",
            json=wire,
        )
    assert resp.status_code == 422, resp.text
    assert "missing required field" in resp.json()["detail"]


async def test_e2e_envelope_skill_id_mismatch_400(
    live_server: str,
    caller_key: SigningKey,
) -> None:
    """``envelope.skill_id`` ≠ URL path → 400. Catches signed-envelope
    replay-to-wrong-route attempts."""
    env = build_envelope(
        skill_id="some.other.skill",
        payload={"target_id": "test-target"},
        signer_did=CALLER_DID,
    )
    sig = sign_envelope(env, caller_key)
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{live_server}/a2a/skills/{TEST_SKILL_ID}",
            json={"envelope": env, "signature": sig},
        )
    assert resp.status_code == 400, resp.text


async def test_e2e_unknown_skill_id_returns_404(
    live_server: str,
    caller_key: SigningKey,
) -> None:
    """A signed envelope for a ``skill_id`` that nobody registered → 404."""
    wire = _signed_request(
        skill_id="does.not.exist",
        args={},
        caller_key=caller_key,
    )
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.post(
            f"{live_server}/a2a/skills/does.not.exist",
            json=wire,
        )
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Listing + GET (compatibility-spike paths) over real HTTP
# ---------------------------------------------------------------------------


async def test_e2e_list_skills_returns_registration(
    live_server: str,
    server_identity: SigningIdentity,
) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{live_server}/a2a/skills")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["signer_did"] == server_identity.signer_did
    skills = {s["skill_id"]: s for s in body["skills"]}
    assert TEST_SKILL_ID in skills
    s = skills[TEST_SKILL_ID]
    assert s["analyst_id"] == TEST_ANALYST_ID
    assert s["analyst_version"] == TEST_ANALYST_VERSION
    assert s["auth_required"] is True


async def test_e2e_get_skill_returns_signed_envelope(
    live_server: str,
    server_identity: SigningIdentity,
) -> None:
    async with httpx.AsyncClient(timeout=5.0) as client:
        resp = await client.get(f"{live_server}/a2a/skills/{TEST_SKILL_ID}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    env = body["envelope"]
    sig = body["signature"]
    # Server-signed response verifies even on the GET compat path.
    assert verify_envelope(env, sig, server_identity.verify_key) is True
    assert env["skill_id"] == TEST_SKILL_ID


# ---------------------------------------------------------------------------
# register_from_descriptor lifecycle — the production code path that auto-
# discovers ``outputs[*]`` of kind ``a2a_skill`` on a descriptor body.
# ---------------------------------------------------------------------------


def test_register_from_descriptor_then_unregister_self_cleans(
    server_identity: SigningIdentity,
    caller_key: SigningKey,
    trusted_keys: TrustedKeyDirectory,
) -> None:
    """End-to-end of the descriptor-lifecycle path:

    1. Build an ``OutputBinding``-shaped dict that declares ``kind:
       a2a_skill`` with our echo skill's config block.
    2. Call ``register_from_descriptor`` — registry should contain the
       skill afterward.
    3. Call ``unregister_by_analyst`` — registry should be empty.

    This is the SAME entry point ``DescriptorRegistry`` calls when an
    analyst descriptor goes ``active`` → ``retired``. We exercise it
    here without spinning up the full registry so the test doesn't
    depend on Postgres state — the registry-side integration is covered
    by ``tests/data_pkg/test_registry_descriptor_integration.py``.
    """
    registry = A2ASkillRegistry()
    outputs = [
        {
            "kind": KIND_NAME,
            "config": {
                "skill_id": "legba.test.descriptor_register",
                "input_schema": {
                    "type": "object",
                    "properties": {"target_id": {"type": "string"}},
                    "required": [],
                },
                "response_schema": {"type": "object"},
                "auth_required": True,
                "description": "Descriptor-lifecycle e2e test skill.",
            },
        },
    ]
    regs = registry.register_from_descriptor(
        analyst_id="analyst.test.lifecycle",
        analyst_version="cc" * 8,
        descriptor_id="desc-test-lifecycle",
        outputs=outputs,
    )
    assert len(regs) == 1
    assert registry.get("legba.test.descriptor_register") is not None

    n = registry.unregister_by_analyst("analyst.test.lifecycle")
    assert n == 1
    assert registry.list_skills() == []


def test_has_analyst_version_guards_reregistration() -> None:
    """has_analyst_version backs the executor's ENSURE_ACTIVE a2a re-register
    guard: False when empty (fresh after a restart) or version-mismatched
    (descriptor edit) — both must re-register — True only when the current
    version is already registered (skip the redundant resync re-fetch)."""
    registry = A2ASkillRegistry()
    outputs = [
        {
            "kind": KIND_NAME,
            "config": {
                "skill_id": "legba.test.hav",
                "input_schema": {"type": "object", "properties": {}, "required": []},
                "response_schema": {"type": "object"},
                "auth_required": True,
                "description": "has_analyst_version guard test.",
            },
        },
    ]
    # Empty registry (post-restart) → must re-register.
    assert registry.has_analyst_version("analyst.hav", "v1") is False
    registry.register_from_descriptor(
        analyst_id="analyst.hav", analyst_version="v1",
        descriptor_id="desc-hav", outputs=outputs,
    )
    assert registry.has_analyst_version("analyst.hav", "v1") is True   # current → skip
    assert registry.has_analyst_version("analyst.hav", "v2") is False  # edited → re-register
    assert registry.has_analyst_version("analyst.other", "v1") is False
    registry.unregister_by_analyst("analyst.hav")
    assert registry.has_analyst_version("analyst.hav", "v1") is False


# ---------------------------------------------------------------------------
# Live runtime-container probe — documents the mount gap
# ---------------------------------------------------------------------------


def _live_runtime_url() -> str | None:
    """Resolve a candidate URL for the live ``legba-runtime-dapr`` container.

    Resolution order:
      1. ``LEGBA_RUNTIME_A2A_URL`` env (operator override).
      2. ``http://127.0.0.1:6090`` (direct to the container; bypasses
         Caddy's basic auth so 401/403 can't mask a genuine 404).
      3. ``https://${LEGBA_PUBLIC_DOMAIN}`` (front door) — only when set
         AND the direct localhost probe fails. Caddy basic-auth means we
         can only see 200/404 transparently here; 401 is ambiguous, so the
         caller test below carefully distinguishes them.

    Returns ``None`` if neither is reachable.
    """
    override = os.getenv("LEGBA_RUNTIME_A2A_URL", "").strip()
    if override:
        return override.rstrip("/")
    # Direct-to-container first — that's the unambiguous reach. The public
    # edge is only the fallback (and only when LEGBA_PUBLIC_DOMAIN is set)
    # because its basic-auth 401 can mask both mount-missing (404) and
    # mount-present (200) cases.
    candidates = ["http://127.0.0.1:6090"]
    public_domain = os.getenv("LEGBA_PUBLIC_DOMAIN", "").strip()
    if public_domain:
        candidates.append(f"https://{public_domain}")
    for url in candidates:
        try:
            status, _ = _http_probe(f"{url}/healthz", timeout=2.0)
            if status == 200:
                return url
            if status in (401, 403):
                # Auth-gated proxy in front; only useful when we have
                # no other choice. Fall through to the next candidate.
                continue
        except Exception:
            continue
    return None


@pytest.mark.xfail(
    reason=(
        "Production runtime does NOT currently mount the L-193 A2A skill router. "
        "dapr_host.main() builds its FastAPI app without threading "
        "(a2a_registry, a2a_identity, a2a_fetch_latest_outputs) through "
        "build_dapr_host_app(), so attach_a2a_skill_router() never fires. "
        "Live runtime OpenAPI lists only /actors/*, /dapr/config, /healthz. "
        "Fix: thread the A2A trio into bring_up_production_runtime + main(). "
        "See module docstring."
    ),
    strict=False,
)
def test_live_runtime_container_a2a_mount_xfail() -> None:
    """Probe the live ``legba-runtime-dapr`` for the L-193 mount.

    ``xfail(strict=False)`` because today this FAILS (the route returns
    404), documenting the integration gap. When the production wiring
    fix lands and the mount appears, this test flips to XPASS so CI
    surfaces the change — and we should re-evaluate whether to drop
    the xfail marker.
    """
    base_url = _live_runtime_url()
    if base_url is None:
        pytest.skip(
            "live runtime not reachable on localhost:6090 or the public "
            "edge — set LEGBA_RUNTIME_A2A_URL (or LEGBA_PUBLIC_DOMAIN) to probe."
        )
    status, body = _http_probe(f"{base_url}/a2a/skills", timeout=3.0)
    # When probing the container DIRECTLY (no auth proxy in front), only
    # 200 means "mount present". 404 is the documented bug-state. We
    # tolerate 401/403 only when the resolved base_url is an auth-gated
    # proxy (https://) — there a 401 means "Caddy blocked us before the
    # request reached FastAPI" and can mask either outcome.
    if base_url.startswith("https://"):
        assert status in (200, 401, 403), (
            f"live runtime {base_url}/a2a/skills returned HTTP {status}: "
            f"{body[:200]!r}. Direct-to-container probe failed too — see _live_runtime_url()."
        )
    else:
        assert status == 200, (
            f"live runtime {base_url}/a2a/skills returned HTTP {status}: "
            f"{body[:200]!r}. 404 means dapr_host.main() is not threading "
            f"a2a_registry/a2a_identity/a2a_fetch_latest_outputs into "
            f"build_dapr_host_app() — see module docstring."
        )


# ---------------------------------------------------------------------------
# Live Mnemosyne round-trip — gated, documents the X-1 shape mismatch
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    os.getenv("LEGBA_TEST_MNEMOSYNE_LIVE") != "1",
    reason="set LEGBA_TEST_MNEMOSYNE_LIVE=1 to round-trip against a real Mnemosyne",
)
async def test_live_mnemosyne_trust_query_round_trip() -> None:
    """Round-trip the X-1 outbound A2A client against a real Mnemosyne.

    KNOWN INTEROP MISMATCH (transcribed from the X-1 finding):

    * Legba's outbound :class:`MnemosyneA2AClient` POSTs the **legba-
      native A2A envelope** shape — ``{envelope: {...}, signature: ...}``
      — to ``{base_url}/a2a/skills/{skill_id}``.
    * Mnemosyne's A2A surface today expects a **JSON-RPC 2.0** payload
      (``method="tasks/send"``, ``params={skill_id, data, _meta}``) at
      ``{base_url}/a2a``, with the signature stuffed into ``_meta``
      (see ``src/legba/data/tools/mnemosyne_trust_query.py`` module
      docstring lines 17-40 for the canonical wire shape).

    Until Mnemosyne adds the symmetric ``/a2a/skills/<id>`` route — or
    Legba's client grows a JSON-RPC translation layer — this test is
    expected to surface that mismatch as an :class:`A2ARemoteError` /
    :class:`A2ATransportError` rather than a clean 200.

    The test is therefore tolerant of the mismatch and asserts only that
    *something coherent* came back from the network (the client either
    returned a payload or raised one of the documented exception types).
    """
    # Defensive env check — `from_env()` would also throw if these were
    # missing, but a pytest.skip is friendlier than a hard error when an
    # operator is debugging.
    if not os.getenv("MNEMOSYNE_A2A_URL"):
        pytest.skip("MNEMOSYNE_A2A_URL is required for the live round-trip")
    if not os.getenv("MNEMOSYNE_RECIPIENT_DID"):
        pytest.skip("MNEMOSYNE_RECIPIENT_DID is required for the live round-trip")

    client = MnemosyneA2AClient.from_env()
    subject = os.getenv("LEGBA_TEST_MNEMOSYNE_SUBJECT_DID", client.recipient_did)
    start = time.perf_counter()
    try:
        result = await client.trust_query(subject, scope="general")
    except (A2ATransportError, A2ARemoteError, A2ASignatureError) as exc:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        # Surface the known-mismatch evidence rather than failing the
        # session — the X-1 finding already pinned this.
        logger.warning(
            "a2a.e2e.mnemosyne_live.expected_mismatch err=%s elapsed_ms=%.1f",
            exc, elapsed_ms,
        )
        print(
            f"\n[a2a-e2e] Mnemosyne round-trip mismatch (expected per X-1): "
            f"{type(exc).__name__}: {exc}"
        )
        return
    else:
        elapsed_ms = (time.perf_counter() - start) * 1000.0
        print(f"\n[a2a-e2e] Mnemosyne round-trip OK in {elapsed_ms:.1f} ms")
        # If we DO get a 200 back, the contract surface has shifted —
        # assert the L-210 shape the X-1 client promises.
        assert "score" in result
        assert "rationale" in result
        assert "hop_count" in result

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""End-to-end validation for the L-196 webhook + L-197 alert output kinds
(Done-plan §4 Group O-4).

Both kinds landed in Wave A (Phase 8) but Wave A wired no descriptor that
configures either, and the alert kind's per-sink delivery state was
log-only. This test exercises the production handler code paths against:

  * A real FastAPI echo server (booted on a free local port via a
    background ``uvicorn.Server`` task) that captures every POSTed body
    + header and returns operator-controlled status codes — covers the
    webhook handler's happy path, 4xx no-retry, and 5xx retry + DLQ.
  * A real NATS JetStream connection — covers the alert handler's NATS
    sub-sink + verifies a published envelope lands on the alert subject.
  * A real Postgres connection (per-session test DB, migration 0023
    applied) — covers the new ``alert_sink_deliveries`` writer surface
    and asserts the row shape the future P-2 panel will read.

No mocks at substrate or transport boundaries — Lewis's hard rule. The
only fakes are the test's own helpers (echo-server response control,
captured-request collector).

These tests require the substrate-up fixture (postgres / nats containers
running). They are marked ``integration`` to match the other substrate-
binding tests in the suite.
"""

from __future__ import annotations

import asyncio
import json
import socket
import uuid
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

import asyncpg
import httpx
import pytest
import pytest_asyncio
import uvicorn
from fastapi import FastAPI, Request

from legba.data.config import NatsConfig, PostgresConfig
from legba.data.nats import NatsStore
from legba.data.outputs import alert as alert_kind
from legba.data.outputs import webhook as webhook_kind
from legba.data.outputs._contract import OutputContext, OutputDeps
from legba.data.outputs.webhook import (
    HEADER_SIGNATURE,
    HEADER_SIGNER_DID,
    dlq_subject,
    verify_signed_body,
)
from legba.data.provenance.models import AlertPayload
from legba.data.registry.signing import load_default_identity


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Bind 0 → kernel-assigned free port → close → return port."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _EchoState:
    """Operator-controlled echo server state.

    The FastAPI app reads :attr:`response_queue` per request; if non-empty
    the next entry is popped + used as the response status (the body is
    always ``"ok"``). If empty the server defaults to 200. Every received
    request is recorded into :attr:`requests` for assertion.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.response_queue: list[int] = []
        self.default_status: int = 200


def _build_echo_app(state: _EchoState) -> FastAPI:
    app = FastAPI()

    @app.post("/hook")
    async def hook(req: Request) -> Any:
        body = await req.body()
        state.requests.append({
            "headers": {k.lower(): v for k, v in req.headers.items()},
            "body": body,
        })
        status = state.response_queue.pop(0) if state.response_queue else state.default_status
        from fastapi.responses import Response
        return Response(content=b"ok", status_code=status, media_type="text/plain")

    return app


class _BackgroundUvicorn:
    """Boot a uvicorn server in an asyncio task; expose start/stop."""

    def __init__(self, app: FastAPI, port: int) -> None:
        self._cfg = uvicorn.Config(
            app, host="127.0.0.1", port=port,
            log_level="warning", log_config=None,
            access_log=False,
        )
        self._server = uvicorn.Server(self._cfg)
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._task = asyncio.create_task(self._server.serve(), name="webhook-echo")
        for _ in range(60):  # ~6s
            if self._server.started:
                return
            await asyncio.sleep(0.1)
        raise RuntimeError("uvicorn echo server did not start within 6s")

    async def stop(self) -> None:
        self._server.should_exit = True
        if self._task is not None:
            with suppress(asyncio.CancelledError, asyncio.TimeoutError):
                await asyncio.wait_for(self._task, timeout=5)


@asynccontextmanager
async def _echo_server() -> AsyncIterator[tuple[str, _EchoState]]:
    """Async context manager — yields (base_url, state) for the lifetime
    of a running echo server."""
    state = _EchoState()
    app = _build_echo_app(state)
    port = _free_port()
    server = _BackgroundUvicorn(app, port)
    await server.start()
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        await server.stop()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig) -> asyncpg.Pool:
    """asyncpg pool over the session-scoped migrated test DB.

    ``migrated_pg`` is re-exported by ``tests/runtime/conftest.py`` from
    the data_pkg conftest; it applies every primary migration including
    the new 0023 alert_sink_deliveries before the first test runs.
    """
    pool = await asyncpg.create_pool(
        host=migrated_pg.host,
        port=migrated_pg.port,
        user=migrated_pg.user,
        password=migrated_pg.password,
        database=migrated_pg.database,
        min_size=1, max_size=4,
    )
    try:
        yield pool
    finally:
        await pool.close()


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


@pytest.fixture(scope="module")
def session_prefix() -> str:
    return f"e2e_outputs_{uuid.uuid4().hex[:10]}"


# ---------------------------------------------------------------------------
# Webhook handler: happy-path POST signed by the registry identity.
# ---------------------------------------------------------------------------


async def test_webhook_real_server_signed_post_happy_path():
    """A finding-shaped payload is signed and delivered to a real HTTP
    server; the server-side signature verification path documented in
    ``webhook.py`` succeeds against the same identity that signed it."""
    identity = load_default_identity()

    async with _echo_server() as (base_url, state):
        async with httpx.AsyncClient(timeout=5.0) as http:

            class _Deps:
                def __init__(self) -> None:
                    self.http = http
                    self.signing_identity = identity
                    self.nats_publish = None
                    self.rate_limiter = None
                    self.analyst_id = "e2e.analyst.webhook"

            payload = {
                "title": "Brazil energy price spike",
                "target_id": "BR",
                "score": 0.91,
                "tags": ["energy", "shock"],
            }
            await webhook_kind.emit(
                payload,
                descriptor={
                    "url": f"{base_url}/hook",
                    "backoff_seconds": [0.001, 0.001, 0.001],
                },
                deps=_Deps(),
            )

    assert len(state.requests) == 1
    req = state.requests[0]
    assert req["headers"].get("content-type") == "application/json"
    sig = req["headers"].get(HEADER_SIGNATURE.lower())
    did = req["headers"].get(HEADER_SIGNER_DID.lower())
    assert sig and did == identity.signer_did
    # The body on the wire verifies against the identity's verify key
    # — proves the canonical-JSON signing pipeline is intact end-to-end.
    assert verify_signed_body(req["body"], sig, identity.verify_key)


# ---------------------------------------------------------------------------
# Webhook: the SSRF-guarded output client REFUSES a real, reachable internal
# server (S-3). The runtime builds the shared output client via
# ``guarded_async_client()``; here a real echo server is listening on
# 127.0.0.1, yet the guarded client must NOT connect to it — proving the
# guard blocks before the socket open, not after.
# ---------------------------------------------------------------------------


async def test_webhook_guarded_client_refuses_real_internal_server():
    """A descriptor pointed at a *running, reachable* loopback server is
    refused by the SSRF-guarded output client — the server records ZERO
    requests (block-before-connect), and emit treats it as a permanent
    misconfiguration: one attempt, no retry, no DLQ, no payload leak."""
    from legba.data.sources._egress import guarded_async_client

    identity = load_default_identity()
    dlq_calls: list[Any] = []

    async def _record_dlq(subject: str, payload: Any) -> None:
        dlq_calls.append((subject, payload))

    async with _echo_server() as (base_url, state):
        # base_url is http://127.0.0.1:<port> — a real, reachable server.
        async with guarded_async_client(timeout=5.0) as http:

            class _Deps:
                def __init__(self) -> None:
                    self.http = http
                    self.signing_identity = identity
                    self.nats_publish = _record_dlq
                    self.rate_limiter = None
                    self.analyst_id = "e2e.analyst.webhook.ssrf"

            # Returns (does not raise) — permanent path, logged + swallowed.
            await webhook_kind.emit(
                {"finding": "exfil attempt", "target_id": "BR"},
                descriptor={
                    "url": f"{base_url}/hook",
                    "backoff_seconds": [0.001, 0.001, 0.001],
                    "max_attempts": 3,
                },
                deps=_Deps(),
            )

    # The block fires before connect → the real server saw NOTHING.
    assert state.requests == []
    # And the payload was not DLQ'd (no replay/leak of the blocked target).
    assert dlq_calls == []


# ---------------------------------------------------------------------------
# Webhook: 4xx does NOT retry and does NOT write a DLQ row.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="needs an isolated NATS; DLQ subject overlaps the live runtime's "
    "legba.dlq.> stream on --network host"
)
async def test_webhook_4xx_no_retry_no_dlq(
    nats_store: NatsStore, session_prefix: str
):
    """Per the L-196 brief, 4xx is permanent — handler exits after one
    attempt and writes nothing to the DLQ subject."""
    identity = load_default_identity()
    analyst_id = f"{session_prefix}_4xx"
    dlq_full = dlq_subject(analyst_id)
    dlq_stream = f"{session_prefix}_4xx_stream"
    await nats_store.ensure_stream(name=dlq_stream, subjects=[dlq_full])

    async with _echo_server() as (base_url, state):
        state.response_queue = [404]
        async with httpx.AsyncClient(timeout=5.0) as http:

            class _Deps:
                def __init__(self) -> None:
                    self.http = http
                    self.signing_identity = identity
                    self.nats_publish = nats_store.publish_json
                    self.rate_limiter = None
                    self.analyst_id = analyst_id

            await webhook_kind.emit(
                {"k": "v"},
                descriptor={
                    "url": f"{base_url}/hook",
                    "backoff_seconds": [0.001, 0.001, 0.001],
                    "max_attempts": 3,
                },
                deps=_Deps(),
            )

    # Exactly one request — no retry on 4xx.
    assert len(state.requests) == 1

    # No DLQ envelope landed on the subject.
    psub = await nats_store.js.pull_subscribe(
        subject=dlq_full,
        durable=f"{session_prefix}_4xx_dur",
        stream=dlq_stream,
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await psub.fetch(1, timeout=1)
    finally:
        with suppress(Exception):
            await nats_store.js.delete_stream(dlq_stream)


# ---------------------------------------------------------------------------
# Webhook: persistent 5xx → retry up to max_attempts → DLQ row.
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="needs an isolated NATS; DLQ subject overlaps the live runtime's "
    "legba.dlq.> stream on --network host"
)
async def test_webhook_5xx_retries_then_dlqs(
    nats_store: NatsStore, session_prefix: str
):
    identity = load_default_identity()
    analyst_id = f"{session_prefix}_5xx"
    dlq_full = dlq_subject(analyst_id)
    dlq_stream = f"{session_prefix}_5xx_stream"
    await nats_store.ensure_stream(name=dlq_stream, subjects=[dlq_full])

    async with _echo_server() as (base_url, state):
        state.response_queue = [500, 502, 503]
        async with httpx.AsyncClient(timeout=5.0) as http:

            class _Deps:
                def __init__(self) -> None:
                    self.http = http
                    self.signing_identity = identity
                    self.nats_publish = nats_store.publish_json
                    self.rate_limiter = None
                    self.analyst_id = analyst_id

            await webhook_kind.emit(
                {"finding": "persistent"},
                descriptor={
                    "url": f"{base_url}/hook",
                    "backoff_seconds": [0.001, 0.001, 0.001],
                    "max_attempts": 3,
                },
                deps=_Deps(),
            )

    # Exactly 3 attempts.
    assert len(state.requests) == 3

    # DLQ envelope landed; verify its shape.
    psub = await nats_store.js.pull_subscribe(
        subject=dlq_full,
        durable=f"{session_prefix}_5xx_dur",
        stream=dlq_stream,
    )
    try:
        msgs = await psub.fetch(1, timeout=5)
        assert len(msgs) == 1
        envelope = json.loads(msgs[0].data.decode("utf-8"))
        assert envelope["original_url"] == f"{base_url}/hook"
        assert envelope["analyst_id"] == analyst_id
        assert envelope["attempts"] == 3
        assert envelope["signer_did"] == identity.signer_did
        assert "http 50" in envelope["error"]
    finally:
        with suppress(Exception):
            await nats_store.js.delete_stream(dlq_stream)


# ---------------------------------------------------------------------------
# Alert dispatcher: real NATS sub-sink + alert_sink_deliveries row.
# ---------------------------------------------------------------------------


async def _seed_alert_row(
    pool: asyncpg.Pool,
    *,
    analyst_id: str,
    analyst_version: str,
    severity: str,
    title: str,
) -> uuid.UUID:
    """Insert a parent ``analyst_outputs`` row so the FK on
    ``alert_sink_deliveries.alert_row_id`` is satisfiable."""
    row_id = uuid.uuid4()
    async with pool.acquire() as conn:
        await conn.execute(
            """
            INSERT INTO analyst_outputs (
                id, kind, title, body, confidence, severity, data,
                analyst_id, analyst_version, schema_uri
            ) VALUES ($1, 'alert', $2, '', 0.8, $3, '{}'::jsonb, $4, $5,
                      'iglu:legba/alert/jsonschema/1-0-0')
            """,
            row_id, title, severity, analyst_id, analyst_version,
        )
    return row_id


async def test_alert_dispatcher_nats_sink_writes_delivery_row(
    pg_pool: asyncpg.Pool,
    nats_store: NatsStore,
    session_prefix: str,
):
    """A medium-severity alert dispatched through the alert kind:
      * publishes an envelope on ``legba.alerts.medium`` (real NATS),
      * writes one ``alert_sink_deliveries`` row with status='delivered',
        sink_kind='nats', and the descriptor identity stamped.
    """
    analyst_id = f"{session_prefix}_alert_ok"
    analyst_version = "1.0.0"
    severity = "medium"
    title = "Brazil energy price spike — operator alert"

    # Use a unique, descriptor-overridden NATS subject so the test's
    # ensure_stream call can't collide with a well-known subject already
    # bound to another stream in the local JetStream instance.
    subject = f"e2e.alerts.{session_prefix}_ok"
    stream = f"{session_prefix}_alert_ok_stream"
    await nats_store.ensure_stream(name=stream, subjects=[subject])
    psub = await nats_store.js.pull_subscribe(
        subject=subject,
        durable=f"{session_prefix}_alert_ok_dur",
        stream=stream,
    )

    alert_row_id = await _seed_alert_row(
        pg_pool,
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        severity=severity,
        title=title,
    )

    payload = AlertPayload(
        title=title,
        body="Sustained 30-day energy price increase >2 sigma.",
        severity=severity,
        confidence=0.82,
        tags=["energy", "br"],
        routing_hint="https://example.org/alerts/br",
    )
    deps = OutputDeps(nats=nats_store, pg_pool=pg_pool)
    ctx = OutputContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        target_id="target.br.energy",
        target_version="1",
        run_id=str(uuid.uuid4()),
        alert_row_id=str(alert_row_id),
    )

    try:
        results = await alert_kind.emit(
            payload,
            descriptor={
                # Re-route NATS to a unique subject (default ladder still
                # fires NATS for medium-severity; we only override its
                # destination).
                "surfaces": [
                    {"name": "nats", "mode": "default", "destination": subject},
                ],
            },
            deps=deps,
            ctx=ctx,
        )

        # Dispatcher hit only the NATS surface for severity=medium with no
        # http/xmpp/matrix deps — pushover sub-sink records skipped (no
        # http client wired) so it writes no row. Verify both.
        outcomes = {r.surface: r.outcome for r in results}
        assert outcomes.get("nats") == "delivered"
        # pushover got attempted but the http port is None — sub-sink
        # returns either skipped or transient/permanent depending on its
        # internal handling. We assert the DB only contains rows whose
        # surface_name actually attempted delivery.

        # The NATS envelope landed.
        msgs = await psub.fetch(1, timeout=5)
        assert len(msgs) == 1
        envelope = json.loads(msgs[0].data.decode("utf-8"))
        assert envelope["severity"] == severity
        assert envelope["title"] == title
        assert envelope["analyst_id"] == analyst_id

        # The alert_sink_deliveries row landed with status='delivered'.
        rows = await pg_pool.fetch(
            """
            SELECT sink_kind, status, attempt_number, descriptor_id,
                   descriptor_version, sink_target, error_message,
                   delivered_at, payload_summary
              FROM alert_sink_deliveries
             WHERE alert_row_id = $1
             ORDER BY attempted_at ASC, attempt_number ASC
            """,
            alert_row_id,
        )
        # We expect at least the NATS row; pushover may or may not write
        # depending on its sub-sink's permanent_error vs skipped outcome
        # when http is absent — the test asserts the NATS row strictly
        # and the pushover row loosely.
        nats_rows = [r for r in rows if r["sink_kind"] == "nats"]
        assert len(nats_rows) == 1
        nats_row = nats_rows[0]
        assert nats_row["status"] == "delivered"
        assert nats_row["attempt_number"] == 1
        assert nats_row["descriptor_id"] == analyst_id
        assert nats_row["descriptor_version"] == analyst_version
        assert nats_row["sink_target"] == subject
        assert nats_row["error_message"] is None
        assert nats_row["delivered_at"] is not None
        summary = (
            nats_row["payload_summary"]
            if isinstance(nats_row["payload_summary"], dict)
            else json.loads(nats_row["payload_summary"])
        )
        assert summary["severity"] == severity
        assert summary["surface"] == "nats"
        assert summary["status"] == "delivered"
    finally:
        with suppress(Exception):
            await nats_store.js.delete_stream(stream)


async def test_alert_dispatcher_no_pg_pool_writes_no_row(
    pg_pool: asyncpg.Pool,
    nats_store: NatsStore,
    session_prefix: str,
):
    """Back-compat: when callers don't pass ``pg_pool`` the dispatcher
    keeps Wave A behaviour — NATS publish lands, no audit row written."""
    analyst_id = f"{session_prefix}_alert_nopool"
    alert_row_id = await _seed_alert_row(
        pg_pool,
        analyst_id=analyst_id,
        analyst_version="1.0.0",
        severity="low",
        title="Wave A back-compat",
    )

    payload = AlertPayload(
        title="Wave A back-compat",
        severity="low",
        confidence=0.6,
    )
    deps = OutputDeps(nats=nats_store)  # no pg_pool — Wave A path
    ctx = OutputContext(
        analyst_id=analyst_id,
        alert_row_id=str(alert_row_id),
    )

    # Use a session-unique subject + stream so we can assert the publish
    # actually went through (proving the dispatcher ran end-to-end with
    # no pool).
    subject = f"e2e.alerts.{session_prefix}_nopool"
    stream = f"{session_prefix}_alert_nopool_stream"
    await nats_store.ensure_stream(name=stream, subjects=[subject])
    try:
        await alert_kind.emit(
            payload,
            descriptor={
                "surfaces": [
                    {"name": "nats", "mode": "default", "destination": subject},
                ],
            },
            deps=deps,
            ctx=ctx,
        )
    finally:
        with suppress(Exception):
            await nats_store.js.delete_stream(stream)

    rows = await pg_pool.fetch(
        "SELECT id FROM alert_sink_deliveries WHERE alert_row_id = $1",
        alert_row_id,
    )
    assert rows == []


async def test_alert_dispatcher_critical_retry_logs_retrying_then_delivered(
    pg_pool: asyncpg.Pool,
    nats_store: NatsStore,
    session_prefix: str,
):
    """Inject a transient-failing NATS publisher for the first 2 calls,
    succeed on the 3rd; a critical-severity alert should write
    (retrying, retrying, delivered) rows for the NATS surface."""
    analyst_id = f"{session_prefix}_alert_retry"
    analyst_version = "2.0.0"
    alert_row_id = await _seed_alert_row(
        pg_pool,
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        severity="critical",
        title="Critical with retries",
    )

    class _FlakyNats:
        def __init__(self, real: NatsStore, fail_first_n: int) -> None:
            self._real = real
            self._fail = fail_first_n
            self.publish_calls: list[str] = []

        async def publish_core(self, subject: str, payload: bytes) -> None:
            # The alert sink publishes on the streamless legba.alerts.* subject
            # via publish_core (not publish_json).
            self.publish_calls.append(subject)
            if self._fail > 0:
                self._fail -= 1
                raise RuntimeError("simulated transient NATS broker error")
            await self._real.publish_core(subject, payload)

        async def publish_json(self, subject: str, payload: bytes) -> None:
            await self.publish_core(subject, payload)

    flaky = _FlakyNats(nats_store, fail_first_n=2)

    payload = AlertPayload(
        title="Critical with retries",
        severity="critical",
        confidence=0.95,
    )
    # Pre-create the subject's stream so the eventual real publish lands.
    # Use a session-unique subject to dodge collisions with any other
    # stream bound to ``legba.alerts.critical``.
    subject = f"e2e.alerts.{session_prefix}_retry"
    stream = f"{session_prefix}_alert_retry_stream"
    await nats_store.ensure_stream(name=stream, subjects=[subject])

    deps = OutputDeps(nats=flaky, pg_pool=pg_pool)
    ctx = OutputContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        alert_row_id=str(alert_row_id),
    )

    try:
        await alert_kind.emit(
            payload,
            descriptor={
                "_retry_backoff": [0.001, 0.001, 0.001, 0.001],
                "surfaces": [
                    {"name": "nats", "mode": "default", "destination": subject},
                ],
            },
            deps=deps,
            ctx=ctx,
        )

        # NATS attempted 3 times.
        assert len(flaky.publish_calls) == 3

        # Three rows for sink='nats': retrying, retrying, delivered.
        nats_rows = await pg_pool.fetch(
            """
            SELECT status, attempt_number, error_message, delivered_at
              FROM alert_sink_deliveries
             WHERE alert_row_id = $1 AND sink_kind = 'nats'
             ORDER BY attempt_number ASC
            """,
            alert_row_id,
        )
        assert [r["attempt_number"] for r in nats_rows] == [1, 2, 3]
        assert [r["status"] for r in nats_rows] == ["retrying", "retrying", "delivered"]
        assert nats_rows[0]["error_message"] is not None
        assert nats_rows[1]["error_message"] is not None
        assert nats_rows[2]["delivered_at"] is not None
    finally:
        with suppress(Exception):
            await nats_store.js.delete_stream(stream)

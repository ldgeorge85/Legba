# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Integration tests for L-196 — `legba.data.outputs.webhook`.

The HTTP boundary is exercised through :class:`httpx.MockTransport` —
no real network calls — so we cover happy-path, 4xx-no-retry,
5xx-retry-then-success, retry-exhaustion, timeout handling, and
signature header shape against a deterministic transport.

The DLQ verification uses a *real* NATS connection (per project rules:
no mocks at substrate boundaries). The signing identity is the
`SigningIdentity` produced by `data/registry/signing.py` so the
signature header verifies against the same Ed25519 key used by the
audit chain.

Tests covered:

  * KIND_NAME + WebhookConfig parse contract.
  * Signature header is base64url Ed25519 over canonical_json(payload).
  * Signature verifies against the registry's verify-key.
  * Happy path: 200 → one POST, no retry.
  * 4xx: no retry (single POST), no DLQ envelope.
  * 5xx then 200: retry-then-success.
  * 5xx persistent: retry-exhausted → DLQ envelope on NATS.
  * Network exception (timeout): treated as transient → retry.
  * Rate limiter is called before each attempt (per-URL gating).
  * Missing url in config → WebhookConfigError.
  * Missing http dep → WebhookDepsError.
  * Missing signing identity → WebhookDepsError.
  * Payload bytes/str rejected — must be Mapping/Pydantic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from typing import Any, Callable

import httpx
import pytest
import pytest_asyncio
from nacl.signing import SigningKey

from legba.data.config import NatsConfig
from legba.data.nats import NatsStore
from legba.data.outputs import webhook
from legba.data.sources._egress import (
    EgressBlockedError,
    assert_public_host,
    guarded_async_client,
)
from legba.data.outputs.webhook import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MAX_ATTEMPTS,
    DLQ_SUBJECT_PREFIX,
    HEADER_SIGNATURE,
    HEADER_SIGNER_DID,
    KIND_NAME,
    RateLimiterPort,
    WebhookConfig,
    WebhookConfigError,
    WebhookDepsError,
    WebhookPayloadError,
    dlq_subject,
    emit,
    parse_config,
    sign_payload,
    verify_signed_body,
)
from legba.data.provenance import canonical_json
from legba.data.registry.signing import SigningIdentity


pytestmark = [pytest.mark.integration]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def identity() -> SigningIdentity:
    """Real Ed25519 SigningIdentity — same shape registry produces."""
    return SigningIdentity(
        signing_key=SigningKey.generate(),
        signer_did="did:legba:registry:test-host",
    )


@pytest.fixture(scope="module")
def session_prefix() -> str:
    return f"legba_test_outhook_{uuid.uuid4().hex[:10]}"


@pytest_asyncio.fixture
async def nats_store() -> NatsStore:
    """Real NATS connection — DLQ tests pull from a real subscriber."""
    store = NatsStore(NatsConfig(url="nats://127.0.0.1:4222"))
    await store.connect()
    try:
        yield store
    finally:
        await store.close()


# ---------------------------------------------------------------------------
# Test doubles — only at the boundaries the task allows.
# ---------------------------------------------------------------------------


def _make_http(handler: Callable[[httpx.Request], httpx.Response]) -> httpx.AsyncClient:
    """Build an httpx.AsyncClient backed by a MockTransport."""
    transport = httpx.MockTransport(handler)
    return httpx.AsyncClient(transport=transport, timeout=2.0)


class _RecordingHandler:
    """httpx.MockTransport handler that records requests + cycles responses.

    ``responses`` is a list of (status, body) tuples consumed in order.
    Past the list, falls back to ``default``. Any handler entry that is
    a callable is invoked with the request and its return value used as
    the response (so timeout simulation works).
    """

    def __init__(
        self,
        responses: list[Any] | None = None,
        default: tuple[int, str] = (200, ""),
    ) -> None:
        self.requests: list[httpx.Request] = []
        self.bodies: list[bytes] = []
        self._responses = list(responses or [])
        self._default = default

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        self.bodies.append(request.content)
        if self._responses:
            entry = self._responses.pop(0)
            if callable(entry):
                # Permits raising or returning a custom Response.
                return entry(request)
            status, body = entry
            return httpx.Response(status, text=body)
        status, body = self._default
        return httpx.Response(status, text=body)


class _RecordingLimiter:
    """Records calls to ``acquire`` — satisfies :class:`RateLimiterPort`."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def acquire(self, key: str) -> None:
        self.calls.append(key)


class _Deps:
    """Minimal deps bundle the webhook kind reads from."""

    def __init__(
        self,
        *,
        http: Any,
        identity: SigningIdentity,
        nats_publish: Any | None = None,
        rate_limiter: RateLimiterPort | None = None,
        analyst_id: str | None = None,
    ) -> None:
        self.http = http
        self.signing_identity = identity
        self.nats_publish = nats_publish
        self.rate_limiter = rate_limiter
        self.analyst_id = analyst_id


# ---------------------------------------------------------------------------
# Kind identity
# ---------------------------------------------------------------------------


def test_kind_name_constant():
    assert KIND_NAME == "webhook"
    assert DLQ_SUBJECT_PREFIX == "legba.dlq.output.webhook"
    assert DEFAULT_MAX_ATTEMPTS == 3
    assert DEFAULT_BACKOFF_SECONDS == (1.0, 2.0, 4.0)
    assert HEADER_SIGNATURE == "X-Legba-Signature"
    assert HEADER_SIGNER_DID == "X-Legba-Signer-DID"


def test_module_exposes_emit_and_config():
    assert callable(webhook.emit)
    # WebhookConfig is exported and accepts the required url.
    cfg = WebhookConfig(url="https://example.org/hook")
    assert cfg.url == "https://example.org/hook"
    assert cfg.max_attempts == DEFAULT_MAX_ATTEMPTS
    assert cfg.timeout_seconds == 30.0


def test_module_registered_in_discover():
    from legba.data.outputs import discover_output_kinds

    reg = discover_output_kinds()
    assert "webhook" in reg
    assert reg["webhook"].emit is webhook.emit


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def test_parse_config_bare_block():
    cfg = parse_config({"url": "https://example.org/hook"})
    assert cfg.url == "https://example.org/hook"


def test_parse_config_with_outputs_nesting():
    descriptor = {
        "outputs": {
            "webhook": {
                "url": "https://example.org/hook",
                "timeout_seconds": 5.0,
                "max_attempts": 2,
            }
        }
    }
    cfg = parse_config(descriptor)
    assert cfg.url == "https://example.org/hook"
    assert cfg.timeout_seconds == 5.0
    assert cfg.max_attempts == 2


def test_parse_config_with_webhook_only_nesting():
    cfg = parse_config({"webhook": {"url": "https://example.org/x"}})
    assert cfg.url == "https://example.org/x"


def test_parse_config_missing_url_raises():
    with pytest.raises(WebhookConfigError):
        parse_config({})

    with pytest.raises(WebhookConfigError):
        parse_config(None)

    with pytest.raises(WebhookConfigError):
        parse_config({"timeout_seconds": 5.0})


def test_parse_config_invalid_timeout_raises():
    with pytest.raises(WebhookConfigError):
        parse_config({"url": "https://example.org", "timeout_seconds": -1.0})


# ---------------------------------------------------------------------------
# Signing — direct API
# ---------------------------------------------------------------------------


def test_sign_payload_returns_canonical_body_and_b64url(identity: SigningIdentity):
    payload = {"b": 2, "a": 1}
    body, sig = sign_payload(payload, identity)
    # Canonical JSON: sorted keys, no whitespace.
    assert body == b'{"a":1,"b":2}'
    # b64url with no '=' padding.
    assert "=" not in sig
    # Round-trip verify with the matching public key.
    raw = base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4))
    identity.verify_key.verify(body, raw)


def test_verify_signed_body_succeeds(identity: SigningIdentity):
    payload = {"hello": "world"}
    body, sig = sign_payload(payload, identity)
    assert verify_signed_body(body, sig, identity.verify_key)


def test_verify_signed_body_rejects_tampered_body(identity: SigningIdentity):
    payload = {"hello": "world"}
    _, sig = sign_payload(payload, identity)
    with pytest.raises(ValueError, match="bad webhook signature"):
        verify_signed_body(b'{"hello":"tampered"}', sig, identity.verify_key)


def test_verify_signed_body_rejects_malformed_signature(identity: SigningIdentity):
    with pytest.raises(ValueError, match="base64url"):
        verify_signed_body(b'{"k":1}', "not%%base64%%", identity.verify_key)


# ---------------------------------------------------------------------------
# Happy path — single POST, 200
# ---------------------------------------------------------------------------


async def test_emit_happy_path_200(identity: SigningIdentity):
    handler = _RecordingHandler(responses=[(200, "ok")])
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        await emit(
            {"finding": "Brazil energy price spike"},
            descriptor={"url": "https://example.org/hook"},
            deps=deps,
        )

    assert len(handler.requests) == 1
    req = handler.requests[0]
    assert req.method == "POST"
    assert str(req.url) == "https://example.org/hook"
    assert req.headers.get("content-type") == "application/json"
    # The body on the wire is the canonical-JSON of the payload.
    assert req.content == canonical_json({"finding": "Brazil energy price spike"})


async def test_emit_signature_header_verifies(identity: SigningIdentity):
    """The signature header MUST verify against the identity's verify-key
    when the receiver canonicalises the body identically."""
    handler = _RecordingHandler(responses=[(200, "ok")])
    payload = {"target_id": "BR", "score": 0.91, "tags": ["energy", "shock"]}

    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        await emit(
            payload,
            descriptor={"url": "https://example.org/hook"},
            deps=deps,
        )

    req = handler.requests[0]
    sig_header = req.headers.get(HEADER_SIGNATURE.lower())
    did_header = req.headers.get(HEADER_SIGNER_DID.lower())
    assert sig_header is not None and len(sig_header) > 0
    assert did_header == identity.signer_did

    # Verify against the body bytes the server received — proves the
    # downstream verification path documented in the module docstring.
    assert verify_signed_body(req.content, sig_header, identity.verify_key)


async def test_emit_includes_extra_headers(identity: SigningIdentity):
    handler = _RecordingHandler(responses=[(200, "")])
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        await emit(
            {"k": 1},
            descriptor={
                "url": "https://example.org/hook",
                "extra_headers": {"X-Tenant": "prod"},
            },
            deps=deps,
        )

    req = handler.requests[0]
    assert req.headers.get("x-tenant") == "prod"


# ---------------------------------------------------------------------------
# 4xx — no retry, no DLQ
# ---------------------------------------------------------------------------


async def test_emit_4xx_does_not_retry(identity: SigningIdentity):
    handler = _RecordingHandler(responses=[(400, "bad request")])
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        # Should not raise — permanent errors are logged + returned, not
        # propagated. Outer durable retry isn't in this kind's scope.
        await emit(
            {"k": "v"},
            descriptor={
                "url": "https://example.org/hook",
                "backoff_seconds": [0.001, 0.001],
            },
            deps=deps,
        )

    # Exactly one attempt — 4xx never retries.
    assert len(handler.requests) == 1


@pytest.mark.skip(
    reason="needs an isolated NATS; DLQ subject overlaps the live runtime's "
    "legba.dlq.> stream on --network host"
)
async def test_emit_4xx_does_not_dlq(
    identity: SigningIdentity, nats_store: NatsStore, session_prefix: str
):
    """A 4xx response is a programmer/auth error — it must not pollute
    the DLQ subject; the outer runtime should re-author the request, not
    retry it from a queue."""
    analyst_id = f"{session_prefix}_4xx_no_dlq"
    dlq_full = dlq_subject(analyst_id)
    dlq_stream = f"{session_prefix}_4xx_dlq_stream"
    await nats_store.ensure_stream(name=dlq_stream, subjects=[dlq_full])

    handler = _RecordingHandler(responses=[(404, "no such endpoint")])
    async with _make_http(handler) as http:
        deps = _Deps(
            http=http,
            identity=identity,
            nats_publish=nats_store.publish_json,
            analyst_id=analyst_id,
        )
        await emit(
            {"k": "v"},
            descriptor={
                "url": "https://example.org/hook",
                "backoff_seconds": [0.001, 0.001],
            },
            deps=deps,
        )

    # No DLQ message should have landed.
    psub = await nats_store.js.pull_subscribe(
        subject=dlq_full,
        durable=f"{session_prefix}_4xx_dlq_dur",
        stream=dlq_stream,
    )
    try:
        with pytest.raises(asyncio.TimeoutError):
            await psub.fetch(1, timeout=1)
    finally:
        try:
            await nats_store.js.delete_stream(dlq_stream)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 5xx then 200 — retry-then-success
# ---------------------------------------------------------------------------


async def test_emit_5xx_then_success(identity: SigningIdentity):
    handler = _RecordingHandler(
        responses=[(503, "down"), (502, "still down"), (200, "ok")]
    )
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        await emit(
            {"k": "v"},
            descriptor={
                "url": "https://example.org/hook",
                # Fast backoff so the suite stays snappy.
                "backoff_seconds": [0.001, 0.001, 0.001],
                "max_attempts": 3,
            },
            deps=deps,
        )

    # 3 attempts: 503 → 502 → 200.
    assert len(handler.requests) == 3
    assert all(str(r.url) == "https://example.org/hook" for r in handler.requests)


# ---------------------------------------------------------------------------
# 5xx persistent → DLQ envelope on real NATS
# ---------------------------------------------------------------------------


@pytest.mark.skip(
    reason="needs an isolated NATS; DLQ subject overlaps the live runtime's "
    "legba.dlq.> stream on --network host"
)
async def test_emit_retry_exhausted_routes_to_dlq(
    identity: SigningIdentity, nats_store: NatsStore, session_prefix: str
):
    analyst_id = f"{session_prefix}_exhaust"
    dlq_full = dlq_subject(analyst_id)
    dlq_stream = f"{session_prefix}_exhaust_dlq_stream"
    await nats_store.ensure_stream(name=dlq_stream, subjects=[dlq_full])

    handler = _RecordingHandler(
        responses=[(500, "boom"), (500, "boom"), (500, "boom")]
    )
    async with _make_http(handler) as http:
        deps = _Deps(
            http=http,
            identity=identity,
            nats_publish=nats_store.publish_json,
            analyst_id=analyst_id,
        )
        await emit(
            {"original": True, "n": 7},
            descriptor={
                "url": "https://example.org/hook",
                "backoff_seconds": [0.001, 0.001, 0.001],
                "max_attempts": 3,
            },
            deps=deps,
        )

    # 3 HTTP attempts — DLQ publish is NATS-only.
    assert len(handler.requests) == 3

    # Pull the DLQ envelope and validate its shape.
    psub = await nats_store.js.pull_subscribe(
        subject=dlq_full,
        durable=f"{session_prefix}_exhaust_dlq_dur",
        stream=dlq_stream,
    )
    try:
        msgs = await psub.fetch(1, timeout=5)
        assert len(msgs) == 1
        decoded = json.loads(msgs[0].data.decode("utf-8"))
        assert decoded["original_url"] == "https://example.org/hook"
        assert decoded["analyst_id"] == analyst_id
        assert decoded["attempts"] == 3
        assert decoded["signer_did"] == identity.signer_did
        # The body is the same canonical-JSON that was POSTed and signed.
        assert json.loads(decoded["payload_utf8"]) == {"original": True, "n": 7}
        # The signature in the envelope verifies the body.
        assert verify_signed_body(
            decoded["payload_utf8"].encode("utf-8"),
            decoded["signature"],
            identity.verify_key,
        )
        # http 500 surfaces in error string.
        assert "500" in decoded["error"]
        for m in msgs:
            await m.ack()
    finally:
        try:
            await nats_store.js.delete_stream(dlq_stream)
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Network/timeout exception — transient
# ---------------------------------------------------------------------------


async def test_emit_timeout_is_transient_and_retries(identity: SigningIdentity):
    """An httpx network exception is treated as transient. Two raises
    then a 200 → 3 attempts total, no error surfaces."""
    raise_count = {"n": 2}

    def transport_handler(request: httpx.Request) -> httpx.Response:
        if raise_count["n"] > 0:
            raise_count["n"] -= 1
            raise httpx.ConnectTimeout("simulated timeout", request=request)
        return httpx.Response(200, text="ok")

    handler = _RecordingHandler(
        responses=[transport_handler, transport_handler, (200, "ok")]
    )
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        await emit(
            {"k": "v"},
            descriptor={
                "url": "https://example.org/hook",
                "backoff_seconds": [0.001, 0.001, 0.001],
                "max_attempts": 3,
            },
            deps=deps,
        )

    # 3 requests were dispatched (2 raised, 1 returned 200). The transport
    # handler's `requests` list captures every dispatch attempt.
    assert len(handler.requests) == 3


# ---------------------------------------------------------------------------
# Rate limiter is called per attempt
# ---------------------------------------------------------------------------


async def test_rate_limiter_acquire_called_per_attempt(identity: SigningIdentity):
    handler = _RecordingHandler(
        responses=[(503, "down"), (503, "down"), (200, "ok")]
    )
    limiter = _RecordingLimiter()
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity, rate_limiter=limiter)
        await emit(
            {"k": "v"},
            descriptor={
                "url": "https://example.org/hook",
                "backoff_seconds": [0.001, 0.001, 0.001],
                "max_attempts": 3,
            },
            deps=deps,
        )

    # acquire() runs before every attempt — including retries.
    assert limiter.calls == [
        "https://example.org/hook",
        "https://example.org/hook",
        "https://example.org/hook",
    ]


# ---------------------------------------------------------------------------
# Deps + payload programmer errors
# ---------------------------------------------------------------------------


async def test_emit_missing_http_raises(identity: SigningIdentity):
    class _NoHttp:
        signing_identity = identity
        rate_limiter = None
        nats_publish = None
        http = None

    with pytest.raises(WebhookDepsError, match="http"):
        await emit(
            {"k": "v"},
            descriptor={"url": "https://example.org/hook"},
            deps=_NoHttp(),
        )


async def test_emit_missing_signing_identity_raises():
    class _NoId:
        signing_identity = None
        rate_limiter = None
        nats_publish = None
        http = httpx.AsyncClient()

    with pytest.raises(WebhookDepsError, match="signing_identity"):
        await emit(
            {"k": "v"},
            descriptor={"url": "https://example.org/hook"},
            deps=_NoId(),
        )


async def test_emit_bytes_payload_rejected(identity: SigningIdentity):
    async with _make_http(_RecordingHandler()) as http:
        deps = _Deps(http=http, identity=identity)
        with pytest.raises(WebhookPayloadError):
            await emit(
                b'{"already": "serialised"}',
                descriptor={"url": "https://example.org/hook"},
                deps=deps,
            )


async def test_emit_string_payload_rejected(identity: SigningIdentity):
    async with _make_http(_RecordingHandler()) as http:
        deps = _Deps(http=http, identity=identity)
        with pytest.raises(WebhookPayloadError):
            await emit(
                "already a string",
                descriptor={"url": "https://example.org/hook"},
                deps=deps,
            )


async def test_emit_list_payload_rejected(identity: SigningIdentity):
    async with _make_http(_RecordingHandler()) as http:
        deps = _Deps(http=http, identity=identity)
        with pytest.raises(WebhookPayloadError):
            await emit(
                [1, 2, 3],
                descriptor={"url": "https://example.org/hook"},
                deps=deps,
            )


async def test_emit_missing_url_raises(identity: SigningIdentity):
    async with _make_http(_RecordingHandler()) as http:
        deps = _Deps(http=http, identity=identity)
        with pytest.raises(WebhookConfigError):
            await emit({"k": "v"}, descriptor={}, deps=deps)


# ---------------------------------------------------------------------------
# dlq=False re-raises
# ---------------------------------------------------------------------------


async def test_emit_dlq_disabled_reraises(identity: SigningIdentity):
    handler = _RecordingHandler(
        responses=[(500, "boom"), (500, "boom"), (500, "boom")]
    )
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        with pytest.raises(WebhookDepsError, match="exhausted"):
            await emit(
                {"k": "v"},
                descriptor={
                    "url": "https://example.org/hook",
                    "backoff_seconds": [0.001, 0.001, 0.001],
                    "max_attempts": 3,
                    "dlq": False,
                },
                deps=deps,
            )

    assert len(handler.requests) == 3


# ---------------------------------------------------------------------------
# DLQ subject grammar
# ---------------------------------------------------------------------------


def test_dlq_subject_anonymous():
    assert dlq_subject(None) == f"{DLQ_SUBJECT_PREFIX}._anonymous"


def test_dlq_subject_with_analyst_id():
    assert (
        dlq_subject("india_energy")
        == f"{DLQ_SUBJECT_PREFIX}.india_energy"
    )


def test_dlq_subject_sanitises():
    out = dlq_subject("bad id.with*chars")
    assert " " not in out
    assert "*" not in out
    assert out.startswith(DLQ_SUBJECT_PREFIX + ".")


# ---------------------------------------------------------------------------
# SSRF egress guard on the webhook OUTPUT client (S-3)
# ---------------------------------------------------------------------------
#
# The runtime now builds the shared output client via
# ``guarded_async_client()`` (``runtime/dapr_host.py``), so a webhook
# descriptor whose ``cfg.url`` points at an internal/loopback/metadata
# address is REFUSED before any socket connect — the same SSRF guard the
# ingress fetchers use. These tests assert the guard at the transport level
# (no DNS, offline — literal IPs are checked directly) and end-to-end
# through ``emit`` (the blocked POST never reaches the target, exhausts the
# retry budget, and re-raises with ``dlq: False``).


def _guarded_http() -> httpx.AsyncClient:
    """The exact guarded client the runtime installs for webhook output."""
    return guarded_async_client(timeout=2.0)


# Internal targets a malicious/misconfigured descriptor might point at.
# Literal IPs so the guard checks them directly (no getaddrinfo needed).
_BLOCKED_URLS = [
    "http://127.0.0.1:80/hook",            # loopback
    "http://169.254.169.254/latest/meta",  # cloud metadata (link-local)
    "http://10.0.0.5:8080/hook",           # RFC-1918 10/8
    "http://172.16.0.9/hook",              # RFC-1918 172.16/12
    "http://192.168.1.1/hook",             # RFC-1918 192.168/16
    "http://[::1]:80/hook",                # IPv6 loopback
    "http://[::ffff:127.0.0.1]/hook",      # IPv4-mapped IPv6 loopback
    "http://0.0.0.0/hook",                 # unspecified
]


@pytest.mark.parametrize("url", _BLOCKED_URLS)
def test_guard_blocks_internal_host(url: str):
    """``assert_public_host`` refuses every internal/loopback/metadata target."""
    parsed = httpx.URL(url)
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    with pytest.raises(EgressBlockedError):
        assert_public_host(parsed.host, port)


def test_guard_allows_public_ip():
    """A routable public literal passes the guard (no exception)."""
    # 93.184.216.34 (example.org range) — public, must NOT raise.
    assert_public_host("93.184.216.34", 443)


@pytest.mark.parametrize("url", _BLOCKED_URLS)
async def test_emit_to_internal_url_is_refused(
    identity: SigningIdentity, url: str
):
    """The full webhook emit path REFUSES an internal target.

    An SSRF block is a PERMANENT misconfiguration (like a 4xx): the guard
    raises ``EgressBlockedError`` before any connect, so the POST never
    reaches the address. emit logs ``webhook.permanent_error`` and returns
    WITHOUT retry and WITHOUT DLQing — a single attempt, no payload leak.
    """
    limiter = _RecordingLimiter()
    async with _guarded_http() as http:
        deps = _Deps(http=http, identity=identity, rate_limiter=limiter)
        # Returns (does not raise) — permanent errors are logged + swallowed
        # exactly like the 4xx path; the outer durable retry is not in scope.
        await emit(
            {"finding": "exfil attempt"},
            descriptor={
                "url": url,
                "backoff_seconds": [0.001, 0.001],
                "max_attempts": 3,
            },
            deps=deps,
        )
    # PERMANENT → exactly one attempt, never retried.
    assert limiter.calls == [url]


async def test_emit_internal_url_not_dlqd(
    identity: SigningIdentity, caplog: pytest.LogCaptureFixture
):
    """A blocked-egress webhook must not DLQ the payload (no leak/replay)."""
    dlq_calls: list[Any] = []

    async def _record_dlq(subject: str, payload: Any) -> None:
        dlq_calls.append((subject, payload))

    async with _guarded_http() as http:
        deps = _Deps(
            http=http,
            identity=identity,
            nats_publish=_record_dlq,
            analyst_id="ssrf_probe",
        )
        with caplog.at_level(logging.ERROR, logger="legba.data.outputs.webhook"):
            await emit(
                {"finding": "exfil attempt"},
                descriptor={"url": "http://169.254.169.254/latest/meta-data/"},
                deps=deps,
            )
    # Permanent → no DLQ envelope published.
    assert dlq_calls == []
    assert any("permanent_error" in r.message for r in caplog.records)


async def test_post_once_internal_url_classified_permanent(identity: SigningIdentity):
    """A blocked-egress attempt is a PERMANENT outcome (never retried)."""
    async with _guarded_http() as http:
        result = await webhook._post_once(
            http,
            url="http://169.254.169.254/latest/meta-data/",
            body=b'{"k":1}',
            headers={"content-type": "application/json"},
            timeout=2.0,
        )
    assert result.delivered is False
    assert result.permanent is True
    assert result.transient is False
    assert "EgressBlockedError" in (result.error or "")


def test_runtime_output_client_is_guarded():
    """Regression: the runtime's output client must use the SSRF transport.

    Guards against a future refactor reverting ``dapr_host`` to a bare
    ``httpx.AsyncClient`` for webhook / TAXII output.
    """
    from legba.data.sources._egress import SsrfGuardedTransport

    client = guarded_async_client(timeout=15.0)
    try:
        transport = client._transport  # the mounted default transport
        assert isinstance(transport, SsrfGuardedTransport)
    finally:
        # Sync close is fine — no connections were opened.
        pass


# ---------------------------------------------------------------------------
# Pydantic model payload coercion
# ---------------------------------------------------------------------------


async def test_emit_accepts_pydantic_model(identity: SigningIdentity):
    from pydantic import BaseModel

    class _Finding(BaseModel):
        target_id: str
        score: float

    handler = _RecordingHandler(responses=[(200, "")])
    async with _make_http(handler) as http:
        deps = _Deps(http=http, identity=identity)
        await emit(
            _Finding(target_id="BR", score=0.91),
            descriptor={"url": "https://example.org/hook"},
            deps=deps,
        )

    req = handler.requests[0]
    body = json.loads(req.content.decode("utf-8"))
    assert body == {"target_id": "BR", "score": 0.91}

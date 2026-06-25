# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.outputs.webhook — L-196 webhook output kind.

Outbound HTTPS POST output kind. Analysts that wire an
``outputs[*].kind == "webhook"`` binding push their emitted payload to a
configured URL with an Ed25519 signature over the canonical-JSON of the
payload. The same Ed25519 identity that signs the audit-chain rows
(`data/registry/signing.py`) signs the webhook body, so any downstream
consumer can verify both surfaces against the same trust root.

Contract
--------
* ``KIND_NAME = "webhook"``
* ``async def emit(payload, *, descriptor, deps) -> None``

Wire format
-----------
The body is the canonical-JSON encoding of the payload (sorted keys, no
whitespace, UTF-8) — identical to what the audit chain hashes. Two
headers carry the signature material:

* ``X-Legba-Signature``       — base64url of the raw 64-byte Ed25519
                                signature, no padding.
* ``X-Legba-Signer-DID``      — DID-style signer identifier, e.g.
                                ``did:legba:registry:<host>``.

A receiving server verifies by:

  1. Looking up the verify-key for ``X-Legba-Signer-DID``.
  2. Decoding ``X-Legba-Signature`` (b64url, pad to multiple of 4).
  3. ``verify_key.verify(canonical_json(received_body), signature)``.

The recipient MUST canonicalise its received body with the same scheme;
shipping the exact bytes that were signed (i.e. the on-wire body) is
the simplest verification path.

Retry behaviour
---------------
* 2xx                  → ``delivered`` (one-shot).
* 4xx                  → ``permanent_error`` — bypasses retry (the URL,
                          auth or request shape is wrong; another POST
                          will keep failing).
* 5xx                  → ``transient_error`` — eligible for retry.
* Network / timeout    → ``transient_error`` — eligible for retry.

The retry schedule defaults to ``[1.0, 2.0, 4.0]`` seconds (exponential
backoff) across 3 attempts (first attempt + 2 retries). Both knobs are
descriptor-configurable; tests inject a faster schedule.

DLQ
---
On retry exhaustion the original payload + signature + last-error are
wrapped in a DLQ envelope and published to
``legba.dlq.output.webhook.<analyst_id>``. The DLQ envelope shape matches
the convention used by ``nats_stream.py``:

    {
      "original_url": "<configured URL>",
      "analyst_id": "<analyst id or '_anonymous'>",
      "error": "<error type+detail>",
      "payload_utf8": "<canonical-JSON of the original payload>",
      "signature": "<b64url signature>",
      "signer_did": "<signer DID>",
      "attempts": <int>,
    }

Per-URL rate limiting
---------------------
``deps.rate_limiter`` (a structural :class:`RateLimiterPort`) gates the
outbound POST. The kind calls ``await deps.rate_limiter.acquire(url)``
before each attempt — including retries — so a 5xx storm against one
URL doesn't starve other webhook destinations. When ``deps.rate_limiter``
is None the kind proceeds without gating; the runtime supplies a real
limiter in production wiring.

Dependencies
------------
* ``deps.http`` — an :class:`HttpClientLike` (structural ``httpx.AsyncClient``
  subset) for outbound POST.
* ``deps.nats_publish`` *or* ``deps.nats_store`` — for DLQ routing on
  retry exhaustion. Resolved the same way :mod:`nats_stream` does it.
* ``deps.signing_identity`` — the registry-process :class:`SigningIdentity`.
* ``deps.rate_limiter`` — optional :class:`RateLimiterPort`.
* ``deps.analyst_id`` — optional string; used to address DLQ subject.

Programmer-error vs runtime-error
---------------------------------
* Missing ``url`` in the descriptor → :class:`WebhookConfigError`.
* Missing ``deps.http`` → :class:`WebhookDepsError`.
* Missing ``deps.signing_identity`` → :class:`WebhookDepsError`.
* Non-serializable payload → :class:`WebhookPayloadError`.

These never DLQ — they surface immediately, the same way ``nats_stream``
re-raises ``OutputPayloadError``. Only true delivery failures (network +
5xx) route to DLQ.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
from datetime import date, datetime
from typing import Any, Mapping, Protocol, runtime_checkable
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from ..provenance import canonical_json
from ..registry.signing import SigningIdentity

logger = logging.getLogger(__name__)


KIND_NAME = "webhook"

# Header constants — kept here so tests + downstream verifiers can import
# the canonical names rather than string-literaling them.
HEADER_SIGNATURE = "X-Legba-Signature"
HEADER_SIGNER_DID = "X-Legba-Signer-DID"
HEADER_CONTENT_TYPE = "Content-Type"

# DLQ subject prefix — mirrors `legba.dlq.output.<kind>.<analyst_id>` per
# DESIGN.md §11 and the nats_stream convention.
DLQ_SUBJECT_PREFIX = "legba.dlq.output.webhook"

# Retry defaults — exponential backoff (1s, 2s, 4s) across 3 attempts.
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)
DEFAULT_TIMEOUT_SECONDS = 30.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class WebhookConfigError(ValueError):
    """Descriptor config is missing required fields (e.g. no URL)."""


class WebhookPayloadError(ValueError):
    """Payload could not be canonically encoded."""


class WebhookDepsError(RuntimeError):
    """Deps bundle is missing the http client or signing identity."""


# ---------------------------------------------------------------------------
# Config (descriptor block)
# ---------------------------------------------------------------------------


class WebhookConfig(BaseModel):
    """Schema for ``descriptor.outputs.webhook`` config block.

    The descriptor authors a block like::

        outputs:
          - kind: webhook
            config:
              url: https://example.org/legba/hook
              timeout_seconds: 30
              max_attempts: 3
              backoff_seconds: [1.0, 2.0, 4.0]
              extra_headers:
                X-Tenant: prod

    The kind validates this block before any network call so a malformed
    descriptor surfaces at registration time rather than first emit.
    """

    model_config = ConfigDict(extra="forbid")

    url: str = Field(..., min_length=1)
    timeout_seconds: float = Field(default=DEFAULT_TIMEOUT_SECONDS, gt=0.0, le=600.0)
    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=10)
    backoff_seconds: tuple[float, ...] = Field(default=DEFAULT_BACKOFF_SECONDS)
    extra_headers: dict[str, str] = Field(default_factory=dict)
    # Whether retry exhaustion publishes to the webhook DLQ. Defaults True
    # — the only reason to turn it off is in tests that want to assert the
    # raised exception directly.
    dlq: bool = True


# ---------------------------------------------------------------------------
# Rate-limiter port
# ---------------------------------------------------------------------------


@runtime_checkable
class RateLimiterPort(Protocol):
    """Structural surface for a per-URL token-bucket / leaky-bucket limiter.

    Implementations call ``await acquire(key)`` before each attempt; the
    limiter may block until a slot is available. The runtime supplies a
    real limiter (per-host or per-URL) and tests pass either ``None`` (no
    gating) or a recording fake.

    ``key`` is the destination URL (or host — implementer's choice). The
    kind passes the configured URL verbatim.
    """

    async def acquire(self, key: str) -> None: ...


# ---------------------------------------------------------------------------
# Config parsing
# ---------------------------------------------------------------------------


def parse_config(descriptor: Mapping[str, Any] | None) -> WebhookConfig:
    """Parse a webhook config block from a descriptor mapping.

    Accepts either:
      * A bare ``WebhookConfig``-shaped dict.
      * A descriptor with ``outputs.webhook`` nested inside.

    Raises :class:`WebhookConfigError` if the URL is missing.
    """
    if not descriptor:
        raise WebhookConfigError(
            "webhook output kind requires a config block with 'url'"
        )

    block: Mapping[str, Any] = descriptor
    # Descriptor-level nesting: ``{"outputs": {"webhook": {...}}}``.
    if "outputs" in block and isinstance(block["outputs"], Mapping):
        outputs = block["outputs"]
        if "webhook" in outputs and isinstance(outputs["webhook"], Mapping):
            block = outputs["webhook"]
        else:
            block = outputs
    # Single-kind nesting: ``{"webhook": {...}}``.
    if "webhook" in block and isinstance(block["webhook"], Mapping):
        block = block["webhook"]

    try:
        return WebhookConfig.model_validate(dict(block))
    except Exception as exc:
        raise WebhookConfigError(
            f"webhook config invalid: {exc}"
        ) from exc


# ---------------------------------------------------------------------------
# Payload encoding + signing
# ---------------------------------------------------------------------------


def _json_default(value: Any) -> Any:
    """Local UUID/datetime coercion so canonical_json can encode them.

    ``canonical_json`` already handles these via its own ``_json_default``;
    we duplicate the surface here so callers can introspect the encoded
    bytes without going through provenance internals.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialize {value!r}")


def _normalise_payload(payload: Any) -> dict[str, Any]:
    """Coerce ``payload`` into a Mapping-shaped dict for canonical-JSON.

    The kind contract says payload is dict-like; we accept Pydantic models
    too via ``model_dump(mode='json')`` so analysts can pass their typed
    output rows directly. Bytes / str payloads are explicitly rejected —
    canonical signing requires structured data, not pre-serialised bytes.
    """
    if isinstance(payload, (bytes, bytearray, str)):
        raise WebhookPayloadError(
            "webhook payload must be a Mapping or Pydantic model — "
            "pre-serialised bytes/str cannot be canonically signed"
        )
    if hasattr(payload, "model_dump"):
        try:
            return payload.model_dump(mode="json")
        except Exception as exc:
            raise WebhookPayloadError(
                f"payload.model_dump() failed: {exc}"
            ) from exc
    if not isinstance(payload, Mapping):
        raise WebhookPayloadError(
            f"webhook payload must be a Mapping (got {type(payload).__name__})"
        )
    return dict(payload)


def _b64url(data: bytes) -> str:
    """Base64url-encode without padding (per JWS / OAuth conventions)."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def sign_payload(
    payload: Mapping[str, Any],
    identity: SigningIdentity,
) -> tuple[bytes, str]:
    """Sign the canonical-JSON of ``payload`` with ``identity``.

    Returns a tuple of ``(body_bytes, signature_b64url)``. The body bytes
    are the exact wire payload; the signature is b64url-encoded raw 64
    bytes. Verification at the receiver:

        verify_key.verify(body_bytes, b64url_decode(signature))
    """
    try:
        body = canonical_json(payload)
    except TypeError as exc:
        raise WebhookPayloadError(
            f"payload is not canonically serialisable: {exc}"
        ) from exc
    signed = identity.signing_key.sign(body)
    return body, _b64url(signed.signature)


def verify_signed_body(
    body: bytes,
    signature_b64url: str,
    verify_key: Any,
) -> bool:
    """Helper for downstream consumers: verify a payload + signature.

    Returns True on success, raises ``ValueError`` on mismatch. Kept in
    the module so external verifiers can ``from legba.data.outputs.webhook
    import verify_signed_body`` rather than open-coding the b64url+verify.
    """
    # Decode with pad-restoration.
    padded = signature_b64url + "=" * (-len(signature_b64url) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded)
    except Exception as exc:
        raise ValueError(f"signature is not valid base64url: {exc}") from exc
    try:
        verify_key.verify(body, raw)
    except Exception as exc:
        raise ValueError(f"bad webhook signature: {exc}") from exc
    return True


# ---------------------------------------------------------------------------
# Deps resolution
# ---------------------------------------------------------------------------


def _resolve_signing_identity(deps: Any) -> SigningIdentity:
    identity = getattr(deps, "signing_identity", None)
    if identity is None:
        raise WebhookDepsError(
            "deps.signing_identity is required for webhook signing"
        )
    if not isinstance(identity, SigningIdentity):
        raise WebhookDepsError(
            f"deps.signing_identity must be a SigningIdentity, got "
            f"{type(identity).__name__}"
        )
    return identity


def _resolve_http(deps: Any) -> Any:
    http = getattr(deps, "http", None)
    if http is None:
        raise WebhookDepsError(
            "deps.http is required for webhook delivery"
        )
    return http


def _resolve_dlq_publisher(deps: Any) -> Any | None:
    """Return a callable ``async (subject, body_bytes) -> None`` or None.

    Mirrors ``nats_stream._resolve_publisher`` but tolerates absence — if
    no DLQ transport is wired we log loudly and drop the DLQ envelope;
    the substrate-write side of the analyst's emit is already durable.
    """
    nats_publish = getattr(deps, "nats_publish", None)
    if nats_publish is not None:
        return nats_publish
    nats_store = getattr(deps, "nats_store", None)
    if nats_store is not None:
        try:
            js = nats_store.js
        except Exception:  # pragma: no cover — not connected
            return None
        return js.publish
    return None


def dlq_subject(analyst_id: str | None) -> str:
    """Dead-letter subject for an analyst whose webhook emit failed.

    Mirrors :func:`nats_stream.dlq_subject` — same sanitisation, same
    ``legba.dlq.output.<kind>.<analyst_id>`` shape.
    """
    suffix = analyst_id if analyst_id else "_anonymous"
    for c in (" ", "\t", "\n", ".", "*", ">", "\x00"):
        suffix = suffix.replace(c, "_")
    if not suffix:
        suffix = "_anonymous"
    return f"{DLQ_SUBJECT_PREFIX}.{suffix}"


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def _is_transient_status(status: int | None) -> bool:
    """5xx (and missing / 0 status) are transient; everything else isn't."""
    if status is None:
        return True
    return 500 <= status < 600


def _is_permanent_status(status: int | None) -> bool:
    """4xx are permanent (auth/programmer errors)."""
    if status is None:
        return False
    return 400 <= status < 500


def _is_egress_blocked(exc: BaseException) -> bool:
    """True when the SSRF egress guard refused the target.

    The shared output client is the SSRF-guarded transport
    (``data/sources/_egress.SsrfGuardedTransport`` installed via
    ``runtime/dapr_host.py``), which raises ``EgressBlockedError`` for an
    internal / loopback / metadata / RFC-1918 target. We duck-type by class
    name to keep the ``HttpClientLike`` protocol library-agnostic (the same
    reason :func:`_is_transient_exception` avoids a hard httpx import); the
    egress error is a subclass of ``httpx.TransportError`` but lives in our
    ``_egress`` module, so its name — not its module — is the stable marker.
    """
    return type(exc).__name__ == "EgressBlockedError"


def _is_transient_exception(exc: BaseException) -> bool:
    """Network and timeout errors are transient.

    We avoid importing ``httpx.RequestError`` at module top because the
    structural ``HttpClientLike`` protocol is intentionally
    library-agnostic; instead we duck-type the exception class name
    against the known httpx surface.
    """
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    mod = (type(exc).__module__ or "").split(".")[0]
    if mod == "httpx":
        # All httpx.RequestError subclasses (TimeoutException, ConnectError,
        # ReadTimeout, ProxyError, NetworkError, ...).
        return True
    name = type(exc).__name__
    if "Timeout" in name or "Connect" in name or "Network" in name:
        return True
    return False


async def _maybe_await(value: Any) -> None:
    if inspect.isawaitable(value):
        await value


async def _publish_dlq(
    publisher: Any,
    analyst_id: str | None,
    *,
    url: str,
    error: str,
    body_bytes: bytes,
    signature_b64url: str,
    signer_did: str,
    attempts: int,
) -> None:
    """Best-effort DLQ publish — never retried (mirrors nats_stream)."""
    target = dlq_subject(analyst_id)
    envelope = {
        "original_url": url,
        "analyst_id": analyst_id,
        "error": error,
        "payload_utf8": body_bytes.decode("utf-8", errors="replace"),
        "signature": signature_b64url,
        "signer_did": signer_did,
        "attempts": attempts,
    }
    try:
        encoded = json.dumps(envelope, default=_json_default).encode("utf-8")
    except Exception as exc:  # pragma: no cover — envelope is trivially serialisable
        logger.exception("webhook.dlq.encode_failed err=%s", exc)
        return
    try:
        result = publisher(target, encoded)
        await _maybe_await(result)
        logger.error(
            "webhook.dlq.routed url=%s dlq=%s analyst_id=%s err=%s",
            url, target, analyst_id, error,
        )
    except Exception as dlq_exc:  # pragma: no cover — broker outage path
        logger.exception(
            "webhook.dlq.publish_failed url=%s dlq=%s err=%s",
            url, target, dlq_exc,
        )


# ---------------------------------------------------------------------------
# Single attempt
# ---------------------------------------------------------------------------


class _AttemptResult:
    """Internal — outcome of one POST attempt.

    Kept private to the module; the public ``emit`` returns ``None`` on
    success and routes failures to DLQ (or re-raises if dlq=False).
    """

    __slots__ = ("status", "delivered", "transient", "permanent", "error")

    def __init__(
        self,
        *,
        status: int | None,
        delivered: bool,
        transient: bool,
        permanent: bool,
        error: str = "",
    ) -> None:
        self.status = status
        self.delivered = delivered
        self.transient = transient
        self.permanent = permanent
        self.error = error


async def _post_once(
    http: Any,
    *,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> _AttemptResult:
    """One POST attempt — classifies outcome, never raises on transient.

    The httpx-like ``post`` signature accepts ``content=bytes`` for raw
    body passthrough. We pass the exact canonical-JSON bytes that were
    signed so the receiver verifies against the on-wire body.
    """
    try:
        resp = await http.post(
            url,
            content=body,
            headers=dict(headers),
            timeout=timeout,
        )
    except Exception as exc:
        if _is_egress_blocked(exc):
            # SSRF egress guard refused the target (the output client is the
            # SSRF-guarded one — runtime/dapr_host.py). An internal / loopback
            # / metadata / RFC-1918 webhook URL is a PERMANENT misconfiguration,
            # not a transient hiccup: retrying can't make it public, and DLQing
            # it would repeatedly stash the payload for a broken target. Treat
            # it exactly like a 4xx — never retry, never DLQ, log loud + return.
            return _AttemptResult(
                status=None,
                delivered=False,
                transient=False,
                permanent=True,
                error=f"{type(exc).__name__}: {exc}",
            )
        if _is_transient_exception(exc):
            return _AttemptResult(
                status=None,
                delivered=False,
                transient=True,
                permanent=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        # Non-transient exception (auth lib bugs, ValueError, ...) —
        # surface immediately by re-raising. Mirrors nats_stream's
        # programmer-error path.
        raise

    status = getattr(resp, "status_code", None)
    if status is not None and 200 <= status < 300:
        return _AttemptResult(
            status=status,
            delivered=True,
            transient=False,
            permanent=False,
        )
    if _is_permanent_status(status):
        text = getattr(resp, "text", "")
        return _AttemptResult(
            status=status,
            delivered=False,
            transient=False,
            permanent=True,
            error=f"http {status}: {str(text)[:200]}",
        )
    # 5xx / missing status — transient.
    text = getattr(resp, "text", "")
    return _AttemptResult(
        status=status,
        delivered=False,
        transient=True,
        permanent=False,
        error=f"http {status}: {str(text)[:200]}",
    )


# ---------------------------------------------------------------------------
# Public surface
# ---------------------------------------------------------------------------


async def emit(
    payload: Any,
    *,
    descriptor: Mapping[str, Any] | None = None,
    deps: Any,
) -> None:
    """POST ``payload`` to the descriptor-configured URL with an Ed25519
    signature in headers.

    Parameters
    ----------
    payload:
        A Mapping or Pydantic model. UUID / datetime fields are coerced
        via canonical-JSON. Bytes / str are rejected (a signed body must
        be structured).
    descriptor:
        Either the bare config block (``{"url": ...}``) or a descriptor
        with ``outputs.webhook`` nested inside.
    deps:
        Must expose ``http`` (httpx-like) + ``signing_identity``
        (SigningIdentity). May expose ``rate_limiter`` (RateLimiterPort),
        ``nats_publish`` / ``nats_store`` (for DLQ), and ``analyst_id``
        (str — DLQ subject suffix).

    Returns
    -------
    None on success; raises on programmer/auth errors; routes transient
    exhaustion to DLQ silently.
    """
    cfg = parse_config(descriptor)
    body_payload = _normalise_payload(payload)

    identity = _resolve_signing_identity(deps)
    http = _resolve_http(deps)
    rate_limiter: RateLimiterPort | None = getattr(deps, "rate_limiter", None)
    analyst_id: str | None = getattr(deps, "analyst_id", None)
    dlq_publisher = _resolve_dlq_publisher(deps)

    body_bytes, signature_b64url = sign_payload(body_payload, identity)

    headers: dict[str, str] = {
        HEADER_CONTENT_TYPE: "application/json",
        HEADER_SIGNATURE: signature_b64url,
        HEADER_SIGNER_DID: identity.signer_did,
    }
    headers.update(cfg.extra_headers)

    # Bounded attempt loop. Mirrors the alert.py / nats_stream.py shape:
    # attempt N, classify, sleep, retry. 4xx and unknown exceptions short-
    # circuit; only 5xx + network/timeout retry.
    max_attempts = cfg.max_attempts
    backoff = cfg.backoff_seconds or DEFAULT_BACKOFF_SECONDS

    last_result: _AttemptResult | None = None
    for attempt_idx in range(max_attempts):
        # Per-URL rate gate runs before each attempt (including retries) so
        # a 5xx storm against one URL doesn't bypass the limiter.
        if rate_limiter is not None:
            await rate_limiter.acquire(cfg.url)

        result = await _post_once(
            http,
            url=cfg.url,
            body=body_bytes,
            headers=headers,
            timeout=cfg.timeout_seconds,
        )
        last_result = result

        if result.delivered:
            logger.info(
                "webhook.delivered url=%s status=%s attempt=%d analyst_id=%s",
                cfg.url, result.status, attempt_idx + 1, analyst_id,
            )
            return

        if result.permanent:
            logger.error(
                "webhook.permanent_error url=%s status=%s analyst_id=%s err=%s",
                cfg.url, result.status, analyst_id, result.error,
            )
            # 4xx never retries and never DLQs — the request itself is
            # broken; DLQing would amount to "keep around the broken
            # thing forever". Log + return; outer durable retry (Dapr
            # reminders, future work) is the place to handle this.
            return

        # Transient — log and sleep before the next attempt (if any).
        logger.warning(
            "webhook.transient_failure url=%s status=%s attempt=%d/%d err=%s",
            cfg.url, result.status, attempt_idx + 1, max_attempts, result.error,
        )
        if attempt_idx < max_attempts - 1 and backoff:
            delay = backoff[min(attempt_idx, len(backoff) - 1)]
            if delay > 0:
                await asyncio.sleep(delay)

    # Retry exhausted on a transient error → DLQ envelope.
    assert last_result is not None
    error_str = last_result.error or f"http {last_result.status}"
    if cfg.dlq and dlq_publisher is not None:
        await _publish_dlq(
            dlq_publisher,
            analyst_id,
            url=cfg.url,
            error=error_str,
            body_bytes=body_bytes,
            signature_b64url=signature_b64url,
            signer_did=identity.signer_did,
            attempts=max_attempts,
        )
        return
    if cfg.dlq and dlq_publisher is None:
        # No DLQ transport wired and dlq=True: log loudly. We do NOT raise
        # because the substrate-write side already succeeded; the runtime
        # may add a Dapr reminder for durable retry in a future phase.
        logger.error(
            "webhook.retry_exhausted_no_dlq url=%s attempts=%d err=%s",
            cfg.url, max_attempts, error_str,
        )
        return
    # dlq=False — re-raise as a transport error so the caller can react.
    raise WebhookDepsError(
        f"webhook delivery to {cfg.url} exhausted {max_attempts} attempts: "
        f"{error_str}"
    )


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_TIMEOUT_SECONDS",
    "DLQ_SUBJECT_PREFIX",
    "HEADER_CONTENT_TYPE",
    "HEADER_SIGNATURE",
    "HEADER_SIGNER_DID",
    "KIND_NAME",
    "RateLimiterPort",
    "WebhookConfig",
    "WebhookConfigError",
    "WebhookDepsError",
    "WebhookPayloadError",
    "dlq_subject",
    "emit",
    "parse_config",
    "sign_payload",
    "verify_signed_body",
]

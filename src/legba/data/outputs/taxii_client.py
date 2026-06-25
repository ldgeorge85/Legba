# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""TAXII 2.1 push client — outbound STIX-bundle delivery (export-interop).

The STIX serialization (:mod:`legba.data.outputs.stix_bundle`) builds a
`STIX 2.1 <https://docs.oasis-open.org/cti/stix/v2.1/stix-v2.1.html>`_
``bundle`` and publishes it to NATS / a file. This module closes the
*outbound* loop: it POSTs that bundle to a configured `TAXII 2.1
<https://docs.oasis-open.org/cti/taxii/v2.1/taxii-v2.1.html>`_ collection
so the same payload reaches an upstream sharing server (OpenCTI / MISP /
EclecticIQ / a reference TAXII server).

Why a hand-rolled httpx client (not ``taxii2-client``)
------------------------------------------------------
The "add objects" hop is a single, well-defined HTTPS POST:

    POST {server_url}/{api_root}/collections/{collection_id}/objects/
    Content-Type: application/taxii+json;version=2.1
    Accept:       application/taxii+json;version=2.1
    <body: a TAXII envelope { "objects": [ ...SDOs... ] }>

We already depend on ``httpx`` and on the structural
:class:`legba.data.outputs._contract.HttpClientLike` port (the same port
the webhook kind uses). Reusing that port — rather than pulling
``taxii2-client`` into the runtime image — keeps the dependency surface
small, keeps the transport injectable for tests, and gives us direct
control of timeout / retry / TLP handling. The body we send is a TAXII
*envelope* (not a STIX *bundle*): TAXII 2.1 §3.6.1 specifies the request
body for the add-objects endpoint is an ``envelope`` resource whose
``objects`` member is the list of SDOs. We unwrap the STIX bundle's
``objects`` array into that envelope.

Delivery semantics — degrade, do not drop
------------------------------------------
* **Un-provisioned destination is fail-loud.** With no ``server_url`` the
  client raises :class:`TaxiiServerNotConfiguredError` (a programmer /
  config error — there is no destination, so there is nothing to attempt).
  This is the declared SEAM guard rail: an un-provisioned TAXII target
  refuses, it never silently no-ops. See ``docs/SEAMS.md`` seam 10.
* **A provisioned-but-unreachable destination degrades.** Once a
  ``server_url`` is configured, transient failures (network / timeout /
  5xx) are retried with bounded backoff and, on exhaustion, returned as a
  structured :class:`TaxiiPushResult` (``outcome="transient_error"``) —
  never raised. The STIX bundle is already durable (NATS + file), so an
  upstream-server outage must not break the analyst run or lose the
  bundle.
* **4xx is permanent** (bad collection id, auth, malformed envelope) — no
  retry; returned as ``outcome="permanent_error"``.

The single public entrypoint is :func:`push_bundle_to_taxii`, called by
``stix_bundle.upload_bundle_to_taxii`` and by ``stix_bundle.emit`` (behind
the descriptor ``outputs.stix_bundle.config.taxii`` binding flag).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Literal, Mapping, Sequence
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Media types / defaults
# ---------------------------------------------------------------------------

#: TAXII 2.1 media type for request/response bodies (spec §1.6.8).
TAXII_MEDIA_TYPE: str = "application/taxii+json;version=2.1"

#: Default per-attempt POST timeout (seconds).
DEFAULT_TIMEOUT_SECONDS: float = 15.0

#: Default backoff schedule — exponential across 3 attempts (1 + 2 retries).
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (1.0, 2.0, 4.0)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TaxiiServerNotConfiguredError(RuntimeError):
    """Raised when a TAXII push is requested with no destination configured.

    This is the fail-loud guard rail for the un-provisioned-destination
    seam (``docs/SEAMS.md`` seam 10). The client + wiring are real, but a
    binding that asks to push to TAXII without a ``server_url`` is a
    config error — we refuse loudly rather than silently dropping the
    bundle. NOT a delivery failure (those degrade); a missing destination
    means there is nothing to attempt.
    """


# ---------------------------------------------------------------------------
# Config + result models
# ---------------------------------------------------------------------------


AuthKind = Literal["none", "basic", "bearer"]


class TaxiiConfig(BaseModel):
    """Descriptor-side TAXII push configuration.

    Built from the ``outputs.stix_bundle.config.taxii`` mapping. The
    ``collection_id`` is the TAXII collection UUID (or alias) on the
    upstream server; ``server_url`` + ``api_root`` locate the API root the
    collection lives under.

    Auth (optional):
      * ``auth_kind="basic"`` → HTTP Basic with ``username`` / ``password``
        (``password`` is typically a vault-resolved secret).
      * ``auth_kind="bearer"`` → ``Authorization: Bearer <token>``.
      * ``auth_kind="none"`` (default) → no Authorization header.
    """

    model_config = ConfigDict(extra="forbid")

    server_url: str
    api_root: str
    collection_id: str
    auth_kind: AuthKind = "none"
    username: str | None = None
    password: str | None = None
    token: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS
    backoff_seconds: tuple[float, ...] = Field(default=DEFAULT_BACKOFF_SECONDS)
    # Extra headers merged onto every request (e.g. a tenant id). Auth +
    # content-type headers always win over anything supplied here.
    headers: dict[str, str] = Field(default_factory=dict)

    def objects_url(self) -> str:
        """Build the TAXII 2.1 add-objects endpoint URL.

        ``POST {server_url}/{api_root}/collections/{collection_id}/objects/``
        TAXII 2.1 requires the trailing slash (spec §1.6.5 — URLs end in
        ``/``). We normalise duplicate slashes between the joined parts.
        """
        base = self.server_url.rstrip("/")
        root = self.api_root.strip("/")
        cid = self.collection_id.strip("/")
        return f"{base}/{root}/collections/{cid}/objects/"


class TaxiiPushResult(BaseModel):
    """Structured outcome of a push attempt.

    ``outcome``:
      * ``"delivered"`` — a 2xx (typically 202 Accepted with a TAXII
        ``status`` resource).
      * ``"transient_error"`` — network / timeout / 5xx after retry
        exhaustion. The caller may re-attempt later; the bundle is durable.
      * ``"permanent_error"`` — 4xx (auth / bad collection / malformed).
        Re-posting the same bundle will keep failing.

    ``status_id`` carries the TAXII ``status`` resource id from a 202 body
    when present (the upstream uses it to report per-object accept/fail).
    """

    model_config = ConfigDict(extra="forbid")

    outcome: Literal["delivered", "transient_error", "permanent_error"]
    http_status: int | None = None
    attempts: int = 0
    status_id: str | None = None
    detail: str = ""

    @property
    def delivered(self) -> bool:
        return self.outcome == "delivered"

    @property
    def is_transient(self) -> bool:
        return self.outcome == "transient_error"


# ---------------------------------------------------------------------------
# Classification helpers (mirror the webhook kind's surface)
# ---------------------------------------------------------------------------


def _is_transient_exception(exc: BaseException) -> bool:
    """Network + timeout errors are transient (duck-typed, lib-agnostic).

    Mirrors ``legba.data.outputs.webhook._is_transient_exception`` so the
    two outbound-HTTP kinds classify identically without importing
    ``httpx`` at module top (the HTTP port is structural).
    """
    if isinstance(exc, (asyncio.TimeoutError, ConnectionError, OSError)):
        return True
    mod = (type(exc).__module__ or "").split(".")[0]
    if mod == "httpx":
        return True
    name = type(exc).__name__
    if "Timeout" in name or "Connect" in name or "Network" in name:
        return True
    return False


def _is_permanent_status(status: int | None) -> bool:
    """4xx are permanent (auth / bad collection / malformed envelope)."""
    if status is None:
        return False
    return 400 <= status < 500


def _auth_headers(cfg: TaxiiConfig) -> dict[str, str]:
    """Build the Authorization header for the configured auth kind.

    Returns an empty dict for ``auth_kind="none"`` or when the required
    credential field is absent (the request proceeds unauthenticated; a
    server that requires auth answers 401 → permanent_error, surfaced in
    the result detail).
    """
    if cfg.auth_kind == "basic" and cfg.username is not None:
        import base64

        raw = f"{cfg.username}:{cfg.password or ''}".encode("utf-8")
        return {"Authorization": "Basic " + base64.b64encode(raw).decode("ascii")}
    if cfg.auth_kind == "bearer" and cfg.token:
        return {"Authorization": f"Bearer {cfg.token}"}
    return {}


def _require_https(url: str) -> None:
    """Refuse a plaintext destination unless it is loopback.

    STIX/TAXII content carries TLP-marked intelligence; pushing it over
    cleartext HTTP to a non-loopback host would leak it. Loopback http is
    allowed so a local reference TAXII server (and the test harness) work
    without TLS. Anything else must be https.
    """
    parts = urlsplit(url)
    if parts.scheme == "https":
        return
    host = (parts.hostname or "").lower()
    if parts.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
        return
    raise TaxiiServerNotConfiguredError(
        f"TAXII server_url must be https (got scheme={parts.scheme!r} "
        f"host={host!r}); refusing to push TLP-marked content in cleartext"
    )


# ---------------------------------------------------------------------------
# Envelope construction
# ---------------------------------------------------------------------------


def bundle_to_taxii_envelope(bundle_or_objects: Any) -> dict[str, Any]:
    """Unwrap a STIX bundle's ``objects`` into a TAXII 2.1 envelope.

    TAXII 2.1 §3.6.1: the add-objects request body is an ``envelope``
    resource ``{"objects": [ ...SDOs... ]}`` — NOT a STIX ``bundle``.
    Accepts a ``stix2.Bundle`` (serialised via ``.serialize()`` /
    ``.objects``), a pre-serialised bundle JSON string, or an already-dict
    bundle/envelope; always returns a plain-dict envelope ready to POST.
    """
    # stix2.Bundle → use its canonical serialisation, then re-parse so we
    # emit the exact wire shape the library produced (spec-version stamped).
    serialize = getattr(bundle_or_objects, "serialize", None)
    if callable(serialize):
        data = json.loads(serialize())
    elif isinstance(bundle_or_objects, (bytes, bytearray)):
        data = json.loads(bytes(bundle_or_objects).decode("utf-8"))
    elif isinstance(bundle_or_objects, str):
        data = json.loads(bundle_or_objects)
    elif isinstance(bundle_or_objects, Mapping):
        data = dict(bundle_or_objects)
    else:
        raise TypeError(
            "bundle_to_taxii_envelope expects a stix2.Bundle, JSON str/bytes, "
            f"or mapping; got {type(bundle_or_objects).__name__}"
        )

    objects: Sequence[Any]
    if isinstance(data, Mapping) and "objects" in data:
        objects = data["objects"] or []
    elif isinstance(data, list):
        objects = data
    else:
        objects = []
    return {"objects": list(objects)}


# ---------------------------------------------------------------------------
# Single POST attempt
# ---------------------------------------------------------------------------


class _AttemptResult:
    """Outcome of one POST attempt — classified, never raises on transient."""

    __slots__ = ("status", "delivered", "transient", "permanent", "status_id", "error")

    def __init__(
        self,
        *,
        status: int | None,
        delivered: bool,
        transient: bool,
        permanent: bool,
        status_id: str | None = None,
        error: str = "",
    ) -> None:
        self.status = status
        self.delivered = delivered
        self.transient = transient
        self.permanent = permanent
        self.status_id = status_id
        self.error = error


def _extract_status_id(resp: Any) -> str | None:
    """Pull the TAXII ``status`` resource id from a 202 response body."""
    try:
        body = resp.json() if callable(getattr(resp, "json", None)) else None
    except Exception:
        body = None
    if isinstance(body, Mapping):
        sid = body.get("id")
        if isinstance(sid, str):
            return sid
    return None


async def _post_once(
    http: Any,
    *,
    url: str,
    body: bytes,
    headers: Mapping[str, str],
    timeout: float,
) -> _AttemptResult:
    """One add-objects POST — classifies, never raises on transient."""
    try:
        resp = await http.post(
            url,
            content=body,
            headers=dict(headers),
            timeout=timeout,
        )
    except Exception as exc:  # noqa: BLE001 — classify, do not crash the run
        if _is_transient_exception(exc):
            return _AttemptResult(
                status=None,
                delivered=False,
                transient=True,
                permanent=False,
                error=f"{type(exc).__name__}: {exc}",
            )
        # Non-transient (programmer error in the transport) — re-raise so
        # the caller's best-effort wrapper logs it; never silently eaten.
        raise

    status = getattr(resp, "status_code", None)
    if status is not None and 200 <= status < 300:
        return _AttemptResult(
            status=status,
            delivered=True,
            transient=False,
            permanent=False,
            status_id=_extract_status_id(resp),
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
    text = getattr(resp, "text", "")
    return _AttemptResult(
        status=status,
        delivered=False,
        transient=True,
        permanent=False,
        error=f"http {status}: {str(text)[:200]}",
    )


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


async def push_bundle_to_taxii(
    bundle: Any,
    *,
    config: TaxiiConfig,
    http: Any,
    sleep: Any = asyncio.sleep,
) -> TaxiiPushResult:
    """POST a STIX bundle to the configured TAXII 2.1 collection.

    Parameters
    ----------
    bundle:
        A ``stix2.Bundle`` (or its serialised JSON). Its ``objects`` are
        unwrapped into a TAXII envelope before POSTing.
    config:
        Resolved :class:`TaxiiConfig`. An empty ``server_url`` is rejected
        here (the un-provisioned-destination seam guard rail).
    http:
        An :class:`HttpClientLike` async HTTP client (structural
        ``httpx.AsyncClient`` subset). The runtime injects a real client;
        tests inject a recording fake.
    sleep:
        Injectable backoff sleeper (tests pass a no-op).

    Returns
    -------
    TaxiiPushResult
        Structured outcome. NEVER raises on a delivery failure —
        transient errors degrade to ``transient_error`` so the caller's
        durable bundle is preserved. Raises only
        :class:`TaxiiServerNotConfiguredError` for an un-provisioned /
        cleartext destination (a config error, not a delivery failure).
    """
    if not config.server_url:
        raise TaxiiServerNotConfiguredError(
            "TAXII push requested with no server_url — un-provisioned "
            "destination (see docs/SEAMS.md seam 10)"
        )
    if http is None:
        raise TaxiiServerNotConfiguredError(
            "TAXII push requested but no HTTP client is wired (deps.http is None)"
        )

    url = config.objects_url()
    _require_https(url)

    envelope = bundle_to_taxii_envelope(bundle)
    body = json.dumps(envelope, separators=(",", ":")).encode("utf-8")
    headers = {
        "Content-Type": TAXII_MEDIA_TYPE,
        "Accept": TAXII_MEDIA_TYPE,
        **dict(config.headers),
        **_auth_headers(config),
    }

    backoff = config.backoff_seconds or DEFAULT_BACKOFF_SECONDS
    max_attempts = len(backoff) + 1
    last_error = ""
    last_status: int | None = None

    for attempt_idx in range(max_attempts):
        result = await _post_once(
            http,
            url=url,
            body=body,
            headers=headers,
            timeout=config.timeout_seconds,
        )
        if result.delivered:
            logger.info(
                "taxii.push.delivered url=%s status=%s attempts=%d status_id=%s",
                url, result.status, attempt_idx + 1, result.status_id,
            )
            return TaxiiPushResult(
                outcome="delivered",
                http_status=result.status,
                attempts=attempt_idx + 1,
                status_id=result.status_id,
            )
        if result.permanent:
            logger.warning(
                "taxii.push.permanent_error url=%s status=%s err=%s",
                url, result.status, result.error,
            )
            return TaxiiPushResult(
                outcome="permanent_error",
                http_status=result.status,
                attempts=attempt_idx + 1,
                detail=result.error,
            )
        # Transient — retry with backoff unless this was the last attempt.
        last_error = result.error
        last_status = result.status
        if attempt_idx < max_attempts - 1 and backoff:
            delay = backoff[min(attempt_idx, len(backoff) - 1)]
            logger.info(
                "taxii.push.retry url=%s attempt=%d/%d delay=%.1fs err=%s",
                url, attempt_idx + 1, max_attempts, delay, result.error,
            )
            await sleep(delay)

    logger.warning(
        "taxii.push.transient_exhausted url=%s attempts=%d last_status=%s err=%s",
        url, max_attempts, last_status, last_error,
    )
    return TaxiiPushResult(
        outcome="transient_error",
        http_status=last_status,
        attempts=max_attempts,
        detail=last_error,
    )


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "TAXII_MEDIA_TYPE",
    "AuthKind",
    "TaxiiConfig",
    "TaxiiPushResult",
    "TaxiiServerNotConfiguredError",
    "bundle_to_taxii_envelope",
    "push_bundle_to_taxii",
]

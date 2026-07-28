# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Generic webhook alert sink (P1-1) — the first outward AlertSink.

HTTP POST of the :class:`~.sinks.AlertSinkPayload` JSON to an
operator-configured URL. This is the ENV-configured OPERATOR alert edge —
distinct from the descriptor-bound, Ed25519-signed ``webhook`` OUTPUT KIND
(:mod:`legba.data.outputs.webhook`), which is a per-analyst publishing
surface. This sink pages a human endpoint (Slack/Discord/ntfy-compatible
generic JSON receiver, an automation bus, ...) about alerts that already
cleared the escalation gate.

Configuration (env)
-------------------
* ``LEGBA_ALERT_WEBHOOK_URL`` — the POST destination. Unset ⇒ the sink is
  DECLARED-INACTIVE: it still registers, the dispatcher logs the gap once
  at startup, and every fan-out writes a ``skipped_unconfigured`` ledger
  row so the missing edge is visible, never a silent drop.
* ``LEGBA_ALERT_WEBHOOK_MIN_SEVERITY`` — severity floor (default
  ``high``). Alerts below the floor skip this sink (no ledger row — a
  configured filter working as designed).

Wire format
-----------
``POST <url>`` with ``Content-Type: application/json``; the body is the
payload's ``model_dump(mode="json")`` — summary, severity, target/geo,
source links, effective confidence, verify state, timestamps, and the
lineage receipt link. Flat JSON, no signature: the receiving end is an
operator convenience endpoint, and the receipt link back into the lineage
API is the verification handle.

Retry behaviour (mirrors :mod:`legba.data.outputs.taxii_client`)
----------------------------------------------------------------
* 2xx                → ``delivered``.
* 4xx                → ``permanent_error`` — no retry (the URL/auth is
                        wrong; another POST keeps failing).
* 5xx / network      → retried with bounded backoff (2 retries, so 3
                        attempts total), then ``transient_error``.
* Anything else      → ``permanent_error`` (classified, logged).

``deliver`` NEVER raises into the caller — the alert path must survive a
misbehaving endpoint. The URL never appears un-redacted in logs, results,
or ledger rows (webhook URLs routinely embed secrets); only its host does.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Callable, Mapping

from .sinks import (
    SEVERITY_ORDER,
    AlertSinkPayload,
    DeliveryResult,
    normalise_severity,
    redact_url_to_host,
    register_alert_sink,
)

logger = logging.getLogger(__name__)


ENV_WEBHOOK_URL = "LEGBA_ALERT_WEBHOOK_URL"
ENV_WEBHOOK_MIN_SEVERITY = "LEGBA_ALERT_WEBHOOK_MIN_SEVERITY"

DEFAULT_MIN_SEVERITY = "high"
#: Per-attempt POST timeout (seconds).
DEFAULT_TIMEOUT_SECONDS = 10.0
#: Backoff between retries — 2 retries, so 3 attempts total (the P1-1
#: bounded-retry contract; mirrors the taxii_client schedule shape).
DEFAULT_BACKOFF_SECONDS: tuple[float, ...] = (2.0, 5.0)


def _is_transient_exception(exc: BaseException) -> bool:
    """Network + timeout errors are transient (duck-typed, lib-agnostic).

    Mirrors ``legba.data.outputs.taxii_client._is_transient_exception`` so
    the outbound-HTTP surfaces classify identically without a hard httpx
    import at module top.
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
    if status is None:
        return False
    return 400 <= status < 500


class WebhookAlertSink:
    """The generic webhook :class:`~.sinks.AlertSink` implementation.

    Structural fit to the AlertSink protocol; instantiate via
    :meth:`from_env` (production) or the constructor (tests inject url /
    http / sleep directly).
    """

    def __init__(
        self,
        *,
        url: str = "",
        min_severity: str = DEFAULT_MIN_SEVERITY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        http: Any | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._url = (url or "").strip()
        self._min_severity = normalise_severity(
            min_severity, default=DEFAULT_MIN_SEVERITY
        )
        self._timeout_s = float(timeout_seconds)
        self._backoff = tuple(backoff_seconds)
        self._http = http
        self._sleep = sleep
        self._host = redact_url_to_host(self._url)

    # -- construction ----------------------------------------------------

    @classmethod
    def from_env(cls, *, http: Any | None = None) -> "WebhookAlertSink":
        """Build from ``LEGBA_ALERT_WEBHOOK_URL`` (+ optional min severity).

        An unset URL yields an UNCONFIGURED sink — registered, visible,
        inactive (the dispatcher records ``skipped_unconfigured`` rows).
        """
        return cls(
            url=os.environ.get(ENV_WEBHOOK_URL, ""),
            min_severity=os.environ.get(
                ENV_WEBHOOK_MIN_SEVERITY, DEFAULT_MIN_SEVERITY
            ),
            http=http,
        )

    # -- AlertSink protocol ---------------------------------------------

    @property
    def sink_kind(self) -> str:
        return "webhook"

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def target_summary(self) -> str:
        """Host-only redaction — the full URL may embed a secret."""
        return self._host

    def accepts_severity(self, severity: str) -> bool:
        rank = SEVERITY_ORDER.get(normalise_severity(severity), 0)
        return rank >= SEVERITY_ORDER[self._min_severity]

    async def deliver(self, payload: AlertSinkPayload) -> DeliveryResult:
        """POST the payload JSON with bounded retry. Never raises."""
        if not self.configured:
            return DeliveryResult(
                sink_kind=self.sink_kind,
                outcome="skipped_unconfigured",
                detail=f"webhook sink not configured ({ENV_WEBHOOK_URL} unset)",
            )
        if not self.accepts_severity(payload.severity):
            return DeliveryResult(
                sink_kind=self.sink_kind,
                outcome="skipped_below_severity",
                target=self._host,
                detail=(
                    f"severity {payload.severity!r} below configured floor "
                    f"{self._min_severity!r}"
                ),
            )

        try:
            body = json.dumps(
                payload.model_dump(mode="json"), separators=(",", ":")
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:  # pragma: no cover — payload is a model
            return DeliveryResult(
                sink_kind=self.sink_kind,
                outcome="permanent_error",
                target=self._host,
                detail=f"payload not serialisable: {exc}",
            )
        headers = {"Content-Type": "application/json"}

        max_attempts = len(self._backoff) + 1
        last_status: int | None = None
        last_error = ""
        for attempt_idx in range(max_attempts):
            status, error, transient = await self._post_once(body, headers)
            if status is not None and 200 <= status < 300:
                logger.info(
                    "alert_sinks.webhook.delivered host=%s status=%s "
                    "attempt=%d severity=%s channel=%s",
                    self._host, status, attempt_idx + 1,
                    payload.severity, payload.channel_name,
                )
                return DeliveryResult(
                    sink_kind=self.sink_kind,
                    outcome="delivered",
                    http_status=status,
                    attempts=attempt_idx + 1,
                    target=self._host,
                )
            if not transient:
                logger.warning(
                    "alert_sinks.webhook.permanent_error host=%s status=%s "
                    "err=%s", self._host, status, error,
                )
                return DeliveryResult(
                    sink_kind=self.sink_kind,
                    outcome="permanent_error",
                    http_status=status,
                    attempts=attempt_idx + 1,
                    target=self._host,
                    detail=error,
                )
            last_status, last_error = status, error
            if attempt_idx < max_attempts - 1:
                delay = self._backoff[min(attempt_idx, len(self._backoff) - 1)]
                logger.info(
                    "alert_sinks.webhook.retry host=%s attempt=%d/%d "
                    "delay=%.1fs err=%s",
                    self._host, attempt_idx + 1, max_attempts, delay, error,
                )
                if delay > 0:
                    await self._sleep(delay)

        logger.warning(
            "alert_sinks.webhook.transient_exhausted host=%s attempts=%d "
            "last_status=%s err=%s",
            self._host, max_attempts, last_status, last_error,
        )
        return DeliveryResult(
            sink_kind=self.sink_kind,
            outcome="transient_error",
            http_status=last_status,
            attempts=max_attempts,
            target=self._host,
            detail=last_error,
        )

    # -- one attempt -----------------------------------------------------

    async def _post_once(
        self, body: bytes, headers: Mapping[str, str]
    ) -> tuple[int | None, str, bool]:
        """One POST attempt → (status, error, transient?). Never raises.

        Unlike the taxii client (which re-raises programmer errors into its
        caller's best-effort wrapper) EVERY exception is classified here —
        the P1-1 contract is that the alert path cannot die on this edge.
        """
        try:
            http = self._client()
            resp = await http.post(
                self._url,
                content=body,
                headers=dict(headers),
                timeout=self._timeout_s,
            )
        except Exception as exc:  # noqa: BLE001 — classify, never crash the alert path
            if _is_transient_exception(exc):
                return None, f"{type(exc).__name__}: {exc}", True
            logger.error(
                "alert_sinks.webhook.unexpected_error host=%s err=%s",
                self._host, exc,
            )
            return None, f"{type(exc).__name__}: {exc}", False

        status = getattr(resp, "status_code", None)
        if status is not None and 200 <= status < 300:
            return status, "", False
        text = str(getattr(resp, "text", ""))[:200]
        if _is_permanent_status(status):
            return status, f"http {status}: {text}", False
        # 5xx / missing status — transient.
        return status, f"http {status}: {text}", True

    def _client(self) -> Any:
        """The injected HTTP client, or a lazily-built ``httpx.AsyncClient``.

        Built once on first use (import deferred — the structural client
        surface stays library-agnostic, mirroring taxii/webhook kinds).
        The URL is OPERATOR-set env, not descriptor/LLM-controlled, so the
        SSRF egress guard the descriptor-bound webhook kind needs does not
        apply here.
        """
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(follow_redirects=False)
        return self._http


# Register the built-in factory. `legba.data.alerts` (the package __init__)
# imports this module, so building a dispatcher via the package always sees
# the webhook sink. `replace=True` keeps a module re-import (importlib.reload
# in test rigs) idempotent instead of a spurious duplicate-registration raise.
register_alert_sink("webhook", WebhookAlertSink.from_env, replace=True)


__all__ = [
    "DEFAULT_BACKOFF_SECONDS",
    "DEFAULT_MIN_SEVERITY",
    "DEFAULT_TIMEOUT_SECONDS",
    "ENV_WEBHOOK_MIN_SEVERITY",
    "ENV_WEBHOOK_URL",
    "WebhookAlertSink",
]

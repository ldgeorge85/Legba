# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ntfy alert sink (E-2) — the second outward AlertSink, a NATIVE ntfy publish.

The P1-1 slot filled as designed: one module implementing the
:class:`~.sinks.AlertSink` protocol plus one
:func:`~.sinks.register_alert_sink` call — the dispatcher supplies the
ledger audit, idempotency, cooldown and never-raise fan-out for free.

Unlike the generic :mod:`.webhook_sink` (which POSTs the payload JSON to
any receiver), this sink speaks ntfy's native publish protocol so the
alert is HUMAN-READABLE ON A PHONE, not a JSON blob:

* the request BODY is plaintext — summary on the first line; then a
  ``severity · target · confidence · verify-state`` meta line; then the
  receipt link on its own line;
* ntfy semantics ride the headers — ``X-Title`` / ``X-Priority`` /
  ``X-Tags`` / ``X-Click`` (see the mapping tables below), so tapping the
  notification opens the lineage receipt.

Configuration (env)
-------------------
* ``LEGBA_ALERT_NTFY_URL`` — the FULL topic URL (e.g.
  ``http://ntfy/legba-alerts`` in-network, or
  ``http://127.0.0.1:8093/legba-alerts``). Unset ⇒ the sink is
  DECLARED-INACTIVE: it still registers, the dispatcher logs the gap once
  at startup, and every fan-out writes a ``skipped_unconfigured`` ledger
  row — visible, never a silent drop (the P1-1 unconfigured contract,
  identical to the webhook sink).
* ``LEGBA_ALERT_NTFY_TOKEN`` — optional access token for a protected ntfy
  server, sent as ``Authorization: Bearer <token>``. Never logged.
* ``LEGBA_ALERT_NTFY_MIN_SEVERITY`` — severity floor (default ``high``,
  mirroring the webhook sink's default). Alerts below the floor skip this
  sink (no ledger row — a configured filter working as designed).

Header mapping (ntfy publish conventions)
-----------------------------------------
* ``X-Title``    — ``Legba: <severity> - <target-or-channel>`` (ASCII by
  construction: HTTP header values are latin-1/ASCII territory, so the
  title uses a plain hyphen and :func:`_header_safe` strips anything
  outside printable ASCII rather than risking an encode error).
* ``X-Priority`` — severity → ntfy priority: critical→5 (urgent),
  high→4, medium→3 (default), else→2 (low).
* ``X-Tags``     — ``<severity emoji shortcode>,<channel>``: critical→
  ``rotating_light``, high→``warning``, medium→``loudspeaker``,
  low/info→``information_source``; plus the originating channel name
  (sanitised to ntfy's tag alphabet), e.g. ``warning,escalations``.
* ``X-Click``    — the ABSOLUTE receipt URL when present (requires
  ``LEGBA_PUBLIC_BASE_URL``); omitted otherwise — a relative API path is
  useless as a phone tap target.

Retry behaviour
---------------
The webhook sink's bounded-retry / timeout / never-raise pattern,
verbatim — the status/exception classification helpers are IMPORTED from
:mod:`.webhook_sink` so the two outbound alert edges can never drift:

* 2xx           → ``delivered``.
* 4xx           → ``permanent_error`` — no retry (URL/token is wrong).
* 5xx / network → bounded backoff (2 retries, 3 attempts total), then
                  ``transient_error``.
* anything else → ``permanent_error`` (classified, logged).

``deliver`` NEVER raises into the caller. The topic URL never appears
un-redacted in logs, results, or ledger rows — a topic name is
effectively a secret (anyone who knows it can read/write the topic), so
only the HOST survives redaction, exactly like the webhook sink.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from typing import Any, Callable, Mapping

from .sinks import (
    SEVERITY_ORDER,
    AlertSinkPayload,
    DeliveryResult,
    normalise_severity,
    redact_url_to_host,
    register_alert_sink,
)
from .webhook_sink import (
    DEFAULT_BACKOFF_SECONDS,
    DEFAULT_MIN_SEVERITY,
    DEFAULT_TIMEOUT_SECONDS,
    _is_permanent_status,
    _is_transient_exception,
)

logger = logging.getLogger(__name__)


ENV_NTFY_URL = "LEGBA_ALERT_NTFY_URL"
ENV_NTFY_TOKEN = "LEGBA_ALERT_NTFY_TOKEN"
ENV_NTFY_MIN_SEVERITY = "LEGBA_ALERT_NTFY_MIN_SEVERITY"


#: Severity → ntfy ``X-Priority`` (5=urgent … 1=min). Unlisted → "2" (low).
SEVERITY_PRIORITY: dict[str, str] = {
    "critical": "5",
    "high": "4",
    "medium": "3",
}
DEFAULT_PRIORITY = "2"

#: Severity → ntfy emoji-shortcode tag (rendered as the emoji on-device).
SEVERITY_TAGS: dict[str, str] = {
    "critical": "rotating_light",
    "high": "warning",
    "medium": "loudspeaker",
    "low": "information_source",
    "info": "information_source",
}

#: ntfy tags are comma-separated words — keep the channel tag to a safe
#: alphabet so a weird channel name can't smuggle a delimiter.
_TAG_UNSAFE = re.compile(r"[^a-z0-9_]+")


def _header_safe(value: str) -> str:
    """Printable-ASCII-only, whitespace-collapsed header value.

    HTTP header values must not carry control characters (CR/LF splits a
    request) and non-latin-1 text fails to encode inside the client —
    which would surface as a spurious permanent_error. Strip rather than
    risk it; the plaintext BODY carries the full UTF-8 content anyway.
    """
    cleaned = "".join(ch if 32 <= ord(ch) < 127 else " " for ch in value)
    return " ".join(cleaned.split())


def ntfy_title(payload: AlertSinkPayload) -> str:
    """``Legba: <severity> - <target-or-channel>`` (ASCII, see module doc)."""
    severity = normalise_severity(payload.severity)
    subject = (payload.target_id or payload.channel_name or "").strip()
    title = f"Legba: {severity}"
    if subject:
        title = f"{title} - {subject}"
    return _header_safe(title)


def ntfy_priority(severity: str) -> str:
    return SEVERITY_PRIORITY.get(normalise_severity(severity), DEFAULT_PRIORITY)


def ntfy_tags(payload: AlertSinkPayload) -> str:
    """``<severity tag>,<channel>`` — e.g. ``rotating_light,band_crossing``."""
    tags = [SEVERITY_TAGS.get(normalise_severity(payload.severity), "information_source")]
    channel = _TAG_UNSAFE.sub("_", (payload.channel_name or "").lower()).strip("_")
    if channel:
        tags.append(channel)
    return ",".join(tags)


def ntfy_message(payload: AlertSinkPayload) -> str:
    """The plaintext notification body — readable on a phone.

    Line 1: the summary. Line 2: ``severity · target · confidence ·
    verify-state`` (absent pieces omitted; severity + verify state are
    always present — an outward page always states its verification
    posture). Last line: the receipt link (absolute when available, else
    the relative lineage path — still copy-pasteable).
    """
    meta = [normalise_severity(payload.severity)]
    if payload.target_id:
        meta.append(str(payload.target_id))
    if payload.effective_confidence is not None:
        meta.append(f"confidence {payload.effective_confidence:.2f}")
    meta.append(payload.verify_state)
    lines = [payload.summary or "(no summary)", " · ".join(meta)]
    # Cooldown coalescing: a burst distilled, not silently thinned — the
    # dispatcher attaches what its cooldown suppressed since the last send.
    if payload.suppressed_in_cooldown > 0:
        lines.append(
            f"+{payload.suppressed_in_cooldown} more alert(s) during cooldown:"
        )
        lines.extend(f"  • {p}" for p in payload.suppressed_preview)
        overflow = payload.suppressed_in_cooldown - len(payload.suppressed_preview)
        if overflow > 0:
            lines.append(f"  … and {overflow} more (see console)")
    link = payload.receipt_url or payload.receipt_path
    if link:
        lines.append(link)
    return "\n".join(lines)


class NtfyAlertSink:
    """The native-ntfy :class:`~.sinks.AlertSink` implementation.

    Structural fit to the AlertSink protocol; instantiate via
    :meth:`from_env` (production) or the constructor (tests inject url /
    token / http / sleep directly). Shape mirrors
    :class:`~.webhook_sink.WebhookAlertSink` deliberately.
    """

    def __init__(
        self,
        *,
        url: str = "",
        token: str = "",
        min_severity: str = DEFAULT_MIN_SEVERITY,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
        http: Any | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        self._url = (url or "").strip()
        self._token = (token or "").strip()
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
    def from_env(cls, *, http: Any | None = None) -> "NtfyAlertSink":
        """Build from ``LEGBA_ALERT_NTFY_URL`` (+ optional token / floor).

        An unset URL yields an UNCONFIGURED sink — registered, visible,
        inactive (the dispatcher records ``skipped_unconfigured`` rows).
        """
        return cls(
            url=os.environ.get(ENV_NTFY_URL, ""),
            token=os.environ.get(ENV_NTFY_TOKEN, ""),
            min_severity=os.environ.get(
                ENV_NTFY_MIN_SEVERITY, DEFAULT_MIN_SEVERITY
            ),
            http=http,
        )

    # -- AlertSink protocol ---------------------------------------------

    @property
    def sink_kind(self) -> str:
        return "ntfy"

    @property
    def configured(self) -> bool:
        return bool(self._url)

    def target_summary(self) -> str:
        """Host-only redaction — the topic path is effectively a secret."""
        return self._host

    def accepts_severity(self, severity: str) -> bool:
        rank = SEVERITY_ORDER.get(normalise_severity(severity), 0)
        return rank >= SEVERITY_ORDER[self._min_severity]

    async def deliver(self, payload: AlertSinkPayload) -> DeliveryResult:
        """Native ntfy publish with bounded retry. Never raises."""
        if not self.configured:
            return DeliveryResult(
                sink_kind=self.sink_kind,
                outcome="skipped_unconfigured",
                detail=f"ntfy sink not configured ({ENV_NTFY_URL} unset)",
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

        body = ntfy_message(payload).encode("utf-8")
        headers: dict[str, str] = {
            "Content-Type": "text/plain; charset=utf-8",
            "X-Title": ntfy_title(payload),
            "X-Priority": ntfy_priority(payload.severity),
            "X-Tags": ntfy_tags(payload),
        }
        if payload.receipt_url:
            # Absolute only — tapping the notification must open a page.
            headers["X-Click"] = _header_safe(payload.receipt_url)
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"

        max_attempts = len(self._backoff) + 1
        last_status: int | None = None
        last_error = ""
        for attempt_idx in range(max_attempts):
            status, error, transient = await self._post_once(body, headers)
            if status is not None and 200 <= status < 300:
                logger.info(
                    "alert_sinks.ntfy.delivered host=%s status=%s "
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
                    "alert_sinks.ntfy.permanent_error host=%s status=%s "
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
                    "alert_sinks.ntfy.retry host=%s attempt=%d/%d "
                    "delay=%.1fs err=%s",
                    self._host, attempt_idx + 1, max_attempts, delay, error,
                )
                if delay > 0:
                    await self._sleep(delay)

        logger.warning(
            "alert_sinks.ntfy.transient_exhausted host=%s attempts=%d "
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
        """One publish attempt → (status, error, transient?). Never raises.

        Classification is shared with the webhook sink (imported helpers)
        so the two outbound alert edges classify identically.
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
                "alert_sinks.ntfy.unexpected_error host=%s err=%s",
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

        The topic URL is OPERATOR-set env, not descriptor/LLM-controlled,
        so the SSRF egress guard the descriptor-bound webhook OUTPUT KIND
        needs does not apply here (same posture as the webhook sink).
        """
        if self._http is None:
            import httpx

            self._http = httpx.AsyncClient(follow_redirects=False)
        return self._http


# Register the built-in factory beside the webhook sink's — the package
# __init__ imports this module, so building a dispatcher via the package
# always sees the ntfy sink. `replace=True` keeps a module re-import
# (importlib.reload in test rigs) idempotent.
register_alert_sink("ntfy", NtfyAlertSink.from_env, replace=True)


__all__ = [
    "DEFAULT_PRIORITY",
    "ENV_NTFY_MIN_SEVERITY",
    "ENV_NTFY_TOKEN",
    "ENV_NTFY_URL",
    "SEVERITY_PRIORITY",
    "SEVERITY_TAGS",
    "NtfyAlertSink",
    "ntfy_message",
    "ntfy_priority",
    "ntfy_tags",
    "ntfy_title",
]

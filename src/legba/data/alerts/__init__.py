# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.alerts — modular OUTWARD alert-sink plane (P1-1).

The delivery ledger (``alert_sink_deliveries``) already records every
internal alert emit — the agency ChannelEmitter escalations (migration
0061) and the liveness-watchdog global-stall rows — but nothing pushed
those alerts OUTWARD to an operator endpoint. This package is the
outward edge:

* :mod:`.sinks` — the converged alert anatomy
  (:class:`~.sinks.AlertSinkPayload`), the structural
  :class:`~.sinks.AlertSink` protocol, the sink registry (keyed by
  ``sink_kind``), and the :class:`~.sinks.AlertSinkDispatcher` that fans
  an alert out across every registered sink while writing one durable
  ledger row per sink outcome (delivered / failed /
  skipped_unconfigured / skipped_cooldown).
* :mod:`.webhook_sink` — the first concrete sink: a generic HTTP POST
  of the payload JSON to ``LEGBA_ALERT_WEBHOOK_URL``, bounded-retry
  (mirrors :mod:`legba.data.outputs.taxii_client`), never raising into
  the alert path.
* :mod:`.ntfy_sink` — a NATIVE ntfy publish to ``LEGBA_ALERT_NTFY_URL``
  (plaintext body + ``X-Title``/``X-Priority``/``X-Tags``/``X-Click``
  headers, human-readable on a phone), same bounded-retry/redaction
  contract as the webhook sink.

A future sink (``apprise``, ...) is one module implementing the
:class:`~.sinks.AlertSink` protocol plus one
:func:`~.sinks.register_alert_sink` call — the dispatcher, ledger
audit, idempotency and cooldown are shared machinery.

Importing this package registers the built-in sinks (webhook, ntfy).
"""

from __future__ import annotations

from .sinks import (
    ENV_PUBLIC_BASE_URL,
    SEVERITY_ORDER,
    AlertSink,
    AlertSinkDispatcher,
    AlertSinkPayload,
    DeliveryResult,
    build_registered_sinks,
    receipt_link,
    redact_url_to_host,
    register_alert_sink,
    registered_sink_kinds,
    runtime_alert_payload,
)
from .webhook_sink import (
    ENV_WEBHOOK_MIN_SEVERITY,
    ENV_WEBHOOK_URL,
    WebhookAlertSink,
)
from .ntfy_sink import (
    ENV_NTFY_MIN_SEVERITY,
    ENV_NTFY_TOKEN,
    ENV_NTFY_URL,
    NtfyAlertSink,
)

__all__ = [
    "ENV_NTFY_MIN_SEVERITY",
    "ENV_NTFY_TOKEN",
    "ENV_NTFY_URL",
    "ENV_PUBLIC_BASE_URL",
    "ENV_WEBHOOK_MIN_SEVERITY",
    "ENV_WEBHOOK_URL",
    "SEVERITY_ORDER",
    "AlertSink",
    "AlertSinkDispatcher",
    "AlertSinkPayload",
    "DeliveryResult",
    "NtfyAlertSink",
    "WebhookAlertSink",
    "build_registered_sinks",
    "receipt_link",
    "redact_url_to_host",
    "register_alert_sink",
    "registered_sink_kinds",
    "runtime_alert_payload",
]

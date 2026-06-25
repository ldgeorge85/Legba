# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""NATS sink for the ``alert`` output kind (L-197).

Publishes an alert envelope (JSON-encoded) on
``legba.alerts.<severity>`` so internal subscribers — UI live feed,
audit log writer, downstream meta-analyst kinds — can react regardless
of operator-facing routing.

The NATS publisher is structural-typed (``NatsPublisher`` Protocol in
``.._contract``); the real :class:`legba.data.nats.NatsStore` satisfies
it via ``publish_core`` (alerts are an interest-only, streamless
fan-out). Tests inject a recording fake.

Errors are classified:

  * No publisher wired (``deps.nats is None``) → ``permanent_error``
    with ``detail="no-publisher"``. Caller decides whether that's fatal;
    for ``info`` severity it is, because NATS is the *only* default
    surface and the alert would otherwise vanish.
  * Publisher raises → ``transient_error`` so critical-severity retries
    can replay. NATS publishing is the kind of failure that's usually
    transient (broker drain, reconnect window).
"""

from __future__ import annotations

import json
from typing import Any

from ...provenance.models import AlertPayload
from .._contract import OutputContext, OutputDeps, SurfaceResult


def _envelope(payload: AlertPayload, ctx: OutputContext) -> dict[str, Any]:
    # Local copy to avoid an import cycle with .alert (which imports us).
    from datetime import datetime, timezone
    return {
        "kind": "alert",
        "severity": payload.severity,
        "title": payload.title,
        "body": payload.body,
        "confidence": payload.confidence,
        "tags": list(payload.tags),
        "evidence": list(payload.evidence),
        "routing_hint": payload.routing_hint,
        "analyst_id": ctx.analyst_id,
        "analyst_version": ctx.analyst_version,
        "target_id": ctx.target_id,
        "target_version": ctx.target_version,
        "run_id": ctx.run_id,
        "emitted_at": datetime.now(tz=timezone.utc).isoformat(),
    }


async def send_nats_alert(
    payload: AlertPayload,
    *,
    ctx: OutputContext,
    deps: OutputDeps,
    subject: str,
) -> SurfaceResult:
    """Publish ``payload`` on ``subject`` via ``deps.nats``."""
    if deps.nats is None:
        ctx.logger.warning(
            "alert.nats: no publisher wired; subject=%s severity=%s",
            subject,
            payload.severity,
        )
        return SurfaceResult(
            surface="nats",
            outcome="permanent_error",
            detail="no-publisher",
        )

    try:
        body = json.dumps(_envelope(payload, ctx), separators=(",", ":")).encode("utf-8")
        # ``legba.alerts.*`` is an interest-only fan-out with NO JetStream
        # stream — core publish, not ``publish_json`` (which awaits a stream
        # ack and raises NoStreamResponseError here, silently dropping every
        # alert: the root cause of alert_sink_deliveries delivering 0).
        await deps.nats.publish_core(subject, body)
    except Exception as err:                                # pragma: no cover - exercised via fakes
        ctx.logger.warning(
            "alert.nats: publish failed subject=%s err=%s",
            subject,
            err,
        )
        return SurfaceResult(
            surface="nats",
            outcome="transient_error",
            detail=f"{type(err).__name__}: {err}",
        )

    # Success: leave ``detail`` empty. ``detail`` is consumed by the alert
    # dispatcher as ``error_message`` on the audit row, so overloading it to
    # carry the delivered subject would record a successful delivery as an
    # error. The subject is already known to the dispatcher (it derives it /
    # passes it in as ``destination``) and is recoverable from the severity.
    return SurfaceResult(surface="nats", outcome="delivered", detail="")


__all__ = ["send_nats_alert"]

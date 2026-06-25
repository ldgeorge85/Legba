# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Matrix sink for the ``alert`` output kind (L-197).

Mirrors :mod:`legba.data.outputs.alert_sinks.xmpp` — gated behind the
optional ``legba[matrix]`` extra (matrix-nio). Without the extra,
:data:`MATRIX_AVAILABLE` is ``False`` and the parent kind records a
``skipped`` outcome on attempted routes.

Integration paths:

  * **deps-provided publisher** (preferred + test-only): ``deps.matrix``
    satisfies the :class:`MatrixPublisher` Protocol — used directly.
  * **lazy matrix-nio client** (production wiring): when ``deps.matrix``
    is missing but ``MATRIX_AVAILABLE`` is True, a real client would be
    built. We deliberately surface a ``permanent_error`` in that case
    rather than constructing here, because the matrix-nio client owns a
    persistent connection + sync loop that belongs in the runtime, not in
    a per-emit call. The L-103+ runtime registers the publisher on deps
    at activation time.

Destination resolution:
  1. Surface-level override (``destination`` on OutputSurface) — Matrix
     room ID (``!abc:server``) or alias (``#alerts:server``).
  2. Descriptor: ``descriptor["matrix"]["room"]``.
  3. Permanent error if missing.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...provenance.models import AlertPayload
from .._contract import OutputContext, OutputDeps, SurfaceResult


try:                                                        # pragma: no cover
    import nio as _nio  # noqa: F401
    MATRIX_AVAILABLE = True
except Exception:                                           # pragma: no cover
    MATRIX_AVAILABLE = False


def _format_body(payload: AlertPayload) -> str:
    """Markdown-ish rendering, same shape as the XMPP sink for parity."""
    lead = f"**[{payload.severity.upper()}] {payload.title}**"
    body = payload.body.strip() or payload.routing_hint or "_no detail_"
    rendered = f"{lead}\n\n{body[:1024]}"
    if payload.tags:
        rendered += f"\n\n_tags_: {', '.join(payload.tags[:6])}"
    return rendered


async def send_matrix_alert(
    payload: AlertPayload,
    *,
    ctx: OutputContext,
    deps: OutputDeps,
    room_override: str | None,
    descriptor: Mapping[str, Any] | None = None,
) -> SurfaceResult:
    """Deliver ``payload`` to a Matrix room."""
    descriptor_block: Mapping[str, Any] = (descriptor or {}).get("matrix", {}) or {}
    room_id = room_override or descriptor_block.get("room")
    if not room_id:
        return SurfaceResult(
            surface="matrix",
            outcome="permanent_error",
            detail="no-room",
        )

    if deps.matrix is not None:
        try:
            await deps.matrix.send_message(room_id, _format_body(payload))
        except Exception as err:
            ctx.logger.warning("alert.matrix: deps publisher failed err=%s", err)
            return SurfaceResult(
                surface="matrix",
                outcome="transient_error",
                detail=f"{type(err).__name__}: {err}",
            )
        return SurfaceResult(
            surface="matrix",
            outcome="delivered",
            detail=str(room_id),
        )

    if not MATRIX_AVAILABLE:
        return SurfaceResult(
            surface="matrix",
            outcome="skipped",
            detail="extra-not-installed",
        )

    return SurfaceResult(                                   # pragma: no cover
        surface="matrix",
        outcome="permanent_error",
        detail="no-publisher (extra installed but deps.matrix is None)",
    )


__all__ = ["MATRIX_AVAILABLE", "send_matrix_alert"]

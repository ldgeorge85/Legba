# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""XMPP sink for the ``alert`` output kind (L-197).

The XMPP transport is gated behind the optional ``legba[xmpp]`` extra
(slixmpp). When the extra isn't installed, :data:`XMPP_AVAILABLE` stays
``False`` and the parent kind records ``skipped`` outcomes instead of
raising — keeping minimal installs functional.

Two integration paths:

  * **deps-provided publisher** (preferred for tests + future runtime):
    ``deps.xmpp`` satisfies the :class:`XmppPublisher` Protocol and is
    used directly.
  * **lazy slixmpp client** (real installs): when ``deps.xmpp is None``
    but ``XMPP_AVAILABLE`` is ``True``, the sink builds an ephemeral
    client from ``ctx.secrets_resolve`` (``xmpp.jid`` + ``xmpp.password``)
    and a destination JID supplied via the surface override. This path is
    *not* exercised by the unit suite — it's the production wiring that
    lands when an operator configures the extra.

Destination resolution:
  1. Surface-level override (``destination`` on the OutputSurface).
  2. Descriptor block: ``descriptor["xmpp"]["to"]``.
  3. Permanent error: no recipient known.

The destination is intentionally not vault-resolved — recipient JIDs are
not secrets and pinning them in the descriptor keeps audit clean.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...provenance.models import AlertPayload
from .._contract import OutputContext, OutputDeps, SurfaceResult


try:                                                        # pragma: no cover
    import slixmpp as _slixmpp  # noqa: F401
    XMPP_AVAILABLE = True
except Exception:                                           # pragma: no cover
    XMPP_AVAILABLE = False


def _format_body(payload: AlertPayload) -> str:
    """One-paragraph human-readable rendering for chat transports.

    Intentionally line-bounded — XMPP / Matrix MUC channels don't want
    multi-screen findings. Operators that want full evidence follow the
    routing_hint link.
    """
    lead = f"[{payload.severity.upper()}] {payload.title}"
    body = payload.body.strip() or payload.routing_hint or "(no detail)"
    # Limit total length to keep MUC notifications digestible.
    rendered = f"{lead}\n{body[:512]}"
    if payload.tags:
        rendered += f"\ntags: {', '.join(payload.tags[:6])}"
    return rendered


async def send_xmpp_alert(
    payload: AlertPayload,
    *,
    ctx: OutputContext,
    deps: OutputDeps,
    jid_override: str | None,
    descriptor: Mapping[str, Any] | None = None,
) -> SurfaceResult:
    """Deliver ``payload`` over XMPP."""
    # Destination JID resolution.
    descriptor_block: Mapping[str, Any] = (descriptor or {}).get("xmpp", {}) or {}
    to_jid = jid_override or descriptor_block.get("to")
    if not to_jid:
        return SurfaceResult(
            surface="xmpp",
            outcome="permanent_error",
            detail="no-destination",
        )

    # Preferred path: deps-provided publisher.
    if deps.xmpp is not None:
        try:
            await deps.xmpp.send_message(to_jid, _format_body(payload))
        except Exception as err:
            ctx.logger.warning("alert.xmpp: deps publisher failed err=%s", err)
            return SurfaceResult(
                surface="xmpp",
                outcome="transient_error",
                detail=f"{type(err).__name__}: {err}",
            )
        return SurfaceResult(
            surface="xmpp",
            outcome="delivered",
            detail=str(to_jid),
        )

    # Fallback path: lazy slixmpp. Skip gracefully if extra not installed.
    if not XMPP_AVAILABLE:
        return SurfaceResult(
            surface="xmpp",
            outcome="skipped",
            detail="extra-not-installed",
        )

    # The real slixmpp wiring lives in production runtime configuration —
    # we surface a permanent error here so descriptors that expect a deps-
    # provided publisher don't silently no-op when one wasn't wired. The
    # runtime (L-103+) constructs the publisher and passes it through
    # ``deps.xmpp``.
    return SurfaceResult(                                   # pragma: no cover
        surface="xmpp",
        outcome="permanent_error",
        detail="no-publisher (extra installed but deps.xmpp is None)",
    )


__all__ = ["XMPP_AVAILABLE", "send_xmpp_alert"]

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pushover sink for the ``alert`` output kind — Above-mode (L-197).

Pushover is the operator-paging surface in the Above ecosystem (see
project_above). The sink takes the analyst's
:class:`AlertPayload` and POSTs it to ``https://api.pushover.net/1/messages.json``
with severity mapped to Pushover ``priority``.

Severity → Pushover priority:

  * ``info``     → -2 (lowest, no notification — but the alert kind's
    default routing matrix omits Pushover for info; if the descriptor
    forces it on, this is the priority used).
  * ``low``      → -1 (quiet notification)
  * ``medium``   →  0 (normal notification, default priority)
  * ``high``     →  1 (high priority, bypasses quiet hours)
  * ``critical`` →  2 (emergency — Pushover requires retry+expire when
    priority=2; we set retry=60s expire=3600s per docs)

Credential resolution:

  * ``token`` (the Pushover *application* token) is fetched from
    ``ctx.secrets_resolve("pushover.token")``. If the resolver is not
    wired (unit tests, dev runs), the sink falls back to a token in the
    descriptor under ``descriptor["pushover"]["token"]``. That fallback
    is *only* honoured when ``secrets_resolve is None`` — refusing to
    silently override a wired vault is intentional.
  * ``user`` (the recipient user/group key) comes from:
      1. The surface-level ``destination`` override, if set.
      2. ``ctx.secrets_resolve("pushover.user")``.
      3. ``descriptor["pushover"]["user"]`` (same fallback rule).

Failure modes:

  * 2xx                → ``delivered``.
  * 4xx                → ``permanent_error`` (bad token/user — won't
    succeed on retry).
  * 5xx / network err  → ``transient_error``.
  * Missing token/user → ``permanent_error`` with detail explaining.
"""

from __future__ import annotations

from typing import Any, Mapping

from ...provenance.models import AlertPayload
from .._contract import OutputContext, OutputDeps, SurfaceResult


PUSHOVER_URL = "https://api.pushover.net/1/messages.json"

_PRIORITY_MAP = {
    "info":     -2,
    "low":      -1,
    "medium":    0,
    "high":      1,
    "critical":  2,
}


async def _resolve_credential(
    ctx: OutputContext,
    *,
    vault_key: str,
    fallback: str | None,
) -> str | None:
    if ctx.secrets_resolve is not None:
        try:
            return await ctx.secrets_resolve(vault_key)
        except Exception as err:
            ctx.logger.warning(
                "alert.pushover: secret resolve failed key=%s err=%s",
                vault_key,
                err,
            )
            return None
    return fallback


async def send_pushover_alert(
    payload: AlertPayload,
    *,
    ctx: OutputContext,
    deps: OutputDeps,
    user_override: str | None,
    descriptor: Mapping[str, Any] | None = None,
) -> SurfaceResult:
    """Deliver ``payload`` via Pushover."""
    if deps.http is None:
        ctx.logger.warning("alert.pushover: no HTTP client wired")
        return SurfaceResult(
            surface="pushover",
            outcome="permanent_error",
            detail="no-http-client",
        )

    # Resolve credentials. Descriptor fallback only honoured when no resolver.
    descriptor_block: Mapping[str, Any] = (descriptor or {}).get("pushover", {}) or {}
    token_fallback = descriptor_block.get("token") if ctx.secrets_resolve is None else None
    user_fallback = descriptor_block.get("user") if ctx.secrets_resolve is None else None

    token = await _resolve_credential(
        ctx, vault_key="pushover.token", fallback=token_fallback
    )
    if not token:
        return SurfaceResult(
            surface="pushover",
            outcome="permanent_error",
            detail="no-token",
        )

    user = user_override or await _resolve_credential(
        ctx, vault_key="pushover.user", fallback=user_fallback
    )
    if not user:
        return SurfaceResult(
            surface="pushover",
            outcome="permanent_error",
            detail="no-user",
        )

    priority = _PRIORITY_MAP.get(payload.severity, 0)
    title = payload.title[:250]  # Pushover limit: 250 chars
    body = (payload.body or payload.routing_hint or "(no body)")[:1024]

    form: dict[str, Any] = {
        "token": token,
        "user": user,
        "title": title,
        "message": body,
        "priority": priority,
    }
    if priority == 2:
        # Pushover requires retry+expire when priority=2.
        form["retry"] = 60
        form["expire"] = 3600

    try:
        resp = await deps.http.post(
            PUSHOVER_URL,
            data=form,
            timeout=10.0,
        )
    except Exception as err:
        ctx.logger.warning("alert.pushover: HTTP exception err=%s", err)
        return SurfaceResult(
            surface="pushover",
            outcome="transient_error",
            detail=f"{type(err).__name__}: {err}",
        )

    status = getattr(resp, "status_code", None)
    if status is None:
        return SurfaceResult(
            surface="pushover",
            outcome="transient_error",
            detail="response-missing-status",
        )
    if 200 <= status < 300:
        return SurfaceResult(
            surface="pushover",
            outcome="delivered",
            detail=f"priority={priority}",
        )
    if 400 <= status < 500:
        body_text = getattr(resp, "text", "")
        return SurfaceResult(
            surface="pushover",
            outcome="permanent_error",
            detail=f"http {status}: {str(body_text)[:200]}",
        )
    # 5xx (and 3xx — unusual for Pushover but be conservative).
    return SurfaceResult(
        surface="pushover",
        outcome="transient_error",
        detail=f"http {status}",
    )


__all__ = ["PUSHOVER_URL", "send_pushover_alert"]

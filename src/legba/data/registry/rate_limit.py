# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Defense-in-depth rate limiting for the registry's expensive endpoints.

The registry sits behind Caddy (single perimeter password + a single injected
bearer), so a compromised/abused token could otherwise drive the expensive
actor-dispatch endpoints (``/consult``, ``/deep_consult``) at full rate — each
spends LLM budget and pins a registry worker for up to 180s. This module wires
``slowapi`` so those endpoints return ``429 Too Many Requests`` past a per-key
budget.

Lower-risk choice (vs a Caddy-layer ``rate_limit``): the Caddy ``rate_limit``
directive is NOT a stock-Caddy built-in — it needs the third-party
``caddy-ratelimit`` plugin compiled via a custom ``xcaddy`` image, changing the
edge build and forcing an edge rebuild/redeploy (an edge-wide blast radius).
``slowapi`` is a pure-pip, in-app dep scoped to the registry process only.

Key function: the bearer principal (single-tenant today, but the right axis —
when scoped tokens land, the limit is naturally per-principal). Falls back to
the client address when no bearer is presented.

Degrade-not-break: if ``slowapi`` is somehow unavailable at runtime, the
limiter object exposes a no-op ``.limit(...)`` decorator and ``install`` logs a
loud warning and returns — the registry still boots and serves (the limit is a
hardening layer, not a correctness invariant). The dep is declared in
``pyproject.toml`` so a built image always has it.
"""
from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from fastapi import FastAPI, Request

logger = logging.getLogger(__name__)

# Per-principal budgets. Generous enough that an interactive operator never
# trips them, tight enough that a runaway script is throttled.
CONSULT_RATE_LIMIT = os.getenv("LEGBA_RATE_LIMIT_CONSULT", "10/minute")
DEEP_CONSULT_RATE_LIMIT = os.getenv("LEGBA_RATE_LIMIT_DEEP_CONSULT", "10/minute")


def _principal_key(request: "Request") -> str:
    """Rate-limit key = bearer principal, else client address.

    Behind Caddy the bearer is the single injected token, so today this keys
    all real traffic to one bucket (intended — abuse protection, not tenancy).
    When scoped tokens land this becomes naturally per-principal.
    """
    auth = request.headers.get("authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        if token:
            # Don't put the raw secret in limiter state — a short stable digest.
            import hashlib

            return "tok:" + hashlib.sha256(token.encode("utf-8")).hexdigest()[:16]
    client = request.client
    return client.host if client is not None else "anon"


class _NoopLimiter:
    """Stand-in used only when slowapi is unavailable — decorators pass through.

    This is NOT a stub of a claimed feature: rate limiting is declared as a
    best-effort hardening layer (see module docstring + RUNBOOK). ``install``
    warns loudly so the operator sees that enforcement is inert.
    """

    enabled = False

    def limit(self, *_args: Any, **_kwargs: Any) -> Callable[[Any], Any]:
        def _decorator(fn: Any) -> Any:
            return fn

        return _decorator


def build_limiter() -> Any:
    """Return a configured ``slowapi.Limiter`` or a loud-warn no-op fallback."""
    try:
        from slowapi import Limiter
    except Exception as exc:  # pragma: no cover - only when dep absent
        logger.warning(
            "slowapi unavailable (%s) — registry rate limiting is INERT this "
            "boot; rebuild the registry image to enforce it",
            exc,
        )
        return _NoopLimiter()
    limiter = Limiter(key_func=_principal_key, headers_enabled=True)
    limiter.enabled = True
    return limiter


# Module-level singleton — the endpoint modules decorate against THIS instance
# (``@limiter.limit(...)``) at route-definition time, and ``create_app``
# installs the same instance's middleware/handler. slowapi resolves the live
# limiter via ``request.app.state.limiter`` at request time.
limiter: Any = build_limiter()


def install(app: "FastAPI", limiter: Any) -> None:
    """Attach the limiter + its 429 handler + middleware to ``app``.

    Safe to call with the no-op fallback (returns early after a warning)."""
    if not getattr(limiter, "enabled", False):
        logger.warning(
            "registry rate limiting not installed (limiter disabled/absent)"
        )
        return
    from slowapi import _rate_limit_exceeded_handler
    from slowapi.errors import RateLimitExceeded
    from slowapi.middleware import SlowAPIMiddleware

    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    app.add_middleware(SlowAPIMiddleware)
    logger.info("registry rate limiting installed (consult/deep_consult)")

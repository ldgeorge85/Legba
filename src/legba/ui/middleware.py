"""Auth middleware for Legba UI.

Checks JWT cookie on /api/ routes when AUTH_ENABLED=true.
Skips auth entirely when AUTH_ENABLED is not set or not "true" (backward compatible).
Always allows auth endpoints (/api/v2/auth/*) and health endpoints through.
"""

from __future__ import annotations

import logging
import os

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse

from .auth import verify_token

logger = logging.getLogger("legba.ui.middleware")

# Paths that never require auth
_AUTH_EXEMPT = frozenset({
    "/api/v2/auth/login",
    "/api/v2/auth/logout",
    "/api/v2/auth/me",
    "/api/v2/health",
    "/health",
})

TOKEN_COOKIE_NAME = "legba_token"


class AuthMiddleware(BaseHTTPMiddleware):
    """JWT authentication middleware.

    Only active when AUTH_ENABLED env var is "true".
    Checks all /api/ routes for a valid JWT cookie.
    """

    async def dispatch(self, request: Request, call_next):
        # Skip if auth is disabled (default — backward compatible)
        if os.getenv("AUTH_ENABLED", "").lower() != "true":
            return await call_next(request)

        path = request.url.path

        # Only protect /api/ routes
        if not path.startswith("/api/"):
            return await call_next(request)

        # Exempt paths (auth endpoints, health)
        if path in _AUTH_EXEMPT:
            return await call_next(request)

        # Check for JWT cookie
        token = request.cookies.get(TOKEN_COOKIE_NAME)
        if not token:
            return JSONResponse(
                {"error": {"status": 401, "message": "Authentication required"}},
                status_code=401,
            )

        payload = verify_token(token)
        if payload is None:
            return JSONResponse(
                {"error": {"status": 401, "message": "Invalid or expired token"}},
                status_code=401,
            )

        # Attach user info to request state for downstream use
        request.state.user = {
            "username": payload.get("sub"),
            "role": payload.get("role"),
        }

        return await call_next(request)

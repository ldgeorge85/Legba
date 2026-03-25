"""Standard API response helpers for Legba UI.

Provides a consistent error envelope across all API endpoints:

    {
        "error": {
            "status": 404,
            "message": "Entity not found",
            "detail": "No entity with id abc-123"  // optional
        }
    }
"""

from __future__ import annotations

from typing import Optional

from fastapi.responses import JSONResponse


def api_error(status: int, message: str, detail: Optional[str] = None) -> JSONResponse:
    """Return a standardized JSON error response.

    Parameters
    ----------
    status : int
        HTTP status code (e.g. 400, 401, 404, 500).
    message : str
        Short human-readable error message.
    detail : str, optional
        Additional context (stack trace excerpt, field name, etc.).

    Returns
    -------
    JSONResponse
        A FastAPI JSONResponse with the standard error envelope.
    """
    body: dict = {"error": {"status": status, "message": message}}
    if detail:
        body["error"]["detail"] = detail
    return JSONResponse(body, status_code=status)

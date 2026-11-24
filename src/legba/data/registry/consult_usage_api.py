# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /api/v1/consult/usage`` — one principal's consult usage per day (X2 C1).

The account-linking hook. Every consult assistant turn now carries a usage
receipt (``consult_turns.usage``, ``consult_usage/1``) and a verification stamp
(``consult_turns.verification``, ``consult_verification/1``); this route sums
them per UTC day for the sessions one principal opened. Today the principal is
the bearer label the front door stamps on ``consult_sessions.principal``
(B1's); when user accounts land (B3) the same route keys on the user and
nothing about its shape changes.

Absence renders as absence: a day whose turns carried no usage reads
``input_tokens: null``, not 0; ``cost_usd`` sums only turns whose route PRICED
its calls (``priced_turns``) and is ``null`` when none did — the self-hosted
plane is unpriced, not free-by-estimate; turns written before 0234 count under
``verification_missing``, never as judged or unjudged.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status

from . import route_roles as _rr
from .api import RegistryAPIDeps, require_bearer

__all__ = ["USAGE_SQL", "build_consult_usage_router"]

#: The one read. ``$1`` principal, ``$2`` window start. Served by the
#: ``consult_sessions`` pkey + ``idx_consult_turns_session_created``; the live
#: EXPLAIN is in the C1 report.
USAGE_SQL = """
SELECT (t.created_at AT TIME ZONE 'UTC')::date AS day,
       count(*) AS turns,
       count(*) FILTER (WHERE t.usage->>'input_tokens' IS NOT NULL) AS turns_with_usage,
       sum((t.usage->>'input_tokens')::bigint) AS input_tokens,
       sum((t.usage->>'output_tokens')::bigint) AS output_tokens,
       sum((t.usage->>'cost_usd')::numeric) AS cost_usd,
       count(*) FILTER (WHERE t.usage->>'cost_usd' IS NOT NULL) AS priced_turns,
       sum((t.usage->>'elapsed_s')::numeric) AS elapsed_s,
       count(*) FILTER (WHERE t.verification->>'state' = 'judged') AS judged,
       count(*) FILTER (WHERE t.verification->>'state' = 'unassessable') AS unassessable,
       count(*) FILTER (WHERE t.verification->>'state' = 'not_judged') AS not_judged,
       count(*) FILTER (WHERE t.verification IS NULL) AS verification_missing
  FROM consult_turns t
  JOIN consult_sessions s ON s.id = t.session_id
 WHERE t.role = 'assistant'
   AND s.principal = $1
   AND t.created_at >= $2
 GROUP BY 1
 ORDER BY 1 DESC
"""

_COUNT_KEYS = (
    "turns", "turns_with_usage", "priced_turns",
    "judged", "unassessable", "not_judged", "verification_missing",
)
_SUM_KEYS = ("input_tokens", "output_tokens", "cost_usd", "elapsed_s")


def _num(value: Any) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return round(float(value), 6)
    return value


def _totals(days: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {k: sum(d[k] for d in days) for k in _COUNT_KEYS}
    for key in _SUM_KEYS:
        vals = [d[key] for d in days if d[key] is not None]
        out[key] = round(sum(vals), 6) if vals else None
    return out


def build_consult_usage_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["consult"])

    @router.get("/consult/usage")
    async def consult_usage(
        request: Request,
        user: str | None = Query(default=None, max_length=200),
        days: int = Query(default=30, ge=1, le=366),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """Consult usage per UTC day for ``user`` (default: the caller).

        X3-B3: a signed-in user's principal is ``user:<id>``; anyone below the
        operator class may read only their OWN usage (403 otherwise)."""
        who = user or principal
        if who != principal and not _rr.resolve_principal(request).is_operator:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={
                    "error": "not_row_owner",
                    "required_role": _rr.ROLE_OPERATOR,
                    "message": "only the operator may read another user's usage",
                },
            )
        now = datetime.now(tz=timezone.utc)
        since = (now - timedelta(days=days)).replace(
            hour=0, minute=0, second=0, microsecond=0,
        )
        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(USAGE_SQL, who, since)
        per_day = [
            {
                "day": r["day"].isoformat(),
                **{k: int(r[k]) for k in _COUNT_KEYS},
                **{k: _num(r[k]) for k in _SUM_KEYS},
            }
            for r in rows
        ]
        return {
            "user": who,
            "basis": (
                "consult_turns.usage and .verification on assistant turns of "
                "sessions this principal opened; days in UTC"
            ),
            "window_days": days,
            "since": since.isoformat(),
            "as_of": now.isoformat(),
            "days": per_day,
            "totals": _totals(per_day),
        }

    return router

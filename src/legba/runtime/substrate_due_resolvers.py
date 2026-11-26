# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``list_due_resolvers`` — the forecast ledger's due-date reader.

The consult had no way to ask "what resolves next": open-question watches,
inquiry hypotheses and the forecast ledger each live elsewhere, and a model
spent ~30 calls hunting for a ledger. This read answers from the ledger
itself (:func:`legba.data.forecast_resolution.list_due_resolvers`): the
questions due or overdue within a horizon, the resolver that decides each,
every issuer's number, and the newest rule proposal with the judgement it
waits on.

The port keeps a thin delegating method (it sits at its size ceiling), as
``substrate_source_health`` does. One-way import: nothing here reaches back
into the port.
"""

from __future__ import annotations

from typing import Any

from ..data.forecast_resolution import list_due_resolvers as _list_due

__all__ = ["list_due_resolvers"]

_MAX_HORIZON_DAYS = 120
_MAX_ROWS = 100


async def list_due_resolvers(
    pool: Any, *, horizon_days: int = 14, include_closed: bool = False, limit: int = 50,
) -> dict[str, Any]:
    horizon = max(0, min(int(horizon_days), _MAX_HORIZON_DAYS))
    rows = max(1, min(int(limit), _MAX_ROWS))
    try:
        async with pool.acquire() as conn:
            return await _list_due(conn, horizon_days=horizon, include_closed=bool(include_closed), limit=rows)
    except Exception as exc:  # noqa: BLE001 — the ledger unreadable is said, not hidden
        return {"error": f"forecast ledger unreadable ({type(exc).__name__})", "items": [], "count": 0}

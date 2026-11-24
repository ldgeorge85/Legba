# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/ledger/resolution-records`` — one question's resolution record (U-11).

The append-only ``forecast_resolution_records`` rows (migration 0244) for ONE
question, newest first: every proposal the resolver job wrote (its outcome,
the evidence it cites, the clauses of the sealed criterion each piece of
evidence meets, the scope it searched, the void rule that applies and the
judgements it waits on) and every act on it (confirmation, override,
correction, with the actor and the reason).

The scorecard (``GET /v3/ledger/scorecard``) already carries each question's
newest proposal and act, but only their outcome, actor and an evidence COUNT.
The console's resolution queue (the admin side) shows the evidence itself
before anyone confirms, so it reads this route for the one question it opens.

Read-only; bearer-gated like its siblings, the default ``read`` role. It
CONFIRMS NOTHING: confirming, overriding and correcting stay with
``scripts/resolve_forecasts.py`` (``forecast_resolution.confirm_proposal``);
no write route exists. Bounded: one question (the 0244
``idx_forecast_records_question_recorded`` index), at most ``limit`` rows,
each row's evidence cut at :data:`EVIDENCE_CAP` items with the total kept.
Defensive: the table unreadable (0244 not applied) ⇒ ``available: false`` at
HTTP 200, never a 500 and never an empty list that reads as "nothing proposed".

Registry-slim: stdlib + fastapi + ``.api`` only.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from fastapi import APIRouter, Depends, Query, Request

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

FORECAST_RECORDS_ROUTE_VERSION = "2026-11/u11"
_ROUTE = "/ledger/resolution-records"

#: The most evidence items one record carries in the answer (the rest are counted).
EVIDENCE_CAP = 40

RECORDS_FOR_QUESTION_SQL = """
SELECT id::text AS id, question_id::text AS question_id, record_kind, outcome,
       resolver_kind, resolver_version, as_of, recorded_at, actor,
       evidence::text AS evidence, clauses::text AS clauses,
       judgements::text AS judgements, searched::text AS searched,
       void_rule::text AS void_rule, basis_record_id::text AS basis_record_id,
       supersedes_id::text AS supersedes_id, resolution_id::text AS resolution_id,
       reason, content_sha256
  FROM forecast_resolution_records
 WHERE question_id = $1::uuid
 ORDER BY recorded_at DESC, id DESC
 LIMIT $2
"""

_UUID = r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _loads(raw: Any, default: Any) -> Any:
    if raw is None:
        return default
    if isinstance(raw, (list, dict)):
        return raw
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    return iso() if callable(iso) else str(value)


def record_out(row: Any) -> dict[str, Any]:
    """One stored record as the route answers it (evidence capped, JSON decoded)."""
    evidence = _loads(row["evidence"], [])
    if not isinstance(evidence, list):
        evidence = []
    return {
        "id": row["id"],
        "question_id": row["question_id"],
        "record_kind": row["record_kind"],
        "outcome": row["outcome"],
        "resolver_kind": row["resolver_kind"],
        "resolver_version": row["resolver_version"],
        "as_of": _iso(row["as_of"]),
        "recorded_at": _iso(row["recorded_at"]),
        "actor": row["actor"],
        "evidence": evidence[:EVIDENCE_CAP],
        "evidence_total": len(evidence),
        "clauses": _loads(row["clauses"], []),
        "judgements": _loads(row["judgements"], []),
        "searched": _loads(row["searched"], {}),
        "void_rule": _loads(row["void_rule"], None),
        "basis_record_id": row["basis_record_id"],
        "supersedes_id": row["supersedes_id"],
        "resolution_id": row["resolution_id"],
        "reason": row["reason"],
        "content_sha256": row["content_sha256"],
    }


def build_forecast_records_router(deps: RegistryAPIDeps) -> APIRouter:
    """Build the read-only router (mounted at ``/api/v1/v3``)."""
    router = APIRouter(tags=["ledger"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE)
    async def ledger_resolution_records(
        request: Request,
        question_id: str = Query(..., pattern=_UUID),
        limit: int = Query(default=20, ge=1, le=100),
        principal: str = Depends(require_bearer),
    ) -> dict[str, Any]:
        """One question's resolution record, newest first."""
        d = _get_deps(request)
        base: dict[str, Any] = {
            "version": FORECAST_RECORDS_ROUTE_VERSION,
            "question_id": question_id.lower(),
            "evidence_cap": EVIDENCE_CAP,
            "confirm_route": None,
        }
        try:
            async with d.descriptor_registry.pg.acquire() as conn:
                rows = await conn.fetch(RECORDS_FOR_QUESTION_SQL, question_id.lower(), limit)
        except Exception as exc:  # noqa: BLE001 — 0244 not applied: degrade, HTTP 200
            logger.info("v3.ledger.resolution_records.unavailable err_class=%s", type(exc).__name__)
            return {**base, "available": False, "records": []}
        return {**base, "available": True, "records": [record_out(r) for r in rows]}

    return router

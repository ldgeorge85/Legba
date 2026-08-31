# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``GET /v3/system/external-audit`` — the standing external auditor's verdicts.

The ops-deck surface for the D5 audit plane, built to the GLASS-3 idiom: its own
module (rather than another block on ``v3_api``), a
``build_external_audit_router(deps)`` factory, bearer-gated, and — the rule that
actually matters for a polled panel — a read failure logs at INFO and returns an
honest empty payload at HTTP 200 with ``measured: false``, never a 500.

WHAT AN OPERATOR IS READING FOR, in priority order. This route answers three
questions, and they are NOT the same question:

  1. **Is the auditor alive?** The ``heartbeat`` block, read straight off the
     durable row the handler upserts every run
     (``alert_trigger_watermarks`` under ``trigger_class='external_audit'``).
     ``analyst_traces`` cannot answer this: a run whose search provider is
     unbound still ends ``status='success'``, so a trace-based liveness read
     shows a dead auditor as a healthy one — the exact shape of the judge
     outage that went unnoticed for three days. The heartbeat carries
     ``claims_checked`` (UNCHECKED verdicts deliberately excluded) and an
     explicit ``degraded_reason``, so "quiet world" and "broken auditor" are
     distinguishable on the wire.
  2. **What did it find?** The verdict mix over a trailing window, plus the
     CONTRADICTED rows named individually — a contradiction is rare by
     construction and is the one verdict an operator should read every instance
     of.
  3. **Was it measuring anything?** ``checked`` vs ``n``. A window of pure
     ``UNCHECKED`` is a broken search plane wearing an audit's clothes.

TWO HONESTY RULES, both structural rather than conventional:

  * Every rate ships with the count it was computed over, and a rate over zero
    rows is ``None``, never ``0.0``. ``contradiction_rate`` is over CHECKED
    claims only — dividing by claims the auditor never managed to check would
    silently flatter the platform every time the search plane degraded.
  * ``stale`` is computed against a threshold this route publishes
    (``stale_after_hours``) rather than baked into a caller. The auditor's
    cadence is daily; a heartbeat older than a comfortable multiple of that is
    the signal, and the multiple is visible so nobody has to guess what the
    boolean meant.

NO MIGRATION. Verdicts are ``analyst_outputs`` rows with ``kind='critique'``
pinned on the ``'External audit%'`` title prefix — the mirror of the
``'Faithfulness verify%'`` pin every faithfulness reader uses, and the reason the
two critique planes can share a table without ever reading each other's rows.
The heartbeat rides migration 0091's existing watermark table.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import APIRouter, Depends, Query, Request
from pydantic import BaseModel, Field

from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)

_ROUTE = "/system/external-audit"

DEFAULT_WINDOW_DAYS = 14
MAX_WINDOW_DAYS = 90

#: Daily cadence × 2 + margin. A heartbeat older than this means the auditor
#: missed a whole day and its recovery tick, which is an operator event.
DEFAULT_STALE_AFTER_HOURS = 30.0

#: Named individually rather than counted — see the module docstring.
_CONTRADICTION_SAMPLE_CAP = 25

# Mirrors ``standing_auditor`` without importing it: this module ships in the
# registry image, which does not carry the analyst runtime.
_TITLE_PREFIX = "External audit%"
_TRIGGER_CLASS = "external_audit"
_HEARTBEAT_KEY = "_heartbeat"

_HEARTBEAT_SQL = """
SELECT state, fired_at, first_seen, updated_at
FROM alert_trigger_watermarks
WHERE trigger_class = $1 AND watermark_key = $2
"""

#: The verdict lives at ``data->'data'->'external_audit'->>'verdict'``: the
#: ``data`` COLUMN is the whole CritiquePayload dump, so the payload's own
#: ``data`` field is one level down.
_VERDICTS_SQL = """
SELECT ao.id,
       ao.produced_at,
       ao.target_id,
       ao.data->'data'->'external_audit'->>'verdict'    AS verdict,
       ao.data->'data'->'external_audit'->>'desk_key'   AS desk_key,
       ao.data->'data'->'external_audit'->>'claim'      AS claim,
       ao.data->'data'->'external_audit'->>'rationale'  AS rationale,
       ao.data->'data'->'external_audit'->>'severity'   AS audited_severity,
       ao.data->'data'->'external_audit'->>'analyst_id' AS audited_analyst_id,
       ao.data->'data'->'external_audit'->'source_urls' AS source_urls,
       ao.data->'data'->'external_audit'->>'pipeline_version' AS pipeline_version
FROM analyst_outputs ao
WHERE ao.kind = 'critique'
  AND ao.title LIKE $1
  AND ao.produced_at > $2
ORDER BY ao.produced_at DESC
LIMIT $3
"""

_VERDICT_FETCH_CAP = 2000

#: The four verdicts the plane can write. Published so a panel never hardcodes
#: them, and so a window with none of a given verdict still shows the key at 0.
VERDICTS: tuple[str, ...] = (
    "SUPPORTED", "CONTRADICTED", "NOT_FOUND", "UNCHECKED",
)
#: The three that represent a COMPLETED external check.
CHECKED_VERDICTS: frozenset[str] = frozenset(
    {"SUPPORTED", "CONTRADICTED", "NOT_FOUND"}
)


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class AuditHeartbeat(BaseModel):
    """The auditor's own liveness row. ``present=False`` means it never ran."""

    present: bool = False
    ran_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    last_alert_at: Optional[datetime] = None
    age_hours: Optional[float] = None
    stale: bool = False
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS
    healthy: bool = False
    degraded: bool = False
    degraded_reason: str = ""
    heads_sampled: list[str] = Field(default_factory=list)
    claims_extracted: Optional[int] = None
    claims_checked: Optional[int] = None
    verdicts: dict[str, int] = Field(default_factory=dict)
    pipeline_version: str = ""


class AuditContradiction(BaseModel):
    """One CONTRADICTED verdict, named in full."""

    id: str
    produced_at: datetime
    desk_key: str = ""
    audited_analyst_id: str = ""
    audited_severity: Optional[str] = None
    claim: str = ""
    rationale: str = ""
    source_urls: list[str] = Field(default_factory=list)


class ExternalAuditOut(BaseModel):
    """``GET /v3/system/external-audit`` response."""

    generated_at: datetime
    window_days: int
    measured: bool = False
    heartbeat: AuditHeartbeat = Field(default_factory=AuditHeartbeat)
    #: Verdicts written in the window, by verdict. Always carries all four keys.
    by_verdict: dict[str, int] = Field(default_factory=dict)
    n: int = 0
    #: Verdicts representing a completed check (n minus UNCHECKED).
    checked: int = 0
    #: CONTRADICTED / checked. ``None`` over zero checked — never 0.0.
    contradiction_rate: Optional[float] = None
    #: How many distinct desks the window's verdicts touched.
    desks_audited: int = 0
    pipeline_versions: list[str] = Field(default_factory=list)
    contradictions: list[AuditContradiction] = Field(default_factory=list)


def _blank(window_days: int, generated_at: datetime) -> ExternalAuditOut:
    """The honest empty payload — HTTP 200, ``measured: false``."""
    return ExternalAuditOut(
        generated_at=generated_at,
        window_days=window_days,
        measured=False,
        by_verdict={v: 0 for v in VERDICTS},
    )


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------


def _coerce_state(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return {}
    return dict(raw) if isinstance(raw, dict) else {}


def _coerce_urls(raw: Any) -> list[str]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    return [str(u) for u in raw] if isinstance(raw, list) else []


def build_heartbeat(
    row: Any, *, now: datetime, stale_after_hours: float
) -> AuditHeartbeat:
    """Project the watermark row into the heartbeat block.

    A row present but unparsable is reported ``present=True, healthy=False``
    rather than absent: "the auditor wrote something we cannot read" and "the
    auditor never ran" are different failures and must not share a shape.
    """
    if row is None:
        return AuditHeartbeat(stale_after_hours=stale_after_hours)
    state = _coerce_state(row.get("state"))
    updated_at = row.get("updated_at")
    ran_raw = state.get("ran_at")
    ran_at: Optional[datetime] = None
    if isinstance(ran_raw, str):
        try:
            ran_at = datetime.fromisoformat(ran_raw)
        except ValueError:
            ran_at = None
    anchor = ran_at or updated_at
    age_hours: Optional[float] = None
    if isinstance(anchor, datetime):
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        age_hours = (now - anchor).total_seconds() / 3600.0
    verdicts = state.get("verdicts")
    return AuditHeartbeat(
        present=True,
        ran_at=ran_at,
        updated_at=updated_at,
        last_alert_at=row.get("fired_at"),
        age_hours=age_hours,
        stale=bool(age_hours is not None and age_hours > stale_after_hours),
        stale_after_hours=stale_after_hours,
        healthy=bool(state.get("healthy")),
        degraded=bool(state.get("degraded")),
        degraded_reason=str(state.get("degraded_reason") or ""),
        heads_sampled=[str(h) for h in (state.get("heads_sampled") or [])],
        claims_extracted=state.get("claims_extracted"),
        claims_checked=state.get("claims_checked"),
        verdicts={str(k): int(v) for k, v in (verdicts or {}).items()}
        if isinstance(verdicts, dict) else {},
        pipeline_version=str(state.get("pipeline_version") or ""),
    )


def build_payload(
    heartbeat_row: Any,
    verdict_rows: list[dict[str, Any]],
    *,
    window_days: int,
    generated_at: datetime,
    stale_after_hours: float = DEFAULT_STALE_AFTER_HOURS,
) -> ExternalAuditOut:
    """Assemble the response. Pure — every DB read happens in the route."""
    by_verdict = {v: 0 for v in VERDICTS}
    desks: set[str] = set()
    versions: set[str] = set()
    contradictions: list[AuditContradiction] = []
    for r in verdict_rows:
        verdict = str(r.get("verdict") or "").strip().upper()
        if verdict in by_verdict:
            by_verdict[verdict] += 1
        desk = str(r.get("desk_key") or "")
        if desk:
            desks.add(desk)
        version = str(r.get("pipeline_version") or "")
        if version:
            versions.add(version)
        if verdict == "CONTRADICTED" and len(contradictions) < _CONTRADICTION_SAMPLE_CAP:
            contradictions.append(
                AuditContradiction(
                    id=str(r.get("id")),
                    produced_at=r["produced_at"],
                    desk_key=desk,
                    audited_analyst_id=str(r.get("audited_analyst_id") or ""),
                    audited_severity=r.get("audited_severity"),
                    claim=str(r.get("claim") or ""),
                    rationale=str(r.get("rationale") or ""),
                    source_urls=_coerce_urls(r.get("source_urls")),
                )
            )

    n = sum(by_verdict.values())
    checked = sum(v for k, v in by_verdict.items() if k in CHECKED_VERDICTS)
    return ExternalAuditOut(
        generated_at=generated_at,
        window_days=window_days,
        measured=True,
        heartbeat=build_heartbeat(
            heartbeat_row, now=generated_at, stale_after_hours=stale_after_hours,
        ),
        by_verdict=by_verdict,
        n=n,
        checked=checked,
        # Over CHECKED, never over n: an UNCHECKED claim was never audited, and
        # letting it into the denominator would make a degraded search plane
        # read as a clean bill of health.
        contradiction_rate=(
            by_verdict["CONTRADICTED"] / checked if checked else None
        ),
        desks_audited=len(desks),
        pipeline_versions=sorted(versions),
        contradictions=contradictions,
    )


# ---------------------------------------------------------------------------
# Router
# ---------------------------------------------------------------------------


def build_external_audit_router(deps: RegistryAPIDeps) -> APIRouter:
    router = APIRouter(tags=["system"])

    def _get_deps(request: Request) -> RegistryAPIDeps:
        return getattr(request.app.state, "registry_deps", deps)

    @router.get(_ROUTE, response_model=ExternalAuditOut)
    async def system_external_audit(
        days: int = Query(
            default=DEFAULT_WINDOW_DAYS, ge=1, le=MAX_WINDOW_DAYS,
            description="Trailing window in days over the audit verdicts.",
        ),
        _principal: str = Depends(require_bearer),
        deps_: RegistryAPIDeps = Depends(_get_deps),
    ) -> ExternalAuditOut:
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(days=days)
        try:
            async with deps_.descriptor_registry.pg.acquire() as conn:
                hb = await conn.fetchrow(
                    _HEARTBEAT_SQL, _TRIGGER_CLASS, _HEARTBEAT_KEY
                )
                rows = await conn.fetch(
                    _VERDICTS_SQL, _TITLE_PREFIX, cutoff, _VERDICT_FETCH_CAP
                )
        except Exception as exc:  # noqa: BLE001 — a polled panel must not 500
            logger.info("v3.system.external_audit.unavailable err=%s", exc)
            return _blank(days, now)
        return build_payload(
            dict(hb) if hb is not None else None,
            [dict(r) for r in rows],
            window_days=days,
            generated_at=now,
        )

    return router


__all__ = [
    "CHECKED_VERDICTS",
    "DEFAULT_STALE_AFTER_HOURS",
    "DEFAULT_WINDOW_DAYS",
    "MAX_WINDOW_DAYS",
    "VERDICTS",
    "AuditContradiction",
    "AuditHeartbeat",
    "ExternalAuditOut",
    "build_external_audit_router",
    "build_heartbeat",
    "build_payload",
]

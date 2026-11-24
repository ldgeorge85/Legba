# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``ingestion_intake`` pack — an agent asks for something to be ingested.

Producer (b) of the intake. Two tools:

* ``request_ingestion(address | source_id + window_start, pipeline?, sink?,
  note?, priority?)`` — submits ONE request (reason ``agent_request``, budget
  class ``agent``) and returns its id and status — ``queued``, ``done``
  (satisfied from a stored copy, nothing fetched) or ``refused`` with the
  reason. Admission (:mod:`legba.data.intake_store`) decides; the tool adds
  nothing of its own but the requester identity.
* ``ingestion_request_status(id)`` — one of the CALLER's own requests: state,
  refusal, result, the records the run wrote.

WHO ASKED: ``requested_by`` is ``analyst::<calling descriptor id>``, read off
the per-run provenance identity (``ctx.writeback.analyst_ctx``), never an
argument — an agent cannot submit as another. ``on_behalf_of`` stays NULL:
no run context carries the person a consult turn acts for today (the
planned identity design will supply real principals; the column is there).
The status tool is scoped the same way.

THE SWITCH: ``LEGBA_INTAKE_AGENT_REQUESTS`` (off by default). Off, both tools
answer ``failed`` naming the switch — and no analyst is granted the pack
until an operator adds the grant, so the roll changes nothing.

FOUR-SURFACE convergence (tools must be pack tools): this tuple ==
``descriptors/action_pack_ingestion_intake.yaml`` ``tools`` ==
:func:`register_intake_tools` == every tool a bound analyst can dispatch
through this pack.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from ...intake_contract import DEFAULT_PAGE_PIPELINE
from ...intake_store import (
    IntakeRequestInvalid,
    SubmitRequest,
    get_request,
    submit_request,
)
from ...schemas.action_pack import ActionPack
from .tools import ToolCall, ToolContext, ToolResult

logger = logging.getLogger(__name__)

INTAKE_PACK_ID = "ingestion_intake"
INTAKE_TOOLS = ("request_ingestion", "ingestion_request_status")
AGENT_REQUESTS_ENV = "LEGBA_INTAKE_AGENT_REQUESTS"
DEFAULT_PIPELINE_ENV = "LEGBA_INTAKE_DEFAULT_PIPELINE"

__all__ = [
    "AGENT_REQUESTS_ENV",
    "INTAKE_PACK_ID",
    "INTAKE_TOOLS",
    "ingestion_request_status_tool",
    "register_intake_tools",
    "request_ingestion_tool",
]


def _enabled() -> bool:
    return os.environ.get(AGENT_REQUESTS_ENV, "").strip().lower() in {"1", "on", "true", "yes"}


def _off() -> ToolResult:
    return ToolResult(status="failed", error=f"intake_agent_requests_off: {AGENT_REQUESTS_ENV} is off")


def _caller(ctx: ToolContext) -> tuple[Any, str] | None:
    wb = ctx.writeback
    if wb is None or wb.pg_pool is None or wb.analyst_ctx is None:
        return None
    return wb.pg_pool, f"analyst::{wb.analyst_ctx.analyst_id}"


def _tool_config(pack: ActionPack, name: str) -> dict[str, Any]:
    for spec in getattr(pack, "tools", None) or []:
        if getattr(spec, "name", None) == name:
            return dict(getattr(spec, "config", None) or {})
    return {}


async def request_ingestion_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    if not _enabled():
        return _off()
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for request_ingestion")
    pool, requested_by = caller
    args = call.args or {}
    address = str(args.get("address") or "").strip() or None
    source_id = str(args.get("source_id") or "").strip() or None
    if (address is None) == (source_id is None):
        return ToolResult(status="failed", error="give exactly one of address or source_id")
    pipeline = (str(args.get("pipeline") or "").strip()
                or str(_tool_config(pack, "request_ingestion").get("default_pipeline") or "").strip()
                or os.environ.get(DEFAULT_PIPELINE_ENV, "").strip()
                # I-2: an address with no pipeline named anywhere runs under
                # the instance's default page pipeline (the ``page`` kind).
                or (DEFAULT_PAGE_PIPELINE if address else None))
    window_start = None
    if source_id is not None:
        raw = str(args.get("window_start") or "").strip()
        try:
            window_start = datetime.fromisoformat(raw.replace("Z", "+00:00")) if raw else None
        except ValueError:
            return ToolResult(status="failed", error=f"window_start is not ISO-8601: {raw!r}")
        if window_start is not None and window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
    try:
        priority = int(args.get("priority", 50))
    except (TypeError, ValueError):
        priority = 50
    try:
        res = await submit_request(pool, SubmitRequest(
            target_kind="address" if address else "source_window",
            address=address, source_id=source_id, window_start=window_start,
            pipeline_source_id=pipeline if address else (pipeline or source_id),
            sink=(str(args["sink"]).strip() if args.get("sink") else None),
            reason="agent_request", budget_class="agent",
            requested_by=requested_by, on_behalf_of=None,
            note=(str(args.get("note") or "")[:2000] or None),
            priority=max(0, min(100, priority)),
        ))
    except IntakeRequestInvalid as exc:
        return ToolResult(status="failed", error=f"invalid_request: {exc}")
    except Exception as exc:  # noqa: BLE001 — a DB failure folds into the loop
        logger.warning("intake_tools.request_failed requested_by=%s err=%s", requested_by, exc)
        return ToolResult(status="failed", error=f"request_ingestion_failed: {exc!s}")
    out = {
        "id": str(res.id), "status": res.state, "refusal_reason": res.refusal_reason,
        "satisfied_from": res.satisfied_from,
    }
    if res.duplicate_of is not None:
        out["duplicate_of"] = str(res.duplicate_of)
    return ToolResult(status="completed", output=out, units=1)


async def ingestion_request_status_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    if not _enabled():
        return _off()
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for ingestion_request_status")
    pool, requested_by = caller
    try:
        rid = UUID(str((call.args or {}).get("id")))
    except (TypeError, ValueError):
        return ToolResult(status="failed", error="id is not a valid UUID")
    row = await get_request(pool, rid, requested_by=requested_by)
    if row is None:
        return ToolResult(status="failed", error=f"no request {rid} of yours")
    keep = ("target_kind", "address", "source_id", "pipeline_source_id", "sink", "reason",
            "state", "refusal_reason", "satisfied_from", "attempts", "last_error", "result",
            "result_version_id", "records")
    out = {k: row.get(k) for k in keep}
    out["id"] = str(rid)
    for k in ("created_at", "finished_at", "not_before"):
        v = row.get(k)
        out[k] = v.isoformat() if isinstance(v, datetime) else None
    return ToolResult(status="completed", output=out, units=1)


def register_intake_tools(registry: Any) -> None:
    registry.register("request_ingestion", request_ingestion_tool)
    registry.register("ingestion_request_status", ingestion_request_status_tool)

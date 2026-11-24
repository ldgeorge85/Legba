# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``evidence_drift`` pack — agents see what a source changed after it was
read, and ask for a re-read or a re-derivation (lane D-1).

Five tools:

* ``evidence_versions(record_id | address)`` — every version of the page
  (dates, sizes, fetch counts) and the classification between consecutive
  ones (the stored one, or — for a pair with no drift row because the text
  did not change — computed on the spot from the stored text).
* ``evidence_diff(record_id | address, earlier_version_id?, later_version_id?)``
  — both texts' changed passages side by side for one pair (default: the
  newest classified change), bounded (:data:`DIFF_MAX_CHARS`), with the class,
  the rule and the numbers, dates, names and negations that changed.
* ``request_evidence_reread(address, note?)`` — submits ONE intake request to
  re-read the address and keep only the fetched version (sink ``receipt``,
  reason ``agent_request``, budget class ``agent``); the drift row and its
  classification follow when the text changed. Refuses unless
  ``LEGBA_INTAKE_AGENT_REQUESTS`` is on (the intake's own agent switch).
* ``request_rederive(flag_id, note?)`` — records "this dependant should be
  re-derived with the changed text" (``evidence_rederive_requests``) for the
  statement layer. Nothing is re-run.
* ``review_drift_flag(flag_id, outcome, note)`` — moves a flag: ``reviewed``,
  or resolved as ``unchanged_in_substance`` / ``dependant_corrected`` /
  ``dependant_withdrawn``. The principal is the calling analyst. (D-2: a
  corrected / withdrawn resolution also records a re-derive request per
  output derived from the dependant.)

D-2 adds three, and the assessment to the first two:

* ``evidence_drift_assessment(drift_id | flag_id | record_id | address)`` —
  what KIND of change it was (type, acknowledged, scope, timing, summary,
  history_rewrite) and each flag's claim effect;
* ``drift_flags(state?, closed_by?, would_auto_close?, source_id?, limit?)``
  — flags in review order; ``closed_by=system`` lists the drift assessor's
  tracked auto-closes, ``would_auto_close=true`` the shadow count;
* ``reopen_drift_flag(flag_id, note)`` — re-opens an auto-closed flag as a new
  open flag (``reopened_from``).

WHO ASKED: ``analyst::<calling descriptor id>``, from the run's provenance
identity, never an argument.

FOUR-SURFACE convergence (tools must be pack tools): :data:`EVIDENCE_DRIFT_TOOLS`
== ``descriptors/action_pack_evidence_drift.yaml`` ``tools`` ==
:func:`register_evidence_drift_tools` == every tool a bound analyst can
dispatch through this pack. No analyst is granted the pack until an operator
adds the grant.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from ...schemas.action_pack import ActionPack
from .tools import ToolCall, ToolContext, ToolResult

logger = logging.getLogger(__name__)

EVIDENCE_DRIFT_PACK_ID = "evidence_drift"
EVIDENCE_DRIFT_TOOLS = (
    "evidence_versions", "evidence_diff", "request_evidence_reread",
    "request_rederive", "review_drift_flag",
    "evidence_drift_assessment", "drift_flags", "reopen_drift_flag",
)
#: The assessment fields carried on a transition / a diff.
_ASSESSMENT_FIELDS = ("assessment_id", "change_type", "component_types", "acknowledged", "summary",
                      "history_rewrite", "late_unexplained_change", "assessor", "timing_bucket")
#: Characters of changed passages returned by evidence_diff (both sides).
DIFF_MAX_CHARS = 6000
_REVIEW_OUTCOMES = ("reviewed", "unchanged_in_substance", "dependant_corrected", "dependant_withdrawn")

__all__ = [
    "DIFF_MAX_CHARS",
    "EVIDENCE_DRIFT_PACK_ID",
    "EVIDENCE_DRIFT_TOOLS",
    "register_evidence_drift_tools",
]

_RECORD_VERSION_SQL = """
SELECT v.address FROM fetch_version_records r JOIN fetch_versions v ON v.id = r.version_id
 WHERE r.record_id = $1 AND v.role IN ('page', 'archiver') LIMIT 1
"""
_RECORD_ARCHIVE_SQL = """
SELECT v.address FROM evidence_archive ea
  JOIN fetch_versions v ON v.sha256 = ea.sha256 AND v.receipt_kind = 'fetch'
 WHERE ea.signal_id = $1 LIMIT 1
"""


def _caller(ctx: ToolContext) -> tuple[Any, str] | None:
    wb = ctx.writeback
    if wb is None or wb.pg_pool is None or wb.analyst_ctx is None:
        return None
    return wb.pg_pool, f"analyst::{wb.analyst_ctx.analyst_id}"


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


async def _address_for(conn: Any, args: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    """(canonical address, error) from ``address`` or ``record_id``."""
    from ..._url_canon import canonical_url  # noqa: PLC0415

    address = str(args.get("address") or "").strip() or None
    record = str(args.get("record_id") or "").strip() or None
    if (address is None) == (record is None):
        return None, "give exactly one of address or record_id"
    if address is not None:
        return canonical_url(address) or address, None
    try:
        rid = UUID(record)
    except ValueError:
        return None, "record_id is not a valid UUID"
    found = await conn.fetchval(_RECORD_VERSION_SQL, rid) or await conn.fetchval(_RECORD_ARCHIVE_SQL, rid)
    if found is None:
        return None, f"no fetched page version is linked to record {record}"
    return str(found), None


async def evidence_versions_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for evidence_versions")
    pool, _who = caller
    from ...evidence_drift_classify import classify_pair  # noqa: PLC0415
    from ...evidence_drift_reads import version_history  # noqa: PLC0415

    async with pool.acquire() as conn:
        address, err = await _address_for(conn, call.args or {})
        if err:
            return ToolResult(status="failed", error=err)
        hist = await version_history(conn, address)
        if hist is None:
            return ToolResult(status="failed", error="evidence drift tables are not migrated (0252)")
        texts = {}
        if len(hist["versions"]) > 1:
            ids = [int(v["id"]) for v in hist["versions"]]
            texts = {int(r["id"]): r["extracted_text"] for r in await conn.fetch(
                "SELECT id, extracted_text FROM fetch_versions WHERE id = ANY($1::bigint[])", ids)}
    by_pair = {(c["earlier_version_id"], c["later_version_id"]): c for c in hist["changes"]}
    transitions = []
    vers = hist["versions"]
    for a, b in zip(vers, vers[1:]):
        c = by_pair.get((a["id"], b["id"]))
        if c is not None:
            transitions.append({"from": a["id"], "to": b["id"], "class": c["drift_class"],
                                "rule": c["rule"], "at": c["later_fetched_at"], "recorded": True,
                                "assessment": _assessment_of(c)})
        else:
            cls = classify_pair(texts.get(int(a["id"])), texts.get(int(b["id"])))
            transitions.append({"from": a["id"], "to": b["id"], "class": cls.drift_class,
                                "rule": cls.rule, "at": b["first_seen_at"], "recorded": False})
    gone = [c for c in hist["changes"] if c.get("later_gone")]
    out = {
        "address": address,
        "versions": [{k: v.get(k) for k in ("id", "first_seen_at", "last_seen_at", "fetch_count",
                                             "text_chars", "size_bytes", "status", "role")} for v in vers],
        "transitions": transitions,
        "gone": [{"from": c["earlier_version_id"], "class": c["drift_class"], "at": c["later_fetched_at"],
                  "status": c.get("later_status")} for c in gone],
        "unclassified_drift_rows": hist["unclassified_drift_rows"],
    }
    return ToolResult(status="completed", output=out, units=1)


def _assessment_of(row: dict[str, Any]) -> Optional[dict[str, Any]]:
    """The assessment fields of a change row, or None when it has none."""
    if not row.get("change_type"):
        return None
    return {k.replace("assessment_id", "id"): row.get(k) for k in _ASSESSMENT_FIELDS}


def _bound_pairs(items: list[dict[str, Any]], budget: int) -> tuple[list[dict[str, Any]], bool]:
    kept, used = [], 0
    for it in items:
        size = len(str(it.get("earlier") or "")) + len(str(it.get("later") or ""))
        if used + size > budget:
            return kept, True
        kept.append(it)
        used += size
    return kept, False


async def evidence_diff_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for evidence_diff")
    pool, _who = caller
    from ...evidence_drift_classify import classify_pair  # noqa: PLC0415

    args = call.args or {}
    async with pool.acquire() as conn:
        address, err = await _address_for(conn, args)
        if err:
            return ToolResult(status="failed", error=err)
        e_id, l_id = args.get("earlier_version_id"), args.get("later_version_id")
        if e_id is None or l_id is None:
            row = await conn.fetchrow(
                "SELECT earlier_version_id, later_version_id FROM evidence_drift_classifications "
                "WHERE address = $1 AND later_version_id IS NOT NULL "
                "ORDER BY later_fetched_at DESC, id DESC LIMIT 1", address)
            if row is None:
                return ToolResult(status="completed", units=1, output={
                    "address": address, "changed": False,
                    "note": "no classified text change recorded for this address"})
            e_id, l_id = row["earlier_version_id"], row["later_version_id"]
        try:
            e_id, l_id = int(e_id), int(l_id)
        except (TypeError, ValueError):
            return ToolResult(status="failed", error="version ids must be integers")
        rows = {int(r["id"]): r for r in await conn.fetch(
            "SELECT id, address, extracted_text, first_seen_at, last_seen_at FROM fetch_versions "
            "WHERE id = ANY($1::bigint[])", [e_id, l_id])}
        assessment = await _pair_assessment(conn, e_id, l_id)
    if e_id not in rows or l_id not in rows or rows[e_id]["address"] != address \
            or rows[l_id]["address"] != address:
        return ToolResult(status="failed", error="both versions must exist and belong to the address")
    cls = classify_pair(rows[e_id]["extracted_text"], rows[l_id]["extracted_text"])
    ev = cls.evidence()
    removed = [{"earlier": s, "later": None} for s in ev.get("removed", [])]
    added = [{"earlier": None, "later": s} for s in ev.get("added", [])]
    side_by_side, truncated = _bound_pairs(list(ev.get("changed", [])) + removed + added, DIFF_MAX_CHARS)
    out = {
        "address": address, "earlier_version_id": e_id, "later_version_id": l_id,
        "earlier_seen": _iso(rows[e_id]["first_seen_at"]), "later_seen": _iso(rows[l_id]["first_seen_at"]),
        "class": cls.drift_class, "rule": cls.rule, "counts": ev.get("counts", {}),
        "critical": ev.get("critical", {}), "passages": side_by_side, "truncated": truncated,
        "assessment": assessment,
    }
    return ToolResult(status="completed", output=out, units=1)


async def _pair_assessment(conn: Any, e_id: int, l_id: int) -> Optional[dict[str, Any]]:
    """The latest assessment of the classified change between two versions
    (None when there is none, or 0253 is not applied)."""
    from ...evidence_drift_reads import LATEST_ASSESSMENT_SQL  # noqa: PLC0415

    try:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT a.* FROM evidence_drift_classifications c "
                "JOIN LATERAL (" + LATEST_ASSESSMENT_SQL + ") a ON true "
                "WHERE c.earlier_version_id = $1 AND c.later_version_id = $2 "
                "ORDER BY c.id DESC LIMIT 1", e_id, l_id)
    except Exception:  # noqa: BLE001 — no assessment layer yet
        return None
    if row is None:
        return None
    d = dict(row)
    d["component_types"] = list(d.get("component_types") or [])
    return {k: d.get(k) for k in ("id", "change_type", "component_types", "acknowledged", "summary",
                                  "history_rewrite", "late_unexplained_change", "model_type_doubt", "assessor", "assessor_stamp", "timing_bucket",
                                  "interval_hours", "claims_changed", "share_changed")}


async def request_evidence_reread_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    from .intake_tools import AGENT_REQUESTS_ENV  # noqa: PLC0415

    if os.environ.get(AGENT_REQUESTS_ENV, "").strip().lower() not in {"1", "on", "true", "yes"}:
        return ToolResult(status="failed", error=f"intake_agent_requests_off: {AGENT_REQUESTS_ENV} is off")
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for request_evidence_reread")
    pool, who = caller
    from ...intake_store import IntakeRequestInvalid, SubmitRequest, submit_request  # noqa: PLC0415

    args = call.args or {}
    address = str(args.get("address") or "").strip()
    if not address:
        return ToolResult(status="failed", error="address is required")
    cfg = {}
    for spec in getattr(pack, "tools", None) or []:
        if getattr(spec, "name", None) == "request_evidence_reread":
            cfg = dict(getattr(spec, "config", None) or {})
    from ...intake_contract import DEFAULT_PAGE_PIPELINE  # noqa: PLC0415

    pipeline = (str(cfg.get("pipeline") or "").strip()
                or os.environ.get("LEGBA_INTAKE_DRIFT_PIPELINE", "").strip() or DEFAULT_PAGE_PIPELINE)
    if pipeline.lower() in ("none", "off"):
        return ToolResult(status="failed", error="re-reads are off (LEGBA_INTAKE_DRIFT_PIPELINE=none)")
    try:
        res = await submit_request(pool, SubmitRequest(
            target_kind="address", address=address, pipeline_source_id=pipeline, sink="receipt",
            reason="agent_request", budget_class="agent", requested_by=who,
            note=(str(args.get("note") or "")[:2000] or "re-read for source drift"), priority=40,
        ))
    except IntakeRequestInvalid as exc:
        return ToolResult(status="failed", error=f"invalid_request: {exc}")
    except Exception as exc:  # noqa: BLE001
        logger.warning("evidence_drift_tools.reread_failed requested_by=%s err=%s", who, exc)
        return ToolResult(status="failed", error=f"request_evidence_reread_failed: {exc!s}")
    return ToolResult(status="completed", units=1, output={
        "id": str(res.id), "status": res.state, "refusal_reason": res.refusal_reason,
        "satisfied_from": res.satisfied_from})


def _flag_id(args: dict[str, Any]) -> Optional[int]:
    try:
        return int(args.get("flag_id"))
    except (TypeError, ValueError):
        return None


async def request_rederive_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for request_rederive")
    pool, who = caller
    from ...evidence_drift_store import request_rederive  # noqa: PLC0415

    fid = _flag_id(call.args or {})
    if fid is None:
        return ToolResult(status="failed", error="flag_id must be an integer")
    out = await request_rederive(pool, fid, requested_by=who, note=(call.args or {}).get("note"))
    if out["status"] == "unknown_flag":
        return ToolResult(status="failed", error=f"no drift flag {fid}")
    return ToolResult(status="completed", output=out, units=1)


async def review_drift_flag_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for review_drift_flag")
    pool, who = caller
    from ...evidence_drift_store import resolve_flag, review_flag  # noqa: PLC0415

    args = call.args or {}
    fid = _flag_id(args)
    outcome = str(args.get("outcome") or "").strip()
    note = str(args.get("note") or "").strip()
    if fid is None:
        return ToolResult(status="failed", error="flag_id must be an integer")
    if outcome not in _REVIEW_OUTCOMES:
        return ToolResult(status="failed", error=f"outcome must be one of {_REVIEW_OUTCOMES}")
    if not note:
        return ToolResult(status="failed", error="a note saying why is required")
    if outcome == "reviewed":
        state = await review_flag(pool, fid, principal=who, note=note)
    else:
        state = await resolve_flag(pool, fid, principal=who, resolution=outcome, note=note)
    if state is None:
        return ToolResult(status="failed", error=f"flag {fid} is unknown or already past {outcome}")
    return ToolResult(status="completed", output={"flag_id": fid, "state": state, "outcome": outcome}, units=1)


async def evidence_drift_assessment_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for evidence_drift_assessment")
    pool, _who = caller
    from ...evidence_drift_reads import assessments_for  # noqa: PLC0415

    args = call.args or {}
    drift_id, flag_id = args.get("drift_id"), args.get("flag_id")
    keyed = [k for k in ("drift_id", "flag_id", "record_id", "address") if args.get(k) not in (None, "")]
    if len(keyed) != 1:
        return ToolResult(status="failed", error="give exactly one of drift_id, flag_id, record_id, address")
    try:
        drift_id = None if drift_id in (None, "") else int(drift_id)
        flag_id = None if flag_id in (None, "") else int(flag_id)
    except (TypeError, ValueError):
        return ToolResult(status="failed", error="drift_id / flag_id must be integers")
    async with pool.acquire() as conn:
        address = None
        if keyed[0] in ("record_id", "address"):
            address, err = await _address_for(conn, args)
            if err:
                return ToolResult(status="failed", error=err)
        body = await assessments_for(conn, drift_id=drift_id, flag_id=flag_id, address=address,
                                     limit=int(args.get("limit") or 10))
    if body is None:
        return ToolResult(status="failed", error="evidence drift assessment tables are not migrated (0253)")
    for a in body["assessments"]:
        for k in ("model_output", "observations"):
            a.pop(k, None)
    return ToolResult(status="completed", output={**body, "count": len(body["assessments"])}, units=1)


async def drift_flags_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for drift_flags")
    pool, _who = caller
    from ...evidence_drift_reads import CLOSED_BY, open_flags  # noqa: PLC0415

    args = call.args or {}
    state = str(args.get("state") or "open")
    closed_by = str(args.get("closed_by") or "any")
    if state not in ("open", "reviewed", "unresolved", "resolved"):
        return ToolResult(status="failed", error="state must be open, reviewed, unresolved or resolved")
    if closed_by not in CLOSED_BY:
        return ToolResult(status="failed", error=f"closed_by must be one of {CLOSED_BY}")
    try:
        limit = max(1, min(int(args.get("limit") or 25), 100))
    except (TypeError, ValueError):
        return ToolResult(status="failed", error="limit must be an integer")
    async with pool.acquire() as conn:
        rows = await open_flags(conn, limit=limit, state=state, closed_by=closed_by,
                                would_auto_close=bool(args.get("would_auto_close")),
                                source_id=(str(args["source_id"]) if args.get("source_id") else None))
    if rows is None:
        return ToolResult(status="failed", error="evidence drift tables are not migrated (0252/0253)")
    keep = ("id", "address", "source_id", "drift_class", "dependant_kind", "dependant_id", "depth", "state",
            "resolution", "resolved_by", "reopened_from", "change_type", "component_types", "acknowledged",
            "summary", "history_rewrite", "late_unexplained_change", "model_type_doubt", "claim_effect",
            "claim_after_text", "auto_closed", "would_auto_close",
            "review_priority", "later_fetched_at")
    return ToolResult(status="completed", units=1, output={
        "state": state, "closed_by": closed_by, "count": len(rows),
        "would_auto_close_count": sum(1 for r in rows if r.get("would_auto_close")),
        "flags": [{k: r.get(k) for k in keep} for r in rows]})


async def reopen_drift_flag_tool(call: ToolCall, pack: ActionPack, ctx: ToolContext) -> ToolResult:
    caller = _caller(ctx)
    if caller is None:
        return ToolResult(status="failed", error="no writeback surface wired for reopen_drift_flag")
    pool, who = caller
    from ...evidence_drift_assess_store import reopen_flag  # noqa: PLC0415

    args = call.args or {}
    fid = _flag_id(args)
    note = str(args.get("note") or "").strip()
    if fid is None:
        return ToolResult(status="failed", error="flag_id must be an integer")
    if not note:
        return ToolResult(status="failed", error="a note saying why is required")
    out = await reopen_flag(pool, fid, principal=who, note=note)
    if out["status"] != "reopened":
        return ToolResult(status="failed", error=f"flag {fid}: {out['status']}")
    return ToolResult(status="completed", output=out, units=1)


def register_evidence_drift_tools(registry: Any) -> None:
    registry.register("evidence_versions", evidence_versions_tool)
    registry.register("evidence_diff", evidence_diff_tool)
    registry.register("request_evidence_reread", request_evidence_reread_tool)
    registry.register("request_rederive", request_rederive_tool)
    registry.register("review_drift_flag", review_drift_flag_tool)
    registry.register("evidence_drift_assessment", evidence_drift_assessment_tool)
    registry.register("drift_flags", drift_flags_tool)
    registry.register("reopen_drift_flag", reopen_drift_flag_tool)

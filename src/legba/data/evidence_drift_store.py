# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source drift — classify the drift rows, flag the dependants, and the flag
lifecycle.

THE PASS (:func:`classify_pending`). Every ``fetch_drift_observations`` row
without a classification, oldest first, bounded per pass:

1. load both versions' stored extracted text (and, best-effort, each
   version's ``<title>`` from its stored bytes);
2. classify (:func:`legba.data.evidence_drift_classify.classify_pair`);
3. for a flagging class (substantive_edit, title_changed,
   retracted_or_replaced, gone) walk what relied on the earlier version
   (:func:`legba.data.evidence_drift_walk.walk_dependants`, bounded);
4. in ONE transaction: the classification row (insert-only) and one flag per
   dependant (``ON CONFLICT DO NOTHING`` on (drift row, dependant) — a re-run
   writes nothing twice). Each flag carries the cited span, when the
   dependant's citation has one, and the exact folded check of whether that
   span is in the earlier text and still in the later one.

A cosmetic class writes no flag; it is counted on its classification row.
Nothing here touches a dependant.

THE LIFECYCLE (:func:`review_flag`, :func:`resolve_flag`): open -> reviewed
(principal + note) -> resolved (``unchanged_in_substance`` |
``dependant_corrected`` | ``dependant_withdrawn``, principal + note). A flag
may be resolved straight from open (the review fields are filled with the
same principal). The table's trigger refuses anything else. (D-2: a flag
resolved ``dependant_corrected`` / ``dependant_withdrawn`` also records a
re-derive request per output derived from its dependant; an auto-closed flag
is re-opened as a NEW flag row, ``reopened_from``.)

RE-DERIVE (:func:`request_rederive`): records "this dependant should be
re-derived with the changed text" for the statement layer. Nothing re-runs.
"""
from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import urlsplit

from .evidence_drift_classify import (
    FLAGGING_CLASSES,
    check_cited_span,
    classify_pair,
    extract_title,
)
from .evidence_drift_walk import _DERIVED_SQL, DEFAULT_MAX_DEPENDANTS, DEFAULT_MAX_DEPTH, walk_dependants

logger = logging.getLogger(__name__)

__all__ = [
    "PassReport",
    "RESOLUTIONS",
    "classify_pending",
    "classify_drift_row",
    "rederive_derived_outputs",
    "request_rederive",
    "resolve_flag",
    "resolve_flag_conn",
    "review_flag",
]

RESOLUTIONS = ("unchanged_in_substance", "dependant_corrected", "dependant_withdrawn")
DEFAULT_PASS_LIMIT = 50

_PENDING_SQL = """
SELECT d.id, d.address, d.source_id, d.earlier_version_id, d.later_version_id,
       d.earlier_fetched_at, d.later_fetched_at, d.later_status, d.later_gone
  FROM fetch_drift_observations d
 WHERE NOT EXISTS (SELECT 1 FROM evidence_drift_classifications c WHERE c.drift_id = d.id)
 ORDER BY d.id
 LIMIT $1
"""

_VERSION_SQL = """
SELECT id, url, sha256, extracted_text, object_ref, bytes_pruned_at
  FROM fetch_versions WHERE id = ANY($1::bigint[])
"""

_INSERT_CLASS_SQL = """
INSERT INTO evidence_drift_classifications (
    drift_id, address, source_id, host, earlier_version_id, later_version_id,
    earlier_fetched_at, later_fetched_at, drift_class, rule, classifier_version,
    evidence, dependants_found, flags_written, inputs_only, walk_depth, walk_truncated
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb, $13, $14, $15, $16, $17)
ON CONFLICT (drift_id) DO NOTHING
RETURNING id
"""

_INSERT_FLAG_SQL = """
INSERT INTO evidence_drift_flags (
    classification_id, drift_id, address, source_id, earlier_version_id, later_version_id,
    drift_class, dependant_kind, dependant_id, depth, via_kind, via_id, record_version_id,
    cited_span, span_check, span_in_earlier, span_in_later
) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15::jsonb, $16, $17)
ON CONFLICT (drift_id, dependant_kind, dependant_id) WHERE reopened_from IS NULL DO NOTHING
RETURNING id
"""


@dataclass
class PassReport:
    """One pass's counts, and the per-source tallies the alert reads."""

    classified: int = 0
    flags_written: int = 0
    by_class: dict[str, int] = field(default_factory=dict)
    #: subject (source id, else ``host:<host>``) -> class -> count
    by_subject: dict[str, dict[str, int]] = field(default_factory=dict)
    #: subject -> flags written
    flags_by_subject: dict[str, int] = field(default_factory=dict)
    errors: int = 0


def host_of(address: str) -> str:
    try:
        return (urlsplit(address).hostname or "").lower()
    except ValueError:
        return ""


def drift_subject(source_id: Optional[str], address: str) -> str:
    """The alert subject for a drift: the source when known, else the host."""
    return source_id or f"host:{host_of(address) or 'unknown'}"


async def _title_of(conn: Any, row: Any) -> Optional[str]:
    """The version's ``<title>`` from its stored bytes, best-effort."""
    if row is None or row["object_ref"] is None or row["bytes_pruned_at"] is not None:
        return None
    try:
        from .archive import archive_root  # noqa: PLC0415
        from .archive_store import EvidenceStore  # noqa: PLC0415
        from .archive_tier import read_original  # noqa: PLC0415

        store = EvidenceStore.from_env(archive_root())
        body = await read_original(conn, store, str(row["sha256"]))
    except Exception as exc:  # noqa: BLE001 — a title is a bonus
        logger.debug("evidence_drift.title_unreadable version=%s err=%s", row["id"], exc)
        return None
    return extract_title(body)


async def classify_drift_row(
    conn: Any, drift: Any, *, max_depth: int = DEFAULT_MAX_DEPTH,
    max_dependants: int = DEFAULT_MAX_DEPENDANTS, read_titles: bool = True, write: bool = True,
) -> dict[str, Any]:
    """Classify one drift row, walk and flag. ``write=False`` computes the same
    and writes nothing (the backfill's dry run). Returns a summary."""
    ids = [int(drift["earlier_version_id"])]
    if drift["later_version_id"] is not None:
        ids.append(int(drift["later_version_id"]))
    vers = {int(r["id"]): r for r in await conn.fetch(_VERSION_SQL, ids)}
    earlier = vers.get(int(drift["earlier_version_id"]))
    later = vers.get(int(drift["later_version_id"])) if drift["later_version_id"] is not None else None
    e_title = await _title_of(conn, earlier) if read_titles else None
    l_title = await _title_of(conn, later) if read_titles and later is not None else None
    cls = classify_pair(
        earlier["extracted_text"] if earlier is not None else None,
        later["extracted_text"] if later is not None else None,
        later_gone=bool(drift["later_gone"]), later_status=drift["later_status"],
        earlier_title=e_title, later_title=l_title,
    )
    walk = None
    if cls.flags_dependants:
        walk = await walk_dependants(conn, int(drift["earlier_version_id"]),
                                     max_depth=max_depth, max_dependants=max_dependants)
    later_text = later["extracted_text"] if later is not None else None
    texts: dict[int, Optional[str]] = {k: v["extracted_text"] for k, v in vers.items()}
    flags: list[dict[str, Any]] = []
    for dep in (walk.dependants if walk else []):
        span_check = None
        if dep.cited_span:
            base_id = dep.record_version_id or int(drift["earlier_version_id"])
            if base_id not in texts:
                texts[base_id] = await conn.fetchval(
                    "SELECT extracted_text FROM fetch_versions WHERE id = $1", base_id)
            span_check = check_cited_span(dep.cited_span, texts[base_id], later_text)
        flags.append({"dep": dep, "span": span_check})
    summary = {
        "drift_id": int(drift["id"]), "address": drift["address"], "source_id": drift["source_id"],
        "class": cls.drift_class, "rule": cls.rule, "evidence": cls.evidence(),
        "dependants": [
            {"kind": f["dep"].kind, "id": f["dep"].id, "depth": f["dep"].depth,
             "span_in_later": None if f["span"] is None else f["span"].span_in_later}
            for f in flags
        ],
        "inputs_only": walk.inputs_only if walk else 0,
        "walk_truncated": bool(walk and walk.truncated), "flags_written": 0,
    }
    if not write:
        return summary
    async with conn.transaction():
        cid = await conn.fetchval(
            _INSERT_CLASS_SQL, int(drift["id"]), drift["address"], drift["source_id"],
            host_of(drift["address"]) or None, int(drift["earlier_version_id"]),
            drift["later_version_id"], drift["earlier_fetched_at"], drift["later_fetched_at"],
            cls.drift_class, cls.rule, cls.classifier_version, json.dumps(cls.evidence(), default=str),
            len(flags), len(flags), walk.inputs_only if walk else 0,
            walk.depth_reached if walk else 0, bool(walk and walk.truncated),
        )
        if cid is None:  # classified concurrently: nothing more to write
            return summary
        written = 0
        for f in flags:
            dep, sc = f["dep"], f["span"]
            fid = await conn.fetchval(
                _INSERT_FLAG_SQL, cid, int(drift["id"]), drift["address"], drift["source_id"],
                int(drift["earlier_version_id"]), drift["later_version_id"], cls.drift_class,
                dep.kind, dep.id, dep.depth, dep.via_kind, dep.via_id, dep.record_version_id,
                dep.cited_span, None if sc is None else json.dumps(sc.as_dict()),
                None if sc is None else sc.span_in_earlier, None if sc is None else sc.span_in_later,
            )
            written += fid is not None
    summary["flags_written"] = written
    return summary


async def classify_pending(
    pool: Any, *, limit: int = DEFAULT_PASS_LIMIT, max_depth: int = DEFAULT_MAX_DEPTH,
    max_dependants: int = DEFAULT_MAX_DEPENDANTS,
) -> PassReport:
    """One bounded pass over unclassified drift rows. Idempotent."""
    report = PassReport()
    async with pool.acquire() as conn:
        rows = await conn.fetch(_PENDING_SQL, int(limit))
        for drift in rows:
            try:
                s = await classify_drift_row(conn, drift, max_depth=max_depth,
                                             max_dependants=max_dependants)
            except Exception as exc:  # noqa: BLE001 — one bad row must not stop the pass
                report.errors += 1
                logger.warning("evidence_drift.classify_failed drift=%s err=%s", drift["id"], exc)
                continue
            report.classified += 1
            report.flags_written += s["flags_written"]
            report.by_class[s["class"]] = report.by_class.get(s["class"], 0) + 1
            subj = drift_subject(drift["source_id"], drift["address"])
            per = report.by_subject.setdefault(subj, {})
            per[s["class"]] = per.get(s["class"], 0) + 1
            report.flags_by_subject[subj] = report.flags_by_subject.get(subj, 0) + s["flags_written"]
    return report


# -- the flag lifecycle ------------------------------------------------------------

def _principal(principal: str) -> str:
    p = str(principal or "").strip()
    if not p or len(p) > 256:
        raise ValueError("a principal string of 1-256 characters is required")
    return p


def _note(note: Optional[str]) -> Optional[str]:
    return (str(note).strip()[:2000] or None) if note is not None else None


async def review_flag(pool: Any, flag_id: int, *, principal: str, note: Optional[str] = None,
                      now: Optional[datetime] = None) -> Optional[str]:
    """open -> reviewed. Returns the new state, or None when the flag is not
    open (unknown, already reviewed or resolved)."""
    now = now or datetime.now(timezone.utc)
    return await pool.fetchval(
        "UPDATE evidence_drift_flags SET state = 'reviewed', reviewed_at = $2, reviewed_by = $3, "
        "review_note = $4 WHERE id = $1 AND state = 'open' RETURNING state",
        int(flag_id), now, _principal(principal), _note(note),
    )


async def resolve_flag(pool: Any, flag_id: int, *, principal: str, resolution: str,
                       note: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
    """open|reviewed -> resolved. Returns the new state, or None when the flag
    is unknown or already resolved. A flag resolved ``dependant_corrected`` or
    ``dependant_withdrawn`` also records one pending re-derive request per
    output derived from its dependant (D-2, :func:`rederive_derived_outputs`),
    in the same transaction."""
    async with pool.acquire() as conn:
        async with conn.transaction():
            return await resolve_flag_conn(conn, flag_id, principal=principal, resolution=resolution,
                                           note=note, now=now)


async def resolve_flag_conn(conn: Any, flag_id: int, *, principal: str, resolution: str,
                            note: Optional[str] = None, now: Optional[datetime] = None) -> Optional[str]:
    """:func:`resolve_flag` on a held connection (the caller's transaction)."""
    if resolution not in RESOLUTIONS:
        raise ValueError(f"resolution must be one of {RESOLUTIONS}")
    now = now or datetime.now(timezone.utc)
    who = _principal(principal)
    state = await conn.fetchval(
        "UPDATE evidence_drift_flags SET state = 'resolved', resolved_at = $2, resolved_by = $3, "
        "resolution = $4, resolution_note = $5, "
        "reviewed_at = coalesce(reviewed_at, $2), reviewed_by = coalesce(reviewed_by, $3) "
        "WHERE id = $1 AND state <> 'resolved' RETURNING state",
        int(flag_id), now, who, resolution, _note(note),
    )
    if state == "resolved" and resolution in REDERIVE_RESOLUTIONS:
        n = await rederive_derived_outputs(conn, int(flag_id), requested_by=who)
        if n:
            logger.info("evidence_drift.rederive_requested flag=%s outputs=%d", flag_id, n)
    return state


#: Resolutions that mean the dependant itself changed: what was derived from
#: it should be re-derived too.
REDERIVE_RESOLUTIONS = ("dependant_corrected", "dependant_withdrawn")
#: Derived outputs recorded per resolved flag, at most.
REDERIVE_MAX_OUTPUTS = 200


async def rederive_derived_outputs(conn: Any, flag_id: int, *, requested_by: str) -> int:
    """One pending ``evidence_rederive_requests`` row per output derived from
    the flag's dependant (the depth-2 set: ``analyst_outputs`` whose
    ``derived_from`` holds it, and its ``output_consumption`` consumers).
    Idempotent (one pending per output and later version). Nothing re-runs.
    Returns how many were newly recorded."""
    flag = await conn.fetchrow(
        "SELECT id, dependant_kind, dependant_id, address, earlier_version_id, later_version_id, resolution "
        "FROM evidence_drift_flags WHERE id = $1", int(flag_id))
    if flag is None or flag["dependant_kind"] in ("signal", "observation"):
        return 0
    try:
        dep = uuid.UUID(str(flag["dependant_id"]))
    except ValueError:
        return 0
    written = 0
    for row in await conn.fetch(_DERIVED_SQL, [dep], REDERIVE_MAX_OUTPUTS):
        if str(row["id"]) == str(dep):
            continue
        rid = await conn.fetchval(
            "INSERT INTO evidence_rederive_requests (id, flag_id, dependant_kind, dependant_id, address, "
            "earlier_version_id, later_version_id, requested_by, note) "
            "VALUES ($1, $2, 'derived_output', $3, $4, $5, $6, $7, $8) ON CONFLICT DO NOTHING RETURNING id",
            uuid.uuid4(), flag["id"], str(row["id"]), flag["address"], flag["earlier_version_id"],
            flag["later_version_id"], requested_by,
            f"derived from {flag['dependant_kind']} {flag['dependant_id']}, resolved "
            f"{flag['resolution']} (flag {flag['id']})")
        written += rid is not None
    return written


async def request_rederive(pool: Any, flag_id: int, *, requested_by: str,
                           note: Optional[str] = None) -> dict[str, Any]:
    """Record "re-derive this flag's dependant with the changed text". One
    pending request per (dependant, later version): a repeat returns the
    pending one (``duplicate: true``)."""
    who = _principal(requested_by)
    async with pool.acquire() as conn:
        flag = await conn.fetchrow(
            "SELECT id, dependant_kind, dependant_id, address, earlier_version_id, later_version_id "
            "FROM evidence_drift_flags WHERE id = $1", int(flag_id))
        if flag is None:
            return {"status": "unknown_flag"}
        rid = uuid.uuid4()
        inserted = await conn.fetchval(
            "INSERT INTO evidence_rederive_requests (id, flag_id, dependant_kind, dependant_id, address, "
            "earlier_version_id, later_version_id, requested_by, note) "
            "VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9) ON CONFLICT DO NOTHING RETURNING id",
            rid, flag["id"], flag["dependant_kind"], flag["dependant_id"], flag["address"],
            flag["earlier_version_id"], flag["later_version_id"], who, _note(note),
        )
        if inserted is not None:
            return {"status": "pending", "id": str(inserted), "duplicate": False}
        existing = await conn.fetchval(
            "SELECT id FROM evidence_rederive_requests WHERE dependant_kind = $1 AND dependant_id = $2 "
            "AND coalesce(later_version_id, -1) = coalesce($3::bigint, -1) "
            "AND state IN ('pending', 'taken') LIMIT 1",
            flag["dependant_kind"], flag["dependant_id"], flag["later_version_id"])
        return {"status": "pending", "id": str(existing) if existing else None, "duplicate": True}

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The held family's reads: the two
``GET /v3/record/assertions`` routes and the verify script.

* :func:`list_assertions` — a LIGHT page by default: per assertion its class,
  type, subject, current status and version number, and the first
  :data:`SUMMARY_CHARS` characters of its current text; ``full=True`` adds the
  whole text and the slots. Filters: subject key (an entity id, a target id,
  an ISO code or a normalised surface name), class, status. Newest first,
  keyset-paged on ``(created_at, id)``. Replay batches are never listed.
* :func:`assertion_detail` — one assertion: every version, the status log
  with its chain fields and the chain re-verified for that assertion, the
  cache row and whether it equals the rebuild, and every support link with its
  bytes label (``received_bytes`` / ``archived_later`` / ``no_bytes_kept``, or
  ``none`` when no receipt was pinned; a quote is cut to one display span,
  as the Record sheet's support read cuts it) and the restatement links that
  point at it.
* :func:`chain_report` — every assertion's chain, and the cache against the
  rebuild (the verify script's body).

Read-only. A LEAF: stdlib + :mod:`held_record_chain` + ``display_policy`` (the registry's slim
image imports it).
"""
from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from .display_policy import SPAN_CHARS
from .held_record_chain import LOG_COLUMNS, rebuild_cache, verify_chain

__all__ = [
    "CLASSES",
    "DEFAULT_LIMIT",
    "MAX_LIMIT",
    "STATUSES",
    "SUMMARY_CHARS",
    "assertion_detail",
    "chain_report",
    "list_assertions",
    "list_sql",
]

DEFAULT_LIMIT = 50
MAX_LIMIT = 200
SUMMARY_CHARS = 240
MAX_SUPPORT = 400
CLASSES = ("statement", "attributed", "assessment", "absence")
STATUSES = ("held", "not_held", "contested", "superseded", "withdrawn")

_LIST_SQL = """
SELECT a.id::text AS id, a.class, a.type_id, a.basis, a.subject_kind, a.subject_key, a.subject_text,
       a.speaker_text, a.collection, a.created_at, s.status, s.status_at, s.entries AS status_entries,
       v.version_no, v.valid_from, v.valid_to, {text} AS text, length(v.text) AS text_chars{extra}
  FROM assertions a
  JOIN assertion_status s ON s.assertion_id = a.id
  JOIN assertion_versions v ON v.id = s.version_id
 WHERE a.batch IS NULL{where}
 ORDER BY a.created_at DESC, a.id DESC
 LIMIT ${n}
"""


def list_sql(*, subject: str | None, class_: str | None, status: str | None, cursor: str | None,
             limit: int, full: bool) -> tuple[str, list[Any]]:
    """The page's SQL with ONLY the filters asked for (no ``$n IS NULL OR``
    arms, so a cached generic plan cannot lose the index), and its args."""
    where, args = [], []
    for col, val in (("a.subject_key", subject), ("a.class", class_), ("s.status", status)):
        if val is not None:
            args.append(val)
            where.append(f"{col} = ${len(args)}")
    at, aid = _cursor(cursor)
    if at is not None:
        args += [at, aid]
        where.append(f"(a.created_at, a.id) < (${len(args) - 1}, ${len(args)}::uuid)")
    args.append(limit)
    sql = _LIST_SQL.format(
        text="v.text" if full else f"left(v.text, {SUMMARY_CHARS})",
        extra=", v.slots, a.subject_entity_id::text AS subject_entity_id" if full else "",
        where="".join(f"\n   AND {w}" for w in where), n=len(args))
    return sql, args


_ASSERTION_SQL = """
SELECT a.id::text AS id, a.class, a.type_id, a.basis, a.subject_kind, a.subject_entity_id::text AS subject_entity_id,
       a.subject_key, a.subject_text, a.speaker_text, a.speaker_entity_id::text AS speaker_entity_id, a.claim_key,
       a.collection, a.promoted_from::text AS promoted_from, a.origin, a.batch, p.handle AS written_by, a.created_at
  FROM assertions a LEFT JOIN principals p ON p.id = a.written_by
 WHERE a.id = $1::uuid
"""

_VERSIONS_SQL = """
SELECT v.id, v.version_no, v.prev_version_id, v.reason_code, v.text, v.text_sha256, v.slots, v.valid_from,
       v.valid_from_precision, v.valid_to, v.shadow_id, v.shadow_row, v.run_id, v.analyst_id, v.contract_version,
       v.gate_version, pp.handle AS proposed_by, pw.handle AS written_by, v.recorded_at
  FROM assertion_versions v
  LEFT JOIN principals pp ON pp.id = v.proposed_by
  LEFT JOIN principals pw ON pw.id = v.written_by
 WHERE v.assertion_id = $1::uuid
 ORDER BY v.version_no
"""

_LOG_SQL = f"""
SELECT {LOG_COLUMNS}, (SELECT handle FROM principals p WHERE p.id = l.caused_by) AS caused_by_handle
  FROM assertion_status_log l
 WHERE l.assertion_id = $1::uuid
 ORDER BY l.seq
"""

_CACHE_SQL = """
SELECT status, version_id, last_seq, last_row_hash, status_at, entries
  FROM assertion_status WHERE assertion_id = $1::uuid
"""

_SUPPORT_SQL = """
SELECT s.id, s.claimant_version_id, s.claimant_shadow_row, s.role, s.target_kind, s.signal_id::text AS signal_id,
       s.observation_id::text AS observation_id, s.sentence_id, s.output_id::text AS output_id, s.target_version_id,
       s.verdict_key, s.verdict, s.target_basis, s.fetch_version_id, s.fetch_link_role, s.bytes_label, s.bytes_sha256,
       s.text_sha256, s.quote, s.quote_found_in, s.stance, s.speaker, s.detail, s.recorded_at
  FROM assertion_support s
 WHERE s.claimant_version_id = ANY($1::bigint[]) OR s.target_version_id = ANY($1::bigint[])
 ORDER BY s.id
 LIMIT $2
"""

_ALL_LOG_SQL = f"SELECT {LOG_COLUMNS} FROM assertion_status_log l ORDER BY l.assertion_id, l.seq"
_ALL_CACHE_SQL = ("SELECT assertion_id::text AS assertion_id, status, version_id, last_seq, last_row_hash, status_at, "
                  "entries FROM assertion_status")


def _iso(v: Any) -> Any:
    return v.isoformat() if isinstance(v, datetime) else v


def _plain(row: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for k, v in dict(row).items():
        if k in ("slots", "detail") and isinstance(v, str):
            v = json.loads(v)
        out[k] = _iso(v) if not isinstance(v, (int, float, bool, str, dict, list, type(None))) else v
        if not isinstance(out[k], (int, float, bool, str, dict, list, type(None))):
            out[k] = str(out[k])
    return out


def _cursor(cursor: str | None) -> tuple[datetime | None, str | None]:
    if not cursor:
        return None, None
    at, _, aid = cursor.partition("|")
    return datetime.fromisoformat(at), aid


async def list_assertions(conn: Any, *, subject: str | None = None, class_: str | None = None,
                          status: str | None = None, limit: int = DEFAULT_LIMIT, cursor: str | None = None,
                          full: bool = False) -> dict[str, Any]:
    """One page. ``cursor`` is the previous page's ``next_cursor``."""
    limit = max(1, min(int(limit), MAX_LIMIT))
    sql, args = list_sql(subject=subject, class_=class_, status=status, cursor=cursor, limit=limit + 1, full=full)
    rows = await conn.fetch(sql, *args)
    items = [_plain(r) for r in rows[:limit]]
    for it in items:
        it["text_truncated"] = (not full) and it["text_chars"] > SUMMARY_CHARS
    nxt = None
    if len(rows) > limit:
        last = rows[limit - 1]
        nxt = f"{last['created_at'].isoformat()}|{last['id']}"
    return {"weight": "full" if full else "summary", "items": items, "next_cursor": nxt}


async def assertion_detail(conn: Any, assertion_id: str) -> dict[str, Any]:
    head = await conn.fetchrow(_ASSERTION_SQL, assertion_id)
    if head is None:
        return {"found": False}
    versions = [_plain(r) for r in await conn.fetch(_VERSIONS_SQL, assertion_id)]
    log_rows = await conn.fetch(_LOG_SQL, assertion_id)
    checked, breaks = verify_chain([dict(r) for r in log_rows])
    cache = await conn.fetchrow(_CACHE_SQL, assertion_id)
    rebuilt = rebuild_cache([dict(r) for r in log_rows]).get(head["id"])
    cache_ok = cache is not None and rebuilt is not None and all(
        rebuilt[k] == cache[k] for k in ("status", "version_id", "last_seq", "last_row_hash", "status_at", "entries"))
    vids = [int(v["id"]) for v in versions]
    support = [_plain(r) for r in await conn.fetch(_SUPPORT_SQL, vids, MAX_SUPPORT + 1)]
    for s in support:
        # a quote is cut to one display span: no publisher text beyond it (display policy)
        s["quote"] = s["quote"][:SPAN_CHARS] if isinstance(s.get("quote"), str) else None
        if s["target_kind"] in ("signal", "observation"):
            s["bytes"] = s["bytes_label"] or "none"
    log = []
    for r in log_rows:
        d = _plain(r)
        d["caused_by"] = d.pop("caused_by_handle")
        log.append(d)
    return {
        "found": True, "assertion": _plain(head), "versions": versions, "status_log": log,
        "status": _plain(cache) if cache is not None else None,
        "chain": {"rows": checked, "ok": not breaks, "breaks": [b.as_dict() for b in breaks],
                  "cache_equals_rebuild": cache_ok},
        "rests_on": [s for s in support[:MAX_SUPPORT] if s["claimant_version_id"] in vids],
        "restated_or_used_by": [s for s in support[:MAX_SUPPORT] if s["target_version_id"] in vids
                                and s["claimant_version_id"] not in vids],
        "truncated": len(support) > MAX_SUPPORT,
    }


async def chain_report(conn: Any) -> dict[str, Any]:
    """Every chain re-verified, and the stored cache against the rebuild."""
    rows = [dict(r) for r in await conn.fetch(_ALL_LOG_SQL)]
    checked, breaks = verify_chain(rows)
    rebuilt = rebuild_cache(rows)
    stored = {r["assertion_id"]: dict(r) for r in await conn.fetch(_ALL_CACHE_SQL)}
    keys = ("status", "version_id", "last_seq", "last_row_hash", "status_at", "entries")
    differ = sorted(a for a in set(rebuilt) | set(stored)
                    if a not in rebuilt or a not in stored or any(rebuilt[a][k] != stored[a][k] for k in keys))
    return {"rows": checked, "assertions": len(rebuilt), "breaks": [b.as_dict() for b in breaks],
            "cache_rows": len(stored), "cache_differs": differ, "rebuilt": rebuilt}

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``kev_digest`` sub-handler — a deterministic digest of NEW catalogue entries.

No model call. Lane K-2 (2026-11-14). The cyber desk's register feed
(``source.cisa.kev``, lane K-1) lands each newly added catalogue entry as ONE
signal: ``payload.external_id`` = the CVE id, ``payload.item`` = the whole
record (cveID, vendorProject, product, vulnerabilityName, dateAdded, dueDate,
knownRansomwareCampaignUse, …). This handler lists the entries that entered
the collection SINCE THE LAST DIGEST, one line each, copying the record's own
fields verbatim — nothing inferred, nothing re-worded, no severity invented.

THE PRECEDENT. ``series_change`` (N-L): a deterministic FINDING over a live
sink, citing the rows it rests on, readable through the existing findings
route (``GET /api/v1/findings?analyst_id=kev_digest``) with the
``unverified — structural`` badge every deterministic finding carries
(``provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS``). No new table, no
migration, no new route, no new panel.

"SINCE THE LAST DIGEST" — THE WATERMARK. Every finding carries
``data.watermark`` = the newest ``signals.created_at`` it digested. The next
run reads its own newest finding's watermark and selects rows created AFTER
it (``idx_signals_source_created_at``). A run that finds nothing new returns
``force_trace_only`` (the indicator_tracker precedent): no feed row, and the
watermark stays where the last real digest left it, so nothing is skipped.
The first run (no prior digest) reaches back ``initial_lookback_hours``.
Re-yields never re-list: only canonical rows are read (a tier-2 alias carries
``canonical_signal_id``), and a CVE id the previous digest already listed is
dropped.

WHAT IT DOES NOT SAY. The catalogue's EDITS to an existing entry never arrive
(K-1's absence contract), so a digest never reports "ransomware use changed";
the flag is whatever the feed gave on the day the entry was added. A digest
says nothing about entries that are not new.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "kev_digest"
METHOD_VERSION = "kev_digest/2026-11.1"

DEFAULT_SOURCE_IDS: tuple[str, ...] = ("source.cisa.kev",)
DEFAULT_INITIAL_LOOKBACK_HOURS = 72
DEFAULT_MAX_ENTRIES = 200

#: The record fields the digest copies, in the order a line prints them.
FIELDS: tuple[str, ...] = (
    "cveID", "vendorProject", "product", "vulnerabilityName",
    "dateAdded", "dueDate", "knownRansomwareCampaignUse",
)

#: The newest prior digest (its own watermark + the CVE ids it listed).
#: Rides idx_analyst_outputs_analyst_id; a digest writes at most two rows a day.
_PRIOR_SQL = """
SELECT data
  FROM analyst_outputs
 WHERE analyst_id = $1
   AND kind = 'finding'
 ORDER BY produced_at DESC
 LIMIT 1
"""

#: Canonical rows of the register feed(s) created after the watermark.
#: Rides idx_signals_source_created_at (source_id, created_at).
_NEW_ROWS_SQL = """
SELECT id, source_id, created_at, payload -> 'item' AS item,
       payload ->> 'external_id' AS external_id
  FROM signals
 WHERE source_id = ANY($1::text[])
   AND created_at > $2
   AND canonical_signal_id IS NULL
 ORDER BY created_at, id
 LIMIT $3
"""


def _as_dict(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return {}
    return dict(raw) if isinstance(raw, Mapping) else {}


def prior_state(row_data: Any) -> tuple[datetime | None, frozenset[str]]:
    """``(watermark, listed CVE ids)`` off a prior digest's stored ``data``.

    The row's ``data`` column is the FindingPayload dump, so the handler's own
    ``data`` sits one level in (``data -> 'data'``). Unparseable = no prior."""
    outer = _as_dict(row_data)
    inner = _as_dict(outer.get("data")) or outer
    if inner.get("sub_handler") != SUB_HANDLER_NAME:
        return None, frozenset()
    mark: datetime | None = None
    raw = inner.get("watermark")
    if isinstance(raw, str) and raw:
        try:
            mark = datetime.fromisoformat(raw)
        except ValueError:
            mark = None
        if mark is not None and mark.tzinfo is None:
            mark = mark.replace(tzinfo=timezone.utc)
    listed = frozenset(
        str(e.get("cveID")) for e in (inner.get("entries") or [])
        if isinstance(e, Mapping) and e.get("cveID")
    )
    return mark, listed


def entry_from_row(row: Mapping[str, Any]) -> dict[str, Any] | None:
    """One digest entry from one signal row (pure). ``None`` = no CVE id.

    Every field is the feed's own value, copied; a field the record lacks is
    ``None`` (rendered "not given"), never a guess."""
    item = _as_dict(row.get("item"))
    cve = str(item.get("cveID") or row.get("external_id") or "").strip()
    if not cve:
        return None
    entry: dict[str, Any] = {k: (str(item[k]).strip() if item.get(k) not in (None, "") else None)
                             for k in FIELDS}
    entry["cveID"] = cve
    entry["signal_id"] = str(row["id"])
    entry["source_id"] = str(row.get("source_id") or "")
    return entry


def digest_entries(
    rows: Sequence[Mapping[str, Any]], *, already_listed: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """The new entries, one per CVE id, newest ``dateAdded`` first (pure)."""
    seen: set[str] = set(already_listed)
    out: list[dict[str, Any]] = []
    for row in rows:
        entry = entry_from_row(row)
        if entry is None or entry["cveID"] in seen:
            continue
        seen.add(entry["cveID"])
        out.append(entry)
    out.sort(key=lambda e: (e.get("dateAdded") or "", e["cveID"]), reverse=True)
    return out


def _given(value: Any) -> str:
    return str(value) if value not in (None, "") else "not given"


def render_line(entry: Mapping[str, Any]) -> str:
    """One digest line, the feed's own words (pure)."""
    return (
        f"- {entry['cveID']} — {_given(entry.get('vendorProject'))} / "
        f"{_given(entry.get('product'))}: {_given(entry.get('vulnerabilityName'))}. "
        f"Added {_given(entry.get('dateAdded'))}; due {_given(entry.get('dueDate'))}; "
        f"known ransomware campaign use: {_given(entry.get('knownRansomwareCampaignUse'))}."
    )


def build_finding(
    entries: Sequence[Mapping[str, Any]], *, since: datetime, watermark: datetime,
    first_run: bool, as_of: datetime, sources: Sequence[str],
) -> FindingPayload:
    """The digest finding (pure)."""
    n = len(entries)
    since_txt = since.isoformat()
    title = (f"KEV digest: {n} new catalogue entr{'y' if n == 1 else 'ies'} since {since_txt[:16]}Z"
             if n else f"KEV digest: no new catalogue entries since {since_txt[:16]}Z")
    head = (
        f"Rule {METHOD_VERSION}: entries of {', '.join(sources)} that entered this collection "
        f"after {since_txt}"
        + (" (first digest: initial look-back)" if first_run else " (the previous digest's watermark)")
        + ". Each line copies the catalogue record's own fields: CVE id, vendor / product, "
        "vulnerability name, date added, remediation due date, and the known-ransomware-use flag "
        "as the feed gives it. Edits to existing entries are not collected."
    )
    body = head + ("\n\n" + "\n".join(render_line(e) for e in entries) if entries else "")
    return FindingPayload(
        title=title[:2048], body=body[:65536], confidence=1.0,
        tags=["kev_digest", "deterministic", "no_llm"],
        data={
            "sub_handler": SUB_HANDLER_NAME, "method_version": METHOD_VERSION,
            "as_of": as_of.isoformat(), "since": since_txt, "first_run": first_run,
            "watermark": watermark.isoformat(), "sources": list(sources),
            "entries_total": n, "entries": [dict(e) for e in entries],
            "citations": [{"ref_kind": "signal", "ref_id": e["signal_id"],
                           "cveID": e["cveID"], "source_id": e["source_id"]} for e in entries],
        },
    )


async def handle(
    inputs: list[dict[str, Any]], options: Mapping[str, Any], deps: Any | None,
) -> AnalystMethodResult:
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "kev_digest requires a live deps.pg_pool — refusing to report "
            "'no new entries' without reading the feed"
        )
    sources = [str(s) for s in (options.get("source_ids") or DEFAULT_SOURCE_IDS)]
    lookback_h = max(1, int(options.get("initial_lookback_hours", DEFAULT_INITIAL_LOOKBACK_HOURS)))
    max_entries = max(1, int(options.get("max_entries", DEFAULT_MAX_ENTRIES)))
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    as_of = datetime.now(tz=timezone.utc)

    async with pool.acquire() as conn:
        prior = await conn.fetchrow(_PRIOR_SQL, analyst_id)
        mark, listed = prior_state(prior["data"] if prior is not None else None)
        first_run = mark is None
        since = mark if mark is not None else as_of - timedelta(hours=lookback_h)
        rows = await conn.fetch(_NEW_ROWS_SQL, sources, since, max_entries)

    entries = digest_entries(rows, already_listed=listed)
    watermark = max((r["created_at"] for r in rows), default=since)
    finding = build_finding(entries, since=since, watermark=watermark,
                            first_run=first_run, as_of=as_of, sources=sources)
    logger.info("kev_digest.done rows=%d entries=%d first_run=%s", len(rows), len(entries), first_run)
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        # Nothing new = no feed row; the watermark stays on the last real digest.
        force_trace_only=not entries,
        derived_from=[UUID(e["signal_id"]) for e in entries],
    )


__all__ = [
    "DEFAULT_SOURCE_IDS",
    "FIELDS",
    "METHOD_VERSION",
    "SUB_HANDLER_NAME",
    "build_finding",
    "digest_entries",
    "entry_from_row",
    "handle",
    "prior_state",
    "render_line",
]

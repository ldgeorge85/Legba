# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The DEPENDANT WALK of a superseded page version (lane D-1) — bounded.

What relied on version V of address A, by depth:

* **depth 0 — the records built from it.** ``fetch_version_records`` rows of
  V and of the earlier versions of A (a record built from an earlier version
  carried the same text up to V unless a classified change in between said
  otherwise), plus the signals whose evidence-archive copy IS one of those
  versions' bytes (``evidence_archive.sha256`` = the version's sha256, same
  canonical-URL hash — the archiver's own versions are linked that way, not
  through ``fetch_version_records``). Kinds ``signal`` / ``observation``.
* **depth 1 — what CITES those records.** Findings (``analyst_outputs``
  whose ``data.data.citations[*]`` names the record as ``signal_id`` or as a
  ``ref_id`` of kind signal/observation — found through the GIN index on
  ``derived_from``, which contains every cited signal: measured 9,582 of
  9,582 signal citations on 2026-11-13; the three ``ref_id`` citations of the
  same two days that were not in ``derived_from`` are missed), journal entries
  and proposals (``cited_substrate_refs``), consult turns (``cited_refs``),
  inquiry ledger rows, claim contentions (``ref_signal_ids``) and the sealed
  ledgers' evidence (``forecast_resolutions`` / proposals / ``scorecard_entries``
  ``evidence_refs``). An output whose ``derived_from`` holds the record but
  that does not cite it consumed it as an INPUT only: counted
  (``inputs_only``), never flagged — a desk slice holds hundreds of rows and
  flagging every run that saw one would bury the citing ones.
* **depth 2 — outputs derived from those findings.** ``analyst_outputs``
  whose ``derived_from`` names a depth-1 finding, and consumers in
  ``output_consumption`` (kind ``derived_output``).

THE BOUND: ``max_depth`` (0-2) and ``max_dependants`` (the walk stops adding
when it reaches the cap and says so: ``truncated``). Each step is one
statement over an indexed column; the record and finding id sets are capped
before the next step.

The cited span: a finding's citation carries the text the model was shown —
``quote`` when present, else ``evidence_text``, else ``snippet``, else
``source_text`` — kept (bounded) for the exact span check.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

__all__ = [
    "DEFAULT_MAX_DEPENDANTS",
    "DEFAULT_MAX_DEPTH",
    "Dependant",
    "WalkResult",
    "walk_dependants",
    "walk_from_records",
]

DEFAULT_MAX_DEPTH = 2
DEFAULT_MAX_DEPENDANTS = 200
#: Versions of one address considered "relied on up to V".
MAX_EARLIER_VERSIONS = 20
#: Characters of a cited span kept on the flag.
SPAN_MAX_CHARS = 4000

_SPAN_KEYS = ("quote", "evidence_text", "snippet", "source_text")


@dataclass
class Dependant:
    kind: str
    id: str
    depth: int
    via_kind: Optional[str] = None
    via_id: Optional[str] = None
    record_version_id: Optional[int] = None
    cited_span: Optional[str] = None


@dataclass
class WalkResult:
    dependants: list[Dependant] = field(default_factory=list)
    inputs_only: int = 0
    depth_reached: int = 0
    truncated: bool = False

    def _add(self, dep: Dependant, cap: int) -> bool:
        if len(self.dependants) >= cap:
            self.truncated = True
            return False
        self.dependants.append(dep)
        self.depth_reached = max(self.depth_reached, dep.depth)
        return True


_VERSIONS_SQL = """
SELECT v.id, v.sha256
  FROM fetch_versions v, fetch_versions e
 WHERE e.id = $1 AND v.address = e.address AND v.receipt_kind = 'fetch'
   AND v.first_seen_at <= e.first_seen_at
 ORDER BY v.first_seen_at DESC, v.id DESC
 LIMIT $2
"""

_RECORDS_SQL = """
SELECT r.record_kind, r.record_id::text AS record_id, r.version_id
  FROM fetch_version_records r
 WHERE r.version_id = ANY($1::bigint[])
UNION
SELECT 'signal', s.id::text, v.id
  FROM fetch_versions v
  JOIN evidence_archive ea ON ea.sha256 = v.sha256
  JOIN signals s ON s.id = ea.signal_id
                AND s.canonical_url_hash = encode(sha256(convert_to(v.address, 'UTF8')), 'hex')
 WHERE v.id = ANY($1::bigint[])
LIMIT $2
"""

#: Outputs holding one of the records in derived_from (GIN), with the
#: citations that name one of them. $1 = record ids (uuid[]), $2 = limit.
_CITING_OUTPUTS_SQL = """
WITH held AS (
    SELECT o.id, o.kind, o.data
      FROM analyst_outputs o
     WHERE o.derived_from && $1::uuid[]
), cited AS (
    SELECT h.id, h.kind,
           (SELECT coalesce(jsonb_agg(c), '[]'::jsonb)
              FROM jsonb_array_elements(CASE WHEN jsonb_typeof(h.data->'data'->'citations') = 'array'
                                             THEN h.data->'data'->'citations' ELSE '[]'::jsonb END) c
             WHERE jsonb_typeof(c) = 'object'
               AND ((c->>'signal_id') = ANY($3::text[])
                    OR ((c->>'ref_kind') IN ('signal', 'observation')
                        AND (c->>'ref_id') = ANY($3::text[])))) AS cites
      FROM held h
)
SELECT id::text AS id, kind, cites, (SELECT count(*) FROM held) AS held
  FROM cited
 WHERE jsonb_array_length(cites) > 0
 LIMIT $2
"""

#: Every other citation edge: (kind, id, cited record id).
_OTHER_CITERS_SQL = """
SELECT 'journal_entry' AS kind, j.id::text AS id, u::text AS rid
  FROM journal_entries j, unnest(j.cited_substrate_refs) u WHERE u = ANY($1::uuid[])
UNION ALL
SELECT 'journal_proposal', p.id::text, u::text
  FROM journal_proposals p, unnest(p.cited_substrate_refs) u WHERE u = ANY($1::uuid[])
UNION ALL
SELECT 'consult_turn', t.id::text, c->>'id'
  FROM consult_turns t,
       jsonb_array_elements(CASE WHEN jsonb_typeof(t.cited_refs) = 'array' THEN t.cited_refs ELSE '[]'::jsonb END) c
 WHERE jsonb_typeof(c) = 'object' AND (c->>'id') = ANY($2::text[])
UNION ALL
SELECT 'inquiry', i.id::text, x
  FROM inquiry_ledger i,
       jsonb_array_elements_text(CASE WHEN jsonb_typeof(i.cited_refs) = 'array' THEN i.cited_refs ELSE '[]'::jsonb END) x
 WHERE x = ANY($2::text[])
UNION ALL
SELECT 'claim_contention', cc.id::text, u::text
  FROM claim_contentions cc, unnest(cc.ref_signal_ids) u WHERE u = ANY($1::uuid[])
UNION ALL
SELECT 'forecast_resolution', f.id::text, u
  FROM forecast_resolutions f, unnest(f.evidence_refs) u WHERE u = ANY($2::text[])
UNION ALL
SELECT 'forecast_resolution_proposal', f.id::text, u
  FROM forecast_resolution_proposals f, unnest(f.evidence_refs) u WHERE u = ANY($2::text[])
UNION ALL
SELECT 'scorecard_entry', s.id::text, u
  FROM scorecard_entries s, unnest(s.evidence_refs) u WHERE u = ANY($2::text[])
LIMIT $3
"""

#: Depth 2: outputs derived from a depth-1 finding, and its consumers.
_DERIVED_SQL = """
SELECT o.id::text AS id, f::text AS via
  FROM analyst_outputs o, unnest(o.derived_from) f
 WHERE o.derived_from && $1::uuid[] AND f = ANY($1::uuid[])
UNION
SELECT oc.consumer_id::text, oc.consumed_id::text
  FROM output_consumption oc
 WHERE oc.consumed_id = ANY($1::uuid[])
LIMIT $2
"""


def _span(cite: Any) -> Optional[str]:
    if not isinstance(cite, dict):
        return None
    for key in _SPAN_KEYS:
        val = cite.get(key)
        if isinstance(val, str) and val.strip():
            return val[:SPAN_MAX_CHARS]
    return None


async def walk_dependants(
    conn: Any, earlier_version_id: int, *,
    max_depth: int = DEFAULT_MAX_DEPTH, max_dependants: int = DEFAULT_MAX_DEPENDANTS,
) -> WalkResult:
    """What relied on ``earlier_version_id`` (and the address's versions
    before it), to ``max_depth``, at most ``max_dependants``."""
    cap = max(1, int(max_dependants))
    versions = await conn.fetch(_VERSIONS_SQL, int(earlier_version_id), MAX_EARLIER_VERSIONS)
    vids = [int(r["id"]) for r in versions] or [int(earlier_version_id)]
    recs = await conn.fetch(_RECORDS_SQL, vids, cap + 1)
    return await walk_from_records(
        conn, [(str(r["record_kind"]), str(r["record_id"]), int(r["version_id"])) for r in recs],
        max_depth=max_depth, max_dependants=cap)


async def walk_from_records(
    conn: Any, records: list[tuple[str, str, Optional[int]]], *,
    max_depth: int = DEFAULT_MAX_DEPTH, max_dependants: int = DEFAULT_MAX_DEPENDANTS,
) -> WalkResult:
    """The walk from known depth-0 records ``(kind, id, version id or None)``
    — :func:`walk_dependants` after its version step, and the backfill's
    entry for evidence-archive copies that predate the version table."""
    out = WalkResult()
    cap = max(1, int(max_dependants))
    record_version: dict[str, Optional[int]] = {}
    for kind, rid, vid in records:
        if rid in record_version:
            continue
        record_version[rid] = vid
        out._add(Dependant(kind, rid, 0, "version", None if vid is None else str(vid), vid), cap)
    if max_depth < 1 or not record_version or out.truncated:
        return out
    rids = list(record_version)
    finding_ids: list[str] = []
    seen: set[tuple[str, str]] = {(d.kind, d.id) for d in out.dependants}
    citing = await conn.fetch(_CITING_OUTPUTS_SQL, rids, cap + 1, rids)
    if citing:
        out.inputs_only = max(0, int(citing[0]["held"]) - len(citing))
    else:
        out.inputs_only = int(await conn.fetchval(
            "SELECT count(*) FROM analyst_outputs WHERE derived_from && $1::uuid[]", rids) or 0)
    for row in citing:
        cites = row["cites"]
        if isinstance(cites, str):
            cites = json.loads(cites)
        first = cites[0]
        via = str(first.get("signal_id") or first.get("ref_id") or "")
        key = ("finding", str(row["id"]))
        if key in seen:
            continue
        seen.add(key)
        if out._add(Dependant("finding", str(row["id"]), 1, "signal", via,
                              record_version.get(via), _span(first)), cap):
            finding_ids.append(str(row["id"]))
    for row in await conn.fetch(_OTHER_CITERS_SQL, rids, rids, cap + 1):
        key = (row["kind"], str(row["id"]))
        if key in seen:
            continue
        seen.add(key)
        rid = str(row["rid"])
        out._add(Dependant(row["kind"], str(row["id"]), 1, "record", rid,
                           record_version.get(rid)), cap)
    if max_depth < 2 or not finding_ids or out.truncated:
        return out
    for row in await conn.fetch(_DERIVED_SQL, finding_ids, cap + 1):
        key = ("derived_output", str(row["id"]))
        if key in seen or ("finding", str(row["id"])) in seen:
            continue
        seen.add(key)
        out._add(Dependant("derived_output", str(row["id"]), 2, "finding", str(row["via"])), cap)
    return out

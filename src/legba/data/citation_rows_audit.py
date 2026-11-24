# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Citations as rows (lane DM-A): the day's measurement, in SQL alone.

ONE number, two sources, the same unit, so before and after compare:

    of a day's citation LINKS, the share that join to a record, and the share
    that join to a record AND to stored bytes.

A LINK is one record a citation names (a grounding block of N members is N
links), plus one dead link for every citation entry that names nothing.

* ``source='json'`` (the baseline; works on any day): the citations stored in
  each finding's document, exploded and resolved by the SAME declared rules
  the writer uses (the ``citation_target`` family), generated into SQL here.
* ``source='rows'`` (the proof after deploy): ``citation_support`` plus the
  entry-level ``citation_unresolved`` rows of findings written that day.

"Joins to a record": the id names a row of its table now. "Joins to stored
bytes": the record is material (a signal or an observation) and a receipt
version it links to still holds its bytes (``object_ref`` set, not pruned);
``hash_only`` counts material whose receipts kept only the hash, ``no_receipt``
material with no receipt at all. Non-material links (a prior read, a register
member) never join to bytes: they rest on other records, not on bytes.

Broken down by desk (``analyst_id``) and by target kind. Read-only SELECTs.
The SQL is text so it can be run through ``psql`` with a read-only session
(``scripts/citation_rows_report.py --sql``).
"""
from __future__ import annotations

from collections.abc import Mapping

from .citation_rows import TargetSpec, target_specs

__all__ = ["ENTRY_REASONS", "report_sql"]

#: Unresolved reasons that are about an ENTRY (a dead link), as opposed to a
#: prose marker with no entry (counted apart).
ENTRY_REASONS = ("entry_without_target", "unknown_ref_kind", "malformed_id")

_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"


def _lit(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _json_links_sql(specs: Mapping[str, TargetSpec]) -> str:
    """One row per link (fid, analyst_id, kind, key) from the stored citations,
    plus a dead row (kind NULL) per entry that names nothing."""
    signal_kinds = [s for s in specs.values() if s.kind == "signal"]
    parts = []
    named = []
    if signal_kinds:
        parts.append(
            "SELECT fid, analyst_id, n, 'signal' AS kind, e->>'signal_id' AS key FROM e"
            " WHERE coalesce(e->>'signal_id', '') <> ''")
        named.append("coalesce(e->>'signal_id', '') <> ''")
    not_signal = "coalesce(e->>'signal_id', '') = ''"
    for spec in sorted(specs.values(), key=lambda s: s.kind):
        refs = [r for r in spec.ref_kinds]
        if refs:
            idexpr = "coalesce(" + ", ".join(f"nullif(e->>{_lit(f)}, '')" for f in spec.id_fields) + ")"
            cond = f"{not_signal} AND e->>'ref_kind' IN ({', '.join(_lit(r) for r in refs)})"
            parts.append(
                f"SELECT fid, analyst_id, n, {_lit(spec.kind)}, {idexpr} FROM e"
                f" WHERE {cond} AND {idexpr} IS NOT NULL")
            named.append(f"({cond} AND {idexpr} IS NOT NULL)")
        for block, fld in sorted(spec.member_fields.items()):
            cond = (f"{not_signal} AND e->>'ref_kind' = {_lit(block)}"
                    f" AND jsonb_typeof(e->{_lit(fld)}) = 'array'")
            parts.append(
                f"SELECT fid, analyst_id, n, {_lit(spec.kind)}, m FROM e,"
                f" jsonb_array_elements_text(e->{_lit(fld)}) m WHERE {cond}")
            named.append(f"({cond} AND jsonb_array_length(e->{_lit(fld)}) > 0)")
    parts.append(
        "SELECT fid, analyst_id, n, NULL, NULL FROM e WHERE NOT ("
        + " OR ".join(named) + ")")
    return "\n  UNION ALL\n  ".join(parts)


def _exists_sql(specs: Mapping[str, TargetSpec], kind_col: str, key_col: str) -> str:
    """A CASE that is true when the link's record exists (key compared as text
    after a uuid shape check, so a malformed id is simply not found)."""
    arms = []
    for spec in sorted(specs.values(), key=lambda s: s.kind):
        if spec.key == "uuid":
            arms.append(
                f"WHEN {kind_col} = {_lit(spec.kind)} THEN {key_col} ~ {_lit(_UUID_RE)} AND EXISTS ("
                f"SELECT 1 FROM {spec.table} x WHERE x.id = CASE WHEN {key_col} ~ {_lit(_UUID_RE)}"
                f" THEN {key_col}::uuid END)")
        elif spec.table == "desk_baselines":
            arms.append(
                f"WHEN {kind_col} = {_lit(spec.kind)} THEN EXISTS (SELECT 1 FROM desk_baselines x"
                f" WHERE x.desk_id || ':' || x.metric = {key_col})")
    return "CASE " + " ".join(arms) + " ELSE false END"


def _bytes_sql(specs: Mapping[str, TargetSpec], kind_col: str, key_col: str, pinned: str | None) -> str:
    """``held`` / ``hash_only`` / ``no_receipt`` for a material link, NULL else.
    ``pinned`` = the pinned version column (rows source) or None (any receipt)."""
    material = sorted(s.kind for s in specs.values() if s.material)
    in_material = f"{kind_col} IN ({', '.join(_lit(k) for k in material)})"
    if pinned is not None:
        return (
            f"CASE WHEN NOT ({in_material}) THEN NULL"
            f" WHEN {pinned} IS NULL THEN 'no_receipt'"
            f" WHEN EXISTS (SELECT 1 FROM fetch_versions v WHERE v.id = {pinned}"
            " AND v.object_ref IS NOT NULL AND v.bytes_pruned_at IS NULL) THEN 'held'"
            " ELSE 'hash_only' END")
    probe = (
        f"FROM fetch_version_records r JOIN fetch_versions v ON v.id = r.version_id"
        f" WHERE r.record_kind = {kind_col} AND {key_col} ~ {_lit(_UUID_RE)}"
        f" AND r.record_id = CASE WHEN {key_col} ~ {_lit(_UUID_RE)} THEN {key_col}::uuid END")
    return (
        f"CASE WHEN NOT ({in_material}) THEN NULL"
        f" WHEN EXISTS (SELECT 1 {probe} AND v.object_ref IS NOT NULL AND v.bytes_pruned_at IS NULL) THEN 'held'"
        f" WHEN EXISTS (SELECT 1 {probe}) THEN 'hash_only'"
        " ELSE 'no_receipt' END")


_SUMMARY = """
, scored AS (
  SELECT analyst_id, coalesce(kind, '(names nothing)') AS kind,
         kind IS NOT NULL AND {exists} AS joins_record,
         {bytes} AS bytes
    FROM links
)
SELECT 'by_desk' AS cut, analyst_id AS key, count(*) AS links,
       count(*) FILTER (WHERE joins_record) AS joins_record,
       count(*) FILTER (WHERE joins_record AND bytes = 'held') AS joins_record_and_bytes,
       count(*) FILTER (WHERE bytes = 'hash_only') AS hash_only,
       count(*) FILTER (WHERE bytes = 'no_receipt') AS no_receipt,
       count(*) FILTER (WHERE kind = '(names nothing)') AS names_nothing
  FROM scored GROUP BY analyst_id
UNION ALL
SELECT 'by_kind', kind, count(*), count(*) FILTER (WHERE joins_record),
       count(*) FILTER (WHERE joins_record AND bytes = 'held'),
       count(*) FILTER (WHERE bytes = 'hash_only'), count(*) FILTER (WHERE bytes = 'no_receipt'),
       count(*) FILTER (WHERE kind = '(names nothing)')
  FROM scored GROUP BY kind
UNION ALL
SELECT 'total', 'all', count(*), count(*) FILTER (WHERE joins_record),
       count(*) FILTER (WHERE joins_record AND bytes = 'held'),
       count(*) FILTER (WHERE bytes = 'hash_only'), count(*) FILTER (WHERE bytes = 'no_receipt'),
       count(*) FILTER (WHERE kind = '(names nothing)')
  FROM scored
ORDER BY 1, 3 DESC, 2;
"""


def report_sql(source: str, *, day_start: str, day_end: str,
               specs: Mapping[str, TargetSpec] | None = None) -> str:
    """The day's report as one SELECT (``day_start`` / ``day_end``: ISO
    timestamps, the half-open window on the finding's ``produced_at``)."""
    specs = specs if specs is not None else target_specs()
    lo, hi = _lit(day_start), _lit(day_end)
    if source == "json":
        head = f"""
WITH e AS (
  SELECT o.id AS fid, o.analyst_id, x.e, x.n
    FROM analyst_outputs o,
         jsonb_array_elements(o.data->'data'->'citations') WITH ORDINALITY AS x(e, n)
   WHERE o.kind = 'finding' AND o.produced_at >= {lo}::timestamptz AND o.produced_at < {hi}::timestamptz
     AND jsonb_typeof(o.data->'data'->'citations') = 'array'
     AND jsonb_typeof(x.e) = 'object'
), links(fid, analyst_id, n, kind, key) AS (
  {_json_links_sql(specs)}
)"""
        return head + _SUMMARY.format(
            exists=_exists_sql(specs, "kind", "key"), bytes=_bytes_sql(specs, "kind", "key", None))
    if source == "rows":
        typed = "coalesce(" + ", ".join(
            f"s.{c}::text" for c in ("signal_id", "observation_id", "output_id", "situation_id",
                                     "event_id", "hypothesis_id", "inquiry_id", "legacy_key")) + ")"
        reasons = ", ".join(_lit(r) for r in ENTRY_REASONS)
        head = f"""
WITH f AS (
  SELECT finding_id, analyst_id FROM finding_citation_receipts
   WHERE produced_at >= {lo}::timestamptz AND produced_at < {hi}::timestamptz
), links(analyst_id, kind, key, pinned) AS (
  -- one link per (finding, marker, record), the json source's unit: a
  -- citation used by three sentences is three support rows but one link
  SELECT f.analyst_id, s.target_kind, {typed}, min(s.fetch_version_id)
    FROM f JOIN citation_support s ON s.finding_id = f.finding_id
   GROUP BY f.finding_id, f.analyst_id, s.marker, s.target_kind, {typed}
  UNION ALL
  SELECT f.analyst_id, NULL, NULL, NULL::bigint
    FROM f JOIN citation_unresolved u ON u.finding_id = f.finding_id
   WHERE u.reason IN ({reasons})
)"""
        return head + _SUMMARY.format(
            exists=_exists_sql(specs, "kind", "key"), bytes=_bytes_sql(specs, "kind", "key", "pinned"))
    raise ValueError(f"source must be 'json' or 'rows', not {source!r}")

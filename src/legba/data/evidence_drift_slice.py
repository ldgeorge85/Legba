# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mark a desk slice row whose source text changed after it was read (lane
D-1) — BEHIND A SWITCH, OFF BY DEFAULT.

The desk input slice and its render are under a byte-identity contract: the
rendered rows ARE the prompt. With ``LEGBA_SLICE_SOURCE_DRIFT_MARK`` off
(the default) nothing here runs and no row carries
:data:`SOURCE_DRIFT_ROW_KEY`, so the slice query, the rows and the rendered
prompt are byte-identical to the tree before this lane.

Switched on, :func:`annotate_source_drift` makes ONE extra read after the
slice is cut — the drift flags whose dependant is one of the slice's signals
(``evidence_drift_flags`` depth 0, i.e. the signal was built from a version
since superseded by a substantive edit, a title change, a retraction or
replacement, or the page going away) — and stamps those rows with the
earliest such change. The render then adds ONE line to each marked row,
after its provenance line::

    source text changed on 2026-11-10: substantive edit

and ONE legend line to the header, only when at least one row is marked. A
slice with no marked row renders byte-identically even with the switch on.

D-2: when the change behind the mark has an assessment, the line carries it
instead of the class::

    source text changed on 2026-11-10: figure revised 19 → 17 (claim reversed, correction notice)

(``mixed`` names its components: ``mixed: claim reversed + update new
facts``). The summary is one line, clipped, from code or a schema-checked
model reply; with no assessment the D-1 line is unchanged.
The slice SQL is never changed (the marking is a second statement on the
rows already selected), so the rows chosen and their order do not move.
"""
from __future__ import annotations

import os
from typing import Any, Mapping, Sequence

__all__ = [
    "SLICE_DRIFT_MARK_ENV",
    "SOURCE_DRIFT_LEGEND",
    "SOURCE_DRIFT_ROW_KEY",
    "annotate_source_drift",
    "slice_drift_mark_enabled",
    "source_drift_line",
]

SLICE_DRIFT_MARK_ENV = "LEGBA_SLICE_SOURCE_DRIFT_MARK"
SOURCE_DRIFT_ROW_KEY = "source_drift"

SOURCE_DRIFT_LEGEND = (
    "Note: a row with a 'source text changed' line cites a page its source "
    "edited, retracted or removed after the platform read it; the snippet is "
    "the text as first read. Do not present such a row's claim as the "
    "source's current wording.\n"
)

_MARK_SQL = """
SELECT f.dependant_id AS signal_id, f.drift_class, c.later_fetched_at
  FROM evidence_drift_flags f
  JOIN evidence_drift_classifications c ON c.id = f.classification_id
 WHERE f.dependant_kind = 'signal' AND f.dependant_id = ANY($1::text[])
 ORDER BY c.later_fetched_at, f.id
"""

#: D-2: the same, with the change's latest assessment (0253).
_MARK_ASSESSED_SQL = """
SELECT f.dependant_id AS signal_id, f.drift_class, c.later_fetched_at,
       a.change_type, a.component_types, a.acknowledged, a.summary
  FROM evidence_drift_flags f
  JOIN evidence_drift_classifications c ON c.id = f.classification_id
  LEFT JOIN LATERAL (SELECT a.change_type, a.component_types, a.acknowledged, a.summary
                       FROM evidence_drift_assessments a WHERE a.classification_id = c.id
                      ORDER BY a.assessed_at DESC, a.id DESC LIMIT 1) a ON true
 WHERE f.dependant_kind = 'signal' AND f.dependant_id = ANY($1::text[])
 ORDER BY c.later_fetched_at, f.id
"""

#: Characters of an assessment summary carried on the line.
MARK_SUMMARY_MAX_CHARS = 160


def slice_drift_mark_enabled() -> bool:
    return os.environ.get(SLICE_DRIFT_MARK_ENV, "").strip().lower() in {"1", "on", "true", "yes"}


async def annotate_source_drift(conn: Any, rows: Sequence[dict[str, Any]]) -> int:
    """Stamp the rows whose signal has a drift flag (the EARLIEST change
    wins: that is the first moment the text the desk reads stopped being the
    source's). Returns how many rows were marked; never raises (a failed
    read marks nothing)."""
    ids = [str(r["id"]) for r in rows if r.get("id") is not None]
    if not ids:
        return 0
    found = None
    for sql in (_MARK_ASSESSED_SQL, _MARK_SQL):
        try:
            # A savepoint when the caller holds a transaction: a failed read
            # (0252/0253 not applied yet) must not abort the caller's
            # transaction. Without 0253 the D-1 read still marks.
            async with conn.transaction():
                found = await conn.fetch(sql, ids)
            break
        except Exception:  # noqa: BLE001 — degrade to the next read, then unmarked
            continue
    if found is None:
        return 0
    first: dict[str, Mapping[str, Any]] = {}
    for f in found:
        first.setdefault(str(f["signal_id"]), f)
    marked = 0
    for r in rows:
        f = first.get(str(r.get("id")))
        if f is None:
            continue
        when = f["later_fetched_at"]
        mark = {
            "date": when.date().isoformat() if hasattr(when, "date") else str(when)[:10],
            "class": str(f["drift_class"]),
        }
        keys = f.keys() if hasattr(f, "keys") else ()
        if "change_type" in keys and f["change_type"] and f["summary"]:
            mark["assessment"] = {"type": str(f["change_type"]),
                                  "components": list(f["component_types"] or []),
                                  "acknowledged": str(f["acknowledged"]), "summary": str(f["summary"])}
        r[SOURCE_DRIFT_ROW_KEY] = mark
        marked += 1
    return marked


def source_drift_line(row: Mapping[str, Any]) -> str | None:
    """The row's ``source text changed`` line, else ``None``."""
    mark = row.get(SOURCE_DRIFT_ROW_KEY)
    if not isinstance(mark, Mapping) or not mark.get("date") or not mark.get("class"):
        return None
    a = mark.get("assessment")
    if isinstance(a, Mapping) and a.get("summary") and a.get("type"):
        summary = " ".join(str(a["summary"]).split())
        if len(summary) > MARK_SUMMARY_MAX_CHARS:
            summary = summary[: MARK_SUMMARY_MAX_CHARS - 1] + "…"
        kind = str(a["type"]).replace("_", " ")
        comps = [str(c).replace("_", " ") for c in (a.get("components") or [])]
        if a["type"] == "mixed" and comps:
            kind = "mixed: " + " + ".join(comps)
        ack = str(a.get("acknowledged") or "silent").replace("_", " ")
        return f"    source text changed on {mark['date']}: {summary} ({kind}, {ack})"
    return f"    source text changed on {mark['date']}: {str(mark['class']).replace('_', ' ')}"

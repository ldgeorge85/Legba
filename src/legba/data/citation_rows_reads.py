# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""What a finding's sentences rest on, for the Record sheet (lane DM-A).

:func:`finding_support` answers ``GET /v3/record/finding/{id}/support``. It
PREFERS the rows (``finding_sentences`` / ``citation_support`` /
``citation_unresolved``, migration ``20261115T1828_citations_as_rows``) and
FALLS BACK to today's path when a finding has none (every finding written
before the dual write, or with it switched off): the citations stored in the
finding's own document, resolved by the same declared rules the writer uses
(``citation_rows.targets_of_entry``), with each signal's bytes labelled by
``byte_path`` as the receipts route labels them. The answer says which path
it took (``basis``: ``support_rows`` / ``citations_json`` / ``none``).

The fallback binds no citation to a sentence (that would need the analyst
package's segmenter, which the slim registry image does not carry); the rows
do. Read-only, bounded (:data:`MAX_SUPPORTS`, :data:`MAX_SENTENCES`), and a
quote is cut to one display span (``display_policy.SPAN_CHARS``). Stdlib-only
apart from the registry declarations: imported by the slim registry image.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from uuid import UUID

from .byte_path import evidence_states
from .citation_rows import target_specs, targets_of_entry
from .display_policy import SPAN_CHARS

__all__ = ["MAX_SENTENCES", "MAX_SUPPORTS", "finding_support"]

MAX_SUPPORTS = 500
MAX_SENTENCES = 400

_TYPED = ("signal_id", "observation_id", "output_id", "situation_id", "event_id",
          "hypothesis_id", "inquiry_id", "legacy_key")

_RECEIPT_SQL = """
SELECT finding_id::text, run_id::text, analyst_id, produced_at, sentences, cited_sentences,
       markers, foreign_markers, supports, unresolved, handed_recorded, handed, origin, backfill_batch
  FROM finding_citation_receipts WHERE finding_id = $1
"""
_SENTENCES_SQL = """
SELECT id, ordinal, char_start, char_end, text, kind, marker_count
  FROM finding_sentences WHERE finding_id = $1 AND marker_count > 0
 ORDER BY ordinal LIMIT $2
"""
_SUPPORT_SQL = f"""
SELECT sentence_ordinal, marker, role, target_kind, via_block, {", ".join(f"{c}::text AS {c}" for c in _TYPED)},
       fetch_version_id, fetch_link_role, bytes_label, bytes_sha256, text_sha256,
       quote, stance, speaker, source_id
  FROM citation_support WHERE finding_id = $1
 ORDER BY sentence_ordinal NULLS LAST, id LIMIT $2
"""
_UNRESOLVED_SQL = """
SELECT sentence_ordinal, marker, reason, ref_kind, ref_value
  FROM citation_unresolved WHERE finding_id = $1 ORDER BY id LIMIT $2
"""
_FINDING_SQL = """
SELECT data->'data'->'citations' AS citations FROM analyst_outputs WHERE id = $1 AND kind = 'finding'
"""


def _iso(v: Any) -> Any:
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else v


def _span(text: Any) -> str | None:
    return str(text)[:SPAN_CHARS] if isinstance(text, str) and text else None


async def finding_support(conn: Any, finding_id: str) -> dict[str, Any]:
    """The Record sheet's support read for one finding."""
    try:
        fid = UUID(str(finding_id))
    except (ValueError, TypeError):
        return {"finding_id": str(finding_id), "found": False, "basis": "none"}
    # The reader may roll before the migration: no tables = today's path.
    has_rows = await conn.fetchval("SELECT to_regclass('public.finding_citation_receipts') IS NOT NULL")
    receipt = await conn.fetchrow(_RECEIPT_SQL, fid) if has_rows else None
    if receipt is not None:
        supports = []
        for r in await conn.fetch(_SUPPORT_SQL, fid, MAX_SUPPORTS):
            target = next((r[c] for c in _TYPED if r[c] is not None), None)
            supports.append({
                "sentence": r["sentence_ordinal"], "marker": r["marker"], "role": r["role"],
                "kind": r["target_kind"], "id": target, "via_block": r["via_block"],
                "fetch_version_id": r["fetch_version_id"], "link_role": r["fetch_link_role"],
                "bytes": r["bytes_label"], "bytes_sha256": r["bytes_sha256"],
                "text_sha256": r["text_sha256"], "quote": _span(r["quote"]),
                "stance": r["stance"], "speaker": r["speaker"], "source_id": r["source_id"],
            })
        sentences = [
            {"ordinal": r["ordinal"], "char_start": r["char_start"], "char_end": r["char_end"],
             "text": r["text"], "kind": r["kind"], "markers": r["marker_count"]}
            for r in await conn.fetch(_SENTENCES_SQL, fid, MAX_SENTENCES)
        ]
        unresolved = [dict(r) for r in await conn.fetch(_UNRESOLVED_SQL, fid, MAX_SUPPORTS)]
        rec = {k: _iso(v) for k, v in dict(receipt).items()}
        return {"finding_id": str(fid), "found": True, "basis": "support_rows", "receipt": rec,
                "sentences": sentences, "supports": supports, "unresolved": unresolved,
                "truncated": int(rec["supports"]) > len(supports)}
    row = await conn.fetchrow(_FINDING_SQL, fid)
    if row is None:
        return {"finding_id": str(fid), "found": False, "basis": "none"}
    citations = row["citations"]
    if isinstance(citations, str):
        import json

        citations = json.loads(citations)
    specs = target_specs()
    supports, unresolved = [], []
    for entry in citations if isinstance(citations, list) else []:
        if not isinstance(entry, Mapping):
            continue
        targets, problems = targets_of_entry(entry, specs)
        marker = entry.get("marker") if isinstance(entry.get("marker"), str) else None
        for t in targets:
            supports.append({"sentence": None, "marker": marker,
                             "role": "restates" if t.restates else "cites",
                             "kind": t.kind, "id": t.key, "via_block": t.via_block})
        unresolved += [{"sentence_ordinal": None, "marker": marker, "reason": p.reason,
                        "ref_kind": p.ref_kind, "ref_value": p.ref_value} for p in problems]
    truncated = len(supports) > MAX_SUPPORTS
    supports = supports[:MAX_SUPPORTS]
    states = await evidence_states(conn, [s["id"] for s in supports if s["kind"] == "signal"])
    for s in supports:
        st = states.get(s["id"]) if s["kind"] == "signal" else None
        if st is not None:
            s.update({"fetch_version_id": st.version_id, "link_role": st.link_role, "bytes": st.label})
    return {"finding_id": str(fid), "found": True, "basis": "citations_json", "receipt": None,
            "sentences": [], "supports": supports, "unresolved": unresolved[:MAX_SUPPORTS],
            "truncated": truncated}

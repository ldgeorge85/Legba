# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Event citations — the v3 P2 ref kind (spec §2.5).

When an analyst cites ``event:<uuid>``, the citation BUILDER expands it at
build time — one ordinary citation entry per contributing signal, each
carrying the signal's real ``signal_id`` and its RAW ``source_text`` — and
stamps ``ref_kind="event"`` + ``event_id`` so the lineage stays visible.
The judge grounds on the expanded signals through the path it already has;
``verify.py`` does not change at all.

What this module deliberately is NOT:

* The event's own ``summary`` is NEVER copied anywhere — not into
  ``source_text``, ``evidence_text`` or ``snippet`` — by any path here or by
  either call site's builder injection. A finding that cites an event is
  graded on the event's member signals, not on prose the platform wrote
  about them.
* ``event`` is NOT a member of ``kinds.GROUNDING_REF_KINDS`` and must never
  become one: every expanded entry carries a ``signal_id``, so
  ``is_grounding_citation`` correctly returns False on it; admitting the
  kind would let an event be cited with ``evidence_text`` set to its own
  summary — the exact rubber-stamp the feature exists to prevent (see the
  comment at the set itself).
* ``journal_entries`` stays out of lineage; events enter it
  (``lineage_api._SUBSTRATE_TABLES``) because a finding's ``derived_from``
  carries the event id and the expanded signal ids both (§2.5 rule 4).

Both gates are env flags, both default OFF/small:

* ``LEGBA_EVENT_CITATIONS`` — nothing expands while off; an ``event:<uuid>``
  token in prose or ``evidence`` is just text, byte-identical to today.
* ``LEGBA_EVENT_EXPANSION_MAX_SIGNALS`` — per-event signal cap (default 4,
  sized against the judge's ``_EVIDENCE_TOTAL_CHARS`` envelope). An event
  with more members stamps ``event_expansion_truncated`` on every emitted
  entry — the same honest-truncation idiom the claim-verdicts ledger uses.
"""
from __future__ import annotations

import os
import re
from typing import Any, Callable, Mapping

EVENT_CITATION_VERSION = "2026-09/p2"

#: Master flag. OFF ⇒ no analyst emits an event citation; the token stays
#: ordinary prose. Only ``1/true/yes/on`` (case-insensitive) turns it on.
EVENT_CITATIONS_ENV = "LEGBA_EVENT_CITATIONS"

#: Per-event expansion cap. Default 4 — the judge's envelope is already
#: bounded, so four richest links is the honest spend.
EVENT_EXPANSION_MAX_ENV = "LEGBA_EVENT_EXPANSION_MAX_SIGNALS"
_DEFAULT_MAX_SIGNALS = 4

#: The ``event:<uuid>`` token — the core ref grammar shared by the unit
#: (``[event:<uuid>]`` / bare) and composition (``[[event:<uuid>]]``) call
#: sites, which wrap it in their own bracket conventions. Exported so the
#: two sites can never drift on what counts as a uuid.
EVENT_ID_RE = re.compile(
    r"event:([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
)

#: Ranked member read — richest link first, then newest evidence. The +1
#: probe row is how the caller learns the event had MORE members than the
#: cap (see ``event_expansion_truncated``) without a second COUNT query.
_EXPANSION_SQL = """
    SELECT s.id, s.payload, s.canonical_url, s.source_id,
           l.relevance, l.linked_at
      FROM signal_event_links l
      JOIN signals s ON s.id = l.signal_id
     WHERE l.event_id = $1
     ORDER BY l.relevance DESC, l.linked_at DESC
     LIMIT $2
"""


def event_citations_enabled() -> bool:
    """The ONE read site for ``LEGBA_EVENT_CITATIONS`` (off by default)."""
    return os.environ.get(EVENT_CITATIONS_ENV, "").strip().lower() in (
        "1", "true", "yes", "on",
    )


def event_expansion_max_signals() -> int:
    """The ONE read site for ``LEGBA_EVENT_EXPANSION_MAX_SIGNALS`` (default
    4, floor 1 — a cap of 0 would mean "cite an event, expand nothing",
    which is the unresolved shape, not a knob)."""
    try:
        return max(1, int(os.environ.get(EVENT_EXPANSION_MAX_ENV, "") or 4))
    except (TypeError, ValueError):
        return _DEFAULT_MAX_SIGNALS


# ---------------------------------------------------------------------------
# The default entry builder — the MIRRORED source_text precedence.
# ---------------------------------------------------------------------------
#
# ``_citation_entry`` (inline_target) owns the canonical RAW-source
# precedence — archived_text → raw_body → text → body → content →
# content_text → summary → description, ``distilled_body`` DELIBERATELY
# excluded so the judge grounds on the real article and catches a
# summarizer hallucination. Both call sites SHOULD pass that builder in so
# the full treatment (archived-article prose, GDELT record projection,
# markup cleaning) applies verbatim; this default exists so the leaf stays
# stdlib+asyncpg-safe — the provenance package cannot import the analysts
# package without closing the import cycle the verify layer documents.
# The chain is mirrored, not imported — same discipline citation_markers
# uses for its verify-side copy.
_SOURCE_TEXT_CHARS = 3200


def _default_entry(
    *, signal_id: str, title: Any, source: Any, source_id: Any,
    fields: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Minimal honest entry when no ``build_entry`` was injected: the same
    RAW-source field precedence and the same 3,200-char cap, minus the
    analyst-side niceties (snippet, GDELT prose, markup cleaning) the real
    ``_citation_entry`` adds. A non-str body field (a GDELT ``raw_body``
    mapping) is SKIPPED to the next precedence level, never ``str()``-ed —
    ``str()`` on the 61-column CAMEO record is the exact judge-noise the
    trust boundary comment in ``_citation_entry`` warns about.
    """
    source_text = ""
    source_truncated = False
    if isinstance(fields, Mapping):
        raw_source = ""
        for key in (
            "archived_text", "raw_body", "text", "body",
            "content", "content_text", "summary", "description",
        ):
            candidate = fields.get(key)
            if isinstance(candidate, str) and candidate.strip():
                raw_source = candidate
                break
        cleaned = " ".join(raw_source.split())
        source_truncated = len(cleaned) > _SOURCE_TEXT_CHARS
        source_text = cleaned[:_SOURCE_TEXT_CHARS]
    return {
        "signal_id": signal_id,
        "title": str(title) if title is not None else None,
        "source": str(source) if source else None,
        "source_id": str(source_id) if source_id else None,
        "source_text": source_text or None,
        "source_truncated": source_truncated,
    }


async def expand_event_citation(
    conn: Any,
    event_id: Any,
    *,
    start_ordinal: int,
    max_signals: int,
    build_entry: Callable[..., dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Expand one ``event:<uuid>`` ref into per-signal citation entries.

    One entry per contributing signal — ranked ``relevance DESC,
    ``linked_at DESC`` over ``signal_event_links``, capped at
    ``max_signals`` — each an ordinary citation dict:

      * ``ordinal`` — ``start_ordinal + i``; the caller stamps its own
        marker grammar (``[K]`` for the unit path, ``[[ref:K]]`` for the
        composition path) and rewrites the prose token to it. The marker is
        deliberately NOT stamped here — the leaf is grammar-free.
      * ``signal_id`` / ``title`` / ``source`` / ``source_id`` /
        ``source_text`` / ``source_truncated`` — through ``build_entry``
        when injected (the REAL ``_citation_entry`` precedence — the
        signature is the same ``signal_id/title/source/source_id/fields``
        kwargs it takes) else the mirrored default above. The event's
        ``summary`` is never consulted.
      * ``ref_kind="event"`` + ``event_id`` — the lineage back to the cited
        event row.
      * ``event_expansion_truncated=True`` on EVERY emitted entry when the
        event had more linked signals than the cap (the +1 probe).

    ``conn`` is duck-typed — anything with ``.fetch`` (an asyncpg Pool or a
    Connection) works, matching the house idiom. An empty event (unknown id
    or no linked signals) returns ``[]``; the CALLER decides how the
    unresolvable ref is recorded (a marker with no backing entry), the
    leaf never fabricates one.
    """
    rows = await conn.fetch(
        _EXPANSION_SQL, event_id, int(max_signals) + 1,
    )
    truncated = len(rows) > max_signals
    emit = rows[:max_signals]
    out: list[dict[str, Any]] = []
    for i, row in enumerate(emit):
        fields = row["payload"]
        if not isinstance(fields, Mapping):
            fields = {}
        entry = (
            build_entry(
                signal_id=str(row["id"]),
                title=fields.get("title"),
                source=row["canonical_url"],
                source_id=row["source_id"],
                fields=fields,
            )
            if build_entry is not None
            else _default_entry(
                signal_id=str(row["id"]),
                title=fields.get("title"),
                source=row["canonical_url"],
                source_id=row["source_id"],
                fields=fields,
            )
        )
        entry["ordinal"] = start_ordinal + i
        entry["ref_kind"] = "event"
        entry["event_id"] = str(event_id)
        if truncated:
            entry["event_expansion_truncated"] = True
        out.append(entry)
    return out


__all__ = [
    "EVENT_CITATION_VERSION",
    "EVENT_CITATIONS_ENV",
    "EVENT_EXPANSION_MAX_ENV",
    "EVENT_ID_RE",
    "event_citations_enabled",
    "event_expansion_max_signals",
    "expand_event_citation",
]

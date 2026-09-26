# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""FINDINGS JUDGMENT PROJECTION — the CHECKED band's citation reduction
(7b-v, ``registry.substrate_reads_api``'s ``GET /findings?fields=judgment``).

One function, in a module with NOTHING but the standard library behind it:

    project_citations(raw)   a finding's citation array, reduced to the
                              seven leaves a reader needs to go argue with a
                              flagged sentence — never the full stored entry.

── WHAT THE JUDGMENT WEIGHT EXISTS TO SHED ─────────────────────────────────

Measured live 2026-09-24: the Morning Read's CHECKED band fetches 200 rows of
``/findings`` on open and every five minutes — 5.5 MB, 72% of it the finding's
own ``data`` (the assembly's quoted blocks, spans and signals; the band reads
NONE of it). The route's ``fields=judgment`` weight (see
``substrate_reads_api.py``) answers with the verify verdict WHOLE
(``verification``, including its own ``unsupported_spans`` — the thing the
band exists to show) plus enough about each citation to render "cited [N]" and
say who is behind it — never the finding's full ``data``, ``derived_from`` or
``body``.

A stored citation entry carries far more than that (``evidence_text``,
``title``, ``tier``, ``derived_from``, ``effective_confidence``, the bare
``ref_id``/``signal_id``, …) — see ``composition_citations.py`` and
``event_citations.py`` for the producers, and ``registry.export_api``'s own
``_citation_export_entry`` for a RICHER reduction built for a different
reader (the export document, which re-resolves a signal citation live). This
module owns the judgment weight's SMALLER, read-only reduction — the same
seven leaves for every citation, whatever kind it is, so the route never has
to sniff a shape to answer "no more data than these fields".

── WHY THIS IS ITS OWN FILE, AND WHY IT IS A LEAF ──────────────────────────

The registry image is deliberately SLIM: it carries no runtime analyst
dependencies. This module never imports a sibling ``legba`` package — not
even a convenience one — so the route that uses it can never drag the
analyst/runtime stack in behind it. ``tests/data_pkg/test_substrate_reads_api
.py`` proves the key set is pinned and that the leaf imports nothing but the
standard library.
"""
from __future__ import annotations

import json
from typing import Any

#: The exact key set every judgment-weight citation entry carries — no more,
#: no less. A finding may cite a signal, a composition sub-claim, or a desk
#: grounding block (three different stored shapes, see the module docstring);
#: this projection is deliberately shape-agnostic and reads the SAME seven
#: keys off whichever one it is handed, defaulting an absent leaf rather than
#: omitting the key (the response stays self-describing).
JUDGMENT_FIELDS: frozenset[str] = frozenset(
    {
        "ordinal",
        "source",
        "source_id",
        "produced_at",
        "single_source",
        "wire_folded",
        "marker_class",
    }
)


def _as_citation_list(raw: Any) -> list[Any]:
    """Coerce a finding's raw citations value to a list, or ``[]``.

    The SQL side hands this whatever the ``citations_raw`` column decoded to
    — a Python list (the pool's jsonb codec, the common case), a raw JSON
    string (a codec-less connection), or ``None`` (no citations key at any
    nesting level). Anything else is treated as absent rather than raising —
    this is a read surface, never a place to 500 on a malformed row.
    """
    if raw is None:
        return []
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return []
    return raw if isinstance(raw, list) else []


def _ordinal(value: Any) -> int | None:
    """A citation's ordinal, or ``None``. Never coerced from a string — an
    ordinal this reduction had to guess is not an ordinal (mirrors
    ``registry.export_api._ordinal``)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _text_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _reduce_one(entry: Any) -> dict[str, Any] | None:
    """One stored citation entry -> the judgment key set, or ``None`` when
    the entry is not itself a mapping (never fabricated from a bare id)."""
    if not isinstance(entry, dict):
        return None
    return {
        "ordinal": _ordinal(entry.get("ordinal")),
        "source": _text_or_none(entry.get("source")),
        "source_id": _text_or_none(entry.get("source_id")),
        "produced_at": _text_or_none(entry.get("produced_at")),
        # Stamped ONLY when true by every producer (composition_citations.py,
        # source_independence.py) — absence means "not computed" as often as
        # "false", so this reduction folds both to the honest boolean the UI
        # renders on, never fabricating a positive.
        "single_source": bool(entry.get("single_source", False)),
        "wire_folded": bool(entry.get("wire_folded", False)),
        "marker_class": _text_or_none(entry.get("marker_class")),
    }


def project_citations(raw: Any) -> list[dict[str, Any]]:
    """A finding's citation array, reduced to :data:`JUDGMENT_FIELDS`.

    ``raw`` is already the finding's own citations array — the caller (the
    route's SQL) has already resolved the nested-vs-flat storage shape
    (``data.data.citations`` vs ``data.citations``, see ``export_api
    ._citation_list``'s docstring for why both exist); this leaf never
    touches the finding's full ``data`` blob, only the array it was handed.
    """
    return [
        reduced
        for entry in _as_citation_list(raw)
        if (reduced := _reduce_one(entry)) is not None
    ]

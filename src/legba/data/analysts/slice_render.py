# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Slice rendering for ``inline_target`` — extracted at the regrowth-gate seam
(2026-08-05, the W1-D x v-voice x r-smalls merge pushed inline_target past its
ceiling; each branch fit alone, the sum did not).

Owns the per-row signal render, the Phase-V dated header, the R9 CAMEO marking,
the wire-pair ``carried_by`` annotation, and the per-clean render RECEIPT
(``_slice_render_stats`` — every counter it emits is a statement about what the
render did to a row, so it belongs beside the render, not beside the caller).
``inline_target`` re-exports every name, so callers and tests are untouched.
``_signal_title`` / ``_signal_body`` stay in ``inline_target`` (they serve
non-render paths too) and are imported back here — one direction only at call
time, no import cycle at module load.
"""
from __future__ import annotations

import datetime
from typing import Any, Mapping

from ..claim_support import claim_of, claim_row_label
from ..evidence_drift_slice import SOURCE_DRIFT_LEGEND, source_drift_line
from .wire_pair_collapse import WIRE_COLLAPSE_ROW_KEY, carried_by_line


def _it():
    """Late import of inline_target for the two shared text helpers —
    call-time only, so module load stays acyclic."""
    from . import inline_target
    return inline_target

#: Stamped after the TITLE of a GDELT/CAMEO row (R9).
#:
#: A GDELT title is not a headline — it is synthesized by our own handler from
#: CAMEO columns (``gdelt_files.synthesize_title``: "<Actor1> <-> <Actor2>:
#: <root label> in <location>"), and the root labels are taxonomy terms, not
#: English verbs. "PRISON: coerce in North Korea" means CAMEO root 17 was coded
#: against an actor whose code is PRISON; it does not mean a prison coerced
#: anybody. Rendered bare, the model reads the taxonomy as narrative and writes
#: sentences like "the Acting Attorney General reduced relations in New York" —
#: fluent, cited, and about nothing that happened.
#:
#: ``_gdelt_prose`` already caveats the BODY, but two paths defeat it: ~28% of
#: GDELT rows also carry an archived article, which takes body precedence and
#: drops the caveat entirely, and the title line sits ABOVE the body either
#: way. So the mark goes on the title, where the misreading starts.
#:
#: Terse on purpose — it is repeated per row inside the input-token budget, and
#: :func:`_render_user_prompt` explains it once in the header rather than
#: paying for the explanation on every line.
_CAMEO_TITLE_TAG = " [CAMEO event code]"

#: The one-line legend, emitted only when the slice actually contains a coded
#: row. Unlike the per-row tag this is NOT counted by the ORIENT estimator
#: (which budgets per-row via :func:`_render_signal`); it is a single constant
#: line against a 32k-token input budget, so the under-count is bounded and
#: fixed rather than proportional to the slice.
_CAMEO_LEGEND = (
    "Note: titles tagged [CAMEO event code] are GDELT machine codings of a "
    "news report, not headlines — the words in them are CAMEO taxonomy labels "
    "and actor codes, not a description of what happened. Do not paraphrase "
    "such a title as narrative prose.\n"
)


#: Lane N-X — the one-line legend for incidents at sea, emitted only when the
#: slice carries one (so an in-country slice renders byte-identically).
_AT_SEA_LEGEND = (
    "Note: rows with a location= line marked 'at sea' are incidents reported "
    "at sea, filed under no country. The countries on that line are the "
    "nearest coasts, inferred from the reported coordinates with the distance "
    "offshore. Never describe such an incident as having happened in, or "
    "inside the territory of, one of those countries.\n"
)


def _names_list(names: list[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " or " + names[-1]


def at_sea_line(row: Mapping[str, Any]) -> str | None:
    """The ``location=`` line for an unfiled at-sea row, else ``None``.

    "at sea (Strait of Hormuz), about 16 km off Oman, 46 km off United Arab
    Emirates, 67 km off Iran (nearest coasts inferred from the reported
    coordinates; not an event in Oman, United Arab Emirates or Iran)".
    Only for a row the ingest left unfiled (empty ``geo``) with
    ``payload.geo.at_sea`` — a filed row never gets one."""
    if row.get("geo"):
        return None
    data = row.get("data")
    geo = data.get("geo") if isinstance(data, Mapping) else None
    if not isinstance(geo, Mapping) or geo.get("at_sea") is not True:
        return None
    from .._polity_match import home_country_name

    place = geo.get("location_name")
    head = f"at sea ({place})" if isinstance(place, str) and place.strip() else "at sea"
    offs: list[str] = []
    names: list[str] = []
    for entry in geo.get("inferred") or []:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("iso2"), str):
            continue
        iso2 = entry["iso2"]
        name = home_country_name(iso2) or iso2
        km = entry.get("distance_km")
        dist = f"{round(km)} km" if isinstance(km, (int, float)) else "near"
        offs.append(f"{dist} off {name}")
        names.append(name)
    if not offs:
        return f"    location={head}, no coast within 200 km (not an event in any country)"
    return (
        f"    location={head}, about {', '.join(offs)} (nearest coasts inferred "
        f"from the reported coordinates; not an event in {_names_list(names)})"
    )


def _row_is_cameo_coded(row: Mapping[str, Any]) -> bool:
    """True when this slice row's title is a synthesized CAMEO code label."""
    data = row.get("data")
    return isinstance(data, Mapping) and _it()._gdelt_event_record(data) is not None


def _render_signal(idx: int, row: Mapping[str, Any]) -> str:
    """Render ONE signal block (title + provenance + truncated snippet).

    Shared by the user-prompt renderer and the ORIENT token-budget estimator so
    the token accounting matches the bytes actually sent to the LLM.

    A row that SURVIVED a wire-pair collapse (``_orient`` folded one syndicated
    story's several mastheads into this single numbered signal) also carries a
    ``carried_by=`` line between its provenance and its snippet — the count of
    mastheads that ran it, and the explicit statement that this is reach rather
    than independent corroboration. Because the estimator renders through this
    same function, that line is inside the INPUT-token budget like any other.
    """
    data = row.get("data")
    title = (_it()._signal_title(row) or "(untitled)")[:_it()._MAX_TITLE_CHARS]
    # Tag AFTER the truncation so the marker can never be the thing that gets
    # cut — a silently-dropped tag would put us back where we started.
    if _row_is_cameo_coded(row):
        title += _CAMEO_TITLE_TAG
    produced_at = row.get("produced_at")
    source = row.get("source_url") or ""
    # `produced_at` is INGESTION (fetch) time, NOT the event date — label it
    # honestly and surface the article's own published date when present, so the
    # LLM can't read fetch-time as event-time (the world-assessor temporal-collapse
    # class: a fresh June article about a Feb event got dated "today").
    published_at = data.get("published_at") if isinstance(data, dict) else None
    # ONE body precedence, shared with the citation-evidence snippet, so the
    # verify judge's WORKING text is byte-identical to what the analyst read.
    snippet = _it()._signal_body(row).text
    published_str = f" published={published_at}" if published_at else ""
    lines = [
        f"[{idx}] {title}",
        f"    ingested={produced_at}{published_str} source={source}",
    ]
    # C-1: a CLAIM row (payload.claim, stamped at ingest) is labelled as one —
    # claimant and the only sentence it can support. Claim rows only; every
    # other row renders byte-identically.
    claim = claim_of(data) if isinstance(data, Mapping) else None
    if claim is not None:
        lines.append(f"    {claim_row_label(claim)}")
    sea = at_sea_line(row)
    if sea is not None:
        lines.append(sea)
    # Lane D-1: only a row the slice reader stamped (LEGBA_SLICE_SOURCE_DRIFT_MARK,
    # off by default) carries the line; an unstamped row renders byte-identically.
    drift = source_drift_line(row)
    if drift is not None:
        lines.append(drift)
    marker = row.get(WIRE_COLLAPSE_ROW_KEY)
    if isinstance(marker, Mapping):
        lines.append(carried_by_line(marker))
    lines.append(f"    snippet={snippet}")
    return "\n".join(lines)


def _slice_render_stats(sliced: list[Mapping[str, Any]]) -> dict[str, int]:
    """Per-clean receipt for the rows that actually reached the prompt.

    Lives HERE, next to the render it describes: every counter is a statement
    about what :func:`_render_signal` did to a row, and reading them off the
    body-kind resolver is the same walk the renderer makes.

    One counter per QW1-A render clean, so an operator reading an
    ``analyst_traces`` ORIENT step can see what the renderer did to this slice
    instead of inferring it from prompt bytes:

      ``gdelt_prosed``          CAMEO records rendered as one prose line
                                instead of a stringified 61-column dict.
      ``untranslated_marked``   non-Latin bodies replaced by the honest
                                ``[body untranslated: <lang>]`` marker.
      ``full_body_rows``        rows rendering a SUBSTANTIVE body (distilled /
                                translated / message / archived / raw) — the
                                content the teaser used to crowd out.
      ``teaser_rows``           rows still on the thin summary/description tail.
      ``empty_body_rows``       rows kept for their headline alone (a real
                                title with no body is evidence, not a dead row).
      ``structures_collapsed``  duplicate graph-structure pseudo-signals folded
                                away by the slice reader (see
                                ``actor_substrate_slice._collapse_structure_items``).

    ``dropped_dead_rows`` and ``wire_copies_collapsed`` are stamped by
    ``inline_target._orient`` itself — both count rows that never made it into
    ``sliced``, so neither can be recovered by walking the rows that did.
    """
    counts: dict[str, int] = {
        "gdelt_prosed": 0,
        "untranslated_marked": 0,
        "full_body_rows": 0,
        "teaser_rows": 0,
        "empty_body_rows": 0,
        "structures_collapsed": 0,
    }
    for row in sliced:
        kind = _it()._signal_body(row).kind
        if kind == "gdelt_prose":
            counts["gdelt_prosed"] += 1
        elif kind == "untranslated":
            counts["untranslated_marked"] += 1
        elif kind == "empty":
            counts["empty_body_rows"] += 1
        if kind in _it()._FULL_BODY_KINDS:
            counts["full_body_rows"] += 1
        elif kind == "teaser":
            counts["teaser_rows"] += 1
        data = row.get("data")
        if isinstance(data, Mapping):
            collapsed = data.get("duplicates_collapsed")
            if isinstance(collapsed, int) and collapsed > 0:
                counts["structures_collapsed"] += collapsed
    return counts


def _format_window(hours: int | None) -> str | None:
    """A slice window a reader can say out loud, or ``None`` when unknown.

    Never guesses: an unresolvable window omits the header line entirely rather
    than printing a default the descriptor may not actually use. That silence is
    the honest state — the D8a defect this exists to prevent was a prompt
    asserting a 24h window over a 72h slice, and a fabricated "24h" here would
    be the same lie one layer down.
    """
    if not isinstance(hours, int) or isinstance(hours, bool) or hours <= 0:
        return None
    if hours % 24 == 0 and hours >= 24:
        days = hours // 24
        return f"{hours}h ({days} day{'s' if days != 1 else ''})"
    return f"{hours}h"


def _render_user_prompt(
    inputs: list[Mapping[str, Any]],
    target_id: str | None,
    *,
    run_date: str | None = None,
    window_hours: int | None = None,
    spread_block: str | None = None,
    feed_roster: str | None = None,
) -> str:
    """Render the (already ORIENTed) substrate slice into a user prompt.

    Each signal carries title + provenance + a snippet truncated to
    ``_it()._MAX_SNIPPET_CHARS``; the slice itself is bounded by the INPUT-token
    budget in :func:`_orient`.

    PHASE-V D1/D8a — the header carries the READ'S OWN COORDINATES: the run
    date, the slice window, and the signal count. Before this the header was
    two lines (target + count) and the model had no licensed anchor for "when"
    at all, which is why 86% of the analytic corpus ran undated and why one
    unit's prompt could claim a 24h slice over 72h of data for weeks without
    anything noticing. Three properties make this the right place for it:

      * The window is DERIVED, never written down. It arrives from the
        descriptor's own ``subscription.time_window`` via
        ``options['slice_window_hours']`` — the SAME value
        ``actor_substrate_slice.resolve_slice_window_hours`` uses to cut the
        slice — so the prompt cannot drift from the query that built it. A
        descriptor prompt that states its own window in prose can; that is
        exactly the bug.
      * It is RENDERED TEXT, so the as-of line the grounding clause asks for is
        a COPY, not an assertion. The faithfulness judge grades against the same
        bytes, so dating a read ADDS verifiable surface instead of risk.
      * ``run_date`` defaults to today UTC, matching the date the grounding
        preamble stamps on its AUTHORITATIVE CURRENT CONTEXT header, so the two
        dated anchors in one prompt agree. Injectable for deterministic tests.

    Absent values degrade to absent LINES (never a fabricated default), so a
    caller that wires neither renders the pre-Phase-V header plus a run date.

    ``spread_block`` (lane narrative, Program 7 piece 7e) is the
    ``narrative_coordination`` unit's pre-computed SPREAD BLOCK text
    (:func:`legba.data.analysts.spread_block.build_spread_block`) — a
    deterministic, code-computed statement about cross-source near-verbatim
    reuse, synchrony and class mix, cited by the SAME ``[N]`` ordinals the
    signal blocks below carry. ``None`` (every caller but that one unit) adds
    NOTHING to the header — byte-identical to the pre-existing render, same
    contract as ``window_hours``. When the caller DOES engage it, the line
    renders even when no reuse was found (an explicit "none found" line, not
    a silently-omitted one) so the model always reads the same block shape.
    """
    header_lines = [f"Target: {target_id or 'unspecified'}"]
    header_lines.append(
        "Run date (as-of): "
        + (run_date or datetime.datetime.now(tz=datetime.timezone.utc).date().isoformat())
    )
    window = _format_window(window_hours)
    if window is not None:
        header_lines.append(f"Slice window: trailing {window} to the run date")
    header_lines.append(f"Number of signals: {len(inputs)}")
    # R9 CAMEO legend rides the same header (merge of r-smalls x v-voice): the
    # machine-coded-row marker must survive the Phase-V header restructure.
    if any(_row_is_cameo_coded(r) for r in inputs):
        header_lines.append(_CAMEO_LEGEND.strip("\n"))
    if any(at_sea_line(r) is not None for r in inputs):
        header_lines.append(_AT_SEA_LEGEND.strip("\n"))
    if any(source_drift_line(r) is not None for r in inputs):
        header_lines.append(SOURCE_DRIFT_LEGEND.strip("\n"))
    if spread_block is not None:
        header_lines.append(spread_block)
    # Lane K-2 — a desk's declared FEED ROSTER (feed_roster_block.py): per feed,
    # the rows it collected in this window and the ordinals it supplied. None
    # (every descriptor without ``method.options.feed_roster``) adds nothing.
    if feed_roster is not None:
        header_lines.append(feed_roster)
    header = "\n".join(header_lines) + "\n\n"
    body_lines = [_render_signal(i, row) for i, row in enumerate(inputs, start=1)]
    return header + "\n".join(body_lines)

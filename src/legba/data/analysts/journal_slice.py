# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B0 (JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §6, the T2.2 seam taken early)
— the journal's priming-slice SELECTION machinery, extracted verbatim from
``journal_assessor.py`` ahead of the B1 (T1.2 instrument label) and B2 (T1.4
routine label) lanes that both add row-labelling behavior here next. Behavior
is byte-identical to the pre-extraction code — same sort keys, same floor
logic, same docstrings — and ``journal_assessor`` re-exports every name, so
every existing import path (direct, or via the ``journal_assessor`` module
attribute the way ``tests/data_pkg/test_salience_consumption_s2.py`` reaches
``ja._select_journal_slice`` / ``ja._salience_ordered`` / ``ja._JOURNAL_RENDER_CAP``)
keeps working unchanged.

Moved: ``_slice_recency_key``, ``_JOURNAL_RENDER_CAP``, ``_JOURNAL_FRESH_RESERVE``,
``_salience_ordered`` and ``_select_journal_slice`` — the whole S-2a
consequence-first, freshness-floored slice pick.

Left behind in ``journal_assessor.py``: ``_salience_tag`` / ``_untranslated_tag`` /
``_row_payload`` / ``_row_title`` / ``_row_lang`` (the per-row RENDER helpers read
by all four tier prompts, not by selection) and the four ``_render_*_user_prompt``
functions themselves.
"""

from __future__ import annotations

import re
from typing import Any, Mapping


def _slice_recency_key(row: Mapping[str, Any]) -> str:
    """ISO-8601 recency string for a slice row (the S-2a tertiary sort tiebreak).

    Coerce to a string so recency can never hard-fail the sort under a mixed
    tuple, and so newest sorts FIRST under ``reverse=True`` (ISO sorts
    chronologically)."""
    v = row.get("produced_at")
    if v is None:
        v = row.get("fetched_at")
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    iso = getattr(v, "isoformat", None)
    return iso() if callable(iso) else str(v)


_JOURNAL_RENDER_CAP = 60        # rows rendered into the priming slice
_JOURNAL_FRESH_RESERVE = 12     # tail slots guaranteed for the FRESHEST rows


def _salience_ordered(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """S-2a: order the priming slice by CONSEQUENCE, not recency.

    Sort key = ``salience_sort_key`` = (magnitude, authority_rank), DESC — so the
    highest-consequence signal leads and ties break to the more authoritative
    source (the Graham tabloid-frame guard: a wire report outranks adversary
    state_media at equal magnitude). Crucially this is a STABLE sort on the
    salience key ALONE — NO recency tiebreak — so rows with equal (or unscored,
    ``(-1.0, 0)``) salience keep the reader's DELIVERED order. That matters
    because the journal's global slice is per-source DIVERSITY-CAPPED upstream
    (``_diversify_by_source``), NOT pure ``fetched_at DESC``; a recency tiebreak
    would collapse the unscored tail back to pure recency and re-let a firehose
    source monopolize the window. When NOTHING is scored yet, the stable sort is
    a no-op → the delivered (diversity) order is returned UNCHANGED."""
    from .signal_salience import salience_sort_key

    return sorted(
        inputs, key=lambda r: salience_sort_key(r.get("salience")), reverse=True
    )


def _select_journal_slice(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """S-2a: pick + order the ≤``_JOURNAL_RENDER_CAP`` rows to render.

    Consequence LEADS (``_salience_ordered``), but the tail is RESERVED for the
    freshest delivered rows so a breaking event ingested AFTER the salience
    sweep's last tick (still ``salience IS NULL`` → magnitude ``-1.0`` → sorts
    below every scored row) is never truncated out of the narrator's window by
    the ``[:cap]`` cut. Without this floor a window with >cap scored (many of
    them routine, magnitude 0.1-0.3) rows would bury a fresh, unscored,
    high-consequence signal past the cut — the recency-starvation inverse of the
    tabloid-frame bug. The floor draws from the ALREADY diversity-capped
    ``inputs``, so it can't re-introduce firehose monopoly. When nothing is
    scored, or the slice fits, the plain salience order is returned unchanged."""
    ordered = _salience_ordered(inputs)
    from .signal_salience import magnitude_of

    n_scored = sum(1 for r in inputs if magnitude_of(r.get("salience")) >= 0.0)
    if n_scored == 0 or len(ordered) <= _JOURNAL_RENDER_CAP:
        return ordered[:_JOURNAL_RENDER_CAP]
    head_n = _JOURNAL_RENDER_CAP - _JOURNAL_FRESH_RESERVE
    head = ordered[:head_n]
    head_ids = {id(r) for r in head}
    tail: list[dict[str, Any]] = []
    for r in sorted(inputs, key=_slice_recency_key, reverse=True):
        if id(r) in head_ids:
            continue
        tail.append(r)
        if len(tail) >= _JOURNAL_FRESH_RESERVE:
            break
    return head + tail


# ---------------------------------------------------------------------------
# B1 (T1.2, JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3 P4 / §6) — instrument
# rows: slice rows that are EVENT-CODED instrument output (GDELT's CAMEO pseudo-
# titles), not a report. Labelled here — a pure, composable step OVER
# ``_select_journal_slice``'s output — so B0's byte-identity proof for the
# SELECTOR keeps holding forever: this never touches selection or order, only
# stamps ``row['journal_label']`` onto the rows already chosen.
# ---------------------------------------------------------------------------

# The one instrument source live today (per the census, 4/5 of the required
# calibration hit's refs). A second source with the same CAMEO pseudo-title
# shape would be caught by the title regex below even without a name here.
_INSTRUMENT_SOURCE_ID = "source.gdelt.files"
# CAMEO-style pseudo-title: "POLICE <-> GOVERNMENT: assault" / "PRISON: coerce…"
# — an event coder's generated label, not a human headline (kept identical to
# scripts/journal_connective_census.py's own detector (d), independently
# maintained — the census is a measurement tool, this is the live label).
_INSTRUMENT_TITLE_RE = re.compile(r"^[A-Z][A-Z ]+(?: <-> [A-Z][A-Z ]+)?: ")


def _row_raw_title(row: Mapping[str, Any]) -> str:
    """The row's RAW (untranslated) title — GDELT pseudo-titles are already
    ALL-CAPS ASCII, so this deliberately does NOT prefer ``title_en`` the way
    the render helpers do; it just needs whatever title the row carries."""
    for key in ("data", "payload"):
        v = row.get(key)
        if isinstance(v, Mapping):
            t = v.get("title")
            if isinstance(t, str) and t.strip():
                return t.strip()
    t = row.get("title")
    return t.strip() if isinstance(t, str) else ""


def _is_instrument_row(row: Mapping[str, Any]) -> bool:
    if row.get("source_id") == _INSTRUMENT_SOURCE_ID:
        return True
    return bool(_INSTRUMENT_TITLE_RE.match(_row_raw_title(row)))


# ---------------------------------------------------------------------------
# B2 (T1.4, JOURNAL_CONNECTIVE_AUDIT_PROPOSAL_2026-09-09 §3 P6 / §6) — routine
# products: slice rows from a SCHEDULED statistical-product source class (a
# hazard feed, a seismic/weather catalog, a press-release wire) — a signal of
# change only when its number departs from its own baseline.
#
# Derived read-only (never a live query at label time — this is a STATIC
# allowlist, re-derive by re-running the same query if the source register
# changes) 2026-09-09 from the live source_descriptors register, mirroring
# scripts/journal_connective_census.py's own ``_fetch_routine_allowlist``
# query (kept independently — the census is a measurement tool, this is the
# live label):
#
#   SELECT descriptor_id FROM source_descriptors WHERE is_head AND (
#     descriptor_id = 'source.nws.active_alerts'
#     OR (kind IN ('geojson','json_api') AND body->'scope'->'tags' ? 'hazard')
#     OR descriptor_id LIKE '%.press'
#     OR descriptor_id LIKE '%press_release%')
#
# 11 source_ids, each justified below (one line, why it's a SCHEDULED
# product rather than event-driven reporting):
ROUTINE_PRODUCT_SOURCES: frozenset[str] = frozenset({
    # the floor named by the task — scheduled US weather hazard alerts.
    "source.nws.active_alerts",
    # scheduled seismic-magnitude catalogs (hazard-tagged geojson/json_api) —
    # a periodic statistical product, not a report someone filed.
    "source.usgs.earthquakes_m45",
    "source.emsc.seismic",
    # scheduled multi-hazard event catalog (fires/storms/floods/volcanoes).
    "source.nasa.eonet_events",
    # scheduled disease-surveillance bulletin (WHO), a structured health
    # statistical product, not narrated reporting.
    "source.who.disease_outbreak_news",
    # scheduled press-release wires — an institution's own announcements
    # cadence, not independent reporting on it.
    "source.eia.press",
    "source.federalreserve.press",
    "source.rand.press",
    "source.rbi.press_releases",           # the task's own example
    "source.stategov.press_releases",
    "source.un.press",
})


def _is_routine_row(row: Mapping[str, Any]) -> bool:
    return row.get("source_id") in ROUTINE_PRODUCT_SOURCES


def _labeled_journal_slice(inputs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The render/REFLECT-facing entry point: the SELECTED slice
    (``_select_journal_slice``, unchanged) with ``row['journal_label']``
    stamped in place — ``'instrument'`` for GDELT/event-coded rows,
    ``'routine'`` for a scheduled-product source (checked second — an
    instrument-shaped title always wins), else unset. Idempotent (skips an
    already-labelled row) and mutates the row dicts it's given (the same
    objects ``inputs`` holds), so calling it more than once per run — every
    render tier + the REFLECT honesty step each call it independently — is
    cheap and never re-derives a different answer.

    T2.2: the SELECTOR is now ``journal_clusters.journal_window`` — the one
    place ``LEGBA_JOURNAL_CLUSTER_FIRST`` is read. Flag OFF it calls
    ``_select_journal_slice`` above and returns its list unchanged, so B0's
    byte-identity proof still covers the shipped default path; flag ON the
    cluster block leads the window. Deferred import: ``journal_clusters``
    imports back into this module for the cap/order primitives."""
    from .journal_clusters import journal_window

    rows = journal_window(inputs)
    for row in rows:
        if row.get("journal_label"):
            continue
        if _is_instrument_row(row):
            row["journal_label"] = "instrument"
        elif _is_routine_row(row):
            row["journal_label"] = "routine"
    return rows

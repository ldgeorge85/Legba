# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``collection_gap`` sub-handler — S3-T3 collection-requirements analyst.

A DETERMINISTIC (no-LLM) META analyst on a MONTHLY cadence over the banded
scorecard rows (``kind='scorecard'``; produced by
:mod:`scorecard_producer` — one persisted per-country card whose
``data->'data'->'bands'->'dimensions'`` carries the T1
:func:`scorecard_banding.band_target` verdict). It reads the honest
``insufficient-evidence`` signal the banding engine already computes and turns
it into a forward-looking COLLECTION requirement: which ``desk × dimension``
cells are STARVED, WHY (the aggregated banding ``reason``), and which SOURCE
CLASSES would plausibly feed them.

What "starved" means
--------------------

A cell = one ``(desk, dimension)`` where ``desk`` is the scorecard's
``target_id`` (a country desk) and ``dimension`` is one of the fixed bounded-unit
:data:`scorecard_banding.DIMENSIONS`. A cell is STARVED when it is
``band == 'insufficient-evidence'`` in that desk's CURRENT (latest-in-window)
scorecard — a live gap, not a settled-history one. Over the window (all scorecard
rows for the desk, HEAD + superseded, in the lookback) we ALSO aggregate the
distinct insufficient reasons + how PERSISTENT the starvation is (a cell
insufficient in every card all month is a harder collection gap than a
one-off) — this is the ``insufficient-evidence`` reason aggregation the task asks
for, keyed ``desk × dimension``.

Ranking (why US tops the list)
------------------------------

Cells are ordered so the most-starved DESKS surface first: primary key =
``desk_starved_dims`` DESC (a desk with ALL six dimensions insufficient — an
all-insufficient card — sorts every one of its cells above a desk starved in a
single dimension), then ``insufficient_count`` DESC (persistence), then the desk
id, then the fixed dimension order. So an "US all-insufficient" desk tops the
gap list.

Source classes
--------------

:data:`SOURCE_CLASSES_BY_DIMENSION` is the static collection-doctrine map from a
dimension to the ``source_class`` vocabulary (``reporting`` / ``analysis`` /
``official`` / ``state_media``) that would plausibly feed it. It is a fixed,
auditable table — NEVER model-generated — so a collection manager reading a
starved ``narrative_coordination`` cell sees ``state_media`` named as the
plausible feed, an ``energy_security`` cell sees ``official`` first, etc.

Honesty / idempotency (the findings-feed dedup lesson)
------------------------------------------------------

The "collection requirements" summary is a per-run RECEIPT. It reaches the feed
ONLY when at least one cell is starved:

  * NO starved cell → ``force_trace_only=True`` (trace + no feed row) so an
    idempotent monthly re-run over a fully-fed roster never repeats a "nothing
    starved" row.
  * Gaps found, but the gap set is BYTE-IDENTICAL to the last EMITTED
    collection_gap finding (the same scorecards re-swept) → likewise
    ``force_trace_only=True`` (live path only; degrade-to-EMIT on any dedup
    error). The body is deterministic from the gap set, so a body match == an
    unchanged gap set — the same dedup contract :mod:`indicator_tracker` uses.

``deps=None`` runs the synthetic (no-DB) path: ``inputs`` are pre-shaped
scorecard rows (``target_id`` / ``id`` / ``produced_at`` + either a direct
``dimensions`` map or the persisted ``data`` shape) — used by the unit tests.

R-2 — collection REQUIREMENTS (the Mali fix)
---------------------------------------------

A correctness review found a desk asserting "no observable coercive economic
pressure" while a months-long fuel blockade was heavily reported worldwide —
FAITHFUL to its inputs, wrong about the world, because our sources never
carried the story. ``collection_gap`` already NAMES the starved cell; nothing
turned that into a collection ACTION. This module now also writes durable
``collection_requirements`` rows (migration 0113) — a first-class, queryable,
append-only-content proposal object: "desk X lacks coverage of Y; here is the
evidence; here is what would satisfy it." Extends this organ rather than
minting a new one (the coherence audit's core finding: ~40 organs where ~15
archetypes would do) — no new analyst kind, no new cadence, no new registrar
train; the SAME monthly sweep that computes the gaps proposes against them.

Two origins feed one object, both EXISTING organs:
  * ``collection_gap`` itself — every starved desk×dimension cell (this
    module's own aggregation), evidence = the cell's own current scorecard
    row (``analyst_outputs.kind='scorecard'``) — NOT this handler's own
    monthly rollup finding, whose id does not exist yet at handler time.
  * ``hypotheses.status='source_request'`` — the standing backlog the
    ``request_source`` write tool already fills (an assessor's live "no
    source covers X" flag; S6 review S-1). Evidence = the hypothesis row.

For each, :func:`_attach_candidates` cross-references the EXISTING
``source_descriptors`` pool (any lifecycle state, matched on source_class +
the desk's ISO2 geo when derivable) — "reuse before create": a paused /
retired / draft match is a reactivation candidate; a match that is already
``active`` is an honest "this exists but the cell is still starved" flag (a
quality gap, not a registration gap). A non-active candidate's own registered
URL becomes ``suggested_fetch_url`` — something an operator could sample with
the EXISTING guarded ``web_fetch`` single-URL GET before deciding whether to
reactivate the feed. **Never auto-registers anything** — this module has no
write path to ``source_descriptors``; a proposal is not an activation.

Honesty (constraint): when no known candidate exists at all,
``fillable=False`` + ``unfillable_reason='no_known_feed'`` — the requirement
is still written, never dropped (an unfillable requirement is itself
intelligence about our collection posture).

Idempotent by construction: ``natural_key`` (``collection_gap:<desk>:<dim>``
or ``source_request:<hypothesis id>``) is UNIQUE at the schema layer: a
still-starved cell re-swept next month, or a re-run over the same backlog,
never creates a second row for the same key — checked in bulk before insert
AND backstopped by ``ON CONFLICT (natural_key) DO NOTHING``. Bounded per run
(:data:`_MAX_GAP_REQUIREMENTS_PER_RUN` / :data:`_MAX_SOURCE_REQUEST_REQUIREMENTS_PER_RUN`)
and fully degrade-not-break: any failure in the requirements side-write is
caught + logged and never blocks the primary collection-requirements finding
(the synthetic ``deps=None`` test path skips this side-write entirely, same
as the finding-dedup check above).
"""

from __future__ import annotations

import json
import logging
from collections import Counter, defaultdict
from typing import Any, Mapping, Sequence

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import scorecard_banding

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "collection_gap"

#: The fixed dimensions = the bounded-unit analyst_ids the scorecard bands, in
#: their canonical order (imported so a new unit added to the tower flows through
#: with zero drift here).
DIMENSIONS: tuple[str, ...] = scorecard_banding.DIMENSIONS

#: The banding sentinel for a dimension with no qualifying verified claim.
INSUFFICIENT: str = scorecard_banding.INSUFFICIENT

#: Static collection-doctrine map: dimension → the ``source_class`` vocabulary
#: (S1-T8: reporting / analysis / official / state_media) that would plausibly
#: FEED that dimension, ordered by collection priority. A fixed, auditable table
#: — never model-generated. Keep the keys in sync with :data:`DIMENSIONS`.
SOURCE_CLASSES_BY_DIMENSION: dict[str, tuple[str, ...]] = {
    # Succession / cabinet / gazette signal — official announcements first,
    # wire reporting, then think-tank succession analysis.
    "leadership_transition": ("official", "reporting", "analysis"),
    # Energy statistics / ministry / regulator data first, wire reporting,
    # market analysis.
    "energy_security": ("official", "reporting", "analysis"),
    # Conflict / mobilization signal — wire reporting + official MOD/MFA
    # statements, conflict-event analysis, and adversary state media as framing.
    "escalation": ("reporting", "official", "analysis", "state_media"),
    # Influence / framing — state media is the PRIMARY evidence class here,
    # then reporting + narrative analysis.
    "narrative_coordination": ("state_media", "reporting", "analysis"),
    # Unrest / protest / cohesion — wire reporting + protest-event analysis +
    # official statistics.
    "internal_stability": ("reporting", "analysis", "official"),
    # Force posture / procurement — official MOD data + defense analysis
    # (SIPRI-class) + wire reporting.
    "military_posture": ("official", "analysis", "reporting"),
    # Sanctions / trade / currency coercion — official designations (Treasury/
    # OFAC/EU/UN listings), customs + central-bank/reserve data first, then wire
    # reporting, sanctions/trade analysis, and adversary state media as framing.
    "economic_coercion": ("official", "reporting", "analysis", "state_media"),
}

#: Fallback classes for any dimension not in the doctrine map (defensive — every
#: :data:`DIMENSIONS` entry is mapped above).
_DEFAULT_SOURCE_CLASSES: tuple[str, ...] = ("reporting", "analysis", "official")

#: Only scorecards produced within this many days are aggregated — a monthly
#: cadence with a >month window so a run always sees the full prior month of
#: cards (HEAD + superseded). Override via ``options['window_days']``.
_DEFAULT_WINDOW_DAYS: int = 35

#: Per-run safety cap on how many gap cells ride inside the finding data / body.
_MAX_GAPS_IN_FINDING: int = 500

# ---------------------------------------------------------------------------
# R-2 — collection-requirements caps (bounded, fail-safe by construction)
# ---------------------------------------------------------------------------

#: Per-run cap on NEW collection_requirements rows proposed from starved
#: scorecard cells. Gaps are already priority-ordered (see aggregate_gaps);
#: this trims the tail, it never reorders.
_MAX_GAP_REQUIREMENTS_PER_RUN: int = 50

#: Per-run cap on NEW collection_requirements rows drained from the standing
#: ``source_request`` hypotheses backlog.
_MAX_SOURCE_REQUEST_REQUIREMENTS_PER_RUN: int = 20

#: Per-requirement cap on how many source_descriptors candidates ride in
#: ``candidate_sources`` (an operator triage list, not an exhaustive dump).
_MAX_CANDIDATE_SOURCES: int = 5

#: The ``request_source`` write tool's fixed prose prefix (see
#: ``legba.data.analysts.agency.write_tools.request_source_tool``) — stripped
#: for a cleaner ``topic`` when present; tolerated absent (defensive).
_SOURCE_REQUEST_PREFIX = "Source coverage gap: "


# ---------------------------------------------------------------------------
# Extraction (shared by the live + synthetic paths)
# ---------------------------------------------------------------------------


def _parse_data(raw: Any) -> dict[str, Any]:
    """Normalize a scorecard row's ``data`` to a dict (tolerate a JSONB str)."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _extract_dimensions(row: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    """The per-dimension band verdicts carried by one scorecard row.

    Reads, in priority order:
      1. a direct ``row['dimensions']`` map (the convenience shape unit tests
         author);
      2. the persisted payload shape — ``analyst_outputs.data`` is the full
         ``ScorecardPayload`` model_dump, so the banded dimensions live at
         ``data->'data'->'bands'->'dimensions'`` (mirrors the v3 read route in
         ``registry.v3_api.eval_country_scorecard``); a top-level
         ``data->'bands'->'dimensions'`` is tolerated as a fallback.

    Returns only well-shaped ``{dimension: {band, reason, ...}}`` entries; a
    malformed value is dropped (degrade-not-drop).
    """
    direct = row.get("dimensions")
    if isinstance(direct, Mapping):
        return {str(k): dict(v) for k, v in direct.items() if isinstance(v, Mapping)}
    data = _parse_data(row.get("data"))
    inner = data.get("data")
    bands = inner.get("bands") if isinstance(inner, Mapping) else None
    if not isinstance(bands, Mapping):
        bands = data.get("bands")
    dims = bands.get("dimensions") if isinstance(bands, Mapping) else None
    if isinstance(dims, Mapping):
        return {str(k): dict(v) for k, v in dims.items() if isinstance(v, Mapping)}
    return {}


def _sort_key(row: Mapping[str, Any]) -> tuple[Any, str]:
    # Newest card first: latest produced_at, tie-broken by largest id.
    return (row.get("produced_at"), str(row.get("id")))


def _source_classes(dimension: str) -> list[str]:
    return list(SOURCE_CLASSES_BY_DIMENSION.get(dimension, _DEFAULT_SOURCE_CLASSES))


# ---------------------------------------------------------------------------
# Aggregation core (pure — testable without a DB)
# ---------------------------------------------------------------------------


def aggregate_gaps(
    rows: list[dict[str, Any]],
    *,
    dimensions: tuple[str, ...] = DIMENSIONS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Aggregate insufficient-evidence over ``desk × dimension`` → the gap list.

    Groups the scorecard rows by desk (``target_id``); within each desk it
    picks the CURRENT (latest-in-window) card to decide which cells are STARVED,
    and aggregates over ALL the desk's window cards to count how many were
    insufficient + which reasons drove it. A cell is a gap iff it is
    ``band == INSUFFICIENT`` in the CURRENT card.

    Returns ``(gaps, stats)``. Each gap cell::

        {desk, dimension, reason, reasons, insufficient_count,
         window_scorecards, persistence, source_classes,
         latest_scorecard_id, desk_starved_dims}

    ordered so the most-starved desks surface first (``desk_starved_dims`` DESC,
    then ``insufficient_count`` DESC, then desk id, then dimension order) — an
    all-insufficient desk (e.g. US) tops the list.
    """
    by_desk: dict[str, list[dict[str, Any]]] = defaultdict(list)
    valid_rows = 0
    for r in rows:
        desk = r.get("target_id")
        if desk is None:
            continue
        by_desk[str(desk)].append(r)
        valid_rows += 1

    dim_order = {d: i for i, d in enumerate(dimensions)}
    gaps: list[dict[str, Any]] = []

    for desk, members in by_desk.items():
        ordered = sorted(members, key=_sort_key, reverse=True)
        latest = ordered[0]
        latest_dims = _extract_dimensions(latest)

        # Aggregate over the whole window for this desk.
        agg: dict[str, dict[str, Any]] = {
            dim: {"insufficient": 0, "total": 0, "reasons": Counter()}
            for dim in dimensions
        }
        for member in members:
            mdims = _extract_dimensions(member)
            for dim in dimensions:
                d = mdims.get(dim)
                if not isinstance(d, Mapping):
                    continue
                agg[dim]["total"] += 1
                if d.get("band") == INSUFFICIENT:
                    agg[dim]["insufficient"] += 1
                    reason = str(d.get("reason") or "unspecified")
                    agg[dim]["reasons"][reason] += 1

        # A gap = a dimension INSUFFICIENT in the CURRENT (latest) card.
        for dim in dimensions:
            ld = latest_dims.get(dim)
            if not isinstance(ld, Mapping) or ld.get("band") != INSUFFICIENT:
                continue
            a = agg[dim]
            total = int(a["total"])
            insufficient = int(a["insufficient"])
            gaps.append({
                "desk": desk,
                "dimension": dim,
                "reason": str(ld.get("reason") or "unspecified"),
                "reasons": dict(a["reasons"]),
                "insufficient_count": insufficient,
                "window_scorecards": total,
                "persistence": (
                    round(insufficient / total, 3) if total else None
                ),
                "source_classes": _source_classes(dim),
                "latest_scorecard_id": (
                    str(latest.get("id")) if latest.get("id") is not None else None
                ),
            })

    # Annotate each cell with its desk's total starved-dimension count (the
    # primary ranking key) — an all-insufficient desk lifts all its cells.
    starved_by_desk: Counter[str] = Counter(g["desk"] for g in gaps)
    for g in gaps:
        g["desk_starved_dims"] = int(starved_by_desk[g["desk"]])

    gaps.sort(
        key=lambda g: (
            -g["desk_starved_dims"],
            -g["insufficient_count"],
            g["desk"],
            dim_order.get(g["dimension"], len(dimensions)),
        )
    )

    # Per-dimension rollup for collection prioritization (how many desks are
    # starved in each dimension + the plausible feed classes).
    desks_by_dim: dict[str, set[str]] = defaultdict(set)
    for g in gaps:
        desks_by_dim[g["dimension"]].add(g["desk"])
    by_dimension = {
        dim: {
            "desks_starved": len(desks_by_dim.get(dim, set())),
            "source_classes": _source_classes(dim),
        }
        for dim in dimensions
        if desks_by_dim.get(dim)
    }

    stats = {
        "scorecards_seen": valid_rows,
        "desks_seen": len(by_desk),
        "starved_desks": [
            {"desk": desk, "starved_dim_count": count}
            for desk, count in sorted(
                starved_by_desk.items(), key=lambda kv: (-kv[1], kv[0])
            )
        ],
        "by_dimension": by_dimension,
    }
    return gaps, stats


# ---------------------------------------------------------------------------
# R-2 — collection-requirement row construction (pure — testable without a DB)
# ---------------------------------------------------------------------------


def _desk_iso2(desk: Any) -> str | None:
    """Best-effort ISO2 for a ``country_<tier>_<iso2>`` desk id (e.g.
    ``country_g20_ml`` -> ``ML``) — the SAME target-id convention
    :func:`legba.runtime.grounding.world_context_country_filter_values` reads
    off the identical id shape (kept independent here: a deterministic-handler
    module stays DB/runtime-import-free on its pure side). Returns ``None``
    for a non-country or malformed id — the caller then matches candidates on
    ``source_class`` alone (no geo filter), never raises."""
    if not isinstance(desk, str) or not desk.startswith("country"):
        return None
    token = desk.rsplit("_", 1)[-1].strip()
    if len(token) == 2 and token.isalpha():
        return token.upper()
    return None


def build_gap_requirement_rows(
    gaps: list[dict[str, Any]],
    *,
    limit: int = _MAX_GAP_REQUIREMENTS_PER_RUN,
) -> list[dict[str, Any]]:
    """Starved desk×dimension cells -> candidate ``collection_requirements``
    rows (pre-candidate-match shape; :func:`_attach_candidates` fills the
    ``source_descriptors`` cross-reference).

    ``gaps`` is already priority-ordered (:func:`aggregate_gaps`) — this only
    TRIMS to ``limit``, it never reorders, so ``priority_rank`` mirrors the
    gap list's own rank. A gap cell whose scorecard evidence id is missing
    (defensive — ``aggregate_gaps`` always sets it from a real row) is
    skipped: a requirement is never written without a citable evidence row.
    """
    rows: list[dict[str, Any]] = []
    for rank, g in enumerate(gaps):
        if len(rows) >= limit:
            break
        evidence_id = g.get("latest_scorecard_id")
        if not evidence_id:
            continue
        desk = str(g["desk"])
        dim = str(g["dimension"])
        reason = str(g.get("reason") or "insufficient-evidence")
        window = g.get("window_scorecards") or 0
        rationale = f"starved in the desk's current scorecard ({reason})"
        if window:
            rationale += (
                f"; insufficient {g.get('insufficient_count', 0)}/{window} "
                f"sweeps this window (persistence={g.get('persistence')})"
            )
        starved_dims = g.get("desk_starved_dims")
        if starved_dims:
            rationale += f"; desk starved in {starved_dims} dimension(s) this sweep"
        rows.append({
            "natural_key": f"collection_gap:{desk}:{dim}",
            "origin": "collection_gap",
            "desk": desk,
            "dimension": dim,
            "topic": f"{dim} coverage for {desk}: {reason}",
            "rationale": rationale,
            "evidence_kind": "analyst_output",
            "evidence_id": str(evidence_id),
            "source_classes_wanted": list(
                g.get("source_classes") or _source_classes(dim)
            ),
            "priority_rank": rank,
        })
    return rows


def build_source_request_row(
    hyp: Mapping[str, Any],
    *,
    priority_rank: int,
) -> dict[str, Any]:
    """One ``hypotheses`` row (``status='source_request'``) -> a candidate
    ``collection_requirements`` row. Pure — the caller supplies the row +
    its rank; DB reads (fetch + candidate match) happen around this."""
    hid = str(hyp["id"])
    need = str(hyp.get("thesis") or "").strip()
    topic = (
        need[len(_SOURCE_REQUEST_PREFIX):]
        if need.startswith(_SOURCE_REQUEST_PREFIX)
        else need
    ).strip() or "(no coverage-gap text recorded)"
    rationale = str(hyp.get("counter_thesis") or "").strip()
    desk = hyp.get("target_id")
    return {
        "natural_key": f"source_request:{hid}",
        "origin": "source_request",
        "desk": str(desk) if desk else None,
        "dimension": None,
        "topic": topic[:2048],
        "rationale": rationale[:2048],
        "evidence_kind": "hypothesis",
        "evidence_id": hid,
        "source_classes_wanted": list(_DEFAULT_SOURCE_CLASSES),
        "priority_rank": priority_rank,
    }


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    gaps: list[dict[str, Any]], stats: Mapping[str, Any]
) -> FindingPayload:
    n = len(gaps)
    starved_desks = stats.get("starved_desks") or []
    n_desks = len(starved_desks)
    if n:
        title = (
            f"Collection requirements: {n} starved cell(s) across "
            f"{n_desks} desk(s)"
        )
        lines: list[str] = []
        for g in gaps[:_MAX_GAPS_IN_FINDING]:
            classes = ", ".join(g.get("source_classes") or []) or "(none)"
            persistence = g.get("persistence")
            persist = (
                f" [{g['insufficient_count']}/{g['window_scorecards']}"
                f"={persistence}]"
                if g.get("window_scorecards")
                else ""
            )
            lines.append(
                f"- {g['desk']} / {g['dimension']}: {g['reason']}{persist} "
                f"→ feed: {classes}"
            )
        body = "\n".join(lines)
    else:
        title = "Collection requirements: no starved cells"
        body = (
            "No desk×dimension cell is insufficient in the current scorecards "
            "this sweep."
        )
    tags = ["deterministic", SUB_HANDLER_NAME]
    if n:
        tags.append("collection_gap")
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "gap_count": n,
            "starved_desk_count": n_desks,
            "scorecards_seen": stats.get("scorecards_seen", 0),
            "desks_seen": stats.get("desks_seen", 0),
            "starved_desks": starved_desks,
            "by_dimension": stats.get("by_dimension", {}),
            "gaps": gaps[:_MAX_GAPS_IN_FINDING],
        },
    )


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


_FETCH_SCORECARDS_SQL = """
    SELECT id::text AS id, target_id, produced_at, data
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND produced_at > NOW() - make_interval(days => $1)
     ORDER BY produced_at DESC, id DESC
"""


async def _fetch_scorecards(
    conn: Any, *, window_days: int
) -> list[dict[str, Any]]:
    """All scorecard rows produced within the window (HEAD + superseded).

    NO ``superseded_by IS NULL`` filter: the aggregation needs every card in the
    window to count how persistently a cell has been insufficient. The latest
    card per desk (picked in :func:`aggregate_gaps`) decides current starvation.
    """
    rows = await conn.fetch(_FETCH_SCORECARDS_SQL, int(window_days))
    return [
        {
            "id": r["id"],
            "target_id": r["target_id"],
            "produced_at": r["produced_at"],
            "data": r["data"],
        }
        for r in rows
    ]


async def _last_emitted_body(pool: Any, analyst_id: str) -> str | None:
    """Body of the most recent FEED finding this analyst emitted (or None).

    Trace-only suppressed runs write no ``analyst_outputs`` row, so this is the
    last NON-suppressed summary — exactly what a re-swept identical gap set
    should be deduped against.
    """
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT body FROM analyst_outputs "
            "WHERE analyst_id = $1 AND kind = 'finding' "
            "ORDER BY produced_at DESC LIMIT 1",
            analyst_id,
        )
    return row["body"] if row else None


# ---------------------------------------------------------------------------
# R-2 — collection-requirements side-write (live-pool only; degrade-not-break)
# ---------------------------------------------------------------------------

_FETCH_SOURCE_REQUESTS_SQL = """
    SELECT id, thesis, counter_thesis, target_id, produced_at
      FROM hypotheses
     WHERE status = 'source_request'
     ORDER BY produced_at DESC, id
     LIMIT $1
"""

# Any lifecycle state matches — reuse-before-create wants a paused/retired/
# draft descriptor surfaced as a REACTIVATION candidate just as much as an
# active one (an active match is its own honest signal: the cell is still
# starved despite a matching source already running — a quality gap, not a
# registration gap). A source with an EMPTY geo array is a global feed and
# matches any desk; a source WITH a geo array must overlap the desk's ISO2
# (or the desk has none, in which case $2 is empty and every source_class
# match qualifies).
_MATCH_CANDIDATE_SOURCES_SQL = """
    SELECT descriptor_id, state,
           body -> 'scope' ->> 'source_class' AS source_class,
           body -> 'scope' ->> 'license_class' AS license_class,
           body -> 'config' -> 'url' ->> 'raw' AS url
      FROM source_descriptors
     WHERE is_head
       AND (body -> 'scope' ->> 'source_class') = ANY($1::text[])
       AND (
             $2::text[] = '{}'::text[]
             OR NOT (body -> 'scope' ? 'geo')
             OR jsonb_array_length(body -> 'scope' -> 'geo') = 0
             OR (body -> 'scope' -> 'geo') ?| $2::text[]
           )
     ORDER BY (state = 'active'), descriptor_id
     LIMIT $3
"""

_EXISTING_NATURAL_KEYS_SQL = (
    "SELECT natural_key FROM collection_requirements "
    "WHERE natural_key = ANY($1::text[])"
)

_INSERT_REQUIREMENT_SQL = """
    INSERT INTO collection_requirements
        (natural_key, origin, desk, dimension, topic, rationale,
         evidence_kind, evidence_id, source_classes_wanted,
         candidate_sources, suggested_fetch_url, fillable, unfillable_reason,
         priority_rank)
    VALUES ($1, $2, $3, $4, $5, $6, $7, $8::uuid, $9::text[], $10::jsonb,
            $11, $12, $13, $14)
    ON CONFLICT (natural_key) DO NOTHING
"""


async def _match_candidate_sources(
    conn: Any,
    source_classes: Sequence[str],
    geo_codes: Sequence[str],
    *,
    limit: int,
) -> list[dict[str, Any]]:
    """Existing ``source_descriptors`` (any state) matching ``source_classes``
    (+ ``geo_codes`` when non-empty) — the "reuse before create" cross-
    reference. Degrade-not-break: a query failure logs + returns ``[]`` (the
    requirement still writes, just ``fillable=False`` this run)."""
    try:
        rows = await conn.fetch(
            _MATCH_CANDIDATE_SOURCES_SQL,
            list(source_classes),
            list(geo_codes),
            limit,
        )
    except Exception as exc:  # noqa: BLE001 — degrade: no candidates, not a crash
        logger.warning("collection_gap.candidate_match_failed err=%s", exc)
        return []
    return [
        {
            "descriptor_id": r["descriptor_id"],
            "state": r["state"],
            "source_class": r["source_class"],
            "license_class": r["license_class"],
            "url": r["url"],
        }
        for r in rows
    ]


async def _attach_candidates(conn: Any, row: Mapping[str, Any]) -> dict[str, Any]:
    """Fill ``candidate_sources`` / ``suggested_fetch_url`` / ``fillable`` /
    ``unfillable_reason`` on one pre-shaped requirement row via the
    source_descriptors cross-reference. Returns a NEW dict (input untouched).
    """
    out = dict(row)
    iso2 = _desk_iso2(row.get("desk"))
    geo = [iso2] if iso2 else []
    candidates = await _match_candidate_sources(
        conn,
        row["source_classes_wanted"],
        geo,
        limit=_MAX_CANDIDATE_SOURCES,
    )
    out["candidate_sources"] = [
        {
            "descriptor_id": c["descriptor_id"],
            "state": c["state"],
            "source_class": c["source_class"],
            "license_class": c["license_class"],
            "match_reason": (
                f"source_class={c['source_class']}"
                + (f", geo={iso2}" if iso2 else "")
            ),
        }
        for c in candidates
    ]
    out["fillable"] = bool(candidates)
    out["unfillable_reason"] = None if candidates else "no_known_feed"
    # A NON-active candidate's own known url — a one-off web_fetch sample
    # before an operator decides whether to reactivate the full feed. Active
    # candidates are already being fetched on cadence; no fetch to suggest.
    out["suggested_fetch_url"] = next(
        (c["url"] for c in candidates if c.get("state") != "active" and c.get("url")),
        None,
    )
    return out


async def _write_requirements(
    pool: Any,
    rows: list[dict[str, Any]],
) -> int:
    """Idempotently write pre-shaped (pre-candidate-match) requirement rows.

    Bulk-checks existing ``natural_key``s first (skips already-proposed work
    without a per-row round trip), attaches candidates only for the rows
    actually being written, then inserts — ``ON CONFLICT DO NOTHING`` is the
    schema-enforced backstop against a race with a concurrent run. Returns the
    count actually written. Degrade-not-break: any failure logs + returns 0 —
    the primary collection-requirements finding is never blocked."""
    if not rows:
        return 0
    try:
        async with pool.acquire() as conn:
            existing = {
                r["natural_key"]
                for r in await conn.fetch(
                    _EXISTING_NATURAL_KEYS_SQL,
                    [r["natural_key"] for r in rows],
                )
            }
            written = 0
            for row in rows:
                if row["natural_key"] in existing:
                    continue
                full = await _attach_candidates(conn, row)
                tag = await conn.execute(
                    _INSERT_REQUIREMENT_SQL,
                    full["natural_key"],
                    full["origin"],
                    full["desk"],
                    full["dimension"],
                    full["topic"],
                    full["rationale"],
                    full["evidence_kind"],
                    full["evidence_id"],
                    full["source_classes_wanted"],
                    json.dumps(full["candidate_sources"]),
                    full["suggested_fetch_url"],
                    full["fillable"],
                    full["unfillable_reason"],
                    full["priority_rank"],
                )
                if tag.endswith(" 1"):
                    written += 1
            return written
    except Exception as exc:  # noqa: BLE001 — degrade-not-break, never blocks the finding
        logger.warning("collection_gap.requirements_write_failed err=%s", exc)
        return 0


async def _propose_collection_requirements(
    pool: Any,
    gaps: list[dict[str, Any]],
) -> int:
    """The full R-2 side-write for one run: gap-cell rows + the standing
    ``source_request`` backlog, both bounded, both idempotent. Returns the
    total rows written (0 on any failure or when the table does not exist —
    e.g. an un-migrated dev DB — degrade-not-break)."""
    gap_rows = build_gap_requirement_rows(gaps, limit=_MAX_GAP_REQUIREMENTS_PER_RUN)
    written = await _write_requirements(pool, gap_rows)

    try:
        async with pool.acquire() as conn:
            hyp_rows = await conn.fetch(
                _FETCH_SOURCE_REQUESTS_SQL,
                _MAX_SOURCE_REQUEST_REQUIREMENTS_PER_RUN,
            )
    except Exception as exc:  # noqa: BLE001 — degrade: skip this half, keep the gap half
        logger.warning("collection_gap.source_request_fetch_failed err=%s", exc)
        return written

    request_rows = [
        build_source_request_row(dict(h), priority_rank=i)
        for i, h in enumerate(hyp_rows)
    ]
    written += await _write_requirements(pool, request_rows)
    return written


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see the module docstring.

    ``deps`` is the analyst pool bundle (``deps.pg_pool``); ``deps=None`` runs the
    synthetic path (aggregates pre-shaped ``inputs`` scorecard rows, no DB) for
    unit tests.

    Options
    -------
    window_days:
        Only scorecards this recent are aggregated (default 35).
    analyst_id:
        This analyst's own id (used to scope the last-emitted-body dedup lookup).
    """
    window_days = int(options.get("window_days", _DEFAULT_WINDOW_DAYS))
    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            async with pool.acquire() as conn:
                rows = await _fetch_scorecards(conn, window_days=window_days)
            gaps, stats = aggregate_gaps(rows)
        except Exception as exc:  # noqa: BLE001 — degrade-not-drop
            logger.warning("collection_gap.pool_failed err=%s", exc)
            gaps, stats = [], {"scorecards_seen": 0, "desks_seen": 0,
                               "starved_desks": [], "by_dimension": {}}
    else:
        gaps, stats = aggregate_gaps([dict(r) for r in inputs])

    finding = _build_finding(gaps, stats)

    # Emit a FEED finding only when there is a starved cell to surface. A no-gap
    # sweep is suppressed (trace-only) so an idempotent monthly re-run does not
    # repeat 'nothing starved'; and on the live path a gap set byte-identical to
    # the last EMITTED summary (the same scorecards re-swept) is likewise
    # suppressed. Degrade to emit on any dedup-check failure.
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    force_trace_only = not gaps
    if gaps and pool is not None:
        try:
            force_trace_only = (
                await _last_emitted_body(pool, analyst_id) == finding.body
            )
        except Exception as exc:  # noqa: BLE001 — degrade: emit rather than crash
            logger.warning("collection_gap.dedup_check_failed err=%s", exc)
            force_trace_only = False

    # R-2 — collection-requirements side-write: bounded, idempotent, best-
    # effort. Runs on the live pool path only (the synthetic deps=None path
    # has no table to write to — unit tests exercise the pure builders +
    # _write_requirements/_attach_candidates directly against a real DB).
    # Never blocks the primary finding: any failure degrades to 0 written.
    if pool is not None:
        finding.data["requirements_proposed"] = await _propose_collection_requirements(
            pool, gaps
        )
    else:
        finding.data["requirements_proposed"] = 0

    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=force_trace_only,
    )


__all__ = ["handle", "aggregate_gaps", "SOURCE_CLASSES_BY_DIMENSION", "SUB_HANDLER_NAME"]

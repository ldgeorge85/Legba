# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``geo_convergence_scan`` sub-handler — A7 geographic convergence detector
(worldmonitor pattern, LLM-free).

A deterministic META analyst on a ~30-minute cadence that bins the last
``window_hours`` (24h) of geolocated signals into geographic bins and fires a
``medium`` ``kind='alert'`` row when signals from **at least
``min_distinct_families`` (3) DISTINCT source families** converge in one bin —
diversity is the signal; a same-family pile-on (20 quake feed rows, or one
outlet's syndication burst) never fires.

What the geo data actually supports — the two-tier honesty split
-----------------------------------------------------------------
Signals carry TWO geographic surfaces of very different precision:

  * ``payload.geo.lat``/``lon`` (JSONB) — a geocoded point whose declared
    ``payload.geo.precision`` is ``country`` for roughly half the geocoded
    rows. A country-precision nominatim point is the COUNTRY CENTROID, not an
    event location — binning it into a 1°×1° cell would fabricate precision
    the data does not have.
  * ``signals.geo`` (text[]) — ISO2 country tags.

So the scan bins on two honest tiers:

  * **cell tier** — 1°×1° cells (floor(lat)×floor(lon)), fed ONLY by signals
    whose point is trustworthy at sub-degree scale: geocode precision in
    (``region``/``municipality``/``address``), or ``payload.geo.source ==
    'geometry'`` (the DQ-C1 geometry-first branch preserves the EXACT
    authoritative source coordinate — USGS epicentre, NASA/GDACS point — and
    only the country *attribution* is coarse).
  * **country tier** — one bin per ISO2 tag in ``signals.geo``, fed by every
    country-tagged signal (cell-tier signals included: a localized cluster is
    also activity in its country).

A strong localized convergence can therefore fire one cell alert and
contribute to its country bin; the state-transition contract below (a
permanently-diverse bin like ``country:US`` seeds as formed and never fires)
plus the per-desk cap keep that honest rather than noisy.

Source family
-------------
The diversity axis is the **source family**: the first ``scope.tags`` entry of
the signal's head ``source_descriptors`` row — the curated in-repo taxonomy
(``news`` / ``gis`` / ``health`` / ``defense`` / ``crisis`` / ``social`` /
``osint`` / …). All the geo feeds collapse into ``gis``, all wire outlets into
``news`` — exactly the same-family fold the diversity rule needs. A source
with no head descriptor or no scope tags gets the honest per-source fallback
family ``src:<source_id>`` (it counts once, never inflates diversity across
its own signals).

Score
-----
``score = distinct_family_count + volume_bonus`` where ``volume_bonus`` is
+1 per ``VOLUME_BONUS_STEP`` (10) contributing signals, capped at
``VOLUME_BONUS_CAP`` (2). Severity is flat ``medium`` on formation (the score
is ranking metadata, not a severity ladder) and ``info`` on dissolution.

Statefulness — formation/dissolution edges, no refire
-----------------------------------------------------
Durable per-bin watermarks live in the EXISTING ``alert_trigger_watermarks``
table (migration 0091 — a generic ``(trigger_class, watermark_key, state)``
store) under this handler's own ``trigger_class='geo_convergence'`` namespace;
NO new migration. The scan fires on the **formation edge** (a bin crossing
into ≥N distinct families whose watermark is absent or inactive) and ONCE on
the **dissolution edge** (an active bin dropping below N — cheap, since both
the watermark map and the current bins are already in hand); a persisting
convergence never refires. The FIRST-EVER scan seeds every currently-formed
bin silently (``_seeded`` marker — the alert_trigger_scan bring-up contract:
history must not page the operator). Inactive watermark rows untouched for
``_WM_PRUNE_DAYS`` are pruned.

Output path
-----------
Fired transitions land as ``kind='alert'`` rows (payload lists the
contributing signals: ids + sources + families; ``derived_from`` carries up to
``_MAX_DERIVED_REFS`` contributing signal ids) with the shared per-desk cap +
honest rollup (``alert_trigger_scan.apply_desk_cap``), then fan outward
through the shared P1-1 dispatcher. ``target_id`` is the desk whose
``scope.geo`` covers the bin's country when one matches. The run's returned
summary is a genuine FINDING when something happened (formations /
dissolutions / first-scan seeding) and ``force_trace_only`` on a quiet sweep —
the indicator_tracker pattern, which keeps this handler in the
FINDING-emitting set the STRUCTURAL_VERIFY_EXEMPT drift guard asserts.

Registered via ``scripts/bringup_register_geo_convergence_scan.py`` (descriptor
``descriptors/analyst_geo_convergence_scan.yaml``, ships ``state: draft``).
"""
from __future__ import annotations

import logging
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping, Optional
from uuid import UUID, uuid4

from ....runtime.analyst_method import AnalystMethodResult
from ...provenance.models import FindingPayload
from . import alert_trigger_scan as ats

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "geo_convergence_scan"

#: Dispatcher channel name (mirrors 'trigger_scan' on the sibling scan).
CHANNEL_NAME = "geo_convergence"

#: This handler's watermark namespace inside alert_trigger_watermarks. The
#: table PK is (trigger_class, watermark_key), so the sibling scan's classes
#: (band_crossing / verified_finding / …) can never collide with ours.
TRIGGER_CLASS = "geo_convergence"

#: Rolling signal window the bins are computed over.
DEFAULT_WINDOW_HOURS = 24

#: The diversity bar: distinct source families required in one bin.
DEFAULT_MIN_DISTINCT_FAMILIES = 3

#: Per-desk alert cap per scan (shared shape with the sibling trigger scan).
DEFAULT_PER_DESK_CAP = 3

#: Geocode precisions whose lat/lon is trustworthy at 1° cell scale.
POINT_PRECISIONS = frozenset({"region", "municipality", "address"})

#: score = families + min(VOLUME_BONUS_CAP, signal_count // VOLUME_BONUS_STEP)
VOLUME_BONUS_STEP = 10
VOLUME_BONUS_CAP = 2

#: Defensive bounds.
_MAX_SIGNALS_PER_TIER = 50_000
_MAX_CONTRIB_SIGNALS = 12   # contributors listed on the alert payload
_MAX_DERIVED_REFS = 8       # signal ids carried in derived_from
_WM_PRUNE_DAYS = 30


# ---------------------------------------------------------------------------
# Pure helpers (testable with NO database)
# ---------------------------------------------------------------------------


def cell_key(lat: Any, lon: Any) -> Optional[str]:
    """The 1°×1° cell bin key for a point, or None for an unusable point.

    ``cell:<floor(lat)>:<floor(lon)>`` with the top edges folded into the last
    valid cell (lat 90 → 89, lon 180 → 179) so the key space is exactly
    [-90..89]×[-180..179]. Non-finite / out-of-range values return None —
    a junk coordinate must never mint a bin.
    """
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(flat) and math.isfinite(flon)):
        return None
    if not (-90.0 <= flat <= 90.0 and -180.0 <= flon <= 180.0):
        return None
    cell_lat = min(int(math.floor(flat)), 89)
    cell_lon = min(int(math.floor(flon)), 179)
    return f"cell:{cell_lat}:{cell_lon}"


def country_key(tag: Any) -> Optional[str]:
    """The country bin key for one ``signals.geo`` tag, or None.

    The geo column's live vocabulary is ISO2 codes; anything else (junk tags
    predating migration 0062) is skipped rather than minting a junk bin.
    """
    if not isinstance(tag, str):
        return None
    t = tag.strip().upper()
    if len(t) != 2 or not t.isalpha():
        return None
    return f"country:{t}"


def source_family(scope_tags: Any, source_id: str) -> str:
    """The source's family: first non-empty ``scope.tags`` entry, else the
    honest per-source fallback ``src:<source_id>`` (counts once, never
    fabricates cross-source diversity)."""
    if isinstance(scope_tags, (list, tuple)):
        for t in scope_tags:
            if isinstance(t, str) and t.strip():
                return t.strip()
    return f"src:{source_id}"


def convergence_score(n_families: int, n_signals: int) -> int:
    """distinct-family count + a small volume bonus (+1 per
    :data:`VOLUME_BONUS_STEP` signals, capped at :data:`VOLUME_BONUS_CAP`)."""
    bonus = min(VOLUME_BONUS_CAP, max(0, n_signals) // VOLUME_BONUS_STEP)
    return int(n_families) + bonus


@dataclass
class BinAgg:
    """One geographic bin's rolling-window aggregate."""

    bin_key: str
    bin_kind: str                       # 'cell' | 'country'
    country_iso2: Optional[str] = None  # cell: modal contributor iso2
    #: (signal_id, source_id, family) per contributing signal.
    contributors: list[tuple[str, str, str]] = field(default_factory=list)
    families: set[str] = field(default_factory=set)

    @property
    def signal_count(self) -> int:
        return len(self.contributors)

    @property
    def score(self) -> int:
        return convergence_score(len(self.families), self.signal_count)


def build_bins(
    point_rows: Iterable[Mapping[str, Any]],
    country_rows: Iterable[Mapping[str, Any]],
    families_by_source: Mapping[str, Any],
) -> dict[str, BinAgg]:
    """Bin the window's signals on both tiers (see module docstring).

    ``point_rows``: mappings with ``id`` / ``source_id`` / ``lat`` / ``lon`` /
    ``iso2`` — ONLY point-trustworthy signals (the SQL enforces the precision
    gate). ``country_rows``: mappings with ``id`` / ``source_id`` /
    ``country`` (one row per signal×geo-tag). ``families_by_source`` maps
    source_id → scope tags (list) — resolved through :func:`source_family`.
    """
    bins: dict[str, BinAgg] = {}
    cell_iso2: dict[str, Counter] = {}

    def _family(source_id: str) -> str:
        return source_family(families_by_source.get(source_id), source_id)

    for row in point_rows:
        key = cell_key(row.get("lat"), row.get("lon"))
        if key is None:
            continue
        sid = str(row.get("id") or "")
        src = str(row.get("source_id") or "")
        agg = bins.setdefault(key, BinAgg(bin_key=key, bin_kind="cell"))
        fam = _family(src)
        agg.contributors.append((sid, src, fam))
        agg.families.add(fam)
        iso2 = row.get("iso2")
        if isinstance(iso2, str) and iso2.strip():
            cell_iso2.setdefault(key, Counter())[iso2.strip().upper()] += 1

    # Modal contributor country per cell (deterministic: count desc, then code).
    for key, counts in cell_iso2.items():
        bins[key].country_iso2 = min(
            counts, key=lambda c: (-counts[c], c)
        )

    for row in country_rows:
        key = country_key(row.get("country"))
        if key is None:
            continue
        sid = str(row.get("id") or "")
        src = str(row.get("source_id") or "")
        agg = bins.setdefault(
            key,
            BinAgg(bin_key=key, bin_kind="country", country_iso2=key[8:]),
        )
        fam = _family(src)
        agg.contributors.append((sid, src, fam))
        agg.families.add(fam)

    return bins


def edge_actions(
    seeded: bool,
    prev_states: Mapping[str, Mapping[str, Any]],
    formed_keys: Iterable[str],
) -> tuple[list[str], list[str], list[str]]:
    """The state-transition core: (fire_formed, fire_dissolved, silent_seed).

    * First-ever scan (``seeded`` False): every currently-formed bin is seeded
      silently — bring-up must not page history. Nothing fires.
    * Formation edge: formed now, watermark absent or inactive → fire.
    * Dissolution edge: watermark active, no longer formed → fire once.
    * Persisting convergence (active + still formed): nothing.
    """
    formed = set(formed_keys)
    if not seeded:
        return [], [], sorted(formed)
    fire_formed = [
        k
        for k in sorted(formed)
        if not bool((prev_states.get(k) or {}).get("active"))
    ]
    fire_dissolved = [
        k
        for k in sorted(prev_states)
        if bool((prev_states.get(k) or {}).get("active")) and k not in formed
    ]
    return fire_formed, fire_dissolved, []


def bin_label(bin_key: str, country_iso2: Optional[str]) -> str:
    """Human label: 'IQ' for country bins; 'cell(33..34°, 44..45°) IQ' for
    cells (the 1° extent stated, never a fake point)."""
    if bin_key.startswith("country:"):
        return bin_key[8:]
    try:
        _, lat_s, lon_s = bin_key.split(":")
        lat0, lon0 = int(lat_s), int(lon_s)
        label = f"cell({lat0}..{lat0 + 1}°, {lon0}..{lon0 + 1}°)"
    except (ValueError, TypeError):  # pragma: no cover — key we minted
        label = bin_key
    return f"{label} {country_iso2}" if country_iso2 else label


def _uuid_or_none(raw: Any) -> Optional[UUID]:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Cell tier: ONLY point-trustworthy coordinates (sub-country geocode precision,
# or the geometry-first branch's exact authoritative source point). Dedup'd
# children (canonical_signal_id → another row) are excluded so a syndication
# burst can't pad the volume bonus.
_POINT_SIGNALS_SQL = """
    SELECT s.id::text                          AS id,
           s.source_id                         AS source_id,
           s.payload->'geo'->>'lat'            AS lat,
           s.payload->'geo'->>'lon'            AS lon,
           s.payload->'geo'->>'country_iso2'   AS iso2
      FROM signals s
     WHERE s.fetched_at > now() - make_interval(hours => $1)
       AND (s.canonical_signal_id IS NULL OR s.canonical_signal_id = s.id)
       AND s.payload->'geo'->>'lat' IS NOT NULL
       AND s.payload->'geo'->>'lon' IS NOT NULL
       AND (
             s.payload->'geo'->>'precision' IN
                 ('region', 'municipality', 'address')
             OR s.payload->'geo'->>'source' = 'geometry'
           )
     ORDER BY s.fetched_at DESC
     LIMIT $2
"""

# Country tier: every country-tagged signal, one row per signal×ISO2 tag.
_COUNTRY_SIGNALS_SQL = """
    SELECT s.id::text  AS id,
           s.source_id AS source_id,
           g.country   AS country
      FROM signals s
      CROSS JOIN LATERAL unnest(s.geo) AS g(country)
     WHERE s.fetched_at > now() - make_interval(hours => $1)
       AND (s.canonical_signal_id IS NULL OR s.canonical_signal_id = s.id)
       AND cardinality(s.geo) > 0
     ORDER BY s.fetched_at DESC
     LIMIT $2
"""

_SOURCE_FAMILIES_SQL = """
    SELECT descriptor_id            AS source_id,
           body->'scope'->'tags'    AS scope_tags
      FROM source_descriptors
     WHERE is_head
"""

# Desk attribution: non-retired target heads with a scope.geo — first
# (alphabetical) desk covering the bin's country wins.
_DESK_GEO_SQL = """
    SELECT descriptor_id,
           body->'scope'->'geo' AS geo
      FROM target_descriptors
     WHERE is_head
       AND COALESCE(state, 'active') <> 'retired'
     ORDER BY descriptor_id
     LIMIT 500
"""

_WM_PRUNE_INACTIVE_SQL = """
    DELETE FROM alert_trigger_watermarks
     WHERE trigger_class = $1
       AND watermark_key <> $2
       AND updated_at < now() - make_interval(days => $3)
       AND (state->>'active') IS DISTINCT FROM 'true'
"""


async def _load_desk_by_iso2(conn: Any) -> dict[str, str]:
    rows = await conn.fetch(_DESK_GEO_SQL)
    out: dict[str, str] = {}
    for row in rows:
        geo = ats._parse_jsonish(row["geo"])
        if not isinstance(geo, list):
            continue
        for code in geo:
            if isinstance(code, str):
                out.setdefault(code.strip().upper(), str(row["descriptor_id"]))
    return out


# ---------------------------------------------------------------------------
# Candidate builders
# ---------------------------------------------------------------------------


def _formation_candidate(
    agg: BinAgg,
    *,
    window_hours: int,
    min_families: int,
    desk_by_iso2: Mapping[str, str],
) -> ats.AlertCandidate:
    families = sorted(agg.families)
    contributors = [
        {"id": sid, "source_id": src, "family": fam}
        for sid, src, fam in agg.contributors[:_MAX_CONTRIB_SIGNALS]
    ]
    refs: list[UUID] = []
    for sid, _, _ in agg.contributors:
        u = _uuid_or_none(sid)
        if u is not None and u not in refs and len(refs) < _MAX_DERIVED_REFS:
            refs.append(u)
    label = bin_label(agg.bin_key, agg.country_iso2)
    desk = desk_by_iso2.get(agg.country_iso2 or "")
    per_family = Counter(fam for _, _, fam in agg.contributors)
    state = {
        "active": True,
        "families": families,
        "count": agg.signal_count,
    }
    tier_note = (
        "1°×1° cell over point-trustworthy coordinates only (sub-country "
        "geocode precision or an authoritative source geometry point)"
        if agg.bin_kind == "cell"
        else "country bin over signals.geo ISO2 tags (no sub-country claim)"
    )
    return ats.AlertCandidate(
        trigger_class=TRIGGER_CLASS,
        severity="medium",
        title=(
            f"Geo convergence formed: {label} — {len(families)} source "
            f"families, {agg.signal_count} signals ({window_hours}h)"
        ),
        body=(
            f"bin={agg.bin_key} kind={agg.bin_kind} "
            f"country={agg.country_iso2 or '?'}\n"
            f"distinct_families={len(families)} (bar={min_families}) "
            f"signals={agg.signal_count} score={agg.score}\n"
            f"families: "
            + ", ".join(f"{f}×{per_family[f]}" for f in families)
            + f"\nbinning: {tier_note}"
        ),
        target_id=desk,
        derived_from=refs,
        data={
            "event": "formed",
            "bin_key": agg.bin_key,
            "bin_kind": agg.bin_kind,
            "country_iso2": agg.country_iso2,
            "families": families,
            "distinct_family_count": len(families),
            "signal_count": agg.signal_count,
            "score": agg.score,
            "min_distinct_families": min_families,
            "window_hours": window_hours,
            "contributing_signals": contributors,
            "contributors_truncated": agg.signal_count > len(contributors),
        },
        watermarks=[(TRIGGER_CLASS, agg.bin_key, state)],
    )


def _dissolution_candidate(
    bin_key: str,
    prev_state: Mapping[str, Any],
    *,
    window_hours: int,
    min_families: int,
    desk_by_iso2: Mapping[str, str],
) -> ats.AlertCandidate:
    iso2 = bin_key[8:] if bin_key.startswith("country:") else None
    prev_families = [
        f for f in (prev_state.get("families") or []) if isinstance(f, str)
    ]
    label = bin_label(bin_key, iso2)
    state = {
        "active": False,
        "families": [],
        "count": 0,
        "last_families": prev_families,
    }
    return ats.AlertCandidate(
        trigger_class=TRIGGER_CLASS,
        severity="info",
        title=(
            f"Geo convergence dissolved: {label} — below "
            f"{min_families} distinct source families ({window_hours}h window)"
        ),
        body=(
            f"bin={bin_key} country={iso2 or '?'}\n"
            f"previously_converging_families={prev_families}\n"
            f"the {window_hours}h window no longer holds "
            f">={min_families} distinct source families here"
        ),
        target_id=desk_by_iso2.get(iso2 or ""),
        derived_from=[],
        data={
            "event": "dissolved",
            "bin_key": bin_key,
            "bin_kind": "country" if iso2 else "cell",
            "country_iso2": iso2,
            "previous_families": prev_families,
            "min_distinct_families": min_families,
            "window_hours": window_hours,
        },
        watermarks=[(TRIGGER_CLASS, bin_key, state)],
    )


def _sink_payload(alert_row_id: UUID, cand: ats.AlertCandidate) -> Any:
    from ...alerts.sinks import AlertSinkPayload, receipt_link, unverified_state

    path, url = receipt_link(str(alert_row_id), row_kind="alert")
    return AlertSinkPayload(
        summary=cand.title[:512],
        detail=cand.body[:4000],
        severity=cand.severity,
        channel_name=CHANNEL_NAME,
        target_id=cand.target_id,
        effective_confidence=None,
        verify_state=unverified_state(
            "deterministic geographic-convergence trigger (source-family "
            "diversity count over binned signals; no LLM prose)"
        ),
        event_at=None,
        alert_row_id=str(alert_row_id),
        receipt_path=path,
        receipt_url=url,
    )


# ---------------------------------------------------------------------------
# Summary finding
# ---------------------------------------------------------------------------


def _build_summary(
    *,
    seeded_now: bool,
    formed_fired: int,
    dissolved_fired: int,
    rollups: int,
    suppressed: int,
    write_failures: int,
    active_bins: int,
    cell_bins_formed: int,
    country_bins_formed: int,
    point_signals: int,
    country_signal_rows: int,
    window_hours: int,
    min_families: int,
    per_desk_cap: int,
) -> FindingPayload:
    if seeded_now:
        title = (
            f"Geo convergence scan seeded: {active_bins} already-converging "
            f"bin(s) recorded silently (no history paged)"
        )
    else:
        title = (
            f"Geo convergence scan: {formed_fired} formation(s), "
            f"{dissolved_fired} dissolution(s), {rollups} rollup(s)"
        )
    body_lines = [
        f"window_hours={window_hours} min_distinct_families={min_families}",
        (
            f"formed_fired={formed_fired} dissolved_fired={dissolved_fired} "
            f"rollups={rollups} suppressed_into_rollups={suppressed} "
            f"write_failures={write_failures}"
        ),
        (
            f"currently_formed_bins={active_bins} "
            f"(cell={cell_bins_formed}, country={country_bins_formed})"
        ),
        (
            f"window_signals: point_trustworthy={point_signals} "
            f"country_tag_rows={country_signal_rows}"
        ),
        (
            "binning: 1°×1° cells for sub-country-precision/geometry points; "
            "country bins for ISO2-tagged signals (country-centroid geocodes "
            "are NEVER cell-binned)"
        ),
    ]
    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "seeded": seeded_now,
            "formed_fired": formed_fired,
            "dissolved_fired": dissolved_fired,
            "rollups": rollups,
            "suppressed_into_rollups": suppressed,
            "write_failures": write_failures,
            "currently_formed_bins": active_bins,
            "cell_bins_formed": cell_bins_formed,
            "country_bins_formed": country_bins_formed,
            "point_signals": point_signals,
            "country_signal_rows": country_signal_rows,
            "window_hours": window_hours,
            "min_distinct_families": min_families,
            "per_desk_cap": per_desk_cap,
            # C2b (P4-6) — the structural_claims verify CONTRACT. This handler is
            # in STRUCTURAL_CLAIMS_VERIFY_ANALYSTS, so the deterministic
            # re-derivation profile (verify.verify_structural_claims) checks each
            # declared claim after the finding lands and stamps a
            # ``structural_verified`` critique. The rollup identity below —
            # currently_formed_bins == cell_bins_formed + country_bins_formed — is
            # an always-true internal invariant (formed bins are ONLY cell- or
            # country-tier), so a future miscount bug surfaces as a FLAGGED
            # structural critique instead of silently shipping a wrong headline.
            "structural_claims": [
                {
                    "id": "formed_bins_rollup",
                    "statement": (
                        f"currently_formed_bins ({active_bins}) = "
                        f"cell_bins_formed ({cell_bins_formed}) + "
                        f"country_bins_formed ({country_bins_formed})"
                    ),
                    "op": "sum",
                    "asserted": active_bins,
                    "basis": [cell_bins_formed, country_bins_formed],
                },
            ],
        },
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — one global convergence scan (module docstring).

    REFUSES LOUD on a missing pool (the sibling trigger-scan contract): a scan
    that cannot read the substrate must error visibly, never report a quiet
    zero-convergence run.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "geo_convergence_scan requires a live deps.pg_pool — refusing to "
            "report a zero-convergence scan without reading the substrate"
        )

    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = options.get("analyst_version")
    raw_run_id = options.get("run_id")
    try:
        run_uuid = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        run_uuid = uuid4()

    window_hours = int(options.get("window_hours", DEFAULT_WINDOW_HOURS))
    min_families = max(
        2, int(options.get("min_distinct_families", DEFAULT_MIN_DISTINCT_FAMILIES))
    )
    per_desk_cap = max(1, int(options.get("per_desk_cap", DEFAULT_PER_DESK_CAP)))

    async with pool.acquire() as conn:
        seeded, prev_states = await ats._load_class_watermarks(
            conn, TRIGGER_CLASS
        )
        point_rows = await conn.fetch(
            _POINT_SIGNALS_SQL, window_hours, _MAX_SIGNALS_PER_TIER
        )
        country_rows = await conn.fetch(
            _COUNTRY_SIGNALS_SQL, window_hours, _MAX_SIGNALS_PER_TIER
        )
        fam_rows = await conn.fetch(_SOURCE_FAMILIES_SQL)
        desk_by_iso2 = await _load_desk_by_iso2(conn)

        families_by_source = {
            str(r["source_id"]): ats._parse_jsonish(r["scope_tags"])
            for r in fam_rows
        }
        bins = build_bins(point_rows, country_rows, families_by_source)
        formed = {
            k: agg for k, agg in bins.items() if len(agg.families) >= min_families
        }
        fire_formed, fire_dissolved, silent_seed = edge_actions(
            seeded, prev_states, formed.keys()
        )

        # Silent bookkeeping inside the same connection: first-scan seeding,
        # and family-set refreshes on persisting (active, still-formed) bins.
        seeded_now = not seeded
        for key in silent_seed:
            agg = formed[key]
            await ats._upsert_watermark(
                conn,
                TRIGGER_CLASS,
                key,
                {
                    "active": True,
                    "families": sorted(agg.families),
                    "count": agg.signal_count,
                },
                fired=False,
            )
        if seeded_now:
            await ats._mark_seeded(conn, TRIGGER_CLASS)
        else:
            for key, agg in formed.items():
                prev = prev_states.get(key)
                if (
                    prev is not None
                    and bool(prev.get("active"))
                    and key not in fire_formed
                    and sorted(agg.families) != sorted(
                        f for f in (prev.get("families") or [])
                        if isinstance(f, str)
                    )
                ):
                    await ats._upsert_watermark(
                        conn,
                        TRIGGER_CLASS,
                        key,
                        {
                            "active": True,
                            "families": sorted(agg.families),
                            "count": agg.signal_count,
                        },
                        fired=False,
                    )
        await conn.execute(
            _WM_PRUNE_INACTIVE_SQL, TRIGGER_CLASS, ats.SEED_KEY, _WM_PRUNE_DAYS
        )

    candidates: list[ats.AlertCandidate] = [
        _formation_candidate(
            formed[key],
            window_hours=window_hours,
            min_families=min_families,
            desk_by_iso2=desk_by_iso2,
        )
        for key in fire_formed
    ] + [
        _dissolution_candidate(
            key,
            prev_states.get(key) or {},
            window_hours=window_hours,
            min_families=min_families,
            desk_by_iso2=desk_by_iso2,
        )
        for key in fire_dissolved
    ]

    kept, rollup_cands = ats.apply_desk_cap(candidates, per_desk_cap)
    suppressed = sum(
        int(r.data.get("suppressed_count", 0)) for r in rollup_cands
    )

    dispatcher = ats._resolve_dispatcher(deps)
    formed_fired = 0
    dissolved_fired = 0
    rollups_written = 0
    write_failures = 0
    to_fan_out: list[tuple[UUID, ats.AlertCandidate]] = []

    async with pool.acquire() as conn:
        for cand in kept + rollup_cands:
            row_id = await ats._write_alert_row(
                conn,
                cand,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                run_uuid=run_uuid,
            )
            if row_id is None:
                # Watermark NOT advanced: the transition retries next scan.
                write_failures += 1
                continue
            if cand.trigger_class == "rollup":
                rollups_written += 1
            elif cand.data.get("event") == "dissolved":
                dissolved_fired += 1
            else:
                formed_fired += 1
            for wm_class, wm_key, wm_state in cand.watermarks:
                await ats._upsert_watermark(
                    conn, wm_class, wm_key, wm_state, fired=True
                )
            to_fan_out.append((row_id, cand))

    if dispatcher is None and to_fan_out:
        logger.warning(
            "geo_convergence_scan.fanout_unavailable alerts=%d — no "
            "alert_sink_dispatcher wired (rows persisted; outward delivery "
            "skipped this run)",
            len(to_fan_out),
        )
    elif dispatcher is not None:
        for row_id, cand in to_fan_out:
            try:
                await dispatcher.fan_out(_sink_payload(row_id, cand))
            except Exception as exc:  # noqa: BLE001 — never-raise fan-out contract
                logger.warning(
                    "geo_convergence_scan.fanout_failed alert_row=%s err=%s",
                    row_id,
                    exc,
                )

    if formed_fired or dissolved_fired or seeded_now:
        logger.info(
            "geo_convergence_scan.done formed=%d dissolved=%d rollups=%d "
            "seeded=%s formed_bins=%d write_failures=%d",
            formed_fired,
            dissolved_fired,
            rollups_written,
            seeded_now,
            len(formed),
            write_failures,
        )

    finding = _build_summary(
        seeded_now=seeded_now,
        formed_fired=formed_fired,
        dissolved_fired=dissolved_fired,
        rollups=rollups_written,
        suppressed=suppressed,
        write_failures=write_failures,
        active_bins=len(formed),
        cell_bins_formed=sum(
            1 for a in formed.values() if a.bin_kind == "cell"
        ),
        country_bins_formed=sum(
            1 for a in formed.values() if a.bin_kind == "country"
        ),
        point_signals=len(point_rows),
        country_signal_rows=len(country_rows),
        window_hours=window_hours,
        min_families=min_families,
        per_desk_cap=per_desk_cap,
    )
    # Quiet steady-state sweep (nothing fired, nothing seeded) → trace-only:
    # the feed only ever carries real convergence events (indicator_tracker
    # pattern; the FINDING kind itself is what the structural-exempt drift
    # guard keys on).
    quiet = not (
        seeded_now or formed_fired or dissolved_fired or write_failures
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=quiet,
    )


__all__ = [
    "BinAgg",
    "bin_label",
    "build_bins",
    "cell_key",
    "convergence_score",
    "country_key",
    "edge_actions",
    "handle",
    "source_family",
]

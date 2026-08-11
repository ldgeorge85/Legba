# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``alert_trigger_scan`` sub-handler — P1-3 trigger set v1 (verification-gated
alerting).

A deterministic META analyst on a ~10-minute cadence that watches VERIFIED
substrate state for TRANSITIONS and side-writes one ``kind='alert'``
``analyst_outputs`` row per fired trigger, then fans each outward through the
shared P1-1 :class:`legba.data.alerts.AlertSinkDispatcher` (ledger row per sink
outcome + webhook when configured). Seven trigger classes:

  1. **band_crossing** — a desk×dimension scorecard band changed vs the
     previous scorecard row (the T1 bands rest ONLY on verified claims, so a
     crossing is a verified-state transition by construction). Fires on BOTH
     directions; deterioration (up the risk ladder) = ``high``, improvement =
     ``medium``, transitions in/out of ``insufficient-evidence`` = ``medium``
     with an explicit evidence-gained / evidence-lost direction (an evidence
     change is a coverage event, not a risk read).
  2. **verified_finding** — a NEW high-severity finding that MET the verified
     bar: ``severity >= high`` AND a faithfulness-verify critique exists AND
     ``effective_confidence = min(confidence, faithfulness) >= floor`` (0.50),
     not superseded, and not from a verify-exempt structural analyst
     (:data:`~legba.data.provenance.kinds.STRUCTURAL_VERIFY_EXEMPT_ANALYSTS` —
     those never enter the verify pass, so they can never meet the bar; the
     exclusion makes that explicit rather than incidental).
  3. **contention_flip** — a ``fact_contention`` group changed state
     (status / surfaced winner) or newly appeared, AND at least one of its
     supporting facts is cited (``derived_from``) by a non-superseded finding
     that met the SAME verified bar. The verified tie is what makes a
     contention product-relevant (a dispute among junk facts nobody's verified
     finding rests on is not an operator page). ``medium``.
  4. **baseline_deviation** — per desk, the last-24h signal volume or
     high-severity finding count exceeded the trailing 28-day same-desk
     baseline (simple mean + 2σ over zero-filled 24h buckets, computed in SQL)
     — RISING-EDGE only (fires when a desk crosses INTO exceedance, re-arms
     when it falls back out), with an absolute minimum-count floor so a
     2-vs-0.1 blip on a quiet desk cannot page. ``medium``.
  5. **watchlist_hit** (P5-6, Watchlist v2) — an OPERATOR-defined standing
     watch (the ``watchlist`` table, migration 0105: an entity, a free-text
     topic, or a place) matched a VERIFIED finding in the scan window —
     regardless of desk or severity (unless the watch sets ``min_severity``).
     Same verified bar as class 2, WIDENED to count structural-verify-exempt
     analysts (deterministic, no LLM prose — "structural-verified"). One
     watermark per (watch, finding); a new watch never pages findings that
     predate it; at most ``per_watch_cap`` (default 3) alerts per watch per
     scan, remainder folded into one honest per-watch rollup. Matching
     semantics + design rationale: :mod:`._watchlist_scan`.
  6. **geo_convergence** (A7, folded 2026-07-29) — signals from at least
     ``geo_min_distinct_families`` (default 3) DISTINCT source families
     converged in one geographic bin (1°×1° cell for point-trustworthy
     geocodes, or a country bin for ISO2-tagged signals) over the trailing
     ``geo_window_hours`` (default 24h) — diversity is the signal; a
     same-family pile-on never fires. Fires on the FORMATION edge (``medium``)
     and once on the DISSOLUTION edge (``info``); a persisting convergence
     never refires. Folded from the former standalone ``geo_convergence_scan``
     analyst — same firing conditions, same payload content, same
     ``trigger_class='geo_convergence'`` watermark namespace (no migration,
     no watermark discontinuity); only the anti-noise per-desk cap changed
     (now shared across all six classes rather than an isolated per-analyst
     budget). Binning tiers, source-family fold, and scoring rationale:
     :mod:`.geo_convergence_scan`.
  7. **production_deficit** (S-1, 2026-08-03) — the only class whose subject
     is THIS ENGINE rather than the world. A producing loop — an analyst's
     cadence, an analyst's output, a source's signal production, a declared
     backlog's drain — has produced nothing measured against ITS OWN declared
     cadence and trailing history. Expectations are derived from descriptors
     and observed production, so a newly-activated analyst or source is gauged
     from its first tick with no code change. Fires at ``medium`` and worse
     only, once on appearance and again on each ESCALATION rung; recovery is
     silent. Judgment lives in
     :mod:`legba.data.registry.production_gauge` — the SAME function the
     ``/v3/system/production-gauge`` route reads, so a threshold cannot mean
     one thing on the table and another on the phone. Adapter and paging
     policy: :mod:`._production_deficit_scan`.

     This class exists because the engine's characteristic failure is silent
     ABSENCE, not error (ENGINE_REVIEW_2026-08-02 §1): five AP feeds polled
     130 times each over six days, every poll ``success`` and ``healthy``,
     writing zero signals, and no alert ever fired for any of them.

Statefulness — the no-refire contract
-------------------------------------
Every trigger class keeps a durable last-seen watermark in
``alert_trigger_watermarks`` (migration 0091): band crossings and baseline
deviations upsert one row per desk×dimension / desk×metric whose ``state``
fingerprints the last-seen value; verified findings append one row per finding
id (pruned once older than the scan window); contentions upsert one row per
contention id fingerprinting status + surfaced winner; production deficits
upsert one row per loop fingerprinting the deficit's severity RANK, so an
ongoing deficit stays silent while an escalation fires. A transition fires when
— and only when — the live value differs from the watermark, and the watermark
is advanced ONLY after the alert row landed (a failed write retries next scan).
The analyst's FIRST-EVER scan of each class seeds watermarks silently (a
per-class ``_seeded`` marker row) and fires NOTHING — bringing the analyst up
on a live substrate must not page the operator with history. The
production_deficit seed is loud in the RECEIPT even so (``seeded_deficits``
plus a WARNING line naming each adopted loop), because for a gauge the
standing backlog IS the news and a silent adoption could read as "all clear".

Anti-noise at the trigger tier
------------------------------
Per desk (target_id; desk-less contention flips share a ``_global`` bucket) at
most ``per_desk_cap`` (default 3) alerts per scan, worst-first by severity;
the remainder is folded into ONE per-desk rollup alert whose count and
per-alert summaries are stated honestly — and whose members' watermarks ARE
advanced (they were reported, in rollup form; a rollup is not a refire ticket).
The dispatcher's per-sink cooldown + per-alert-row idempotency remain the
downstream floor (P1-1).

Output path
-----------
The run's own returned summary is a TRACE_ONLY receipt (fully audited in
``analyst_traces``); the REAL product is the side-written ``kind='alert'``
rows, whose ``derived_from`` NAMES the rows the trigger rests on (scorecard
row + band basis findings / the verified finding / the contention's supporting
facts + tied finding) so the P1 lineage walk resolves every alert. Outward
fan-out builds the converged :class:`~legba.data.alerts.AlertSinkPayload`
per alert (summary / severity / target / verify state / receipt link with
``row_kind='alert'``) and pushes it through the SHARED runtime dispatcher —
resolved from ``deps.extras['alert_sink_dispatcher']`` or the
``AGENCY_HOLDER`` the escalation edge already uses — so trigger alerts share
the escalation edge's ledger, cooldown and idempotency state. No dispatcher
wired ⇒ the rows are still durable and the receipt says the fan-out was
unavailable (visible, never silent).

Registered via ``scripts/bringup_register_alert_trigger_scan.py`` — NOT inline
through a test fixture.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Optional
from uuid import UUID, uuid4

from ...provenance import AnalystContext, write_analyst_output
from ...provenance.kinds import (
    STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
    OutputKind,
)
from ...provenance.models import AlertPayload, FindingPayload, severity_from_tags
from ....runtime.analyst_method import AnalystMethodResult
from . import (
    _production_deficit_scan,
    _situation_escalation_scan,
    _watchlist_scan,
    geo_convergence_scan,
    scorecard_banding,
)

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "alert_trigger_scan"

#: The dispatcher channel name every trigger alert carries (mirrors
#: 'escalations' / 'liveness_stall' on the other two P1-1 edges).
CHANNEL_NAME = "trigger_scan"

# Trigger-class identifiers (the watermark table's trigger_class values).
TRIGGER_BAND = "band_crossing"
TRIGGER_FINDING = "verified_finding"
TRIGGER_CONTENTION = "contention_flip"
TRIGGER_BASELINE = "baseline_deviation"
TRIGGER_WATCHLIST = "watchlist_hit"
#: Folded 2026-07-29 from the standalone geo_convergence_scan analyst — the
#: SAME trigger_class string that module always used, so existing
#: alert_trigger_watermarks rows continue under this handler with no
#: migration and no watermark discontinuity.
TRIGGER_GEO_CONVERGENCE = "geo_convergence"
#: S-1 (2026-08-03) — the engine watching ITSELF: a producing loop (analyst
#: cadence, analyst output, source signal production, declared backlog drain)
#: that has produced nothing against its own trailing expectation. The only
#: class whose subject is Legba rather than the world. Judgment lives in
#: legba.data.registry.production_gauge (shared with the v3 route); see
#: _production_deficit_scan for the adapter.
TRIGGER_PRODUCTION_DEFICIT = "production_deficit"
#: Continuity P2 — a VERIFIED `escalates` row on the trajectory ledger; the
#: first class whose subject is a FRAME. Bar + judgment: the sibling module.
TRIGGER_SITUATION_ESCALATION = _situation_escalation_scan.TRIGGER_CLASS

#: Per-class seed marker key — present ⇒ the class completed its first scan.
SEED_KEY = "_seeded"

#: Cap bucket for desk-less alerts (contention flips carry no target_id).
GLOBAL_DESK_KEY = "_global"

#: Default per-desk alert cap per scan (worst-first; remainder → one rollup).
DEFAULT_PER_DESK_CAP = 3

#: The verified bar: effective_confidence = min(confidence, faithfulness)
#: must clear this. Deliberately THE same floor as the scorecard banding's
#: dedicated faithfulness floor (the system-wide 0.50 decision).
DEFAULT_EFFECTIVE_CONF_FLOOR = scorecard_banding.FAITH_FLOOR

#: verified_finding scan window (hours). Wider than the cadence so a critique
#: that lands AFTER its finding (verify is a separate pass) is still seen; the
#: per-finding-id watermark is what prevents refire inside the window.
DEFAULT_FINDING_WINDOW_HOURS = 24

#: verified_finding watermark rows older than this are pruned (they can no
#: longer match the scan window, so keeping them buys nothing).
_FINDING_WATERMARK_PRUNE_DAYS = 7

#: baseline_deviation: trailing same-desk baseline depth (24h buckets).
DEFAULT_BASELINE_DAYS = 28
#: baseline_deviation: exceedance threshold in sigmas over the baseline mean.
DEFAULT_BASELINE_SIGMA = 2.0
#: Absolute floors — a desk must at least reach these in the current 24h
#: window before a statistical exceedance can fire (guards the σ≈0 quiet-desk
#: degenerate case where 2 signals over a 0.1 mean would otherwise page).
MIN_CURRENT_SIGNALS = 10
MIN_CURRENT_FINDINGS = 3

#: Bounds (defensive) on per-scan result sizes.
_MAX_FINDINGS_PER_SCAN = 200
_MAX_CONTENTIONS_PER_SCAN = 500
_MAX_DESKS = 200
#: Cap on derived_from refs carried per alert row (lineage stays skimmable).
_MAX_DERIVED_REFS = 8

_SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}

#: Worst-first tie-break across classes at equal severity (band deterioration
#: and verified findings ahead of operator watch hits, ahead of
#: flips/deviations, ahead of geo-convergence formations — the newest fold,
#: given no established relative order against the other five pre-fold, so
#: it is conservatively ranked last rather than displacing any of them).
#: production_deficit takes the same conservative treatment: appended, never
#: displacing an established pair. It costs it nothing in practice —
#: apply_desk_cap sorts by SEVERITY first and this class only ever emits
#: medium/high/critical, so a real production deficit still outranks a
#: lower-severity world event regardless of where it sits here.
_CLASS_PRIORITY = {
    TRIGGER_BAND: 0,
    TRIGGER_FINDING: 1,
    TRIGGER_WATCHLIST: 2,
    TRIGGER_CONTENTION: 3,
    TRIGGER_BASELINE: 4,
    TRIGGER_GEO_CONVERGENCE: 5,
    TRIGGER_PRODUCTION_DEFICIT: 6,
    TRIGGER_SITUATION_ESCALATION: 7,
}

#: geo_convergence scan window / diversity-bar defaults — the CANONICAL
#: values live in geo_convergence_scan (this class's former standalone
#: module); referenced here, never duplicated, so the two cannot drift.
DEFAULT_GEO_WINDOW_HOURS = geo_convergence_scan.DEFAULT_WINDOW_HOURS
DEFAULT_GEO_MIN_DISTINCT_FAMILIES = geo_convergence_scan.DEFAULT_MIN_DISTINCT_FAMILIES


# ---------------------------------------------------------------------------
# Candidate + pure helpers (testable with NO database)
# ---------------------------------------------------------------------------


@dataclass
class AlertCandidate:
    """One would-be alert plus the watermark upserts that record it as seen.

    ``watermarks`` is a list of ``(trigger_class, key, state)`` rows to upsert
    AFTER the alert row (or its rollup) durably landed — the ordering that
    makes a failed write retry instead of silently losing the transition.
    """

    trigger_class: str
    severity: str
    title: str
    body: str
    target_id: Optional[str]
    derived_from: list[UUID] = field(default_factory=list)
    data: dict[str, Any] = field(default_factory=dict)
    watermarks: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    effective_confidence: Optional[float] = None
    faithfulness_score: Optional[float] = None
    event_at: Optional[datetime] = None

    @property
    def desk_key(self) -> str:
        return self.target_id or GLOBAL_DESK_KEY


def classify_band_transition(from_band: str, to_band: str) -> tuple[str, str]:
    """(direction, severity) for one band change.

    Ladder→ladder: up the risk ladder = ``deterioration``/``high``; down =
    ``improvement``/``medium`` (the P1-3 severity mapping). Transitions in/out
    of ``insufficient-evidence`` are coverage events, not risk reads:
    ``evidence-gained`` / ``evidence-lost`` at ``medium``. An off-ladder value
    (defensive — the banding engine only emits ladder values + INSUFFICIENT)
    reads ``indeterminate``/``medium`` so a real change is never dropped.
    """
    ladder = scorecard_banding.BAND_LADDER
    ins = scorecard_banding.INSUFFICIENT
    if from_band == ins and to_band in ladder:
        return "evidence-gained", "medium"
    if to_band == ins and from_band in ladder:
        return "evidence-lost", "medium"
    if from_band in ladder and to_band in ladder:
        if ladder.index(to_band) > ladder.index(from_band):
            return "deterioration", "high"
        return "improvement", "medium"
    return "indeterminate", "medium"


def baseline_exceeds(
    current: float,
    mean: float,
    sigma: float,
    *,
    min_current: float,
    n_sigma: float = DEFAULT_BASELINE_SIGMA,
) -> bool:
    """The mean+Nσ exceedance test with an absolute minimum-count floor."""
    return current >= min_current and current > mean + n_sigma * sigma


def apply_desk_cap(
    candidates: list[AlertCandidate], cap: int
) -> tuple[list[AlertCandidate], list[AlertCandidate]]:
    """Per-desk cap: keep the worst ``cap`` per desk, fold the rest into ONE
    per-desk rollup candidate (count stated honestly; watermarks carried over
    so the summarized transitions never refire). Returns (kept, rollups).
    """
    by_desk: dict[str, list[AlertCandidate]] = {}
    for cand in candidates:
        by_desk.setdefault(cand.desk_key, []).append(cand)

    kept: list[AlertCandidate] = []
    rollups: list[AlertCandidate] = []
    for desk, cands in sorted(by_desk.items()):
        ordered = sorted(
            cands,
            key=lambda c: (
                -_SEVERITY_RANK.get(c.severity, 0),
                _CLASS_PRIORITY.get(c.trigger_class, 9),
                c.title,
            ),
        )
        kept.extend(ordered[:cap])
        rest = ordered[cap:]
        if not rest:
            continue
        worst = max(
            (c.severity for c in rest), key=lambda s: _SEVERITY_RANK.get(s, 0)
        )
        summaries = [
            {
                "trigger_class": c.trigger_class,
                "severity": c.severity,
                "title": c.title[:200],
            }
            for c in rest
        ]
        merged_watermarks: list[tuple[str, str, dict[str, Any]]] = []
        merged_refs: list[UUID] = []
        for c in rest:
            merged_watermarks.extend(c.watermarks)
            for ref in c.derived_from:
                if ref not in merged_refs and len(merged_refs) < _MAX_DERIVED_REFS:
                    merged_refs.append(ref)
        desk_label = desk if desk != GLOBAL_DESK_KEY else "global"
        rollups.append(
            AlertCandidate(
                trigger_class="rollup",
                severity=worst,
                title=(
                    f"Alert rollup ({desk_label}): {len(rest)} further trigger "
                    f"alert(s) this scan beyond the per-desk cap of {cap}"
                ),
                body="\n".join(
                    f"[{s['severity']}] {s['trigger_class']}: {s['title']}"
                    for s in summaries
                ),
                target_id=None if desk == GLOBAL_DESK_KEY else desk,
                derived_from=merged_refs,
                data={
                    "trigger_class": "rollup",
                    "suppressed_count": len(rest),
                    "per_desk_cap": cap,
                    "suppressed": summaries,
                },
                watermarks=merged_watermarks,
            )
        )
    return kept, rollups


def _parse_jsonish(raw: Any) -> Any:
    """JSONB columns arrive as dict/list (asyncpg codec) or str — normalise."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _uuid_or_none(raw: Any) -> Optional[UUID]:
    try:
        return UUID(str(raw))
    except (ValueError, TypeError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Watermark I/O
# ---------------------------------------------------------------------------

_WM_LOAD_SQL = """
    SELECT watermark_key, state
      FROM alert_trigger_watermarks
     WHERE trigger_class = $1
"""

_WM_UPSERT_SQL = """
    INSERT INTO alert_trigger_watermarks
        (trigger_class, watermark_key, state, fired_at)
    VALUES ($1, $2, $3::jsonb, CASE WHEN $4 THEN now() END)
    ON CONFLICT (trigger_class, watermark_key) DO UPDATE
       SET state = EXCLUDED.state,
           updated_at = now(),
           fired_at = COALESCE(EXCLUDED.fired_at,
                               alert_trigger_watermarks.fired_at)
"""

_WM_PRUNE_FINDINGS_SQL = """
    DELETE FROM alert_trigger_watermarks
     WHERE trigger_class = $1
       AND watermark_key <> $2
       AND first_seen < now() - make_interval(days => $3)
"""


async def _load_class_watermarks(
    conn: Any, trigger_class: str
) -> tuple[bool, dict[str, dict[str, Any]]]:
    """(seeded?, {key: state}) for one trigger class (SEED_KEY excluded)."""
    rows = await conn.fetch(_WM_LOAD_SQL, trigger_class)
    seeded = False
    by_key: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row["watermark_key"]
        if key == SEED_KEY:
            seeded = True
            continue
        state = _parse_jsonish(row["state"])
        by_key[key] = state if isinstance(state, dict) else {}
    return seeded, by_key


async def _upsert_watermark(
    conn: Any,
    trigger_class: str,
    key: str,
    state: Mapping[str, Any],
    *,
    fired: bool,
) -> None:
    await conn.execute(
        _WM_UPSERT_SQL,
        trigger_class,
        key,
        json.dumps(dict(state), separators=(",", ":"), default=str),
        fired,
    )


async def _mark_seeded(conn: Any, trigger_class: str) -> None:
    await _upsert_watermark(
        conn,
        trigger_class,
        SEED_KEY,
        {"seeded_at": datetime.now().astimezone().isoformat()},
        fired=False,
    )


# ---------------------------------------------------------------------------
# Trigger 1 — band crossing (scorecard head vs the watermark's last-seen band)
# ---------------------------------------------------------------------------

_HEAD_SCORECARDS_SQL = """
    SELECT id::text AS row_id, target_id, data, produced_at
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND superseded_by IS NULL
     ORDER BY target_id
"""


async def _scan_band_crossings(
    conn: Any,
) -> tuple[list[AlertCandidate], list[tuple[str, str, dict[str, Any]]], bool]:
    """Returns (candidates, silent_watermark_upserts, was_seeded)."""
    seeded, watermarks = await _load_class_watermarks(conn, TRIGGER_BAND)
    rows = await conn.fetch(_HEAD_SCORECARDS_SQL)

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        desk = str(row["target_id"] or "")
        if not desk:
            continue
        data = _parse_jsonish(row["data"]) or {}
        if not isinstance(data, Mapping):
            continue
        # The analyst_outputs.data column stores the FULL payload dump, so the
        # producer's payload `data` dict is NESTED under 'data' (mirrors the
        # parse_unit_eval contract in scorecard_producer). Accept a bare
        # {'bands': ...} too (defensive).
        inner = data.get("data") if isinstance(data.get("data"), Mapping) else data
        bands = inner.get("bands") if isinstance(inner, Mapping) else None
        dims = bands.get("dimensions") if isinstance(bands, Mapping) else None
        if not isinstance(dims, Mapping):
            continue
        row_id = str(row["row_id"])
        for dim, verdict in dims.items():
            if not isinstance(verdict, Mapping):
                continue
            band = str(verdict.get("band") or "")
            if not band:
                continue
            key = f"{desk}|{dim}"
            state = {"band": band, "scorecard_row_id": row_id}
            prev = watermarks.get(key)
            if not seeded or prev is None:
                # First-ever scan of the class, or a desk×dimension appearing
                # for the first time: seed silently (no previous row to
                # transition FROM).
                silent.append((TRIGGER_BAND, key, state))
                continue
            prev_band = str(prev.get("band") or "")
            if prev_band == band:
                if prev.get("scorecard_row_id") != row_id:
                    # New scorecard row, same band — bookkeeping only.
                    silent.append((TRIGGER_BAND, key, state))
                continue
            direction, severity = classify_band_transition(prev_band, band)
            refs: list[UUID] = []
            rid = _uuid_or_none(row_id)
            if rid is not None:
                refs.append(rid)
            for bid in verdict.get("basis") or []:
                b = _uuid_or_none(bid)
                if b is not None and b not in refs and len(refs) < _MAX_DERIVED_REFS:
                    refs.append(b)
            transition_key = f"{desk}|{dim}|{prev_band}->{band}|{row_id}"
            candidates.append(
                AlertCandidate(
                    trigger_class=TRIGGER_BAND,
                    severity=severity,
                    title=(
                        f"Band {direction}: {desk} {dim} "
                        f"{prev_band} → {band}"
                    ),
                    body=(
                        f"desk={desk} dimension={dim}\n"
                        f"from={prev_band} to={band} direction={direction}\n"
                        f"scorecard_row={row_id} "
                        f"prev_scorecard_row={prev.get('scorecard_row_id')}\n"
                        f"band_reason={verdict.get('reason')} "
                        f"effective_confidence={verdict.get('effective_confidence')}"
                    ),
                    target_id=desk,
                    derived_from=refs,
                    data={
                        "trigger_class": TRIGGER_BAND,
                        "desk": desk,
                        "dimension": str(dim),
                        "from_band": prev_band,
                        "to_band": band,
                        "direction": direction,
                        "scorecard_row_id": row_id,
                        "prev_scorecard_row_id": prev.get("scorecard_row_id"),
                        "transition_key": transition_key,
                        "band_basis": [str(b) for b in (verdict.get("basis") or [])],
                    },
                    watermarks=[(TRIGGER_BAND, key, state)],
                    event_at=row["produced_at"],
                )
            )
    return candidates, silent, seeded


# ---------------------------------------------------------------------------
# Trigger 2 — new high-severity VERIFIED finding
# ---------------------------------------------------------------------------

# INNER lateral join: the verified bar REQUIRES a faithfulness verdict — a
# finding with no critique cannot qualify, so (unlike the banding gather) there
# is nothing honest to report about its absence here.
_VERIFIED_FINDINGS_SQL = """
    SELECT f.id::text            AS finding_id,
           f.analyst_id          AS analyst_id,
           f.target_id           AS target_id,
           f.title               AS title,
           f.confidence          AS confidence,
           f.severity            AS severity,
           f.data -> 'tags'      AS tags,
           f.produced_at         AS produced_at,
           v.faithfulness_score  AS faithfulness_score
      FROM analyst_outputs f
      JOIN LATERAL (
          SELECT (cr.data->>'overall_score')::real AS faithfulness_score
            FROM analyst_outputs cr
           WHERE cr.kind = 'critique'
             AND cr.data->>'analyzed_output_id' = f.id::text
             AND cr.data->>'overall_score' IS NOT NULL
             AND cr.title LIKE 'Faithfulness verify%'
           ORDER BY cr.produced_at DESC, cr.id DESC
           LIMIT 1
      ) v ON TRUE
     WHERE f.kind = 'finding'
       AND f.superseded_by IS NULL
       AND f.produced_at > now() - make_interval(hours => $1)
       AND f.analyst_id <> ALL($2::text[])
       AND (
             f.severity IN ('high', 'critical')
             OR f.data -> 'tags' ?| ARRAY['severity:high', 'severity:critical']
           )
       AND LEAST(f.confidence, v.faithfulness_score) >= $3
       AND NOT EXISTS (
             SELECT 1 FROM alert_trigger_watermarks w
              WHERE w.trigger_class = $4
                AND w.watermark_key = f.id::text
       )
     ORDER BY f.produced_at DESC
     LIMIT $5
"""


def _finding_severity(row: Mapping[str, Any]) -> str:
    """The finding's resolved severity level ('high' or 'critical' here — the
    SQL already gated) — column first, tag fallback (write-path lift mirror)."""
    col = str(row.get("severity") or "").strip().lower()
    if col in ("high", "critical"):
        return col
    tag = severity_from_tags(scorecard_banding._coerce_tags(row.get("tags")))
    return tag if tag in ("high", "critical") else "high"


async def _scan_verified_findings(
    conn: Any,
    *,
    floor: float,
    window_hours: int,
) -> tuple[list[AlertCandidate], list[tuple[str, str, dict[str, Any]]], bool]:
    seeded, _ = await _load_class_watermarks(conn, TRIGGER_FINDING)
    # Prune aged-out per-finding rows (they can no longer match the window).
    await conn.execute(
        _WM_PRUNE_FINDINGS_SQL,
        TRIGGER_FINDING,
        SEED_KEY,
        _FINDING_WATERMARK_PRUNE_DAYS,
    )
    rows = await conn.fetch(
        _VERIFIED_FINDINGS_SQL,
        int(window_hours),
        sorted(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS),
        float(floor),
        TRIGGER_FINDING,
        _MAX_FINDINGS_PER_SCAN,
    )

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        finding_id = str(row["finding_id"])
        conf = float(row["confidence"]) if row["confidence"] is not None else None
        faith = (
            float(row["faithfulness_score"])
            if row["faithfulness_score"] is not None
            else None
        )
        eff = min(conf, faith) if conf is not None and faith is not None else None
        severity = _finding_severity(row)
        state = {
            "severity": severity,
            "effective_confidence": eff,
        }
        if not seeded:
            silent.append((TRIGGER_FINDING, finding_id, state))
            continue
        refs = [r for r in (_uuid_or_none(finding_id),) if r is not None]
        candidates.append(
            AlertCandidate(
                trigger_class=TRIGGER_FINDING,
                # P1-3 mapping: new-high-sev = high; a critical finding keeps
                # its own (strictly higher) level rather than being understated.
                severity="critical" if severity == "critical" else "high",
                title=(
                    f"Verified {severity}-severity finding: "
                    f"{str(row['title'] or '')[:160]}"
                ),
                body=(
                    f"analyst={row['analyst_id']} target={row['target_id']}\n"
                    f"finding={finding_id}\n"
                    f"severity={severity} confidence={conf} "
                    f"faithfulness={faith} effective_confidence={eff} "
                    f"(floor={floor})"
                ),
                target_id=str(row["target_id"]) if row["target_id"] else None,
                derived_from=refs,
                data={
                    "trigger_class": TRIGGER_FINDING,
                    "finding_id": finding_id,
                    "finding_analyst_id": str(row["analyst_id"] or ""),
                    "finding_severity": severity,
                    "confidence": conf,
                    "faithfulness_score": faith,
                    "effective_confidence": eff,
                    "effective_conf_floor": floor,
                },
                watermarks=[(TRIGGER_FINDING, finding_id, state)],
                effective_confidence=eff,
                faithfulness_score=faith,
                event_at=row["produced_at"],
            )
        )
    return candidates, silent, seeded


# ---------------------------------------------------------------------------
# Trigger 3 — contention flip (verified-tied fact_contention state change)
# ---------------------------------------------------------------------------

# Every contention group + its non-junk supporting fact ids + the SIGNALS those
# facts were derived from + (when one exists) the freshest non-superseded
# finding that RESTS ON the dispute and meets the verified bar. The GIN indexes
# on analyst_outputs.derived_from and facts.derived_from carry the && probes;
# the contention table is small by construction.
#
# W1-C3 — WHY THE SIGNAL BRIDGE. Until 2026-08-03 this LATERAL matched
# `f.derived_from && v.fact_ids` alone, and the class had NEVER fired an alert
# in its life despite 1,606 watermark rows. The reason is not tuning, it is a
# type mismatch between two id populations that never meet:
#
#   * a contention group tracks FACT ids — all 7,602 non-junk
#     `supporting_fact_ids` live in `facts`;
#   * a finding's `derived_from` holds SIGNAL ids — of 229,768 refs carried by
#     findings in the trailing 7 days, 221,268 resolved to `signals`, 7,874 to
#     other `analyst_outputs`, and **0 to `facts`**. All-time it is 34 of
#     757,436 (0.0045%).
#
# So the join could not fire, and did not: exactly 1 of 2,152 groups with
# supporting facts had ANY finding citing them, verified or not. That is a
# structurally impossible predicate reported as a quiet gauge — the worst shape
# a trigger can have, because "0 alerts" reads as "nothing happened".
#
# The bridge is the substrate's own lineage, not a heuristic: a fact carries the
# signals it was derived from (`facts.derived_from`), and a finding cites those
# same signals. A finding "rests on" a contested fact when it cites evidence
# that fact was built from. Measured on the live substrate: 1,243 of 2,152
# groups (58%) acquire a live finding under the bridge, and the shipped query
# shape resolves 157 verified-bar findings over the 500-group scan window
# (vs 0), with no latency regression (9.4s bridged vs 12.5s before).
#
# Volume is NOT unbounded by this change: the trigger still fires only on a
# status/surfaced_fact_id CHANGE against the watermark, and real surface changes
# run ~40/day fleet-wide (352 all-time supersessions in `surface_history`), so
# expect low tens of medium-severity candidates/day, further bounded by
# `apply_desk_cap` (3/desk/scan) + rollup coalescing like every other class.
_CONTENTIONS_SQL = """
    SELECT c.id::text          AS contention_id,
           c.subject_key       AS subject_key,
           c.predicate_key     AS predicate_key,
           c.status            AS status,
           c.surfaced_value    AS surfaced_value,
           c.surfaced_fact_id::text AS surfaced_fact_id,
           c.value_count       AS value_count,
           c.updated_at        AS updated_at,
           v.fact_ids          AS fact_ids,
           vf.finding_id       AS verified_finding_id,
           vf.eff_conf         AS verified_eff_conf
      FROM fact_contention c
      LEFT JOIN LATERAL (
          SELECT COALESCE(array_agg(DISTINCT u.fid), '{}'::uuid[]) AS fact_ids,
                 COALESCE(
                     array_agg(DISTINCT s.sid) FILTER (WHERE s.sid IS NOT NULL),
                     '{}'::uuid[]
                 ) AS evidence_ids
            FROM (
              SELECT unnest(fcv.supporting_fact_ids) AS fid
                FROM fact_contention_values fcv
               WHERE fcv.contention_id = c.id
                 AND NOT fcv.is_junk
            ) u
            LEFT JOIN LATERAL (
              SELECT unnest(f2.derived_from) AS sid
                FROM facts f2
               WHERE f2.id = u.fid
            ) s ON TRUE
      ) v ON TRUE
      LEFT JOIN LATERAL (
          SELECT f.id::text AS finding_id,
                 LEAST(f.confidence, cr.faithfulness_score) AS eff_conf
            FROM analyst_outputs f
            JOIN LATERAL (
                SELECT (c2.data->>'overall_score')::real AS faithfulness_score
                  FROM analyst_outputs c2
                 WHERE c2.kind = 'critique'
                   AND c2.data->>'analyzed_output_id' = f.id::text
                   AND c2.data->>'overall_score' IS NOT NULL
                   AND c2.title LIKE 'Faithfulness verify%'
                 ORDER BY c2.produced_at DESC, c2.id DESC
                 LIMIT 1
            ) cr ON TRUE
           WHERE f.kind = 'finding'
             AND f.superseded_by IS NULL
             AND f.derived_from && (v.fact_ids || v.evidence_ids)
             AND f.analyst_id <> ALL($1::text[])
             AND LEAST(f.confidence, cr.faithfulness_score) >= $2
           ORDER BY f.produced_at DESC, f.id DESC
           LIMIT 1
      ) vf ON TRUE
     ORDER BY c.updated_at DESC
     LIMIT $3
"""


async def _scan_contention_flips(
    conn: Any,
    *,
    floor: float,
) -> tuple[list[AlertCandidate], list[tuple[str, str, dict[str, Any]]], bool]:
    seeded, watermarks = await _load_class_watermarks(conn, TRIGGER_CONTENTION)
    rows = await conn.fetch(
        _CONTENTIONS_SQL,
        sorted(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS),
        float(floor),
        _MAX_CONTENTIONS_PER_SCAN,
    )

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for row in rows:
        cid = str(row["contention_id"])
        state = {
            "status": str(row["status"] or ""),
            "surfaced_fact_id": row["surfaced_fact_id"],
        }
        prev = watermarks.get(cid)
        verified_finding_id = row["verified_finding_id"]
        if not seeded:
            silent.append((TRIGGER_CONTENTION, cid, state))
            continue
        if prev is None:
            change = "new-contention"
        elif (
            str(prev.get("status") or "") != state["status"]
            or prev.get("surfaced_fact_id") != state["surfaced_fact_id"]
        ):
            change = f"{prev.get('status') or '?'}->{state['status']}"
        else:
            continue  # unchanged
        if verified_finding_id is None:
            # No verified finding rests on this dispute — record the state so
            # the SAME change can't fire later, but page nobody.
            silent.append((TRIGGER_CONTENTION, cid, state))
            continue

        refs: list[UUID] = []
        vf = _uuid_or_none(verified_finding_id)
        if vf is not None:
            refs.append(vf)
        for fid in list(row["fact_ids"] or []):
            f = _uuid_or_none(fid)
            if f is not None and f not in refs and len(refs) < _MAX_DERIVED_REFS:
                refs.append(f)
        candidates.append(
            AlertCandidate(
                trigger_class=TRIGGER_CONTENTION,
                severity="medium",
                title=(
                    f"Contention {change}: "
                    f"{row['subject_key']} / {row['predicate_key']} "
                    f"[{state['status']}]"
                ),
                body=(
                    f"contention={cid}\n"
                    f"subject={row['subject_key']} predicate={row['predicate_key']}\n"
                    f"change={change} status={state['status']} "
                    f"surfaced_value={row['surfaced_value']} "
                    f"value_count={row['value_count']}\n"
                    f"verified_finding={verified_finding_id} "
                    f"(effective_confidence={row['verified_eff_conf']}, "
                    f"floor={floor})"
                ),
                target_id=None,
                derived_from=refs,
                data={
                    "trigger_class": TRIGGER_CONTENTION,
                    "contention_id": cid,
                    "subject_key": str(row["subject_key"] or ""),
                    "predicate_key": str(row["predicate_key"] or ""),
                    "change": change,
                    "from_state": dict(prev) if prev else None,
                    "to_state": state,
                    "surfaced_value": row["surfaced_value"],
                    "value_count": int(row["value_count"] or 0),
                    "verified_finding_id": str(verified_finding_id),
                    "verified_effective_confidence": (
                        float(row["verified_eff_conf"])
                        if row["verified_eff_conf"] is not None
                        else None
                    ),
                },
                watermarks=[(TRIGGER_CONTENTION, cid, state)],
                event_at=row["updated_at"],
            )
        )
    return candidates, silent, seeded


# ---------------------------------------------------------------------------
# Trigger 4 — deviation vs the trailing same-desk baseline
# ---------------------------------------------------------------------------

_DESKS_SQL = """
    SELECT descriptor_id,
           body -> 'scope' -> 'geo' AS geo
      FROM target_descriptors
     WHERE is_head = TRUE
       AND COALESCE(state, 'active') <> 'retired'
       AND (body -> 'scope' -> 'tags') ?| array['g20', 'watch']
     ORDER BY descriptor_id
     LIMIT $1
"""

# Zero-filled 24h buckets relative to NOW: bucket 0 = the current 24h window,
# buckets 1..N = the trailing baseline. One pass over the (indexed) window.
_SIGNAL_BUCKETS_SQL = """
    WITH hits AS (
        SELECT floor(extract(epoch FROM (now() - fetched_at)) / 86400.0)::int
                 AS bucket
          FROM signals
         WHERE geo && $1::text[]
           AND fetched_at > now() - make_interval(days => $2 + 1)
    ), counts AS (
        SELECT gs.n AS bucket, count(h.bucket) AS c
          FROM generate_series(0, $2::int) gs(n)
          LEFT JOIN hits h ON h.bucket = gs.n
         GROUP BY gs.n
    )
    SELECT max(c) FILTER (WHERE bucket = 0)::float              AS current,
           avg(c) FILTER (WHERE bucket >= 1)::float             AS mean,
           COALESCE(stddev_samp(c) FILTER (WHERE bucket >= 1), 0)::float
                                                                AS sigma
      FROM counts
"""

_FINDING_BUCKETS_SQL = """
    WITH hits AS (
        SELECT floor(extract(epoch FROM (now() - produced_at)) / 86400.0)::int
                 AS bucket
          FROM analyst_outputs
         WHERE kind = 'finding'
           AND target_id = $1
           AND produced_at > now() - make_interval(days => $2 + 1)
           AND (
                 severity IN ('high', 'critical')
                 OR data -> 'tags' ?| ARRAY['severity:high', 'severity:critical']
               )
    ), counts AS (
        SELECT gs.n AS bucket, count(h.bucket) AS c
          FROM generate_series(0, $2::int) gs(n)
          LEFT JOIN hits h ON h.bucket = gs.n
         GROUP BY gs.n
    )
    SELECT max(c) FILTER (WHERE bucket = 0)::float              AS current,
           avg(c) FILTER (WHERE bucket >= 1)::float             AS mean,
           COALESCE(stddev_samp(c) FILTER (WHERE bucket >= 1), 0)::float
                                                                AS sigma
      FROM counts
"""

METRIC_SIGNAL_VOLUME = "signal_volume_24h"
METRIC_HIGH_SEV_FINDINGS = "high_sev_findings_24h"

# ---------------------------------------------------------------------------
# E-1 (2026-07-27 sweep item 4) — the ``desk_baselines`` sidecar (mig 0103,
# refreshed daily by the P3-7 ``desk_baseline`` analyst) is the PERSISTENT
# version of the baseline this trigger used to recompute inline every scan —
# same metrics, same window shape, same absolute floors (the sidecar module
# IMPORTS them from here, so the two cannot drift). When a fresh sidecar row
# exists for a (desk, metric), the trigger PREFERS its stored (expected,
# robust_sigma) as (mean, sigma) and only computes the LIVE current-24h count
# (a cheap 1-window count instead of the full trailing-window bucket scan).
# Absent/stale sidecar (the analyst is newly activated; the table populates
# ~05:17Z) → the inline bucket compute, byte-for-byte the prior behavior.
# Table missing / read failure → same inline fallback (degrade-not-drop).
# ---------------------------------------------------------------------------

#: A sidecar row older than this is STALE and falls back to the inline compute
#: (2× the daily refresh cadence — one missed refresh tolerated, not two).
SIDECAR_FRESH_HOURS = 48

_SIDECAR_BASELINES_SQL = """
    SELECT desk_id, metric, expected, robust_sigma, computed_at
      FROM desk_baselines
     WHERE computed_at > now() - make_interval(hours => $1)
"""

# Live current-24h counts only (bucket 0 of the buckets SQL, without the
# trailing-window scan) — used on the sidecar-preferred path, where the
# baseline mean/sigma come from the stored row but the CURRENT window (the
# thing the edge detector triggers on) must always be live.
_SIGNAL_CURRENT_SQL = """
    SELECT count(*)::float AS current
      FROM signals
     WHERE geo && $1::text[]
       AND fetched_at > now() - interval '24 hours'
"""

_FINDING_CURRENT_SQL = """
    SELECT count(*)::float AS current
      FROM analyst_outputs
     WHERE kind = 'finding'
       AND target_id = $1
       AND produced_at > now() - interval '24 hours'
       AND (
             severity IN ('high', 'critical')
             OR data -> 'tags' ?| ARRAY['severity:high', 'severity:critical']
           )
"""

BASELINE_SOURCE_SIDECAR = "desk_baselines"
BASELINE_SOURCE_INLINE = "inline"


async def _load_fresh_sidecar_baselines(
    conn: Any,
) -> dict[tuple[str, str], Mapping[str, Any]]:
    """Fresh (< ``SIDECAR_FRESH_HOURS``) ``desk_baselines`` rows keyed by
    (desk_id, metric). Degrade-not-drop: any read failure (including the table
    not existing yet) returns {} → every desk takes the inline path unchanged.
    """
    try:
        rows = await conn.fetch(_SIDECAR_BASELINES_SQL, int(SIDECAR_FRESH_HOURS))
    except Exception as exc:
        logger.warning(
            "alert_trigger_scan.baseline_sidecar.read_failed err=%s — "
            "falling back to inline baseline compute", exc,
        )
        return {}
    return {(str(r["desk_id"]), str(r["metric"])): r for r in rows}


async def _scan_baseline_deviation(
    conn: Any,
    *,
    baseline_days: int,
    n_sigma: float,
) -> tuple[list[AlertCandidate], list[tuple[str, str, dict[str, Any]]], bool]:
    seeded, watermarks = await _load_class_watermarks(conn, TRIGGER_BASELINE)
    desks = await conn.fetch(_DESKS_SQL, _MAX_DESKS)
    sidecar = await _load_fresh_sidecar_baselines(conn)

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    for desk_row in desks:
        desk = str(desk_row["descriptor_id"])
        geo_raw = _parse_jsonish(desk_row["geo"])
        geo = [str(g) for g in geo_raw] if isinstance(geo_raw, list) else []

        # Per metric: (metric, row-with-current/mean/sigma, floor, source).
        # The sidecar-preferred arm reads the stored (expected, robust_sigma)
        # and computes only the live current-24h count; the fallback arm is the
        # pre-existing inline bucket scan, byte-for-byte.
        metrics: list[tuple[str, Any, float, str]] = []
        if geo:
            side = sidecar.get((desk, METRIC_SIGNAL_VOLUME))
            if side is not None:
                cur = await conn.fetchrow(_SIGNAL_CURRENT_SQL, geo)
                metrics.append((
                    METRIC_SIGNAL_VOLUME,
                    {
                        "current": (cur["current"] if cur else 0.0),
                        "mean": side["expected"],
                        "sigma": side["robust_sigma"],
                    },
                    float(MIN_CURRENT_SIGNALS),
                    BASELINE_SOURCE_SIDECAR,
                ))
            else:
                sig = await conn.fetchrow(
                    _SIGNAL_BUCKETS_SQL, geo, int(baseline_days)
                )
                metrics.append((
                    METRIC_SIGNAL_VOLUME, sig, float(MIN_CURRENT_SIGNALS),
                    BASELINE_SOURCE_INLINE,
                ))
        side = sidecar.get((desk, METRIC_HIGH_SEV_FINDINGS))
        if side is not None:
            cur = await conn.fetchrow(_FINDING_CURRENT_SQL, desk)
            metrics.append((
                METRIC_HIGH_SEV_FINDINGS,
                {
                    "current": (cur["current"] if cur else 0.0),
                    "mean": side["expected"],
                    "sigma": side["robust_sigma"],
                },
                float(MIN_CURRENT_FINDINGS),
                BASELINE_SOURCE_SIDECAR,
            ))
        else:
            fnd = await conn.fetchrow(
                _FINDING_BUCKETS_SQL, desk, int(baseline_days)
            )
            metrics.append((
                METRIC_HIGH_SEV_FINDINGS, fnd, float(MIN_CURRENT_FINDINGS),
                BASELINE_SOURCE_INLINE,
            ))

        for metric, row, min_current, baseline_source in metrics:
            if row is None:
                continue
            current = float(row["current"] or 0.0)
            mean = float(row["mean"] or 0.0)
            sigma = float(row["sigma"] or 0.0)
            exceeding = baseline_exceeds(
                current, mean, sigma, min_current=min_current, n_sigma=n_sigma
            )
            key = f"{desk}|{metric}"
            state = {"exceeding": exceeding}
            prev = watermarks.get(key)
            if not seeded or prev is None:
                silent.append((TRIGGER_BASELINE, key, state))
                continue
            was_exceeding = bool(prev.get("exceeding"))
            if exceeding == was_exceeding:
                continue  # steady state (still-high or still-quiet): no edge
            if not exceeding:
                # Falling edge: re-arm silently.
                silent.append((TRIGGER_BASELINE, key, state))
                continue
            candidates.append(
                AlertCandidate(
                    trigger_class=TRIGGER_BASELINE,
                    severity="medium",
                    title=(
                        f"Baseline deviation: {desk} {metric} "
                        f"24h={current:.0f} vs μ={mean:.1f} σ={sigma:.1f} "
                        f"({baseline_days}d)"
                    ),
                    body=(
                        f"desk={desk} metric={metric}\n"
                        f"current_24h={current:.0f} baseline_mean={mean:.2f} "
                        f"baseline_sigma={sigma:.2f} n_sigma={n_sigma} "
                        f"threshold={mean + n_sigma * sigma:.2f} "
                        f"min_current={min_current:.0f}\n"
                        f"baseline_days={baseline_days} geo={geo} "
                        f"baseline_source={baseline_source}"
                    ),
                    target_id=desk,
                    derived_from=[],
                    data={
                        "trigger_class": TRIGGER_BASELINE,
                        "desk": desk,
                        "metric": metric,
                        "current_24h": current,
                        "baseline_mean": mean,
                        "baseline_sigma": sigma,
                        "n_sigma": n_sigma,
                        "min_current": min_current,
                        "baseline_days": baseline_days,
                        # E-1 — which baseline fed the exceedance test:
                        # 'desk_baselines' (fresh sidecar row) or 'inline'.
                        "baseline_source": baseline_source,
                    },
                    watermarks=[(TRIGGER_BASELINE, key, state)],
                )
            )
    return candidates, silent, seeded


# ---------------------------------------------------------------------------
# Alert row write + outward fan-out
# ---------------------------------------------------------------------------


def _resolve_dispatcher(deps: Any | None) -> Any | None:
    """The shared P1-1 AlertSinkDispatcher, or None (fan-out unavailable).

    Resolution order: explicit injection (``deps.extras['alert_sink_dispatcher']``
    — tests and future wiring) → the process-wide dispatcher the source-first
    bring-up publishes on ``AGENCY_HOLDER`` (the SAME instance the escalation
    edge and the liveness watchdog fan out through, so cooldown + idempotency
    state is shared). Never raises; None just means the rows stay
    internal-only this run (counted in the receipt).
    """
    extras = getattr(deps, "extras", None) if deps is not None else None
    if extras:
        d = extras.get("alert_sink_dispatcher")
        if d is not None:
            return d
    try:
        from ....runtime.source_first_runtime import AGENCY_HOLDER

        return AGENCY_HOLDER.get("alert_sink_dispatcher")
    except Exception:  # pragma: no cover — import-shape guard
        return None


#: Per-class verify posture for the outward payload. verified_finding carries
#: the REAL faithfulness score instead (see _sink_payload).
_UNVERIFIED_REASONS = {
    TRIGGER_BAND: (
        "deterministic band-crossing trigger over already-verified scorecard "
        "bands (no LLM prose of its own)"
    ),
    TRIGGER_CONTENTION: (
        "deterministic contention-flip trigger; the tied finding passed the "
        "faithfulness verify (see data.verified_finding_id)"
    ),
    TRIGGER_BASELINE: (
        "deterministic baseline-deviation trigger (aggregate counts; no LLM "
        "prose)"
    ),
    # watchlist_hit only lands here when the matched finding carries NO
    # faithfulness score — i.e. the structural-verified branch (a
    # verify-exempt deterministic analyst); a scored match reports its real
    # faithfulness instead (see _sink_payload).
    TRIGGER_WATCHLIST: (
        "watch hit on a structural verify-exempt deterministic finding (no "
        "LLM prose to verify; counted under the structural-verified bar)"
    ),
    # Folded 2026-07-29 from geo_convergence_scan._sink_payload — the exact
    # same reason text the standalone analyst used.
    TRIGGER_GEO_CONVERGENCE: (
        "deterministic geographic-convergence trigger (source-family "
        "diversity count over binned signals; no LLM prose)"
    ),
    # S-1: the gauge reads receipts, descriptors and row birth timestamps —
    # engine telemetry, not a claim about the world. There is nothing to
    # faithfulness-verify because there is no prose and no assertion about any
    # desk's substance.
    TRIGGER_PRODUCTION_DEFICIT: (
        "deterministic expected-vs-actual production gauge over descriptor "
        "cadence + trailing production baselines (engine telemetry; no LLM "
        "prose and no claim about the world)"
    ),
    TRIGGER_SITUATION_ESCALATION: _situation_escalation_scan.UNVERIFIED_REASON,
    "rollup": "deterministic per-desk rollup of trigger alerts (no LLM prose)",
}

#: Per-class outward ``channel_name`` override. Every class defaults to this
#: handler's own ``CHANNEL_NAME`` ("trigger_scan") EXCEPT geo_convergence,
#: which keeps the channel identity ("geo_convergence") it carried as a
#: standalone analyst (2026-07-29 fold) — ledger/log filtering by source is
#: unaffected by the fold. Purely a labeling preservation: the dispatcher's
#: cooldown is keyed by sink_kind, never channel_name (see
#: legba.data.alerts.sinks.AlertSinkDispatcher._deliver_one), so this has no
#: bearing on dedup/cooldown semantics.
_CHANNEL_BY_CLASS = {
    TRIGGER_GEO_CONVERGENCE: geo_convergence_scan.CHANNEL_NAME,
}


def _sink_payload(alert_row_id: UUID, cand: AlertCandidate) -> Any:
    """The converged AlertSinkPayload for one persisted alert row."""
    # sinks (not the package __init__): unverified_state /
    # verify_state_from_score are module-level exports only.
    from ...alerts.sinks import (
        AlertSinkPayload,
        receipt_link,
        unverified_state,
        verify_state_from_score,
    )

    path, url = receipt_link(str(alert_row_id), row_kind="alert")
    if cand.trigger_class == TRIGGER_FINDING or (
        cand.trigger_class == TRIGGER_WATCHLIST
        and cand.faithfulness_score is not None
    ):
        verify_state = verify_state_from_score(cand.faithfulness_score)
    else:
        verify_state = unverified_state(
            _UNVERIFIED_REASONS.get(
                cand.trigger_class, "deterministic trigger alert"
            )
        )
    return AlertSinkPayload(
        summary=cand.title[:512],
        detail=cand.body[:4000],
        severity=cand.severity,
        channel_name=_CHANNEL_BY_CLASS.get(cand.trigger_class, CHANNEL_NAME),
        target_id=cand.target_id,
        effective_confidence=cand.effective_confidence,
        verify_state=verify_state,
        event_at=cand.event_at,
        alert_row_id=str(alert_row_id),
        receipt_path=path,
        receipt_url=url,
    )


async def _write_alert_row(
    conn: Any,
    cand: AlertCandidate,
    *,
    analyst_id: str,
    analyst_version: str | None,
    run_uuid: UUID,
) -> UUID | None:
    """Persist one kind='alert' analyst_outputs row. None on rejection."""
    payload = AlertPayload(
        title=cand.title[:2048],
        body=cand.body[:65536],
        confidence=1.0,
        evidence=[],
        tags=[
            "deterministic",
            SUB_HANDLER_NAME,
            f"trigger:{cand.trigger_class}",
            f"severity:{cand.severity}",
        ]
        + ([f"target:{cand.target_id}"] if cand.target_id else []),
        data={"sub_handler": SUB_HANDLER_NAME, **cand.data},
        severity=cand.severity,  # type: ignore[arg-type]
        routing_hint=cand.trigger_class[:256],
    )
    ctx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version or "0" * 16,
        run_id=run_uuid,
        target_id=cand.target_id,
    )
    new_id = uuid4()
    row, dead = await write_analyst_output(
        conn,
        analyst_ctx=ctx,
        kind=OutputKind.ALERT,
        output_payload=payload,
        derived_from=cand.derived_from,
        row_id=new_id,
    )
    if dead is not None or row is None:
        logger.warning(
            "alert_trigger_scan.write_rejected trigger=%s desk=%s reason=%s",
            cand.trigger_class,
            cand.desk_key,
            getattr(dead, "reason", "schema_fail"),
        )
        return None
    return new_id


# ---------------------------------------------------------------------------
# Receipt
# ---------------------------------------------------------------------------


def _build_receipt(
    *,
    counts_by_class: dict[str, dict[str, int]],
    seeded_classes: list[str],
    fired: int,
    rollups: int,
    suppressed: int,
    write_failures: int,
    fanout_ok: int,
    fanout_failed: int,
    fanout_unavailable: bool,
    per_desk_cap: int,
) -> FindingPayload:
    title = (
        f"Alert trigger scan: {fired} alert(s) fired, {rollups} rollup(s), "
        f"{suppressed} summarized"
    )
    if seeded_classes:
        title = f"{title} (seeded: {', '.join(sorted(seeded_classes))})"
    body_lines = [
        f"fired={fired} rollups={rollups} suppressed_into_rollups={suppressed}",
        f"per_desk_cap={per_desk_cap} write_failures={write_failures}",
        (
            f"fanout_ok={fanout_ok} fanout_failed={fanout_failed} "
            f"fanout_unavailable={fanout_unavailable}"
        ),
    ]
    for cls in sorted(counts_by_class):
        c = counts_by_class[cls]
        body_lines.append(
            f"{cls}: candidates={c.get('candidates', 0)} "
            f"seeded={'yes' if cls in seeded_classes else 'no'}"
        )
    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "fired": fired,
            "rollups": rollups,
            "suppressed_into_rollups": suppressed,
            "write_failures": write_failures,
            "fanout_ok": fanout_ok,
            "fanout_failed": fanout_failed,
            "fanout_unavailable": fanout_unavailable,
            "per_desk_cap": per_desk_cap,
            "seeded_classes": sorted(seeded_classes),
            "counts_by_class": counts_by_class,
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
    """Sub-handler entry point — one global trigger scan (see module docstring).

    REFUSES LOUD on a missing pool (mirrors integrity_sweep): a trigger scan
    that cannot read the substrate must error visibly, never report a quiet
    zero-alert run. Scan-read failures inside a class also propagate — the
    receipt only ever reports classes that genuinely ran.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "alert_trigger_scan requires a live deps.pg_pool — refusing to "
            "report a zero-alert scan without reading the substrate"
        )

    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = options.get("analyst_version")
    raw_run_id = options.get("run_id")
    try:
        run_uuid = UUID(str(raw_run_id)) if raw_run_id else uuid4()
    except (ValueError, TypeError):
        run_uuid = uuid4()

    per_desk_cap = max(1, int(options.get("per_desk_cap", DEFAULT_PER_DESK_CAP)))
    per_watch_cap = max(
        1,
        int(
            options.get("per_watch_cap", _watchlist_scan.DEFAULT_PER_WATCH_CAP)
        ),
    )
    floor = float(
        options.get("effective_conf_floor", DEFAULT_EFFECTIVE_CONF_FLOOR)
    )
    window_hours = int(
        options.get("finding_window_hours", DEFAULT_FINDING_WINDOW_HOURS)
    )
    baseline_days = int(options.get("baseline_days", DEFAULT_BASELINE_DAYS))
    n_sigma = float(options.get("baseline_sigma", DEFAULT_BASELINE_SIGMA))
    geo_window_hours = int(
        options.get("geo_window_hours", DEFAULT_GEO_WINDOW_HOURS)
    )
    geo_min_distinct_families = max(
        2,
        int(
            options.get(
                "geo_min_distinct_families", DEFAULT_GEO_MIN_DISTINCT_FAMILIES
            )
        ),
    )
    # S-1: every gauge threshold is a GaugeConfig field under the `gauge_`
    # option prefix, so the operator retunes precision through descriptor
    # options with no deploy. Unknown or uncoercible keys keep their default
    # — a mistyped knob must not take the gauge offline.
    gauge_config = _production_deficit_scan.config_from_options(options)

    candidates: list[AlertCandidate] = []
    silent: list[tuple[str, str, dict[str, Any]]] = []
    seeded_classes: list[str] = []
    counts_by_class: dict[str, dict[str, int]] = {}

    async with pool.acquire() as conn:
        for cls, scan in (
            (TRIGGER_BAND, _scan_band_crossings(conn)),
            (
                TRIGGER_FINDING,
                _scan_verified_findings(
                    conn, floor=floor, window_hours=window_hours
                ),
            ),
            (TRIGGER_CONTENTION, _scan_contention_flips(conn, floor=floor)),
            (
                TRIGGER_BASELINE,
                _scan_baseline_deviation(
                    conn, baseline_days=baseline_days, n_sigma=n_sigma
                ),
            ),
        ):
            cls_candidates, cls_silent, was_seeded = await scan
            candidates.extend(cls_candidates)
            silent.extend(cls_silent)
            counts_by_class[cls] = {"candidates": len(cls_candidates)}
            if not was_seeded:
                seeded_classes.append(cls)

        # Trigger 5 — operator watchlist hits (P5-6). Separate call shape:
        # the scan also returns per-class stats (watches evaluated / window
        # findings / per-watch rollup suppression / table-unavailable) that
        # ride the receipt's counts_by_class entry for honesty.
        wl_candidates, wl_silent, wl_seeded, wl_stats = (
            await _watchlist_scan.scan_watchlist(
                conn,
                floor=floor,
                window_hours=window_hours,
                per_watch_cap=per_watch_cap,
            )
        )
        candidates.extend(wl_candidates)
        silent.extend(wl_silent)
        counts_by_class[TRIGGER_WATCHLIST] = {
            "candidates": len(wl_candidates),
            **wl_stats,
        }
        if not wl_seeded:
            seeded_classes.append(TRIGGER_WATCHLIST)

        # Trigger 6 — geo convergence (A7, folded 2026-07-29 from the former
        # standalone geo_convergence_scan analyst). Same call shape as the
        # watchlist scan: per-class stats (currently_formed_bins / cell vs
        # country breakdown / window signal counts) ride the receipt's
        # counts_by_class entry — the exact figures the standalone analyst's
        # own summary finding used to report.
        geo_candidates, geo_silent, geo_seeded, geo_stats = (
            await geo_convergence_scan.scan_geo_convergence(
                conn,
                window_hours=geo_window_hours,
                min_families=geo_min_distinct_families,
            )
        )
        candidates.extend(geo_candidates)
        silent.extend(geo_silent)
        counts_by_class[TRIGGER_GEO_CONVERGENCE] = {
            "candidates": len(geo_candidates),
            **geo_stats,
        }
        if not geo_seeded:
            seeded_classes.append(TRIGGER_GEO_CONVERGENCE)

        # Trigger 7 — production deficit (S-1). Same 4-tuple call shape; the
        # per-class stats (loops / gauged / deficits / paging / escalations /
        # recoveries / seeded_deficits) ride the receipt so a scan that pages
        # NOTHING still records what the gauge measured. That receipt is the
        # point: "the engine checked 268 loops and 261 were producing" is a
        # fact worth having in analyst_traces every ten minutes.
        pd_candidates, pd_silent, pd_seeded, pd_stats = (
            await _production_deficit_scan.scan_production_deficits(
                conn, config=gauge_config
            )
        )
        candidates.extend(pd_candidates)
        silent.extend(pd_silent)
        counts_by_class[TRIGGER_PRODUCTION_DEFICIT] = {
            "candidates": len(pd_candidates),
            **pd_stats,
        }
        if not pd_seeded:
            seeded_classes.append(TRIGGER_PRODUCTION_DEFICIT)

        # Trigger 8 — situation escalation (continuity P2). Same 4-tuple shape.
        se_candidates, se_silent, se_seeded, se_stats = (
            await _situation_escalation_scan.scan_situation_escalations(
                conn, floor=floor,
            )
        )
        candidates.extend(se_candidates)
        silent.extend(se_silent)
        counts_by_class[TRIGGER_SITUATION_ESCALATION] = {
            "candidates": len(se_candidates),
            **se_stats,
        }
        if not se_seeded:
            seeded_classes.append(TRIGGER_SITUATION_ESCALATION)

        # Silent bookkeeping (seeds / no-change refreshes / non-fired state
        # advances) — these represent OBSERVED state, never a fired alert.
        for wm_class, wm_key, wm_state in silent:
            await _upsert_watermark(conn, wm_class, wm_key, wm_state, fired=False)
        for cls in seeded_classes:
            await _mark_seeded(conn, cls)

    # Anti-noise: per-desk cap, worst-first; remainder into one honest rollup.
    kept, rollup_cands = apply_desk_cap(candidates, per_desk_cap)
    suppressed = sum(int(r.data.get("suppressed_count", 0)) for r in rollup_cands)

    dispatcher = _resolve_dispatcher(deps)
    fired = 0
    rollups_written = 0
    write_failures = 0
    fanout_ok = 0
    fanout_failed = 0
    to_fan_out: list[tuple[UUID, AlertCandidate]] = []

    async with pool.acquire() as conn:
        for cand in kept + rollup_cands:
            row_id = await _write_alert_row(
                conn,
                cand,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                run_uuid=run_uuid,
            )
            if row_id is None:
                # NOT advancing the watermark: the transition retries next scan.
                write_failures += 1
                continue
            if cand.trigger_class == "rollup":
                rollups_written += 1
            else:
                fired += 1
            for wm_class, wm_key, wm_state in cand.watermarks:
                await _upsert_watermark(
                    conn, wm_class, wm_key, wm_state, fired=True
                )
            to_fan_out.append((row_id, cand))

    # Outward fan-out — best-effort by P1-1 contract; the rows are already
    # durable and every sink outcome lands its own ledger row.
    if dispatcher is None and to_fan_out:
        logger.warning(
            "alert_trigger_scan.fanout_unavailable alerts=%d — no "
            "alert_sink_dispatcher wired (rows persisted; outward delivery "
            "skipped this run)",
            len(to_fan_out),
        )
    elif dispatcher is not None:
        for row_id, cand in to_fan_out:
            try:
                await dispatcher.fan_out(_sink_payload(row_id, cand))
                fanout_ok += 1
            except Exception as exc:  # noqa: BLE001 — belt over the never-raise contract
                fanout_failed += 1
                logger.warning(
                    "alert_trigger_scan.fanout_failed alert_row=%s err=%s",
                    row_id,
                    exc,
                )

    if fired or rollups_written or seeded_classes:
        logger.info(
            "alert_trigger_scan.done fired=%d rollups=%d suppressed=%d "
            "seeded=%s write_failures=%d fanout_ok=%d fanout_failed=%d",
            fired,
            rollups_written,
            suppressed,
            sorted(seeded_classes),
            write_failures,
            fanout_ok,
            fanout_failed,
        )

    finding = _build_receipt(
        counts_by_class=counts_by_class,
        seeded_classes=seeded_classes,
        fired=fired,
        rollups=rollups_written,
        suppressed=suppressed,
        write_failures=write_failures,
        fanout_ok=fanout_ok,
        fanout_failed=fanout_failed,
        fanout_unavailable=(dispatcher is None and bool(to_fan_out)),
        per_desk_cap=per_desk_cap,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = [
    "AlertCandidate",
    "apply_desk_cap",
    "baseline_exceeds",
    "classify_band_transition",
    "handle",
]

#: The full trigger-class vocabulary this handler emits, in _CLASS_PRIORITY
#: order. Exported so the drift guard in test_alert_trigger_scan can assert
#: every class is registered in all four per-class registries (priority,
#: unverified reason, receipt counts, watermark namespace) — the four places a
#: new class silently half-lands if any one is missed.
TRIGGER_CLASSES: tuple[str, ...] = (
    TRIGGER_BAND,
    TRIGGER_FINDING,
    TRIGGER_WATCHLIST,
    TRIGGER_CONTENTION,
    TRIGGER_BASELINE,
    TRIGGER_GEO_CONVERGENCE,
    TRIGGER_PRODUCTION_DEFICIT,
    TRIGGER_SITUATION_ESCALATION,
)

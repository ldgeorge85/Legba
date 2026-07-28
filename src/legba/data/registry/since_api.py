# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P1-6 — the "since last visit" diff + band-trajectory read routes.

Two read-only GET endpoints mounted under ``/api/v1/v3`` beside the v3
telemetry router (the SAME ``RegistryAPIDeps`` bundle + ``require_bearer``
gate, per the ``v3_api`` / ``substrate_reads_api`` wiring convention):

  * ``GET /since?cursor=<iso-ts>`` — ONE composed server-side diff of
    everything product-relevant that changed since the client's last look.
    The server is STATELESS: the client owns cursor storage and passes back
    the ``server_now`` it received last time as its next ``cursor``.
    ``server_now`` is captured BEFORE the section queries run, so a row that
    lands mid-query is reported again next visit (at-least-once) rather than
    silently skipped forever. The additive ``channel=`` param scopes ONLY the
    ``alerts`` section to one alert channel (default: unfiltered) — the
    alerts section is severity-ranked under ``SECTION_CAP``, so a low-volume
    channel (e.g. the map's medium/info ``geo_convergence`` rows) would
    otherwise be crowded out of a busy window by high-severity traffic.
  * ``GET /eval/band_trajectory?target_id=<desk>&days=30`` — per desk ×
    dimension, the time-ordered band sequence projected from the persisted
    ``kind='scorecard'`` rows (superseded rows INCLUDED — old heads ARE the
    history). The wall tile's trajectory data.

Registry-slim (the ``eval_country_scorecard`` precedent): this module NEVER
imports the runtime / deterministic handlers. The band-transition classifier
and the small constants it rests on are MIRRORED here from their producers
(``scorecard_banding`` band ladder + faithfulness floor,
``alert_trigger_scan.classify_band_transition``'s comparison shape,
``situation_clustering``'s lifecycle decay thresholds) — pure reads over the
persisted rows, none of the trigger scan's watermark machinery. The drift
guards in ``tests/data_pkg/test_v3_since_api.py`` assert each mirror stays
equal to its source of truth.

Honesty rules (house style):

  * Every section reports its FULL matching count (``total``) + an explicit
    ``truncated`` flag next to the capped ``items`` — a capped list is never
    presented as the whole story.
  * ``new_findings`` carries ONLY verified findings (a faithfulness-verify
    critique exists AND ``min(confidence, faithfulness) >= 0.50``), with the
    verify-EXEMPT structural analysts excluded — those never enter the verify
    pass, so they can never meet the bar (mirrors the P1-3 trigger's
    ``verified_finding`` gate).
  * A fresh cursor returns a valid all-empty envelope (HTTP 200), never a 404.
  * Situation lifecycle edges are DERIVED, not fabricated: the clustering
    handler's status is a pure decay function of ``last_event_at`` (active ≤2d,
    dormant ≤7d, closed beyond), so the pre-cursor status is recomputable
    exactly for a situation with no fresh events. When fresh events DID land
    since the cursor the prior ``last_event_at`` is unknowable server-side, so
    ``from_status`` is honestly ``null`` and the edge reads ``escalating``.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from ..provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
from .api import RegistryAPIDeps, require_bearer

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Mirrored constants (drift-guarded — see test_v3_since_api.py)
# ---------------------------------------------------------------------------

#: The system-wide effective-confidence floor (the 0.50 decision) — mirrors
#: ``scorecard_banding.FAITH_FLOOR`` (which ``alert_trigger_scan`` also reuses
#: as its verified-finding bar). NOT imported: the registry image stays slim.
EFFECTIVE_CONF_FLOOR: float = 0.50

#: The risk-band ladder + the insufficient-evidence sentinel — mirrors
#: ``scorecard_banding.BAND_LADDER`` / ``scorecard_banding.INSUFFICIENT``.
BAND_LADDER: tuple[str, ...] = ("low", "watch", "elevated", "high", "critical")
INSUFFICIENT_BAND: str = "insufficient-evidence"

#: Situation lifecycle decay thresholds (days) — mirrors
#: ``situation_clustering._STATUS_ACTIVE_MAX_DAYS`` / ``_STATUS_DORMANT_MAX_DAYS``
#: (active → dormant → closed by last-member age; pure function of
#: ``last_event_at``, which is what makes the pre-cursor status derivable).
SITUATION_ACTIVE_MAX_DAYS: float = 2.0
SITUATION_DORMANT_MAX_DAYS: float = 7.0

#: Hard bound on the cursor lookback — a >90d cursor is rejected with a clear
#: 400 (the diff is a "since last visit" surface, not an archive walk).
MAX_LOOKBACK_DAYS: int = 90

#: Per-section item cap on /since (each section still reports its full
#: ``total`` + ``truncated``). new_findings is spec'd at 50; the rest share it.
SECTION_CAP: int = 50

#: /eval/band_trajectory bounds: max window (mirrors the cursor rule) and the
#: scorecard-row scan cap (honest ``truncated`` flag when exceeded).
MAX_TRAJECTORY_DAYS: int = 90
TRAJECTORY_ROW_CAP: int = 5000

_SEVERITY_RANK_SQL = (
    "CASE {col} WHEN 'critical' THEN 4 WHEN 'high' THEN 3 "
    "WHEN 'medium' THEN 2 WHEN 'low' THEN 1 ELSE 0 END"
)

#: The shape a ``channel=`` filter value must take — channels are identifier
#: tokens (a routing_hint / trigger_class / analyst_id), never free text.
_CHANNEL_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable with no database)
# ---------------------------------------------------------------------------


def classify_band_transition(from_band: str, to_band: str) -> tuple[str, str]:
    """(direction, severity) for one desk×dimension band change.

    Mirrors ``alert_trigger_scan.classify_band_transition`` (the P1-3 severity
    mapping) WITHOUT importing the trigger scan: up the risk ladder =
    ``deterioration``/``high``; down = ``improvement``/``medium``; transitions
    in/out of ``insufficient-evidence`` are coverage events, not risk reads —
    ``evidence-gained`` / ``evidence-lost`` at ``medium``. An off-ladder value
    reads ``indeterminate``/``medium`` so a real change is never dropped.
    """
    if from_band == INSUFFICIENT_BAND and to_band in BAND_LADDER:
        return "evidence-gained", "medium"
    if to_band == INSUFFICIENT_BAND and from_band in BAND_LADDER:
        return "evidence-lost", "medium"
    if from_band in BAND_LADDER and to_band in BAND_LADDER:
        if BAND_LADDER.index(to_band) > BAND_LADDER.index(from_band):
            return "deterioration", "high"
        return "improvement", "medium"
    return "indeterminate", "medium"


def situation_decay_status(last_event_at: datetime | None, at: datetime) -> str:
    """The clustering handler's lifecycle at time ``at`` — pure decay function.

    Mirrors ``situation_clustering._situation_status``: active (fresh) →
    dormant (quiet) → closed (stale), by last-member age. ``None`` reads
    ``active`` (the handler's convention for a memberless frame).
    """
    if last_event_at is None:
        return "active"
    age_days = (at - last_event_at).total_seconds() / 86400.0
    if age_days <= SITUATION_ACTIVE_MAX_DAYS:
        return "active"
    if age_days <= SITUATION_DORMANT_MAX_DAYS:
        return "dormant"
    return "closed"


def classify_situation_change(
    created_at: datetime,
    last_event_at: datetime | None,
    *,
    cursor: datetime,
    now: datetime,
) -> tuple[str, str | None, str]:
    """(change, from_status, to_status) for one situation the SQL matched.

    * created since cursor → ``appeared`` (no prior state to name).
    * fresh events since cursor on a pre-existing situation → ``escalating``;
      the prior ``last_event_at`` is not stored, so ``from_status`` is
      honestly ``null`` rather than guessed.
    * no fresh events but a decay boundary crossed between cursor and now →
      the derived edge: ``resolved`` when it decayed into ``closed``,
      ``quieted`` when it decayed into ``dormant``.
    """
    if created_at > cursor:
        return "appeared", None, situation_decay_status(last_event_at, now)
    if last_event_at is not None and last_event_at > cursor:
        return "escalating", None, situation_decay_status(last_event_at, now)
    frm = situation_decay_status(last_event_at, cursor)
    to = situation_decay_status(last_event_at, now)
    change = "resolved" if to == "closed" else "quieted"
    return change, frm, to


def _jsonish(raw: Any) -> Any:
    """JSONB columns arrive as dict/list (asyncpg codec) or str — normalise."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (ValueError, TypeError):
            return None
    return raw


def _scorecard_dimensions(data: Any) -> dict[str, Any]:
    """The per-dimension verdicts out of one scorecard row's ``data`` column.

    The column stores the WHOLE ScorecardPayload dump, so the product bands
    live one level down — ``data['data']['bands']['dimensions']`` (the
    ``eval_country_scorecard`` / ``alert_trigger_scan`` nesting). A bare
    ``{'bands': ...}`` is accepted defensively.
    """
    payload = _jsonish(data)
    if not isinstance(payload, Mapping):
        return {}
    inner = payload.get("data") if isinstance(payload.get("data"), Mapping) else payload
    bands = inner.get("bands") if isinstance(inner, Mapping) else None
    dims = bands.get("dimensions") if isinstance(bands, Mapping) else None
    return dict(dims) if isinstance(dims, Mapping) else {}


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class SinceFinding(BaseModel):
    """One VERIFIED finding created since the cursor (new_findings section)."""
    id: str
    analyst_id: str | None
    target_id: str | None
    title: str
    severity: str | None
    confidence: float
    faithfulness_score: float
    effective_confidence: float
    produced_at: datetime


class SupersededBy(BaseModel):
    """The row that replaced a superseded finding (the other half of the
    reversal surface). ``None`` upstream when the pointer dangles."""
    id: str
    analyst_id: str | None
    title: str | None
    produced_at: datetime | None


class SupersededFinding(BaseModel):
    """A finding that WAS the head at cursor time but got superseded since."""
    id: str
    analyst_id: str | None
    target_id: str | None
    title: str
    severity: str | None
    superseded_at: datetime
    superseded_by: SupersededBy | None


class BandChange(BaseModel):
    """One desk×dimension band transition between the scorecard the client
    last saw (latest pre-cursor row) and the current head."""
    target_id: str
    dimension: str
    from_band: str
    to_band: str
    direction: str
    severity: str
    from_scorecard_row_id: str
    to_scorecard_row_id: str
    changed_at: datetime


class SituationChange(BaseModel):
    """One situation lifecycle edge since the cursor (see module docstring
    for how ``change`` / ``from_status`` / ``to_status`` are derived).
    ``status`` is the STORED column (which may lag the derived state by up to
    one clustering run — both are surfaced so nothing is hidden)."""
    id: str
    name: str
    target_id: str | None
    category: str
    change: str
    from_status: str | None
    to_status: str
    status: str
    last_event_at: datetime | None
    updated_at: datetime
    intensity_score: float


class SinceAlert(BaseModel):
    """One ``kind='alert'`` row landed since the cursor."""
    id: str
    severity: str | None
    channel: str
    summary: str
    target_id: str | None
    produced_at: datetime


class FindingsSection(BaseModel):
    items: list[SinceFinding] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class SupersededSection(BaseModel):
    items: list[SupersededFinding] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class BandChangesSection(BaseModel):
    items: list[BandChange] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class SituationsSection(BaseModel):
    items: list[SituationChange] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class AlertsSection(BaseModel):
    items: list[SinceAlert] = Field(default_factory=list)
    total: int = 0
    truncated: bool = False


class SinceResponse(BaseModel):
    """The composed "since last visit" diff envelope.

    ``server_now`` is the client's NEXT cursor (captured before the section
    queries so the hand-off is at-least-once, never lossy). ``counts`` carries
    each section's FULL matching total — identical to the section's own
    ``total``, surfaced flat so a tile can render badges without walking
    sections.
    """
    cursor: datetime
    server_now: datetime
    counts: dict[str, int]
    new_findings: FindingsSection
    superseded: SupersededSection
    band_changes: BandChangesSection
    situations: SituationsSection
    alerts: AlertsSection


class TrajectoryPoint(BaseModel):
    """One scorecard observation of a dimension: when, which band, at what
    folded confidence, and whether the cross-eval flagged faithfulness.

    ``effective_confidence`` is the PERSISTED fold from the banding engine
    (``None`` for an insufficient dimension — never fabricated).
    ``faithfulness_flagged`` mirrors the producer's display flag
    (``eval.faithfulness_flagged``); absent/unmeasured reads ``False`` by the
    producer's own contract (absence of proof is not proof of unfaithfulness).
    """
    ts: datetime
    band: str
    effective_confidence: float | None = None
    faithfulness_flagged: bool = False
    scorecard_row_id: str


class DeskTrajectory(BaseModel):
    target_id: str
    dimensions: dict[str, list[TrajectoryPoint]] = Field(default_factory=dict)


class BandTrajectoryResponse(BaseModel):
    """Per-desk band trajectories over the window. ``truncated`` is honest:
    when the scan cap was hit the LAST desk group may be incomplete."""
    days: int
    server_now: datetime
    desks: list[DeskTrajectory] = Field(default_factory=list)
    total_rows: int = 0
    truncated: bool = False


# ---------------------------------------------------------------------------
# SQL
# ---------------------------------------------------------------------------

# Verified new findings since cursor. INNER lateral: the verified bar REQUIRES
# a faithfulness verdict (a finding with no critique cannot qualify). The
# lateral pins to the faithfulness critique (title LIKE 'Faithfulness
# verify%') exactly like /findings + the P1-3 trigger — a later generic
# critique must not win the produced_at race. NULL analyst_id rows are kept
# (they are not structural-exempt; plain <> ALL would silently drop them).
_NEW_FINDINGS_SQL = f"""
    SELECT f.id::text           AS id,
           f.analyst_id         AS analyst_id,
           f.target_id          AS target_id,
           f.title              AS title,
           f.severity           AS severity,
           f.confidence         AS confidence,
           v.faithfulness_score AS faithfulness_score,
           LEAST(f.confidence, v.faithfulness_score) AS effective_confidence,
           f.produced_at        AS produced_at,
           count(*) OVER ()     AS total
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
       AND f.produced_at > $1
       AND (f.analyst_id IS NULL OR f.analyst_id <> ALL($2::text[]))
       AND LEAST(f.confidence, v.faithfulness_score) >= $3
     ORDER BY {_SEVERITY_RANK_SQL.format(col='f.severity')} DESC,
              f.produced_at DESC, f.id DESC
     LIMIT $4
"""

# The reversal surface: findings that WERE the head at cursor time
# (produced_at <= cursor, supersession stamped after cursor) and what replaced
# them. superseded_at is authoritative when stamped; a legacy row without it
# falls back to the superseding row's produced_at. A row where NEITHER is
# known (dangling pointer + no stamp) cannot be dated into the window and is
# honestly excluded rather than guessed.
_SUPERSEDED_SQL = """
    SELECT f.id::text        AS id,
           f.analyst_id      AS analyst_id,
           f.target_id       AS target_id,
           f.title           AS title,
           f.severity        AS severity,
           COALESCE(f.superseded_at, s.produced_at) AS superseded_at,
           s.id::text        AS by_id,
           s.analyst_id      AS by_analyst_id,
           s.title           AS by_title,
           s.produced_at     AS by_produced_at,
           count(*) OVER ()  AS total
      FROM analyst_outputs f
      LEFT JOIN analyst_outputs s ON s.id = f.superseded_by
     WHERE f.kind = 'finding'
       AND f.superseded_by IS NOT NULL
       AND f.produced_at <= $1
       AND COALESCE(f.superseded_at, s.produced_at) > $1
     ORDER BY superseded_at DESC, id DESC
     LIMIT $2
"""

# The scorecard the client LAST SAW per desk: the freshest row at cursor time
# (head-ness NOW is irrelevant — a since-superseded row was the head then).
_PRE_CURSOR_SCORECARDS_SQL = """
    SELECT DISTINCT ON (target_id)
           target_id, id::text AS id, produced_at, data
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND target_id IS NOT NULL
       AND produced_at <= $1
     ORDER BY target_id, produced_at DESC, id DESC
"""

# The current head per desk (the eval_country_scorecard head semantics).
_HEAD_SCORECARDS_SQL = """
    SELECT DISTINCT ON (target_id)
           target_id, id::text AS id, produced_at, data
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND target_id IS NOT NULL
       AND superseded_by IS NULL
     ORDER BY target_id, produced_at DESC, id DESC
"""

# Situations with a derivable lifecycle edge in (cursor, now]: appeared /
# fresh events / a decay boundary (active→dormant at +$2 secs, →closed at
# +$3 secs) crossed inside the window. Pure timestamp math — no watermarks.
_SITUATIONS_SQL = """
    SELECT id::text         AS id,
           name, target_id, category, status, intensity_score,
           created_at, last_event_at, updated_at,
           count(*) OVER ()  AS total
      FROM situations
     WHERE created_at > $1
        OR last_event_at > $1
        OR (last_event_at IS NOT NULL AND last_event_at <= $1
            AND (
                  ((last_event_at + make_interval(secs => $2)) > $1
                   AND (last_event_at + make_interval(secs => $2)) <= now())
               OR ((last_event_at + make_interval(secs => $3)) > $1
                   AND (last_event_at + make_interval(secs => $3)) <= now())
            ))
     ORDER BY updated_at DESC, id DESC
     LIMIT $4
"""

# Alert rows since cursor. ``channel`` resolves the AlertPayload's
# routing_hint (the P1-3 trigger class), falling back to the nested
# trigger_class / the emitting analyst — never fabricated beyond that. The
# resolution expression is factored so the projection and the optional
# ``channel=`` filter ($2, NULL = unfiltered) can never drift apart.
_ALERT_CHANNEL_SQL = (
    "COALESCE(NULLIF(data->>'routing_hint', ''), "
    "data->'data'->>'trigger_class', analyst_id, '')"
)
_ALERTS_SQL = f"""
    SELECT id::text AS id,
           severity,
           {_ALERT_CHANNEL_SQL} AS channel,
           title            AS summary,
           target_id        AS target_id,
           produced_at      AS produced_at,
           count(*) OVER () AS total
      FROM analyst_outputs
     WHERE kind = 'alert'
       AND produced_at > $1
       AND ($2::text IS NULL OR {_ALERT_CHANNEL_SQL} = $2)
     ORDER BY {_SEVERITY_RANK_SQL.format(col='severity')} DESC,
              produced_at DESC, id DESC
     LIMIT $3
"""

# Trajectory scan: EVERY scorecard row in the window, time-ascending per desk
# (superseded rows included — old heads are the history).
_TRAJECTORY_SQL = """
    SELECT target_id, id::text AS id, produced_at, data
      FROM analyst_outputs
     WHERE kind = 'scorecard'
       AND target_id IS NOT NULL
       AND produced_at > now() - make_interval(days => $1)
       AND ($2::text IS NULL OR target_id = $2)
     ORDER BY target_id, produced_at ASC, id ASC
     LIMIT $3
"""


# ---------------------------------------------------------------------------
# Pure reducers (unit-testable with no database)
# ---------------------------------------------------------------------------


def scorecard_band_changes(
    prev_rows: list[Mapping[str, Any]],
    head_rows: list[Mapping[str, Any]],
    *,
    cursor: datetime,
) -> list[BandChange]:
    """Desk×dimension transitions between the latest pre-cursor scorecard and
    the current head (the P1-3 band-crossing comparison shape, watermark-free).

    Skips honestly rather than fabricating:

      * a desk whose head predates the cursor (nothing new to compare);
      * a desk with no pre-cursor row (first-ever scorecard — no FROM state);
      * a dimension absent (or band-less) on either side.

    Sorted worst-first: severity rank desc, then desk, then dimension.
    """
    prev_by_target: dict[str, Mapping[str, Any]] = {
        str(r["target_id"]): r for r in prev_rows
    }
    changes: list[BandChange] = []
    for head in head_rows:
        head_at = head["produced_at"]
        if head_at is None or head_at <= cursor:
            continue
        target = str(head["target_id"])
        prev = prev_by_target.get(target)
        if prev is None or str(prev["id"]) == str(head["id"]):
            continue
        prev_dims = _scorecard_dimensions(prev["data"])
        head_dims = _scorecard_dimensions(head["data"])
        for dim, verdict in head_dims.items():
            if not isinstance(verdict, Mapping):
                continue
            to_band = str(verdict.get("band") or "")
            prev_verdict = prev_dims.get(dim)
            if not isinstance(prev_verdict, Mapping):
                continue
            from_band = str(prev_verdict.get("band") or "")
            if not from_band or not to_band or from_band == to_band:
                continue
            direction, severity = classify_band_transition(from_band, to_band)
            changes.append(
                BandChange(
                    target_id=target,
                    dimension=str(dim),
                    from_band=from_band,
                    to_band=to_band,
                    direction=direction,
                    severity=severity,
                    from_scorecard_row_id=str(prev["id"]),
                    to_scorecard_row_id=str(head["id"]),
                    changed_at=head_at,
                )
            )
    rank = {"critical": 4, "high": 3, "medium": 2, "low": 1}
    changes.sort(
        key=lambda c: (-rank.get(c.severity, 0), c.target_id, c.dimension)
    )
    return changes


def trajectory_desks(
    rows: list[Mapping[str, Any]],
) -> list[DeskTrajectory]:
    """Group time-ascending scorecard rows into per-desk dimension series."""
    desks: list[DeskTrajectory] = []
    current: DeskTrajectory | None = None
    for r in rows:
        target = str(r["target_id"])
        if current is None or current.target_id != target:
            current = DeskTrajectory(target_id=target)
            desks.append(current)
        dims = _scorecard_dimensions(r["data"])
        for dim, verdict in dims.items():
            if not isinstance(verdict, Mapping):
                continue
            band = str(verdict.get("band") or "")
            if not band:
                continue
            eff = verdict.get("effective_confidence")
            ev = verdict.get("eval")
            flagged = bool(ev.get("faithfulness_flagged")) if isinstance(ev, Mapping) else False
            current.dimensions.setdefault(str(dim), []).append(
                TrajectoryPoint(
                    ts=r["produced_at"],
                    band=band,
                    effective_confidence=(
                        float(eff)
                        if isinstance(eff, (int, float)) and not isinstance(eff, bool)
                        else None
                    ),
                    faithfulness_flagged=flagged,
                    scorecard_row_id=str(r["id"]),
                )
            )
    return desks


def _parse_cursor(raw: str, *, now: datetime) -> datetime:
    """Parse + validate the client cursor. Clear 400s, never a stack trace."""
    try:
        cur = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
    except (ValueError, AttributeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"invalid cursor (want an ISO-8601 timestamp): {exc}",
        )
    if cur.tzinfo is None:
        cur = cur.replace(tzinfo=timezone.utc)
    if now - cur > timedelta(days=MAX_LOOKBACK_DAYS):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"cursor lookback exceeds {MAX_LOOKBACK_DAYS} days; "
                f"/since is a since-last-visit diff, not an archive walk — "
                f"use the paged read routes for history"
            ),
        )
    return cur


def _parse_channel(raw: str | None) -> str | None:
    """Validate the optional alerts-section ``channel=`` filter. Clear 400s.

    ``None`` (absent) means unfiltered — the default behaviour is unchanged.
    An UNKNOWN-but-well-formed channel is NOT rejected here: it yields a valid
    all-empty alerts section (the fresh-cursor precedent — never a 404).
    """
    if raw is None:
        return None
    chan = raw.strip()
    if not _CHANNEL_RE.fullmatch(chan):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "invalid channel (want 1-64 chars of [A-Za-z0-9_.-], "
                "e.g. 'geo_convergence')"
            ),
        )
    return chan


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def build_since_router(deps: RegistryAPIDeps) -> APIRouter:
    """Construct the P1-6 router bound to the registry deps.

    Both endpoints are read-only GETs, bearer-gated via ``require_bearer``,
    reading from the primary pool via ``deps.descriptor_registry.pg.acquire()``
    — the same path the rest of the v3 surface uses.
    """
    router = APIRouter(tags=["since"])

    @router.get("/since", response_model=SinceResponse)
    async def since_diff(
        cursor: str = Query(...),
        channel: str | None = Query(default=None),
        principal: str = Depends(require_bearer),
    ) -> SinceResponse:
        """One composed diff of what changed since ``cursor`` (see module doc).

        ``channel`` (additive, default unfiltered) scopes ONLY the ``alerts``
        section — its ``items``/``total``/``truncated`` and ``counts.alerts``
        then describe that channel alone; every other section is untouched.
        """
        server_now = datetime.now(timezone.utc)
        cur = _parse_cursor(cursor, now=server_now)
        chan = _parse_channel(channel)
        cap = int(SECTION_CAP)

        async with deps.descriptor_registry.pg.acquire() as conn:
            finding_rows = await conn.fetch(
                _NEW_FINDINGS_SQL,
                cur,
                sorted(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS),
                float(EFFECTIVE_CONF_FLOOR),
                cap,
            )
            superseded_rows = await conn.fetch(_SUPERSEDED_SQL, cur, cap)
            prev_cards = await conn.fetch(_PRE_CURSOR_SCORECARDS_SQL, cur)
            head_cards = await conn.fetch(_HEAD_SCORECARDS_SQL)
            situation_rows = await conn.fetch(
                _SITUATIONS_SQL,
                cur,
                SITUATION_ACTIVE_MAX_DAYS * 86400.0,
                SITUATION_DORMANT_MAX_DAYS * 86400.0,
                cap,
            )
            alert_rows = await conn.fetch(_ALERTS_SQL, cur, chan, cap)

        def _total(rows: list[Any]) -> int:
            return int(rows[0]["total"]) if rows else 0

        new_findings = FindingsSection(
            items=[
                SinceFinding(
                    id=r["id"],
                    analyst_id=r["analyst_id"],
                    target_id=r["target_id"],
                    title=r["title"],
                    severity=r["severity"],
                    confidence=float(r["confidence"]),
                    faithfulness_score=float(r["faithfulness_score"]),
                    effective_confidence=float(r["effective_confidence"]),
                    produced_at=r["produced_at"],
                )
                for r in finding_rows
            ],
            total=_total(finding_rows),
            truncated=_total(finding_rows) > len(finding_rows),
        )

        superseded = SupersededSection(
            items=[
                SupersededFinding(
                    id=r["id"],
                    analyst_id=r["analyst_id"],
                    target_id=r["target_id"],
                    title=r["title"],
                    severity=r["severity"],
                    superseded_at=r["superseded_at"],
                    superseded_by=(
                        SupersededBy(
                            id=r["by_id"],
                            analyst_id=r["by_analyst_id"],
                            title=r["by_title"],
                            produced_at=r["by_produced_at"],
                        )
                        if r["by_id"] is not None
                        else None
                    ),
                )
                for r in superseded_rows
            ],
            total=_total(superseded_rows),
            truncated=_total(superseded_rows) > len(superseded_rows),
        )

        all_band_changes = scorecard_band_changes(
            [dict(r) for r in prev_cards],
            [dict(r) for r in head_cards],
            cursor=cur,
        )
        band_changes = BandChangesSection(
            items=all_band_changes[:cap],
            total=len(all_band_changes),
            truncated=len(all_band_changes) > cap,
        )

        situation_items: list[SituationChange] = []
        for r in situation_rows:
            change, frm, to = classify_situation_change(
                r["created_at"],
                r["last_event_at"],
                cursor=cur,
                now=server_now,
            )
            situation_items.append(
                SituationChange(
                    id=r["id"],
                    name=r["name"],
                    target_id=r["target_id"],
                    category=r["category"],
                    change=change,
                    from_status=frm,
                    to_status=to,
                    status=r["status"],
                    last_event_at=r["last_event_at"],
                    updated_at=r["updated_at"],
                    intensity_score=float(r["intensity_score"]),
                )
            )
        situations = SituationsSection(
            items=situation_items,
            total=_total(situation_rows),
            truncated=_total(situation_rows) > len(situation_rows),
        )

        alerts = AlertsSection(
            items=[
                SinceAlert(
                    id=r["id"],
                    severity=r["severity"],
                    channel=str(r["channel"] or ""),
                    summary=r["summary"],
                    target_id=r["target_id"],
                    produced_at=r["produced_at"],
                )
                for r in alert_rows
            ],
            total=_total(alert_rows),
            truncated=_total(alert_rows) > len(alert_rows),
        )

        return SinceResponse(
            cursor=cur,
            server_now=server_now,
            counts={
                "new_findings": new_findings.total,
                "superseded": superseded.total,
                "band_changes": band_changes.total,
                "situations": situations.total,
                "alerts": alerts.total,
            },
            new_findings=new_findings,
            superseded=superseded,
            band_changes=band_changes,
            situations=situations,
            alerts=alerts,
        )

    @router.get("/eval/band_trajectory", response_model=BandTrajectoryResponse)
    async def band_trajectory(
        target_id: str | None = Query(default=None),
        days: int = Query(default=30),
        principal: str = Depends(require_bearer),
    ) -> BandTrajectoryResponse:
        """Per desk×dimension, the time-ordered band sequence over ``days``."""
        if days < 1 or days > MAX_TRAJECTORY_DAYS:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"days must be in [1, {MAX_TRAJECTORY_DAYS}]",
            )
        server_now = datetime.now(timezone.utc)
        row_cap = int(TRAJECTORY_ROW_CAP)

        async with deps.descriptor_registry.pg.acquire() as conn:
            rows = await conn.fetch(
                _TRAJECTORY_SQL, int(days), target_id, row_cap + 1,
            )

        truncated = len(rows) > row_cap
        page = [dict(r) for r in rows[:row_cap]]
        return BandTrajectoryResponse(
            days=int(days),
            server_now=server_now,
            desks=trajectory_desks(page),
            total_rows=len(page),
            truncated=truncated,
        )

    return router

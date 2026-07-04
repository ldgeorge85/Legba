# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Situation clustering — materialize ``situations`` rows from clustered findings.

The :mod:`finding_supersession` analyst stamps a ``situation_signature`` on
every finding that belongs to a multi-member cluster (the latest live finding
plus the ones it supersedes). This handler reads those stamped findings, groups
them by signature, and UPSERTS one row in the ``situations`` table per
signature — the durable, queryable "situation" object that the ``/situations``
read API, the recursive lineage walk, and the STIX incident producer all
already consume but that nothing previously WROTE (the situations table sat at 0
rows: a built-but-unproduced write-path, the last dark leg of the analysis
plane).

Pipeline position
-----------------
Runs AFTER finding_supersession on the deterministic cadence: supersession
derives + stamps the signatures, this materializes them into situations. Like
supersession (which writes its ``finding_supersessions`` link rows directly and
returns a single summary FindingPayload), this handler does its OWN situation
writes — the deterministic dispatcher persists exactly one ``analyst_output``
per run, so a per-cluster fan-out of situation rows is written here directly and
the returned FindingPayload is the run summary.

Idempotency
-----------
A situation is keyed by ``(situation_signature, analyst_id)``. A re-run UPDATES
the existing row (event_count / last_event_at / intensity / derived_from)
instead of inserting a duplicate. NEVER deletes a situation.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from math import exp, log
from typing import Any, Mapping
from uuid import UUID, uuid4

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from ....runtime.grounding import is_non_event_situation_name
from .finding_supersession import _COMPOSITION_ANALYST_IDS

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "situation_clustering"
_DEFAULT_LOOKBACK_DAYS = 30
_MAX_MEMBERS = 500
_SITUATION_SCHEMA_URI = "iglu:legba/situation/jsonschema/2-0-0"
_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a DB)
# ---------------------------------------------------------------------------


def _topic_from_signature(sig: str) -> str:
    """Recover the topic/category from a finding_supersession signature.

    ``sig:<topic>|<entities>`` → ``<topic>``; ``sit:<explicit-id>`` → ``""``.
    """
    if sig.startswith("sig:"):
        return sig[4:].split("|", 1)[0].strip()
    return ""


def _target_for_category(category: str, fallback: str | None) -> str | None:
    """Derive the owning target_id for a situation from its topic category.

    A per-country situation's category IS the country target slug
    (``country_g20_us``) — so populate ``situations.target_id`` with it (review
    follow-up: scope situation grounding on a real target_id, not the
    ``category==slug`` coincidence; a future THEMATIC situation then has a
    distinct target_id and never leaks into a country assessor's grounding).
    A non-country topic has no country home → fall back to the run's target_id
    (usually None for this meta analyst)."""
    if isinstance(category, str) and category.startswith("country"):
        return category
    return fallback


def _latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic latest member: newest produced_at, then largest id."""
    def _key(r: dict[str, Any]) -> tuple[str, str]:
        # Coerce produced_at to a string so a str/NULL value never collides with
        # datetime rows under `<` (the heterogeneous-key TypeError). "" sorts
        # oldest, so a missing timestamp is never chosen as the max.
        v = r.get("produced_at")
        if v is None:
            pa = ""
        elif isinstance(v, str):
            pa = v
        else:
            iso = getattr(v, "isoformat", None)
            pa = iso() if callable(iso) else str(v)
        return (pa, str(r.get("id")))

    return max(rows, key=_key)


# DQ P6 — reject a situation NAME that is a REPORT-SNAPSHOT receipt rather than a
# frame label: a dated world snapshot ("World situational assessment — 2026-06-30")
# or a leaked JSON-envelope fragment ('"title": "World situational assessment …",'
# — the #125 parse-fallback class). A frame is a durable object; a dated snapshot
# is a point-in-time receipt whose name churns every run and pollutes /situations.
_SNAPSHOT_NAME_RE = re.compile(
    r"[-–—]\s*\d{4}-\d{2}-\d{2}\b"        # "... — YYYY-MM-DD"
    r"|^\s*[\"']?title[\"']?\s*:",        # leaked JSON '"title":' fragment
    re.IGNORECASE,
)


def _is_report_snapshot_name(title: Any) -> bool:
    """True for a dated-snapshot / leaked-JSON title that must not name a frame."""
    return isinstance(title, str) and _SNAPSHOT_NAME_RE.search(title) is not None


def _situation_name(rows: list[dict[str, Any]], sig: str) -> str:
    """Human label — the latest member finding's title (the freshest framing).

    DQ P6: a dated-snapshot or leaked-JSON title (the #125 parse-fallback class)
    is REJECTED — fall back to the signature's topic label so a report receipt
    never mints a situation named after a raw JSON fragment or a churning date.
    """
    title = str(_latest(rows).get("title") or "").strip()
    if title and not _is_report_snapshot_name(title):
        return title[:512]
    topic = _topic_from_signature(sig)
    return (f"Situation: {topic}" if topic else f"Situation {sig}")[:512]


# Situation lifecycle (decay) — the "events come and go" mechanic. Intensity is
# RECENCY-WEIGHTED (each member contributes exp half-life since its produced_at)
# so a situation that stops getting fresh findings fades instead of holding a
# flat corroboration count forever; status transitions active → dormant →
# closed by the age of its most-recent member, and REOPENS (→ active)
# automatically when a new member lands (the upsert recomputes every run).
# Tunables, in days.
_INTENSITY_HALF_LIFE_DAYS = 3.0
_STATUS_ACTIVE_MAX_DAYS = 2.0
_STATUS_DORMANT_MAX_DAYS = 7.0
_LN2 = log(2.0)


def _aware(dt: Any) -> datetime | None:
    """Coerce a produced_at value to a tz-aware datetime, or None."""
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _decayed_intensity(produced: list[datetime], now: datetime) -> float:
    """Sum of exp(half-life) weights over member produced_at timestamps."""
    total = 0.0
    for p in produced:
        age_days = max(0.0, (now - p).total_seconds() / 86400.0)
        total += exp(-_LN2 * age_days / _INTENSITY_HALF_LIFE_DAYS)
    return round(total, 4)


def _situation_status(last_event_at: datetime | None, now: datetime) -> str:
    """active (fresh) → dormant (quiet) → closed (stale), by last-member age."""
    if last_event_at is None:
        return "active"
    age_days = (now - last_event_at).total_seconds() / 86400.0
    if age_days <= _STATUS_ACTIVE_MAX_DAYS:
        return "active"
    if age_days <= _STATUS_DORMANT_MAX_DAYS:
        return "dormant"
    return "closed"


def _situation_fields(
    sig: str, rows: list[dict[str, Any]], *, now: datetime | None = None,
) -> dict[str, Any]:
    """Derive the situations-table column values for one cluster."""
    now = now or datetime.now(timezone.utc)
    member_ids = [str(r["id"]) for r in rows if r.get("id")]
    produced = [p for p in (_aware(r.get("produced_at")) for r in rows) if p is not None]
    last_event_at = max(produced, default=None)
    status = _situation_status(last_event_at, now)
    name = _situation_name(rows, sig)
    return {
        "situation_signature": sig,
        "name": name,
        "category": _topic_from_signature(sig),
        # DQ P6 — mark a steady-state / non-event frame authoritatively at
        # MATERIALIZATION (not only name-filtered on read) using the SAME shared
        # predicate the grounding read uses, so the two never drift. Stored in the
        # situation ``data`` payload; a steady-state frame is a "nothing to
        # report" / status-quo read and must not head the intensity ranking.
        "steady_state": is_non_event_situation_name(name),
        "event_count": len(rows),
        # Recency-weighted intensity (exp half-life) — fades as the situation
        # goes quiet, rises as fresh findings land. Falls back to the raw count
        # only when no produced_at is resolvable.
        "intensity_score": (
            _decayed_intensity(produced, now) if produced else float(len(rows))
        ),
        # Lifecycle status — drives the timeline span fade + "comes and goes".
        "status": status,
        "last_event_at": last_event_at,
        # Temporal frame (Phase 5a, migration 0040): a situation is valid FROM
        # its earliest member finding and stays open (valid_until NULL) while
        # active/dormant; when it CLOSES we stamp valid_until = last_event_at so
        # it expresses "active over [t0, t1)" like facts/nexuses. This is what
        # makes a situation a persistent FRAME rather than a mutable snapshot.
        "valid_from": min(produced, default=None),
        "valid_until": last_event_at if status == "closed" else None,
        "member_finding_ids": member_ids[:_MAX_MEMBERS],
    }


def _group_by_signature(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        # DQ P6 — a COMPOSITION / META producer's report never materializes as a
        # situation (the live SQL fetch already excludes them; this also covers
        # the synthetic deps=None path). ``analyst_id`` is absent on some legacy
        # synthetic rows → treated as non-composition (unchanged behavior).
        if str(r.get("analyst_id") or "") in _COMPOSITION_ANALYST_IDS:
            continue
        sig = r.get("situation_signature")
        if not sig:
            continue
        groups.setdefault(str(sig), []).append(r)
    return groups


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _upsert_situation(
    conn: Any,
    *,
    fields: dict[str, Any],
    analyst_id: str,
    analyst_version: str | None,
    target_id: str | None,
    run_id: Any,
) -> str:
    """Insert or update ONE situation keyed by (signature, analyst_id).

    Returns ``"created"`` or ``"updated"``. NEVER deletes.

    Atomic UPSERT on the ``(situation_signature, analyst_id)`` unique index
    (migration 0040) — replaces the prior racy SELECT-then-INSERT/UPDATE and
    promotes ``situation_signature`` to a first-class column (it is also kept
    in ``data`` for the member-id payload). ``valid_from``/``valid_until``
    carry the temporal frame: ``valid_from`` only ever moves EARLIER
    (``LEAST(stored, min(current members))``) so a frame's start can be pulled
    back to the true earliest member but never drifts FORWARD when old members
    age out of the lookback (this also self-heals the 0040→0041 backfill); and
    ``valid_until`` tracks the CURRENT lifecycle each run — stamped to
    ``last_event_at`` when the frame is closed, and re-set to NULL (re-opened)
    when a fresh member flips it back to active/dormant (the ``ON CONFLICT DO
    UPDATE`` writes ``EXCLUDED.valid_until``, which ``_situation_fields``
    derives as ``None`` for any non-closed status). So an open frame is always
    ``valid_until IS NULL`` and a closed one always carries its close time —
    consistent with the facts/nexuses temporal gate the grounding read uses.
    """
    sig = fields["situation_signature"]
    derived_from = [UUID(m) for m in fields["member_finding_ids"]]
    data = {
        "situation_signature": sig,
        "member_finding_ids": fields["member_finding_ids"],
        "sub_handler": SUB_HANDLER_NAME,
        # DQ P6 — authoritative steady-state marker (see _situation_fields).
        "steady_state": bool(fields.get("steady_state")),
    }
    run_uuid = None
    if run_id:
        try:
            run_uuid = UUID(str(run_id))
        except (ValueError, TypeError):
            run_uuid = None

    row = await conn.fetchrow(
        """
        INSERT INTO situations
            (id, data, name, status, category, last_event_at, event_count,
             intensity_score, target_id, analyst_id, analyst_version,
             produced_at, derived_from, schema_uri, run_id,
             situation_signature, valid_from, valid_until)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,NOW(),$12,$13,$14,$15,$16,$17)
        ON CONFLICT (situation_signature, analyst_id)
            WHERE situation_signature IS NOT NULL
        DO UPDATE SET
            name=EXCLUDED.name, category=EXCLUDED.category,
            event_count=EXCLUDED.event_count,
            intensity_score=EXCLUDED.intensity_score,
            last_event_at=EXCLUDED.last_event_at,
            derived_from=EXCLUDED.derived_from, data=EXCLUDED.data,
            status=EXCLUDED.status, valid_until=EXCLUDED.valid_until,
            valid_from=LEAST(situations.valid_from, EXCLUDED.valid_from),
            target_id=COALESCE(situations.target_id, EXCLUDED.target_id),
            updated_at=NOW()
        RETURNING (xmax = 0) AS inserted
        """,
        uuid4(), json.dumps(data), fields["name"], fields["status"],
        fields["category"], fields["last_event_at"], fields["event_count"],
        fields["intensity_score"],
        _target_for_category(fields["category"], target_id),
        analyst_id, analyst_version,
        derived_from, _SITUATION_SCHEMA_URI, run_uuid,
        sig, fields["valid_from"], fields["valid_until"],
    )
    # xmax = 0 on the freshly-INSERTed tuple, non-zero when ON CONFLICT took the
    # UPDATE branch — the idiomatic upsert created/updated discriminator.
    return "created" if row and row["inserted"] else "updated"


async def _resolve_pool(
    pool: Any,
    *,
    analyst_id: str,
    analyst_version: str | None,
    target_id: str | None,
    run_id: Any,
    lookback_days: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Materialize situations from signature-stamped findings.

    Returns ``(created, updated, clusters)``.
    """
    created = 0
    updated = 0
    clusters: list[dict[str, Any]] = []
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            f"""
            SELECT id, title, produced_at, situation_signature, analyst_id
            FROM analyst_outputs
            WHERE kind = 'finding' AND situation_signature IS NOT NULL
              AND produced_at > NOW() - INTERVAL '{int(lookback_days)} days'
              AND analyst_id <> ALL($1::text[])
            ORDER BY produced_at ASC, id ASC
            """,
            list(_COMPOSITION_ANALYST_IDS),
        )
        groups = _group_by_signature([dict(r) for r in rows])
        for sig, members in groups.items():
            fields = _situation_fields(sig, members)
            action = await _upsert_situation(
                conn, fields=fields, analyst_id=analyst_id,
                analyst_version=analyst_version, target_id=target_id, run_id=run_id,
            )
            created += action == "created"
            updated += action == "updated"
            clusters.append({
                "situation_signature": sig,
                "event_count": fields["event_count"],
                "action": action,
            })
    return created, updated, clusters


def _resolve_synthetic(inputs: list[dict[str, Any]]) -> tuple[int, int, list[dict[str, Any]]]:
    """deps=None path (unit tests): group pre-shaped rows, no DB writes."""
    groups = _group_by_signature([dict(r) for r in inputs])
    clusters = [
        {
            "situation_signature": sig,
            "event_count": len(members),
            "action": "synthetic",
            "name": _situation_fields(sig, members)["name"],
            "category": _topic_from_signature(sig),
        }
        for sig, members in groups.items()
    ]
    return 0, 0, clusters


def _build_finding(
    *, created: int, updated: int, clusters: list[dict[str, Any]] | None, target_id: str | None,
) -> FindingPayload:
    n = len(clusters or [])
    title = f"Situation clustering: {n} situations ({created} new, {updated} updated)"
    if target_id:
        title = f"{title} for {target_id}"
    return FindingPayload(
        title=title[:2048],
        body="\n".join([f"situations={n}", f"created={created}", f"updated={updated}"])[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "situations_created": created,
            "situations_updated": updated,
            "clusters": clusters if clusters is not None and len(clusters) <= 100 else None,
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
    """Sub-handler entry point — see module docstring.

    Options
    -------
    lookback_days:
        Only findings this recent are eligible (default 30) — older settled
        history isn't a live situation.
    """
    analyst_id = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_version = options.get("analyst_version")
    target_id = options.get("target_id")
    run_id = options.get("run_id")
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))

    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            created, updated, clusters = await _resolve_pool(
                pool,
                analyst_id=analyst_id,
                analyst_version=analyst_version,
                target_id=target_id,
                run_id=run_id,
                lookback_days=lookback_days,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("situation_clustering.pool_failed err=%s", exc)
            created, updated, clusters = 0, 0, []
        clusters_for_finding = clusters if len(clusters) <= 100 else None
    else:
        created, updated, clusters = _resolve_synthetic(inputs)
        clusters_for_finding = clusters

    finding = _build_finding(
        created=created, updated=updated, clusters=clusters_for_finding, target_id=target_id,
    )
    # Emit a FEED finding only when a NEW situation actually formed. A run that
    # only re-touched existing situations (created == 0) is an idempotent
    # refresh — the situation rows carry the updates; repeating the identical
    # "N situations (0 new, N updated)" summary into the feed every cadence tick
    # is noise. The run is still fully traced (force_trace_only skips only the
    # analyst_outputs row, not the trace or the situation side-writes).
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        force_trace_only=(created == 0),
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]

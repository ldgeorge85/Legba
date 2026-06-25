# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``proposed_edge_governance`` sub-handler — FIX P3-1 (proposed_edges lifecycle).

The ``entity_resolution`` producer lands pairwise ``co_occurs`` rows in
``proposed_edges`` (confidence starts at 0.4 and accrues +0.05 per re-co-occurrence,
so ``confidence`` is a co-occurrence-count proxy). The ``relationship_reifier``
META kind reads them as candidates but never changes their lifecycle — so the
table grew to thousands of rows ALL ``status='pending'``, ``relationship_type=
'co_occurs'``, 0 ever reviewed or promoted (the deep-review audit's P3-1 gap).

This deterministic governance pass closes the lifecycle WITHOUT an LLM:

  * **Promote** — a ``pending`` co-occurrence whose confidence clears
    ``promote_min_confidence`` is reified into a first-class ``nexuses`` row via
    the live ``write_nexus`` path (so Phase-B predicate normalization +
    open-triple supersession apply), then its status flips to ``'promoted'``.
    Co-occurrence is an UNTYPED, NEUTRAL relationship, so the promoted nexus is
    the canonical neutral predicate ``CoOccursWith`` with ``polarity=0`` — this
    is the honest type for a bare co-mention (the signed/typed reification stays
    the ``relationship_reifier``'s LLM job; this pass only graduates the
    well-corroborated co-occurrence skeleton out of the pending queue).
  * **Reject (age-out)** — a ``pending`` edge whose confidence is below
    ``reject_max_confidence`` and that is older than ``reject_min_age_days`` is
    marked ``status='rejected'`` (it never accrued enough corroboration). This
    caps the unbounded ``pending`` backlog. Disabled by a non-positive age.

Everything is thresholded + bounded. Defaults are CONSERVATIVE: promote only
edges seen several times (≥ 0.6 ⇒ co-occurred ~4+ times), reject only thin
(< 0.45) AND stale (≥ 30d) edges, so a still-accruing pair is never prematurely
rejected.

Output ``data`` keys:
    promoted_count   int — proposed_edges promoted to nexuses
    rejected_count   int — proposed_edges aged-out to rejected
    candidates       int — pending promotion candidates scanned
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload, NexusPayload
from ...provenance.writes import write_nexus
from ....runtime.analyst_method import AnalystMethodResult
from ._entity_canon import is_demonym, is_junk_entity

logger = logging.getLogger(__name__)

# --- conservative defaults --------------------------------------------------

DEFAULT_PROMOTE_MIN_CONFIDENCE: float = 0.6
"""Promote a co-occurrence only once it has accrued to >= this confidence. With
the entity_resolution accrual (0.4 + 0.05/re-co-occurrence) this is ~4+ sightings
— "corroborated", not a single co-mention."""

DEFAULT_REJECT_MAX_CONFIDENCE: float = 0.45
"""Reject (age-out) only edges still below this confidence — a single or near-
single co-mention that never corroborated."""

DEFAULT_REJECT_MIN_AGE_DAYS: int = 30
"""Only reject an under-corroborated edge once it is at least this old, so a
freshly-seen pair that is still accruing is never prematurely rejected. A
non-positive value DISABLES rejection (promotion still runs)."""

DEFAULT_MAX_PROMOTIONS_PER_RUN: int = 200
"""Bounds the per-run promotion work (each promotion is a nexus write)."""

DEFAULT_MAX_REJECTIONS_PER_RUN: int = 2000
"""Bounds the per-run rejection work (a single bulk UPDATE, but capped)."""

# Canonical neutral predicate for a bare co-occurrence (the vocabulary's
# CoOccursWith; write_nexus normalizes it to the lowercase-spaced row form).
_CO_OCCURRENCE_REL_TYPE: str = "CoOccursWith"


async def _promote_candidates(
    conn: Any,
    *,
    analyst_id: str,
    analyst_version: str,
    run_id: UUID | None,
    target_id: str | None,
    target_version: str | None,
    min_confidence: float,
    limit: int,
) -> int:
    """Promote well-corroborated pending ``co_occurs`` edges into neutral
    ``CoOccursWith`` nexuses and flip their status to ``'promoted'``.

    Skips a candidate that is already reified into an OPEN nexus (the
    relationship_reifier may have typed it first — its signed nexus wins; we
    just mark the pending row promoted so it leaves the queue). Each promotion
    is best-effort: a per-row write failure leaves that row pending for the next
    sweep, never failing the run.
    """
    from ...provenance import AnalystContext  # local import — avoid import cycle

    rows = await conn.fetch(
        """
        SELECT id, source_entity, target_entity, evidence_text, confidence,
               derived_from, produced_at
          FROM proposed_edges
         WHERE status = 'pending'
           AND relationship_type = 'co_occurs'
           AND confidence >= $1
         ORDER BY confidence DESC, produced_at DESC
         LIMIT $2
        """,
        float(min_confidence),
        int(limit),
    )
    promoted = 0
    actx = AnalystContext(
        analyst_id=analyst_id,
        analyst_version=analyst_version,
        run_id=run_id if isinstance(run_id, UUID) else None,
        target_id=target_id,
        target_version=target_version,
    )
    for r in rows:
        subj = str(r["source_entity"] or "").strip()
        obj = str(r["target_entity"] or "").strip()
        if not subj or not obj or subj.lower() == obj.lower():
            # Degenerate edge — mark rejected so it leaves the queue.
            await conn.execute(
                "UPDATE proposed_edges SET status = 'rejected', reviewed_at = now() "
                "WHERE id = $1",
                r["id"],
            )
            continue
        # DQ-H4 entity-quality gate: never promote an edge whose endpoint is a
        # national demonym ("Iranian") or a junk token ("TV") into a first-class
        # nexus — those are the same referent as their country (inflating graph
        # centrality, e.g. Iran↔Iranian) or not entities at all. Reject so they
        # leave the queue instead of graduating to the graph.
        if (
            is_demonym(subj) or is_demonym(obj)
            or is_junk_entity(subj) or is_junk_entity(obj)
        ):
            await conn.execute(
                "UPDATE proposed_edges SET status = 'rejected', reviewed_at = now() "
                "WHERE id = $1",
                r["id"],
            )
            continue
        # Already reified into an open nexus? Mark promoted (it graduated) and
        # skip the duplicate write.
        already = await conn.fetchval(
            """
            SELECT 1 FROM nexuses n
             WHERE n.valid_until IS NULL AND n.superseded_by IS NULL
               AND lower(n.subject) = lower($1) AND lower(n.object) = lower($2)
             LIMIT 1
            """,
            subj, obj,
        )
        if already:
            await conn.execute(
                "UPDATE proposed_edges SET status = 'promoted', reviewed_at = now() "
                "WHERE id = $1",
                r["id"],
            )
            promoted += 1
            continue

        derived = [u for u in (r["derived_from"] or []) if isinstance(u, UUID)]
        ev = r["produced_at"]
        payload = NexusPayload(
            subject=subj[:2048],
            object=obj[:2048],
            rel_type=_CO_OCCURRENCE_REL_TYPE,
            label=f"{subj} CoOccursWith {obj}"[:4096],
            polarity=0,  # bare co-occurrence is neutral by construction
            intent="neutral",
            channel="direct",
            confidence=float(r["confidence"] or 0.5),
            valid_from=ev if isinstance(ev, datetime) else None,
            source_signal_ids=list(derived),
            data={
                "promoted_from_proposed_edge": str(r["id"]),
                "evidence_text": str(r["evidence_text"] or "")[:1200],
            },
        )
        try:
            out, dlq = await write_nexus(
                conn, analyst_ctx=actx, payload=payload, derived_from=derived,
            )
        except Exception as exc:  # pragma: no cover - per-row best-effort
            logger.warning(
                "proposed_edge_governance.promote_write_failed id=%s err=%s",
                r["id"], exc,
            )
            continue
        if out is not None:
            await conn.execute(
                "UPDATE proposed_edges SET status = 'promoted', reviewed_at = now() "
                "WHERE id = $1",
                r["id"],
            )
            promoted += 1
        elif dlq is not None:
            logger.warning(
                "proposed_edge_governance.promote_dlq id=%s", r["id"],
            )
    return promoted


async def _reject_stale_thin(
    conn: Any,
    *,
    max_confidence: float,
    min_age_days: int,
    limit: int,
) -> int:
    """Age-out thin, stale pending edges → ``status='rejected'``. A single
    bounded UPDATE. Disabled when ``min_age_days <= 0``."""
    if min_age_days <= 0:
        return 0
    result = await conn.execute(
        """
        UPDATE proposed_edges
           SET status = 'rejected', reviewed_at = now()
         WHERE id IN (
             SELECT id FROM proposed_edges
              WHERE status = 'pending'
                AND confidence < $1
                AND produced_at < NOW() - make_interval(days => $2)
              ORDER BY produced_at ASC
              LIMIT $3
         )
        """,
        float(max_confidence),
        int(min_age_days),
        int(limit),
    )
    try:
        return int(result.split()[-1]) if result else 0
    except (ValueError, IndexError):  # pragma: no cover - defensive
        return 0


def _build_finding(
    *,
    promoted: int,
    rejected: int,
    candidates: int,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Proposed-edge governance: {promoted} promoted, {rejected} rejected"
    )
    if target_id:
        title = f"{title} for {target_id}"
    tags = ["deterministic", "proposed_edge_governance"]
    if promoted:
        tags.append("edges_promoted")
    if rejected:
        tags.append("edges_rejected")
    return FindingPayload(
        title=title[:2048],
        body=(
            f"promoted={promoted} rejected={rejected} candidates={candidates}"
        )[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": "proposed_edge_governance",
            "promoted_count": promoted,
            "rejected_count": rejected,
            "candidates": candidates,
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Config (all on ``options``, conservative defaults):
      * ``promote_min_confidence`` (float, default 0.6)
      * ``reject_max_confidence``  (float, default 0.45)
      * ``reject_min_age_days``    (int,   default 30; <=0 disables rejection)
      * ``max_promotions_per_run`` (int,   default 200)
      * ``max_rejections_per_run`` (int,   default 2000)
    """
    promote_min = float(options.get("promote_min_confidence", DEFAULT_PROMOTE_MIN_CONFIDENCE))
    reject_max = float(options.get("reject_max_confidence", DEFAULT_REJECT_MAX_CONFIDENCE))
    reject_age = int(options.get("reject_min_age_days", DEFAULT_REJECT_MIN_AGE_DAYS))
    max_promotions = int(options.get("max_promotions_per_run", DEFAULT_MAX_PROMOTIONS_PER_RUN))
    max_rejections = int(options.get("max_rejections_per_run", DEFAULT_MAX_REJECTIONS_PER_RUN))

    analyst_id = str(options.get("analyst_id") or "proposed_edge_governance")
    analyst_version = str(options.get("analyst_version") or "")
    target_id = options.get("target_id")
    target_version = options.get("target_version")
    run_id = options.get("run_id")
    if isinstance(run_id, str):
        try:
            run_id = UUID(run_id)
        except ValueError:
            run_id = None
    if not isinstance(run_id, UUID):
        run_id = None

    promoted = 0
    rejected = 0
    candidates = 0
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is not None:
        try:
            async with pool.acquire() as conn:
                # Count pending promotion candidates first (for the receipt).
                candidates = int(
                    await conn.fetchval(
                        """
                        SELECT count(*) FROM proposed_edges
                         WHERE status = 'pending'
                           AND relationship_type = 'co_occurs'
                           AND confidence >= $1
                        """,
                        promote_min,
                    )
                    or 0
                )
                promoted = await _promote_candidates(
                    conn,
                    analyst_id=analyst_id,
                    analyst_version=analyst_version,
                    run_id=run_id,
                    target_id=str(target_id) if target_id else None,
                    target_version=str(target_version) if target_version else None,
                    min_confidence=promote_min,
                    limit=max_promotions,
                )
                rejected = await _reject_stale_thin(
                    conn,
                    max_confidence=reject_max,
                    min_age_days=reject_age,
                    limit=max_rejections,
                )
        except Exception as exc:
            logger.warning("proposed_edge_governance.failed err=%s", exc)

    finding = _build_finding(
        promoted=promoted,
        rejected=rejected,
        candidates=candidates,
        target_id=str(target_id) if target_id else None,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]

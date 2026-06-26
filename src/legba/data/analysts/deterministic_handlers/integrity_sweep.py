# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``integrity_sweep`` sub-handler — events-free referential-integrity sweep.

The re-homed successor to the pre-pivot ``integrity_verification`` handler
(deleted in review 2.4, see ``docs/DIRECTION.md`` §9). That handler ran eight
checks, but its FIRST check anchored on the dropped ``events`` table, so in
production the whole sweep raised, the error was swallowed, and it emitted a
zeroed "no issues" finding — fake success (the exact no-stub-rule violation).
Three more of its checks referenced tables the source-first pivot dropped
(``signal_event_links``, ``situation_events``, ``nexuses``).

This module keeps ONLY the checks that run against LIVE pivot-era tables, and
re-homes the two whose substrate moved:

  1. orphan ``signal_entity_links`` — signal-side (``signal_id`` with no
     ``signals`` row) and entity-side (``entity_id`` with no ``entity_profiles``
     row).
  2. orphan ``proposed_edges`` — ``source_entity`` / ``target_entity`` not
     present in ``entity_profiles.canonical_name``. This is the pivot's
     graph-edge table and REPLACES the old ``nexuses`` check (``nexuses`` was
     dropped in the pivot).
  3. ``facts`` with no supporting evidence — live, non-expired facts whose
     ``evidence_set`` is NULL or ``[]``.
  4. broken finding supersession — ``analyst_outputs.superseded_by`` pointing to
     a missing output row. The pivot moved supersession off ``facts`` onto the
     finding pool, so this REPLACES the old ``facts.superseded_by`` check.
  5. dangling ``analyst_outputs.derived_from`` edges — ``derived_from`` array
     elements that reference NO row in any lineage-catalog table (signals /
     analyst_outputs / facts / entity_profiles). This makes the dead-edge debt
     OBSERVABLE (D23) and is the regression sentinel for D10: ``country_optimizer``
     used to write ``analyst_traces.run_id`` into ``derived_from`` — those are
     not lineage-catalog rows, so they land here as a rising count if D10 ever
     regresses. The prune itself is a later operator-gated migration (roadmap
     0051); this handler only COUNTS, per its read-only audit contract.

Crucially — and unlike its predecessor — it **refuses loud**: a failing check
(e.g. a relation that does not exist) is NOT swallowed into a zeroed finding.
The exception propagates, the deterministic run errors visibly, and no
fake-clean finding is written. A 0-issue finding from this handler therefore
means every check genuinely ran and found nothing — never that the sweep aborted.

Scope: it is a **read-only audit** — it COUNTS drift and emits a finding. It does
NO destructive repair (the predecessor auto-nulled / auto-deleted; re-homing that
is deliberately out of scope — surfacing the counts for an operator/follow-up is
the safe first step).

Target-agnostic META analyst: the subscription declares no ``targets`` selector,
so the cadence heartbeat is a SINGLE global sweep over the whole substrate.

Registered via ``scripts/bringup_register_integrity_sweep.py`` — NOT inline
through a test fixture.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "integrity_sweep"

# Each check: (issue_key, SQL returning a single COUNT). Pure reads against LIVE
# pivot-era tables. A missing relation RAISES (asyncpg UndefinedTableError) and
# is deliberately NOT caught here — refuse loud (see module docstring).
_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "orphan_signal_entity_links_signal",
        """
        SELECT count(*) FROM signal_entity_links sel
        WHERE NOT EXISTS (SELECT 1 FROM signals s WHERE s.id = sel.signal_id)
        """,
    ),
    (
        "orphan_signal_entity_links_entity",
        """
        SELECT count(*) FROM signal_entity_links sel
        WHERE NOT EXISTS (
            SELECT 1 FROM entity_profiles ep WHERE ep.id = sel.entity_id
        )
        """,
    ),
    (
        "orphan_proposed_edges_source",
        """
        SELECT count(*) FROM proposed_edges pe
        WHERE NOT EXISTS (
            SELECT 1 FROM entity_profiles ep
            WHERE ep.canonical_name = pe.source_entity
        )
        """,
    ),
    (
        "orphan_proposed_edges_target",
        """
        SELECT count(*) FROM proposed_edges pe
        WHERE NOT EXISTS (
            SELECT 1 FROM entity_profiles ep
            WHERE ep.canonical_name = pe.target_entity
        )
        """,
    ),
    (
        "facts_no_evidence",
        """
        SELECT count(*) FROM facts f
        WHERE COALESCE(f.data->>'expired', 'false') <> 'true'
          AND (f.evidence_set IS NULL OR f.evidence_set = '[]'::jsonb)
        """,
    ),
    (
        "broken_finding_supersession",
        """
        SELECT count(*) FROM analyst_outputs ao
        WHERE ao.superseded_by IS NOT NULL
          AND NOT EXISTS (
              SELECT 1 FROM analyst_outputs ao2 WHERE ao2.id = ao.superseded_by
          )
        """,
    ),
    (
        # D23 observability + D10 regression sentinel. Counts DISTINCT
        # derived_from elements on analyst_outputs that reference NO row in any
        # lineage-catalog table. A trace run_id (the D10 bug) matches none of
        # these, so a D10 regression shows up here as a rising count. Read-only:
        # the prune is roadmap migration 0051. Cross-join UNNEST + a single NOT
        # EXISTS over the union keeps it index-friendly (GIN on derived_from is
        # not used here, but the per-table id PKs are).
        "dangling_analyst_output_derived_from",
        """
        SELECT count(*) FROM (
            SELECT DISTINCT df.ref
            FROM analyst_outputs ao
            CROSS JOIN LATERAL unnest(ao.derived_from) AS df(ref)
            WHERE array_length(ao.derived_from, 1) IS NOT NULL
              AND NOT EXISTS (SELECT 1 FROM signals s WHERE s.id = df.ref)
              AND NOT EXISTS (
                    SELECT 1 FROM analyst_outputs ao2 WHERE ao2.id = df.ref
              )
              AND NOT EXISTS (SELECT 1 FROM facts f WHERE f.id = df.ref)
              AND NOT EXISTS (
                    SELECT 1 FROM entity_profiles ep WHERE ep.id = df.ref
              )
        ) dangling
        """,
    ),
)


async def _verify(pool: Any) -> dict[str, int]:
    """Run every check. A missing relation RAISES (not caught) — refuse loud."""
    issues: dict[str, int] = {}
    async with pool.acquire() as conn:
        for key, sql in _CHECKS:
            issues[key] = int((await conn.fetchval(sql)) or 0)
    return issues


def _build_finding(*, issues: dict[str, int], target_id: str | None) -> FindingPayload:
    total = sum(issues.values())
    title = f"Integrity sweep: {total} issue(s) across {len(issues)} checks"
    if target_id:
        title = f"{title} for {target_id}"
    body_lines = [f"total_issues={total}"]
    for k in sorted(issues):
        body_lines.append(f"{k}={issues[k]}")
    tags = ["deterministic", "integrity_sweep"]
    tags.append("integrity_issues_present" if total > 0 else "integrity_clean")
    return FindingPayload(
        title=title[:2048],
        body="\n".join(body_lines)[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "issues": issues,
            "total_issues": total,
        },
    )


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    REFUSES LOUD: requires a live ``deps.pg_pool``; a failing check (e.g. a
    missing relation) propagates rather than being swallowed into a zeroed
    finding. Emits an honest summary finding every run — a 0-issue finding means
    the checks genuinely ran clean, never that the sweep aborted.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "integrity_sweep requires a live deps.pg_pool — refusing to emit a "
            "zeroed integrity finding without running the checks"
        )
    issues = await _verify(pool)  # NOT wrapped — a missing relation refuses loud
    total = sum(issues.values())
    if total > 0:
        logger.warning("integrity_sweep.issues total=%d detail=%s", total, issues)
    else:
        logger.info("integrity_sweep.clean checks=%d", len(issues))
    finding = _build_finding(issues=issues, target_id=options.get("target_id"))
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle"]

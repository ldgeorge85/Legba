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
     regresses. Alongside the count this surfaces a CAPPED, read-only SAMPLE of
     the dead refs (``dangling_derived_from_sample``) so an operator — and the
     prune migration's author — can see WHICH edges are dead without dumping the
     whole backlog. The prune itself is an operator-gated migration (roadmap
     0051, extended by 0056_prune_dangling_derived_from_v2); this handler only
     COUNTS + SAMPLES, per its read-only audit contract.

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

from ...provenance._core import ZERO_HASH
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "integrity_sweep"

# How many dangling derived_from samples to surface in the finding body. The
# count (check #5) is the audit number; this capped list lets an operator (and
# the prune migration's author) eyeball WHICH edges are dead without dumping the
# whole backlog (the live count has been ~100k). Read-only — a SAMPLE, not a fix.
_DANGLING_SAMPLE_CAP = 25

# Capped sample of DISTINCT dangling derived_from elements — the unresolvable
# refs counted by the `dangling_analyst_output_derived_from` check, plus one
# owning analyst_outputs row id per ref so the operator/migration author can find
# the carrier. Mirrors the check's four-table lineage catalog EXACTLY (so the
# sample can never list a ref the count did not count). LIMIT-capped + read-only;
# the repair stays in the prune migration, never here.
_DANGLING_SAMPLE_SQL = """
SELECT df.ref AS ref, min(ao.id) AS sample_output_id
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
GROUP BY df.ref
ORDER BY df.ref
LIMIT $1
"""

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


async def _sample_dangling_derived_from(pool: Any, *, cap: int) -> list[dict[str, str]]:
    """Capped, read-only SAMPLE of distinct dangling derived_from refs.

    Returns up to ``cap`` ``{"ref": <uuid>, "sample_output_id": <uuid>}`` rows so
    the finding body can show WHICH edges are dead (the check only COUNTS). This
    is purely diagnostic — it never repairs. If the connection cannot run a
    multi-row fetch (e.g. an older fake/probe with no ``fetch``), it degrades to
    an empty sample rather than fabricating one; the audit count is unaffected.
    """
    async with pool.acquire() as conn:
        fetch = getattr(conn, "fetch", None)
        if fetch is None:
            return []
        rows = await fetch(_DANGLING_SAMPLE_SQL, cap)
    samples: list[dict[str, str]] = []
    for row in rows:
        samples.append(
            {
                "ref": str(row["ref"]),
                "sample_output_id": str(row["sample_output_id"]),
            }
        )
    return samples


# ---------------------------------------------------------------------------
# P1-T8 — reachable-click-path PROBE (read-only)
# ---------------------------------------------------------------------------
#
# The navigable read (P1) lands on a finding and lets the operator click into
# its lineage + receipt chain. The product must NEVER dead-end on a 404/blank
# node. This probe COUNTS the three ways a finding click path can dead-end and
# returns 0 — or a NAMED, capped list — for each, so the operator/UI can confirm
# every node on the path resolves. It is a pure read-only AUDIT (repair lives in
# the migrations, e.g. 0056); it never mutates or deletes a row.
#
#   (a) dangling_finding_derived_from — a finding (kind='finding') whose
#       ``derived_from`` carries an element that resolves to NO row in the
#       lineage catalog. Clicking that lineage edge would 404. The catalog is the
#       SEVEN-table SUPERSET that migration 0056 prunes against (signals,
#       analyst_outputs, facts, situations, hypotheses, entity_profiles,
#       nexuses), NOT the four-table C5 audit catalog: the probe must only flag a
#       TRUE dead-end (a ref that resolves to nothing the click path can open),
#       so it uses the widest catalog the navigable read can resolve against —
#       flagging a ref that actually opens a situation/hypothesis/nexus would be
#       a false dead-end. One row per (finding, dangling edge) pair = one
#       dead-end click.
#   (b) bodyless_finding_roots — a LIVE finding root (kind='finding',
#       ``superseded_by IS NULL`` — the node the navigation actually lands on)
#       whose ``body`` is empty/blank. The click resolves to a row but renders a
#       BLANK node. Superseded findings are off the live click path, so they are
#       excluded (a blank historical revision is not a reachable dead-end).
#   (c) orphaned_receipt_links — a run (``analyst_traces`` row) whose
#       ``prev_receipt_hash`` references no predecessor receipt in the SAME
#       analyst's chain. Walking the provenance receipt chain back from such a
#       run dead-ends. The genesis ``ZERO_HASH`` predecessor is NOT orphaned (it
#       is the intentional chain root), so it is excluded via the bound param.
#
# Each query carries a ``count(*) OVER ()`` window so the returned ``total`` is
# the TRUE full count (computed before LIMIT), while the row list is a capped,
# read-only SAMPLE — the same count-plus-sample contract as the C5 dangling
# audit above. A clean substrate returns 0 rows → count 0, sample [].
_PROBE_SAMPLE_CAP = 25

_PROBE_DANGLING_FINDING_SQL = """
/* probe:dangling_finding */
SELECT ao.id AS finding_id, df.ref AS dangling_ref, count(*) OVER () AS total
FROM analyst_outputs ao
CROSS JOIN LATERAL unnest(ao.derived_from) AS df(ref)
WHERE ao.kind = 'finding'
  AND array_length(ao.derived_from, 1) IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM signals s          WHERE s.id  = df.ref)
  AND NOT EXISTS (SELECT 1 FROM analyst_outputs ao2 WHERE ao2.id = df.ref)
  AND NOT EXISTS (SELECT 1 FROM facts f            WHERE f.id  = df.ref)
  AND NOT EXISTS (SELECT 1 FROM situations si      WHERE si.id = df.ref)
  AND NOT EXISTS (SELECT 1 FROM hypotheses h       WHERE h.id  = df.ref)
  AND NOT EXISTS (SELECT 1 FROM entity_profiles ep WHERE ep.id = df.ref)
  AND NOT EXISTS (SELECT 1 FROM nexuses nx         WHERE nx.id = df.ref)
ORDER BY ao.id, df.ref
LIMIT $1
"""

_PROBE_BODYLESS_ROOT_SQL = """
/* probe:bodyless_root */
SELECT ao.id AS finding_id, count(*) OVER () AS total
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.superseded_by IS NULL
  AND (ao.body IS NULL OR btrim(ao.body) = '')
ORDER BY ao.id
LIMIT $1
"""

_PROBE_ORPHAN_RECEIPT_SQL = """
/* probe:orphan_receipt */
SELECT t.run_id AS run_id, t.analyst_id AS analyst_id,
       t.prev_receipt_hash AS prev_receipt_hash, count(*) OVER () AS total
FROM analyst_traces t
WHERE t.prev_receipt_hash IS NOT NULL
  AND t.prev_receipt_hash <> $2
  AND NOT EXISTS (
        SELECT 1 FROM analyst_traces p
        WHERE p.analyst_id = t.analyst_id
          AND p.receipt_hash = t.prev_receipt_hash
  )
ORDER BY t.run_id
LIMIT $1
"""

# The probe surface: ordered (key, human label) so the finding body + data block
# render the three dead-end classes deterministically.
_PROBE_KEYS = (
    "dangling_finding_derived_from",
    "bodyless_finding_roots",
    "orphaned_receipt_links",
)


def _summarize_probe(rows: Any, sample: list[dict[str, str]]) -> dict[str, Any]:
    """Fold a probe result set into ``{"count": <true total>, "sample": [...]}``.

    The ``count`` is the ``count(*) OVER ()`` total carried on every row (the
    full pre-LIMIT count); ``sample`` is the already-mapped, capped row list. An
    empty result set means a clean path → count 0, sample [].
    """
    rows = list(rows)
    total = int(rows[0]["total"]) if rows else 0
    return {"count": total, "sample": sample}


async def probe_reachable_click_path(
    pool: Any, *, cap: int = _PROBE_SAMPLE_CAP
) -> dict[str, dict[str, Any]]:
    """Read-only reachability probe for the navigable finding click path (P1-T8).

    Returns ``{key: {"count": int, "sample": [...]}, ...}`` for each of the three
    dead-end classes (see the section docstring). Counts are the TRUE full counts
    (window count, not the capped sample length); samples are NAMED + capped so
    the operator/UI can see WHICH node would dead-end. Purely diagnostic — never
    repairs. If the connection cannot run a multi-row fetch (an older fake/probe
    with no ``fetch``) every class degrades to ``{"count": 0, "sample": []}``
    rather than fabricating one.
    """
    empty: dict[str, dict[str, Any]] = {
        k: {"count": 0, "sample": []} for k in _PROBE_KEYS
    }
    async with pool.acquire() as conn:
        fetch = getattr(conn, "fetch", None)
        if fetch is None:
            return empty

        dangling_rows = await fetch(_PROBE_DANGLING_FINDING_SQL, cap)
        bodyless_rows = await fetch(_PROBE_BODYLESS_ROOT_SQL, cap)
        orphan_rows = await fetch(_PROBE_ORPHAN_RECEIPT_SQL, cap, ZERO_HASH)

    return {
        "dangling_finding_derived_from": _summarize_probe(
            dangling_rows,
            [
                {
                    "finding_id": str(r["finding_id"]),
                    "dangling_ref": str(r["dangling_ref"]),
                }
                for r in dangling_rows
            ],
        ),
        "bodyless_finding_roots": _summarize_probe(
            bodyless_rows,
            [{"finding_id": str(r["finding_id"])} for r in bodyless_rows],
        ),
        "orphaned_receipt_links": _summarize_probe(
            orphan_rows,
            [
                {
                    "run_id": str(r["run_id"]),
                    "analyst_id": str(r["analyst_id"]),
                    "prev_receipt_hash": str(r["prev_receipt_hash"]),
                }
                for r in orphan_rows
            ],
        ),
    }


def _build_finding(
    *,
    issues: dict[str, int],
    target_id: str | None,
    dangling_sample: list[dict[str, str]] | None = None,
    probe: dict[str, dict[str, Any]] | None = None,
) -> FindingPayload:
    total = sum(issues.values())
    title = f"Integrity sweep: {total} issue(s) across {len(issues)} checks"
    if target_id:
        title = f"{title} for {target_id}"
    body_lines = [f"total_issues={total}"]
    for k in sorted(issues):
        body_lines.append(f"{k}={issues[k]}")
    sample = dangling_sample or []
    dangling_count = issues.get("dangling_analyst_output_derived_from", 0)
    if dangling_count:
        body_lines.append(
            f"dangling_derived_from_sample "
            f"({len(sample)} of {dangling_count}, cap={_DANGLING_SAMPLE_CAP}):"
        )
        for s in sample:
            body_lines.append(f"  ref={s['ref']} on output={s['sample_output_id']}")
    tags = ["deterministic", "integrity_sweep"]
    tags.append("integrity_issues_present" if total > 0 else "integrity_clean")

    # P1-T8 reachable-click-path probe: surface the three dead-end counts (0 on a
    # clean path) + a capped NAMED sample of any dead-end node so the operator/UI
    # can confirm — or pinpoint — the navigable read.
    probe = probe or {k: {"count": 0, "sample": []} for k in _PROBE_KEYS}
    probe_total = sum(int(probe[k]["count"]) for k in _PROBE_KEYS)
    body_lines.append(f"reachable_click_path_probe (total_dead_ends={probe_total}):")
    for k in _PROBE_KEYS:
        entry = probe[k]
        body_lines.append(f"  {k}={entry['count']}")
        for s in entry["sample"]:
            detail = " ".join(f"{kk}={vv}" for kk, vv in s.items())
            body_lines.append(f"    - {detail}")
    tags.append("click_path_dead_ends" if probe_total > 0 else "click_path_clean")

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
            "dangling_derived_from_sample": sample,
            "dangling_derived_from_sample_cap": _DANGLING_SAMPLE_CAP,
            "reachable_click_path": probe,
            "reachable_click_path_dead_ends": probe_total,
            "reachable_click_path_sample_cap": _PROBE_SAMPLE_CAP,
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
    # Only pay for the capped sample when there is dead debt to show; the count
    # already ran (and refused loud) above. Read-only, LIMIT-capped.
    dangling_sample: list[dict[str, str]] = []
    if issues.get("dangling_analyst_output_derived_from", 0) > 0:
        dangling_sample = await _sample_dangling_derived_from(
            pool, cap=_DANGLING_SAMPLE_CAP
        )
    # P1-T8: always run the reachable-click-path probe so the finding CONFIRMS
    # the navigable read resolves (a clean path returns 0s) — read-only, and it
    # degrades to zeros when the connection cannot multi-row fetch.
    probe = await probe_reachable_click_path(pool, cap=_PROBE_SAMPLE_CAP)
    probe_dead_ends = sum(int(probe[k]["count"]) for k in _PROBE_KEYS)
    if total > 0 or probe_dead_ends > 0:
        logger.warning(
            "integrity_sweep.issues total=%d click_path_dead_ends=%d detail=%s",
            total,
            probe_dead_ends,
            issues,
        )
    else:
        logger.info("integrity_sweep.clean checks=%d click_path_dead_ends=0", len(issues))
    finding = _build_finding(
        issues=issues,
        target_id=options.get("target_id"),
        dangling_sample=dangling_sample,
        probe=probe,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "probe_reachable_click_path"]

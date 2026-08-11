#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Harvest OPEN QUESTIONS from analysis products that already exist (K-2a).

The system's unresolved questions are first-class, queryable objects: a
``hypotheses`` row with ``status='open_question'`` (the same shape the
``open_question`` agency write tool lands — see
``legba.data.analysts.agency.write_tools.open_question_tool``). The tool
faucet exists but has never been opened by a model, so the open-question set
is empty even though the substrate ALREADY records plenty of question-shaped
state. This script seeds the set deterministically from five source classes:

  * ``scorecard_disagreement`` — a scorecard↔composition contradiction (B0-5):
    a dimension the scorecard banded ``insufficient-evidence`` while the live
    composition head cites / derives from a finding of that same dimension.
    Recomputed with the SAME pure reducers both read surfaces use
    (``legba.data.registry.scorecard_reconcile``), so the harvest can never
    drift from what the panel shows.
  * ``freshness_advisory`` — an F-1 compose-time staleness stamp
    (``data.freshness.stale_roots`` on an OPEN composition head): the head was
    composed over an input reading that was later materially reversed. Whether
    the composition still holds is a standing question. (The sibling
    ``inputs_as_of`` ledger is timestamps only — not question-shaped — and is
    deliberately not harvested.)
  * ``below_floor`` — an OPEN finding whose latest faithfulness critique
    grades it below the verify floor (effective = min(confidence, score) <
    ``scorecard_banding.FAITH_FLOOR``): a floored claim is an unresolved
    assertion, not a settled one. Ungraded findings are NOT harvested (no
    verdict is not a question about the claim's truth).
  * ``fact_contention`` — an open contested-fact group from the arbiter's
    sidecar (``fact_contention`` status ``contested`` OR ``surfaced`` — a
    surfaced winner still has a live dispute; ``collapsed`` groups are
    resolved and skipped).
  * ``collection_gap`` — each starved ``desk × dimension`` cell in an OPEN
    ``collection_gap`` finding (``data.data.gaps``). Keyed by
    ``(desk, dimension)`` WITHOUT the finding id so a gap that persists across
    monthly sweeps stays ONE open question.

Idempotency / marker
--------------------

Each harvested row carries ONE durable marker object in the persisted
``diagnostic_evidence`` JSONB column::

    {"marker": "open_question_origin", "origin": "harvest",
     "harvest_class": "<class>", "source_id": "<class-specific id>"}

Re-runs dedup on jsonb containment of ``(harvest_class, source_id)`` — never a
duplicate. NOTE: the marker lives in ``diagnostic_evidence`` (not the payload's
``data`` dict) because the ``hypotheses`` table has NO ``data`` column —
``writes._insert_hypothesis`` drops the payload's ``data`` extras on insert
(true for the ``open_question`` tool's marker today too). ``diagnostic_evidence``
persists and is containment-queryable.

Dry-run by default; ``--apply`` writes. Bounded per class via ``--limit``.

Run in the registry container (runtime deps installed), pointed at live PG::

  docker exec -e LEGBA_DATA_PG_DB=legba -e LEGBA_DATA_PG_HOST=legba-postgres-1 \\
      -e PYTHONPATH=/app/src <registry-container> \\
      python3 /app/scripts/harvest_open_questions.py [--apply]

Or on the host: ``PYTHONPATH=src LEGBA_DATA_PG_DB=legba python3
scripts/harvest_open_questions.py [--apply] [--limit N] [--classes a,b]``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID, uuid4

import asyncpg

from legba.data.analysts.deterministic_handlers.scorecard_banding import FAITH_FLOOR
from legba.data.analysts.question_text import ungrounded_office
from legba.data.provenance import AnalystContext, HypothesisPayload, write_hypothesis
from legba.data.registry.scorecard_reconcile import (
    composition_usages,
    scorecard_disagreements,
)

# Provenance identity stamped on every harvested row (functional, queryable).
HARVEST_ANALYST_ID = "open_question_harvest"
HARVEST_ANALYST_VERSION = "1"

# The durable idempotency-marker key (diagnostic_evidence containment key).
MARKER_KEY = "open_question_origin"

HARVEST_CLASSES: tuple[str, ...] = (
    "scorecard_disagreement",
    "freshness_advisory",
    "below_floor",
    "fact_contention",
    "collection_gap",
)

# Cap on derived_from refs per harvested row (a contention group can carry many
# supporting facts; the question needs lineage, not the full roster).
_MAX_DERIVED_REFS = 16


@dataclass
class HarvestItem:
    """One candidate open question, ready to write."""

    harvest_class: str
    source_id: str
    question: str
    derived_from: list[UUID]
    target_id: str | None = None
    counter: str = ""


@dataclass
class ClassCounts:
    """Per-class accounting, printed in both dry-run and apply modes."""

    candidates: int = 0
    existing: int = 0
    written: int = 0
    skipped: dict[str, int] = field(default_factory=dict)

    def skip(self, reason: str) -> None:
        self.skipped[reason] = self.skipped.get(reason, 0) + 1


def _uuid_or_none(value: Any) -> UUID | None:
    try:
        return value if isinstance(value, UUID) else UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _jsonb(value: Any) -> dict[str, Any]:
    """A row's jsonb column as a dict (asyncpg may hand back str or dict)."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return {}
    return value if isinstance(value, dict) else {}


def marker_for(harvest_class: str, source_id: str) -> dict[str, str]:
    """The durable idempotency marker stored in ``diagnostic_evidence``."""
    return {
        "marker": MARKER_KEY,
        "origin": "harvest",
        "harvest_class": harvest_class,
        "source_id": source_id,
    }


async def _already_harvested(
    conn: asyncpg.Connection, harvest_class: str, source_id: str
) -> bool:
    """True when a prior run already landed this (class, source) marker."""
    probe = json.dumps(
        [{"marker": MARKER_KEY, "harvest_class": harvest_class, "source_id": source_id}]
    )
    row = await conn.fetchval(
        "SELECT 1 FROM hypotheses "
        "WHERE status = 'open_question' AND diagnostic_evidence @> $1::jsonb "
        "LIMIT 1",
        probe,
    )
    return row is not None


# ---------------------------------------------------------------------------
# Collectors — one per harvest class. Each returns (items, counts).
# ---------------------------------------------------------------------------


async def collect_scorecard_disagreements(
    conn: asyncpg.Connection, *, limit: int
) -> tuple[list[HarvestItem], ClassCounts]:
    """Scorecard↔composition contradictions via the SHARED pure reducers.

    Mirrors the fetch recipe of ``substrate_query_port.
    _reconcile_scorecard_disagreements`` / the registry ``/eval/country_scorecard``
    endpoint: latest OPEN scorecard head per target (``data.data.bands.
    dimensions``) + latest OPEN ``country_composition`` head per target
    (``data.data.citations`` + the ``derived_from`` uuid[] column), reduced by
    ``scorecard_reconcile``. A target with only one of the two heads has nothing
    to reconcile → counted ``skipped.unpaired_target``.
    """
    counts = ClassCounts()
    sc_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (target_id) id, target_id, data
          FROM analyst_outputs
         WHERE kind = 'scorecard' AND superseded_by IS NULL
           AND target_id IS NOT NULL
         ORDER BY target_id, produced_at DESC, id DESC
        """
    )
    comp_rows = await conn.fetch(
        """
        SELECT DISTINCT ON (target_id)
               id, target_id, derived_from::text[] AS derived_from, data
          FROM analyst_outputs
         WHERE kind = 'finding' AND analyst_id = 'country_composition'
           AND superseded_by IS NULL AND target_id IS NOT NULL
         ORDER BY target_id, produced_at DESC, id DESC
        """
    )
    dims_by_target: dict[str, tuple[UUID, dict[str, Any]]] = {}
    for r in sc_rows:
        payload = _jsonb(r["data"])
        dims = ((payload.get("data") or {}).get("bands") or {}).get("dimensions")
        if isinstance(dims, dict):
            dims_by_target[str(r["target_id"])] = (r["id"], dims)
        else:
            counts.skip("scorecard_without_bands")
    comp_by_target: dict[str, tuple[UUID, Any, list[str]]] = {}
    unresolved: set[str] = set()
    for r in comp_rows:
        payload = _jsonb(r["data"])
        citations = (payload.get("data") or {}).get("citations")
        derived = [str(x) for x in (r["derived_from"] or [])]
        comp_by_target[str(r["target_id"])] = (r["id"], citations, derived)
        cited = composition_usages(citations, [], {})
        unresolved.update(f for f in derived if f not in cited)
    derived_analysts: dict[str, str] = {}
    if unresolved:
        lu = await conn.fetch(
            "SELECT id::text AS id, analyst_id FROM analyst_outputs "
            "WHERE id = ANY($1::uuid[])",
            sorted(_uuid_or_none(u) for u in unresolved if _uuid_or_none(u)),
        )
        derived_analysts = {r["id"]: r["analyst_id"] for r in lu if r["analyst_id"]}

    items: list[HarvestItem] = []
    for tgt in sorted(set(dims_by_target) | set(comp_by_target)):
        if tgt not in dims_by_target or tgt not in comp_by_target:
            counts.skip("unpaired_target")
            continue
        sc_id, dims = dims_by_target[tgt]
        comp_id, citations, derived = comp_by_target[tgt]
        for d in scorecard_disagreements(
            dims, composition_usages(citations, derived, derived_analysts)
        ):
            if len(items) >= limit:
                counts.skip("over_limit")
                continue
            refs = [sc_id, comp_id]
            fid = _uuid_or_none(d.finding_id)
            if fid is not None:
                refs.insert(0, fid)
            verb = "cites" if d.composition_usage == "cited" else "derives from"
            items.append(
                HarvestItem(
                    harvest_class="scorecard_disagreement",
                    source_id=f"{tgt}:{d.dimension}:{d.finding_id}",
                    question=(
                        f"Is the {d.dimension} assessment for {tgt} adequately "
                        f"evidenced? The scorecard excluded it "
                        f"({d.scorecard_verdict}) while the current composition "
                        f"head {verb} finding {d.finding_id} from that same "
                        f"dimension — which reading is right?"
                    ),
                    counter=d.note,
                    derived_from=refs,
                    target_id=tgt,
                )
            )
    counts.candidates = len(items)
    return items, counts


async def collect_freshness_advisories(
    conn: asyncpg.Connection, *, limit: int
) -> tuple[list[HarvestItem], ClassCounts]:
    """F-1 stale-input stamps on OPEN composition heads → standing questions.

    One question per (open head, stale root): the head composed over a reading
    that was later materially reversed. Malformed stale_root entries (no
    resolvable old_id) are counted ``skipped.malformed_stale_root``. Only OPEN
    heads are scanned, so a re-compose that resolved the staleness stops
    producing candidates (the old head's harvested question remains for the
    operator to close).
    """
    counts = ClassCounts()
    rows = await conn.fetch(
        """
        SELECT id, analyst_id, target_id, data
          FROM analyst_outputs
         WHERE kind = 'finding' AND superseded_by IS NULL
           AND data->'data'->'freshness'->'stale_roots' IS NOT NULL
         ORDER BY produced_at DESC, id DESC
         LIMIT $1
        """,
        limit,
    )
    items: list[HarvestItem] = []
    for r in rows:
        payload = _jsonb(r["data"])
        stale_roots = ((payload.get("data") or {}).get("freshness") or {}).get(
            "stale_roots"
        )
        if not isinstance(stale_roots, list):
            counts.skip("malformed_freshness_block")
            continue
        for sr in stale_roots:
            if len(items) >= limit:
                counts.skip("over_limit")
                continue
            if not isinstance(sr, dict):
                counts.skip("malformed_stale_root")
                continue
            old_id = _uuid_or_none(sr.get("old_id"))
            if old_id is None:
                counts.skip("malformed_stale_root")
                continue
            new_id = _uuid_or_none(sr.get("new_id"))
            unit = str(sr.get("unit") or "unit")
            target = sr.get("target") or r["target_id"]
            refs = [r["id"], old_id] + ([new_id] if new_id is not None else [])
            items.append(
                HarvestItem(
                    harvest_class="freshness_advisory",
                    source_id=f"{r['id']}:{old_id}",
                    question=(
                        f"Does the {r['analyst_id']} composition for "
                        f"{target or 'its scope'} still hold, given its {unit} "
                        f"input \"{str(sr.get('old_title') or '')[:200]}\" "
                        f"(confidence {sr.get('old_confidence')}) was superseded "
                        f"by \"{str(sr.get('new_title') or '')[:200]}\" "
                        f"(confidence {sr.get('new_confidence')})?"
                    ),
                    derived_from=refs,
                    target_id=str(target) if target else None,
                )
            )
    counts.candidates = len(items)
    return items, counts


async def collect_below_floor(
    conn: asyncpg.Connection, *, limit: int
) -> tuple[list[HarvestItem], ClassCounts]:
    """OPEN findings floored by their latest faithfulness critique.

    Mirrors the read-path verdict (``substrate_reads_api``): latest critique
    pinned by ``title LIKE 'Faithfulness verify%'``; effective =
    min(confidence, overall_score); below-floor iff effective < FAITH_FLOOR.
    The INNER lateral join means ungraded findings never appear (an ungraded
    row has no verdict — not harvested, by design).
    """
    counts = ClassCounts()
    rows = await conn.fetch(
        """
        SELECT f.id, f.title, f.confidence, f.target_id, f.analyst_id,
               c.critique_id, c.critic_score
          FROM analyst_outputs f
          JOIN LATERAL (
              SELECT cr.id AS critique_id,
                     (cr.data->>'overall_score')::real AS critic_score
                FROM analyst_outputs cr
               WHERE cr.kind = 'critique'
                 AND cr.data->>'analyzed_output_id' = f.id::text
                 AND cr.data->>'overall_score' IS NOT NULL
                 AND cr.title LIKE 'Faithfulness verify%'
               ORDER BY cr.produced_at DESC, cr.id DESC
               LIMIT 1
          ) c ON TRUE
         WHERE f.kind = 'finding' AND f.superseded_by IS NULL
           AND LEAST(f.confidence, c.critic_score) < $1
         ORDER BY f.produced_at DESC, f.id DESC
         LIMIT $2
        """,
        FAITH_FLOOR,
        limit,
    )
    items: list[HarvestItem] = []
    for r in rows:
        effective = min(float(r["confidence"]), float(r["critic_score"]))
        scope = f"{r['analyst_id']}, {r['target_id']}" if r["target_id"] else str(
            r["analyst_id"]
        )
        items.append(
            HarvestItem(
                harvest_class="below_floor",
                source_id=str(r["id"]),
                question=(
                    f"Floored claim ({scope}): \"{str(r['title'])[:300]}\" failed "
                    f"the faithfulness floor (effective {effective:.2f} < "
                    f"{FAITH_FLOOR:.2f}). Does the underlying assertion hold "
                    f"against better evidence, or should it be discarded?"
                ),
                derived_from=[r["id"], r["critique_id"]],
                target_id=r["target_id"],
            )
        )
    counts.candidates = len(items)
    return items, counts


async def collect_fact_contentions(
    conn: asyncpg.Connection, *, limit: int
) -> tuple[list[HarvestItem], ClassCounts]:
    """Open contested-fact groups (arbiter sidecar) → one question each.

    Harvests ``status IN ('contested','surfaced')`` — a surfaced winner is the
    arbiter's current best, but the group remains a live dispute. ``collapsed``
    groups are resolved (counted ``skipped.collapsed``); a group with fewer than
    two non-junk value clusters is not a genuine dispute (counted
    ``skipped.single_value``). Lineage = the non-junk clusters' representative
    facts + the surfaced winner (bounded).
    """
    counts = ClassCounts()
    collapsed = await conn.fetchval(
        "SELECT count(*) FROM fact_contention WHERE status = 'collapsed'"
    )
    if collapsed:
        counts.skipped["collapsed"] = int(collapsed)
    groups = await conn.fetch(
        """
        SELECT id, subject_key, predicate_key, status, surfaced_value,
               surfaced_fact_id, value_count
          FROM fact_contention
         WHERE status IN ('contested', 'surfaced')
         ORDER BY opened_at DESC, id
         LIMIT $1
        """,
        limit,
    )
    values_by_group: dict[UUID, list[asyncpg.Record]] = {}
    if groups:
        vrows = await conn.fetch(
            """
            SELECT contention_id, representative_fact_id, is_junk
              FROM fact_contention_values
             WHERE contention_id = ANY($1::uuid[])
             ORDER BY arbiter_score DESC NULLS LAST, id
            """,
            [g["id"] for g in groups],
        )
        for v in vrows:
            values_by_group.setdefault(v["contention_id"], []).append(v)
    items: list[HarvestItem] = []
    for g in groups:
        clusters = [
            v for v in values_by_group.get(g["id"], []) if not v["is_junk"]
        ]
        if len(clusters) < 2:
            counts.skip("single_value")
            continue
        refs: list[UUID] = []
        if g["surfaced_fact_id"] is not None:
            refs.append(g["surfaced_fact_id"])
        for v in clusters:
            rf = v["representative_fact_id"]
            if rf is not None and rf not in refs:
                refs.append(rf)
        refs = refs[:_MAX_DERIVED_REFS]
        winner = (
            f"current surfaced winner: \"{str(g['surfaced_value'])[:200]}\""
            if g["status"] == "surfaced" and g["surfaced_value"]
            else "no winner surfaced"
        )
        items.append(
            HarvestItem(
                harvest_class="fact_contention",
                source_id=str(g["id"]),
                question=(
                    f"Contested fact: which value of \"{g['predicate_key']}\" "
                    f"for \"{g['subject_key']}\" is correct? "
                    f"{len(clusters)} competing value clusters; {winner}."
                ),
                derived_from=refs,
            )
        )
    counts.candidates = len(items)
    return items, counts


async def collect_collection_gaps(
    conn: asyncpg.Connection, *, limit: int
) -> tuple[list[HarvestItem], ClassCounts]:
    """Starved desk×dimension cells from OPEN ``collection_gap`` findings.

    Keyed ``(desk, dimension)`` WITHOUT the finding id, so the same cell
    surfacing in successive monthly sweeps stays ONE open question (newest
    finding wins the lineage; repeats counted ``skipped.repeat_cell``).
    Malformed gap entries counted ``skipped.malformed_gap``.
    """
    counts = ClassCounts()
    rows = await conn.fetch(
        """
        SELECT id, target_id, data
          FROM analyst_outputs
         WHERE kind = 'finding' AND superseded_by IS NULL
           AND data->'data'->>'sub_handler' = 'collection_gap'
         ORDER BY produced_at DESC, id DESC
         LIMIT $1
        """,
        limit,
    )
    items: list[HarvestItem] = []
    seen_cells: set[tuple[str, str]] = set()
    for r in rows:
        payload = _jsonb(r["data"])
        gaps = (payload.get("data") or {}).get("gaps")
        if not isinstance(gaps, list):
            counts.skip("malformed_gaps_block")
            continue
        for g in gaps:
            if len(items) >= limit:
                counts.skip("over_limit")
                continue
            if not isinstance(g, dict) or not g.get("desk") or not g.get("dimension"):
                counts.skip("malformed_gap")
                continue
            desk = str(g["desk"])
            dim = str(g["dimension"])
            if (desk, dim) in seen_cells:
                counts.skip("repeat_cell")
                continue
            seen_cells.add((desk, dim))
            classes = ", ".join(
                str(c) for c in (g.get("source_classes") or [])
            ) or "(none mapped)"
            persist = ""
            if g.get("window_scorecards"):
                persist = (
                    f"; persistent {g.get('insufficient_count')}/"
                    f"{g.get('window_scorecards')} cards"
                )
            items.append(
                HarvestItem(
                    harvest_class="collection_gap",
                    source_id=f"{desk}:{dim}",
                    question=(
                        f"Collection gap: the {dim} dimension for desk {desk} "
                        f"is starved ({g.get('reason') or 'insufficient-evidence'}"
                        f"{persist}). What sources would close it? Plausible "
                        f"source classes: {classes}."
                    ),
                    derived_from=[r["id"]],
                    target_id=desk,
                )
            )
    counts.candidates = len(items)
    return items, counts


_COLLECTORS = {
    "scorecard_disagreement": collect_scorecard_disagreements,
    "freshness_advisory": collect_freshness_advisories,
    "below_floor": collect_below_floor,
    "fact_contention": collect_fact_contentions,
    "collection_gap": collect_collection_gaps,
}


# ---------------------------------------------------------------------------
# Writer + driver
# ---------------------------------------------------------------------------


async def _write_item(conn: asyncpg.Connection, item: HarvestItem) -> bool:
    """Land ONE harvested question via the SAME write path the tool uses.

    Returns True when a row landed (False = payload rejected to dead-letter).
    """
    ctx = AnalystContext(
        analyst_id=HARVEST_ANALYST_ID,
        analyst_version=HARVEST_ANALYST_VERSION,
        run_id=uuid4(),
        target_id=item.target_id,
    )
    payload = HypothesisPayload(
        thesis=item.question[:4096],
        counter_thesis=item.counter[:4096],
        status="open_question",
        diagnostic_evidence=[marker_for(item.harvest_class, item.source_id)],
    )
    row, _dlq = await write_hypothesis(
        conn,
        analyst_ctx=ctx,
        payload=payload,
        derived_from=item.derived_from[:_MAX_DERIVED_REFS],
    )
    return row is not None


async def run_harvest(
    conn: asyncpg.Connection,
    *,
    classes: list[str] | None = None,
    limit: int = 100,
    apply: bool = False,
) -> dict[str, ClassCounts]:
    """Collect + (optionally) write; returns per-class counts.

    Dry-run (``apply=False``) performs ZERO writes — it still runs the dedup
    probe so the printed "written" column is what an apply WOULD land.
    """
    selected = list(classes) if classes else list(HARVEST_CLASSES)
    unknown = [c for c in selected if c not in _COLLECTORS]
    if unknown:
        raise SystemExit(f"unknown harvest class(es): {', '.join(unknown)}")
    results: dict[str, ClassCounts] = {}
    for cls in selected:
        items, counts = await _COLLECTORS[cls](conn, limit=limit)
        for item in items:
            # CW-8 — an office with nothing to bind it to is not a question
            # the substrate can answer; it is a slot. Flagged and NOT
            # harvested, counted under its own reason so a collector that
            # starts producing them is visible in the report rather than
            # discovered later in a precision round. Inert for today's
            # collectors (their theses are machine-built from substrate keys
            # and always name a subject or a desk) — which is exactly the
            # posture a guard on a generated surface should have.
            offices = ungrounded_office(item.question)
            if offices:
                counts.skip("ungrounded_office")
                continue
            if await _already_harvested(conn, item.harvest_class, item.source_id):
                counts.existing += 1
                continue
            if apply:
                if await _write_item(conn, item):
                    counts.written += 1
                else:
                    counts.skip("dead_lettered")
            else:
                counts.written += 1  # dry-run: "would write"
        results[cls] = counts
    return results


def _print_report(results: dict[str, ClassCounts], *, apply: bool) -> None:
    mode = "APPLY" if apply else "DRY-RUN"
    written_hdr = "written" if apply else "would_write"
    print(f"open-question harvest — {mode}")
    total = 0
    for cls, c in results.items():
        skips = (
            "; ".join(f"{k}={v}" for k, v in sorted(c.skipped.items())) or "-"
        )
        print(
            f"  {cls:24s} candidates={c.candidates:4d} existing={c.existing:4d} "
            f"{written_hdr}={c.written:4d} skipped: {skips}"
        )
        total += c.written
    print(f"  TOTAL {written_hdr}: {total}")


async def _amain() -> int:
    ap = argparse.ArgumentParser(
        description="Harvest open questions from existing analysis products."
    )
    ap.add_argument(
        "--apply", action="store_true",
        help="write the hypotheses rows (default: dry-run, zero writes)",
    )
    ap.add_argument(
        "--limit", type=int, default=100,
        help="max harvested items per class (default 100)",
    )
    ap.add_argument(
        "--classes", default="",
        help=f"comma-separated subset of: {', '.join(HARVEST_CLASSES)}",
    )
    args = ap.parse_args()
    classes = [c.strip() for c in args.classes.split(",") if c.strip()] or None

    conn = await asyncpg.connect(
        host=os.environ.get("LEGBA_DATA_PG_HOST", "127.0.0.1"),
        port=int(os.environ.get("LEGBA_DATA_PG_PORT", "5432")),
        user=os.environ.get("LEGBA_DATA_PG_USER", "legba"),
        password=os.environ.get("LEGBA_DATA_PG_PASSWORD", "legba"),
        database=os.environ.get("LEGBA_DATA_PG_DB", "legba"),
    )
    try:
        results = await run_harvest(
            conn, classes=classes, limit=args.limit, apply=args.apply
        )
    finally:
        await conn.close()
    _print_report(results, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_amain()))

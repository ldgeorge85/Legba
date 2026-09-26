# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The WIDTH sweep — one hourly tick of external grading at width.

This is the leg ``standing_auditor.handle()`` takes when
``LEGBA_EXTERNAL_GRADING_WIDTH`` is on. It is a SIBLING MODULE rather than a
branch inside the handler for the reason the module-size gate exists: the width
path is a cohesive unit (refill → drain → search → grade → check → write) with
no inbound dependency on the handler, and the handler was already at 1,027 lines
against a 1,500-line entry threshold. Extracting the seam is the house answer;
raising a ceiling is not.

WHAT ONE TICK DOES, in order, and why each step is where it is:

  1. **Load the day's queue** off ``alert_trigger_watermarks`` and roll it over
     if the stored day is older. The rollover is the BUDGET RESET, and it
     happens on read because the auditor has no midnight tick — one that waited
     for a scheduled reset would carry yesterday's exhaustion into a day with a
     full budget.
  2. **Refill** from reads produced since the queue's own watermark. The claim
     population is a FIELD in the assembly payload, so this is a SQL read plus a
     pure enumeration — zero LLM calls, where the shipped sweep spent up to four
     core-plane calls a day to produce the same list less reliably.
  3. **Plan the drain** against three ceilings — the pack governor, the day's
     claim budget, the day's SERP budget — and degrade to a hash-gated SAMPLED
     mode when the day cannot cover the population. Never fabricate, never
     silently shrink n.
  4. **Grade** each claim: the UNCHECKABLE pre-filter answers before any query;
     an absence claim the search plane cannot verify emptiness for is UNCHECKED
     with the reason named; everything else gets one search, one grader call,
     and at most one reformulation.
  5. **Check the decisive span** through W-3's ``external_span_check.check_span``
     (soft import). No span check ⇒ no decisive verdict — a decisive verdict
     without it rests on the grader's word that a page says something.
  6. **Double-grade** 10% hash-gated plus 100% of CONTRADICTED with the fourth
     family, over the BYTE-IDENTICAL cached envelope.
  7. **Write** the ledger rows, then ONE critique per graded READ (not per
     claim), then an alert row for a confirmed high-severity contradiction.

ONE CRITIQUE PER READ, AND THE ARITHMETIC BEHIND IT (design 0.10 / F-4). At
width the per-claim shape would write ~470 ``kind='critique'`` rows a day
against a fleet that produces ~580 faithfulness critiques a day — an ~80% shift
in every fleet critique aggregate, and no consumer splits on the
``title LIKE 'External audit%'`` prefix. So the per-claim detail moved to the
ledger, where it has SQL, strata and append-only guarantees, and the critique
became the read-level roll-up (~70 rows/day) that keeps ``analyzed_output_id``
meaningful. Alerts stay per-claim: a contradiction is an operator event, not a
statistic.
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

from ...archive import archive_root
from ...provenance import external_grades as eg
from ...provenance.external_span_check import (
    as_datetime as _esc_as_datetime,
    evidence_window_bounds as _esc_evidence_window_bounds,
)
from ..agency.robots import RobotsCache
from ..composition_window import human_date
from ._external_audit_claims import (
    POPULATION_ASSEMBLY_SPAN,
    POPULATION_ASSESSMENT_SENTENCE,
    WidthClaim,
    build_query,
    claims_from_read,
    iter_unique,
    prefilter_counts,
)
from ._external_audit_fetch import FetchedPage, fetch_decisive_page
from ._external_audit_grader import (
    RATER_AUDIT,
    RATER_PRIMARY,
    RUBRIC_VERSION,
    EvidenceEnvelope,
    WidthGrade,
    absence_unverified_grade,
    check_decisive_span,
    grade_claim,
    should_double_grade,
    uncheckable_grade,
)
# THE SEARCH LEG — imported ONE WAY from its own leaf and re-exported, so
# ``width.run_search`` / ``width.SEARCH_TIMEOUT_SECONDS`` resolve exactly as
# they did before the paid rung moved the ladder into its own module.
from ._external_audit_search import (
    SEARCH_TIMEOUT_SECONDS,
    escalate_to_paid_rung,
    paid_rungs,
    run_search,
)
from ._external_audit_queue import (
    DEFAULT_MAX_CLAIMS_PER_DAY,
    DEFAULT_MAX_CLAIMS_PER_TICK,
    DEFAULT_MAX_QUEUE_DEPTH,
    DEFAULT_MAX_SERP_PER_DAY,
    DEFAULT_MAX_WRITE_ATTEMPTS,
    load_queue,
    plan_drain,
    record_spend,
    refill,
    requeue_failed_writes,
    save_queue,
    serp_provider_order,
    utc_day,
)
from ._external_audit_sampling import (
    UNCHECKABLE_VERDICT,
    UNCHECKED_OUT_OF_WINDOW,
    UNCHECKED_ROBOTS_DISALLOWED,
    VERDICT_CONTRADICTED,
    VERDICT_NOT_FOUND,
    VERDICT_SUPPORTED,
    VERDICT_UNCHECKED,
    WINDOW_BASES,
    WINDOW_BASIS_EVIDENCE,
    WINDOW_BASIS_HEADS,
    is_high_severity,
    width_pipeline_version,
)

logger = logging.getLogger(__name__)

#: G-3's grace, made operator-settable. Default ``0.0`` == today's exact rule
#: (byte-identical — see ``test_width_flag_off_is_the_shipped_sweep`` and the
#: grace-specific fixtures in ``test_external_grading_width.py``). The env
#: value is the base, retunable with no deploy; the ``window_grace_hours``
#: handler option (X-1 catalogued on ``standing_auditor`` —
#: ``handler_options_programs.EXTERNAL_GRADING_WIDTH_OPTIONS``) wins when
#: present, the same ``_coerce`` idiom ``_coverage_floor_scan.
#: CoverageFloorConfig.from_options`` uses.
#:
#: FLIPPING THIS ABOVE 0 IN PRODUCTION IS AN INSTRUMENT CHANGE, not a retune.
#: A verdict admitted only because a source published a few hours before
#: ``window.oldest`` was let in is a decision from a DIFFERENT instrument than
#: one that required the source inside the window outright — exactly the
#: distinction ``EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH``'s own banner draws
#: for the 2026-09-05/06 stamps. The operator's flip must add a NEW entry to
#: that lineage (``_external_audit_sampling.EXTERNAL_AUDIT_PIPELINE_VERSION_WIDTH``,
#: "2026-09-07/1"-style — bump the date, keep the ``/1``), so a row graded
#: under grace=0 and a row graded under grace>0 stay distinguishable by their
#: own stamp rather than by cross-referencing a config value nobody archived.
#:
#: THAT LINEAGE STEP IS NOW PERFORMED (2026-09-07). The stamp is no longer a
#: constant: ``_external_audit_sampling.width_pipeline_version`` returns
#: ``2026-09-06/1`` for heads/grace-0 and ``2026-09-07/1`` for any other window
#: configuration, so flipping either knob in ``.env`` moves the stamp with no
#: code edit and no chance of a widened measurement wearing the narrow label.
WINDOW_GRACE_HOURS_ENV = "LEGBA_EXTERNAL_AUDIT_WINDOW_GRACE_HOURS"

#: G-3's window BASIS. Default ``heads`` == today's exact rule, byte-identical:
#: the window is the stamp the READ wrote for itself
#: (``composition_window.evidence_window_span`` — oldest-to-newest ``produced_at``
#: over the heads it consumed), untouched.
#:
#: ``evidence`` replaces ``oldest`` ONLY — with the oldest ``signals.fetched_at``
#: reached by walking the read's own ``derived_from`` lineage down to signals
#: (:data:`_EVIDENCE_WINDOW_SQL`). ``newest`` never moves, and neither does the
#: upper bound ``max(newest, produced_at)``: a source published after the read
#: cannot have supported it, whatever the basis.
#:
#: WHY. Measured on the live ledger 2026-09-06: the world read stamped at 12:00Z
#: carried ``{oldest: 08:30Z, newest: 11:41Z, heads: 33}`` — a 3.19-hour window —
#: while the evidence those 33 heads rest on reaches back ~13 days. The heads'
#: ``produced_at`` spread is the ARRIVAL TIME OF THE SUMMARIES, not the age of
#: what they summarise, and G-3 was reading it as the latter: 22 of 24 decisive
#: proposals were demoted ``out_of_window`` against it, including every one of
#: the 12 whose span resolved VERBATIM on a page published 0-2.1 days before
#: ``oldest``. R3 §4.7 said the same thing from the other side — *"every declared
#: span is 1-2 days inside a read graded and consumed as a 14-day country read"*.
#: A window that excludes the read's own 14-day corpus is not the read's window.
#:
#: SETTING THIS TO ``evidence`` IN PRODUCTION IS AN INSTRUMENT CHANGE, exactly as
#: a nonzero grace is, and it moves the stamp the same way (see above).
WINDOW_BASIS_ENV = "LEGBA_EXTERNAL_AUDIT_WINDOW_BASIS"

#: How many ``derived_from`` hops the evidence walk may take before it stops.
#: The live lineage needs 4 and this leaves one spare: a country/escalation
#: composition reaches signals in 2 hops (composition -> desk heads -> signals),
#: the world read in 3 via a country composition and 4 via a region one, and the
#: Assessment in 5 because it is fenced to a single spine head (D-6). A cap
#: rather than an open walk because ``derived_from`` is a DAG nobody promised is
#: acyclic, and an audit tick must not be the thing that discovers it is not.
_EVIDENCE_WALK_MAX_DEPTH = 6

#: The evidence walk, as ONE query for the whole tick's reads.
#:
#: Seeded with every distinct ``graded_output_id`` in the drain and carrying the
#: seed as ``root``, so the per-read answer stays per-read while the 32 country
#: compositions that share desk heads share the traversal. Signals are LEAVES by
#: construction — the recursive term joins ``analyst_outputs`` for the parent, so
#: a node that is a signal has no children to expand — which is also why the walk
#: never reads a signal's own ``derived_from`` and never wanders into the
#: entity/fact graph.
#:
#: ``COALESCE(fetched_at, published_at)`` is DEFENSIVE ONLY: ``signals.fetched_at``
#: is ``NOT NULL DEFAULT now()`` (migration 0001), so the second arm is
#: unreachable today and COALESCE short-circuits before its cast can ever be
#: evaluated. It is written down because the brief names the fallback and because
#: a future nullable column must not silently drop rows out of ``min()``.
#:
#: COST, measured against the live tower at this depth cap: 297-406 ms (89k
#: buffer hits, all shared, no disk) for the WORST realistic tick — the 23:30
#: burst, 34 roots including a world read whose lineage reaches 4,212 signals —
#: and 15 ms for a typical hourly tick of 1-2 country compositions (~130 signals
#: each). One query per tick, on the connection the refill already holds; it is
#: not per read and not per claim, and the 32 country compositions that share
#: desk heads share the traversal.
_EVIDENCE_WINDOW_SQL = """
WITH RECURSIVE walk(root, node, depth) AS (
    SELECT ao.id, ao.id, 0
    FROM analyst_outputs ao
    WHERE ao.id = ANY($1::uuid[])
  UNION
    SELECT w.root, child.id, w.depth + 1
    FROM walk w
    JOIN analyst_outputs p ON p.id = w.node
    CROSS JOIN LATERAL unnest(p.derived_from) AS child(id)
    WHERE w.depth < $2
)
SELECT w.root AS root,
       min(COALESCE(
           s.fetched_at,
           (NULLIF(s.payload->>'published_at', ''))::timestamptz
       )) AS oldest,
       count(DISTINCT s.id) AS evidence_signals
FROM walk w
JOIN signals s ON s.id = w.node
GROUP BY w.root
"""


def _window_grace_hours(options: Mapping[str, Any]) -> float:
    """G-3's before-bound grace, in hours. Never raises.

    A value that will not coerce to ``float``, or one that is negative, keeps
    the predecessor (env default, or ``0.0``) rather than being applied — a
    mistyped knob must not silently widen what a decisive verdict admits.
    """
    value = 0.0
    env_raw = os.environ.get(WINDOW_GRACE_HOURS_ENV)
    if env_raw is not None:
        try:
            value = float(env_raw)
        except (TypeError, ValueError):
            logger.warning(
                "external_audit.bad_window_grace_env value=%r — keeping 0.0",
                env_raw,
            )
            value = 0.0
    if "window_grace_hours" in options:
        raw = options.get("window_grace_hours")
        try:
            value = float(raw)
        except (TypeError, ValueError):
            logger.warning(
                "external_audit.bad_window_grace_option value=%r — keeping %r",
                raw, value,
            )
    return value if value > 0 else 0.0


def _window_basis(options: Mapping[str, Any]) -> str:
    """G-3's window basis — ``"heads"`` (today) or ``"evidence"``. Never raises.

    Same ``_coerce`` idiom as :func:`_window_grace_hours`: the env
    (:data:`WINDOW_BASIS_ENV`) supplies the base, the ``window_basis`` handler
    option wins when present, and a value that is not in
    :data:`~._external_audit_sampling.WINDOW_BASES` keeps the PREDECESSOR rather
    than being applied. A typo must never widen what a decisive verdict admits,
    and it must never land a row stamped as a measurement nobody took.
    """
    value = WINDOW_BASIS_HEADS
    env_raw = os.environ.get(WINDOW_BASIS_ENV)
    if env_raw is not None:
        candidate = env_raw.strip().lower()
        if candidate in WINDOW_BASES:
            value = candidate
        else:
            logger.warning(
                "external_audit.bad_window_basis_env value=%r — keeping %r",
                env_raw, value,
            )
    if "window_basis" in options:
        raw = options.get("window_basis")
        candidate = str(raw or "").strip().lower()
        if candidate in WINDOW_BASES:
            value = candidate
        else:
            logger.warning(
                "external_audit.bad_window_basis_option value=%r — keeping %r",
                raw, value,
            )
    return value


@dataclass(frozen=True)
class WindowConfig:
    """The two knobs that decide G-3's admissible window, resolved once.

    Resolved once per tick and carried, rather than re-read at each hop, because
    the ledger row's STAMP is a function of exactly these two values
    (``_external_audit_sampling.width_pipeline_version``) — an instrument that
    re-read its own configuration mid-tick could stamp a row with a window it
    did not use.
    """

    basis: str = WINDOW_BASIS_HEADS
    grace_hours: float = 0.0

    @property
    def is_default(self) -> bool:
        """Today's instrument: heads basis, no grace. Byte-identical leg."""
        return self.basis == WINDOW_BASIS_HEADS and self.grace_hours <= 0

    @property
    def pipeline_version(self) -> str:
        """The WIDTH stamp this configuration's rows must carry."""
        return width_pipeline_version(
            window_basis=self.basis, grace_before_hours=self.grace_hours
        )


def resolve_window_config(options: Mapping[str, Any]) -> WindowConfig:
    """Both window knobs, env-then-option, in one object."""
    return WindowConfig(
        basis=_window_basis(options),
        grace_hours=_window_grace_hours(options),
    )


#: Every read that ASSERTS something about the world. The region tier is absent
#: on purpose: it retired to ``region_rollup.v1``, carries no claims of its own
#: (D-5: 256/256 blocks byte-identical), and grading an arithmetic statement is a
#: real cost for a guaranteed nothing.
WIDTH_READ_ANALYST_IDS: tuple[str, ...] = (
    "world_assessor",
    "country_composition",
    "escalation_composition",
    "world_assessment",
)

#: The refill's own fan-in cap. A SAFETY VALVE, not a window — ordered NEWEST
#: first so that if it were ever reached it would truncate the OLDEST reads,
#: which are the ones the watermark will pick up next tick anyway.
_REFILL_FETCH_CAP = 200

#: How far back a first-ever refill reaches when the queue has no watermark.
_REFILL_COLD_START_HOURS = 48

_REFILL_SQL = """
SELECT ao.id, ao.analyst_id, ao.target_id, ao.title, ao.body, ao.data,
       ao.produced_at
FROM analyst_outputs ao
WHERE ao.kind = 'finding'
  AND ao.analyst_id = ANY($1::text[])
  AND ao.superseded_by IS NULL
  AND ao.produced_at > $2
ORDER BY ao.produced_at DESC, ao.id DESC
LIMIT $3
"""



@dataclass
class WidthRunResult:
    """What one tick did — the heartbeat's width block, and the receipt's."""

    graded: list[WidthGrade] = field(default_factory=list)
    ledger_written: int = 0
    ledger_skipped: int = 0
    critiques: int = 0
    alerts: int = 0
    write_failures: int = 0
    #: PRIMARY-claim ledger writes that did NOT land this tick (an exception,
    #: or the pre-INSERT ``missing_graded_output_id`` refusal) — distinct from
    #: ``write_failures`` above, which is critique/alert write failures plus
    #: ``ledger_skipped`` (idempotent conflicts included). This one drives the
    #: requeue: see ``run_width_tick``.
    write_failed: int = 0
    #: Of ``write_failed``, how many were put back into the pending queue for
    #: the next tick's drain (the rest dead-lettered — see below).
    requeued: int = 0
    #: Of ``write_failed``, how many hit ``DEFAULT_MAX_WRITE_ATTEMPTS`` and
    #: were moved to the queue's ``dead_letter`` list instead of requeued.
    write_dead_lettered: int = 0
    reads_enumerated: int = 0
    refill: dict[str, int] = field(default_factory=dict)
    plan: dict[str, Any] = field(default_factory=dict)
    prefilter: dict[str, int] = field(default_factory=dict)
    searches: int = 0
    #: Decisive pages fetched this tick (one per distinct decisive URL).
    fetches: int = 0
    #: Decisive pages robots.txt refused — F-7's cost, made countable.
    robots_refused: int = 0
    #: Decisive pages the fetch leg could not read for any other reason.
    fetch_failed: int = 0
    reformulations: int = 0
    #: Claims escalated to the PAID SERP rung and answered by it. Every one is
    #: a charge, so it is counted next to the reformulations it displaces or
    #: (since 2026-09-21/3) carries.
    paid_escalations: int = 0
    #: Escalations the paid rung was ASKED for and did not serve — a refused
    #: spend cap, an undeclared rung, an unresolved key. Counted apart because
    #: "we could not afford to look" must never read as "we looked".
    paid_escalations_refused: int = 0
    double_graded: int = 0
    degraded: list[str] = field(default_factory=list)
    #: The grace ACTUALLY in force this tick (``_window_grace_hours``'s
    #: return). Carried on the receipt rather than the ledger — ``external_grades``
    #: has no such column and this build adds no migration for one.
    window_grace_hours: float = 0.0
    #: The window BASIS actually in force this tick (``_window_basis``). The
    #: default is today's; the ledger row carries the consequence in its own
    #: ``read_evidence_window.basis`` and in the stamp.
    window_basis: str = WINDOW_BASIS_HEADS
    #: The stamp every ledger row this tick carries — recorded so the receipt
    #: reports the stamp that was WRITTEN rather than re-deriving it from a
    #: configuration that could have been re-read in between.
    pipeline_version: str = ""
    #: The evidence walk's own counters, or ``None`` when the walk did not run
    #: (basis ``heads``, or a drain with no claims). ``None`` and a dict of
    #: zeros are different facts: the first says the question was not asked.
    evidence_window_stats: dict[str, int] | None = None

    @property
    def verdict_mix(self) -> dict[str, int]:
        mix: dict[str, int] = {}
        for grade in self.graded:
            if grade.rater_role != RATER_PRIMARY:
                continue
            mix[grade.verdict] = mix.get(grade.verdict, 0) + 1
        return mix

    @property
    def claims_checked(self) -> int:
        """Only completed external checks — the same exclusion the shipped
        heartbeat makes, extended to the new verdict. An auditor whose search
        plane is dead must not look busy, and neither must one whose whole day
        was pre-filtered."""
        mix = self.verdict_mix
        return sum(
            n for k, n in mix.items()
            if k in (VERDICT_SUPPORTED, VERDICT_CONTRADICTED, VERDICT_NOT_FOUND)
        )

    @property
    def tier_unknown(self) -> int:
        return sum(
            1 for g in self.graded
            if g.rater_role == RATER_PRIMARY and g.tier_unknown
        )

    @property
    def out_of_window_gap_buckets(self) -> dict[str, int]:
        return out_of_window_gap_buckets(self.graded)


#: Bucket labels, ordered nearest-gap first. A dict literal in
#: ``width_heartbeat_block`` would work too; naming the order here is what lets
#: a test assert the shape without hard-coding it twice.
GAP_BUCKET_24H = "<=24h"
GAP_BUCKET_72H = "<=72h"
GAP_BUCKET_7D = "<=7d"
GAP_BUCKET_OVER_7D = ">7d"
GAP_BUCKET_ORDER: tuple[str, ...] = (
    GAP_BUCKET_24H, GAP_BUCKET_72H, GAP_BUCKET_7D, GAP_BUCKET_OVER_7D,
)


def out_of_window_gap_buckets(graded: Sequence[WidthGrade]) -> dict[str, int]:
    """VISIBILITY for the grace knob, BEFORE it is ever turned on (design §W-9
    follow-up, 2026-09-06 live sweep).

    How far BEFORE ``window.oldest`` each resolved-but-demoted decisive source
    sits, bucketed. An operator reads this receipt field and can tell what a
    candidate ``window_grace_hours`` would admit without flipping anything —
    the live sweep that motivated this knob found 12 of 22 demoted proposals
    span-resolved verbatim with a same-event publish date 0.0-2.1 DAYS before
    the window opened (the ``<=72h`` bucket), against a long tail of 14-146
    day background pieces (``>7d``) that a grace knob correctly never reaches.

    Scoped to rows the grace knob could actually move:

    * PRIMARY rater only (the audit rater's row is the same claim twice);
    * ``unchecked_reason == 'out_of_window'`` specifically — a
      ``no_publish_date`` demotion has no gap to bucket, and is a different
      fact (G-3's other reason);
    * the decisive span RESOLVED verbatim (``decisive_span_sha256`` set) — G-2
      passed, so G-3 alone is what a grace value would change;
    * published STRICTLY BEFORE ``window.oldest``. A source published after
      ``window.newest`` is also ``out_of_window`` but grace never reaches it
      (§G-3's after bound is unchanged) — counting it here would describe a
      knob that cannot move it.

    Pure and read-only: it computes the RAW window (``grace_before_hours=0``)
    regardless of what grace the tick actually ran under, so the buckets
    always answer "what would N hours of grace admit", not "what did today's
    configured grace admit".
    """
    buckets = {label: 0 for label in GAP_BUCKET_ORDER}
    for grade in graded:
        if grade.rater_role != RATER_PRIMARY:
            continue
        if grade.unchecked_reason != UNCHECKED_OUT_OF_WINDOW:
            continue
        if not grade.decisive_span_sha256:
            continue
        published = _esc_as_datetime(grade.decisive_published_at)
        if published is None:
            continue
        bounds = _esc_evidence_window_bounds(dict(grade.claim.read_evidence_window))
        if not bounds.measured or bounds.start is None:
            continue
        if published >= bounds.start:
            continue
        gap_hours = (bounds.start - published).total_seconds() / 3600.0
        if gap_hours <= 24:
            buckets[GAP_BUCKET_24H] += 1
        elif gap_hours <= 72:
            buckets[GAP_BUCKET_72H] += 1
        elif gap_hours <= 168:
            buckets[GAP_BUCKET_7D] += 1
        else:
            buckets[GAP_BUCKET_OVER_7D] += 1
    return buckets


# ---------------------------------------------------------------------------
# THE EVIDENCE-BASED WINDOW (G-3, window_basis="evidence")
# ---------------------------------------------------------------------------
#
# THE STAMPING SITE. A read stamps its own ``data.data.evidence_window`` at
# composition time (``composition_window.evidence_window_span``) and
# ``_external_audit_claims._evidence_window`` copies it verbatim onto every
# claim; that copy is what ``check_span`` grades against and what lands in the
# ledger row's ``read_evidence_window``. Under ``window_basis="evidence"`` the
# drain rewrites the ``oldest`` of THAT COPY — never the read's own stamped
# payload, which belongs to the read and to the prompt directive it already
# printed. The audit is a reader here, not an author.
#
# WHY THE DRAIN AND NOT THE REFILL. A claim can be enumerated on one tick and
# graded several ticks later, so a refill-time rewrite would let a tick write
# rows under this tick's stamp carrying the PREVIOUS configuration's window.
# Rewriting what the drain actually selected makes the stamp true by
# construction: every row a tick writes was graded against the window that
# tick's stamp names.


def evidence_window(
    window: Mapping[str, Any],
    *,
    oldest: datetime | None,
    evidence_signals: int,
) -> dict[str, Any]:
    """One read's window, re-based onto its own evidence. Pure.

    ``oldest`` is the oldest ``signals.fetched_at`` under the read's
    ``derived_from`` lineage. ``newest`` is NEVER touched — the after bound is
    not a judgement call under any basis.

    THREE THINGS THIS REFUSES TO DO:

    * It never returns an UNMEASURED window from a measured one. A walk that
      resolved nothing (``oldest is None``) returns the heads window verbatim,
      because ``check_span`` DECLINES TO RUN G-3 on an unmeasured window — a
      failed lineage walk would otherwise admit every source ever published,
      which is the opposite of what a wider window is for.
    * It never NARROWS. ``oldest`` is ``min(evidence, heads)``: the read rests
      on its heads as well as on the signals under them, so the admissible
      window is the union of both bases. In live data the evidence is always
      older (a head cannot precede its own evidence) and the ``min`` never
      binds; if it ever does, that is a lineage defect, and demoting live
      verdicts on it would be the audit failing loud in the wrong direction.
    * It never leaves a derived field describing the OLD bound. ``span_hours``
      and ``oldest_human_date`` are recomputed, because a dict reading
      "oldest: 13 days ago, span_hours: 3.03" is a lie a ledger reader has no
      way to catch.

    The heads value is KEPT as ``oldest_heads`` and the basis is named, so the
    ledger row shows both windows and the ~13-day/3-hour gap between them is
    readable off a single row rather than reconstructed from a config nobody
    archived.
    """
    out = dict(window)
    heads_oldest_raw = out.get("oldest")
    if oldest is None:
        return out
    heads_oldest = _esc_as_datetime(heads_oldest_raw)
    resolved = min(oldest, heads_oldest) if heads_oldest is not None else oldest
    out["oldest"] = resolved.isoformat()
    out["basis"] = WINDOW_BASIS_EVIDENCE
    out["oldest_heads"] = heads_oldest_raw if heads_oldest_raw else None
    out["evidence_signals"] = int(evidence_signals)
    human = human_date(resolved)
    if human is not None:
        out["oldest_human_date"] = human
    newest = _esc_as_datetime(out.get("newest"))
    if newest is not None:
        out["span_hours"] = round(
            max(0.0, (newest - resolved).total_seconds() / 3600.0), 2
        )
    return out


async def evidence_oldest_by_read(
    conn: Any,
    read_ids: Sequence[str],
    *,
    max_depth: int = _EVIDENCE_WALK_MAX_DEPTH,
) -> dict[str, tuple[datetime | None, int]]:
    """``{read_id: (oldest_fetched_at, signals_reached)}`` for the tick's reads.

    ONE query for every read in the drain (:data:`_EVIDENCE_WINDOW_SQL`). Never
    raises: a walk that fails for any reason returns ``{}`` and every claim then
    keeps its heads window — a degraded lineage read must not be able to widen
    OR narrow what a decisive verdict admits, and it must not take the tick down.
    """
    ids = [str(i) for i in read_ids if i]
    if not ids:
        return {}
    try:
        rows = await conn.fetch(_EVIDENCE_WINDOW_SQL, ids, int(max_depth))
    except Exception:
        logger.warning(
            "external_audit.evidence_window_walk_failed reads=%d — "
            "every claim keeps its heads window this tick",
            len(ids), exc_info=True,
        )
        return {}
    out: dict[str, tuple[datetime | None, int]] = {}
    for row in rows:
        out[str(row["root"])] = (
            _esc_as_datetime(row["oldest"]),
            int(row["evidence_signals"] or 0),
        )
    return out


async def apply_evidence_windows(
    conn: Any,
    claims: Sequence[WidthClaim],
    *,
    max_depth: int = _EVIDENCE_WALK_MAX_DEPTH,
) -> tuple[list[WidthClaim], dict[str, int]]:
    """Re-base every drained claim's window onto its read's own evidence.

    Returns the rewritten claims and the tick's receipt counters. Claims are
    frozen, so this replaces rather than mutates — the queue's copy is
    untouched, and a requeued claim is re-based fresh next tick against
    whatever configuration is in force then.
    """
    read_ids = sorted({c.graded_output_id for c in claims if c.graded_output_id})
    resolved = await evidence_oldest_by_read(conn, read_ids, max_depth=max_depth)
    stats = {
        "reads": len(read_ids),
        "resolved": 0,
        "unresolved": 0,
        "signals": 0,
        "claims_rebased": 0,
    }
    for read_id in read_ids:
        oldest, signals = resolved.get(read_id, (None, 0))
        if oldest is None:
            stats["unresolved"] += 1
            continue
        stats["resolved"] += 1
        stats["signals"] += signals
    out: list[WidthClaim] = []
    for claim in claims:
        oldest, signals = resolved.get(claim.graded_output_id, (None, 0))
        window = evidence_window(
            claim.read_evidence_window, oldest=oldest, evidence_signals=signals,
        )
        if window == claim.read_evidence_window:
            out.append(claim)
            continue
        stats["claims_rebased"] += 1
        out.append(replace(claim, read_evidence_window=window))
    if stats["unresolved"]:
        logger.warning(
            "external_audit.evidence_window_unresolved reads=%d of %d — "
            "those claims keep their heads window",
            stats["unresolved"], stats["reads"],
        )
    return out, stats


# ---------------------------------------------------------------------------
# Refill
# ---------------------------------------------------------------------------


async def _fetch_new_reads(
    conn: Any, *, watermark: str, now: datetime
) -> list[Mapping[str, Any]]:
    """Reads published since the queue's watermark (48h on a cold start)."""
    cutoff: Any
    if watermark:
        try:
            cutoff = datetime.fromisoformat(watermark)
        except ValueError:
            cutoff = None
    else:
        cutoff = None
    if cutoff is None:
        from datetime import timedelta

        cutoff = now - timedelta(hours=_REFILL_COLD_START_HOURS)
    if cutoff.tzinfo is None:
        cutoff = cutoff.replace(tzinfo=timezone.utc)
    rows = await conn.fetch(
        _REFILL_SQL, list(WIDTH_READ_ANALYST_IDS), cutoff, _REFILL_FETCH_CAP
    )
    return [dict(r) for r in rows]


async def refill_from_reads(
    conn: Any, state: Mapping[str, Any], *, now: datetime, max_depth: int
) -> tuple[dict[str, Any], dict[str, int], int, list[WidthClaim]]:
    """Enumerate every new read's claims into the queue.

    Returns ``(state, refill_counts, reads_seen, enumerated)``. ``enumerated`` is
    the tick's whole claim harvest BEFORE dedup and queueing, which is what the
    pre-filter counts are computed over — the population as published, not the
    population as graded, because the difference between those two numbers is
    the thing §1.2 exists to make visible.
    """
    rows = await _fetch_new_reads(conn, watermark=str(state.get("refill_watermark") or ""), now=now)
    enumerated: list[WidthClaim] = []
    watermark = str(state.get("refill_watermark") or "")
    for row in rows:
        enumerated.extend(claims_from_read(row))
        produced = row.get("produced_at")
        iso = produced.isoformat() if hasattr(produced, "isoformat") else str(produced)
        watermark = max(watermark, iso)
    unique = list(iter_unique(enumerated))
    state, counts = refill(state, unique, watermark=watermark, max_depth=max_depth)
    return state, counts, len(rows), enumerated


# ---------------------------------------------------------------------------
# The fetch leg — one decisive page, cached for the tick
# ---------------------------------------------------------------------------


class PageCache:
    """The tick's fetched decisive pages, keyed by URL. Bounded by the drain.

    ONE FETCH PER URL PER TICK, and it exists for two reasons that are really
    the same reason. The double-grade path hands the audit rater the
    BYTE-IDENTICAL envelope the primary saw — R3's declared bias #5 measured
    search access moving agreement by 0.070, and an overlap computed over two
    different result sets measures the SEARCH, not the graders. A page re-fetched
    between the two raters breaks that invariant exactly as a second search
    would: the two raters would be gated against two different documents, and
    the overlap number would quietly stop meaning what it says. Caching also
    keeps the leg inside the egress budget the queue already reserves
    (``EGRESS_CALLS_PER_CLAIM``), which is the cheaper of the two arguments and
    the less important one.

    A FAILURE IS CACHED TOO. A URL that robots.txt refused must not be asked
    about again a moment later on the audit rater's behalf — the answer is the
    same and the second ask is a second unwelcome request to the same host.
    """

    def __init__(self) -> None:
        self._pages: dict[str, tuple[FetchedPage | None, str]] = {}
        self.robots_cache = RobotsCache()
        self.fetches = 0
        self.robots_refused = 0
        self.fetch_failed = 0

    async def get(
        self, binding: Any, url: str
    ) -> tuple[FetchedPage | None, str]:
        key = str(url or "")
        if key in self._pages:
            return self._pages[key]
        page, reason = await fetch_decisive_page(
            binding, key, robots_cache=self.robots_cache,
        )
        self.fetches += 1
        if reason == UNCHECKED_ROBOTS_DISALLOWED:
            self.robots_refused += 1
        elif reason:
            self.fetch_failed += 1
        self._pages[key] = (page, reason)
        return page, reason


async def check_one_decisive(
    grade: WidthGrade,
    envelope: EvidenceEnvelope,
    *,
    binding: Any,
    pages: PageCache,
    span_checker: Any | None,
    archive_dir: Any,
    grace_before_hours: float = 0.0,
) -> None:
    """Fetch the decisive page (if there is one) and run W-3's gates on it.

    The order matters and is the whole repair: the page is fetched FIRST, by the
    drain, through the auditor's own binding — because W-3's checker is pure and
    does not fetch, and for one day nothing else did either.

    A non-decisive grade never fetches. That is the budget's shape (~190
    fetches/day against ~470 claims) and also the honest one: a NOT_FOUND has no
    page to check.

    ``grace_before_hours`` is the tick's resolved ``window_grace_hours`` knob
    (:func:`_window_grace_hours`), forwarded to W-3's G-3 gate unchanged.
    Default ``0.0`` — today's rule.
    """
    if not grade.decisive:
        return
    page: FetchedPage | None = None
    reason = ""
    if grade.decisive_url:
        page, reason = await pages.get(binding, grade.decisive_url)
    check_decisive_span(
        grade, envelope,
        checker=span_checker, page=page, fetch_reason=reason,
        archive_root=archive_dir, grace_before_hours=grace_before_hours,
    )


# ---------------------------------------------------------------------------
# One claim, end to end
# ---------------------------------------------------------------------------


async def grade_one(
    claim: WidthClaim,
    *,
    binding: Any,
    grader: Any,
    audit_rater: Any,
    grader_family: str,
    grader_component_id: str,
    rater_family: str,
    rater_component_id: str,
    search_limit: int,
    provider_order: Sequence[str],
    sample_fraction: float,
    span_checker: Any | None,
    pages: PageCache,
    archive_dir: Any,
    result: WidthRunResult,
    grace_before_hours: float = 0.0,
) -> list[WidthGrade]:
    """Grade ONE claim and return every row it produced (1, or 2 if double-graded)."""
    if not claim.checkable:
        # Answered before a query was issued. ~9% of every read. This branch
        # only runs once `grader`/`binding` are both wired (the caller's
        # `grader is not None and binding is not None` guard), so the resolved
        # route is stamped even though no call was made — migration 0190
        # requires every row to name a grader, decided or not.
        return [uncheckable_grade(
            claim, sample_fraction=sample_fraction,
            grader_family=grader_family, grader_component_id=grader_component_id,
        )]

    query = build_query(claim)
    envelope, unchecked = await run_search(
        binding, query, limit=search_limit, provider_order=provider_order
    )
    result.searches += 1
    if envelope is None:
        return [WidthGrade(
            claim=claim, verdict=VERDICT_UNCHECKED, unchecked_reason=unchecked,
            grader_family=grader_family, grader_component_id=grader_component_id,
            sample_fraction=sample_fraction,
        )]

    # THE SECOND SEARCH, SPENT AT MOST ONCE PER CLAIM. It is either the paid
    # rung below or the NOT_FOUND reformulation further down, never both — see
    # ``escalate_to_paid_rung`` on why the budget arithmetic requires that.
    second_search_spent = False

    # THE ABSENCE GATE APPLIES TO AN EMPTY, NOT TO HITS (2026-09-21/1). The
    # pack's doctrine — an absence claim may only be SUPPORTED by an empty the
    # engine set proved it was alive for — was enforced as "refuse unless
    # supports_absence_claim", and that predicate is true ONLY for a verified
    # empty. A search that returned HITS therefore set it false and the claim
    # was refused before the grader ever saw the hits, on every rung: 24 h of
    # live rows, 104 of 104 absence claims UNCHECKED, including every answer
    # the paid rung was bought for. Hits are graded under the claim-shape rule
    # the grader prompt already carries (a result that merely fails to mention
    # the event does NOT support it); only an UNVERIFIED EMPTY is refused.
    if (
        claim.absence_shaped and not envelope.results
        and not envelope.supports_absence_claim
    ):
        # Rung 0 returned an empty it could not prove alive for — THIS is the
        # claim the paid rung was bought for, because a first-party index with
        # no partial-service channel CAN answer an empty that means something.
        # One query, then the doctrine applies to whatever came back.
        paid, paid_unchecked = await escalate_to_paid_rung(
            binding, query, limit=search_limit, provider_order=provider_order,
        )
        if paid is not None and paid_unchecked == "":
            second_search_spent = True
            result.searches += 1
            result.paid_escalations += 1
            envelope = paid
        elif paid_unchecked and paid_unchecked != "no paid rung declared":
            # The rung was declared and still did not answer — a refused spend
            # cap, an unresolved key, a transient. Countable, and NOT an empty.
            result.searches += 1
            result.paid_escalations_refused += 1
            logger.info(
                "external_audit.paid_rung_unavailable reason=%s", paid_unchecked,
            )
        if (
            claim.absence_shaped and not envelope.results
            and not envelope.supports_absence_claim
        ):
            # Still an unverified EMPTY — either no paid rung, or the paid
            # rung's own empty was unverified too. Same honest answer as before.
            # (A paid rung that returned HITS falls through to the grader.)
            # Same reasoning as the uncheckable branch above: stamp the
            # resolved route even though the grader itself was never called.
            return [absence_unverified_grade(
                claim, envelope, sample_fraction=sample_fraction,
                grader_family=grader_family,
                grader_component_id=grader_component_id,
            )]

    grade = await grade_claim(
        grader, claim, envelope,
        rater_role=RATER_PRIMARY,
        grader_family=grader_family,
        grader_component_id=grader_component_id,
        sample_fraction=sample_fraction,
    )

    # ONE reformulation, on NOT_FOUND only, and only when the grader itself said
    # a better query exists. Bounded at one because the second search is the
    # cheapest way to turn a search-health problem into a truth number and the
    # third is the most expensive way to turn a truth number into a search.
    if grade.verdict == VERDICT_NOT_FOUND and not grade.retry_query \
            and not second_search_spent:
        # NOTHING USABLE, and the grader named no better query — so a second
        # query against the SAME index would ask the same thing twice. A
        # different index is the only thing that can change the answer, and
        # this is the other claim the paid rung was bought for.
        paid, paid_unchecked = await escalate_to_paid_rung(
            binding, query, limit=search_limit, provider_order=provider_order,
        )
        if paid is not None and paid_unchecked == "":
            second_search_spent = True
            result.searches += 1
            result.paid_escalations += 1
            envelope = paid
            grade = await grade_claim(
                grader, claim, envelope,
                rater_role=RATER_PRIMARY,
                grader_family=grader_family,
                grader_component_id=grader_component_id,
                sample_fraction=sample_fraction,
            )
        elif paid_unchecked and paid_unchecked != "no paid rung declared":
            result.searches += 1
            result.paid_escalations_refused += 1
            logger.info(
                "external_audit.paid_rung_unavailable reason=%s", paid_unchecked,
            )

    if grade.verdict == VERDICT_NOT_FOUND and grade.retry_query \
            and not second_search_spent:
        # THE REFORMULATION RUNS ON THE PAID RUNG WHEN ONE IS DECLARED
        # (2026-09-21/3, PAID_REFORMULATION). A second query against the SAME
        # index asks the index that just answered nothing to answer again —
        # measured live on 2026-09-21: 321 searches for 164 claims and ZERO
        # calls to google.serper.dev, because every reformulatable NOT_FOUND
        # spent the second search on rung 0 and the paid rung was reachable
        # only from the no-reformulation branch above, which the grader's
        # retry_query short-circuits. A different index is the only thing
        # that can change the answer, and that is what the paid rung was
        # bought for. It is still the ONE second search — the same slot, the
        # same EGRESS_CALLS_PER_CLAIM reservation, the same spend brake —
        # and rung 0 keeps the reformulation only when no paid rung is
        # declared at all.
        reformulate_on_paid = bool(paid_rungs(provider_order))
        if reformulate_on_paid:
            retry, retry_unchecked = await escalate_to_paid_rung(
                binding, grade.retry_query, limit=search_limit,
                provider_order=provider_order,
            )
        else:
            retry, retry_unchecked = await run_search(
                binding, grade.retry_query, limit=search_limit,
                provider_order=provider_order,
            )
        result.searches += 1
        result.reformulations += 1
        if retry is not None and retry_unchecked == "":
            second_search_spent = True
            if reformulate_on_paid:
                result.paid_escalations += 1
            envelope = EvidenceEnvelope(
                query=retry.query, results=retry.results,
                search_status=retry.search_status, provider=retry.provider,
                reformulated_from=query,
            )
            grade = await grade_claim(
                grader, claim, envelope,
                rater_role=RATER_PRIMARY,
                grader_family=grader_family,
                grader_component_id=grader_component_id,
                sample_fraction=sample_fraction,
            )
        elif (
            reformulate_on_paid and retry_unchecked
            and retry_unchecked != "no paid rung declared"
        ):
            # The declared rung was ASKED and refused (spend cap, undeclared
            # pin, unresolved key) — counted, and rung 0's verdict stands.
            # The second-search slot stays unspent, the same rule the two
            # escalation sites above keep.
            result.paid_escalations_refused += 1
            logger.info(
                "external_audit.paid_rung_unavailable reason=%s",
                retry_unchecked,
            )

    await check_one_decisive(
        grade, envelope, binding=binding, pages=pages,
        span_checker=span_checker, archive_dir=archive_dir,
        grace_before_hours=grace_before_hours,
    )
    grades = [grade]

    if audit_rater is not None and should_double_grade(grade):
        # THE ENVELOPE IS BYTE-IDENTICAL. The rater issues no search of its own:
        # an overlap computed over two different result sets measures the SEARCH,
        # not the graders — R3's declared bias #5 measured that effect at 0.070.
        audit = await grade_claim(
            audit_rater, claim, envelope,
            rater_role=RATER_AUDIT,
            grader_family=rater_family,
            grader_component_id=rater_component_id,
            sample_fraction=sample_fraction,
        )
        await check_one_decisive(
            audit, envelope, binding=binding, pages=pages,
            span_checker=span_checker, archive_dir=archive_dir,
            grace_before_hours=grace_before_hours,
        )
        grades.append(audit)
        result.double_graded += 1
    return grades


# ---------------------------------------------------------------------------
# The writes
# ---------------------------------------------------------------------------


def build_read_rollup(
    output_id: str, grades: Sequence[WidthGrade]
) -> dict[str, Any]:
    """The per-READ roll-up carried on the one critique row (design 0.10).

    Every number here is paired with the n it was computed over, and the
    accuracy is ``None`` — never ``0.0`` — when nothing was decided. A read
    whose six claims were all NOT_FOUND has an unmeasured accuracy and a decided
    rate of zero, and those are two different sentences.
    """
    primary = [g for g in grades if g.rater_role == RATER_PRIMARY]
    record = eg.score(
        (g.verdict for g in primary), min_decided=1,
    )
    return {
        "graded_output_id": output_id,
        "n_claims": len(primary),
        "n_searched": record["n_searched"],
        "n_decided": record["n_decided"],
        "supported": record["supported"],
        "contradicted": record["contradicted"],
        "n_unchecked": record["n_unchecked"],
        "n_uncheckable": record["n_uncheckable"],
        "accuracy": record["accuracy"],
        "decided_rate": record["decided_rate"],
        "status": record["status"],
        "tier_unknown": sum(1 for g in primary if g.tier_unknown),
        "double_graded": sum(1 for g in grades if g.rater_role == RATER_AUDIT),
        "sample_fraction": min(
            [g.sample_fraction for g in primary] or [1.0]
        ),
        "verdicts": record["mix"],
        "populations": sorted({g.claim.population for g in primary}),
        "grader_family": next(
            (g.grader_family for g in primary if g.grader_family), ""
        ),
        # THE ROUTE, ON THE ROLL-UP (2026-09-20). The family label alone said
        # "mistral" for 2.7 days after OpenRouter removed the model the route
        # pointed at. A family is a class of model; the component id is the
        # stack ref an operator can actually go and repoint, and the model id
        # is what was on the wire. All three, or the row cannot answer "which
        # grader was this?".
        "grader_component_id": next(
            (g.grader_component_id for g in primary if g.grader_component_id),
            "",
        ),
        "grader_model_name": next(
            (g.grader_model_name for g in primary if g.grader_model_name), ""
        ),
        "grader_served_by": next(
            (g.grader_served_by for g in primary if g.grader_served_by), ""
        ),
        "rubric_version": RUBRIC_VERSION,
        "claims": [
            {
                "claim_key": g.claim.key,
                "verdict": g.verdict,
                "claim": g.claim.claim_text[:400],
                "span_role": g.claim.span_role,
                "block_ordinal": g.claim.block_ordinal,
                "origin_head_id": g.claim.origin_head_id,
                "decisive_url": g.decisive_url,
                "uncheckable_class": g.uncheckable_class,
                "unchecked_reason": g.unchecked_reason,
            }
            for g in primary
        ],
    }


def group_by_read(grades: Sequence[WidthGrade]) -> dict[str, list[WidthGrade]]:
    """Grades keyed by the READ they graded, insertion-ordered."""
    out: dict[str, list[WidthGrade]] = {}
    for grade in grades:
        out.setdefault(grade.claim.graded_output_id, []).append(grade)
    return out


def pageable(grade: WidthGrade, confirmed_by: Mapping[str, str]) -> bool:
    """The FIVE preconditions for a contradiction to write an alert row.

    All five, and the fifth is the one the rounds paid for: a CONTRADICTED
    verdict is one grader's reading of one span on one page, so two families
    must agree before it pages — and even then it is an OPERATOR EVENT, not a
    retraction. When no audit rater is wired the confirmation is absent and the
    contradiction lands in the ledger without paging, which is the honest
    outcome rather than a suppressed one.
    """
    return (
        grade.verdict == VERDICT_CONTRADICTED
        and grade.rater_role == RATER_PRIMARY
        # G-1: a decisive verdict needs a registered Tier-1/2 domain. An UNKNOWN
        # tier is published (F-3) but does not PAGE — visibility and paging are
        # different bars, and the page is the expensive one.
        and grade.decisive_source_tier in (1, 2)
        # G-2: the span resolved verbatim in a fetch of the cited URL.
        and bool(grade.decisive_span_sha256)
        and is_high_severity(grade.claim.claim_severity)
        and confirmed_by.get(grade.claim.key) == VERDICT_CONTRADICTED
    )


def confirmations(grades: Sequence[WidthGrade]) -> dict[str, str]:
    """``claim_key -> the audit rater's verdict``, for the paging precondition."""
    return {
        g.claim.key: g.verdict for g in grades if g.rater_role == RATER_AUDIT
    }


# ---------------------------------------------------------------------------
# The tick
# ---------------------------------------------------------------------------


async def run_width_tick(
    *,
    pool: Any,
    options: Mapping[str, Any],
    binding: Any,
    grader: Any,
    audit_rater: Any,
    grader_family: str,
    grader_component_id: str,
    rater_family: str,
    rater_component_id: str,
    trigger_class: str,
    pipeline_version: str,
    now: datetime | None = None,
    span_checker: Any | None = None,
) -> WidthRunResult:
    """One hourly drain. Never raises for a per-claim failure; the tick reports.

    The ONE thing that raises is a missing pool, and that is the caller's
    contract, not this function's: an audit that cannot read the tower must not
    emit a clean-looking zero.
    """
    moment = now or datetime.now(timezone.utc)
    day = utc_day(moment)
    result = WidthRunResult()

    max_per_tick = _pos(options.get("max_claims_per_tick"), DEFAULT_MAX_CLAIMS_PER_TICK)
    max_per_day = _pos(options.get("max_claims_per_day"), DEFAULT_MAX_CLAIMS_PER_DAY)
    max_serp = _pos(options.get("max_serp_per_day"), DEFAULT_MAX_SERP_PER_DAY)
    max_depth = _pos(options.get("max_queue_depth"), DEFAULT_MAX_QUEUE_DEPTH)
    search_limit = _pos(options.get("search_limit"), 5)
    providers = serp_provider_order(options.get("serp_provider_order"))
    window = resolve_window_config(options)
    grace_hours = window.grace_hours
    result.window_grace_hours = grace_hours
    result.window_basis = window.basis
    result.pipeline_version = str(pipeline_version)

    async with pool.acquire() as conn:
        state = await load_queue(conn, trigger_class=trigger_class, day=day)
        state, refill_counts, reads_seen, enumerated = await refill_from_reads(
            conn, state, now=moment, max_depth=max_depth,
        )
        claims, state, plan = plan_drain(
            state,
            max_claims_per_tick=max_per_tick,
            max_claims_per_day=max_per_day,
            max_serp_per_day=max_serp,
            # THE LIVE GOVERNOR, not a copy of it (2026-09-20). ``binding``
            # carries the ``web_access`` ActionPack the enforcer itself will
            # consult on the next call, so the clamp cannot drift from the
            # descriptor the way the old hard-coded 120 did. ``None`` binding
            # (no pack granted / agency plane down) falls back to the env
            # mirror and then to "uncapped" — and in that state nothing is
            # graded anyway, so the clamp is moot.
            binding=binding,
        )
        # The evidence walk rides the connection the refill already holds, and
        # runs over what the drain SELECTED (not what the refill enumerated) —
        # see the section banner above for why the drain is the honest site.
        if window.basis == WINDOW_BASIS_EVIDENCE and claims:
            claims, result.evidence_window_stats = await apply_evidence_windows(
                conn, claims,
            )
    result.refill = refill_counts
    result.reads_enumerated = reads_seen
    result.prefilter = prefilter_counts(enumerated)
    result.plan = plan
    fraction = float(plan.get("sample_fraction") or 1.0)

    if grader is None:
        result.degraded.append(
            "no external grader wired (method.llm.grader unset, unresolvable, "
            "or refused by the family fence)"
        )
    if binding is None:
        result.degraded.append(
            "no web_access binding wired (pack not granted / agency plane down)"
        )

    # The grading archive root. ONE resolution per tick, handed down. Reusing
    # ``LEGBA_ARCHIVE_ROOT`` is W-3's own layout decision, not a shortcut: the
    # grading corpus is "its OWN subtree under the archive root"
    # (``GRADING_ARCHIVE_SUBDIR``) with its OWN scheme (``cas:grading/sha256/``),
    # so F-8's separation is carried by the subtree and the prefix, and the
    # volume that already survives container recreation carries the bytes. An
    # unwritable root is DEGRADED, NOT WRONG — ``archive_grading_page`` returns
    # the content address with the bytes unsaved rather than failing a verdict.
    archive_dir = archive_root()
    pages = PageCache()

    if grader is not None and binding is not None:
        for claim in claims:
            result.graded.extend(await grade_one(
                claim,
                binding=binding, grader=grader, audit_rater=audit_rater,
                grader_family=grader_family,
                grader_component_id=grader_component_id,
                rater_family=rater_family,
                rater_component_id=rater_component_id,
                search_limit=search_limit,
                provider_order=providers,
                sample_fraction=fraction,
                span_checker=span_checker,
                pages=pages,
                archive_dir=archive_dir,
                result=result,
                grace_before_hours=grace_hours,
            ))
    elif claims:
        # BOTH PLANES OR NEITHER, the shipped rule kept: the claims stay QUEUED
        # rather than being drained into a pile of UNCHECKED rows that say
        # nothing the heartbeat's degraded_reason does not already say.
        state, _ = refill(state, claims, max_depth=max_depth)
        result.plan["selected"] = 0

    # The fetch leg's spend, reported whichever branch ran (both are zero when
    # the drain did not grade — a tick that fetched nothing must say 0, not
    # leave the field at a stale default).
    result.fetches = pages.fetches
    result.robots_refused = pages.robots_refused
    result.fetch_failed = pages.fetch_failed

    max_write_attempts = _pos(
        options.get("max_write_attempts"), DEFAULT_MAX_WRITE_ATTEMPTS
    )
    async with pool.acquire() as conn:
        rows = [g.as_dict() for g in result.graded]
        result.ledger_written, result.ledger_skipped, outcomes = (
            await eg.write_grades(
                conn, rows, pipeline_version=pipeline_version, graded_at=moment,
            )
        )

        # A plane that grades against the world must tolerate a failed WRITE
        # without losing the claim (defense in depth: the two writer bugs that
        # caused this in production are fixed, but the loss mode they created
        # — the claim vanishes from the queue AND is blocked from `refill`
        # rediscovering it for the rest of the UTC day — must never recur from
        # ANY future write failure). `graded_keys` is therefore built ONLY
        # from rows that actually landed; a PRIMARY claim whose row did not
        # land is requeued instead, never silently marked graded.
        primary_grades = [g for g in result.graded if g.rater_role == RATER_PRIMARY]
        graded_keys: list[str] = []
        failed_primary: list[tuple[WidthClaim, str]] = []
        for grade, outcome in zip(result.graded, outcomes):
            if grade.rater_role != RATER_PRIMARY:
                continue
            if outcome.landed:
                graded_keys.append(grade.claim.key)
            else:
                failed_primary.append((grade.claim, outcome.error_class or "unknown"))

        state = record_spend(
            state,
            claims=len(primary_grades),
            serp=result.searches,
            graded_keys=graded_keys,
        )
        state, result.requeued, result.write_dead_lettered = requeue_failed_writes(
            state, failed_primary, max_write_attempts=max_write_attempts,
        )
        result.write_failed = len(failed_primary)
        await save_queue(conn, state, trigger_class=trigger_class)
    return result


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, or its in-source default.

    Callers pass ``options.get("<literal>")`` rather than a key, deliberately:
    the X-1 catalog's reachability sweep proves a declared knob is real by
    grepping for the literal read, and a key threaded through a variable would
    be invisible to it.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def width_heartbeat_block(result: WidthRunResult) -> dict[str, Any]:
    """The heartbeat's width block — the tick's own receipt.

    ``sample_fraction`` is on the heartbeat as well as on every ledger row and
    every published number, because a reader looking at the auditor's liveness
    row has to be able to see that a day was sampled without joining to the
    ledger to find out.
    """
    plan = dict(result.plan)
    return {
        "width": True,
        # THE STAMP THE LEDGER ROWS ACTUALLY CARRY, merged over the shipped
        # heartbeat's own ``pipeline_version`` key (2026-09-07). That key is
        # filled by ``build_heartbeat_state`` from a zero-argument
        # ``pipeline_version()``, which cannot see this tick's window
        # configuration; leaving it would let the liveness row claim an
        # instrument the ledger rows beside it do not carry. Absent-or-empty
        # falls through to whatever the shipped key already said.
        **({"pipeline_version": result.pipeline_version}
           if result.pipeline_version else {}),
        "reads_enumerated": result.reads_enumerated,
        "refill": dict(result.refill),
        "queue_pending": plan.get("pending_after"),
        "claims_drained": plan.get("selected"),
        "tick_cap": plan.get("tick_cap"),
        "governor_clamped": plan.get("governor_clamped"),
        "day_complete": plan.get("day_complete"),
        "sample_fraction": plan.get("sample_fraction", 1.0),
        "sample_reason": plan.get("sample_reason"),
        "prefilter": dict(result.prefilter),
        "searches": result.searches,
        "fetches": result.fetches,
        "robots_refused": result.robots_refused,
        "fetch_failed": result.fetch_failed,
        "reformulations": result.reformulations,
        # The PAID rung's footprint on this tick: how many claims it decided,
        # and how many it was asked for and could not serve. Both are zero on
        # every tick that never escalates, which is every tick until the
        # operator PUTs a two-rung ``serp_provider_order``.
        "paid_escalations": result.paid_escalations,
        "paid_escalations_refused": result.paid_escalations_refused,
        "double_graded": result.double_graded,
        "tier_unknown": result.tier_unknown,
        "ledger_written": result.ledger_written,
        "ledger_skipped": result.ledger_skipped,
        "write_failed": result.write_failed,
        "requeued": result.requeued,
        "write_dead_lettered": result.write_dead_lettered,
        "rubric_version": RUBRIC_VERSION,
        # G-3 grace visibility (2026-09-06 follow-up). ``window_grace_hours``
        # is the grace ACTUALLY in force this tick (0.0 == today); the
        # buckets are computed at the RAW window regardless, so they always
        # answer "what would N hours admit" — see ``out_of_window_gap_buckets``.
        "window_grace_hours": result.window_grace_hours,
        "out_of_window_gap_buckets": result.out_of_window_gap_buckets,
        # G-3 BASIS (2026-09-07). The two fields a reader needs to know which
        # instrument wrote this tick's rows without joining to anything:
        # the basis in force, and — under ``evidence`` — how many reads the
        # lineage walk actually resolved. ``evidence_window`` is ``None``, not
        # a dict of zeros, when the walk did not run at all.
        "window_basis": result.window_basis,
        "evidence_window": (
            dict(result.evidence_window_stats)
            if result.evidence_window_stats is not None else None
        ),
    }


__all__ = [
    "GAP_BUCKET_24H",
    "GAP_BUCKET_72H",
    "GAP_BUCKET_7D",
    "GAP_BUCKET_OVER_7D",
    "GAP_BUCKET_ORDER",
    "POPULATION_ASSEMBLY_SPAN",
    "POPULATION_ASSESSMENT_SENTENCE",
    "UNCHECKABLE_VERDICT",
    "WIDTH_READ_ANALYST_IDS",
    "WINDOW_BASIS_ENV",
    "WINDOW_BASIS_EVIDENCE",
    "WINDOW_BASIS_HEADS",
    "WINDOW_GRACE_HOURS_ENV",
    "WidthRunResult",
    "WindowConfig",
    "apply_evidence_windows",
    "build_read_rollup",
    "confirmations",
    "evidence_oldest_by_read",
    "evidence_window",
    "grade_one",
    "group_by_read",
    "out_of_window_gap_buckets",
    "pageable",
    "refill_from_reads",
    "resolve_window_config",
    "run_search",
    "run_width_tick",
    "width_heartbeat_block",
]

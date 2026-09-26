# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""G1 — THE CORRECTNESS GRADER: Program 1's hand-run instrument, as a job.

WHAT THIS MEASURES, and what nothing else in the platform does. ~84% of this
fleet's LLM calls are the system grading itself: faithfulness judges scoring our
prose against our own citations, calibration trackers scoring our own bands. Not
one of them can see a tower that is internally immaculate and FACTUALLY WRONG.
On 2026-09-16 the instrument in this module, run by hand over Israel, found two
false claims the platform had published that day — a desk asserting a
"deadlock over list submissions" on a day the lists were filed, and a
composition asserting "no price spikes" while crude was over $100. The desk had
hedged the second; the composition un-hedged it. That is the laundering hazard
the spine names, caught by measurement rather than by argument.

THE SHAPE, per (target, as_of):

  1. RESOLVE THE REFERENCE — the ``unit_references`` row that is CURRENT for
     this target at ``as_of``: ``window_start <= as_of <= window_end + grace``,
     newest ``window_end`` wins. A reference is built for a window ending at its
     own T0 and every read it grades lands AFTER that instant, so strict
     containment made the nightly sweep ungradable by construction — the first
     live run proved it. ``LEGBA_GRADER_REFERENCE_GRACE_DAYS`` (default 7, env
     over descriptor option) sets how long a reference stays current, and the
     age it carried is written on the row (``reference_age_days``) and printed
     on the receipt. No current reference, no number: the target is recorded
     ``no_reference`` (none was ever built) or ``reference_stale`` (one was, and
     the builder stopped) and NOTHING is graded. A correctness number computed
     against the platform's own evidence is a self-consistency check wearing the
     wrong label.
  2. FREEZE THE HEADS — the eight dimension desks and the composition, at the
     SAME as-of predicate the hand run used (``created_at <= T0 AND
     (superseded_at IS NULL OR superseded_at > T0)``). A registered unit with no
     head is recorded MISSING and never padded.
  3. SEGMENT — the SHIPPED segmenter (``verify._segment_claims`` +
     ``._is_judgeable_claim``), then step 2's exclusion rules, every excluded
     span carrying its named reason (:mod:`_correctness_segment`). Among them
     ``prior_relative``: a desk read is written against ITS OWN previous read
     ("No material change since the prior read") and the reference has no prior
     read, so a grader handed such a span can only mislabel it. Excluded with
     the pattern that caught it, counted on the receipt, never graded.
  4. BUILD ONE PACKET — byte-identical across families, the reference reduced to
     a five-field whitelist, leak-scanned (:mod:`_correctness_packet`).
  5. CHECK THE GATE — refuse unless a passing ``grader_calibrations`` row covers
     this rubric sha and these model ids (:mod:`_correctness_calibration`).
  6. GRADE — F0 on every claim ($0 core plane); F2/F3 ONLY on claims F0 did not
     call ``silent``, and ONLY while the daily ceiling allows
     (:mod:`_correctness_grade`).
  7. ADJUDICATE AND WRITE — >=2 of 3 else ``split``; the unit row and its
     per-claim ledger (:mod:`_correctness_adjudicate`).

WHY ``deterministic`` AND NOT AN LLM KIND. The SAMPLE, the EXCLUSIONS, the
PACKET, the CEILING, the ADJUDICATION and the WRITES are code; the model is a
bounded instrument answering one question per call against a frozen rubric. That
is the standing auditor's own ruling, and it applies here more strongly: a
grader whose sampling a model could influence is not a grader.

ROTATION. ``max_targets_per_run`` takes a PREFIX of the population, and the
population arrives LEAST RECENTLY GRADED FIRST (never-graded targets ahead of
everything). A cap therefore CYCLES the roster instead of pinning it: until
2026-09-20 the population was ``ORDER BY target_id`` and the nightly sweep
graded AR, AU, BR, CA, CN every night and the other 27 countries never once.
The cap bounds what one night COSTS; it must never decide which countries are
ever measured.

FLAG. ``LEGBA_CORRECTNESS_GRADER_ENABLED``, default OFF. Off, the handler
returns a receipt saying so and writes NOTHING — no row, no LLM call, no spend.
The deploy is inert until an operator flips it.

CEILING. ``LEGBA_GRADER_DAILY_CEILING_USD``, default ``0`` — at which F2 and F3
are NEVER CALLED and every claim carries F0's label alone, flagged
``single_family``. The operator raises it; nothing here raises it for them.

SEAMS. The REFERENCE BUILDER is not in this module and is not built: track R2
owns it (``docs/SEAMS.md`` #55). This job grades against whatever
``unit_references`` rows exist and refuses loudly when there are none.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence
from uuid import UUID, uuid4

from ...correctness_reference_currency import (  # noqa: F401 — re-exported
    DEFAULT_REFERENCE_GRACE_DAYS,
    REFERENCE_GRACE_ENV,
    non_neg_int as _non_neg,
    reference_age_days,
    reference_grace_days,
)
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult
from . import _correctness_adjudicate as ADJ
from . import _correctness_grade as GRADE
from . import _correctness_packet as PACKET
from . import _correctness_segment as SEG
from . import fact_contention_pass as _pass
from ._correctness_calibration import CalibrationRefusal, resolve_calibration
from ._correctness_rubric import RUBRIC_NAME, RUBRIC_SHA256

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "correctness_grader"

#: This instrument's OWN population stamp — deliberately not the judge's and not
#: the external auditor's. A number produced under a changed pipeline is a
#: different number and must be distinguishable rather than pooled.
#:
#: BUMPED 2026-09-20 by the ``prior_relative`` exclusion class. It changes which
#: spans reach the rubric — and therefore every unit's DENOMINATOR — so shares
#: written before and after it are not the same measurement and must not pool.
#: The stamp travels on ``unit_correctness.families.pipeline_version``, which is
#: what makes the two populations separable in a query instead of only in a
#: commit message.
GRADER_PIPELINE_VERSION = "2026-09-20/1"

#: The kill switch. Default OFF; read HERE (not only in the deps builder) so an
#: operator can disarm the instrument with an env change and a recreate, without
#: unregistering the descriptor.
ENABLED_ENV = "LEGBA_CORRECTNESS_GRADER_ENABLED"

#: deps.extras keys, one per family. Distinct by construction: handing the
#: producer family's handle to a paid lane would silently make a three-family
#: number a one-family number.
F0_DEPS_EXTRA_KEY = "correctness_grader_f0"
F2_DEPS_EXTRA_KEY = "correctness_grader_f2"
F3_DEPS_EXTRA_KEY = "correctness_grader_f3"
DEPS_EXTRA_KEY_BY_FAMILY: dict[str, str] = {
    "F0": F0_DEPS_EXTRA_KEY,
    "F2": F2_DEPS_EXTRA_KEY,
    "F3": F3_DEPS_EXTRA_KEY,
}

#: The floor-1 read. Scored BESIDE the desks, never inside their pool: it is a
#: different grain, and pooling would let a composition that quotes eight desks
#: count those desks' claims twice.
COMPOSITION_ANALYST_ID = "country_composition"

#: P3 LANE A — the per-country interpretive channel, graded as a COMPOSITION-grain
#: head (``CR-`` atoms) beside the record it reads.
#:
#: WHY COMPOSITION GRAIN AND NOT A THIRD ONE. The grain answers one question:
#: "may these atoms be pooled with the desks'?" The answer here is the same
#: answer, for the same reason — this head's claims REST ON the eight desk reads
#: below it, so pooling would count those desks' claims twice. It is floor 1 of
#: the same tower, and ``GRAIN_PREFIX`` has exactly two entries because there
#: are exactly two answers to that question.
#:
#: WHY IT IS A SEPARATE CONSTANT ANYWAY. ``unit_correctness`` rows carry
#: ``analyst_id`` beside ``grain``, so the two heads stay separable in every
#: query that wants them apart (the route takes ``analyst_id`` as a filter) while
#: pooling correctly in every query that wants a floor-1 number. One grain, two
#: analyst ids, no third vocabulary.
#:
#: Spelled in FULL rather than as ``ASSESSMENT_ANALYST_ID``: that bare name is
#: already taken, one package over, by the WORLD channel
#: (``assessment_channel.ASSESSMENT_ANALYST_ID == "world_assessment"``), and two
#: modules disagreeing about which read a bare "the assessment" means is how a
#: grader ends up pointed at the wrong tier.
COUNTRY_ASSESSMENT_ANALYST_ID = "country_assessment"

#: The floor-1 heads, in the order they are appended. A tuple rather than two
#: calls so a third floor-1 read is one entry rather than a second copy of the
#: fetch-and-append below.
COMPOSITION_ANALYST_IDS: tuple[str, ...] = (
    COMPOSITION_ANALYST_ID,
    COUNTRY_ASSESSMENT_ANALYST_ID,
)

#: The eighth desk (present on 8 of 32 targets; the watch countries carry it).
PROLIFERATION = "proliferation_watch"

#: The head window, carried from R4 (``r4_common.WINDOW_DAYS``): a desk head
#: older than this is not the current read of the window the reference covers.
DEFAULT_HEAD_WINDOW_DAYS = 14

#: REFERENCE CURRENCY. How long past its ``window_end`` a reference stays the
#: current reference for this target. Env FIRST, then the descriptor option,
#: then this default — the ceiling's discipline (``_correctness_grade``), for
#: the same reason: the env is a deploy step with a human at the other end, and
#: it must be able to overrule a descriptor PUT rather than be overruled by one.
#: Zero is a legal setting and means STRICT containment (the pre-fix behaviour).
#: DEFINED in the leaf ``data/correctness_reference_currency`` and re-exported
#: above, so ``correctness_grader.REFERENCE_GRACE_ENV`` still resolves for this
#: module's tests and for ``reference_builder.grader_grace_days()``. The leaf
#: exists because the READ route needs the same rule and the registry image
#: carries no runtime deps — importing THIS module from a route reached
#: ``feedparser`` and 500'd the deployed endpoint.

#: The two reference gaps, kept APART. ``no_reference`` means track R2 has not
#: reached this target; ``reference_stale`` means it did and then STOPPED, and
#: the reference it left is older than the grace allows. Same zero graded units,
#: different operator action — so they are different statuses on the receipt.
STATUS_NO_REFERENCE = "no_reference"
STATUS_REFERENCE_STALE = "reference_stale"
#: Bounds on one sweep. Every one bounds a COST — LLM calls, DB reads, the size
#: of the graded sample — so an operator widens or narrows with a PUT.
DEFAULT_MAX_TARGETS_PER_RUN = 5
DEFAULT_MAX_CLAIMS_PER_UNIT = 40
DEFAULT_MAX_CLAIMS_PER_RUN = 400

#: THE TURN BUDGET (2026-09-21, H2). The sweep holds ONE actor turn for the
#: whole roster — 32 targets at ~2.5 min each is ~75 minutes, so reconcile's
#: deadline blew behind it (``actor_turn.budget_exceeded``) and the 2026-09-21
#: redeploy cut the sweep at 26/32 with NO receipt, because the receipt is the
#: turn's return value. ``LEGBA_GRADER_PASS_BUDGET_SECONDS`` bounds the wall
#: clock the TARGET LOOP may hold: the check fires BETWEEN targets (a target
#: in flight always finishes — it is the atomic write), the deferred tail is
#: named on the receipt, and least-recently-graded-first puts it at the head
#: of the next sweep's queue. ``<= 0`` disables the budget (pre-fix behavior).
PASS_BUDGET_ENV = "LEGBA_GRADER_PASS_BUDGET_SECONDS"
DEFAULT_PASS_BUDGET_SECONDS = 300.0

#: Receipt caps — a finding body is not a log.
_RECEIPT_UNIT_CAP = 24
_RECEIPT_WARNING_CAP = 20

# ---------------------------------------------------------------------------
# SQL — every statement PARAMETERISED and SELECT/INSERT only
# ---------------------------------------------------------------------------

#: The reference that is CURRENT at the as-of stamp.
#:
#: NOT ``window_end >= as_of``. A reference is built for a window that ENDS at
#: its own T0, and every read it grades lands AFTER that instant — the nightly
#: 01:20Z sweep reads a reference whose window closed hours or days earlier. The
#: first live forced run proved it: a reference for 09-02→09-16T19:30Z, read at
#: 22:32Z the same day, matched NOTHING and the sweep graded nothing. A
#: reference therefore stays CURRENT past its window_end until a newer one for
#: that target supersedes it or it ages out of the grace window.
#:
#: NEWEST ``window_end`` WINS, then newest build: among two references that both
#: cover the stamp, the one whose window reaches closest to it is the better
#: measurement, and ``built_at`` only breaks the tie (a reference reloaded from
#: a file keeps its own built_at, so the later-built one is better even if it
#: landed first).
_REFERENCE_SQL = """
SELECT id, target_id, window_start, window_end, built_at, builder, ref_json,
       span_verified_rate, thin_dimensions, sha256
  FROM unit_references
 WHERE target_id = $1
   AND window_start <= $2
   AND window_end + make_interval(days => $3) >= $2
 ORDER BY window_end DESC, built_at DESC, created_at DESC
 LIMIT 1
"""

#: The newest reference this target has AT ALL, ignoring the stamp. Read ONLY
#: when the currency lookup found nothing, and only to tell the two gaps apart:
#: a target that never had a reference (``no_reference``) and one whose
#: reference has aged out past the grace (``reference_stale``). Those are
#: different operator actions — the first waits on track R2, the second means
#: the builder has STOPPED — and a single status would hide the second inside
#: the first.
_LATEST_REFERENCE_SQL = """
SELECT id, window_start, window_end, built_at, sha256
  FROM unit_references
 WHERE target_id = $1
 ORDER BY window_end DESC, built_at DESC, created_at DESC
 LIMIT 1
"""

#: Which targets have a CURRENT reference at this stamp, LEAST RECENTLY GRADED
#: FIRST. The sweep's population when the descriptor names no explicit list — so
#: the job widens on its own as track R2 lands references, and costs nothing
#: while the table is empty. Same grace predicate as the lookup above,
#: deliberately: a population that admitted a target the lookup then refused
#: would grade nothing and say nothing.
#:
#: THE ORDER IS THE ROTATION, AND IT IS NOT COSMETIC. ``max_targets_per_run``
#: takes a PREFIX of this list. Under ``ORDER BY target_id`` — what this
#: statement said until 2026-09-20 — that prefix is the SAME prefix every night:
#: with the shipped cap of 5, AR/AU/BR/CA/CN were graded every single sweep and
#: the other 27 members of the roster were never graded at all, which is not a
#: sample of the roster but a measurement of five countries wearing the
#: roster's name. Raising the cap to cover the whole roster is a stopgap that
#: pays for the whole roster every night and breaks again the moment the roster
#: grows.
#:
#: LEFT JOIN, and NULLS FIRST, are both load-bearing. The join is a LEFT join so
#: a target that has NEVER been graded still appears — an INNER join would have
#: made "never graded" mean "never eligible", the exact starvation this fixes.
#: ``NULLS FIRST`` then puts those never-graded targets at the head of the
#: queue, ahead of the ones that at least have a number; Postgres's default for
#: ASC is NULLS LAST, so leaving it implicit would have starved them again, more
#: quietly. ``target_id`` breaks ties so the order is TOTAL and a sweep is
#: reproducible.
#:
#: The recency is ``MAX(created_at)`` over ALL of a target's ``unit_correctness``
#: rows, not per analyst and not filtered by rubric: the question the rotation
#: asks is "when did this instrument last spend a sweep on this country", and
#: every row in that table is an answer to it.
_TARGETS_WITH_REFERENCE_SQL = """
SELECT r.target_id
  FROM (
        SELECT DISTINCT target_id
          FROM unit_references
         WHERE window_start <= $1
           AND window_end + make_interval(days => $2) >= $1
       ) AS r
  LEFT JOIN (
        SELECT target_id, MAX(created_at) AS last_graded_at
          FROM unit_correctness
         GROUP BY target_id
       ) AS g ON g.target_id = r.target_id
 ORDER BY g.last_graded_at ASC NULLS FIRST, r.target_id
"""

#: The floor-0 desk heads, as of the stamp. DISTINCT ON keeps one head per
#: analyst — the newest produced, ties broken by id, exactly as the hand run's
#: ``build_r4_packets.unit_heads`` did it.
_DESK_HEADS_SQL = """
SELECT DISTINCT ON (analyst_id)
       analyst_id, id AS head_id, title, body, confidence, produced_at,
       created_at, superseded_at
  FROM analyst_outputs
 WHERE kind = 'finding'
   AND target_id = $1
   AND analyst_id = ANY($2::text[])
   AND created_at <= $3
   AND (superseded_at IS NULL OR superseded_at > $3)
   AND produced_at > $3 - make_interval(days => $4)
 ORDER BY analyst_id, produced_at DESC, id DESC
"""

#: The floor-1 composition head, WITH the structured payload whose ``regime``
#: decides whether the redactor runs at all.
_COMPOSITION_HEAD_SQL = """
SELECT analyst_id, id AS head_id, title, body, confidence, produced_at,
       created_at, superseded_at,
       data->'data'->'assembly' AS assembly
  FROM analyst_outputs
 WHERE kind = 'finding'
   AND analyst_id = $1
   AND target_id = $2
   AND created_at <= $3
   AND (superseded_at IS NULL OR superseded_at > $3)
 ORDER BY produced_at DESC, id DESC
 LIMIT 1
"""

#: Already graded under this exact (head, reference, rubric). Read FIRST so a
#: re-run does not BURN the paid calls the unique index would then discard.
_EXISTING_SQL = """
SELECT analyst_id, head_id
  FROM unit_correctness
 WHERE target_id = $1 AND reference_id = $2 AND rubric_sha = $3
"""

#: What this instrument has already spent today (UTC day).
_SPENT_TODAY_SQL = """
SELECT COALESCE(SUM(cost_usd), 0)::float8
  FROM unit_correctness
 WHERE created_at >= $1
"""

_INSERT_CORRECTNESS_SQL = """
INSERT INTO unit_correctness (
    id, analyst_id, target_id, head_id, as_of, reference_id, rubric_sha, grain,
    n_claims, n_contains, n_contradicts, n_silent, n_split, n_unparseable,
    n_single_family, correctness_share, coverage_share, families, cost_usd,
    reference_age_days
) VALUES (
    $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
    $18::jsonb, $19, $20
)
ON CONFLICT (analyst_id, target_id, head_id, reference_id, rubric_sha)
  DO NOTHING
RETURNING id
"""

_INSERT_CLAIM_SQL = """
INSERT INTO unit_correctness_claims (
    id, correctness_id, claim_id, grain, claim_text, label_by_family,
    adjudicated, n_families, single_family, spans
) VALUES ($1, $2, $3, $4, $5, $6::jsonb, $7, $8, $9, $10::jsonb)
ON CONFLICT (correctness_id, claim_id) DO NOTHING
"""


# ---------------------------------------------------------------------------
# Options + the flag
# ---------------------------------------------------------------------------


def grader_enabled(options: Mapping[str, Any] | None = None) -> bool:
    """``LEGBA_CORRECTNESS_GRADER_ENABLED`` — default OFF.

    Off, the handler writes NOTHING: no row, no LLM call, no spend, no
    watermark. The fleet is byte-identical to a tree without this train.
    """
    return str(os.getenv(ENABLED_ENV, "")).strip().lower() in (
        "1", "true", "yes", "on",
    )


def _pos(raw: Any, default: int) -> int:
    """A positive-int knob, or its in-source default.

    Callers pass ``options.get("<literal>")`` rather than a key name,
    deliberately: the X-1 catalog's reachability sweep proves a declared knob is
    real by grepping for the literal read in THIS module.
    """
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


#: ``_non_neg`` / ``reference_grace_days`` / ``reference_age_days`` are imported
#: from the leaf at the top of this file. The knob literal the X-1 reachability
#: sweep greps for (``options.get("reference_grace_days")``) travels with the
#: function, in the leaf.


def pass_budget_seconds() -> float:
    """The per-sweep wall-clock ceiling in seconds (``<= 0`` = unbounded).

    An ENV var, not a descriptor option — same discipline as the spend
    ceiling: how long one actor turn may run is a deploy-time decision, and a
    malformed value reads as the default, never as zero.
    """
    raw = os.getenv(PASS_BUDGET_ENV, "").strip()
    if not raw:
        return DEFAULT_PASS_BUDGET_SECONDS
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "correctness_grader.bad_env %s=%r; using %s",
            PASS_BUDGET_ENV, raw, DEFAULT_PASS_BUDGET_SECONDS,
        )
        return DEFAULT_PASS_BUDGET_SECONDS


def _str_list(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(v).strip() for v in raw if str(v).strip())


def _as_of(options: Mapping[str, Any]) -> datetime:
    """The ONE stamped instant this sweep measures at.

    An explicit ``as_of`` option exists for a REPLAY (an on-demand re-grade of a
    past stamp, which the idempotence key makes a no-op if it already ran).
    Absent, it is now.
    """
    raw = options.get("as_of")
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            logger.warning(
                "correctness_grader.as_of_unparseable raw=%r — using now()", raw
            )
            return datetime.now(timezone.utc)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# The freeze
# ---------------------------------------------------------------------------


def _desk_head(row: Mapping[str, Any]) -> dict[str, Any]:
    body = row.get("body") or ""
    return {
        "analyst_id": str(row["analyst_id"]),
        "grain": SEG.GRAIN_DESK,
        "output_id": str(row["head_id"]),
        "created_at": _stamp(row.get("created_at")),
        "produced_at": _stamp(row.get("produced_at")),
        "title": row.get("title"),
        "body": body,
        # The desks render plain markdown; there is nothing to redact and
        # nothing is. `grader_body` is the body, byte for byte.
        "grader_body": body,
        "redaction": None,
    }


def _composition_head(row: Mapping[str, Any]) -> dict[str, Any]:
    body = row.get("body") or ""
    assembly = row.get("assembly")
    if isinstance(assembly, (str, bytes)):
        try:
            assembly = json.loads(assembly)
        except (ValueError, TypeError):
            assembly = None
    regime = str((assembly or {}).get("regime") or "") if isinstance(
        assembly, Mapping
    ) else ""
    grader_body, cuts = (
        SEG.redact_render(body) if regime == "assembly" else (body, None)
    )
    return {
        "analyst_id": str(row["analyst_id"]),
        "grain": SEG.GRAIN_COMPOSITION,
        "output_id": str(row["head_id"]),
        "created_at": _stamp(row.get("created_at")),
        "produced_at": _stamp(row.get("produced_at")),
        "title": row.get("title"),
        "body": body,
        "grader_body": grader_body,
        "redaction": cuts,
    }


def _stamp(value: Any) -> str:
    """A timestamp as the id derivation sees it — ISO 8601, stable across reads.

    The atom id hashes this string, so it must be derived the same way every
    time or a re-run mints new ids for the same claims and the per-claim ledger
    stops joining to itself.
    """
    if isinstance(value, datetime):
        stamped = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return stamped.astimezone(timezone.utc).isoformat()
    return str(value or "")


async def freeze_heads(
    conn: Any, target_id: str, as_of: datetime, *, window_days: int,
    dimensions: Sequence[str],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Every floor-0 desk head and the floor-1 heads, as of the stamp.

    FLOOR 1 IS TWO READS SINCE P3 LANE A: the ``country_composition`` record and
    the ``country_assessment`` voice written from it. Both are graded at
    :data:`_correctness_segment.GRAIN_COMPOSITION` (``CR-`` atoms) and neither
    pools with the desks — see :data:`COUNTRY_ASSESSMENT_ANALYST_ID`. The redactor runs
    on whichever of them carries an ``assembly.v1`` payload under
    ``regime = assembly``; the Assessment carries ``assessment.v1`` instead, so
    it is graded on its own prose, byte for byte, which is what it is.

    A registered unit with NO head is returned in the ``missing`` list and never
    padded: an absent read is a fact about the product, and inferring a claim
    from its silence is exactly what the desk-grain arm refused to do.
    """
    desk_rows = await conn.fetch(
        _DESK_HEADS_SQL, target_id, list(dimensions), as_of, int(window_days)
    )
    heads = [_desk_head(r) for r in sorted(
        desk_rows, key=lambda r: str(r["analyst_id"])
    )]
    for floor1_id in COMPOSITION_ANALYST_IDS:
        comp_row = await conn.fetchrow(
            _COMPOSITION_HEAD_SQL, floor1_id, target_id, as_of
        )
        if comp_row is not None:
            heads.append(_composition_head(comp_row))
    present = {h["analyst_id"] for h in heads}
    # ``country_assessment`` is deliberately NOT in ``expected``. The channel
    # ships ``state: draft`` and writes no rows until an operator transitions it,
    # so listing it here would report it MISSING on every target of every run —
    # a fabricated gap in the product, which is precisely the inference the
    # desk-grain arm refuses to make in the other direction. A head that exists
    # is graded; a head that does not exist yet is not a hole.
    expected = set(dimensions) | {COMPOSITION_ANALYST_ID}
    return heads, sorted(expected - present)


# ---------------------------------------------------------------------------
# Grading one target
# ---------------------------------------------------------------------------


async def _grade_claims(
    items: Sequence[Mapping[str, Any]],
    llms: Mapping[str, Any],
    *,
    ceiling_usd: float,
    spent_usd: float,
    system_message: str,
    policy: Mapping[str, frozenset[str]],
) -> tuple[dict[str, dict[str, dict[str, Any]]], dict[str, Any]]:
    """Grade the packet's items. Returns ``({family: {claim_id: row}}, ledger)``.

    F0 grades EVERY claim. F2/F3 grade only the claims F0 did NOT call
    ``silent`` — the triage that makes a daily fleet-wide number affordable —
    and only while the ceiling allows. The guard refuses the call that WOULD
    breach, never the one after it.
    """
    by_family: dict[str, dict[str, dict[str, Any]]] = {}
    ledger: dict[str, Any] = {
        "ceiling_usd": ceiling_usd,
        "spent_before_run_usd": round(spent_usd, 6),
        "run_cost_usd": 0.0,
        "capped_out": False,
        "capped_before": None,
        "calls": {},
        "families_used": [],
        "skipped_families": {},
    }
    run_cost = 0.0

    f0_llm = llms.get("F0")
    if f0_llm is None:
        ledger["skipped_families"]["F0"] = "no core-plane handler wired"
        return by_family, ledger

    f0_rows: dict[str, dict[str, Any]] = {}
    for item in items:
        row = await GRADE.grade_one(
            item, "F0", f0_llm,
            system_message=system_message, policy=policy,
        )
        f0_rows[str(item["p1_id"])] = row
    by_family["F0"] = f0_rows
    ledger["families_used"].append("F0")
    ledger["calls"]["F0"] = len(f0_rows)

    # THE TRIAGE. A claim F0 called `silent` is one the reference does not bear
    # on at all; it sits in the coverage denominator and in neither share's
    # numerator, so a second and third opinion cannot move the correctness
    # number and would only spend money confirming an absence.
    triaged = [
        item for item in items
        if (f0_rows.get(str(item["p1_id"])) or {}).get("verdict") != "silent"
    ]
    ledger["triage"] = {
        "n_claims": len(items),
        "n_sent_to_paid_families": len(triaged),
        "rule": "F2/F3 grade only the claims F0 did not call `silent`",
    }

    for family in GRADE.PAID_FAMILIES:
        llm = llms.get(family)
        if llm is None:
            ledger["skipped_families"][family] = "no handler wired"
            continue
        if ceiling_usd <= 0.0:
            # AT A $0 CEILING THE PAID FAMILIES ARE NEVER CALLED. Not resolved,
            # not attempted, not estimated. This branch is the one an operator's
            # default runs through, and it must cost nothing at all.
            ledger["skipped_families"][family] = (
                f"{GRADE.CEILING_ENV} is $0 — paid families not called"
            )
            continue
        worst = float(GRADE.FAMILIES[family]["est_first_cost_usd"])
        rows: dict[str, dict[str, Any]] = {}
        for item in triaged:
            if GRADE.would_breach(spent_usd + run_cost, ceiling_usd, worst):
                ledger["capped_out"] = True
                ledger["capped_before"] = f"{family}:{item['p1_id']}"
                logger.warning(
                    "correctness_grader.ceiling_guard family=%s spent=%.6f "
                    "est_next=%.6f ceiling=%.2f — stopping BEFORE the call",
                    family, spent_usd + run_cost, worst, ceiling_usd,
                )
                break
            row = await GRADE.grade_one(
                item, family, llm,
                system_message=system_message, policy=policy,
            )
            cost = float(row.get("cost_usd") or 0.0)
            run_cost += cost
            worst = max(worst, cost)
            rows[str(item["p1_id"])] = row
        if rows:
            by_family[family] = rows
            ledger["families_used"].append(family)
            ledger["calls"][family] = len(rows)
        if ledger["capped_out"]:
            break

    ledger["run_cost_usd"] = round(run_cost, 6)
    return by_family, ledger


def adjudicate_claims(
    claims: Sequence[Mapping[str, Any]],
    by_family: Mapping[str, Mapping[str, Mapping[str, Any]]],
) -> list[dict[str, Any]]:
    """One adjudicated row per claim, carrying every family's label and span."""
    out: list[dict[str, Any]] = []
    for claim in claims:
        claim_id = str(claim["id"])
        label_by_family: dict[str, str] = {}
        spans: dict[str, Any] = {}
        for family, rows in by_family.items():
            row = rows.get(claim_id)
            if row is None:
                continue
            label_by_family[family] = str(row.get("verdict") or GRADE.UNPARSEABLE)
            spans[family] = {
                "core_claim": row.get("core_claim"),
                "decisive_span": row.get("decisive_span"),
                "reason": row.get("reason"),
                "span_unverified": bool(row.get("span_unverified")),
                "span_failure": row.get("span_failure"),
                "model": row.get("model"),
                "retried": bool(row.get("retried")),
            }
        label, n_agree, single = ADJ.adjudicate(label_by_family)
        out.append({
            "claim_id": claim_id,
            "grain": claim["grain"],
            "analyst_id": claim["analyst_id"],
            "claim_text": claim["text"],
            "label_by_family": label_by_family,
            "adjudicated": label,
            "n_agreeing": n_agree,
            "n_families": len(label_by_family),
            "single_family": single,
            "spans": spans,
        })
    return out


def _family_block(
    by_family: Mapping[str, Mapping[str, Mapping[str, Any]]],
    claim_ids: Sequence[str],
    ledger: Mapping[str, Any],
) -> tuple[dict[str, Any], float]:
    """The ``families`` jsonb for one unit, plus that unit's own cost."""
    block: dict[str, Any] = {
        "ceiling_usd": ledger.get("ceiling_usd"),
        "capped_out": bool(ledger.get("capped_out")),
        "skipped": dict(ledger.get("skipped_families") or {}),
        "pipeline_version": GRADER_PIPELINE_VERSION,
        "rubric": RUBRIC_NAME,
    }
    wanted = set(claim_ids)
    configured = GRADE.model_ids()
    unit_cost = 0.0
    for family in GRADE.FAMILY_ORDER:
        rows = by_family.get(family) or {}
        mine = [r for cid, r in rows.items() if cid in wanted]
        if not mine:
            continue
        labels: dict[str, int] = {}
        cost = 0.0
        unverified = 0
        for row in mine:
            label = str(row.get("verdict") or GRADE.UNPARSEABLE)
            labels[label] = labels.get(label, 0) + 1
            cost += float(row.get("cost_usd") or 0.0)
            unverified += int(bool(row.get("span_unverified")))
        unit_cost += cost
        block[family] = {
            # The model the handler REPORTED where it reported one, else the
            # id this run resolved. A reported id that differs from the
            # configured one is exactly what a reader needs to see.
            "model": next(
                (str(r["model"]) for r in mine if r.get("model")),
                configured[family],
            ),
            "model_configured": configured[family],
            "component": GRADE.FAMILIES[family]["component"],
            "n_calls": len(mine),
            "labels": dict(sorted(labels.items())),
            "span_unverified": unverified,
            "cost_usd": round(cost, 6),
        }
    block["single_family"] = sum(
        1 for f in GRADE.FAMILY_ORDER if f in block
    ) <= 1
    return block, round(unit_cost, 6)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def _dec(value: Any) -> Decimal | None:
    """A ``numeric`` column's argument. asyncpg wants a :class:`Decimal`, and
    ``Decimal(str(x))`` is the house conversion (``agency/governor.py``): going
    through the string keeps the value a reader would see rather than the
    nearest binary float."""
    if value is None:
        return None
    return Decimal(str(value))


async def _persist_unit(
    conn: Any,
    *,
    analyst_id: str,
    target_id: str,
    head_id: str,
    as_of: datetime,
    reference_id: Any,
    grain: str,
    unit_score: Mapping[str, Any],
    families: Mapping[str, Any],
    cost_usd: float,
    claim_rows: Sequence[Mapping[str, Any]],
    ref_age_days: float | None,
) -> UUID | None:
    """One unit row + its per-claim ledger, in ONE transaction.

    Returns the row id, or ``None`` when the idempotence index says this exact
    measurement already exists. The claims are written inside the same
    transaction as their parent so a share can never exist without the ledger
    that justifies it.
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            _INSERT_CORRECTNESS_SQL,
            uuid4(), analyst_id, target_id, UUID(str(head_id)), as_of,
            reference_id, RUBRIC_SHA256, grain,
            int(unit_score["n"]), int(unit_score["contains"]),
            int(unit_score["contradicts"]), int(unit_score["silent"]),
            int(unit_score["split"]), int(unit_score["unparseable"]),
            int(unit_score["n_single_family"]),
            _dec(unit_score["correctness_share"]),
            _dec(unit_score["coverage_share"]),
            json.dumps(dict(families)), _dec(round(float(cost_usd), 6)),
            _dec(ref_age_days),
        )
        if row is None:
            return None
        correctness_id = row["id"]
        for claim in claim_rows:
            await conn.execute(
                _INSERT_CLAIM_SQL,
                uuid4(), correctness_id, claim["claim_id"], claim["grain"],
                claim["claim_text"],
                json.dumps(dict(claim["label_by_family"])),
                claim["adjudicated"], int(claim["n_families"]),
                bool(claim["single_family"]),
                json.dumps(dict(claim["spans"])),
            )
        return correctness_id


# ---------------------------------------------------------------------------
# One target
# ---------------------------------------------------------------------------


async def _record_reference_gap(
    conn: Any,
    result: dict[str, Any],
    *,
    target_id: str,
    as_of: datetime,
    grace_days: int,
) -> dict[str, Any]:
    """No CURRENT reference — say WHICH of the two gaps this is, and how far.

    ``reference_stale`` is not a softer ``no_reference``: it means a reference
    for this target exists, the builder produced it, and it has aged past the
    grace. That is a builder that stopped, and it is hidden completely if the
    receipt only ever says "no reference". The age is reported in both cases so
    an operator reads the distance rather than inferring it.
    """
    latest = await conn.fetchrow(_LATEST_REFERENCE_SQL, target_id)
    age = (
        reference_age_days(as_of, latest["window_end"])
        if latest is not None else None
    )
    if latest is not None and age is not None and age > 0:
        result["status"] = STATUS_REFERENCE_STALE
        result["reference_age_days"] = age
        result["reference"] = {
            "id": str(latest["id"]),
            "sha256": latest["sha256"],
            "window_start": _stamp(latest["window_start"]),
            "window_end": _stamp(latest["window_end"]),
            "age_days": age,
            "grace_days": grace_days,
            "current": False,
        }
        result["warnings"].append(
            f"{target_id}: the newest reference closed "
            f"{_stamp(latest['window_end'])} — {age:.2f} day(s) before "
            f"{as_of.isoformat()}, past the {grace_days}-day grace "
            f"({REFERENCE_GRACE_ENV}). REFERENCE STALE: nothing graded, and "
            "the builder (track R2) has stopped for this target rather than "
            "never started."
        )
        return result
    result["status"] = STATUS_NO_REFERENCE
    result["reference_age_days"] = None
    result["warnings"].append(
        f"{target_id}: no unit_references row is current at "
        f"{as_of.isoformat()} (window_start <= as_of <= window_end + "
        f"{grace_days}d) — nothing graded. Track R2 builds these; "
        "scripts/load_unit_reference.py loads one by hand."
    )
    return result


async def grade_target(
    conn: Any,
    *,
    target_id: str,
    as_of: datetime,
    llms: Mapping[str, Any],
    dimensions: Sequence[str],
    dimension_names: frozenset[str],
    segmenter: Any,
    judge: Any,
    ceiling_usd: float,
    spent_usd: float,
    window_days: int,
    max_claims_per_unit: int,
    max_claims_per_run: int,
    grace_days: int,
) -> dict[str, Any]:
    """Grade one country target at one stamp. Never raises for a data gap.

    Every gap is a NAMED outcome on the returned dict — ``no_reference``,
    ``reference_stale``, ``no_heads``, ``no_claims``, ``leak_scan_failed``,
    ``already_graded`` — and the sweep reports it. An instrument that crashes on
    a missing input is invisible to everything except a log.
    """
    result: dict[str, Any] = {
        "target_id": target_id, "status": "ok", "units": [],
        "warnings": [], "cost_usd": 0.0, "n_claims": 0, "n_units_written": 0,
    }

    ref_row = await conn.fetchrow(
        _REFERENCE_SQL, target_id, as_of, grace_days
    )
    if ref_row is None:
        return await _record_reference_gap(
            conn, result, target_id=target_id, as_of=as_of,
            grace_days=grace_days,
        )
    ref_age = reference_age_days(as_of, ref_row["window_end"])
    reference_raw = ref_row["ref_json"]
    if isinstance(reference_raw, (str, bytes)):
        reference_raw = json.loads(reference_raw)
    result["reference"] = {
        "id": str(ref_row["id"]),
        "sha256": ref_row["sha256"],
        "builder": ref_row["builder"],
        "window_start": _stamp(ref_row["window_start"]),
        "window_end": _stamp(ref_row["window_end"]),
        "span_verified_rate": (
            float(ref_row["span_verified_rate"])
            if ref_row["span_verified_rate"] is not None else None
        ),
        "thin_dimensions": list(ref_row["thin_dimensions"] or []),
        # The age of the reference this number rests on. Published WITH the
        # share, never behind it: "88% correct against a reference that closed
        # six days ago" is a different statement from "88% correct".
        "age_days": ref_age,
        "grace_days": grace_days,
    }
    result["reference_age_days"] = ref_age

    heads, missing = await freeze_heads(
        conn, target_id, as_of, window_days=window_days, dimensions=dimensions
    )
    if missing:
        result["warnings"].append(
            f"{target_id}: no head as of the stamp for {missing} — recorded "
            "missing, never padded"
        )
    if not heads:
        result["status"] = "no_heads"
        return result

    already = {
        (str(r["analyst_id"]), str(r["head_id"]))
        for r in await conn.fetch(
            _EXISTING_SQL, target_id, ref_row["id"], RUBRIC_SHA256
        )
    }
    heads = [
        h for h in heads
        if (h["analyst_id"], str(h["output_id"])) not in already
    ]
    if not heads:
        result["status"] = "already_graded"
        return result

    segmented = SEG.segment_all(heads, segmenter, judge, dimension_names)
    result["exclusions"] = segmented["totals"]
    claims = _cap_claims(
        segmented["claims"], max_claims_per_unit, max_claims_per_run, result
    )
    if not claims:
        result["status"] = "no_claims"
        result["warnings"].append(
            f"{target_id}: the segmenter kept no claim on any head — every "
            "span was excluded with a named reason (see exclusions)."
        )
        return result
    result["n_claims"] = len(claims)

    country = target_id[-2:].upper()
    reference = PACKET.reduce_reference(reference_raw, country)
    if not reference["ref_developments"]:
        result["status"] = "reference_empty"
        result["warnings"].append(
            f"{target_id}: the reference carries no ref_developments — every "
            "claim would be ungradable. Refusing to grade."
        )
        return result
    packet = PACKET.build_packet(
        claims, reference, packet_kind=f"{SUB_HANDLER_NAME}_{country}"
    )
    leaks = PACKET.scan_packet(packet)
    if leaks:
        result["status"] = "leak_scan_failed"
        result["leaks"] = PACKET.leak_summary(leaks)
        result["warnings"].append(
            f"{target_id}: the packet leaks platform apparatus into a grader "
            f"lane ({len(leaks)} occurrence(s)) — refusing to call any family."
        )
        return result
    result["packet_sha256"] = PACKET.packet_sha256(packet)

    by_family, ledger = await _grade_claims(
        packet["items"], llms,
        ceiling_usd=ceiling_usd, spent_usd=spent_usd,
        system_message=GRADE.build_system_message(),
        policy=GRADE.span_policy(),
    )
    result["grading"] = ledger
    result["cost_usd"] = float(ledger.get("run_cost_usd") or 0.0)
    if not by_family:
        result["status"] = "no_grader"
        result["warnings"].append(
            f"{target_id}: no grader family was wired — nothing was graded. "
            "The receipt records the gap rather than a clean-looking zero."
        )
        return result

    adjudicated = adjudicate_claims(claims, by_family)
    by_claim_id = {row["claim_id"]: row for row in adjudicated}
    heads_by_analyst = {h["analyst_id"]: h for h in heads}

    for analyst_id in sorted({c["analyst_id"] for c in claims}):
        unit_claim_ids = [
            c["id"] for c in claims if c["analyst_id"] == analyst_id
        ]
        unit_rows = [by_claim_id[cid] for cid in unit_claim_ids]
        head = heads_by_analyst.get(analyst_id)
        if head is None:  # unreachable in practice; never guess a head id
            continue
        grain = str(head["grain"])
        unit_score = ADJ.score_unit(
            unit_rows, analyst_id=analyst_id, grain=grain
        )
        families, unit_cost = _family_block(by_family, unit_claim_ids, ledger)
        written = await _persist_unit(
            conn,
            analyst_id=analyst_id, target_id=target_id,
            head_id=str(head["output_id"]), as_of=as_of,
            reference_id=ref_row["id"], grain=grain,
            unit_score=unit_score, families=families, cost_usd=unit_cost,
            claim_rows=unit_rows, ref_age_days=ref_age,
        )
        result["units"].append({
            **{k: v for k, v in unit_score.items()
               if k not in ("per_family_labels",)},
            "head_id": str(head["output_id"]),
            "cost_usd": unit_cost,
            "written": written is not None,
            "correctness_id": str(written) if written else None,
        })
        if written is not None:
            result["n_units_written"] += 1
    return result


def _cap_claims(
    claims: Sequence[Mapping[str, Any]],
    max_per_unit: int,
    max_per_run: int,
    result: dict[str, Any],
) -> list[dict[str, Any]]:
    """Bound the sample, and SAY SO when it bites.

    A cap that silently truncated would publish a share over a denominator
    nobody could reconstruct. When a cap fires, the claims kept are the first by
    id (a stable, content-derived order, not the read's) and the receipt records
    how many were dropped.
    """
    kept: list[dict[str, Any]] = []
    per_unit: dict[str, int] = {}
    dropped = 0
    for claim in sorted(claims, key=lambda c: str(c["id"])):
        analyst_id = str(claim["analyst_id"])
        if per_unit.get(analyst_id, 0) >= max_per_unit:
            dropped += 1
            continue
        if len(kept) >= max_per_run:
            dropped += 1
            continue
        per_unit[analyst_id] = per_unit.get(analyst_id, 0) + 1
        kept.append(dict(claim))
    if dropped:
        result.setdefault("warnings", []).append(
            f"claim cap fired: {dropped} claim(s) dropped "
            f"(max_claims_per_unit={max_per_unit}, "
            f"max_claims_per_run={max_per_run}). Every published share rests "
            "on the claims that were kept, and its n says so."
        )
        result["claims_dropped_by_cap"] = dropped
    return kept


# ---------------------------------------------------------------------------
# The receipt
# ---------------------------------------------------------------------------


def build_receipt(
    *,
    as_of: datetime,
    enabled: bool,
    targets: Sequence[str],
    per_target: Sequence[Mapping[str, Any]],
    calibration: Mapping[str, Any] | None,
    refusal: Mapping[str, Any] | None,
    ceiling_usd: float,
    spent_before: float,
    warnings: Sequence[str],
    grace_days: int = DEFAULT_REFERENCE_GRACE_DAYS,
    deferred: Sequence[str] = (),
) -> FindingPayload:
    """The sweep's own row — a counting-not-repairing receipt.

    It NEVER gates and NEVER alerts. It carries the caveat on its own face so a
    reader who sees only this row sees the limits with the numbers.
    """
    written = sum(int(t.get("n_units_written") or 0) for t in per_target)
    cost = round(sum(float(t.get("cost_usd") or 0.0) for t in per_target), 6)
    if not enabled:
        headline = f"correctness grading DISABLED ({ENABLED_ENV} is off)"
    elif refusal:
        headline = f"REFUSED — {refusal.get('reason')}"
    elif written:
        headline = (
            f"graded {written} unit(s) across {len(per_target)} target(s) "
            f"for ${cost:.4f}"
        )
    else:
        statuses = sorted({str(t.get("status")) for t in per_target})
        headline = f"wrote no unit numbers ({', '.join(statuses) or 'no targets'})"

    body = [
        f"Correctness grader — {headline}.",
        f"  as_of={as_of.isoformat()} rubric={RUBRIC_NAME} "
        f"sha={RUBRIC_SHA256[:16]}… pipeline={GRADER_PIPELINE_VERSION}",
        f"  ceiling=${ceiling_usd:.2f}/day spent_before=${spent_before:.4f} "
        f"targets={list(targets)}",
        f"  reference grace={grace_days}d ({REFERENCE_GRACE_ENV}) — a "
        "reference stays current until window_end + grace, or until a newer "
        "one supersedes it",
    ]
    if refusal:
        body.append(f"  REFUSED: {refusal.get('detail')}")
    if calibration:
        body.append(
            f"  calibration: pooled={calibration.get('pooled')} "
            f"packet={str(calibration.get('packet_sha'))[:16]}… "
            f"n_atoms={calibration.get('n_atoms')}"
        )
    for target in per_target[:_RECEIPT_UNIT_CAP]:
        age = target.get("reference_age_days")
        body.append(
            f"  - [{target.get('status')}] {target.get('target_id')}: "
            f"{target.get('n_units_written')} unit(s), "
            f"{target.get('n_claims')} claim(s), "
            f"${float(target.get('cost_usd') or 0.0):.4f}"
            + ("" if age is None else f", reference_age={float(age):.2f}d")
        )
        for unit in (target.get("units") or [])[:_RECEIPT_UNIT_CAP]:
            share = unit.get("correctness_share")
            coverage = unit.get("coverage_share")
            body.append(
                f"      {unit.get('analyst_id')}: correctness="
                + ("—" if share is None else f"{float(share) * 100:.1f}%")
                + f" (n={unit.get('correctness_n')})  coverage="
                + ("—" if coverage is None else f"{float(coverage) * 100:.1f}%")
                + f" (n={unit.get('coverage_n')})"
            )
    prior_relative = sum(
        n for target in per_target
        for reason, n in (
            (target.get("exclusions") or {}).get("excluded_by_reason") or {}
        ).items()
        if str(reason).startswith(f"{SEG.PRIOR_RELATIVE_REASON}:")
    )
    if prior_relative:
        body.append(
            f"  prior-relative spans excluded: {prior_relative} — claims about "
            "this read's relation to its OWN previous read. The reference "
            "carries no prior read, so grading them measured nothing "
            "(see per_target[].exclusions.excluded_by_reason)."
        )
    if deferred:
        body.append(
            f"  deferred ({len(deferred)}): {list(deferred)} — the turn "
            f"budget or the daily ceiling stopped the sweep before they ran; "
            "the least-recently-graded-first rotation puts them at the head "
            "of the next sweep's queue"
        )
    for warning in list(warnings)[:_RECEIPT_WARNING_CAP]:
        body.append(f"  WARNING: {warning}")

    return FindingPayload(
        title=f"Correctness grader — {headline}"[:2048],
        body="\n".join(body)[:65536],
        confidence=1.0,
        evidence=[],
        tags=["deterministic", SUB_HANDLER_NAME, "correctness_measurement",
              "severity:low"],
        data={
            "sub_handler": SUB_HANDLER_NAME,
            "pipeline_version": GRADER_PIPELINE_VERSION,
            "enabled": bool(enabled),
            "as_of": as_of.isoformat(),
            "rubric": RUBRIC_NAME,
            "rubric_sha256": RUBRIC_SHA256,
            "ceiling_usd": ceiling_usd,
            "ceiling_env": GRADE.CEILING_ENV,
            "reference_grace_days": grace_days,
            "reference_grace_env": REFERENCE_GRACE_ENV,
            "spent_before_run_usd": round(spent_before, 6),
            "run_cost_usd": cost,
            "targets": list(targets),
            "deferred_targets": list(deferred),
            "n_units_written": written,
            "calibration": dict(calibration) if calibration else None,
            "refusal": dict(refusal) if refusal else None,
            "per_target": [dict(t) for t in per_target],
            "n_prior_relative_excluded": prior_relative,
            "warnings": list(warnings),
            "caveat": (
                "Correctness here is correctness AGAINST ONE REFERENCE, not "
                "against the world: a claim the reference does not mention is "
                "`silent`, which is why every share is published beside its "
                "coverage. A `silent` claim carries ONE family's label by "
                "design (the cost triage), and at a $0 ceiling every claim "
                "does — see n_single_family. The producer family (F0) grades "
                "reads its own model wrote; that is disclosed, and "
                "VERDICT_P1v4 measured it not to be the outlier."
            ),
        },
    )


def _zero_usage() -> dict[str, int]:
    return {"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: Any, options: Mapping[str, Any], deps: Any
) -> AnalystMethodResult:
    """One correctness sweep: every target with a live reference, at one stamp.

    REFUSES LOUD on a missing ``deps.pg_pool`` — an instrument that cannot read
    the substrate must not emit a clean-looking zero. Every OTHER missing plane
    DEGRADES to a named status on the receipt: no reference, no head, no grader
    family wired, a leak, a ceiling. A crash is invisible to everything except a
    log; a named status is not.

    ``inputs`` is the generic materialized slice the cadence actor hands every
    META analyst. It is IGNORED: the population is the reference table and the
    heads, both read directly.
    """
    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    if pool is None:
        raise RuntimeError(
            "correctness_grader requires a live deps.pg_pool — refusing to "
            "report a correctness measurement without reading the substrate"
        )

    as_of = _as_of(options)
    ceiling_usd = GRADE.daily_ceiling_usd()
    enabled = grader_enabled(options)
    explicit_targets = _str_list(options.get("grader_targets"))
    max_targets = _pos(
        options.get("max_targets_per_run"), DEFAULT_MAX_TARGETS_PER_RUN
    )
    max_claims_per_unit = _pos(
        options.get("max_claims_per_unit"), DEFAULT_MAX_CLAIMS_PER_UNIT
    )
    max_claims_per_run = _pos(
        options.get("max_claims_per_run"), DEFAULT_MAX_CLAIMS_PER_RUN
    )
    window_days = _pos(
        options.get("head_window_days"), DEFAULT_HEAD_WINDOW_DAYS
    )
    grace_days = reference_grace_days(options)

    if not enabled:
        return AnalystMethodResult(
            finding=build_receipt(
                as_of=as_of, enabled=False, targets=explicit_targets,
                per_target=(), calibration=None, refusal=None,
                ceiling_usd=ceiling_usd, spent_before=0.0,
                warnings=(f"{ENABLED_ENV} is off — nothing read, nothing "
                          "graded, nothing spent",),
                grace_days=grace_days,
            ),
            usage=_zero_usage(),
        )

    from ...provenance import verify
    from . import scorecard_banding as banding

    dimensions = tuple(banding.DIMENSIONS) + (PROLIFERATION,)
    dimension_names = frozenset(dimensions)

    extras = dict(getattr(deps, "extras", None) or {})
    llms = {
        family: extras.get(key)
        for family, key in DEPS_EXTRA_KEY_BY_FAMILY.items()
    }
    llms = {f: handler for f, handler in llms.items() if handler is not None}

    warnings: list[str] = []
    per_target: list[dict[str, Any]] = []
    calibration: dict[str, Any] | None = None
    refusal: dict[str, Any] | None = None
    spent_before = 0.0

    day_start = as_of.astimezone(timezone.utc).replace(
        hour=0, minute=0, second=0, microsecond=0
    )

    async with pool.acquire() as conn:
        # THE GATE, before anything is read and long before anything is spent.
        configured_models = GRADE.model_ids()
        required_models = [configured_models[f] for f in sorted(llms)] or [
            configured_models["F0"]
        ]
        try:
            calibration = await resolve_calibration(
                conn, RUBRIC_SHA256, required_models
            )
        except CalibrationRefusal as exc:
            refusal = {"reason": exc.reason, "detail": exc.detail}
            logger.warning(
                "correctness_grader.refused reason=%s detail=%s",
                exc.reason, exc.detail,
            )
            return AnalystMethodResult(
                finding=build_receipt(
                    as_of=as_of, enabled=True, targets=explicit_targets,
                    per_target=(), calibration=None, refusal=refusal,
                    ceiling_usd=ceiling_usd, spent_before=0.0,
                    warnings=(exc.detail,), grace_days=grace_days,
                ),
                usage=_zero_usage(),
            )
        calibration = {
            "id": str(calibration.get("id")),
            "pooled": (
                float(calibration["pooled"])
                if calibration.get("pooled") is not None else None
            ),
            "packet_sha": calibration.get("packet_sha"),
            "n_atoms": calibration.get("n_atoms"),
            "created_at": _stamp(calibration.get("created_at")),
        }

        spent_before = float(
            await conn.fetchval(_SPENT_TODAY_SQL, day_start) or 0.0
        )

        targets = explicit_targets
        rotated = not targets
        if not targets:
            targets = tuple(
                str(r["target_id"]) for r in
                await conn.fetch(
                    _TARGETS_WITH_REFERENCE_SQL, as_of, grace_days
                )
            )
            if not targets:
                warnings.append(
                    "no unit_references row is current at this stamp for any "
                    f"target (window_start <= as_of <= window_end + "
                    f"{grace_days}d, {REFERENCE_GRACE_ENV}) — the reference "
                    "builder (track R2, docs/SEAMS.md #55) has not run, or "
                    "every reference it built has aged out. Nothing graded, "
                    "nothing spent."
                )
        # The cap takes a PREFIX of the roster, and the roster arrives
        # least-recently-graded first (``_TARGETS_WITH_REFERENCE_SQL``), so a
        # cap of 5 over 32 countries cycles the whole roster rather than
        # grading the same five every night. Say so when it bites: an operator
        # reading "5 of 32" must be able to tell rotation from starvation.
        roster = tuple(targets)
        targets = roster[:max_targets]
        if len(roster) > len(targets):
            warnings.append(
                f"max_targets_per_run={max_targets} took the "
                f"{len(targets)} least-recently-graded of {len(roster)} "
                "target(s) with a current reference; the rest rotate in on "
                "the following sweeps (order: never-graded first, then "
                "oldest unit_correctness.created_at, then target_id)."
                if rotated else
                f"max_targets_per_run={max_targets} took the first "
                f"{len(targets)} of {len(roster)} EXPLICITLY NAMED target(s) "
                "(grader_targets). An explicit list is taken in the order it "
                "was written, not by recency — the rotation only orders the "
                "roster the job builds for itself."
            )

        # THE TURN BUDGET is checked BETWEEN targets, never inside one: a
        # target in flight finishes (its unit rows commit atomically) and the
        # NEXT target does not start past the deadline. One target at the
        # measured ~2.5 min keeps the worst-case turn at budget + one target.
        budget = _pass.PassBudget(seconds=pass_budget_seconds())
        run_cost = 0.0
        attempted = 0
        for target_id in targets:
            if budget.exhausted():
                warnings.append(
                    f"{PASS_BUDGET_ENV} spent after {attempted} target(s) — "
                    f"the sweep ends INSIDE its actor turn and defers "
                    f"{list(targets[attempted:])}; least-recently-graded "
                    "first puts them at the head of the next sweep's queue."
                )
                break
            outcome = await grade_target(
                conn,
                target_id=target_id, as_of=as_of, llms=llms,
                dimensions=dimensions, dimension_names=dimension_names,
                segmenter=verify._segment_claims,
                judge=verify._is_judgeable_claim,
                ceiling_usd=ceiling_usd,
                spent_usd=spent_before + run_cost,
                window_days=window_days,
                max_claims_per_unit=max_claims_per_unit,
                max_claims_per_run=max_claims_per_run,
                grace_days=grace_days,
            )
            per_target.append(outcome)
            attempted += 1
            warnings.extend(outcome.get("warnings") or [])
            run_cost += float(outcome.get("cost_usd") or 0.0)
            if (outcome.get("grading") or {}).get("capped_out"):
                warnings.append(
                    f"daily ceiling ${ceiling_usd:.2f} reached during "
                    f"{target_id} — the remaining targets were not graded by "
                    "the paid families this cycle."
                )
                break
        deferred = list(targets[attempted:])

    return AnalystMethodResult(
        finding=build_receipt(
            as_of=as_of, enabled=True, targets=targets,
            per_target=per_target, calibration=calibration, refusal=None,
            ceiling_usd=ceiling_usd, spent_before=spent_before,
            warnings=warnings, grace_days=grace_days, deferred=deferred,
        ),
        usage=_zero_usage(),
    )


__all__ = [
    "COMPOSITION_ANALYST_ID",
    "DEFAULT_PASS_BUDGET_SECONDS",
    "DEFAULT_REFERENCE_GRACE_DAYS",
    "DEPS_EXTRA_KEY_BY_FAMILY",
    "ENABLED_ENV",
    "F0_DEPS_EXTRA_KEY",
    "F2_DEPS_EXTRA_KEY",
    "F3_DEPS_EXTRA_KEY",
    "GRADER_PIPELINE_VERSION",
    "PASS_BUDGET_ENV",
    "PROLIFERATION",
    "REFERENCE_GRACE_ENV",
    "STATUS_NO_REFERENCE",
    "STATUS_REFERENCE_STALE",
    "SUB_HANDLER_NAME",
    "adjudicate_claims",
    "build_receipt",
    "freeze_heads",
    "grade_target",
    "grader_enabled",
    "handle",
    "pass_budget_seconds",
    "reference_age_days",
    "reference_grace_days",
]

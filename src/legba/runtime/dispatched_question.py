# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A DISPATCHED standing question is an ASSIGNMENT, not a suggestion.

THE DEFECT THIS MODULE FIXES (live, 2026-09-07 03:37Z)
------------------------------------------------------
``_coverage_floor_scan`` measured that ``country_watch_il``'s own evidence keeps
naming Palestine while none of its eight open frames does, and
``_research_dispatch`` converted that alert into a standing question with
``harvest_class='coverage_floor'``, ``target_id='country_watch_il'``,
``geo=['IL']``. The 2026-09-06 ranking tune then did its job exactly: the live
03:37Z ``corpus_researcher`` trace shows the IL gap rendered as **[Q1]**, first
in the block, carrying ``scope=country_watch_il geo=IL``.

The run answered **[Q2]** — a 35-day-old ``unit_payload`` question about a
wildfire near Sizewell B — made one ``search_corpus`` call, never reached the
web, stamped no ``addressed_question``, and left the IL row ``open_question``.

Ranking was never the whole bug. The backlog reached the planner as a *menu*:
eight questions under a header that says "PREFER" and "NOT a topic you are
forced to force an answer to". A dispatched question is not a preference — it is
an ACTION the platform already took: a trigger fired, an alert landed, a row was
written naming a desk that needs this answered. Offering it as option one of
eight and hoping is not dispatch.

THE RULE
--------
When the ranked backlog contains a question of a DISPATCHED class
(:data:`DISPATCHED_HARVEST_CLASSES` — ``coverage_floor`` and ``reference_gap``,
the two classes an alert fires on), that question becomes the run's ASSIGNMENT:

  * it is the ONLY question offered — the rest of the backlog is not rendered,
    so there is no menu left to pick the wrong item off;
  * the block states it as the job, prints the ``hypothesis_id`` the run must
    quote back, and states the web leg as mandatory rather than advisory;
  * the run CLAIMS it (:func:`claim_dispatched_question`) so the next tick takes
    the NEXT dispatched gap instead of re-assigning this one forever.

Self-selection is untouched and stays the fallback: with no dispatched question
open, the backlog block renders exactly as it did before this module existed
(byte-identical — the golden case), and a run with an empty backlog self-selects
as it always has.

WHY THE WEB LEG IS MANDATORY ON A DISPATCHED RUN — the null test
----------------------------------------------------------------
The "web-on-null" rung says: reach outside only when our own corpus genuinely
cannot answer. On a dispatched run that test is satisfied BY CONSTRUCTION, and
that is precisely why these two classes rank ahead of every harvested class
(see ``grounding._HARVEST_CLASS_PRIORITY``):

  * ``coverage_floor`` — the gap was MEASURED on our own collection (the target's
    slice keeps naming an entity that no open frame carries). Re-reading the
    collection that produced the measurement is the one instrument guaranteed
    not to close it.
  * ``reference_gap`` — an out-of-plane reference model named a development on
    this desk's bounded question and all three collection arms (url / entity /
    prose) found it NOWHERE in the desk's own slice. The corpus read already
    came back null; that null IS the dispatch.

So a dispatched run does not have to *discover* the null before it earns the
web: the null is the reason the question exists. The corpus read still happens
(what we do and do not hold is part of the answer), but it cannot be the end of
the run.

THE ID THAT CARRIES THE EVIDENCE — and the bug it also fixes
-------------------------------------------------------------
``web_evidence`` resolves the desk's ``geo`` from ``args.hypothesis_id`` ->
``hypotheses.target_id`` -> ``target_descriptors.scope.geo``
(``agency/research_tools._resolve_dispatch``). ``target_id=`` and ``geo=``
kwargs are **not** parameters of that tool and are silently ignored — yet the
descriptor's F-8 clause told the model to pass exactly those, and nothing ever
printed the ``hypothesis_id`` it actually needs. A model that obeyed the prompt
to the letter still landed geo-less evidence that reaches no desk. The
assignment block prints the id and names the one call shape that works.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping, Sequence

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWERED_STATUS",
    "CLAIMED_STATUS",
    "DISPATCHED_HARVEST_CLASSES",
    "DispatchedAssignment",
    "build_assignment_block",
    "claim_dispatched_question",
    "claim_reclaim_hours",
    "fill_question_sink",
    "resolve_backlog_block",
    "resolve_standing_assignment",
    "select_dispatched_assignment",
    "settle_claimed_questions",
]

#: The harvest classes an ALERT fired on — the two the platform DISPATCHED
#: rather than passively harvested. Kept as a tuple in the same order
#: ``grounding._HARVEST_CLASS_PRIORITY`` ranks them (coverage_floor 0,
#: reference_gap 1) so "first dispatched class in the ranked backlog" and
#: "highest-priority dispatched class" are the same statement. Extending the
#: priority table with a new dispatched class means adding it HERE too — the
#: ordinal decides where it ranks, this tuple decides whether it commands a run.
DISPATCHED_HARVEST_CLASSES: tuple[str, ...] = ("coverage_floor", "reference_gap")

#: The status a question wears while a research run holds it. NOT a new
#: lifecycle: it exists so the backlog drain stops re-offering a question that a
#: run is already answering, which is the whole of "the next tick takes the next
#: one". Every other reader of ``status='open_question'`` (claim_watch, the desk
#: STANDING OPEN QUESTIONS block, the dispatch dedup probe) sees the claim as
#: "not currently draining"; the dedup probe is widened to treat it as still
#: open so a claim can never mint a duplicate question (see
#: ``_research_dispatch._EXISTS_SQL``).
CLAIMED_STATUS = "in_progress"

#: The terminal status for a claimed question a research finding actually bears
#: on. Reached ONLY through evidence — a ``bearing_edges`` row
#: (``src_kind='finding'``, ``edge_kind='bears_on'``) written by the runtime
#: after that finding durably persisted — never by assuming the run worked.
ANSWERED_STATUS = "answered"

#: Hours after which an UNANSWERED claim is released back to the backlog. A run
#: that died between GROUND and PERSIST must never bury a dispatched gap
#: forever. 26h = two researcher ticks (cadence ``37 3,15 * * *``) plus margin,
#: so the claim survives the run that made it and one retry window, and no
#: longer. Env-overridable; a bad/blank/negative value falls back to the default.
_CLAIM_RECLAIM_HOURS: float = 26.0
CLAIM_RECLAIM_HOURS_ENV = "LEGBA_DISPATCH_CLAIM_RECLAIM_HOURS"


def claim_reclaim_hours() -> float:
    """Hours a claim may stand unanswered before the question is released.

    Reads :data:`CLAIM_RECLAIM_HOURS_ENV`; falls back to
    :data:`_CLAIM_RECLAIM_HOURS` when unset, blank, malformed or non-positive
    (a non-positive window would reclaim the claim the run just took). Never
    raises.
    """
    raw = os.getenv(CLAIM_RECLAIM_HOURS_ENV)
    if not raw or not raw.strip():
        return _CLAIM_RECLAIM_HOURS
    try:
        value = float(raw.strip())
    except (TypeError, ValueError):
        return _CLAIM_RECLAIM_HOURS
    return value if value > 0.0 else _CLAIM_RECLAIM_HOURS


@dataclass(frozen=True)
class DispatchedAssignment:
    """The one question a dispatched run answers, with everything the run needs.

    ``question`` is the :class:`~legba.runtime.grounding.GroundingOpenQuestion`
    itself (kept whole so the render, the sink and the claim all read the SAME
    row rather than three copies of it); ``tag`` is the positional tag the block
    prints and the model answers with — always ``"Q1"``, because a dispatched
    run is offered exactly one question.
    """

    question: Any
    tag: str = "Q1"

    @property
    def question_id(self) -> Any:
        return self.question.id

    @property
    def target_id(self) -> str | None:
        return self.question.target_id

    @property
    def geo(self) -> tuple[str, ...]:
        return tuple(self.question.geo)

    @property
    def harvest_class(self) -> str:
        return self.question.harvest_class


def select_dispatched_assignment(
    questions: Sequence[Any],
) -> DispatchedAssignment | None:
    """The ranked backlog's highest-priority DISPATCHED question, or ``None``.

    Scans the caller's already-ranked sequence in order and returns the FIRST
    entry whose ``harvest_class`` is in :data:`DISPATCHED_HARVEST_CLASSES` and
    which carries a ``target_id`` — the desk that asked. Order matters and is
    the caller's: ``resolve_open_questions`` has already applied the class
    ordinal and the age decay, so "first dispatched row in this list" is
    "highest-priority dispatched row", with no second ranking pass here.

    A dispatched-class row with NO ``target_id`` is deliberately skipped rather
    than assigned: without a target the tool can resolve no ``geo``, so the run
    would do the research and reach no desk — the exact failure F-8 names. Such
    a row still renders in the ordinary backlog block and can still be
    self-selected; it just cannot COMMAND a run.

    THE ONE BOUND, stated rather than hidden: this sees only what the resolver
    handed it, and that is hard-capped at ``_MAX_OPEN_QUESTIONS_GROUNDING`` (8).
    A dispatched question ranked 9th would not be assigned. That takes EIGHT
    questions with FRESH (inside the decay window) forward reach ahead of a
    class-0/1 row, which the 2026-09-06 tune exists to make unlikely — but it is
    a real ceiling, not an impossibility, and if it ever bites the fix is the
    fetch cap, not a second ranking pass here.

    Pure + DB-free. Returns ``None`` for an empty backlog, for a backlog of
    purely harvested questions, and for any input shape it cannot read — the
    caller then renders the ordinary priority-ordered block, byte-identical to
    the pre-dispatch path.
    """
    for q in questions or ():
        harvest_class = getattr(q, "harvest_class", None)
        if harvest_class not in DISPATCHED_HARVEST_CLASSES:
            continue
        target_id = getattr(q, "target_id", None)
        if not (isinstance(target_id, str) and target_id.strip()):
            continue
        return DispatchedAssignment(question=q)
    return None


_ASSIGNMENT_HEADER = (
    "DISPATCHED RESEARCH ASSIGNMENT — this run has ONE job and it was chosen "
    "for you, not offered to you. A live trigger fired on the gap below, an "
    "alert landed, and the platform dispatched this question to THIS run. It "
    "is not one option among a backlog: it is the question this run answers. "
    "No other standing question is offered, and self-selecting a topic instead "
    "is a failed run, not a choice. Set the top-level \"addressed_question\" "
    "field of your response to its tag:"
)


def _web_leg_clause(assignment: DispatchedAssignment) -> str:
    """The mandatory-web-leg paragraph, naming the ONE call shape that works.

    THE SHAPE IS THE PROTOCOL'S, NOT PYTHON'S (2026-09-08). This clause used to
    print ``web_evidence(query=…, hypothesis_id=…)`` — a call in a syntax the
    GATHER loop does not read. Handed that alongside a system prompt asking for
    ``{"tool": …, "args": {…}}``, the live runs split the difference and emitted
    ``{"action": "web_evidence", "query": …, "hypothesis_id": …}``, which the
    loop refused, so the model narrated the call into its FINDING instead. The
    loop now reads that shape too (``planner_action``), but a prompt that
    contradicts its own protocol is a defect whether or not the parser covers
    it: print the exact object the loop executes.
    """
    return (
        "WHY REACHING OUTSIDE IS MANDATORY HERE, NOT OPTIONAL. This question's "
        "class is one our own corpus cannot close by construction: the gap was "
        "measured ON our collection, so re-reading that same collection is the "
        "one instrument guaranteed not to answer it. The corpus read is still "
        "part of the job — search it, and say plainly what it does and does "
        "not hold — but it CANNOT be the end of the run. After it, you MUST "
        "emit this as a TOOL CALL — a turn whose whole content is this object, "
        "in the gathering protocol your instructions describe — and then read "
        "what comes back:\n"
        '    {"tool": "web_evidence", "args": {"query": "<your query>", '
        f'"hypothesis_id": "{assignment.question_id}"}}}}\n'
        "DESCRIBING this call is not making it. A turn that says you will "
        "fetch evidence, or that puts this object in your FINDING instead of "
        "in a tool-call turn, has fetched nothing — the run fails and the gap "
        "stays open. Pass hypothesis_id EXACTLY as printed. That id is the "
        "ONLY thing that carries what you find to the desk that asked: the "
        "tool reads that desk's geo off the question's own target descriptor, "
        "and it does NOT accept a target_id or geo argument — inventing one "
        "lands the evidence with no scope, where no desk will ever read it. A "
        "run that never calls web_evidence has reported \"we do not have it\" "
        "as though it meant \"nobody does\", which is a claim about the world "
        "you did not earn. A null result that survived the web is a complete, "
        "legitimate, and much stronger finding; a null result that never left "
        "the building is not."
    )


def build_assignment_block(
    assignment: DispatchedAssignment,
    *,
    now: datetime | None = None,
) -> str:
    """Render the ONE dispatched question as the run's assignment block.

    Replaces (never supplements) the STANDING OPEN QUESTIONS backlog block for
    this run: rendering the rest of the backlog underneath would restore the
    menu this exists to remove. The question line reuses
    ``GroundingOpenQuestion.render`` verbatim — same ``[Q1] (class; opened Nd
    ago; scope=<target> geo=<CODES>) thesis`` shape the backlog block prints, so
    the scope token the descriptor documents is unchanged — and adds the one
    thing that render never carried: the ``hypothesis_id`` the tool needs.
    """
    q = assignment.question
    lines = [
        _ASSIGNMENT_HEADER,
        f"- {q.render(tag=assignment.tag, now=now)}",
        f"  hypothesis_id={q.id}",
        "",
        _web_leg_clause(assignment),
        "",
    ]
    return "\n".join(lines)


def fill_question_sink(
    sink: Any,
    questions: Sequence[Any],
    *,
    assignment: DispatchedAssignment | None = None,
) -> None:
    """Fill the tag -> question map REFLECT resolves ``addressed_question``
    against, in the SAME order the block rendered (the tag is positional).

    A no-op when ``sink`` is not a dict (no run is listening). On a dispatched
    run ``questions`` is the single-element assignment sequence, so the only
    tag that resolves is ``Q1`` — a model that answers a question it was not
    given resolves to nothing, exactly as an invented tag always has.
    """
    if not isinstance(sink, dict):
        return
    dispatched_id = str(assignment.question_id) if assignment is not None else None
    for i, q in enumerate(questions, start=1):
        sink[f"Q{i}"] = {
            "id": str(q.id),
            "produced_at": (
                q.produced_at.isoformat()
                if isinstance(q.produced_at, datetime)
                else None
            ),
            "harvest_class": q.harvest_class,
            # R-B — the DISPATCH scope (RESEARCH_PROGRAM_SPEC §1.3/F-8). ``geo``
            # is the only key that puts a research signal in a desk's slice, and
            # a META researcher's run carries no target of its own, so the scope
            # has to travel with the question. Empty list/None for every
            # question nobody dispatched.
            "target_id": q.target_id,
            "geo": list(q.geo),
            # Whether THIS entry is the run's assignment (not merely a
            # dispatched-class row that happened to be offered) — the receipt a
            # trace reader needs to tell "answered the job" from "answered
            # something".
            "dispatched": dispatched_id is not None and str(q.id) == dispatched_id,
        }


# ---------------------------------------------------------------------------
# The claim — status movement, so the next tick takes the NEXT question
# ---------------------------------------------------------------------------

_CLAIM_SQL = """
    UPDATE hypotheses
       SET status = $2,
           run_id = COALESCE($3::uuid, run_id),
           updated_at = now()
     WHERE id = $1::uuid
       AND status = 'open_question'
"""

# A claimed question is ANSWERED once a FINDING durably bears on it. The
# evidence is the append-only ``bearing_edges`` pointer the runtime writes after
# the finding's row lands, so this can never mark a question answered on the
# strength of a run that crashed before persisting.
#
# ``src_kind = 'finding'`` is load-bearing: claim_watch writes 21,643
# ``signal -> hypothesis`` edges under the SAME ``bears_on`` kind, and an
# unqualified EXISTS would read a routine signal match as a research answer.
# ``src_as_of >= h.updated_at`` is the second half — a question researched
# once, left open and later re-dispatched must be answered by THIS claim's
# finding, not by the edge its predecessor left behind.
_ANSWERED_SQL = """
    UPDATE hypotheses h
       SET status = $1,
           resolved_at = now(),
           resolved_by = $2,
           updated_at = now()
     WHERE h.status = $3
       AND EXISTS (
           SELECT 1 FROM bearing_edges be
            WHERE be.dst_id = h.id
              AND be.dst_kind = 'hypothesis'
              AND be.src_kind = 'finding'
              AND be.edge_kind = 'bears_on'
              AND be.src_as_of >= h.updated_at
       )
"""

# THE CLAIM THAT STANDS — a question this analyst took and did not answer.
#
# THE DEFECT (live, 2026-09-08 03:37Z + 15:37Z). ``_OPEN_QUESTIONS_BACKLOG_SQL``
# reads ``status = 'open_question'`` and nothing else, so the moment a run
# CLAIMED the IL coverage gap the question became invisible to the ranker. The
# 09-07 15:37Z run claimed it, narrated its web call instead of making it, and
# published the narration; the next two ticks were then handed the ordinary
# unit_payload menu with no assignment at all, and the gap sat ``in_progress``,
# unanswered and un-offered, until the 26h reclaim. One run that fumbles a
# dispatch should cost the gap one tick, not a day.
#
# So a claim this analyst still holds is re-rendered as the SAME assignment on
# the next tick. Three conditions, all necessary:
#
#   * DISPATCHED CLASS — the row's ``open_question_origin`` marker says
#     ``harvest=coverage_floor|reference_gap``. A claim on any other class is
#     not an assignment and is left to the reclaim window.
#   * OWNED BY THIS ANALYST — the claiming run's ``analyst_traces`` row names
#     ``$1``. Ownership is read from the TRACE rather than re-stamped onto the
#     question, so a re-render never overwrites the record of who claimed it:
#     the original claimer stays the owner, and a run that died before writing
#     any trace at all resolves to nobody and falls to the reclaim window —
#     which is exactly what that window is for.
#   * NOT ALREADY ANSWERED — no ``finding`` bearing edge newer than the claim.
#     The settle above runs first and would have moved such a row to
#     ``answered``; this repeats the test so a settle that failed (it swallows
#     its own errors) cannot cause a re-assignment of an answered question.
#
# ``updated_at`` is deliberately NOT touched by the re-render (see
# :func:`resolve_standing_assignment`): the reclaim window keeps running from
# the ORIGINAL claim, so a question that cannot be answered is re-offered at
# most once or twice before it goes back on the general backlog. A re-render
# that refreshed the clock would hold a stuck gap forever.
#
# NARROWED BY THE RELEASE (2026-09-09). ``_RELEASE_FAILED_SQL`` now settles a
# claim whose owning run FAILED before this scan runs, so what reaches here is
# the case that release cannot see: a run that SUCCEEDED — published a finding,
# wrote its trace — and still did not answer the question it was assigned. That
# is the 09-07 15:37Z shape (a narration published as a finding, no
# ``addressed_question``, no bearing edge), and re-rendering is exactly right
# for it. A run that died before writing any trace still resolves to nobody and
# still falls to the reclaim window.
_STANDING_CLAIM_SQL = """
    SELECT h.id, h.thesis, h.target_id, h.produced_at, h.diagnostic_evidence
      FROM hypotheses h
     WHERE h.status = $1
       AND h.run_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM analyst_traces t
            WHERE t.run_id = h.run_id AND t.analyst_id = $2
       )
       AND NOT EXISTS (
           SELECT 1 FROM bearing_edges be
            WHERE be.dst_id = h.id
              AND be.dst_kind = 'hypothesis'
              AND be.src_kind = 'finding'
              AND be.edge_kind = 'bears_on'
              AND be.src_as_of >= h.updated_at
       )
     ORDER BY h.updated_at
     LIMIT $3
"""

# An unanswered claim older than the reclaim window goes back on the backlog.
# The gap is still real; only this run's hold on it expired.
_RECLAIM_SQL = """
    UPDATE hypotheses
       SET status = 'open_question',
           updated_at = now()
     WHERE status = $1
       AND updated_at < now() - make_interval(secs => $2)
"""

# A CLAIM HELD BY A RUN THAT DIED IS NOT A CLAIM (2026-09-09).
#
# THE RULE. A claim exists for exactly one reason: to stop a second run doing
# work a first run is already doing. A run recorded ``status <> 'success'`` in
# ``analyst_traces`` is not doing anything — it is over, it published nothing,
# and the DLQ already has it. Holding the question ``in_progress`` on its
# behalf buys nothing and costs the reclaim window: the IL coverage gap sat
# claimed across THREE consecutive hard fails (09-08 15:37Z, 09-09 03:37Z,
# 09-09 15:37Z), and the only thing that ever freed it was the 26h timer.
#
# So the claim is released the moment the platform can see the run died — the
# head of the next backlog resolve, which is also the first moment any consumer
# could take the question. Evidence-driven: the trace row IS the death
# certificate, and it now carries the prompt, the completions and the phases
# reached (``actor_payload._write_failure_trace``), so "why did it die" is
# answerable from the same row that releases the claim.
#
# WHY NOT AT THE INSTANT OF FAILURE. The failing run holds no pool — the
# runtime's except-handler is in the frozen ``dapr_actors`` — and nothing can
# consume a released question between the failure and the next resolve anyway.
# The timing difference is unobservable; the plumbing difference is not.
#
# ``updated_at`` IS refreshed, deliberately and against the re-render rule
# (which leaves it alone): a re-render keeps the ORIGINAL claim's clock running
# so a stuck gap ages back onto the general backlog, but this row is not stuck
# in a claim any more — it is BACK on the general backlog already, ranked with
# every other open question. The reclaim window has nothing left to protect
# here, and dating the release to the release is the honest stamp.
#
# NOT SCOPED TO ONE ANALYST, for the same reason the reclaim is not: a dead
# run's hold is void whoever owned it, and the analyst whose resolve happens to
# run first is not a meaningful owner of that fact.
_RELEASE_FAILED_SQL = """
    UPDATE hypotheses h
       SET status = 'open_question',
           updated_at = now()
     WHERE h.status = $1
       AND h.run_id IS NOT NULL
       AND EXISTS (
           SELECT 1 FROM analyst_traces t
            WHERE t.run_id = h.run_id
              AND t.status IS DISTINCT FROM 'success'
       )
       AND NOT EXISTS (
           SELECT 1 FROM bearing_edges be
            WHERE be.dst_id = h.id
              AND be.dst_kind = 'hypothesis'
              AND be.src_kind = 'finding'
              AND be.edge_kind = 'bears_on'
              AND be.src_as_of >= h.updated_at
       )
"""


def _rows_affected(tag: Any) -> int:
    """Rows touched, from asyncpg's ``"UPDATE <n>"`` command tag. 0 on any
    shape this cannot read — a counter must never fail a settle."""
    try:
        return int(str(tag).rsplit(" ", 1)[-1])
    except (TypeError, ValueError):
        return 0


async def claim_dispatched_question(
    pool: Any, *, question_id: Any, run_id: Any = None,
) -> bool:
    """Move ONE dispatched question ``open_question`` -> :data:`CLAIMED_STATUS`,
    stamping the run that took it. Returns ``True`` iff this call claimed it.

    Conditional on the row still being ``open_question``, so two runs racing the
    same question cannot both believe they own it — the loser updates 0 rows and
    is told so.

    DEGRADE-NOT-BREAK: never raises. A failed claim is logged and returns
    ``False``; the run still gets its assignment block and still does the
    research. The only cost of a lost claim is that the next tick may re-assign
    the same question — the pre-fix behaviour, not a regression.
    """
    try:
        async with pool.acquire() as conn:
            tag = await conn.execute(
                _CLAIM_SQL,
                str(question_id),
                CLAIMED_STATUS,
                str(run_id) if run_id else None,
            )
    except Exception as exc:
        logger.warning(
            "dispatched_question.claim.failed question=%s err=%s", question_id, exc,
        )
        return False
    claimed = _rows_affected(tag) > 0
    logger.info(
        "dispatched_question.claim question=%s run=%s claimed=%s",
        question_id, run_id, claimed,
    )
    return claimed


async def settle_claimed_questions(
    pool: Any, *, resolved_by: str,
) -> Mapping[str, int]:
    """Settle every outstanding claim: ANSWER the ones a finding bears on,
    RELEASE the ones whose owning run died, RECLAIM the ones whose window
    expired. Returns ``{"answered": n, "released": n, "reclaimed": n}``.

    Runs once at the head of each backlog resolve — the same place, the same
    pool, the same tick that hands out the next assignment — so a claim is
    always settled before the ranking that would re-offer it is computed.
    Answering LAGS the finding by one tick by design: the bearing edge is
    written after the output row persists, which is after this run's GROUND
    phase is long over, and waiting for durable evidence is the point.

    THE ORDER IS THE PRECEDENCE, and it is: answered, then released, then
    reclaimed. A question a finding bears on is ANSWERED even if the run that
    produced it later died on a post-persist leg — durable evidence outranks
    the run's exit code, which is why ``_RELEASE_FAILED_SQL`` repeats the
    bearing-edge test rather than trusting the ordering alone. Release before
    reclaim so a dead run's hold is counted as what it is (a release) rather
    than silently absorbed by the 26h timer 26 hours later.

    DEGRADE-NOT-BREAK: never raises. On failure the claims simply stand and are
    settled on a later tick.
    """
    counts = {"answered": 0, "released": 0, "reclaimed": 0}
    try:
        async with pool.acquire() as conn:
            counts["answered"] = _rows_affected(
                await conn.execute(
                    _ANSWERED_SQL, ANSWERED_STATUS, resolved_by, CLAIMED_STATUS,
                )
            )
            counts["released"] = _rows_affected(
                await conn.execute(_RELEASE_FAILED_SQL, CLAIMED_STATUS)
            )
            counts["reclaimed"] = _rows_affected(
                await conn.execute(
                    _RECLAIM_SQL, CLAIMED_STATUS, claim_reclaim_hours() * 3600.0,
                )
            )
    except Exception as exc:
        logger.warning("dispatched_question.settle.failed err=%s", exc)
        return counts
    if any(counts.values()):
        logger.info(
            "dispatched_question.settle answered=%d released=%d reclaimed=%d",
            counts["answered"], counts["released"], counts["reclaimed"],
        )
    return counts


#: How many standing claims to read before giving up on finding a dispatched
#: one. A researcher holds at most one live claim per tick, so this only ever
#: bounds a pathological backlog; it exists so a malformed marker cannot make
#: the scan unbounded.
_STANDING_CLAIM_SCAN_CAP = 8


async def resolve_standing_assignment(
    pool: Any, *, analyst_id: str,
) -> DispatchedAssignment | None:
    """The assignment a previous run of THIS analyst claimed and did not
    answer, ready to be re-rendered, or ``None``.

    Reconstructs the same :class:`~legba.runtime.grounding.GroundingOpenQuestion`
    the ranker would have built for the row — same ``harvest_class_of`` and
    ``dispatch_scope_of`` marker reads, so the block renders byte-identically
    to the assignment the claiming run was shown (minus the age, which has
    genuinely moved on). ``live_reach``/``desk_salience`` are not re-derived:
    they are RANKING inputs, and a re-render is not a ranking — this row was
    already chosen. They render as 0 / omitted, which is what they render as
    for a question with no forward reach today.

    DEGRADE-NOT-BREAK: never raises. On any read failure the caller simply
    ranks the ordinary backlog, which is the pre-fix behaviour.
    """
    from .grounding import GroundingOpenQuestion, dispatch_scope_of, harvest_class_of

    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(
                _STANDING_CLAIM_SQL, CLAIMED_STATUS, analyst_id,
                _STANDING_CLAIM_SCAN_CAP,
            )
    except Exception as exc:
        logger.warning("dispatched_question.standing_claim.failed err=%s", exc)
        return None
    for r in rows:
        harvest_class = harvest_class_of(r["diagnostic_evidence"])
        if harvest_class not in DISPATCHED_HARVEST_CLASSES:
            continue
        scope_target, scope_geo = dispatch_scope_of(r["diagnostic_evidence"])
        target_id = r["target_id"] or scope_target
        if not (isinstance(target_id, str) and target_id.strip()):
            continue
        logger.info(
            "dispatched_question.reassign question=%s analyst=%s class=%s",
            r["id"], analyst_id, harvest_class,
        )
        return DispatchedAssignment(
            question=GroundingOpenQuestion(
                id=r["id"],
                thesis=str(r["thesis"] or ""),
                harvest_class=harvest_class,
                target_id=target_id,
                produced_at=r["produced_at"],
                live_reach=0,
                desk_salience=0.0,
                geo=scope_geo,
            ),
        )
    return None


async def resolve_backlog_block(
    resolver: Any,
    pool: Any,
    *,
    limit: int,
    analyst_id: str,
    run_id: Any = None,
    sink: Any = None,
) -> str | None:
    """The whole GROUND-phase backlog leg: settle, rank, assign-or-offer, claim,
    fill the sink. Returns the block to prepend, or ``None`` for nothing to say.

    Sequence, and why it is this order:

      1. **Settle** outstanding claims first (:func:`settle_claimed_questions`),
         so a question already answered is off the backlog and an abandoned
         claim is back on it BEFORE the ranking that decides this run's job.
      1.5 **Re-render a claim that stands** (:func:`resolve_standing_assignment`)
         — a dispatched gap THIS analyst claimed and did not answer is this
         run's assignment again. It outranks the ranking because the ranker
         cannot see it at all (its SQL reads ``status='open_question'``), which
         is how the 09-07 narrated run orphaned the IL gap for two ticks.
      2. **Rank** through the resolver's own recursive-CTE + priority key —
         untouched: the class ordinal and the age decay stay exactly as the
         2026-09-06 tune left them.
      3. **Assign or offer.** A DISPATCHED question in the ranked backlog
         becomes the run's assignment and the ONLY question rendered
         (:func:`build_assignment_block`). With none, the full ranked backlog
         renders through ``grounding.build_open_questions_block`` — the same
         call, the same arguments, the same bytes as before this existed.
      4. **Claim** the assignment so the next tick takes the NEXT gap. After
         the render, never before: a failed claim must not cost this run its
         assignment, and the block is already built by then.
      5. **Fill the sink** from whatever was actually rendered, so the tags the
         model can answer with are exactly the tags it was shown.

    DEGRADE-NOT-BREAK throughout: the resolver already returns ``[]`` on any
    read failure, and the settle/claim writes swallow their own errors, so the
    worst case is the pre-dispatch behaviour (self-selection), never a failed
    run.
    """
    from .grounding import build_open_questions_block

    await settle_claimed_questions(pool, resolved_by=analyst_id)
    # 1.5 — A CLAIM THIS ANALYST STILL HOLDS OUTRANKS THE RANKING. The settle
    # above has already answered or released everything it could, so a claim
    # that survives it is a dispatched gap the previous run took and did not
    # answer. Re-render it as the same assignment; the ranker cannot, because
    # its SQL reads ``status='open_question'`` and a claim is not that.
    standing = await resolve_standing_assignment(pool, analyst_id=analyst_id)
    questions = list(await resolver.resolve_open_questions(limit=limit))
    assignment = standing or select_dispatched_assignment(questions)
    if assignment is None:
        block = build_open_questions_block(questions)
    else:
        questions = [assignment.question]
        block = build_assignment_block(assignment)
        if standing is None:
            # A re-rendered claim is NOT re-claimed: the claim already stands,
            # and re-stamping it would move ``updated_at`` and push the reclaim
            # window out by another window on every tick — a gap nobody can
            # answer would then never return to the general backlog.
            await claim_dispatched_question(
                pool, question_id=assignment.question_id, run_id=run_id,
            )
    if not block:
        return None
    fill_question_sink(sink, questions, assignment=assignment)
    return block

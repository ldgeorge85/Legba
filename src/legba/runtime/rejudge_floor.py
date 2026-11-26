# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""RE-JUDGE an outage's floor critiques (the apply half of SEAMS #69).

When every judge rung is down the faithfulness verify pass publishes the
deterministic floor (``judge_status='deterministic'``, ``judge_unavailable_reason``
``judge_error`` / ``judge_empty``, score under the 0.85 provisional ceiling) and
nothing re-judges it when a rung comes back. ``scripts/rejudge_floor_critiques.py``
inventories that debt; this module is what its ``--apply`` runs.

THE PASS IS THE CRITIC'S PASS. For each selected floor critique the finding it
was derived from is re-read and ``verify_finding_faithfulness`` runs with the
arguments ``actor_critic.verify_inline_target_finding`` builds — the stored
body, title, citations (with the source text the bridge captured at the time),
indicators, eval and assembly blocks, the composition confidence, the finding's
run id (so a scoped-absence claim is judged against the RETAINED input slice of
that run, never a new one) — against the CURRENT judge chain of the analyzed
analyst (the resolution ladder + ``method.llm.judge_chain``, wired exactly as
``dapr_host`` wires it). Nothing is re-fetched from a source; nothing about the
finding is written.

WHAT THE OLD VERDICT BECOMES (the shape SEAMS #69 said was undesigned):

  * the new critique is an ordinary ``Faithfulness verify`` row, produced now,
    ``derived_from = [finding id]``, so every reader's fold (the latest critique
    per finding by ``produced_at``) reads it with no reader change;
  * ``verification.rejudge_of = <old critique id>`` and
    ``verification.judge_trigger = 'rejudge_floor'`` on it — the trigger keeps a
    re-judged verdict OUT of the sampled arm (every arm comparison filters
    ``COALESCE(judge_trigger, 'sampled') = 'sampled'``): the row was selected by
    an outage, not by the sampling policy;
  * the old floor row is CLOSED, not edited: ``superseded_by = <new id>``,
    ``superseded_at = now()`` — the columns ``analyst_outputs`` already carries
    and the decision-time reads already honour (validity
    ``[produced_at, superseded_at)``), so "what did Legba publish on 10-18" still
    answers the floor. Its ``data`` is untouched. The insert and the close are
    one transaction; a close that finds the row already closed rolls the insert
    back (a concurrent apply never writes two supersessors).

NEVER A SECOND FLOOR. A re-run the judge did not adjudicate (``is_provisional``
— a floor again, unsampled, flag off) writes NOTHING: the old critique stays the
published verdict, the row is counted ``judge_failed``.

SELECTION. Only critiques that are STILL the finding's newest faithfulness
critique and not already superseded: a finding a later run has re-judged keeps
that verdict (re-judging the stale floor would publish a newer row over it).
Oldest first, bounded by the caller's limit.

THE MISSING MODE (``--missing``; the ``verify_backfill`` sweep). A
rolling deploy recreates the runtime actor host; an analyst run in flight at
that moment has already WRITTEN its finding, but the verify leg that writes the
critique never runs, so the finding stands published with NO critique at all.
:func:`select_missing_findings` finds them — cited ``kind='finding'`` rows older
than a minimum age (default 30 min, so a verify still running is never raced),
of an analyst the critic demonstrably covers (it has faithfulness critiques in
the window), not a deterministic rollup, not superseded, with no faithfulness
critique — oldest first; :func:`backfill_one` runs the SAME verify pass over
each (the shared :func:`_verify_and_write`) and writes the critique the critic
would have written: ``derived_from = [finding id]``, the critic's
``analyst_ctx`` (the finding's analyst, version and run), stamped
``verification.judge_trigger = 'verify_backfill'`` (out of the sampled arm, like
a re-judge) and ``verification.backfill_of_run = <finding run_id>``. There is
nothing to supersede. A judge that does not answer writes NOTHING (the finding
stays missing and is retried); the insert re-checks, under a per-finding
advisory lock in the same transaction, that no critique appeared meanwhile.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence
from uuid import UUID, uuid4

logger = logging.getLogger(__name__)

#: The faithfulness critique family — the load-bearing title prefix.
TITLE_PREFIX = "Faithfulness verify%"
#: "The judge was ASKED and did not answer" — the only floors that are debt.
UNAVAILABLE_REASONS: tuple[str, ...] = ("judge_error", "judge_empty")
#: ``verification.judge_trigger`` on every re-judged critique.
JUDGE_TRIGGER_REJUDGE_FLOOR = "rejudge_floor"
#: ``verification.judge_trigger`` on every backfilled critique.
JUDGE_TRIGGER_VERIFY_BACKFILL = "verify_backfill"
#: The missing mode never selects a finding younger than this: its verify leg
#: may still be running (a judge call can take minutes).
DEFAULT_MIN_AGE_MINUTES = 30
#: The refusal ceiling on one apply run's ``--limit``.
MAX_APPLY_LIMIT = 500
#: One row's verify pass may not run longer than this (a hung rung is a failure).
PER_ROW_TIMEOUT_S = 300.0
#: Consecutive judge failures after which the run stops (the chain is down again).
DEFAULT_MAX_CONSECUTIVE_FAILURES = 5

#: The analyst kinds the critic grades through the unit / composition branches.
UNIT_KIND = "inline_target"
COMPOSITION_KINDS = frozenset(
    {"meta_findings_synthesizer", "cross_analyst_correlator", "situation_tracker"}
)
#: Journal kinds never persist a floor (a judge-down journal entry gets no
#: critique at all), so a journal floor is not debt this module can owe.
JOURNAL_KINDS = frozenset({"journal_assessor", "inquiry"})

# Outcomes (one per row).
OUTCOME_REJUDGED = "rejudged"
OUTCOME_JUDGE_FAILED = "judge_failed"
OUTCOME_WEAK_SKIPPED = "weak_skipped"
OUTCOME_SKIPPED = "skipped"
OUTCOME_DLQ = "dlq"
OUTCOME_RACED = "raced"
OUTCOME_BACKFILLED = "backfilled"

_UUID_RE = "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"

#: The apply selection. $1 since, $2 until (on the critique's ``created_at``, as
#: the inventory), $3 the title prefix, $4 the floor reasons, $5 an optional
#: analyzed analyst id, $6 the limit. The finding is LEFT-joined so a critique
#: whose finding is gone is reported, not silently dropped.
SELECT_APPLY_SQL = f"""
SELECT o.id                                   AS critique_id,
       o.created_at                           AS critique_created_at,
       o.run_id                               AS critique_run_id,
       o.analyst_id                           AS critique_analyst_id,
       o.analyst_version                      AS critique_analyst_version,
       o.data                                 AS critique_data,
       f.id                                   AS finding_id,
       f.title                                AS finding_title,
       f.body                                 AS finding_body,
       f.confidence                           AS finding_confidence,
       (f.data->'data')                       AS finding_payload_data,
       f.target_id                            AS finding_target_id,
       f.run_id                               AS finding_run_id
  FROM public.analyst_outputs o
  LEFT JOIN public.analyst_outputs f
         ON f.id = CASE WHEN o.data->>'analyzed_output_id' ~ '{_UUID_RE}'
                        THEN (o.data->>'analyzed_output_id')::uuid END
 WHERE o.kind = 'critique'
   AND o.title LIKE $3::text
   AND o.created_at >= $1::timestamptz
   AND o.created_at <  $2::timestamptz
   AND o.data->'data'->'verification'->>'judge_status' = 'deterministic'
   AND o.data->'data'->'verification'->>'judge_unavailable_reason' = ANY($4::text[])
   AND ($5::text IS NULL OR o.data->>'analyzed_analyst_id' = $5)
   AND o.superseded_by IS NULL
   AND NOT EXISTS (
       SELECT 1
         FROM public.analyst_outputs n
        WHERE n.kind = 'critique'
          AND n.title LIKE $3::text
          AND n.data->>'analyzed_output_id' = o.data->>'analyzed_output_id'
          AND (n.produced_at, n.id) > (o.produced_at, o.id)
   )
 ORDER BY o.created_at ASC, o.id ASC
 LIMIT $6
"""

#: Close the old floor. Guarded on ``superseded_by IS NULL`` so a second apply
#: (or a concurrent one) can never re-point an already-closed row.
SUPERSEDE_SQL = """
UPDATE public.analyst_outputs
   SET superseded_by = $2, superseded_at = now()
 WHERE id = $1
   AND kind = 'critique'
   AND superseded_by IS NULL
"""


class _Raced(RuntimeError):
    """The old critique was closed between selection and write — roll back."""


def _coerce_json(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (bytes, bytearray)):
        value = value.decode()
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    return value


def _float(value: Any) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class FloorCritique:
    """One selected floor critique beside the finding it was derived from."""

    critique_id: UUID
    created_at: Any
    run_id: Any
    analyst_id: str
    analyst_version: str
    critique_data: dict[str, Any]
    finding_id: UUID | None
    finding_title: str = ""
    finding_body: str = ""
    finding_confidence: float | None = None
    finding_data: dict[str, Any] = field(default_factory=dict)
    finding_target_id: str | None = None
    finding_run_id: Any = None

    @property
    def analyzed_analyst_id(self) -> str:
        return str(self.critique_data.get("analyzed_analyst_id") or self.analyst_id or "")

    @property
    def old_overall_score(self) -> float | None:
        return _float(self.critique_data.get("overall_score"))

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> "FloorCritique":
        cdata = _coerce_json(rec["critique_data"]) or {}
        fdata = _coerce_json(rec["finding_payload_data"]) or {}
        return cls(
            critique_id=rec["critique_id"],
            created_at=rec["critique_created_at"],
            run_id=rec["critique_run_id"],
            analyst_id=str(rec["critique_analyst_id"] or ""),
            analyst_version=str(rec["critique_analyst_version"] or ""),
            critique_data=dict(cdata) if isinstance(cdata, Mapping) else {},
            finding_id=rec["finding_id"],
            finding_title=str(rec["finding_title"] or ""),
            finding_body=str(rec["finding_body"] or ""),
            finding_confidence=_float(rec["finding_confidence"]),
            finding_data=dict(fdata) if isinstance(fdata, Mapping) else {},
            finding_target_id=rec["finding_target_id"],
            finding_run_id=rec["finding_run_id"],
        )


@dataclass
class AnalystJudge:
    """The CURRENT judge wiring for one analyzed analyst — what ``dapr_host``
    hands the critic as ``verify_judge`` / ``verify_judge_ref`` /
    ``verify_judge_route`` — plus the analyst's kind."""

    kind: str
    judge: Any
    judge_llm_ref: str = ""
    judge_route: str = ""
    #: ``method.llm.primary`` (its ``raw`` StackRef path) — what the critic
    #: stamps as ``analyzed_model``; the backfill has no old critique to copy.
    analyzed_model: str = ""


#: ``analyst_id -> AnalystJudge | None`` (None: no judge route / unresolvable).
JudgeResolver = Callable[[str], Awaitable["AnalystJudge | None"]]


@dataclass
class RowOutcome:
    #: The floor critique re-judged; ``None`` for a backfilled (missing) row.
    critique_id: UUID | None
    finding_id: UUID | None
    analyst_id: str
    outcome: str
    old_score: float | None
    new_score: float | None = None
    judge_status: str | None = None
    judge_llm_ref: str | None = None
    new_critique_id: UUID | None = None
    detail: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "critique_id": str(self.critique_id) if self.critique_id else None,
            "finding_id": str(self.finding_id) if self.finding_id else None,
            "analyst_id": self.analyst_id,
            "outcome": self.outcome,
            "old_score": self.old_score,
            "new_score": self.new_score,
            "judge_status": self.judge_status,
            "judge_llm_ref": self.judge_llm_ref,
            "new_critique_id": str(self.new_critique_id) if self.new_critique_id else None,
            "detail": self.detail,
        }


async def select_floor_critiques(
    conn: Any,
    *,
    since: Any,
    until: Any,
    limit: int,
    analyst_id: str | None = None,
) -> list[FloorCritique]:
    """The apply selection — still-floor-only, unsuperseded, oldest first."""
    rows = await conn.fetch(
        SELECT_APPLY_SQL, since, until, TITLE_PREFIX, list(UNAVAILABLE_REASONS),
        analyst_id, int(limit),
    )
    return [FloorCritique.from_record(r) for r in rows]


def _skip_reason(row: FloorCritique, kind: str) -> str | None:
    """The critic's own scope guard, applied to the stored finding: a row the
    critic would not grade today is not re-graded here either."""
    from ..data.provenance.kinds import is_deterministic_rollup

    if row.finding_id is None:
        return "finding_missing"
    if kind in JOURNAL_KINDS:
        return f"journal_kind:{kind}"
    if is_deterministic_rollup(row.finding_data):
        return "deterministic_rollup"
    if kind == UNIT_KIND:
        return None
    if kind in COMPOSITION_KINDS:
        return None if row.finding_data.get("citations") is not None else "no_citations"
    return f"kind_not_verified:{kind or '?'}"


def verify_kwargs(row: FloorCritique, *, kind: str, judge_llm: Any, conn: Any) -> dict[str, Any]:
    """``verify_finding_faithfulness`` arguments, built as the critic builds them
    (``actor_critic.verify_inline_target_finding``) from the STORED finding.
    No sampling gate: the floor being repaid was a sampled pass whose judge did
    not answer, and the operator chose to re-ask."""
    data = row.finding_data
    return {
        "body": row.finding_body,
        "citations": data.get("citations"),
        "judge_llm": judge_llm,
        # T7 hedge-laundering input — compositions only (the unit path keeps None).
        "finding_confidence": (
            row.finding_confidence if kind in COMPOSITION_KINDS else None
        ),
        "indicators": data.get("indicators"),
        "title": row.finding_title,
        "target_id": row.finding_target_id,
        "facts_conn": conn,
        # V-B: the RETAINED slice of the run that wrote the finding — the same
        # slice the original pass read, not a new one.
        "slice_conn": conn,
        "run_id": row.finding_run_id,
        "eval_block": data.get("eval"),
        "assembly": data.get("assembly"),
        "judge_sampling": None,
    }


def _critique_payload(
    row: Any,
    report: Any,
    wiring: AnalystJudge,
    session: Any,
    *,
    kind: str,
    analyzed_analyst_version: str,
    analyzed_model: str,
    judge_trigger: str,
) -> dict[str, Any]:
    """The faithfulness critique payload, built exactly as the critic builds it
    (B6 off-subject flags, the chain stamps, the unit uncited share)."""
    from ..data.provenance.verify import build_faithfulness_critique_payload
    from .actor_critic import _flag_off_subject_claims
    from .judge_fallback_chain import judge_stamps

    # B6 — the critic flags off-subject claims before persisting; so does this.
    _flag_off_subject_claims(
        report, title=row.finding_title, body=row.finding_body,
        target_id=row.finding_target_id,
    )
    judge_model = str(getattr(wiring.judge, "subprovider", "") or "deterministic-floor")
    judge_model, judge_llm_ref, judge_route, chain_extras = judge_stamps(
        judge=wiring.judge, session=session, judge_model=judge_model,
        judge_llm_ref=wiring.judge_llm_ref, judge_route=wiring.judge_route,
        report=report,
    )
    payload = build_faithfulness_critique_payload(
        report,
        analyzed_output_id=row.finding_id,
        analyzed_analyst_id=row.analyzed_analyst_id,
        analyzed_analyst_version=analyzed_analyst_version,
        analyzed_model=analyzed_model,
        judge_model=judge_model,
        judge_llm_ref=judge_llm_ref,
        judge_route=judge_route,
        judge_trigger=judge_trigger,
    )
    verification = payload.setdefault("data", {}).setdefault("verification", {})
    if chain_extras:
        verification.update(chain_extras)
    if kind == UNIT_KIND:
        from ..data.provenance.uncited_share import uncited_share_fields

        verification.update(uncited_share_fields(row.finding_body))
    return payload


def build_rejudge_payload(
    row: FloorCritique,
    report: Any,
    wiring: AnalystJudge,
    session: Any,
    *,
    kind: str,
) -> dict[str, Any]:
    """The critique payload for an adjudicated re-run, stamped as a re-judge."""
    cdata = row.critique_data
    payload = _critique_payload(
        row, report, wiring, session, kind=kind,
        analyzed_analyst_version=str(
            cdata.get("analyzed_analyst_version") or row.analyst_version or ""
        ),
        analyzed_model=str(cdata.get("analyzed_model") or ""),
        judge_trigger=JUDGE_TRIGGER_REJUDGE_FLOOR,
    )
    payload["data"]["verification"]["rejudge_of"] = str(row.critique_id)
    return payload


async def persist_rejudge(
    conn: Any, row: FloorCritique, payload: dict[str, Any],
) -> tuple[str, UUID | None]:
    """Insert the new critique and close the old floor, in ONE transaction.

    Returns ``(outcome, new_id)``: ``rejudged`` with the new id, ``dlq`` when
    the payload failed validation (the dead-letter row commits; the old floor
    stays open), ``raced`` when the old row was closed meanwhile (rolled back).
    """
    from ..data.provenance._core import AnalystContext
    from ..data.provenance.writes import write_critique

    new_id = uuid4()
    ctx = AnalystContext(
        analyst_id=row.analyst_id or row.analyzed_analyst_id,
        analyst_version=row.analyst_version,
        run_id=row.run_id,
        target_id=None,
        target_version=None,
    )
    try:
        async with conn.transaction():
            written, _dlq = await write_critique(
                conn, analyst_ctx=ctx, payload=payload,
                derived_from=[row.finding_id], row_id=new_id,
            )
            if written is None:
                return OUTCOME_DLQ, None
            status = await conn.execute(SUPERSEDE_SQL, row.critique_id, new_id)
            if not str(status).endswith(" 1"):
                raise _Raced(str(status))
    except _Raced:
        return OUTCOME_RACED, None
    return OUTCOME_REJUDGED, new_id


async def _verify_and_write(
    conn: Any,
    row: Any,
    wiring: AnalystJudge | None,
    out: RowOutcome,
    *,
    build_payload: Callable[..., dict[str, Any]],
    persist: Callable[[Any, Any, dict[str, Any]], Awaitable[tuple[str, UUID | None]]],
    verify_fn: Callable[..., Awaitable[Any]] | None,
    accept_weak: bool,
    timeout_s: float,
) -> RowOutcome:
    """THE ONE VERIFY STEP both modes share: the critic's scope guard, the
    critic's verify pass over the stored finding, NEVER A FLOOR written (an
    unadjudicated re-run is a counted ``judge_failed``), the payload, the
    weak-rung refusal, the write. Never raises."""
    from ..data.provenance.judge_assessability import is_provisional
    from .judge_fallback_chain import open_session

    if wiring is None or wiring.judge is None:
        out.outcome = OUTCOME_JUDGE_FAILED
        out.detail = "no_judge_wired"
        return out
    skip = _skip_reason(row, wiring.kind)
    if skip is not None:
        out.detail = skip
        return out
    if verify_fn is None:
        from ..data.provenance.verify import verify_finding_faithfulness as verify_fn

    session = open_session(wiring.judge)
    try:
        report = await asyncio.wait_for(
            verify_fn(**verify_kwargs(row, kind=wiring.kind, judge_llm=session, conn=conn)),
            timeout=timeout_s,
        )
    except Exception as exc:  # noqa: BLE001 — a failed pass is a counted failure
        out.outcome = OUTCOME_JUDGE_FAILED
        out.detail = f"verify_error:{type(exc).__name__}"
        return out
    out.judge_status = str(getattr(report, "judge_status", "") or "")
    if is_provisional(out.judge_status):
        # NEVER A FLOOR WRITTEN HERE: the published state stays as it is.
        out.outcome = OUTCOME_JUDGE_FAILED
        out.detail = str(getattr(report, "judge_unavailable_reason", None) or out.judge_status)
        return out
    try:
        payload = build_payload(row, report, wiring, session, kind=wiring.kind)
    except Exception as exc:  # noqa: BLE001
        out.outcome = OUTCOME_JUDGE_FAILED
        out.detail = f"payload_error:{type(exc).__name__}"
        return out
    out.new_score = _float(payload.get("overall_score"))
    out.judge_llm_ref = str(payload.get("judge_llm_ref") or "")
    chain = (payload.get("data") or {}).get("verification", {}).get("judge_chain") or {}
    if chain.get("weak") and not accept_weak:
        out.outcome = OUTCOME_WEAK_SKIPPED
        out.detail = f"weak_rung:{chain.get('used')}"
        return out
    try:
        out.outcome, out.new_critique_id = await persist(conn, row, payload)
    except Exception as exc:  # noqa: BLE001 — the transaction rolled back
        out.outcome = OUTCOME_JUDGE_FAILED
        out.detail = f"write_error:{type(exc).__name__}"
        out.new_score = None
        return out
    if out.outcome not in (OUTCOME_REJUDGED, OUTCOME_BACKFILLED):
        out.new_score = None
    return out


async def rejudge_one(
    conn: Any,
    row: FloorCritique,
    wiring: AnalystJudge | None,
    *,
    verify_fn: Callable[..., Awaitable[Any]] | None = None,
    accept_weak: bool = True,
    timeout_s: float = PER_ROW_TIMEOUT_S,
) -> RowOutcome:
    """Re-judge ONE floor critique. Never raises; never writes a second floor."""
    out = RowOutcome(
        critique_id=row.critique_id, finding_id=row.finding_id,
        analyst_id=row.analyzed_analyst_id, outcome=OUTCOME_SKIPPED,
        old_score=row.old_overall_score,
    )
    return await _verify_and_write(
        conn, row, wiring, out, build_payload=build_rejudge_payload,
        persist=persist_rejudge, verify_fn=verify_fn, accept_weak=accept_weak,
        timeout_s=timeout_s,
    )


# ---------------------------------------------------------------------------
# The MISSING mode: findings whose verify leg never ran
# ---------------------------------------------------------------------------

#: The missing-mode predicate, shared by the selection, the per-day count and
#: the backlog count. $1 since, $2 until (on the finding's ``produced_at`` — the
#: ``idx_analyst_outputs_kind (kind, produced_at)`` range), $3 the minimum age in
#: minutes, $4 the title prefix, $5 an optional analyst id, $6 the deterministic
#: rollup schema, $7 finding ids to leave out (the sweep's parked rows).
#:
#: THE CRITIC'S SCOPE, as far as SQL can see it: a CITED finding (the critic's
#: composition branch no-ops without a citation bridge; the unit branch always
#: grades, but an uncited unit finding is out of this lane's scope), never the
#: deterministic rollup (D-5 — not graded), never a superseded finding (it is
#: no longer the published one), and only of an analyst the critic demonstrably
#: COVERS — one with at least one faithfulness critique in the window. That last
#: test is what keeps an analyst the critic never fires for (no verify route,
#: an uncovered kind) from filling the oldest-first queue forever; the kind
#: guard (:func:`_skip_reason`) still runs per row on the resolved descriptor.
#: The ``NOT EXISTS`` rides ``idx_analyst_outputs_critique_analyzed_output_id``.
_MISSING_FROM_WHERE = """
  FROM public.analyst_outputs f
 WHERE f.kind = 'finding'
   AND f.produced_at >= $1::timestamptz
   AND f.produced_at <  LEAST($2::timestamptz, now() - make_interval(mins => $3::int))
   AND f.superseded_by IS NULL
   AND (CASE WHEN jsonb_typeof(f.data->'data'->'citations') = 'array'
             THEN jsonb_array_length(f.data->'data'->'citations') ELSE 0 END) > 0
   AND COALESCE(f.data->'data'->'rollup'->>'schema', '') <> $6::text
   AND ($5::text IS NULL OR f.analyst_id = $5)
   AND NOT (f.id = ANY($7::uuid[]))
   AND f.analyst_id IN (
       SELECT DISTINCT c.data->>'analyzed_analyst_id'
         FROM public.analyst_outputs c
        WHERE c.kind = 'critique'
          AND c.title LIKE $4::text
          AND c.produced_at >= $1::timestamptz
   )
   -- set-based: the window's critiqued ids as ONE hashed set — the critic
   -- guard (tests/runtime/test_critic_fold_set_based.py) forbids the per-row
   -- correlated probe on analyzed_output_id in production code
   AND f.id::text NOT IN (
       SELECT n.data->>'analyzed_output_id'
         FROM public.analyst_outputs n
        WHERE n.kind = 'critique'
          AND n.title LIKE $4::text
          AND n.produced_at >= $1::timestamptz
          AND n.data->>'analyzed_output_id' IS NOT NULL
   )
"""

#: The missing-mode selection — oldest first, bounded by $8.
SELECT_MISSING_SQL = f"""
SELECT f.id                                   AS finding_id,
       f.produced_at                          AS finding_produced_at,
       f.run_id                               AS finding_run_id,
       f.analyst_id                           AS finding_analyst_id,
       f.analyst_version                      AS finding_analyst_version,
       f.title                                AS finding_title,
       f.body                                 AS finding_body,
       f.confidence                           AS finding_confidence,
       (f.data->'data')                       AS finding_payload_data,
       f.target_id                            AS finding_target_id
{_MISSING_FROM_WHERE}
 ORDER BY f.produced_at ASC, f.id ASC
 LIMIT $8
"""

#: Per UTC day (the dry run's table).
COUNT_MISSING_BY_DAY_SQL = f"""
SELECT date_trunc('day', f.produced_at AT TIME ZONE 'UTC')::date AS day,
       count(*)::int                                            AS missing
{_MISSING_FROM_WHERE}
 GROUP BY 1
 ORDER BY 1
"""

#: The whole backlog (the sweep's ``still_missing``).
COUNT_MISSING_SQL = f"SELECT count(*)::int AS missing{_MISSING_FROM_WHERE}"

#: Serialises two backfills of ONE finding (the sweep and a hand-run script).
BACKFILL_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended($1::text, 0))"

#: Re-checked inside the write transaction: a critique that landed after the
#: selection (a late verify leg, another backfill) wins, and nothing is written.
HAS_CRITIQUE_SQL = """
SELECT EXISTS (
    SELECT 1
      FROM public.analyst_outputs
     WHERE kind = 'critique'
       AND data->>'analyzed_output_id' = $1::text
       AND title LIKE $2::text
)
"""


@dataclass(frozen=True)
class MissingFinding:
    """One cited finding the critic never wrote a critique for."""

    finding_id: UUID
    produced_at: Any
    finding_run_id: Any
    analyst_id: str
    analyst_version: str
    finding_title: str = ""
    finding_body: str = ""
    finding_confidence: float | None = None
    finding_data: dict[str, Any] = field(default_factory=dict)
    finding_target_id: str | None = None

    @property
    def analyzed_analyst_id(self) -> str:
        return self.analyst_id

    @classmethod
    def from_record(cls, rec: Mapping[str, Any]) -> "MissingFinding":
        fdata = _coerce_json(rec["finding_payload_data"]) or {}
        return cls(
            finding_id=rec["finding_id"],
            produced_at=rec["finding_produced_at"],
            finding_run_id=rec["finding_run_id"],
            analyst_id=str(rec["finding_analyst_id"] or ""),
            analyst_version=str(rec["finding_analyst_version"] or ""),
            finding_title=str(rec["finding_title"] or ""),
            finding_body=str(rec["finding_body"] or ""),
            finding_confidence=_float(rec["finding_confidence"]),
            finding_data=dict(fdata) if isinstance(fdata, Mapping) else {},
            finding_target_id=rec["finding_target_id"],
        )


def _missing_args(
    *, since: Any, until: Any, min_age_minutes: int, analyst_id: str | None,
    exclude: Sequence[Any],
) -> tuple[Any, ...]:
    from ..data.provenance.kinds import ROLLUP_PAYLOAD_SCHEMA

    return (
        since, until, int(min_age_minutes), TITLE_PREFIX, analyst_id,
        ROLLUP_PAYLOAD_SCHEMA, [x if isinstance(x, UUID) else UUID(str(x)) for x in exclude],
    )


async def select_missing_findings(
    conn: Any,
    *,
    since: Any,
    until: Any,
    limit: int,
    min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES,
    analyst_id: str | None = None,
    exclude: Sequence[Any] = (),
) -> list[MissingFinding]:
    """The missing-mode selection — cited, covered, critique-less, older than
    ``min_age_minutes``, oldest first."""
    rows = await conn.fetch(
        SELECT_MISSING_SQL,
        *_missing_args(since=since, until=until, min_age_minutes=min_age_minutes,
                       analyst_id=analyst_id, exclude=exclude),
        int(limit),
    )
    return [MissingFinding.from_record(r) for r in rows]


async def count_missing_by_day(
    conn: Any,
    *,
    since: Any,
    until: Any,
    min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES,
    analyst_id: str | None = None,
) -> list[dict[str, Any]]:
    rows = await conn.fetch(
        COUNT_MISSING_BY_DAY_SQL,
        *_missing_args(since=since, until=until, min_age_minutes=min_age_minutes,
                       analyst_id=analyst_id, exclude=()),
    )
    return [{"day": r["day"], "missing": int(r["missing"] or 0)} for r in rows]


async def count_missing(
    conn: Any,
    *,
    since: Any,
    until: Any,
    min_age_minutes: int = DEFAULT_MIN_AGE_MINUTES,
    analyst_id: str | None = None,
) -> int:
    value = await conn.fetchval(
        COUNT_MISSING_SQL,
        *_missing_args(since=since, until=until, min_age_minutes=min_age_minutes,
                       analyst_id=analyst_id, exclude=()),
    )
    return int(value or 0)


def build_backfill_payload(
    row: MissingFinding,
    report: Any,
    wiring: AnalystJudge,
    session: Any,
    *,
    kind: str,
) -> dict[str, Any]:
    """The critique the critic would have written, stamped as a backfill."""
    payload = _critique_payload(
        row, report, wiring, session, kind=kind,
        analyzed_analyst_version=row.analyst_version,
        analyzed_model=wiring.analyzed_model,
        judge_trigger=JUDGE_TRIGGER_VERIFY_BACKFILL,
    )
    payload["data"]["verification"]["backfill_of_run"] = (
        str(row.finding_run_id) if row.finding_run_id is not None else None
    )
    return payload


async def persist_backfill(
    conn: Any, row: MissingFinding, payload: dict[str, Any],
) -> tuple[str, UUID | None]:
    """Insert the backfilled critique as the critic would have (its
    ``analyst_ctx`` = the finding's analyst, version and run; ``derived_from =
    [finding id]``), in ONE transaction that first takes the finding's advisory
    lock and re-checks that no critique landed meanwhile (``raced``, nothing
    written). ``dlq`` when the payload failed validation."""
    from ..data.provenance._core import AnalystContext
    from ..data.provenance.writes import write_critique

    new_id = uuid4()
    ctx = AnalystContext(
        analyst_id=row.analyst_id,
        analyst_version=row.analyst_version,
        run_id=row.finding_run_id,
        target_id=None,
        target_version=None,
    )
    try:
        async with conn.transaction():
            await conn.execute(BACKFILL_LOCK_SQL, f"verify_backfill:{row.finding_id}")
            if await conn.fetchval(HAS_CRITIQUE_SQL, str(row.finding_id), TITLE_PREFIX):
                raise _Raced("critique_exists")
            written, _dlq = await write_critique(
                conn, analyst_ctx=ctx, payload=payload,
                derived_from=[row.finding_id], row_id=new_id,
            )
            if written is None:
                return OUTCOME_DLQ, None
    except _Raced:
        return OUTCOME_RACED, None
    return OUTCOME_BACKFILLED, new_id


async def backfill_one(
    conn: Any,
    row: MissingFinding,
    wiring: AnalystJudge | None,
    *,
    verify_fn: Callable[..., Awaitable[Any]] | None = None,
    accept_weak: bool = True,
    timeout_s: float = PER_ROW_TIMEOUT_S,
) -> RowOutcome:
    """Verify ONE critique-less finding. Never raises; never writes a floor."""
    out = RowOutcome(
        critique_id=None, finding_id=row.finding_id, analyst_id=row.analyst_id,
        outcome=OUTCOME_SKIPPED, old_score=None,
    )
    return await _verify_and_write(
        conn, row, wiring, out, build_payload=build_backfill_payload,
        persist=persist_backfill, verify_fn=verify_fn, accept_weak=accept_weak,
        timeout_s=timeout_s,
    )


@dataclass
class ApplySummary:
    selected: int = 0
    outcomes: list[RowOutcome] = field(default_factory=list)
    stopped_early: str | None = None
    #: ``rejudge`` (floor critiques) or ``backfill`` (missing critiques).
    mode: str = "rejudge"

    def counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for o in self.outcomes:
            out[o.outcome] = out.get(o.outcome, 0) + 1
        return out

    def as_dict(self) -> dict[str, Any]:
        written = [
            o for o in self.outcomes if o.outcome in (OUTCOME_REJUDGED, OUTCOME_BACKFILLED)
        ]
        olds = [o.old_score for o in written if o.old_score is not None]
        news = [o.new_score for o in written if o.new_score is not None]
        return {
            "mode": self.mode,
            "selected": self.selected,
            "processed": len(self.outcomes),
            "counts": self.counts(),
            "mean_old_score": round(sum(olds) / len(olds), 4) if olds else None,
            "mean_new_score": round(sum(news) / len(news), 4) if news else None,
            "stopped_early": self.stopped_early,
            "rows": [o.as_dict() for o in self.outcomes],
        }


async def apply(
    conn: Any,
    rows: Sequence[FloorCritique | MissingFinding],
    resolve_judge: JudgeResolver,
    *,
    verify_fn: Callable[..., Awaitable[Any]] | None = None,
    accept_weak: bool = True,
    max_consecutive_failures: int = DEFAULT_MAX_CONSECUTIVE_FAILURES,
    on_row: Callable[[RowOutcome], None] | None = None,
    acquire: Callable[[], Any] | None = None,
    max_seconds: float | None = None,
) -> ApplySummary:
    """Re-judge (a :class:`FloorCritique`) or backfill (a
    :class:`MissingFinding`) ``rows`` one at a time, in order. Stops after
    ``max_consecutive_failures`` judge failures in a row (the chain is down
    again — keep the rest of the debt for a later run, unspent), and — when
    ``max_seconds`` is set — before starting a row past that wall clock.
    ``acquire`` (a pool's ``acquire``) runs each row on its own connection, so a
    long run never pins one; without it every row runs on ``conn``."""
    summary = ApplySummary(
        selected=len(rows),
        mode="backfill" if rows and isinstance(rows[0], MissingFinding) else "rejudge",
    )
    wiring_cache: dict[str, AnalystJudge | None] = {}
    consecutive = 0
    loop = asyncio.get_running_loop()
    started = loop.time()
    for row in rows:
        if max_seconds is not None and loop.time() - started >= max_seconds:
            summary.stopped_early = (
                f"the {max_seconds:.0f}s run budget is spent; the remaining rows are untouched"
            )
            break
        aid = row.analyzed_analyst_id
        if aid not in wiring_cache:
            try:
                wiring_cache[aid] = await resolve_judge(aid)
            except Exception as exc:  # noqa: BLE001
                logger.warning("rejudge_floor.resolve_failed analyst=%s err=%s", aid, exc)
                wiring_cache[aid] = None
        one = backfill_one if isinstance(row, MissingFinding) else rejudge_one
        if acquire is not None:
            async with acquire() as row_conn:
                outcome = await one(
                    row_conn, row, wiring_cache[aid], verify_fn=verify_fn,
                    accept_weak=accept_weak,
                )
        else:
            outcome = await one(
                conn, row, wiring_cache[aid], verify_fn=verify_fn, accept_weak=accept_weak,
            )
        summary.outcomes.append(outcome)
        if on_row is not None:
            on_row(outcome)
        consecutive = consecutive + 1 if outcome.outcome == OUTCOME_JUDGE_FAILED else 0
        if max_consecutive_failures > 0 and consecutive >= max_consecutive_failures:
            summary.stopped_early = (
                f"{consecutive} consecutive judge failures — the chain is not answering; "
                "the remaining rows are untouched"
            )
            break
    return summary


def render_row(o: RowOutcome) -> str:
    def _s(v: float | None) -> str:
        return "—" if v is None else f"{v:.2f}"

    head = str(o.critique_id) if o.critique_id else "(no critique)"
    line = (
        f"  {head}  finding={o.finding_id}  {_s(o.old_score)} → {_s(o.new_score)}  "
        f"{o.outcome}  judge_status={o.judge_status or '—'}  "
        f"judge_llm_ref={o.judge_llm_ref or '—'}"
    )
    if o.new_critique_id:
        line += f"  new={o.new_critique_id}"
    if o.detail:
        line += f"  ({o.detail})"
    return line


def render_summary(summary: ApplySummary) -> str:
    d = summary.as_dict()
    counts = ", ".join(f"{k}={v}" for k, v in sorted(d["counts"].items())) or "none"
    label = "verify backfill" if summary.mode == "backfill" else "re-judge apply"
    written = "backfilled" if summary.mode == "backfill" else "re-judged"
    lines = [
        f"{label}: selected={d['selected']} processed={d['processed']}  {counts}",
        f"mean overall_score over {written} rows: {d['mean_old_score']} → {d['mean_new_score']}",
    ]
    if summary.stopped_early:
        lines.append(f"STOPPED EARLY: {summary.stopped_early}")
    return "\n".join(lines)


def primary_model_ref(llm: Any) -> str:
    """``method.llm.primary`` as the critic stamps it (``analyzed_model``): the
    StackRef dump's ``raw`` path, or the bare string — mirrors
    ``actor_critic._extract_primary_model_ref`` over the TYPED descriptor dict."""
    primary = llm.get("primary") if isinstance(llm, Mapping) else None
    if isinstance(primary, Mapping):
        return str(primary.get("raw") or "")
    if isinstance(primary, str):
        return primary
    return ""


def live_judge_resolver(
    *, registry_client: Any, secrets_resolve: Callable[[str], Awaitable[bytes]],
) -> tuple[JudgeResolver, Callable[[], Awaitable[None]]]:
    """The CURRENT judge wiring per analyst, built as ``dapr_host`` builds it:
    the typed descriptor's ``method.llm`` through the resolution ladder
    (``LEGBA_JUDGE_STACK_REF`` → judge → verify → primary), then
    :func:`wire_judge_chain` (``method.llm.judge_chain`` /
    ``LEGBA_JUDGE_CHAIN_DEFAULT``). Handlers are cached per component id.
    Returns ``(resolver, close)``."""
    from .analyst_deps_builder import (
        build_llm_handler_from_stack_component,
        resolve_judge_route_from_llm_block,
    )
    from .judge_fallback_chain import wire_judge_chain

    handlers: dict[str, Any] = {}

    async def factory(ref: str) -> Any:
        if ref not in handlers:
            handlers[ref] = await build_llm_handler_from_stack_component(
                ref, registry_client=registry_client, secrets_resolve=secrets_resolve,
            )
        return handlers[ref]

    async def resolve(analyst_id: str) -> AnalystJudge | None:
        typed = await registry_client.get_descriptor_typed(analyst_id, family="analyst")
        if not isinstance(typed, Mapping):
            return None
        kind = str(((typed.get("identity") or {}).get("kind")) or "")
        llm = ((typed.get("method") or {}).get("llm")) or {}
        analyzed_model = primary_model_ref(llm)
        route = resolve_judge_route_from_llm_block(llm)
        if route is None:
            return AnalystJudge(kind=kind, judge=None, analyzed_model=analyzed_model)
        judge, ref, route_class = await wire_judge_chain(
            route, llm, llm_handler_factory=factory, analyst_id=analyst_id,
            actor_id="rejudge_floor",
        )
        return AnalystJudge(kind=kind, judge=judge, judge_llm_ref=ref, judge_route=route_class,
                            analyzed_model=analyzed_model)

    async def close() -> None:
        for h in handlers.values():
            if hasattr(h, "on_deactivate"):
                try:
                    await h.on_deactivate(None)
                except Exception:  # noqa: BLE001 — best-effort teardown
                    pass

    return resolve, close


__all__ = [
    "BACKFILL_LOCK_SQL",
    "COMPOSITION_KINDS",
    "COUNT_MISSING_BY_DAY_SQL",
    "COUNT_MISSING_SQL",
    "DEFAULT_MAX_CONSECUTIVE_FAILURES",
    "DEFAULT_MIN_AGE_MINUTES",
    "HAS_CRITIQUE_SQL",
    "JUDGE_TRIGGER_REJUDGE_FLOOR",
    "JUDGE_TRIGGER_VERIFY_BACKFILL",
    "MAX_APPLY_LIMIT",
    "SELECT_APPLY_SQL",
    "SELECT_MISSING_SQL",
    "SUPERSEDE_SQL",
    "AnalystJudge",
    "ApplySummary",
    "FloorCritique",
    "MissingFinding",
    "RowOutcome",
    "apply",
    "backfill_one",
    "build_backfill_payload",
    "build_rejudge_payload",
    "count_missing",
    "count_missing_by_day",
    "live_judge_resolver",
    "persist_backfill",
    "persist_rejudge",
    "primary_model_ref",
    "rejudge_one",
    "render_row",
    "render_summary",
    "select_floor_critiques",
    "select_missing_findings",
    "verify_kwargs",
]

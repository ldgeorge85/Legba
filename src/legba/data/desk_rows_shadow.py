# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contract-v2 SHADOW of a desk run (lane D-1) — measured, never read.

WHAT IT DOES. Beside a live desk run (``inline_target``), on the SAME handed
slice object and the SAME rendered prompt, a second synthesis asks the core
plane for contract-v2 ROWS (:mod:`desk_rows_contract`) instead of prose. Then,
before anything is written:

1. every row through its gates (:func:`desk_rows_gates.gate_reply`);
2. ONE bounded repair round — the model sees only the failed rows with their
   reasons and may fix a quote, narrow a claim or withdraw it, never add one —
   and the repaired reply gated again from the top;
3. the clean-context verifier (:func:`statement_verifier.verify_support`) on
   every fact row that passed (thinking ON when the handler can think);
4. the read RENDERED from the rows (:mod:`desk_rows_render`) with the model's
   optional lead checked sentence by sentence;
5. one row in ``desk_rows_shadow`` (migration 0268): the rows, every outcome
   with its reasons, the rendered read, the computed block, the slice scope,
   calls and seconds per stage.

READS (D-2): two, both indexed, before the synthesis — the entity links of
the slice's rows, and ONE source-roster read (:mod:`desk_rows_roster`: the
target head, the active source heads, their poll outcomes in the window) so
the computed block states which feeds of the desk's pool were failing or
silent instead of ``not_handed``.

WHAT IT IS NOT. Nothing reads ``desk_rows_shadow``: no reader surface, Consult
tool, slice, indicator, alert, composition or downstream analyst (a guard test
proves the table name appears in no other module). The shadow never touches
the run's finding, its trace steps beyond one launch receipt, the run's
outcome or its latency: it runs as a DETACHED task after the run has its
finding, bounded by its own timeout, and every failure is counted on its own
row (``status``), logged, and never raised.

THE SWITCH. ``LEGBA_DESK_ROWS_SHADOW`` — a comma list of analyst ids
(unset/empty = OFF, the shipped default). The runtime builds the shadow only
for a named analyst (``runtime/desk_rows_shadow_wiring.py``) and the launch
re-reads the switch, so clearing it stops new shadows without a rebuild.

THINKING (decided from the measurements, stated on every record):

* the SYNTHESIS and REPAIR calls run thinking OFF. The live desks' synthesis
  calls run on the plain alias today (their traces say ``fast``), so OFF keeps
  the v1/v2 comparison about the contract alone; a desk-sized thinking call
  measured p50 ~27 s / p95 ~121 s at 10k prompt tokens and desk prompts are
  ~4x that; and the checking thinking would buy is done here by code (the
  gates) and by the verifier.
* the VERIFIER runs thinking ON (S-6: catch 0.983 on vs 0.967 off, short
  prompts, cheap).

COST. One desk-sized call per target per cycle, plus at most one repair call
(only when a row failed), plus one short verifier call per fact row that
passed its gates.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .desk_rows_computed import (
    computed_block,
    handed_slice,
    offered_subjects,
    slice_evidence,
    slice_scope,
    subjects_block,
)
from .desk_rows_contract import CONTRACT_VERSION, parse_repairs, parse_rows_reply, reply_as_dict
from .desk_rows_gates import (
    DESK_GATES_VERSION,
    ESTABLISHED_ELIGIBLE,
    FAILED,
    WITHDRAWN,
    GateContext,
    RowOutcome,
    apply_repairs,
    build_repair_prompt,
    failed_ids,
    gate_reply,
    tally,
)
from .desk_rows_render import render_read
from .statement_seats import CallBudget, SeatCall, call_seat, parse_reply_object
from .statement_shapes import GATE_VERSION
from .statement_typing import evidence_texts
from .statement_verifier import SUPPORTS, verify_support

logger = logging.getLogger(__name__)

#: The switch: a comma list of analyst ids. Unset / empty = OFF.
SHADOW_ENV = "LEGBA_DESK_ROWS_SHADOW"
#: The draft descriptor carrying the v2 seat prompt (the prompt manifest's
#: ``desk_rows_v2_seat`` entry).
SEAT_ID = "desk_rows_v2_seat"
#: The token the seat prompt carries for the desk's bounded question.
QUESTION_TOKEN = "<<BOUNDED_QUESTION>>"
#: The shadow ledger (migration 0268). Written HERE and nowhere else.
LEDGER_TABLE = "desk_rows_shadow"
SYNTHESIS_THINKING = False
REPAIR_THINKING = False
VERIFIER_THINKING = True
#: One shadow's wall-clock bound (seconds), the run's own budget untouched.
DEFAULT_TIMEOUT_SECONDS = 900.0

_OUTPUT_REMINDER = ("Return the ONE JSON object your instructions specify — rows, not prose. "
                    "Quote every cited row word for word.")


def shadow_analysts() -> frozenset[str]:
    """The ONE read site for :data:`SHADOW_ENV`."""
    raw = os.environ.get(SHADOW_ENV, "")
    return frozenset(x.strip() for x in raw.split(",") if x.strip())


def shadow_enabled_for(analyst_id: str | None) -> bool:
    return bool(analyst_id) and str(analyst_id) in shadow_analysts()


def rows_system_prompt(template: str, bounded_question: str | None) -> str:
    return template.replace(QUESTION_TOKEN, (bounded_question or "the desk's bounded question").strip())


@dataclass(frozen=True)
class ShadowPrompts:
    rows_system: str
    verifier_system: str


@dataclass(frozen=True)
class ShadowInput:
    """One run, as the live run held it."""

    analyst_id: str
    target_id: str | None
    run_id: str | None
    user_prompt: str
    sliced: Sequence[Mapping[str, Any]]
    citation_index: Mapping[int, Mapping[str, Any]]
    derived_from: Sequence[Any]
    coverage: Mapping[str, Any] | None = None
    window_hours: int | None = None
    run_at: datetime | None = None
    linked: Sequence[Any] = ()
    bounded_question: str | None = None
    #: The run's ONE source-roster read, classified
    #: (:func:`desk_rows_roster.roster_state`); ``None`` = not read.
    roster: Mapping[str, Any] | None = None


@dataclass
class ShadowRecord:
    analyst_id: str
    target_id: str | None
    run_id: str | None
    status: str = "started"
    scope: dict[str, Any] = field(default_factory=dict)
    computed: dict[str, Any] = field(default_factory=dict)
    subjects: dict[str, Any] = field(default_factory=dict)
    first_pass: dict[str, Any] = field(default_factory=dict)
    final: dict[str, Any] = field(default_factory=dict)
    repair: dict[str, Any] = field(default_factory=dict)
    verify: dict[str, Any] = field(default_factory=dict)
    lead: dict[str, Any] = field(default_factory=dict)
    calls: list[dict[str, Any]] = field(default_factory=list)
    seconds: dict[str, float] = field(default_factory=dict)
    reply_defects: list[str] = field(default_factory=list)
    rows: dict[str, Any] = field(default_factory=dict)
    outcomes: list[dict[str, Any]] = field(default_factory=list)
    body: str = ""
    title: str = ""
    thinking: dict[str, bool] = field(default_factory=dict)
    #: What the repair round proposed, each with its verdict (CONTENT: the
    #: ledger record keeps it; :meth:`counts` never carries it).
    repair_proposals: list[dict[str, Any]] = field(default_factory=list)
    #: In-memory only (the harness reads it): the final outcomes and verdicts.
    final_outcomes: list[RowOutcome] = field(default_factory=list, repr=False)
    final_reply: Any = field(default=None, repr=False)
    verdicts: dict[str, str] = field(default_factory=dict)

    def counts(self) -> dict[str, Any]:
        """Counts only — what a report may carry."""
        return {
            "status": self.status, "first_pass": self.first_pass, "final": self.final, "repair": self.repair,
            "verify": self.verify, "lead": self.lead, "seconds": self.seconds,
            "calls": {k: sum(1 for c in self.calls if c["stage"] == k) for k in ("synthesis", "repair", "verify")},
            "body_chars": len(self.body), "reply_defects": len(self.reply_defects),
            "subjects": {k: v for k, v in self.subjects.items() if k != "declined"},
            "roster_basis": self.computed.get("sources_silent_basis"),
        }

    def as_dict(self) -> dict[str, Any]:
        d = {k: v for k, v in self.__dict__.items() if k not in ("final_outcomes", "final_reply")}
        d["contract_version"] = CONTRACT_VERSION
        d["gate_version"] = GATE_VERSION
        d["desk_gates_version"] = DESK_GATES_VERSION
        return d


def _call_row(stage: str, call: SeatCall | None, seconds: float) -> dict[str, Any]:
    return {"stage": stage, "ok": bool(call and call.ok), "error": call.error if call else "no_call",
            "thinking": call.thinking if call else None, "prompt_tokens": call.prompt_tokens if call else None,
            "completion_tokens": call.completion_tokens if call else None, "seconds": round(seconds, 2)}


def _thinks(llm: Any, wanted: bool) -> bool:
    if not wanted:
        return False
    from .stack.llm.thinking import thinking_available

    return thinking_available(llm)


async def run_contract_v2(
    llm: Any,
    inp: ShadowInput,
    prompts: ShadowPrompts,
    *,
    budget: CallBudget | None = None,
    verify: bool = True,
    verify_cap: int | None = None,
    synthesis_thinking: bool = SYNTHESIS_THINKING,
    verifier_thinking: bool = VERIFIER_THINKING,
    in_flight: int = 3,
) -> ShadowRecord:
    """One run under contract v2: synthesis, gates, one repair round, gates,
    verifier, render. No table is read or written here; ``llm`` is the only
    I/O. Never raises on a bad reply: the record says what happened."""
    rec = ShadowRecord(inp.analyst_id, inp.target_id, inp.run_id)
    t_all = time.monotonic()
    scope = slice_scope(inp.sliced, inp.derived_from, collection=inp.target_id, coverage=inp.coverage,
                        window_hours=inp.window_hours, run_at=inp.run_at)
    evidence = slice_evidence(inp.sliced)
    computed = computed_block(inp.sliced, scope, coverage=inp.coverage, evidence=evidence, roster=inp.roster)
    subjects, links, sdetail, found_by = offered_subjects(inp.target_id, inp.linked, evidence)
    block_kinds: dict[int, str] = {}
    for n, entry in (inp.citation_index or {}).items():
        pre = entry.get("_citation") if isinstance(entry, Mapping) else None
        if isinstance(pre, Mapping):
            block_kinds[int(n)] = str(pre.get("ref_kind") or pre.get("grounding") or "grounding")
    ctx = GateContext(evidence, block_kinds, subjects, links, max(1, sdetail["desk_subjects"]),
                      handed_slice(scope, inp.run_id), inp.target_id, inp.run_at, computed, found_by)
    rec.scope, rec.computed, rec.subjects = scope, computed, sdetail
    think_v = _thinks(llm, verifier_thinking)
    rec.thinking = {"synthesis": synthesis_thinking, "repair": synthesis_thinking and REPAIR_THINKING,
                    "verifier": think_v}
    system = prompts.rows_system
    user = f"{inp.user_prompt}\n\n{subjects_block(subjects)}\n\n{_OUTPUT_REMINDER}"

    t0 = time.monotonic()
    content, call = await call_seat(llm, seat=SEAT_ID, system_prompt=system, user_content=user,
                                    budget=budget, thinking=synthesis_thinking)
    rec.calls.append(_call_row("synthesis", call, time.monotonic() - t0))
    rec.seconds["synthesis"] = round(time.monotonic() - t0, 2)
    reply = parse_rows_reply(parse_reply_object(content)) if content else None
    if reply is None:
        rec.status = "no_call" if content is None else "reply_unparseable"
        rec.seconds["total"] = round(time.monotonic() - t_all, 2)
        return rec
    rec.reply_defects = list(reply.defects)
    outcomes = gate_reply(reply, ctx)
    rec.first_pass = tally(outcomes)

    # --- ONE repair round -------------------------------------------------
    failed = failed_ids(outcomes)
    repair_prompt = build_repair_prompt(reply, outcomes, ctx)
    withdrawn: list[RowOutcome] = []
    if repair_prompt is not None:
        t0 = time.monotonic()
        content2, call2 = await call_seat(llm, seat=SEAT_ID, system_prompt=system, user_content=repair_prompt,
                                          budget=budget, thinking=synthesis_thinking and REPAIR_THINKING)
        rec.calls.append(_call_row("repair", call2, time.monotonic() - t0))
        rec.seconds["repair"] = round(time.monotonic() - t0, 2)
        parsed = parse_repairs(parse_reply_object(content2) if content2 else None, failed)
        reply, rdetail = apply_repairs(reply, parsed, ctx)
        rec.repair_proposals = list(rdetail["proposals"])
        before = {o.row_id: o for o in outcomes}
        withdrawn = [RowOutcome(**{**before[w].__dict__, "outcome": WITHDRAWN})
                     for w in sorted(parsed.withdraw) if w in before]
        outcomes = gate_reply(reply, ctx, repaired=frozenset(rdetail["accepted"]))
        now_ok = {o.row_id for o in outcomes if o.outcome != FAILED}
        rec.repair = {"failed_rows_shown": len(failed), "fixed_accepted": len(rdetail["accepted"]),
                      "split_accepted": rdetail["split"],
                      "withdrawn": len(withdrawn), "refused": rdetail["refused"],
                      "repaired_now_pass": len(set(rdetail["accepted"]) & now_ok),
                      "repaired_now_eligible": sum(1 for o in outcomes if o.repaired
                                                   and o.outcome == ESTABLISHED_ELIGIBLE),
                      "reply_parsed": content2 is not None and "*" not in {r for r, _ in parsed.refused}}
    final = outcomes + withdrawn
    rec.final = tally(final)

    # --- verifier on the fact rows that passed -----------------------------
    if verify:
        eligible = [o for o in outcomes if o.outcome == ESTABLISHED_ELIGIBLE and o.result and o.result.draft]
        if verify_cap is not None:
            eligible = eligible[:verify_cap]
        sem = asyncio.Semaphore(max(1, in_flight))
        t0 = time.monotonic()

        async def _one(o: RowOutcome) -> tuple[str, Any, float]:
            evs = [ctx.evidence[n] for n in dict.fromkeys(o.cited) if n in ctx.evidence]
            async with sem:
                t1 = time.monotonic()
                rep = await verify_support(llm, o.result.draft, o.result.support, texts=evidence_texts(evs),
                                           system_prompt=prompts.verifier_system, budget=budget,
                                           read_at=inp.run_at, thinking=think_v)
                return o.row_id, rep, time.monotonic() - t1

        results = await asyncio.gather(*[_one(o) for o in eligible])
        for rid, rep, secs in results:
            rec.verdicts[rid] = rep.verdict
            if rep.decided_by != "code":
                rec.calls.append(_call_row("verify", rep.call, secs))
        rec.seconds["verify"] = round(time.monotonic() - t0, 2)
        vc: dict[str, int] = {}
        for v in rec.verdicts.values():
            vc[v] = vc.get(v, 0) + 1
        rec.verify = {"eligible": rec.final["by_outcome"].get(ESTABLISHED_ELIGIBLE, 0), "verified": len(rec.verdicts),
                      "verdicts": vc, "established": vc.get(SUPPORTS, 0)}

    # --- prose last --------------------------------------------------------
    names = {s.key: s.display(1) for s in subjects}
    rec.body, lead = render_read(reply, outcomes, computed, subject_order=[s.key for s in subjects],
                                 subject_names=names, verdicts=rec.verdicts)
    rec.lead = lead.as_counts()
    rec.title = reply.title
    rec.rows = reply_as_dict(reply)
    rec.outcomes = [o.as_dict() for o in final]
    for o in rec.outcomes:
        if o["id"] in rec.verdicts:
            o["verdict"] = rec.verdicts[o["id"]]
    rec.final_outcomes = final
    rec.final_reply = reply
    rec.status = "written"
    rec.seconds["total"] = round(time.monotonic() - t_all, 2)
    return rec


# ---------------------------------------------------------------------------
# The live path: launch detached, write the ledger row
# ---------------------------------------------------------------------------

_INSERT_SQL = f"""
INSERT INTO {LEDGER_TABLE}
    (run_id, analyst_id, target_id, contract_version, gate_version, status, counts, record)
VALUES ($1, $2, $3, $4, $5, $6, $7::jsonb, $8::jsonb)
"""

#: Detached tasks are held here so the loop does not collect them mid-flight.
_TASKS: set[asyncio.Task] = set()


@dataclass
class DeskRowsShadow:
    """What the runtime hands one desk's runner when the switch names it."""

    llm: Any
    pg: Any
    prompts: ShadowPrompts
    analyst_id: str
    bounded_question: str | None = None
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS


async def _write(pg: Any, rec: ShadowRecord) -> None:
    async with pg.acquire() as conn:
        await conn.execute(_INSERT_SQL, rec.run_id, rec.analyst_id, rec.target_id, CONTRACT_VERSION,
                           GATE_VERSION, rec.status, json.dumps(rec.counts(), default=str),
                           json.dumps(rec.as_dict(), default=str))


async def _linked(pg: Any, sliced: Sequence[Mapping[str, Any]]) -> list[Any]:
    from .statement_actor_subjects import fetch_linked_entities

    ids = [str(r.get("id")) for r in sliced if r.get("id")]
    async with pg.acquire() as conn:
        return await fetch_linked_entities(conn, ids)


async def _roster(pg: Any, inp: ShadowInput) -> dict[str, Any]:
    """The run's ONE roster read (:mod:`desk_rows_roster`), over the slice's
    window; a failed read is stated (``roster_read_failed``), never guessed."""
    from .desk_rows_computed import _utc
    from .desk_rows_roster import fetch_roster, roster_state

    scope = slice_scope(inp.sliced, inp.derived_from, collection=inp.target_id, coverage=inp.coverage,
                        window_hours=inp.window_hours, run_at=inp.run_at)
    start, end = _utc(scope.get("window_start")), _utc(scope.get("window_end"))
    if start is None or end is None:
        return {"basis": "window_unknown"}
    try:
        async with pg.acquire() as conn:
            rows = await fetch_roster(conn, inp.target_id, start, end)
    except Exception as exc:  # noqa: BLE001 — stated on the record, never raised
        logger.warning("desk_rows_shadow.roster_failed analyst=%s err=%s", inp.analyst_id, type(exc).__name__)
        return {"basis": "roster_read_failed"}
    return roster_state(rows, scope.get("source_ids") or ())


async def _shadow_task(shadow: DeskRowsShadow, inp: ShadowInput) -> None:
    rec: ShadowRecord | None = None
    try:
        linked = await _linked(shadow.pg, inp.sliced)
        roster = await _roster(shadow.pg, inp)
        inp = ShadowInput(**{**inp.__dict__, "linked": linked, "roster": roster})
        rec = await asyncio.wait_for(run_contract_v2(shadow.llm, inp, shadow.prompts), shadow.timeout_seconds)
    except asyncio.TimeoutError:
        rec = ShadowRecord(inp.analyst_id, inp.target_id, inp.run_id, status="timeout")
    except Exception as exc:  # noqa: BLE001 — a shadow never breaks anything
        logger.warning("desk_rows_shadow.failed analyst=%s target=%s err=%s", inp.analyst_id, inp.target_id,
                       type(exc).__name__)
        rec = ShadowRecord(inp.analyst_id, inp.target_id, inp.run_id, status=f"error:{type(exc).__name__}")
    try:
        await _write(shadow.pg, rec)
    except Exception as exc:  # noqa: BLE001
        logger.warning("desk_rows_shadow.write_failed analyst=%s target=%s err=%s", inp.analyst_id,
                       inp.target_id, type(exc).__name__)


def launch_shadow(
    shadow: DeskRowsShadow | None,
    *,
    options: Mapping[str, Any],
    user_prompt: str,
    sliced: Sequence[Mapping[str, Any]],
    citation_index: Mapping[int, Mapping[str, Any]],
    derived_from: Sequence[Any],
) -> list[dict[str, Any]]:
    """Start the shadow DETACHED and return the run's one launch receipt
    (a trace step), or ``[]`` when there is no shadow / the switch is off /
    the slice is empty. Never raises, never awaits the shadow."""
    if shadow is None or not sliced or not shadow_enabled_for(options.get("analyst_id")):
        return []
    try:
        inp = ShadowInput(
            analyst_id=str(options.get("analyst_id")), target_id=options.get("target_id"),
            run_id=str(options["run_id"]) if options.get("run_id") else None, user_prompt=user_prompt,
            sliced=list(sliced), citation_index=dict(citation_index), derived_from=list(derived_from),
            coverage=options.get("slice_coverage"), window_hours=options.get("slice_window_hours"),
            run_at=datetime.now(timezone.utc), bounded_question=shadow.bounded_question,
        )
        task = asyncio.get_running_loop().create_task(_shadow_task(shadow, inp))
    except Exception as exc:  # noqa: BLE001
        return [{"phase": "shadow", "kind": "desk_rows_v2", "launched": False, "reason": type(exc).__name__}]
    _TASKS.add(task)
    task.add_done_callback(_TASKS.discard)
    return [{"phase": "shadow", "kind": "desk_rows_v2", "launched": True, "contract": CONTRACT_VERSION}]


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "LEDGER_TABLE",
    "QUESTION_TOKEN",
    "REPAIR_THINKING",
    "SEAT_ID",
    "SHADOW_ENV",
    "SYNTHESIS_THINKING",
    "VERIFIER_THINKING",
    "DeskRowsShadow",
    "ShadowInput",
    "ShadowPrompts",
    "ShadowRecord",
    "launch_shadow",
    "rows_system_prompt",
    "run_contract_v2",
    "shadow_analysts",
    "shadow_enabled_for",
]

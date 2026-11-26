# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult TURN CONTRACT — what an answer must do, enforced in code.

Written against a failure seen in review: a
record-only question whose answer chose its own window, gave probabilities
about proxy events instead of causes, and published sentences resting on no
row while its judge's list of unsupported sentences changed nothing.

``run_method`` calls three hooks; everything else lives in the leaves:

1. :func:`open_turn` — before the first round. Resolves the WINDOW in force
   (``consult_window``), the GROUNDING MODE (``consult_grounding``), runs the
   deterministic COVERAGE read (``consult_coverage``) and returns the
   ``TURN CONTRACT`` block that leads the model's first user turn.
2. :func:`settle_answer` — after synthesis, before the census, the mint and
   the judge. Strips the model's ```contract block, checks the typed
   HYPOTHESES (``consult_hypotheses``; one bounded repair, then
   non-conforming), compares the citation span with the window, and assembles
   the answer: the code-built window lines (and the cost-stop line,
   ``consult_turn_cost``), the model's prose, the rendered explanations, the
   code-built coverage block.
3. :func:`enforce_after_judge` — after the settle-point judge. Applies the
   grounding rules to the MODEL's prose only (the code-built blocks are never
   cut), uses the judge's unsupported list, appends the accounting block, and
   chains the verification digest (``verification.grounding``) so the front
   door can prove the persisted text is the judged text after a recorded,
   deterministic edit.

ARMED WHEN. ``deps.turn_contract`` is True (the production deps builder sets
it; hand-built deps leave it off and the turn is byte-identical to before) and
the row is not a bake-off question (the bake-off's conditions are
pre-registered — ``consult_bakeoff``). Unarmed, a ```contract block the model
wrote anyway is still parsed and rendered (rule B needs no reads), and a
cost-stop line is still printed.

Everything the contract computed rides on ``data["contract"]``
(``consult_contract/1``), which the front door lifts onto the response and the
turn's response bag.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping, Sequence

from .. import consult_on_screen as _os
from . import consult_coverage as _cov
from . import consult_grounding as _gr
from . import consult_hypotheses as _hy
from . import consult_window as _win
from .consult_turn_cost import stop_note

logger = logging.getLogger(__name__)

__all__ = [
    "CONTRACT_SCHEMA",
    "TurnContract",
    "enforce_after_judge",
    "open_turn",
    "settle_answer",
]

CONTRACT_SCHEMA = "consult_contract/1"
_BAKEOFF_METER_KEY = "bakeoff_meter"  # consult_bakeoff.METER_KEY (no import cycle)

Record = Callable[[dict[str, Any]], Awaitable[None]]
LlmCall = Callable[[str, list[dict[str, Any]]], Awaitable[str]]


@dataclass
class TurnContract:
    window: _win.TurnWindow
    grounding_mode: str
    grounding_source: str
    expectations: bool
    coverage: dict[str, Any]
    target_ids: list[str]
    pool: Any = None
    prompt_block: str = ""
    model_body: str | None = None
    coverage_block: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    on_screen: dict[str, Any] | None = None
    #: Q-2 — set on a RECOVERED turn (``synthesize_from``): where its window,
    #: grounding mode and coverage came from (see :func:`open_recovery_turn`).
    recovery: dict[str, Any] | None = None


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _grounding(first: Mapping[str, Any], question: str) -> tuple[str, str]:
    console = str(first.get("grounding_mode") or "").strip().lower()
    if console == _gr.GROUNDING_RECORD_ONLY:
        return _gr.GROUNDING_RECORD_ONLY, "console"
    if _gr.grounding_mode_from_words(question):
        return _gr.GROUNDING_RECORD_ONLY, "user_words"
    return _gr.GROUNDING_DEFAULT, "default"


def _targets(first: Mapping[str, Any]) -> list[str]:
    out: list[str] = []
    for pin in first.get("pinned_context") or []:
        if isinstance(pin, Mapping) and pin.get("kind") == "target" and pin.get("id"):
            out.append(str(pin["id"]))
    point = _os.point_of(first.get("on_screen"))
    if point and point["kind"] == "target" and point["id"] not in out:
        out.append(point["id"])
    return out


def lead_user_prompt(user_prompt: str, contract: "TurnContract | None", first: Mapping[str, Any]) -> str:
    """The first user turn: the TURN CONTRACT, then ON SCREEN, then the question."""
    blocks = [contract.prompt_block if contract else "", _os.render_on_screen_block(first.get("on_screen"))]
    lead = "\n\n".join(b for b in blocks if b)
    return f"{lead}\n\n{user_prompt}" if lead else user_prompt


def _point_line(first: Mapping[str, Any]) -> str:
    point = _os.point_of(first.get("on_screen"))
    if not point:
        return ""
    name = f"\"{point['label']}\" " if point["label"] else ""
    return f"> **Point (from the console):** {name}({point['kind']} {point['id']})."


def _quoted(text: Any) -> str:
    """Words of the person asking as a JSON string, so a quote mark or a newline in
    them cannot break the block's frame."""
    return json.dumps(str(text or ""), ensure_ascii=False)


def _prompt_block(c: TurnContract) -> str:
    w = c.window
    source = {
        _win.SOURCE_USER_WORDS: f"taken from the words of the person asking: {_quoted(w.basis)}",
        _win.SOURCE_CONSOLE: f"the console's pinned window ({w.basis})",
        _win.SOURCE_DEFAULT: f"the declared default ({w.basis}); the person asking stated none"
        + (" — the question says 'this window' but no console window arrived" if w.deictic else ""),
    }[w.source]
    grounding = (
        "record_only — every sentence about the world must cite, in square brackets, a row a "
        "tool returned; the platform removes fact sentences without a resolving row after you "
        "answer, labels judgments without one as your inference, and "
        + ("moves general expectations (the person asking asked for them) under a 'not from the record' heading."
           if c.expectations else "removes general-knowledge filler.")
        if c.grounding_mode == _gr.GROUNDING_RECORD_ONLY
        else "default — every sentence that states a fact cites the id of a row a tool returned."
    )
    extra = ""
    if w.reading:
        extra += f"- The window words of the person asking were read as: {w.reading}.\n"
    if w.anchor and not w.anchor.get("resolved"):
        extra += (
            f"- The question asks for an event-anchored window ({_quoted(w.anchor.get('words'))}); "
            "the platform did NOT resolve it to a date and you must not date the event yourself: "
            "use the window in force and say plainly that the anchored window was not resolved.\n"
        )
    return (
        "TURN CONTRACT (set by the platform for this turn):\n"
        f"- Window in force: {w.start:%Y-%m-%d %H:%M} → {w.end:%Y-%m-%d %H:%M} UTC, {source}.\n"
        + extra +
        f"- Record {_cov.render_coverage_for_prompt(c.coverage)}\n"
        f"- Grounding: {grounding}\n"
        "- End the answer with the ```contract block described in your instructions."
    )


async def open_turn(
    *, first: Mapping[str, Any], question: str, deps: Any, arm: Any, record: Record,
) -> TurnContract | None:
    """The turn's contract, or ``None`` when it is not armed (see the module doc)."""
    if not getattr(deps, "turn_contract", False) or arm is not None or first.get(_BAKEOFF_METER_KEY):
        return None
    pool = getattr(deps, "contract_pool", None) or getattr(deps, "pg_pool", None)
    # The console's pinned window: the explicit ``window`` field, else the
    # window ``on_screen`` reports (U-6) — the same span the console shows.
    console_window = first.get("window") or _os.window_of(first.get("on_screen"))
    window = _win.resolve_window(question, console_window)
    mode, source = _grounding(first, question)
    targets = _targets(first)
    coverage = await _cov.coverage_by_day(pool, window, target_ids=targets)
    contract = TurnContract(
        window=window, grounding_mode=mode, grounding_source=source,
        expectations=_gr.expectations_requested(question), coverage=coverage,
        target_ids=targets, pool=pool,
        on_screen=dict(first["on_screen"]) if isinstance(first.get("on_screen"), Mapping) else None,
    )
    contract.prompt_block = _prompt_block(contract)
    await record({
        "phase": "plan", "kind": "turn_contract",
        "window": window.as_dict(), "grounding_mode": mode, "grounding_source": source,
        "coverage_state": coverage.get("state"),
        "zero_days": coverage.get("zero_days"), "sparse_days": coverage.get("sparse_days"),
    })
    return contract


async def open_recovery_turn(
    *, recovery: Mapping[str, Any], deps: Any, record: Record,
) -> TurnContract | None:
    """The contract of a RECOVERED turn (``synthesize_from``, Q-2).

    A recovery writes a NEW answer over evidence an earlier turn gathered, so
    the new answer gets the same contract a normal turn's does — the window
    lines, the coverage block, the reach of ITS citations, its typed
    explanations, the grounding pass over ITS prose, the judge's list — built
    by the same functions (:func:`settle_answer`, :func:`enforce_after_judge`).
    What it does not do is re-resolve the turn's terms:

    * the WINDOW is the original turn's stored window (``contract.window``):
      the evidence was gathered under it, and re-reading "the last 48 hours"
      at recovery time would move it. A turn that stored none (before Q-1)
      has its question re-read at the original turn's instant
      (``asked_at``), else now — and ``recovery.window_from`` says which;
    * the GROUNDING MODE is the original's, else read from the question;
    * the COVERAGE is the original's measured read, else measured now over
      that window;
    * ON SCREEN is the original's (context, never evidence).

    ``None`` — no contract — when the deps are unarmed or the original turn
    stored an UNARMED contract (a bake-off row keeps its pre-registered
    conditions on a recovery too).
    """
    if not getattr(deps, "turn_contract", False):
        return None
    original = recovery.get("contract") if isinstance(recovery.get("contract"), Mapping) else None
    if original is not None and original.get("armed") is False:
        return None
    original = original or {}
    question = str(recovery.get("question") or "")
    pool = getattr(deps, "contract_pool", None) or getattr(deps, "pg_pool", None)
    window = _win.TurnWindow.from_dict(original.get("window"))
    window_from = "original_turn"
    if window is None:
        asked = _win.parse_instant(recovery.get("asked_at"))
        window = _win.resolve_window(question, None, now=asked)
        window_from = "question_at_original_turn" if asked else "question_at_recovery"
    mode = str(original.get("grounding_mode") or "")
    if mode in (_gr.GROUNDING_RECORD_ONLY, _gr.GROUNDING_DEFAULT):
        g_source = str(original.get("grounding_source") or "original_turn")
    else:
        mode, g_source = _grounding({}, question)
    coverage = original.get("coverage")
    targets = list(((coverage or {}).get("subject_filter") or {}).get("target_ids") or []) \
        if isinstance(coverage, Mapping) else []
    coverage_from = "original_turn"
    if not (isinstance(coverage, Mapping) and coverage.get("state") == "measured"):
        coverage = await _cov.coverage_by_day(pool, window, target_ids=targets)
        coverage_from = "measured_at_recovery"
    on_screen = original.get("on_screen") if isinstance(original.get("on_screen"), Mapping) else None
    contract = TurnContract(
        window=window, grounding_mode=mode, grounding_source=g_source,
        expectations=_gr.expectations_requested(question), coverage=dict(coverage),
        target_ids=targets, pool=pool, on_screen=dict(on_screen) if on_screen else None,
        recovery={
            "window_from": window_from,
            "coverage_from": coverage_from,
            "original_turn_id": recovery.get("turn_id"),
            **({"original_cost_stop": original["cost_stop"]} if original.get("cost_stop") else {}),
        },
    )
    await record({
        "phase": "plan", "kind": "turn_contract", "recovery": True,
        "window": window.as_dict(), "grounding_mode": mode, "grounding_source": g_source,
        "coverage_state": contract.coverage.get("state"), **contract.recovery,
    })
    return contract


def _parse_repair(reply: str) -> tuple[dict[str, Any] | None, str | None]:
    _rest, parsed, err = _hy.extract_contract_block(reply or "")
    if parsed is not None or err is not None:
        return parsed, err
    import json

    text = (reply or "").strip()
    try:
        value = json.loads(text[text.index("{"): text.rindex("}") + 1])
    except (ValueError, IndexError):
        return None, "repair reply carried no contract block"
    return (value, None) if isinstance(value, dict) else (None, "repair reply is not an object")


async def _settle_hypotheses(
    block: dict[str, Any] | None, err: str | None, *, held: Sequence[str],
    llm_call: LlmCall | None, spend: Any, record: Record, raw_block: str | None = None,
) -> tuple[dict[str, Any] | None, list[str], dict[str, Any]]:
    problems = [err] if err else _hy.check_hypotheses(block, held_refs=held)
    status: dict[str, Any] = {"repair": "not_needed"}
    if not problems:
        return block, [], {**status, "conforming": True}
    can_spend = spend is None or spend.exhausted_reason() is None
    if llm_call is None or not can_spend:
        status["repair"] = "skipped_no_route" if llm_call is None else "skipped_for_cost"
    else:
        system, messages = _hy.repair_messages(block, err, problems, raw_block=raw_block)
        try:
            reply = await llm_call(system, messages)
            repaired, rerr = _parse_repair(reply)
            again = [rerr] if rerr else _hy.check_hypotheses(repaired, held_refs=held)
            status["repair"] = "repaired" if not again else "still_failing"
            if repaired is not None:
                block = repaired
            problems = again
        except Exception as exc:  # noqa: BLE001 — a failed repair marks, never loses, the answer
            logger.warning("consult.contract.repair_failed err=%s", exc)
            status["repair"] = f"failed: {type(exc).__name__}"
    await record({"phase": "reflect", "kind": "hypotheses_repair", **status,
                  "problems": list(problems)[:12]})
    return block, problems, {**status, "conforming": not problems, "problems": list(problems)}


_WINDOW_FROM = {
    "original_turn": "the window the original turn resolved",
    "question_at_original_turn": "the question re-read at the original turn's time (it stored no window)",
    "question_at_recovery": "the question re-read now (the original turn stored no window or time)",
}


def _recovery_line(rec: Mapping[str, Any]) -> str:
    """The recovered answer's statement of where its contract terms came from."""
    line = (
        "> **Recovered answer:** written over the evidence an earlier turn gathered; "
        f"window: {_WINDOW_FROM.get(str(rec.get('window_from')), rec.get('window_from'))}; "
        "coverage: " + ("the original turn's read" if rec.get("coverage_from") == "original_turn"
                        else "measured at recovery") + "."
    )
    if rec.get("original_cost_stop"):
        line += " The original turn stopped drilling for cost, so this evidence is what it had gathered by then."
    return line


async def settle_answer(
    consult: Any, contract: TurnContract | None, *, steps: list[dict[str, Any]],
    spend: Any, llm_call: LlmCall | None, record: Record, turn_ref: str = "",
    first: Mapping[str, Any] | None = None,
) -> Any:
    """Rule A (window lines, coverage block, narrowing) + rule B (typed explanations)."""
    first = first or {}
    on_screen = first.get("on_screen") if isinstance(first.get("on_screen"), Mapping) else None
    answer = str(consult.answer or "")
    note = stop_note(steps, spend) if spend is not None else None
    body, block, err = _hy.extract_contract_block(answer)
    if contract is None and block is None and err is None and note is None and on_screen is None:
        return consult
    held = [str(r).lower() for r in consult.cited_substrate_refs]
    data: dict[str, Any] = {"schema": CONTRACT_SCHEMA, "armed": contract is not None}
    if on_screen is not None:
        # Stored on the turn (the response bag's ``contract``): what was on
        # screen when this was asked. Context only — never a citation.
        data["on_screen"] = dict(on_screen)
    hyp_section = ""
    if block is not None or err is not None:
        has_hyp = err is not None or bool((block or {}).get("hypotheses"))
        if has_hyp:
            block, problems, status = await _settle_hypotheses(
                block, err, held=held, llm_call=llm_call, spend=spend, record=record,
                raw_block=_hy.contract_block_text(answer) if block is None else None,
            )
            if block is not None and block.get("hypotheses"):
                hyp_section = _hy.render_hypotheses(block, problems=problems)
            elif problems:
                hyp_section = (
                    "## Competing explanations\n\n**Non-conforming:** the answer's explanation "
                    "block could not be read after one repair — " + "; ".join(problems) + "."
                )
            data["hypotheses"] = {
                **status,
                "items": (block or {}).get("hypotheses"),
                "something_else_p": (block or {}).get("something_else_p"),
                "forecast_proposals": _hy.forecast_proposals(
                    block if not problems else None,
                    turn_ref=turn_ref or "turn",
                ),
            }
        data["declared"] = {
            k: (block or {}).get(k) for k in ("window_used", "wider_reach")
        }
    if note:
        data["cost_stop"] = note
    head: list[str] = [note] if note else []
    if contract is not None:
        dates = await _cov.cited_row_dates(contract.pool, held) if contract.pool else None
        reach = _win.assess_reach(
            contract.window, (dates or {}).values(),
            rows_by_day=_cov.rows_by_day(contract.coverage),
            declared=(block or {}),
        ) if dates is not None else None
        lines = _win.render_window_lines(contract.window, reach)
        point = _point_line(first)
        if contract.recovery is not None:
            lines += "\n" + _recovery_line(contract.recovery)
        head.insert(0, f"{lines}\n{point}" if point else lines)
        contract.coverage_block = _cov.render_coverage_block(contract.coverage)
        data.update({
            "window": contract.window.as_dict(),
            "reach": reach.as_dict() if reach else {"state": "unmeasured"},
            "coverage": contract.coverage,
            "grounding_mode": contract.grounding_mode,
            "grounding_source": contract.grounding_source,
            **({"recovery": dict(contract.recovery)} if contract.recovery is not None else {}),
        })
        contract.model_body = body.strip()
        contract.data = data
        await record({"phase": "reflect", "kind": "window_reach",
                      **(reach.as_dict() if reach else {"state": "unmeasured"})})
    parts = [*head, body.strip(), hyp_section, contract.coverage_block if contract else ""]
    new_answer = "\n\n".join(p for p in parts if p)[:65000]
    return consult.model_copy(update={
        "answer": new_answer, "data": {**consult.data, "contract": data},
    })


async def enforce_after_judge(result: Any, contract: TurnContract | None, *, record: Record) -> Any:
    """Rules C and D over the model's prose, after the settle-point judge."""
    consult = getattr(result, "consult_response", None)
    if contract is None or consult is None or not contract.model_body:
        return result
    from ..registry.consult_turn_stamps import settle_digest

    answer = str(consult.answer or "")
    verification = dict(consult.data.get("verification") or {})
    cited = [str(r).lower() for r in consult.cited_substrate_refs]
    if contract.model_body not in answer:
        acct = {"schema": _gr.GROUNDING_SCHEMA, "mode": contract.grounding_mode,
                "applied": False, "not_applied_reason": "answer_moved_after_settle"}
        new_answer = answer
    else:
        new_body, acct = await _gr.enforce(
            contract.model_body, mode=contract.grounding_mode, on_screen=contract.on_screen,
            expectations=contract.expectations, cited_refs=cited,
            verification=verification, pool=contract.pool,
        )
        new_answer = answer.replace(contract.model_body, new_body, 1)
        if acct.get("applied"):
            block = _gr.render_accounting(acct)
            anchor = contract.coverage_block
            new_answer = (
                new_answer.replace(anchor, anchor + "\n\n" + block, 1)
                if anchor and anchor in new_answer else new_answer.rstrip() + "\n\n" + block
            )
    new_answer = new_answer[:65000]
    grounding_stamp = {
        "schema": _gr.GROUNDING_SCHEMA,
        "mode": contract.grounding_mode,
        "applied": bool(acct.get("applied")),
        "input_sha256": settle_digest(answer, cited),
        "output_sha256": settle_digest(new_answer, cited),
        "counts": acct.get("counts"),
        "verifier_items": len((acct.get("verifier") or {}).get("items") or []),
    }
    if verification:
        verification["grounding"] = grounding_stamp
    data = {**consult.data, "contract": {**(consult.data.get("contract") or {}), "grounding": acct}}
    if verification:
        data["verification"] = verification
    await record({"phase": "verify", "kind": "grounding", "mode": contract.grounding_mode,
                  "applied": grounding_stamp["applied"], "counts": acct.get("counts"),
                  "numbers_unsupported": acct.get("numbers_unsupported")})
    consult = consult.model_copy(update={"answer": new_answer, "data": data})
    finding = getattr(result, "finding", None)
    if isinstance(getattr(finding, "data", None), dict) and "consult_response" in finding.data:
        finding = finding.model_copy(update={
            "body": new_answer or finding.body,
            "data": {**finding.data, "consult_response": consult.model_dump(mode="json")},
        })
    import dataclasses

    return dataclasses.replace(result, consult_response=consult, finding=finding)

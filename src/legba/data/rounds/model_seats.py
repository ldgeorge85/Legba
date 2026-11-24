# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Model and judge seats: the seat provider that calls a model (plan P4 §3,
lane G-5).

:class:`ModelSeats` is a :data:`~.seats.SeatFn` for ``kind: model`` and
``kind: judge`` seats. What it guarantees, per attempt:

* **The prompt is the card and the handed inputs, nothing else.**
  :func:`render_prompt` reads only the :class:`~.seats.SeatCall` the
  interpreter built — the card text, the inputs the store released
  (:meth:`~.visibility.Visibility.inputs_for` decided them), the item,
  candidates, turn and iteration — plus the row type's output contract. It
  has no store, no run folder and no record in reach, so it cannot hand a
  seat anything the store withheld.
* **One clean context per attempt.** A fresh message list; no earlier
  attempt, sibling or seat is in it. A tool round appends only that
  attempt's own tool calls and their (cut, filtered) results.
* **Typed output.** The reply's ONE JSON object (full-width brackets folded
  with the house fold, ``statement_seats.parse_reply_object``) is cut to the
  declared row type's fields (keys the type does not declare are dropped and
  listed on the receipt) and handed back; the interpreter validates it with
  :func:`~.rows.parse_row`. No object is a :class:`~.seats.SeatFailure` with
  the raw text kept — never a silent empty row.
* **The house sampling.** ``temperature`` is
  :data:`legba.shared.sampling.HOUSE_TEMPERATURE`; no ``max_tokens`` is
  passed (the handler sends none on the core plane).
* **Thinking as declared, decided in one place.** The seat's ``thinking``
  (``on``/``off``/absent) is parsed by ``thinking_choice.parse_thinking`` and
  passed as ``thinking=`` to ``chat_complete``; the alias is resolved by
  ``stack/llm/thinking.resolve_call_model`` (T-1) and a thinking-specific
  failure falls back once to the plain alias there (T-2). No alias string is
  named here.
* **The plane is the seat's.** ``seat.plane`` picks the handler; a plane the
  host did not build is a failure, never a substitute.
* **Scheduled, gated.** Calls run in the ``scheduled`` priority class of the
  process-wide call gate (``call_gate``; the handler acquires it); a seat
  call made inside an interactive context is refused. ``max_in_flight``
  bounds this provider's own concurrent calls on every plane.
* **Receipts.** ``SeatReply.meta`` carries, per wire call: the component,
  the model the call named and the model the response reported, thinking,
  a thinking fallback, prompt / completion / reasoning tokens, duration,
  gate wait, tool calls; and the rendered prompt's sha256 and size.

Model calls are made through an injected handler map and an injected
coroutine runner; this module opens no connection of its own.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import threading
import time
import types
import typing
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal, Union

from pydantic import BaseModel

from . import judge_render as jr
from .rows import ROW_TYPES, VERDICT_REASONS
from .seats import SeatCall, SeatFailure, SeatReply

#: The row-type notes a contract adds under the shape (what each common
#: field means to the gates that will check it).
_CITATION_RULES = (
    "Every `support` entry cites one row you were handed (or that a tool returned) "
    "by its exact `ref_id`; `quote` copies at least three consecutive words of that "
    "row's text exactly as written; `kind` is the row's kind (`signal` for signal "
    "rows, `observation` for series rows, `row` for another seat's output); `role` "
    "is `evidence`, `contradicts` or `context`.",
    "A sentence states one figure at most, and any number or date it states appears "
    "in the quote it cites.",
    "Never invent an id. If the rows do not support a sentence, leave it out.",
)


#: Said to a seat after its last tool round (the next call offers no tools).
FINAL_ROUND_NOTE = ("The tool rounds are over; no more tool calls will run. Reply now with the ONE "
                    "JSON object your output section asks for, and nothing else.")


# ---------------------------------------------------------------------------
# The output contract: a compact shape rendered from the row type's model
# ---------------------------------------------------------------------------


def _shape(tp: Any, depth: int = 0) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin is Literal:
        return " | ".join(json.dumps(a) for a in args)
    if origin in (Union, types.UnionType):
        inner = [a for a in args if a is not type(None)]
        shaped = _shape(inner[0], depth) if len(inner) == 1 else " | ".join(
            str(_shape(a, depth)) for a in inner)
        return shaped if len(inner) == 1 and not isinstance(shaped, str) else f"{shaped} (optional)"
    if origin in (tuple, list):
        item = args[0] if args else Any
        return [_shape(item, depth + 1)]
    if origin is dict:
        val = args[1] if len(args) > 1 else Any
        return {"<key>": _shape(val, depth + 1)}
    if isinstance(tp, type) and issubclass(tp, BaseModel):
        if depth > 4:
            return "{…}"
        return {name: _shape(f.annotation, depth + 1) for name, f in tp.model_fields.items()}
    if tp is str:
        return "string"
    if tp is bool:
        return "true | false"
    if tp is int:
        return "integer"
    if tp is float:
        return "number"
    return "any"


def _closed_fields(model: Any, out: dict[str, list[str]] | None = None,
                   depth: int = 0) -> dict[str, list[str]]:
    """Every field (at any depth) whose type is a closed list, with its values."""
    out = {} if out is None else out
    if depth > 4 or not (isinstance(model, type) and issubclass(model, BaseModel)):
        return out
    for name, f in model.model_fields.items():
        stack = [f.annotation]
        while stack:
            t = stack.pop()
            origin = typing.get_origin(t)
            if origin is Literal:
                out.setdefault(name, [str(a) for a in typing.get_args(t)])
            elif origin in (Union, types.UnionType, tuple, list, dict):
                stack.extend(a for a in typing.get_args(t) if a is not type(None) and a is not Ellipsis)
            elif isinstance(t, type) and issubclass(t, BaseModel):
                _closed_fields(t, out, depth + 1)
    return out


def output_contract(row_type: str) -> str:
    """The row type's shape, as a JSON-like skeleton, and the rules the gates
    apply. Pure: depends on the row type only."""
    model = ROW_TYPES.get(row_type)
    if model is None:
        raise SeatFailure(f"row type {row_type!r} is not registered")
    required = sorted(n for n, f in model.model_fields.items() if f.is_required())
    lines = [
        f"## Output: `{row_type}`",
        "Reply with ONE JSON object of this shape and nothing else — no prose before or "
        "after it, no markdown fence around it. Fields not shown are not allowed. The shape "
        "says what TYPE each field takes; do not copy its words as values, and leave out an "
        "optional field you have nothing for.",
        "```json",
        json.dumps(_shape(model), indent=1, ensure_ascii=False),
        "```",
        f"Required: {', '.join(required) if required else '(none)'}.",
    ]
    closed = _closed_fields(model)
    if closed:
        lines.append("Fields with a closed list take exactly one of its values, spelled as shown: "
                     + "; ".join(f"`{k}`: {', '.join(v)}" for k, v in closed.items()) + ".")
    if "support" in json.dumps(_shape(model)):
        lines.extend(f"- {r}" for r in _CITATION_RULES)
    if row_type == "verdict/1":
        lines.append(f"- `reason` is one of: {', '.join(VERDICT_REASONS)}. Give one verdict "
                     "per candidate you were asked to rule on, by its exact candidate id.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Rendering: the card + the handed inputs, nothing else
# ---------------------------------------------------------------------------


def _json(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, indent=1, sort_keys=True, default=str)


def render_prompt(call: SeatCall, *, tool_names: Sequence[str] = ()) -> tuple[str, str]:
    """``(system, user)`` for one seat call, from the call alone."""
    if not call.card_text:
        raise SeatFailure(f"seat {call.seat_id!r} has no card text to render")
    system = [call.card_text.strip(), "", output_contract(call.row_type)]
    if tool_names:
        system += ["", "## Tools",
                   "You may call the read tools offered (" + ", ".join(tool_names) + ") to read "
                   "the record as it stood at the as-of time. Rows a tool returns may be cited "
                   "by their id exactly like handed rows. Call tools first; then reply with the "
                   "JSON object only."]
    user = ["# What you were handed"]
    for h in call.inputs:
        if h.read.startswith(("input.", "item.")) or h.ref_id.startswith(("input.", "item.")):
            user.append(f"\n## {h.ref_id.split('.', 1)[-1]}\n{_json(h.payload)}")
            continue
        if h.absent:
            user.append(f"\n## {h.read} — absent ({h.absent})")
            continue
        head = f"\n## {h.read} (from {h.seat or 'the round'}; row type {h.row_type}; id {h.ref_id})"
        user.append(f"{head}\n```json\n{_json(h.payload)}\n```")
    if call.item is not None:
        user.append(f"\n## The item you work on\n{_json(call.item)}")
    if call.candidate is not None:
        user.append(f"\n## The candidate you rule on\n{call.candidate}")
    if call.candidates:
        user.append("\n## The candidates you rule on\n" + "\n".join(call.candidates))
    if call.turn is not None:
        user.append(f"\n## Turn\n{call.turn}")
    if call.iteration is not None:
        user.append(f"\n## Pass\n{call.iteration}")
    if call.repair:
        user.append(render_repair(call.repair))
    return "\n".join(system), "\n".join(user)


def render_repair(note: Mapping[str, Any]) -> str:
    """The ``repair: reasons`` section: why the previous attempt was refused,
    sentence by sentence, and that attempt's own reply."""
    lines = [f"\n## Your previous reply (attempt {note.get('attempt')}) was refused"]
    if note.get("kind") == "parse":
        lines.append(f"It could not be read: {note.get('error')}")
        lines.append("Reply with the ONE JSON object only, every field as the output shape says, "
                     "every closed-list field spelled exactly as listed.")
        return "\n".join(lines)
    lines.append("The code gates refused these sentences. Fix each one so it passes — quote the "
                 "figure or date from the row, or drop the figure, or drop the sentence — and keep "
                 "every sentence that passed as it was. Do not add new claims.")
    for f in note.get("failed") or ():
        where = f"sentence `{f['sentence']}`" if f.get("sentence") else "the reply"
        line = f"- {where}: {f.get('gate')} refused it ({f.get('reason')})"
        if f.get("missing"):
            line += f"; not in the quote: {', '.join(map(str, f['missing']))}"
        if f.get("not_handed"):
            line += f"; not handed: {', '.join(map(str, f['not_handed']))}"
        if f.get("closest_passage"):
            line += f"; the row's own words nearest your quote: \"{f['closest_passage']}\""
        if f.get("fix"):
            line += f". Rule: {f['fix']}"
        lines.append(line)
        if f.get("text"):
            lines.append(f"  text: {f['text']}")
    if note.get("previous") is not None:
        lines += ["Your refused reply:", "```json", _json(note["previous"]), "```"]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Reading the reply into the declared row type
# ---------------------------------------------------------------------------


def _prune(value: Any, tp: Any, path: str, dropped: list[str]) -> Any:
    origin = typing.get_origin(tp)
    args = typing.get_args(tp)
    if origin in (Union, types.UnionType):
        models = [a for a in args if isinstance(a, type) and issubclass(a, BaseModel)]
        if models and isinstance(value, dict):
            return _prune(value, models[0], path, dropped)
        return value
    if origin in (tuple, list) and isinstance(value, list) and args:
        return [_prune(v, args[0], f"{path}[{i}]", dropped) for i, v in enumerate(value)]
    if origin is dict and isinstance(value, dict) and len(args) > 1:
        return {k: _prune(v, args[1], f"{path}.{k}", dropped) for k, v in value.items()}
    if isinstance(tp, type) and issubclass(tp, BaseModel) and isinstance(value, dict):
        out = {}
        for k, v in value.items():
            f = tp.model_fields.get(k)
            if f is None:
                dropped.append(f"{path}.{k}".lstrip("."))
                continue
            out[k] = _prune(v, f.annotation, f"{path}.{k}", dropped)
        return out
    return value


def parse_reply(row_type: str, content: str | None) -> tuple[dict[str, Any], list[str]]:
    """The reply's JSON object cut to ``row_type``'s declared fields, and the
    paths of the keys dropped. Raises :class:`SeatFailure` (raw kept) when the
    reply carries no JSON object. Validation is the interpreter's
    (``parse_row``); this only reads and cuts."""
    from ..statement_seats import parse_reply_object

    model = ROW_TYPES.get(row_type)
    if model is None:
        raise SeatFailure(f"row type {row_type!r} is not registered", content)
    obj = parse_reply_object(content)
    if obj is None:
        raise SeatFailure("unparseable_reply: no JSON object", content)
    fields = set(model.model_fields)
    if not (set(obj) & fields) and len(obj) == 1:
        inner = next(iter(obj.values()))
        if isinstance(inner, dict) and set(inner) & fields:
            obj = inner  # one wrapper key around the object asked for
    dropped: list[str] = []
    return _prune(obj, model, "", dropped), dropped


# ---------------------------------------------------------------------------
# A loop thread: the synchronous interpreter calls async handlers through it
# ---------------------------------------------------------------------------


class LoopThread:
    """One event loop on a daemon thread; ``run(coro)`` blocks for its result.

    The interpreter is synchronous and the LLM handlers are async; every seat
    call (from any interpreter thread) runs on this ONE loop, so a handler's
    client and the provider's in-flight bound live on one loop."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="rounds-seat-loop",
                                        daemon=True)
        self._thread.start()

    def run(self, coro: Awaitable[Any]) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result()  # type: ignore[arg-type]

    def close(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=10)


# ---------------------------------------------------------------------------
# The provider
# ---------------------------------------------------------------------------


class CallCapReached(SeatFailure):
    """The host's hard cap on model calls is spent; refused before sending."""


@dataclass
class ModelSeats:
    """The model/judge :data:`~.seats.SeatFn`.

    * ``planes`` — stack-component id → a configured LLM handler (anything
      with ``async chat_complete(messages, **kw)``);
    * ``run`` — runs a coroutine to completion from the calling thread
      (:meth:`LoopThread.run`);
    * ``tools`` — the run's tool binding (:class:`~.tool_binding.RoundTools`),
      or ``None`` (a seat that declares tools then fails: unbound tools are
      not silently dropped);
    * ``max_tool_rounds`` — tool rounds per attempt before the seat must answer;
    * ``max_in_flight`` — this provider's concurrent wire calls, every plane
      (capped at 4), or ``limiter`` — one semaphore shared by several
      providers (several runs on one loop);
    * ``take_call`` — the host's hard call cap: called before every wire call,
      ``False`` refuses it (:class:`CallCapReached`).
    """

    planes: Mapping[str, Any]
    run: Callable[[Awaitable[Any]], Any]
    tools: Any = None
    max_tool_rounds: int = 2
    max_in_flight: int = 4
    take_call: Callable[[], bool] | None = None
    limiter: asyncio.Semaphore | None = None
    keep_prompts: Callable[[str, str, str, str], None] | None = None
    _sem: asyncio.Semaphore | None = field(default=None, init=False, repr=False)

    def __call__(self, call: SeatCall) -> SeatReply:
        if call.seat.kind not in ("model", "judge"):
            raise SeatFailure(f"ModelSeats does not run {call.seat.kind} seats")
        return self.run(self._attempt(call))

    # -- one attempt ---------------------------------------------------------

    def _handler(self, call: SeatCall) -> Any:
        plane = call.seat.plane
        if not plane or plane not in self.planes:
            raise SeatFailure(f"plane_not_built: {plane!r}")
        return self.planes[plane]

    async def _attempt(self, call: SeatCall) -> SeatReply:
        from legba.shared.sampling import HOUSE_TEMPERATURE
        from legba.shared.thinking_choice import parse_thinking

        from ..stack.llm import call_gate
        from ..stack.llm import tool_rounds as tr

        if call_gate.current_priority() != call_gate.SCHEDULED:
            raise SeatFailure("seat calls run in the scheduled class; refused in an interactive context")
        if self._sem is None:
            self._sem = self.limiter or asyncio.Semaphore(max(1, min(int(self.max_in_flight), 4)))
        llm = self._handler(call)
        thinking = parse_thinking(call.seat.thinking)
        names: tuple[str, ...] = ()
        if call.seat.tools:
            if self.tools is None:
                raise SeatFailure(f"tools_not_bound: {list(call.seat.tools)}")
            names = self.tools.tools_for(call.seat)
        view = None
        if call.row_type == jr.JUDGE_ROW_TYPE:
            if not call.card_text:
                raise SeatFailure(f"seat {call.seat_id!r} has no card text to render")
            cands = call.candidates or ((call.candidate,) if call.candidate else ())
            system, user, view = jr.render(call.card_text, call.inputs, cands)
            if call.repair:
                user += "\n" + render_repair(call.repair)
        else:
            system, user = render_prompt(call, tool_names=names)
        prompt_sha = hashlib.sha256(f"{system}\n\x00\n{user}".encode()).hexdigest()
        if self.keep_prompts is not None:
            self.keep_prompts(prompt_sha, call.seat_id, system, user)
        if view is not None and getattr(llm, "subprovider", None) == "decide":
            return await self._decide_attempt(call, llm, view, prompt_sha)
        wire = tr.provider_for_subprovider(getattr(llm, "subprovider", None)) or tr.PROVIDER_OPENAI_COMPAT
        specs = tr.build_tool_specs(list(names)) if names else []
        wire_tools = tr.render_tools(wire, specs) if specs else None
        messages: list[dict[str, Any]] = [{"role": "user", "content": user}]
        calls: list[dict[str, Any]] = []
        tool_log: list[dict[str, Any]] = []
        tool_rows: list[dict[str, Any]] = []
        t0 = time.monotonic()
        response: Any = None
        for rnd in range(self.max_tool_rounds + 1):
            offer = wire_tools if rnd < self.max_tool_rounds else None
            response, receipt = await self._wire(llm, messages, system=system, thinking=thinking,
                                                 tools=offer, temperature=HOUSE_TEMPERATURE,
                                                 plane=call.seat.plane or "")
            got = tr.parse_tool_calls(wire, response) if offer else []
            receipt["tool_calls"] = [c.name for c in got]
            calls.append(receipt)
            if not got:
                break
            results = []
            for c in got:
                spec = next((s for s in specs if s.name == c.name), None)
                problem = tr.validate_args(spec, c.args) if spec is not None else None
                if problem:
                    out: Any = {"error": f"tool_blocked: {problem}"}
                    tool_log.append({"tool": c.name, "blocked": "invalid_arg"})
                else:
                    out, rows, entry = await self.tools.call(call, c.name, dict(c.args))
                    tool_rows.extend(rows)
                    tool_log.append(entry)
                results.append(out)
            messages = messages + [tr.assistant_tool_turn(wire, response, got)] \
                + tr.tool_result_messages(wire, got, results)
            if rnd + 1 >= self.max_tool_rounds:
                # the next call offers no tools: a seat that still tries one
                # returns nothing readable (lane G-6 runs) — say so
                messages.append({"role": "user", "content": FINAL_ROUND_NOTE})
        content = tr.visible_text(response)
        meta: dict[str, Any] = {
            "prompt_sha256": prompt_sha, "prompt_chars": len(system) + len(user),
            "system_chars": len(system), "user_chars": len(user),
            "thinking_declared": call.seat.thinking, "calls": calls,
            "tools_offered": list(names), "tool_calls": tool_log,
            "tool_rows": len(tool_rows), "attempt_s": round(time.monotonic() - t0, 3),
        }
        try:
            payload, dropped = parse_reply(call.row_type, content)
        except SeatFailure as exc:
            exc.raw = _with_meta(content, meta)
            raise
        if dropped:
            meta["dropped_keys"] = dropped[:50]
        if view is not None:
            payload, unmapped = jr.read_back(payload, view)
            meta["render"] = jr.RENDER_VERSION
            if unmapped:
                meta["unmapped"] = unmapped[:50]
        return SeatReply(payload, prose=None, raw=content, elapsed_s=meta["attempt_s"],
                         meta=meta, tool_rows=tuple(tool_rows), tools_bound=bool(names))

    async def _decide_attempt(self, call: SeatCall, llm: Any, view: Any, prompt_sha: str) -> SeatReply:
        """A ``verdict/2`` judge on a DECISION plane (``/decide``): one typed
        ``holds`` / ``fails`` question per sentence, one wire call, the
        verdict built in code (:func:`judge_render.verdict_from_answers`)."""
        from .. import run_accounting as ra

        state, questions = jr.decide_questions(call.card_text or "", view)
        if not questions:
            raise SeatFailure("decide: no sentences to check")
        if self.take_call is not None and not self.take_call():
            raise CallCapReached("call_cap_reached: the host's model-call cap is spent")
        assert self._sem is not None
        async with self._sem:
            token = ra.bind_run_accounting()
            t0 = time.monotonic()
            err: BaseException | None = None
            answers: Any = None
            try:
                answers = await llm.decide(state, questions, leg=f"round:{call.seat_id}",
                                           max_retries=0)
            except BaseException as exc:  # noqa: BLE001 - recorded, re-raised
                err = exc
            duration = time.monotonic() - t0
            accounts = ra.current_llm_calls()
            ra.reset_run_accounting(token)
        receipt = _call_receipt(call.seat.plane or "", None, duration, None, accounts)
        receipt["questions"] = len(questions)
        meta: dict[str, Any] = {"prompt_sha256": prompt_sha, "calls": [receipt], "render": jr.RENDER_VERSION,
                                "decide": True, "attempt_s": round(duration, 3)}
        if err is not None:
            receipt["error"] = f"{type(err).__name__}: {str(err)[:300]}"
            raise SeatFailure(f"call_failed: {type(err).__name__}", _dump({"call": receipt}))
        payload = jr.verdict_from_answers(view, answers)
        return SeatReply(payload, raw=jr.dumps(answers), elapsed_s=meta["attempt_s"], meta=meta)

    async def _wire(self, llm: Any, messages: list[dict[str, Any]], *, system: str,
                    thinking: bool | None, tools: Any, temperature: float,
                    plane: str) -> tuple[Any, dict[str, Any]]:
        from .. import run_accounting as ra

        if self.take_call is not None and not self.take_call():
            raise CallCapReached("call_cap_reached: the host's model-call cap is spent")
        kwargs: dict[str, Any] = {"system": system, "temperature": temperature}
        if thinking is not None:
            kwargs["thinking"] = thinking
        if tools:
            kwargs["tools"] = tools
        assert self._sem is not None
        async with self._sem:
            token = ra.bind_run_accounting()
            t0 = time.monotonic()
            err: BaseException | None = None
            response = None
            try:
                response = await llm.chat_complete(messages, **kwargs)
            except BaseException as exc:  # noqa: BLE001 - recorded, re-raised
                err = exc
            duration = time.monotonic() - t0
            accounts = ra.current_llm_calls()
            ra.reset_run_accounting(token)
        receipt = _call_receipt(plane, thinking, duration, response, accounts)
        if err is not None:
            receipt["error"] = f"{type(err).__name__}: {str(err)[:300]}"
            raise SeatFailure(f"call_failed: {type(err).__name__}", _dump({"call": receipt}))
        return response, receipt


def _call_receipt(plane: str, thinking: bool | None, duration: float, response: Any,
                  accounts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """What one wire call cost and what it ran on (never its text)."""
    wired = [a for a in accounts if isinstance(a, Mapping) and "model" in a]
    usage = getattr(response, "usage", None)
    raw = getattr(response, "raw_response", None)
    out: dict[str, Any] = {
        "plane": plane,
        "thinking_requested": thinking,
        "duration_s": round(duration, 3),
        "model_named": [a.get("model") for a in wired],
        "served_model": (raw.get("model") if isinstance(raw, Mapping) else None)
        or getattr(usage, "model", None),
        "prompt_tokens": getattr(usage, "prompt_tokens", None),
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "reasoning_tokens": getattr(usage, "reasoning_tokens", None),
        "finish_reason": getattr(response, "finish_reason", None),
        "cost_estimate_usd": getattr(usage, "cost_estimate_usd", None),
    }
    fallbacks = [a.get("thinking_fallback") for a in wired if a.get("thinking_fallback")]
    if fallbacks:
        out["thinking_fallback"] = fallbacks
    waits = [int(a.get("gate_wait_ms") or 0) for a in wired]
    if any(waits):
        out["gate_wait_ms"] = sum(waits)
    if len(wired) > 1:
        out["wire_attempts"] = len(wired)
    return out


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, sort_keys=True, default=str)


def _with_meta(content: str | None, meta: Mapping[str, Any]) -> str:
    return _dump({"reply": content, "meta": meta})


__all__ = [
    "CallCapReached",
    "LoopThread",
    "ModelSeats",
    "output_contract",
    "parse_reply",
    "render_prompt",
]

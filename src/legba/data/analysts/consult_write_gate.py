# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The consult WRITE GATE — run-scoped preconditions for the ``consult_write`` tools
(C-2b-1; design C-2a §5), and the pack's provider declaration on the C-2a §7 hook.

An armed turn (the operator switched on "Allow changes this turn",
``allow_writes``) offers the write tools beside the reads. Each call
passes THREE stages:

1. **This gate** (one per run): the spend ceiling (once the run's guard has
   fired, every later write is ``spend_ceiling_reached`` — a partial plan is
   delivered as a partial plan); the closed grammar (``validate_args``, deep);
   and the **read set**: an object id and its ``expected_version`` are admitted
   only if an ``inspect_object`` result returned them in an EARLIER round, a
   ``write_id`` only if an inspect listed it (C-2a §5.2 — the C-1a typed
   harvest, extended). Ids the operator typed still need an inspect first.
   A refusal is a ``governor_events`` BLOCK and ``tool_blocked: <cause>: …``.
2. **The agency hard gate** through the per-request binding (grant, known tool,
   grammar, governor, ``action_pack_invocations`` row + ALLOW event).
3. **The registry executor** (via the runtime's thin client): the kill
   switches re-read on every write, the durable operator turn, compare-and-set,
   protected / writable paths, preflight, tiering, caps, the receipt.

The gate counts what happened and composes the TRUTH LINE every armed turn
ends with — ``writes: executed n · staged n · refused n`` or ``writes: none`` —
on the server, beside whatever the prose says; and the bag's ``writes`` list
(``[]`` = armed, nothing written) for reload. The ledger is the authority.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping

from .. import consult_write_vocabulary as W
from ..stack.llm.tool_rounds import (
    ARG_BLOCK_CAUSES,
    TOOL_SCHEMAS,
    ToolSpec,
    split_arg_problem,
    validate_args,
)
from .agency.consult_write import _NAMED_HANDLERS, WRITE_TURN
from .agency.events import GovernorEvent, record_governor_event
from .consult_case import harvest_refs
from .consult_tool_providers import ConsultToolProvider, register_consult_tools

logger = logging.getLogger(__name__)

__all__ = ["WRITE_PROVIDER", "WRITE_TOOL_SPECS", "WriteGate", "refuse_unarmed_writes", "write_prompt_block"]

Record = Callable[[dict[str, Any]], Awaitable[None]]

#: The tools' specs, from the vocabulary's closed grammar.
WRITE_TOOL_SPECS: dict[str, ToolSpec] = {
    name: ToolSpec(name=name, description=W.TOOL_SCHEMAS[name][0], json_schema=W.TOOL_SCHEMAS[name][1])
    for name in W.WRITE_TOOL_NAMES
}

_WRITE_PHASE = "write"


def write_prompt_block(binding: Any) -> str:
    """The write protocol, appended to the run context ONLY on an armed turn."""
    from .consult_prompt_context import _pack_block

    pack_text = _pack_block(binding)
    if not pack_text:
        return ""
    lines = [pack_text, "Changes (call them like any tool; they run after the round's reads, in order):"]
    for name in W.WRITE_TOOL_NAMES:
        spec = TOOL_SCHEMAS[name]
        args = ", ".join(spec.json_schema.get("properties", {}))
        lines.append(f"  - {name}({args}) — {spec.description}")
    seams: dict[int, list[str]] = {}
    for (cls, verb), entry in W.VOCABULARY.items():
        # a class with built verbs names only its seam'd verb ("unit create")
        word = f"{cls} {verb}" if cls in W.BUILT_CLASSES else cls
        if entry != "built" and word not in seams.get(int(entry[1]), []):
            seams.setdefault(int(entry[1]), []).append(word)
    lines.append(
        "Writable classes: " + ", ".join(W.BUILT_CLASSES) + ". Not built (tell the operator): "
        + "; ".join(f"{', '.join(c)} — SEAMS #{n}" for n, c in sorted(seams.items())) + "."
    )
    return "\n\n" + "\n".join(lines)


async def _ledger_block(binding: Any, tool: str, cause: str, detail: str) -> None:
    """One ``governor_events`` BLOCK for a write-gate refusal. Never raises."""
    if binding is None or getattr(binding, "pg_pool", None) is None:
        return
    assert cause in W.WRITE_BLOCK_CAUSES or cause in ARG_BLOCK_CAUSES, cause
    try:
        async with binding.pg_pool.acquire() as conn:
            await record_governor_event(
                conn,
                GovernorEvent(
                    pack_id=W.CONSULT_WRITE_PACK_ID, tool_name=tool, decision="block",
                    cause=cause, budget_account=binding.budget_account,
                    requested_by=binding.requested_by, tenant_id=binding.tenant_id,
                    detail=detail[:2000],
                ),
                nats_publish=getattr(binding.agency, "_nats_publish", None),
            )
    except Exception as exc:  # noqa: BLE001 — a ledger hiccup never fails a round
        logger.warning("consult_write.block_ledger_failed tool=%s err=%s", tool, exc)


async def refuse_unarmed_writes(binding: Any, tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A write tool called on a turn the operator did not arm."""
    detail = (
        f"{tool} changes the platform and the operator did not allow changes on this "
        "turn; answer, and tell the operator to switch on 'Allow changes this turn'."
    )
    await _ledger_block(binding, tool, "writes_not_armed", detail)
    return (
        {"error": f"tool_blocked: writes_not_armed: {detail}"},
        {"governed": binding is not None, "admitted": False},
    )


@dataclass
class WriteGate:
    """One armed run's writes: its read set, its counts, its bag entries."""

    binding: Any
    record: Record
    spend: Any = None
    read_set: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    versions: dict[tuple[str, str], set[str]] = field(default_factory=dict)
    entries: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def open(cls, binding: Any, *, record: Record, first: Mapping[str, Any], spend: Any = None) -> "WriteGate":
        del first
        return cls(binding=binding, record=record, spend=spend)

    # ---- the read set -----------------------------------------------------

    def harvest(self, round_no: int, calls: list[Mapping[str, Any]], results: list[Any]) -> None:
        """Add one round's results (after its provider calls ran) to the read set."""
        for call, outcome in zip(calls, results):
            tool = str(call.get("tool"))
            result = outcome[0] if isinstance(outcome, tuple) else outcome
            if not isinstance(result, Mapping) or "error" in result:
                continue
            where = {"round": round_no, "tool": tool}
            if tool == "inspect_object":
                if not result.get("found"):
                    continue
                key = (str(result.get("class")), str(result.get("id")))
                self.read_set.setdefault(key, where)
                if result.get("version"):
                    self.versions.setdefault(key, set()).add(str(result["version"]))
                for w in result.get("writes") or ():
                    if isinstance(w, Mapping) and w.get("write_id"):
                        self.read_set.setdefault(("write", str(w["write_id"])), where)
                for f in result.get("open") or () if key[0] == "fact" else ():  # W11: a fact pair's open ids
                    if isinstance(f, Mapping) and f.get("id"):
                        self.read_set.setdefault(("fact", str(f["id"])), where)
                continue
            for kind, ref_id in harvest_refs(tool, result):
                kind = "subject" if kind == "target" else kind  # C-2a §12: subject = code target
                if kind in W.CLASSES:
                    self.read_set.setdefault((kind, ref_id), where)

    # ---- preconditions ----------------------------------------------------

    def _precheck(self, tool: str, args: Mapping[str, Any]) -> tuple[str, str] | None:
        reason = self.spend.exhausted_reason() if self.spend is not None else None
        if reason and tool != "inspect_object":
            return "spend_ceiling_reached", f"{reason}; no further change runs on this turn."
        problem = validate_args(TOOL_SCHEMAS[tool], args)
        if problem is not None:
            return split_arg_problem(problem)
        if tool in {"update_object", "transition_object"}:
            key = (str(args["class"]), str(args["id"]))
            if key not in self.read_set or str(args["expected_version"]) not in self.versions.get(key, ()):
                return "ref_not_read", (
                    f"{key[0]} {key[1]} at version {str(args['expected_version'])[:12]} was not "
                    "returned by inspect_object in an earlier round. Inspect it first, then change it."
                )
        if tool == "rerun_object":
            key = (str(args["class"]), str(args["id"]))
            subject = args.get("subject")
            if key not in self.read_set or (subject and ("subject", str(subject)) not in self.read_set):
                return "ref_not_read", (
                    f"{key[0]} {key[1]}" + (f" or subject {subject}" if subject else "")
                    + " was not returned by a read in an earlier round. Read it first, then rerun it."
                )
        if tool == "edit_entity":
            key = ("entity", str(args["entity_id"]))
            if key not in self.read_set or str(args["expected_version"]) not in self.versions.get(key, ()):
                return "ref_not_read", (
                    f"entity {key[1]} at version {args['expected_version']} was not returned by "
                    "inspect_object in an earlier round. Inspect it first, then change it."
                )
        if tool in {"merge_entities", "unmerge_entity"}:
            ids = [str(args[k]) for k in ("keeper_id", "loser_id") if k in args]
            unread = [i for i in ids if ("entity", i) not in self.read_set]
            if unread:
                return "ref_not_read", (
                    f"entity {', '.join(unread)} was not returned by inspect_object in an earlier "
                    "round. Inspect both sides first, then merge or unmerge."
                )
        # W11 C-2b-2/facts: a fact id named by close_fact or assert_fact's supersede list.
        fact_ids = [args["fact_id"]] if tool == "close_fact" else (
            args.get("supersede") if tool == "assert_fact" and isinstance(args.get("supersede"), list) else [])
        unread = [str(f) for f in fact_ids if ("fact", str(f).strip().lower()) not in self.read_set]
        if unread:
            return "ref_not_read", (
                f"fact {unread[0]} was not returned by inspect_object(class='fact') in an earlier round. "
                "Inspect the pair first, then change it."
            )
        if tool == "revert_write" and ("write", str(args["write_id"])) not in self.read_set:
            return "ref_not_read", (
                f"write {args['write_id']} was not listed by inspect_object in an earlier round."
            )
        return None

    # ---- one write call ---------------------------------------------------

    async def run(self, tool: str, args: Mapping[str, Any], round_no: int) -> tuple[dict[str, Any], dict[str, Any]]:
        """Gate, govern and submit ONE write-pack call. Never raises."""
        args = {k: v for k, v in dict(args).items() if k != "scope_predicate"}
        refusal = self._precheck(tool, args)
        if refusal is not None:
            cause, detail = refusal
            await _ledger_block(self.binding, tool, cause, detail)
            return await self._refused(tool, round_no, cause, detail, write_id=None)
        token = WRITE_TURN.set({"turn_cost_usd": getattr(self.spend, "cost_usd", None)})
        try:
            outcome = await self.binding.run_tool(tool, dict(args))
        except Exception as exc:  # noqa: BLE001 — one call never fails a round
            logger.warning("consult_write.binding_error tool=%s err=%s", tool, exc)
            return await self._refused(tool, round_no, None, f"tool_failed: {exc}", write_id=None)
        finally:
            WRITE_TURN.reset(token)
        if not outcome.admitted:
            return await self._refused(tool, round_no, str(outcome.block_cause), outcome.detail, write_id=None)
        result = outcome.tool_result
        if result is None or result.status == "failed":
            err = result.error if result is not None else "tool produced no result"
            return await self._refused(tool, round_no, None, f"tool_failed: {err}", write_id=None)
        out = dict(result.output)
        if tool == "inspect_object":
            return out, {"governed": True, "admitted": True}
        status = out.get("status")
        wid = out.get("write_id")
        if status == "executed":
            self.entries.append({"write_id": wid, "status": "executed", "tool": tool,
                                 "summary": out.get("card"), "cause": None})
            await self._frame("write_executed", tool, round_no, write_id=wid)
            return (
                {"ok": True, "status": "executed", "write_id": wid, "summary": out.get("card"),
                 "receipt": "the operator's panel shows the receipt and its Revert"},
                {"governed": True, "admitted": True},
            )
        if status == "pending_confirmation":
            self.entries.append({"write_id": wid, "status": "pending_confirmation", "tool": tool,
                                 "summary": out.get("card"), "cause": None})
            await self._frame("write_staged", tool, round_no, write_id=wid)
            return (
                {"write_staged": wid, "detail": (
                    "confirmation required: nothing has changed. The operator confirms on the "
                    "card (Confirm, or by typing the code shown there); you cannot confirm it — "
                    "tell the operator to press Confirm.")},
                {"governed": True, "admitted": True},
            )
        cause = out.get("cause")
        return await self._refused(
            tool, round_no, str(cause) if cause else None,
            str(out.get("detail") or status or "refused"), write_id=wid,
        )

    async def _refused(
        self, tool: str, round_no: int, cause: str | None, detail: str, *, write_id: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        if tool != "inspect_object":
            self.entries.append({"write_id": write_id, "status": "refused", "tool": tool,
                                 "summary": detail[:300], "cause": cause})
        await self._frame("write_refused", tool, round_no, write_id=write_id, cause=cause, detail=detail)
        prefix = f"tool_blocked: {cause}" if cause else "tool_failed"
        return {"error": f"{prefix}: {detail}"}, {"governed": True, "admitted": False}

    async def _frame(self, kind: str, tool: str, round_no: int, **extra: Any) -> None:
        await self.record({
            "phase": _WRITE_PHASE, "kind": kind, "tool": tool, "round": round_no,
            **{k: (str(v)[:600] if isinstance(v, str) else v) for k, v in extra.items() if v is not None},
        })

    # ---- the record -------------------------------------------------------

    def counts(self) -> dict[str, int]:
        out = {"executed": 0, "staged": 0, "refused": 0}
        for e in self.entries:
            key = {"executed": "executed", "pending_confirmation": "staged"}.get(e["status"], "refused")
            out[key] += 1
        return out

    def truth_line(self) -> str:
        if not self.entries:
            return "writes: none"
        c = self.counts()
        return f"writes: executed {c['executed']} · staged {c['staged']} · refused {c['refused']}"

    def answer_suffix(self) -> str:
        return "\n\n" + self.truth_line()

    def cited_uuids(self) -> list[str]:
        return []

    def bag(self) -> dict[str, Any]:
        return {"writes": list(self.entries)}


#: The ``consult_write`` family: offered only on a turn the operator armed with
#: ``allow_writes``; an armed turn without the binding raises the words the
#: registry maps to its refusal; a write call on an unarmed turn is
#: ``writes_not_armed``. Every tool joins the serial write set.
WRITE_PROVIDER = register_consult_tools(ConsultToolProvider(
    pack_id=W.CONSULT_WRITE_PACK_ID,
    specs=tuple(WRITE_TOOL_SPECS[name] for name in W.WRITE_TOOL_NAMES),
    handlers=dict(_NAMED_HANDLERS),
    writes=True,
    offered=lambda flags: flags.allow_writes,
    prompt_fragment=write_prompt_block,
    open_gate=WriteGate.open,
    refuse=refuse_unarmed_writes,
    unavailable=f"writes_unavailable: {W.CONSULT_WRITE_PACK_ID} not bound",
))

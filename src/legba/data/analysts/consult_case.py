# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The CASE GATE — run-scoped preconditions for the drive tools (C-1b; C-1a §2, §5).

A case turn (the operator armed ``present_case``) offers the nine
``workstation_drive`` tools beside the substrate reads. Each drive call passes
TWO stages, and every refusal is ledgered:

1. **This gate** (one instance per run): armed? ``no_citation`` (every action
   but ``done`` cites 1..8 refs) → the closed grammar (``validate_args`` over
   the closed schema, deep) → the per-kind rules (a bound panel kind needs its
   subject — ``kind_not_drivable``; a map with no map surface —
   ``no_map_surface``; the window arithmetic) → not sealed, not over the cap →
   **every ref it names is in the run's READ SET** (``ref_not_read``). A
   refusal is a ``governor_events`` BLOCK under the
   :data:`~legba.data.workstation_vocabulary.CASE_BLOCK_CAUSES` vocabulary, a
   ``case_refused`` frame, and ``tool_blocked: <cause>: <detail>`` to the model
   — a round it can recover from.
2. **The agency hard gate**, through the per-request
   :class:`~.agency.binding.AgencyToolBinding`: grant, known tool, grammar,
   governor, ledger (an ``action_pack_invocations`` row + an ALLOW event).

Only then is the action stamped — ``seq`` (1..n, no gap), ``at``, and
``rests_on`` (the round and tool that returned each cite; the model never
supplies provenance) — and emitted as ONE ``case_action`` frame through the
run's own ``_record``, so the live stream and the trace stay one source.

**The read set.** A ref may be cited, focused, scoped, fitted or opened only if
a read tool returned it in an EARLIER round, or the operator pinned it. A ref
returned in the SAME batch does not count — the model wrote the action before
it saw that result — which falls out of the order: drives run after the
round's reads, and the round's reads are harvested after its drives.
:func:`harvest_refs` is the typed per-tool table (``_refs_from_tool_result``
lifts uuids only; a target id or an entity name is a ref too). A kind no tool
harvests cannot be named at all.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Mapping
from uuid import UUID

from .. import panel_actions as PA
from .. import workstation_vocabulary as V
from ..stack.llm.tool_rounds import TOOL_SCHEMAS, split_arg_problem, validate_args
from .agency.events import GovernorEvent, record_governor_event
from .agency.workstation_drive import _NAMED_HANDLERS, iso_z, window_problem
from .consult_case_schema import (
    CaseActionFrame,
    CaseRefusedFrame,
    CaseSealedFrame,
    Cite,
    RestsOn,
    DRIVE_TOOLS,
    DRIVE_TOOL_SPECS,
    PANEL_ACTION_SPEC,
)
from .consult_tool_providers import ConsultToolProvider, register_consult_tools

logger = logging.getLogger(__name__)

__all__ = ["DRIVE_PROVIDER", "CaseGate", "case_prompt_block", "harvest_refs", "refuse_unarmed"]

Record = Callable[[dict[str, Any]], Awaitable[None]]

# ---------------------------------------------------------------------------
# The typed read-set harvest
# ---------------------------------------------------------------------------

#: Per read tool: the kind its ``refs`` list carries, and the row fields that
#: name a ref (``(field, kind)``). A tool absent here contributes nothing.
_HARVEST: dict[str, tuple[str | None, tuple[tuple[str, str], ...]]] = {
    "search_signals": ("signal", (("id", "signal"), ("source_id", "source"))),
    "search_corpus": ("signal", (("id", "signal"), ("source_id", "source"))),
    "vector_search": ("signal", (("id", "signal"),)),
    "read_document": ("signal", (("id", "signal"), ("doc_id", "signal"))),
    "list_findings": (
        "finding", (("id", "finding"), ("target_id", "target"), ("analyst_id", "analyst")),
    ),
    "belief_as_of": (
        "finding", (("id", "finding"), ("target_id", "target"), ("analyst_id", "analyst")),
    ),
    "list_situations": ("situation", (("id", "situation"),)),
    "query_events": ("event", (("id", "event"),)),
    "inspect_event": ("event", ()),
    "list_targets": (None, (("target_id", "target"),)),
    "compare_targets": (None, (("target_id", "target"),)),
    "list_sources": (None, (("source_id", "source"),)),
    "get_source_health": (None, (("source_id", "source"),)),
    "inspect_entity": (None, ()),
}

_ROW_LISTS = ("rows", "items", "targets", "compared", "entries")

#: ``report`` and ``finding`` name one id space (a report IS a finding row).
_FOLD = {"report": "finding"}


def _key(kind: str, ref_id: str) -> tuple[str, str]:
    return _FOLD.get(kind, kind), str(ref_id)


def harvest_refs(tool: str, result: Mapping[str, Any]) -> list[tuple[str, str]]:
    """The typed ``(kind, id)`` refs one read result returned."""
    if not isinstance(result, Mapping) or "error" in result:
        return []
    spec = _HARVEST.get(tool)
    if spec is None:
        return []
    refs_kind, fields = spec
    out: list[tuple[str, str]] = []
    if refs_kind is not None:
        for raw in result.get("refs") or ():
            out.append((refs_kind, str(raw)))
    for list_key in _ROW_LISTS:
        rows = result.get(list_key)
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            for fld, kind in fields:
                value = row.get(fld)
                if value not in (None, ""):
                    out.append((kind, str(value)))
    if tool == "inspect_entity" and result.get("found"):
        profile = result.get("profile") if isinstance(result.get("profile"), Mapping) else {}
        for name in (profile.get("canonical_name"), result.get("entity")):
            if name:
                out.append(("entity", str(name)))
    if tool == "inspect_event" and isinstance(result.get("event"), Mapping):
        if result["event"].get("id"):
            out.append(("event", str(result["event"]["id"])))
    return out


# ---------------------------------------------------------------------------
# The prompt block an armed turn appends
# ---------------------------------------------------------------------------


def case_prompt_block(binding: Any) -> str:
    """The case protocol, appended to the run context ONLY on an armed turn.

    The pack's own fragments and rules (read off the live descriptor, so an
    operator edit reaches the prompt without a code change) plus one line per
    drive tool with its grammar — the JSON-text route has no tools array, so
    the catalogue must be stated. ``""`` when there is no binding.
    """
    from .consult_prompt_context import _pack_block

    pack_text = _pack_block(binding)
    if not pack_text:
        return ""
    lines = [pack_text, "Case actions (call them like any tool; they run after the round's reads, in order):"]
    for name in (*V.DRIVE_TOOL_NAMES, PA.PANEL_ACTION_TOOL):
        spec = TOOL_SCHEMAS[name]
        args = ", ".join(spec.json_schema.get("properties", {}))
        lines.append(f"  - {name}({args}) — {spec.description}")
    lines.append(panel_catalogue_line())
    return "\n\n" + "\n".join(lines)


def panel_catalogue_line() -> str:
    """One compact line per DECLARED view: its actions and their arguments."""
    doc = PA.catalog()
    parts = []
    for vid, view in doc["views"].items():
        if not view.get("declared"):
            continue
        acts = []
        for aid, specs in view["actions"].items():
            argtxt = ",".join(
                s["name"] + ("=" + "|".join(s["values"]) if s.get("values") else "")
                for s in specs
            )
            acts.append(f"{aid}({argtxt})")
        if "subject" in view["binds"] and "bind" not in view["actions"]:
            acts.append("bind(subject=target ref)")
        parts.append(f"{vid}: " + (", ".join(acts) or "open/close/focus only"))
    return "  panel_action views (every view also takes open(place=stage|panel), close, focus): " + "; ".join(parts)


# ---------------------------------------------------------------------------
# Refusal ledger
# ---------------------------------------------------------------------------


async def _ledger_block(binding: Any, tool: str, cause: str, detail: str) -> None:
    """One ``governor_events`` BLOCK for a case-gate refusal. Never raises."""
    if binding is None or getattr(binding, "pg_pool", None) is None:
        return
    assert cause in V.CASE_BLOCK_CAUSES or cause.endswith("_arg"), cause
    try:
        async with binding.pg_pool.acquire() as conn:
            await record_governor_event(
                conn,
                GovernorEvent(
                    pack_id=V.WORKSTATION_DRIVE_PACK_ID, tool_name=tool,
                    decision="block", cause=cause,
                    budget_account=binding.budget_account,
                    requested_by=binding.requested_by,
                    tenant_id=binding.tenant_id, detail=detail[:2000],
                ),
                nats_publish=getattr(binding.agency, "_nats_publish", None),
            )
    except Exception as exc:  # noqa: BLE001 — a ledger hiccup never fails a round
        logger.warning("consult_case.block_ledger_failed tool=%s err=%s", tool, exc)


async def refuse_unarmed(binding: Any, tool: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """A drive tool called on a turn the operator did not arm."""
    detail = (
        f"{tool} is a case action and this turn is not presenting a case; "
        "answer the question with the read tools."
    )
    await _ledger_block(binding, tool, "drive_not_armed", detail)
    return (
        {"error": f"tool_blocked: drive_not_armed: {detail}"},
        {"governed": binding is not None, "admitted": False},
    )


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------


def _trim(args: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in list(args.items())[:12]:
        text = repr(value)
        out[str(key)] = value if len(text) <= 300 else text[:300] + "…"
    return out


@dataclass
class CaseGate:
    """One run's case: its read set, its counter, its admitted actions."""

    binding: Any
    record: Record
    request_id: str | None = None
    plane: str | None = None
    cap: int = V.LIMITS["case_cap_default"]
    read_set: dict[tuple[str, str], RestsOn] = field(default_factory=dict)
    actions: list[dict[str, Any]] = field(default_factory=list)
    refused: int = 0
    sealed: bool = False
    summary: str | None = None
    now: Callable[[], datetime] = field(default=lambda: datetime.now(timezone.utc))
    #: Q-1 — the instance ids the console reported on screen (``on_screen.views``).
    on_screen_ids: tuple[str, ...] = ()

    @classmethod
    def for_run(
        cls, binding: Any, *, record: Record, first: Mapping[str, Any], plane: str | None,
    ) -> "CaseGate":
        governor = getattr(getattr(binding, "pack", None), "governor", None)
        cap = getattr(governor, "max_invocations_per_run", None)
        gate = cls(
            binding=binding,
            record=record,
            request_id=(str(first["request_id"]) if first.get("request_id") else None),
            plane=plane,
            cap=int(cap) if cap is not None else V.LIMITS["case_cap_default"],
            on_screen_ids=tuple(
                str(v.get("id")) for v in ((first.get("on_screen") or {}).get("views") or ())
                if isinstance(v, Mapping) and v.get("id")
            ) if isinstance(first.get("on_screen"), Mapping) else (),
        )
        for pin in first.get("pinned_context") or ():
            if isinstance(pin, Mapping) and pin.get("id"):
                kind = "finding" if pin.get("kind") == "meta_finding" else str(pin.get("kind"))
                if kind in V.REF_KINDS:
                    gate.read_set.setdefault(_key(kind, pin["id"]), RestsOn(0, "pinned"))
        return gate

    @classmethod
    def open(
        cls, binding: Any, *, record: Record, first: Mapping[str, Any], spend: Any = None,
    ) -> "CaseGate":
        """The provider hook's gate factory (C-2a §7); ``spend`` is unused here."""
        del spend
        return cls.for_run(binding, record=record, first=first, plane=None)

    def bag(self) -> dict[str, Any]:
        """The turn's bag entry: ``case`` — composed ONCE, from the admitted actions."""
        return {"case": self.to_record()}

    # ---- the read set -----------------------------------------------------

    def harvest(self, round_no: int, calls: list[Mapping[str, Any]], results: list[Any]) -> None:
        """Add one round's READ results to the read set (after its drives ran)."""
        for call, outcome in zip(calls, results):
            tool = str(call.get("tool"))
            if tool in DRIVE_TOOLS:
                continue
            result = outcome[0] if isinstance(outcome, tuple) else outcome
            for kind, ref_id in harvest_refs(tool, result):
                self.read_set.setdefault(_key(kind, ref_id), RestsOn(round_no, tool))

    def _named_refs(self, name: str, args: Mapping[str, Any]) -> list[dict[str, Any]]:
        refs = [r for r in args.get("cites") or () if isinstance(r, Mapping)]
        if name == "focus":
            refs.append(args["ref"])
        if name == "map" and args.get("ref") is not None:
            refs.append(args["ref"])
        if name == "open_panel" and (args.get("params") or {}).get("subject") is not None:
            refs.append(args["params"]["subject"])
        if name == "set_scope" and args.get("kind") != "none":
            refs.append({"kind": args["kind"], "id": args.get("id")})
        if name == PA.PANEL_ACTION_TOOL:
            for key in ("ref", "subject"):
                ref = (args.get("args") or {}).get(key)
                if isinstance(ref, Mapping):
                    refs.append({"kind": ref.get("kind"), "id": ref.get("id")})
        return refs

    def _panel_problem(self, args: Mapping[str, Any]) -> tuple[str, str] | None:
        """The console's own rules (``panel_actions``): view, action, typed args."""
        view = str(args["view"])
        if not PA.openable(view, self.on_screen_ids):
            return "kind_not_drivable", (
                f"{view} is neither on screen nor a view the console declares; "
                "name a view from the panel_action catalogue or the on-screen list."
            )
        vid = PA.view_id_of(view)
        specs = PA.accepted_action(vid, str(args["action"]))
        if specs is None:
            return "invalid_arg", f"{vid} does not accept {args['action']!r}."
        _checked, problem = PA.check_args(specs, args.get("args") or {})
        if problem is not None:
            return "invalid_arg", f"panel_action {args['action']} on {vid}: {problem}."
        return None

    # ---- preconditions ----------------------------------------------------

    def _precheck(self, name: str, args: Mapping[str, Any]) -> tuple[str, str] | None:
        if name != "done" and not args.get("cites"):
            return "no_citation", (
                f"{name} must cite 1 to {V.LIMITS['max_cites']} records a read "
                "tool returned in an earlier round (cites: [{kind, id}])."
            )
        problem = validate_args(TOOL_SCHEMAS[name], args)
        if problem is not None:
            return split_arg_problem(problem)
        if name == "open_panel":
            kind = args["kind"]
            need = V.DRIVABLE_KINDS[kind]["subject"]
            params = args.get("params") or {}
            subject = params.get("subject")
            if need is not None and (subject is None or subject.get("kind") != need):
                return "kind_not_drivable", (
                    f"{kind} opens on one {need}: pass params.subject {{kind: '{need}', id}}."
                )
            if need is None and subject is not None:
                return "invalid_arg", f"{kind} is not opened on a subject; drop params.subject."
            tab = params.get("tab")
            if tab is not None and tab not in V.PANEL_TABS.get(kind, ()):
                return "invalid_arg", (
                    f"{kind} has no tab {tab!r}; its tabs are "
                    f"{', '.join(V.PANEL_TABS.get(kind, ())) or '(none)'}."
                )
        if name == "set_scope":
            if args["kind"] == "none" and args.get("id") is not None:
                return "invalid_arg", "set_scope kind 'none' clears the scope and takes no id."
            if args["kind"] != "none" and not args.get("id"):
                return "invalid_arg", f"set_scope kind {args['kind']!r} needs an id."
        if name == "map":
            if V.MAP_SURFACE_KIND is None:
                return "no_map_surface", "this workstation declares no map surface."
            if (args["view"] == "fit_ref") != (args.get("ref") is not None):
                return "invalid_arg", "map view 'fit_ref' takes a ref; 'reset' takes none."
        if name == "timeline":
            problem = window_problem(args, self.now())
            if problem is not None:
                return "invalid_arg", problem
        if name == PA.PANEL_ACTION_TOOL:
            problem_pair = self._panel_problem(args)
            if problem_pair is not None:
                return problem_pair
        if self.sealed:
            return "case_sealed", "the case is sealed (done was called); write your final answer."
        if name != "done" and len(self.actions) >= self.cap:
            return "case_cap_reached", (
                f"the case already has {self.cap} actions, its cap; call done and answer."
            )
        unread = [
            f"{r.get('kind')}:{r.get('id')}" for r in self._named_refs(name, args)
            if _key(str(r.get("kind")), str(r.get("id"))) not in self.read_set
        ]
        if unread:
            return "ref_not_read", (
                f"{', '.join(unread[:4])} was not returned by any read tool in an "
                "earlier round and was not pinned. Read it first, then act on it."
            )
        return None

    # ---- one drive call ---------------------------------------------------

    async def run(
        self, tool: str, args: Mapping[str, Any], round_no: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Gate, govern, stamp and emit ONE drive call. Never raises."""
        args = {k: v for k, v in dict(args).items() if k != "scope_predicate"}
        refusal = self._precheck(tool, args)
        if refusal is None:
            try:
                outcome = await self.binding.run_tool(tool, dict(args))
            except Exception as exc:  # noqa: BLE001 — one call never fails a round
                logger.warning("consult_case.binding_error tool=%s err=%s", tool, exc)
                return await self._refused(tool, args, round_no, "tool_failed", str(exc), ledger=False)
            if not outcome.admitted:
                return await self._refused(
                    tool, args, round_no, str(outcome.block_cause), outcome.detail, ledger=False,
                )
            result = outcome.tool_result
            if result is None or result.status == "failed":
                err = result.error if result is not None else "tool produced no result"
                return await self._refused(tool, args, round_no, "tool_failed", str(err), ledger=False)
            return await self._admitted(tool, dict(result.output), round_no)
        cause, detail = refusal
        return await self._refused(tool, args, round_no, cause, detail, ledger=True)

    async def _refused(
        self, tool: str, args: Mapping[str, Any], round_no: int,
        cause: str, detail: str, *, ledger: bool,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        self.refused += 1
        if ledger:
            await _ledger_block(self.binding, tool, cause, detail)
        await self.record(CaseRefusedFrame(
            action=tool, cause=cause, detail=detail, args=_trim(args), round=round_no,
        ).to_step())
        prefix = "tool_failed" if cause == "tool_failed" else f"tool_blocked: {cause}"
        return {"error": f"{prefix}: {detail}"}, {"governed": True, "admitted": False}

    async def _admitted(
        self, tool: str, output: Mapping[str, Any], round_no: int,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        at = iso_z(self.now())
        if tool == "done":
            self.sealed = True
            self.summary = output["args"].get("summary")
            await self.record(CaseSealedFrame(
                seq_through=len(self.actions), summary=self.summary, at=at, round=round_no,
            ).to_step())
            return (
                {"ok": True, "sealed": True, "actions": len(self.actions)},
                {"governed": True, "admitted": True},
            )
        cites = [Cite(kind=c["kind"], id=c["id"]) for c in output.get("cites") or ()]
        rests_on = [self.read_set[_key(c.kind, c.id)] for c in cites]
        frame = CaseActionFrame(
            seq=len(self.actions) + 1, action=tool, args=dict(output["args"]),
            cites=cites, rests_on=rests_on, at=at, round=round_no,
        )
        step = frame.to_step()
        self.actions.append({k: step[k] for k in ("seq", "action", "args", "at", "cites", "rests_on")})
        await self.record(step)
        return (
            {"ok": True, "seq": frame.seq, "action": tool, "args": frame.args},
            {"governed": True, "admitted": True},
        )

    # ---- the record -------------------------------------------------------

    def cited_uuids(self) -> list[str]:
        """Every uuid-shaped id the admitted actions cite — for the census union."""
        out: list[str] = []
        for action in self.actions:
            for cite in action["cites"]:
                try:
                    value = str(UUID(str(cite["id"])))
                except (ValueError, TypeError):
                    continue
                if value not in out:
                    out.append(value)
        return out

    def to_record(self) -> dict[str, Any]:
        """The bag's ``case`` value — composed ONCE, from the admitted actions."""
        from ..consult_case_record import compose_record

        return compose_record(
            request_id=self.request_id,
            status="sealed" if self.sealed else "unsealed",
            plane=self.plane,
            refused=self.refused,
            summary=self.summary,
            actions=self.actions,
            as_of=iso_z(self.now()),
        )


# ---------------------------------------------------------------------------
# The provider declaration (C-2a §7 — the hook C-1b's wiring moved onto)
# ---------------------------------------------------------------------------

#: The ``workstation_drive`` family: offered only on a turn the operator armed
#: with ``present_case``; an armed turn without the binding raises the words
#: the registry maps to 409; a drive call on an unarmed turn is
#: ``drive_not_armed``. Read-only: ``writes=False``.
DRIVE_PROVIDER = register_consult_tools(ConsultToolProvider(
    pack_id=V.WORKSTATION_DRIVE_PACK_ID,
    specs=(*(DRIVE_TOOL_SPECS[name] for name in V.DRIVE_TOOL_NAMES), PANEL_ACTION_SPEC),
    handlers=dict(_NAMED_HANDLERS),
    writes=False,
    offered=lambda flags: flags.present_case,
    prompt_fragment=case_prompt_block,
    open_gate=CaseGate.open,
    refuse=refuse_unarmed,
    unavailable=f"case_unavailable: {V.WORKSTATION_DRIVE_PACK_ID} not bound",
    deps_attr="drive_binding",
))

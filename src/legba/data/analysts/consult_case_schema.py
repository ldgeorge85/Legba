# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE FROZEN CONTRACT for a consult case (C-1b; design C-1a §2, §3, §4).

Three things, and the UI lane (C-1b/ui) builds against all three in parallel,
so a change here after the first commit is a contract change and is said so:

1. **The nine drive tools** (:data:`DRIVE_TOOL_SPECS`) — the action grammar a
   case turn offers the model. Every schema is CLOSED at every level
   (``additionalProperties: false``), every enum is generated from
   :mod:`legba.data.workstation_vocabulary`, and the specs are registered into
   ``tool_rounds.TOOL_SCHEMAS`` on import so the agency hard gate's
   ``validate_args`` covers them like every other pack tool.
2. **The step frames** (:class:`CaseActionFrame`, :class:`CaseRefusedFrame`,
   :class:`CaseSealedFrame`, :class:`CaseStoppedFrame`) — what rides the
   existing ``/consult/stream/{request_id}`` SSE as ``{"type": "step", ...}``,
   one frame per action, each carrying the reads it rests on.
3. **The case record** (:class:`CaseRecord`) — what persists under the turn's
   0223 response bag key ``case``: the gate-admitted actions in canonical form,
   replayable without the model.

:func:`contract_document` renders all three as one JSON document; its bytes
are pinned by ``tests/data_pkg/test_consult_case_schema.py`` and exported to
the UI inside ``legba-ui-v3/src/lib/workstationVocabulary.json``.

What the model writes vs what is stored
---------------------------------------
The model writes the TOOL ARGS below. The case gate canonicalizes them before
anything is emitted: relative spans become absolute instants, cites are
de-duplicated, and the gate — never the model — stamps ``seq``, ``at`` and
``rests_on`` (the round and tool that returned each cited ref). The canonical
``args`` per action are documented on :data:`CANONICAL_ARGS`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .. import workstation_vocabulary as V
from ..panel_actions import PANEL_ACTION_TOOL, VIEW_ACTION_IDS as PANEL_VIEW_ACTION_IDS
from ..stack.llm.tool_rounds import ToolSpec, register_tool_schema

__all__ = [
    "CANONICAL_ARGS",
    "PANEL_ACTION_SPEC",
    "PANEL_ACTION_TOOL",
    "CaseActionFrame",
    "CaseActionRecord",
    "CaseRecord",
    "CaseRefusedFrame",
    "CaseSealedFrame",
    "CaseStoppedFrame",
    "Cite",
    "DRIVE_TOOLS",
    "DRIVE_TOOL_SPECS",
    "RestsOn",
    "contract_document",
    "contract_sha256",
]

# ---------------------------------------------------------------------------
# Shared fragments
# ---------------------------------------------------------------------------

_LIM = V.LIMITS

#: One ``{kind, id}`` reference. ``id`` is the record's own id as a read tool
#: returned it (a uuid, a target id, an entity's canonical name).
_REF: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": list(V.REF_KINDS)},
        "id": {"type": "string", "minLength": 1, "maxLength": _LIM["ref_id_max_chars"]},
    },
    "required": ["kind", "id"],
    "additionalProperties": False,
}

_CITES: dict[str, Any] = {
    "type": "array",
    "items": _REF,
    "minItems": 1,
    "maxItems": _LIM["max_cites"],
    "description": (
        "The records this action rests on: 1 to "
        f"{_LIM['max_cites']} {{kind, id}} refs, each returned by a read tool "
        "in an EARLIER round (or pinned by the operator)."
    ),
}

_ISO = {"type": "string", "format": "date-time", "maxLength": 40}

_ALL_TABS = sorted({t for tabs in V.PANEL_TABS.values() for t in tabs})


def _closed(properties: dict[str, Any], required: list[str]) -> dict[str, Any]:
    schema: dict[str, Any] = {
        "type": "object",
        "properties": properties,
        "additionalProperties": False,
    }
    if required:
        schema["required"] = required
    return schema


_READ_RULE = (
    " Every id you name must have come back from a read tool in an EARLIER "
    "round (or be one of the operator's pinned records); an id from this same "
    "batch, or one you did not read, is refused."
)

# ---------------------------------------------------------------------------
# 1. The nine drive tools
# ---------------------------------------------------------------------------

DRIVE_TOOL_SPECS: dict[str, ToolSpec] = {
    "open_panel": ToolSpec(
        name="open_panel",
        description=(
            "Open one workstation panel for the operator. kind is a drivable "
            "panel kind; a per-subject (target.*) or per-analyst (analyst.*) "
            "kind, or analysis.cross_framing, needs params.subject. params.tab "
            "opens a tabbed panel on one tab." + _READ_RULE
        ),
        json_schema=_closed(
            {
                "kind": {"type": "string", "enum": list(V.DRIVABLE_KINDS)},
                "params": _closed(
                    {"subject": _REF, "tab": {"type": "string", "enum": _ALL_TABS}},
                    [],
                ),
                "cites": _CITES,
            },
            ["kind", "cites"],
        ),
    ),
    "arrange": ToolSpec(
        name="arrange",
        description=(
            "Arrange the wall: switch to a stance or seed a layout preset. The "
            "Consult tile stays open." + _READ_RULE
        ),
        json_schema=_closed(
            {
                "layout": {"type": "string", "enum": list(V.ARRANGEABLE_LAYOUTS)},
                "cites": _CITES,
            },
            ["layout", "cites"],
        ),
    ),
    "set_scope": ToolSpec(
        name="set_scope",
        description=(
            "Scope the wall to one record (a report, subject, entity, "
            "situation or journal entry); every scoped panel follows it. "
            "kind 'none' clears the scope (the whole roster) and takes no id."
            + _READ_RULE
        ),
        json_schema=_closed(
            {
                "kind": {"type": "string", "enum": list(V.SCOPE_KINDS)},
                "id": {"type": "string", "minLength": 1, "maxLength": _LIM["ref_id_max_chars"]},
                "cites": _CITES,
            },
            ["kind", "cites"],
        ),
    ),
    "focus": ToolSpec(
        name="focus",
        description=(
            "Select one record so the Inspector opens it and linked panels "
            "highlight it." + _READ_RULE
        ),
        json_schema=_closed({"ref": _REF, "cites": _CITES}, ["ref", "cites"]),
    ),
    "map": ToolSpec(
        name="map",
        description=(
            "Point the map: view 'fit_ref' fits the camera to one record's own "
            "geometry (ref required), 'reset' returns it home. layers, when "
            "given, is the full set of layers to draw. The map's time is the "
            "shared window — set it with timeline, never here." + _READ_RULE
        ),
        json_schema=_closed(
            {
                "view": {"type": "string", "enum": list(V.MAP_VIEWS)},
                "ref": _REF,
                "layers": {
                    "type": "array",
                    "items": {"type": "string", "enum": list(V.MAP_LAYERS)},
                    "minItems": 1,
                    "maxItems": len(V.MAP_LAYERS),
                },
                "cites": _CITES,
            },
            ["view", "cites"],
        ),
    ),
    "timeline": ToolSpec(
        name="timeline",
        description=(
            "Set the shared clock the map and the timeline read: window is "
            "either {start, end} (ISO-8601, start < end, end no later than now) "
            "or {span} (one of " + ", ".join(V.SPAN_PRESETS) + ", ending now). "
            "at (ISO-8601, optional) centres the timeline on one instant."
            + _READ_RULE
        ),
        json_schema=_closed(
            {
                "window": _closed(
                    {
                        "start": _ISO,
                        "end": _ISO,
                        "span": {"type": "string", "enum": list(V.SPAN_PRESETS)},
                    },
                    [],
                ),
                "at": _ISO,
                "cites": _CITES,
            },
            ["window", "cites"],
        ),
    ),
    "run_mission": ToolSpec(
        name="run_mission",
        description=(
            "Run a named mission: its stance, its scope rule, its window and "
            "its layers in one step." + _READ_RULE
        ),
        json_schema=_closed(
            {
                "name": {"type": "string", "enum": list(V.MISSIONS)},
                "cites": _CITES,
            },
            ["name", "cites"],
        ),
    ),
    "narrate": ToolSpec(
        name="narrate",
        description=(
            f"One plain-text line of the case (at most {_LIM['narrate_max_chars']} "
            "characters) shown in the case strip with its cites as record "
            "links. Say what the operator is looking at and why." + _READ_RULE
        ),
        json_schema=_closed(
            {
                "text": {
                    "type": "string", "minLength": 1,
                    "maxLength": _LIM["narrate_max_chars"],
                },
                "cites": _CITES,
            },
            ["text", "cites"],
        ),
    ),
    "done": ToolSpec(
        name="done",
        description=(
            "Seal the case: no action after this one is admitted. Then write "
            "your final answer as usual. summary (optional, at most "
            f"{_LIM['done_summary_max_chars']} characters) is the case's one-line "
            "caption."
        ),
        json_schema=_closed(
            {
                "summary": {
                    "type": "string",
                    "maxLength": _LIM["done_summary_max_chars"],
                },
            },
            [],
        ),
    ),
}

assert tuple(DRIVE_TOOL_SPECS) == V.DRIVE_TOOL_NAMES, "the nine tools drifted from the vocabulary"

#: Q-1 — PANEL ACTIONS: drive one console view by its id, through the console's
#: own contract (``legba.data.panel_actions``, generated from
#: ``panel-registry/contracts.ts``). Deliberately OUTSIDE the frozen nine and
#: outside :func:`contract_document`: the case/1 contract the console builds
#: against (and its pinned bytes) is unchanged, so every recorded case replays
#: as before; a ``panel_action`` step is an additive action name the console
#: routes to ``applyPanelAction``. Its args schema is closed over the union of
#: the catalogue's argument names; the per-view action and argument rules are
#: the case gate's (``consult_case``), from the same catalogue.
_PANEL_REF: dict[str, Any] = _closed(
    {
        "kind": {"type": "string", "minLength": 1, "maxLength": 32},
        "id": {"type": "string", "minLength": 1, "maxLength": _LIM["ref_id_max_chars"]},
        "label": {"type": "string", "maxLength": 80},
    },
    ["kind", "id"],
)
PANEL_ACTION_SPEC = ToolSpec(
    name=PANEL_ACTION_TOOL,
    description=(
        "Drive one console view through its declared contract: view is a view id "
        "(v4.map) or an instance id (subject.page:<subject>) that is on screen or "
        "openable; action is one the view accepts (open, close, focus, bind, "
        "set_tab, set_window, select, fly_to, set_layer, home, open_record; on the Watch views "
        "filter_alerts, select_alert, filter_silences, open_flag; on the Ledger views "
        "show_forecast, filter, show_run); args are that action's typed "
        "arguments. View changes only — nothing here writes to the record, and "
        "every change is undoable in the console." + _READ_RULE
    ),
    json_schema=_closed(
        {
            "view": {"type": "string", "minLength": 1, "maxLength": 120},
            "action": {"type": "string", "enum": sorted(PANEL_VIEW_ACTION_IDS)},
            "args": _closed(
                {
                    "place": {"type": "string", "enum": ["stage", "panel"]},
                    "tab": {"type": "string", "maxLength": 40},
                    "span": {"type": "string", "maxLength": 8},
                    "ref": _PANEL_REF,
                    "subject": _PANEL_REF,
                    "layer": {"type": "string", "maxLength": 40},
                    "on": {"type": "boolean"},
                    "from": {"type": "string", "maxLength": 40},
                    "to": {"type": "string", "maxLength": 40},
                    **{k: {"type": "number"} for k in ("lon", "lat", "zoom", "west", "south", "east", "north")},
                    # U-9 — the Watch views' filters and focus (the catalogue types them)
                    **{k: {"type": "string", "maxLength": 40} for k in ("group", "severity", "paged", "state", "side")},
                    **{k: {"type": "string", "maxLength": 80} for k in ("alert", "flag")},
                    "cite": {"type": "integer", "minimum": 1, "maximum": 999},
                    # U-11 — the Ledger views (the catalogue types them)
                    **{k: {"type": "string", "maxLength": 120} for k in ("forecast", "run")},
                    "show": {"type": "string", "enum": ["open", "resolved", "all"]},
                },
                [],
            ),
            "cites": _CITES,
        },
        ["view", "action", "cites"],
    ),
)

#: The drive tool names — run SERIALLY, in emitted order, after a round's
#: reads. Not in ``tool_rounds.WRITE_TOOLS``: they write nothing.
DRIVE_TOOLS: frozenset[str] = frozenset(DRIVE_TOOL_SPECS) | {PANEL_ACTION_TOOL}

for _spec in (*DRIVE_TOOL_SPECS.values(), PANEL_ACTION_SPEC):
    register_tool_schema(_spec)

#: The canonical ``args`` each action is stored and emitted with (after the
#: case gate). ``cites`` is never inside ``args``; it sits beside them.
CANONICAL_ARGS: dict[str, dict[str, Any]] = {
    "open_panel": _closed(
        {
            "kind": {"type": "string", "enum": list(V.DRIVABLE_KINDS)},
            "params": _closed(
                {"subject": _REF, "tab": {"type": "string", "enum": _ALL_TABS}}, [],
            ),
        },
        ["kind", "params"],
    ),
    "arrange": _closed(
        {
            "layout": {"type": "string", "enum": list(V.ARRANGEABLE_LAYOUTS)},
            "layout_type": {"type": "string", "enum": ["stance", "preset"]},
        },
        ["layout", "layout_type"],
    ),
    "set_scope": _closed(
        {
            "kind": {"type": "string", "enum": list(V.SCOPE_KINDS)},
            "id": {"type": ["string", "null"]},
        },
        ["kind", "id"],
    ),
    "focus": _closed({"ref": _REF}, ["ref"]),
    "map": _closed(
        {
            "view": {"type": "string", "enum": list(V.MAP_VIEWS)},
            "ref": {"oneOf": [_REF, {"type": "null"}]},
            "layers": {
                "oneOf": [
                    {"type": "array", "items": {"type": "string", "enum": list(V.MAP_LAYERS)}},
                    {"type": "null"},
                ],
            },
        },
        ["view", "ref", "layers"],
    ),
    "timeline": _closed(
        {
            "start": _ISO,
            "end": _ISO,
            "span": {"oneOf": [{"type": "string", "enum": list(V.SPAN_PRESETS)}, {"type": "null"}]},
            "at": {"oneOf": [_ISO, {"type": "null"}]},
        },
        ["start", "end", "span", "at"],
    ),
    "run_mission": _closed(
        {"name": {"type": "string", "enum": list(V.MISSIONS)}}, ["name"],
    ),
    "narrate": _closed({"text": {"type": "string"}}, ["text"]),
    "done": _closed({"summary": {"type": ["string", "null"]}}, ["summary"]),
}

# ---------------------------------------------------------------------------
# 2. The step frames
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cite:
    """One ``{kind, id}`` an action cites."""

    kind: str
    id: str


@dataclass(frozen=True)
class RestsOn:
    """Where one cite came from — stamped by the gate, never by the model.

    ``round`` is the consult round whose read returned the ref, ``tool`` the
    read tool. A ref the operator pinned rests on ``{"round": 0, "tool":
    "pinned"}``; one the scope pin carried on ``{"round": 0, "tool": "scope"}``.
    One entry per cite, positional.
    """

    round: int
    tool: str


@dataclass(frozen=True)
class CaseActionFrame:
    """One ADMITTED action, emitted the moment the gate admits it.

    Step dict: ``{"phase": "case", "kind": "case_action", "seq", "action",
    "args", "cites", "rests_on", "at", "round"}``. ``seq`` runs 1..n with no
    gap across the case; a missing seq on the live stream is a dropped frame.
    """

    seq: int
    action: str
    args: dict[str, Any]
    cites: list[Cite]
    rests_on: list[RestsOn]
    at: str
    round: int

    def to_step(self) -> dict[str, Any]:
        return {"phase": V.CASE_PHASE, "kind": "case_action", **asdict(self)}


@dataclass(frozen=True)
class CaseRefusedFrame:
    """One REFUSED action — trace only, never executed, carries no seq.

    ``cause`` is a :data:`~legba.data.workstation_vocabulary.CASE_BLOCK_CAUSES`
    member or an agency-gate cause (``invalid_arg``, ``result_shaped_arg``, a
    governor cause). ``args`` are the model's own, bounded, for the trace.
    """

    action: str
    cause: str
    detail: str
    args: dict[str, Any]
    round: int

    def to_step(self) -> dict[str, Any]:
        return {"phase": V.CASE_PHASE, "kind": "case_refused", **asdict(self)}


@dataclass(frozen=True)
class CaseSealedFrame:
    """The ``done`` — the case is sealed at ``seq_through`` admitted actions."""

    seq_through: int
    summary: str | None
    at: str
    round: int

    def to_step(self) -> dict[str, Any]:
        return {"phase": V.CASE_PHASE, "kind": "case_sealed", **asdict(self)}


@dataclass(frozen=True)
class CaseStoppedFrame:
    """The operator's stop, published by the REGISTRY once acknowledged.

    Nothing with ``seq > stopped_after_seq`` is executed or recorded.
    """

    stopped_after_seq: int
    at: str

    def to_step(self) -> dict[str, Any]:
        return {"phase": V.CASE_PHASE, "kind": "case_stopped", **asdict(self)}


# ---------------------------------------------------------------------------
# 3. The case record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaseActionRecord:
    """One action as stored: the admitted frame minus ``round``/``phase``."""

    seq: int
    action: str
    args: dict[str, Any]
    at: str
    cites: list[Cite]
    rests_on: list[RestsOn]


@dataclass(frozen=True)
class CaseRecord:
    """The bag key ``case`` (``null`` = this turn presented no case).

    * ``status`` — ``sealed`` | ``unsealed`` | ``stopped``.
    * ``as_of`` — when the record was composed (UTC, ``Z``).
    * ``plane`` — the friendly LLM plane that drove it (``core``/``opus``/``fable``),
      ``None`` when not known.
    * ``vocabulary`` — :func:`~legba.data.workstation_vocabulary.vocabulary_hash`.
    * ``refused`` — how many actions the gates refused (never executed).
    * ``stopped_after_seq`` — set only when ``stopped``.
    * ``complete`` / ``gap_at_seq`` — False / the first missing seq when the
      record was rebuilt from frames and one was lost; replay stops there.
    * ``summary`` — the ``done`` summary, ``None`` otherwise.
    """

    version: str
    request_id: str | None
    status: str
    as_of: str
    plane: str | None
    vocabulary: str
    refused: int
    stopped_after_seq: int | None
    complete: bool
    gap_at_seq: int | None
    summary: str | None
    actions: list[CaseActionRecord] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# JSON-schema renderings of the frames and the record (the UI's reading)
# ---------------------------------------------------------------------------

_CITE_LIST = {"type": "array", "items": _REF, "maxItems": _LIM["max_cites"]}
_RESTS_ON_LIST = {
    "type": "array",
    "items": _closed(
        {"round": {"type": "integer", "minimum": 0}, "tool": {"type": "string"}},
        ["round", "tool"],
    ),
}
_ACTION_ENUM = {"type": "string", "enum": list(V.DRIVE_TOOL_NAMES)}


def _frame(kind: str, properties: dict[str, Any]) -> dict[str, Any]:
    return _closed(
        {
            "phase": {"const": V.CASE_PHASE},
            "kind": {"const": kind},
            **properties,
        },
        ["phase", "kind", *properties],
    )


_FRAME_SCHEMAS: dict[str, dict[str, Any]] = {
    "case_action": _frame("case_action", {
        "seq": {"type": "integer", "minimum": 1},
        "action": _ACTION_ENUM,
        "args": {"type": "object", "description": "CANONICAL_ARGS[action]"},
        "cites": _CITE_LIST,
        "rests_on": _RESTS_ON_LIST,
        "at": _ISO,
        "round": {"type": "integer", "minimum": 1},
    }),
    "case_refused": _frame("case_refused", {
        "action": {"type": "string"},
        "cause": {"type": "string"},
        "detail": {"type": "string"},
        "args": {"type": "object"},
        "round": {"type": "integer", "minimum": 1},
    }),
    "case_sealed": _frame("case_sealed", {
        "seq_through": {"type": "integer", "minimum": 0},
        "summary": {"type": ["string", "null"]},
        "at": _ISO,
        "round": {"type": "integer", "minimum": 1},
    }),
    "case_stopped": _frame("case_stopped", {
        "stopped_after_seq": {"type": "integer", "minimum": 0},
        "at": _ISO,
    }),
}

_RECORD_SCHEMA: dict[str, Any] = _closed(
    {
        "version": {"const": V.CASE_RECORD_VERSION},
        "request_id": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": list(V.CASE_STATUSES)},
        "as_of": _ISO,
        "plane": {"type": ["string", "null"]},
        "vocabulary": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "refused": {"type": "integer", "minimum": 0},
        "stopped_after_seq": {"type": ["integer", "null"], "minimum": 0},
        "complete": {"type": "boolean"},
        "gap_at_seq": {"type": ["integer", "null"], "minimum": 1},
        "summary": {"type": ["string", "null"]},
        "actions": {
            "type": "array",
            "maxItems": _LIM["case_cap_default"],
            "items": _closed(
                {
                    "seq": {"type": "integer", "minimum": 1},
                    "action": _ACTION_ENUM,
                    "args": {"type": "object", "description": "CANONICAL_ARGS[action]"},
                    "at": _ISO,
                    "cites": _CITE_LIST,
                    "rests_on": _RESTS_ON_LIST,
                },
                ["seq", "action", "args", "at", "cites", "rests_on"],
            ),
        },
    },
    [
        "version", "request_id", "status", "as_of", "plane", "vocabulary",
        "refused", "stopped_after_seq", "complete", "gap_at_seq", "summary",
        "actions",
    ],
)


def contract_document() -> dict[str, Any]:
    """Everything the UI builds against, as one JSON-able document."""
    return {
        "tools": {
            name: {"description": spec.description, "input_schema": spec.json_schema}
            for name, spec in DRIVE_TOOL_SPECS.items()
        },
        "canonical_args": CANONICAL_ARGS,
        "frames": _FRAME_SCHEMAS,
        "record": _RECORD_SCHEMA,
        "request_flag": {
            "name": "present_case",
            "type": "boolean",
            "default": False,
            "unavailable": "409 case_unavailable: workstation_drive not bound",
        },
        "stop_body": _closed(
            {"executed_through_seq": {"type": "integer", "minimum": 0}}, [],
        ),
    }


def contract_sha256() -> str:
    """sha256 over the canonical contract bytes — pinned by the schema test."""
    blob = json.dumps(
        contract_document(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()

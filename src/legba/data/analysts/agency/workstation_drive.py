# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``workstation_drive`` pack's nine handlers — pure CANONICALIZERS (C-1b).

Design C-1a §2: a drive tool never touches the UI and never touches a store.
Its handler takes the gate-validated arguments and returns the CANONICAL
action — relative spans resolved to absolute instants, cites de-duplicated,
optional fields made explicit (``null``), a layout's type named — which the
case gate then stamps (``seq``, ``at``, ``rests_on``) and emits as one
``case_action`` frame. The workstation's executor is what moves the wall.

Read-only by construction: nothing here imports a writer, opens a connection
or reads ``ctx``; the guard test asserts the pack's tools are disjoint from
``tool_rounds.WRITE_TOOLS``. Running through the agency hard gate is still the
point — every admitted action lands an ``action_pack_invocations`` row and an
ALLOW event, every refusal a BLOCK, through the one real binding path.

The window rules live here too (:func:`window_problem`) because they are the
same arithmetic the canonicalizer uses: the case gate calls the check BEFORE
the agency gate (a refusal must not spend the pack's rate) and the handler
resolves AFTER it, from one definition.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from ... import workstation_vocabulary as V
from .. import consult_case_schema as _schema  # noqa: F401 — registers the nine TOOL_SCHEMAS entries
from .tools import ToolCall, ToolContext, ToolRegistry, ToolResult

__all__ = [
    "WORKSTATION_DRIVE_TOOLS",
    "canonical_action",
    "dedupe_refs",
    "iso_z",
    "parse_instant",
    "register_workstation_drive_tools",
    "window_problem",
]

#: The nine case/1 tools plus Q-1's ``panel_action`` (a view action by view id,
#: validated against the console's contract catalogue by the case gate).
WORKSTATION_DRIVE_TOOLS: tuple[str, ...] = (*V.DRIVE_TOOL_NAMES, "panel_action")


def iso_z(ts: datetime) -> str:
    """``2026-10-03T12:00:00Z`` — second precision, UTC, the one stored form."""
    return ts.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_instant(raw: Any) -> datetime | None:
    """An ISO-8601 instant as an aware UTC datetime, or None when unparseable.

    A naive value is read as UTC — the model writes ``2026-10-01T00:00:00``
    for midnight UTC far more often than it means local time on a server.
    """
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        ts = datetime.fromisoformat(text)
    except ValueError:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _window_bounds(args: Mapping[str, Any], now: datetime) -> tuple[datetime, datetime, str | None]:
    window = args.get("window") or {}
    span = window.get("span")
    if span is not None:
        return now - timedelta(seconds=V.SPAN_PRESETS[span]), now, str(span)
    start = parse_instant(window.get("start"))
    end = parse_instant(window.get("end"))
    assert start is not None and end is not None  # window_problem ran first
    return start, end, None


def window_problem(args: Mapping[str, Any], now: datetime) -> str | None:
    """Why a ``timeline`` call's window is unusable, or None.

    Exactly one form: ``{span}`` OR ``{start, end}``. ``start < end``; ``end``
    and ``at`` no later than now + the skew; the absolute window no wider than
    the vocabulary's ceiling; ``at`` inside the window. The returned detail is
    written FOR THE MODEL.
    """
    window = args.get("window")
    if not isinstance(window, Mapping):
        return "timeline needs window: {span} or {start, end}."
    has_span = window.get("span") is not None
    has_abs = window.get("start") is not None or window.get("end") is not None
    if has_span == has_abs:
        return "timeline window takes EITHER span OR start+end, not both and not neither."
    skew = timedelta(seconds=V.LIMITS["future_skew_seconds"])
    if has_abs:
        start = parse_instant(window.get("start"))
        end = parse_instant(window.get("end"))
        if start is None or end is None:
            return "timeline window start and end must both be ISO-8601 instants."
        if not start < end:
            return "timeline window start must be before end."
        if end > now + skew:
            return (
                f"timeline window end {iso_z(end)} is in the future (now is "
                f"{iso_z(now)}); a case shows what is on record."
            )
        if (end - start).total_seconds() > V.LIMITS["max_window_seconds"]:
            return "timeline window is wider than 365 days."
    at_raw = args.get("at")
    if at_raw is not None:
        at = parse_instant(at_raw)
        if at is None:
            return "timeline at must be an ISO-8601 instant."
        if at > now + skew:
            return f"timeline at {iso_z(at)} is in the future."
        start, end, _span = _window_bounds(args, now)
        if not start <= at <= end:
            return "timeline at must fall inside the window."
    return None


def _ref(raw: Any) -> dict[str, str] | None:
    if not isinstance(raw, Mapping):
        return None
    return {"kind": str(raw.get("kind")), "id": str(raw.get("id"))}


def dedupe_refs(raw: Any) -> list[dict[str, str]]:
    """``[{kind, id}]`` in first-seen order, duplicates dropped."""
    out: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw or ():
        ref = _ref(item)
        if ref is None:
            continue
        key = (ref["kind"], ref["id"])
        if key not in seen:
            seen.add(key)
            out.append(ref)
    return out


def canonical_action(name: str, args: Mapping[str, Any], now: datetime) -> dict[str, Any]:
    """The canonical ``args`` for one gate-validated action (``CANONICAL_ARGS``)."""
    if name == "open_panel":
        params = args.get("params") or {}
        canon: dict[str, Any] = {}
        if params.get("subject") is not None:
            canon["subject"] = _ref(params["subject"])
        if params.get("tab") is not None:
            canon["tab"] = str(params["tab"])
        return {"kind": str(args["kind"]), "params": canon}
    if name == "arrange":
        layout = str(args["layout"])
        return {"layout": layout, "layout_type": V.ARRANGEABLE_LAYOUTS[layout]["type"]}
    if name == "set_scope":
        kind = str(args["kind"])
        return {"kind": kind, "id": None if kind == "none" else str(args.get("id"))}
    if name == "focus":
        return {"ref": _ref(args["ref"])}
    if name == "map":
        view = str(args["view"])
        layers = args.get("layers")
        return {
            "view": view,
            "ref": _ref(args.get("ref")) if view == "fit_ref" else None,
            "layers": (
                [layer for layer in V.MAP_LAYERS if layer in set(layers)]
                if layers else None
            ),
        }
    if name == "timeline":
        start, end, span = _window_bounds(args, now)
        at = parse_instant(args.get("at"))
        return {
            "start": iso_z(start),
            "end": iso_z(end),
            "span": span,
            "at": iso_z(at) if at is not None else None,
        }
    if name == "run_mission":
        return {"name": str(args["name"])}
    if name == "narrate":
        return {"text": " ".join(str(args["text"]).split())}
    if name == "panel_action":
        given = args.get("args") or {}
        return {
            "view": str(args["view"]),
            "action": str(args["action"]),
            "args": {k: given[k] for k in sorted(given) if given[k] not in (None, "")},
        }
    if name == "done":
        summary = args.get("summary")
        text = " ".join(str(summary).split()) if summary is not None else ""
        return {"summary": text or None}
    raise ValueError(f"{name!r} is not a workstation_drive action")


async def _drive(name: str, call: ToolCall) -> ToolResult:
    """The one canonicalizer body behind the nine named handlers below."""
    now = datetime.now(timezone.utc)
    if name == "done":
        # ``done`` declares no ``cites`` (it seals the case); reading the key
        # here would make the F-7 schema guard see a read the schema never
        # declared (an order-dependent failure on w5-int, 2026-10-04).
        cites: list[dict[str, Any]] = []
    else:
        cites = dedupe_refs(call.args.get("cites"))
    return ToolResult(
        status="completed",
        output={
            "action": name,
            "args": canonical_action(name, call.args, now),
            "cites": cites,
        },
        units=1,
    )


# One named module-level handler per tool, registered by LITERAL name below:
# tests/data_pkg/test_tool_schema_matches_handlers.py (F-7) indexes
# ``registry.register("<tool>", <fn>)`` by AST and walks the callee to prove
# the handler reads exactly the properties the schema declares — a loop over
# DRIVE_TOOL_NAMES is invisible to it (roll-34 gate, 2026-10-04).
async def open_panel_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("open_panel", call)


async def arrange_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("arrange", call)


async def set_scope_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("set_scope", call)


async def focus_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("focus", call)


async def map_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("map", call)


async def timeline_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("timeline", call)


async def run_mission_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("run_mission", call)


async def narrate_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("narrate", call)


async def done_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("done", call)


async def panel_action_tool(call: ToolCall, pack: Any, ctx: ToolContext) -> ToolResult:
    del pack, ctx
    return await _drive("panel_action", call)


_NAMED_HANDLERS: dict[str, Any] = {
    "open_panel": open_panel_tool, "arrange": arrange_tool, "set_scope": set_scope_tool,
    "focus": focus_tool, "map": map_tool, "timeline": timeline_tool,
    "run_mission": run_mission_tool, "narrate": narrate_tool, "done": done_tool,
    "panel_action": panel_action_tool,
}
assert set(_NAMED_HANDLERS) == set(WORKSTATION_DRIVE_TOOLS), (
    "a drive tool has no named handler — add one above (F-7 guard)")


def register_workstation_drive_tools(registry: ToolRegistry) -> None:
    """Register the nine canonicalizers on the agency tool registry."""
    registry.register("open_panel", open_panel_tool)
    registry.register("arrange", arrange_tool)
    registry.register("set_scope", set_scope_tool)
    registry.register("focus", focus_tool)
    registry.register("map", map_tool)
    registry.register("timeline", timeline_tool)
    registry.register("run_mission", run_mission_tool)
    registry.register("narrate", narrate_tool)
    registry.register("done", done_tool)
    registry.register("panel_action", panel_action_tool)

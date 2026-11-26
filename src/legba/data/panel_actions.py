# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The server's copy of the console's PANEL ACTION catalogue — a leaf.

The console declares, per view, what Consult may do to it
(``legba-ui-v3/src/panel-registry/contracts.ts``) and routes every
action through ``applyPanelAction(id, action, args)`` with Undo. This module
loads the GENERATED copy of that declaration
(``panel_action_catalog.json``, written by ``scripts/gen_panel_action_catalog.py``
from the TypeScript; a drift test regenerates and compares) and mirrors the
console's two checks — :func:`accepted_action` (``acceptedAction``) and
:func:`check_args` (``checkArgs``) — so the case gate refuses what the console
would refuse, before it is emitted.

VIEW ACTIONS ONLY. :data:`VIEW_ACTION_IDS` is the closed set of action ids the
server will ever emit — every one of them changes what is on screen and nothing
else (open / close / focus / bind a view, show a tab, set the shared window,
select a record, turn a map layer on or off, fly the map to a record, a position
or an area, send the map home). The loader REFUSES a catalogue
that declares any other action id, so a console contract can never smuggle a
record-writing action into the consult's vocabulary.

A leaf: stdlib only (the registry's slim image may import it).
"""

from __future__ import annotations

import json
import math
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping

__all__ = [
    "CATALOG_PATH",
    "PANEL_ACTION_TOOL",
    "VIEW_ACTION_IDS",
    "accepted_action",
    "catalog",
    "check_args",
    "openable",
    "view_id_of",
]

PANEL_ACTION_TOOL = "panel_action"
CATALOG_PATH = Path(__file__).with_name("panel_action_catalog.json")

#: Every action id the server may emit. All are view changes; none writes.
VIEW_ACTION_IDS: frozenset[str] = frozenset({
    "open", "close", "focus", "bind", "set_tab", "set_window", "select", "fly_to", "set_layer", "home",
    # U-9, the Watch surface: narrow the alert log or the silences, focus one
    # alert, open one flag's two versions — what is shown, never what is kept.
    "filter_alerts", "select_alert", "filter_silences", "open_flag",
    # U-10 — open the Record sheet on one record (optionally one cited sentence).
    "open_record",
    # U-11, the Ledger: one forecast brought to the point (also the Gate's
    # proposal), the list narrowed to open / resolved / all, one evaluation run.
    "show_forecast", "filter", "show_run",
})

#: The union of argument names any view action takes (the closed tool schema's
#: ``args`` properties).
ARG_NAMES: tuple[str, ...] = (
    "alert", "cite", "east", "flag", "forecast", "from", "group", "lat", "layer", "lon", "north", "on", "paged",
    "place", "ref", "run", "severity", "show", "side", "south", "span", "state", "subject", "tab", "to", "west",
    "zoom",
)


@lru_cache(maxsize=1)
def catalog() -> dict[str, Any]:
    """The catalogue; raises when it declares a non-view action."""
    doc = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    ids: set[str] = set(doc["common_actions"]) | {"bind"}
    for view in doc["views"].values():
        ids |= set(view["actions"])
    names: set[str] = set()
    for args in [*doc["common_actions"].values(), doc["bind_subject"], doc["bind_unit"],
                 *(a for v in doc["views"].values() for a in v["actions"].values())]:
        names |= {a["name"] for a in args}
    stray = ids - VIEW_ACTION_IDS
    if stray:
        raise ValueError(f"the panel catalogue declares non-view actions: {sorted(stray)}")
    if not names <= set(ARG_NAMES):
        raise ValueError(f"the panel catalogue declares unknown arguments: {sorted(names - set(ARG_NAMES))}")
    return doc


def view_id_of(instance_id: str) -> str:
    """``subject.page:brazil`` → ``subject.page``; ``view:…`` / ``slot:…`` are whole ids."""
    views = catalog()["views"]
    if instance_id in views:
        return instance_id
    head, sep, _rest = instance_id.partition(":")
    if sep and head not in ("slot", "view"):
        return head
    return instance_id


def openable(instance_id: str, on_screen_ids: Iterable[str] = ()) -> bool:
    """On screen now, or a view the console declares (so it can be opened)."""
    return instance_id in set(on_screen_ids) or view_id_of(instance_id) in catalog()["views"]


def accepted_action(view_id: str, action: str) -> list[dict[str, Any]] | None:
    """The action's argument specs when ``view_id`` accepts it (the console's rule)."""
    doc = catalog()
    view = doc["views"].get(view_id)
    if view is None:
        return None
    if action in doc["common_actions"]:
        return doc["common_actions"][action]
    if action in view["actions"]:
        return view["actions"][action]
    if action == "bind":
        if "subject" in view["binds"]:
            return doc["bind_subject"]
        if "unit" in view["binds"]:
            return doc["bind_unit"]
    return None


def check_args(specs: Iterable[Mapping[str, Any]], args: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """``(checked_args, problem)`` — the console's ``checkArgs``, plus unknown-arg refusal."""
    specs = list(specs)
    known = {s["name"] for s in specs}
    extra = [k for k in args if k not in known and args[k] not in (None, "")]
    if extra:
        return None, f"unexpected argument(s) {', '.join(sorted(extra))}"
    out: dict[str, Any] = {}
    for s in specs:
        name = s["name"]
        v = args.get(name)
        if v is None or v == "":
            if s.get("required"):
                return None, f"missing {name}"
            continue
        kind = s["type"]
        if kind == "string" and not isinstance(v, str):
            return None, f"{name} must be text"
        if kind == "boolean" and not isinstance(v, bool):
            return None, f"{name} must be true or false"
        if kind == "number" and (isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)):
            return None, f"{name} must be a number"
        if kind == "enum" and (not isinstance(v, str) or v not in (s.get("values") or [])):
            return None, f"{name} must be one of {', '.join(s.get('values') or [])}"
        if kind == "ref":
            if not isinstance(v, Mapping) or not isinstance(v.get("kind"), str) or not v.get("id"):
                return None, f"{name} must be a {{kind, id}} ref"
            if s.get("ref_kinds") and v["kind"] not in s["ref_kinds"]:
                return None, f"{name} takes {' or '.join(s['ref_kinds'])}, not {v['kind']}"
            v = {"kind": str(v["kind"]), "id": str(v["id"]),
                 **({"label": str(v["label"])[:80]} if v.get("label") else {})}
        out[name] = v
    return out, None

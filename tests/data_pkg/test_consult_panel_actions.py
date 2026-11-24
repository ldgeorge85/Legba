# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Q-1 — consult PANEL ACTIONS: the console's contracts, one catalogue, view actions only.

* The server catalogue is GENERATED from the console's
  ``panel-registry/contracts.ts`` (``scripts/gen_panel_action_catalog.py``);
  the drift test regenerates it and compares bytes.
* Every action id in it is a VIEW action; the loader refuses anything else, and
  ``panel_action`` is not a write tool.
* The case gate admits a panel action only for a view that is on screen or
  openable, an action that view accepts, typed args the console would accept,
  and refs the run has read; it is offered only on a turn armed to drive.
* The frozen case/1 contract (and its pinned bytes) is untouched, so every
  recorded case replays as before.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from typing import Any

import pytest

from legba.data import panel_actions as PA
from legba.data import workstation_vocabulary as V
from legba.data.analysts import consult_case as C
from legba.data.analysts import consult_case_schema as S
from legba.data.analysts.consult_case_schema import RestsOn
from legba.data.analysts.consult_tool_providers import RunFlags, offered_providers
from legba.data.stack.llm import tool_rounds as TR

REPO = Path(__file__).resolve().parents[2]
CONTRACTS = REPO / "legba-ui-v3" / "src" / "panel-registry" / "contracts.ts"


def _gen():
    spec = importlib.util.spec_from_file_location(
        "gen_panel_action_catalog", REPO / "scripts" / "gen_panel_action_catalog.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.skipif(
    not CONTRACTS.exists(),
    reason="legba-ui-v3/src/panel-registry/contracts.ts arrives with lane U-6; "
           "the merger merges U-6 first, then this drift test runs",
)
def test_committed_catalogue_matches_the_console_contracts() -> None:
    committed = PA.CATALOG_PATH.read_text(encoding="utf-8")
    assert committed == _gen().render(), "run scripts/gen_panel_action_catalog.py"


def test_the_reader_fails_loud_on_an_unknown_construct(tmp_path: Path) -> None:
    gen = _gen()
    (tmp_path / "panel-registry").mkdir()
    (tmp_path / "panel-registry" / "contracts.ts").write_text(
        "export const COMMON_ACTIONS: readonly PanelActionSpec[] = [makeSpec()]\n", encoding="utf-8",
    )
    with pytest.raises(gen.ContractsShapeError):
        gen.build(tmp_path)


def test_the_map_layer_ids_are_read_through_the_globe_catalogue() -> None:
    """U-7 derives MAP_LAYER_IDS from GLOBE_LAYERS; the reader evaluates exactly
    that filter: every row in order, a row carrying ``unavailable`` left out."""
    ids = _gen().globe_layer_ids(REPO / "legba-ui-v3" / "src")
    assert ids == ["change", "blanks", "series", "forecasts", "events", "at_sea"]
    assert PA.catalog()["views"]["v4.map"]["actions"]["set_layer"][0]["values"] == ids


def test_a_number_argument_is_a_finite_number_never_a_bool() -> None:
    specs = [{"name": "lon", "type": "number", "required": True}]
    assert PA.check_args(specs, {"lon": 12.5}) == ({"lon": 12.5}, None)
    assert PA.check_args(specs, {"lon": 3}) == ({"lon": 3}, None)
    for bad in (True, "12.5", float("inf"), float("nan")):
        assert PA.check_args(specs, {"lon": bad})[1] == "lon must be a number"


def test_catalogue_is_view_actions_only_and_not_a_write() -> None:
    doc = PA.catalog()
    ids = set(doc["common_actions"]) | {a for v in doc["views"].values() for a in v["actions"]}
    assert ids <= PA.VIEW_ACTION_IDS
    assert PA.PANEL_ACTION_TOOL not in TR.WRITE_TOOLS and not TR.is_write_tool(PA.PANEL_ACTION_TOOL)
    assert PA.PANEL_ACTION_TOOL in S.DRIVE_TOOLS


def test_loader_refuses_a_catalogue_that_declares_a_write(monkeypatch, tmp_path: Path) -> None:
    doc = json.loads(PA.CATALOG_PATH.read_text(encoding="utf-8"))
    doc["views"]["v4.map"]["actions"]["delete_record"] = []
    bad = tmp_path / "panel_action_catalog.json"
    bad.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(PA, "CATALOG_PATH", bad)
    PA.catalog.cache_clear()
    try:
        with pytest.raises(ValueError, match="non-view actions"):
            PA.catalog()
    finally:
        PA.catalog.cache_clear()


def test_case_contract_bytes_are_unchanged_so_old_cases_replay() -> None:
    from .test_consult_case_schema import CONTRACT_SHA256

    assert S.contract_sha256() == CONTRACT_SHA256
    assert "panel_action" not in V.DRIVE_TOOL_NAMES  # an additive name, not a case/1 change


def test_panel_action_is_offered_only_on_a_turn_armed_to_drive() -> None:
    unarmed = offered_providers(RunFlags.from_request({}))
    armed = offered_providers(RunFlags.from_request({"present_case": True}))
    assert not any("panel_action" in p.tool_names for p in unarmed)
    assert any("panel_action" in p.tool_names for p in armed)


async def _noop(_step: dict[str, Any]) -> None:
    return None


def _gate(on_screen: list[str] | None = None) -> C.CaseGate:
    gate = C.CaseGate(binding=None, record=_noop, on_screen_ids=tuple(on_screen or ()))
    gate.read_set[("target", "subject_a")] = RestsOn(1, "list_targets")
    gate.read_set[("signal", "s1")] = RestsOn(1, "search_signals")
    return gate


CITE = [{"kind": "signal", "id": "s1"}]


@pytest.mark.parametrize("args,expect", [
    ({"view": "v4.map", "action": "set_layer", "args": {"layer": "events", "on": True}}, None),
    ({"view": "v4.map", "action": "fly_to", "args": {"ref": {"kind": "target", "id": "subject_a"}}}, None),
    ({"view": "subject.page:subject_a", "action": "set_tab", "args": {"tab": "evidence"}}, None),
    ({"view": "v4.morning_read", "action": "set_window", "args": {"span": "7d"}}, None),
    ({"view": "v4.map", "action": "open", "args": {"place": "panel"}}, None),
    ({"view": "no.such_view", "action": "open", "args": {}}, "kind_not_drivable"),
    ({"view": "v4.map", "action": "set_tab", "args": {"tab": "x"}}, "invalid_arg"),
    ({"view": "v4.map", "action": "set_layer", "args": {"layer": "heat", "on": True}}, "invalid_arg"),
    ({"view": "v4.map", "action": "set_layer", "args": {"layer": "events"}}, "invalid_arg"),
    ({"view": "v4.map", "action": "fly_to", "args": {"ref": {"kind": "report", "id": "r"}}}, "invalid_arg"),
    # U-7: the one map contract — a position or an area by number, home, a window by dates.
    ({"view": "v4.map", "action": "fly_to", "args": {"lon": 56.3, "lat": 26.6, "zoom": 5}}, None),
    ({"view": "v4.map", "action": "fly_to", "args": {"west": 30, "south": 10, "east": 60.5, "north": 30}}, None),
    ({"view": "v4.map", "action": "fly_to", "args": {"lon": "56.3", "lat": 26.6}}, "invalid_arg"),
    ({"view": "v4.map", "action": "fly_to", "args": {"lon": True, "lat": 26.6}}, "invalid_arg"),
    ({"view": "v4.map", "action": "fly_to", "args": {"lon": float("nan"), "lat": 26.6}}, "invalid_arg"),
    ({"view": "v4.map", "action": "home", "args": {}}, None),
    ({"view": "v4.map", "action": "set_window", "args": {"from": "2026-11-01T00:00:00Z", "to": "2026-11-08T00:00:00Z"}}, None),
    ({"view": "v4.map", "action": "set_layer", "args": {"layer": "drift", "on": True}}, "invalid_arg"),
    ({"view": "v4.map", "action": "layer", "args": {"layer": "events", "on": True}}, "invalid_arg"),
    ({"view": "v4.map", "action": "fly_to", "args": {"ref": {"kind": "target", "id": "unread"}}}, "ref_not_read"),
])
def test_the_gate_applies_the_console_contract(args: dict[str, Any], expect: str | None) -> None:
    refusal = _gate()._precheck("panel_action", {**args, "cites": CITE})
    assert (refusal[0] if refusal else None) == expect, refusal


def test_an_on_screen_instance_is_drivable_and_cites_are_required() -> None:
    gate = _gate(on_screen=["v4.map"])
    assert gate._precheck("panel_action", {"view": "v4.map", "action": "focus", "args": {}, "cites": CITE}) is None
    assert gate._precheck("panel_action", {"view": "v4.map", "action": "focus", "args": {}})[0] == "no_citation"


@pytest.mark.asyncio
async def test_the_canonicalizer_keeps_view_action_and_typed_args() -> None:
    from legba.data.analysts.agency.tools import ToolCall
    from legba.data.analysts.agency.workstation_drive import panel_action_tool

    call = ToolCall(pack_id=V.WORKSTATION_DRIVE_PACK_ID, tool_name="panel_action", args={
        "view": "v4.map", "action": "set_layer", "args": {"layer": "events", "on": True}, "cites": CITE,
    })
    out = (await panel_action_tool(call, None, None)).output
    assert out == {"action": "panel_action",
                   "args": {"view": "v4.map", "action": "set_layer", "args": {"layer": "events", "on": True}},
                   "cites": CITE}


def test_the_case_prompt_lists_the_catalogue_compactly() -> None:
    line = C.panel_catalogue_line()
    assert "v4.map: fly_to(ref,lon,lat,zoom,west,south,east,north)" in line
    assert "set_layer(layer=change|blanks|series|forecasts|events|at_sea,on)" in line
    # roll 84: U-9 (Watch), U-10 (Record sheet) and U-11 (Ledger) together add
    # eight declared view actions (~1540 chars); each lane alone was under 1400.
    assert len(line) < 1700

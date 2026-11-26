# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The frozen case contract (C-1b, design C-1a §2/§4/§6) — bytes pinned.

The UI builds against ``consult_case_schema.contract_document()`` in
parallel, so its bytes are pinned here: a change to the nine tool schemas, the
frame shapes or the record shape turns this red and must be said out loud.

Also the vocabulary drift guards (C-1a §6 "one vocabulary file, two readers"):
the committed UI JSON is exactly what the generator prints, and every id the
vocabulary names exists in the TypeScript source it was read from.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from legba.data import workstation_vocabulary as V
from legba.data.analysts import consult_case_schema as S
from legba.data.stack.llm import tool_rounds as TR

REPO = Path(__file__).resolve().parents[2]
UI = REPO / "legba-ui-v3" / "src"

#: THE PIN. Changing the contract changes this; say so in the change description.
CONTRACT_SHA256 = "bcc36c2682399999c69a8cf97c6ae4ff879679b5c986f5d61d61ff8d7f7f1288"


def test_contract_bytes_are_pinned() -> None:
    assert S.contract_sha256() == CONTRACT_SHA256


def test_committed_ui_json_matches_the_generator() -> None:
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "gen_workstation_vocabulary", REPO / "scripts" / "gen_workstation_vocabulary.py",
    )
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    committed = (UI / "lib" / "workstationVocabulary.json").read_text(encoding="utf-8")
    assert committed == mod.render(), "run scripts/gen_workstation_vocabulary.py"


def test_nine_tools_in_vocabulary_order_and_registered() -> None:
    assert tuple(S.DRIVE_TOOL_SPECS) == V.DRIVE_TOOL_NAMES
    assert len(V.DRIVE_TOOL_NAMES) == 9
    for name, spec in S.DRIVE_TOOL_SPECS.items():
        assert TR.TOOL_SCHEMAS[name] is spec
    # Drive tools write nothing: not WRITE_TOOLS, and not treated as writes.
    assert not (S.DRIVE_TOOLS & TR.WRITE_TOOLS)
    assert not any(TR.is_write_tool(n) for n in S.DRIVE_TOOLS)


def _assert_closed(schema: Any, where: str) -> None:
    if not isinstance(schema, dict):
        return
    if schema.get("type") == "object":
        assert schema.get("additionalProperties") is False, f"{where} is open"
        for key, sub in (schema.get("properties") or {}).items():
            _assert_closed(sub, f"{where}.{key}")
    if schema.get("type") == "array":
        _assert_closed(schema.get("items"), f"{where}[]")
    for branch in schema.get("oneOf") or ():
        _assert_closed(branch, where)


@pytest.mark.parametrize("name", V.DRIVE_TOOL_NAMES)
def test_every_tool_schema_is_closed_at_every_level(name: str) -> None:
    _assert_closed(dict(S.DRIVE_TOOL_SPECS[name].json_schema), name)


def test_register_tool_schema_refuses_a_second_grammar() -> None:
    spec = S.DRIVE_TOOL_SPECS["done"]
    TR.register_tool_schema(spec)  # the same spec again is a no-op
    other = TR.ToolSpec(name="done", description="x", json_schema={"type": "object"})
    with pytest.raises(ValueError):
        TR.register_tool_schema(other)
    assert TR.TOOL_SCHEMAS["done"] is spec


def test_case_block_causes_are_their_own_vocabulary() -> None:
    assert V.CASE_BLOCK_CAUSES.isdisjoint(TR.ARG_BLOCK_CAUSES)
    assert "drive_not_armed" in V.CASE_BLOCK_CAUSES
    assert "ref_not_read" in V.CASE_BLOCK_CAUSES


def test_layouts_and_missions_only_seed_drivable_tiles() -> None:
    allowed = set(V.DRIVABLE_KINDS) | {"system.consult"}
    for lid, layout in V.ARRANGEABLE_LAYOUTS.items():
        assert set(layout["seeds"]) <= allowed, lid
    for mid, mission in V.MISSIONS.items():
        assert mission["stance"] in V.ARRANGEABLE_LAYOUTS, mid
        assert V.ARRANGEABLE_LAYOUTS[mission["stance"]]["type"] == "stance"
        assert set(mission["adds"]) <= allowed, mid
    assert "system.consult" not in V.DRIVABLE_KINDS
    for kind in V.PANEL_TABS:
        assert kind in V.DRIVABLE_KINDS


# ---------------------------------------------------------------------------
# Drift vs the TypeScript sources
# ---------------------------------------------------------------------------


def _ts(rel: str) -> str:
    return (UI / rel).read_text(encoding="utf-8")


def _union(text: str, type_name: str) -> set[str]:
    """The string members of ``export type <type_name> = 'a' | 'b' ...``."""
    head = re.search(rf"export type {type_name}\s*=", text)
    assert head, type_name
    body = re.sub(r"/\*.*?\*/|//[^\n]*", "", text[head.end():], flags=re.S)
    members: set[str] = set()
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        if not (line.startswith("|") or line.startswith("'")):
            break
        members |= set(re.findall(r"'([a-z0-9_.]+)'", line))
    assert members, type_name
    return members


def test_drivable_kinds_exist_in_the_registry() -> None:
    kinds = _union(_ts("types.ts"), "PanelKind")
    registry = _ts("panel-registry/registry.ts")
    for kind, meta in V.DRIVABLE_KINDS.items():
        assert kind in kinds, kind
        assert f"'{kind}': {{" in registry, kind
        # A bound kind is a requiresBinding kind in the registry, and vice versa.
        row = re.search(rf"def\('{re.escape(kind)}',[^\n]*\)", registry)
        assert row, kind
        assert (", true, [" in row.group(0)) == (meta["subject"] is not None
                                                 and kind != "analysis.cross_framing"), kind


def test_panel_tabs_exist_in_their_panels() -> None:
    files = {
        "system.timeline": ("panels/merged/Timeline.tsx", "Mode"),
        "system.alerts_watches": ("panels/merged/AlertsWatches.tsx", "Tab"),
        "system.provenance": ("panels/merged/Provenance.tsx", "Tab"),
        "system.register": ("panels/merged/Register.tsx", "Tab"),
        "system.wall": ("panels/merged/Wall.tsx", "Tab"),
        "subject.page": ("lib/subjectPage.ts", "SubjectTab"),
    }
    assert set(files) == set(V.PANEL_TABS)
    for kind, (rel, type_name) in files.items():
        m = re.search(rf"type {type_name} = ([^\n]+)", _ts(rel))
        assert m, rel
        assert set(re.findall(r"'([a-z_]+)'", m.group(1))) == set(V.PANEL_TABS[kind]), kind


def _seeds(text: str, layout_id: str) -> set[str]:
    blocks = re.split(r"\n\s*id: '", text)
    for block in blocks[1:]:
        if block.startswith(f"{layout_id}'"):
            return set(re.findall(r"kind: '([a-z0-9_.]+)'", block))
    raise AssertionError(f"layout {layout_id!r} not found")


def test_layout_ids_and_seeds_match_the_shell() -> None:
    stances = _ts("lib/workspaces.ts")
    presets = _ts("lib/layoutPresets.ts")
    for lid, layout in V.ARRANGEABLE_LAYOUTS.items():
        text = stances if layout["type"] == "stance" else presets
        assert _seeds(text, lid) == set(layout["seeds"]), lid


def test_missions_layers_spans_and_ref_kinds_match_the_shell() -> None:
    missions = _ts("lib/missions.ts")
    assert set(V.MISSIONS) <= _union(missions, "MissionId")
    for mid, mission in V.MISSIONS.items():
        block = missions.split(f"id: '{mid}'", 1)[1].split("\n  {", 1)[0]
        assert f"workspace: '{mission['stance']}'" in block, mid
        assert set(re.findall(r"kind: '([a-z0-9_]+\.[a-z0-9_.]+)'", block)) == set(mission["adds"]), mid
    assert set(V.MAP_LAYERS) == _union(_ts("v4/world/worldState.ts"), "WorldLayer")
    spans = set(re.findall(r"\{ id: '([0-9a-z]+)', label", _ts("lib/mapTime.ts")))
    assert set(V.SPAN_PRESETS) == spans
    assert set(V.REF_KINDS) <= _union(_ts("state/selection.ts"), "SelectionKind")
    assert set(V.SCOPE_KINDS) == _union(_ts("state/scope.ts"), "ScopeKind")


# ---------------------------------------------------------------------------
# validate_args goes DEEP for a closed schema only
# ---------------------------------------------------------------------------


def test_validate_args_is_deep_for_the_closed_drive_schemas() -> None:
    spec = S.DRIVE_TOOL_SPECS
    cite = [{"kind": "target", "id": "t1"}]

    def check(name: str, args: dict[str, Any]) -> str | None:
        return TR.validate_args(spec[name], args)

    assert check("map", {"view": "reset", "layers": ["events"], "cites": cite}) is None
    assert check("map", {"view": "reset", "layers": ["heat"], "cites": cite}).startswith(
        "invalid_arg: map argument 'layers[0]' must be one of")
    assert check("focus", {"ref": {"kind": "target", "id": "t", "x": 1}, "cites": cite}).startswith(
        "invalid_arg: focus argument 'ref' has no field x")
    assert check("focus", {"ref": {"kind": "target"}, "cites": cite}).startswith(
        "invalid_arg: focus argument 'ref' requires 'id'")
    assert check("narrate", {"text": "x" * 601, "cites": cite}).startswith(
        "invalid_arg: narrate argument 'text' is longer than 600")
    assert check("narrate", {"text": "x", "cites": cite * 9}).startswith(
        "invalid_arg: narrate argument 'cites' takes at most 8")
    assert check("narrate", {"text": "x", "cites": "t1"}).startswith(
        "invalid_arg: narrate argument 'cites' must be a list")


def test_open_schemas_keep_the_shallow_tolerant_check() -> None:
    # An authored (open) schema is validated exactly as before: a bare string
    # where a list is declared is still the handlers' documented tolerance.
    assert TR.validate_args(TR.TOOL_SCHEMAS["series_compare"], {
        "series_id": "s", "subjects": "US", "from": "2010", "to": "2020"}) is None
    assert TR.validate_args(TR.TOOL_SCHEMAS["search_corpus"], {
        "query": "q", "filters": {"anything": ["goes", 1]}}) is None

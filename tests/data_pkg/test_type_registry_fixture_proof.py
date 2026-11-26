# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""BEFORE/AFTER — adding a deterministic handler and an alert kind.

The fixture package ``tests/data_pkg/type_registry_fixture`` adds one handler
(``fixture_echo``, with an inline option) and one alert kind
(``fixture_echo_alert``) through the registry, the way an installed package's
``legba.types`` entry point would. This test proves every derived table picks
them up with NO edit to any shared file, and that the invariants hold for them.

The count, for the record: the fixture is THREE new files (handler code, its
declaration, the alert kind's declaration) plus THIS test = four. In-tree, a
a real change adds a descriptor YAML (and, when the host activates it, one
``descriptors/LIFECYCLE.d`` row file), and re-runs ``scripts/gen_type_tables.py``,
which regenerates the docs tables (and, for a ``finding`` handler, the console
mirror) — generated lines, no hand edits. The measured cost of the same change
this week was 18 files, 14 of them edits to shared files.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from legba.data.alerts.kinds import build_kinds
from legba.data.alerts.topics import build_topic_by_kind
from legba.data.analysts.deterministic import dispatch_table, output_kinds
from legba.data.option_catalog.handler_options_declared import splice_declared
from legba.data.provenance.kinds import OutputKind
from legba.data.type_registry import TypeRegistry, default_registry
from legba.data.type_registry.deterministic_handlers import structural_exempt_ids

FIXTURE = "tests.data_pkg.type_registry_fixture"
FIXTURE_DIR = Path(__file__).resolve().parent / "type_registry_fixture"


@pytest.fixture(scope="module")
def reg() -> TypeRegistry:
    return TypeRegistry.build(
        extra_modules=[f"{FIXTURE}.echo_handler_decl", f"{FIXTURE}.echo_alert_decl"],
        entry_point_group=None,
    )


def test_the_fixture_is_three_files_plus_this_test():
    files = sorted(p.name for p in FIXTURE_DIR.glob("*.py") if p.name != "__init__.py")
    assert files == ["echo_alert_decl.py", "echo_handler.py", "echo_handler_decl.py"]


def test_every_derived_table_picks_the_new_types_up(reg):
    base = default_registry()
    table = dispatch_table(reg)
    assert set(table) == set(dispatch_table(base)) | {"fixture_echo"}
    assert output_kinds(reg)["fixture_echo"] is OutputKind.FINDING
    assert structural_exempt_ids(reg) == structural_exempt_ids(base) | {"fixture_echo"}

    kinds = build_kinds(reg)
    names = list(kinds)
    assert names.index("fixture_echo_alert") == names.index("due_and_absent") + 1
    assert build_topic_by_kind(reg)["fixture_echo_alert"] == "engine"

    catalog: dict = {}
    splice_declared(catalog, reg)
    assert [s.name for s in catalog["fixture_echo"]] == ["label"]


@pytest.mark.asyncio
async def test_the_new_handler_dispatches_and_runs(reg):
    handle = dispatch_table(reg)["fixture_echo"]
    result = await handle([{}, {}], {"sub_handler": "fixture_echo", "label": "x",
                                    "run_id": uuid4()}, None)
    assert result.finding.data == {"sub_handler": "fixture_echo", "rows": 2, "label": "x"}


def test_the_new_types_satisfy_the_invariants(reg):
    handler = reg.get("deterministic_handler", "fixture_echo")
    alert = reg.get("alert_kind", "fixture_echo_alert")
    assert set(handler.attr("alerts")) <= set(reg.ids("alert_kind"))
    assert handler.retention == "findings" and handler.doc
    assert alert.attr("paging") == "logged_only" and alert.evidence == "never"

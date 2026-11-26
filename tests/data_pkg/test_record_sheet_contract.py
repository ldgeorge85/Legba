# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The Record sheet's contract pieces, no database.

* the reads route and its leaf import in the SLIM registry image (nothing
  from ``legba.data.analysts`` / ``legba.runtime``), mounted by the app;
* the text route's refusing-licence set is the evidence archiver's retention
  refusal, never a fork of it;
* Consult's ``panel_action`` catalogue carries ``open_record`` on the Record
  (``system.inspector``) with a typed ``ref`` and an optional numeric
  ``cite``, generated from the console's contract;
* a fact is a lineage root (the sheet opens on one).
"""
from __future__ import annotations

from legba.data import panel_actions as PA
from legba.data import record_sheet_reads as R
from legba.data.analysts.consult_case_schema import PANEL_ACTION_SPEC
from legba.data.analysts.deterministic_handlers.evidence_archiver import FORBID_RETENTION_CLASSES
from legba.data.registry import lineage_api
from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe


def test_the_record_routes_import_in_the_slim_image() -> None:
    _assert_ok(_probe(
        """
        import legba.data.registry.server as server
        import legba.data.registry.record_sheet_api as api
        import legba.data.record_sheet_reads as reads

        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable from the record routes: %r" % leaked
        assert api.build_record_sheet_router is not None and reads.record_receipts is not None
        print("OK")
        """
    ))


def test_the_app_mounts_the_record_routes() -> None:
    from pathlib import Path

    text = Path(lineage_api.__file__).with_name("server.py").read_text(encoding="utf-8")
    assert "build_record_sheet_router(deps), prefix=\"/api/v1/v3\"" in text


def test_the_refusing_licences_are_the_archivers() -> None:
    assert R.FORBID_TEXT_LICENSE_CLASSES == FORBID_RETENTION_CLASSES


def test_the_reads_are_bounded() -> None:
    assert R.MAX_IDS == 25 and R.MAX_LATER == 8 and R.MAX_TEXT_CHARS == 100_000
    assert R.NO_RECEIPT_REASONS == ("before_receipts_start", "source_kind_without_receipts", "not_linked")


def test_open_record_is_a_panel_action_on_the_record() -> None:
    specs = PA.accepted_action("system.inspector", "open_record")
    assert specs is not None
    assert {s["name"]: s["type"] for s in specs} == {"ref": "ref", "cite": "number"}
    ok, problem = PA.check_args(specs, {"ref": {"kind": "finding", "id": "f-1"}, "cite": 3})
    assert problem is None and ok == {"ref": {"kind": "finding", "id": "f-1"}, "cite": 3}
    assert PA.check_args(specs, {"ref": {"kind": "fact", "id": "x"}})[1] is None
    assert "takes" in (PA.check_args(specs, {"ref": {"kind": "entity", "id": "e"}})[1] or "")
    assert "number" in (PA.check_args(specs, {"ref": {"kind": "finding", "id": "f"}, "cite": "3"})[1] or "")
    assert "open_record" in PA.VIEW_ACTION_IDS and "cite" in PA.ARG_NAMES
    schema = PANEL_ACTION_SPEC.json_schema
    assert "open_record" in schema["properties"]["action"]["enum"]
    assert schema["properties"]["args"]["properties"]["cite"]["type"] == "integer"


def test_a_fact_is_a_lineage_root() -> None:
    assert lineage_api._TABLES_BY_KIND["fact"] == ("facts", None)

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-8 — every list route declares a light projection (and answers it by
default) or is exempt with a reason; every list route has a page budget.

"A list route" = a mounted GET whose parameters include ``limit`` or
``cursor`` (the registry's paging contract), read off the real app's OpenAPI.
The fixture byte-budget tests for the light-by-default routes live beside
their routes' own fixtures (``test_substrate_reads_api.py``,
``test_journal_api.py``, ``test_runtime_telemetry_api.py``) and read their
budgets from ``list_weights.LIST_ROUTES``.
"""
from __future__ import annotations

import logging

import pytest

from legba.data.registry import list_weights as lw


@pytest.fixture(scope="module")
def openapi_gets() -> dict[str, list[str]]:
    logging.disable(logging.WARNING)
    try:
        from legba.data.registry.server import create_app

        spec = create_app(enable_vocabulary_subscription=False).openapi()
    finally:
        logging.disable(logging.NOTSET)
    return {
        path: [p["name"] for p in ops["get"].get("parameters", [])]
        for path, ops in spec["paths"].items() if "get" in ops
    }


def test_every_paging_list_route_is_declared(openapi_gets):
    paging = {p for p, params in openapi_gets.items()
              if "limit" in params or "cursor" in params}
    missing = sorted(paging - set(lw.LIST_ROUTES))
    assert not missing, (
        "list routes with no declared weight (add them to "
        f"registry/list_weights.py, light by default or exempt with a reason): {missing}")


def test_no_declared_route_is_stale(openapi_gets):
    stale = sorted(set(lw.LIST_ROUTES) - set(openapi_gets))
    assert not stale, f"declared list routes the app no longer mounts: {stale}"


def test_each_route_is_light_by_default_or_exempt_with_a_reason():
    assert lw.undeclared_problems() == []


def test_light_by_default_routes_take_fields_and_name_the_full_read(openapi_gets):
    for path, w in lw.LIST_ROUTES.items():
        if w.light_by_default:
            assert "fields" in openapi_gets[path], path
            assert w.full, f"{path}: say how the full record is read"


def test_the_finding_by_id_route_is_mounted(openapi_gets):
    assert "/api/v1/findings/{finding_id}" in openapi_gets


def test_the_defaults_are_the_light_weight():
    from legba.data.registry import journal_api, runtime_telemetry_api, substrate_reads_api

    assert substrate_reads_api.FINDINGS_DEFAULT_FIELDS == "summary"
    assert journal_api.JOURNAL_DEFAULT_FIELDS == "summary"
    assert runtime_telemetry_api.OUTPUTS_DEFAULT_FIELDS == "summary"


def test_the_by_id_route_imports_in_the_slim_registry_image() -> None:
    from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe

    _assert_ok(_probe(
        """
        import legba.data.registry.server as server
        import legba.data.registry.findings_by_id_api as by_id
        import legba.data.registry.list_weights as lw
        leaked = sorted(
            m for m in sys.modules
            if m.startswith("legba.data.analysts") or m.startswith("legba.runtime")
        )
        assert not leaked, "analyst/runtime reachable: %r" % leaked
        assert by_id.build_finding_by_id_router is not None
        assert lw.LIST_ROUTES
        print("OK")
        """
    ))


# ---------------------------------------------------------------------------
# Walk fix (the Watch page 4.57 MB against 2 MB, the
# phone landing's largest response 0.279 MB against 0.25 MB).
# ---------------------------------------------------------------------------


def test_signals_are_light_by_default(openapi_gets):
    from legba.data.registry import signal_list_weight as sw

    assert sw.SIGNALS_DEFAULT_FIELDS == "summary"
    assert lw.LIST_ROUTES["/api/v1/signals"].light_by_default
    assert "fields" in openapi_gets["/api/v1/signals"]


def test_the_descriptor_list_takes_a_summary_by_name(openapi_gets):
    w = lw.LIST_ROUTES["/api/v1/registry/descriptors"]
    assert w.light == "fields=summary" and not w.light_by_default and w.exempt
    assert "fields" in openapi_gets["/api/v1/registry/descriptors"]


def test_the_light_modules_import_in_the_slim_registry_image() -> None:
    from tests.data_pkg.test_collection_slim_imports import _assert_ok, _probe

    _assert_ok(_probe(
        """
        import legba.data.registry.signal_list_weight as sw
        import legba.data.registry.descriptor_list_weight as dw
        import legba.data.registry.cited_ref_kinds as ck
        import legba.data.registry.event_list_weight as ew
        assert sw.SignalSummaryRow and dw.summary_body and ck.typed_refs and ew.EventSummaryRow
        print("OK")
        """
    ))


def test_events_take_a_summary_by_name(openapi_gets):
    """The Watch page 2.01 MB against 2.00, its largest read
    ``/v3/events?limit=500`` at 0.42 MB; the map and the time band ask the
    light row by name, every other reader keeps the whole row."""
    from legba.data.registry import event_list_weight as ew

    w = lw.LIST_ROUTES["/api/v1/v3/events"]
    assert w.light == "fields=summary" and not w.light_by_default and w.exempt
    assert ew.EVENTS_DEFAULT_FIELDS == "full"
    assert "fields" in openapi_gets["/api/v1/v3/events"]

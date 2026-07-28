# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-173 dispatcher tests for the deterministic analyst kind.

Unit tests only — sub-handler internals live in
``test_analyst_deterministic_handlers.py``. These tests cover:

  * Module-level contract: KIND_NAME constant present + correct.
  * Sub-handler dispatch table: all four expected handlers registered.
  * ``run_method`` raises on missing or unknown ``options.sub_handler``.
  * ``run_method`` routes to the right sub-handler when ``sub_handler``
    is valid — verified by monkey-patching one handler and asserting it
    was the one invoked.
  * Result type contract: returns ``AnalystMethodResult`` with a
    ``FindingPayload`` carrying ``data.sub_handler`` set to the dispatched
    name and zero token usage.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic import (
    DeterministicDispatchError,
    KIND_NAME,
    SUB_HANDLERS,
    run_method,
)
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult


def test_kind_name_constant():
    assert KIND_NAME == "deterministic"
    assert deterministic.KIND_NAME == KIND_NAME


def test_sub_handler_registry_has_l173_four():
    """L-173 — the original four deterministic sub-handlers must remain registered.

    L-203 extends the registry with maintenance migrations; we don't pin the
    full set here so future migrations can land without rewriting the test.
    """
    l173_expected = {
        "graph_mining",
        "anomaly_detection",
        "structural_balance",
        "calibration_tracking",
    }
    assert l173_expected.issubset(set(SUB_HANDLERS))
    for name, fn in SUB_HANDLERS.items():
        assert callable(fn), f"{name} entry is not callable"


def test_run_method_missing_sub_handler_raises():
    import asyncio
    with pytest.raises(DeterministicDispatchError) as exc:
        asyncio.run(run_method([], {}, None))
    assert "sub_handler" in str(exc.value)


def test_run_method_unknown_sub_handler_raises():
    import asyncio
    with pytest.raises(DeterministicDispatchError) as exc:
        asyncio.run(
            run_method([], {"sub_handler": "not_a_handler"}, None)
        )
    assert "unknown" in str(exc.value).lower()


@pytest.mark.asyncio
async def test_run_method_dispatches_to_named_handler(monkeypatch):
    """Replace one handler with a sentinel, invoke run_method, assert hit."""
    sentinel_called: dict[str, bool] = {"hit": False}

    async def fake_handler(inputs, options, deps):
        sentinel_called["hit"] = True
        return AnalystMethodResult(
            finding=FindingPayload(
                title="sentinel",
                body="",
                confidence=1.0,
                tags=["test"],
                data={"sub_handler": "graph_mining"},
            ),
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    monkeypatch.setitem(SUB_HANDLERS, "graph_mining", fake_handler)
    result = await run_method(
        [],
        {"sub_handler": "graph_mining", "analyst_id": "test", "run_id": uuid4()},
        None,
    )
    assert sentinel_called["hit"] is True
    assert isinstance(result, AnalystMethodResult)
    assert result.finding.data["sub_handler"] == "graph_mining"
    assert result.usage["prompt_tokens"] == 0


@pytest.mark.asyncio
async def test_run_method_rejects_wrong_return_type(monkeypatch):
    async def bad_handler(inputs, options, deps):
        return {"not": "an AnalystMethodResult"}

    monkeypatch.setitem(SUB_HANDLERS, "graph_mining", bad_handler)
    with pytest.raises(TypeError) as exc:
        await run_method(
            [], {"sub_handler": "graph_mining"}, None,
        )
    assert "AnalystMethodResult" in str(exc.value)


@pytest.mark.asyncio
async def test_run_method_each_real_sub_handler_returns_method_result():
    """Sanity: each real sub-handler returns the right shape on empty input."""
    for name in SUB_HANDLERS:
        if name in (
            "integrity_sweep",
            "composition_lineage_sweep",
            "alert_trigger_scan",
            # P2-3: refuses loud without a pool ("never report a zero-claim
            # calibration run without reading the substrate") — was missing
            # from this list when it landed; surfaced once the loop reached it.
            "band_calibration_tracker",
            # A7: same refuse-loud contract ("never report a zero-convergence
            # scan without reading the substrate"); exercised with a real pool
            # in test_geo_convergence_scan.py.
            "geo_convergence_scan",
            # C4: same refuse-loud contract ("never report a decay distribution
            # without reading the substrate"); exercised with a real pool in
            # test_fact_decay_scan.py.
            "fact_decay_scan",
            # A6 P3-3: same refuse-loud contract ("never report a zero-record
            # run without reading the substrate"); exercised with a real pool in
            # test_source_track_record_db.py.
            "source_track_record",
            # A7 P3-7: same refuse-loud contract ("never report a zero-desk
            # baseline without reading the substrate"); exercised with a real
            # pool in test_desk_baseline.py.
            "desk_baseline",
            # P4-1 (75958a5): same refuse-loud contract ("never report a
            # quiet zero-narrative run without reading the substrate") — was
            # missing from this list when it landed; surfaced once the loop
            # reached it (E-1 suite run). Exercised with a real pool in
            # test_narrative_mapper_db.py.
            "narrative_mapper",
        ):
            # These REFUSE LOUD without a live pg_pool by design — they must never
            # emit a zeroed clean finding without actually running their checks
            # (integrity_sweep per DIRECTION §9; composition_lineage_sweep per
            # P3-T6, "refuse rather than emit a zeroed clean lineage finding
            # without walking the tower"; alert_trigger_scan per P1-3, "refuse
            # rather than report a zero-alert scan without reading the
            # substrate"), so unlike the other deterministic handlers they have
            # no synthetic/no-pool path. Their empty-input contract is
            # exercised with a real/fake pool in their own test modules.
            continue
        result = await run_method(
            [],
            {"sub_handler": name, "analyst_id": "test", "run_id": uuid4()},
            None,
        )
        assert isinstance(result, AnalystMethodResult), name
        assert isinstance(result.finding, FindingPayload), name
        assert result.finding.data["sub_handler"] == name, name
        assert result.usage == {
            "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0,
        }, name

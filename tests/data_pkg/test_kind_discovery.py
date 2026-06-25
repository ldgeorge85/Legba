# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-241 — analyst- + output-kind discovery surface tests.

These exercise the host-side ``discover_analyst_kinds`` /
``discover_output_kinds`` registries that the Phase 5/6 daprd host calls
once at startup.  The discovery layer is the contract integration
between the per-kind modules and the runtime dispatcher.
"""

from __future__ import annotations

import pytest

from legba.data.analysts import KindHandler, discover_analyst_kinds
from legba.data.outputs import OutputHandler, discover_output_kinds
from legba.data.provenance.kinds import OutputKind


# ---------------------------------------------------------------------------
# Analyst kind discovery
# ---------------------------------------------------------------------------


_EXPECTED_ANALYST_KINDS = {
    "inline_target",
    "cross_target_raw",
    "meta_findings_synthesizer",
    "cross_analyst_correlator",
    "relationship_reifier",
    "consult_on_demand",
    "predictor",
    "deterministic",
    # The 11th OutputKind's producer — Legba's first-person reflective voice.
    "journal_assessor",
}


def test_discover_analyst_kinds_returns_all_first_party_kinds():
    """All 7 first-party analyst kinds register against the host."""
    registry = discover_analyst_kinds()
    assert _EXPECTED_ANALYST_KINDS.issubset(set(registry.keys())), (
        f"missing kinds: {_EXPECTED_ANALYST_KINDS - set(registry.keys())}"
    )


def test_discover_analyst_kinds_returns_typed_handler_bundles():
    """Each entry is a ``KindHandler`` with a callable ``run_method``."""
    registry = discover_analyst_kinds()
    for name, handler in registry.items():
        assert isinstance(handler, KindHandler), (name, type(handler))
        assert callable(handler.run_method), name
        assert handler.kind_name == name


def test_inline_target_carries_finding_output_kind():
    handler = discover_analyst_kinds()["inline_target"]
    assert handler.output_kind == OutputKind.FINDING
    # READ_SLICE = None — the kind uses the host's default signals reader.
    assert handler.read_slice is None
    # build_prompt_module is exposed (dspy-wrapped).
    assert callable(handler.build_prompt_module)


def test_predictor_declares_prediction_output_kind():
    handler = discover_analyst_kinds()["predictor"]
    assert handler.output_kind == OutputKind.PREDICTION


def test_journal_assessor_declares_journal_output_kind():
    """The 11th OutputKind's producer registers with OutputKind.JOURNAL and the
    Wave-0 default META reader (READ_SLICE=None)."""
    from legba.data.provenance.kinds import KIND_REGISTRY

    handler = discover_analyst_kinds()["journal_assessor"]
    assert handler.output_kind == OutputKind.JOURNAL
    assert handler.read_slice is None
    assert callable(handler.build_prompt_module)
    # the 11th kind is in the output-kind registry against the rebuilt set
    assert OutputKind.JOURNAL in KIND_REGISTRY
    assert KIND_REGISTRY[OutputKind.JOURNAL].table == "journal_entries"


def test_cross_target_raw_exposes_dedicated_reader():
    handler = discover_analyst_kinds()["cross_target_raw"]
    assert handler.output_kind == OutputKind.FINDING
    assert handler.read_slice is not None
    assert callable(handler.read_slice)


def test_meta_findings_synthesizer_exposes_dedicated_reader():
    handler = discover_analyst_kinds()["meta_findings_synthesizer"]
    assert handler.output_kind == OutputKind.FINDING
    assert handler.read_slice is not None
    assert callable(handler.read_slice)


def test_cross_analyst_correlator_exposes_dedicated_reader():
    handler = discover_analyst_kinds()["cross_analyst_correlator"]
    assert handler.output_kind == OutputKind.FINDING
    assert handler.read_slice is not None
    assert callable(handler.read_slice)


def test_consult_on_demand_has_no_substrate_reader():
    handler = discover_analyst_kinds()["consult_on_demand"]
    # Consult kind receives its inputs via the A2A skill / MCP tool /
    # panel — there is no per-cadence substrate read.
    assert handler.read_slice is None
    assert handler.output_kind == OutputKind.FINDING


def test_deterministic_kind_exposes_sub_handlers():
    """The deterministic kind dispatcher exposes the L-173 four sub-handlers.

    L-203 extends the SUB_HANDLERS table with maintenance-migrated sub-handlers;
    we assert subset here (not equality) so new migrations can land without
    rewriting the discovery test.
    """
    handler = discover_analyst_kinds()["deterministic"]
    assert handler.output_kind == OutputKind.FINDING
    # The deterministic module surfaces SUB_HANDLERS so discovery callers
    # can introspect the sub-handler table directly.
    assert hasattr(handler.module, "SUB_HANDLERS")
    l173_expected = {
        "graph_mining",
        "anomaly_detection",
        "structural_balance",
        "calibration_tracking",
    }
    assert l173_expected.issubset(set(handler.module.SUB_HANDLERS.keys()))


# ---------------------------------------------------------------------------
# Output kind discovery
# ---------------------------------------------------------------------------


_EXPECTED_OUTPUT_KINDS = {
    "substrate_writer",
    "nats_stream",
    "a2a_skill",
    "mcp_tool",
    "alert",
}


def test_discover_output_kinds_returns_all_five_first_party_kinds():
    registry = discover_output_kinds()
    assert _EXPECTED_OUTPUT_KINDS.issubset(set(registry.keys())), (
        f"missing kinds: {_EXPECTED_OUTPUT_KINDS - set(registry.keys())}"
    )


def test_alert_kind_exposes_emit():
    """``alert`` carries the uniform async ``emit`` surface."""
    handler = discover_output_kinds()["alert"]
    assert handler.emit is not None
    assert callable(handler.emit)


def test_nats_stream_kind_exposes_emit():
    """``nats_stream`` carries the uniform async ``emit`` surface."""
    handler = discover_output_kinds()["nats_stream"]
    assert handler.emit is not None
    assert callable(handler.emit)


@pytest.mark.parametrize("kind", ["substrate_writer", "a2a_skill", "mcp_tool"])
def test_surface_kinds_carry_no_uniform_emit(kind: str):
    """Surface-only output kinds (substrate / a2a_skill / mcp_tool) don't
    expose ``emit`` — they expose surface-specific helpers (``write_*``,
    ``register_*_route``, ``MCPToolRegistry``).  Discovery surfaces this
    by carrying ``emit = None``.
    """
    handler = discover_output_kinds()[kind]
    assert handler.emit is None

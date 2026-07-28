# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The "Findings as a real output type" split (trace-only META analysts).

The META analysts (relationship_reifier, competing_hypotheses, the
deterministic maintenance sub-handlers) used to write a redundant FINDING
*receipt* into ``analyst_outputs`` even though their REAL product is
side-written (nexuses / hypotheses / maintenance stamps) and every run is
already audited in ``analyst_traces``. They are now TRACE_ONLY: no
``analyst_outputs`` row, side-writes + trace intact. ``FINDING`` becomes a
genuine OutputKind emitted only by kinds that actually produce a finding.

Pure-logic coverage of the split table + the actor's per-run effective-kind
resolver. The live "no analyst_outputs row" behaviour is exercised by
``tests/runtime/test_trace_only_dispatch.py`` and against the running stack.
"""
from __future__ import annotations

import pytest

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.provenance.kinds import TRACE_ONLY, OutputKind


# The operator-confirmed trace-only deterministic sub-handlers (real product is
# side-written; run audited in analyst_traces; NO analyst_outputs receipt).
TRACE_ONLY_SUB_HANDLERS = (
    "structural_balance",
    "nexus_decay",
    "proposed_edge_governance",
    "entity_resolution",
    "cross_source_dedup",
    "cross_source_coalesce",
    "fact_decay",
    "finding_supersession",
    "integrity_sweep",
    "entity_gc",
)

# Sub-handlers that KEEP emitting a real analytical FINDING (unchanged).
KEEP_FINDING_SUB_HANDLERS = (
    "graph_mining",
    "anomaly_detection",
    "situation_clustering",
    "calibration_tracking",
    # P2-3 band-calibration harness — its summary IS the measurement product
    # the /eval/calibration band_calibration section reads.
    "band_calibration_tracker",
)


def test_trace_only_sentinel_is_a_singleton_not_an_output_kind():
    """TRACE_ONLY is its own sentinel — never a real OutputKind."""
    from legba.data.provenance.kinds import TRACE_ONLY as again

    assert TRACE_ONLY is again
    assert not isinstance(TRACE_ONLY, OutputKind)
    assert repr(TRACE_ONLY) == "TRACE_ONLY"
    # It has no `.value` — code paths must compare with `is`, never `.value`.
    assert not hasattr(TRACE_ONLY, "value")


@pytest.mark.parametrize("name", TRACE_ONLY_SUB_HANDLERS)
def test_trace_only_sub_handlers_are_marked_trace_only(name):
    """Each operator-confirmed maintenance sub-handler is TRACE_ONLY and still
    dispatchable (the side-write handler itself is untouched)."""
    assert name in SUB_HANDLERS, f"{name!r} dropped from SUB_HANDLERS"
    assert OUTPUT_KIND_BY_SUB_HANDLER[name] is TRACE_ONLY, (
        f"{name!r} must be TRACE_ONLY (no redundant FINDING receipt)"
    )


@pytest.mark.parametrize("name", KEEP_FINDING_SUB_HANDLERS)
def test_keep_finding_sub_handlers_still_emit_a_finding(name):
    """Sub-handlers that produce a genuine analytical finding stay FINDING."""
    assert OUTPUT_KIND_BY_SUB_HANDLER[name] is OutputKind.FINDING


def test_top_level_meta_kinds_are_trace_only():
    """relationship_reifier + competing_hypotheses declare TRACE_ONLY so the
    actor skips their analyst_outputs receipt while the side-writes
    (write_nexus / write_hypothesis) + the trace still run."""
    from legba.data.analysts import competing_hypotheses, relationship_reifier

    assert relationship_reifier.OUTPUT_KIND is TRACE_ONLY
    assert competing_hypotheses.OUTPUT_KIND is TRACE_ONLY


def test_resolver_picks_trace_only_for_maintenance_sub_handlers():
    """The actor's per-run resolver routes a deterministic run through the
    per-sub-handler table — trace-only for maintenance, FINDING otherwise."""
    from legba.runtime.dapr_actors import _resolve_effective_output_kind

    for name in TRACE_ONLY_SUB_HANDLERS:
        resolved = _resolve_effective_output_kind(
            kind="deterministic",
            bind_output_kind=OutputKind.FINDING,  # the deterministic bind default
            options={"sub_handler": name},
        )
        assert resolved is TRACE_ONLY, name

    for name in KEEP_FINDING_SUB_HANDLERS:
        resolved = _resolve_effective_output_kind(
            kind="deterministic",
            bind_output_kind=OutputKind.FINDING,
            options={"sub_handler": name},
        )
        assert resolved is OutputKind.FINDING, name


def test_resolver_passes_through_bind_kind_for_non_deterministic_kinds():
    """For top-level kinds the bind-time OUTPUT_KIND IS the effective kind —
    TRACE_ONLY for the two META kinds, the real kind for everyone else."""
    from legba.runtime.dapr_actors import _resolve_effective_output_kind

    # relationship_reifier / competing_hypotheses bind TRACE_ONLY directly.
    assert (
        _resolve_effective_output_kind(
            kind="relationship_reifier",
            bind_output_kind=TRACE_ONLY,
            options={},
        )
        is TRACE_ONLY
    )
    assert (
        _resolve_effective_output_kind(
            kind="competing_hypotheses",
            bind_output_kind=TRACE_ONLY,
            options={},
        )
        is TRACE_ONLY
    )
    # A genuine-finding kind is untouched.
    assert (
        _resolve_effective_output_kind(
            kind="inline_target",
            bind_output_kind=OutputKind.FINDING,
            options={},
        )
        is OutputKind.FINDING
    )
    assert (
        _resolve_effective_output_kind(
            kind="predictor",
            bind_output_kind=OutputKind.PREDICTION,
            options={},
        )
        is OutputKind.PREDICTION
    )
    # None bind kind degrades to FINDING (spike back-compat).
    assert (
        _resolve_effective_output_kind(
            kind="inline_target",
            bind_output_kind=None,
            options={},
        )
        is OutputKind.FINDING
    )


def test_structural_verify_exempt_registry_matches_finding_sub_handlers():
    """P0-4 drift guard — the STRUCTURAL_VERIFY_EXEMPT_ANALYSTS registry (the
    `unverified — structural` badge source) must equal EXACTLY the deterministic
    sub-handlers that emit a genuine FINDING. Deterministic runs never route
    through the faithfulness verify pass, so a FINDING sub-handler missing from
    the registry would surface verify-exempt rows WITHOUT the honest badge; a
    registry entry with no FINDING sub-handler would badge nothing real."""
    from legba.data.provenance.kinds import (
        STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
        verify_exempt_reason,
    )

    finding_sub_handlers = {
        name
        for name, kind in OUTPUT_KIND_BY_SUB_HANDLER.items()
        if kind is OutputKind.FINDING
    }
    assert finding_sub_handlers == set(STRUCTURAL_VERIFY_EXEMPT_ANALYSTS)

    # The stamp helper: structural for registry members, honest None otherwise.
    assert verify_exempt_reason("graph_mining") == "structural"
    assert verify_exempt_reason("indicator_tracker") == "structural"
    assert verify_exempt_reason("country_assessor") is None
    assert verify_exempt_reason("world_assessor") is None
    assert verify_exempt_reason(None) is None

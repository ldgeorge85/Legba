# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The origin-gate reader inventory (V3/P7 → 7g-1; SEAMS #57 resolved, #62).

Migration 0209 added ``origin_class`` to ``signals``/``facts``/``events`` and
refused the three history classes at the table BECAUSE only the two v3 reader
files had adopted the new gate. Program 7g-1 finished that sweep, and this
file is what says so in a way a future edit cannot quietly undo.

THREE THINGS ARE PINNED HERE, and they do different jobs.

1. **The literal inventory** (unchanged from P7). Every file in ``src/``
   carrying ``superseded_by IS NULL``, and every file carrying the as-of
   predicate's ``-infinity``, is listed EXACTLY. A file that gains one fails
   here until it is classified; a pinned file that loses one fails here too,
   so the list can never go stale in either direction.

2. **The classification.** Every one of those files falls into exactly one
   of three buckets, and the buckets are the whole argument that the sweep is
   finished:

   ``_RENDERS_THE_GATE``
       The file's open-row sites are on ``facts``/``events`` (or ``signals``
       on a fenced surface) and it now takes its predicate from
       :mod:`legba.data.provenance.origin` — ``origin_class_clause`` or
       ``live_gate_sql``, never spelled inline.
   ``_INDEX_PREDICATE_ONLY``
       The site is an ``ON CONFLICT`` arbiter predicate that must match a
       partial unique index's own ``WHERE`` byte for byte. It CANNOT carry
       the leg — adding one would stop matching the index — and that is
       exactly why the ``facts`` history-writer guard is still armed
       (SEAMS #62).
   ``_NON_ORIGIN_CLASSED_TABLE``
       The open-row pair is on a table with no ``origin_class`` column at all
       (``analyst_outputs``, ``situations``, ``nexuses``, ``entity_edges``,
       ``event_edges``, ``journal_entries``, ``source_ratings``,
       ``source_dossiers``, ``entity_profiles``, ``fact_decay_states``), so
       the pair IS the whole gate there. Nothing to sweep.

3. **The eight fenced surfaces, in their RENDERED SQL.** The collection
   firewall names eight surfaces a holding must be invisible to — cadence
   analysts, freshness, source health, calibration, salience, alerts,
   reactive triggers, surge detection. For each, the actual query string the
   module builds is checked for the class leg, not the source text around it:
   a constant that stopped interpolating would still look right in a grep and
   would fail here. Reactive triggers are the exception by design — that
   plane gates a ROW, not a query, so
   ``runtime.triggers.coalescer.is_live_origin`` is checked directly.
"""
from __future__ import annotations

import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parents[2] / "src"

# ---------------------------------------------------------------------------
# 1. The literal inventory
# ---------------------------------------------------------------------------

#: Files carrying ``origin_class_clause`` / ``live_gate_sql`` — the swept set.
#: Their open-row sites are on an origin-classed table (or on a fenced
#: surface over ``signals``) and the predicate comes from the leaf module.
_RENDERS_THE_GATE = frozenset({
    "src/legba/data/analysts/competing_hypotheses.py",
    "src/legba/data/analysts/deterministic_handlers/alert_trigger_scan.py",
    "src/legba/data/analysts/deterministic_handlers/entity_gc.py",
    "src/legba/data/analysts/deterministic_handlers/fact_contention_arbiter.py",
    "src/legba/data/analysts/deterministic_handlers/fact_decay.py",
    "src/legba/data/analysts/deterministic_handlers/fact_decay_scan.py",
    "src/legba/data/analysts/relationship_reifier.py",
    "src/legba/data/analysts/unit_grounding.py",
    "src/legba/data/filters/fact_extractor.py",
    "src/legba/data/provenance/origin.py",
    "src/legba/data/provenance/world_knowledge_guards.py",
    "src/legba/data/provenance/writes_supersession.py",
    "src/legba/data/registry/events_api.py",
    "src/legba/data/seed/manual_batch.py",
    "src/legba/runtime/substrate_frame_reads.py",
    "src/legba/runtime/substrate_query_port.py",
    "src/legba/runtime/substrate_temporal.py",
})

#: The ONE site that cannot carry the leg. ``writes.py``'s two ``facts`` /
#: ``nexuses`` open-row literals are ``ON CONFLICT`` arbiter predicates: they
#: must equal ``idx_facts_temporal_triple_open``'s own ``WHERE`` exactly or
#: the upsert stops matching the index. Its other two are ``journal_entries``.
_INDEX_PREDICATE_ONLY = frozenset({
    "src/legba/data/provenance/writes.py",
})

#: The open-row pair is on a table with no ``origin_class`` column, so the
#: pair is the whole gate. Nothing to sweep — and a file that starts reading
#: an origin-classed table moves to ``_RENDERS_THE_GATE`` or fails here.
_NON_ORIGIN_CLASSED_TABLE = frozenset({
    "src/legba/data/_frame_content.py",
    "src/legba/data/analysts/assessment_channel.py",
    "src/legba/data/analysts/composition_slice.py",
    "src/legba/data/analysts/composition_window.py",
    "src/legba/data/analysts/cross_analyst_correlator.py",
    "src/legba/data/analysts/deterministic_handlers/_band_crossing_scan.py",
    "src/legba/data/analysts/deterministic_handlers/_contention_flip_scan.py",
    # 7a: the contrary pass enumerates the top-layer reads it will counter-query
    # from ``analyst_outputs`` (no ``origin_class`` column; the open-row pair is
    # the whole gate). Its counter pages are LINKED to ``signals`` by
    # ``content_hash`` — an equality lookup, not an open-row read.
    "src/legba/data/analysts/deterministic_handlers/_contrary_store.py",
    "src/legba/data/analysts/deterministic_handlers/_coverage_floor_scan.py",
    "src/legba/data/analysts/deterministic_handlers/_event_candidates.py",
    "src/legba/data/analysts/deterministic_handlers/_external_audit_width.py",
    "src/legba/data/analysts/deterministic_handlers/_reference_diff.py",
    "src/legba/data/analysts/deterministic_handlers/_watchlist_scan.py",
    "src/legba/data/analysts/deterministic_handlers/collection_gap.py",
    "src/legba/data/analysts/deterministic_handlers/event_reconciler.py",
    "src/legba/data/analysts/deterministic_handlers/evidence_archiver.py",
    "src/legba/data/analysts/deterministic_handlers/finding_supersession.py",
    "src/legba/data/analysts/deterministic_handlers/graph_mining.py",
    "src/legba/data/analysts/deterministic_handlers/integrity_sweep.py",
    "src/legba/data/analysts/deterministic_handlers/proposed_edge_governance.py",
    "src/legba/data/analysts/deterministic_handlers/scorecard_banding.py",
    "src/legba/data/analysts/deterministic_handlers/scorecard_producer.py",
    "src/legba/data/analysts/deterministic_handlers/standing_auditor.py",
    "src/legba/data/analysts/deterministic_handlers/structural_balance.py",
    "src/legba/data/analysts/deterministic_handlers/thematic_proposal.py",
    "src/legba/data/analysts/deterministic_handlers/unit_correctness_scorer.py",
    "src/legba/data/analysts/meta_findings_synthesizer.py",
    "src/legba/data/analysts/reifier_selection.py",
    "src/legba/data/analysts/situation_tracker.py",
    "src/legba/data/analysts/window_ledger.py",
    "src/legba/data/provenance/entity_edge_writes.py",
    "src/legba/data/registry/absence_api.py",
    "src/legba/data/registry/api.py",
    "src/legba/data/registry/backlog_drains.py",
    "src/legba/data/registry/entities_api.py",
    "src/legba/data/registry/goldset_api.py",
    "src/legba/data/registry/graph_triggers_api.py",
    "src/legba/data/registry/graph_walk_api.py",
    "src/legba/data/registry/journal_api.py",
    "src/legba/data/registry/journal_proposals_api.py",
    "src/legba/data/registry/labels_api.py",
    "src/legba/data/registry/layers_api.py",
    "src/legba/data/registry/production_gauge_staleness.py",
    "src/legba/data/registry/since_api.py",
    "src/legba/data/registry/source_assurance_api.py",
    "src/legba/data/registry/source_quality_api.py",
    "src/legba/data/registry/v3_api.py",
    "src/legba/data/schemas/analyst.py",
    "src/legba/data/seed/source_ratings_loader.py",
    "src/legba/prompts/journal_assessor/__init__.py",
    "src/legba/runtime/dapr_workflow/gepa.py",
    "src/legba/runtime/grounding.py",
})

#: Every file carrying the open-row literal, whatever its bucket.
_OPEN_ROW_SITES = (
    _RENDERS_THE_GATE | _INDEX_PREDICATE_ONLY | _NON_ORIGIN_CLASSED_TABLE
)

#: Files carrying the ``-infinity`` literal — the marker of the as-of
#: validity predicate and of its mentions in comments/docstrings.
_AS_OF_SITES = frozenset({
    "src/legba/data/analysts/deterministic_handlers/_event_lifecycle.py",
    "src/legba/data/provenance/origin.py",
    "src/legba/data/registry/substrate_reads_api.py",
    "src/legba/runtime/substrate_query_port.py",
    "src/legba/runtime/substrate_temporal.py",
})


def _files_containing(literal: str) -> frozenset[str]:
    """Every ``src/legba/**/*.py`` path whose text carries ``literal``,
    rendered repo-relative (``src/legba/...``) to match the allowlist."""
    hits = set()
    for p in SRC.rglob("*.py"):
        if literal in p.read_text():
            hits.add(str(p.relative_to(SRC.parent)))
    return frozenset(hits)


def test_open_row_literal_inventory_is_pinned() -> None:
    """No ``superseded_by IS NULL`` site may appear or vanish unclassified."""
    assert _files_containing("superseded_by IS NULL") == _OPEN_ROW_SITES


def test_as_of_literal_inventory_is_pinned() -> None:
    """No ``-infinity`` (as-of predicate) site may appear or vanish."""
    assert _files_containing("-infinity") == _AS_OF_SITES


# ---------------------------------------------------------------------------
# 2. The classification
# ---------------------------------------------------------------------------


def test_the_three_buckets_are_disjoint() -> None:
    assert not (_RENDERS_THE_GATE & _INDEX_PREDICATE_ONLY)
    assert not (_RENDERS_THE_GATE & _NON_ORIGIN_CLASSED_TABLE)
    assert not (_INDEX_PREDICATE_ONLY & _NON_ORIGIN_CLASSED_TABLE)


def test_every_swept_file_takes_its_gate_from_the_leaf_module() -> None:
    """The swept set is DERIVED, not asserted: a file is in it exactly when
    it renders the gate from ``provenance.origin``."""
    rendering = {
        path
        for path in _OPEN_ROW_SITES
        if any(
            token in (SRC.parent / path).read_text()
            for token in ("origin_class_clause", "live_gate_sql")
        )
    }
    assert rendering == set(_RENDERS_THE_GATE)


def test_no_unswept_file_spells_an_origin_class_predicate_inline() -> None:
    """The renderings live in ONE place. A file that starts filtering on
    ``origin_class`` without importing the leaf module is a second
    vocabulary, and a second vocabulary is how the two drift apart.
    """
    offenders = sorted(
        path
        for path in _NON_ORIGIN_CLASSED_TABLE
        if "origin_class" in (SRC.parent / path).read_text()
    )
    assert offenders == []


def test_the_canonical_renderings_live_only_in_the_leaf() -> None:
    origin = (SRC / "legba/data/provenance/origin.py").read_text()
    assert "def live_gate_sql" in origin
    assert "def as_of_gate_sql" in origin
    assert "def origin_class_clause" in origin
    defined_elsewhere = sorted(
        path
        for path in _OPEN_ROW_SITES
        if path != "src/legba/data/provenance/origin.py"
        and "def origin_class_clause" in (SRC.parent / path).read_text()
    )
    assert defined_elsewhere == []


def test_the_two_v3_readers_still_hold_their_p7_gate() -> None:
    port = (SRC / "legba/runtime/substrate_query_port.py").read_text()
    temporal = (SRC / "legba/runtime/substrate_temporal.py").read_text()
    assert "live_gate_sql" in port
    assert "as_of_gate_sql" in temporal


# ---------------------------------------------------------------------------
# 3. The eight fenced surfaces, in their RENDERED SQL
# ---------------------------------------------------------------------------

#: ``surface -> (module, [attribute, ...])``. The attribute is the query
#: STRING the module hands Postgres; asserting on it rather than on the
#: source text is what makes a constant that stopped interpolating fail here.
_FENCED_SURFACE_SQL: dict[str, tuple[str, tuple[str, ...]]] = {
    "salience": (
        "legba.data.analysts.signal_salience", ("_SELECT_BATCH_SQL",),
    ),
    "alerts": (
        "legba.data.analysts.deterministic_handlers.alert_trigger_scan",
        ("_SIGNAL_BUCKETS_SQL", "_SIGNAL_CURRENT_SQL"),
    ),
    "surge_detection": (
        "legba.data.analysts.deterministic_handlers.desk_baseline",
        ("_SIGNAL_BUCKET_VECTOR_SQL",),
    ),
    "surge_detection_geo": (
        "legba.data.analysts.deterministic_handlers.geo_convergence_scan",
        ("_POINT_SIGNALS_SQL", "_COUNTRY_SIGNALS_SQL"),
    ),
    "source_health": (
        "legba.data.analysts.deterministic_handlers.source_track_record",
        ("_RECORDS_SQL",),
    ),
    "freshness": (
        "legba.data.registry.production_gauge", ("_SOURCE_SQL",),
    ),
}

#: The surfaces whose query is built inside a function rather than held as a
#: module constant. The gate is still rendered from the leaf module — the
#: module-level constant it interpolates is what is checked.
_FENCED_SURFACE_INLINE: dict[str, tuple[str, str]] = {
    "calibration": (
        "legba.data.analysts.deterministic_handlers.calibration_tracking",
        "_LIVE_SIGNALS",
    ),
    "surge_detection_anomaly": (
        "legba.data.analysts.deterministic_handlers.anomaly_detection",
        "_LIVE_SIGNALS",
    ),
    "cadence_analysts": (
        "legba.runtime.actor_substrate_slice", "_LIVE_SIGNALS",
    ),
}

_EXPECTED_CLAUSE = "origin_class IN ('live','web_retrieval','seed')"


@pytest.mark.parametrize(
    "surface", sorted(_FENCED_SURFACE_SQL), ids=sorted(_FENCED_SURFACE_SQL)
)
def test_a_fenced_surfaces_rendered_sql_carries_the_gate(surface: str) -> None:
    import importlib

    module_path, attributes = _FENCED_SURFACE_SQL[surface]
    module = importlib.import_module(module_path)
    for attribute in attributes:
        sql = getattr(module, attribute)
        assert _EXPECTED_CLAUSE in sql, f"{module_path}.{attribute}"


@pytest.mark.parametrize(
    "surface", sorted(_FENCED_SURFACE_INLINE), ids=sorted(_FENCED_SURFACE_INLINE)
)
def test_a_fenced_surface_built_inline_renders_the_gate(surface: str) -> None:
    import importlib

    module_path, attribute = _FENCED_SURFACE_INLINE[surface]
    module = importlib.import_module(module_path)
    assert getattr(module, attribute) == _EXPECTED_CLAUSE
    source = pathlib.Path(module.__file__).read_text()
    assert f"{{{attribute}}}" in source or attribute + "," in source


def test_reactive_triggers_gate_the_ROW_not_the_query() -> None:
    """The trigger plane reads rows off NATS, so there is no WHERE clause to
    add — the predicate is on the delivered row, before it is counted."""
    from legba.runtime.triggers.coalescer import is_live_origin

    assert is_live_origin({"origin_class": "live"})
    assert is_live_origin({"origin_class": "web_retrieval"})
    assert is_live_origin({"origin_class": "seed"})
    # A row written before the column existed reads as live (the column's own
    # default), but an UNKNOWN class does not — fail-closed.
    assert is_live_origin({})
    assert not is_live_origin({"origin_class": "archive"})
    assert not is_live_origin({"origin_class": "backfill_native"})
    assert not is_live_origin({"origin_class": "backfill_reconstructed"})
    assert not is_live_origin({"origin_class": "a_class_from_the_future"})

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""D4 contamination — per-country scoping of the graph-structure slice leg.

``_select_graph_structure_items`` folds the knowledge graph's "interesting"
shortlist into the assessor's input rows. Before the fix it scored in-scope
items FIRST then TOPPED UP with the highest-scored GLOBAL items — so every
country slice inherited the globally-most-central (US-centric) structures.

These pure-function tests assert:
  * a PER-COUNTRY slice (target_scoped=True + a geo) DROPS the global tail;
  * a META / no-target slice (target_scoped=False) KEEPS the global structures;
  * a scoped run with no geo degrades to global rather than emitting nothing.
"""

from __future__ import annotations

from legba.runtime.actor_substrate_slice import _select_graph_structure_items


def _payloads() -> dict[str, dict]:
    # United States (global top) outscores Indonesia; only Indonesia is in the
    # Indonesia run's geo scope.
    return {
        "graph_mining": {
            "interesting": [
                {"kind": "broker", "label": "United States", "score": 0.99,
                 "rationale": "globally most central", "entities": ["United States"]},
                {"kind": "tense_actor", "label": "Indonesia", "score": 0.40,
                 "rationale": "regional", "entities": ["Indonesia"]},
            ]
        },
        "structural_balance": {},
    }


def test_per_country_drops_global_structure_topup():
    items = _select_graph_structure_items(
        _payloads(), target_geo=["Indonesia"], limit=8, target_scoped=True,
    )
    labels = {it["label"] for it in items}
    assert labels == {"Indonesia"}, "per-country slice must not inherit the US-central item"


def test_meta_slice_keeps_global_structures():
    # A meta / no-target slice (no geo, not scoped) keeps the global top items.
    items = _select_graph_structure_items(
        _payloads(), target_geo=[], limit=8, target_scoped=False,
    )
    labels = {it["label"] for it in items}
    assert "United States" in labels and "Indonesia" in labels


def test_default_keeps_global_topup_backward_compatible():
    # The default (target_scoped omitted) preserves the historical scope-first-
    # then-global-topup behaviour: both present, in-scope ordered first.
    items = _select_graph_structure_items(
        _payloads(), target_geo=["Indonesia"], limit=8,
    )
    labels = [it["label"] for it in items]
    assert set(labels) == {"United States", "Indonesia"}
    assert labels[0] == "Indonesia"  # in-scope floated to front


def test_scoped_run_without_geo_degrades_to_global():
    # A scoped run that resolved no geo must not blank the structure leg.
    items = _select_graph_structure_items(
        _payloads(), target_geo=[], limit=8, target_scoped=True,
    )
    assert {it["label"] for it in items} == {"United States", "Indonesia"}

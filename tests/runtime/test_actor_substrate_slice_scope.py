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

QW1-A adds the duplicate-render collapse (``_collapse_structure_items``) — see
the second section below.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from legba.data.analysts.unit_grounding import (
    GROUNDING_PRIOR_READ,
    GROUNDING_SITUATIONS,
    UNIT_GROUNDING_ROW_KEY,
)
from legba.runtime.actor_substrate_slice import (
    _collapse_structure_items,
    _read_substrate_slice,
    _select_graph_structure_items,
)


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


# ---------------------------------------------------------------------------
# QW1-A clean 5 — ASSESSED-STRUCTURE duplicate-render collapse
#
# P5 gallery: the corpus_researcher's 128-row META slice ended in FIVE
# consecutive `[ASSESSED STRUCTURE]` pseudo-signals whose snippets were
# byte-identical and whose titles differed only in the third vertex —
# Belgium-Egypt-{IMO, IHO, OIF, UNESCO, IBRD}. Five numbered rows, five
# citation slots, one structural fact.
# ---------------------------------------------------------------------------


_TRIAD_RATIONALE = (
    "unbalanced signed triad (sign product negative — 1 hostile edge(s)); "
    "Heider-unstable, predicts realignment"
)


def _belgium_egypt_triads() -> list[dict]:
    """The live P5 tail, verbatim in shape: same kind, same rationale, one
    varying vertex."""
    return [
        {"kind": "sign_imbalanced_triad", "score": 0.5,
         "rationale": _TRIAD_RATIONALE,
         "label": f"Belgium - Egypt - {third}",
         "entities": ["Belgium", "Egypt", third]}
        for third in (
            "International Maritime Organization",
            "International Hydrographic Organization",
            "Organisation internationale de la Francophonie",
            "UNESCO",
            "International Bank for Reconstruction and Development",
        )
    ]


def test_duplicate_triads_collapse_to_one_row_keeping_every_label():
    groups = _collapse_structure_items(_belgium_egypt_triads(), limit=8)
    assert len(groups) == 1
    item, siblings = groups[0]
    assert item["label"] == "Belgium - Egypt - International Maritime Organization"
    # The COUNT and every collapsed vertex survive — nothing is lost but the
    # repetition of one identical rationale.
    assert len(siblings) == 4
    assert "Belgium - Egypt - UNESCO" in siblings
    assert "Belgium - Egypt - International Hydrographic Organization" in siblings


def test_collapse_frees_the_budget_for_DISTINCT_structures():
    """The payoff: a slice that spent its whole structure cap on one repeated
    fact now carries other, genuinely different structures too."""
    candidates = _belgium_egypt_triads() + [
        {"kind": "broker", "label": "Turkey", "score": 0.9,
         "rationale": "brokers between two hostile camps", "entities": ["Turkey"]},
        {"kind": "new_hostile_edge", "label": "Iran - United States", "score": 0.8,
         "rationale": "relationship turned hostile this window",
         "entities": ["Iran", "United States"]},
    ]
    labels = [it["label"] for it, _sib in _collapse_structure_items(candidates, limit=3)]
    assert labels == [
        "Belgium - Egypt - International Maritime Organization",
        "Turkey",
        "Iran - United States",
    ]


def test_distinct_structures_are_untouched_and_order_preserved():
    """Keep-test: with no duplicates the collapse is a no-op — same items, same
    order, empty sibling lists — i.e. byte-for-byte the pre-QW1-A rows."""
    items = _select_graph_structure_items(
        _payloads(), target_geo=[], limit=8, target_scoped=False,
    )
    groups = _collapse_structure_items(items, limit=8)
    assert [it for it, _ in groups] == items
    assert all(siblings == [] for _, siblings in groups)


def test_same_rationale_different_KIND_is_not_a_duplicate():
    """Two structure CLASSES that happen to share rationale text are different
    facts — only same kind AND same rationale collapses."""
    candidates = [
        {"kind": "broker", "label": "Turkey", "score": 0.9, "rationale": "same text"},
        {"kind": "tense_actor", "label": "Iran", "score": 0.8, "rationale": "same text"},
    ]
    assert len(_collapse_structure_items(candidates, limit=8)) == 2


def test_collapse_respects_the_limit():
    candidates = [
        {"kind": "broker", "label": f"Actor {i}", "score": 1.0 - i / 100,
         "rationale": f"rationale {i}"}
        for i in range(20)
    ]
    assert len(_collapse_structure_items(candidates, limit=8)) == 8
# QW1-B — the DESK GROUNDING leg's three gates
# ---------------------------------------------------------------------------
#
# The leg appends the unit's own memory (prior read / open situations / desk
# baseline / standing questions) as MARKED rows. It fires ONLY for an
# ``inline_target`` analyst, ONLY with a real desk target, and ONLY when the
# signal slice is non-empty — the third gate is what keeps the actor's
# ``no_inputs`` NOOP intact, so a unit can never synthesize off memory alone.


class _SliceConn:
    """Fake connection covering every query family ``_read_substrate_slice``
    fires: the target-descriptor lookup, the signals read, graph_metrics, and
    the four grounding reads."""

    def __init__(self, *, signals: list[dict[str, Any]] | None = None) -> None:
        self._signals = signals or []
        self.queries: list[str] = []

    async def fetchrow(self, _query: str, *_params: Any) -> dict[str, Any] | None:
        return {"body": json.dumps({"sources": [], "scope": {"geo": ["IR"]}})}

    async def fetch(self, query: str, *_params: Any) -> list[dict[str, Any]]:
        self.queries.append(query)
        if "FROM signals" in query:
            return list(self._signals)
        if "FROM graph_metrics" in query:
            return []
        if "FROM situations" in query:
            return [{
                "id": uuid4(), "name": "Iran - open frame", "status": "active",
                "intensity_score": 50.0, "event_count": 3,
                "last_event_at": None, "opened_at": None, "age_days": 4.0,
                "target_id": "country_watch_ir",
            }]
        if "AS age_hours" in query:
            return [{
                "id": uuid4(), "title": "prior", "body": "b",
                "analyst_id": "escalation",
                "produced_at": "2026-07-30T07:00:00+00:00", "age_hours": 25.0,
            }]
        return []

    def fired(self, needle: str) -> bool:
        return any(needle in q for q in self.queries)


def _descriptor(kind: Any) -> SimpleNamespace:
    return SimpleNamespace(
        identity=SimpleNamespace(id="escalation", kind=kind),
        subscription=SimpleNamespace(
            substrate={}, targets=SimpleNamespace(time_window="72h"),
        ),
    )


def _signal_row() -> dict[str, Any]:
    return {
        "id": uuid4(), "source_id": "s1", "source_version": None,
        "canonical_url": "https://example.test/1", "payload": {"title": "t"},
        "language": "en", "geo": ["IR"], "tags": [], "fetched_at": None,
        "derived_from": None, "entity_classes": [], "source_credibility": None,
        "modality": None, "salience": None,
    }


@pytest.mark.asyncio
async def test_unit_slice_carries_the_desk_grounding_rows():
    conn = _SliceConn(signals=[_signal_row()])
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor("inline_target"),
        target_filter="country_watch_ir",
    )
    kinds = {r.get(UNIT_GROUNDING_ROW_KEY) for r in rows}
    assert GROUNDING_PRIOR_READ in kinds
    assert GROUNDING_SITUATIONS in kinds


@pytest.mark.asyncio
async def test_the_kind_gate_survives_an_analystkind_enum_member():
    """``identity.kind`` is declared ``str`` but a caller may hand in the
    ``AnalystKind`` member — and ``str(AnalystKind.INLINE_TARGET)`` is
    ``'AnalystKind.INLINE_TARGET'``, not the value. A naive ``str()`` compare
    would make the whole leg silently dead in production."""
    from legba.data.schemas.analyst import AnalystKind

    conn = _SliceConn(signals=[_signal_row()])
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor(AnalystKind.INLINE_TARGET),
        target_filter="country_watch_ir",
    )
    assert any(r.get(UNIT_GROUNDING_ROW_KEY) for r in rows)


@pytest.mark.asyncio
async def test_a_non_unit_kind_never_sees_a_grounding_row():
    """Only ``inline_target``'s run_method knows how to partition these; any
    other kind would read a marked row as evidence."""
    conn = _SliceConn(signals=[_signal_row()])
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor("predictor"),
        target_filter="country_watch_ir",
    )
    assert all(UNIT_GROUNDING_ROW_KEY not in r for r in rows)
    assert not conn.fired("FROM situations")


@pytest.mark.asyncio
async def test_an_empty_signal_slice_stays_empty():
    """The actor's ``no_inputs`` NOOP must still fire — grounding rows must
    never resurrect a dead slice into a memory-only synthesis."""
    conn = _SliceConn(signals=[])
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor("inline_target"),
        target_filter="country_watch_ir",
    )
    assert rows == []
    assert not conn.fired("FROM situations")


@pytest.mark.asyncio
async def test_a_target_less_unit_run_gathers_no_grounding():
    conn = _SliceConn(signals=[_signal_row()])
    rows = await _read_substrate_slice(
        conn, descriptor=_descriptor("inline_target"), target_filter=None,
    )
    assert all(UNIT_GROUNDING_ROW_KEY not in r for r in rows)
    assert not conn.fired("FROM situations")

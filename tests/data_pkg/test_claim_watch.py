# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""KW-3 — the ``claim_watch`` flag-only new-evidence-vs-open-question matcher.

Pure tests (no DB): cosine, the fused-weight model (per-plane floors, the
conservative threshold arithmetic), question age dampening. Ephemeral-DB
tests (the ``migrated_pg`` fixture): the seed-silently first run; matching
writes edges above threshold and NOTHING below; the circularity guard; the
NER-name entity fallback through the existing election machinery; review
flags ONLY for questions tracing FORWARD over ``output_consumption`` to live
(non-superseded) products; staleness_debt correctness incl. the
closed-by-supersession and superseded-consumer exclusions; idempotent
re-runs (unique-constraint dedup); cursor watermark advance on the ridden
0091 table; per-run caps with honest deferral; and the vector plane over an
injected store + embedder (the signal_embedder deps keys).
"""
from __future__ import annotations

import json
import pathlib
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import yaml

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
    TRACE_ONLY,
)
from legba.data.analysts.deterministic_handlers import bearing_gate as bg
from legba.data.analysts.deterministic_handlers import claim_watch as cw
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Registry wiring
# ---------------------------------------------------------------------------


def test_registered_as_trace_only_sub_handler():
    """The receipt is TRACE_ONLY (real product = side-written bearing_edges +
    review_flags), so the STRUCTURAL_VERIFY_EXEMPT drift guard's FINDING-set
    equality holds without a registry entry — the alert_trigger_scan
    precedent."""
    assert SUB_HANDLERS["claim_watch"] is cw.handle
    assert OUTPUT_KIND_BY_SUB_HANDLER["claim_watch"] is TRACE_ONLY


def test_trace_only_keeps_the_structural_exempt_registry_untouched():
    from legba.data.provenance.kinds import STRUCTURAL_VERIFY_EXEMPT_ANALYSTS

    assert "claim_watch" not in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS


async def test_refuses_loud_without_pool():
    with pytest.raises(RuntimeError, match="pg_pool"):
        await cw.handle([], {"sub_handler": "claim_watch"}, None)


_DESCRIPTOR = (
    pathlib.Path(__file__).resolve().parents[2]
    / "descriptors"
    / "analyst_claim_watch.yaml"
)


def test_claim_watch_descriptor_exists_and_is_valid():
    assert _DESCRIPTOR.is_file(), f"missing {_DESCRIPTOR}"
    body = yaml.safe_load(_DESCRIPTOR.read_text())

    ident = body["identity"]
    assert ident["id"] == "claim_watch"
    assert ident["kind"] == "deterministic"
    # DRAFT in-tree — registration + the operator's activate flip are deploy
    # steps (the alert_trigger_scan precedent).
    assert ident["state"] == "draft"

    method = body["method"]
    assert method["kind"] == "deterministic"
    assert method["impl"] == "legba.data.analysts.deterministic:run_method"
    assert method["sub_handler"] == "claim_watch"
    assert method["sub_handler"] in SUB_HANDLERS
    assert method["sub_handler"] in OUTPUT_KIND_BY_SUB_HANDLER

    # META analyst — no targets selector → single global cadence run.
    sub = body["subscription"]
    assert "targets" not in sub

    cadence = body["cadence"]
    assert isinstance(cadence["fallback_schedule"], str)
    assert "/30" in cadence["fallback_schedule"]  # the ~30-minute tick
    assert isinstance(cadence["cooldown_seconds"], int)
    assert cadence["cooldown_seconds"] < 1800  # never swallows its own tick

    # No output bindings — the summary is TRACE_ONLY; the products are the
    # side-written 0107 rows.
    assert body["outputs"] == []


# ---------------------------------------------------------------------------
# Pure — cosine / fusion / age dampening
# ---------------------------------------------------------------------------


def test_cosine_similarity():
    assert cw.cosine_similarity([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)
    assert cw.cosine_similarity([1.0, 0.0], [0.0, 1.0]) == pytest.approx(0.0)
    assert cw.cosine_similarity([], [1.0]) == 0.0
    assert cw.cosine_similarity([0.0, 0.0], [1.0, 0.0]) == 0.0


def _fuse(vector_sim=None, shared_entities=0, geo=False, age=1.0):
    return cw.fuse_weight(
        vector_sim=vector_sim,
        shared_entities=shared_entities,
        geo_overlap=geo,
        age_factor=age,
    )


def test_fuse_weight_conservative_arithmetic():
    thr = cw.DEFAULT_MATCH_THRESHOLD
    # Geo alone can NEVER clear the threshold.
    w, planes = _fuse(geo=True)
    assert planes == ["geo"] and w < thr
    # ONE shared entity alone can NEVER clear the threshold.
    w, planes = _fuse(shared_entities=1)
    assert planes == ["entity"] and w < thr
    # A sub-floor cosine contributes NOTHING (not even the plane).
    w, planes = _fuse(vector_sim=cw.VECTOR_SIM_FLOOR - 0.01)
    assert planes == [] and w == 0.0
    # A very strong cosine alone can just clear it on a fresh question — the
    # documented "vector alone needs >=0.90 cosine" contract, unchanged.
    w, planes = _fuse(vector_sim=0.90)
    assert planes == ["vector"] and w >= thr
    assert _fuse(vector_sim=0.89)[0] < thr


def test_entity_and_geo_alone_can_never_clear_the_threshold():
    """THE anti-desk-co-membership property (the KW-3 first-live defect).

    A signal that shares ONE canonicalized entity with a question and sits
    in the same desk geo is, on a country desk, close to trivially true
    ("mentions Iran + geo-Iran" vs "a question about Iran"). It must not be
    a match at ANY question age — including a brand-new question, where the
    age dampener is 1.0 and therefore contributes nothing.
    """
    thr = cw.DEFAULT_MATCH_THRESHOLD
    w, planes = _fuse(shared_entities=1, geo=True, age=1.0)
    assert sorted(planes) == ["entity", "geo"]
    assert w == pytest.approx(0.30)
    assert w < thr
    # Not a knife-edge: there is real margin, so float noise or a slightly
    # fresher question can never flip it.
    assert thr - w > 0.10
    # And no age factor can raise it (the dampener only ever reduces).
    for age in (0.6, 0.75, 0.9, 1.0):
        assert _fuse(shared_entities=1, geo=True, age=age)[0] < thr


def test_strong_entity_evidence_still_matches():
    """The escape hatch: SEVERAL distinct shared entities is real evidence."""
    thr = cw.DEFAULT_MATCH_THRESHOLD
    # Two shared entities + geo clears it on a fresh question.
    w, planes = _fuse(shared_entities=2, geo=True)
    assert sorted(planes) == ["entity", "geo"] and w >= thr
    # Two shared entities WITHOUT geo do not — geo is a tie-breaker.
    assert _fuse(shared_entities=2)[0] < thr
    # Three carry it alone.
    assert _fuse(shared_entities=3)[0] >= thr
    # Counting is CAPPED — an entity-dense signal cannot buy unbounded weight.
    assert _fuse(shared_entities=50)[0] == pytest.approx(
        _fuse(shared_entities=cw.MAX_SHARED_ENTITIES_COUNTED)[0]
    )
    # Monotone in the number of shared entities.
    weights = [_fuse(shared_entities=n)[0] for n in range(0, 5)]
    assert weights == sorted(weights)


def test_vector_supported_matches_still_pass():
    """Semantic support is the OTHER way to clear the bar — corroborating
    the single-entity-plus-geo case that entity evidence alone could not
    carry. At the measured 0.45 floor a floor-grade echo is WEAKER than the
    old 0.60 floor was, so it now needs BOTH the entity and geo alongside."""
    thr = cw.DEFAULT_MATCH_THRESHOLD
    assert _fuse(vector_sim=cw.VECTOR_SIM_FLOOR, shared_entities=1, geo=True)[0] >= thr
    # A floor-grade vector plus one entity, without geo, is NOT enough...
    assert _fuse(vector_sim=cw.VECTOR_SIM_FLOOR, shared_entities=1)[0] < thr
    # ...and neither is a floor-grade vector plus mere desk geo.
    assert _fuse(vector_sim=cw.VECTOR_SIM_FLOOR, geo=True)[0] < thr
    # A STRONG echo (the old floor grade) with one entity still passes.
    assert _fuse(vector_sim=0.60, shared_entities=1)[0] >= thr


def test_entity_component_grading():
    assert cw.entity_component(0) == 0.0
    assert cw.entity_component(-3) == 0.0
    assert cw.entity_component(1) == pytest.approx(cw.W_ENTITY_FIRST)
    assert cw.entity_component(2) == pytest.approx(
        cw.W_ENTITY_FIRST + cw.W_ENTITY_ADDITIONAL
    )
    assert cw.entity_component(99) == cw.entity_component(
        cw.MAX_SHARED_ENTITIES_COUNTED
    )


def test_old_questions_match_colder():
    now = datetime.now(timezone.utc)
    # Fresh question: full weight.
    assert cw.question_age_factor(now, now) == pytest.approx(1.0)
    # Past the lifetime: dampened to the floor, never zero.
    old = now - timedelta(days=cw.QUESTION_LIFETIME_DAYS + 60)
    assert cw.question_age_factor(old, now) == pytest.approx(cw.AGE_FACTOR_FLOOR)
    # At the floor even the strongest ENTITY-ONLY evidence (3 shared
    # entities + geo, raw 0.66) no longer clears the threshold — a
    # long-standing question has to be re-flagged on semantic support.
    assert _fuse(shared_entities=3, geo=True, age=cw.AGE_FACTOR_FLOOR)[0] < (
        cw.DEFAULT_MATCH_THRESHOLD
    )
    # Vector support still lands on an old-but-open question.
    w, _ = _fuse(vector_sim=1.0, shared_entities=1, geo=True,
                 age=cw.AGE_FACTOR_FLOOR)
    assert w >= cw.DEFAULT_MATCH_THRESHOLD
    # Defensive None reads as fully aged (the floor), never fresh.
    assert cw.question_age_factor(None, now) == pytest.approx(cw.AGE_FACTOR_FLOOR)


# ---------------------------------------------------------------------------
# Pure — entity SPECIFICITY (the desk-relative IDF weight)
#
# The 2.0.0 live run wrote 185 edges of which 150 sat at exactly 0.560: the
# 3-entity cap, entity plane alone. Grading the COUNT was not enough, because
# on a country desk the three entities everything shares are the desk's own
# headline names. These pin the weight that fixes it and, just as importantly,
# the boundaries where it must stay INERT.
# ---------------------------------------------------------------------------


def test_entity_specificity_curve_boundaries():
    # A rare entity is worth its full weight, and so is anything at or below
    # the knee — the rule must not nibble at ordinary overlap.
    assert cw.entity_specificity(0.0) == pytest.approx(1.0)
    assert cw.entity_specificity(cw.DF_UBIQUITY_KNEE) == pytest.approx(1.0)
    # Past the knee it ramps DOWN, monotonically, to the floor at df = 1.0.
    mid = cw.entity_specificity((cw.DF_UBIQUITY_KNEE + 1.0) / 2.0)
    assert cw.ENTITY_SPECIFICITY_FLOOR < mid < 1.0
    assert cw.entity_specificity(1.0) == pytest.approx(
        cw.ENTITY_SPECIFICITY_FLOOR
    )
    # A FLOOR, not a zero: the desk's headline name is weak evidence, not
    # anti-evidence.
    assert cw.entity_specificity(1.0) > 0.0
    # Out-of-range inputs clamp rather than extrapolate.
    assert cw.entity_specificity(-1.0) == pytest.approx(1.0)
    assert cw.entity_specificity(5.0) == pytest.approx(
        cw.ENTITY_SPECIFICITY_FLOOR
    )


def test_entity_component_is_continuous_and_matches_integer_counts():
    """Specificity makes the shared-entity 'count' fractional, so the graded
    component has to stay continuous and MONOTONE — otherwise a weight could
    go UP because an entity got less specific."""
    # Whole numbers are exactly the integer-count values (nothing re-tuned).
    assert cw.entity_component(1.0) == pytest.approx(cw.W_ENTITY_FIRST)
    assert cw.entity_component(2.0) == pytest.approx(
        cw.W_ENTITY_FIRST + cw.W_ENTITY_ADDITIONAL
    )
    assert cw.entity_component(3.0) == pytest.approx(0.56)
    # The sub-one interval ramps linearly from zero (continuity at both ends).
    assert cw.entity_component(0.5) == pytest.approx(cw.W_ENTITY_FIRST * 0.5)
    assert cw.entity_component(0.0) == 0.0
    # Monotone across the whole range.
    steps = [cw.entity_component(n / 20.0) for n in range(0, 81)]
    assert all(b >= a for a, b in zip(steps, steps[1:]))
    # Still capped.
    assert cw.entity_component(99.5) == cw.entity_component(
        float(cw.MAX_SHARED_ENTITIES_COUNTED)
    )


def test_desk_furniture_triple_cannot_reach_the_threshold():
    """The measured pathology: three shared entities that nearly every
    question on the desk already carries. Under 2.0.0 that fused to 0.560 and
    wrote an edge; weighted by specificity it must not."""
    n_eff = sum(cw.entity_specificity(df) for df in (1.0, 0.90, 0.85))
    w, planes = _fuse(shared_entities=n_eff, geo=True)
    assert planes == ["entity", "geo"]
    assert w < cw.DEFAULT_MATCH_THRESHOLD
    # ...while three DISTINCTIVE shared entities are untouched by the rule.
    assert _fuse(shared_entities=3.0)[0] == pytest.approx(0.56)


def test_specificity_can_only_lower_a_weight():
    """The safety property that keeps every conservatism claim above intact:
    nothing that failed to match can start matching because of this rule."""
    for raw in (1, 2, 3):
        for df in (0.0, 0.6, 0.8, 1.0):
            weighted = raw * cw.entity_specificity(df)
            assert cw.entity_component(weighted) <= cw.entity_component(
                float(raw)
            ) + 1e-12


def test_build_entity_specificity_is_inert_below_the_question_floor():
    """Document frequency computed from one document says 'everything is
    ubiquitous'. A desk too small to estimate it must score on raw counts —
    silently muting a whole desk would be worse than not weighting it."""
    n = cw.MIN_DESK_QUESTIONS_FOR_SPECIFICITY
    desk_of = {f"q{i}": "small_desk" for i in range(n - 1)}
    keys = {qid: {"iran"} for qid in desk_of}
    assert cw.build_entity_specificity(keys, desk_of) == {}

    # One more question and the desk becomes estimable.
    desk_of[f"q{n - 1}"] = "small_desk"
    keys[f"q{n - 1}"] = {"iran"}
    built = cw.build_entity_specificity(keys, desk_of)
    assert built[("small_desk", "iran")] == pytest.approx(
        cw.ENTITY_SPECIFICITY_FLOOR
    )


def test_build_entity_specificity_is_per_desk_and_only_downweights():
    """Ubiquity is a property of a DESK, not of the corpus: the same entity
    can be furniture on one desk and evidence on another."""
    desk_of = {}
    keys = {}
    for i in range(6):
        desk_of[f"a{i}"] = "desk_a"
        keys[f"a{i}"] = {"shared", "rare"} if i == 0 else {"shared"}
        desk_of[f"b{i}"] = "desk_b"
        keys[f"b{i}"] = {"rare"} if i < 2 else set()
    built = cw.build_entity_specificity(keys, desk_of)
    # 'shared' is furniture on desk_a (df 1.0) and absent from desk_b.
    assert built[("desk_a", "shared")] == pytest.approx(
        cw.ENTITY_SPECIFICITY_FLOOR
    )
    # 'rare' is distinctive on BOTH desks (df 1/6 and 2/6) — so it is not in
    # the map at all: a missing key reads 1.0.
    assert ("desk_a", "rare") not in built
    assert ("desk_b", "rare") not in built
    # Questions carrying NO entities still count in the denominator (they are
    # documents the entity did not appear in).
    assert built == {("desk_a", "shared"): built[("desk_a", "shared")]}


# ---------------------------------------------------------------------------
# Pure — v3.2.0 lever L2: GLOBAL (signal-side) entity ubiquity
# ---------------------------------------------------------------------------


def test_global_entity_specificity_curve_boundaries():
    """The stream-side curve: fully specific up to the knee, ramping to the
    shared floor at saturation and holding there. Knee/saturation are SET FROM
    MEASUREMENT (live DB, 10k newest signals ⇒ 5,673 attributed: United States
    0.144, Russia 0.099, Iran 0.094 vs France 0.040, China 0.030 and the mass
    below 0.01)."""
    g = cw.global_entity_specificity
    assert g(0.0) == 1.0
    assert g(cw.GLOBAL_DF_UBIQUITY_KNEE) == 1.0
    assert g(cw.GLOBAL_DF_SATURATION) == pytest.approx(
        cw.ENTITY_SPECIFICITY_FLOOR
    )
    # Past saturation it HOLDS at the floor — a hub is weak evidence, never
    # anti-evidence, so nothing below the floor is ever reachable.
    assert g(0.5) == pytest.approx(cw.ENTITY_SPECIFICITY_FLOOR)
    assert g(1.0) == pytest.approx(cw.ENTITY_SPECIFICITY_FLOOR)
    # Out-of-range inputs clamp rather than crash.
    assert g(-1.0) == 1.0
    assert g(2.0) == pytest.approx(cw.ENTITY_SPECIFICITY_FLOOR)


def test_global_entity_specificity_is_monotone_and_continuous():
    g = cw.global_entity_specificity
    prev = 1.0
    for i in range(0, 201):
        df = i / 1000.0
        cur = g(df)
        assert cur <= prev + 1e-12, f"not monotone at df={df}"
        assert cw.ENTITY_SPECIFICITY_FLOOR - 1e-12 <= cur <= 1.0
        prev = cur


def test_the_measured_hubs_are_damped_and_specifics_are_not():
    """The K-4 failure class, as arithmetic, against the MEASURED live stream
    df (10k newest signals ⇒ 5,576 attributed). Only the United States sits
    past saturation; Russia and Iran sit just under it and land within 0.05 of
    the floor. The point of the assertion is the SEPARATION from ordinary desk
    subjects, not that every named hub reaches the floor exactly."""
    g = cw.global_entity_specificity
    floor = cw.ENTITY_SPECIFICITY_FLOOR
    assert g(0.1460) == pytest.approx(floor)  # United States — past saturation
    assert g(0.0954) < floor + 0.07  # Russia  -> 0.293
    assert g(0.0934) < floor + 0.07  # Iran    -> 0.312
    assert g(0.0649) < 0.65  # Ukraine
    assert g(0.0542) < 0.75  # Trump
    # Ordinary subjects keep most of their worth; the long tail keeps ALL of
    # it — the discount must not become a blanket deflation of every entity.
    assert g(0.0289) > 0.85  # China
    assert g(0.0260) > 0.90  # Japan
    assert g(0.0025) == 1.0  # Mali
    assert g(0.0011) == 1.0  # Ebola


def test_three_stream_hubs_plus_geo_cannot_reach_the_threshold():
    """The property L2 exists for, stated end-to-end: the K-4 entity-only
    class (0/54) was hub bridging, and three names all ubiquitous in the
    stream must no longer add up to a match even with desk geo on top.

    (The one measured K-4 row that still clears by a hair —
    ``United States`` + ``Trump`` + ``Donald Trump`` — does so because
    ``Trump`` and ``Donald Trump`` are two UNMERGED entity_profiles rows, so
    one person is counted twice at two different document frequencies. That is
    an entity-resolution defect, named here deliberately rather than papered
    over by tuning the knee around it: folded, the pair counts once at the
    combined df and the row does not clear.)"""
    floor = cw.ENTITY_SPECIFICITY_FLOOR
    hub_worth = cw.combined_specificity(1.0, cw.global_entity_specificity(0.15))
    assert hub_worth == pytest.approx(floor)
    weight, planes = cw.fuse_weight(
        vector_sim=None,
        shared_entities=3 * hub_worth,
        geo_overlap=True,
        age_factor=1.0,
    )
    assert weight < cw.DEFAULT_MATCH_THRESHOLD
    assert planes == ["entity", "geo"]
    # Sanity: the SAME three entities at full specificity DO clear — so the
    # exclusion is the discount's doing, not a broken fusion model.
    raw, _ = cw.fuse_weight(
        vector_sim=None, shared_entities=3.0, geo_overlap=True, age_factor=1.0
    )
    assert raw >= cw.DEFAULT_MATCH_THRESHOLD


def test_combined_specificity_composes_both_discounts_under_one_floor():
    """Two questions, two discounts, ONE floor: a name may be desk furniture,
    a stream hub, or both, and is never worth less than
    ENTITY_SPECIFICITY_FLOOR."""
    c = cw.combined_specificity
    floor = cw.ENTITY_SPECIFICITY_FLOOR
    assert c(1.0, 1.0) == 1.0
    assert c(1.0, floor) == pytest.approx(floor)
    assert c(floor, 1.0) == pytest.approx(floor)
    # BOTH discounts would multiply below the floor — clamped, not zeroed.
    assert c(floor, floor) == pytest.approx(floor)
    # A partial pair composes as a product.
    assert c(0.8, 0.5) == pytest.approx(0.4)


def test_combined_specificity_can_only_lower_a_weight():
    """The conservatism invariant: composing the global discount onto the
    desk-relative one never RAISES an entity's worth, so nothing that failed
    to match before can start matching because of L2."""
    floor = cw.ENTITY_SPECIFICITY_FLOOR
    for desk in (floor, 0.4, 0.7, 1.0):
        for glob in (floor, 0.4, 0.7, 1.0):
            got = cw.combined_specificity(desk, glob)
            assert got <= desk + 1e-12
            assert got <= 1.0
            assert got >= floor - 1e-12
    # Monotone in each argument.
    assert cw.combined_specificity(0.5, 0.9) >= cw.combined_specificity(0.5, 0.6)
    assert cw.combined_specificity(0.9, 0.5) >= cw.combined_specificity(0.6, 0.5)


# ---------------------------------------------------------------------------
# Pure — v3.2.0 lever L1: the harvest class behind a meta question
# ---------------------------------------------------------------------------


def test_harvest_class_reads_the_durable_marker():
    marker = [
        {
            "marker": cw.HARVEST_MARKER_KEY,
            "origin": "harvest",
            "harvest_class": "collection_gap",
            "source_id": "x",
        }
    ]
    assert cw.harvest_class(marker) == "collection_gap"
    # asyncpg may hand a jsonb column back as a STRING — parse, don't crash.
    assert cw.harvest_class(json.dumps(marker)) == "collection_gap"
    # The agency faucet / unit-payload origins carry the SAME marker key with
    # NO harvest_class: not harvested, therefore never meta.
    assert (
        cw.harvest_class(
            [
                {
                    "marker": cw.HARVEST_MARKER_KEY,
                    "origin": "unit_payload",
                    "source_id": "y",
                    "question_sha256": "deadbeef",
                }
            ]
        )
        is None
    )
    # Junk, empties and wrong shapes read None rather than raising.
    assert cw.harvest_class([]) is None
    assert cw.harvest_class(None) is None
    assert cw.harvest_class("not json") is None
    assert cw.harvest_class([{"marker": "something_else"}]) is None
    assert cw.harvest_class([{"marker": cw.HARVEST_MARKER_KEY}]) is None
    assert cw.harvest_class(["a string element"]) is None


def test_is_meta_question_covers_the_measured_classes_only():
    """Measured per class on the K-4 gold set: collection_gap 1/40,
    below_floor 0/10, freshness_advisory 0/5, scorecard_disagreement 1/3 —
    all excluded. ``fact_contention`` is a question about the WORLD ("which
    value of 'border with' for 'madrid' is correct?") and stays IN."""

    def marked(hc: str) -> list[Any]:
        return [
            {
                "marker": cw.HARVEST_MARKER_KEY,
                "origin": "harvest",
                "harvest_class": hc,
                "source_id": "s",
            }
        ]

    for hc in (
        "collection_gap",
        "below_floor",
        "freshness_advisory",
        "scorecard_disagreement",
    ):
        assert cw.is_meta_question(marked(hc)) is True, hc
    assert cw.is_meta_question(marked("fact_contention")) is False
    assert cw.is_meta_question([]) is False
    # An explicit empty class set disables the lever entirely.
    assert cw.is_meta_question(marked("collection_gap"), frozenset()) is False


def test_meta_classes_do_not_drift_from_the_harvest_script():
    """DRIFT GUARD. The marker key and the class vocabulary are OWNED by
    ``scripts/harvest_open_questions.py``; this module mirrors them (scripts/
    is not importable from the runtime). If the harvest renames a class or the
    marker, this fails instead of the exclusion silently going inert."""
    import importlib.util
    import sys

    script = (
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts"
        / "harvest_open_questions.py"
    )
    assert script.is_file(), f"missing {script}"
    spec = importlib.util.spec_from_file_location("_kw3_harvest_probe", script)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["_kw3_harvest_probe"] = mod
    spec.loader.exec_module(mod)

    assert cw.HARVEST_MARKER_KEY == mod.MARKER_KEY
    # Every excluded class must be a class the harvest actually produces.
    assert cw.META_QUESTION_CLASSES <= set(mod.HARVEST_CLASSES)
    # ...and fact_contention must remain a class the harvest produces AND the
    # matcher scores (the deliberate non-exclusion).
    assert "fact_contention" in mod.HARVEST_CLASSES
    assert "fact_contention" not in cw.META_QUESTION_CLASSES


# ---------------------------------------------------------------------------
# Pure — v3.2.0 lever L3: same-article duplicate collapse
# ---------------------------------------------------------------------------


def _url_row(url: str | None) -> dict[str, Any]:
    return {"canonical_url": url}


def test_dedupe_by_canonical_url_keeps_the_newest_occurrence():
    """Rows arrive oldest-first, so the keeper is the LAST occurrence."""
    a1, a2, b1 = _url_row("a"), _url_row("a"), _url_row("b")
    kept, dropped = cw.dedupe_by_canonical_url([a1, b1, a2])
    assert kept == [b1, a2]
    assert dropped == 1
    # The keeper is identity-checked, not just url-equal.
    assert kept[1] is a2


def test_dedupe_by_canonical_url_never_drops_the_last_row():
    """Load-bearing for the cursor: the batch's final row must survive, or the
    watermark would stop short of it and the run would re-fetch forever."""
    rows = [_url_row("a") for _ in range(5)]
    kept, dropped = cw.dedupe_by_canonical_url(rows)
    assert kept == [rows[-1]]
    assert kept[0] is rows[-1]
    assert dropped == 4


def test_dedupe_by_canonical_url_leaves_url_less_rows_alone():
    """An absent url is not a shared identity — two rows with no url are two
    documents, not a duplicate pair."""
    rows = [_url_row(None), _url_row(""), _url_row("   "), _url_row("a")]
    kept, dropped = cw.dedupe_by_canonical_url(rows)
    assert kept == rows
    assert dropped == 0
    assert cw.dedupe_by_canonical_url([]) == ([], 0)


# ---------------------------------------------------------------------------
# Ephemeral-DB rig
# ---------------------------------------------------------------------------

_ANALYST = "test_kw3"
_SRC = "test_kw3_src"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    """Fresh matcher state: the ridden watermark class, both 0107 sidecars,
    the 0106 consumption index, and this file's substrate rows. TRUNCATE on
    review_flags is deliberate — the 0107 forbid-delete trigger is row-level
    (BEFORE DELETE) and does not fire on TRUNCATE, so the test rig can reset
    without weakening the production no-DELETE posture."""
    async with pg_pool.acquire() as conn:
        await conn.execute("TRUNCATE alert_trigger_watermarks")
        await conn.execute("TRUNCATE bearing_edges")
        await conn.execute("TRUNCATE review_flags")
        await conn.execute("TRUNCATE output_consumption")
        await conn.execute(
            "DELETE FROM hypotheses WHERE analyst_id = $1", _ANALYST
        )
        # The scanner is GLOBAL over open questions by design, so any other
        # suite's leftover open_question rows (e.g. the faucet tests') change
        # this file's zero-question and match-count assertions — neutralize
        # them all, not just this file's analyst.
        await conn.execute(
            "DELETE FROM hypotheses WHERE status = 'open_question'"
        )
        await conn.execute("DELETE FROM facts WHERE analyst_id = $1", _ANALYST)
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM signal_entity_links WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute(
            "DELETE FROM entity_profiles WHERE analyst_id = $1", _ANALYST
        )
        await conn.execute("DELETE FROM signals WHERE source_id = $1", _SRC)
        await conn.execute(
            "DELETE FROM target_descriptors WHERE owner = $1", _ANALYST
        )
    cw._QUESTION_EMBED_CACHE.clear()
    yield


class _Deps:
    def __init__(self, pool: Any, extras: dict[str, Any] | None = None) -> None:
        self.pg_pool = pool
        self.extras = dict(extras or {})


async def _run(pool: Any, *, extras: dict[str, Any] | None = None, **opts: Any):
    options = {
        "sub_handler": "claim_watch",
        "analyst_id": "claim_watch",
        "run_id": str(uuid4()),
        **opts,
    }
    result = await cw.handle([], options, _Deps(pool, extras))
    assert isinstance(result, AnalystMethodResult)
    return result


def _counters(result: AnalystMethodResult) -> dict[str, Any]:
    return dict(result.finding.data)


# -- insert helpers ---------------------------------------------------------

_SIG_SEQ = {"n": 0}


async def _insert_signal(
    conn: Any,
    *,
    geo: tuple[str, ...] = (),
    entities: list[Any] | None = None,
    canonical_url: str | None = None,
) -> UUID:
    """A signal strictly NEWER than everything before it (monotonic future
    offsets, so post-seed inserts always land past the cursor)."""
    _SIG_SEQ["n"] += 1
    sid = uuid4()
    payload: dict[str, Any] = {"title": f"kw3 signal {_SIG_SEQ['n']}"}
    if entities is not None:
        payload["entities"] = entities
    await conn.execute(
        "INSERT INTO signals (id, source_id, geo, fetched_at, payload, "
        " content_hash, canonical_url) "
        "VALUES ($1, $2, $3::text[], now() + make_interval(secs => $4), "
        "        $5::jsonb, $6, $7)",
        sid,
        _SRC,
        list(geo),
        float(_SIG_SEQ["n"]),
        json.dumps(payload),
        uuid4().hex,
        canonical_url,
    )
    return sid


_STREAM_BASE_OFFSET = 86400.0


async def _insert_signal_at(
    conn: Any,
    *,
    offset_seconds: float,
    geo: tuple[str, ...] = (),
) -> UUID:
    """A signal at a CONTROLLED position in the stream (``now + offset``).

    The cursor-policy tests need real spacing between signals, which
    :func:`_insert_signal`'s one-second monotonic steps cannot express. Every
    offset is taken from :data:`_STREAM_BASE_OFFSET` (a day out), so the
    fixture's stream is strictly newer than anything any other suite left in
    the shared session database — the same protection the monotonic offsets
    give, at the spacing these tests need."""
    sid = uuid4()
    await conn.execute(
        "INSERT INTO signals (id, source_id, geo, fetched_at, payload, "
        " content_hash) "
        "VALUES ($1, $2, $3::text[], now() + make_interval(secs => $4), "
        "        $5::jsonb, $6)",
        sid,
        _SRC,
        list(geo),
        float(offset_seconds),
        json.dumps({"title": f"kw3 stream signal @{offset_seconds}"}),
        uuid4().hex,
    )
    return sid


async def _stream_head(conn: Any) -> datetime:
    return await conn.fetchval("SELECT max(fetched_at) FROM signals")


async def _cursor_lag_from_head(conn: Any) -> float:
    """Seconds between the newest signal and the persisted cursor — the exact
    quantity the freshness horizon bounds."""
    cursor = await _cursor_row(conn)
    assert cursor is not None
    return (
        await _stream_head(conn) - datetime.fromisoformat(cursor["fetched_at"])
    ).total_seconds()


async def _signals_after_cursor(conn: Any) -> int:
    cursor = await _cursor_row(conn)
    assert cursor is not None
    return await conn.fetchval(
        "SELECT count(*)::int FROM signals "
        " WHERE (fetched_at, id) > ($1::timestamptz, $2::uuid)",
        datetime.fromisoformat(cursor["fetched_at"]),
        UUID(cursor["signal_id"]),
    )


async def _insert_entity(
    conn: Any,
    name: str,
    *,
    entity_class: str = "organization",
    merged_into: UUID | None = None,
) -> UUID:
    eid = uuid4()
    await conn.execute(
        "INSERT INTO entity_profiles "
        "  (id, data, canonical_name, entity_class, analyst_id, merged_into) "
        "VALUES ($1, '{}'::jsonb, $2, $3, $4, $5)",
        eid,
        name,
        entity_class,
        _ANALYST,
        merged_into,
    )
    return eid


async def _link(conn: Any, signal_id: UUID, entity_id: UUID) -> None:
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, analyst_id) "
        "VALUES ($1, $2, $3) ON CONFLICT DO NOTHING",
        signal_id,
        entity_id,
        _ANALYST,
    )


async def _link_all(conn: Any, signal_id: UUID, entity_ids: Any) -> None:
    """Link every entity of a matchable scene onto one signal.

    The fusion model grades on the NUMBER of distinct shared canonical
    entities, so a scene that is meant to MATCH must share more than one —
    a single shared entity is desk co-membership and deliberately falls
    below the threshold (see ``test_one_shared_entity_plus_geo_never_matches``).
    """
    for eid in entity_ids:
        await _link(conn, signal_id, eid)


async def _insert_fact(conn: Any, derived_from: list[UUID]) -> UUID:
    fid = uuid4()
    # A unique open triple per call (the facts table enforces one open row per
    # (subject, predicate, value) triple).
    await conn.execute(
        "INSERT INTO facts (id, subject, predicate, value, analyst_id, "
        " derived_from) VALUES ($1, 'kw3 subject', 'involved in', $2, $3, "
        " $4::uuid[])",
        fid,
        f"kw3-{fid.hex}",
        _ANALYST,
        derived_from,
    )
    return fid


async def _insert_question(
    conn: Any,
    thesis: str,
    *,
    status: str = "open_question",
    target_id: str | None = None,
    derived_from: list[UUID] | None = None,
    supporting: list[UUID] | None = None,
    refuting: list[UUID] | None = None,
    age_days: float = 0.0,
    harvest_class: str | None = None,
    diagnostic_evidence: list[Any] | None = None,
) -> UUID:
    """One open question. ``harvest_class`` stamps the DURABLE
    ``diagnostic_evidence`` marker ``scripts/harvest_open_questions.py``
    writes — the surface the L1 meta-question exclusion reads."""
    qid = uuid4()
    if diagnostic_evidence is None:
        diagnostic_evidence = []
        if harvest_class is not None:
            diagnostic_evidence = [
                {
                    "marker": cw.HARVEST_MARKER_KEY,
                    "origin": "harvest",
                    "harvest_class": harvest_class,
                    "source_id": f"kw3-{uuid4().hex}",
                }
            ]
    await conn.execute(
        "INSERT INTO hypotheses (id, thesis, status, target_id, analyst_id, "
        "  derived_from, supporting_signals, refuting_signals, produced_at, "
        "  diagnostic_evidence) "
        "VALUES ($1, $2, $3, $4, $5, $6::uuid[], $7::uuid[], $8::uuid[], "
        "        now() - make_interval(secs => $9), $10::jsonb)",
        qid,
        thesis,
        status,
        target_id,
        _ANALYST,
        derived_from or [],
        supporting or [],
        refuting or [],
        age_days * 86400.0,
        json.dumps(diagnostic_evidence),
    )
    return qid


async def _insert_desk(conn: Any, desk: str, geo: tuple[str, ...]) -> None:
    await conn.execute(
        "INSERT INTO target_descriptors (descriptor_id, version, schema_uri, "
        "  is_head, state, owner, name, body) "
        "VALUES ($1, 'v1', 'legba/target/2.0.0', TRUE, 'active', $2, $1, "
        "        $3::jsonb) ON CONFLICT DO NOTHING",
        desk,
        _ANALYST,
        json.dumps({"scope": {"geo": list(geo), "tags": ["watch"]}}),
    )


async def _insert_consumer(
    conn: Any, *, superseded: bool = False, kind: str = "finding"
) -> UUID:
    cid = uuid4()
    await conn.execute(
        "INSERT INTO analyst_outputs "
        "  (id, kind, title, body, confidence, data, analyst_id, schema_uri, "
        "   superseded_by, superseded_at) "
        "VALUES ($1, $2, $3, '', 0.9, '{}'::jsonb, $4, "
        "        'iglu:legba/finding/jsonschema/1-0-0', $5, "
        "        CASE WHEN $5::uuid IS NULL THEN NULL ELSE now() END)",
        cid,
        kind,
        f"kw3 consumer {cid}",
        _ANALYST,
        uuid4() if superseded else None,
    )
    return cid


async def _consume(
    conn: Any,
    consumer_id: UUID,
    consumed_id: UUID,
    *,
    consumer_kind: str = "meta_findings_synthesizer",
    context: str = "composition_basis",
) -> None:
    await conn.execute(
        "INSERT INTO output_consumption "
        "  (consumer_id, consumed_id, consumer_kind, context) "
        "VALUES ($1, $2, $3, $4) ON CONFLICT DO NOTHING",
        consumer_id,
        consumed_id,
        consumer_kind,
        context,
    )


async def _edges(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT src_id, dst_id, weight, planes, provenance_class, "
        "       matcher_version, edge_kind "
        "  FROM bearing_edges ORDER BY created_at, id"
    )


async def _flags(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT output_id, founded_on_id, moved_at, reason, closed_at "
        "  FROM review_flags ORDER BY created_at, id"
    )


async def _cursor_row(conn: Any) -> dict[str, Any] | None:
    row = await conn.fetchrow(
        "SELECT state FROM alert_trigger_watermarks "
        " WHERE trigger_class = $1 AND watermark_key = $2",
        cw.TRIGGER_CLASS,
        cw.CURSOR_KEY,
    )
    if row is None:
        return None
    state = row["state"]
    return json.loads(state) if isinstance(state, str) else dict(state)


# A standard matchable scene: desk (geo IR) + TWO canonical entities + a
# question whose lineage (fact → signal → entity links) carries both. Two is
# the minimum that matches under the graded entity plane: 2 shared entities
# (0.38) + geo (0.10) = 0.48 ≥ 0.45, while one shared entity + geo is 0.30
# and writes nothing.
async def _matchable_question(
    conn: Any,
    *,
    desk: str = "kw3_desk",
    thesis: str = "kw3 open question",
    n_entities: int = 2,
) -> tuple[UUID, tuple[UUID, ...], UUID]:
    """Returns (question_id, entity_ids, lineage_signal_id)."""
    await _insert_desk(conn, desk, ("IR",))
    ents = tuple(
        [
            await _insert_entity(conn, f"KW3 Org {uuid4().hex[:8]}")
            for _ in range(n_entities)
        ]
    )
    lineage_sig = await _insert_signal(conn)
    await _link_all(conn, lineage_sig, ents)
    fact = await _insert_fact(conn, [lineage_sig])
    qid = await _insert_question(
        conn, thesis, target_id=desk, derived_from=[fact]
    )
    return qid, ents, lineage_sig


# ---------------------------------------------------------------------------
# First run seeds silently (the 0091 bring-up contract on the ridden table)
# ---------------------------------------------------------------------------


async def test_first_run_seeds_silently(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn)
        # A pre-existing signal that WOULD match — bring-up must not flood.
        s_old = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_old, ents)

    result = await _run(pg_pool)
    c = _counters(result)
    assert c["seeded"] is True
    assert c["edges_written"] == 0 and c["flags_written"] == 0

    async with pg_pool.acquire() as conn:
        assert await _edges(conn) == []
        cursor = await _cursor_row(conn)
        assert cursor is not None
        # Seeded AT the newest signal — the backlog is behind the cursor.
        assert cursor["signal_id"] == str(s_old)
        seeded = await conn.fetchrow(
            "SELECT 1 FROM alert_trigger_watermarks "
            " WHERE trigger_class = $1 AND watermark_key = '_seeded'",
            cw.TRIGGER_CLASS,
        )
        assert seeded is not None


# ---------------------------------------------------------------------------
# Matching writes edges above threshold and NOTHING below
# ---------------------------------------------------------------------------


async def test_match_writes_edges_above_threshold_and_nothing_below(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn)
        # A second open question with NO desk: entity overlap alone stays
        # below the threshold — nothing may be written for it.
        fact2 = await _insert_fact(conn, [])
        q_entity_only = await _insert_question(
            conn, "kw3 deskless question", derived_from=[fact2]
        )
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        # Give the deskless question the SAME entities through its lineage.
        lineage2 = await _insert_signal(conn)
        await _link_all(conn, lineage2, ents)
        await conn.execute(
            "UPDATE facts SET derived_from = $2::uuid[] WHERE id = $1",
            fact2,
            [lineage2],
        )
        s_match = await _insert_signal(conn, geo=("IR",))  # entities + geo
        await _link_all(conn, s_match, ents)
        s_nomatch = await _insert_signal(conn, geo=("FR",))  # nothing

    result = await _run(pg_pool)
    c = _counters(result)
    assert c["seeded"] is False
    assert c["edges_written"] == 1
    assert c["matches_entity"] == 1 and c["matches_geo"] == 1
    assert c["matches_vector"] == 0

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
        assert len(edges) == 1
        e = edges[0]
        assert e["src_id"] == s_match and e["dst_id"] == qid
        assert sorted(e["planes"]) == ["entity", "geo"]
        assert e["provenance_class"] == "live"
        assert e["matcher_version"] == cw.MATCHER_VERSION
        assert e["edge_kind"] == "bears_on"
        # 2 shared entities (0.38) + geo (0.10) on a fresh question.
        assert 0.45 <= float(e["weight"]) <= 0.4801
        # The deskless (entity-only) question and the no-overlap signal wrote
        # NOTHING — sub-threshold pairs never land.
        assert all(r["dst_id"] != q_entity_only for r in edges)
        assert all(r["src_id"] != s_nomatch for r in edges)


async def test_one_shared_entity_plus_geo_never_matches(pg_pool, clean_slate):
    """THE production defect, end to end: a signal sharing exactly ONE
    canonical entity with a question and sitting in the same desk geo is
    desk co-membership, not new evidence. It must write NOTHING — even
    though the question is brand new and the age dampener is 1.0."""
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn)
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        # Shares exactly one of the question's two lineage entities + geo.
        s_thin = await _insert_signal(conn, geo=("IR",))
        await _link(conn, s_thin, ents[0])

    c = _counters(await _run(pg_pool))
    assert c["examined_signals"] == 1
    assert c["edges_written"] == 0
    async with pg_pool.acquire() as conn:
        assert await _edges(conn) == []

    # ...and the SAME signal lands the moment a second entity is shared.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE alert_trigger_watermarks SET state = $3::jsonb "
            " WHERE trigger_class = $1 AND watermark_key = $2",
            cw.TRIGGER_CLASS,
            cw.CURSOR_KEY,
            json.dumps(
                {"fetched_at": "1970-01-01T00:00:00+00:00",
                 "signal_id": str(UUID(int=0))}
            ),
        )
        await _link(conn, s_thin, ents[1])
    c = _counters(await _run(pg_pool))
    assert c["edges_written"] == 1


async def test_ner_name_fallback_resolves_through_election_machinery(
    pg_pool, clean_slate
):
    """A NEW signal the resolution sweep has not linked yet still matches on
    the entity plane via its NER surface names resolved through
    resolve_keeper (article-variant surface → the elected canonical row)."""
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn)
        names = [
            (
                await conn.fetchrow(
                    "SELECT canonical_name FROM entity_profiles WHERE id = $1",
                    e,
                )
            )["canonical_name"]
            for e in ents
        ]
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        # NO signal_entity_links row — only the raw NER surfaces, with an
        # article prefix the election machinery folds. BOTH of the question's
        # entities appear, so the graded entity plane has real evidence.
        s_ner = await _insert_signal(
            conn, geo=("IR",), entities=[f"The {n}" for n in names]
        )

    result = await _run(pg_pool)
    c = _counters(result)
    assert c["edges_written"] == 1
    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
        assert edges[0]["src_id"] == s_ner and edges[0]["dst_id"] == qid
        assert "entity" in edges[0]["planes"]


# ---------------------------------------------------------------------------
# Circularity guard
# ---------------------------------------------------------------------------


async def test_circularity_guard_own_evidence_never_matches(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "kw3_desk", ("IR",))
        ents = (
            await _insert_entity(conn, f"KW3 Circular {uuid4().hex[:8]}"),
            await _insert_entity(conn, f"KW3 Circular {uuid4().hex[:8]}"),
        )
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        # Both signals arrive AFTER the cursor and would fully match on
        # entity+geo — but each sits in the question's OWN evidence.
        s_in_lineage = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_in_lineage, ents)
        s_supporting = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_supporting, ents)
        fact = await _insert_fact(conn, [s_in_lineage])
        await _insert_question(
            conn,
            "kw3 circular question",
            target_id="kw3_desk",
            derived_from=[fact],
            supporting=[s_supporting],
        )

    result = await _run(pg_pool)
    c = _counters(result)
    assert c["examined_signals"] == 2
    assert c["edges_written"] == 0
    async with pg_pool.acquire() as conn:
        assert await _edges(conn) == []


# ---------------------------------------------------------------------------
# Review flags — FORWARD consumption walk to live products only
# ---------------------------------------------------------------------------


async def test_review_flags_only_for_live_traced_questions(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        # Q1 traces forward to live products; Q2 is consumed by nothing.
        q1, ents1, _ = await _matchable_question(conn, thesis="kw3 traced q")
        q2, ents2, _ = await _matchable_question(
            conn, desk="kw3_desk2", thesis="kw3 untraced q"
        )
        c_live = await _insert_consumer(conn)  # consumed Q1, live
        c_dead = await _insert_consumer(conn, superseded=True)  # superseded
        c_hop2 = await _insert_consumer(conn)  # consumes c_live (2nd hop)
        await _consume(conn, c_live, q1)
        await _consume(conn, c_dead, q1)
        await _consume(conn, c_hop2, c_live, context="journal_slice")
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s1, ents1)
        await _link_all(conn, s1, ents2)

    result = await _run(pg_pool)
    c = _counters(result)
    # Both questions matched (edges), but ONLY Q1 flags — and only its LIVE
    # consumers (the direct one and the second hop; never the superseded one).
    assert c["edges_written"] == 2
    assert c["flags_written"] == 2

    async with pg_pool.acquire() as conn:
        flags = await _flags(conn)
        assert len(flags) == 2
        flagged = {f["output_id"] for f in flags}
        assert flagged == {c_live, c_hop2}
        assert all(f["founded_on_id"] == q1 for f in flags)
        assert all(f["reason"] == cw.FLAG_REASON for f in flags)
        assert all(f["closed_at"] is None for f in flags)
        assert all(f["moved_at"] is not None for f in flags)


# ---------------------------------------------------------------------------
# staleness_debt — open flags on LIVE consumers only
# ---------------------------------------------------------------------------


async def test_staleness_debt_counts_and_exclusions(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        q1, ents, _ = await _matchable_question(conn)
        c_a = await _insert_consumer(conn)
        c_b = await _insert_consumer(conn)
        await _consume(conn, c_a, q1)
        await _consume(conn, c_b, q1)
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s1, ents)

    result = await _run(pg_pool)
    c = _counters(result)
    assert c["flags_written"] == 2
    assert c["staleness_debt"] == 2

    # A consumer that got SUPERSEDED drops out of the debt (its flag is
    # still open, but there is no live head left to re-review).
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE analyst_outputs SET superseded_by = $2, superseded_at = "
            " now() WHERE id = $1",
            c_a,
            uuid4(),
        )
    c = _counters(await _run(pg_pool))
    assert c["staleness_debt"] == 1

    # A flag CLOSED by supersession is excluded too.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE review_flags SET closed_by = $2, closed_at = now() "
            " WHERE output_id = $1 AND closed_at IS NULL",
            c_b,
            uuid4(),
        )
    c = _counters(await _run(pg_pool))
    assert c["staleness_debt"] == 0


# ---------------------------------------------------------------------------
# Idempotency — the 0107 unique constraints do the dedup
# ---------------------------------------------------------------------------


async def test_idempotent_rerun_inserts_nothing_new(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        q1, ents, _ = await _matchable_question(conn)
        c_live = await _insert_consumer(conn)
        await _consume(conn, c_live, q1)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        pre_cursor = await _cursor_row(conn)
        s1 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s1, ents)

    c = _counters(await _run(pg_pool))
    assert c["edges_written"] == 1 and c["flags_written"] == 1

    # Rewind the cursor so the SAME signal is re-scanned; the re-run must
    # dedup everything through the unique constraints and write zero rows.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE alert_trigger_watermarks SET state = $3::jsonb "
            " WHERE trigger_class = $1 AND watermark_key = $2",
            cw.TRIGGER_CLASS,
            cw.CURSOR_KEY,
            json.dumps(pre_cursor),
        )
    c = _counters(await _run(pg_pool))
    assert c["examined_signals"] == 1
    assert c["edges_written"] == 0 and c["edges_deduped"] == 1
    assert c["flags_written"] == 0 and c["flags_deduped"] == 1

    async with pg_pool.acquire() as conn:
        assert len(await _edges(conn)) == 1
        assert len(await _flags(conn)) == 1


# ---------------------------------------------------------------------------
# Watermark advance on the ridden 0091 table
# ---------------------------------------------------------------------------


async def test_cursor_advances_and_next_run_examines_nothing(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        await _matchable_question(conn)
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        _ = await _insert_signal(conn, geo=("FR",))
        s_last = await _insert_signal(conn, geo=("DE",))

    c = _counters(await _run(pg_pool))
    assert c["examined_signals"] == 2 and c["edges_written"] == 0

    async with pg_pool.acquire() as conn:
        cursor = await _cursor_row(conn)
        assert cursor["signal_id"] == str(s_last)

    c = _counters(await _run(pg_pool))
    assert c["examined_signals"] == 0


async def test_no_questions_still_advances_cursor(pg_pool, clean_slate):
    await _run(pg_pool)  # seed (no questions, maybe no signals)
    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn, geo=("IR",))
    c = _counters(await _run(pg_pool))
    assert c["examined_signals"] == 1 and c["questions_scanned"] == 0
    async with pg_pool.acquire() as conn:
        cursor = await _cursor_row(conn)
        assert cursor["signal_id"] == str(s1)


# ---------------------------------------------------------------------------
# Caps — bounded work, honest deferral, no silent loss
# ---------------------------------------------------------------------------


async def test_edge_cap_defers_whole_signals_and_next_run_catches_up(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        q1, ents, _ = await _matchable_question(conn)
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        s_a = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_a, ents)
        s_b = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_b, ents)

    c = _counters(await _run(pg_pool, edge_cap=1))
    assert c["edges_written"] == 1
    assert c["examined_signals"] == 1
    assert c["deferred_signals"] == 1  # s_b deferred, NOT dropped
    assert c["edges_dropped_run_cap"] == 0  # deferral never DROPS

    async with pg_pool.acquire() as conn:
        cursor = await _cursor_row(conn)
        assert cursor["signal_id"] == str(s_a)  # only past processed work

    c = _counters(await _run(pg_pool, edge_cap=1))
    assert c["edges_written"] == 1  # s_b lands on the catch-up run
    async with pg_pool.acquire() as conn:
        assert {e["src_id"] for e in await _edges(conn)} == {s_a, s_b}


async def test_signal_batch_cap_reports_deferral(pg_pool, clean_slate):
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for _ in range(3):
            await _insert_signal(conn)
    c = _counters(await _run(pg_pool, signal_cap=2))
    assert c["examined_signals"] == 2
    # The batch overflow is its OWN flag — deferred_signals stays a true
    # count of signals this run deferred, never a count plus a sentinel.
    assert c["signal_batch_truncated"] is True
    assert c["deferred_signals"] == 0
    # No questions to match, so nothing stalled: the cursor kept pace.
    assert c["cursor_falling_behind"] is False


# ---------------------------------------------------------------------------
# Cursor policy — the freshness horizon
#
# The structural defect the 2.0.0 diagnostics proved: an oldest-first cursor
# that advances only over fully-processed signals cannot keep pace with a
# stream faster than one run's throughput (measured live: 39 processed, 461
# deferred, ~70 ingested per tick), and a permanently lagging cursor is a
# permanently starved vector plane because signal_embedder drains
# newest-first. The horizon bounds the lag; everything it gives up is counted.
# ---------------------------------------------------------------------------


async def test_skip_ahead_counts_and_titles_what_it_abandons(
    pg_pool, clean_slate
):
    """A skip is a real loss. It may never read as coverage: the count is
    exact, it lands in the counters AND in the receipt title, and processed +
    abandoned accounts for every signal in the window."""
    await _run(pg_pool)  # seed at the pre-existing head
    async with pg_pool.acquire() as conn:
        sids = [
            await _insert_signal_at(
                conn, offset_seconds=_STREAM_BASE_OFFSET + 3600.0 * i
            )
            for i in range(1, 14)
        ]

    # Horizon 1.5h behind a head 13h out: the oldest 11 fall outside it.
    result = await _run(pg_pool, max_lag_seconds=5400.0, signal_cap=50)
    c = _counters(result)
    assert c["cursor_skipped_ahead"] is True
    assert c["signals_skipped_ahead"] == 11
    assert c["skip_count_clipped"] is False
    assert c["examined_signals"] == 2
    # Nothing unaccounted for: every signal was matched or explicitly given up.
    assert c["signals_skipped_ahead"] + c["examined_signals"] == len(sids)
    # The abandonment is in the TITLE, not buried in a counter body.
    assert "SKIPPED" in result.finding.title
    assert "11" in result.finding.title

    async with pg_pool.acquire() as conn:
        cursor = await _cursor_row(conn)
        assert cursor["signal_id"] == str(sids[-1])
        assert await _cursor_lag_from_head(conn) == pytest.approx(0.0)


async def test_no_skip_when_nothing_lies_behind_the_horizon(
    pg_pool, clean_slate
):
    """A quiet stream is not a lagging cursor. When the cursor is old but
    every unprocessed signal is INSIDE the horizon, nothing is abandoned and
    nothing is warned about — the skip fires on work given up, not on a
    clock."""
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for i in range(1, 4):
            await _insert_signal_at(
                conn, offset_seconds=_STREAM_BASE_OFFSET + 60.0 * i
            )

    c = _counters(await _run(pg_pool, max_lag_seconds=3600.0))
    assert c["cursor_lag_seconds"] > 3600.0  # the cursor IS far behind
    assert c["cursor_skipped_ahead"] is False
    assert c["signals_skipped_ahead"] == 0
    assert c["examined_signals"] == 3


async def test_skip_ahead_is_disablable_for_a_deliberate_backlog_grind(
    pg_pool, clean_slate
):
    """max_lag_seconds <= 0 forfeits the pace guarantee on purpose — an
    operator grinding a backlog must be able to say so."""
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for i in range(1, 8):
            await _insert_signal_at(
                conn, offset_seconds=_STREAM_BASE_OFFSET + 3600.0 * i
            )
    c = _counters(await _run(pg_pool, max_lag_seconds=0, signal_cap=50))
    assert c["cursor_skipped_ahead"] is False
    assert c["signals_skipped_ahead"] == 0
    assert c["examined_signals"] == 7


async def test_cursor_keeps_pace_with_a_realistic_ingest_rate(
    pg_pool, clean_slate
):
    """THE acceptance property.

    Six 30-minute ticks at 20 signals each, against a matcher deliberately
    under-provisioned at 5 signals per run — the live shape, where the run
    processes a fraction of what arrives. Without the horizon the cursor
    falls behind by 15 signals every tick, forever, which is exactly how it
    ends up parked in the un-embedded band. With it:

      * after EVERY run the cursor is within the horizon of the stream head
        (the lag is bounded, not compounding), and
      * processed + abandoned + still-pending accounts for every signal
        ingested — the shortfall is paid out loud, never dropped quietly.
    """
    ticks, per_tick, tick_seconds = 6, 20, 1800.0
    horizon = 3645.0  # ~2 ticks, deliberately off the signal-spacing grid
    await _run(pg_pool)  # seed

    examined = skipped = 0
    for t in range(ticks):
        async with pg_pool.acquire() as conn:
            for k in range(per_tick):
                await _insert_signal_at(
                    conn,
                    offset_seconds=(
                        _STREAM_BASE_OFFSET
                        + t * tick_seconds
                        + k * (tick_seconds / per_tick)
                    ),
                )
        c = _counters(
            await _run(pg_pool, signal_cap=5, max_lag_seconds=horizon)
        )
        examined += c["examined_signals"]
        skipped += c["signals_skipped_ahead"]
        # The batch was never starved of work, and the lag is BOUNDED.
        assert c["examined_signals"] == 5
        async with pg_pool.acquire() as conn:
            assert await _cursor_lag_from_head(conn) <= horizon

    async with pg_pool.acquire() as conn:
        pending = await _signals_after_cursor(conn)
    assert examined == ticks * 5
    assert skipped > 0  # the shortfall was real...
    # ...and every single signal is accounted for.
    assert examined + skipped + pending == ticks * per_tick


async def test_the_horizon_bounds_an_edge_cap_stall(pg_pool, clean_slate):
    """The edge-cap deferral and the horizon coexist: deferral keeps a run
    loss-free, and the horizon keeps a REPEATED deferral from walking the
    cursor off the back of the stream. Raising edge_cap is still not the fix
    — this just stops the stall from being unbounded."""
    async with pg_pool.acquire() as conn:
        _q, ents, _ = await _matchable_question(conn)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for i in range(1, 21):
            s = await _insert_signal_at(
                conn,
                offset_seconds=_STREAM_BASE_OFFSET + 600.0 * i,
                geo=("IR",),
            )
            await _link_all(conn, s, ents)

    horizon = 3100.0
    first = _counters(
        await _run(
            pg_pool, edge_cap=1, signal_cap=50, max_lag_seconds=horizon
        )
    )
    assert first["signals_skipped_ahead"] > 0
    assert first["deferred_signals"] > 0  # the edge cap still defers, losslessly
    assert first["edges_dropped_run_cap"] == 0

    for _ in range(3):
        await _run(pg_pool, edge_cap=1, signal_cap=50, max_lag_seconds=horizon)
        async with pg_pool.acquire() as conn:
            assert await _cursor_lag_from_head(conn) <= horizon


# ---------------------------------------------------------------------------
# Vector plane — injected store + embedder (the signal_embedder deps keys)
# ---------------------------------------------------------------------------


class _FakeStoreCfg:
    signals_collection = "legba_signals"


class _FakeStore:
    def __init__(self, vectors: dict[str, list[float]]) -> None:
        self.cfg = _FakeStoreCfg()
        self.vectors = vectors
        self.requested: list[str] = []

    async def retrieve_vectors(self, collection, ids):
        self.requested.extend(ids)
        return {i: self.vectors[i] for i in ids if i in self.vectors}


class _FakeEmbedder:
    def __init__(self, vec: list[float]) -> None:
        self.vec = vec
        self.calls = 0

    async def embed(self, text: str) -> list[float]:
        self.calls += 1
        return list(self.vec)


def _vec(direction: int, dim: int = 8) -> list[float]:
    v = [0.0] * dim
    v[direction] = 1.0
    return v


async def test_vector_plane_matches_on_stored_signal_vectors(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        # No entities, no desk — the vector plane must carry the match alone.
        qid = await _insert_question(conn, "kw3 vector question")
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        s_close = await _insert_signal(conn)
        s_far = await _insert_signal(conn)

    store = _FakeStore({str(s_close): _vec(0), str(s_far): _vec(1)})
    embedder = _FakeEmbedder(_vec(0))  # thesis embeds parallel to s_close
    extras = {
        cw.QDRANT_DEPS_EXTRA_KEY: store,
        cw.EMBEDDER_DEPS_EXTRA_KEY: embedder,
    }

    c = _counters(await _run(pg_pool, extras=extras))
    assert c["vector_plane_wired"] is True
    assert c["signal_vectors_found"] == 2
    assert c["question_embeds"] == 1
    assert c["edges_written"] == 1
    assert c["matches_vector"] == 1

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
        assert len(edges) == 1
        assert edges[0]["src_id"] == s_close and edges[0]["dst_id"] == qid
        assert edges[0]["planes"] == ["vector"]
        # cosine 1.0 → 0.5 * 1.0 on a fresh question.
        assert float(edges[0]["weight"]) == pytest.approx(0.5, abs=1e-3)

    # Re-scan (cursor rewound): the question embedding comes from the cache.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "UPDATE alert_trigger_watermarks SET state = $3::jsonb "
            " WHERE trigger_class = $1 AND watermark_key = $2",
            cw.TRIGGER_CLASS,
            cw.CURSOR_KEY,
            json.dumps(
                {"fetched_at": "1970-01-01T00:00:00+00:00",
                 "signal_id": str(UUID(int=0))}
            ),
        )
    embeds_before = embedder.calls
    c = _counters(await _run(pg_pool, extras=extras))
    assert c["question_embed_cache_hits"] == 1
    assert embedder.calls == embeds_before  # no re-embed
    assert c["edges_written"] == 0 and c["edges_deduped"] == 1


async def test_vector_plane_absent_degrades_to_entity_geo(pg_pool, clean_slate):
    async with pg_pool.acquire() as conn:
        q1, ents, _ = await _matchable_question(conn)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s1, ents)
    c = _counters(await _run(pg_pool))  # no extras — plane unwired
    assert c["vector_plane_wired"] is False
    # An UNWIRED plane is not a STARVED one — starvation is specifically a
    # wired plane that covers nothing.
    assert c["vector_plane_starved"] is False
    assert c["edges_written"] == 1  # strong entity evidence still lands


# ---------------------------------------------------------------------------
# Vector-plane STARVATION — the KW-3 first-live defect
#
# Production wrote 591 bearing_edges across several runs with ZERO 'vector'
# in planes and every weight at exactly 0.500, while the boot log said the
# plane was wired. The plane was wired; it just covered nothing, and the
# handler had no way to say so — an empty retrieve looked exactly like "no
# signals matched semantically". These tests pin the distinction.
# ---------------------------------------------------------------------------


class _EmptyStore:
    """A WIRED store that holds no vectors for this batch — the production
    condition (the cursor sits in signals signal_embedder has not reached)."""

    def __init__(self) -> None:
        class _C:
            signals_collection = "legba_signals"

        self.cfg = _C()
        self.requested: list[str] = []

    async def retrieve_vectors(self, collection, ids):
        self.requested.extend(ids)
        return {}


class _BrokenStore:
    """A WIRED store whose transport fails."""

    def __init__(self) -> None:
        class _C:
            signals_collection = "legba_signals"

        self.cfg = _C()
        self.calls = 0

    async def retrieve_vectors(self, collection, ids):
        self.calls += 1
        raise RuntimeError("qdrant unreachable")


async def test_wired_vector_plane_covering_nothing_is_reported_loudly(
    pg_pool, clean_slate
):
    """REGRESSION: a wired vector plane that contributes to NOTHING must be
    a named, asserted condition — not an empty dict indistinguishable from
    'no semantic match'. This is the counter the first-live triage needed
    and did not have."""
    async with pg_pool.acquire() as conn:
        await _insert_question(conn, "kw3 starved question")
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for _ in range(3):
            await _insert_signal(conn)

    store = _EmptyStore()
    embedder = _FakeEmbedder(_vec(0))
    c = _counters(
        await _run(
            pg_pool,
            extras={
                cw.QDRANT_DEPS_EXTRA_KEY: store,
                cw.EMBEDDER_DEPS_EXTRA_KEY: embedder,
            },
        )
    )
    assert c["vector_plane_wired"] is True
    # Both sides were ASKED for: the question embedded, the signals looked up.
    assert c["question_embeds"] == 1
    assert len(store.requested) == 3
    # ...and the coverage shortfall is named, counted and flagged.
    assert c["signal_vectors_found"] == 0
    assert c["signal_vectors_missing"] == 3
    assert c["vector_plane_starved"] is True
    # A coverage shortfall is NOT a transport error — they must not conflate.
    assert c["vector_plane_errors"] == 0
    assert c["matches_vector"] == 0


async def test_vector_store_transport_failure_is_distinct_from_no_coverage(
    pg_pool, clean_slate
):
    """A broken store and an un-embedded batch both yield zero vectors. They
    are different faults with different fixes, so they get different
    counters — conflating them is what made the live plane undiagnosable."""
    async with pg_pool.acquire() as conn:
        await _insert_question(conn, "kw3 broken-store question")
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        await _insert_signal(conn)

    store = _BrokenStore()
    c = _counters(
        await _run(
            pg_pool,
            extras={
                cw.QDRANT_DEPS_EXTRA_KEY: store,
                cw.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder(_vec(0)),
            },
        )
    )
    assert store.calls == 1
    assert c["vector_plane_errors"] == 1
    assert c["vector_plane_starved"] is True
    assert c["signal_vectors_found"] == 0
    # The run still COMPLETES — degrade-not-break survives the loudness.
    assert c["seeded"] is False


async def test_vector_fetch_is_chunked_and_a_bad_chunk_stays_partial(
    pg_pool, clean_slate
):
    """The batch is up to signal_cap ids at ~1k floats each: one whole-batch
    retrieve is both a large response and a single point of failure. Chunked
    fetching keeps a transport error PARTIAL instead of zeroing the plane."""
    n = cw._MAX_VECTOR_FETCH_IDS + 5
    async with pg_pool.acquire() as conn:
        await _insert_question(conn, "kw3 chunked question")
    await _run(pg_pool)  # seed
    sids = []
    async with pg_pool.acquire() as conn:
        for _ in range(n):
            sids.append(await _insert_signal(conn))

    class _ChunkStore:
        def __init__(self) -> None:
            class _C:
                signals_collection = "legba_signals"

            self.cfg = _C()
            self.chunks: list[int] = []

        async def retrieve_vectors(self, collection, ids):
            ids = list(ids)
            self.chunks.append(len(ids))
            if len(self.chunks) == 1:
                raise RuntimeError("chunk 1 transport failure")
            return {i: _vec(0) for i in ids}

    store = _ChunkStore()
    c = _counters(
        await _run(
            pg_pool,
            extras={
                cw.QDRANT_DEPS_EXTRA_KEY: store,
                cw.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder(_vec(0)),
            },
            edge_cap=n,
        )
    )
    # More than one call, and none exceeding the chunk bound.
    assert len(store.chunks) > 1
    assert max(store.chunks) <= cw._MAX_VECTOR_FETCH_IDS
    assert sum(store.chunks) == n
    # The failed chunk cost only its own ids — the rest still scored.
    assert c["vector_plane_errors"] == 1
    assert c["signal_vectors_found"] == n - cw._MAX_VECTOR_FETCH_IDS
    assert c["signal_vectors_missing"] == cw._MAX_VECTOR_FETCH_IDS
    assert c["vector_plane_starved"] is False  # partial coverage is not starved
    assert c["matches_vector"] == n - cw._MAX_VECTOR_FETCH_IDS


# ---------------------------------------------------------------------------
# TAIL-HOLD — the other half of "the vector plane never participates"
#
# Even with a cursor at the head, the freshest rows in a batch are routinely
# younger than signal_embedder's last (15-minute, newest-first) sweep.
# Matching them blind is how the plane contributes nothing while every counter
# says it is wired. They are held one tick instead — held, never dropped.
# ---------------------------------------------------------------------------


async def _vector_only_question(conn: Any) -> UUID:
    """A question with no desk and no lineage entities: only the vector plane
    can carry a match, so vector participation is unambiguous."""
    return await _insert_question(conn, "kw3 tail-hold question")


async def test_unembedded_head_is_held_then_scored_with_vectors_next_run(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        qid = await _vector_only_question(conn)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn)
        s2 = await _insert_signal(conn)
        s3 = await _insert_signal(conn)  # the newest: not embedded yet

    store = _FakeStore({str(s1): _vec(0), str(s2): _vec(0)})
    extras = {
        cw.QDRANT_DEPS_EXTRA_KEY: store,
        cw.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder(_vec(0)),
    }

    c = _counters(await _run(pg_pool, extras=extras))
    assert c["held_for_embedding"] == 1
    assert c["examined_signals"] == 2
    # Coverage is reported over what the run actually MATCHED on, so a held
    # row is not booked as a coverage miss.
    assert c["signal_vectors_found"] == 2
    assert c["signal_vectors_missing"] == 0
    assert c["matches_vector"] == 2
    async with pg_pool.acquire() as conn:
        assert {e["src_id"] for e in await _edges(conn)} == {s1, s2}
        # The cursor stopped SHORT of the held row — nothing was skipped.
        cursor = await _cursor_row(conn)
        assert cursor["signal_id"] == str(s2)

    # The embedder catches up; the held signal now scores WITH its vector.
    store.vectors[str(s3)] = _vec(0)
    c = _counters(await _run(pg_pool, extras=extras))
    assert c["held_for_embedding"] == 0
    assert c["examined_signals"] == 1
    assert c["matches_vector"] == 1
    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
        assert {e["src_id"] for e in edges} == {s1, s2, s3}
        assert all(e["planes"] == ["vector"] for e in edges)
        assert all(
            e["matcher_version"] == cw.MATCHER_VERSION for e in edges
        )


async def test_tail_hold_is_disarmed_when_the_batch_is_truncated(
    pg_pool, clean_slate
):
    """A truncated batch means the run is BEHIND the head, where uncovered
    means the newest-first embedder has not come back this far — and never
    will on this tick. Holding there would wedge the cursor, so it must not
    happen."""
    async with pg_pool.acquire() as conn:
        await _vector_only_question(conn)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        sids = [await _insert_signal(conn) for _ in range(4)]

    store = _FakeStore({str(sids[0]): _vec(0)})
    c = _counters(
        await _run(
            pg_pool,
            signal_cap=2,
            extras={
                cw.QDRANT_DEPS_EXTRA_KEY: store,
                cw.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder(_vec(0)),
            },
        )
    )
    assert c["signal_batch_truncated"] is True
    assert c["held_for_embedding"] == 0
    assert c["examined_signals"] == 2
    assert c["signal_vectors_missing"] == 1  # honestly reported, not held


async def test_tail_hold_never_engages_when_the_plane_covers_nothing(
    pg_pool, clean_slate
):
    """A plane covering NOTHING is starvation — already its own loud
    condition. Holding the whole batch on top of it would turn a diagnosable
    fault into a silent stall."""
    async with pg_pool.acquire() as conn:
        await _vector_only_question(conn)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for _ in range(3):
            await _insert_signal(conn)

    c = _counters(
        await _run(
            pg_pool,
            extras={
                cw.QDRANT_DEPS_EXTRA_KEY: _EmptyStore(),
                cw.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder(_vec(0)),
            },
        )
    )
    assert c["vector_plane_starved"] is True
    assert c["held_for_embedding"] == 0
    assert c["examined_signals"] == 3  # forward progress, not a wedge
    async with pg_pool.acquire() as conn:
        assert await _signals_after_cursor(conn) == 0


async def test_tail_hold_releases_rows_older_than_the_grace(
    pg_pool, clean_slate
):
    """The hold waits for an embedder that is plausibly still coming. Past the
    grace it is not coming, and the matcher processes the row rather than
    waiting on it forever — a dead embedder costs one grace period, not the
    analyst."""
    async with pg_pool.acquire() as conn:
        await _vector_only_question(conn)
    await _run(pg_pool)  # seed
    base = _STREAM_BASE_OFFSET
    async with pg_pool.acquire() as conn:
        covered = await _insert_signal_at(conn, offset_seconds=base)
        stale = await _insert_signal_at(conn, offset_seconds=base + 3600.0)
        fresh = [
            await _insert_signal_at(conn, offset_seconds=base + 36000.0 + 60.0 * k)
            for k in range(3)
        ]

    store = _FakeStore({str(covered): _vec(0)})
    c = _counters(
        await _run(
            pg_pool,
            unembedded_hold_max_age_seconds=7200.0,
            max_lag_seconds=0,  # horizon off: this test isolates the grace
            extras={
                cw.QDRANT_DEPS_EXTRA_KEY: store,
                cw.EMBEDDER_DEPS_EXTRA_KEY: _FakeEmbedder(_vec(0)),
            },
        )
    )
    # The three rows at the head are held; the one 8h back is not.
    assert c["held_for_embedding"] == len(fresh)
    assert c["examined_signals"] == 2
    async with pg_pool.acquire() as conn:
        cursor = await _cursor_row(conn)
        assert cursor["signal_id"] == str(stale)


# ---------------------------------------------------------------------------
# Anti-explosion — a realistic desk must not match everything to everything
# ---------------------------------------------------------------------------


async def test_desk_shaped_fixture_does_not_explode(pg_pool, clean_slate):
    """A country desk: many open questions, all sharing the desk's headline
    entity and its geo, plus signals that mention that one entity.

    Under the pre-fix fusion (boolean 0.35*entity + 0.15*geo = 0.50 > 0.45)
    every signal matched EVERY question — the per-signal cap and then the
    per-run edge cap were the only things containing it, and the run
    saturated at edge_cap while the cursor crawled. The acceptance property
    is that mere desk co-membership produces NO edges at all, and that the
    run therefore keeps pace with its signal batch.
    """
    n_questions = 40
    n_signals = 25
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "kw3_desk", ("IR",))
        headline = await _insert_entity(conn, f"KW3 Desk HQ {uuid4().hex[:8]}")
        lineage = await _insert_signal(conn)
        await _link(conn, lineage, headline)
        fact = await _insert_fact(conn, [lineage])
        for i in range(n_questions):
            await _insert_question(
                conn, f"kw3 desk question {i}", target_id="kw3_desk",
                derived_from=[fact],
            )
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        for _ in range(n_signals):
            s = await _insert_signal(conn, geo=("IR",))
            await _link(conn, s, headline)  # the ONE desk entity + desk geo

    c = _counters(await _run(pg_pool))
    assert c["questions_scanned"] == n_questions
    assert c["examined_signals"] == n_signals
    # Desk co-membership is not evidence: nothing lands at all.
    assert c["edges_written"] == 0
    assert c["edges_written"] / n_signals < 1.0
    # Nothing was silently dropped to achieve that — the caps never engaged.
    assert c["edges_dropped_per_signal_cap"] == 0
    assert c["edges_dropped_run_cap"] == 0
    # ...and the run kept pace: the whole batch was processed, so the cursor
    # advances with the stream instead of stranding in the un-embedded band.
    assert c["deferred_signals"] == 0
    assert c["cursor_falling_behind"] is False


async def test_cursor_falling_behind_is_loud_when_the_edge_cap_stalls_it(
    pg_pool, clean_slate
):
    """The mechanism that starved the vector plane: a run that BOTH fills its
    signal batch AND defers work advanced the cursor by less than one batch
    while the stream moved on. That has to be visible."""
    async with pg_pool.acquire() as conn:
        q1, ents, _ = await _matchable_question(conn)
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        for _ in range(4):
            s = await _insert_signal(conn, geo=("IR",))
            await _link_all(conn, s, ents)

    c = _counters(await _run(pg_pool, signal_cap=2, edge_cap=1))
    assert c["signal_batch_truncated"] is True
    assert c["deferred_signals"] == 1
    assert c["cursor_falling_behind"] is True
    # Loss-free all the same: the deferred signal is caught, not dropped.
    assert c["edges_dropped_run_cap"] == 0


# ---------------------------------------------------------------------------
# Entity SPECIFICITY on a real desk — the measured 2.0.0 pathology
# ---------------------------------------------------------------------------


async def _specificity_desk(
    conn: Any, *, desk: str, n_questions: int
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], list[UUID]]:
    """A country-desk shape: THREE headline entities carried by the lineage of
    every question on the desk (df = 1.0 — the desk's furniture), plus two
    rare entities carried by exactly one of them.

    Returns (furniture entity ids, rare entity ids, question ids)."""
    await _insert_desk(conn, desk, ("IR",))
    furniture = tuple(
        [
            await _insert_entity(conn, f"KW3 Headline {uuid4().hex[:8]}")
            for _ in range(3)
        ]
    )
    rare = tuple(
        [await _insert_entity(conn, f"KW3 Rare {uuid4().hex[:8]}") for _ in range(2)]
    )
    f_sig = await _insert_signal(conn)
    await _link_all(conn, f_sig, furniture)
    f_fact = await _insert_fact(conn, [f_sig])
    r_sig = await _insert_signal(conn)
    await _link_all(conn, r_sig, rare)
    r_fact = await _insert_fact(conn, [r_sig])

    qids = []
    for i in range(n_questions):
        qids.append(
            await _insert_question(
                conn,
                f"kw3 {desk} question {i}",
                target_id=desk,
                derived_from=[f_fact] if i else [f_fact, r_fact],
            )
        )
    return furniture, rare, qids


async def test_desk_furniture_entities_do_not_add_up_to_a_match(
    pg_pool, clean_slate
):
    """THE 2.0.0 measurement: 150 of 185 edges at exactly 0.560 — three shared
    entities, entity plane alone. On a country desk those three are the desk's
    own headline names, shared with almost every question on it. Counting them
    as three entities' worth of evidence is the same desk co-membership the
    graded plane was built to exclude, wearing three names.

    Weighted by desk-relative specificity, the furniture signal writes nothing
    while a signal sharing two genuinely rare entities still lands."""
    async with pg_pool.acquire() as conn:
        furniture, rare, qids = await _specificity_desk(
            conn, desk="kw3_spec_desk", n_questions=6
        )
    await _run(pg_pool)  # seed

    async with pg_pool.acquire() as conn:
        s_furniture = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_furniture, furniture)
        s_rare = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_rare, rare)

    c = _counters(await _run(pg_pool))
    assert c["questions_scanned"] == 6
    assert c["entity_specificity_desks"] == 1
    # The three furniture entities, down-weighted on BOTH comparison surfaces
    # (canonical ids and canonical names). The rare pair is untouched.
    assert c["entity_specificity_downweighted"] == 6

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
    # Three shared desk-headline entities + desk geo: NOTHING. Two shared rare
    # entities + geo: one edge, to the one question whose lineage carries them.
    assert {(e["src_id"], e["dst_id"]) for e in edges} == {(s_rare, qids[0])}
    assert edges[0]["matcher_version"] == cw.MATCHER_VERSION


async def test_one_specific_entity_plus_geo_still_never_matches(
    pg_pool, clean_slate
):
    """The standing anti-co-membership property, restated against the new
    weighting: a FULLY specific single shared entity plus desk geo is still
    below threshold. Specificity lowers weights; it never raises one."""
    async with pg_pool.acquire() as conn:
        _furniture, rare, _qids = await _specificity_desk(
            conn, desk="kw3_spec_one", n_questions=6
        )
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        s = await _insert_signal(conn, geo=("IR",))
        await _link(conn, s, rare[0])  # ONE rare entity + desk geo

    c = _counters(await _run(pg_pool))
    assert c["edges_written"] == 0
    async with pg_pool.acquire() as conn:
        assert await _edges(conn) == []


async def test_specificity_stays_inert_on_a_desk_too_small_to_estimate(
    pg_pool, clean_slate
):
    """Document frequency from three documents is not an estimate. Below the
    floor the rule does nothing and the desk scores on raw distinct counts —
    an honest limitation, and far better than muting a whole desk on a
    statistic invented from one observation."""
    n = cw.MIN_DESK_QUESTIONS_FOR_SPECIFICITY - 2
    async with pg_pool.acquire() as conn:
        furniture, _rare, qids = await _specificity_desk(
            conn, desk="kw3_spec_small", n_questions=n
        )
    await _run(pg_pool)  # seed
    async with pg_pool.acquire() as conn:
        s = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s, furniture)

    c = _counters(await _run(pg_pool))
    assert c["entity_specificity_desks"] == 0
    assert c["entity_specificity_downweighted"] == 0
    # Raw counts: 3 shared entities (0.56) + geo clears the threshold.
    assert c["edges_written"] == n
    async with pg_pool.acquire() as conn:
        assert {e["dst_id"] for e in await _edges(conn)} == set(qids)


# ---------------------------------------------------------------------------
# v3.2.0 lever L1 — META-QUESTION EXCLUSION (live)
# ---------------------------------------------------------------------------

# L2 is a QUERIED lever over the whole signal stream, so every test below that
# is not ABOUT L2 pins it inert with an unreachable sample floor. Without that
# pin an unrelated suite's signal volume could silently start damping these
# fixtures' entities and turn a lever test into a coin flip.
_L2_OFF = {"global_df_min_signals": 10**9}


async def test_meta_questions_are_skipped_counted_and_stay_open(
    pg_pool, clean_slate
):
    """The K-4 measurement: harvested meta classes scored 2/58 = 0.034 against
    32/65 = 0.492 for substantive theses. A collection-gap question ("what
    sources would close this gap") cannot be answered by a wire story, so the
    matcher refuses to score it — and SAYS so in the receipt counters AND the
    title. It stays an open question: only the matcher ignores it."""
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "kw3_meta_desk", ("IR",))
        ents = tuple(
            [
                await _insert_entity(conn, f"KW3 Meta Ent {uuid4().hex[:8]}")
                for _ in range(2)
            ]
        )
        lineage = await _insert_signal(conn)
        await _link_all(conn, lineage, ents)
        fact = await _insert_fact(conn, [lineage])
        # Two questions with IDENTICAL matchable lineage — the ONLY difference
        # is the durable harvest-class marker.
        q_meta = await _insert_question(
            conn,
            "Collection gap: the military_posture dimension for desk X is "
            "starved. What sources would close it?",
            target_id="kw3_meta_desk",
            derived_from=[fact],
            harvest_class="collection_gap",
        )
        q_real = await _insert_question(
            conn,
            "Will the strike on the Jordan base draw a direct Iranian reply?",
            target_id="kw3_meta_desk",
            derived_from=[fact],
        )
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    result = await _run(pg_pool, **_L2_OFF)
    c = _counters(result)
    assert c["questions_scanned"] == 2
    assert c["skipped_meta_questions"] == 1
    assert c["questions_matchable"] == 1
    # The exclusion is in the TITLE, not only the counters.
    assert "meta question(s) not scored" in result.finding.title

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
        # The meta question is STILL an open question — L1 is a matcher-side
        # skip, never a status mutation.
        assert (
            await conn.fetchval(
                "SELECT status FROM hypotheses WHERE id = $1", q_meta
            )
            == "open_question"
        )
    assert {e["dst_id"] for e in edges} == {q_real}
    # The stamp is the ONLY thing that tells an edge written under one model
    # apart from an edge written under another, so it is pinned to a LITERAL
    # here — a bump must be a deliberate edit, never a silent inherit.
    assert edges[0]["matcher_version"] == "claim_watch/3.3.0"


async def test_every_meta_class_is_skipped_and_fact_contention_is_not(
    pg_pool, clean_slate
):
    """Class-by-class, against the live substrate. ``fact_contention``
    ("which value of 'border with' for 'madrid' is correct?") is a question
    about the WORLD and is deliberately still scored."""
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "kw3_cls_desk", ("IR",))
        ents = tuple(
            [
                await _insert_entity(conn, f"KW3 Cls Ent {uuid4().hex[:8]}")
                for _ in range(2)
            ]
        )
        lineage = await _insert_signal(conn)
        await _link_all(conn, lineage, ents)
        fact = await _insert_fact(conn, [lineage])
        scored: dict[str, UUID] = {}
        for hc in sorted(cw.META_QUESTION_CLASSES) + ["fact_contention", None]:
            scored[str(hc)] = await _insert_question(
                conn,
                f"kw3 class probe {hc}",
                target_id="kw3_cls_desk",
                derived_from=[fact],
                harvest_class=hc,
            )
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    c = _counters(await _run(pg_pool, **_L2_OFF))
    assert c["questions_scanned"] == len(cw.META_QUESTION_CLASSES) + 2
    assert c["skipped_meta_questions"] == len(cw.META_QUESTION_CLASSES)
    assert c["questions_matchable"] == 2

    async with pg_pool.acquire() as conn:
        matched = {e["dst_id"] for e in await _edges(conn)}
    # Only the world-state question and the unmarked one were scored.
    assert matched == {scored["fact_contention"], scored["None"]}


async def test_meta_exclusion_is_operator_disablable(pg_pool, clean_slate):
    """An EXPLICIT empty class list turns the lever off — the taxonomy is
    data-defined, so re-including a class must not need a deploy. The receipt
    then honestly reports a zero rather than staying silent."""
    async with pg_pool.acquire() as conn:
        await _insert_desk(conn, "kw3_meta_off", ("IR",))
        ents = tuple(
            [
                await _insert_entity(conn, f"KW3 Off Ent {uuid4().hex[:8]}")
                for _ in range(2)
            ]
        )
        lineage = await _insert_signal(conn)
        await _link_all(conn, lineage, ents)
        fact = await _insert_fact(conn, [lineage])
        q_meta = await _insert_question(
            conn,
            "Floored claim (military_posture, desk X) failed the floor",
            target_id="kw3_meta_off",
            derived_from=[fact],
            harvest_class="below_floor",
        )
    await _run(pg_pool, meta_question_classes=[], **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        probe = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, probe, ents)

    c = _counters(await _run(pg_pool, meta_question_classes=[], **_L2_OFF))
    assert c["skipped_meta_questions"] == 0
    assert c["questions_matchable"] == 1
    async with pg_pool.acquire() as conn:
        assert {e["dst_id"] for e in await _edges(conn)} == {q_meta}


# ---------------------------------------------------------------------------
# v3.2.0 lever L2 — GLOBAL (signal-side) hub-entity damping (live)
# ---------------------------------------------------------------------------


async def _global_df_scene(
    conn: Any, *, desk: str, n_filler: int
) -> tuple[tuple[UUID, ...], tuple[UUID, ...], list[UUID]]:
    """A stream in which THREE entities are globally ubiquitous and three are
    rare, on a desk too SMALL for the desk-relative rule to fire — so the only
    lever under test is the global one.

    ``n_filler`` signals each carry the three hubs, which is what MAKES them
    hubs: nothing about their names is special and nothing is hardcoded, which
    is the property the lever claims (computed, never curated).
    Returns (hub ids, rare ids, question ids)."""
    await _insert_desk(conn, desk, ("IR",))
    hubs = tuple(
        [await _insert_entity(conn, f"KW3 Hub {uuid4().hex[:8]}") for _ in range(3)]
    )
    rares = tuple(
        [await _insert_entity(conn, f"KW3 Rare {uuid4().hex[:8]}") for _ in range(3)]
    )
    # ONE lineage signal carrying all six, so both probes share their three
    # with every question and the ONLY difference is stream ubiquity.
    lineage = await _insert_signal(conn)
    await _link_all(conn, lineage, hubs + rares)
    fact = await _insert_fact(conn, [lineage])
    # Below MIN_DESK_QUESTIONS_FOR_SPECIFICITY ⇒ the desk-relative rule is
    # inert and cannot be mistaken for the global one.
    qids = [
        await _insert_question(
            conn, f"kw3 {desk} q{i}", target_id=desk, derived_from=[fact]
        )
        for i in range(cw.MIN_DESK_QUESTIONS_FOR_SPECIFICITY - 2)
    ]
    # Filler stream: bulk-insert n_filler signals, each linked to the hubs.
    # Stamped OLDER than the fixture's other rows (a negative offset) so they
    # sit behind the seed cursor and never enter a match batch — they are
    # stream CONTEXT for the df window, not candidates.
    tag = f"kw3 filler {desk}"
    await conn.execute(
        "INSERT INTO signals (id, source_id, geo, fetched_at, payload, "
        "  content_hash) "
        "SELECT gen_random_uuid(), $1, '{}'::text[], "
        "       now() - make_interval(secs => 600 + g), "
        "       jsonb_build_object('title', $3::text), md5(random()::text) "
        "  FROM generate_series(1, $2) g",
        _SRC,
        n_filler,
        tag,
    )
    await conn.execute(
        "INSERT INTO signal_entity_links (signal_id, entity_id, analyst_id) "
        "SELECT s.id, e.eid, $1 FROM signals s "
        "  CROSS JOIN (SELECT unnest($2::uuid[]) AS eid) e "
        " WHERE s.source_id = $3 AND s.payload->>'title' = $4 "
        " ON CONFLICT DO NOTHING",
        _ANALYST,
        list(hubs),
        _SRC,
        tag,
    )
    return hubs, rares, qids


async def _attributed_and_window(conn: Any) -> tuple[int, int]:
    """(attributed signals, window size) for THIS fixture's stream.

    The window is pinned to the fixture's own signal count so the measurement
    can never be diluted by another suite's rows; the attributed count is
    recomputed independently here so the test pins the REAL df denominator the
    handler used rather than trusting its own arithmetic."""
    window = await conn.fetchval(
        "SELECT count(*)::int FROM signals WHERE source_id = $1", _SRC
    )
    attributed = await conn.fetchval(
        "SELECT count(DISTINCT sel.signal_id)::int "
        "  FROM signal_entity_links sel "
        "  JOIN (SELECT id FROM signals ORDER BY fetched_at DESC, id DESC "
        "         LIMIT $1) w ON w.id = sel.signal_id",
        window,
    )
    return attributed, window


async def test_stream_hub_entities_are_floored_and_specific_ones_untouched(
    pg_pool, clean_slate
):
    """THE K-4 entity-only measurement (0/54): globally ubiquitous names
    bridge unrelated desks, and v3's df is desk-question-side so it cannot see
    them.

    Three entities carried by ~all of the stream fuse BELOW threshold even
    with desk geo; three carried by almost none of it still land. The desk is
    deliberately too small for the desk-relative rule, isolating L2."""
    # 100+ attributed signals so a rare entity's df (2 appearances) sits at or
    # under GLOBAL_DF_UBIQUITY_KNEE and is genuinely UNTOUCHED, not merely
    # discounted less.
    async with pg_pool.acquire() as conn:
        hubs, rares, qids = await _global_df_scene(
            conn, desk="kw3_glob_desk", n_filler=100
        )
    await _run(pg_pool, **_L2_OFF)  # seed with the lever pinned off

    async with pg_pool.acquire() as conn:
        s_hub = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_hub, hubs)
        s_rare = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_rare, rares)
        attributed, window = await _attributed_and_window(conn)

    c = _counters(
        await _run(pg_pool, global_df_window=window, global_df_min_signals=50)
    )
    # The sample is the ATTRIBUTED signals in the window, and it is REPORTED.
    assert c["global_specificity_inert"] is False
    assert c["global_specificity_sample"] == attributed
    # Exactly the three hubs were down-weighted; the three rare ones were not.
    assert c["global_specificity_downweighted"] == 3
    # The desk-relative rule stayed inert, so this is the global lever alone.
    assert c["entity_specificity_desks"] == 0
    assert c["entity_specificity_downweighted"] == 0

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
    # Three STREAM-UBIQUITOUS shared entities + desk geo: NOTHING (3 x 0.25
    # ⇒ 0.15 + 0.10 = 0.25 < 0.45). Three rare ones + geo: 0.66, one edge per
    # question.
    assert {e["src_id"] for e in edges} == {s_rare}
    assert {e["dst_id"] for e in edges} == set(qids)


async def test_global_damping_is_inert_below_the_sample_floor(
    pg_pool, clean_slate
):
    """Document frequency from a handful of documents is not an estimate — one
    burst story would manufacture a hub. Below the floor the lever does
    NOTHING and reports that it did, mirroring
    MIN_DESK_QUESTIONS_FOR_SPECIFICITY's honesty rather than guessing."""
    async with pg_pool.acquire() as conn:
        hubs, _rares, qids = await _global_df_scene(
            conn, desk="kw3_glob_inert", n_filler=12
        )
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        s_hub = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s_hub, hubs)
        _attributed, window = await _attributed_and_window(conn)

    c = _counters(
        await _run(
            pg_pool,
            global_df_window=window,
            global_df_min_signals=cw.MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY,
        )
    )
    assert c["global_specificity_inert"] is True
    assert c["global_specificity_downweighted"] == 0
    assert 0 < c["global_specificity_sample"] < (
        cw.MIN_SIGNALS_FOR_GLOBAL_SPECIFICITY
    )
    # Inert ⇒ raw worth ⇒ 3 shared entities + geo still clears, exactly as it
    # did before this lever existed.
    assert c["edges_written"] == len(qids)


async def test_global_damping_only_ever_lowers_a_weight(pg_pool, clean_slate):
    """The conservatism invariant, live: turning the lever ON can never make a
    pair match that did not match with it OFF, and never raises a weight."""
    async with pg_pool.acquire() as conn:
        hubs, rares, _qids = await _global_df_scene(
            conn, desk="kw3_glob_mono", n_filler=100
        )
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        s = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s, hubs + rares)

    off = _counters(await _run(pg_pool, **_L2_OFF))
    async with pg_pool.acquire() as conn:
        weights_off = sorted(float(e["weight"]) for e in await _edges(conn))
        # Reset only the matcher's OWN state and re-run the same scene with the
        # lever on (TRUNCATE, never DELETE — the 0107 forbid-delete trigger is
        # row-level and the clean_slate fixture takes the same route).
        await conn.execute("TRUNCATE bearing_edges")
        await conn.execute("TRUNCATE alert_trigger_watermarks")

    await _run(pg_pool, **_L2_OFF)  # re-seed
    async with pg_pool.acquire() as conn:
        s2 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s2, hubs + rares)
        _attributed, window = await _attributed_and_window(conn)

    on = _counters(
        await _run(pg_pool, global_df_window=window, global_df_min_signals=50)
    )
    async with pg_pool.acquire() as conn:
        weights_on = sorted(float(e["weight"]) for e in await _edges(conn))

    assert off["edges_written"] > 0
    # The lever was live and caught at least the three hubs. (It also clips the
    # rare trio here: sharing ONE probe signal with the hubs puts them at
    # df 3/103 = 0.029, just past the knee — which is the rule working, not a
    # leak, and is exactly why the isolation test above uses separate probes.)
    assert on["global_specificity_inert"] is False
    assert on["global_specificity_downweighted"] >= 3
    assert on["edges_written"] <= off["edges_written"]
    for w_on, w_off in zip(weights_on, weights_off):
        assert w_on <= w_off + 1e-9
    assert s is not None and s2 is not None


# ---------------------------------------------------------------------------
# v3.2.0 lever L3 — omnibus damper + same-url dedup (live)
# ---------------------------------------------------------------------------


async def test_omnibus_signal_cap_fires_and_is_counted(pg_pool, clean_slate):
    """Measured over the 11,195 live 3.x edges: 943 source signals, a MEDIAN
    of 15 distinct questions each, p90 = p99 = max = 20 — so the old 20-edge
    cap only ever engaged in the top decile while half of every run was
    already spraying. The cap is now a real backstop, and it reports BOTH
    facts: the edges it cost, and the number of SIGNALS it engaged on (the
    omnibus population, which no edge count reveals)."""
    async with pg_pool.acquire() as conn:
        # A desk below the specificity floor so every question matches on raw
        # counts — the cap is what does the cutting here, nothing else.
        furniture, _rare, qids = await _specificity_desk(
            conn, desk="kw3_omnibus", n_questions=3
        )
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        omnibus = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, omnibus, furniture)

    result = await _run(pg_pool, max_questions_per_signal=1, **_L2_OFF)
    c = _counters(result)
    assert c["omnibus_capped"] == 1
    assert c["edges_dropped_per_signal_cap"] == len(qids) - 1
    assert c["edges_written"] == 1
    assert "omnibus signal(s) capped" in result.finding.title

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
    assert len(edges) == 1
    assert edges[0]["src_id"] == omnibus


async def test_omnibus_cap_keeps_the_strongest_candidates(pg_pool, clean_slate):
    """The cap TRIMS, it does not sample: what survives is the highest-weight
    candidate, so damping an omnibus signal never costs it its best edge."""
    async with pg_pool.acquire() as conn:
        furniture, _rare, qids = await _specificity_desk(
            conn, desk="kw3_omnibus_rank", n_questions=3
        )
        # Age one question so its weight is strictly lower than its siblings'
        # while STAYING above threshold — a deterministic ranking with three
        # live candidates, so the cap has something to cut. (400 days would
        # drop it below threshold entirely and there would be no third
        # candidate to trim.)
        await conn.execute(
            "UPDATE hypotheses SET produced_at = now() - interval '100 days' "
            " WHERE id = $1",
            qids[-1],
        )
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        omnibus = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, omnibus, furniture)

    c = _counters(await _run(pg_pool, max_questions_per_signal=2, **_L2_OFF))
    assert c["omnibus_capped"] == 1
    assert c["edges_written"] == 2
    async with pg_pool.acquire() as conn:
        kept = {e["dst_id"] for e in await _edges(conn)}
    # The aged (weakest) question is the one dropped.
    assert qids[-1] not in kept
    assert omnibus is not None


async def test_no_omnibus_counter_when_the_cap_does_not_engage(
    pg_pool, clean_slate
):
    """A counter that fires on ordinary traffic is noise. With the fan-out
    under the cap both L3 counters stay at zero and the title stays clean."""
    async with pg_pool.acquire() as conn:
        furniture, _rare, qids = await _specificity_desk(
            conn, desk="kw3_omnibus_quiet", n_questions=3
        )
    await _run(pg_pool, **_L2_OFF)  # seed
    async with pg_pool.acquire() as conn:
        s = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s, furniture)

    result = await _run(pg_pool, **_L2_OFF)
    c = _counters(result)
    assert len(qids) <= cw.MAX_QUESTIONS_PER_SIGNAL
    assert c["edges_written"] == len(qids)
    assert c["omnibus_capped"] == 0
    assert c["edges_dropped_per_signal_cap"] == 0
    assert "omnibus" not in result.finding.title


async def test_same_url_duplicates_are_collapsed_and_counted(
    pg_pool, clean_slate
):
    """One article ingested twice (two ``signals`` rows, one canonical_url) is
    ONE document's worth of evidence, not two. Measured: 88 droppable rows in
    a 500-signal batch, worst url 40x. The NEWEST row survives, the drop is
    counted AND titled, and the cursor still advances past the whole batch."""
    url = f"https://example.invalid/kw3/{uuid4().hex}"
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn, desk="kw3_url_desk")
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        older = await _insert_signal(conn, geo=("IR",), canonical_url=url)
        await _link_all(conn, older, ents)
        newer = await _insert_signal(conn, geo=("IR",), canonical_url=url)
        await _link_all(conn, newer, ents)

    result = await _run(pg_pool, **_L2_OFF)
    c = _counters(result)
    assert c["signals_url_deduped"] == 1
    assert c["examined_signals"] == 1
    assert "duplicate url(s) dropped" in result.finding.title

    async with pg_pool.acquire() as conn:
        edges = await _edges(conn)
        # The cursor advanced past BOTH rows — a dropped duplicate is passed
        # over, never stranded for the next run to re-fetch forever.
        assert await _signals_after_cursor(conn) == 0
    assert {(e["src_id"], e["dst_id"]) for e in edges} == {(newer, qid)}
    assert older is not None


async def test_distinct_urls_and_missing_urls_are_never_deduped(
    pg_pool, clean_slate
):
    """The dedup key is a SHARED canonical url. Two different articles, and
    rows with no url at all, are separate documents."""
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn, desk="kw3_url_distinct")
    await _run(pg_pool, **_L2_OFF)  # seed

    async with pg_pool.acquire() as conn:
        a = await _insert_signal(
            conn, geo=("IR",), canonical_url="https://example.invalid/kw3/a"
        )
        await _link_all(conn, a, ents)
        b = await _insert_signal(
            conn, geo=("IR",), canonical_url="https://example.invalid/kw3/b"
        )
        await _link_all(conn, b, ents)
        c_null = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, c_null, ents)

    c = _counters(await _run(pg_pool, **_L2_OFF))
    assert c["signals_url_deduped"] == 0
    assert c["examined_signals"] == 3
    async with pg_pool.acquire() as conn:
        assert {e["src_id"] for e in await _edges(conn)} == {a, b, c_null}
    assert qid is not None


# ---------------------------------------------------------------------------
# W-B1/W-B2 — THE BEARING PIPELINE, end to end against the substrate
#
# The gate's own logic is unit-tested in test_bearing_gate.py. What can only
# be proven HERE is the part that touches rows: that a gate NO writes no
# bearing_edges row AND raises no review_flag; that a stamp really lands in
# bearing_edges.data (migration 0116); that an outage still writes; and that
# a gate-OFF run is byte-identical to what 3.2.0 wrote.
# ---------------------------------------------------------------------------


class _FakeGateLLM:
    """Scripted chat_complete for the gate / confirm legs. ``replies`` may
    hold strings (returned in order) or Exceptions (raised)."""

    def __init__(self, *replies: Any, default: Any = "YES") -> None:
        self.replies = list(replies)
        self.default = default
        self.prompts: list[str] = []

    async def chat_complete(self, messages, **kwargs):
        self.prompts.append(messages[0]["content"])
        reply = self.replies.pop(0) if self.replies else self.default
        if isinstance(reply, Exception):
            raise reply

        class _R:
            content = reply

        return _R()


def _gate_on(**over: Any) -> dict[str, Any]:
    """Run options with the pipeline ON (plus the L2 damper off, so these
    scenes match on entity+geo exactly like their 3.2.0 siblings)."""
    return {**_L2_OFF, "bearing_gate": "on", **over}


def _gate_extras(gate: Any = None, confirm: Any = None) -> dict[str, Any]:
    extras: dict[str, Any] = {}
    if gate is not None:
        extras[bg.SLM_DEPS_EXTRA_KEY] = gate
    if confirm is not None:
        extras[bg.CONFIRM_LLM_DEPS_EXTRA_KEY] = confirm
    return extras


async def _edges_with_data(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT src_id, dst_id, weight, planes, matcher_version, data "
        "  FROM bearing_edges ORDER BY created_at, id"
    )


def _edge_data(row: Any) -> dict[str, Any]:
    raw = row["data"]
    return json.loads(raw) if isinstance(raw, str) else dict(raw or {})


async def _gate_scene(pg_pool, desk: str) -> tuple[UUID, UUID]:
    """A seeded matcher plus ONE new signal the deterministic matcher would
    edge (2 shared entities + desk geo = 0.48). Returns (question, signal)."""
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn, desk=desk)
    await _run(pg_pool, **_L2_OFF)  # seed
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, sid, ents)
    return qid, sid


async def test_gate_off_is_byte_identical_to_the_pre_gate_matcher(
    pg_pool, clean_slate
):
    """The X-1 contract, proven at the STORAGE layer: a run with no bearing
    option writes the same row 3.2.0 wrote — an empty ``data``, which is the
    0116 column default — and never touches the model."""
    qid, sid = await _gate_scene(pg_pool, "kw3_gate_off")
    llm = _FakeGateLLM("NO")  # would refuse everything IF it were consulted
    c = _counters(await _run(pg_pool, extras=_gate_extras(llm), **_L2_OFF))

    assert llm.prompts == []                     # never consulted
    assert c["edges_written"] == 1
    assert c["bearing_gate_mode"] == "off"
    async with pg_pool.acquire() as conn:
        edges = await _edges_with_data(conn)
    assert len(edges) == 1
    assert (edges[0]["src_id"], edges[0]["dst_id"]) == (sid, qid)
    assert _edge_data(edges[0]) == {}


async def test_gate_yes_writes_the_edge_with_its_stamp(pg_pool, clean_slate):
    qid, sid = await _gate_scene(pg_pool, "kw3_gate_yes")
    result = await _run(
        pg_pool, extras=_gate_extras(_FakeGateLLM("YES")), **_gate_on()
    )
    c = _counters(result)
    assert c["edges_written"] == 1
    assert c["bearing_gate_yes"] == 1 and c["bearing_gated_out"] == 0
    assert c["bearing_gate_mode"] == "on"
    assert c["bearing_gate_ref"] == bg.DEFAULT_BEARING_GATE_REF
    # The gate's tallies ride the TITLE, not only the counters.
    assert "gate 1 yes / 0 refused" in result.finding.title

    async with pg_pool.acquire() as conn:
        edges = await _edges_with_data(conn)
    assert len(edges) == 1
    data = _edge_data(edges[0])
    assert data["bearing_gate"] == "yes"
    assert data["bearing_gate_ref"] == bg.DEFAULT_BEARING_GATE_REF
    assert data["bearing_gate_prompt"] == bg.GATE_PROMPT_VERSION
    assert edges[0]["src_id"] == sid and edges[0]["dst_id"] == qid


async def test_gate_no_writes_no_row_and_raises_no_review_flag(
    pg_pool, clean_slate
):
    """The whole point of the leg, and the reason ``matched_questions`` is
    rebuilt from the SURVIVORS: a refused pair must not flag a downstream
    product for re-review either."""
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn, desk="kw3_gate_no")
        consumer = await _insert_consumer(conn)
        await _consume(conn, consumer, qid)
    await _run(pg_pool, **_L2_OFF)  # seed
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, sid, ents)

    c = _counters(
        await _run(pg_pool, extras=_gate_extras(_FakeGateLLM("NO")), **_gate_on())
    )
    assert c["edges_written"] == 0
    assert c["bearing_gated_out"] == 1
    # The DETERMINISTIC matcher still counts what IT produced — the two
    # together are the gate's measured effect on this run.
    assert c["matches_entity"] == 1
    assert c["flags_written"] == 0

    async with pg_pool.acquire() as conn:
        assert await _edges_with_data(conn) == []
        assert await _flags(conn) == []
        # The cursor still advanced: the signal WAS fully processed.
        assert await _signals_after_cursor(conn) == 0
    assert sid is not None


@pytest.mark.parametrize(
    "reply",
    [RuntimeError("connection refused"), "I am not sure about this one."],
)
async def test_an_8b_outage_never_silences_the_matcher(
    pg_pool, clean_slate, reply
):
    """STAMP-AND-WRITE. A gate that failed closed would turn one host outage
    into a silent hole in the bearing plane."""
    qid, sid = await _gate_scene(pg_pool, f"kw3_gate_out_{uuid4().hex[:6]}")
    c = _counters(
        await _run(pg_pool, extras=_gate_extras(_FakeGateLLM(reply)), **_gate_on())
    )
    assert c["edges_written"] == 1
    assert c["bearing_gate_errors"] == 1
    assert c["bearing_gated_out"] == 0  # an outage is NOT a refusal

    async with pg_pool.acquire() as conn:
        edges = await _edges_with_data(conn)
    assert _edge_data(edges[0])["bearing_gate"] == "unavailable"
    assert (edges[0]["src_id"], edges[0]["dst_id"]) == (sid, qid)


async def test_over_the_gate_budget_the_edge_is_stamped_deferred_not_dropped(
    pg_pool, clean_slate
):
    async with pg_pool.acquire() as conn:
        qid, ents, _ = await _matchable_question(conn, desk="kw3_gate_cap")
    await _run(pg_pool, **_L2_OFF)  # seed
    async with pg_pool.acquire() as conn:
        s1 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s1, ents)
        s2 = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, s2, ents)

    llm = _FakeGateLLM(default="YES")
    c = _counters(
        await _run(
            pg_pool, extras=_gate_extras(llm), **_gate_on(bearing_gate_cap=1)
        )
    )
    assert c["edges_written"] == 2       # the budget is OURS, not the edge's
    assert c["bearing_gate_calls"] == 1
    assert c["bearing_gate_deferred"] == 1

    async with pg_pool.acquire() as conn:
        stamps = {
            _edge_data(e)["bearing_gate"] for e in await _edges_with_data(conn)
        }
    assert stamps == {"yes", "deferred"}
    assert (qid, s1, s2) is not None


async def test_the_confirm_leg_stamps_a_verdict_and_a_reason(
    pg_pool, clean_slate
):
    qid, sid = await _gate_scene(pg_pool, "kw3_confirm")
    confirm = _FakeGateLLM(
        '[{"id": "e0", "bears": "no", "reason": "different dispute entirely"}]'
    )
    c = _counters(
        await _run(
            pg_pool,
            extras=_gate_extras(_FakeGateLLM("YES"), confirm),
            **_gate_on(),
        )
    )
    # A confirm 'no' NEVER retracts the edge — the gate already wrote it.
    assert c["edges_written"] == 1
    assert c["bearing_confirm_no"] == 1
    assert c["bearing_confirm_calls"] == 1

    async with pg_pool.acquire() as conn:
        data = _edge_data((await _edges_with_data(conn))[0])
    assert data["bearing_gate"] == "yes"
    assert data["bearing_confirm"] == "no"
    assert data["bearing_confirm_reason"] == "different dispute entirely"
    assert data["bearing_confirm_prompt"] == bg.CONFIRM_PROMPT_VERSION
    assert (qid, sid) is not None


async def test_an_unwired_confirm_leg_stamps_only_the_gate(pg_pool, clean_slate):
    """The SHIPPED state: the claim_watch descriptor declares no
    ``method.llm.primary``, so the deps builder wires no confirm client. The
    leg did not run — which must not read as an outage on every edge."""
    qid, sid = await _gate_scene(pg_pool, "kw3_confirm_unwired")
    c = _counters(
        await _run(pg_pool, extras=_gate_extras(_FakeGateLLM("YES")), **_gate_on())
    )
    assert c["bearing_confirm_unavailable"] == 0
    assert c["bearing_confirm_calls"] == 0
    async with pg_pool.acquire() as conn:
        data = _edge_data((await _edges_with_data(conn))[0])
    assert data["bearing_gate"] == "yes"
    assert "bearing_confirm" not in data
    assert (qid, sid) is not None


async def test_every_bearing_counter_rides_every_receipt(pg_pool, clean_slate):
    """Including the SEED run and the nothing-to-do run — a receipt that
    silently omits the gate counters cannot be read as "the gate wrote
    nothing" without knowing which build produced it."""
    expected = set(bg.bearing_counter_defaults())
    seed = _counters(await _run(pg_pool, **_L2_OFF))
    assert seed["seeded"] is True
    assert expected <= set(seed)
    quiet = _counters(await _run(pg_pool, **_L2_OFF))
    assert quiet["examined_signals"] == 0
    assert expected <= set(quiet)


async def test_the_gate_never_widens_what_the_matcher_considers(
    pg_pool, clean_slate
):
    """A sub-threshold pair is refused by the FUSION MODEL and never becomes a
    candidate, so the gate is never asked about it — the matcher stays fully
    deterministic in what it CONSIDERS; the gate can only ever subtract."""
    async with pg_pool.acquire() as conn:
        # One shared entity + geo = 0.30 < 0.45: below threshold.
        qid, ents, _ = await _matchable_question(
            conn, desk="kw3_gate_subthreshold", n_entities=1
        )
    await _run(pg_pool, **_L2_OFF)  # seed
    async with pg_pool.acquire() as conn:
        sid = await _insert_signal(conn, geo=("IR",))
        await _link_all(conn, sid, ents)

    llm = _FakeGateLLM(default="YES")  # would ADMIT it, if it were asked
    c = _counters(await _run(pg_pool, extras=_gate_extras(llm), **_gate_on()))
    assert llm.prompts == []
    assert c["edges_written"] == 0
    assert c["bearing_gate_calls"] == 0
    assert (qid, sid) is not None

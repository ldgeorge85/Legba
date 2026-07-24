# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mining-audit fixes (2026-07-06 live audit) — M20 + M21.

M20 (graph_mining) — the ``new_hostile_edge`` / broker / proxy-chain shortlist
must not amplify NER errors into headline geopolitical signal: a mis-signed
NEUTRAL rel_type relabeled hostile, a fragment / vague endpoint, or a
protest-at-location "state hostile to person" attribution collapse. Real
interstate hostile edges MUST survive.

M21 (thematic_proposal) — an ABSENCE/negation-framed "all-clear" frame is not a
situation to propose; a situation's slug must be STABLE across re-framings; the
proposal list must be deduplicated.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from legba.data.analysts.deterministic_handlers import graph_mining as gm
from legba.data.analysts.deterministic_handlers import thematic_proposal as tp


# ---------------------------------------------------------------------------
# M20 — helper predicates
# ---------------------------------------------------------------------------


def test_hostile_rel_gate_accepts_only_real_hostility_types():
    # Stored lowercase-spaced forms + the CamelCase vocabulary forms both fold.
    for good in ("hostile to", "HostileTo", "targets", "supplies weapons to",
                 "SuppliesWeaponsTo", "in active conflict with"):
        assert gm._is_hostile_rel(good), good
    # A neutral rel_type that a polarity=-1 nexus mis-carries is NOT hostile.
    for bad in ("conducted via", "involved in", "operates in", "co occurs with",
                "member of", "", None):
        assert not gm._is_hostile_rel(bad), bad


def test_explicit_targeting_rel_gate():
    """F2 — EXPLICIT-TARGETING rel_types (a state acting on a NAMED person) are
    exempt from the state->person attribution guard; the generic co-occurrence
    label is not."""
    for good in ("Targets", "targets", "SuppliesWeaponsTo",
                 "supplies weapons to"):
        assert gm._is_explicit_targeting_rel(good), good
    for generic in ("HostileTo", "hostile to", "in active conflict with",
                    "conducted via", "", None):
        assert not gm._is_explicit_targeting_rel(generic), generic


def test_canonical_actor_keeps_mistyped_entity_endpoints():
    """F1 (2026-07-06 review): the class-vet must NOT drop a REAL actor merely
    because the live store mis-typed it as the generic ``entity`` class. Real
    actors (Hamas / IRGC / ISIS / Wagner / Lavrov) are ALL bare ``{entity}`` in
    the live store; dropping them here silently discarded live hostile edges. The
    genuine-fragment drop is is_junk_entity (applied separately upstream)."""
    # Canon types a country / org / location → vetted regardless of ep classes.
    assert gm._is_canonical_actor("Australia", None)
    assert gm._is_canonical_actor("IRGC Navy", {"organization"})
    # A PROFILED-only-``entity`` endpoint is treated the SAME as absent → KEPT
    # (the class-vet no longer drops it; is_junk_entity handles true fragments).
    assert gm._is_canonical_actor("Hamas", {"entity"})
    assert gm._is_canonical_actor("IRGC", {"entity"})
    assert gm._is_canonical_actor("Lavrov", {"entity"})
    assert gm._is_canonical_actor("Parl", {"entity"})   # kept HERE; junk-dropped upstream
    # A surface that entity_profiles types 'person' IS a real actor (kept here;
    # the state->person attribution guard handles it separately).
    assert gm._is_canonical_actor("Isaac Herzog", {"person"})
    # Absent from entity_profiles → benefit of the doubt (not dropped on vetting).
    assert gm._is_canonical_actor("Some New Militia", None)


def test_junk_gate_catches_truncated_institution_fragments():
    """F1: is_junk_entity — the SINGLE fragment authority — must catch the
    truncated institution/agency clippings ("Parl", "Fed"), while the REAL actors
    that share the generic ``{entity}`` class survive it."""
    from legba.data._entity_canon import is_junk_entity
    for junk in ("Parl", "Fed", "West", "Leader"):
        assert is_junk_entity(junk), junk
    for actor in ("Hamas", "IRGC", "ISIS", "Wagner", "Lavrov", "Federal Reserve"):
        assert not is_junk_entity(actor), actor


def test_person_only_and_state_surface_predicates():
    assert gm._is_person_only({"person"})
    # Ambiguous surface with a non-person actor class is NOT person-only.
    assert not gm._is_person_only({"entity", "person"})
    assert not gm._is_person_only({"country"})
    assert not gm._is_person_only(None)
    assert gm._is_state_surface("Australia", None)      # canon country
    assert gm._is_state_surface("Ankara", None)         # canon location
    assert gm._is_state_surface("Kiev", {"location"})   # ep location
    assert not gm._is_state_surface("Isaac Herzog", {"person"})


# ---------------------------------------------------------------------------
# M20 — the hostile-edge shortlist end-to-end (_build_interesting is pure)
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 7, 6, tzinfo=timezone.utc)


def _edge(subject, obj, rel="hostile to", *, subj_cls, obj_cls, conf=1.0,
          days_old=1):
    return {
        "subject": subject,
        "object": obj,
        "polarity": -1,
        "rel_type": rel,
        "valid_from": _NOW - timedelta(days=days_old),
        "confidence": conf,
        "subject_classes": subj_cls,
        "object_classes": obj_cls,
    }


def _labels(recent_hostile):
    items = gm._build_interesting(
        communities=[], centrality={}, proxy_chains=[],
        recent_hostile=recent_hostile, now=_NOW,
    )
    return [it["label"] for it in items if it["kind"] == "new_hostile_edge"]


def test_real_interstate_hostile_edges_survive():
    """CONSERVATIVE — Russia/Ukraine, Israel/Iran, Pakistan/Afghanistan, and a
    state->non-state-org hostile tie MUST all survive."""
    edges = [
        _edge("Russia", "Ukraine", subj_cls=["country"], obj_cls=["country"]),
        _edge("Israel", "Iran", subj_cls=["country"], obj_cls=["country"]),
        _edge("Pakistan", "Afghanistan",
              subj_cls=["country"], obj_cls=["country"]),
        # US -[hostile to]-> Hezbollah: object is dual-typed (entity+person) so
        # it is NOT person-only — the attribution guard must NOT drop it.
        _edge("United States", "Hezbollah",
              subj_cls=["country"], obj_cls=["entity", "person"]),
    ]
    labels = _labels(edges)
    assert "Russia -[hostile to]-> Ukraine" in labels
    assert "Israel -[hostile to]-> Iran" in labels
    assert "Pakistan -[hostile to]-> Afghanistan" in labels
    assert "United States -[hostile to]-> Hezbollah" in labels


def test_neutral_rel_type_relabeled_hostile_is_dropped():
    """A polarity=-1 nexus whose rel_type is NEUTRAL ("conducted via" /
    "involved in" / "operates in") must NOT surface as a hostile tie."""
    edges = [
        _edge("Iran", "Mohammad Baqer", rel="conducted via",
              subj_cls=["country"], obj_cls=["person"]),
        _edge("Trump", "Italy", rel="involved in",
              subj_cls=["person"], obj_cls=["country"]),
        _edge("Iran", "Strait of Hormuz", rel="operates in",
              subj_cls=["country"], obj_cls=["location"]),
    ]
    assert _labels(edges) == []


def test_fragment_and_vague_endpoints_dropped():
    """Vague tokens ("West", "Leader") + truncated institution fragments
    ("Parl", "Fed") are junk (is_junk_entity, F1) — none may surface as a hostile
    edge even though the live store profiles the fragments as bare ``{entity}``."""
    edges = [
        _edge("West", "Israel", subj_cls=["person"], obj_cls=["country"]),
        _edge("Leader", "Trump", subj_cls=["person"], obj_cls=["person"]),
        _edge("Parl", "Trump", subj_cls=["entity"], obj_cls=["person"]),
        _edge("Fed", "Iran", subj_cls=["entity"], obj_cls=["country"]),
    ]
    assert _labels(edges) == []


def test_f1_mistyped_entity_actors_survive():
    """F1 (2026-07-06 review) — the class-vet BLOCKER: real actors are routinely
    mis-typed as the generic ``{entity}`` class in the live store (Hamas / IRGC /
    ISIS / Wagner / Lavrov). The OLD class-vet dropped every one, silently
    discarding real open hostile edges (the live ``Lavrov -[hostile to]-> United
    States`` @ 0.78). They MUST now survive; the genuine fragments still drop."""
    edges = [
        # The exact live edge the old class-vet dropped.
        _edge("Lavrov", "United States", conf=0.78,
              subj_cls=["entity"], obj_cls=["country"]),
        _edge("Hamas", "Israel", subj_cls=["entity"], obj_cls=["country"]),
        _edge("IRGC", "United States", subj_cls=["entity"], obj_cls=["country"]),
        _edge("Wagner", "Ukraine", subj_cls=["entity"], obj_cls=["country"]),
        _edge("ISIS", "Iraq", subj_cls=["entity"], obj_cls=["country"]),
        # Interstate control edges still survive.
        _edge("Russia", "Ukraine", subj_cls=["country"], obj_cls=["country"]),
        _edge("Israel", "Iran", subj_cls=["country"], obj_cls=["country"]),
        # A genuine fragment mis-typed {entity} still DROPS (via is_junk_entity).
        _edge("Parl", "United States", subj_cls=["entity"], obj_cls=["country"]),
    ]
    labels = _labels(edges)
    assert "Lavrov -[hostile to]-> United States" in labels
    assert "Hamas -[hostile to]-> Israel" in labels
    assert "IRGC -[hostile to]-> United States" in labels
    assert "Wagner -[hostile to]-> Ukraine" in labels
    assert "ISIS -[hostile to]-> Iraq" in labels
    assert "Russia -[hostile to]-> Ukraine" in labels
    assert "Israel -[hostile to]-> Iran" in labels
    assert not any(lbl.startswith("Parl ") for lbl in labels)


def test_f2_explicit_state_targets_person_survives():
    """F2 (2026-07-06 review) — the subject-attribution guard over-reached: it
    dropped ANY state->person hostile edge, discarding real state->NAMED-person
    TARGETING (assassination / decapitation / arming I&W). The guard is now
    scoped to the GENERIC co-occurrence label only, so an explicit-targeting
    rel_type ("Targets" / "SuppliesWeaponsTo") state->person edge SURVIVES while
    the "protest-at-location" collapse ("Australia hostile to Isaac Herzog") is
    still dropped."""
    edges = [
        # Explicit targeting of a named person — a genuine I&W signal, KEPT.
        _edge("Israel", "Ismail Haniyeh", rel="Targets",
              subj_cls=["country"], obj_cls=["person"]),
        _edge("Iran", "Some Commander", rel="SuppliesWeaponsTo",
              subj_cls=["country"], obj_cls=["person"]),
        # The generic co-occurrence collapse is STILL dropped.
        _edge("Australia", "Isaac Herzog", rel="hostile to",
              subj_cls=["country"], obj_cls=["person"]),
        # Interstate control edge survives.
        _edge("Russia", "Ukraine", rel="hostile to",
              subj_cls=["country"], obj_cls=["country"]),
    ]
    labels = _labels(edges)
    assert "Israel -[Targets]-> Ismail Haniyeh" in labels
    assert "Iran -[SuppliesWeaponsTo]-> Some Commander" in labels
    assert "Australia -[hostile to]-> Isaac Herzog" not in labels
    assert "Russia -[hostile to]-> Ukraine" in labels


def test_subject_attribution_state_hostile_to_person_dropped():
    """The protest-at-location collapse: "Australia -[hostile to]-> Isaac
    Herzog" (a state 'hostile to' a bare foreign person) must be dropped, while
    a person->person and state->state edge in the same batch survive."""
    edges = [
        _edge("Australia", "Isaac Herzog",
              subj_cls=["country"], obj_cls=["person"]),
        # person -> country survives (a leader's declared hostility).
        _edge("Netanyahu", "Lebanon",
              subj_cls=["person"], obj_cls=["country"]),
        # state -> state survives.
        _edge("Kuwait", "Iran", subj_cls=["country"], obj_cls=["country"]),
    ]
    labels = _labels(edges)
    assert "Australia -[hostile to]-> Isaac Herzog" not in labels
    assert "Netanyahu -[hostile to]-> Lebanon" in labels
    assert "Kuwait -[hostile to]-> Iran" in labels


def test_self_loop_edge_dropped():
    edges = [_edge("Iran", "Iranian", subj_cls=["country"], obj_cls=["country"])]
    assert _labels(edges) == []


def test_confidence_folds_into_edge_quality_score():
    """M20 (d): a thinly-corroborated hostile edge scores below a solid one of
    the same recency."""
    strong = _edge("Russia", "Ukraine",
                   subj_cls=["country"], obj_cls=["country"], conf=1.0)
    weak = _edge("Israel", "Iran",
                 subj_cls=["country"], obj_cls=["country"], conf=0.3)
    items = gm._build_interesting(
        communities=[], centrality={}, proxy_chains=[],
        recent_hostile=[strong, weak], now=_NOW,
    )
    by_label = {it["label"]: it["score"] for it in items}
    assert by_label["Russia -[hostile to]-> Ukraine"] > by_label[
        "Israel -[hostile to]-> Iran"]


def test_broker_and_proxy_shortlist_drop_junk_nodes():
    """A fragment node must not surface as a broker or in a proxy chain."""
    centrality = {
        "West": {"degree": 9.0, "betweenness": 0.9},
        "Russia": {"degree": 8.0, "betweenness": 0.8},
    }
    proxy_chains = [
        {"actor": "Leader", "target": "Iran", "via": ["Hamas"],
         "length": 2, "polarity_sign": -1, "score": 0.5},
        {"actor": "United States", "target": "Iran", "via": ["Hezbollah"],
         "length": 2, "polarity_sign": -1, "score": 0.5},
    ]
    items = gm._build_interesting(
        communities=[["West", "Russia"]], centrality=centrality,
        proxy_chains=proxy_chains, recent_hostile=[], now=_NOW,
    )
    broker_labels = [it["label"] for it in items if it["kind"] == "broker"]
    proxy_labels = [it["label"] for it in items if it["kind"] == "proxy_chain"]
    assert "West" not in broker_labels and "Russia" in broker_labels
    assert not any("Leader" in p for p in proxy_labels)
    assert any("United States" in p for p in proxy_labels)


# ---------------------------------------------------------------------------
# M21 — thematic_proposal absence-exclusion + stable slug + dedup
# ---------------------------------------------------------------------------


def test_absence_framing_detected():
    for name in (
        "United States – No discernible instability vector",
        "Iran – No observable WMD activity; status quo",
        "No coordinated narrative detected",
        "France – deployment rotation shows no standing posture shift",
        "Italy – negligible coercive pressure – neither target nor wielder",
        "Situation shows absence of escalation",
    ):
        assert tp.is_absence_framed(name), name
    # Substantive frames are NOT flagged, and a real referent that merely
    # CONTAINS the letters ("Norway", "Kosovo") is never spuriously matched.
    # F3 (2026-07-06 review): a HYPHENATED "no-…" compound is a SUBSTANTIVE
    # posture name, not absence framing — it must NOT be excluded.
    for name in (
        "Turkey – State Repression Dominates Internal Stability",
        "China nuclear-capable delivery build-up",
        "Norway sovereign-fund diplomatic leverage",
        "Kosovo border tension escalates",
        "Russia – Civilian Small Nuclear Power Plant Development",
        "China – no-first-use policy under review",
        "no-first-use policy",
        "Poland – no-fly zone proposal over Ukraine",
        "no-fly zone",
    ):
        assert not tp.is_absence_framed(name), name


def test_slug_is_stable_across_reframings():
    """One situation (one signature) → one slug, regardless of the volatile
    top keyword its current name happens to lead with."""
    sig = "sig:country_g20_fr"
    a = tp.stable_slug(sig, "France – instability vector observed")
    b = tp.stable_slug(sig, "France – observable leadership strain")
    c = tp.stable_slug(sig, "France – deployment rotation")
    assert a == b == c == "situation_country_g20_fr"
    # A different signature yields a different slug.
    assert tp.stable_slug("sig:country_g20_de", "Germany") != a
    # Empty signature falls back to a stable name hash (still deterministic).
    h1 = tp.stable_slug("", "Sahel insurgency spreads")
    h2 = tp.stable_slug("", "Sahel insurgency spreads")
    assert h1 == h2 and h1.startswith("situation_")


def test_build_proposals_excludes_absence_frames():
    rows = [
        {"situation_signature": "sig:us", "intensity_score": 49.4,
         "name": "United States – No discernible instability vector"},
        {"situation_signature": "sig:tr", "intensity_score": 20.0,
         "name": "Turkey – State Repression Dominates internal stability"},
    ]
    props = tp._build_proposals(rows, covered_text="", floor=1.0)
    sigs = {p["situation_signature"] for p in props}
    # The absence-framed (and highest-intensity) frame is excluded; the
    # substantive one survives.
    assert "sig:us" not in sigs
    assert "sig:tr" in sigs


def test_build_proposals_dedups_on_stable_slug():
    """Two rows for the SAME situation (same signature, re-framed) collapse to a
    single proposal on the stable slug."""
    rows = [
        {"situation_signature": "sig:country_g20_fr", "intensity_score": 37.6,
         "name": "France – instability vector observed"},
        {"situation_signature": "sig:country_g20_fr", "intensity_score": 30.0,
         "name": "France – observable leadership strain"},
    ]
    props = tp._build_proposals(rows, covered_text="", floor=1.0)
    assert len(props) == 1
    assert props[0]["suggested_target_id"] == "situation_country_g20_fr"
    # The most-intense re-framing wins (sorted desc before dedup).
    assert props[0]["intensity_score"] == 37.6

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C4 — the pure fact confidence-decay model (``legba.data.facts.decay``).

Curve math per class, sighting reset, reaction points + the revoke threshold,
classification (predicate normalization + source-type multiplier), operator
config-file overlay, and the consumption-flag parse. No DB, no I/O (the
config-overlay tests write a tmp JSON file only).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from legba.data.facts.decay import (
    DECAY_STATES,
    DEFAULT_DECAY_CLASSES,
    DEFAULT_PREDICATE_CLASSES,
    FACT_DECAY_CONFIG_ENV,
    FACT_DECAY_WEIGHTING_ENV,
    REACTION_POINT_AGING,
    REACTION_POINT_FRESH,
    REVOKE_THRESHOLD,
    DecayClass,
    DecayConfig,
    classify_fact,
    decay_state_for,
    decayed_confidence,
    default_decay_config,
    effective_lifetime_days,
    fact_decay_weighting_enabled,
    load_decay_config,
    retention_factor,
)

_NOW = datetime(2026, 7, 24, 12, 0, 0, tzinfo=timezone.utc)


def _days_ago(days: float) -> datetime:
    return _NOW - timedelta(days=days)


# ---------------------------------------------------------------------------
# Curve math
# ---------------------------------------------------------------------------


def test_retention_full_at_sighting_zero_at_lifetime():
    assert retention_factor(0.0, lifetime_days=100, decay_speed=0.3) == 1.0
    assert retention_factor(100.0, lifetime_days=100, decay_speed=0.3) == 0.0
    assert retention_factor(500.0, lifetime_days=100, decay_speed=0.3) == 0.0
    # Clock skew (future sighting) clamps to full retention, never > 1.
    assert retention_factor(-5.0, lifetime_days=100, decay_speed=0.3) == 1.0


def test_retention_monotonically_decreases_with_elapsed():
    prev = 1.0
    for t in range(0, 101, 5):
        cur = retention_factor(float(t), lifetime_days=100, decay_speed=0.3)
        assert cur <= prev
        prev = cur


def test_small_decay_speed_is_flatter_early():
    """MISP delta semantics: a SMALLER decay_speed holds retention high for
    most of the lifetime (structural facts), a larger one decays sooner."""
    mid_structural = retention_factor(50.0, lifetime_days=100, decay_speed=0.2)
    mid_event = retention_factor(50.0, lifetime_days=100, decay_speed=0.5)
    assert mid_structural > 0.9
    assert mid_event < mid_structural


@pytest.mark.parametrize("name,cls", sorted(DEFAULT_DECAY_CLASSES.items()))
def test_every_default_class_curve_is_well_formed(name, cls):
    """Each shipped class: full retention at t=0, zero at lifetime, and the
    curve stays within [0, 1] throughout."""
    assert cls.name == name
    assert retention_factor(0.0, lifetime_days=cls.lifetime_days, decay_speed=cls.decay_speed) == 1.0
    assert retention_factor(cls.lifetime_days, lifetime_days=cls.lifetime_days, decay_speed=cls.decay_speed) == 0.0
    for frac in (0.1, 0.25, 0.5, 0.75, 0.9, 0.99):
        f = retention_factor(
            cls.lifetime_days * frac,
            lifetime_days=cls.lifetime_days,
            decay_speed=cls.decay_speed,
        )
        assert 0.0 <= f <= 1.0


def test_event_class_decays_faster_than_officeholder():
    """The design invariant: officeholder facts decay slow, event facts fast.
    At the same 60d since sighting the event fact has lost far more."""
    office = decayed_confidence(
        confidence=0.9, predicate="leader of", source_type="ingestion",
        now=_NOW, last_sighting_at=_days_ago(60),
    )
    event = decayed_confidence(
        confidence=0.9, predicate="involved in conflict event",
        source_type="ingestion", now=_NOW, last_sighting_at=_days_ago(60),
    )
    assert office.decay_class == "officeholder"
    assert event.decay_class == "event"
    assert office.decayed_confidence > event.decayed_confidence
    assert event.decay_state == "revoke_candidate"  # 60d > the 45d event lifetime
    assert office.decay_state == "fresh"


# ---------------------------------------------------------------------------
# Sighting reset
# ---------------------------------------------------------------------------


def test_recent_sighting_resets_decay():
    """A corroborating re-observation (a newer last_sighting_at) restores
    retention — the sightings mechanic."""
    stale = decayed_confidence(
        confidence=0.8, predicate="hostile to", source_type="ingestion",
        now=_NOW, last_sighting_at=_days_ago(150),
    )
    resighted = decayed_confidence(
        confidence=0.8, predicate="hostile to", source_type="ingestion",
        now=_NOW, last_sighting_at=_days_ago(1),
    )
    assert resighted.decayed_confidence > stale.decayed_confidence
    assert resighted.decay_state == "fresh"
    assert stale.decay_state in ("stale", "revoke_candidate")


def test_no_sighting_at_all_is_fully_decayed_not_silently_fresh():
    r = decayed_confidence(
        confidence=0.9, predicate="leader of", source_type="ingestion",
        now=_NOW, last_sighting_at=None,
    )
    assert r.decayed_confidence == 0.0
    assert r.decay_state == "revoke_candidate"


def test_naive_datetimes_are_treated_as_utc():
    r = decayed_confidence(
        confidence=0.9, predicate="leader of", source_type="ingestion",
        now=_NOW.replace(tzinfo=None),
        last_sighting_at=_days_ago(1).replace(tzinfo=None),
    )
    assert r.decay_state == "fresh"


# ---------------------------------------------------------------------------
# Reaction points + revoke threshold
# ---------------------------------------------------------------------------


def test_reaction_points_map_retention_onto_states():
    cfg = default_decay_config()
    assert decay_state_for(retention=1.0, decayed_confidence=0.9, config=cfg) == "fresh"
    assert (
        decay_state_for(
            retention=REACTION_POINT_FRESH, decayed_confidence=0.9, config=cfg
        )
        == "fresh"
    )
    assert (
        decay_state_for(
            retention=REACTION_POINT_FRESH - 0.01, decayed_confidence=0.9, config=cfg
        )
        == "aging"
    )
    assert (
        decay_state_for(
            retention=REACTION_POINT_AGING, decayed_confidence=0.9, config=cfg
        )
        == "aging"
    )
    assert (
        decay_state_for(
            retention=REACTION_POINT_AGING - 0.01, decayed_confidence=0.9, config=cfg
        )
        == "stale"
    )


def test_revoke_threshold_is_absolute_and_wins():
    """The MISP score-cutoff semantic: decayed confidence at/below the revoke
    threshold is a revoke candidate even at high retention (a low-stored-
    confidence fact), and every state is in the closed vocabulary."""
    cfg = default_decay_config()
    assert (
        decay_state_for(
            retention=1.0, decayed_confidence=REVOKE_THRESHOLD, config=cfg
        )
        == "revoke_candidate"
    )
    assert (
        decay_state_for(
            retention=0.0, decayed_confidence=0.0, config=cfg
        )
        == "revoke_candidate"
    )
    for state in (
        decay_state_for(retention=r, decayed_confidence=d, config=cfg)
        for r in (0.0, 0.3, 0.6, 0.9, 1.0)
        for d in (0.0, 0.1, 0.5, 0.9)
    ):
        assert state in DECAY_STATES


def test_decayed_confidence_never_exceeds_stored():
    for days in (0, 10, 100, 1000):
        r = decayed_confidence(
            confidence=0.7, predicate="member of", source_type="ingestion",
            now=_NOW, last_sighting_at=_days_ago(days),
        )
        assert 0.0 <= r.decayed_confidence <= 0.7


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def test_classification_normalizes_predicate_surface_forms():
    """Seed CamelCase and ingest lowercase-spaced land on one class."""
    assert classify_fact("LeaderOf").name == "officeholder"
    assert classify_fact("leader of").name == "officeholder"
    assert classify_fact("located in").name == "structural"
    assert classify_fact("hostile to").name == "stance"
    assert classify_fact("member of").name == "affiliation"


def test_unknown_predicate_gets_default_class_never_fabricated():
    assert classify_fact("plans to close").name == "default"
    assert classify_fact(None).name == "default"
    assert classify_fact("   ").name == "default"


def test_every_default_predicate_maps_to_a_shipped_class():
    for pred, cls in DEFAULT_PREDICATE_CLASSES.items():
        assert cls in DEFAULT_DECAY_CLASSES, (pred, cls)


def test_seed_and_curated_rows_age_slower():
    cls = classify_fact("leader of")
    base = effective_lifetime_days(cls, source_type="ingestion")
    seed = effective_lifetime_days(cls, source_type="seed")
    curated = effective_lifetime_days(cls, source_type="curated")
    assert seed == base * 2.0
    assert curated == base * 2.0
    assert effective_lifetime_days(cls, source_type=None) == base


# ---------------------------------------------------------------------------
# Operator config overlay
# ---------------------------------------------------------------------------


def test_config_overlay_merges_over_defaults(tmp_path, monkeypatch):
    overlay = {
        "classes": {
            "event": {"lifetime_days": 30},
            "cyber": {"lifetime_days": 14, "decay_speed": 0.6},
        },
        "predicate_classes": {"Controls": "event"},
        "source_type_multipliers": {"seed": 3.0},
        "revoke_threshold": 0.25,
    }
    path = tmp_path / "decay.json"
    path.write_text(json.dumps(overlay))
    monkeypatch.setenv(FACT_DECAY_CONFIG_ENV, str(path))

    cfg = load_decay_config()
    # Overridden lifetime keeps the default decay_speed for that class.
    assert cfg.classes["event"].lifetime_days == 30
    assert cfg.classes["event"].decay_speed == DEFAULT_DECAY_CLASSES["event"].decay_speed
    # New operator class exists; untouched defaults survive.
    assert cfg.classes["cyber"].lifetime_days == 14
    assert cfg.classes["structural"] == DEFAULT_DECAY_CLASSES["structural"]
    # Predicate remap is casefolded; multiplier + threshold overlaid.
    assert cfg.predicate_classes["controls"] == "event"
    assert cfg.source_type_multipliers["seed"] == 3.0
    assert cfg.revoke_threshold == 0.25


def test_malformed_config_file_degrades_to_defaults(tmp_path, monkeypatch):
    path = tmp_path / "broken.json"
    path.write_text("{not json")
    monkeypatch.setenv(FACT_DECAY_CONFIG_ENV, str(path))
    assert load_decay_config() == default_decay_config()

    monkeypatch.setenv(FACT_DECAY_CONFIG_ENV, str(tmp_path / "missing.json"))
    assert load_decay_config() == default_decay_config()


def test_config_validation_rejects_bad_shapes():
    with pytest.raises(ValueError, match="default"):
        DecayConfig(classes={"structural": DEFAULT_DECAY_CLASSES["structural"]})
    with pytest.raises(ValueError, match="unknown decay class"):
        DecayConfig(predicate_classes={"leader of": "nope"})
    with pytest.raises(ValueError, match="lifetime_days"):
        DecayClass("x", lifetime_days=0.0, decay_speed=0.3)
    with pytest.raises(ValueError, match="decay_speed"):
        DecayClass("x", lifetime_days=10.0, decay_speed=0.0)


# ---------------------------------------------------------------------------
# The consumption flag (default OFF)
# ---------------------------------------------------------------------------


def test_weighting_flag_default_off_and_truthy_set(monkeypatch):
    monkeypatch.delenv(FACT_DECAY_WEIGHTING_ENV, raising=False)
    assert fact_decay_weighting_enabled() is False
    for off in ("", "0", "false", "no", "off", "junk"):
        monkeypatch.setenv(FACT_DECAY_WEIGHTING_ENV, off)
        assert fact_decay_weighting_enabled() is False
    for on in ("1", "true", "YES", "On"):
        monkeypatch.setenv(FACT_DECAY_WEIGHTING_ENV, on)
        assert fact_decay_weighting_enabled() is True

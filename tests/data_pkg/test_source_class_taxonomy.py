# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S1-T8 — source_class taxonomy: schema, catalog pass, and unit wiring.

Covers the four-part task:
  1. the ``source_class`` field on the real ``SourceScope`` schema round-trips
     WITH and WITHOUT an explicit value (defaulted to ``reporting``), and
     rejects an off-vocabulary value;
  2. every committed ``descriptors/source_*.yaml`` validates and carries the
     expected class; the three new WATCH-desk state-media descriptors are
     registration-ready (load + config binds through the RSS handler schema);
  3. the embedded no-auth catalog assigns a vocabulary class to EVERY entry
     (state_media hand-curated, the rest derived from credibility tier);
  4. the ``narrative_coordination`` unit prompt references source_class /
     state_media / framing.
"""

from __future__ import annotations

import os
import pathlib
import sys
from typing import Any

import pytest
import yaml

from legba.data.schemas.source import SourceClass, SourceDescriptor, SourceScope
from legba.data.sources.rss import RSSConfig
from legba.runtime.source_factory import _unwrap_factory_dict

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"

VOCAB = {"reporting", "analysis", "official", "state_media"}

# The committed YAML source descriptors and their honest, conservative class.
EXPECTED_YAML_CLASS: dict[str, str] = {
    "source_bbc_world.yaml": "reporting",
    "source_aljazeera_world.yaml": "reporting",
    "source_dw_world.yaml": "reporting",
    "source_acled_conflict.yaml": "analysis",
    "source_ucdp_ged.yaml": "analysis",  # S1-T9 conflict-event dataset (parallel branch)
    "source_gdelt_bigquery.yaml": "reporting",
    "source_gdelt_doc_api.yaml": "reporting",
    "source_intelmq_cisa_kev.yaml": "official",
    "source_mediacloud.yaml": "reporting",
    "source_opensanctions_bulk.yaml": "official",
    "source_opensanctions_api.yaml": "official",
    "source_reliefweb_api.yaml": "official",
    "source_telegram_monitor.yaml": "reporting",
    "source_usgs_earthquakes.yaml": "official",
    # S1-T8 new WATCH-desk state-media voices
    "source_irna_english.yaml": "state_media",
    "source_presstv_english.yaml": "state_media",
    "source_ukrinform_english.yaml": "state_media",
}

NEW_STATE_MEDIA_FILES = [
    "source_irna_english.yaml",
    "source_presstv_english.yaml",
    "source_ukrinform_english.yaml",
]


def _load_body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load_descriptor(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_load_body(name), strict=False)


# ---------------------------------------------------------------------------
# 1. Schema round-trip: with, without, and off-vocabulary.
# ---------------------------------------------------------------------------


def test_scope_defaults_source_class_to_reporting_when_absent():
    """Omitting source_class validates (backward-compatible) and defaults to the
    conservative ``reporting`` bucket."""
    scope = SourceScope()
    assert scope.source_class == "reporting"
    # Full descriptor with no scope block at all still validates + defaults.
    desc = SourceDescriptor.model_validate(
        {
            "identity": {
                "id": "source.test.noscope",
                "name": "no scope",
                "kind": "rss",
                "schema_uri": "legba/source/1.0.0",
                "version": "0" * 16,
                "owner": "t",
                "created": "2026-07-02T00:00:00Z",
            }
        },
        strict=False,
    )
    assert desc.scope.source_class == "reporting"


@pytest.mark.parametrize("cls", sorted(VOCAB))
def test_scope_accepts_every_vocabulary_class(cls: str):
    scope = SourceScope(source_class=cls)
    assert scope.source_class == cls
    # Round-trip through dict (the YAML-descriptor parse shape) preserves it.
    reparsed = SourceScope.model_validate(scope.model_dump(mode="python"))
    assert reparsed.source_class == cls


def test_scope_rejects_off_vocabulary_class():
    with pytest.raises(Exception):
        SourceScope(source_class="opinion")  # not in the Literal vocabulary


def test_source_class_literal_vocabulary_is_exactly_the_four():
    from typing import get_args

    assert set(get_args(SourceClass)) == VOCAB


def test_descriptor_round_trip_preserves_source_class():
    desc = _load_descriptor("source_irna_english.yaml")
    reparsed = SourceDescriptor.model_validate(desc.model_dump(mode="python"), strict=False)
    assert reparsed.scope.source_class == "state_media"


# ---------------------------------------------------------------------------
# 2. Every committed YAML validates + carries the expected class.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,expected", sorted(EXPECTED_YAML_CLASS.items()))
def test_yaml_descriptor_class(fname: str, expected: str):
    desc = _load_descriptor(fname)
    assert desc.scope.source_class == expected
    assert desc.scope.source_class in VOCAB


def test_every_committed_source_yaml_is_classified_in_vocab():
    """Guard against a future source_*.yaml landing without a vocab class."""
    for path in sorted(DESCRIPTORS_DIR.glob("source_*.yaml")):
        desc = _load_descriptor(path.name)
        assert desc.scope.source_class in VOCAB, path.name
        # And every committed file is pinned in the expected map above.
        assert path.name in EXPECTED_YAML_CLASS, f"unmapped source descriptor: {path.name}"


# ---------------------------------------------------------------------------
# 2b. New WATCH-desk state-media descriptors are registration-ready.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname", NEW_STATE_MEDIA_FILES)
def test_new_state_media_descriptor_is_registration_ready(fname: str):
    desc = _load_descriptor(fname)
    assert desc.identity.kind == "rss"
    assert desc.scope.source_class == "state_media"
    # Activation-ready keyless RSS: active + a cadence schedule (the model
    # validator requires a schedule for an active poll source).
    assert desc.identity.state.value == "active"
    assert desc.acquisition == "poll"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    # Config binds through the PRODUCTION unwrap + the real RSS handler schema —
    # a descriptor that could not bind would permanent-fail at activation.
    cfg = RSSConfig.model_validate(_unwrap_factory_dict(desc.config))
    assert cfg.url.startswith("https://")
    # State media is FRAMING, not a fact source — no fact_extractor stage.
    kinds = [s.kind for s in desc.pipeline.enrichment]
    assert "fact_extractor" not in kinds
    assert "geocode" in kinds


# ---------------------------------------------------------------------------
# 3. The embedded no-auth catalog classifies every entry.
# ---------------------------------------------------------------------------

# Import the catalog script the same guarded way test_source_catalog_bringup
# does (its transitive _p17_registrar import sets a process-global
# LEGBA_DATA_PG_DB at import time — snapshot/restore so it can't leak).
_SCRIPTS_DIR = REPO_ROOT / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    from bringup_register_source_catalog import (  # noqa: E402
        CATALOG,
        STATE_MEDIA_IDS,
        build_descriptor,
        catalog_source_class,
    )
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)


@pytest.mark.parametrize("entry", CATALOG, ids=lambda e: e.id)
def test_every_catalog_entry_gets_a_vocab_class(entry):
    desc = build_descriptor(entry)
    assert desc.scope.source_class in VOCAB
    assert desc.scope.source_class == catalog_source_class(entry)


def test_curated_state_media_ids_classify_as_state_media():
    catalog_ids = {e.id for e in CATALOG}
    assert STATE_MEDIA_IDS  # non-empty curated set
    assert STATE_MEDIA_IDS <= catalog_ids, "curated state_media id not in catalog"
    for entry in CATALOG:
        if entry.id in STATE_MEDIA_IDS:
            assert catalog_source_class(entry) == "state_media", entry.id


def test_state_funded_but_independent_broadcaster_is_not_state_media():
    """VOA is state-FUNDED with a statutory editorial firewall — it must NOT be
    classed state_media (the curation is about state CONTROL, not funding)."""
    voa = next(e for e in CATALOG if e.id == "source.voa.africa")
    assert voa.state_affiliation is True          # still flagged state-affiliated
    assert catalog_source_class(voa) == "reporting"


def test_tier_derivation_maps_thinktank_and_gov():
    thinktank = next(e for e in CATALOG if e.tier == "thinktank")
    assert catalog_source_class(thinktank) == "analysis"
    gov = next(e for e in CATALOG if e.tier == "gov")
    assert catalog_source_class(gov) == "official"


# ---------------------------------------------------------------------------
# 4. The narrative_coordination unit prompt references the class.
# ---------------------------------------------------------------------------


def test_narrative_unit_prompt_references_source_class():
    body = yaml.safe_load((DESCRIPTORS_DIR / "analyst_narrative_coordination.yaml").read_text())
    prompt = body["method"]["system_prompt"]
    lowered = prompt.lower()
    assert "source_class" in lowered
    assert "state_media" in lowered
    assert "framing" in lowered
    # It must instruct low-tier-for-facts treatment of state media.
    assert "fact" in lowered

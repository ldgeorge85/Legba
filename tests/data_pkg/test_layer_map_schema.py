# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 — the ``layer_map`` descriptor schema
(``legba.data.registry.layer_map_schema``, migration 0214).

Covers: the closed layer vocabulary is rejected off-list; an aperture
``declared='absent'`` with no reason is rejected; an aperture declaration
missing a layer (or repeating one) is rejected — a missing layer must never
silently pass validation, because that is exactly the "reads as agreement"
failure the aperture declaration exists to close; entries may not double-
assign a source; and the committed, curated IL map
(``descriptors/layer_map_il.yaml``) round-trips through the real model.
"""

from __future__ import annotations

import pathlib

import pytest
import yaml
from pydantic import ValidationError

from legba.data.layers._vocab import APERTURE_DECLARED_VOCAB, LAYER_VOCAB
from legba.data.registry.layer_map_schema import (
    LayerMapAperture,
    LayerMapDescriptor,
    LayerMapEntry,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"


def _full_apertures(**overrides: dict) -> list[dict]:
    rows = [
        {"layer": layer, "declared": "unmeasured", "reason": ""}
        for layer in LAYER_VOCAB
    ]
    for row in rows:
        if row["layer"] in overrides:
            row.update(overrides[row["layer"]])
    return rows


def _base_body(**kw) -> dict:
    body = {
        "identity": {
            "id": "layer_map_zz",
            "name": "ZZ layer map (test)",
            "schema_uri": "legba/layer_map/1.0.0",
            "kind": "layer_map",
            "owner": "test",
            "created": "2026-01-01T00:00:00Z",
            "state": "draft",
        },
        "country": "ZZ",
        "map_version": "layer_map_zz.v1",
        "entries": [],
        "apertures": _full_apertures(),
    }
    body.update(kw)
    return body


# ---------------------------------------------------------------------------
# 1. The layer vocabulary is closed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layer", sorted(LAYER_VOCAB))
def test_entry_accepts_every_vocab_layer(layer: str):
    entry = LayerMapEntry(source_id="source.x", layer=layer, reason="y")
    assert entry.layer == layer


def test_entry_rejects_off_vocabulary_layer():
    with pytest.raises(ValidationError):
        LayerMapEntry(source_id="source.x", layer="opinion", reason="y")


def test_aperture_rejects_off_vocabulary_layer():
    with pytest.raises(ValidationError):
        LayerMapAperture(layer="opinion", declared="unmeasured")


def test_layer_vocab_is_exactly_the_six():
    assert set(LAYER_VOCAB) == {
        "official", "domestic_press", "foreign_press",
        "social_digest", "public_data", "physical",
    }


# ---------------------------------------------------------------------------
# 2. entries[].reason is never optional.
# ---------------------------------------------------------------------------


def test_entry_rejects_blank_reason():
    with pytest.raises(ValidationError):
        LayerMapEntry(source_id="source.x", layer="official", reason="   ")


def test_entry_rejects_missing_reason():
    with pytest.raises(ValidationError):
        LayerMapEntry.model_validate({"source_id": "source.x", "layer": "official"})


# ---------------------------------------------------------------------------
# 3. apertures[].declared vocabulary + the absent-needs-reason rule.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("declared", sorted(APERTURE_DECLARED_VOCAB))
def test_aperture_accepts_every_declared_state(declared: str):
    reason = "structurally closed" if declared == "absent" else ""
    aperture = LayerMapAperture(layer="official", declared=declared, reason=reason)
    assert aperture.declared == declared


def test_aperture_rejects_off_vocabulary_declared():
    with pytest.raises(ValidationError):
        LayerMapAperture(layer="official", declared="mostly", reason="")


def test_aperture_absent_without_reason_is_rejected():
    with pytest.raises(ValidationError):
        LayerMapAperture(layer="official", declared="absent", reason="")
    with pytest.raises(ValidationError):
        LayerMapAperture(layer="official", declared="absent", reason="   ")


def test_aperture_absent_with_reason_is_accepted():
    aperture = LayerMapAperture(
        layer="official", declared="absent",
        reason="no state press office operates for this desk",
    )
    assert aperture.declared == "absent"


def test_aperture_present_and_unmeasured_do_not_require_a_reason():
    LayerMapAperture(layer="official", declared="present", reason="")
    LayerMapAperture(layer="official", declared="unmeasured", reason="")


# ---------------------------------------------------------------------------
# 4. A layer_map must declare every layer in the aperture — a missing layer
#    must never silently validate as "unstated == agreement".
# ---------------------------------------------------------------------------


def test_descriptor_rejects_apertures_missing_a_layer():
    apertures = _full_apertures()
    apertures.pop()  # drop the last layer's declaration entirely
    with pytest.raises(ValidationError, match="missing"):
        LayerMapDescriptor.model_validate(
            _base_body(apertures=apertures), strict=False
        )


def test_descriptor_rejects_apertures_repeating_a_layer():
    apertures = _full_apertures()
    apertures.append(dict(apertures[0]))
    with pytest.raises(ValidationError, match="repeats"):
        LayerMapDescriptor.model_validate(
            _base_body(apertures=apertures), strict=False
        )


def test_descriptor_accepts_full_six_layer_aperture_coverage():
    desc = LayerMapDescriptor.model_validate(_base_body(), strict=False)
    assert {a.layer for a in desc.apertures} == set(LAYER_VOCAB)


# ---------------------------------------------------------------------------
# 5. entries[] may not double-assign one source to two layers.
# ---------------------------------------------------------------------------


def test_descriptor_rejects_duplicate_source_entries():
    entries = [
        {"source_id": "source.dup", "layer": "official", "reason": "a"},
        {"source_id": "source.dup", "layer": "foreign_press", "reason": "b"},
    ]
    with pytest.raises(ValidationError, match="more than one layer"):
        LayerMapDescriptor.model_validate(
            _base_body(entries=entries), strict=False
        )


# ---------------------------------------------------------------------------
# 6. country is a strict ISO-3166-1 alpha-2 code.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["zz", "ZZZ", "1L", ""])
def test_descriptor_rejects_malformed_country(bad: str):
    with pytest.raises(ValidationError):
        LayerMapDescriptor.model_validate(_base_body(country=bad), strict=False)


# ---------------------------------------------------------------------------
# 7. The committed IL map (curated, Program 6 L0 curate_il lane) round-trips
#    through the real model. IL was the pilot DRAFT (docs/LAYERS.md) and has
#    since been curated: reviewed state, real per-layer aperture
#    declarations, and a reason on every entry that is either a confirmed
#    "derived:" rule or a fresh "curated:" per-country judgement.
# ---------------------------------------------------------------------------


def test_committed_israel_map_validates():
    path = DESCRIPTORS_DIR / "layer_map_il.yaml"
    body = yaml.safe_load(path.read_text(encoding="utf-8"))
    desc = LayerMapDescriptor.model_validate(body, strict=False)
    assert desc.country == "IL"
    assert desc.identity.state.value == "reviewed"
    assert desc.map_version == "layer_map_il.v1"
    assert desc.entries, "the curated map must carry at least one entry"
    assert {a.layer for a in desc.apertures} == set(LAYER_VOCAB)
    assert not all(a.declared == "unmeasured" for a in desc.apertures), (
        "a curated map must declare real present/absent on at least one "
        "layer — all-unmeasured is the untouched-draft shape, not a "
        "reviewed one"
    )
    assert all(
        e.reason.startswith("derived:") or e.reason.startswith("curated:")
        for e in desc.entries
    ), "every entry's reason must be a confirmed derivation or a curated one"


def test_committed_israel_pilot_draft_is_not_a_source_descriptor():
    """`descriptors/source_*.yaml` is reserved for SourceDescriptor
    (tests/data_pkg/test_source_class_taxonomy.py) — this file must NOT match
    that prefix."""
    assert not (DESCRIPTORS_DIR / "layer_map_il.yaml").name.startswith("source_")

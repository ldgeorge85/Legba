# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Program 6 L0 — the ``layer_map`` descriptor kind (migration 0214).

``descriptors/layer_map_<cc>.yaml`` is a LAYER TABLE for one country: which
curated sources read as which layer (``official`` / ``domestic_press`` /
``foreign_press`` / ``social_digest`` / ``public_data`` / ``physical``) and why
— plus that desk's APERTURE DECLARATION, one row per layer, saying whether the
layer is ``present``, structurally ``absent`` (never inferred — always a
reasoned operator call) or ``unmeasured``.

This is NOT a ``SourceDescriptor`` — ``descriptors/source_*.yaml`` is reserved
for that kind (``tests/data_pkg/test_source_class_taxonomy.py``). It is also
not registry-lifecycle-managed in this lane: there is no route, no FSM
transition, no analyst reading it. It is validated here and upserted by
``scripts/load_layer_map.py`` / ``legba.data.layers.loader`` directly into
``source_layers`` / ``desk_apertures`` — the same "plain static map an operator
edits" tradition as ``descriptors/wire_map.yaml``, except this one IS loaded
into tables because the composition/scorecard tower (a later program) has to
join against it by country, not just read it at review time.

Lives in the registry package (not ``schemas/``) because it is not a peer of
``SourceDescriptor``/``TargetDescriptor``/``AnalystDescriptor`` in the
registry's own CRUD/lifecycle sense — see the module docstring above.
"""

from __future__ import annotations

import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from ..layers._vocab import APERTURE_DECLARED_VOCAB, LAYER_VOCAB

_ISO2_RE = re.compile(r"^[A-Z]{2}$")


class LayerMapState(str, Enum):
    """This kind's OWN state vocabulary — deliberately not the registry's
    ``LifecycleState`` (``draft`` / ``configured`` / ``active`` / ``paused`` /
    ``retired``). A ``layer_map`` is never registered/activated through the
    audited registry route (see the module docstring: no FSM transition, no
    analyst reads it), so it needs none of that machinery's states — only
    whether a human has turned the retag generator's proposal into a curated
    map. ``draft`` is the generator's untouched output (every entry
    ``reason: "derived: ..."``, every aperture ``unmeasured`` —
    ``descriptors/layer_map_il.yaml`` is the committed example); ``reviewed``
    is an operator-curated map — real per-entry reasons, real apertures, a
    real ``map_version`` (docs/LAYERS.md "Loading a curated map")."""

    DRAFT = "draft"
    REVIEWED = "reviewed"


class LayerMapIdentity(BaseModel):
    """Mirrors the shape of ``SourceIdentity`` / ``TargetIdentity`` closely
    enough that a reader who knows those two shapes needs nothing new here,
    without inheriting their registry-lifecycle machinery (this kind is never
    registered/activated through the audited route in this lane) — including
    its ``state`` vocabulary, which is ``LayerMapState`` above, not
    ``LifecycleState``."""

    model_config = ConfigDict(strict=True, extra="forbid")

    id: str = Field(pattern=r"^layer_map_[a-z]{2}$")
    name: str
    schema_uri: str = Field(pattern=r"^legba/layer_map/\d+\.\d+\.\d+$")
    kind: str = Field(default="layer_map", pattern=r"^layer_map$")
    owner: str
    created: datetime
    state: LayerMapState = LayerMapState.DRAFT

    @field_validator("state", mode="before")
    @classmethod
    def _coerce_state(cls, v: Any) -> Any:
        # Same wire-form concern WireEnumCoercion documents for the registry
        # identity models: strict=True turns str -> Enum coercion off, but
        # YAML/JSON always hand this field a bare string.
        if isinstance(v, str) and not isinstance(v, LayerMapState):
            return LayerMapState(v)
        return v


class LayerMapEntry(BaseModel):
    """One curated (or draft-derived) row: this source reads as this layer,
    for this reason. ``reason`` is never optional — curation IS the layer's
    bias, and an unreasoned entry hides exactly the judgement the map exists
    to make visible."""

    model_config = ConfigDict(strict=True, extra="forbid")

    source_id: str = Field(min_length=1)
    layer: str
    reason: str = Field(min_length=1)

    @field_validator("layer")
    @classmethod
    def _known_layer(cls, v: str) -> str:
        if v not in LAYER_VOCAB:
            raise ValueError(
                f"unknown layer {v!r} — must be one of {LAYER_VOCAB}"
            )
        return v

    @field_validator("reason")
    @classmethod
    def _nonblank_reason(cls, v: str) -> str:
        if not v.strip():
            raise ValueError(
                "entries[].reason must be non-blank — curation is the "
                "layer's bias, and an entry with no reason hides it"
            )
        return v


class LayerMapAperture(BaseModel):
    """This desk's declared reach into one layer. ``declared='absent'``
    REQUIRES a non-blank reason — an absence with no stated reason is
    indistinguishable from one nobody ever checked, which is exactly the
    silent-agreement failure the aperture declaration exists to rule out."""

    model_config = ConfigDict(strict=True, extra="forbid")

    layer: str
    declared: str
    reason: str = ""

    @field_validator("layer")
    @classmethod
    def _known_layer(cls, v: str) -> str:
        if v not in LAYER_VOCAB:
            raise ValueError(
                f"unknown layer {v!r} — must be one of {LAYER_VOCAB}"
            )
        return v

    @field_validator("declared")
    @classmethod
    def _known_declared(cls, v: str) -> str:
        if v not in APERTURE_DECLARED_VOCAB:
            raise ValueError(
                f"unknown declared state {v!r} — must be one of "
                f"{APERTURE_DECLARED_VOCAB}"
            )
        return v

    @model_validator(mode="after")
    def _absent_needs_reason(self) -> "LayerMapAperture":
        if self.declared == "absent" and not self.reason.strip():
            raise ValueError(
                "apertures[].reason is required when declared='absent' — a "
                "missing layer must never read as an unreasoned agreement"
            )
        return self


class LayerMapDescriptor(BaseModel):
    """The whole file: ``descriptors/layer_map_<cc>.yaml``."""

    model_config = ConfigDict(strict=True, extra="forbid")

    identity: LayerMapIdentity
    country: str
    map_version: str = Field(min_length=1)
    entries: list[LayerMapEntry] = Field(default_factory=list)
    apertures: list[LayerMapAperture]

    @field_validator("country")
    @classmethod
    def _iso2(cls, v: str) -> str:
        if not _ISO2_RE.match(v):
            raise ValueError(
                f"country must be an uppercase ISO-3166-1 alpha-2 code, got {v!r}"
            )
        return v

    @model_validator(mode="after")
    def _apertures_cover_the_whole_vocab_exactly_once(self) -> "LayerMapDescriptor":
        seen = [a.layer for a in self.apertures]
        if len(seen) != len(set(seen)):
            dupes = sorted({v for v in seen if seen.count(v) > 1})
            raise ValueError(f"apertures[] repeats layer(s): {dupes}")
        missing = set(LAYER_VOCAB) - set(seen)
        if missing:
            raise ValueError(
                "apertures[] must declare every layer in the vocabulary "
                f"(present/absent/unmeasured) — missing {sorted(missing)}. "
                "A layer with no declaration would read as silent agreement."
            )
        return self

    @model_validator(mode="after")
    def _entries_do_not_double_assign_a_source(self) -> "LayerMapDescriptor":
        seen = [e.source_id for e in self.entries]
        dupes = sorted({v for v in seen if seen.count(v) > 1})
        if dupes:
            raise ValueError(
                f"entries[] assigns more than one layer to: {dupes} — a "
                "source reads as exactly one layer per country map"
            )
        return self


__all__ = [
    "LayerMapAperture",
    "LayerMapDescriptor",
    "LayerMapEntry",
    "LayerMapIdentity",
    "LayerMapState",
]

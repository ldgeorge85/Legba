# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The LAYER vocabulary — Program 6 L0 (docs/DIRECTION.md, "The layered source
fan-out"; migration 0214).

THE IDEA. Today a desk reads one pool of sources. The next widening ingests,
per country, the same stack of source LAYERS — official, domestic press,
foreign press, a social digest, public data, the physical/hazard layer — and
makes the CHANGE in that country's own layer-to-layer divergence the finding,
never the raw gap (every country has one). The mapping from platform to layer
is per country, curated and versioned, because curation IS the layer's bias:
the same channel is independent commentary in one country and the state's
voice in another.

A LAYER is not an ``origin_class`` (``data/provenance/origin.py`` — where a row
came from: live vs. seed vs. backfilled history) and it is not a
``source_class`` (``data/schemas/source.py`` — the editorial tier a source's
OWN descriptor declares: reporting / analysis / official / state_media,
platform-wide). A layer is the country-scoped READING of a source: the same
``source_class: state_media`` outlet is ``official`` for the country it speaks
for and, read from another desk, is foreign state messaging rather than that
desk's own official layer at all. The three axes stay orthogonal on purpose —
mixing them would collapse a platform-wide editorial fact into a per-desk
interpretive one.

APERTURE. A desk's aperture declares, per layer, whether that layer is
``present`` (curated sources exist), ``absent`` (structurally unavailable — a
declared, reasoned absence) or ``unmeasured`` (nobody has looked yet). The
three-state vocabulary exists so a missing layer never silently reads as
agreement: an absent foreign-press layer for a closed country is a fact about
that country's information environment, not a gap in the curation.

A LEAF: stdlib only, no I/O, importable from the slim registry image and from
any handler without pulling in the runtime.
"""

from __future__ import annotations

#: The closed six-layer vocabulary (migration 0214's `source_layers_layer_vocab`
#: and `desk_apertures_layer_vocab` CHECKs mirror this exactly — see
#: `tests/data_pkg/test_layer_vocab_matches_migration.py`), in canonical order.
#:
#:   official        — the target country's OWN government / primary-source
#:                      publisher, or a state-controlled outlet read as that
#:                      state's voice (state_media source_class, scoped to
#:                      this country).
#:   domestic_press  — independent/commercial press headquartered in and
#:                      covering this country (reporting source_class, scoped
#:                      to this country).
#:   foreign_press   — international press, wire/agency dispatches, and press
#:                      not scoped to this country.
#:   social_digest    — channel-level social monitoring (Telegram/Discord and
#:                      similar), counts and summaries only — never identities
#:                      (docs/DIRECTION.md: "removal is a first-class signal,
#:                      stored as counts and summaries, never as identities").
#:   public_data     — structured feeds: IGO/government open data, sanctions
#:                      lists, conflict-event datasets, analytical/think-tank
#:                      output with no single-country scope.
#:   physical        — model-free structured feeds (GeoJSON-style hazard /
#:                      sensor data) — the one layer with no editorial voice
#:                      at all.
LAYER_VOCAB: tuple[str, ...] = (
    "official",
    "domestic_press",
    "foreign_press",
    "social_digest",
    "public_data",
    "physical",
)

#: The closed three-state aperture-declaration vocabulary (migration 0214's
#: `desk_apertures_declared_vocab` CHECK mirrors this exactly).
#:
#:   present     — curated `source_layers` rows exist for this (target, layer).
#:   absent      — declared, reasoned: the layer is structurally unavailable
#:                 for this desk (e.g. no foreign press is permitted to
#:                 operate). REQUIRES a non-blank `reason` — an absence with no
#:                 stated reason is indistinguishable from one nobody checked.
#:   unmeasured  — nobody has curated this layer for this desk yet. The honest
#:                 default the retag generator stamps on every draft aperture:
#:                 the code NEVER infers `absent` on its own.
APERTURE_DECLARED_VOCAB: tuple[str, ...] = ("present", "absent", "unmeasured")


def is_valid_layer(value: str) -> bool:
    return value in LAYER_VOCAB


def is_valid_declared(value: str) -> bool:
    return value in APERTURE_DECLARED_VOCAB


__all__ = [
    "APERTURE_DECLARED_VOCAB",
    "LAYER_VOCAB",
    "is_valid_declared",
    "is_valid_layer",
]

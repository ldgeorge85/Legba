# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The four TAXON families — vocabularies that lived as code literals, now
registered vocabulary families (W7 TAXON-1; Review A §2.5 #9).

    salience_class        the ``signal_salience`` event classes
                          (``signals.salience->>'event_class'``)
    collection_gap_kind   the ``collection_requirements.origin`` kinds
                          (the collection_gap / source_request / reference_gap
                          organs that raise a requirement)
    band                  the scorecard's band ladder plus the
                          ``insufficient-evidence`` sentinel
    source_layer          the six source LAYER names (``source_layers.layer``,
                          ``desk_apertures.layer``)

THE LITERAL IS THE FALLBACK AND THE DRIFT GUARD. Each family's tuple below is
the set the code used before this lane, in its canonical order; the owning
modules (``signal_salience.EVENT_CLASSES``, ``scorecard_banding.BAND_LADDER`` /
``INSUFFICIENT``, ``layers._vocab.LAYER_VOCAB``, the collection_gap origins)
now read their literal from HERE, the bring-up walker
(``scripts/bringup_register_descriptors.py``) seeds the rows from HERE, and
``tests/data_pkg/test_taxon_families.py`` asserts the seeded rows equal these
tuples — so the code and the registry cannot drift apart silently.

READ-THROUGH. :func:`taxon_values` answers a family's current values from the
INSTALLED vocabulary source (:func:`install_vocabulary` — the registry installs
its ``VocabularyCache``; the runtime installs none yet, so it reads the
fallback), degrading PER FAMILY to the literal: a family the source does not
hold (no rows yet — the walker has not run on that host) or holds as
``families_unloaded`` reads the literal tuple, never an empty set.

CHECK-BOUND FAMILIES. ``collection_gap_kind`` and ``source_layer`` are also
DB CHECK constraints (migrations 0193, 0214). The CHECK is the write gate for
those two: a value the family admits but the CHECK does not would fail the
insert, so their read-through is INTERSECTED with the literal (the CHECK
set) — a family can narrow them, never widen them, until a migration lane
widens the CHECK.

WRITE-TIME BINDING. :func:`bind_taxon` is this lane's own minimal check (TAG-1's
``bind_tag`` had not merged at the pin): a value in the family binds
``bound``; anything else binds ``unclassified`` with the raw value kept —
the same status words TAG-1 uses (``bound`` / ``unclassified``; its third,
``legacy_src``, is a bare-tag status no taxon value can take). Writers of a
CHECK-bound family REFUSE an unclassified value by name rather than write
``unclassified`` into a column whose CHECK would reject it.

PROFILES. Each family is a platform family (``schemas/vocabulary.py``
``PLATFORM_FAMILIES``) that a profile may carry its own copy of, as
``<profile>.<family>`` (``PROFILE_OVERRIDABLE_FAMILIES``). No reader consults
a profile's copy yet — this lane registers the sets; reading a profile's
override is the next step.

A LEAF: stdlib only, no I/O — importable from the slim registry image, the
runtime and every handler.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Protocol

logger = logging.getLogger(__name__)

SALIENCE_CLASS = "salience_class"
COLLECTION_GAP_KIND = "collection_gap_kind"
BAND = "band"
SOURCE_LAYER = "source_layer"

#: The ``signal_salience`` S-1a closed event-class taxonomy (``other`` is the
#: safe generic bucket a value outside it coerces to).
SALIENCE_CLASSES: tuple[str, ...] = (
    "leader_death", "kinetic_strike", "mass_casualty", "coup_unrest",
    "diplomatic_rupture", "sanctions_economic", "market_move",
    "disaster_natural", "procurement_routine", "meme_sports_culture",
    "institutional_change", "other",
)

#: ``collection_requirements.origin`` — mirrored by the
#: ``collection_requirements_origin_check`` CHECK (migration 0193).
COLLECTION_GAP_KINDS: tuple[str, ...] = (
    "collection_gap", "source_request", "reference_gap",
)

#: The band ladder, ascending (demotion walks one rung down, clamped at low).
BAND_LADDER: tuple[str, ...] = ("low", "watch", "elevated", "high", "critical")

#: The banding sentinel for a dimension with no qualifying verified claim — a
#: band VALUE, not a rung of the ladder.
INSUFFICIENT_BAND: str = "insufficient-evidence"

BANDS: tuple[str, ...] = BAND_LADDER + (INSUFFICIENT_BAND,)

#: The closed six-layer vocabulary, canonical order — mirrored by the
#: ``source_layers_layer_vocab`` / ``desk_apertures_layer_vocab`` CHECKs
#: (migration 0214).
SOURCE_LAYERS: tuple[str, ...] = (
    "official",
    "domestic_press",
    "foreign_press",
    "social_digest",
    "public_data",
    "physical",
)

#: family -> the fallback literal, in canonical order.
TAXON_FALLBACKS: dict[str, tuple[str, ...]] = {
    SALIENCE_CLASS: SALIENCE_CLASSES,
    COLLECTION_GAP_KIND: COLLECTION_GAP_KINDS,
    BAND: BANDS,
    SOURCE_LAYER: SOURCE_LAYERS,
}

TAXON_FAMILIES: tuple[str, ...] = tuple(TAXON_FALLBACKS)

#: The families whose values are also a DB CHECK (family -> constraint name).
CHECK_BOUND_FAMILIES: dict[str, str] = {
    COLLECTION_GAP_KIND: "collection_requirements_origin_check",
    SOURCE_LAYER: "source_layers_layer_vocab",
}

STATUS_BOUND = "bound"
STATUS_UNCLASSIFIED = "unclassified"
UNCLASSIFIED = "unclassified"

#: One-line seed notes, per (family, value) — what the walker writes into
#: ``vocabulary_entries.notes``. Every fallback value has one (pinned).
TAXON_NOTES: dict[tuple[str, str], str] = {
    (SALIENCE_CLASS, "leader_death"): "death or removal of a head of state or government",
    (SALIENCE_CLASS, "kinetic_strike"): "a strike, attack or military action",
    (SALIENCE_CLASS, "mass_casualty"): "an event with mass casualties",
    (SALIENCE_CLASS, "coup_unrest"): "a coup, uprising, protest or unrest",
    (SALIENCE_CLASS, "diplomatic_rupture"): "a diplomatic break or rupture",
    (SALIENCE_CLASS, "sanctions_economic"): "sanctions, trade or economic measures",
    (SALIENCE_CLASS, "market_move"): "a market or currency move",
    (SALIENCE_CLASS, "disaster_natural"): "a natural disaster",
    (SALIENCE_CLASS, "procurement_routine"): "routine procurement or acquisition",
    (SALIENCE_CLASS, "meme_sports_culture"): "meme, sports, culture or celebrity",
    (SALIENCE_CLASS, "institutional_change"): "a cabinet or command-level personnel change, removal proceedings, government formation, a change of legislative control, a ratification or emergency rule",
    (SALIENCE_CLASS, "other"): "the safe generic bucket an unrecognised class coerces to",
    (COLLECTION_GAP_KIND, "collection_gap"): "a starved desk x dimension cell in the current scorecard",
    (COLLECTION_GAP_KIND, "source_request"): "a standing source_request hypothesis (an assessor's no-source-covers-X flag)",
    (COLLECTION_GAP_KIND, "reference_gap"): "an out-of-plane reference item our own collection does not carry (the reference gap dispatch)",
    (BAND, "low"): "band ladder rung 0 of 4 (ascending)",
    (BAND, "watch"): "band ladder rung 1 of 4 (ascending); severity moderate maps here",
    (BAND, "elevated"): "band ladder rung 2 of 4 (ascending)",
    (BAND, "high"): "band ladder rung 3 of 4 (ascending)",
    (BAND, "critical"): "band ladder rung 4 of 4 (ascending)",
    (BAND, INSUFFICIENT_BAND): "the banding sentinel: no qualifying verified claim; not a ladder rung",
    (SOURCE_LAYER, "official"): "the subject's own government or primary-source publisher, or its state voice",
    (SOURCE_LAYER, "domestic_press"): "independent or commercial press based in and covering the subject",
    (SOURCE_LAYER, "foreign_press"): "international press, wires and press not scoped to the subject",
    (SOURCE_LAYER, "social_digest"): "channel-level social monitoring, counts and summaries only",
    (SOURCE_LAYER, "public_data"): "structured open data, lists, datasets and unscoped analysis",
    (SOURCE_LAYER, "physical"): "model-free structured hazard or sensor feeds",
}


class VocabularySource(Protocol):
    """What :func:`taxon_values` reads: ``values(family)`` (the current
    canonical values; empty when the family holds none) and, optionally,
    ``families_unloaded`` (a list of ``{"family": ...}`` dicts). The
    registry's ``VocabularyCache`` is one."""

    def values(self, family: str) -> set[str]: ...


_installed: Any = None


def install_vocabulary(source: VocabularySource | None) -> None:
    """Install the process's vocabulary source (``None`` uninstalls). The
    source is held by reference: a cache that refreshes in place is read
    live."""
    global _installed
    _installed = source


def installed_vocabulary() -> Any:
    return _installed


def _family_values(source: Any, family: str) -> set[str] | None:
    """The source's values for ``family``, or ``None`` when the family is
    not loaded there (absent, empty, unloaded, or the source failed)."""
    if source is None:
        return None
    try:
        unloaded = {
            u.get("family") for u in (getattr(source, "families_unloaded", None) or [])
            if isinstance(u, dict)
        }
        if family in unloaded:
            return None
        vals = source.values(family)
    except Exception as exc:  # noqa: BLE001 — degrade to the literal, never raise
        logger.warning("taxon read-through failed family=%s: %s", family, exc)
        return None
    vals = {v for v in (vals or ()) if isinstance(v, str)}
    return vals or None


def taxon_values(family: str, *, vocab: Any = None) -> tuple[str, ...]:
    """The current values of a TAXON family, canonical order first (the
    fallback's order), any value the registry adds after it, sorted.

    ``vocab`` overrides the installed source. A family the source does not
    hold reads the fallback literal; a CHECK-bound family is intersected with
    its literal (a family can narrow it, never widen it)."""
    fallback = TAXON_FALLBACKS[family]
    vals = _family_values(vocab if vocab is not None else _installed, family)
    if vals is None:
        return fallback
    if family in CHECK_BOUND_FAMILIES:
        vals &= set(fallback)
        if not vals:
            return fallback
    ordered = [v for v in fallback if v in vals]
    ordered += sorted(vals - set(fallback))
    return tuple(ordered)


def taxon_set(family: str, *, vocab: Any = None) -> frozenset[str]:
    return frozenset(taxon_values(family, vocab=vocab))


@dataclass(frozen=True)
class TaxonBinding:
    """A value bound to a TAXON family at write time. ``value`` is what the
    writer persists (the canonical value, or ``unclassified``); ``raw`` is
    what it was handed; ``status`` is ``bound`` / ``unclassified``."""

    family: str
    value: str
    raw: str
    status: str

    @property
    def bound(self) -> bool:
        return self.status == STATUS_BOUND


_LOGGED_UNKNOWN: set[tuple[str, str]] = set()
_LOGGED_UNKNOWN_CAP = 512


def bind_taxon(family: str, raw: Any, *, vocab: Any = None) -> TaxonBinding:
    """Bind ``raw`` to ``family``: ``bound`` when it is a current value,
    otherwise ``unclassified`` (value ``unclassified``, ``raw`` kept). Never
    raises; one log line per new unknown (family, value) per process,
    bounded."""
    text = raw if isinstance(raw, str) else str(raw)
    if text in taxon_set(family, vocab=vocab):
        return TaxonBinding(family, text, text, STATUS_BOUND)
    key = (family, text[:200])
    if key not in _LOGGED_UNKNOWN and len(_LOGGED_UNKNOWN) < _LOGGED_UNKNOWN_CAP:
        _LOGGED_UNKNOWN.add(key)
        logger.warning("taxon value unclassified family=%s value=%r", family, text[:200])
    return TaxonBinding(family, UNCLASSIFIED, text, STATUS_UNCLASSIFIED)


def taxon_seed_rows(families: Iterable[str] | None = None) -> list[tuple[str, str, str]]:
    """``(family, value, notes)`` for every fallback value — what the
    bring-up walker seeds (ON CONFLICT DO NOTHING)."""
    out: list[tuple[str, str, str]] = []
    for fam in (families or TAXON_FAMILIES):
        for val in TAXON_FALLBACKS[fam]:
            out.append((fam, val, TAXON_NOTES[(fam, val)]))
    return out


__all__ = [
    "BAND", "BANDS", "BAND_LADDER", "CHECK_BOUND_FAMILIES", "COLLECTION_GAP_KIND",
    "COLLECTION_GAP_KINDS", "INSUFFICIENT_BAND", "SALIENCE_CLASS", "SALIENCE_CLASSES",
    "SOURCE_LAYER", "SOURCE_LAYERS", "STATUS_BOUND", "STATUS_UNCLASSIFIED",
    "TAXON_FALLBACKS", "TAXON_FAMILIES", "TAXON_NOTES", "TaxonBinding", "UNCLASSIFIED",
    "VocabularySource", "bind_taxon", "install_vocabulary", "installed_vocabulary",
    "taxon_seed_rows", "taxon_set", "taxon_values",
]

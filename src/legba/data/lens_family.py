# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The lean-lens roster as a profile vocabulary family (W9 SPLIT-2).

The journal_assessor kind runs two lens rosters. The five EPISTEMIC lenses
(trend, baserate, capability, intent, and the chorus diff) are platform prose
in ``legba.prompts`` and a platform tuple in ``journal_assessor``. The six
political-LEAN lenses are the geopolitics exemplar's: their prose lives in the
profile (``profiles/geopolitics/prompts/legba_geopolitics/lens_*``) and their
ids are the profile's ``geopolitics.lens`` vocabulary family, seeded by the
bring-up walker from ``descriptors/vocabulary_geopolitics_lens.yaml``.

THE LITERAL IS THE FALLBACK AND THE DRIFT GUARD (TAXON-1's idiom,
``legba.data.taxon_families``). :data:`LEAN_LENS_FALLBACK` is the roster the
code used before this lane, in its canonical order;
``tests/data_pkg/test_profile_prompts.py`` asserts the seed file's values equal
it, so the registry and the code cannot drift apart silently.

READ-THROUGH. :func:`lean_lens_ids` answers the family's current values from a
vocabulary source — the ``vocab`` argument, else the process's installed
TAXON source (``taxon_families.install_vocabulary``: the registry installs its
``VocabularyCache``) — degrading to the literal when the family is absent,
empty, unloaded, or the source fails; never an empty roster. A loaded family
reads through in the literal's order, any value it adds after, sorted.
``journal_assessor.LENS_LEAN_ANALYST_IDS`` is built from it at import, so — as
with every import-time roster constant (``rosters``' rule) — it holds the
roster the process started with; the runtime installs no TAXON source, so its
constant is the literal.

WHICH FAMILY. The family read is the exemplar's (:data:`LEAN_LENS_FAMILY`),
the same way ``taxon_families`` reads only the platform families today: a
reader that selects ``<profile>.lens`` by the running profile is the next step,
not this lane's.

WHERE A LEAN'S PROSE IS. :func:`lean_prompt_module` names the module a lean id
lives in: ``legba_<profile>.<id>`` (``profile.prompts_package_name``). The
descriptor's own ``method.prompt_module`` is what the runtime resolves for the
system prompt; this map serves the journal kind's declared-prior echo and its
logged prompt_module.

A LEAF: imports only ``legba.data.profile`` (itself a leaf) and the stdlib.
"""

from __future__ import annotations

import logging
from typing import Any

from .profile import prompts_package_name

logger = logging.getLogger(__name__)

__all__ = [
    "LEAN_LENS_FALLBACK",
    "LEAN_LENS_FAMILY",
    "LEAN_LENS_PROFILE",
    "LENS_AXIS",
    "lean_lens_ids",
    "lean_prompt_module",
    "lens_family",
]

#: The axis a profile's lens family is registered under (``<profile>.lens``).
LENS_AXIS = "lens"

#: The profile whose lean family and prompt package the platform reads.
LEAN_LENS_PROFILE = "geopolitics"


def lens_family(profile_id: str) -> str:
    """``<profile>.lens`` — the VOCAB-1 namespaced family of a profile's lenses."""
    return f"{profile_id}.{LENS_AXIS}"


#: The family :func:`lean_lens_ids` reads.
LEAN_LENS_FAMILY = lens_family(LEAN_LENS_PROFILE)

#: The six political-lean lens ids, canonical order — the documented fallback
#: (VOICES LEANS 2026-09-21; operator decision D-4 accepted the priors as drafts).
LEAN_LENS_FALLBACK: tuple[str, ...] = (
    "lens_left", "lens_right", "lens_centre",
    "lens_pragmatist", "lens_militarist", "lens_isolationist",
)


def _family_values(source: Any, family: str) -> set[str] | None:
    """``source``'s values for ``family``, or ``None`` when it does not hold
    the family loaded. Reads a TAXON-style source (``values(family)`` plus an
    optional ``families_unloaded``) or a ``data.tags.VocabularyIndex``
    (``family_loaded`` + a ``values`` mapping)."""
    if source is None:
        return None
    try:
        if callable(getattr(source, "family_loaded", None)):
            if not source.family_loaded(family):
                return None
            vals = source.values.get(family)
        else:
            unloaded = {
                u.get("family") for u in (getattr(source, "families_unloaded", None) or [])
                if isinstance(u, dict)
            }
            if family in unloaded:
                return None
            vals = source.values(family)
    except Exception as exc:  # noqa: BLE001 — degrade to the literal, never raise
        logger.warning("lens family read-through failed family=%s: %s", family, exc)
        return None
    vals = {v for v in (vals or ()) if isinstance(v, str)}
    return vals or None


def lean_lens_ids(*, vocab: Any = None) -> tuple[str, ...]:
    """The lean-lens ids: the ``geopolitics.lens`` family when a vocabulary
    source holds it loaded, otherwise :data:`LEAN_LENS_FALLBACK`.

    ``vocab`` overrides the installed source. Never empty."""
    if vocab is None:
        from .taxon_families import installed_vocabulary

        vocab = installed_vocabulary()
    vals = _family_values(vocab, LEAN_LENS_FAMILY)
    if vals is None:
        return LEAN_LENS_FALLBACK
    ordered = [v for v in LEAN_LENS_FALLBACK if v in vals]
    ordered += sorted(vals - set(LEAN_LENS_FALLBACK))
    return tuple(ordered)


def lean_prompt_module(analyst_id: str) -> str:
    """The dotted module a lean lens's prose lives in
    (``legba_geopolitics.lens_left``)."""
    return f"{prompts_package_name(LEAN_LENS_PROFILE)}.{analyst_id}"

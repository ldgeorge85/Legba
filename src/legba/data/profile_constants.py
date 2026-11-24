# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The instance's constants, read from its profile (SPLIT-3).

The platform's mechanisms key on a handful of values that are the INSTANCE's,
not the platform's: which producer's claims the external-audit drain grades
first, which stratum a producer's grades fall in, which tier is the
per-subject one, which entity class a frame is anchored on and which curated
alias pairs merge without the adjudicator, which languages are translated
before NER, which adjacency edges feed the desk baselines, and which
applicability tags a subject carries. They are profile DATA
(``profiles/<id>/profile.yaml`` ``constants``, docs/PROFILES.md "Constants"),
and this module is the ONE reader the code calls (Review A §2.3).

WHERE THEY COME FROM. The ``rosters`` rule, restated: ``LEGBA_PROFILE`` set ⇒
that profile directory is loaded (a profile that does not load is an error,
never a silent fallback). Unset ⇒ the PACKAGED export
``profile_constants.json`` beside this file — the runtime and registry images
carry ``src/`` only, so the selected profile's constants ship inside the
package (``scripts/gen_profile_constants.py`` writes it; the committed file is
the default profile's export, and ``tests/data_pkg/test_profile_constants.py``
fails when it is stale).

THE FALLBACK. With no packaged export at all (a source tree that lost it),
the profile is UNLOADED and the reader answers :data:`FALLBACK` — the literals
the code spelled at fcbafb2e, the geopolitics exemplar's — logged once by
name (``profile_constants.unloaded``). A profile that LOADS and leaves a key
out gets ``none`` / empty instead, never the exemplar's value: each helper's
caller then refuses by name.

Cached per process; :func:`refresh` drops the cache (and can inject a
profile). Module constants built from a helper at import (the drain's
``_TIER_RANK``, ``desk_baseline._ADJACENCY_EDGES``, the NER filter's default
translate set, …) hold the constants the process started with.

**A leaf.** Imports only ``legba.data.profile`` (itself a leaf) and the stdlib.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .profile import MANIFEST_NAME, PROFILE_ENV, Profile, ProfileError, load_profile, resolve_profile

logger = logging.getLogger(__name__)

__all__ = [
    "EXPORT_SCHEMA",
    "FALLBACK",
    "PACKAGED_EXPORT",
    "ConstantSet",
    "adjacency_edges",
    "anchor_class",
    "applicability_tags",
    "audit_tier_rank",
    "auto_merge_class",
    "constant_set",
    "current",
    "desk_tier_by_analyst",
    "export_constants",
    "reference_subject_tags",
    "refresh",
    "subject_frame",
    "subject_producer",
    "top_floor",
    "translation_languages",
]

#: The packaged copy of the selected profile's constants.
PACKAGED_EXPORT = Path(__file__).with_name("profile_constants.json")
#: The ``schema`` stamp of :data:`PACKAGED_EXPORT`.
EXPORT_SCHEMA = "legba/profile_constants/1"

_Edges = tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ConstantSet:
    """One profile's constants, RESOLVED (frame ids already mapped to their
    producers), as the code reads them."""

    profile: str
    #: Where it was read from: ``profile:<manifest>``, ``packaged:<file>``,
    #: ``fallback:literal``.
    origin: str
    top_floor: str | None
    #: The drain's ranked producers, highest first.
    audit_tier_producers: tuple[str, ...]
    #: ``(analyst, tier)`` in declared order.
    desk_tiers: tuple[tuple[str, str], ...]
    subject_frame: str | None
    #: The subject frame's producer (``ui.frames``), or ``None``.
    subject_producer: str | None
    anchor_class: str | None
    auto_merge_class: str | None
    translation_languages: tuple[str, ...]
    #: ``(family, edges)`` in declared order.
    adjacency: tuple[tuple[str, _Edges], ...]
    applicability_tags: tuple[str, ...]
    #: The non-scored subject-roster tags the correctness roster also covers
    #: (INST-1), in declared order.
    reference_subject_tags: tuple[str, ...] = ()


#: The literals the code spelled at the pin fcbafb2e — what an UNLOADED
#: profile reads (see the module doc). Each is named by the site it came from.
FALLBACK = ConstantSet(
    profile="geopolitics",
    origin="fallback:literal",
    # profile.yaml top_floor (no code literal: the manifest's own)
    top_floor="world_assessor",
    # _external_audit_queue._TIER_RANK (4, 3, 2, 1)
    audit_tier_producers=(
        "world_assessor", "escalation_composition", "world_assessment", "country_composition",
    ),
    # external_truth._DESK_TIER_BY_ANALYST and its SQL CASE twin
    desk_tiers=(
        ("world_assessor", "world"),
        ("country_composition", "country"),
        ("region_composition", "region"),
        ("escalation_composition", "thematic"),
        ("world_assessment", "voice"),
        ("country_assessment", "voice"),
    ),
    # situation_clustering._target_for_category's startswith("country")
    subject_frame="country",
    # v3_api.eval_country_scorecard's analyst_id = 'country_composition'
    subject_producer="country_composition",
    # _frame_anchor.FrameAnchorConfig.class_gate; _coverage_floor_scan's fold
    anchor_class="country",
    # _entity_candidates' both_country band
    auto_merge_class="country",
    # reenrich_translation / reenrich_ner _DEFAULT_TRANSLATE_LANGS,
    # filters/ner._NON_LATIN_TRANSLATE_LANGS, journal_assessor._TRANSLATE_LANGS
    translation_languages=("ar", "fa", "he", "ru", "uk", "zh", "ja", "ko", "hi", "th", "ur"),
    # desk_baseline._ADJACENCY_EDGES
    adjacency=(("land_border", (
        ("AR", "BR"), ("CA", "US"), ("MX", "US"), ("DE", "FR"), ("FR", "IT"),
        ("CN", "RU"), ("CN", "IN"), ("CN", "KP"), ("CN", "PK"), ("CN", "MM"),
        ("IN", "PK"), ("IN", "MM"), ("RU", "UA"), ("RU", "KP"), ("KR", "KP"),
        ("IR", "TR"), ("IR", "PK"), ("ML", "BF"), ("ML", "NE"), ("BF", "NE"),
    )),),
    # scripts/bringup_tag_targets.WATCH_TAGS
    applicability_tags=("nuclear_watch", "conflict_active", "sanctions_regime"),
    # INST-1 (no literal at the pin: the reference roster was the scored desks
    # alone). The exemplar's declared value, so an unloaded profile reads what
    # the packaged export reads — the supply-chain lanes and flows are graded.
    reference_subject_tags=("supply_chain",),
)


def constant_set(profile: Profile, *, origin: str | None = None) -> ConstantSet:
    """The :class:`ConstantSet` of a loaded profile (frame ids resolved)."""
    try:
        where = str(profile.manifest_path)
    except ProfileError:
        where = "<in-memory>"
    c = profile.constants
    producer = {f.id: f.producer for f in profile.ui.frames}
    return ConstantSet(
        profile=profile.id,
        origin=origin or f"profile:{where}",
        top_floor=profile.top_floor,
        audit_tier_producers=tuple(str(producer[f]) for f in c.audit_tier_order),
        desk_tiers=tuple((t.analyst, t.tier) for t in c.desk_tiers),
        subject_frame=c.subject_frame,
        subject_producer=producer.get(c.subject_frame) if c.subject_frame else None,
        anchor_class=c.anchor_class,
        auto_merge_class=c.auto_merge_class,
        translation_languages=tuple(c.translation_languages),
        adjacency=tuple(
            (family, tuple((a, b) for a, b in edges)) for family, edges in c.adjacency.items()
        ),
        applicability_tags=tuple(c.applicability_tags),
        reference_subject_tags=tuple(c.reference_subject_tags),
    )


def export_constants(profile: Profile) -> str:
    """The text of :data:`PACKAGED_EXPORT` for ``profile``: keys sorted,
    two-space indent, trailing newline — deterministic bytes. Ordered values
    (the drain order, the tier table, the edge lists) are lists, so the key
    sort never reorders them."""
    cs = constant_set(profile)
    doc = {
        "_generated": (
            "scripts/gen_profile_constants.py from profiles/"
            f"{profile.id}/{MANIFEST_NAME} - do not edit by hand"
        ),
        "schema": EXPORT_SCHEMA,
        "profile": cs.profile,
        "top_floor": cs.top_floor,
        "audit_tier_producers": list(cs.audit_tier_producers),
        "desk_tiers": [[a, t] for a, t in cs.desk_tiers],
        "subject_frame": cs.subject_frame,
        "subject_producer": cs.subject_producer,
        "anchor_class": cs.anchor_class,
        "auto_merge_class": cs.auto_merge_class,
        "translation_languages": list(cs.translation_languages),
        "adjacency": [[family, [[a, b] for a, b in edges]] for family, edges in cs.adjacency],
        "applicability_tags": list(cs.applicability_tags),
        "reference_subject_tags": list(cs.reference_subject_tags),
    }
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=True) + "\n"


def _from_export(path: Path) -> ConstantSet:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != EXPORT_SCHEMA:
        raise ProfileError(f"{path} is not a {EXPORT_SCHEMA} export")

    def opt(key: str) -> str | None:
        v = doc.get(key)
        if v is not None and not isinstance(v, str):
            raise ProfileError(f"{path}: {key} is not a string")
        return v

    def strs(val: Any, key: str) -> tuple[str, ...]:
        if not isinstance(val, list) or not all(isinstance(v, str) for v in val):
            raise ProfileError(f"{path}: {key} is not a list of strings")
        return tuple(val)

    def pairs(val: Any, key: str) -> tuple[tuple[str, str], ...]:
        if not isinstance(val, list):
            raise ProfileError(f"{path}: {key} is not a list")
        out = [strs(p, key) for p in val]
        if any(len(p) != 2 for p in out):
            raise ProfileError(f"{path}: {key} holds a non-pair")
        return tuple((p[0], p[1]) for p in out)

    raw_adj = doc.get("adjacency")
    if not isinstance(raw_adj, list) or not all(
        isinstance(x, list) and len(x) == 2 and isinstance(x[0], str) for x in raw_adj
    ):
        raise ProfileError(f"{path}: adjacency is not a list of [family, edges]")
    return ConstantSet(
        profile=str(doc.get("profile") or ""),
        origin=f"packaged:{path}",
        top_floor=opt("top_floor"),
        audit_tier_producers=strs(doc.get("audit_tier_producers"), "audit_tier_producers"),
        desk_tiers=pairs(doc.get("desk_tiers"), "desk_tiers"),
        subject_frame=opt("subject_frame"),
        subject_producer=opt("subject_producer"),
        anchor_class=opt("anchor_class"),
        auto_merge_class=opt("auto_merge_class"),
        translation_languages=strs(doc.get("translation_languages"), "translation_languages"),
        adjacency=tuple((fam, pairs(edges, f"adjacency[{fam}]")) for fam, edges in raw_adj),
        applicability_tags=strs(doc.get("applicability_tags"), "applicability_tags"),
        # INST-1: an export written before the key existed declares none.
        reference_subject_tags=strs(
            doc.get("reference_subject_tags", []), "reference_subject_tags"
        ),
    )


def _select(env: Mapping[str, str] | None = None, *, export: Path | None = None) -> ConstantSet:
    env = os.environ if env is None else env
    if env.get(PROFILE_ENV):
        return constant_set(load_profile(resolve_profile(env=env)))
    path = PACKAGED_EXPORT if export is None else export
    if not path.is_file():
        logger.warning(
            "profile_constants.unloaded no profile (%s unset) and no packaged export at %s"
            " — reading the fcbafb2e literals (FALLBACK)", PROFILE_ENV, path,
        )
        return FALLBACK
    return _from_export(path)


_lock = threading.Lock()
_current: ConstantSet | None = None


def current() -> ConstantSet:
    """The process's constants (read once, then cached)."""
    global _current
    cs = _current
    if cs is None:
        with _lock:
            if _current is None:
                _current = _select()
            cs = _current
    return cs


def refresh(profile: Profile | str | os.PathLike[str] | None = None) -> ConstantSet:
    """Drop the cache and read again. ``profile`` (a loaded profile or its
    directory) is used instead of the selection rule; ``None`` re-applies it.
    Import-time constants built from a helper keep their first value."""
    global _current
    with _lock:
        if profile is None:
            _current = _select()
        elif isinstance(profile, Profile):
            _current = constant_set(profile)
        else:
            _current = constant_set(load_profile(profile))
        return _current


# ---------------------------------------------------------------------------
# The helpers — one per constant
# ---------------------------------------------------------------------------


def top_floor() -> str | None:
    """The producer id of the instance's top read, or ``None``."""
    return current().top_floor


def audit_tier_rank() -> dict[str, int]:
    """producer id -> its drain rank (higher drains first): the declared
    ``audit_tier_order`` counted down from its length. A producer not listed
    ranks 0; an instance that declares none ranks everything 0."""
    order = current().audit_tier_producers
    return {aid: len(order) - i for i, aid in enumerate(order)}


def desk_tier_by_analyst() -> dict[str, str]:
    """producer id -> its external-truth stratum, in declared order."""
    return dict(current().desk_tiers)


def subject_frame() -> str | None:
    """The per-subject tier's ``ui.frames`` id, or ``None``."""
    return current().subject_frame


def subject_producer() -> str | None:
    """The producer of the per-subject read (the subject frame's), or ``None``."""
    return current().subject_producer


def anchor_class() -> str | None:
    """The entity class a frame anchor canonicalizes to, or ``None`` (the
    anchor bar refuses by name)."""
    return current().anchor_class


def auto_merge_class() -> str | None:
    """The entity class whose curated alias pairs auto-merge, or ``None``
    (every alias pair stays gray)."""
    return current().auto_merge_class


def translation_languages() -> tuple[str, ...]:
    """The source languages translated before NER, in declared order."""
    return current().translation_languages


def adjacency_edges(family: str) -> _Edges:
    """The undirected edges of one adjacency family; ``()`` for a family the
    profile does not declare (no edge, no spillover — logged by name)."""
    for fam, edges in current().adjacency:
        if fam == family:
            return edges
    logger.info("profile_constants.adjacency_undeclared family=%s profile=%s", family, current().profile)
    return ()


def applicability_tags() -> tuple[str, ...]:
    """The closed applicability tag vocabulary, in declared order."""
    return current().applicability_tags


def reference_subject_tags() -> tuple[str, ...]:
    """The non-scored subject-roster tags whose subjects the correctness
    roster also covers (INST-1), in declared order; ``()`` when the profile
    declares none — the roster is then the scored subjects alone."""
    return current().reference_subject_tags

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""ACTORS as subject candidates for a sentence piece.

WHY. Desk sentences are mostly about ACTORS — an armed group, an agency, a
company, a named official — and the typing seat was offered only the desk's
own subject and the countries a thematic desk spans, so it correctly declined
(``subject_not_offered`` was S-4's top outcome). The instance already knows
which actors a cited row is about: the entity layer links each signal to the
entity profiles it mentions (``signal_entity_links`` → ``entity_profiles``,
merged rows folded to their keeper). This module turns those links into a
short, ranked list of typed candidates — IN CODE, from the rows the piece
itself cites — that the seat may pick from or decline. The seat never names a
subject that was not offered.

WHAT A CANDIDATE IS. ``SubjectCandidate(kind="entity", key=<profile id>,
names=<SubjectNames from the profile's canonical name and aliases>,
entity_id, canonical_name, entity_type)``. A COUNTRY-class entity becomes a
``place`` candidate keyed by its ISO code (its names come from the gazetteer,
exactly as a country desk's do) — the same subject the record already uses for
a country, never a second spelling of it.

WHAT IS NOT A SUBJECT (each declined with a named reason, counted):

==========================  ===================================================
``publisher``               the entity is the PUBLISHER of a row the piece cites
                            (its name is the row's source / outlet): a source
                            reporting a fact is not what the fact is about
``place_in_passing``        a location (or country) the piece names only as
                            where something happened ("… strikes **in** Gaza")
``desk_itself``             the desk's own subject under another spelling
                            (already offered as S1)
``already_offered``         a subject already offered (a related country)
``not_named_in_piece``      the piece does not name it: a sentence is about
                            what it names, not about everything its rows link
``event_not_actor``         an entity of class ``event`` (a summit, a storm)
``no_proper_name``          none of its names is a proper name (``government``)
==========================  ===================================================

RANKING (code): named in the piece is required; then named in the cited rows'
text, then the number of the piece's cited rows that link it, then class
(person / organization / corporation before the generic ``entity`` and
``location``), then name — at most :data:`MAX_ACTOR_CANDIDATES`.

READS. :func:`fetch_linked_entities` reads links BY SIGNAL ID (the indexed
``signal_entity_links (signal_id)`` path) — never the jsonb alias scan the
keeper resolver runs. The caller owns the connection and its read-only
transaction; nothing is written. Everything else here is pure.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from .provenance.text_fold import normalize_for_match
from .statement_subject_names import (
    SubjectNames,
    _country_code_of_name,
    _default_gazetteer,
    name_in_text,
    resolve_subject_names,
)
from .statement_typing import EvidenceRef, SubjectCandidate

#: Actor candidates offered per piece ("the top few").
MAX_ACTOR_CANDIDATES = 4

#: Entity classes that can be a statement's subject (``event`` cannot).
ACTOR_CLASSES = frozenset({"person", "organization", "corporation", "entity", "location", "country"})
PLACE_CLASSES = frozenset({"location", "country"})
_CLASS_RANK = {"person": 0, "organization": 0, "corporation": 0, "country": 1, "entity": 2, "location": 3}

#: The named reasons an entity linked to a cited row is not offered.
NOT_SUBJECT_REASONS = (
    "publisher", "place_in_passing", "desk_itself", "already_offered",
    "not_named_in_piece", "event_not_actor", "no_proper_name",
)

#: A place named only after one of these is named in passing ("in Gaza",
#: "across northern Gaza", "from the Gaza Strip").
_LOCATIVE_RE = re.compile(
    r"(?:\b(?:in|at|near|from|across|into|inside|outside|throughout|over|off|around|within|"
    r"toward|towards|via|through|along|between)\s+(?:the\s+)?(?:[A-Za-z-]+\s+)?)$",
    re.IGNORECASE,
)

#: Links by signal id, merged rows folded onto their keeper (one hop, the
#: ``merged_into`` pointer), merged/junk keepers dropped.
LINKED_ENTITIES_SQL = """
SELECT l.signal_id::text AS signal_id,
       k.id::text AS entity_id, k.canonical_name, k.entity_class,
       k.data->'merged_aliases' AS aliases,
       p.canonical_name AS linked_name
  FROM signal_entity_links l
  JOIN entity_profiles p ON p.id = l.entity_id
  JOIN entity_profiles k ON k.id = COALESCE(p.merged_into, p.id)
 WHERE l.signal_id = ANY($1::uuid[])
   AND COALESCE(k.data->>'gc_status', '') NOT IN ('merged', 'junk')
"""


@dataclass(frozen=True)
class LinkedEntity:
    """One entity profile linked to one or more of the cited signals."""

    entity_id: str
    canonical_name: str
    entity_class: str
    aliases: tuple[str, ...] = ()
    signal_ids: tuple[str, ...] = ()

    def profile(self) -> dict[str, Any]:
        """The ``entity_profiles`` mapping :func:`resolve_subject_names` reads."""
        return {"canonical_name": self.canonical_name, "aliases": list(self.aliases),
                "entity_class": self.entity_class}


@dataclass(frozen=True)
class ActorOffer:
    """The actor candidates for one piece, and what was declined and why."""

    offered: tuple[SubjectCandidate, ...]
    declined: tuple[tuple[str, str], ...] = ()
    detail: Mapping[str, Any] = field(default_factory=dict)

    def declined_counts(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for _, reason in self.declined:
            out[reason] = out.get(reason, 0) + 1
        return out


def _aliases(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            raw = [raw]
    if not isinstance(raw, list):
        return ()
    return tuple(str(a) for a in raw if isinstance(a, str) and a.strip())


def linked_from_rows(rows: Iterable[Mapping[str, Any]]) -> list[LinkedEntity]:
    """Fold link rows (``LINKED_ENTITIES_SQL``'s columns) into one
    :class:`LinkedEntity` per keeper, carrying every cited signal that links
    it and the linked surface names as aliases."""
    by_id: dict[str, dict[str, Any]] = {}
    for r in rows:
        eid = str(r["entity_id"])
        e = by_id.setdefault(eid, {"canonical_name": str(r["canonical_name"] or ""),
                                   "entity_class": str(r["entity_class"] or "entity"),
                                   "aliases": [], "signals": []})
        for a in (*_aliases(r.get("aliases")), str(r.get("linked_name") or "")):
            if a and a != e["canonical_name"] and a not in e["aliases"]:
                e["aliases"].append(a)
        sid = str(r["signal_id"]).lower()
        if sid not in e["signals"]:
            e["signals"].append(sid)
    return [LinkedEntity(eid, e["canonical_name"], e["entity_class"], tuple(e["aliases"]),
                         tuple(e["signals"])) for eid, e in sorted(by_id.items())]


async def fetch_linked_entities(conn: Any, signal_ids: Sequence[str]) -> list[LinkedEntity]:
    """The entities linked to ``signal_ids``, read by signal id on ``conn``
    (an asyncpg-shaped connection the caller opened read-only)."""
    ids = sorted({str(s).lower() for s in signal_ids if s})
    if not ids:
        return []
    return linked_from_rows(dict(r) for r in await conn.fetch(LINKED_ENTITIES_SQL, ids))


# ---------------------------------------------------------------------------
# What is not a subject
# ---------------------------------------------------------------------------


def _fold(s: str | None) -> str:
    return normalize_for_match(s or "")


def is_publisher(names: SubjectNames, evidence: Sequence[EvidenceRef]) -> bool:
    """The entity is the publisher of a cited row: one of its names IS the
    row's source / outlet name (folded), or the row's source id spells it
    (``aljazeera`` for Al Jazeera)."""
    forms = {_fold(n.name) for n in names.usable}
    squashed = {re.sub(r"[^a-z0-9]", "", f) for f in forms}
    for ev in evidence:
        src_name = _fold(getattr(ev, "source_name", None))
        if src_name and src_name in forms:
            return True
        sid = re.sub(r"[^a-z0-9]", "", (ev.source_id or "").lower())
        if sid and any(len(s) >= 4 and (sid == s or sid.startswith(s)) for s in squashed):
            return True
    return False


def named_only_in_passing(names: SubjectNames, text: str) -> bool:
    """Every occurrence of the place's names in ``text`` follows a locative
    preposition ("in X", "across northern X"): named as where, not as who."""
    hits = 0
    for n in names.usable:
        exact = n.case_sensitive
        hay = text if exact else normalize_for_match(text)
        needle = n.name if exact else normalize_for_match(n.name)
        if not needle:
            continue
        for m in re.finditer(rf"(?<!\w){re.escape(needle)}(?!\w)", hay):
            hits += 1
            if not _LOCATIVE_RE.search(hay[:m.start()]):
                return False
    return hits > 0


def _named(names: SubjectNames, text: str | None) -> bool:
    return bool(text) and any(name_in_text(n, text, excluded=names.excluded) for n in names.usable)


# ---------------------------------------------------------------------------
# Candidates
# ---------------------------------------------------------------------------


def _candidate_for(
    ent: LinkedEntity, *, gazetteer: Mapping[str, Any] | None, metonyms: Iterable[str] | None,
    subjects: Mapping[str, Any] | None,
) -> SubjectCandidate:
    kw: dict[str, Any] = {}
    if gazetteer is not None:
        kw["gazetteer"] = gazetteer
    if metonyms is not None:
        kw["metonyms"] = metonyms
    if subjects is not None:
        kw["subjects"] = subjects
    if ent.entity_class == "country":
        gz = gazetteer if gazetteer is not None else _default_gazetteer()
        iso = _country_code_of_name(ent.canonical_name, gz)
        if iso:
            return SubjectCandidate("place", iso.upper(), resolve_subject_names("place", iso.upper(), **kw))
    names = resolve_subject_names("entity", ent.entity_id, entity_profiles={ent.entity_id: ent.profile()}, **kw)
    return SubjectCandidate("entity", ent.entity_id, names, entity_id=ent.entity_id,
                            canonical_name=ent.canonical_name, entity_type=ent.entity_class)


def actor_candidates(
    piece_text: str,
    evidence: Sequence[EvidenceRef],
    linked: Iterable[LinkedEntity],
    *,
    offered: Sequence[SubjectCandidate] = (),
    limit: int = MAX_ACTOR_CANDIDATES,
    gazetteer: Mapping[str, Any] | None = None,
    metonyms: Iterable[str] | None = None,
    subjects: Mapping[str, Any] | None = None,
) -> ActorOffer:
    """The actor candidates for ONE piece: the entities linked to the rows
    the piece cites (``evidence``), minus what is not a subject, ranked.

    ``offered`` are the subjects already offered (the desk's own first): an
    entity spelling one of them is ``desk_itself`` (the first) or
    ``already_offered``."""
    cited = {str(ev.ref_id).lower() for ev in evidence}
    row_texts = [ev.text for ev in evidence]
    desk_forms: list[set[str]] = [{_fold(n.name) for n in getattr(s.names, "usable", s.names)
                                   if _fold(getattr(n, "name", n))} for s in offered]
    offered_keys = {(s.kind, s.key) for s in offered}
    declined: list[tuple[str, str]] = []
    ranked: list[tuple[tuple[Any, ...], SubjectCandidate]] = []
    seen: set[tuple[str, str]] = set()
    for ent in linked:
        rows = [s for s in ent.signal_ids if s in cited]
        if not rows:
            continue
        if ent.entity_class not in ACTOR_CLASSES:
            declined.append((ent.entity_id, "event_not_actor"))
            continue
        cand = _candidate_for(ent, gazetteer=gazetteer, metonyms=metonyms, subjects=subjects)
        names = cand.names
        if not names.usable:
            declined.append((ent.entity_id, "no_proper_name"))
            continue
        forms = {_fold(n.name) for n in names.usable}
        if (cand.kind, cand.key) in offered_keys or any(forms & d for d in desk_forms):
            hit = next((i for i, d in enumerate(desk_forms) if forms & d), None)
            if (cand.kind, cand.key) in offered_keys:
                hit = next(i for i, s in enumerate(offered) if (s.kind, s.key) == (cand.kind, cand.key))
            declined.append((ent.entity_id, "desk_itself" if hit == 0 else "already_offered"))
            continue
        if not _named(names, piece_text):
            declined.append((ent.entity_id, "not_named_in_piece"))
            continue
        if is_publisher(names, evidence):
            declined.append((ent.entity_id, "publisher"))
            continue
        if ent.entity_class in PLACE_CLASSES and named_only_in_passing(names, piece_text):
            declined.append((ent.entity_id, "place_in_passing"))
            continue
        if (cand.kind, cand.key) in seen:
            continue
        seen.add((cand.kind, cand.key))
        in_rows = any(_named(names, t) for t in row_texts)
        key = (-int(in_rows), -len(rows), _CLASS_RANK.get(ent.entity_class, 4), ent.canonical_name.lower())
        ranked.append((key, cand))
    ranked.sort(key=lambda kc: kc[0])
    offered_out = tuple(c for _, c in ranked[:limit])
    detail = {"linked": len({e.entity_id for e in linked if set(e.signal_ids) & cited}),
              "eligible": len(ranked), "offered": len(offered_out),
              "cut_by_limit": max(0, len(ranked) - limit)}
    return ActorOffer(offered_out, tuple(declined), detail)


__all__ = [
    "ACTOR_CLASSES",
    "LINKED_ENTITIES_SQL",
    "MAX_ACTOR_CANDIDATES",
    "NOT_SUBJECT_REASONS",
    "ActorOffer",
    "LinkedEntity",
    "actor_candidates",
    "fetch_linked_entities",
    "is_publisher",
    "linked_from_rows",
    "named_only_in_passing",
]

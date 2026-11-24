# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""THE SUBJECT LADDER — what a target is ABOUT, resolved one way (W10 SUBJECT-1).

The reference builder, the correctness grader and their helpers each worked out
a target's subject on their own: the grader sliced the last two characters off
the target id (``country_g20_ar`` -> ``AR``), the reference roster took the
first ``scope.geo`` code and fell back to the same slice, five more modules
split the id on its last underscore and called the tail an ISO2. On the
exemplar's 32 country desks every copy agreed by accident of spelling; on any
other target each one guessed differently (``lane_hormuz`` graded as ``UZ``,
``situation_iran_war`` as ``AR``). This module is the one resolver, and the id
is never parsed: the target descriptor's declared geometry is the evidence.

THE LADDER. Three rungs each, most authoritative first, deterministic in every
branch:

:func:`subject_code`
    1. ``scope.subject_code`` when the descriptor declares one;
    2. the single ``scope.geo`` code (an ISO2 on a country desk);
    3. the target id itself (a region, a thematic frame, a lane or a flow —
       no single geo — is keyed by its own id, never by an ISO guess).
:func:`subject_name`
    1. the gazetteer's name for that code, when the code is one of the
       target's declared geo codes and the caller hands a gazetteer (and,
       optionally, a ``spell`` that picks the newsroom spelling);
    2. the descriptor's ``identity.name``;
    3. the de-slugged id.
:func:`subject_kind`
    1. the frame kind, when the target is itself a frame of the profile's
       ``frame`` family (``region_mena`` -> ``region``);
    2. the descriptor's ``scope.domain`` (``geo``, ``thematic``, ``entity``,
       ``estate``);
    3. ``""`` — an id nothing declares has no kind.

``desk_reference.target_subject`` was already this shape and stays the ladder's
first caller.

WHAT A TARGET CAN BE. A descriptor body (``{"identity": …, "scope": …}``), a
flat row (``target_id`` / ``descriptor_id``, ``name``, ``geo``, ``domain``,
``subject_code`` — the shape the SQL below returns), or a bare id. A bare id is
looked up in the PROFILE's declared subjects — the packaged export
``profile_subjects.json`` beside this file, built from the profile's target
descriptor files the way ``profile_rosters.json`` is built from its rosters
(``scripts/gen_profile_subjects.py``; a drift test refuses a stale one) — so a
DB-free caller holding only an id (the grounding guards, the collection-gap
row builder) resolves a declared desk exactly as an async caller holding the
live body does. An id the profile does not declare resolves to itself:
rung 3, never a guess. ``LEGBA_PROFILE`` set reads that profile from disk
instead of the export, as ``legba.data.rosters`` does.

THE LABEL STYLE. The profile's ``subjects.label`` (separator, accepted aliases,
order) rides the same export; ``analysts/unit_names`` reads it through
:func:`label_style` and is the one producer and parser of the
``Tier — Name (slug)`` label.

**A leaf.** Imports only ``legba.data.profile`` (itself a leaf), ``yaml`` and
the stdlib; the I/O helpers take the caller's connection.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import yaml

from .profile import PROFILE_ENV, LabelStyle, Profile, load_profile, resolve_profile

__all__ = [
    "EXPORT_SCHEMA",
    "GAZETTEER_SQL",
    "HEADS_SQL",
    "PACKAGED_EXPORT",
    "Subject",
    "SubjectIndex",
    "current",
    "export_subjects",
    "gazetteer_names",
    "item_code",
    "label_style",
    "load_subject",
    "load_subjects",
    "refresh",
    "resolve",
    "subject_code",
    "subject_geo",
    "subject_kind",
    "subject_name",
]

#: The packaged copy of the selected profile's declared subjects.
PACKAGED_EXPORT = Path(__file__).with_name("profile_subjects.json")
#: The ``schema`` stamp of :data:`PACKAGED_EXPORT`.
EXPORT_SCHEMA = "legba/profile_subjects/1"
#: The vocabulary family whose entries are frames (``legba.data.frames``).
_FRAME_FAMILY = "frame"
#: A spell callable: ``(code, gazetteer_name) -> name or ""``.
Spell = Callable[[str, str], str]


@dataclass(frozen=True)
class Subject:
    """One target's subject, resolved: the code every per-subject key takes,
    the name a query or a sentence is built around, the kind, and the declared
    geometry (``scope.geo``, upper-cased; empty when none is declared)."""

    id: str
    code: str
    name: str
    kind: str
    geo: tuple[str, ...] = ()

    def as_tuple(self) -> tuple[str, str, str, str]:
        return (self.id, self.code, self.name, self.kind)


@dataclass(frozen=True)
class _Decl:
    """What a descriptor DECLARES about its subject — the ladder's input."""

    id: str
    name: str = ""
    geo: tuple[str, ...] = ()
    domain: str = ""
    code: str = ""


@dataclass(frozen=True)
class SubjectIndex:
    """The profile's declared subjects (by target id), its frame kinds (frame
    id -> kind) and its label style."""

    profile: str
    label: LabelStyle
    frames: Mapping[str, str] = field(default_factory=dict)
    subjects: Mapping[str, _Decl] = field(default_factory=dict)
    origin: str = ""


# ---------------------------------------------------------------------------
# Reading a target in any of its shapes
# ---------------------------------------------------------------------------


def _codes(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return ()
    if not isinstance(raw, (list, tuple)):
        return ()
    return tuple(str(g).strip().upper() for g in raw if isinstance(g, str) and g.strip())


def _mapping(target: Any) -> dict[str, Any] | None:
    if isinstance(target, Mapping):
        return dict(target)
    keys = getattr(target, "keys", None)
    if callable(keys):  # an asyncpg Record
        return {k: target[k] for k in keys()}
    return None


def _decl_of_body(body: Mapping[str, Any]) -> _Decl:
    identity = body.get("identity") if isinstance(body.get("identity"), Mapping) else {}
    scope = body.get("scope") if isinstance(body.get("scope"), Mapping) else {}
    return _Decl(
        id=str(identity.get("id") or ""),
        name=str(identity.get("name") or "").strip(),
        geo=_codes(scope.get("geo")),
        # GeoScope is the schema's default domain: a scope that names none is geo.
        domain=str(scope.get("domain") or ("geo" if scope else "")),
        code=str(scope.get("subject_code") or "").strip(),
    )


def _decl(target: Any) -> _Decl:
    if isinstance(target, _Decl):
        return target
    if isinstance(target, Subject):
        return _Decl(id=target.id, name=target.name, geo=target.geo,
                     domain=target.kind, code=target.code)
    if isinstance(target, str):
        tid = target.strip()
        return current().subjects.get(tid) or _Decl(id=tid)
    m = _mapping(target)
    if m is None:
        return _Decl(id=str(target or "").strip())
    body = m.get("body")
    if isinstance(body, str):
        try:
            body = json.loads(body)
        except ValueError:
            body = None
    if "identity" not in m and isinstance(body, Mapping):
        m = dict(body)
    if isinstance(m.get("identity"), Mapping):
        return _decl_of_body(m)
    tid = m.get("target_id") or m.get("descriptor_id") or m.get("id") or ""
    return _Decl(
        id=str(tid).strip(),
        name=str(m.get("name") or m.get("target_name") or "").strip(),
        geo=_codes(m.get("geo")),
        domain=str(m.get("domain") or "").strip(),
        code=str(m.get("subject_code") or "").strip(),
    )


# ---------------------------------------------------------------------------
# The ladder
# ---------------------------------------------------------------------------


def subject_code(target: Any) -> str:
    """The subject's code: the declared ``subject_code``, else the single
    ``scope.geo`` code, else the target id. Never parsed out of the id."""
    if isinstance(target, Subject):
        return target.code
    d = _decl(target)
    if d.code:
        return d.code
    if len(d.geo) == 1:
        return d.geo[0]
    return d.id


def subject_geo(target: Any) -> tuple[str, ...]:
    """The target's declared geometry (``scope.geo``), upper-cased, in order;
    ``()`` when it declares none. The only geo a subject has."""
    return _decl(target).geo


def subject_name(
    target: Any,
    *,
    gazetteer: Mapping[str, str] | None = None,
    spell: Spell | None = None,
) -> str:
    """The subject's name: the gazetteer's name for its code (through
    ``spell`` when given — e.g. ``_polity_match.home_country_name``), else the
    descriptor's name, else the de-slugged id. The gazetteer rung applies only
    when the code IS one of the target's declared geo codes."""
    d = _decl(target)
    code = subject_code(d)
    if code and code in d.geo and (gazetteer is not None or spell is not None):
        gazetteer_name = str((gazetteer or {}).get(code) or "")
        resolved = spell(code, gazetteer_name) if spell is not None else gazetteer_name
        if resolved:
            return str(resolved)
    if d.name:
        return d.name
    return d.id.replace("_", " ").strip()


def subject_kind(target: Any, *, frame_kinds: Mapping[str, str] | None = None) -> str:
    """The subject's kind: its frame kind when the target is itself a frame
    (the profile's ``frame`` family, or ``frame_kinds`` when given), else its
    ``scope.domain``, else ``""``."""
    if isinstance(target, Subject):
        return target.kind
    d = _decl(target)
    kinds = current().frames if frame_kinds is None else frame_kinds
    return str(kinds.get(d.id) or d.domain or "")


def resolve(
    target: Any,
    *,
    gazetteer: Mapping[str, str] | None = None,
    spell: Spell | None = None,
    frame_kinds: Mapping[str, str] | None = None,
) -> Subject:
    """All four, from one read of the target."""
    d = _decl(target)
    return Subject(
        id=d.id,
        code=subject_code(d),
        name=subject_name(d, gazetteer=gazetteer, spell=spell),
        kind=subject_kind(d, frame_kinds=frame_kinds),
        geo=d.geo,
    )


def item_code(code: str) -> str:
    """The resolved code as it rides a reference item id (``RD-<CODE>-…``)
    and the grader's packet kind: upper-cased, nothing else."""
    return str(code or "").strip().upper()


# ---------------------------------------------------------------------------
# The I/O helpers — the caller's connection, SELECT-only
# ---------------------------------------------------------------------------

#: The head descriptor fields the ladder reads, for a set of target ids.
HEADS_SQL = """
SELECT td.descriptor_id AS target_id,
       td.body -> 'identity' ->> 'name' AS name,
       ARRAY(SELECT jsonb_array_elements_text(td.body -> 'scope' -> 'geo')) AS geo,
       td.body -> 'scope' ->> 'domain' AS domain,
       td.body -> 'scope' ->> 'subject_code' AS subject_code
  FROM target_descriptors td
 WHERE td.is_head = TRUE
   AND td.descriptor_id = ANY($1::text[])
"""

#: The gazetteer read the ladder's first name rung takes.
GAZETTEER_SQL = """
SELECT iso2, name FROM iso_countries WHERE iso2 = ANY($1::text[])
"""


async def load_subjects(
    conn: Any,
    target_ids: Iterable[str],
    *,
    gazetteer: Mapping[str, str] | None = None,
    spell: Spell | None = None,
) -> dict[str, Subject]:
    """``{target_id: Subject}`` from the LIVE head descriptors. An id with no
    head resolves through the profile's declared subjects, then to itself."""
    ids = list(dict.fromkeys(str(t).strip() for t in target_ids if str(t or "").strip()))
    rows = await conn.fetch(HEADS_SQL, ids) if ids else []
    out = {
        str(r["target_id"]): resolve(r, gazetteer=gazetteer, spell=spell)
        for r in rows
    }
    for tid in ids:
        if tid not in out:
            out[tid] = resolve(tid, gazetteer=gazetteer, spell=spell)
    return out


async def load_subject(conn: Any, target_id: str) -> Subject:
    """One target's :class:`Subject`, from its live head (see
    :func:`load_subjects`)."""
    tid = str(target_id or "").strip()
    if not tid:
        return resolve(tid)
    return (await load_subjects(conn, [tid]))[tid]


async def gazetteer_names(conn: Any, codes: Iterable[str]) -> dict[str, str]:
    """``{code: gazetteer name}`` for the codes the gazetteer carries."""
    wanted = sorted({str(c) for c in codes if str(c or "").strip()})
    if not wanted:
        return {}
    rows = await conn.fetch(GAZETTEER_SQL, wanted)
    return {str(r["iso2"]): str(r["name"]) for r in rows}


# ---------------------------------------------------------------------------
# The profile's declared subjects — the export and the process index
# ---------------------------------------------------------------------------


def _frame_kinds(descriptors_dir: Path) -> dict[str, str]:
    """frame id -> its kind (the root of its parent walk), from the profile's
    ``frame`` vocabulary seed files. A kind itself (no parent) is not a frame."""
    parents: dict[str, str] = {}
    for p in sorted(descriptors_dir.glob("vocabulary_*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(body, Mapping) or body.get("family") != _FRAME_FAMILY:
            continue
        for entry in body.get("entries") or ():
            value = str((entry or {}).get("value") or "")
            parent = str((entry or {}).get("parent") or "")
            fam, sep, val = parent.partition(":")
            if value and sep and fam == _FRAME_FAMILY and val:
                parents[value] = val
    out: dict[str, str] = {}
    for frame in parents:
        kind, seen = parents[frame], {frame}
        while kind in parents and kind not in seen:
            seen.add(kind)
            kind = parents[kind]
        out[frame] = kind
    return dict(sorted(out.items()))


def _declared_targets(descriptors_dir: Path) -> list[_Decl]:
    """Every L1 target descriptor file in the profile's descriptors dir."""
    out: list[_Decl] = []
    for p in sorted(descriptors_dir.glob("*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        if not isinstance(body, Mapping) or not isinstance(body.get("identity"), Mapping):
            continue
        identity = body["identity"]
        if not str(identity.get("schema_uri") or "").startswith("legba/target/"):
            continue
        if str(identity.get("abstraction_level") or "L1") != "L1":
            continue
        out.append(_decl_of_body(body))
    return sorted(out, key=lambda d: d.id)


def _index_of_profile(profile: Profile) -> SubjectIndex:
    descriptors = profile.descriptors_path
    return SubjectIndex(
        profile=profile.id,
        label=profile.subjects.label,
        frames=_frame_kinds(descriptors),
        subjects={d.id: d for d in _declared_targets(descriptors)},
        origin=f"profile:{profile.root}",
    )


def export_subjects(profile: Profile) -> str:
    """The text of ``profile_subjects.json`` for ``profile``: the label style,
    the frame kinds and each declared L1 target's ladder inputs (id, name,
    geo, domain, declared code). Keys sorted, two-space indent, trailing
    newline: deterministic bytes."""
    index = _index_of_profile(profile)
    doc = {
        "schema": EXPORT_SCHEMA,
        "profile": index.profile,
        "label": index.label.model_dump(mode="json"),
        "frames": dict(index.frames),
        "subjects": [
            {k: v for k, v in (
                ("id", d.id), ("name", d.name), ("geo", list(d.geo)),
                ("domain", d.domain), ("subject_code", d.code),
            ) if v or k == "id"}
            for d in index.subjects.values()
        ],
    }
    return json.dumps(doc, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def _from_export(path: Path) -> SubjectIndex:
    doc = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(doc, dict) or doc.get("schema") != EXPORT_SCHEMA:
        raise ValueError(f"{path}: not a {EXPORT_SCHEMA} export")
    decls = [
        _Decl(
            id=str(s["id"]), name=str(s.get("name") or ""), geo=_codes(s.get("geo")),
            domain=str(s.get("domain") or ""), code=str(s.get("subject_code") or ""),
        )
        for s in doc.get("subjects") or ()
    ]
    return SubjectIndex(
        profile=str(doc.get("profile") or ""),
        label=LabelStyle.model_validate(doc.get("label") or {}),
        frames={str(k): str(v) for k, v in (doc.get("frames") or {}).items()},
        subjects={d.id: d for d in decls},
        origin=f"packaged:{path}",
    )


def _select(env: Mapping[str, str] | None = None) -> SubjectIndex:
    env = os.environ if env is None else env
    if env.get(PROFILE_ENV):
        return _index_of_profile(load_profile(resolve_profile(env=env)))
    return _from_export(PACKAGED_EXPORT)


_lock = threading.Lock()
_current: SubjectIndex | None = None


def current() -> SubjectIndex:
    """The process's declared subjects (read once, then cached)."""
    global _current
    idx = _current
    if idx is None:
        with _lock:
            if _current is None:
                _current = _select()
            idx = _current
    return idx


def refresh(profile: Profile | str | os.PathLike[str] | None = None) -> SubjectIndex:
    """Drop the cache and read again; ``profile`` (loaded, or its directory)
    instead of the selection rule when given."""
    global _current
    with _lock:
        if profile is None:
            _current = _select()
        elif isinstance(profile, Profile):
            _current = _index_of_profile(profile)
        else:
            _current = _index_of_profile(load_profile(profile))
        return _current


def label_style() -> LabelStyle:
    """The profile's subject label style (``subjects.label``)."""
    return current().label


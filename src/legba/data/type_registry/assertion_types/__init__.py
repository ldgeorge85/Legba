# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``assertion_type`` family: what kinds of held record exist (data-model
lane 10, "the held family").

An assertion (``assertions``, migration ``20261115T1940_held_record``) is
something said about a subject, for a period, with versions, a hash-chained
status log and links to what it rests on. On screen the checked kind is a
"statement"; ``assertion`` is the database name of the family, which will also
hold entered knowledge (lane 11).

Two levels, on purpose (merged recommendation §4.1, item 3):

* a CLOSED list of behaviour classes, in code (:data:`ASSERTION_CLASSES`): a
  class fixes what may establish a record and what it may be used for, so a
  new class is a reviewed code change and a migration (the table holds the
  class list as a CHECK for that reason);
* an OPEN list of types, as declarations here: each type picks one class and
  declares its identifying KEY (the fields whose normalised values, with the
  subject and the type, make the claim key two records are matched on).
  ``assertions.type_id`` holds a declared id; the writer refuses any other
  before SQL, and a container test holds every stored id to this family.

A declaration carries:

``assertion_class``
    One of :data:`ASSERTION_CLASSES`.
``key``
    The identifying fields, from :data:`KEY_FIELDS` (``text`` = the
    normalised claim text; ``topic`` = an absence's normalised topic;
    ``collection`` = the collection an absence is scoped to).
``bases``
    How a record of this type may come to exist: ``derived`` (worked out by a
    run and passed through the gates), ``declared`` (entered by a person or a
    loader, lane 11). ``extracted`` is reserved for the machine-read triples
    and refused by the writer (lane DM-E keeps them in ``facts``).
``desk_row_kinds``
    The contract-v2 desk row kinds that map to this type (the writer's map is
    DERIVED from these; two types may not claim one row kind).

``evidence`` (the common field) is the standing of a record of this type when
something rests on it: ``background`` for everything derived (a held
statement may be a PREMISE, never new evidence for another statement; the
support table's CHECK enforces it).
"""

from __future__ import annotations

from typing import Any

from .. import REQUIRED, FamilySpec, TypeDecl, register_family

__all__ = ["ASSERTION_CLASSES", "BASES", "FAMILY", "KEY_FIELDS"]

#: The behaviour classes (closed, in code). ``statement``: checkable against
#: a quote, may be held when the gates pass and the clean-context verifier
#: answers "supports". ``attributed``: "X said Y", checkable the same way, its
#: speaker named. ``assessment``: the desk's judgement, resting on held
#: statements as premises; never "established". ``absence``: nothing matching
#: a topic was collected from these sources in this window; a record about the
#: collection, never about the world.
ASSERTION_CLASSES: tuple[str, ...] = ("statement", "attributed", "assessment", "absence")
#: ``extracted`` is reserved (the triples stay in ``facts``); the writer refuses it.
BASES: tuple[str, ...] = ("derived", "declared", "extracted")
KEY_FIELDS: tuple[str, ...] = ("text", "topic", "collection")


def _check(decl: TypeDecl) -> list[str]:
    problems: list[str] = []
    cls = decl.attr("assertion_class")
    if cls not in ASSERTION_CLASSES:
        problems.append(f"assertion_class must be one of {list(ASSERTION_CLASSES)}")
    key: Any = decl.attr("key")
    if not isinstance(key, tuple) or not key or not all(k in KEY_FIELDS for k in key):
        problems.append(f"key must be a non-empty tuple of {list(KEY_FIELDS)}")
    bases: Any = decl.attr("bases")
    if not isinstance(bases, tuple) or not bases or not all(b in BASES for b in bases):
        problems.append(f"bases must be a non-empty tuple of {list(BASES)}")
    elif "extracted" in bases:
        problems.append("the 'extracted' basis is reserved: the machine-read triples stay in facts")
    kinds: Any = decl.attr("desk_row_kinds")
    if not isinstance(kinds, tuple) or not all(isinstance(k, str) and k for k in kinds):
        problems.append("desk_row_kinds must be a tuple of row kind names")
    if decl.evidence != "background":
        problems.append("a derived record's standing is 'background': premise, never new evidence")
    return problems


FAMILY = register_family(FamilySpec(
    name="assertion_type",
    doc="what kinds of held record exist (assertions.type_id): each picks a behaviour class and its claim key",
    attrs={"assertion_class": REQUIRED, "key": REQUIRED, "bases": ("derived",), "desk_row_kinds": ()},
    outputs=(None,),
    check=_check,
))

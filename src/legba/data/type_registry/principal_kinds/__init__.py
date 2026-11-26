# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``principal_kind`` family: the kinds of thing that can WRITE a row
("who writes").

A principal is one row in ``principals``: a person (linked to ``users``), a
service, an analyst seat (descriptor id + version), a deterministic rule, or a
credential several people hold (the shared admin token). Its handle is
``<kind>:<name>``; the part before the colon must be one of these declared
ids (``legba.data.principals`` refuses anything else before it reaches SQL,
and a container test holds every stored kind to this family).

A declaration carries:

``name_shape``
    How the name after the colon is formed — one line a person reads
    (``docs/TYPES.md``).
``example``
    An invented example handle.

A new kind is one module here (and its handle builder in
``legba.data.principals``); it is a reviewed change on purpose, because it
changes who the record can say wrote something.
"""

from __future__ import annotations

from .. import REQUIRED, FamilySpec, TypeDecl, register_family

__all__ = ["FAMILY"]


def _check(decl: TypeDecl) -> list[str]:
    problems: list[str] = []
    example = decl.attr("example")
    if not isinstance(example, str) or not example.startswith(f"{decl.id}:"):
        problems.append(f"example must be a handle starting with '{decl.id}:'")
    if not isinstance(decl.attr("name_shape"), str) or not decl.attr("name_shape").strip():
        problems.append("name_shape is required: how the name after the colon is formed")
    return problems


FAMILY = register_family(FamilySpec(
    name="principal_kind",
    doc="the kinds of thing that can write a row (person, service, seat, rule, shared credential)",
    attrs={"name_shape": REQUIRED, "example": REQUIRED},
    outputs=(None,),
    check=_check,
))

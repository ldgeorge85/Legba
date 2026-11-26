# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The ``citation_target`` family: what a finding's sentence may rest on.

A citation row (``citation_support``, migration
``20261115T1828_citations_as_rows``) says "this sentence rests on that record".
Each kind of record it may point at is one declaration here, carrying:

``table``
    Where the record lives (its existence is checked there at write).
``column``
    The typed key column on ``citation_support`` / the list on ``run_handed``
    that holds it (:data:`SUPPORT_COLUMNS`). Two kinds may share a column (a
    finding and, later, a critique both live in ``analyst_outputs``): the
    row's ``target_kind`` says which. A kind needing a column not in the set
    is a migration plus a reviewed edit of :data:`SUPPORT_COLUMNS`.
``key``
    ``uuid`` (joinable) or ``text`` (a kind with no uuid, held in
    ``legacy_key`` and counted, never joined: the 04 note's "kind + id only for
    legacy kinds").
``material``
    True when the record has received bytes behind it (a receipt in
    ``fetch_version_records`` under ``record_kind`` = the id): the writer pins
    the receipt version the citation was read from.
``ref_kinds``
    The ``ref_kind`` values a persisted citation entry uses for ONE record of
    this kind, and ``id_fields`` the entry fields that carry its id, in order.
``member_fields``
    For a grounding block that stands for N records of this kind (the window
    ledger's findings, the register's situations, ...): ``{block ref_kind:
    entry field holding the list}``.
``restates``
    True when a citation of it is the desk's OWN earlier read (never new
    evidence): ``ref_kinds`` / blocks listed here get role ``restates``.

``evidence`` (the common field) is the standing: ``evidence`` for material,
``background`` for anything a desk wrote or a register holds.

DERIVED from this family: the citation-entry resolution in
``legba.data.citation_rows`` and the citation-target table in
``docs/TYPES.md``. Declarations import nothing but the registry.
"""

from __future__ import annotations

from typing import Any, Mapping

from .. import REQUIRED, FamilySpec, TypeDecl, register_family

__all__ = ["FAMILY", "KEYS", "SUPPORT_COLUMNS"]

#: The typed key columns of ``citation_support`` (one non-null per row) — the
#: migration's list. ``tests/data_pkg/test_citation_rows.py`` holds the two equal.
SUPPORT_COLUMNS: tuple[str, ...] = (
    "signal_id", "observation_id", "output_id", "situation_id", "event_id",
    "hypothesis_id", "inquiry_id", "legacy_key",
)
KEYS: tuple[str, ...] = ("uuid", "text")


def _strings(value: Any) -> bool:
    return isinstance(value, tuple) and all(isinstance(v, str) and v for v in value)


def _check(decl: TypeDecl) -> list[str]:
    problems: list[str] = []
    column, key = decl.attr("column"), decl.attr("key")
    if column not in SUPPORT_COLUMNS:
        problems.append(f"column must be one of {list(SUPPORT_COLUMNS)} (a new column is a migration)")
    if key not in KEYS:
        problems.append(f"key must be one of {list(KEYS)}")
    if (key == "text") != (column == "legacy_key"):
        problems.append("a text key lives in legacy_key, and only a text key does")
    if not isinstance(decl.attr("table"), str) or not decl.attr("table"):
        problems.append("table is required")
    for name in ("ref_kinds", "id_fields"):
        if not isinstance(decl.attr(name), tuple) or not (
            decl.attr(name) == () or _strings(decl.attr(name))):
            problems.append(f"{name} must be a tuple of strings")
    members = decl.attr("member_fields")
    if not isinstance(members, Mapping) or not all(
        isinstance(k, str) and isinstance(v, str) for k, v in members.items()):
        problems.append("member_fields must map a block ref_kind to an entry field")
    if not decl.attr("ref_kinds") and not members:
        problems.append("a target no citation entry can name is dead: give ref_kinds or member_fields")
    if decl.attr("ref_kinds") and not decl.attr("id_fields"):
        problems.append("ref_kinds need id_fields (where the entry carries the id)")
    if decl.attr("material") and key != "uuid":
        problems.append("material records are uuid-keyed (fetch_version_records.record_id)")
    if decl.evidence not in ("evidence", "background"):
        problems.append("evidence must be 'evidence' (material) or 'background'")
    elif (decl.evidence == "evidence") != bool(decl.attr("material")):
        problems.append("only material records stand as evidence")
    restates = decl.attr("restates")
    if not isinstance(restates, tuple) or not all(
        r in decl.attr("ref_kinds") or r in members for r in restates):
        problems.append("restates names ref_kinds or member blocks of this target")
    return problems


FAMILY = register_family(FamilySpec(
    name="citation_target",
    doc="what a finding's sentence may rest on (citation_support target kinds)",
    attrs={
        "table": REQUIRED,
        "column": REQUIRED,
        "key": "uuid",
        "material": False,
        "ref_kinds": (),
        "id_fields": ("ref_id",),
        "member_fields": {},
        "restates": (),
    },
    outputs=(None,),
    check=_check,
))

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Principals — who wrote a row.

THREE "WHO"s, kept apart: who WROTE the row
(a principal, this module), where the CONTENT came from (an origin and its
origin group, :mod:`legba.data.origins`), and who is SPEAKING inside it (the
claimant). This module is only the first.

A PRINCIPAL is one row in ``principals`` with a stable uuid and a unique,
immutable HANDLE ``<kind>:<name>``. The kind is a ``principal_kind``
declaration in the type registry (``type_registry/principal_kinds/``):

==========  =============================================  =====================
kind        name                                           example (invented)
==========  =============================================  =====================
``user``    the person's ``users.id``                      ``user:0b6c…0001``
``service`` registry / runtime / worker / console; fleet   ``service:runtime``
``agent``   ``<analyst descriptor id>@<version>``          ``agent:desk_x@3f9a``
``rule``    a deterministic handler id or code path        ``rule:collection_gap``
``shared``  ``admin-token`` / ``dev``                      ``shared:admin-token``
==========  =============================================  =====================

CREATED ON FIRST USE, NEVER BY HAND. A write names its principal by HANDLE
and the database function ``legba_principal(handle)`` (migration
``20261115T1827_principals_origins_written_by``) upserts the row and returns
its id inside the same statement: ``INSERT … written_by …
VALUES (…, legba_principal($9))``. No write site makes a second round trip,
and no list of principals is ever seeded. The handle is validated HERE, in
Python, before it reaches SQL: an undeclared kind or a malformed name raises
:class:`PrincipalError` (the database checks only the handle's shape).

SIGN-IN OFF = EXACTLY AS BEFORE. With ``LEGBA_AUTH_SIGNIN`` off every person
presents the one shared admin token, so every person's write names
``shared:admin-token`` — the credential, never a person and never "the
operator". Author columns that a request body used to fill keep doing so
(``route_roles.recorded_author``); ``written_by`` is the server's stamp beside
them.

A LEAF: stdlib + the stdlib-only type registry. Imported by the registry,
the runtime handlers and the scripts.
"""

from __future__ import annotations

import re
from typing import Any
from uuid import UUID

__all__ = [
    "PrincipalError",
    "SERVICE_FLEET",
    "SHARED_ADMIN_TOKEN",
    "SHARED_DEV",
    "SQL_FN",
    "declared_kinds",
    "handle_for_actor",
    "output_writer_handle",
    "parse_handle",
    "resolve",
    "rule_handle",
    "seat_handle",
    "service_handle",
    "user_handle",
    "validate_handle",
]

#: The SQL function every write site calls on its handle parameter.
SQL_FN = "legba_principal"

#: The shared admin credential (``LEGBA_REGISTRY_API_TOKEN``): with sign-in off
#: every person's write; with it on, a break-glass use.
SHARED_ADMIN_TOKEN = "shared:admin-token"
#: A development-mode registry (``LEGBA_DEV_MODE=1``, no credential at all).
SHARED_DEV = "shared:dev"
#: The shared service token (``LEGBA_REGISTRY_SERVICE_TOKEN``) — any fleet
#: process may present it; a named service presents its own.
SERVICE_FLEET = "service:fleet"

#: A handle: a declared kind, a colon, a name of printable non-space ASCII
#: (descriptor ids, versions, uuids and handler ids all fit), at most 255
#: characters (the database checks the same shape; its regex engine caps a
#: repetition count at 255).
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/+-]{0,254}$")
_UUID = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
_SERVICE_NAME = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


class PrincipalError(ValueError):
    """A handle that names no declared kind, or a malformed name."""


def declared_kinds() -> tuple[str, ...]:
    """The ``principal_kind`` ids the type registry declares."""
    from .type_registry import default_registry

    return default_registry().ids("principal_kind")


def parse_handle(handle: str) -> tuple[str, str]:
    """``(kind, name)`` of a handle (no validation of the kind)."""
    kind, sep, name = (handle or "").partition(":")
    if not sep:
        raise PrincipalError(f"a principal handle is <kind>:<name>, not {handle!r}")
    return kind, name


def validate_handle(handle: str) -> str:
    """``handle`` unchanged if it names a declared kind and a well-formed name;
    :class:`PrincipalError` otherwise."""
    kind, name = parse_handle(handle)
    kinds = declared_kinds()
    if kind not in kinds:
        raise PrincipalError(f"principal kind {kind!r} is not declared (declared: {list(kinds)})")
    if not _NAME.match(name):
        raise PrincipalError(f"principal name {name!r} is malformed")
    if kind == "user" and not _UUID.match(name):
        raise PrincipalError(f"a user principal is user:<users.id>, not {handle!r}")
    if kind == "agent" and "@" not in name:
        raise PrincipalError(f"an agent principal is agent:<descriptor id>@<version>, not {handle!r}")
    return handle


def user_handle(user_id: Any) -> str:
    return validate_handle(f"user:{str(user_id).lower()}")


def service_handle(name: str) -> str:
    if not _SERVICE_NAME.match(name or ""):
        raise PrincipalError(f"service name {name!r} is malformed")
    return validate_handle(f"service:{name}")


def seat_handle(analyst_id: str | None, analyst_version: Any) -> str | None:
    """An analyst seat that calls a model: ``agent:<id>@<version>``.

    ``None`` when the run carries no id or no version (a hand-built context):
    the row then names no writer rather than a guessed one."""
    version = str(analyst_version or "").strip()
    if not analyst_id or not version:
        return None
    return validate_handle(f"agent:{analyst_id}@{version}")


def rule_handle(name: str) -> str:
    """Deterministic code: a handler id (= its analyst id) or a code path."""
    return validate_handle(f"rule:{name}")


def output_writer_handle(analyst_id: str | None, analyst_version: Any) -> str | None:
    """Who wrote an analyst output (and the rows written beside it): the
    deterministic handler ``rule:<id>`` when the analyst id is a declared
    ``deterministic_handler`` (a handler's id is its analyst id), else the
    seat ``agent:<id>@<version>``; ``None`` when neither can be named."""
    if not analyst_id:
        return None
    from .type_registry import default_registry

    if analyst_id in default_registry().ids("deterministic_handler"):
        return rule_handle(analyst_id)
    return seat_handle(analyst_id, analyst_version)


def handle_for_actor(actor: Any) -> str | None:
    """The principal handle for a SERVER-STAMPED audit actor, or ``None``.

    The registry's gates stamp ``principal_actor`` (``_deps.py``) and the
    session middleware ``user:<id>`` (``session_auth.py``); this maps each to
    its principal:

    * ``user:<uuid>``                         -> the person;
    * ``service:<name>``                      -> that service (sign-in on);
    * ``service:bearer:<sha12>`` / ``service`` -> ``service:fleet``;
    * ``operator:bearer:<sha12>``, ``admin-token:bearer:<sha12>``,
      ``operator`` (the subject an un-hashed operator stamp carries)
                                              -> ``shared:admin-token``;
    * ``anonymous`` / bare ``bearer:<sha12>`` (development mode) -> ``shared:dev``;
    * anything else (``None``, ``unresolved``, a by-hand name a request body
      claimed) -> ``None``: never guess a writer.
    """
    if not isinstance(actor, str) or not actor.strip():
        return None
    actor = actor.strip()
    head, _, rest = actor.partition(":")
    if head == "user" and _UUID.match(rest.lower()):
        return user_handle(rest)
    if head == "service":
        if not rest or rest.startswith("bearer:"):
            return SERVICE_FLEET
        if _SERVICE_NAME.match(rest):
            return service_handle(rest)
        return None
    if head in ("operator", "admin-token") and (not rest or rest.startswith("bearer:")):
        return SHARED_ADMIN_TOKEN
    if actor == "anonymous" or head == "bearer":
        return SHARED_DEV
    return None


async def resolve(conn: Any, handle: str | None) -> UUID | None:
    """The principal id for ``handle`` (created on first use), or ``None``.

    For a reader that needs the id itself; write sites call
    ``legba_principal($n)`` inside their own statement instead."""
    if handle is None:
        return None
    return await conn.fetchval(f"SELECT {SQL_FN}($1)", validate_handle(handle))

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The per-family half of :mod:`legba.data.registry.descriptor`.

Extracted from ``descriptor.py`` when the ``collection`` family landed
(Program 7g-1). Two cohesive units live here, both of which were the only
places in the registry that had to know what family they were looking at:

* :func:`insert_descriptor_row` — the family-specific INSERT. Each family's
  table carries a slightly different column set (``analyst_descriptors`` has
  ``kind``/``type_signature`` and no ``abstraction_level``;
  ``source_descriptors`` has both; ``collection_descriptors`` adds
  ``origin_class``/``licence_class``/``collection_version`` so a reader can
  find a holding without opening its body) and the INSERT is written out per
  family rather than assembled, so the column list is readable beside the
  migration that created it.

* The LIFECYCLE state machines. Four of the five families share
  ``LifecycleState`` (``draft`` → ``configured`` → ``active`` → ``paused`` →
  ``retired``). A ``collection`` does not: a bounded holding of the past is
  never "paused" and never "active" — it is drafted, reviewed by the
  operator, loaded once, and one day superseded. :func:`state_machine_for`
  is the one place that mapping lives, so a family cannot be added with a
  lifecycle nobody validates.

The module takes the family as a plain ``str`` (``'target'``, ``'analyst'``,
``'source'``, ``'action_pack'``, ``'collection'``) rather than importing the
``Family`` enum, which lives in ``descriptor.py`` and would be a cycle.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID

import asyncpg
from pydantic import BaseModel

from ..schemas import (
    ActionPack,
    AnalystDescriptor,
    LifecycleState,
    SourceDescriptor,
    TargetDescriptor,
)
from ..schemas.collection import (
    COLLECTION_TRANSITIONS,
    CollectionDescriptor,
    CollectionState,
)
from ..schemas.lifecycle import ALLOWED_TRANSITIONS

#: Deploy marker — the rolling deploy greps for this exact string.
DESCRIPTOR_FAMILIES_VERSION = "2026-09/7g-1"

__all__ = [
    "DESCRIPTOR_FAMILIES_VERSION",
    "insert_descriptor_row",
    "state_machine_for",
    "terminal_state_for",
]


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def state_machine_for(
    family_value: str,
) -> tuple[type[Enum], dict[Any, set[Any]]]:
    """``(state_enum, allowed_transitions)`` for one descriptor family.

    Called wherever the registry has to read a persisted ``state`` string
    back into an enum member: parsing ``'reviewed'`` as a
    :class:`~legba.data.schemas.lifecycle.LifecycleState` raises
    ``ValueError``, which is exactly the bug a shared state machine invites
    the moment a family stops sharing it.
    """
    if family_value == "collection":
        return CollectionState, COLLECTION_TRANSITIONS  # type: ignore[return-value]
    return LifecycleState, ALLOWED_TRANSITIONS  # type: ignore[return-value]


def terminal_state_for(family_value: str) -> Enum:
    """The state ``retire()`` moves a head row to, per family.

    ``retired`` for the four shared-lifecycle families; ``superseded`` for a
    collection, whose terminal state means "a newer version of this manifest
    is the one to read" rather than "this is switched off".
    """
    if family_value == "collection":
        return CollectionState.SUPERSEDED
    return LifecycleState.RETIRED


# ---------------------------------------------------------------------------
# The family-specific INSERT
# ---------------------------------------------------------------------------


def _jsonify(value: Any) -> Any:
    """``json.dumps`` fallback for the descriptor body.

    Moved here with the INSERTs it serves — it had no other caller in
    ``descriptor.py``. Raising on an unknown type is deliberate: a body that
    cannot be serialized must fail the write, never land half-encoded.
    """
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, set):
        return sorted(value)
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    raise TypeError(f"cannot serialize {value!r}")


async def insert_descriptor_row(
    conn: asyncpg.Connection,
    family_value: str,
    descriptor: Any,
    version: str,
    body: dict[str, Any],
) -> None:
    """INSERT one descriptor row into its family's table as the new head."""
    now = datetime.now(tz=timezone.utc)
    if family_value == "target":
        assert isinstance(descriptor, TargetDescriptor)
        await conn.execute(
            """
            INSERT INTO target_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, state, owner, name, body,
                 inherits, created_at, retire_after)
            VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb, $9, $10, $11)
            """,
            descriptor.identity.id,
            version,
            descriptor.identity.schema_uri,
            descriptor.identity.abstraction_level.value,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            json.dumps(body, default=_jsonify),
            list(descriptor.identity.inherits),
            now,
            descriptor.identity.retire_after,
        )
    elif family_value == "analyst":
        assert isinstance(descriptor, AnalystDescriptor)
        await conn.execute(
            """
            INSERT INTO analyst_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 kind, state, owner, name, body,
                 type_signature, inherits, created_at)
            VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb,
                    $9::jsonb, $10, $11)
            """,
            descriptor.identity.id,
            version,
            descriptor.identity.schema_uri,
            descriptor.identity.kind,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            json.dumps(body, default=_jsonify),
            json.dumps(
                descriptor.identity.type_signature.model_dump(mode="json"),
                default=_jsonify,
            ),
            list(descriptor.identity.inherits),
            now,
        )
    elif family_value == "source":
        assert isinstance(descriptor, SourceDescriptor)
        await conn.execute(
            """
            INSERT INTO source_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, kind, state, owner, name, body,
                 inherits, created_at, retire_after)
            VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8, $9::jsonb,
                    $10, $11, $12)
            """,
            descriptor.identity.id,
            version,
            descriptor.identity.schema_uri,
            descriptor.identity.abstraction_level.value,
            descriptor.identity.kind,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            json.dumps(body, default=_jsonify),
            list(descriptor.identity.inherits),
            now,
            descriptor.identity.retire_after,
        )
    elif family_value == "collection":
        assert isinstance(descriptor, CollectionDescriptor)
        # `collection_version` is the hash of the MANIFEST, not of the
        # descriptor: re-approving a licence line must not invalidate a load
        # that already happened. It is the key `collection_loads` joins on.
        await conn.execute(
            """
            INSERT INTO collection_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, state, owner, name, body,
                 inherits, created_at, retire_after,
                 origin_shape, origin_class, licence_class,
                 collection_version)
            VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb,
                    $9, $10, $11, $12, $13, $14, $15)
            """,
            descriptor.identity.id,
            version,
            descriptor.identity.schema_uri,
            descriptor.identity.abstraction_level.value,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            json.dumps(body, default=_jsonify),
            list(descriptor.identity.inherits),
            now,
            descriptor.identity.retire_after,
            descriptor.origin_shape,
            descriptor.origin_class,
            descriptor.licence_class,
            descriptor.manifest_hash(),
        )
    elif family_value == "action_pack":
        assert isinstance(descriptor, ActionPack)
        await conn.execute(
            """
            INSERT INTO action_pack_descriptors
                (descriptor_id, version, schema_uri, is_head,
                 abstraction_level, state, owner, name, body,
                 inherits, created_at, retire_after)
            VALUES ($1, $2, $3, true, $4, $5, $6, $7, $8::jsonb,
                    $9, $10, $11)
            """,
            descriptor.identity.id,
            version,
            descriptor.identity.schema_uri,
            descriptor.identity.abstraction_level.value,
            descriptor.identity.state.value,
            descriptor.identity.owner,
            descriptor.identity.name,
            json.dumps(body, default=_jsonify),
            list(descriptor.identity.inherits),
            now,
            descriptor.identity.retire_after,
        )
    else:  # pragma: no cover - Family is a closed enum upstream
        raise ValueError(f"no INSERT routing for descriptor family {family_value!r}")

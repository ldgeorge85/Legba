# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""legba.data.seed._base — the curated/authoritative seeding ROOTS (flavor b).

This module defines the *framework* primitives that every seed adapter shares,
mirroring how ``data/sources/`` source handlers share a contract — except seed
adapters write to the **knowledge layer** (``facts`` / ``nexuses`` /
``entity_profiles``) instead of ``signals``. See planning/SEEDING_SKETCH.md.

Three concerns live here:

  1. The typed seed payloads an adapter's ``map()`` yields:
     :class:`SeedEntity`, :class:`SeedFact`, :class:`SeedNexus` — small,
     adapter-agnostic dataclasses carrying real ``valid_from`` /
     ``valid_until`` from the source.
  2. The :class:`SeedSource` ``Protocol`` an adapter implements
     (``name`` / ``source_type`` / async ``fetch`` / ``map``).
  3. :class:`SeedContext` — the per-run handle (DB pool/conn + dry-run flag)
     the driver passes to ``fetch``.

The DRIVER (``_driver.py``) is the part that actually resolves entities +
writes via ``write_fact`` / ``write_nexus`` + records the ``seed_batches``
row. Adapters stay pure data: fetch raw → map to typed payloads. RELATIONAL
seeds (alliances/hostility/sanctions) map DIRECTLY to typed signed
:class:`SeedNexus` payloads — no LLM reifier (operator decision); the reifier
is only ever for free-text seeds (a later adapter).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Protocol, runtime_checkable


# ---------------------------------------------------------------------------
# Typed seed payloads (what an adapter's map() yields)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SeedEntity:
    """A canonical entity to fold into ``entity_profiles`` (deduped by
    ``lower(canonical_name)``).

    Adapters rarely need to emit these explicitly — the driver auto-resolves
    every fact subject/object and nexus endpoint against ``entity_profiles``
    by canonical name. Emit a :class:`SeedEntity` only to enrich an entity
    with a class / geo the facts alone wouldn't carry (e.g. a country's ISO
    code + capital coords).
    """

    canonical_name: str
    entity_class: str = "entity"           # country | person | organization | …
    geo_lat: float | None = None
    geo_lon: float | None = None
    geo_country: str | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SeedFact:
    """One ``(subject, predicate, value)`` attribute triple → a ``facts`` row.

    ``valid_from`` is REQUIRED (curated seeds are temporally honest — a leader
    fact carries the inauguration date); ``valid_until`` is optional (an open
    term is NULL). The driver stamps ``source_type`` + the ``seed_batch_id`` —
    the adapter never sets those.
    """

    subject: str
    predicate: str
    value: str
    valid_from: datetime
    valid_until: datetime | None = None
    confidence: float = 0.95
    geo_lat: float | None = None
    geo_lon: float | None = None
    data: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SeedNexus:
    """One reified, typed, SIGNED relationship → a ``nexuses`` row.

    Relational seeds (alliance/hostility/sanctions) map directly here:
    ``rel_type`` is the canonical predicate (``MemberOf`` / ``AlliedWith`` /
    ``HostileTo`` / ``Sanctions``); ``polarity`` is the structural-balance sign
    (+1 supportive / -1 antagonistic / 0 neutral). No LLM typing needed — the
    adapter already knows the sign. ``intermediary`` is ``None`` for a direct
    A→B relationship.
    """

    subject: str
    object: str
    rel_type: str
    polarity: int
    valid_from: datetime
    valid_until: datetime | None = None
    intermediary: str | None = None
    label: str = ""
    intent: str = ""
    channel: str = "direct"
    confidence: float = 0.95
    data: dict[str, Any] = field(default_factory=dict)


SeedPayload = SeedEntity | SeedFact | SeedNexus


# ---------------------------------------------------------------------------
# Per-run context + the adapter protocol
# ---------------------------------------------------------------------------


@dataclass
class SeedContext:
    """Per-run handle the driver hands an adapter's ``fetch``.

    ``pool`` is an asyncpg pool (or any object exposing ``acquire()``); some
    adapters (curated YAML) never touch it, others (a future Wikidata SPARQL
    adapter) might want a side connection. ``dry_run`` lets ``fetch`` skip
    network calls it would otherwise make.
    """

    pool: Any | None = None
    dry_run: bool = False
    options: dict[str, Any] = field(default_factory=dict)


@runtime_checkable
class SeedSource(Protocol):
    """A curated/authoritative seed adapter.

    Mirrors the ``data/sources`` handler shape but targets the knowledge
    layer. An adapter is pure data: ``fetch`` pulls raw records (from a curated
    YAML file, a SPARQL endpoint, a CSV, …); ``map`` turns them into typed
    :data:`SeedPayload` objects. The driver owns entity resolution, the write,
    the marker, and the batch row.
    """

    #: Stable adapter id used on the CLI (``--source <name>``) + recorded on
    #: the ``seed_batches`` row.
    name: str

    #: Provenance class stamped on every row this adapter writes — ``'seed'``
    #: (curated/authoritative) or ``'backfill'`` (bulk historical).
    source_type: str

    async def fetch(self, ctx: SeedContext) -> Any:
        """Pull the raw records (any shape ``map`` understands)."""
        ...

    def map(self, raw: Any) -> Iterable[SeedPayload]:
        """Turn raw records into typed seed payloads."""
        ...


__all__ = [
    "SeedEntity",
    "SeedFact",
    "SeedNexus",
    "SeedPayload",
    "SeedContext",
    "SeedSource",
]

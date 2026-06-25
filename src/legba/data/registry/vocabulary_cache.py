# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime-extensible vocabulary cache (L-101 §8 / L-110 injection).

The vendored pydantic descriptor schemas (`legba.data.schemas`) accept any
typed-string value matching the regex shape (`entity_class` =
`lowercase_snake`, `relationship_type` = `PascalCase`). The *closed-set*
check ("does the value actually exist in the registry?") was deferred from
L-001 to L-110 — that's what this module supplies.

Two integration points:

  1. Snapshot loader. Pulls live rows from `vocabulary_entries`, builds a
     `VocabularyRegistry`, exposes a fast `contains(family, value)` lookup.

  2. NATS subscription. When a `vocabulary.updated.*` message arrives, the
     cache reloads from Postgres. The descriptor registry calls into this
     for every validate.

The validator function returned by `make_validator(family)` is the
"injected" closure that pydantic doesn't carry natively in
`TargetScope.entity_classes` — the descriptor registry runs it manually
after pydantic-level parsing, before content-hashing.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Any, Callable

import asyncpg

from ..schemas.vocabulary import VocabularyEntry, VocabularyRegistry
from .errors import UnknownVocabularyValue
from .events import VOCABULARY_UPDATED_TOPIC

logger = logging.getLogger(__name__)


class VocabularyCache:
    """In-memory snapshot of `vocabulary_entries`.

    Thread-safety: a single `asyncio.Lock` guards refresh. Reads against the
    snapshot dict don't take the lock — Python dict reads are atomic and
    the snapshot is replaced wholesale on refresh.

    Lifecycle:
        cache = VocabularyCache(pg_store)
        await cache.refresh()                # initial load
        await cache.start_subscription(nats) # optional auto-refresh
        ...
        await cache.stop_subscription()
    """

    def __init__(
        self,
        pg_store: Any,
        *,
        seed_aliases: bool = True,
    ):
        self._pg = pg_store
        self._seed_aliases = seed_aliases
        self._registry = VocabularyRegistry(entries=[])
        self._values_by_family: dict[str, set[str]] = {}
        # Alias -> canonical value, scoped per family.
        self._aliases: dict[str, dict[str, str]] = {}
        self._lock = asyncio.Lock()
        self._sub = None  # NATS subscription handle
        self._nats = None  # legba.data.nats.NatsStore (or None)
        # Optional post-refresh hooks (L-241): callers that want to mirror
        # cache contents into other registries (e.g. ANALYST_KIND_REGISTRY)
        # register a no-arg callable here that fires after every refresh
        # the NATS subscription drives.
        self._post_refresh_hooks: list[Callable[[], None]] = []

    # ------------------------------------------------------------------
    # Snapshot accessors (read-only)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> VocabularyRegistry:
        return self._registry

    def values(self, family: str) -> set[str]:
        return self._values_by_family.get(family, set())

    def families(self) -> list[str]:
        return sorted(self._values_by_family.keys())

    def contains(self, family: str, value: str) -> bool:
        """Returns True if `value` is a known canonical value or an alias
        for one. Aliases are resolved to their canonical before the check."""
        canonical = self.resolve_alias(family, value)
        return canonical in self._values_by_family.get(family, set())

    def resolve_alias(self, family: str, value: str) -> str:
        return self._aliases.get(family, {}).get(value, value)

    # ------------------------------------------------------------------
    # Refresh
    # ------------------------------------------------------------------

    async def refresh(self) -> int:
        """Reload the snapshot from `vocabulary_entries`. Returns the row
        count."""
        async with self._lock:
            async with self._pg.acquire() as conn:
                rows = await conn.fetch(
                    """
                    SELECT family, value, schema_uri, introduced, deprecated,
                           notes, aliases, parent
                    FROM vocabulary_entries
                    """
                )
            entries: list[VocabularyEntry] = []
            values_by_family: dict[str, set[str]] = {}
            aliases: dict[str, dict[str, str]] = {}
            for row in rows:
                entry = _row_to_entry(row)
                entries.append(entry)
                if entry.deprecated is None:
                    values_by_family.setdefault(entry.family, set()).add(entry.value)
                    if self._seed_aliases:
                        per_family = aliases.setdefault(entry.family, {})
                        for alias in entry.aliases:
                            per_family[alias] = entry.value
            self._registry = VocabularyRegistry(entries=entries)
            self._values_by_family = values_by_family
            self._aliases = aliases
            logger.debug(
                "vocabulary cache refreshed: %d entries across families %s",
                len(entries),
                sorted(values_by_family.keys()),
            )
            return len(entries)

    # ------------------------------------------------------------------
    # NATS-driven invalidation
    # ------------------------------------------------------------------

    async def start_subscription(self, nats_store: Any) -> None:
        """Subscribe to `vocabulary.updated.>` and refresh on each message.

        `nats_store` is a `legba.data.nats.NatsStore` (already connected).
        We use the core NATS subscription rather than JetStream — this is a
        fire-and-forget cache-invalidation signal, not a durable queue.
        """
        if self._sub is not None:
            return
        self._nats = nats_store

        async def _handler(_msg: Any) -> None:
            try:
                count = await self.refresh()
                logger.info("vocabulary cache reloaded: %d entries", count)
                self._fire_post_refresh_hooks()
            except Exception as exc:
                logger.error("vocabulary cache refresh failed: %s", exc)

        self._sub = await nats_store.nc.subscribe(
            f"{VOCABULARY_UPDATED_TOPIC}.>", cb=_handler
        )

    async def start_subscription_hook(self, hook: Callable[[], None]) -> None:
        """Register a no-arg callable to fire after every NATS-driven
        refresh. Idempotent — duplicate registration is silently dropped."""
        if hook not in self._post_refresh_hooks:
            self._post_refresh_hooks.append(hook)

    def _fire_post_refresh_hooks(self) -> None:
        for hook in list(self._post_refresh_hooks):
            try:
                hook()
            except Exception as exc:
                logger.warning("vocabulary post-refresh hook failed: %s", exc)

    async def stop_subscription(self) -> None:
        if self._sub is not None:
            try:
                await self._sub.unsubscribe()
            except Exception:
                pass
            self._sub = None
            self._nats = None

    # ------------------------------------------------------------------
    # Validator factory
    # ------------------------------------------------------------------

    def make_validator(self, family: str) -> Callable[[list[str]], list[str]]:
        """Return a callable that validates a list of values against the
        live cache for `family`. Unknown values raise
        `UnknownVocabularyValue`.

        Caller passes the *raw* values; aliases resolve transparently and
        the canonical form is returned. Per L-101 §8: silently dropping or
        accepting unknown values is wrong; the descriptor registry's caller
        wraps this and routes the payload to the dead-letter on failure.
        """
        def _check(values: list[str]) -> list[str]:
            resolved: list[str] = []
            unknown: list[str] = []
            known = self._values_by_family.get(family, set())
            alias_map = self._aliases.get(family, {})
            for v in values:
                canon = alias_map.get(v, v)
                if canon in known:
                    resolved.append(canon)
                else:
                    unknown.append(v)
            if unknown:
                raise UnknownVocabularyValue(family, unknown)
            return resolved
        return _check


# ---------------------------------------------------------------------------
# Row → VocabularyEntry helper
# ---------------------------------------------------------------------------


def _row_to_entry(row: asyncpg.Record) -> VocabularyEntry:
    schema_uri = row["schema_uri"] or "legba/vocabulary/1.0.0"
    # The DB seed defaults to the Iglu form; pydantic wants the bare form.
    if schema_uri.startswith("iglu:"):
        # Map e.g. 'iglu:legba/vocabulary/jsonschema/1-0-0' -> 'legba/vocabulary/1.0.0'.
        try:
            tail = schema_uri.split("/jsonschema/", 1)[1]
            major, minor, patch = tail.split("-")
            schema_uri = f"legba/vocabulary/{major}.{minor}.{patch}"
        except Exception:
            schema_uri = "legba/vocabulary/1.0.0"
    return VocabularyEntry(
        family=row["family"],
        value=row["value"],
        schema_uri=schema_uri,
        introduced=row["introduced"] or datetime.fromtimestamp(0),
        deprecated=row["deprecated"],
        notes=row["notes"],
        aliases=list(row["aliases"] or []),
        parent=row["parent"],
    )

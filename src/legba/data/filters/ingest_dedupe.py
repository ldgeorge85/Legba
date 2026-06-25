# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Source-side ingest dedupe — tiers 1 (URL) + 2 (content-hash), alias/canonical.

P-02 / P-09 at INGEST. The batch :mod:`cross_source_dedup` analyst sweeps the
whole pool on a cadence; this module is its cheap, deterministic ingest-time
counterpart. As each raw signal lands at the source, we look it up against the
shared ``signals`` pool by two cheap, exact keys — canonical-URL hash (tier 1)
then content hash (tier 2) — and, on a hit, **link** the new raw row to the
matched row's canonical instead of letting a duplicate float free until the next
analyst run.

Alias / canonical model — NEVER destructive collapse
----------------------------------------------------

This is the same contract the :mod:`cross_source_dedup` analyst honours:

  * Every raw row is **kept**. Dedupe writes a ``signal_aliases`` link + sets
    the alias row's ``signals.canonical_signal_id`` — it never deletes or
    merges a row.
  * A ``canonical_only`` subscription (``canonical_signal_id IS NULL OR
    canonical_signal_id = id``) then sees exactly one row per duplicate set:
    the canonical. Aliases (``canonical_signal_id`` pointing elsewhere) are
    filtered out at delivery.
  * The canonical of an existing match is resolved **transitively**: if the
    matched row is itself an alias of some earlier canonical, the new row is
    linked to that earlier canonical (not to an alias), so the canonical of a
    duplicate set is always a true self-canonical / NULL root.

Why Postgres, not Redis
-----------------------

The target-side :class:`legba.data.filters.dedupe.Dedupe4TierHandler` is a
per-target *marker* (Redis-keyed, sets ``payload.duplicate_of``). Source-side
ingest dedupe is *cross-source* and *durable*: the canonical link is a column on
the shared signal row, so the lookup is a query over the ``signals`` table — the
same source of truth the analyst and the ``canonical_only`` subscription read.
We reuse the handler's static canonicalisation (``canonical_url`` /
``normalized_content``) so the ingest hash and the analyst/target hashes stay
byte-identical.

Wiring
------

The engine is constructed from a source descriptor's
``pipeline.ingestion_filters`` (the ``dedupe_tier_1`` / ``dedupe_tier_2``
stages) via :func:`ingest_dedupe_from_stages`. The :class:`SourceCore` runs it
**after** the raw row is written (the alias link needs the row to exist) inside
the same connection, so the link lands in the same short transaction as the
insert.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import UUID

from .dedupe import Dedupe4TierHandler
from ..sources._contract import Signal

logger = logging.getLogger(__name__)


# Tier kinds recognised in ``pipeline.ingestion_filters``. Tiers 3/4 are NOT
# ingest-side (they need Qdrant / a temporal window and live in the periodic
# analyst + the target-side marker); the ingest path is the two cheap exact
# tiers only.
TIER_1_URL = "dedupe_tier_1"
TIER_2_CONTENT = "dedupe_tier_2"
_INGEST_TIER_KINDS = frozenset({TIER_1_URL, TIER_2_CONTENT})

# Reason tags written into signal_aliases.reason (match the analyst's vocabulary
# where they overlap so the link table reads consistently across producers).
_REASON_URL = "ingest_url"
_REASON_CONTENT = "content_hash"

# Exact-key matches are certain → score 1.0 (same convention as the analyst).
_EXACT_SCORE = 1.0


@dataclass
class IngestDedupeResult:
    """Outcome of an ingest-dedupe resolution for one signal."""

    is_duplicate: bool = False
    tier: int = 0                      # 1 (url) | 2 (content) | 0 (miss)
    reason: str = ""                   # signal_aliases.reason on a hit
    canonical_signal_id: UUID | None = None
    matched_signal_id: UUID | None = None


@dataclass
class IngestDedupe:
    """Source-side ingest dedupe over the shared ``signals`` pool.

    Stateless apart from its tier config + ``produced_by`` tag; the entire
    duplicate state lives in the ``signals`` / ``signal_aliases`` tables. One
    instance per source actor; safe to reuse across pulls.

    Parameters
    ----------
    tiers:
        Active tier numbers, a subset of ``{1, 2}``. Tier 1 = canonical-URL
        hash; tier 2 = normalised content hash. Order is fixed cheapest-first
        (1 then 2) regardless of the input order.
    produced_by:
        Stamped onto ``signal_aliases.produced_by`` so an ingest-linked alias
        is attributable (distinct from the analyst's own produced_by).
    owner_tenant:
        Tenancy pin — lookups are scoped to this tenant so two tenants' pools
        never cross-link. ``None`` (rare) scans all tenants.
    """

    tiers: frozenset[int] = field(default_factory=lambda: frozenset({1, 2}))
    produced_by: str = "ingest_dedupe"
    owner_tenant: str | None = None

    def is_tier_active(self, tier: int) -> bool:
        return tier in self.tiers

    # -- hashing (shared with the target-side handler for byte-parity) --------

    @staticmethod
    def url_hash(signal: Signal) -> str | None:
        """Canonical-URL SHA-256 for tier 1, or ``None`` if no URL is present.

        Reuses :meth:`Dedupe4TierHandler._canonical_url_hash` so the ingest
        hash matches the target-side tier-1 hash exactly.
        """
        # The static-ish helper reads ``signal.canonical_url`` + the usual
        # payload url/link/source_url fallbacks. It's an instance method only
        # because it reaches ``self.canonical_url`` (a staticmethod); call it
        # unbound with a throwaway via the staticmethod directly.
        url = (
            signal.canonical_url
            or _payload_str(signal, "url")
            or _payload_str(signal, "link")
            or _payload_str(signal, "source_url")
        )
        if not url:
            return None
        import hashlib

        canon = Dedupe4TierHandler.canonical_url(url)
        if not canon:
            return None
        return hashlib.sha256(canon.encode("utf-8")).hexdigest()

    @staticmethod
    def content_hash(signal: Signal) -> str:
        """Tier-2 key: prefer the baseline-stamped ``signal.content_hash``.

        The per-source baseline (``_enrich_structured``) always stamps
        ``content_hash`` (hash of canonical_url + payload title) as a backstop.
        When that's present we use it verbatim so the ingest tier-2 key matches
        the column the ``cross_source_dedup`` analyst groups on. If a source set
        a richer ``content_hash`` itself, that wins too. Only when the column is
        empty do we fall back to hashing the normalised body — keeping the
        ingest path self-sufficient even for a source that skipped the baseline.
        """
        if signal.content_hash:
            return signal.content_hash
        import hashlib

        text = Dedupe4TierHandler.normalized_content(signal)
        if not text:
            return ""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    # -- resolution -----------------------------------------------------------

    async def resolve(self, conn: Any, signal: Signal) -> IngestDedupeResult:
        """Find an existing canonical this signal should alias to.

        Looks up tier 1 (URL) then tier 2 (content) against the ``signals``
        pool, EXCLUDING ``signal``'s own row (it may already be inserted). On a
        hit, returns the *transitively resolved* canonical of the matched row.
        Read-only — writing the alias link is :meth:`link_alias`'s job, run by
        the caller after the raw row exists.
        """
        if 1 in self.tiers:
            uh = self.url_hash(signal)
            if uh is not None:
                matched = await self._lookup_by_url_hash(conn, signal, uh)
                if matched is not None:
                    return self._hit(matched, tier=1, reason=_REASON_URL)

        if 2 in self.tiers:
            ch = self.content_hash(signal)
            if ch:
                matched = await self._lookup_by_content_hash(conn, signal, ch)
                if matched is not None:
                    return self._hit(matched, tier=2, reason=_REASON_CONTENT)

        return IngestDedupeResult(is_duplicate=False)

    async def apply(self, conn: Any, signal: Signal) -> IngestDedupeResult:
        """Resolve + (on a hit) link the alias, all in ``conn``.

        Returns the result. On a hit the row's ``canonical_signal_id`` is set,
        the canonical row is stamped self-canonical, and a ``signal_aliases``
        link is written (idempotent). NEVER deletes a row. ``signal`` is
        mutated in place so the caller's in-memory copy reflects the new
        ``canonical_signal_id``.
        """
        result = await self.resolve(conn, signal)
        if not result.is_duplicate or result.canonical_signal_id is None:
            return result
        await self.link_alias(
            conn,
            alias_id=signal.signal_id,
            canonical_id=result.canonical_signal_id,
            reason=result.reason,
        )
        signal.canonical_signal_id = result.canonical_signal_id
        return result

    # -- DB primitives --------------------------------------------------------

    def _tenant_clause(self, base_params: list[Any]) -> tuple[str, list[Any]]:
        """Append the tenant pin to a param list; return (sql, params)."""
        if self.owner_tenant is None:
            return "", base_params
        base_params.append(self.owner_tenant)
        return f" AND owner_tenant = ${len(base_params)}", base_params

    async def _lookup_by_url_hash(
        self, conn: Any, signal: Signal, url_hash: str,
    ) -> "dict[str, Any] | None":
        """Earliest existing row whose canonical-URL equals this signal's.

        The stored ``signals.canonical_url`` is the source-provided URL as
        landed: the baseline does NOT canonicalise it (it stays the display
        URL the lineage / substrate-read APIs surface). So a raw exact match
        against the *canonicalised* probe was inert for every row whose stored
        URL differed only by fragment / query order / default port / host case
        — the dedupe silently never fired. We therefore canonicalise BOTH
        sides: scan the tenant's rows that carry a ``canonical_url`` and pick
        the earliest whose ``Dedupe4TierHandler.canonical_url(stored)`` equals
        the canonicalised probe. This is the lower-risk of the two fixes — it
        touches only this lookup, leaving the stored/displayed URL and the
        content-hash backstop (which hashes the raw ``canonical_url``)
        untouched. There is no index on ``canonical_url`` either way (see the
        signals migration), so this matches the prior query's cost profile.
        """
        canon = signal.canonical_url or _payload_str(signal, "url") or \
            _payload_str(signal, "link") or _payload_str(signal, "source_url")
        if not canon:
            return None
        canon = Dedupe4TierHandler.canonical_url(canon)
        if not canon:
            return None
        params: list[Any] = [signal.signal_id]
        tclause, params = self._tenant_clause(params)
        rows = await conn.fetch(
            f"""
            SELECT id, canonical_signal_id, canonical_url
            FROM signals
            WHERE id <> $1
              AND canonical_url IS NOT NULL
              AND canonical_url <> ''
              {tclause}
            ORDER BY fetched_at ASC, id ASC
            """,
            *params,
        )
        for row in rows:
            stored = row["canonical_url"]
            if stored and Dedupe4TierHandler.canonical_url(stored) == canon:
                return dict(row)
        return None

    async def _lookup_by_content_hash(
        self, conn: Any, signal: Signal, content_hash: str,
    ) -> "dict[str, Any] | None":
        """Earliest existing row sharing this exact non-empty content hash."""
        params: list[Any] = [signal.signal_id, content_hash]
        tclause, params = self._tenant_clause(params)
        row = await conn.fetchrow(
            f"""
            SELECT id, canonical_signal_id
            FROM signals
            WHERE id <> $1
              AND content_hash = $2
              AND content_hash <> ''
              {tclause}
            ORDER BY fetched_at ASC, id ASC
            LIMIT 1
            """,
            *params,
        )
        return dict(row) if row is not None else None

    async def link_alias(
        self,
        conn: Any,
        *,
        alias_id: UUID,
        canonical_id: UUID,
        reason: str,
    ) -> bool:
        """Write one alias link + stamp the alias's canonical pointer.

        Idempotent (``ON CONFLICT DO NOTHING`` on the
        ``(alias_signal_id, canonical_signal_id)`` PK + an idempotent
        ``canonical_signal_id`` UPDATE). Also stamps the canonical row
        self-canonical so a ``canonical_only`` subscription resolves the set to
        one row. NEVER deletes a raw row — only sets the link column.

        Returns True iff a NEW alias row was inserted.
        """
        if alias_id == canonical_id:
            # A row never aliases itself; just stamp it self-canonical.
            await conn.execute(
                "UPDATE signals SET canonical_signal_id = id, updated_at = NOW() "
                "WHERE id = $1 AND canonical_signal_id IS DISTINCT FROM id",
                canonical_id,
            )
            return False
        # Canonical points at itself (so canonical_only sees exactly one row).
        await conn.execute(
            "UPDATE signals SET canonical_signal_id = id, updated_at = NOW() "
            "WHERE id = $1 AND canonical_signal_id IS DISTINCT FROM id",
            canonical_id,
        )
        inserted = await conn.fetchval(
            """
            INSERT INTO signal_aliases
                (alias_signal_id, canonical_signal_id, reason, score, produced_by)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (alias_signal_id, canonical_signal_id) DO NOTHING
            RETURNING alias_signal_id
            """,
            alias_id, canonical_id, reason, _EXACT_SCORE, self.produced_by,
        )
        await conn.execute(
            "UPDATE signals SET canonical_signal_id = $2, updated_at = NOW() "
            "WHERE id = $1",
            alias_id, canonical_id,
        )
        return inserted is not None

    # -- internals ------------------------------------------------------------

    @staticmethod
    def _hit(matched: dict[str, Any], *, tier: int, reason: str) -> IngestDedupeResult:
        """Build a hit result, resolving the matched row's canonical transitively.

        If the matched row already has a ``canonical_signal_id`` (it's itself an
        alias), the new row links to THAT canonical — never to an alias. If it
        has none, the matched row IS the canonical of the (new) duplicate set.
        """
        matched_id = matched["id"]
        existing_canon = matched.get("canonical_signal_id")
        canonical_id = existing_canon if existing_canon is not None else matched_id
        return IngestDedupeResult(
            is_duplicate=True,
            tier=tier,
            reason=reason,
            canonical_signal_id=canonical_id,
            matched_signal_id=matched_id,
        )


def _payload_str(signal: Signal, key: str) -> str:
    val = signal.payload.get(key)
    if val is None:
        return ""
    if isinstance(val, bytes):
        return val.decode("utf-8", "replace")
    return str(val)


def _stage_kind(stage: Any) -> str:
    """Extract a stage's ``kind`` from a FilterStage model or a plain mapping."""
    kind = getattr(stage, "kind", None)
    if kind is None and isinstance(stage, dict):
        kind = stage.get("kind")
    return str(kind) if kind else ""


def ingest_dedupe_from_stages(
    stages: Iterable[Any],
    *,
    produced_by: str = "ingest_dedupe",
    owner_tenant: str | None = None,
) -> IngestDedupe | None:
    """Build an :class:`IngestDedupe` from a source's ``ingestion_filters``.

    Scans the stage list for ``dedupe_tier_1`` / ``dedupe_tier_2`` kinds and
    returns an engine with exactly those tiers active. Returns ``None`` when no
    ingest dedupe tier is declared (the common case for sources that lean on the
    periodic analyst only) — the caller then skips ingest dedupe entirely.

    Non-dedupe ingestion-filter kinds (should any source declare one) are
    ignored here; they remain available to the periodic analyst / target
    pipeline. Tier 3/4 names in ``ingestion_filters`` are likewise ignored —
    they aren't ingest-side (see module docstring).
    """
    active: set[int] = set()
    for stage in stages or []:
        kind = _stage_kind(stage)
        if kind == TIER_1_URL:
            active.add(1)
        elif kind == TIER_2_CONTENT:
            active.add(2)
    if not active:
        return None
    return IngestDedupe(
        tiers=frozenset(active),
        produced_by=produced_by,
        owner_tenant=owner_tenant,
    )


__all__ = [
    "IngestDedupe",
    "IngestDedupeResult",
    "ingest_dedupe_from_stages",
    "TIER_1_URL",
    "TIER_2_CONTENT",
]

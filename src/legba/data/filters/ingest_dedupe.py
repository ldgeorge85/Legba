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
import time
from dataclasses import dataclass, field
from typing import Any, Iterable
from uuid import UUID

from .._url_canon import canonical_url_hash
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

# N-S: ``payload.source_url`` is the address the SOURCE fetched — the RSS feed,
# the GeoJSON endpoint, the JSON API (UKMTO's ``/api/ukmto/all`` serves ~60
# incidents from one address). Every item of a fetch shares it, so it names no
# document and is never a tier-1 key: a signal whose ONLY URL is this one skips
# tier 1 and is decided by tier 2 (its content hash, which for a json_api item
# folds in the item's own id). Before N-S such a probe could only match a row
# whose stored ``canonical_url`` happened to equal the endpoint — measured on
# live 2026-11-13 over all 50,854 rows that probe this way (NWS alerts, WHO
# DON, UKMTO): zero ever matched, so the rule changes no past outcome; it
# forecloses the one that would have collapsed a whole feed into one row.
_ENDPOINT_ORIGIN = "source_url"

# Fallback-scan logging: one INFO line per interval with the cumulative count,
# so a backfill in progress is visible without a line per written signal.
_FALLBACK_LOG_INTERVAL_S = 60.0
_fallback_state = {"calls": 0, "rows": 0, "last_log": 0.0}


def _note_fallback(rows: int) -> None:
    st = _fallback_state
    st["calls"] += 1
    st["rows"] += rows
    now = time.monotonic()
    if now - st["last_log"] >= _FALLBACK_LOG_INTERVAL_S:
        logger.info(
            "ingest_dedupe.url_hash.fallback_scan calls=%d rows_read=%d "
            "(rows without canonical_url_hash; run "
            "scripts/backfill_signal_url_hash.py until this stops)",
            st["calls"], st["rows"],
        )
        st["last_log"] = now
        st["calls"] = 0
        st["rows"] = 0


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
    def probe_url(signal: Signal) -> tuple[str, str]:
        """The URL tier 1 probes with, and which field it came from.

        ``(url, origin)`` with origin one of ``canonical_url`` / ``url`` /
        ``link`` / ``source_url``, or ``("", "")`` when the signal carries none.
        Same precedence as the target-side handler's tier-1 key.
        """
        for origin, url in (
            ("canonical_url", signal.canonical_url or ""),
            ("url", _payload_str(signal, "url")),
            ("link", _payload_str(signal, "link")),
            ("source_url", _payload_str(signal, "source_url")),
        ):
            if url:
                return url, origin
        return "", ""

    @staticmethod
    def url_hash(signal: Signal) -> str | None:
        """Canonical-URL SHA-256 for tier 1, or ``None`` if tier 1 has no key.

        ``None`` when the signal carries no URL, when the URL canonicalises to
        nothing, and — N-S, see :data:`_ENDPOINT_ORIGIN` — when the only URL
        is ``payload.source_url``, the address the SOURCE fetched (an RSS feed,
        a GeoJSON endpoint, a JSON API such as UKMTO's
        ``/api/ukmto/all``), which is shared by every item of that fetch and
        identifies no document. The key itself is
        :func:`legba.data._url_canon.canonical_url_hash`, byte-identical to the
        target-side handler's tier-1 hash and to ``signals.canonical_url_hash``.
        """
        url, origin = IngestDedupe.probe_url(signal)
        if not url or origin == _ENDPOINT_ORIGIN:
            return None
        if not Dedupe4TierHandler.canonical_url(url):
            return None
        return canonical_url_hash(url)

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
        """Earliest existing row whose canonical URL equals this signal's.

        The stored ``signals.canonical_url`` is the URL as landed (the display
        URL; the baseline does not canonicalise it), so the comparison has
        always been canonical-form to canonical-form. Until N-S that meant
        reading EVERY non-empty ``canonical_url`` row of the tenant and
        canonicalising each in Python, once per written signal — 380,879 rows
        and ~7 s per call on 2026-11-13, the dominant per-signal cost of the
        whole write path. Migration 0247 stores the canonical form's hash
        (``canonical_url_hash``, minted by the same
        :func:`~legba.data._url_canon.canonical_url_hash` as the probe), so the
        lookup is one indexed equality.

        Rows written before 0247 carry no hash until
        ``scripts/backfill_signal_url_hash.py`` reaches them. They are still
        matched, by the old canonicalise-in-Python comparison restricted to
        the UN-HASHED rows (a partial index serves exactly that set, and it
        shrinks to nothing as the backfill proceeds), and only rows earlier
        than the indexed hit are read. Same matches, same misses, same
        earliest-row choice as the full scan — proven in
        ``tests/data_pkg/test_ingest_dedupe_url_hash_equivalence.py`` against
        the frozen pre-N-S module.
        """
        url, _origin = self.probe_url(signal)
        canon = Dedupe4TierHandler.canonical_url(url) if url else ""
        if not canon:
            return None
        params: list[Any] = [signal.signal_id, url_hash]
        tclause, params = self._tenant_clause(params)
        hit = await conn.fetchrow(
            f"""
            SELECT id, canonical_signal_id, fetched_at
            FROM signals
            WHERE canonical_url_hash = $2
              AND id <> $1
              {tclause}
            ORDER BY fetched_at ASC, id ASC
            LIMIT 1
            """,
            *params,
        )
        hit = dict(hit) if hit is not None else None
        older = await self._lookup_unhashed(conn, signal, canon, before=hit)
        best = older if older is not None else hit
        if best is None:
            return None
        return {"id": best["id"], "canonical_signal_id": best["canonical_signal_id"]}

    async def _lookup_unhashed(
        self, conn: Any, signal: Signal, canon: str,
        *, before: "dict[str, Any] | None",
    ) -> "dict[str, Any] | None":
        """The pre-0247 comparison over rows not yet carrying a hash.

        Bounded by construction: it reads only rows whose
        ``canonical_url_hash`` is NULL while their ``canonical_url`` is not
        empty (served by ``idx_signals_canonical_url_unhashed``, which the
        backfill empties), and, when the indexed lookup already found a row,
        only rows ordered before it. Logs whenever it reads anything, so a
        backfill that never ran — or an insert path that stopped writing the
        hash — shows up in the logs instead of as a slow write path.
        """
        params: list[Any] = [signal.signal_id]
        bound = ""
        if before is not None:
            params += [before["fetched_at"], before["id"]]
            bound = " AND (fetched_at, id) < ($2::timestamptz, $3::uuid)"
        tclause, params = self._tenant_clause(params)
        rows = await conn.fetch(
            f"""
            SELECT id, canonical_signal_id, canonical_url, fetched_at
            FROM signals
            WHERE canonical_url_hash IS NULL
              AND canonical_url IS NOT NULL
              AND canonical_url <> ''
              AND id <> $1{bound}
              {tclause}
            ORDER BY fetched_at ASC, id ASC
            """,
            *params,
        )
        if not rows:
            return None
        _note_fallback(len(rows))
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

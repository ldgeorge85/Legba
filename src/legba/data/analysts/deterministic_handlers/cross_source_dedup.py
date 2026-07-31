# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``cross_source_dedup`` sub-handler — P-09 cross-source dedup (PIVOT §4.3, P-02).

Deterministic analyst over the shared raw signal pool. Scans for cross-source
duplicates and **links** them — it NEVER collapses or deletes a raw row. Every
observation is preserved so source-level evidence stays audit-grade; the
duplicates are merely tied to a canonical via :data:`signal_aliases` +
``signals.canonical_signal_id``.

Dedup strategy
--------------

  1. **Exact content-hash** (mandatory, deterministic). Groups of signals that
     share the same non-empty ``content_hash`` — whether across distinct
     ``source_id``s ("same content via A & B") or the same source — are tied to
     one canonical. Same content from two sources is the headline P-02 case.

  2. **Semantic near-dup via Qdrant** (best-effort, only when a Qdrant client is
     injected in ``deps.extras['qdrant']`` AND signals carry an
     ``embedding_ref``). For each not-yet-canonicalized signal with an embedding,
     we query its nearest neighbours and link any whose score clears
     ``options['semantic_threshold']`` (default 0.95) to the same canonical.
     When no Qdrant client is available we run **content_hash only** — exactly
     as the P-09 spec permits ("else content_hash only").

Canonical selection is deterministic: within a duplicate set the canonical is
the row with the earliest ``fetched_at``, tie-broken by the smallest ``id``
(UUID ordering). The canonical's own ``canonical_signal_id`` is set to itself so
a ``canonical_only`` subscription (``WHERE canonical_signal_id = id OR
canonical_signal_id IS NULL``) sees exactly one row per duplicate set; aliases
point at the canonical and are filtered out.

The alias write is idempotent: the ``signal_aliases`` primary key is
``(alias_signal_id, canonical_signal_id)`` so re-running the analyst over the
same pool is a no-op (``ON CONFLICT DO NOTHING`` + an idempotent
``canonical_signal_id`` UPDATE). Re-running NEVER produces a destructive
collapse — raw rows are untouched apart from the ``canonical_signal_id`` link
column.

**Bounded + incremental.** The exact pass does NOT re-scan the whole table
every cadence. The candidate query (a) skips content_hash groups whose members
are already fully canonicalised (filtered in the DB, never re-resolved) and
(b) is capped at ``max_groups_per_run`` (default
:data:`DEFAULT_MAX_GROUPS_PER_RUN`) with a stable ``ORDER BY content_hash`` so
a single run does bounded work and the backlog drains across successive
(frequent) cadences. This keeps each run inside the Dapr actor-invoke budget
without changing the dedupe result for any group that is processed.

Output ``data`` keys:
    canonical_count   int — duplicate sets resolved (one canonical each)
    aliases_linked    int — alias links written this run
    exact_aliases     int — aliases linked by content_hash
    semantic_aliases  int — aliases linked by Qdrant near-dup
    qdrant_errors     int — 1 when the semantic pass raised (query/transport
                      failure against ``qdrant_collection``) and degraded to
                      content_hash-only this run; 0 otherwise. A dead/missing
                      collection previously degraded SILENTLY (only a WARNING
                      log line, no counter) — this makes it a receipt fact an
                      operator can alert on.
    sets              [{canonical_signal_id, alias_signal_ids, reason, score}]
                      (omitted on the live-pool path when large; always present
                      in synthetic-input mode for test assertions)
"""

from __future__ import annotations

import logging
from typing import Any, Mapping
from uuid import UUID

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "cross_source_dedup"

# Reason tags written into signal_aliases.reason.
_REASON_EXACT = "content_hash"
_REASON_SEMANTIC = "semantic_qdrant"

# Exact content-hash matches are certain → score 1.0.
_EXACT_SCORE = 1.0

# Default Qdrant cosine-similarity floor for a semantic near-dup link.
_DEFAULT_SEMANTIC_THRESHOLD = 0.95
# Bound the per-signal neighbour fan-out so a runaway pool can't explode.
_SEMANTIC_TOP_K = 10

# Per-run cap on the number of duplicate-hash groups the exact pass resolves.
# Each cadence does bounded incremental work; the backlog drains across runs
# (cadence is frequent). This — together with skipping already-canonicalised
# groups in SQL — replaces the old unbounded full-table re-scan that was
# blowing the Dapr actor-invoke budget on busy targets.
DEFAULT_MAX_GROUPS_PER_RUN: int = 500

# The ONE Qdrant collection every signal actually lives in (see
# legba.data.config.QdrantConfig.signals_collection / signal_embedder). R2:
# this used to default to "signals" — a collection nothing ever created — so
# the semantic pass below silently never found a point and semantic dedupe
# never fired, in all of history. Kept as a named constant (rather than an
# inline literal in options.get) so a regression test can import it and pin
# it against the shared config default.
_DEFAULT_QDRANT_COLLECTION = "legba_signals"


# ---------------------------------------------------------------------------
# Canonical selection
# ---------------------------------------------------------------------------


def _pick_canonical(rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Deterministic canonical: earliest ``fetched_at`` then smallest ``id``.

    Both keys are total orders over the duplicate set, so the choice is stable
    across re-runs regardless of row arrival order.
    """
    def _key(r: Mapping[str, Any]) -> tuple[Any, str]:
        return (r.get("fetched_at"), str(r.get("id")))

    return min(rows, key=_key)


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _link_alias(
    conn: Any,
    *,
    alias_id: Any,
    canonical_id: Any,
    reason: str,
    score: float,
    produced_by: str | None,
) -> bool:
    """Write one alias link + stamp the alias row's canonical pointer.

    Returns True iff a NEW alias row was inserted (idempotent — a repeat run
    over the same pool returns False).
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO signal_aliases
            (alias_signal_id, canonical_signal_id, reason, score, produced_by)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (alias_signal_id, canonical_signal_id) DO NOTHING
        RETURNING alias_signal_id
        """,
        alias_id, canonical_id, reason, score, produced_by,
    )
    # Always (idempotently) point the alias at the canonical. NEVER deletes the
    # raw row — only sets the link column.
    await conn.execute(
        "UPDATE signals SET canonical_signal_id = $2, updated_at = NOW() "
        "WHERE id = $1",
        alias_id, canonical_id,
    )
    return inserted is not None


async def _resolve_exact_pool(
    pool: Any,
    *,
    produced_by: str | None,
    owner_tenant: str | None,
    max_groups: int,
) -> tuple[int, int, list[dict[str, Any]]]:
    """Content-hash dedup over the live ``signals`` pool.

    **Bounded + incremental.** The candidate query (a) only returns
    content_hash groups that still hold at least one *unresolved* member
    (``canonical_signal_id IS NULL``) — already-canonicalised groups are
    skipped in the DB, never re-resolved — and (b) is capped at
    ``max_groups`` per run with a stable ``ORDER BY content_hash``, so each
    cadence does bounded work and the backlog drains across successive runs.
    The dedupe outcome for any group it *does* process is identical to the
    old unbounded scan.

    Returns ``(canonical_count, aliases_linked, sets)``.
    """
    canonical_count = 0
    aliases_linked = 0
    sets: list[dict[str, Any]] = []

    async with pool.acquire() as conn:
        # Candidate hashes: a non-empty content_hash shared by >1 raw row that
        # still has ≥1 member without a canonical link. Already-linked groups
        # (every member canonicalised) are filtered out by the DB so we never
        # re-resolve settled work. Capped + stably ordered for incremental
        # drain across runs.
        hash_filter = ""
        params: list[Any] = []
        if owner_tenant is not None:
            hash_filter = "AND owner_tenant = $1"
            params = [owner_tenant]
        limit_param = f"${len(params) + 1}"
        params.append(max_groups)
        dup_hashes = await conn.fetch(
            f"""
            SELECT content_hash
            FROM signals
            WHERE content_hash <> ''
              {hash_filter}
            GROUP BY content_hash
            HAVING COUNT(*) > 1
               AND COUNT(*) FILTER (WHERE canonical_signal_id IS NULL) > 0
            ORDER BY content_hash ASC
            LIMIT {limit_param}
            """,
            *params,
        )

        for hrow in dup_hashes:
            content_hash = hrow["content_hash"]
            tenant_filter = ""
            row_params: list[Any] = [content_hash]
            if owner_tenant is not None:
                tenant_filter = "AND owner_tenant = $2"
                row_params.append(owner_tenant)
            rows = await conn.fetch(
                f"""
                SELECT id, source_id, fetched_at
                FROM signals
                WHERE content_hash = $1
                  {tenant_filter}
                ORDER BY fetched_at ASC, id ASC
                """,
                *row_params,
            )
            if len(rows) < 2:
                continue
            canonical = _pick_canonical(rows)
            canonical_id = canonical["id"]

            # Canonical points at itself so canonical_only sees exactly one row.
            await conn.execute(
                "UPDATE signals SET canonical_signal_id = id, updated_at = NOW() "
                "WHERE id = $1 AND canonical_signal_id IS DISTINCT FROM id",
                canonical_id,
            )
            canonical_count += 1
            linked_now: list[str] = []
            for r in rows:
                if r["id"] == canonical_id:
                    continue
                did_insert = await _link_alias(
                    conn,
                    alias_id=r["id"],
                    canonical_id=canonical_id,
                    reason=_REASON_EXACT,
                    score=_EXACT_SCORE,
                    produced_by=produced_by,
                )
                if did_insert:
                    aliases_linked += 1
                linked_now.append(str(r["id"]))
            sets.append({
                "canonical_signal_id": str(canonical_id),
                "alias_signal_ids": linked_now,
                "reason": _REASON_EXACT,
                "score": _EXACT_SCORE,
            })

    return canonical_count, aliases_linked, sets


async def _resolve_semantic_pool(
    pool: Any,
    qdrant: Any,
    *,
    threshold: float,
    collection: str,
    produced_by: str | None,
    owner_tenant: str | None,
) -> tuple[int, list[dict[str, Any]], int]:
    """Best-effort semantic near-dup over Qdrant. Returns
    ``(aliases_linked, sets, qdrant_errors)``.

    Only runs when a Qdrant client is injected. Any Qdrant error is swallowed
    (logged AT WARNING) — the mandatory content-hash path already ran, so a
    missing or flaky vector store degrades to content_hash-only rather than
    failing the run. ``qdrant_errors`` is 1 when that happened this run, 0
    otherwise — R2: a swallowed exception with no counter is how a dead/
    misnamed collection went unnoticed for the handler's entire history; the
    WARNING log line alone was never enough because nothing consumes logs as
    a signal, but a receipt counter can be alerted on.
    """
    aliases_linked = 0
    sets: list[dict[str, Any]] = []
    qdrant_errors = 0
    try:
        async with pool.acquire() as conn:
            tenant_filter = ""
            params: list[Any] = []
            if owner_tenant is not None:
                tenant_filter = "WHERE owner_tenant = $1"
                params = [owner_tenant]
            # Only signals still without a canonical AND carrying an embedding
            # are candidates — exact dupes were already linked above.
            rows = await conn.fetch(
                f"""
                SELECT id, embedding_ref, fetched_at
                FROM signals
                {tenant_filter}
                {"AND" if tenant_filter else "WHERE"} canonical_signal_id IS NULL
                  AND embedding_ref IS NOT NULL
                ORDER BY fetched_at ASC, id ASC
                """,
                *params,
            )
            for r in rows:
                # Skip rows linked by a neighbour earlier in this same loop.
                already = await conn.fetchval(
                    "SELECT canonical_signal_id FROM signals WHERE id = $1",
                    r["id"],
                )
                if already is not None:
                    continue
                neighbours = await _qdrant_neighbours(
                    qdrant, collection, r["embedding_ref"], threshold,
                )
                hits = [
                    (UUID(str(nid)), float(score))
                    for nid, score in neighbours
                    if str(nid) != str(r["id"]) and score >= threshold
                ]
                if not hits:
                    continue
                canonical_id = r["id"]
                await conn.execute(
                    "UPDATE signals SET canonical_signal_id = id, updated_at = NOW() "
                    "WHERE id = $1 AND canonical_signal_id IS DISTINCT FROM id",
                    canonical_id,
                )
                linked_now: list[str] = []
                for alias_id, score in hits:
                    exists = await conn.fetchval(
                        "SELECT 1 FROM signals WHERE id = $1", alias_id,
                    )
                    if not exists:
                        continue
                    did_insert = await _link_alias(
                        conn,
                        alias_id=alias_id,
                        canonical_id=canonical_id,
                        reason=_REASON_SEMANTIC,
                        score=round(score, 6),
                        produced_by=produced_by,
                    )
                    if did_insert:
                        aliases_linked += 1
                    linked_now.append(str(alias_id))
                if linked_now:
                    sets.append({
                        "canonical_signal_id": str(canonical_id),
                        "alias_signal_ids": linked_now,
                        "reason": _REASON_SEMANTIC,
                        "score": threshold,
                    })
    except Exception as exc:  # noqa: BLE001 — degrade to content_hash-only
        qdrant_errors = 1
        logger.warning(
            "cross_source_dedup.semantic_failed collection=%s err=%s — "
            "degrading to content_hash-only this run (see qdrant_errors)",
            collection, exc,
        )
    return aliases_linked, sets, qdrant_errors


async def _qdrant_neighbours(
    qdrant: Any,
    collection: str,
    point_id: Any,
    threshold: float,
) -> list[tuple[Any, float]]:
    """Recommend-by-point-id neighbours from Qdrant.

    Returns ``[(point_id, score), ...]``. Tolerates both the async and sync
    qdrant-client surfaces; missing methods raise and are caught upstream.
    """
    recommend = getattr(qdrant, "recommend", None)
    if recommend is None:
        return []
    result = recommend(
        collection_name=collection,
        positive=[point_id],
        limit=_SEMANTIC_TOP_K,
        score_threshold=threshold,
    )
    if hasattr(result, "__await__"):
        result = await result
    out: list[tuple[Any, float]] = []
    for hit in result or []:
        hid = getattr(hit, "id", None)
        score = getattr(hit, "score", None)
        if hid is not None and score is not None:
            out.append((hid, float(score)))
    return out


# ---------------------------------------------------------------------------
# Synthetic-input path (unit tests, no substrate)
# ---------------------------------------------------------------------------


def _resolve_synthetic(
    inputs: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]]]:
    """Content-hash dedup over pre-shaped input rows (deps=None path).

    Input row shape:
        {"id": str|UUID, "source_id": str, "content_hash": str,
         "fetched_at": iso8601 str | comparable}

    Returns ``(canonical_count, aliases_linked, sets)``.
    """
    by_hash: dict[str, list[dict[str, Any]]] = {}
    for row in inputs:
        ch = row.get("content_hash")
        if not ch:
            continue
        by_hash.setdefault(str(ch), []).append(row)

    canonical_count = 0
    aliases_linked = 0
    sets: list[dict[str, Any]] = []
    for _ch, rows in by_hash.items():
        if len(rows) < 2:
            continue
        canonical = _pick_canonical(rows)
        canonical_id = str(canonical.get("id"))
        alias_ids = [str(r.get("id")) for r in rows if str(r.get("id")) != canonical_id]
        canonical_count += 1
        aliases_linked += len(alias_ids)
        sets.append({
            "canonical_signal_id": canonical_id,
            "alias_signal_ids": alias_ids,
            "reason": _REASON_EXACT,
            "score": _EXACT_SCORE,
        })
    return canonical_count, aliases_linked, sets


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    canonical_count: int,
    aliases_linked: int,
    exact_aliases: int,
    semantic_aliases: int,
    qdrant_errors: int,
    sets: list[dict[str, Any]] | None,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Cross-source dedup: {canonical_count} canonical sets, "
        f"{aliases_linked} aliases linked "
        f"({exact_aliases} content_hash, {semantic_aliases} semantic)"
    )
    if qdrant_errors:
        title = f"{title} [qdrant_errors={qdrant_errors}]"
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"canonical_count={canonical_count}",
        f"aliases_linked={aliases_linked}",
        f"exact_aliases={exact_aliases}",
        f"semantic_aliases={semantic_aliases}",
        f"qdrant_errors={qdrant_errors}",
    ])
    tags = ["deterministic", SUB_HANDLER_NAME]
    if aliases_linked:
        tags.append("aliases_linked")
    if qdrant_errors:
        tags.append("qdrant_errors")
    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "canonical_count": canonical_count,
        "aliases_linked": aliases_linked,
        "exact_aliases": exact_aliases,
        "semantic_aliases": semantic_aliases,
        "qdrant_errors": qdrant_errors,
    }
    if sets is not None:
        data["sets"] = sets
    return FindingPayload(
        title=title[:2048],
        body=body[:65536],
        confidence=1.0,
        evidence=[],
        tags=tags,
        data=data,
    )


# ---------------------------------------------------------------------------
# Public handler entry point
# ---------------------------------------------------------------------------


async def handle(
    inputs: list[dict[str, Any]],
    options: Mapping[str, Any],
    deps: Any | None,
) -> AnalystMethodResult:
    """Sub-handler entry point — see module docstring.

    Options
    -------
    owner_tenant:
        Restrict the scan to one tenant (defaults to all). The signals table
        indexes ``owner_tenant`` so this is the tenancy seam.
    semantic_threshold:
        Qdrant cosine floor for a near-dup link (default 0.95).
    qdrant_collection:
        Qdrant collection name (default :data:`_DEFAULT_QDRANT_COLLECTION`,
        ``"legba_signals"`` — the ONE collection signal_embedder actually
        writes into; see :data:`legba.data.config.QdrantConfig.signals_collection`).
    max_groups_per_run:
        Cap on the number of duplicate-hash groups the exact pass resolves per
        run (default :data:`DEFAULT_MAX_GROUPS_PER_RUN`). Bounds per-run work so
        a single cadence stays inside the actor-invoke budget; already-resolved
        groups are skipped in SQL and the remaining backlog drains over
        successive (frequent) cadences.
    """
    produced_by = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    owner_tenant = options.get("owner_tenant")
    threshold = float(options.get("semantic_threshold", _DEFAULT_SEMANTIC_THRESHOLD))
    collection = str(options.get("qdrant_collection", _DEFAULT_QDRANT_COLLECTION))
    max_groups = int(options.get("max_groups_per_run", DEFAULT_MAX_GROUPS_PER_RUN))

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    extras = getattr(deps, "extras", {}) if deps is not None else {}
    qdrant = None
    if isinstance(extras, Mapping):
        qdrant = extras.get("qdrant")

    if pool is not None:
        # Mandatory deterministic pass.
        try:
            canonical_count, exact_aliases, sets = await _resolve_exact_pool(
                pool, produced_by=produced_by, owner_tenant=owner_tenant,
                max_groups=max_groups,
            )
        except Exception as exc:
            logger.warning("cross_source_dedup.exact_failed err=%s", exc)
            canonical_count, exact_aliases, sets = 0, 0, []
        # Best-effort semantic pass (only if Qdrant injected).
        semantic_aliases = 0
        qdrant_errors = 0
        if qdrant is not None:
            semantic_aliases, semantic_sets, qdrant_errors = await _resolve_semantic_pool(
                pool, qdrant,
                threshold=threshold,
                collection=collection,
                produced_by=produced_by,
                owner_tenant=owner_tenant,
            )
            canonical_count += len(semantic_sets)
            sets.extend(semantic_sets)
        aliases_linked = exact_aliases + semantic_aliases
        # Drop the per-set detail from the live finding if it's large — the
        # link rows are the source of truth; the finding is a summary.
        sets_for_finding = sets if len(sets) <= 100 else None
    else:
        canonical_count, exact_aliases, sets = _resolve_synthetic(inputs)
        semantic_aliases = 0
        qdrant_errors = 0
        aliases_linked = exact_aliases
        sets_for_finding = sets

    finding = _build_finding(
        canonical_count=canonical_count,
        aliases_linked=aliases_linked,
        exact_aliases=exact_aliases,
        semantic_aliases=semantic_aliases,
        qdrant_errors=qdrant_errors,
        sets=sets_for_finding,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]

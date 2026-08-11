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
     injected in ``deps.extras['qdrant']`` AND signals carry a REAL
     ``embedding_ref`` — a uuid, never one of signal_embedder's drain
     sentinels). For each not-yet-canonicalized signal with an embedding, we
     ``query_points`` its nearest neighbours and link any whose score clears
     ``options['semantic_threshold']`` to the same canonical. When no Qdrant
     client is available we run **content_hash only** — exactly as the P-09
     spec permits ("else content_hash only").

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
without changing the dedupe result for any group that is processed. The
semantic pass is bounded the same way
(:data:`DEFAULT_MAX_SEMANTIC_CANDIDATES`).

Cost — how this stopped being the largest analyst in the fleet
--------------------------------------------------------------

It was 5,496 runs and **61.9 of 73.6 analyst-hours per day** (84%), and the
single dominant Postgres load in the system (~6,800 lookups/sec), for output
that was structurally zero. Three separate multipliers, all now gone:

  1. **The 44x fan-out.** The descriptor carried a predicate-less
     ``subscription.targets`` block, which the runtime reads as "fan out to
     every active target" — so a sweep documented as target-agnostic ran once
     per desk, each copy executing the identical full-pool query. ``target_id``
     never narrowed anything; it suffixed the finding title. Removing the block
     (``AnalystActor._cadence_targets`` then returns ``None``) makes it one
     global run, the shape ``signal_embedder`` and ``entity_resolution``
     already use.
  2. **The unbounded candidate query + a PK lookup per row.** ~100k rows a run
     (measured 898 ms) followed by one ``SELECT canonical_signal_id`` per row
     (0.228 ms each = 23 s). Now a bounded query (20 ms) and an in-memory set.
  3. **A Qdrant round trip per candidate.** Now ONE ``query_batch_points`` per
     chunk and ONE neighbour-gate query for the whole run.

Measured end to end against the live substrate: **~24 s per run to 0.85 s**,
and 5,496 runs/day to 96.

Two failure classes, deliberately NOT the same
----------------------------------------------

  * **Transport / query failure** (Qdrant down, collection missing, one bad
    point id) — DEGRADES. The mandatory content-hash path already ran, so the
    run still produces its real output; the failure is counted in
    ``qdrant_errors`` and the pass continues with the next candidate. This is
    the correct behaviour for a best-effort tier.

  * **Client-contract failure** (the injected client does not expose the API
    this handler calls) — RAISES :class:`QdrantClientContractError`. It is
    caught by nothing here. A library that changed under us is a DEPLOY defect,
    not a transient one: it will not fix itself on the next cadence, and
    degrading it is exactly how the tier stayed dead for its entire history.
    ``cross_source_dedup.py`` reached Qdrant through
    ``getattr(qdrant, "recommend", None)`` with an ``if ... is None: return []``
    fallthrough. ``recommend()`` was removed in qdrant-client 1.10 in favour of
    ``query_points()``; the installed client is 1.18.0. So the getattr returned
    ``None``, the function returned ``[]``, and every candidate was skipped —
    **no error, no log line, and ``qdrant_errors=0`` at the same time as
    ``semantic_aliases=0``**, because the counter could only fire from inside a
    ``try`` that no Qdrant call ever entered. Two stacked silent no-ops
    (the misnamed collection was the other) survived a full repair pass because
    neither could be falsified by its own receipt.

Output ``data`` keys:
    canonical_count   int — duplicate sets resolved (one canonical each)
    aliases_linked    int — alias links written this run
    exact_aliases     int — aliases linked by content_hash
    semantic_aliases  int — aliases linked by Qdrant near-dup
    semantic_examined int — candidates the semantic pass actually queried
    semantic_gated    int — neighbours REJECTED by the gate: Qdrant orphans, or
                      points whose signal carries no real vector (quarantined
                      sub-floor embeddings). Nonzero is healthy; it is the
                      degenerate class being refused, and it is the difference
                      between 0.728 and 0.992 measured precision.
    qdrant_errors     int — COUNT of candidates whose Qdrant query raised this
                      run (each is skipped; the pass continues). Was a 0/1 flag
                      set by a try wrapping the WHOLE pass — one bad point id
                      aborted every remaining candidate. Nonzero means the
                      vector tier is degraded and is the operator's alert.
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
#
# MEASURED, not chosen. `scripts/measure_dedupe_threshold.py` samples signals
# deterministically, pulls their real neighbours through THIS module's
# `_qdrant_neighbours`, and labels each pair by title/body token overlap — a
# feature the body-only vectors never saw. Worksheet of the labelled sample:
# `tests/data_pkg/fixtures/dedupe_threshold_worksheet_2026-08-02.tsv`.
#
# Cumulative precision at each candidate threshold (2026-08-02, 6,000 signals,
# 5,446 labelled pairs; "prec [lower]" = ambiguous pairs dropped [counted as
# wrong]), over pairs where BOTH sides carry a floor-clearing embed input:
#
#     >=0.94  0.977 [0.924]   290 links
#     >=0.95  0.974 [0.934]   243 links
#     >=0.96  0.974 [0.950]   184 links
#     >=0.97  0.992 [0.959]   131 links   <- chosen
#     >=0.98  1.000 [0.977]    89 links
#
# 0.97 is where the worst-case error rate clears 1-in-25 while the tier still
# links a useful volume; 0.98 buys 1.8pp more for a third of the links. The
# asymmetry is what settles it: a MISSED dedup costs a little redundancy, but a
# FALSE dedup sets `signals.canonical_signal_id`, and every desk slice filters
# `(canonical_signal_id IS NULL OR canonical_signal_id = id)` — so a false link
# makes a real, distinct signal invisible to every analyst on the platform.
#
# THE THRESHOLD IS ONLY HALF THE ANSWER. Over ALL pairs in today's corpus,
# precision at 0.97 is 0.728 [0.693] — barely better than at 0.90 — because
# 60.8% of pairs at >=0.80 are DEGENERATE (both sides embedded from a
# byte-identical or sub-floor input, which scores ~1.0 regardless of content).
# No threshold separates those: they sit at the top of the range. They are
# excluded structurally instead, by requiring both sides to carry a real
# floor-clearing vector — see `_neighbour_gate` and migration 0130.
_DEFAULT_SEMANTIC_THRESHOLD = 0.97
# Bound the per-signal neighbour fan-out so a runaway pool can't explode.
_SEMANTIC_TOP_K = 10

# How many candidates ride one `query_batch_points` call. Bounds the request
# body (and the blast radius of a chunk that has to be retried serially) while
# keeping the round-trip count at candidates/chunk rather than candidates.
_SEMANTIC_BATCH_CHUNK = 128

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

# Per-run bound on how many candidates the semantic pass queries. The candidate
# query used to be UNBOUNDED and returned 99,946 rows per run — the loop over
# them (one PK lookup each, before a Qdrant call that never happened) was the
# single dominant Postgres load in the system. Bounded + stably ordered so the
# backlog drains across cadences, exactly like the exact pass's group cap.
DEFAULT_MAX_SEMANTIC_CANDIDATES: int = 500

# `signals.embedding_ref` is a SENTINEL column, not a foreign key: signal_embedder
# writes the signal's own uuid on success but 'no_body' / 'short_body' /
# 'embed_failed' on the drain paths. Eligibility used to be `IS NOT NULL`, which
# admits all three — and passing 'no_body' to Qdrant as a point id raises. With
# the old try wrapping the WHOLE pass and rows ordered fetched_at ASC, the
# earliest sentinel row would abort every run: the tier would have gone from
# silently-zero to loudly-zero. Match the uuid shape instead.
_UUID_EMBEDDING_REF_RE = (
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
)


# THE NEIGHBOUR GATE. A candidate is filtered on `embedding_ref` before it is
# queried, but a NEIGHBOUR arrives from Qdrant, and the collection holds points
# the substrate no longer vouches for:
#
#   * ORPHANS — `signals_retention` purges signals with no vector-store leg, so
#     ~11-21% of points reference rows that no longer exist. Linking one writes
#     an alias to a signal that is gone.
#   * QUARANTINED POINTS — migration 0130 moved 36,733 rows (61.2% of the
#     vectored corpus) to the `stale_subfloor` sentinel because their vector was
#     built from a sub-floor embed input. Those points are still IN Qdrant and
#     still come back as neighbours, and they are exactly the degenerate class
#     that sits at cosine ~1.0 regardless of content. Gating the candidate side
#     alone would leave every one of them linkable from the other direction.
#
# So the gate demands the SAME uuid-shaped `embedding_ref` of the neighbour that
# eligibility demands of the candidate. One set-based query for the whole
# neighbour set, replacing the per-neighbour PK lookup.
_NEIGHBOUR_GATE_SQL = """
    SELECT id
      FROM signals
     WHERE id = ANY($1::uuid[])
       AND embedding_ref ~ $2
"""


async def _linkable_neighbours(conn: Any, ids: list[Any]) -> list[Any]:
    """Of ``ids``, those that still exist AND carry a real vector."""
    if not ids:
        return []
    rows = await conn.fetch(_NEIGHBOUR_GATE_SQL, ids, _UUID_EMBEDDING_REF_RE)
    return [row["id"] for row in rows]


class QdrantClientContractError(RuntimeError):
    """The injected Qdrant client does not expose the API this handler calls.

    NOT a transport error and NOT degradable — see "Two failure classes" in the
    module docstring. Raised out of the handler so the run fails visibly.
    """


def _require_query_batch_points(qdrant: Any) -> Any:
    """Return the client's ``query_batch_points``, or RAISE.

    Batching is not a nice-to-have here — it is the difference between one
    Qdrant round trip per run and one per candidate. Same discipline as
    :func:`_require_query_points`: the handler declares what it needs, and a
    client that cannot provide it fails the run instead of quietly costing 5x.
    """
    query_batch_points = getattr(qdrant, "query_batch_points", None)
    if not callable(query_batch_points):
        raise QdrantClientContractError(
            "the injected Qdrant client "
            f"({type(qdrant).__module__}.{type(qdrant).__name__}) exposes no "
            "callable query_batch_points() — the semantic dedup tier batches "
            "its neighbour queries and will not silently fall back to a round "
            "trip per candidate. Check the qdrant-client version in the "
            "runtime image."
        )
    return query_batch_points


def _require_query_points(qdrant: Any) -> Any:
    """Return the client's ``query_points``, or RAISE.

    The whole point of this function is that it has no ``None`` return and no
    caller-side fallthrough. If the installed client cannot do the work, the
    run says so instead of quietly reporting zero.
    """
    query_points = getattr(qdrant, "query_points", None)
    if not callable(query_points):
        raise QdrantClientContractError(
            "the injected Qdrant client "
            f"({type(qdrant).__module__}.{type(qdrant).__name__}) exposes no "
            "callable query_points() — the semantic dedup tier cannot run. "
            "This handler previously swallowed exactly this condition "
            "(getattr(qdrant, 'recommend', None) -> None -> return []) and "
            "reported zero aliases with zero errors for its entire history. "
            "Check the qdrant-client version in the runtime image."
        )
    return query_points


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
    max_candidates: int = DEFAULT_MAX_SEMANTIC_CANDIDATES,
) -> tuple[int, list[dict[str, Any]], int, int, int]:
    """Best-effort semantic near-dup over Qdrant. Returns
    ``(aliases_linked, sets, qdrant_errors, examined, gated)``.

    Only runs when a Qdrant client is injected.

    **Three queries, not three per candidate.** One bounded candidate SELECT,
    one batched Qdrant call per chunk, one neighbour-gate SELECT for every
    neighbour the whole run proposed. The pass used to issue an unbounded
    candidate SELECT, then a PK lookup AND a Qdrant round trip per row over
    ~100k rows.

    **The client contract is checked FIRST, outside every ``try``.** A client
    that cannot do the work raises :class:`QdrantClientContractError` straight
    out of this function — see "Two failure classes" in the module docstring.

    **Transport failures cost one candidate, not the pass.** The ``try`` used to
    wrap the entire loop, so the first candidate that raised aborted every
    remaining one; combined with the ``embedding_ref IS NOT NULL`` eligibility
    filter (which admits the ``no_body`` sentinel, and rows ordered
    ``fetched_at ASC``) a single sentinel row would have killed every run
    forever. Now a failed batch is retried serially, ``qdrant_errors`` COUNTS
    the candidates actually lost, and the pass continues.
    """
    # CONTRACT first. Not inside a try, not degradable, not a counter.
    _require_query_points(qdrant)

    aliases_linked = 0
    sets: list[dict[str, Any]] = []
    qdrant_errors = 0
    examined = 0
    gated = 0
    async with pool.acquire() as conn:
        params: list[Any] = []
        tenant_filter = ""
        if owner_tenant is not None:
            params.append(owner_tenant)
            tenant_filter = f"AND owner_tenant = ${len(params)}"
        params.append(_UUID_EMBEDDING_REF_RE)
        ref_param = f"${len(params)}"
        params.append(max_candidates)
        limit_param = f"${len(params)}"
        # Candidates: still un-canonicalised AND carrying a REAL embedding
        # (a uuid, never a drain sentinel). Bounded + stably ordered so a run
        # does finite work and the backlog drains across cadences.
        rows = await conn.fetch(
            f"""
            SELECT id, embedding_ref, fetched_at
            FROM signals
            WHERE canonical_signal_id IS NULL
              AND embedding_ref ~ {ref_param}
              {tenant_filter}
            ORDER BY fetched_at ASC, id ASC
            LIMIT {limit_param}
            """,
            *params,
        )
        if not rows:
            return aliases_linked, sets, qdrant_errors, examined, gated
        examined = len(rows)

        # ONE Qdrant round trip for the whole run instead of one per candidate,
        # and ONE gate query for every neighbour any candidate found. See
        # _qdrant_neighbours_batch: this is the pairwise-scan collapse.
        neighbours_by_candidate, qdrant_errors = await _qdrant_neighbours_batch(
            qdrant, collection,
            [r["embedding_ref"] for r in rows],
            threshold,
        )
        proposed: list[list[tuple[Any, float]]] = []
        every_neighbour: set[Any] = set()
        for r, neighbours in zip(rows, neighbours_by_candidate):
            hits = [
                (UUID(str(nid)), float(score))
                for nid, score in neighbours
                if str(nid) != str(r["id"]) and score >= threshold
            ]
            proposed.append(hits)
            every_neighbour.update(nid for nid, _ in hits)
        linkable = set(await _linkable_neighbours(conn, sorted(every_neighbour)))

        # Rows linked by a neighbour earlier in THIS loop must not become
        # canonicals themselves. Tracked in memory rather than re-SELECTed per
        # row — the per-row PK lookup was ~6,800 reads/sec of pure waste.
        linked_this_run: set[str] = set()
        for r, candidates in zip(rows, proposed):
            if str(r["id"]) in linked_this_run:
                continue
            if not candidates:
                continue
            hits = [(nid, score) for nid, score in candidates if nid in linkable]
            # Counted in NEIGHBOURS refused, in both branches — a counter whose
            # unit depends on which branch ran cannot be summed or alerted on.
            gated += len(candidates) - len(hits)
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
                linked_this_run.add(str(alias_id))
            if linked_now:
                sets.append({
                    "canonical_signal_id": str(canonical_id),
                    "alias_signal_ids": linked_now,
                    "reason": _REASON_SEMANTIC,
                    "score": threshold,
                })
    return aliases_linked, sets, qdrant_errors, examined, gated


async def _qdrant_neighbours_batch(
    qdrant: Any,
    collection: str,
    point_ids: list[Any],
    threshold: float,
    *,
    limit: int = _SEMANTIC_TOP_K,
    chunk: int = _SEMANTIC_BATCH_CHUNK,
) -> tuple[list[list[tuple[Any, float]]], int]:
    """Neighbours for MANY points in one Qdrant round trip per chunk.

    Returns ``(neighbours_per_input, failed_count)``, aligned to ``point_ids``;
    a candidate whose query failed gets ``[]`` and is counted.

    THIS IS THE COLLAPSE. The pass used to issue one ``query_points`` per
    candidate — a round trip each, on top of a Postgres PK lookup each. Measured
    against the live collection: 50 point queries take 366 ms serially and 68 ms
    batched, a **5.3x** cut on the Qdrant leg, and it turns a per-run cost of
    O(candidates) round trips into O(candidates / chunk).

    **Why the serial fallback is not optional.** Qdrant 404s a query whose point
    id is not in the collection — and in a batch, that ONE bad id fails the
    WHOLE batch (verified against the live server). A signal can hold a
    uuid-shaped ``embedding_ref`` with no surviving point, so on a chunk failure
    the chunk is retried one query at a time and only the genuinely bad
    candidates are lost. Fast path batched, failure path isolated; either way
    the loss is counted, never swallowed.
    """
    _require_query_batch_points(qdrant)
    out: list[list[tuple[Any, float]]] = []
    failed = 0
    for start in range(0, len(point_ids), chunk):
        window = point_ids[start:start + chunk]
        try:
            out.extend(await _batch_chunk(qdrant, collection, window, threshold, limit))
            continue
        except QdrantClientContractError:
            raise
        except Exception as exc:  # noqa: BLE001 — isolate the bad id(s)
            logger.warning(
                "cross_source_dedup.semantic_batch_failed collection=%s "
                "chunk=%d..%d err=%s — retrying the chunk serially so one bad "
                "point id cannot cost the whole batch",
                collection, start, start + len(window), exc,
            )
        for point_id in window:
            try:
                out.append(await _qdrant_neighbours(
                    qdrant, collection, point_id, threshold, limit=limit,
                ))
            except QdrantClientContractError:
                raise
            except Exception as exc:  # noqa: BLE001 — degrade THIS candidate
                failed += 1
                out.append([])
                logger.warning(
                    "cross_source_dedup.semantic_candidate_failed collection=%s "
                    "point_id=%s err=%s — skipping this candidate "
                    "(see qdrant_errors)",
                    collection, point_id, exc,
                )
    return out, failed


async def _batch_chunk(
    qdrant: Any,
    collection: str,
    point_ids: list[Any],
    threshold: float,
    limit: int,
) -> list[list[tuple[Any, float]]]:
    """One ``query_batch_points`` call; results in request order."""
    # Imported HERE, not at module scope. Everywhere else this module treats the
    # client as duck-typed and injected, so it stays importable (and unit
    # testable) without qdrant-client present; the batch API is the one place
    # that needs the library's own request type. Keep it that way.
    from qdrant_client.http import models as qmodels

    query_batch_points = _require_query_batch_points(qdrant)
    requests = [
        qmodels.QueryRequest(
            query=str(point_id),
            limit=limit,
            score_threshold=threshold,
            with_payload=False,
        )
        for point_id in point_ids
    ]
    result = query_batch_points(collection_name=collection, requests=requests)
    if hasattr(result, "__await__"):
        result = await result
    responses = list(result or [])
    if len(responses) != len(point_ids):
        raise QdrantClientContractError(
            f"query_batch_points returned {len(responses)} responses for "
            f"{len(point_ids)} requests — results are positional, so a length "
            "mismatch would silently attribute one signal's neighbours to "
            "another and link the wrong rows"
        )
    return [_hits_of(response) for response in responses]


def _hits_of(response: Any) -> list[tuple[Any, float]]:
    """``[(point_id, score), ...]`` from a QueryResponse (or a bare sequence)."""
    hits = getattr(response, "points", response)
    out: list[tuple[Any, float]] = []
    for hit in hits or []:
        hid = getattr(hit, "id", None)
        score = getattr(hit, "score", None)
        if hid is not None and score is not None:
            out.append((hid, float(score)))
    return out


async def _qdrant_neighbours(
    qdrant: Any,
    collection: str,
    point_id: Any,
    threshold: float,
    *,
    limit: int = _SEMANTIC_TOP_K,
) -> list[tuple[Any, float]]:
    """Nearest neighbours of an existing point, via ``query_points``.

    ``query(=<point id>)`` tells Qdrant to search with the stored vector of that
    point and — verified against the live collection — to EXCLUDE the point
    itself from the result, which is the semantic the removed ``recommend()``
    had. Returns ``[(point_id, score), ...]``, highest first.

    A client without ``query_points`` raises :class:`QdrantClientContractError`
    (never a quiet ``[]``); a transport failure raises whatever the client
    raises, and the caller counts it per-candidate.
    """
    query_points = _require_query_points(qdrant)
    result = query_points(
        collection_name=collection,
        query=str(point_id),
        limit=limit,
        score_threshold=threshold,
        with_payload=False,
    )
    if hasattr(result, "__await__"):
        result = await result
    # qdrant-client >=1.10 wraps the hits in a QueryResponse; tolerate a bare
    # sequence too (older surfaces, fakes) rather than assuming the wrapper.
    hits = getattr(result, "points", result)
    out: list[tuple[Any, float]] = []
    for hit in hits or []:
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
    semantic_examined: int,
    semantic_gated: int,
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
        f"semantic_examined={semantic_examined}",
        f"semantic_gated={semantic_gated}",
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
        "semantic_examined": semantic_examined,
        "semantic_gated": semantic_gated,
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
    max_semantic_candidates:
        Cap on the number of candidates the semantic pass queries per run
        (default :data:`DEFAULT_MAX_SEMANTIC_CANDIDATES`). The candidate query
        used to be unbounded — 99,946 rows a run.
    """
    produced_by = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    owner_tenant = options.get("owner_tenant")
    threshold = float(options.get("semantic_threshold", _DEFAULT_SEMANTIC_THRESHOLD))
    collection = str(options.get("qdrant_collection", _DEFAULT_QDRANT_COLLECTION))
    max_groups = int(options.get("max_groups_per_run", DEFAULT_MAX_GROUPS_PER_RUN))
    max_semantic_candidates = int(
        options.get("max_semantic_candidates", DEFAULT_MAX_SEMANTIC_CANDIDATES)
    )

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
        # Best-effort semantic pass (only if Qdrant injected). NOT wrapped in a
        # try here: a transport failure is already handled per-candidate inside,
        # and a QdrantClientContractError MUST reach the caller.
        semantic_aliases = 0
        qdrant_errors = 0
        semantic_examined = 0
        semantic_gated = 0
        if qdrant is not None:
            (
                semantic_aliases, semantic_sets, qdrant_errors, semantic_examined,
                semantic_gated,
            ) = await _resolve_semantic_pool(
                pool, qdrant,
                threshold=threshold,
                collection=collection,
                produced_by=produced_by,
                owner_tenant=owner_tenant,
                max_candidates=max_semantic_candidates,
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
        semantic_examined = 0
        semantic_gated = 0
        aliases_linked = exact_aliases
        sets_for_finding = sets

    finding = _build_finding(
        canonical_count=canonical_count,
        aliases_linked=aliases_linked,
        exact_aliases=exact_aliases,
        semantic_aliases=semantic_aliases,
        semantic_examined=semantic_examined,
        semantic_gated=semantic_gated,
        qdrant_errors=qdrant_errors,
        sets=sets_for_finding,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME", "QdrantClientContractError"]

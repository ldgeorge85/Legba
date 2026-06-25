# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``cross_source_coalesce`` sub-handler — substrate-wide semantic/temporal coalescing.

Review item P2 (data-integrity): "cross-source semantic/temporal coalescing not
built". The ingest-time :class:`legba.data.filters.dedupe.Dedupe4TierHandler`
runs **per target / per source** (its tier-3 Qdrant collection is keyed
``legba_dedup__<target_id>`` and its tier-4 temporal cache is keyed
``(target_id, source_id)``). The pre-existing :mod:`cross_source_dedup`
sub-handler is POOL-wide but only does **exact** content-hash links plus a
best-effort Qdrant ``recommend`` that depends on a pre-populated
``signals.embedding_ref`` (which today's pipeline never writes). Neither closes
the headline P2 gap: the SAME real-world event reported by two DIFFERENT sources,
with DIFFERENT wording (so no shared content_hash) and DIFFERENT URLs, sitting in
the shared pool unlinked.

This handler is that missing leg — a periodic, **deterministic** (no LLM)
substrate-wide coalesce that:

  1. Reads recent canonical (not-yet-aliased) signals across ALL sources within a
     time window (default 24h) from the shared ``signals`` pool.
  2. **Embeds** each one's ``title + summary`` via the L-122 embedding service
     (the same :class:`EmbeddingService` port the dedupe filter uses) into a
     SHARED Qdrant collection (``legba_coalesce`` by default — one collection for
     the whole substrate, NOT one-per-target like the dedupe filter's tier-3).
  3. **Coalesces near-duplicate signals ACROSS sources** by reusing the dedupe
     tier-3 + tier-4 logic:
       * tier-3 (semantic): cosine similarity over the shared collection clears
         ``semantic_threshold`` (default 0.92, the dedupe Tier3Config default);
       * tier-4 (temporal): the matched neighbour is within ``window_hours`` AND
         its normalized title's Levenshtein distance is below
         ``title_distance_threshold`` (default 0.35 — looser than the dedupe
         Tier4Config 0.15 because cross-source re-wordings vary more; see the
         constant's note) — reusing :meth:`Dedupe4TierHandler.normalized_title` +
         :func:`legba.data.filters.dedupe._normalized_levenshtein`.
     A pair is coalesced iff it clears the semantic floor AND the temporal+title
     guard — the conjunction is deliberate so a high cosine on a stale or
     differently-worded headline doesn't over-link.
  4. **Links, never collapses.** Matches from DIFFERENT sources are tied to one
     deterministic canonical (earliest ``fetched_at``, then smallest ``id``) via
     a ``signal_aliases`` row (``reason='cross_source_coalesce'``) + the alias's
     ``canonical_signal_id`` pointer. The raw rows are NEVER deleted — every
     source observation stays audit-grade (the link-never-collapse invariant the
     pivot's P-02 mandates). Same-source near-dups are left to the ingest-time
     filter; this handler's job is the CROSS-source link.

Degrade-not-drop
----------------
The handler needs both an embedding service AND a Qdrant client (threaded via
``deps.extras['embedding_service']`` / ``deps.extras['qdrant']``). When EITHER is
absent it CANNOT do semantic coalescing — there is no deterministic non-vector
fallback for "same event, different words" (exact content_hash is already
``cross_source_dedup``'s job). Rather than silently no-op, it returns a finding
flagged ``coalesce_unavailable`` naming the missing port, and the unwired
production-deps path is a declared SEAM (docs/SEAMS.md #19) guarded loud here —
it never fabricates a link.

Off-by-default / cadence-gated
------------------------------
The bound descriptor (``analyst_cross_source_coalesce.yaml``) is NOT in the
default bringup set. An operator opts in by registering it (a low cadence — every
few hours — is the intended posture; the embed cost is linear in the unaliased
window). ``options['enabled']`` defaulting False is the in-handler kill switch so
even a registered descriptor is a no-op until the operator flips it on.

Output ``data`` keys::

    coalesced_sets   int — cross-source duplicate sets linked this run
    aliases_linked   int — alias rows written this run
    embedded         int — signals embedded into the shared collection this run
    window_hours     int — the temporal window scanned
    unavailable      bool|str — present + truthy when a required port was missing
    sets             [{canonical_signal_id, alias_signal_ids, score, reason}]
                     (omitted on the live path when large; always present in
                     synthetic-input mode for test assertions)
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Mapping
from uuid import UUID

from ...filters.dedupe import Dedupe4TierHandler, _normalized_levenshtein
from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "cross_source_coalesce"

# Reason tag written into signal_aliases.reason — distinct from cross_source_dedup's
# 'content_hash' / 'semantic_qdrant' so the coalesce links are attributable.
_REASON = "cross_source_coalesce"

# The semantic + temporal-window defaults mirror the dedupe filter's
# Tier3Config/Tier4Config so the two paths agree on what "near-duplicate" means.
_DEFAULT_WINDOW_HOURS = 24
_DEFAULT_SEMANTIC_THRESHOLD = 0.92
# The title guard is LOOSER than the ingest-time filter's tier-4 default (0.15):
# the SAME real-world event reported by DIFFERENT sources is routinely re-worded
# (an appended "today" / "overnight", a swapped synonym), so a 0.15 ceiling would
# reject genuine cross-source matches whose cosine already cleared 0.92. 0.35
# still cleanly separates same-event re-wordings (~0.2-0.3 normalized Levenshtein)
# from unrelated headlines (~0.8+) — it's a secondary sanity check against a
# vector collision, not the primary discriminator (the cosine floor is).
_DEFAULT_TITLE_DISTANCE_THRESHOLD = 0.35
_DEFAULT_COLLECTION = "legba_coalesce"
# Bound the per-signal neighbour fan-out so a runaway window can't explode the
# pairwise work.
_SEMANTIC_TOP_K = 10
# Cap the number of signals embedded+scanned per run — a hard ceiling so one tick
# over a huge backlog can't run unbounded. Operators raise it via options.
_DEFAULT_MAX_SIGNALS = 2000


# ---------------------------------------------------------------------------
# Pure helpers (unit-testable without a DB / Qdrant)
# ---------------------------------------------------------------------------


def _signal_text(row: Mapping[str, Any]) -> str:
    """Embed/compare text for a signal row: title + summary (payload-derived).

    Mirrors :meth:`Dedupe4TierHandler._embed_for_signal`'s field preference so
    the coalesce embedding lines up with the dedupe filter's tier-3 embedding.
    """
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    title = str(payload.get("title") or row.get("title") or "").strip()
    summary = str(
        payload.get("summary")
        or payload.get("body")
        or payload.get("raw_body")
        or row.get("summary")
        or ""
    ).strip()
    return (title + "\n" + summary).strip()


def _signal_title(row: Mapping[str, Any]) -> str:
    payload = row.get("payload")
    if not isinstance(payload, Mapping):
        payload = {}
    return str(payload.get("title") or row.get("title") or "")


def _titles_close(a: str, b: str, threshold: float) -> bool:
    """tier-4 title guard — reuse the dedupe filter's normalize + Levenshtein."""
    na = Dedupe4TierHandler.normalized_title(a)
    nb = Dedupe4TierHandler.normalized_title(b)
    if not na or not nb:
        # No title to compare on — the temporal guard alone isn't enough to
        # call a cross-source link; require a title on both sides.
        return False
    return _normalized_levenshtein(na, nb) < threshold


def _aware(dt: Any) -> datetime | None:
    if not isinstance(dt, datetime):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _within_window(a_fetched: Any, b_fetched: Any, window_hours: int) -> bool:
    """tier-4 temporal guard — the two observations are within the window."""
    da = _aware(a_fetched)
    db = _aware(b_fetched)
    if da is None or db is None:
        return False
    return abs((da - db).total_seconds()) <= window_hours * 3600.0


def _pick_canonical(rows: list[Mapping[str, Any]]) -> Mapping[str, Any]:
    """Deterministic canonical: earliest ``fetched_at`` then smallest ``id``.

    Same rule as :mod:`cross_source_dedup` so the two coalescers never fight over
    which row is canonical for an overlapping set.
    """
    def _key(r: Mapping[str, Any]) -> tuple[Any, str]:
        return (_aware(r.get("fetched_at")) or datetime.max.replace(tzinfo=timezone.utc), str(r.get("id")))

    return min(rows, key=_key)


# ---------------------------------------------------------------------------
# Pairing — the reusable coalesce core (vectors in-hand)
# ---------------------------------------------------------------------------


def _coalesce_pairs(
    rows: list[dict[str, Any]],
    vectors: dict[str, list[float]],
    *,
    semantic_threshold: float,
    title_distance_threshold: float,
    window_hours: int,
) -> list[dict[str, Any]]:
    """Group cross-source near-duplicates given pre-computed embeddings.

    Pure + deterministic — the live path and the synthetic test path both call
    this once they hold ``{signal_id: vector}``. A pair (i, j) coalesces iff:

      * ``rows[i].source_id != rows[j].source_id`` (CROSS-source only), AND
      * cosine(vec_i, vec_j) >= ``semantic_threshold`` (tier-3), AND
      * the two are within ``window_hours`` (tier-4 temporal), AND
      * their normalized titles' Levenshtein distance < ``title_distance_threshold``
        (tier-4 title).

    Returns ``[{canonical_signal_id, alias_signal_ids, reason, score}]`` — one
    entry per connected component with >= 2 members spanning >= 2 sources.
    Single-source components are dropped (the ingest-time filter owns those).
    """
    by_id: dict[str, dict[str, Any]] = {str(r["id"]): r for r in rows if r.get("id")}
    ids = [str(r["id"]) for r in rows if r.get("id") and str(r["id"]) in vectors]

    # Union-find over coalescable pairs.
    parent: dict[str, str] = {i: i for i in ids}
    best_score: dict[frozenset[str], float] = {}

    def _find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def _union(a: str, b: str) -> None:
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    n = len(ids)
    for ii in range(n):
        id_i = ids[ii]
        ri = by_id[id_i]
        vi = vectors[id_i]
        # Top-K nearest by cosine over the remaining rows (bounded fan-out).
        scored: list[tuple[float, str]] = []
        for jj in range(n):
            if jj == ii:
                continue
            id_j = ids[jj]
            rj = by_id[id_j]
            if str(ri.get("source_id")) == str(rj.get("source_id")):
                continue  # same source → ingest-time filter's job, not ours
            score = _cosine(vi, vectors[id_j])
            scored.append((score, id_j))
        scored.sort(key=lambda s: (-s[0], s[1]))
        for score, id_j in scored[:_SEMANTIC_TOP_K]:
            if score < semantic_threshold:
                break  # sorted desc — no further neighbour can clear the floor
            rj = by_id[id_j]
            if not _within_window(ri.get("fetched_at"), rj.get("fetched_at"), window_hours):
                continue
            if not _titles_close(_signal_title(ri), _signal_title(rj), title_distance_threshold):
                continue
            _union(id_i, id_j)
            key = frozenset({id_i, id_j})
            best_score[key] = max(best_score.get(key, 0.0), score)

    # Materialize components.
    comps: dict[str, list[str]] = {}
    for i in ids:
        comps.setdefault(_find(i), []).append(i)

    sets: list[dict[str, Any]] = []
    for members in comps.values():
        if len(members) < 2:
            continue
        sources = {str(by_id[m].get("source_id")) for m in members}
        if len(sources) < 2:
            continue  # single-source component — not a CROSS-source link
        member_rows = [by_id[m] for m in members]
        canonical = _pick_canonical(member_rows)
        canonical_id = str(canonical["id"])
        alias_ids = sorted(m for m in members if m != canonical_id)
        # Representative score = the strongest pairwise link in the component.
        comp_set = set(members)
        score = max(
            (s for k, s in best_score.items() if k <= comp_set),
            default=semantic_threshold,
        )
        sets.append({
            "canonical_signal_id": canonical_id,
            "alias_signal_ids": alias_ids,
            "reason": _REASON,
            "score": round(float(score), 6),
        })
    # Deterministic ordering for stable test assertions + idempotent writes.
    sets.sort(key=lambda s: s["canonical_signal_id"])
    return sets


def _cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity. Returns 0.0 for a zero vector or length mismatch."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0.0 or nb <= 0.0:
        return 0.0
    return dot / ((na ** 0.5) * (nb ** 0.5))


# ---------------------------------------------------------------------------
# Embedding (shared-collection write + neighbour query reuse)
# ---------------------------------------------------------------------------


async def _embed_rows(
    rows: list[dict[str, Any]],
    embedder: Any,
) -> dict[str, list[float]]:
    """Embed each row's title+summary via the EmbeddingService port.

    Best-effort per row — a single embed failure skips that row (it just won't
    participate in coalescing this run) rather than sinking the whole sweep.
    """
    out: dict[str, list[float]] = {}
    for r in rows:
        sid = str(r.get("id") or "")
        if not sid:
            continue
        text = _signal_text(r)
        if not text:
            continue
        try:
            vec = await embedder.embed(text)
        except Exception as exc:  # noqa: BLE001 — degrade per-row
            logger.warning("cross_source_coalesce.embed_failed id=%s err=%s", sid, exc)
            continue
        if vec:
            out[sid] = list(vec)
    return out


async def _upsert_shared_collection(
    qdrant: Any,
    collection: str,
    vectors: dict[str, list[float]],
    rows: list[dict[str, Any]],
    dim: int,
) -> None:
    """Best-effort write of the embeddings into the SHARED coalesce collection.

    The substrate-wide collection makes the coalesce embeddings queryable for
    follow-on consult/vector_search reuse; the per-run pairing itself runs over
    the in-memory vectors (so a Qdrant write hiccup never blocks linking). Any
    error degrades to "skipped the persist" — logged, not raised.
    """
    try:
        from qdrant_client.http import models as qmodels

        # Ensure the shared collection exists (idempotent).
        try:
            existing = await qdrant.get_collections()
            names = {c.name for c in getattr(existing, "collections", []) or []}
            if collection not in names:
                await qdrant.create_collection(
                    collection_name=collection,
                    vectors_config=qmodels.VectorParams(
                        size=dim, distance=qmodels.Distance.COSINE,
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("cross_source_coalesce.ensure_collection_failed err=%s", exc)
            return

        by_id = {str(r["id"]): r for r in rows if r.get("id")}
        points = []
        for sid, vec in vectors.items():
            r = by_id.get(sid, {})
            points.append(
                qmodels.PointStruct(
                    id=sid,
                    vector=vec,
                    payload={
                        "signal_id": sid,
                        "source_id": str(r.get("source_id") or ""),
                        "fetched_at": (
                            _aware(r.get("fetched_at")).isoformat()
                            if _aware(r.get("fetched_at")) is not None
                            else None
                        ),
                    },
                )
            )
        if points:
            await qdrant.upsert(collection_name=collection, points=points)
    except Exception as exc:  # noqa: BLE001 — persist is best-effort
        logger.warning("cross_source_coalesce.upsert_failed err=%s", exc)


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _link_alias(
    conn: Any,
    *,
    alias_id: Any,
    canonical_id: Any,
    score: float,
    produced_by: str | None,
) -> bool:
    """Write one alias link + stamp the alias row's canonical pointer.

    Returns True iff a NEW alias row was inserted (idempotent — a repeat run over
    the same pool returns False). NEVER deletes the raw row.
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO signal_aliases
            (alias_signal_id, canonical_signal_id, reason, score, produced_by)
        VALUES ($1, $2, $3, $4, $5)
        ON CONFLICT (alias_signal_id, canonical_signal_id) DO NOTHING
        RETURNING alias_signal_id
        """,
        alias_id, canonical_id, _REASON, score, produced_by,
    )
    await conn.execute(
        "UPDATE signals SET canonical_signal_id = $2, updated_at = NOW() "
        "WHERE id = $1",
        alias_id, canonical_id,
    )
    return inserted is not None


async def _fetch_window_rows(
    pool: Any,
    *,
    window_hours: int,
    owner_tenant: str | None,
    max_signals: int,
) -> list[dict[str, Any]]:
    """Recent canonical (un-aliased) signals across all sources in the window.

    A coalesce candidate is a signal that:
      * was fetched within ``window_hours``,
      * is not already an alias of another signal
        (``canonical_signal_id IS NULL OR canonical_signal_id = id``),
      * has a non-empty title in its payload (no title → no tier-4 guard).
    Ordered earliest-first so the deterministic canonical falls out naturally.
    """
    async with pool.acquire() as conn:
        tenant_clause = ""
        params: list[Any] = []
        if owner_tenant is not None:
            tenant_clause = "AND owner_tenant = $1"
            params = [owner_tenant]
        rows = await conn.fetch(
            f"""
            SELECT id, source_id, fetched_at, payload, content_hash
            FROM signals
            WHERE fetched_at > NOW() - INTERVAL '{int(window_hours)} hours'
              AND (canonical_signal_id IS NULL OR canonical_signal_id = id)
              AND COALESCE(payload->>'title', '') <> ''
              {tenant_clause}
            ORDER BY fetched_at ASC, id ASC
            LIMIT {int(max_signals)}
            """,
            *params,
        )
        out: list[dict[str, Any]] = []
        for r in rows:
            d = dict(r)
            # asyncpg returns jsonb columns as a raw JSON string (no codec
            # registered on this ad-hoc pool); normalize ``payload`` to a dict so
            # the pure helpers can read title/summary uniformly with the
            # synthetic path (which passes a dict directly).
            payload = d.get("payload")
            if isinstance(payload, str):
                try:
                    d["payload"] = json.loads(payload)
                except (ValueError, TypeError):
                    d["payload"] = {}
            out.append(d)
        return out


async def _write_sets(
    pool: Any,
    sets: list[dict[str, Any]],
    *,
    produced_by: str | None,
) -> int:
    """Persist the coalesced sets as alias links. Returns aliases_linked."""
    aliases_linked = 0
    async with pool.acquire() as conn:
        for s in sets:
            canonical_id = UUID(s["canonical_signal_id"])
            # Canonical points at itself so a canonical_only subscription sees one row.
            await conn.execute(
                "UPDATE signals SET canonical_signal_id = id, updated_at = NOW() "
                "WHERE id = $1 AND canonical_signal_id IS DISTINCT FROM id",
                canonical_id,
            )
            for alias in s["alias_signal_ids"]:
                # Skip a signal that vanished or was canonicalized elsewhere mid-run.
                exists = await conn.fetchval(
                    "SELECT 1 FROM signals WHERE id = $1", UUID(alias),
                )
                if not exists:
                    continue
                did_insert = await _link_alias(
                    conn,
                    alias_id=UUID(alias),
                    canonical_id=canonical_id,
                    score=float(s.get("score") or _DEFAULT_SEMANTIC_THRESHOLD),
                    produced_by=produced_by,
                )
                if did_insert:
                    aliases_linked += 1
    return aliases_linked


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    coalesced_sets: int,
    aliases_linked: int,
    embedded: int,
    window_hours: int,
    unavailable: Any,
    sets: list[dict[str, Any]] | None,
    target_id: str | None,
) -> FindingPayload:
    if unavailable:
        title = f"Cross-source coalesce: unavailable ({unavailable})"
    else:
        title = (
            f"Cross-source coalesce: {coalesced_sets} sets, "
            f"{aliases_linked} aliases linked over {window_hours}h"
        )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"coalesced_sets={coalesced_sets}",
        f"aliases_linked={aliases_linked}",
        f"embedded={embedded}",
        f"window_hours={window_hours}",
        f"unavailable={unavailable}",
    ])
    tags = ["deterministic", SUB_HANDLER_NAME]
    if unavailable:
        tags.append("coalesce_unavailable")
    elif aliases_linked:
        tags.append("aliases_linked")
    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "coalesced_sets": coalesced_sets,
        "aliases_linked": aliases_linked,
        "embedded": embedded,
        "window_hours": window_hours,
    }
    if unavailable:
        data["unavailable"] = unavailable
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
    enabled:
        In-handler kill switch (default ``False`` — off-by-default). A registered
        descriptor is a no-op until the operator sets this True.
    window_hours:
        Temporal window to scan + the tier-4 guard width (default 24).
    semantic_threshold:
        Cosine floor for a tier-3 semantic match (default 0.92 — the dedupe
        Tier3Config default).
    title_distance_threshold:
        Normalized Levenshtein ceiling for the tier-4 title guard (default 0.35
        — looser than the ingest filter's 0.15 for cross-source re-wordings).
    qdrant_collection:
        Shared coalesce collection name (default ``legba_coalesce``).
    owner_tenant:
        Restrict the scan to one tenant (default all).
    max_signals:
        Hard ceiling on signals embedded+scanned per run (default 2000).

    Test path: when ``deps`` is None the handler reads pre-shaped ``inputs`` rows
    (each ``{id, source_id, fetched_at, payload:{title,summary}, _vector?}``) and,
    if a per-row ``_vector`` is supplied, runs the pure pairing core without any
    embedding/Qdrant backend — so the coalesce logic is testable without the
    L-122 / Qdrant deps. Rows lacking ``_vector`` are embedded via an injected
    ``options['_test_embedder']`` if present.
    """
    target_id = options.get("target_id")
    enabled = bool(options.get("enabled", False))
    window_hours = int(options.get("window_hours", _DEFAULT_WINDOW_HOURS))
    semantic_threshold = float(
        options.get("semantic_threshold", _DEFAULT_SEMANTIC_THRESHOLD)
    )
    title_distance_threshold = float(
        options.get("title_distance_threshold", _DEFAULT_TITLE_DISTANCE_THRESHOLD)
    )
    collection = str(options.get("qdrant_collection", _DEFAULT_COLLECTION))
    owner_tenant = options.get("owner_tenant")
    max_signals = int(options.get("max_signals", _DEFAULT_MAX_SIGNALS))
    produced_by = str(options.get("analyst_id") or SUB_HANDLER_NAME)

    # Off-by-default gate — a registered-but-not-enabled descriptor no-ops.
    if not enabled:
        finding = _build_finding(
            coalesced_sets=0, aliases_linked=0, embedded=0,
            window_hours=window_hours, unavailable=False, sets=[],
            target_id=target_id,
        )
        finding.data["disabled"] = True
        return AnalystMethodResult(
            finding=finding,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    pool = getattr(deps, "pg_pool", None) if deps is not None else None
    extras = getattr(deps, "extras", {}) if deps is not None else {}
    if not isinstance(extras, Mapping):
        extras = {}
    embedder = extras.get("embedding_service")
    qdrant = extras.get("qdrant")
    embed_dim = int(getattr(embedder, "dim", 1024) or 1024) if embedder is not None else 1024

    # ---- Synthetic test path (deps=None) -------------------------------
    if pool is None:
        rows = [dict(r) for r in inputs]
        vectors: dict[str, list[float]] = {}
        test_embedder = options.get("_test_embedder")
        for r in rows:
            sid = str(r.get("id") or "")
            if not sid:
                continue
            if isinstance(r.get("_vector"), (list, tuple)) and r["_vector"]:
                vectors[sid] = list(r["_vector"])
        # Rows without a precomputed vector: embed via an injected test embedder.
        if test_embedder is not None:
            missing = [r for r in rows if str(r.get("id")) not in vectors]
            embedded_now = await _embed_rows(missing, test_embedder)
            vectors.update(embedded_now)
        if not vectors:
            # No vectors AND no embedder → the unavailable path (same as prod).
            finding = _build_finding(
                coalesced_sets=0, aliases_linked=0, embedded=0,
                window_hours=window_hours,
                unavailable="no_embedding_service",
                sets=[], target_id=target_id,
            )
            return AnalystMethodResult(
                finding=finding,
                usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
            )
        sets = _coalesce_pairs(
            rows, vectors,
            semantic_threshold=semantic_threshold,
            title_distance_threshold=title_distance_threshold,
            window_hours=window_hours,
        )
        aliases_linked = sum(len(s["alias_signal_ids"]) for s in sets)
        finding = _build_finding(
            coalesced_sets=len(sets), aliases_linked=aliases_linked,
            embedded=len(vectors), window_hours=window_hours,
            unavailable=False, sets=sets, target_id=target_id,
        )
        return AnalystMethodResult(
            finding=finding,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    # ---- Live-pool path -------------------------------------------------
    # SEAM #19 guard rail: semantic coalescing REQUIRES both an embedding
    # service AND a Qdrant client. When either is missing we cannot compute
    # "same event, different words" — there is no non-vector deterministic
    # fallback (exact content_hash is cross_source_dedup's job). Degrade-not-
    # drop: emit a finding flagged unavailable naming the missing port and
    # write NOTHING — never fabricate a link.
    missing_ports = []
    if embedder is None:
        missing_ports.append("embedding_service")
    if qdrant is None:
        missing_ports.append("qdrant")
    if missing_ports:
        reason = "missing:" + ",".join(missing_ports)
        logger.warning(
            "cross_source_coalesce.unavailable %s — emitting refusal, no links written",
            reason,
        )
        finding = _build_finding(
            coalesced_sets=0, aliases_linked=0, embedded=0,
            window_hours=window_hours, unavailable=reason, sets=None,
            target_id=target_id,
        )
        return AnalystMethodResult(
            finding=finding,
            usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
        )

    try:
        rows = await _fetch_window_rows(
            pool, window_hours=window_hours, owner_tenant=owner_tenant,
            max_signals=max_signals,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("cross_source_coalesce.fetch_failed err=%s", exc)
        rows = []

    vectors = await _embed_rows(rows, embedder)
    # Persist the embeddings into the shared collection (best-effort) so the
    # vectors are reusable by consult/vector_search downstream.
    if vectors:
        await _upsert_shared_collection(qdrant, collection, vectors, rows, embed_dim)

    sets = _coalesce_pairs(
        rows, vectors,
        semantic_threshold=semantic_threshold,
        title_distance_threshold=title_distance_threshold,
        window_hours=window_hours,
    )
    try:
        aliases_linked = await _write_sets(pool, sets, produced_by=produced_by)
    except Exception as exc:  # noqa: BLE001
        logger.warning("cross_source_coalesce.write_failed err=%s", exc)
        aliases_linked = 0

    sets_for_finding = sets if len(sets) <= 100 else None
    finding = _build_finding(
        coalesced_sets=len(sets), aliases_linked=aliases_linked,
        embedded=len(vectors), window_hours=window_hours,
        unavailable=False, sets=sets_for_finding, target_id=target_id,
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "SUB_HANDLER_NAME"]

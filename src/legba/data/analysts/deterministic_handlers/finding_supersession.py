# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``finding_supersession`` sub-handler — P-FS finding-level dedup / supersession.

Fixes the live duplicate-findings problem (PIVOT_BUILD_PLAN §12, W3): analysts
re-assess an evolving situation each cadence cycle and re-emit near-duplicate
findings (live: 875 findings / 836 distinct titles, many near-dupes). P-09's
dedup is *signal*-level and does NOT cover this — supersession is the
*analysis-plane* mechanism.

What it does
------------

Clusters findings by **situation signature** and links near-dups so a newer
finding **supersedes** the prior one for the same situation, rather than the
feed accumulating a near-dup per cycle. It mirrors the P-09 ``signal_aliases``
link pattern exactly:

  * NEVER a destructive delete — both finding rows are preserved. The audit
    trail of how the assessment evolved stays intact.
  * The link is recorded in ``finding_supersessions`` (older → newer), and the
    superseded row's ``superseded_by`` pointer is stamped. The latest/canonical
    finding for a situation is the one row whose ``superseded_by IS NULL``.

Situation signature
-------------------

A deterministic grouping key per finding, in priority order:

  1. **Explicit** — ``data.situation_id`` or ``data.situation_signature`` if the
     producing analyst already bound the finding to a situation. This is the
     strong path (e.g. ``situation_detection`` / P-10 situation-scoped analysts).
  2. **Derived** — a normalized signature from the finding's entity/event/topic
     content: ``sig:<topic>|<sorted entity tokens>``. ``topic`` falls back
     through ``data.category`` → ``data.topic`` → the analyst's sub_handler. The
     entity tokens come from ``data.key_entities`` / ``data.entities`` /
     ``data.actors`` / ``data.locations`` (lowercased, deduped, sorted) so two
     findings about the same actors+topic collide regardless of phrasing/order.

A finding with no derivable signature (no explicit id, no entities, no topic
beyond a bare summary) is **not** clustered — supersession only applies to
situation-bearing findings, never to summary/metrics findings.

Semantic near-dup (best-effort) is identical in spirit to P-09: when a Qdrant
client is injected AND findings carry an ``embedding_ref`` we *could* merge
signatures by similarity. The mechanism reserves that seam (``deps.extras
['qdrant']``) but the shipped library path is the deterministic
signature-match — exactly as the contract permits ("the handler library can be
minimal").

Canonical / latest selection within a cluster is deterministic: the **most
recent** finding wins (latest ``produced_at``, tie-broken by largest ``id``),
because for an evolving situation the freshest assessment is the one the
UI/feed should surface. Older members are superseded and linked to it.

Output ``data`` keys:
    clustered_count    int — situation clusters processed (>=2 findings each)
    superseded_count   int — supersession links written this run
    latest_count       int — distinct situations with a current/latest finding
    clusters           [{situation_signature, latest_finding_id,
                         superseded_finding_ids, reason, score}]
"""

from __future__ import annotations

import json
import logging
from collections import defaultdict
from typing import Any, Mapping

from ...provenance.models import FindingPayload
from ....runtime.analyst_method import AnalystMethodResult

logger = logging.getLogger(__name__)

SUB_HANDLER_NAME = "finding_supersession"

# Reason tags written into finding_supersessions.reason.
_REASON_EXPLICIT = "situation_id"
_REASON_DERIVED = "signature_match"

# Exact signature matches are certain → score 1.0.
_EXACT_SCORE = 1.0

# Only findings produced no earlier than this many days ago are considered for
# clustering on the live path — old findings are settled history, not an
# evolving situation. Generous default; override via options['lookback_days'].
_DEFAULT_LOOKBACK_DAYS = 30
# DQ-C3 (2026-06-21): the per-run fetch cap. The OLD value (5000) combined with
# ORDER BY produced_at ASC pulled the OLDEST 5000 open findings — which are
# ~83% entity-less cross_source_dedup metric rows (no derivable signature) — so
# the fresh, clusterable country_assessor findings fell OUTSIDE the window and
# were never signature-stamped. That silently froze the whole situations leg
# (clustering re-processed 20 stale rows; all situations closed at 06-12; the
# ASSESSED-SITUATIONS grounding block went empty; thematic_proposal starved).
# Fix: fetch NEWEST-first and lift the cap above the real open-finding pool
# (~19k) so it is a pure safety valve, not a starvation window. ROOT follow-up:
# cross_source_dedup metric outputs should be TRACE_ONLY (not kind='finding'),
# which would shrink this pool ~83% — tracked separately.
_MAX_FINDINGS = 50000


# ---------------------------------------------------------------------------
# Situation signature derivation (shared by live + synthetic paths)
# ---------------------------------------------------------------------------


def _parse_data(raw: Any) -> dict[str, Any]:
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except (json.JSONDecodeError, TypeError):
            return {}
    return {}


def _entity_tokens(data: Mapping[str, Any]) -> list[str]:
    """Normalized entity tokens from the common entity-bearing keys.

    Reads BOTH the top-level finding dump AND its nested ``data`` payload
    sub-dict. The persisted ``analyst_outputs.data`` column is the full
    payload model_dump (``FindingPayload`` is ``extra='forbid'``), so an
    LLM analyst's structured entities live in ``data->'data'->'key_entities'``
    (the inline_target producer sets them there), while a deterministic
    finding may carry them at the top level. Deterministic METRICS findings
    (whose inner dict holds only counts — canonical_count, edges_upserted,
    issues, …) match none of these keys and so yield no tokens here, which is
    exactly the scoping we want: they never cluster.
    """
    tokens: set[str] = set()
    inner = data.get("data")
    sources = (data, inner) if isinstance(inner, Mapping) else (data,)
    for src in sources:
        for key in ("key_entities", "entities", "actors", "locations", "geo", "geo_countries"):
            vals = src.get(key)
            if not vals:
                continue
            if isinstance(vals, (str, bytes)):
                vals = [vals]
            for v in vals:
                t = str(v).strip().lower()
                if len(t) >= 2:
                    tokens.add(t)
    return sorted(tokens)


def _topic(data: Mapping[str, Any], fallback: str | None) -> str:
    inner = data.get("data")
    sources = (data, inner) if isinstance(inner, Mapping) else (data,)
    for src in sources:
        for key in ("category", "topic", "situation_kind", "event_type"):
            v = src.get(key)
            if v:
                return str(v).strip().lower()
    return (fallback or "").strip().lower()


# How many of a finding's entity tokens enter the situation signature.
# 0 = topic-only (the coarsest, most STABLE key): every finding sharing a topic
# clusters into one evolving situation that the lifecycle decay then makes
# breathe (active → dormant → closed → reopen). The previous full-entity-set
# key was the reason only 1 situation formed from 11.5k findings — two findings
# about the SAME evolving event carry slightly different entity lists each
# cycle, so they hashed to different situations and nothing clustered. A small
# positive K (e.g. 2) keeps some entity granularity at the cost of stability;
# 0 is the default because robustness ("situations actually form + breathe")
# is the goal. Finer event-level clustering would need a richer producer signal
# (an event_type, or embeddings) — tracked as a future enhancement.
_SITUATION_SIGNATURE_ENTITY_K = 0


def derive_signature(
    data: Mapping[str, Any],
    *,
    sub_handler_fallback: str | None = None,
) -> str | None:
    """Deterministic situation signature for a finding, or ``None``.

    Priority:
      1. Explicit ``situation_id`` / ``situation_signature`` on ``data``.
      2. Derived ``sig:<topic>[|<top-K entity tokens>]`` — only when there is at
         least one entity token (so a bare summary finding never clusters).

    ``None`` means "do not cluster this finding".
    """
    explicit = data.get("situation_signature") or data.get("situation_id")
    if explicit:
        return f"sit:{str(explicit).strip()}"

    tokens = _entity_tokens(data)
    if not tokens:
        # Entity gate: a finding with no resolvable entities never clusters
        # (keeps deterministic metrics findings out). Unchanged.
        return None
    topic = _topic(data, sub_handler_fallback)
    if not topic:
        # No topic — anchor on the single strongest entity so entity-only
        # findings still cluster, just loosely.
        topic = tokens[0]
    if _SITUATION_SIGNATURE_ENTITY_K > 0:
        key_tokens = tokens[:_SITUATION_SIGNATURE_ENTITY_K]
        return f"sig:{topic}|{','.join(key_tokens)}"
    return f"sig:{topic}"


# ---------------------------------------------------------------------------
# Clustering core (operates on normalized finding rows)
# ---------------------------------------------------------------------------


def _cluster(
    findings: list[dict[str, Any]],
    *,
    sub_handler_fallback: str | None,
) -> dict[str, list[dict[str, Any]]]:
    """Group findings by derived situation signature (only multi-member groups).

    Each finding dict must carry ``id``, ``produced_at`` (comparable) and either
    an already-computed ``situation_signature`` or ``data`` to derive from.
    """
    # Cluster key is (situation_signature, analyst_id): a finding supersedes only
    # PRIOR findings of the SAME analyst within a situation. Different analysts
    # sharing a target-level signature (e.g. the 4 bounded units all stamped
    # `sig:country_g20_us`) are DIFFERENT dimensions and must NOT supersede each
    # other — a country's leadership read is not made stale by its narrative read.
    # (For the single-analyst-per-signature monolith this is a no-op.)
    groups: dict[tuple[str, Any], list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        sig = f.get("situation_signature")
        if not sig:
            sig = derive_signature(
                _parse_data(f.get("data")), sub_handler_fallback=sub_handler_fallback,
            )
        if not sig:
            continue
        f["_sig"] = sig
        groups[(sig, f.get("analyst_id"))].append(f)
    # Only (signature, analyst) keys with >1 finding are supersession candidates.
    return {key: rows for key, rows in groups.items() if len(rows) > 1}


def _pick_latest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic latest: newest ``produced_at`` then largest ``id``.

    For an evolving situation the freshest assessment is canonical.
    """
    def _key(r: dict[str, Any]) -> tuple[Any, str]:
        return (r.get("produced_at"), str(r.get("id")))

    return max(rows, key=_key)


# ---------------------------------------------------------------------------
# Live-pool path (asyncpg)
# ---------------------------------------------------------------------------


async def _link_supersession(
    conn: Any,
    *,
    superseded_id: Any,
    superseding_id: Any,
    situation_signature: str,
    reason: str,
    score: float,
    produced_by: str | None,
) -> bool:
    """Write one supersession link + stamp the superseded row's pointer.

    Returns True iff a NEW link row was inserted (idempotent — a repeat run over
    the same cluster returns False). NEVER deletes a finding row.
    """
    inserted = await conn.fetchval(
        """
        INSERT INTO finding_supersessions
            (superseded_finding_id, superseding_finding_id,
             situation_signature, reason, score, produced_by)
        VALUES ($1, $2, $3, $4, $5, $6)
        ON CONFLICT (superseded_finding_id, superseding_finding_id) DO NOTHING
        RETURNING superseded_finding_id
        """,
        superseded_id, superseding_id, situation_signature, reason, score, produced_by,
    )
    # Stamp the superseded row's pointer (idempotent). NEVER deletes the row —
    # only sets the link columns + the cluster signature for fast reads.
    await conn.execute(
        """
        UPDATE analyst_outputs
        SET superseded_by = $2,
            superseded_at = NOW(),
            situation_signature = $3
        WHERE id = $1
        """,
        superseded_id, superseding_id, situation_signature,
    )
    return inserted is not None


async def _fetch_findings(
    conn: Any,
    *,
    lookback_days: int,
    analyst_id: str | None,
    owner_tenant: str | None,
) -> list[dict[str, Any]]:
    """Recent findings still eligible for supersession.

    Only rows that are NOT already superseded (``superseded_by IS NULL``) are
    pulled — once a finding is superseded it stays history, so a re-run only
    ever re-clusters the currently-live set + any new arrivals. ``owner_tenant``
    scopes via the finding's ``data->>'owner_tenant'`` when present (findings
    don't carry a typed tenant column on analyst_outputs).
    """
    clauses = [
        "kind = 'finding'",
        "superseded_by IS NULL",
        f"produced_at > NOW() - INTERVAL '{int(lookback_days)} days'",
    ]
    params: list[Any] = []
    if analyst_id:
        params.append(analyst_id)
        clauses.append(f"analyst_id = ${len(params)}")
    if owner_tenant:
        params.append(owner_tenant)
        clauses.append(f"(data->>'owner_tenant') = ${len(params)}")
    params.append(_MAX_FINDINGS)
    where = " AND ".join(clauses)
    rows = await conn.fetch(
        f"""
        SELECT id, title, data, produced_at, situation_signature, analyst_id
        FROM analyst_outputs
        WHERE {where}
        ORDER BY produced_at DESC, id DESC
        LIMIT ${len(params)}
        """,
        *params,
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append({
            "id": r["id"],
            "title": r["title"],
            "data": r["data"],
            "produced_at": r["produced_at"],
            "situation_signature": r["situation_signature"],
            "analyst_id": r["analyst_id"],
        })
    return out


async def _resolve_pool(
    pool: Any,
    *,
    produced_by: str | None,
    analyst_id: str | None,
    owner_tenant: str | None,
    lookback_days: int,
    sub_handler_fallback: str | None,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """Supersession over the live ``analyst_outputs`` pool.

    Returns ``(clustered_count, superseded_count, latest_count, clusters)``.
    """
    clustered_count = 0
    superseded_count = 0
    clusters: list[dict[str, Any]] = []

    async with pool.acquire() as conn:
        findings = await _fetch_findings(
            conn,
            lookback_days=lookback_days,
            analyst_id=analyst_id,
            owner_tenant=owner_tenant,
        )
        groups = _cluster(findings, sub_handler_fallback=sub_handler_fallback)

        for (sig, _cluster_analyst_id), rows in groups.items():
            latest = _pick_latest(rows)
            latest_id = latest["id"]
            reason = (
                _REASON_EXPLICIT if sig.startswith("sit:") else _REASON_DERIVED
            )
            # Stamp the latest row's signature so the latest-per-situation read
            # (superseded_by IS NULL + situation_signature) finds it.
            await conn.execute(
                """
                UPDATE analyst_outputs
                SET situation_signature = $2
                WHERE id = $1 AND situation_signature IS DISTINCT FROM $2
                """,
                latest_id, sig,
            )
            clustered_count += 1
            superseded_now: list[str] = []
            for r in rows:
                if r["id"] == latest_id:
                    continue
                did_insert = await _link_supersession(
                    conn,
                    superseded_id=r["id"],
                    superseding_id=latest_id,
                    situation_signature=sig,
                    reason=reason,
                    score=_EXACT_SCORE,
                    produced_by=produced_by,
                )
                if did_insert:
                    superseded_count += 1
                superseded_now.append(str(r["id"]))
            clusters.append({
                "situation_signature": sig,
                "latest_finding_id": str(latest_id),
                "superseded_finding_ids": superseded_now,
                "reason": reason,
                "score": _EXACT_SCORE,
            })

        # latest_count: distinct live situations after this run.
        latest_count = await conn.fetchval(
            "SELECT COUNT(DISTINCT situation_signature) FROM analyst_outputs "
            "WHERE kind='finding' AND situation_signature IS NOT NULL "
            "AND superseded_by IS NULL"
        ) or 0

    return clustered_count, superseded_count, int(latest_count), clusters


# ---------------------------------------------------------------------------
# Synthetic-input path (unit tests, no substrate)
# ---------------------------------------------------------------------------


def _resolve_synthetic(
    inputs: list[dict[str, Any]],
    *,
    sub_handler_fallback: str | None,
) -> tuple[int, int, int, list[dict[str, Any]]]:
    """Signature clustering over pre-shaped finding rows (deps=None path).

    Input row shape:
        {"id": str|UUID, "produced_at": comparable,
         "data": {...} | "situation_signature": str}

    Returns ``(clustered_count, superseded_count, latest_count, clusters)``.
    """
    rows = [dict(r) for r in inputs]
    groups = _cluster(rows, sub_handler_fallback=sub_handler_fallback)

    clustered_count = 0
    superseded_count = 0
    clusters: list[dict[str, Any]] = []
    live_sigs: set[str] = set()

    for (sig, _cluster_analyst_id), members in groups.items():
        latest = _pick_latest(members)
        latest_id = str(latest.get("id"))
        superseded = [str(r.get("id")) for r in members if str(r.get("id")) != latest_id]
        reason = _REASON_EXPLICIT if sig.startswith("sit:") else _REASON_DERIVED
        clustered_count += 1
        superseded_count += len(superseded)
        live_sigs.add(sig)
        clusters.append({
            "situation_signature": sig,
            "latest_finding_id": latest_id,
            "superseded_finding_ids": superseded,
            "reason": reason,
            "score": _EXACT_SCORE,
        })
    # singletons (a single finding per signature) are also "live situations".
    for r in rows:
        sig = r.get("_sig")
        if sig:
            live_sigs.add(sig)
    return clustered_count, superseded_count, len(live_sigs), clusters


# ---------------------------------------------------------------------------
# Finding assembly
# ---------------------------------------------------------------------------


def _build_finding(
    *,
    clustered_count: int,
    superseded_count: int,
    latest_count: int,
    clusters: list[dict[str, Any]] | None,
    target_id: str | None,
) -> FindingPayload:
    title = (
        f"Finding supersession: {clustered_count} situation clusters, "
        f"{superseded_count} findings superseded, {latest_count} latest"
    )
    if target_id:
        title = f"{title} for {target_id}"
    body = "\n".join([
        f"clustered_count={clustered_count}",
        f"superseded_count={superseded_count}",
        f"latest_count={latest_count}",
    ])
    tags = ["deterministic", SUB_HANDLER_NAME]
    if superseded_count:
        tags.append("findings_superseded")
    data: dict[str, Any] = {
        "sub_handler": SUB_HANDLER_NAME,
        "clustered_count": clustered_count,
        "superseded_count": superseded_count,
        "latest_count": latest_count,
    }
    if clusters is not None:
        data["clusters"] = clusters
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
    analyst_id:
        When set on the live path, scopes clustering to one analyst's findings
        (the common case: an evolving-situation analyst re-emitting each cycle).
        Omit to cluster across all finding-producing analysts (the stray
        mis-scoped-duplicate-analyst case the risk item also calls out).
    owner_tenant:
        Restrict to one tenant via ``data->>'owner_tenant'``.
    lookback_days:
        Only findings this recent are eligible (default 30). Older findings are
        settled history, not an evolving situation.
    """
    produced_by = str(options.get("analyst_id") or SUB_HANDLER_NAME)
    analyst_id = options.get("scope_analyst_id") or options.get("cluster_analyst_id")
    owner_tenant = options.get("owner_tenant")
    lookback_days = int(options.get("lookback_days", _DEFAULT_LOOKBACK_DAYS))
    sub_handler_fallback = options.get("topic_fallback")

    pool = getattr(deps, "pg_pool", None) if deps is not None else None

    if pool is not None:
        try:
            clustered_count, superseded_count, latest_count, clusters = await _resolve_pool(
                pool,
                produced_by=produced_by,
                analyst_id=analyst_id,
                owner_tenant=owner_tenant,
                lookback_days=lookback_days,
                sub_handler_fallback=sub_handler_fallback,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("finding_supersession.pool_failed err=%s", exc)
            clustered_count, superseded_count, latest_count, clusters = 0, 0, 0, []
        # Drop per-cluster detail from the finding if it's large — the link
        # rows are the source of truth; the finding is a summary.
        clusters_for_finding = clusters if len(clusters) <= 100 else None
    else:
        clustered_count, superseded_count, latest_count, clusters = _resolve_synthetic(
            inputs, sub_handler_fallback=sub_handler_fallback,
        )
        clusters_for_finding = clusters

    finding = _build_finding(
        clustered_count=clustered_count,
        superseded_count=superseded_count,
        latest_count=latest_count,
        clusters=clusters_for_finding,
        target_id=options.get("target_id"),
    )
    return AnalystMethodResult(
        finding=finding,
        usage={"prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0},
    )


__all__ = ["handle", "derive_signature", "SUB_HANDLER_NAME"]

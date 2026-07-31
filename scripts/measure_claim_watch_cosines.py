#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""measure_claim_watch_cosines.py — MEASURE (never tune) the claim_watch
vector-plane cosine distribution.

``claim_watch`` (:mod:`legba.data.analysts.deterministic_handlers.claim_watch`)
matches new signals against the standing open-question set. Its vector plane
requires ``cosine(question_thesis, signal_body) >= VECTOR_SIM_FLOOR`` before
the semantic plane contributes anything to the fused weight (import the
current value from claim_watch — this measurement RAN at the original 0.60
floor and is WHY it moved to 0.45; the plane mix below is the pre-move state:
entity=511 edges, entity+geo=66, vector+entity+geo=1). Hypothesis tested: a
LONG signal-body embedding compared against a SHORT thesis-sentence embedding
is a structural asymmetry that caps the achievable cosine regardless of how
genuinely related the pair is — and a SHORT DIGEST (title+summary) embedding
of the same signal might close most of that gap.

This script measures both sides of that question using the EXACT SAME
plane claim_watch reads at runtime — same Qdrant collection
(``legba_signals``, point id = signal id), same hosted embedder (the
``embed.primary.openai_compat`` registry stack component, resolved through
the SAME :class:`legba.runtime.registry_client.RegistryHTTPClient` +
:class:`legba.data.registry.credentials.CredentialVault` path
:func:`legba.runtime.embedding_factory.build_embedding_service_from_stack_component`
uses), same entity-linkage SQL (imported directly from ``claim_watch`` —
``_QUESTION_LINEAGE_SQL`` / ``_SIGNAL_ENTITIES_SQL``), same ``cosine_similarity``
helper.

READ-ONLY, by construction:
  * every SQL statement issued is a SELECT (no INSERT/UPDATE/DELETE anywhere
    in this file);
  * ``CredentialVault.resolve`` only SELECTs ``stack_credentials`` — it never
    writes;
  * the Qdrant calls are ``retrieve_vectors`` (read) — never ``upsert_points``;
  * the ONLY writes this script causes anywhere are the hosted embedder's own
    request logs (an inference call, not a database mutation) for the
    freshly-computed THESIS and DIGEST vectors — the STORED signal-BODY
    vectors are read back from Qdrant exactly as claim_watch itself does,
    never re-embedded.

Intended to run INSIDE the live runtime container, which already carries the
production wiring (pg / qdrant / registry / vault env):

    docker cp scripts/measure_claim_watch_cosines.py \\
        legba-legba-runtime-dapr-1:/tmp/measure_claim_watch_cosines.py
    docker exec legba-legba-runtime-dapr-1 \\
        python3 /tmp/measure_claim_watch_cosines.py \\
        --desks country_g20_cn,country_g20_kr,country_g20_de,country_watch_tw

It can also run against exposed host ports (postgres:5432, qdrant:6333,
registry:8090 all published to 127.0.0.1 in the dev compose stack) provided
``LEGBA_DATA_MASTER_KEY`` / ``LEGBA_REGISTRY_API_TOKEN`` / the rest of the
``LEGBA_DATA_PG_*`` env are exported into the shell first — the script itself
never hardcodes or prints any credential.

METHOD
------
1. For each ``--desks`` entry (a ``target_descriptors`` head row):
   * load its ``body.scope.geo`` (the same ``_DESK_GEO_SQL`` claim_watch
     reads);
   * load its open questions (``hypotheses.status='open_question'``,
     ``target_id = desk``), newest-first, up to ``--questions-per-desk``;
   * load its most-recently-embedded signals (``embedding_ref`` set and not a
     degrade sentinel, geo intersecting the desk scope), newest-first, up to
     ``--signals-per-desk``.
2. Bulk-fetch canonical entity ids for the sampled questions' lineage
   (``_QUESTION_LINEAGE_SQL``) and the sampled signals
   (``_SIGNAL_ENTITIES_SQL``) — the SAME two queries claim_watch runs.
3. Embed every sampled question's thesis (bounded to
   ``_MAX_QUESTION_EMBED_CHARS``, exactly the claim_watch contract) and every
   sampled signal's DIGEST (``title`` + ``summary``, already-stored fields —
   no new column, no new sweep). Retrieve every sampled signal's STORED
   full-body vector from Qdrant (never re-embedded).
4. Cosine every sampled signal against EVERY sampled question across ALL
   desks (claim_watch itself does not scope matching to one desk — it loads
   the whole open-question set and compares every new signal against all of
   it), for both the stored BODY vector and the freshly-embedded DIGEST
   vector.
5. Bucket each pair:
     - "related"  = same desk (signal geo ∩ question's desk geo) AND the
       signal shares >=1 canonical entity with the question's lineage — the
       plausibly-on-topic pairs.
     - "random"    = everything else (cross-desk, or same-desk with no
       shared entity) — the accidental-similarity noise floor the vector
       plane actually contends with on every run, because claim_watch does
       not pre-filter by desk before computing cosine.
6. Report p50/p90/p99/max (+ hit-rate at the floor under test) per bucket,
   per embedding side (body vs digest), per desk and pooled.

This script changes NOTHING: no threshold, no claim_watch.py edit, no writes
to signals/hypotheses/bearing_edges/review_flags. The tuning decision (if
any) is reserved for a human reviewing these numbers against the existing
decision tree.
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence
from uuid import UUID

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv optional
    pass

import numpy as np

from legba.data.analysts.deterministic_handlers.claim_watch import (
    _DESK_GEO_SQL,
    _MAX_QUESTION_EMBED_CHARS,
    _MAX_VECTOR_FETCH_IDS,
    _OPEN_QUESTIONS_SQL,
    _QUESTION_LINEAGE_SQL,
    _SIGNAL_ENTITIES_SQL,
    DEFAULT_MATCH_THRESHOLD,
    VECTOR_SIM_FLOOR,
    cosine_similarity,
)
from legba.data.config import PostgresConfig, QdrantConfig
from legba.data.postgres import PostgresStore
from legba.data.qdrant import QdrantStore
from legba.data.registry.credentials import CredentialVault
from legba.runtime.embedding_factory import (
    build_embedding_service_from_stack_component,
)
from legba.runtime.registry_client import RegistryHTTPClient

EMBED_COMPONENT_ID = "embed.primary.openai_compat"

#: The 4 desks the task calls out as "many open questions" — 2 high-signal-
#: volume desks (CN/KR), 1 mid (DE), 1 low (TW) so the sample spans the
#: entity-graph density claim_watch's specificity model already worries about.
DEFAULT_DESKS = (
    "country_g20_cn",
    "country_g20_kr",
    "country_g20_de",
    "country_watch_tw",
)

_RECENT_EMBEDDED_SIGNALS_SQL = """
    SELECT id, fetched_at, geo, payload
      FROM signals
     WHERE geo && $1::text[]
       AND embedding_ref IS NOT NULL
       AND embedding_ref NOT IN ('no_body', 'embed_failed')
     ORDER BY fetched_at DESC
     LIMIT $2
"""

# Digest input bound — generous for a title+summary, never remotely close to
# the 8000-char full-body cap signal_embedder uses, kept here only as a
# defensive ceiling (a pathological payload can't overrun the gateway).
_MAX_DIGEST_CHARS = 4000


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class Question:
    id: str
    desk: str
    thesis: str
    produced_at: Any


@dataclass
class SampledSignal:
    id: str
    desk: str  # the desk it was SAMPLED for (its geo intersected that desk)
    geo: set[str]
    title: str | None
    summary: str | None
    digest_text: str | None


@dataclass
class Sample:
    desks: dict[str, set[str]] = field(default_factory=dict)  # desk -> geo set
    questions: dict[str, Question] = field(default_factory=dict)
    signals: dict[str, SampledSignal] = field(default_factory=dict)
    q_entities: dict[str, set[str]] = field(default_factory=dict)
    s_entities: dict[str, set[str]] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Wiring (identical path to production — see module docstring)
# ---------------------------------------------------------------------------


async def _secrets_resolve_factory(vault: CredentialVault):
    async def _resolve(secret_id: str) -> bytes:
        return await vault.resolve(secret_id)

    return _resolve


async def build_plane() -> tuple[PostgresStore, Any, QdrantStore]:
    """(pg store, hosted embedder, qdrant store) — same construction path
    :func:`legba.runtime.analyst_deps_builder._wire_signal_embedder` uses at
    bring-up (registry lookup -> vault-resolved api_key -> HostedEmbeddingClient),
    and the same :class:`QdrantStore` config the claim_watch vector plane
    reads signal vectors back from."""
    pg = PostgresStore(PostgresConfig.from_env())
    await pg.connect()
    vault = CredentialVault(pg)
    resolve = await _secrets_resolve_factory(vault)
    registry = RegistryHTTPClient()
    embedder = await build_embedding_service_from_stack_component(
        EMBED_COMPONENT_ID,
        registry_client=registry,
        secrets_resolve=resolve,
    )
    qdrant = QdrantStore(QdrantConfig.from_env())
    await qdrant.connect()
    return pg, embedder, qdrant


# ---------------------------------------------------------------------------
# Sampling (read-only SELECTs only)
# ---------------------------------------------------------------------------


def _digest_text(payload: Mapping[str, Any]) -> str | None:
    title = payload.get("title")
    summary = payload.get("summary")
    parts = []
    if isinstance(title, str) and title.strip():
        parts.append(title.strip())
    if isinstance(summary, str) and summary.strip():
        parts.append(summary.strip())
    if not parts:
        return None
    return " — ".join(parts)[:_MAX_DIGEST_CHARS]


async def load_all_open_questions(conn: Any) -> dict[str, list[Any]]:
    """ALL open-question rows in ONE call (477 live at measurement time —
    cheap), newest-first, grouped by ``target_id``. Fetching once up front
    (rather than re-querying per desk with an arbitrary top-N window) avoids
    a desk's older-dated questions falling outside a shared "recent" window
    just because other sampled desks are more active."""
    rows = await conn.fetch(_OPEN_QUESTIONS_SQL, ["open_question"], 5000)
    by_desk: dict[str, list[Any]] = {}
    for r in rows:
        by_desk.setdefault(str(r["target_id"] or ""), []).append(r)
    return by_desk


async def sample_desk(
    conn: Any,
    desk: str,
    *,
    questions_per_desk: int,
    signals_per_desk: int,
    sample: Sample,
    all_open_questions: dict[str, list[Any]],
) -> None:
    geo_rows = await conn.fetch(_DESK_GEO_SQL, [desk])
    geo: set[str] = set()
    for r in geo_rows:
        raw = r["geo"]
        if isinstance(raw, str):
            import json as _json

            try:
                raw = _json.loads(raw)
            except Exception:
                raw = None
        if isinstance(raw, list):
            geo = {str(g).strip().upper() for g in raw if str(g).strip()}
    sample.desks[desk] = geo

    for r in all_open_questions.get(desk, [])[:questions_per_desk]:
        sample.questions[str(r["id"])] = Question(
            id=str(r["id"]),
            desk=desk,
            thesis=str(r["thesis"] or ""),
            produced_at=r["produced_at"],
        )

    if not geo:
        print(f"  [{desk}] WARNING: no desk geo scope found — signal sample skipped")
        return
    s_rows = await conn.fetch(
        _RECENT_EMBEDDED_SIGNALS_SQL, sorted(geo), signals_per_desk
    )
    for r in s_rows:
        payload = r["payload"] or {}
        if isinstance(payload, str):
            import json as _json

            try:
                payload = _json.loads(payload)
            except Exception:
                payload = {}
        sample.signals[str(r["id"])] = SampledSignal(
            id=str(r["id"]),
            desk=desk,
            geo={str(g).strip().upper() for g in (r["geo"] or []) if str(g).strip()},
            title=payload.get("title") if isinstance(payload.get("title"), str) else None,
            summary=payload.get("summary") if isinstance(payload.get("summary"), str) else None,
            digest_text=_digest_text(payload),
        )


async def load_entity_links(conn: Any, sample: Sample) -> None:
    """Bulk entity-id fetch for the sampled questions' lineage + sampled
    signals — the EXACT two SQL statements claim_watch.py runs
    (``_QUESTION_LINEAGE_SQL`` / ``_SIGNAL_ENTITIES_SQL``), reused verbatim
    rather than re-derived, so "shares an entity" here means what it means in
    production."""
    qids = [UUID(qid) for qid in sample.questions]
    if qids:
        rows = await conn.fetch(_QUESTION_LINEAGE_SQL, qids)
        for r in rows:
            qid = str(r["qid"])
            ents = {str(e) for e in (r["entity_ids"] or []) if e is not None}
            sample.q_entities[qid] = ents
    sids = [UUID(sid) for sid in sample.signals]
    if sids:
        rows = await conn.fetch(_SIGNAL_ENTITIES_SQL, sids)
        for r in rows:
            sid = str(r["sid"])
            ents = {str(e) for e in (r["entity_ids"] or []) if e is not None}
            sample.s_entities[sid] = ents


# ---------------------------------------------------------------------------
# Embedding (same client, same call shape as production)
# ---------------------------------------------------------------------------


async def _embed_bounded(
    embedder: Any, texts: dict[str, str], *, char_cap: int, concurrency: int = 8
) -> dict[str, list[float]]:
    """{key: vector} for every non-empty text, via ``embedder.embed`` (the
    SAME hosted client claim_watch / signal_embedder call). Bounded
    concurrency — a courtesy to the shared gateway, not a hard production
    contract (this is an offline measurement, not a cadence tick)."""
    sem = asyncio.Semaphore(concurrency)
    out: dict[str, list[float]] = {}

    async def _one(key: str, text: str) -> None:
        async with sem:
            try:
                vec = await asyncio.wait_for(
                    embedder.embed(text[:char_cap]), timeout=30.0
                )
            except Exception as exc:  # noqa: BLE001 — measurement, degrade loud
                print(f"  embed failed key={key} err={exc}")
                return
            if vec:
                out[key] = list(vec)

    await asyncio.gather(
        *(_one(k, t) for k, t in texts.items() if t and t.strip())
    )
    return out


async def fetch_body_vectors(
    qdrant: QdrantStore, signal_ids: Sequence[str]
) -> dict[str, list[float]]:
    """Stored FULL-BODY vectors, read back by point id — never re-embedded.
    Chunked exactly like claim_watch's ``_fetch_signal_vectors``."""
    out: dict[str, list[float]] = {}
    collection = qdrant.cfg.signals_collection
    ids = list(signal_ids)
    for start in range(0, len(ids), _MAX_VECTOR_FETCH_IDS):
        chunk = ids[start : start + _MAX_VECTOR_FETCH_IDS]
        got = await qdrant.retrieve_vectors(collection, chunk)
        out.update(got)
    return out


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


def _pctile(values: np.ndarray, q: float) -> float:
    if values.size == 0:
        return float("nan")
    return float(np.percentile(values, q))


def _summ(values: list[float]) -> dict[str, float]:
    arr = np.asarray(values, dtype=float)
    if arr.size == 0:
        return {"n": 0, "p50": float("nan"), "p90": float("nan"),
                "p99": float("nan"), "max": float("nan"),
                "hit_rate_060": float("nan")}
    return {
        "n": int(arr.size),
        "p50": _pctile(arr, 50),
        "p90": _pctile(arr, 90),
        "p99": _pctile(arr, 99),
        "max": float(arr.max()),
        "hit_rate_060": float((arr >= VECTOR_SIM_FLOOR).mean()),
    }


def _fmt_row(label: str, s: dict[str, float]) -> str:
    if s["n"] == 0:
        return f"  {label:<28} n=0 (no pairs)"
    return (
        f"  {label:<28} n={s['n']:>6}  p50={s['p50']:.3f}  p90={s['p90']:.3f}  "
        f"p99={s['p99']:.3f}  max={s['max']:.3f}  "
        f"hit@{VECTOR_SIM_FLOOR:.2f}={s['hit_rate_060']*100:5.1f}%"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def run(args: argparse.Namespace) -> None:
    desks = [d.strip() for d in args.desks.split(",") if d.strip()]
    pg, embedder, qdrant = await build_plane()
    sample = Sample()
    try:
        async with pg.acquire() as conn:
            print(f"Sampling {len(desks)} desk(s): {', '.join(desks)}")
            all_open_questions = await load_all_open_questions(conn)
            for desk in desks:
                await sample_desk(
                    conn,
                    desk,
                    questions_per_desk=args.questions_per_desk,
                    signals_per_desk=args.signals_per_desk,
                    sample=sample,
                    all_open_questions=all_open_questions,
                )
                print(
                    f"  [{desk}] geo={sorted(sample.desks.get(desk, set()))} "
                    f"questions={sum(1 for q in sample.questions.values() if q.desk == desk)} "
                    f"signals={sum(1 for s in sample.signals.values() if s.desk == desk)}"
                )
            await load_entity_links(conn, sample)

        n_q, n_s = len(sample.questions), len(sample.signals)
        print(f"\nTotal sampled: {n_q} question(s), {n_s} signal(s) "
              f"-> {n_q * n_s} candidate pairs")

        # -- embeddings --------------------------------------------------
        thesis_texts = {
            qid: q.thesis[:_MAX_QUESTION_EMBED_CHARS]
            for qid, q in sample.questions.items()
        }
        digest_texts = {
            sid: s.digest_text for sid, s in sample.signals.items() if s.digest_text
        }
        print(f"Embedding {len(thesis_texts)} thesis text(s) "
              f"(bounded {_MAX_QUESTION_EMBED_CHARS} chars, same contract as "
              f"claim_watch._embed_questions)...")
        thesis_vecs = await _embed_bounded(
            embedder, thesis_texts, char_cap=_MAX_QUESTION_EMBED_CHARS
        )
        print(f"Embedding {len(digest_texts)} signal DIGEST text(s) "
              f"(title+summary, bounded {_MAX_DIGEST_CHARS} chars)...")
        digest_vecs = await _embed_bounded(
            embedder, digest_texts, char_cap=_MAX_DIGEST_CHARS
        )
        print(f"Retrieving {n_s} stored full-BODY vector(s) from Qdrant "
              f"(collection={qdrant.cfg.signals_collection}, never re-embedded)...")
        body_vecs = await fetch_body_vectors(qdrant, list(sample.signals))

        print(f"\nCoverage: thesis={len(thesis_vecs)}/{n_q}  "
              f"digest={len(digest_vecs)}/{n_s}  body={len(body_vecs)}/{n_s}")

        # -- pair classification + cosine ---------------------------------
        buckets: dict[tuple[str, str, str], list[float]] = {}
        # key = (desk, bucket, side) -> [cosines]; desk == "__all__" for pooled

        for qid, q in sample.questions.items():
            t_vec = thesis_vecs.get(qid)
            if t_vec is None:
                continue
            q_ents = sample.q_entities.get(qid, set())
            desk_geo = sample.desks.get(q.desk, set())
            for sid, s in sample.signals.items():
                s_ents = sample.s_entities.get(sid, set())
                same_desk = bool(desk_geo) and bool(s.geo & desk_geo)
                n_shared = len(q_ents & s_ents)
                # "related" per the task spec (same desk + >=1 shared entity);
                # "related_strong" narrows to >=2 shared entities — a SINGLE
                # shared entity on a country desk is often just the desk's own
                # headline name (co-membership, not evidence — the same
                # observation claim_watch's own entity_specificity rule is
                # built to correct for), so it is reported separately rather
                # than folded silently into "related".
                if same_desk and n_shared >= 1:
                    bucket_names = ["related"]
                    if n_shared >= 2:
                        bucket_names.append("related_strong")
                else:
                    bucket_names = ["random"]

                b_vec = body_vecs.get(sid)
                if b_vec is not None:
                    cos = cosine_similarity(t_vec, b_vec)
                    for bucket_name in bucket_names:
                        buckets.setdefault((q.desk, bucket_name, "body"), []).append(cos)
                        buckets.setdefault(("__all__", bucket_name, "body"), []).append(cos)

                d_vec = digest_vecs.get(sid)
                if d_vec is not None:
                    cos = cosine_similarity(t_vec, d_vec)
                    for bucket_name in bucket_names:
                        buckets.setdefault((q.desk, bucket_name, "digest"), []).append(cos)
                        buckets.setdefault(("__all__", bucket_name, "digest"), []).append(cos)

        # -- report --------------------------------------------------------
        print("\n" + "=" * 88)
        print(f"claim_watch vector-plane cosine measurement "
              f"(VECTOR_SIM_FLOOR={VECTOR_SIM_FLOOR}, "
              f"DEFAULT_MATCH_THRESHOLD={DEFAULT_MATCH_THRESHOLD})")
        print("=" * 88)

        for desk in ["__all__"] + desks:
            label = "POOLED (all desks)" if desk == "__all__" else desk
            print(f"\n-- {label} --")
            for bucket_name in ("related", "related_strong", "random"):
                for side in ("body", "digest"):
                    vals = buckets.get((desk, bucket_name, side), [])
                    row_label = f"{bucket_name}/{side}"
                    print(_fmt_row(row_label, _summ(vals)))

        # -- alternative-floor readout --------------------------------------
        all_random_body = buckets.get(("__all__", "random", "body"), [])
        all_related_body = buckets.get(("__all__", "related", "body"), [])
        all_random_digest = buckets.get(("__all__", "random", "digest"), [])
        all_related_digest = buckets.get(("__all__", "related", "digest"), [])
        print("\n" + "-" * 88)
        print("Alternative-floor readout (pooled, body side — what claim_watch "
              "actually compares):")
        if all_random_body:
            p90_random = _pctile(np.asarray(all_random_body), 90)
            print(f"  p90(random/body) = {p90_random:.3f}  "
                  f"-> p90+0.05 margin = {p90_random + 0.05:.3f}  "
                  f"-> p90+0.10 margin = {p90_random + 0.10:.3f}")
        if all_related_body:
            print(f"  p50(related/body) = {_pctile(np.asarray(all_related_body), 50):.3f}  "
                  f"p90(related/body) = {_pctile(np.asarray(all_related_body), 90):.3f}")
        print("Same readout, digest side (title+summary):")
        if all_random_digest:
            p90_random_d = _pctile(np.asarray(all_random_digest), 90)
            print(f"  p90(random/digest) = {p90_random_d:.3f}  "
                  f"-> p90+0.05 margin = {p90_random_d + 0.05:.3f}  "
                  f"-> p90+0.10 margin = {p90_random_d + 0.10:.3f}")
        if all_related_digest:
            print(f"  p50(related/digest) = {_pctile(np.asarray(all_related_digest), 50):.3f}  "
                  f"p90(related/digest) = {_pctile(np.asarray(all_related_digest), 90):.3f}")
        print("=" * 88)
        print("\nNo threshold, no claim_watch.py code, and no production data were "
              "changed by this run — read-only measurement only.")
    finally:
        await pg.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--desks", default=",".join(DEFAULT_DESKS),
        help="Comma-separated target_descriptors desk ids to sample.",
    )
    parser.add_argument(
        "--questions-per-desk", type=int, default=25,
        help="Max open questions sampled per desk (newest-first).",
    )
    parser.add_argument(
        "--signals-per-desk", type=int, default=60,
        help="Max already-embedded signals sampled per desk (newest-first, "
             "geo-filtered to the desk scope).",
    )
    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""reembed_world_context.py — M22 in-place re-embed of the world_context corpus.

The M22 retrieval recalibration changes the corpus EMBEDDING CONVENTION: a chunk
is now embedded as ``contextual_embedding_input(...)`` — a lean "<Country> —
<section>" context prefix + the chunk body — instead of the raw body alone. That
puts the country + topic anchor INTO the vector, lifting the top on-target cosine
~+0.05 and widening the on/off-target margin (live probe: Germany 0.553→0.606,
Brazil 0.571→0.615; off-target barely moves). The already-loaded 293 points were
embedded under the OLD convention, so they must be RE-EMBEDDED to benefit.

This script re-embeds EVERY point of the ``world_context`` collection IN PLACE:
it scrolls the collection, rebuilds each point's embedding input from its STORED
payload (``title`` / ``section`` / ``countries`` / ``text`` — the exact fields the
Lane-4 loader wrote, via the SAME ``contextual_embedding_input`` helper so the two
paths never drift), re-embeds, and re-upserts the SAME point id with the SAME
payload (only the vector changes). Deterministic point ids make this an overwrite,
never a duplicate — and because the payload is preserved, chunk text / provenance /
country tags are untouched.

This is the vector lane's documented DELETE-EXCEPTION territory (vector rows are
derived, re-embeddable artifacts), but it does NOT delete: it overwrites in place.

Run it in the REGISTRY / RUNTIME container (same env as manual_ingest_vectors.py
and bringup — needs ``LEGBA_DATA_PG_DB=legba`` for the vault, the vault master key,
the registry URL, and Qdrant reachable):

    # DRY-RUN — scroll + report the plan + sample embedding inputs; no embed/upsert
    python3 scripts/reembed_world_context.py --dry-run

    # APPLY — re-embed + re-upsert all points in place
    python3 scripts/reembed_world_context.py

    # a different corpus collection (default world_context)
    python3 scripts/reembed_world_context.py --collection tradecraft

After it completes, re-run the retrieval sanity probe / rag_watch to confirm the
on-target scores cleared the recalibrated floor, then (optionally) tighten
LEGBA_WORLD_CONTEXT_MIN_SCORE toward 0.58-0.60.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

from legba.data.config import QdrantConfig
from legba.data.qdrant import QdrantStore
from legba.data.rag.lane4_loader import contextual_embedding_input


async def _scroll_all(store: QdrantStore, collection: str, page: int) -> list:
    """Scroll EVERY point (payload only) of ``collection`` — the re-embed corpus."""
    points: list = []
    offset = None
    while True:
        batch, offset = await store.client.scroll(
            collection_name=collection,
            limit=page,
            offset=offset,
            with_payload=True,
            with_vectors=False,
        )
        points.extend(batch)
        if offset is None or not batch:
            break
    return points


def _embedding_input_for(payload: dict) -> str:
    """The M22 embedding input for a stored point, from its payload fields."""
    return contextual_embedding_input(
        title=payload.get("title"),
        section=payload.get("section"),
        countries=payload.get("countries"),
        text=payload.get("text") or "",
    )


async def _run(*, collection: str, dry_run: bool, page: int, upsert_batch: int) -> int:
    store = QdrantStore.from_env()
    await store.connect()
    try:
        points = await _scroll_all(store, collection, page)
        total = len(points)
        print(f"scrolled {total} points from collection {collection!r}")
        if total == 0:
            print("nothing to re-embed (collection empty or missing)")
            return 0

        # Show a couple of sample embedding inputs so the operator can eyeball the
        # new convention before applying.
        for p in points[:3]:
            payload = dict(p.payload or {})
            sample = _embedding_input_for(payload)
            head = sample.split("\n\n", 1)[0]
            print(f"  sample id={p.id} embed-lead: {head!r}")

        if dry_run:
            skip = sum(1 for p in points if not (dict(p.payload or {}).get("text") or "").strip())
            print(f"DRY-RUN: would re-embed {total - skip} points "
                  f"({skip} skipped for empty text); no embed / no upsert performed.")
            return 0

        # Live path: build the registry-resolved embedder (mirrors
        # manual_ingest_vectors.py bringup).
        from legba.data.config import PostgresConfig
        from legba.data.postgres import PostgresStore
        from legba.data.registry.credentials import CredentialVault
        from legba.runtime.embedding_factory import (
            build_embedding_service_from_stack_component,
        )
        from legba.runtime.registry_client import RegistryHTTPClient

        pg_store = PostgresStore(PostgresConfig.from_env())
        await pg_store.connect()
        registry_client = RegistryHTTPClient()
        vault = CredentialVault(pg_store)

        async def _secrets_resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        embedder = await build_embedding_service_from_stack_component(
            os.environ.get("LEGBA_DATA_DEFAULT_EMBEDDING", "embed.primary.openai_compat"),
            registry_client=registry_client,
            secrets_resolve=_secrets_resolve,
        )

        written = skipped = 0
        buf: list[tuple[str, list[float], dict]] = []
        try:
            for p in points:
                payload = dict(p.payload or {})
                if not (payload.get("text") or "").strip():
                    skipped += 1
                    continue
                vec = await embedder.embed(_embedding_input_for(payload))
                # Re-upsert the SAME id with the SAME payload — only the vector
                # changes. str(p.id) keeps the deterministic uuid id stable.
                buf.append((str(p.id), vec, payload))
                if len(buf) >= upsert_batch:
                    written += await store.upsert_points(collection, buf)
                    buf = []
                    print(f"  re-embedded {written}/{total} ...", flush=True)
            if buf:
                written += await store.upsert_points(collection, buf)
        finally:
            aclose = getattr(embedder, "aclose", None)
            if aclose is not None:
                await aclose()
            await pg_store.close()

        print(f"DONE: re-embedded + re-upserted {written} points in place "
              f"({skipped} skipped for empty text). Collection {collection!r} unchanged "
              "in size; only vectors updated.")
        return 0
    finally:
        await store.close()


def main() -> int:
    p = argparse.ArgumentParser(description="M22 in-place re-embed of a RAG corpus.")
    p.add_argument("--collection", default=QdrantConfig.from_env().world_context_collection,
                   help="Qdrant collection to re-embed (default: world_context)")
    p.add_argument("--dry-run", action="store_true",
                   help="scroll + report the plan + sample embedding inputs; no writes")
    p.add_argument("--page", type=int, default=128, help="scroll page size")
    p.add_argument("--upsert-batch", type=int, default=64, help="re-upsert batch size")
    args = p.parse_args()
    return asyncio.run(_run(
        collection=args.collection, dry_run=args.dry_run,
        page=args.page, upsert_batch=args.upsert_batch,
    ))


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())

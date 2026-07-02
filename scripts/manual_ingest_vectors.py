#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""manual_ingest_vectors.py — the Lane-4 (vector corpus) manual-ingest CLI.

Loads a validated manual-ingest batch's ``docs`` lane into the Qdrant RAG
corpora (``world_context`` / ``tradecraft``): validate → chunk (heading-aware,
~400-800 tokens) → embed (the hosted ``embed.primary.openai_compat`` component)
→ upsert, riding the ``seed_batches`` ledger for idempotency.

Run it in the REGISTRY container exactly like ``migrate`` / bringup so it sees
the same LEGBA_* env (``LEGBA_DATA_PG_DB=legba`` is the known gotcha — the
default is a test DB), plus the vault master key + registry URL:

    # validate + chunk only; embed/upsert/ledger untouched (needs no GPU/DB)
    python3 scripts/manual_ingest_vectors.py --batch corpora/tl_handbook --dry-run

    # apply (skip mode — a re-run of an identical batch is a NO-OP)
    python3 scripts/manual_ingest_vectors.py --batch corpora/tl_handbook

    # force reload one batch's docs (delete-and-reload the chunks — the vector
    # lane's documented DELETE-EXCEPTION; vector rows are re-embeddable)
    python3 scripts/manual_ingest_vectors.py --batch corpora/tl_handbook --mode force

Only the ``docs`` lane is consumed here; facts/entities/nexuses/signals are the
structured lanes (S4-T2). A batch may carry both — run each loader.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

# Make `legba` importable when run from a checkout.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover — dotenv optional
    pass

from legba.data.config import QdrantConfig
from legba.data.qdrant import QdrantStore
from legba.data.rag import load_vector_batch
from legba.data.rag.lane4_loader import pg_seed_batch_ledger
from legba.data.seed.manual_schema import BatchMode, validate_batch


async def _run(batch_dir: str, *, mode: str | None, dry_run: bool, strict: bool) -> int:
    batch = validate_batch(batch_dir, strict=False)
    if not batch.ok:
        for err in batch.errors:
            print(f"INVALID {err}", file=sys.stderr)
        if strict:
            return 2
    if not batch.docs:
        print("batch declares no docs lane (nothing for Lane-4 to load)", file=sys.stderr)
        return 1

    resolved_mode = BatchMode(mode) if mode else None

    # A pure dry-run needs neither Qdrant, an embedder, nor the ledger — it
    # validates + chunks and reports the plan.
    if dry_run:
        result = await load_vector_batch(
            batch=batch,
            batch_dir=batch_dir,
            store=_DryRunStore(),
            embedder=_DryRunEmbedder(),
            ledger=None,
            mode=resolved_mode,
            dry_run=True,
        )
        print(json.dumps(result.as_dict(), indent=2))
        return 0

    # Live path: pg pool (ledger) + vault + registry-resolved embedder + Qdrant.
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
    store = QdrantStore.from_env()
    await store.connect()
    try:
        result = await load_vector_batch(
            batch=batch,
            batch_dir=batch_dir,
            store=store,
            embedder=embedder,
            ledger=pg_seed_batch_ledger(pg_store.pool),
            mode=resolved_mode,
            dry_run=False,
        )
    finally:
        await store.close()
        aclose = getattr(embedder, "aclose", None)
        if aclose is not None:
            await aclose()
        await pg_store.close()

    print(json.dumps(result.as_dict(), indent=2))
    return 0 if not result.errors else 1


class _DryRunEmbedder:
    """Never called on a dry-run (embeds are skipped), present for the type."""

    async def embed(self, text: str) -> list[float]:  # pragma: no cover
        raise RuntimeError("dry-run must not embed")


class _DryRunStore:
    """A no-op store stand-in for a dry-run (no collection ensure/upsert)."""

    cfg = QdrantConfig()


def main() -> int:
    parser = argparse.ArgumentParser(description="Legba Lane-4 vector-corpus loader.")
    parser.add_argument("--batch", required=True, help="path to a manual-ingest batch dir")
    parser.add_argument(
        "--mode",
        choices=[m.value for m in BatchMode],
        default=None,
        help="override the manifest mode (skip|merge|force). force = delete-and-reload.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate + chunk only; embed/upsert/ledger untouched",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="abort on any per-line validation error instead of loading valid records",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(args.batch, mode=args.mode, dry_run=args.dry_run, strict=args.strict)
    )


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())

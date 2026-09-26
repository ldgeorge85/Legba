#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""manual_ingest_vectors.py — the Lane-4 (vector corpus) manual-ingest CLI.

Loads a validated manual-ingest batch's ``docs`` lane into the Qdrant RAG
corpora (``world_context`` / ``tradecraft`` / ``exemplar``): validate → chunk
(heading-aware, ~400-800 tokens) → embed (the hosted ``embed.primary.openai_compat``
component) → upsert, riding the ``seed_batches`` ledger for idempotency.

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

The ``exemplar`` corpus (EXEMPLAR_SHELF_DRAFT_2026-07-31.md §4 step 4,
``source_class='template'``, NEVER live evidence) additionally takes
``--corpus exemplar --exemplar-id <id>``. The id is validated against the
curated shelf (``seeds/exemplar_shelf.yaml``, see ``legba.data.seed.exemplar_shelf``)
BEFORE anything is read or embedded: it must be a ``shelf.ready_ids()`` member
or one of a merged pattern's aliases; a RETIRED id is refused with the shelf's
own reversal note printed; a HELD (not-yet-ready) id is refused too.
``doctrine_source`` / ``research_ref`` are stamped onto every chunk's payload
straight from the matching shelf row — never typed by the operator:

    python3 scripts/manual_ingest_vectors.py \\
        --batch corpora/bts_port_performance_2026 \\
        --corpus exemplar --exemplar-id port_capacity_crisis --dry-run

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
from legba.data.rag.lane4_loader import (
    CORPUS_COLLECTIONS,
    EXEMPLAR_CORPUS,
    check_exemplar_id,
    pg_seed_batch_ledger,
)
from legba.data.seed.exemplar_shelf import ExemplarShelf, load_shelf
from legba.data.seed.manual_schema import BatchMode, validate_batch


def _preflight_corpus(
    *, corpus: str | None, exemplar_id: str | None, shelf_path: str | None
) -> tuple[int, ExemplarShelf | None]:
    """Validate ``--corpus``/``--exemplar-id`` BEFORE reading the batch dir.

    Returns ``(0, shelf)`` — ``shelf`` is the loaded exemplar shelf when
    ``corpus == 'exemplar'``, else ``None`` — on success, or ``(exit_code,
    None)`` on a refusal (the reason is already printed to stderr). Runs fast
    and reads nothing but the shelf file, so an operator gets a clear refusal
    without a batch dir even existing yet.
    """
    if exemplar_id and corpus != EXEMPLAR_CORPUS:
        print(
            f"ERROR --exemplar-id requires --corpus {EXEMPLAR_CORPUS!r}", file=sys.stderr
        )
        return 2, None
    if corpus == EXEMPLAR_CORPUS and not exemplar_id:
        print(f"ERROR --corpus {EXEMPLAR_CORPUS!r} requires --exemplar-id", file=sys.stderr)
        return 2, None
    if corpus is not None and corpus not in CORPUS_COLLECTIONS:
        print(
            f"ERROR unknown corpus {corpus!r} "
            f"(known: {', '.join(sorted(CORPUS_COLLECTIONS))})",
            file=sys.stderr,
        )
        return 2, None
    if corpus != EXEMPLAR_CORPUS:
        return 0, None

    shelf = load_shelf(shelf_path)
    check = check_exemplar_id(shelf, exemplar_id)
    if not check.ok:
        print(f"ERROR {check.error}", file=sys.stderr)
        return 2, None
    return 0, shelf


async def _run(
    batch_dir: str,
    *,
    mode: str | None,
    dry_run: bool,
    strict: bool,
    corpus: str | None = None,
    exemplar_id: str | None = None,
    shelf_path: str | None = None,
) -> int:
    # Fast pre-flight: --corpus/--exemplar-id validate before the batch dir is
    # even read (an unknown corpus or a bad exemplar_id needs neither disk nor
    # DB to refuse).
    code, shelf = _preflight_corpus(
        corpus=corpus, exemplar_id=exemplar_id, shelf_path=shelf_path
    )
    if code != 0:
        return code

    batch = validate_batch(batch_dir, strict=False)
    if not batch.ok:
        for err in batch.errors:
            print(f"INVALID {err}", file=sys.stderr)
        if strict:
            return 2
    if not batch.docs:
        print("batch declares no docs lane (nothing for Lane-4 to load)", file=sys.stderr)
        return 1

    if corpus is not None:
        mismatched = sorted({d.corpus for d in batch.docs if d.corpus != corpus})
        if mismatched:
            print(
                f"ERROR --corpus {corpus!r} given but the batch declares "
                f"{mismatched!r} (a batch loaded with --corpus must be uniform)",
                file=sys.stderr,
            )
            return 2
    if corpus == EXEMPLAR_CORPUS:
        # The loader's own check_exemplar_id is authoritative (it runs again,
        # per-doc, inside load_vector_batch); this only DEFAULTS a doc's
        # exemplar_id when its own docs.jsonl record didn't already set one —
        # an explicit per-record value is never overwritten here.
        for rec in batch.docs:
            rec.data.setdefault("exemplar_id", exemplar_id)

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
            shelf=shelf,
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
            shelf=shelf,
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
    parser.add_argument(
        "--corpus",
        default=None,
        help=(
            "restrict this run to one corpus (e.g. 'exemplar'); refuses if a doc "
            "declares a different corpus or the name is unknown"
        ),
    )
    parser.add_argument(
        "--exemplar-id",
        default=None,
        help=(
            "the shelf pattern id (or a merged pattern's alias) this batch's docs "
            "support — REQUIRED with --corpus exemplar; validated against the "
            "curated shelf before anything is read or embedded"
        ),
    )
    parser.add_argument(
        "--exemplar-shelf",
        default=None,
        help="override path to the curated shelf yaml (default: seeds/exemplar_shelf.yaml)",
    )
    args = parser.parse_args()
    return asyncio.run(
        _run(
            args.batch,
            mode=args.mode,
            dry_run=args.dry_run,
            strict=args.strict,
            corpus=args.corpus,
            exemplar_id=args.exemplar_id,
            shelf_path=args.exemplar_shelf,
        )
    )


if __name__ == "__main__":  # pragma: no cover — manual invocation
    raise SystemExit(main())

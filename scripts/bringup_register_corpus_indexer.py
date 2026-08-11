# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Corpus Indexer analyst descriptor.

Reads ``descriptors/analyst_corpus_indexer.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This wires the ``corpus_indexer`` deterministic sweep as an ONGOING analyst.
After this script runs, the runtime's reconciler spins up a deterministic actor
that, on its cadence, indexes the next batch of un-indexed signals into the
OpenSearch full-text corpus (``legba_signals_corpus``) — the INDEX PLANE of the
signal-content-depth program (a BM25 lexical mining substrate over the whole
signal body + keyword/date facets). The sweep is idempotent + forward-progressing
(stamps ``signals.indexed_at`` on every examined row; the OpenSearch ``_id`` = the
signal id, so a re-index overwrites in place); migration
0082_signal_indexed_marker.sql adds that marker + its partial scan index.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — resolved via scripts/_token.py (env → .env → dev).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bringup_http import register_create_only, registry_base  # noqa: E402
from _token import resolve_token  # noqa: E402


BASE = registry_base()
TOKEN = resolve_token()

# (family, file, descriptor_id)
TO_REGISTER = [
    (
        "analyst",
        "analyst_corpus_indexer.yaml",
        "corpus_indexer",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

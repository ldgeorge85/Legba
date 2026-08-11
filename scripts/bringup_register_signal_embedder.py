# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Signal Embedder analyst descriptor.

Reads ``descriptors/analyst_signal_embedder.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This wires the ``signal_embedder`` deterministic sweep as an ONGOING analyst.
After this script runs, the runtime's reconciler spins up a deterministic actor
that, on its cadence, embeds the next batch of un-embedded signals into the
Qdrant ``legba_signals`` collection — the VECTOR PLANE of the signal-content-depth
program (a semantic-retrieval substrate that lights up ``vector_search``, which
no-ops today because the collection holds 0 points). The sweep is idempotent +
forward-progressing (stamps ``signals.embedding_ref`` on every examined row; the
Qdrant point ``_id`` = the signal id, so a re-embed overwrites in place);
migration 0084_signal_embedding_marker.sql adds the partial scan index over the
un-embedded pool (the embedding_ref marker column already exists in the baseline).

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
        "analyst_signal_embedder.yaml",
        "signal_embedder",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Corpus Retention analyst descriptor.

Reads ``descriptors/analyst_corpus_retention.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This wires the ``corpus_retention`` deterministic sweep as an ONGOING analyst —
the DELETE half of the corpus plane, and ``corpus_indexer``'s mirror. After this
script runs, the runtime's reconciler spins up a deterministic actor that, on its
cadence, drains ``corpus_tombstones`` (migration 0175) by deleting the
corresponding docs from ``legba_signals_corpus``. Until 2026-08-03 that half did
not exist at all: ``OpenSearchStore`` had no delete surface, so every signals
purge left its documents behind (measured: 75,871 of 182,648 docs, 41.5%,
referencing rows that no longer existed).

SAFE TO REGISTER BEFORE THE BACKFILL, AND INERT UNTIL IT RUNS. The sweep's unit
of work is the tombstone queue, and migration 0175 creates that table EMPTY. So
a freshly deployed corpus_retention finds nothing and no-ops every tick. It only
starts deleting when something fills the queue — either a live purge (which now
tombstones transactionally) or the operator running

    python scripts/seed_corpus_orphan_tombstones.py --apply

which is what queues the 75,871 historical orphans. The drain also re-verifies
that each tombstoned doc's ``signals`` row is really gone before deleting it, so
a bad queue row cannot destroy a live document.

The backlog is visible to the S-1 production gauge as the declared
``corpus_tombstone_drain`` loop, owned by this analyst.

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
        "analyst_corpus_retention.yaml",
        "corpus_retention",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

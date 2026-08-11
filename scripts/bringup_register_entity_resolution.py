# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Entity Resolution analyst descriptor.

Reads ``descriptors/analyst_entity_resolution.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This wires ``scripts/backfill_entity_graph.py`` as an ONGOING ``deterministic``
analyst (PIVOT_BUILD_PLAN §9 fast-follow). After this script runs, the runtime's
reconciler spins up a deterministic actor that, on its cadence, folds new
signals' NER mentions into the entity substrate (``entity_profiles`` +
``signal_entity_links`` + co-occurrence ``proposed_edges``) — so the Entities /
Entity-Graph panels and ``/api/v1/entities*`` stay current without a manual
backfill. The sweep is idempotent + forward-progressing (stamps
``signals.entities_resolved_at``); the one-shot backfill stays usable for a
bulk re-fold.

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
        "analyst_entity_resolution.yaml",
        "entity_resolution",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

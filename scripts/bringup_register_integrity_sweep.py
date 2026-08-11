# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Integrity Sweep analyst descriptor (DIRECTION §9).

Reads ``descriptors/analyst_integrity_sweep.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This ships the re-homed, events-free referential-integrity sweep as a FIRST-CLASS
``deterministic`` analyst descriptor — the successor to the 2.4-deleted
``integrity_verification`` handler. After this script runs the runtime's
reconciler spins up a deterministic actor that, on its hourly cadence, counts
referential drift across live pivot-era tables (orphan signal_entity_links,
orphan proposed_edges, evidence-less facts, broken finding supersession) and
emits a summary finding. It REFUSES LOUD — a missing relation raises rather than
emitting a zeroed clean finding — and does NO destructive repair.

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
        "analyst_integrity_sweep.yaml",
        "integrity_sweep",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

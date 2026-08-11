# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Composition Lineage Sweep analyst descriptor (P3-T6).

Reads ``descriptors/analyst_composition_lineage_sweep.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the system's lineage-integrity VERIFICATION as a FIRST-CLASS
``deterministic`` analyst descriptor. After this script runs the runtime's
reconciler spins up a deterministic actor that, on its cadence (offset AFTER the
world composition lands), walks the ``derived_from`` graph BACKWARD from each
recent composition root (world_assessor / country_composition) via
``validate_lineage`` and emits ONE summary finding: 0 cycles / 0 (true) dangling
/ 0 depth_exhausted on a healthy tower, or a NAMED sample of any root whose
sub-claim floor is broken. It REFUSES LOUD (a missing relation raises rather than
emitting a zeroed clean finding) and does NO destructive repair.

NOTE (operator): a NEW deterministic sub-handler requires a REGISTRY REBUILD
(new sub_handler in the dispatch table) AND a runtime recreate to activate — see
the memory feedback on shared-schema / new-sub-handler deploys. This script only
POSTs the descriptor; it does NOT deploy.

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
        "analyst_composition_lineage_sweep.yaml",
        "composition_lineage_sweep",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

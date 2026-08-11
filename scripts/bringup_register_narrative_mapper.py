# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Narrative Mapper analyst descriptor (P4-1 + P4-2; A11).

Reads ``descriptors/analyst_narrative_mapper.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the reified-narratives + source-echo-graph mapper as a FIRST-CLASS
``deterministic`` analyst descriptor. After this script runs (and the operator
flips the descriptor active) the runtime's reconciler spins up a deterministic
actor that, on its daily cadence, reifies every active contested-claim family
(`fact_contention` group) into a `narratives` row (carrier sources + first-seen
/ echo lags + propagation ordering), refreshes the directed source-echo graph
(`narrative_echo_edges`: leader->follower co-carriage + lag over the narrative
population), and emits the honest per-run distribution finding the
/api/v1/v3/narratives route surfaces. Migration 0102 (narratives +
narrative_echo_edges) must be applied BEFORE first activation.

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
        "analyst_narrative_mapper.yaml",
        "narrative_mapper",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

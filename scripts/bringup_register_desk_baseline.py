# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Desk Baseline analyst descriptor (P3-7 CAST-recipe baseline).

Reads ``descriptors/analyst_desk_baseline.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This ships the per-desk statistical baseline as a FIRST-CLASS ``deterministic``
analyst descriptor. After this script runs (and the operator flips the
descriptor active) the runtime's reconciler spins up a deterministic actor
that, on its DAILY cadence, computes a falsifiable quantitative prior per desk
(g20 + watch) over our own substrate — trailing baseline expectation +
uncertainty band + current-window deviation, plus the CAST feature recipe —
into the ``desk_baselines`` sidecar (migration 0103, which MUST be applied
first) and emits an honest distribution finding. NOT a forecast (no Brier / no
skill / no prediction-as-claim); the deviation feeds the P1-3
baseline_deviation trigger + gives desk LLM reads a prior, and the
``/eval/desk_baselines`` route projects the sidecar.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — resolved via scripts/_token.py (env -> .env -> dev).
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
        "analyst_desk_baseline.yaml",
        "desk_baseline",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

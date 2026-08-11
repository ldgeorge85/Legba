# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Band-Calibration Tracker analyst descriptor (P2-3 harness).

Reads ``descriptors/analyst_band_calibration_tracker.yaml`` and POSTs it to
the registry. Idempotent: if a head row is already present we report it and
exit 0 without re-posting.

This ships the scorecard calibration harness as a FIRST-CLASS
``deterministic`` analyst descriptor. After this script runs (and the operator
flips the descriptor active) the runtime's reconciler spins up a deterministic
actor that, on its daily cadence, logs a resolvable calibration claim per
scorecard band transition, auto-resolves each at T0+14d/28d against LATER
scorecard rows only, and emits the honest persistence/reversal summary finding
the ``/eval/calibration`` ``band_calibration`` section projects. Migration
0093 (band_calibration_claims + band_calibration_scan_state) must be applied
BEFORE first activation.

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
        "analyst_band_calibration_tracker.yaml",
        "band_calibration_tracker",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

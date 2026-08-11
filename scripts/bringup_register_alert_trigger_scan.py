# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Alert Trigger Scan analyst descriptor (P1-3, trigger set v1).

Reads ``descriptors/analyst_alert_trigger_scan.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the verification-gated trigger scan as a FIRST-CLASS
``deterministic`` analyst descriptor. After this script runs (and the operator
flips the descriptor active) the runtime's reconciler spins up a deterministic
actor that, on its ~10-minute cadence, fires PRODUCT alerts on VERIFIED state
transitions — scorecard band crossings, new verified high-severity findings,
contention flips, desk baseline deviations — as ``kind='alert'`` rows fanned
outward through the shared P1-1 alert-sink dispatcher. Durable watermarks
(migration 0091 — apply it BEFORE first activation) make every transition
fire-once; the first scan of each trigger class seeds silently.

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
        "analyst_alert_trigger_scan.yaml",
        "alert_trigger_scan",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

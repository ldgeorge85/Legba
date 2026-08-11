# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Geo Convergence Scan analyst descriptor (A7 detector).

Reads ``descriptors/analyst_geo_convergence_scan.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the geographic convergence detector as a FIRST-CLASS
``deterministic`` analyst descriptor. After this script runs (and the operator
flips the descriptor active) the runtime's reconciler spins up a deterministic
actor that, on its ~30-minute cadence, bins the rolling 24h of geolocated
signals (1°×1° cells for point-trustworthy coordinates, country bins for ISO2
tags) and fires ``kind='alert'`` rows on the FORMATION/DISSOLUTION edges of
≥3-distinct-source-family convergence, fanned outward through the shared P1-1
alert-sink dispatcher. Watermarks live under
``trigger_class='geo_convergence'`` in the EXISTING
``alert_trigger_watermarks`` table (migration 0091 — already applied for the
P1-3 trigger scan; NO new migration); the first scan seeds silently.

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
        "analyst_geo_convergence_scan.yaml",
        "geo_convergence_scan",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

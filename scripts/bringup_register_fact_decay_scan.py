# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Fact Decay Scan analyst descriptor (C4 readout stamper).

Reads ``descriptors/analyst_fact_decay_scan.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the C4 fact confidence-decay readout stamper as a FIRST-CLASS
``deterministic`` analyst descriptor. After this script runs (and the operator
flips the descriptor active) the runtime's reconciler spins up a deterministic
actor that, DAILY, walks every OPEN fact and stamps its derived MISP-curve
decay readout (decayed_confidence + decay_state
fresh|aging|stale|revoke_candidate; sightings derived from the
corroboration-unioned ``facts.derived_from`` signal ids) into the
``fact_decay_states`` SIDECAR — migration 0098 MUST be applied first. The
scan NEVER mutates a ``facts`` row; consumption of the sidecar ships OFF
behind ``LEGBA_FACT_DECAY_WEIGHTING`` (default OFF in docker-compose).

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
        "analyst_fact_decay_scan.yaml",
        "fact_decay_scan",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

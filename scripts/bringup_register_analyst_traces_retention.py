# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the analyst_traces retention analyst descriptor (S-6, sweep 2026-07-27).

Reads ``descriptors/analyst_analyst_traces_retention.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the ``analyst_traces_retention`` deterministic analyst — a TTL purge
for the unbounded ``analyst_traces`` telemetry table (mirrors ``signals_retention``).
It ships DISABLED (``ttl_days <= 0``): the operator opts in by forcing a run with
a generous ``ttl_days`` (MUST stay above the 7-day cadence-health window the
System Status / liveness reads span). Migration ``0101`` (the purge-scan index on
``run_started_at``) MUST be applied first. Read-only until an operator both
flips the descriptor active AND sets a positive TTL.

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
        "analyst_analyst_traces_retention.yaml",
        "analyst_traces_retention",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

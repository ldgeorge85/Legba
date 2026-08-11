# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Source Track Record analyst descriptor (A6 layer 3, P3-3).

Reads ``descriptors/analyst_source_track_record.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This ships the EARNED source-track-record analyst — the MEASURED half of the
source assurance ledger — as a FIRST-CLASS ``deterministic`` analyst
descriptor. After this script runs (and the operator flips the descriptor
active) the runtime's reconciler spins up a deterministic actor that, DAILY,
recomputes every source's wins/losses over RESOLVED ``fact_contention`` groups
(+ corroboration outcomes), smooths the win-rate (Beta-Bernoulli + Wilson lower
bound), and refreshes the ``source_track_records`` table (migration 0099 — MUST
be applied first). The arbiter's consumption of the record is OFF by default
behind ``LEGBA_CONTENTION_EARNED_WEIGHT``; this analyst only computes + stores +
exposes it (the assurance route ``earned`` section + the ``/sources``
``earned_win_rate`` projection).

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
        "analyst_source_track_record.yaml",
        "source_track_record",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

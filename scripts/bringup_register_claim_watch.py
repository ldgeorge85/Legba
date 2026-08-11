# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Claim Watch analyst descriptor (KW-3).

Reads ``descriptors/analyst_claim_watch.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This ships the flag-only new-evidence-vs-open-question matcher as a
FIRST-CLASS ``deterministic`` analyst descriptor. After this script runs (and
the operator flips the descriptor active) the runtime's reconciler spins up a
deterministic actor that, on its ~30-minute cadence, matches NEW signals
against the open-question set (``hypotheses`` status='open_question') and
side-writes append-only ``bearing_edges`` + ``review_flags`` markers — no
alerts, no LLM, never a content write. Migrations 0091 (the ridden watermark
table), 0106 (``output_consumption``) and 0107 (``review_flags`` +
``bearing_edges``) must be applied BEFORE first activation; the first scan
seeds the cursor silently and writes nothing.

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
        "analyst_claim_watch.yaml",
        "claim_watch",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

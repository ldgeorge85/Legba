# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Signal Summarizer analyst descriptor.

Reads ``descriptors/analyst_signal_summarizer.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This wires the ``signal_summarizer`` deterministic sweep as an ONGOING analyst.
After this script runs, the runtime's reconciler spins up a deterministic actor
that, on its cadence, distills the next throttled batch of un-summarized text
signals into ``signals.payload.distilled_body`` (via the CORE self-hosted LLM
plane, $0) — so downstream synthesis reads OUR analysis-tuned brief instead of
the publisher's teaser. The sweep is idempotent + forward-progressing (stamps
``signals.summarized_at`` on every examined row); migration
0081_signal_summarized_marker.sql adds that marker + its partial scan index.

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
        "analyst_signal_summarizer.yaml",
        "signal_summarizer",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

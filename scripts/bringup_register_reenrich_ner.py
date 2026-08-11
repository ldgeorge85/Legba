# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the NER Backfill (reenrich_ner) analyst descriptor.

Reads ``descriptors/analyst_reenrich_ner.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This wires the ``reenrich_ner`` deterministic sweep as an ONGOING analyst. After
this script runs, the runtime's reconciler spins up a deterministic actor that, on
its cadence, re-runs the LIVE multilingual/telegram NER (translate-then-NER +
telegram ``payload.text``) over the next batch of pre-fix signals — the ~9,143
already-persisted rows that were ingested BEFORE the NERMultilingualHandler
M11/M12 fix landed and therefore carry 0 entities. The sweep is idempotent +
forward-progressing (stamps ``signals.reenriched_at`` on every examined row; a row
that gains entities also gets ``payload.entities`` + promoted ``entity_classes`` +
its ``entities_resolved_at`` reset so ``entity_resolution`` re-folds it);
migration 0085_signal_reenriched_marker.sql adds the marker column + the partial
scan index over the un-re-enriched candidate pool.

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
        "analyst_reenrich_ner.yaml",
        "reenrich_ner",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

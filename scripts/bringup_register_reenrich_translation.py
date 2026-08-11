# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Translation Backfill (reenrich_translation) analyst descriptor.

Reads ``descriptors/analyst_reenrich_translation.yaml`` and POSTs it to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting.

This wires the ``reenrich_translation`` deterministic sweep as an ONGOING analyst
(M13/T-1c). After this script runs, the runtime's reconciler spins up a
deterministic actor that, on its cadence, translates the next batch of pre-T-1a
non-Latin signals — the ~1.9k already-persisted rows whose payload language is in
the translate set (ar/fa/he/ru/uk/zh/ja/ko/hi/th/ur) but that carry NO
``payload.title_en`` (the M11/M12 NER lane translated them transiently for NER and
DISCARDED the translation). The sweep translates the title (+ body when present)
via the hosted /translate plane and side-writes ``payload.title_en`` /
``payload.text_en`` so readers narrate English, not a transliterated surface. It is
idempotent + forward-progressing: the field IS the marker (``title_en`` NULL is the
candidate gate), so NO marker column / migration is needed.

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
        "analyst_reenrich_translation.yaml",
        "reenrich_translation",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

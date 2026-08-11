# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Autonomous Corpus Researcher analyst descriptor.

Reads ``descriptors/analyst_corpus_researcher.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting (use the PUT /descriptors/analyst/corpus_researcher route to UPDATE an
existing head — bringup POST is create-only).

This wires the ``corpus_researcher`` analyst (Stage 4 of the signal-content-depth
program — the first corpus mining analyst). After this script runs, the runtime's
reconciler activates a META (no-target) inline_target actor that, on its cadence,
AUTONOMOUSLY picks a research question and deep-investigates it over the OpenSearch
full-text corpus via the ``substrate_read`` GATHER tools (search_corpus /
read_document / list_situations / …), emitting one cited, verify-passed finding.

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
        "analyst_corpus_researcher.yaml",
        "corpus_researcher",
    ),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

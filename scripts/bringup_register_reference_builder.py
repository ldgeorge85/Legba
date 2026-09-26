#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Reference Builder analyst descriptor (Program 2 track R2).

Create-only and idempotent, exactly like ``bringup_register_correctness_grader.py``:
the descriptor lands in ``draft``; the operator (or the deploy runbook) transitions
draft -> configured -> active via ``POST .../descriptors/analyst/reference_builder/transition``.
The handler runs only when ``LEGBA_REFERENCE_BUILDER_ENABLED=1`` (default 0) and spends
nothing but the core plane. Registry base/token as the sibling registrars resolve them.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _bringup_http import register_create_only, registry_base  # noqa: E402
from _token import resolve_token  # noqa: E402

BASE = registry_base()
TOKEN = resolve_token()
TO_REGISTER = [
    ("analyst", "analyst_reference_builder.yaml", "reference_builder"),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Country Assessment analyst descriptor (Program 3 Lane A).

Create-only and idempotent, exactly like ``bringup_register_reference_builder.py``:
the descriptor lands in ``draft`` and creates NO live actor, so registering it
schedules nothing and spends nothing. The transition draft -> configured ->
active is the orchestrator's (or the operator's) call via
``POST .../descriptors/analyst/country_assessment/transition``, and it is a real
decision rather than a formality: going active fans this channel out to ~32
per-country runs per cycle, each of them ALWAYS judged, which is the 33x judge
spend D-6 named when it deferred the country tier.

Registry base/token as the sibling registrars resolve them (``_bringup_http``
/ ``_token``); the token is never printed.
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
    ("analyst", "analyst_country_assessment.yaml", "country_assessment"),
]


def main() -> int:
    return register_create_only(TO_REGISTER, base=BASE, token=TOKEN)


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Declare the core plane's thinking alias on ``llm.primary.openai_compat``.

The gateway serves the core model as ``fast`` (thinking off) and
``deep`` (thinking on). Legba resolves a thinking call to the component's
``thinking_model_name`` (``src/legba/data/stack/llm/thinking.py``); this script
writes a NEW HEAD of the live component with that one field set and every
other field carried over unchanged. The reasoning-budget pair stays unset
(see docs/SEAMS.md): nothing new is sent on a thinking-off call, and a thinking call
sends only the alias.

ORDER: the registry, runtime and workflow worker must already run this
code. The schema change is ``extra="forbid"``; a head carrying the new field
does not parse on an older image (a dead component, not a config change).
The body is validated through ``LLMProvider`` before any PUT. A
stack-component PUT evicts cached handlers through the stack informer; the
next call builds a handler from the new head.

Dry-run by default; ``--apply`` performs the PUT. Never touches secrets.

    python3 scripts/core_thinking_alias.py            # plan
    python3 scripts/core_thinking_alias.py --apply    # write the head
"""
from __future__ import annotations

import argparse
import copy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from _bringup_http import registry_base, registry_client  # noqa: E402
from _token import resolve_token  # noqa: E402

from legba.data.schemas.stack import LLMProvider  # noqa: E402

CORE_ID = "llm.primary.openai_compat"
#: The gateway's thinking-on alias of the core model (thinking on, same model).
#: Registry data from here on, never code.
THINKING_ALIAS = "deep"


def planned_body(live: dict, alias: str = THINKING_ALIAS) -> dict:
    """The live head's body with ``thinking_model_name`` set, as a new head."""
    body = copy.deepcopy(live)
    body["version"] = "0" * 16
    body["config"]["thinking_model_name"] = {"factory_kind": "text", "raw": alias}
    LLMProvider.model_validate(body, strict=False)
    return body


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="PUT the new head (default: dry-run)")
    args = ap.parse_args()
    with registry_client(registry_base(), resolve_token("operator"), timeout=60) as c:
        r = c.get(f"/stack/{CORE_ID}")
        r.raise_for_status()
        live = r.json()["body"]
        cfg = live.get("config") or {}
        current = (cfg.get("thinking_model_name") or {}).get("raw")
        print(f"{CORE_ID}: model_name={cfg.get('model_name', {}).get('raw')!r} "
              f"thinking_model_name={current!r}")
        if current == THINKING_ALIAS:
            print("already declared; nothing to do")
            return 0
        body = planned_body(live)
        if not args.apply:
            print(f"would PUT /stack/{CORE_ID} with thinking_model_name={THINKING_ALIAS!r} "
                  "(validated through LLMProvider)")
            return 0
        r = c.put(f"/stack/{CORE_ID}", json=body)
        r.raise_for_status()
        print(f"PUT /stack/{CORE_ID} -> version {r.json()['version'][:12]} state={r.json()['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

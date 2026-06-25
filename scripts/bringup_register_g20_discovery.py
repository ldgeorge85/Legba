# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register + activate the G20 country discovery descriptor.

Two-step bringup:

  1. Re-register ``template_country`` as state=configured so the discovery
     materialiser has a template body to merge candidates against.  It's
     currently retired (operator action on a prior cycle); template
     descriptors don't run ingest, so configured is the correct active
     state.
  2. Register + activate ``discovery_geopolitical_g20`` (state=active).
     The country_list_discovery handler fires on the next reconcile
     tick; 19 L1 country target instances materialise as
     ``country_geopolitical_<iso2_lower>`` rows in target_descriptors
     with state=configured (per the L-200 materialiser default).

After this script runs, a separate activation pass flips the
materialised L1 instances to state=active so they start ingesting.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402


BASE = "http://127.0.0.1:8090/api/v1/registry"
TOKEN = resolve_token()
DESCRIPTORS_DIR = Path(__file__).resolve().parent.parent / "descriptors"


def _load_body(filename: str) -> dict:
    body = yaml.safe_load((DESCRIPTORS_DIR / filename).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _put(client: httpx.Client, family: str, descriptor_id: str, body: dict) -> str:
    r = client.put(f"/descriptors/{family}/{descriptor_id}", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"PUT {family}/{descriptor_id}: {r.status_code} {r.text[:400]}")
    return r.json().get("version", "?")


def _post(client: httpx.Client, family: str, body: dict) -> str:
    r = client.post(f"/descriptors/{family}", json=body)
    if r.status_code not in (200, 201):
        raise RuntimeError(f"POST {family}: {r.status_code} {r.text[:400]}")
    return r.json().get("version", "?")


def _exists(client: httpx.Client, family: str, descriptor_id: str) -> bool:
    r = client.get(f"/descriptors/{family}/{descriptor_id}")
    return r.status_code == 200


def main() -> int:
    with httpx.Client(
        base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"}, timeout=30
    ) as client:
        # Step 1: register template_country_active.  The original
        # template_country is retired (operator action on a prior cycle)
        # and the lifecycle FSM is terminal at retired (no retired →
        # configured transition).  Clone with a new id; same body.
        template = _load_body("template_country_active.yaml")
        template.setdefault("identity", {})["state"] = "configured"
        action = "updated" if _exists(client, "target", "template_country_active") else "registered"
        ver = (
            _put(client, "target", "template_country_active", template)
            if action == "updated"
            else _post(client, "target", template)
        )
        print(f"  {action:>10}  target/template_country_active  @ {ver[:16]}  (state=configured)")

        # Step 2: register + activate the G20 discovery descriptor.
        g20 = _load_body("discovery_geopolitical_g20.yaml")
        g20.setdefault("identity", {})["state"] = "active"
        action = "updated" if _exists(client, "target", "discovery_geopolitical_g20") else "registered"
        ver = (
            _put(client, "target", "discovery_geopolitical_g20", g20)
            if action == "updated"
            else _post(client, "target", g20)
        )
        print(f"  {action:>10}  target/discovery_geopolitical_g20  @ {ver[:16]}  (state=active)")

        print()
        print("Discovery descriptor activated.  The country_list_discovery")
        print("handler fires on the next reconcile tick (~5 min default,")
        print("overridable via LEGBA_RUNTIME_RESYNC_INTERVAL).  Materialised")
        print("L1 instances land in target_descriptors with state=configured.")
        print()
        print("Verify materialisation:")
        print("  docker exec legba-postgres-1 psql -U legba -d legba -c \\")
        print("    \"SELECT descriptor_id, body->'identity'->>'state' FROM target_descriptors WHERE descriptor_id LIKE 'country_geopolitical_%';\"")
        print()
        print("Then flip them active via:")
        print("  scripts/bringup_activate_g20_country_targets.py")
        return 0


if __name__ == "__main__":
    sys.exit(main())

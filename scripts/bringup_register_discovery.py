# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the L-200 discovery descriptor (and its template).

Optional Phase 9 step. If geopolitical vocabulary terms are not yet
seeded, the registry will route the descriptor to the dead-letter and
report the missing terms — fix forward by registering them via
/vocabulary/<family> first.
"""
from __future__ import annotations

import os
import sys
import httpx

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402
import yaml
from datetime import datetime, timezone


BASE = os.environ.get(
    "LEGBA_REGISTRY_URL",
    "http://127.0.0.1:8090/api/v1/registry",
)
TOKEN = resolve_token()
H = {"Authorization": f"Bearer {TOKEN}"}


def main() -> int:
    paths = [
        ("/usr/local/deployments/active/legba/descriptors/template_country.yaml",
         "template_country"),
        ("/usr/local/deployments/active/legba/descriptors/"
         "discovery_geopolitical_countries.yaml",
         "discovery_geopolitical_countries"),
    ]
    for path, desc_id in paths:
        with open(path) as f:
            body = yaml.safe_load(f)
        iden = body.setdefault("identity", {})
        iden["version"] = "0" * 16
        iden.setdefault("created", datetime.now(tz=timezone.utc).isoformat())

        r = httpx.get(f"{BASE}/descriptors/target/{desc_id}",
                      headers=H, timeout=10)
        if r.status_code == 200:
            print(f"{desc_id}: already registered, skipping")
            continue

        r = httpx.post(f"{BASE}/descriptors/target",
                       headers=H, json=body, timeout=20)
        print(f"{desc_id}: HTTP {r.status_code}")
        if r.status_code not in (200, 201):
            print(f"  body: {r.text[:800]}")
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

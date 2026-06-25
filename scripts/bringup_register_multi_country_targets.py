# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the five Phase 5a multi-target validation country descriptors.

Reads each ``descriptors/target_<country>_news.yaml`` and POSTs it to the
registry (or PUTs an update if a head row already exists). Each successful
register/update returns the registry-minted content-hash version which we
print so the operator has a record per country.

Mirrors ``scripts/bringup_register_brazil_predictor.py`` in style + error
handling — same Bearer token, same DescriptorsRegistry endpoint.

The five descriptors here form the multi-target scale-out for the
india_energy_infra baseline: japan_news, germany_news, nigeria_news,
mexico_news, turkey_news. Each carries a single RSS source + the 7-filter
post-F-bundle pipeline (language_detect, ner_multilingual, classify,
source_credibility, geocode, dedupe_tier_1, dedupe_tier_2) + inline
analyst pointed at gpt-oss-120b.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — defaults to "dev"
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402
import yaml


BASE = os.environ.get(
    "LEGBA_REGISTRY_URL",
    "http://127.0.0.1:8090/api/v1/registry",
)
TOKEN = resolve_token()

DESCRIPTORS_DIR = Path(__file__).resolve().parent.parent / "descriptors"

# (family, file, descriptor_id)
TO_REGISTER = [
    ("target", "target_japan_news.yaml",   "japan_news"),
    ("target", "target_germany_news.yaml", "germany_news"),
    ("target", "target_nigeria_news.yaml", "nigeria_news"),
    ("target", "target_mexico_news.yaml",  "mexico_news"),
    ("target", "target_turkey_news.yaml",  "turkey_news"),
]


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {TOKEN}"},
        timeout=30,
    )


def _load_yaml(name: str) -> dict:
    with open(DESCRIPTORS_DIR / name) as f:
        body = yaml.safe_load(f)
    # YAML carries a placeholder 16-hex version; the registry stamps the
    # real content hash. The schema's pydantic pattern still requires the
    # field to match [a-f0-9]{16,64} on the way in, so we leave the
    # placeholder alone — it was written into the YAML in that shape.
    return body


def _get_head(client: httpx.Client, family: str, descriptor_id: str) -> dict | None:
    r = client.get(f"/descriptors/{family}/{descriptor_id}")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    raise RuntimeError(
        f"GET head failed for {family}/{descriptor_id}: "
        f"{r.status_code} {r.text[:400]}"
    )


def main() -> int:
    with _client() as client:
        results: list[tuple[str, str, str, str]] = []  # (action, family, id, version)
        failures: list[str] = []

        for family, fname, desc_id in TO_REGISTER:
            try:
                head = _get_head(client, family, desc_id)
            except Exception as exc:
                failures.append(f"{family}/{desc_id}: pre-check {exc}")
                continue

            body = _load_yaml(fname)

            if head is None:
                r = client.post(f"/descriptors/{family}", json=body)
                action = "registered"
            else:
                r = client.put(f"/descriptors/{family}/{desc_id}", json=body)
                action = "updated"

            if r.status_code not in (200, 201):
                failures.append(
                    f"{family}/{desc_id}: HTTP {r.status_code} "
                    f"{r.text[:800]}"
                )
                continue
            out = r.json()
            results.append((action, family, desc_id, out.get("version", "?")))

        print("Results:")
        for action, fam, desc, ver in results:
            print(f"  {action:>10}  {fam}/{desc}  @ {ver}")
        if failures:
            print("Failures:")
            for s in failures:
                print(f"  ! {s}")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())

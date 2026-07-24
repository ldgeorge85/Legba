# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the VOICES faculty-lens analyst descriptors (LV-1).

Reads the five ``descriptors/analyst_lens_*.yaml`` files and POSTs them to the
registry. Idempotent: if a head row is already present we report it and exit 0
without re-posting (use the PUT /descriptors/analyst/<id> route to UPDATE an
existing head — bringup POST is create-only).

This wires the VOICES faculty-lens tier (plan planning/VOICES_PLAN_2026-07-21.md,
build spec planning/VOICES_BUILD_DESIGN.md): four function-typed faculty ids on
the journal_assessor kind (lens_trend / lens_baserate / lens_capability /
lens_intent) — each reads the VERIFIED tower top through ONE declared falsifiable
prior (living in its persona module; the descriptor content hash IS the prior
version, DL-2) and writes an interpretive read into ``journal_entries``
(entry_kind='lens'), the V1 faithfulness verify grading it post-persist — plus a
fifth id (lens_diff) that reads the four reads and narrates agree/split/outlier
(entry_kind='lens_diff'). All five run on staggered weekly Monday crons
(06:30-08:30 UTC). NO publish edge exists — reads accumulate internally.

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — resolved via scripts/_token.py (env → .env → dev).
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402
import yaml  # noqa: E402


BASE = os.environ.get(
    "LEGBA_REGISTRY_URL",
    "http://127.0.0.1:8090/api/v1/registry",
)
TOKEN = resolve_token()

DESCRIPTORS_DIR = Path(__file__).resolve().parent.parent / "descriptors"

# (family, file, descriptor_id) — the four faculties then the diff pass. Order
# is cosmetic (each POST is independent); the CADENCE (not registration order) is
# what staggers the runs.
TO_REGISTER = [
    ("analyst", "analyst_lens_trend.yaml", "lens_trend"),
    ("analyst", "analyst_lens_baserate.yaml", "lens_baserate"),
    ("analyst", "analyst_lens_capability.yaml", "lens_capability"),
    ("analyst", "analyst_lens_intent.yaml", "lens_intent"),
    ("analyst", "analyst_lens_diff.yaml", "lens_diff"),
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
    # YAML carries a placeholder for the version; registry stamps the real
    # content hash, but pydantic-strict still wants a hex string in the
    # [a-f0-9]{16,64} shape until then.
    identity = body.setdefault("identity", {})
    identity["version"] = "0" * 16
    return body


def _exists_head(client: httpx.Client, family: str, descriptor_id: str) -> bool:
    r = client.get(f"/descriptors/{family}/{descriptor_id}")
    if r.status_code == 200:
        return True
    if r.status_code == 404:
        return False
    raise RuntimeError(
        f"GET head failed for {family}/{descriptor_id}: "
        f"{r.status_code} {r.text[:200]}"
    )


def main() -> int:
    with _client() as client:
        registered: list[tuple[str, str, str]] = []
        skipped: list[tuple[str, str]] = []
        failures: list[str] = []

        for family, fname, desc_id in TO_REGISTER:
            try:
                if _exists_head(client, family, desc_id):
                    skipped.append((family, desc_id))
                    continue
            except Exception as exc:
                failures.append(f"{family}/{desc_id}: pre-check {exc}")
                continue

            body = _load_yaml(fname)
            r = client.post(f"/descriptors/{family}", json=body)
            if r.status_code not in (200, 201):
                failures.append(
                    f"{family}/{desc_id}: HTTP {r.status_code} {r.text[:500]}"
                )
                continue
            out = r.json()
            registered.append((family, desc_id, out.get("version", "?")[:12]))

        print("Registered:")
        for f, d, v in registered:
            print(f"  + {f}/{d} @ {v}")
        print("Skipped (head already present):")
        for f, d in skipped:
            print(f"  = {f}/{d}")
        if failures:
            print("FAILURES:")
            for line in failures:
                print(f"  ! {line}")
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

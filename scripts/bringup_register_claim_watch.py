# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the Claim Watch analyst descriptor (KW-3).

Reads ``descriptors/analyst_claim_watch.yaml`` and POSTs it to the registry.
Idempotent: if a head row is already present we report it and exit 0 without
re-posting.

This ships the flag-only new-evidence-vs-open-question matcher as a
FIRST-CLASS ``deterministic`` analyst descriptor. After this script runs (and
the operator flips the descriptor active) the runtime's reconciler spins up a
deterministic actor that, on its ~30-minute cadence, matches NEW signals
against the open-question set (``hypotheses`` status='open_question') and
side-writes append-only ``bearing_edges`` + ``review_flags`` markers — no
alerts, no LLM, never a content write. Migrations 0091 (the ridden watermark
table), 0106 (``output_consumption``) and 0107 (``review_flags`` +
``bearing_edges``) must be applied BEFORE first activation; the first scan
seeds the cursor silently and writes nothing.

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

# (family, file, descriptor_id)
TO_REGISTER = [
    (
        "analyst",
        "analyst_claim_watch.yaml",
        "claim_watch",
    ),
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
            print("Failures:")
            for s in failures:
                print(f"  ! {s}")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())

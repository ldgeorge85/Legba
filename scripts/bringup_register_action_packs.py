# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the P-11 seed action packs (media_processing / incident_response /
discovery).

Reads the ``descriptors/action_pack_*.yaml`` seed packs and POSTs them to the
registry's ``action_pack`` family. Idempotent: a pack whose head row already
exists is reported + skipped (exit 0).

After this runs, an analyst that GRANTS one of these packs (``action_packs:``)
AND whose target ALLOWS it (``allowed_action_packs:``), where the pack is
APPLICABLE (scope-tag / predicate), can invoke its tools through
``legba.data.analysts.agency.Agency.run_pack_tool`` — gated by the pack's
governor over the ``action_pack_invocations`` ledger (migration 0025).

Env:
  * ``LEGBA_REGISTRY_URL``   — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_TOKEN`` — defaults via scripts/_token.resolve_token().
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
    ("action_pack", "action_pack_media_processing.yaml", "media_processing"),
    ("action_pack", "action_pack_incident_response.yaml", "incident_response"),
    ("action_pack", "action_pack_substrate_read.yaml", "substrate_read"),
    ("action_pack", "action_pack_escalate.yaml", "escalate_finding"),
    # S6 — external evidence + operator-gated write-back packs.
    ("action_pack", "action_pack_web_access.yaml", "web_access"),
    ("action_pack", "action_pack_propose_facts.yaml", "propose_facts"),
    # Journal Assessor (plan §5 / §12 Wave 0) — the journal's GOVERNED read pack
    # (ONE reused tool, list_findings). Granted ONLY to journal_assessor (§7.6).
    ("action_pack", "action_pack_journal_read.yaml", "journal_read"),
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
    # Registry stamps the real content hash; pydantic-strict still wants a hex
    # string in the [a-f0-9]{16,64} shape until then.
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

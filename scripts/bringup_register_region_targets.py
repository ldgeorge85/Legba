#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S2-T1 — register the FIVE region-frame targets (composition topology).

A region frame is a non-country, non-geo SCOPE FRAME: a thematic target
(``scope.domain == 'thematic'``, ``geo: []``) that exists so the later
``region_composition`` analyst (S2-T2, a separate wave) has a target to hang
off and a ``region`` tag to filter country compositions by. It mirrors the
first thematic target (``descriptors/target_situation_iran_war.yaml``) in shape
— a broad news source_selector to satisfy the "active target needs >=1 source"
constraint — but carries NO inline analyst: these are frames only, nothing
subscribes to them yet.

Five frames, one per macro-region (per the portfolio review's regional tier):

    region_europe   region_mena   region_indo_pacific   region_americas   region_africa

Each is tagged ``region`` (the generic frame tag S2-T2's read filter keys on)
plus its own ``region_<slug>`` coverage tag — the SAME tag the 24 country desks
carry from S1-T1, so the region_composition analyst reads the country reads in
its region by that tag.

Registration goes through the registry REST surface (POST /descriptors/target),
mirroring ``bringup_register_multi_country_targets.py`` + ``_token.py`` auth.
CREATE-only + idempotent: a frame whose head row already exists is left alone
(reported ``exists``), never PUT — bringup does not mutate live descriptors.

Every synthesised body is validated against the real pydantic
``TargetDescriptor`` schema before it touches the registry.

Env:
  * ``LEGBA_REGISTRY_URL``       — defaults to http://127.0.0.1:8090/api/v1/registry
  * ``LEGBA_REGISTRY_API_TOKEN`` — resolved via scripts/_token.py (.env fallback)

Usage:
  python3 scripts/bringup_register_region_targets.py            # register live
  python3 scripts/bringup_register_region_targets.py --dry-run  # validate + print only
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys

import httpx

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _token import resolve_token  # noqa: E402

from legba.data.schemas.target import TargetDescriptor  # noqa: E402

BASE = os.environ.get(
    "LEGBA_REGISTRY_URL",
    "http://127.0.0.1:8090/api/v1/registry",
)

FAMILY = "target"

# The five macro-region frames. ``slug`` is both the id suffix (region_<slug>)
# and the per-region coverage tag (region_<slug>) the country desks carry from
# S1-T1. ``themes`` is a human/semantic label set (snake_case scope tags).
REGIONS: list[dict] = [
    {
        "slug": "europe",
        "name": "Europe",
        "themes": ["europe", "eu", "nato"],
    },
    {
        "slug": "mena",
        "name": "Middle East & North Africa",
        "themes": ["middle_east", "north_africa", "gulf", "mena"],
    },
    {
        "slug": "indo_pacific",
        "name": "Indo-Pacific",
        "themes": ["indo_pacific", "east_asia", "south_asia", "southeast_asia", "oceania"],
    },
    {
        "slug": "americas",
        "name": "Americas",
        "themes": ["americas", "north_america", "latin_america"],
    },
    {
        "slug": "africa",
        "name": "Africa",
        "themes": ["africa", "sub_saharan_africa"],
    },
]

# Same geopolitical vocabulary the G20 / watch / situation targets reason over
# (every value is present in the migrated vocabulary_entries seed; the registry
# rejects unknown values).
ENTITY_CLASSES = [
    "country", "organization", "corporation", "person", "location",
    "political_party", "international_org", "infrastructure", "media_outlet",
    "military_unit", "armed_group", "event_series", "commodity", "concept",
]
RELATIONSHIP_TYPES = [
    "AlliedWith", "HostileTo", "MemberOf", "LeaderOf", "TradesWith",
    "BordersWith", "SanctionsAgainst", "DiplomaticRelationsWith",
    "MilitaryPresenceIn", "SuppliesWeaponsTo",
]


def build_body(region: dict) -> dict:
    """Synthesise the plain-dict TargetDescriptor body for one region frame."""
    slug = region["slug"]
    return {
        "identity": {
            "id": f"region_{slug}",
            "name": f"Region — {region['name']}",
            "schema_uri": "legba/target/2.0.0",
            "version": "0" * 16,  # placeholder; registry stamps the content hash
            "abstraction_level": "L1",
            "inherits": [],
            "state": "active",
            "owner": "region_tier",
            "created": "2026-07-02T00:00:00Z",
        },
        # A non-geo FRAME (mirrors the iran_war thematic target). geo is empty;
        # there is no signal predicate because no analyst reads a slice off this
        # frame yet — S2-T2's region_composition reads country compositions by
        # the region tag, not raw signals.
        "scope": {
            "domain": "thematic",
            "themes": region["themes"],
            "geo": [],
            "entity_classes": ENTITY_CLASSES,
            "relationship_types": RELATIONSHIP_TYPES,
            "time_horizon_days": 90,
            "predicate": None,
            # `region` = the generic frame tag S2-T2's read filter keys on;
            # `region_<slug>` = the per-region coverage tag the 24 desks carry
            # (S1-T1) so region_composition fuses its region's country reads.
            "tags": ["region", f"region_{slug}", "geopolitical"],
        },
        # Broad news selector — purely to satisfy the "active target must
        # declare at least one source" schema constraint (same permissive
        # selector the thematic situation target uses). Nothing consumes it
        # until an analyst subscribes.
        "sources": [
            {
                "source_selector": {
                    "tags": ["news"],
                    "kinds": ["rss"],
                    "owner_tenant": "shared",
                },
                "subscription": {"canonical_only": True},
            }
        ],
        # NO inline analyst: frames only (the region_composition analyst is
        # S2-T2, a later wave). NO outputs, NO action packs.
        "allowed_action_packs": [],
        "pipeline": {"ingestion_filters": [], "enrichment": [], "routing": []},
    }


def build_region_frames() -> list[tuple[str, dict, TargetDescriptor]]:
    """Build + validate every region frame. Returns (id, body, descriptor)."""
    out: list[tuple[str, dict, TargetDescriptor]] = []
    for region in REGIONS:
        body = build_body(region)
        # Validate against the real schema before anything touches the network.
        desc = TargetDescriptor.model_validate(body, strict=False)
        out.append((desc.identity.id, body, desc))
    return out


def _client() -> httpx.Client:
    return httpx.Client(
        base_url=BASE,
        headers={"Authorization": f"Bearer {resolve_token()}"},
        timeout=30,
    )


def _get_head(client: httpx.Client, descriptor_id: str) -> dict | None:
    r = client.get(f"/descriptors/{FAMILY}/{descriptor_id}")
    if r.status_code == 200:
        return r.json()
    if r.status_code == 404:
        return None
    raise RuntimeError(
        f"GET head failed for {FAMILY}/{descriptor_id}: "
        f"{r.status_code} {r.text[:400]}"
    )


def register_frames(client: httpx.Client) -> tuple[list[tuple[str, str, str]], list[str]]:
    """CREATE each region frame that does not already exist (idempotent).

    Returns ``(results, failures)`` where results is a list of
    ``(action, descriptor_id, version)`` and action is ``registered`` or
    ``exists``.
    """
    results: list[tuple[str, str, str]] = []
    failures: list[str] = []

    for desc_id, body, _desc in build_region_frames():
        try:
            head = _get_head(client, desc_id)
        except Exception as exc:  # noqa: BLE001 — surface a per-frame failure
            failures.append(f"{FAMILY}/{desc_id}: pre-check {exc}")
            continue

        # CREATE-only: an existing head is left untouched (never PUT).
        if head is not None:
            results.append(("exists", desc_id, head.get("version", "?")))
            continue

        r = client.post(f"/descriptors/{FAMILY}", json=body)
        if r.status_code not in (200, 201):
            failures.append(f"{FAMILY}/{desc_id}: HTTP {r.status_code} {r.text[:800]}")
            continue
        results.append(("registered", desc_id, r.json().get("version", "?")))

    return results, failures


def _print_results(results: list[tuple[str, str, str]], failures: list[str]) -> None:
    print("Region frames:")
    for action, desc_id, ver in results:
        print(f"  {action:>10}  {FAMILY}/{desc_id}  @ {ver}")
    if failures:
        print("Failures:")
        for s in failures:
            print(f"  ! {s}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate + print the 5 frames without touching the registry.",
    )
    args = parser.parse_args(argv)

    if args.dry_run:
        # No network: prove every body validates, then report what WOULD register.
        frames = build_region_frames()
        print("Region frames (dry-run — nothing written):")
        for desc_id, _body, desc in frames:
            print(f"  would-register  {FAMILY}/{desc_id}  tags={desc.scope.tags}")
        return 0

    with _client() as client:
        results, failures = register_frames(client)
    _print_results(results, failures)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())

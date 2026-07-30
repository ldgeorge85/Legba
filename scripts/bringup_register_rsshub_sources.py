# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A7 — register the RSSHub-lane draft SourceDescriptors (starved-desk feeds).

The "RSSHub lane" bridges credible regional / gov / major-media outlets that
publish no native RSS into the existing ``rss`` source kind, via the self-hosted
``rsshub`` sidecar (docker-compose profile ``sources-extra``). This registrar
lands the ten curated ``descriptors/source_rsshub_*.yaml`` feeds that target the
five WORST-covered non-G7 desks found by a live 7-day signals-per-desk query at
authoring time:

    NE (Niger, 2)   TW (Taiwan, 2)   HT (Haiti, 3)   CD (DR Congo, 4)   KP (North Korea, 4)

Two feeds per desk — a top-tier English wire (AP News country hubs) plus a
strong regional/local voice (RFI Afrique/Amériques, Focus Taiwan/CNA, Al Jazeera
English country page, Radio Free Asia). All are ``source_class: reporting``; NONE
is Chinese state media (the house rule permits state media only for the CN desk,
where it is knowingly ingested as labeled ``state_media``).

NOTE (2026-07-28): ``source_rsshub_rfi_afrique.yaml`` no longer actually
depends on the sidecar — RSSHub's own upstream scrape of ``/rfi/fr/afrique``
started 503-ing, so that one descriptor was re-pointed at RFI's verified
native RSS feed (``https://www.rfi.fr/fr/afrique/rss``) instead. It is still
registered by this same script (nothing about registration itself changed —
just its ``config.url``), so it stays in ``SOURCE_FILES`` below; see the
descriptor's own header comment for the full story. The other nine feeds,
including the sibling ``source_rsshub_rfi_ameriques.yaml``, are unaffected.

Ships INERT / activation is the operator's:
  * Every descriptor ships ``identity.state: draft``, so bulk registration
    creates NO live actor (``runtime/dapr_host.py`` skips draft/configured
    descriptors). The operator activates each (draft -> configured -> active)
    AFTER bringing up the ``rsshub`` sidecar and verifying the route live.
  * The RSS handler's SSRF egress guard blocks internal hosts, so activation
    also requires ``rsshub`` on the runtime's ``LEGBA_EGRESS_ALLOW_HOSTS``
    (defaulted on legba-runtime-dapr in docker-compose.yml).

Idempotent (mirrors scripts/bringup_register_sources.py): re-runs report
``unchanged`` for already-current heads. Also seeds host-level
``source_credibility`` rows for the NEW upstream hosts (the article links in the
RSSHub feeds are the real outlet URLs, so the credibility filter keys on those
hosts, not on ``rsshub``) — INSERT .. ON CONFLICT DO NOTHING, so operator
overrides and any pre-existing seed (apnews.com / aljazeera.com) always win.

DB selection: direct-DB via DescriptorRegistry (default ``legba_pivot_test`` on
the dev rig — override with ``LEGBA_DATA_PG_DB=legba`` for production, exactly as
the other bring-up scripts).
"""
from __future__ import annotations

import asyncio
import pathlib
import sys

import yaml

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from _p17_registrar import (  # noqa: E402
    Family,
    RegisterResult,
    close_registry,
    open_registry,
    print_results,
    register_descriptor,
)

from legba.data.postgres import PostgresStore  # noqa: E402
from legba.data.schemas.source import SourceDescriptor  # noqa: E402

DESCRIPTORS_DIR = pathlib.Path(__file__).resolve().parent.parent / "descriptors"

# The ten RSSHub-lane draft descriptors, grouped by desk (see module docstring).
SOURCE_FILES = [
    # NE — Niger
    "source_rsshub_apnews_niger.yaml",
    "source_rsshub_rfi_afrique.yaml",
    # TW — Taiwan
    "source_rsshub_apnews_taiwan.yaml",
    "source_rsshub_focustaiwan.yaml",
    # HT — Haiti
    "source_rsshub_apnews_haiti.yaml",
    "source_rsshub_rfi_ameriques.yaml",
    # CD — DR Congo
    "source_rsshub_apnews_drcongo.yaml",
    "source_rsshub_aljazeera_drcongo.yaml",
    # KP — North Korea
    "source_rsshub_apnews_north_korea.yaml",
    "source_rsshub_rfa_korea.yaml",
]

SCORED_BY = "a7.rsshub_lane"

# Host-level credibility seeds for the UPSTREAM outlets the RSSHub routes render
# (host, score, rationale, tier, state_affiliation). apnews.com (0.90) and
# aljazeera.com (0.70) are already seeded by the S-1 catalog; they are included
# here so a fresh instance that skipped the catalog still gets them, and
# ON CONFLICT DO NOTHING guarantees an existing/overridden row is never clobbered.
# state_affiliation is HONEST provenance (the outlet is state-funded) and is
# ORTHOGONAL to source_class == reporting (editorial independence) — exactly the
# aljazeera.com precedent (state_affiliation True, reporting).
CREDIBILITY_SEEDS: list[tuple[str, float, str, str, bool]] = [
    (
        "apnews.com", 0.90,
        "Associated Press — top-tier international newswire cooperative.",
        "wire", False,
    ),
    (
        "aljazeera.com", 0.70,
        "Al Jazeera English — international broadcaster (Qatar state-funded, "
        "editorially independent).",
        "wire", True,
    ),
    (
        "rfi.fr", 0.78,
        "Radio France Internationale (France Médias Monde) — French public "
        "broadcaster, state-funded but editorially independent; deep Sahel / "
        "francophone coverage.",
        "wire", True,
    ),
    (
        "focustaiwan.tw", 0.75,
        "Focus Taiwan — English service of CNA (Taiwan's central news agency); "
        "professional national wire, not a state-propaganda conduit.",
        "wire", True,
    ),
    (
        "rfa.org", 0.72,
        "Radio Free Asia (USAGM) — US-government-funded but under a statutory "
        "editorial firewall (VOA-class); among the best-sourced English outlets "
        "on North Korea.",
        "wire", True,
    ),
]


def _load(name: str) -> SourceDescriptor:
    """Mirror scripts/bringup_register_sources.py::_load — yaml + placeholder
    version + strict=False validation against the real SourceDescriptor schema."""
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return SourceDescriptor.model_validate(body, strict=False)


async def seed_credibility(pg: PostgresStore) -> tuple[int, int]:
    """Seed host-level source_credibility rows (0014/0031 convention:
    INSERT .. ON CONFLICT (source_host) DO NOTHING). Returns (inserted,
    already_present). Requires migration 0031 (tier / state_affiliation)."""
    inserted = 0
    skipped = 0
    async with pg.acquire() as conn:
        for host, score, rationale, tier, state_aff in CREDIBILITY_SEEDS:
            status = await conn.execute(
                """
                INSERT INTO source_credibility
                    (source_host, score, score_rationale, scored_by, tier, state_affiliation)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (source_host) DO NOTHING
                """,
                host, score, rationale, SCORED_BY, tier, state_aff,
            )
            if status.endswith("1"):
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


async def main() -> int:
    pg, reg = await open_registry()
    try:
        results: list[RegisterResult] = []
        for fname in SOURCE_FILES:
            desc = _load(fname)
            results.append(
                await register_descriptor(pg, reg, family=Family.SOURCE, descriptor=desc)
            )
        failures = print_results(
            f"RSSHub-lane draft sources ({len(results)} descriptors):", results
        )
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

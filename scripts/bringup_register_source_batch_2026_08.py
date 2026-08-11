# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the 2026-08-03 source batch — AP world re-route + Niger coverage.

Six BRAND-NEW draft SourceDescriptors, all keyless RSS, landing two things the
2026-08-03 remediation roadmap asked for (row B-7):

  1. THE AP RE-ROUTE. The five AP country-hub feeds froze upstream on
     2026-07-28 and are PAUSED; the freeze is in the apnews.com/hub/* pages
     themselves, so they cannot be re-pointed. AP's world NAVIGATION section
     is a different, live surface — ``source.rsshub.apnews.world`` rides the
     same RSSHub sidecar on ``/apnews/nav/world-news``, re-verified live on
     2026-08-03 by reading ``datePublished`` off six of the linked article
     pages (same-day and prior-day AP copy). Geo filtering happens OUR side:
     the descriptor declares ``geo: []`` and each desk narrows per signal on
     its own geo predicate over the in-body ``geocode`` enrichment.

  2. THE NIGER COVERAGE PASS. A read-only 7-day count on the live signals
     table (``'NE' = ANY(geo)``) returned 29 rows across four sources, 14 of
     which came from the now-paused frozen AP Niger hub — so real live inflow
     was ~15 signals/week, and NOT ONE domestic Nigerien outlet was registered
     at all. Five feeds answer that, verified by direct fetch on 2026-08-03
     (HTTP status, item count, newest item date and Niger place-name density
     recorded in each descriptor's own header):

       * ``source.actuniger.politique``  — Niamey independent daily, politics
       * ``source.actuniger.societe``    — same outlet, the security stream
       * ``source.studiokalangou.news``  — Fondation Hirondelle station,
                                           up-country Niger, 5 languages
       * ``source.sahelintelligence.news`` — regional Sahel security specialist
       * ``source.france24.afrique``     — francophone regional wire (the
                                           FRENCH Africa section, distinct
                                           from the existing English global
                                           ``source.france24.english``)

NOT IN THIS BATCH — ``descriptors/source_rsshub_rfa_korea.yaml``. That
descriptor was ALSO edited in the same commit (re-pointed off the sidecar onto
RFA's native whole-service English feed after its "frozen" report turned out to
be an upstream publication-cadence fact, not a broken route — see its header
for the full measurement). It is deliberately absent from ``SOURCE_FILES``
below: its live head is already ``state: active``, and
``_p17_registrar.py::register_descriptor`` only walks FORWARD along the
lifecycle FSM, so feeding it through this draft-batch registrar would report
``failed: no legal FSM path active -> draft`` instead of updating anything.
That edit is a LIVE CONFIG UPDATE and must go through the registry's normal
update path carrying ``state: active`` and the OLD head version in the body
(the update path re-stamps the version) — same posture as the
``source_telegram_monitor.yaml`` carve-out in
scripts/bringup_register_supply_chain_sources.py.

Ships INERT / activation is the operator's: every descriptor here ships
``identity.state: draft`` so bulk registration creates NO live actor
(``runtime/dapr_host.py`` skips draft/configured descriptors). The operator
activates each (draft -> configured -> active) after re-verifying the route on
the instance. ``source.rsshub.apnews.world`` additionally needs ``rsshub`` on
the runtime's ``LEGBA_EGRESS_ALLOW_HOSTS`` (the RSS handler's SSRF egress guard
blocks internal hosts), which docker-compose already defaults.

Source-class notes (S1-T8 taxonomy, docs/DATA_SOURCES.md §2.4): all six are
``reporting``. France 24 is state-FUNDED but editorially independent under
statute, so it takes the RFI/DW treatment — ``reporting`` with
``state_affiliation`` true on the credibility row, never ``state_media``.

Idempotent (mirrors scripts/bringup_register_wave_a_sources.py): re-runs report
``unchanged`` for already-current heads. Also seeds host-level
``source_credibility`` rows for the new upstream hosts — INSERT .. ON CONFLICT
DO NOTHING, so operator overrides and any pre-existing seed always win.

DB selection: direct-DB via DescriptorRegistry (default ``legba_pivot_test`` on
the dev rig — override with ``LEGBA_DATA_PG_DB=legba`` for production, exactly
as the other bring-up scripts).

REGISTRATION STEP (main session), once reviewed:
    docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \\
        python scripts/bringup_register_source_batch_2026_08.py
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

# The six new draft descriptors — all keyless RSS. See module docstring for why
# source_rsshub_rfa_korea.yaml (edited in the same commit) is NOT here.
SOURCE_FILES = [
    # B-7 (1) — AP re-route
    "source_rsshub_apnews_world.yaml",
    # B-7 (2) — Niger coverage pass
    "source_actuniger_politique.yaml",
    "source_actuniger_societe.yaml",
    "source_studiokalangou.yaml",
    "source_sahel_intelligence.yaml",
    "source_france24_afrique.yaml",
]

SCORED_BY = "source_batch_2026_08"

# Host-level credibility seeds (host, score, rationale, tier, state_affiliation)
# — same vocabulary/convention as CREDIBILITY_SEEDS in the Wave-A and RSSHub
# registrars. apnews.com (0.90) and france24.com (0.70) are already seeded
# elsewhere; both are re-listed so a fresh instance that skipped those
# registrars still gets them, and ON CONFLICT DO NOTHING guarantees an
# existing or operator-overridden row is never clobbered.
#
# The tier vocabulary in use on the live table is wire / gov / thinktank /
# social / aggregator — there is no "local" or "regional" tier, so the three
# domestic/specialist outlets take `wire` and carry their differentiation in
# the score instead.
CREDIBILITY_SEEDS: list[tuple[str, float, str, str, bool]] = [
    (
        "apnews.com", 0.90,
        "Associated Press — top-tier international newswire cooperative.",
        "wire", False,
    ),
    (
        "france24.com", 0.70,
        "France 24 (France Médias Monde) — French public international "
        "broadcaster, state-funded but editorially independent under statute; "
        "deep francophone Africa / Sahel desk.",
        "wire", True,
    ),
    (
        "actuniger.com", 0.62,
        "ActuNiger — Niamey-based privately-owned independent online daily, "
        "the most-read general-news site inside Niger. Scored below the "
        "international wires because it operates under a military government "
        "that has suspended foreign broadcasters, a real constraint on any "
        "domestic outlet's independence.",
        "wire", False,
    ),
    (
        "studiokalangou.org", 0.70,
        "Studio Kalangou — Fondation Hirondelle's Niger newsroom (Swiss "
        "non-profit building independent media in fragile states); daily "
        "bulletins in five national languages with a standing fact-checking "
        "desk, and up-country correspondents rather than Niamey-only copy.",
        "wire", False,
    ),
    (
        "sahel-intelligence.com", 0.60,
        "Sahel Intelligence (GIC Conseil, Paris) — decade-old specialist "
        "title on Sahel counter-terrorism, trafficking and security, with a "
        "correspondent network across 10+ countries. Scored below the "
        "international wires because its reporting is largely single-sourced "
        "to that network and rarely independently corroborated.",
        "wire", False,
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
    already_present)."""
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
            f"2026-08 source batch — AP world + Niger coverage ({len(results)} descriptors):",
            results,
        )
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
        print(
            "\nNOT included above (deliberately): descriptors/source_rsshub_rfa_korea.yaml, "
            "re-pointed off the RSSHub sidecar onto RFA's native whole-service English feed "
            "in this same commit. Its live head is already `active`, so this forward-only "
            "draft-batch registrar would refuse it (no legal FSM path active -> draft); "
            "apply that edit through the registry's normal update path carrying "
            "`state: active` and the OLD head version. See that file's header comment.\n"
            "Also unchanged: the five PAUSED AP country-hub descriptors "
            "(source_rsshub_apnews_{niger,taiwan,haiti,drcongo,north_korea}.yaml) stay "
            "paused — their upstream hub pages are the thing that froze."
        )
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

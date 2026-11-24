# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Register the 2026-09-07 source batch — Burkina Faso / Mali coverage pass.

Six BRAND-NEW draft SourceDescriptors, all keyless RSS, answering a measured
gap on the WATCH-tier desks (see the individual descriptor headers for the
full verification detail):

  ``country_watch_bf`` (Burkina Faso) had had NO desk runs since 09-05 13:06Z
  because no signal had been tagged BF since 09-02; the whole 7-day corpus
  held 5 mentions of Burkina/Ouagadougou/Traoré. ``country_watch_ml`` (Mali)
  fared little better — 6 geo-tagged signals in 4 days. NEITHER desk had a
  single BF- or ML-headquartered outlet registered; the existing
  Sahel-relevant roster (rsshub.rfi.afrique, france24.afrique,
  africanews.all, allafrica.headlines, sahelintelligence.news) is regional/
  continental wire copy, none of it domestic to either country.

This batch mirrors the 2026-08-03 Niger batch's shape exactly (one state
press agency + one high-volume independent outlet + one Fondation Hirondelle
up-country station, per country):

  BURKINA FASO:
    * ``source.aib.news``        — AIB, the national state press agency
    * ``source.lefaso.news``     — LeFaso.net, independent, high-volume
    * ``source.studioyafa.news`` — Studio Yafa (Fondation Hirondelle)

  MALI:
    * ``source.amap.news``         — AMAP, the national state press agency
    * ``source.malijet.news``      — Malijet, independent, high-volume
    * ``source.studiotamani.news`` — Studio Tamani (Fondation Hirondelle)

All six verified LIVE 2026-09-07 by direct fetch + feedparser (feedparser
6.0.12, matching the RSS handler's own parser): HTTP 200, well-formed
(bozo=False), every feed's newest item inside the 7-day freshness window. See
each descriptor's own header comment for the fetch detail, the geo (mixed-
content vs single-country) reasoning, and the licence/robots posture.

Ships INERT / activation is the operator's: every descriptor here ships
``identity.state: draft`` so bulk registration creates NO live actor
(``runtime/dapr_host.py`` skips draft/configured descriptors). The operator
activates each (draft -> configured -> active) after re-verifying the route on
the instance.

Source-class notes (S1-T8 taxonomy): AIB and AMAP are each country's own
national state press agency — the Ukrinform/IRNA treatment (``state_media``,
read as framing evidence), NOT ``official`` (that vocabulary slot is for a
government body's own primary-source statements, e.g. ``source_kremlin.yaml``,
not a general-news wire the state happens to own). LeFaso.net and Malijet are
independent commercial outlets (``reporting``, the ActuNiger/Malijet
treatment). Studio Yafa and Studio Tamani are Fondation Hirondelle non-profit
newsrooms (``reporting``, the Studio Kalangou treatment).

Idempotent (mirrors scripts/bringup_register_source_batch_2026_08.py):
re-runs report ``unchanged`` for already-current heads. Also seeds host-level
``source_credibility`` rows for the new upstream hosts — INSERT .. ON CONFLICT
DO NOTHING, so operator overrides and any pre-existing seed always win.

DB selection: direct-DB via DescriptorRegistry (default ``legba_pivot_test`` on
the dev rig — override with ``LEGBA_DATA_PG_DB=legba`` for production, exactly
as the other bring-up scripts).

REGISTRATION STEP (main session), once reviewed — NOT run by this batch, no
token, no execution here:
    docker exec -e LEGBA_DATA_PG_DB=legba legba-legba-registry-1 \\
        python scripts/bringup_register_source_batch_2026_09.py
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

# The six new draft descriptors — all keyless RSS.
SOURCE_FILES = [
    # Burkina Faso (country_watch_bf)
    "source_aib_burkina.yaml",
    "source_lefaso_net.yaml",
    "source_studioyafa.yaml",
    # Mali (country_watch_ml)
    "source_amap_mali.yaml",
    "source_malijet.yaml",
    "source_studiotamani.yaml",
]

SCORED_BY = "source_batch_2026_09"

# Host-level credibility seeds (host, score, rationale, tier, state_affiliation)
# — same vocabulary/convention as source_batch_2026_08's CREDIBILITY_SEEDS.
# Tier vocabulary on the live table: wire / gov / thinktank / social /
# aggregator — the two state press agencies take `gov` (a government-run
# press agency, not a commercial wire); the four independent/NGO outlets take
# `wire` (there is no "local" or "regional" tier).
CREDIBILITY_SEEDS: list[tuple[str, float, str, str, bool]] = [
    (
        "aib.media", 0.55,
        "AIB — Agence d'Information du Burkina, Burkina Faso's national state "
        "press agency (Ministry of Communication). Read as framing/"
        "official-position evidence, not an independent editorial voice — "
        "same source_class treatment as Ukrinform/IRNA. Burkina Faso has been "
        "under a military transitional government (Capitaine Ibrahim Traoré) "
        "since 2022.",
        "gov", True,
    ),
    (
        "lefaso.net", 0.62,
        "LeFaso.net — Ouagadougou-based privately-owned independent online "
        "daily, one of Burkina Faso's most-read general-news sites. Scored "
        "below the international wires because it operates under a military "
        "government that has suspended foreign broadcasters, a real "
        "constraint on any domestic outlet's independence — the same "
        "ActuNiger treatment.",
        "wire", False,
    ),
    (
        "studioyafa.bf", 0.70,
        "Studio Yafa — Fondation Hirondelle's Burkina Faso newsroom (Swiss "
        "non-profit building independent media in fragile states); daily "
        "bulletins plus up-country feature reporting rather than "
        "Ouagadougou-only copy — the same Studio Kalangou treatment.",
        "wire", False,
    ),
    (
        "amap.ml", 0.55,
        "AMAP — Agence Malienne de Presse et de Publicité, Mali's national "
        "state press and publicity agency (public establishment). Read as "
        "framing/official-position evidence, not an independent editorial "
        "voice — same source_class treatment as Ukrinform/IRNA. Mali has "
        "been under a military transitional government (Assimi Goïta) since "
        "2021.",
        "gov", True,
    ),
    (
        "malijet.com", 0.62,
        "Malijet — leading independent Mali news portal/aggregator. Scored "
        "below the international wires because it operates under a military "
        "government with real constraints on domestic press independence — "
        "the same ActuNiger/LeFaso.net treatment. Its own robots.txt is "
        "unusually explicit about welcoming AI/LLM retrieval and citation "
        "crawlers.",
        "wire", False,
    ),
    (
        "studiotamani.org", 0.70,
        "Studio Tamani — Fondation Hirondelle's Mali newsroom (Bamako, since "
        "2013); daily bulletins plus region-datelined humanitarian/"
        "service-delivery reporting and in-feed rumour-debunking — the same "
        "Studio Kalangou treatment.",
        "wire", False,
    ),
    (
        "royalnavy.mod.uk", 0.90,
        "UK Royal Navy / UKMTO (sccd.royalnavy.mod.uk) — the UK Maritime Trade "
        "Operations incident list (roll 73). Official reporting of maritime "
        "security incidents; a state body, read as official reporting.",
        "gov", True,
    ),
    # K-1 cyber desk (2026-11-14). Official CERTs and agencies read as
    # official reporting; the three trade outlets as specialist news (wire
    # tier, the closed CHECK has no 'trade' value). No row for the provider
    # status feeds: their hosts (amazon.com, google.com, azure.status.microsoft)
    # are shared, and a host row would score every URL under them.
    (
        "cisa.gov", 0.92,
        "US CISA (cisa.gov) — the Known Exploited Vulnerabilities catalogue and "
        "the agency's advisories (K-1). A federal agency's primary publication "
        "about vulnerabilities and threats; official, state body.",
        "gov", True,
    ),
    (
        "ncsc.gov.uk", 0.90,
        "UK National Cyber Security Centre (part of GCHQ) — news, reports and "
        "guidance (K-1). Official, state body; its attributions are the UK "
        "government's position, not independent confirmation.",
        "gov", True,
    ),
    (
        "cyber.gouv.fr", 0.90,
        "ANSSI / CERT-FR (cert.cyber.gouv.fr, formerly cert.ssi.gouv.fr) — the "
        "French government CERT's alerts and threat reports (K-1). Official, "
        "state body.",
        "gov", True,
    ),
    (
        "cert.europa.eu", 0.88,
        "CERT-EU — the cybersecurity service of the EU institutions; its "
        "security advisories (K-1). Official EU body.",
        "gov", True,
    ),
    (
        "bsi.bund.de", 0.88,
        "German Federal Office for Information Security (BSI) — press releases "
        "and warnings (K-1). Official, state body.",
        "gov", True,
    ),
    (
        "therecord.media", 0.80,
        "The Record (Recorded Future News) — original cybersecurity reporting "
        "with named-official sourcing and a record of first reports (K-1). "
        "Owned by a threat-intelligence vendor (Recorded Future, a Mastercard "
        "company): first-hand but not disinterested on actors the parent tracks.",
        "wire", False,
    ),
    (
        "cyberscoop.com", 0.78,
        "CyberScoop (Scoop News Group) — original reporting on cyber policy, "
        "government and nation-state operations (K-1).",
        "wire", False,
    ),
    (
        "securityweek.com", 0.75,
        "SecurityWeek — daily cybersecurity news, incidents, vulnerabilities "
        "and ICS security; original reporting plus round-ups that aggregate "
        "other outlets (K-1).",
        "wire", False,
    ),
    # X-1 source expansion (2026-11-14). Hosts whose descriptors write text
    # signals only. No row for gov.uk or federalreserve.gov (the profile's
    # source catalog already seeds both), t.me (shared by every channel), or
    # the series / register endpoints (they write observations and structured
    # change records, not cited pages).
    (
        "novayagazeta.eu", 0.78,
        "Novaya Gazeta Europe — independent Russian-language newsroom in exile "
        "(Riga), founded by Novaya Gazeta journalists in 2022 (X-1).",
        "wire", False,
    ),
    (
        "radiotamazuj.org", 0.72,
        "Radio Tamazuj — independent South Sudanese news outlet (X-1).",
        "wire", False,
    ),
    (
        "oc-media.org", 0.75,
        "OC Media — independent newsroom in Tbilisi covering the South and "
        "North Caucasus (X-1).",
        "wire", False,
    ),
    (
        "ec.europa.eu", 0.90,
        "European Commission (ec.europa.eu) — the Commission's own press "
        "releases and statements (X-1). Official EU institution; its "
        "statements are the Commission's position.",
        "gov", True,
    ),
    (
        "ecb.europa.eu", 0.92,
        "European Central Bank — press releases, monetary-policy decisions and "
        "speeches (X-1). Official, the euro area's central bank.",
        "gov", True,
    ),
    (
        "ecdc.europa.eu", 0.90,
        "European Centre for Disease Prevention and Control — threat reports, "
        "risk assessments and news (X-1). Official EU agency.",
        "gov", True,
    ),
    # I-5 (2026-11-14). The one I-5 host whose descriptor writes text
    # signals; the five series endpoints (World Bank, NBU, BCB, Bank of
    # England database, Eurostat) write observations only, as X-1's ECB
    # series do, and get no row.
    (
        "jma.go.jp", 0.93,
        "Japan Meteorological Agency — Japan's national meteorological and "
        "seismological authority; its earthquake bulletins are the official "
        "Japanese record of a quake's hypocentre, magnitude and intensity (I-5).",
        "gov", True,
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
            f"2026-09 source batch — Burkina Faso / Mali coverage pass ({len(results)} descriptors):",
            results,
        )
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

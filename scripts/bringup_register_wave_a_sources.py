# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-6 — register the Wave-A no-auth breadth batch (draft SourceDescriptors).

The "Wave-A" set is the additive-breadth slice of the 2026-07-02/03 new-source
research sweep (planning/SOURCE_RESEARCH_2026-07-02.md, "Wave A — register this
week (verified live, no auth)"). It ADDS 41 independently verified, keyless
``rss`` / ``json_api`` feeds — desk-gap fills (Israel, Taiwan, N.Korea, Japan,
Brazil/Mexico/Argentina, Spain, Gulf, Indonesia, Russia counterweight,
Australia, Canada, Italy), topical/unit feeds (ISW, State Dept, UN, Kremlin,
EUvsDisinfo, DFRLab, defense/energy/nuclear/arms-control), and zero-effort
quality adds (Guardian, Euronews, Le Monde, Spiegel, Bangkok Post, Dawn).

This is ADDITIVE breadth, not revival. Deliberately EXCLUDED from the batch
(and why):
  * the A1 "re-point existing" feeds (WHO news, CDC travel/outbreaks, EIA press)
    are ALREADY registered in the S-1 catalog (bringup_register_source_catalog.py)
    at their current endpoints — a re-point is the operator's, not a new source;
  * EMSC FDSN was deliberately RETIRED 2026-06-12 as duplicative-of-USGS noise;
  * Focus Taiwan / CNA is already double-covered (source.cna.all +
    source.rsshub.focustaiwan.news);
  * OFAC SDN delta is an XML two-step (year index -> DeltaFile) that the generic
    rss/json_api handlers don't cover — sanctions already ride opensanctions.*.
The three A1 entries that ARE here (WHO Disease Outbreak News json_api, EIA Today
in Energy, CGTN World) are NET-NEW source ids, not re-points.

Ships INERT / activation is the operator's (mirrors the RSSHub-lane registrar):
  * Every descriptor ships ``identity.state: draft`` so bulk registration creates
    NO live actor (``runtime/dapr_host.py`` skips draft/configured descriptors).
    The operator activates each (draft -> configured -> active) AFTER verifying
    the route is live on the instance. The three ``json_api`` feeds carry field
    paths marked VERIFY in-descriptor: probe the live JSON before activating.
  * State media: only ``source.cgtn.world`` is state-controlled Chinese media,
    knowingly ingested for the CN desk as labeled ``state_media`` FRAMING (the
    house rule permits Chinese state media only for the CN desk). State-FUNDED
    but editorially-conventional public broadcasters (NHK/ABC/CBC/RFE-RL/EBC/
    ANTARA) stay ``reporting`` with ``state_affiliation`` True on the credibility
    row — the honest-provenance / editorial-independence split the catalog uses.

Idempotent (mirrors scripts/bringup_register_sources.py): re-runs report
``unchanged`` for already-current heads. Also seeds host-level
``source_credibility`` rows for the new upstream outlets — INSERT .. ON CONFLICT
DO NOTHING, so operator overrides and any pre-existing seed (eia.gov / state.gov
from the S-1 catalog) always win.

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

# The 41 Wave-A draft descriptors, grouped by the research sweep's A1..A4
# provenance sections (planning/SOURCE_RESEARCH_2026-07-02.md).
SOURCE_FILES = [
    # A1 — additive dead-feed COMPLEMENTS (net-new ids; the pure re-points live
    #      in the S-1 catalog already)
    "source_who_disease_outbreak_news.yaml",
    "source_eia_today_in_energy.yaml",
    "source_cgtn_world.yaml",
    # A2 — desk-gap fills
    "source_timesofisrael.yaml",
    "source_jpost.yaml",
    "source_taipeitimes.yaml",
    "source_dailynk.yaml",
    "source_38north.yaml",
    "source_nhk_world.yaml",
    "source_japantimes.yaml",
    "source_agenciabrasil.yaml",
    "source_mexiconewsdaily.yaml",
    "source_batimes.yaml",
    "source_elpais_english.yaml",
    "source_aawsat.yaml",
    "source_middleeasteye.yaml",
    "source_antara.yaml",
    "source_meduza.yaml",
    "source_rferl.yaml",
    "source_abc_australia.yaml",
    "source_cbc_world.yaml",
    "source_ansa.yaml",
    # A3 — topical / unit feeds
    "source_isw.yaml",
    "source_stategov_press.yaml",
    "source_un_press.yaml",
    "source_kremlin.yaml",
    "source_euvsdisinfo.yaml",
    "source_dfrlab.yaml",
    "source_breakingdefense.yaml",
    "source_defensenews.yaml",
    "source_navalnews.yaml",
    "source_oilprice.yaml",
    "source_rigzone.yaml",
    "source_worldnuclearnews.yaml",
    "source_armscontrol.yaml",
    # A4 — quality adds
    "source_guardian_world.yaml",
    "source_euronews.yaml",
    "source_lemonde_english.yaml",
    "source_spiegel_international.yaml",
    "source_bangkokpost.yaml",
    "source_dawn.yaml",
]

SCORED_BY = "p3_6.wave_a"

# Host-level credibility seeds for the Wave-A upstream outlets
# (host, score, rationale, tier, state_affiliation). ``tier`` uses the S-1
# catalog vocabulary (wire / gov / aggregator / thinktank / social);
# ``state_affiliation`` is HONEST provenance (the outlet is state-funded/owned)
# and is ORTHOGONAL to source_class (editorial independence) — exactly the
# France 24 / VOA / Yonhap precedent. eia.gov and state.gov are already seeded
# by the S-1 catalog; ON CONFLICT DO NOTHING guarantees those rows are kept.
CREDIBILITY_SEEDS: list[tuple[str, float, str, str, bool]] = [
    ("who.int", 0.90, "World Health Organization — authoritative on outbreaks and global health.", "gov", True),
    ("eia.gov", 0.90, "US Energy Information Administration — primary-source energy statistics.", "gov", True),
    ("cgtn.com", 0.30, "China Global Television Network — Chinese state broadcaster; conduit for official positions.", "wire", True),
    ("timesofisrael.com", 0.70, "Independent English-language Israeli daily.", "wire", False),
    ("jpost.com", 0.68, "Center-right Israeli English daily; Israel-desk counterweight.", "wire", False),
    ("taipeitimes.com", 0.68, "Independent Taiwanese English daily (pro-independence editorial line).", "wire", False),
    ("dailynk.com", 0.62, "Seoul-based outlet with an inside-North-Korea defector source network; full bodies.", "wire", False),
    ("38north.org", 0.78, "Stimson Center program — expert DPRK/nuclear analysis with named authors.", "thinktank", False),
    ("nhk.or.jp", 0.82, "NHK World — Japan's public broadcaster (state-funded, editorially conventional).", "wire", True),
    ("japantimes.co.jp", 0.72, "Independent English-language Japanese daily.", "wire", False),
    ("agenciabrasil.ebc.com.br", 0.65, "EBC (Brazilian public broadcaster) state news agency; state-funded, conventional wire (label).", "wire", True),
    ("mexiconewsdaily.com", 0.62, "Independent English-language Mexico news site.", "wire", False),
    ("batimes.com.ar", 0.62, "Argentina's English-language newspaper (Perfil group).", "wire", False),
    ("elpais.com", 0.75, "Spain's leading daily (English edition); strong Spain + Latin America coverage.", "wire", False),
    ("aawsat.com", 0.60, "Pan-Arab daily (Saudi-owned, SRMG); Gulf editorial perspective.", "wire", True),
    ("middleeasteye.net", 0.60, "London-based Middle East news site; Gulf + Israel/Iran adjacency.", "wire", False),
    ("antaranews.com", 0.60, "ANTARA — Indonesia's state news agency; state-owned, conventional wire (label).", "wire", True),
    ("meduza.io", 0.70, "Independent Russian outlet (exiled, Latvia-based); Russia-desk counterweight to TASS.", "wire", False),
    ("rferl.org", 0.72, "Radio Free Europe/Radio Liberty (USAGM) — US-funded under a statutory editorial firewall (VOA-class).", "wire", True),
    ("abc.net.au", 0.80, "Australian Broadcasting Corporation — public broadcaster; independent newsroom.", "wire", True),
    ("cbc.ca", 0.80, "Canadian Broadcasting Corporation — public broadcaster; independent newsroom.", "wire", True),
    ("ansa.it", 0.72, "Agenzia Nazionale Stampa Associata — Italy's leading wire agency (cooperative).", "wire", False),
    ("understandingwar.org", 0.82, "Institute for the Study of War — same-day RU/UA + China-Taiwan + Iran campaign assessments.", "thinktank", False),
    ("state.gov", 0.90, "US Department of State — primary-source diplomatic press releases.", "gov", True),
    ("un.org", 0.88, "United Nations — primary-source meetings coverage incl. Security Council (no SC-only feed exists).", "gov", True),
    ("kremlin.ru", 0.35, "Kremlin (Russian presidency) — government PRIMARY source; treat as official-position/framing signal, LOW for neutral facts.", "gov", True),
    ("euvsdisinfo.eu", 0.70, "EEAS StratCom (EU diplomatic service) project — curated pro-Kremlin disinformation case database.", "thinktank", True),
    ("dfrlab.org", 0.78, "Atlantic Council's Digital Forensic Research Lab — OSINT info-ops attribution; published methods.", "thinktank", False),
    ("breakingdefense.com", 0.72, "Defense trade press; budget / industrial-base angle.", "wire", False),
    ("defensenews.com", 0.72, "Sightline Media defense trade press; non-US procurement coverage.", "wire", False),
    ("navalnews.com", 0.68, "Naval-affairs trade press; TW Strait / SCS / Red Sea maritime coverage.", "wire", False),
    ("oilprice.com", 0.60, "Energy-markets news site.", "wire", False),
    ("rigzone.com", 0.62, "Upstream oil & gas operations news.", "wire", False),
    ("world-nuclear-news.org", 0.68, "World Nuclear Association news service (industry-funded); Rosatom exports, Iran, fuel supply.", "wire", False),
    ("armscontrol.org", 0.78, "Arms Control Association — arms-control research/advocacy; named expert authors.", "thinktank", False),
    ("theguardian.com", 0.82, "Major UK/global daily; broad watch-tier coverage.", "wire", False),
    ("euronews.com", 0.70, "Pan-European broadcaster (agency-fed).", "wire", False),
    ("lemonde.fr", 0.82, "France's leading daily (English edition); quality Africa/geopolitics angle.", "wire", False),
    ("spiegel.de", 0.82, "Der Spiegel International — independent German investigative weekly (low volume, high signal).", "wire", False),
    ("bangkokpost.com", 0.65, "Thailand's leading English daily; SEA adjacency.", "wire", False),
    ("dawn.com", 0.68, "Pakistan's oldest English daily; non-India subcontinent voice.", "wire", False),
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
            f"Wave-A draft sources ({len(results)} descriptors):", results
        )
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""S-1 — register the no-auth source catalog (~45 verified RSS/GeoJSON feeds).

The live working set registered only 4 sources while 13 source kinds exist;
this script is the registration-side recovery: an EMBEDDED catalog of
independently verified, no-auth ``rss`` / ``geojson`` feeds with proper
geo/tags metadata (so target source-selectors can match), conservative
cadences (1h wires / 2–6h slow publishers, stagger-offset per source), and
``source_credibility`` seed rows carrying the migration-0031 ``tier`` +
``state_affiliation`` provenance columns.

License note: feed URLs are facts. Every feed below was probed live by us
(2026-06-09/10: HTTP GET + feedparser/GeoJSON parse); descriptors are written
in OUR SourceDescriptor schema. No third-party catalog code or annotation
prose is copied.

Modes
-----
* ``--verify``  — live HTTP probe + parse check per feed; prints a verdict
  table (ok / ok-redirect / dead / parse-fail / empty). Registers NOTHING.
* default       — direct-DB registration via DescriptorRegistry against the
  migrated Postgres (default ``legba_pivot_test`` — override with
  LEGBA_DATA_PG_DB; same _p17_registrar pattern as the other bring-up
  scripts), then seeds ``source_credibility`` rows
  (INSERT .. ON CONFLICT DO NOTHING — operator overrides always win).
  Idempotent: re-runs report ``unchanged`` for already-current heads.

Probe drops (verified dead/blocked on 2026-06-09/10 — keep out until rechecked):
  * ReliefWeb updates RSS  — HTTP 202 WAF/challenge page on every variant.
  * ProMED                 — 404 on both historical feed paths.
  * Brookings              — /feed/ now 302-redirects to the homepage.
  * Nation Africa          — 404/403 on known feed paths.
  * DFAT Smartraveller     — connection timeout (repeated) from the rig.
  * CISA advisories        — HTTP 403 bot-blocked (edge "Access Denied");
                             scraper fallback is out of S-1 scope.
  * Kyodo + SIPRI          — feeds discontinued upstream (not probed).
  * USGS significant quakes — already live as ``source.usgs.earthquakes``
    (descriptors/source_usgs_earthquakes.yaml); the catalog adds the broader
    M4.5+ week feed instead of churning the existing descriptor.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import pathlib
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

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
from legba.data.registry.descriptor import DescriptorRegistry  # noqa: E402
from legba.data.schemas.lifecycle import AbstractionLevel, LifecycleState  # noqa: E402
from legba.data.schemas.properties import Cron  # noqa: E402
from legba.data.schemas.source import (  # noqa: E402
    CadenceBlock,
    FilterStage,
    SourceDescriptor,
    SourceIdentity,
    SourceOutput,
    SourcePipeline,
    SourceScope,
)

# The UA the probes were verified with — also pinned into each descriptor's
# config so the runtime handler sends exactly what we verified against.
USER_AGENT = "Mozilla/5.0 (compatible; LegbaBot/2.0; +https://legba.invalid)"

OWNER = "s1_catalog"
CREATED = datetime(2026, 6, 9, 0, 0, 0, tzinfo=timezone.utc)
SCORED_BY = "catalog.seed"

VALID_TIERS = ("wire", "gov", "aggregator", "thinktank", "social")

# S1-T8 — the STATE-CONTROLLED catalog feeds. A hand-curated set (NOT derived
# from state_affiliation, which is also True for state-FUNDED-but-independent
# public broadcasters like VOA / France 24 / Yonhap): only genuine
# state-controlled outlets whose rationale marks them a conduit for official
# positions belong here. These map to source_class == state_media (framing /
# official-position evidence, LOW-tier for facts). Keep in sync with the
# _REQUIRED_STATE_AFFILIATED set in tests/data_pkg/test_source_catalog_bringup.py
# (minus voanews, which has a statutory editorial firewall → reporting).
STATE_MEDIA_IDS: frozenset[str] = frozenset({
    "source.tass.english",       # Russian state news agency
    "source.xinhua.world",       # Chinese state news agency
    "source.globaltimes.all",    # CCP-affiliated (People's Daily group)
    "source.tehrantimes.all",    # Iranian state-aligned
    "source.anadolu.english",    # Turkish state agency (documented editorial influence)
})


def catalog_source_class(entry: "CatalogEntry") -> str:
    """S1-T8 — the source_class for a catalog entry (a fixed, auditable rule).

    state_media is hand-curated (:data:`STATE_MEDIA_IDS`); the rest derive from
    the existing credibility ``tier`` so every entry lands in the schema
    vocabulary (reporting / analysis / official / state_media):
      * ``thinktank`` -> ``analysis``  (research / advocacy / OSINT)
      * ``gov``       -> ``official``  (government / IGO primary publishers)
      * ``wire`` / ``aggregator`` / ``social`` -> ``reporting`` (the default)
    """
    if entry.id in STATE_MEDIA_IDS:
        return "state_media"
    if entry.tier == "thinktank":
        return "analysis"
    if entry.tier == "gov":
        return "official"
    return "reporting"


# ---------------------------------------------------------------------------
# Catalog model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CatalogEntry:
    """One verified no-auth feed + its credibility seed metadata."""

    id: str
    name: str
    kind: str                       # "rss" | "geojson"
    url: str
    tags: tuple[str, ...]
    cadence_minutes: int            # 30 / 60 / 120 / 360 — conservative only
    geo: tuple[str, ...] = ()       # ISO codes when region-specific; () = global
    language: str = "en"
    # source_credibility seed (host-level; deduped across entries)
    cred_host: str = ""
    cred_score: float = 0.5
    cred_rationale: str = ""
    tier: str = "wire"
    state_affiliation: bool = False
    extra_config: dict = field(default_factory=dict)
    # Opt a structured (geojson) feed into TEXT enrichment (language_detect +
    # ner_multilingual) on top of the default geocode-only chain. Default off:
    # most GIS feeds (USGS quakes, NASA EONET) are high-volume / low-text and
    # geocode is all they need. NWS Severe/Extreme alerts, by contrast, carry a
    # rich English headline/description in `properties` (mapped to the signal's
    # `title`), so leaving them text-unenriched dragged language/entity coverage
    # down (live audit: 400 NWS signals at 0% language/entity). Flagging them in
    # gives those high-value gov alerts the same metadata as the news feeds.
    enrich_text: bool = False
    # Opt a TEXT-rich news feed into ingest-time fact extraction (the
    # `fact_extractor` stage, after ner_multilingual). Default off: each stage
    # adds REBEL/extract calls, so we roll it out incrementally to the
    # full-article news feeds that yield rich (subject,predicate,value)
    # relations (france24/cna/npr/economist) and leave gov/finance/advisory +
    # GeoJSON feeds out (graph-and-data Wave-1b item 4; REVIEW §3.6). The stage
    # defaults reject_quantity_endpoints ON, so no per-descriptor config needed.
    fact_extract: bool = False


# ---------------------------------------------------------------------------
# The embedded catalog — every URL probed live 2026-06-09/10 (HTTP 200 +
# feedparser/GeoJSON parse). Order: crisis/humanitarian, human rights,
# think tanks, defense/OSINT, regional wires, gov/finance, GeoJSON.
# ---------------------------------------------------------------------------

CATALOG: tuple[CatalogEntry, ...] = (
    # --- RSS: crisis / humanitarian -------------------------------------
    CatalogEntry(
        id="source.gdacs.alerts", name="GDACS — Disaster Alerts", kind="rss",
        url="https://www.gdacs.org/xml/rss.xml",
        tags=("crisis", "humanitarian", "hazard", "alert", "global"),
        cadence_minutes=60,
        cred_host="gdacs.org", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="EC/UN Global Disaster Alert and Coordination System — authoritative multi-hazard alerts.",
    ),
    CatalogEntry(
        id="source.crisisgroup.latest", name="International Crisis Group — Latest", kind="rss",
        url="https://www.crisisgroup.org/rss",
        tags=("crisis", "conflict", "analysis", "thinktank"),
        cadence_minutes=360,
        cred_host="crisisgroup.org", cred_score=0.80, tier="thinktank",
        cred_rationale="Conflict-prevention NGO; field-researched, methodology published.",
    ),
    CatalogEntry(
        id="source.newhumanitarian.all", name="The New Humanitarian", kind="rss",
        url="https://www.thenewhumanitarian.org/rss.xml",
        tags=("crisis", "humanitarian", "news"),
        cadence_minutes=120,
        cred_host="thenewhumanitarian.org", cred_score=0.75, tier="wire",
        cred_rationale="Independent humanitarian newsroom (formerly UN IRIN).",
    ),
    CatalogEntry(
        id="source.who.news", name="WHO — News", kind="rss",
        url="https://www.who.int/rss-feeds/news-english.xml",
        tags=("health", "crisis", "gov", "global"),
        cadence_minutes=360,
        cred_host="who.int", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="World Health Organization — authoritative on outbreaks and global health.",
    ),
    CatalogEntry(
        id="source.iaea.topnews", name="IAEA — Top News", kind="rss",
        url="https://www.iaea.org/feeds/topnews",
        tags=("nuclear", "energy", "gov", "global"),
        cadence_minutes=360,
        cred_host="iaea.org", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="International Atomic Energy Agency — authoritative on nuclear safety/safeguards.",
    ),
    CatalogEntry(
        id="source.cdc.travel_notices", name="CDC — Travel Health Notices", kind="rss",
        url="https://wwwnc.cdc.gov/travel/rss/notices.xml",
        tags=("health", "travel", "advisory", "gov"),
        cadence_minutes=360,
        cred_host="cdc.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="US Centers for Disease Control — authoritative public-health agency.",
    ),
    CatalogEntry(
        id="source.cdc.outbreaks_us", name="CDC — US Outbreaks", kind="rss",
        url="https://tools.cdc.gov/api/v2/resources/media/285676.rss",
        tags=("health", "outbreak", "gov"),
        cadence_minutes=360, geo=("US",),
        cred_host="cdc.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="US Centers for Disease Control — authoritative public-health agency.",
    ),
    # --- RSS: human rights ----------------------------------------------
    CatalogEntry(
        id="source.hrw.news", name="Human Rights Watch — News", kind="rss",
        url="https://www.hrw.org/rss/news",
        tags=("human_rights", "news", "global"),
        cadence_minutes=360,
        cred_host="hrw.org", cred_score=0.75, tier="thinktank",
        cred_rationale="Advocacy organisation with documented research methodology.",
    ),
    CatalogEntry(
        id="source.amnesty.latest", name="Amnesty International — Latest", kind="rss",
        url="https://www.amnesty.org/en/latest/feed/",
        tags=("human_rights", "news", "global"),
        cadence_minutes=360,
        cred_host="amnesty.org", cred_score=0.75, tier="thinktank",
        cred_rationale="Advocacy organisation with documented research methodology.",
    ),
    CatalogEntry(
        id="source.civicus.monitor", name="CIVICUS Monitor", kind="rss",
        url="https://monitor.civicus.org/feed/",
        tags=("human_rights", "civil_society", "global"),
        cadence_minutes=360,
        cred_host="civicus.org", cred_score=0.70, tier="thinktank",
        cred_rationale="Civic-space tracking alliance; country reports from partner organisations.",
    ),
    # --- RSS: think tanks / analysis ------------------------------------
    CatalogEntry(
        id="source.csis.analysis", name="CSIS — Analysis", kind="rss",
        url="https://www.csis.org/rss.xml",
        tags=("thinktank", "analysis", "geopolitical"),
        cadence_minutes=360,
        cred_host="csis.org", cred_score=0.80, tier="thinktank",
        cred_rationale="Center for Strategic and International Studies — established policy research.",
    ),
    CatalogEntry(
        id="source.rand.press", name="RAND — Press Releases", kind="rss",
        url="https://www.rand.org/news/press.xml",
        tags=("thinktank", "analysis", "geopolitical"),
        cadence_minutes=360,
        cred_host="rand.org", cred_score=0.80, tier="thinktank",
        cred_rationale="RAND Corporation — peer-reviewed policy research.",
    ),
    CatalogEntry(
        id="source.atlanticcouncil.all", name="Atlantic Council", kind="rss",
        url="https://www.atlanticcouncil.org/feed/",
        tags=("thinktank", "analysis", "geopolitical"),
        cadence_minutes=360,
        cred_host="atlanticcouncil.org", cred_score=0.75, tier="thinktank",
        cred_rationale="Established foreign-policy think tank; declared funders.",
    ),
    CatalogEntry(
        id="source.warontherocks.all", name="War on the Rocks", kind="rss",
        url="https://warontherocks.com/feed/",
        tags=("thinktank", "analysis", "defense"),
        cadence_minutes=360,
        cred_host="warontherocks.com", cred_score=0.75, tier="thinktank",
        cred_rationale="Practitioner-written defense/strategy analysis; named expert authors.",
    ),
    CatalogEntry(
        id="source.bellingcat.all", name="Bellingcat", kind="rss",
        url="https://www.bellingcat.com/feed/",
        tags=("osint", "analysis", "investigation"),
        cadence_minutes=360,
        cred_host="bellingcat.com", cred_score=0.80, tier="thinktank",
        cred_rationale="Open-source investigation collective; methods published per piece.",
    ),
    CatalogEntry(
        id="source.foreignpolicy.all", name="Foreign Policy", kind="rss",
        url="https://foreignpolicy.com/feed/",
        tags=("news", "analysis", "geopolitical"),
        cadence_minutes=120,
        cred_host="foreignpolicy.com", cred_score=0.75, tier="wire",
        cred_rationale="Established foreign-affairs magazine.",
    ),
    CatalogEntry(
        id="source.foreignaffairs.all", name="Foreign Affairs", kind="rss",
        url="https://www.foreignaffairs.com/rss.xml",
        tags=("thinktank", "analysis", "geopolitical"),
        cadence_minutes=360,
        cred_host="foreignaffairs.com", cred_score=0.80, tier="thinktank",
        cred_rationale="Council on Foreign Relations publication.",
    ),
    # --- RSS: defense / OSINT -------------------------------------------
    CatalogEntry(
        id="source.defenseone.all", name="Defense One", kind="rss",
        url="https://www.defenseone.com/rss/all/",
        tags=("defense", "news"),
        cadence_minutes=120,
        cred_host="defenseone.com", cred_score=0.75, tier="wire",
        cred_rationale="GovExec defense trade press; sourced reporting.",
    ),
    CatalogEntry(
        id="source.twz.all", name="The War Zone (TWZ)", kind="rss",
        url="https://www.twz.com/feed",
        tags=("defense", "osint", "news"),
        cadence_minutes=120,
        cred_host="twz.com", cred_score=0.70, tier="wire",
        cred_rationale="Defense/OSINT trade press; imagery-driven reporting.",
    ),
    CatalogEntry(
        id="source.gcaptain.all", name="gCaptain — Maritime News", kind="rss",
        url="https://gcaptain.com/feed/",
        tags=("maritime", "defense", "news"),
        cadence_minutes=120,
        cred_host="gcaptain.com", cred_score=0.70, tier="wire",
        cred_rationale="Maritime-industry trade press.",
    ),
    CatalogEntry(
        id="source.taskandpurpose.all", name="Task & Purpose", kind="rss",
        url="https://taskandpurpose.com/feed/",
        tags=("defense", "news"),
        cadence_minutes=360, geo=("US",),
        cred_host="taskandpurpose.com", cred_score=0.65, tier="wire",
        cred_rationale="US military-affairs outlet.",
    ),
    # --- RSS: regional wires ---------------------------------------------
    CatalogEntry(
        id="source.yonhap.english", name="Yonhap News — English", kind="rss",
        url="https://en.yna.co.kr/RSS/news.xml",
        tags=("news", "wire", "asia"),
        cadence_minutes=60, geo=("KR",),
        cred_host="yna.co.kr", cred_score=0.80, tier="wire", state_affiliation=True,
        cred_rationale="South Korea's national news agency (partly state-funded; editorially conventional wire).",
    ),
    CatalogEntry(
        id="source.cna.all", name="Channel News Asia", kind="rss",
        url="https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
        tags=("news", "asia"),
        cadence_minutes=60, geo=("SG",),
        cred_host="channelnewsasia.com", cred_score=0.75, tier="wire", state_affiliation=True,
        cred_rationale="Mediacorp (Singapore state-owned) regional broadcaster.",
        fact_extract=True,
    ),
    CatalogEntry(
        id="source.anadolu.english", name="Anadolu Agency — English", kind="rss",
        url="https://www.aa.com.tr/en/rss/default?cat=guncel",
        tags=("news", "wire", "middle_east"),
        cadence_minutes=60, geo=("TR",),
        cred_host="aa.com.tr", cred_score=0.45, tier="wire", state_affiliation=True,
        cred_rationale="Turkish state news agency; documented government editorial influence.",
    ),
    CatalogEntry(
        id="source.tehrantimes.all", name="Tehran Times", kind="rss",
        url="https://www.tehrantimes.com/rss",
        tags=("news", "middle_east"),
        cadence_minutes=120, geo=("IR",),
        cred_host="tehrantimes.com", cred_score=0.30, tier="wire", state_affiliation=True,
        cred_rationale="Iranian state-aligned outlet; treat as official-position signal.",
    ),
    CatalogEntry(
        id="source.tass.english", name="TASS — English", kind="rss",
        url="https://tass.com/rss/v2.xml",
        tags=("news", "wire", "europe"),
        cadence_minutes=60, geo=("RU",),
        cred_host="tass.com", cred_score=0.30, tier="wire", state_affiliation=True,
        cred_rationale="Russian state news agency; conduit for official positions.",
    ),
    CatalogEntry(
        id="source.allafrica.headlines", name="AllAfrica — Latest Headlines", kind="rss",
        url="https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
        tags=("news", "aggregator", "africa"),
        cadence_minutes=60,
        cred_host="allafrica.com", cred_score=0.60, tier="aggregator",
        cred_rationale="Aggregates African outlets; credibility varies with the underlying publisher.",
    ),
    CatalogEntry(
        id="source.africanews.all", name="Africanews", kind="rss",
        url="https://www.africanews.com/feed/rss",
        tags=("news", "africa"),
        cadence_minutes=60,
        cred_host="africanews.com", cred_score=0.70, tier="wire",
        cred_rationale="Euronews-group pan-African broadcaster.",
    ),
    CatalogEntry(
        id="source.timesofindia.world", name="Times of India — World", kind="rss",
        url="https://timesofindia.indiatimes.com/rssfeeds/296589292.cms",
        tags=("news", "world", "south_asia"),
        cadence_minutes=60,
        cred_host="timesofindia.indiatimes.com", cred_score=0.65, tier="wire",
        cred_rationale="Major Indian daily; world desk.",
    ),
    CatalogEntry(
        id="source.hindustantimes.world", name="Hindustan Times — World", kind="rss",
        url="https://www.hindustantimes.com/feeds/rss/world-news/rssfeed.xml",
        tags=("news", "world", "south_asia"),
        cadence_minutes=60,
        cred_host="hindustantimes.com", cred_score=0.65, tier="wire",
        cred_rationale="Major Indian daily; world desk.",
    ),
    CatalogEntry(
        id="source.mercopress.all", name="MercoPress — South Atlantic News", kind="rss",
        url="https://en.mercopress.com/rss",
        tags=("news", "americas"),
        cadence_minutes=120,
        cred_host="mercopress.com", cred_score=0.65, tier="wire",
        cred_rationale="Independent South Atlantic / Mercosur news agency.",
    ),
    CatalogEntry(
        id="source.globalvoices.all", name="Global Voices", kind="rss",
        url="https://globalvoices.org/feed/",
        tags=("news", "civil_society", "aggregator", "global"),
        cadence_minutes=120,
        cred_host="globalvoices.org", cred_score=0.65, tier="aggregator",
        cred_rationale="Community newsroom curating citizen media; bylined volunteer contributors.",
    ),
    CatalogEntry(
        id="source.voa.africa", name="VOA — Africa", kind="rss",
        url="https://www.voanews.com/api/z-botl-vomx-tpertmq",
        tags=("news", "africa"),
        cadence_minutes=60,
        cred_host="voanews.com", cred_score=0.60, tier="wire", state_affiliation=True,
        cred_rationale="Voice of America — US-government international broadcaster (USAGM); statutory editorial firewall.",
    ),
    CatalogEntry(
        id="source.npr.world", name="NPR — World", kind="rss",
        url="https://feeds.npr.org/1004/rss.xml",
        tags=("news", "world", "global"),
        cadence_minutes=60,
        cred_host="npr.org", cred_score=0.90, tier="wire",
        cred_rationale="US public broadcaster (already in 0014 baseline; row kept for catalog completeness).",
        fact_extract=True,
    ),
    CatalogEntry(
        id="source.france24.english", name="France 24 — English", kind="rss",
        url="https://www.france24.com/en/rss",
        tags=("news", "world", "global"),
        cadence_minutes=60,
        cred_host="france24.com", cred_score=0.70, tier="wire", state_affiliation=True,
        cred_rationale="France Médias Monde — French state-funded international broadcaster.",
        fact_extract=True,
    ),
    CatalogEntry(
        id="source.economist.international", name="The Economist — International", kind="rss",
        url="https://www.economist.com/international/rss.xml",
        tags=("news", "analysis", "world"),
        cadence_minutes=360,
        cred_host="economist.com", cred_score=0.90, tier="wire",
        cred_rationale="Already in 0014 baseline; row kept for catalog completeness.",
        fact_extract=True,
    ),
    CatalogEntry(
        id="source.xinhua.world", name="Xinhua — World (English)", kind="rss",
        url="http://www.xinhuanet.com/english/rss/worldrss.xml",
        tags=("news", "wire", "world", "asia"),
        cadence_minutes=60,
        cred_host="xinhuanet.com", cred_score=0.35, tier="wire", state_affiliation=True,
        cred_rationale="Chinese state news agency; conduit for official positions.",
    ),
    CatalogEntry(
        id="source.globaltimes.all", name="Global Times", kind="rss",
        url="https://www.globaltimes.cn/rss/outbrain.xml",
        tags=("news", "asia"),
        cadence_minutes=120, geo=("CN",),
        cred_host="globaltimes.cn", cred_score=0.30, tier="wire", state_affiliation=True,
        cred_rationale="People's Daily-group (CCP-affiliated) outlet; strongly state-aligned.",
    ),
    CatalogEntry(
        id="source.aljazeera.arabic", name="Al Jazeera — Arabic", kind="rss",
        url="https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/73d0e1b4-532f-45ef-b135-bfdff8b8cab9",
        tags=("news", "world", "middle_east"),
        cadence_minutes=60, language="ar",
        cred_host="aljazeera.net", cred_score=0.60, tier="wire", state_affiliation=True,
        cred_rationale="Qatari state-funded broadcaster; Arabic edition carries stronger editorial slant than AJE.",
    ),
    # --- RSS: gov / finance ----------------------------------------------
    CatalogEntry(
        id="source.federalreserve.press", name="US Federal Reserve — Press Releases", kind="rss",
        url="https://www.federalreserve.gov/feeds/press_all.xml",
        tags=("gov", "finance", "economic"),
        cadence_minutes=360, geo=("US",),
        cred_host="federalreserve.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="US central bank — primary-source monetary policy.",
    ),
    CatalogEntry(
        id="source.eia.press", name="US EIA — Press Releases", kind="rss",
        url="https://www.eia.gov/rss/press_rss.xml",
        tags=("gov", "energy", "economic"),
        cadence_minutes=360, geo=("US",),
        cred_host="eia.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="US Energy Information Administration — primary-source energy statistics.",
    ),
    CatalogEntry(
        id="source.stategov.travel_advisories", name="US State Dept — Travel Advisories", kind="rss",
        url="https://travel.state.gov/_res/rss/TAsTWs.xml",
        tags=("gov", "travel", "advisory"),
        cadence_minutes=360,
        cred_host="state.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="Official US travel advisories — primary-source policy positions.",
    ),
    CatalogEntry(
        id="source.fcdo.travel_advice", name="UK FCDO — Foreign Travel Advice", kind="rss",
        url="https://www.gov.uk/foreign-travel-advice.atom",
        tags=("gov", "travel", "advisory"),
        cadence_minutes=360,
        cred_host="gov.uk", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="Official UK travel advisories — primary-source policy positions.",
    ),
    # --- GeoJSON (kind exists; source_usgs_earthquakes.yaml is the template)
    # "Major events only" curation (2026-06-12): the raw geophysical feeds
    # firehose minor tremors + every county heat advisory, swamping the signal
    # set with low-newsworthiness noise (and EMSC duplicated USGS). We point
    # each at its significant/severe-only slice instead.
    CatalogEntry(
        id="source.usgs.earthquakes_m45", name="USGS — Significant Earthquakes, Past Week (GeoJSON)",
        kind="geojson",
        # Was 4.5_week (~hundreds/wk of routine quakes). The "significant" feed
        # is USGS's own curated major-event slice (notable M6.5+ / high-impact).
        url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_week.geojson",
        tags=("gis", "geospatial", "structured", "hazard", "seismic", "global"),
        cadence_minutes=30,
        cred_host="usgs.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="US Geological Survey — authoritative seismic feed (significant events).",
        extra_config={"feature_id_key": "id", "max_features": 5000},
    ),
    # EMSC retired 2026-06-12: duplicated USGS coverage with lower-quality
    # titles and no curated significance slice — net noise. (Live descriptor
    # is retired separately; removed here so re-runs don't re-register it.)
    CatalogEntry(
        id="source.nws.active_alerts", name="US NWS — Severe/Extreme Weather Alerts (GeoJSON)",
        kind="geojson",
        # Was all active alerts (every county heat advisory / minor statement).
        # severity=Severe,Extreme keeps only the high-impact, newsworthy alerts.
        url="https://api.weather.gov/alerts/active?severity=Severe,Extreme",
        tags=("gis", "geospatial", "structured", "hazard", "weather", "alert", "gov"),
        cadence_minutes=30, geo=("US",),
        cred_host="weather.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="US National Weather Service — severe/extreme weather alerts only.",
        extra_config={"feature_id_key": "id", "max_features": 5000},
        # Severe/Extreme alert headlines are rich English text — enrich language
        # + entities so these high-value gov alerts stop dragging coverage.
        enrich_text=True,
    ),
    CatalogEntry(
        id="source.nasa.eonet_events", name="NASA EONET — Natural Events (GeoJSON)",
        kind="geojson",
        # EONET is already curated to named natural events; trim the window
        # 7d -> 3d to keep the slice current and lower backfill volume.
        url="https://eonet.gsfc.nasa.gov/api/v3/events/geojson?days=3",
        tags=("gis", "geospatial", "structured", "hazard", "gov", "global"),
        cadence_minutes=60,
        cred_host="nasa.gov", cred_score=0.90, tier="gov", state_affiliation=True,
        cred_rationale="NASA Earth Observatory Natural Event Tracker.",
        extra_config={"feature_id_key": "id", "max_features": 5000},
    ),
)


# ---------------------------------------------------------------------------
# Descriptor construction
# ---------------------------------------------------------------------------


def _stagger_minute(source_id: str, modulo: int = 60) -> int:
    """Stable per-source minute offset so 40+ feeds don't all poll at :00."""
    return int(hashlib.sha256(source_id.encode()).hexdigest(), 16) % modulo


def cron_for(entry: CatalogEntry) -> str:
    """Conservative, constant-period cron with a stable stagger offset."""
    if entry.cadence_minutes < 60:
        # e.g. "7-59/30 * * * *" — every 30 min, offset by a stable 0..29.
        offset = _stagger_minute(entry.id, entry.cadence_minutes)
        return f"{offset}-59/{entry.cadence_minutes} * * * *"
    minute = _stagger_minute(entry.id)
    hours = entry.cadence_minutes // 60
    if hours == 1:
        return f"{minute} * * * *"
    hour_offset = _stagger_minute(entry.id, hours)
    return f"{minute} {hour_offset}-23/{hours} * * *"


def build_descriptor(entry: CatalogEntry) -> SourceDescriptor:
    """Build + validate one SourceDescriptor from a catalog entry.

    Mirrors the registered YAML descriptors (source_bbc_world.yaml /
    source_usgs_earthquakes.yaml): shared open poll source, baseline
    enrichment ONCE at the source, media by reference.
    """
    if entry.kind == "rss":
        enrichment = [
            FilterStage(kind="language_detect", config={}),
            FilterStage(kind="ner_multilingual", config={}),
        ]
        # Ingest-time fact extraction (opt-in, text-rich news feeds only).
        # Mirrors the registered source_bbc_world.yaml stage: reuse the REBEL
        # triples NER already produced; reject_quantity_endpoints is ON by the
        # stage's own default, so no per-descriptor config is needed.
        if entry.fact_extract:
            enrichment.append(
                FilterStage(
                    kind="fact_extractor",
                    config={
                        "backend": "relation",
                        "emit_graph_edges": False,
                        "max_facts_per_signal": 30,
                    },
                )
            )
        enrichment.append(FilterStage(kind="geocode", config={}))
        max_age = 86_400            # 24h news window
    elif entry.kind == "geojson":
        # Model-free structured modality — geocode-only by default. Feeds that
        # carry meaningful text in their feature properties (NWS alert
        # headlines) opt into language + entity enrichment via enrich_text so
        # they get the same language/entity coverage as the news feeds; the
        # enricher reads payload["title"] (geojson maps the feature headline →
        # title), which language_detect / ner_multilingual both consume.
        if entry.enrich_text:
            enrichment = [
                FilterStage(kind="language_detect", config={}),
                FilterStage(kind="ner_multilingual", config={}),
                FilterStage(kind="geocode", config={}),
            ]
        else:
            enrichment = [FilterStage(kind="geocode", config={})]
        max_age = 604_800           # match the hazard feeds' weekly windows
    else:                            # pragma: no cover - guarded by tests
        raise ValueError(f"unsupported catalog kind: {entry.kind!r}")

    config: dict = {
        "url": {"factory_kind": "text", "raw": entry.url},
        "user_agent": {"factory_kind": "text", "raw": USER_AGENT},
    }
    for key, raw in entry.extra_config.items():
        factory_kind = "number" if isinstance(raw, (int, float)) else "text"
        config[key] = {"factory_kind": factory_kind, "raw": raw}

    return SourceDescriptor(
        identity=SourceIdentity(
            id=entry.id,
            name=entry.name,
            kind=entry.kind,
            schema_uri="legba/source/1.0.0",
            version="0" * 16,        # placeholder — registry stamps content hash
            abstraction_level=AbstractionLevel.L1,
            state=LifecycleState.ACTIVE,
            owner=OWNER,
            created=CREATED,
        ),
        scope=SourceScope(
            owner_tenant="shared",
            geo=list(entry.geo),
            languages=[entry.language],
            tags=list(entry.tags),
            source_class=catalog_source_class(entry),   # S1-T8
        ),
        acquisition="poll",
        config=config,
        cadence=CadenceBlock(
            schedule=Cron(raw=cron_for(entry)),
            cooldown_seconds=60,
            jitter_seconds=30,
        ),
        pipeline=SourcePipeline(
            ingestion_filters=[
                FilterStage(kind="dedupe_tier_1", config={}),
                FilterStage(kind="dedupe_tier_2", config={}),
            ],
            enrichment=enrichment,
            media="reference",
        ),
        output=SourceOutput(retention="interest", max_age_seconds=max_age, delivery="lossy"),
        subscription_policy="open",
    )


def credibility_rows() -> list[tuple[str, float, str, str, str, bool]]:
    """Deduped (host, score, rationale, scored_by, tier, state_affiliation) rows.

    Entries sharing a host (e.g. the two cdc.gov feeds) must agree on the
    credibility fields — enforced here so the catalog can't drift silently.
    """
    rows: dict[str, tuple[str, float, str, str, str, bool]] = {}
    for entry in CATALOG:
        row = (
            entry.cred_host, entry.cred_score, entry.cred_rationale,
            SCORED_BY, entry.tier, entry.state_affiliation,
        )
        existing = rows.get(entry.cred_host)
        if existing is not None and existing != row:
            raise ValueError(
                f"conflicting credibility seeds for host {entry.cred_host!r}"
            )
        rows[entry.cred_host] = row
    return list(rows.values())


# ---------------------------------------------------------------------------
# Registration (direct-DB, _p17_registrar pattern)
# ---------------------------------------------------------------------------


async def register_catalog(
    pg: PostgresStore, reg: DescriptorRegistry
) -> list[RegisterResult]:
    """Idempotently register every catalog descriptor. Pure function of the
    injected store/registry — the test suite drives it against the per-session
    test DB; the operator's `main()` wires the env-selected DB."""
    results: list[RegisterResult] = []
    for entry in CATALOG:
        desc = build_descriptor(entry)
        results.append(
            await register_descriptor(pg, reg, family=Family.SOURCE, descriptor=desc)
        )
    return results


async def seed_credibility(pg: PostgresStore) -> tuple[int, int]:
    """Seed source_credibility rows (0014 convention: ON CONFLICT DO NOTHING —
    operator overrides and the 0014 baseline always win). Returns
    (inserted, already_present). Requires migration 0031 (tier /
    state_affiliation columns) — fails loud if it has not been applied."""
    inserted = 0
    skipped = 0
    async with pg.acquire() as conn:
        for host, score, rationale, scored_by, tier, state_aff in credibility_rows():
            status = await conn.execute(
                """
                INSERT INTO source_credibility
                    (source_host, score, score_rationale, scored_by, tier, state_affiliation)
                VALUES ($1, $2, $3, $4, $5, $6)
                ON CONFLICT (source_host) DO NOTHING
                """,
                host, score, rationale, scored_by, tier, state_aff,
            )
            if status.endswith("1"):
                inserted += 1
            else:
                skipped += 1
    return inserted, skipped


# ---------------------------------------------------------------------------
# --verify mode: live probe + parse check, registers nothing
# ---------------------------------------------------------------------------


@dataclass
class ProbeResult:
    entry_id: str
    verdict: str                    # ok / ok-redirect / dead / parse-fail / empty
    detail: str = ""


async def _probe_one(client, entry: CatalogEntry) -> ProbeResult:
    import httpx

    try:
        resp = await client.get(entry.url)
    except httpx.HTTPError as exc:
        return ProbeResult(entry.id, "dead", f"{type(exc).__name__}: {exc}"[:120])
    if resp.status_code >= 400:
        return ProbeResult(entry.id, "dead", f"HTTP {resp.status_code}")
    if resp.status_code != 200:
        return ProbeResult(entry.id, "dead", f"HTTP {resp.status_code} (non-200 challenge?)")

    redirected = bool(resp.history)
    if entry.kind == "rss":
        import feedparser

        parsed = feedparser.parse(resp.content)
        if not parsed.entries:
            why = "no entries"
            if getattr(parsed, "bozo", 0):
                why += f"; bozo: {getattr(parsed, 'bozo_exception', '')}"
            return ProbeResult(entry.id, "empty", why[:120])
    else:  # geojson
        try:
            doc = json.loads(resp.content)
        except ValueError as exc:
            return ProbeResult(entry.id, "parse-fail", str(exc)[:120])
        if not isinstance(doc, dict) or (
            doc.get("type") not in ("FeatureCollection", "Feature")
            and "features" not in doc
        ):
            return ProbeResult(entry.id, "parse-fail", "not a GeoJSON document")

    if redirected:
        return ProbeResult(entry.id, "ok-redirect", f"final: {resp.url}"[:120])
    return ProbeResult(entry.id, "ok")


async def verify_catalog() -> int:
    """Probe every catalog feed; print a verdict table. Returns #failures."""
    import httpx

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=25.0,
        headers={"User-Agent": USER_AGENT},
    ) as client:
        sem = asyncio.Semaphore(8)

        async def bounded(entry: CatalogEntry) -> ProbeResult:
            async with sem:
                return await _probe_one(client, entry)

        results = await asyncio.gather(*(bounded(e) for e in CATALOG))

    failures = 0
    print(f"S-1 catalog verify — {len(results)} feeds:")
    for r in results:
        mark = {"ok": "+", "ok-redirect": "~"}.get(r.verdict, "!")
        line = f"  {mark} {r.verdict:>12}  {r.entry_id}"
        if r.detail:
            line += f"   ({r.detail})"
        print(line)
        if r.verdict not in ("ok", "ok-redirect"):
            failures += 1
    print(f"  -> {len(results) - failures} ok, {failures} failing")
    return failures


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


async def _amain(verify_only: bool) -> int:
    if verify_only:
        return 1 if await verify_catalog() else 0

    pg, reg = await open_registry()
    try:
        results = await register_catalog(pg, reg)
        failures = print_results(f"S-1 source catalog ({len(results)} sources):", results)
        inserted, skipped = await seed_credibility(pg)
        print(f"source_credibility seeds: +{inserted} inserted, ={skipped} already present")
    finally:
        await close_registry(pg, reg)
    return 1 if failures else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--verify", action="store_true",
        help="live HTTP probe + parse check per feed; registers nothing",
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args.verify))


if __name__ == "__main__":
    sys.exit(main())

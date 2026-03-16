#!/usr/bin/env python3
"""Seed high-value data sources into the Legba sources table.

Idempotent — checks for existing sources by URL before inserting.
Safe to re-run.

Usage:
    # Against the running Postgres (from host or container)
    python3 scripts/seed_sources.py

    # Or via docker
    docker exec -it legba-postgres-1 psql -U legba -d legba -f /dev/stdin < scripts/seed_sources.sql
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

# Add src to path so we can import legba modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import asyncpg
from dotenv import load_dotenv

load_dotenv()


@dataclass
class SourceDef:
    name: str
    url: str
    source_type: str = "rss"
    category: str = ""
    geo_origin: str = ""
    language: str = "en"
    fetch_interval_minutes: int = 60
    reliability: float = 0.6
    bias_label: str = "center"
    ownership_type: str = "independent"
    coverage_scope: str = "global"
    description: str = ""
    tags: list[str] = field(default_factory=list)
    query_template: str = ""
    auth_config: dict = field(default_factory=dict)
    initial_status: str = "active"  # set to "paused" to disable on seed


# ============================================================================
# TIER 1: No auth required, real-time/frequent
# ============================================================================

TIER_1_SOURCES = [
    # --- GDELT (highest leverage free source) ---
    SourceDef(
        name="GDELT DOC API — Global",
        url="https://api.gdeltproject.org/api/v2/doc/doc",
        source_type="api",
        category="political",
        fetch_interval_minutes=15,
        reliability=0.7,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="Global news event extraction across 100+ languages, 250 results per query",
        tags=["gdelt", "news", "events", "global"],
        query_template="https://api.gdeltproject.org/api/v2/doc/doc?query=sourcelang:English&mode=artlist&maxrecords=250&format=json&timespan={timespan}",
    ),
    SourceDef(
        name="GDELT DOC API — Conflict",
        url="https://api.gdeltproject.org/api/v2/doc/doc?query=conflict",
        source_type="api",
        category="conflict",
        fetch_interval_minutes=30,
        reliability=0.7,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="GDELT filtered for conflict-related articles",
        tags=["gdelt", "conflict"],
        query_template='https://api.gdeltproject.org/api/v2/doc/doc?query="conflict" OR "attack" OR "military" OR "war"&mode=artlist&maxrecords=250&format=json&timespan={timespan}',
    ),
    SourceDef(
        name="GDELT DOC API — Disaster",
        url="https://api.gdeltproject.org/api/v2/doc/doc?query=disaster",
        source_type="api",
        category="disaster",
        fetch_interval_minutes=30,
        reliability=0.7,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="GDELT filtered for disaster/emergency articles",
        tags=["gdelt", "disaster"],
        query_template='https://api.gdeltproject.org/api/v2/doc/doc?query="earthquake" OR "flood" OR "hurricane" OR "tsunami" OR "wildfire" OR "disaster"&mode=artlist&maxrecords=250&format=json&timespan={timespan}',
    ),

    # --- Seismic / Geological ---
    SourceDef(
        name="USGS Earthquakes 4.5+",
        url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/4.5_day.geojson",
        source_type="geojson",
        category="disaster",
        fetch_interval_minutes=5,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="Global earthquakes M4.5+ in the last day, updated every minute",
        tags=["usgs", "earthquake", "seismic", "real-time"],
    ),
    SourceDef(
        name="USGS Significant Earthquakes",
        url="https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/significant_month.geojson",
        source_type="geojson",
        category="disaster",
        fetch_interval_minutes=60,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="Significant global earthquakes in the last month",
        tags=["usgs", "earthquake", "seismic"],
    ),
    SourceDef(
        name="EMSC Seismology",
        url="https://www.seismicportal.eu/fdsnws/event/1/query?limit=100&format=json&minmag=4",
        source_type="geojson",
        category="disaster",
        fetch_interval_minutes=10,
        reliability=0.9,
        ownership_type="nonprofit",
        geo_origin="FR",
        coverage_scope="global",
        description="Euro-Med Seismological Centre — real-time earthquake feed via SeismicPortal FDSN",
        tags=["earthquake", "seismic", "europe"],
    ),

    # --- Natural Events / Environment ---
    SourceDef(
        name="NASA EONET",
        url="https://eonet.gsfc.nasa.gov/api/v3/events?status=open&limit=50",
        source_type="api",
        category="environment",
        fetch_interval_minutes=30,
        reliability=0.9,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="NASA Earth Observatory Natural Event Tracker — active events globally",
        tags=["nasa", "natural-events", "fire", "storm", "volcano"],
    ),
    SourceDef(
        name="USGS Volcanoes — Elevated",
        url="https://volcanoes.usgs.gov/hans-public/api/volcano/getElevatedVolcanoes",
        source_type="api",
        category="disaster",
        fetch_interval_minutes=360,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="Volcanoes at elevated alert level",
        tags=["usgs", "volcano"],
    ),

    # --- Disaster / Humanitarian ---
    SourceDef(
        name="GDACS Alerts",
        url="https://www.gdacs.org/xml/rss.xml",
        source_type="rss",
        category="disaster",
        fetch_interval_minutes=15,
        reliability=0.85,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="Global Disaster Alert and Coordination System — real-time disaster alerts",
        tags=["gdacs", "disaster", "humanitarian", "real-time"],
    ),
    # Requires pre-approved appname — register at reliefweb.int
    SourceDef(
        name="ReliefWeb Reports",
        url="https://api.reliefweb.int/v2/reports?appname=legba&limit=50&sort[]=date:desc",
        source_type="api",
        category="social",
        fetch_interval_minutes=30,
        reliability=0.85,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="UN OCHA ReliefWeb — humanitarian reports and situation updates",
        tags=["reliefweb", "humanitarian", "un"],
        initial_status="paused",
    ),
    SourceDef(
        name="IFRC Emergencies",
        url="https://goadmin.ifrc.org/api/v2/event/?limit=50&format=json",
        source_type="api",
        category="disaster",
        fetch_interval_minutes=60,
        reliability=0.8,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="International Federation of Red Cross — emergency operations",
        tags=["ifrc", "humanitarian", "disaster"],
    ),
    SourceDef(
        name="New Humanitarian",
        url="https://www.thenewhumanitarian.org/rss/all.xml",
        source_type="rss",
        category="social",
        fetch_interval_minutes=60,
        reliability=0.8,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="Independent humanitarian journalism",
        tags=["humanitarian", "conflict", "crisis"],
    ),

    # --- Health ---
    SourceDef(
        name="WHO Disease Outbreaks",
        url="https://www.who.int/api/news/diseaseoutbreaknews?$orderby=PublicationDate%20desc&$top=50",
        source_type="api",
        category="health",
        fetch_interval_minutes=60,
        reliability=0.9,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="WHO disease outbreak news and emergency declarations",
        tags=["who", "health", "outbreak", "pandemic"],
    ),
    SourceDef(
        name="CDC Travel Health Notices",
        url="https://wwwnc.cdc.gov/travel/rss/notices.xml",
        source_type="rss",
        category="health",
        fetch_interval_minutes=120,
        reliability=0.9,
        bias_label="center",
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="CDC global disease outbreak travel notices with alert levels",
        tags=["health", "disease", "outbreak", "cdc"],
    ),
    SourceDef(
        name="CDC US Outbreaks",
        url="https://tools.cdc.gov/api/v2/resources/media/285676.rss",
        source_type="rss",
        category="health",
        fetch_interval_minutes=120,
        reliability=0.9,
        bias_label="center",
        ownership_type="state",
        geo_origin="US",
        coverage_scope="national",
        description="Active US outbreak investigations - Salmonella, Measles, etc.",
        tags=["health", "disease", "outbreak", "cdc", "us"],
    ),
    SourceDef(
        name="ProMED Disease Alerts",
        url="https://promedmail.org/promed-rss-feed/",
        source_type="rss",
        category="health",
        fetch_interval_minutes=60,
        reliability=0.85,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="International Society for Infectious Diseases — early disease outbreak detection",
        tags=["health", "disease", "outbreak"],
        initial_status="paused",  # Consistently failing
    ),

    # --- Cyber / Security ---
    SourceDef(
        name="CISA Known Exploited Vulnerabilities",
        url="https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json",
        source_type="static_json",
        category="technology",
        fetch_interval_minutes=360,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="CISA catalog of actively exploited CVEs",
        tags=["cisa", "cyber", "cve", "vulnerability"],
    ),
    SourceDef(
        name="CISA Advisories",
        url="https://www.cisa.gov/cybersecurity-advisories/all.xml",
        source_type="rss",
        category="technology",
        fetch_interval_minutes=60,
        reliability=0.9,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="CISA cybersecurity advisories and alerts",
        tags=["cisa", "cyber", "advisory"],
        initial_status="paused",  # Cloudflare-blocked, KEV catalog covers critical data
    ),

    # --- Weather / Alerts ---
    SourceDef(
        name="NWS Active Alerts",
        url="https://api.weather.gov/alerts/active?status=actual&severity=Extreme,Severe",
        source_type="geojson",
        category="disaster",
        fetch_interval_minutes=10,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="national",
        description="US National Weather Service — extreme and severe weather alerts",
        tags=["nws", "weather", "disaster", "us"],
    ),
    SourceDef(
        name="NOAA Storm Events",
        url="https://api.weather.gov/alerts/active?event=Tornado,Hurricane,Tsunami",
        source_type="geojson",
        category="disaster",
        fetch_interval_minutes=15,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="national",
        description="NOAA severe storm event alerts",
        tags=["noaa", "storm", "tornado", "hurricane"],
    ),

    # --- Economic ---
    SourceDef(
        name="Frankfurter Exchange Rates",
        url="https://api.frankfurter.dev/v1/latest",
        source_type="api",
        category="economic",
        fetch_interval_minutes=360,
        reliability=0.9,
        ownership_type="public_broadcast",
        geo_origin="EU",
        coverage_scope="global",
        description="ECB exchange rates for 30+ currencies",
        tags=["economic", "forex", "ecb"],
    ),

    # --- Political / Governance ---
    SourceDef(
        name="OpenSanctions",
        url="https://api.opensanctions.org/collections/default/",
        source_type="api",
        category="political",
        fetch_interval_minutes=1440,
        reliability=0.85,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="330+ sources: sanctions, PEPs, criminal entities",
        tags=["sanctions", "political", "entities"],
    ),
    SourceDef(
        name="Crisis Group",
        url="https://www.crisisgroup.org/rss-0",
        source_type="rss",
        category="conflict",
        fetch_interval_minutes=360,
        reliability=0.9,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="International Crisis Group — 80+ active conflict monitoring",
        tags=["conflict", "analysis", "think-tank"],
    ),

    # --- Conflict ---
    SourceDef(
        name="UCDP API",
        url="https://ucdpapi.pcr.uu.se/api/gedevents/24.1?pagesize=100",
        source_type="api",
        category="conflict",
        fetch_interval_minutes=1440,
        reliability=0.9,
        ownership_type="nonprofit",
        geo_origin="SE",
        coverage_scope="global",
        description="Uppsala Conflict Data Program — global armed conflict events",
        tags=["ucdp", "conflict", "armed-conflict"],
    ),

    # --- Human Rights ---
    SourceDef(
        name="Human Rights Watch",
        url="https://www.hrw.org/rss/news",
        source_type="rss",
        category="social",
        fetch_interval_minutes=120,
        reliability=0.85,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="HRW news and reports",
        tags=["human-rights", "social"],
    ),
    SourceDef(
        name="Amnesty International",
        url="https://www.amnesty.org/en/feed/",
        source_type="rss",
        category="social",
        fetch_interval_minutes=120,
        reliability=0.85,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="Amnesty International news feed",
        tags=["human-rights", "social"],
    ),
    SourceDef(
        name="CIVICUS Monitor",
        url="https://monitor.civicus.org/updates/feed/",
        source_type="rss",
        category="social",
        fetch_interval_minutes=360,
        reliability=0.8,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="Civic space monitoring — protests, repression, press freedom",
        tags=["human-rights", "civic-space", "protest"],
    ),

    # --- Think Tanks / Analysis ---
    SourceDef(
        name="Brookings",
        url="https://www.brookings.edu/feed/",
        source_type="rss",
        category="political",
        fetch_interval_minutes=120,
        reliability=0.85,
        bias_label="center_left",
        ownership_type="nonprofit",
        geo_origin="US",
        description="Brookings Institution analysis",
        tags=["think-tank", "analysis", "policy"],
    ),
    SourceDef(
        name="CSIS",
        url="https://www.csis.org/rss.xml",
        source_type="rss",
        category="political",
        fetch_interval_minutes=120,
        reliability=0.85,
        ownership_type="nonprofit",
        geo_origin="US",
        description="Center for Strategic and International Studies",
        tags=["think-tank", "analysis", "defense", "policy"],
    ),
    SourceDef(
        name="RAND Corporation",
        url="https://www.rand.org/news.xml",
        source_type="rss",
        category="political",
        fetch_interval_minutes=120,
        reliability=0.85,
        ownership_type="nonprofit",
        geo_origin="US",
        description="RAND Corporation research and analysis",
        tags=["think-tank", "analysis", "defense"],
    ),
    SourceDef(
        name="Atlantic Council",
        url="https://www.atlanticcouncil.org/feed/",
        source_type="rss",
        category="political",
        fetch_interval_minutes=120,
        reliability=0.8,
        ownership_type="nonprofit",
        geo_origin="US",
        description="Atlantic Council analysis",
        tags=["think-tank", "analysis", "nato", "transatlantic"],
    ),
    SourceDef(
        name="War on the Rocks",
        url="https://warontherocks.com/feed/",
        source_type="rss",
        category="conflict",
        fetch_interval_minutes=120,
        reliability=0.8,
        ownership_type="independent",
        geo_origin="US",
        description="Defense and national security analysis",
        tags=["defense", "analysis", "military"],
    ),
    SourceDef(
        name="Bellingcat",
        url="https://www.bellingcat.com/feed/",
        source_type="rss",
        category="conflict",
        fetch_interval_minutes=120,
        reliability=0.85,
        ownership_type="nonprofit",
        geo_origin="NL",
        description="Open-source investigative journalism",
        tags=["osint", "investigation", "conflict"],
    ),
    SourceDef(
        name="SIPRI",
        url="https://www.sipri.org/rss.xml",
        initial_status="paused",  # RSS feed discontinued
        source_type="rss",
        category="conflict",
        fetch_interval_minutes=360,
        reliability=0.9,
        ownership_type="nonprofit",
        geo_origin="SE",
        description="Stockholm International Peace Research Institute — arms, conflict, disarmament",
        tags=["arms", "conflict", "peace", "defense"],
    ),
]

# ============================================================================
# TIER 1 continued: Regional RSS sources (no auth)
# ============================================================================

REGIONAL_SOURCES = [
    # East Asia
    SourceDef(name="NHK World", url="https://www3.nhk.or.jp/nhkworld/data/en/news/list.json",
              source_type="api", category="political", geo_origin="JP", tags=["asia", "japan"],
              ownership_type="public_broadcast", fetch_interval_minutes=30),
    SourceDef(name="Kyodo News", url="https://english.kyodonews.net/rss/news.xml",
              category="political", geo_origin="JP", tags=["asia", "japan"],
              fetch_interval_minutes=60, initial_status="paused"),  # RSS feed discontinued
    SourceDef(name="Yonhap News", url="https://en.yna.co.kr/RSS/news.xml",
              category="political", geo_origin="KR", tags=["asia", "korea"],
              fetch_interval_minutes=60),
    SourceDef(name="Channel News Asia", url="https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml",
              category="political", geo_origin="SG", tags=["asia", "southeast-asia"],
              coverage_scope="regional", fetch_interval_minutes=30),

    # Middle East
    SourceDef(name="Al Jazeera English", url="https://www.aljazeera.com/xml/rss/all.xml",
              category="political", geo_origin="QA", tags=["middle-east", "global"],
              ownership_type="state", bias_label="center_left", fetch_interval_minutes=30),
    SourceDef(name="Anadolu Agency", url="https://www.aa.com.tr/en/rss/default?cat=world",
              category="political", geo_origin="TR", tags=["middle-east", "turkey"],
              ownership_type="state", fetch_interval_minutes=60),
    SourceDef(name="Tehran Times", url="https://www.tehrantimes.com/rss",
              category="political", geo_origin="IR", tags=["middle-east", "iran"],
              ownership_type="state", language="en", bias_label="far_left", fetch_interval_minutes=120),

    # Russia / Eurasia
    SourceDef(name="TASS", url="https://tass.com/rss/v2.xml",
              category="political", geo_origin="RU", tags=["russia", "eurasia"],
              ownership_type="state", bias_label="right", fetch_interval_minutes=60),

    # Africa
    SourceDef(name="AllAfrica", url="https://allafrica.com/tools/headlines/rdf/latest/headlines.rdf",
              category="political", geo_origin="ZA", tags=["africa"],
              coverage_scope="regional", fetch_interval_minutes=60),
    SourceDef(name="Africanews", url="https://www.africanews.com/feed/",
              category="political", geo_origin="CG", tags=["africa"],
              coverage_scope="regional", fetch_interval_minutes=60),
    SourceDef(name="Nation Africa", url="https://nation.africa/kenya/rss.xml",
              category="political", geo_origin="KE", tags=["africa", "east-africa"],
              coverage_scope="regional", fetch_interval_minutes=120),

    # South Asia
    SourceDef(name="Times of India", url="https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
              category="political", geo_origin="IN", tags=["south-asia", "india"],
              coverage_scope="national", fetch_interval_minutes=60),
    SourceDef(name="Hindustan Times", url="https://www.hindustantimes.com/feeds/rss/topnews/rssfeed.xml",
              category="political", geo_origin="IN", tags=["south-asia", "india"],
              coverage_scope="national", fetch_interval_minutes=60),

    # Latin America
    SourceDef(name="MercoPress", url="https://en.mercopress.com/rss",
              category="political", geo_origin="UY", tags=["latin-america"],
              coverage_scope="regional", fetch_interval_minutes=120),

    # Multi-regional
    SourceDef(name="Global Voices", url="https://globalvoices.org/feed/",
              category="social", tags=["global", "citizen-journalism", "human-rights"],
              ownership_type="nonprofit", fetch_interval_minutes=120),
    SourceDef(name="VOA News", url="https://www.voanews.com/api/zqboml-vomx-tpeivmy",
              category="political", geo_origin="US", tags=["global"],
              ownership_type="state", fetch_interval_minutes=60),
]

# ============================================================================
# TIER 2: Requires API key (from env vars)
# ============================================================================

TIER_2_SOURCES = [
    # ACLED migrated to OAuth 2.0 (Sept 2025). Requires env vars:
    #   ACLED_CLIENT_ID, ACLED_CLIENT_SECRET
    SourceDef(
        name="ACLED Conflict Events",
        url="https://acleddata.com/api/acled/read",
        source_type="api",
        category="conflict",
        fetch_interval_minutes=1440,
        reliability=0.9,
        ownership_type="nonprofit",
        coverage_scope="global",
        description="Armed Conflict Location and Event Data — structured conflict events with actors, fatalities, locations",
        tags=["acled", "conflict", "violence", "protest"],
        query_template="https://acleddata.com/api/acled/read?limit=200&event_date={date_today}|{date_today}&event_date_where=BETWEEN",
        auth_config={"type": "bearer", "token_url": "https://api.acleddata.com/oauth/token", "client_id": "$ACLED_CLIENT_ID", "client_secret": "$ACLED_CLIENT_SECRET"},
        initial_status="paused",  # Credentials not yet configured
    ),
    SourceDef(
        name="NASA FIRMS — Global Thermal Anomalies",
        url="https://firms.modaps.eosdis.nasa.gov/api/area",
        source_type="csv",
        category="environment",
        fetch_interval_minutes=180,
        reliability=0.9,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="NASA Fire Information for Resource Management — VIIRS/MODIS thermal anomalies",
        tags=["nasa", "firms", "fire", "thermal"],
        query_template="https://firms.modaps.eosdis.nasa.gov/api/area/csv/$FIRMS_MAP_KEY/VIIRS_SNPP_NRT/world/1",
        auth_config={"type": "query_param", "key": "map_key", "value": "$FIRMS_MAP_KEY"},
    ),
    SourceDef(
        name="AlienVault OTX Pulses",
        url="https://otx.alienvault.com/api/v1/pulses/subscribed",
        source_type="api",
        category="technology",
        fetch_interval_minutes=60,
        reliability=0.8,
        ownership_type="corporate",
        coverage_scope="global",
        description="Threat intelligence — IOCs, malware, threat actor activity",
        tags=["cyber", "threat-intel", "ioc", "malware"],
        auth_config={"type": "api_key", "header": "X-OTX-API-KEY", "value": "$OTX_API_KEY"},
        initial_status="paused",  # Pending account setup
    ),
    SourceDef(
        name="NVD CVE Feed",
        url="https://services.nvd.nist.gov/rest/json/cves/2.0",
        source_type="api",
        category="technology",
        fetch_interval_minutes=360,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="NIST National Vulnerability Database — CVE records with CVSS scores",
        tags=["cyber", "cve", "vulnerability", "nist"],
        query_template="https://services.nvd.nist.gov/rest/json/cves/2.0?resultsPerPage=50&lastModStartDate={date_yesterday}T00:00:00.000&lastModEndDate={date_today}T23:59:59.999",
        auth_config={"type": "api_key", "header": "apiKey", "value": "$NVD_API_KEY"},
    ),
    SourceDef(
        name="FRED Economic Data",
        url="https://api.stlouisfed.org/fred/series/observations",
        source_type="api",
        category="economic",
        fetch_interval_minutes=1440,
        reliability=0.95,
        ownership_type="state",
        geo_origin="US",
        coverage_scope="global",
        description="Federal Reserve Economic Data — key macro indicators (GDP, CPI, unemployment, fed funds rate)",
        tags=["economic", "fed", "macro"],
        query_template="https://api.stlouisfed.org/fred/series/observations?series_id=DFF&sort_order=desc&limit=10&file_type=json&api_key=$FRED_API_KEY",
    ),
    SourceDef(
        name="Event Registry — Global",
        url="https://eventregistry.org/api/v1/article/getArticles",
        source_type="api",
        category="political",
        fetch_interval_minutes=60,
        reliability=0.8,
        ownership_type="corporate",
        coverage_scope="global",
        description="Structured news events with entity resolution, better than GDELT for specifics",
        tags=["news", "events", "entities"],
        query_template="https://eventregistry.org/api/v1/article/getArticles?resultType=articles&articlesSortBy=date&articlesCount=50&lang=eng&dateStart={date_yesterday}&dateEnd={date_today}&apiKey=$EVENT_REGISTRY_API_KEY",
        auth_config={"type": "query_param", "key": "apiKey", "value": "$EVENT_REGISTRY_API_KEY"},
    ),
]


async def seed(dry_run: bool = False) -> None:
    """Insert all sources into the database."""
    host = os.getenv("POSTGRES_HOST", "localhost")
    port = int(os.getenv("POSTGRES_PORT", "5432"))
    user = os.getenv("POSTGRES_USER", "legba")
    password = os.getenv("POSTGRES_PASSWORD", "legba")
    database = os.getenv("POSTGRES_DB", "legba")
    dsn = f"postgresql://{user}:{password}@{host}:{port}/{database}"

    print(f"Connecting to {host}:{port}/{database}...")
    pool = await asyncpg.create_pool(dsn, min_size=1, max_size=2)

    all_sources = TIER_1_SOURCES + REGIONAL_SOURCES + TIER_2_SOURCES
    inserted = 0
    skipped = 0
    errors = 0

    for src in all_sources:
        # Check for existing source by URL
        existing = await pool.fetchrow(
            "SELECT id, name FROM sources WHERE url = $1", src.url,
        )
        if existing:
            skipped += 1
            print(f"  SKIP: {src.name} (exists as '{existing['name']}')")
            continue

        # Also check by name
        existing_name = await pool.fetchrow(
            "SELECT id, url FROM sources WHERE name = $1", src.name,
        )
        if existing_name:
            skipped += 1
            print(f"  SKIP: {src.name} (name exists, url={existing_name['url']})")
            continue

        if dry_run:
            print(f"  DRY-RUN: Would insert {src.name} ({src.source_type}, {src.category})")
            inserted += 1
            continue

        # Build the full data JSONB
        source_id = uuid4()
        now = datetime.now(timezone.utc)
        data = {
            "id": str(source_id),
            "name": src.name,
            "url": src.url,
            "source_type": src.source_type,
            "description": src.description,
            "reliability": src.reliability,
            "bias_label": src.bias_label,
            "ownership_type": src.ownership_type,
            "geo_origin": src.geo_origin,
            "language": src.language,
            "timeliness": 0.5,
            "coverage_scope": src.coverage_scope,
            "status": src.initial_status,
            "fetch_interval_minutes": src.fetch_interval_minutes,
            "fetch_success_count": 0,
            "fetch_failure_count": 0,
            "events_produced_count": 0,
            "consecutive_failures": 0,
            "tags": src.tags,
            "config": src.auth_config if src.auth_config else {},
            "query_template": src.query_template,
            "category": src.category,
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        try:
            await pool.execute(
                """
                INSERT INTO sources (id, data, name, url, source_type, status,
                                     geo_origin, language, reliability,
                                     fetch_interval_minutes, category,
                                     next_fetch_at, created_at, updated_at)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, NOW(), NOW(), NOW())
                """,
                source_id,
                json.dumps(data),
                src.name,
                src.url,
                src.source_type,
                src.initial_status,
                src.geo_origin,
                src.language,
                src.reliability,
                src.fetch_interval_minutes,
                src.category,
            )
            inserted += 1
            tier = "T2" if src in TIER_2_SOURCES else ("REG" if src in REGIONAL_SOURCES else "T1")
            print(f"  [{tier}] {src.name} ({src.source_type}, {src.category}, every {src.fetch_interval_minutes}m)")
        except Exception as e:
            errors += 1
            print(f"  ERROR: {src.name} — {e}")

    await pool.close()
    print(f"\nDone: {inserted} inserted, {skipped} skipped, {errors} errors")
    print(f"Total sources defined: {len(all_sources)}")


if __name__ == "__main__":
    dry_run = "--dry-run" in sys.argv
    if dry_run:
        print("DRY RUN — no changes will be made\n")
    asyncio.run(seed(dry_run=dry_run))

"""Source-specific normalizers for structured APIs.

Each normalizer takes a FetchedEntry (with raw_data) and returns enriched
Event fields that override the generic normalizer defaults. This keeps
domain knowledge about API response shapes isolated from the core pipeline.

Dispatched by source name prefix in normalize_entry().
"""

from __future__ import annotations

import logging
import math
import re
from datetime import datetime, timezone

from .fetcher import FetchedEntry

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Return type — fields that override generic normalize_entry() defaults
# ---------------------------------------------------------------------------

class SourceOverrides:
    """Fields to override in the Event after generic normalization."""

    __slots__ = (
        "title", "summary", "guid", "confidence", "category",
        "actors", "locations", "tags", "event_timestamp",
        "geo_coordinates", "source_url",
    )

    def __init__(self, **kwargs):
        for attr in self.__slots__:
            setattr(self, attr, kwargs.get(attr))

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__slots__ if getattr(self, k) is not None}


# ---------------------------------------------------------------------------
# GDELT DOC API
# ---------------------------------------------------------------------------

def normalize_gdelt(entry: FetchedEntry) -> SourceOverrides | None:
    """GDELT DOC API artlist response.

    Key fields: url, title, seendate, socialimage, domain, language,
    sourcecountry, tone (float, -100 to +100)
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    # Tone: GDELT average tone (-100 to +100). Map absolute value to confidence.
    # High absolute tone = strong signal. Neutral ≈ 0 → low confidence.
    tone = raw.get("tone")
    if tone is not None:
        try:
            tone_val = float(str(tone).split(",")[0])  # GDELT tone can be comma-separated
            overrides["confidence"] = min(0.95, 0.4 + abs(tone_val) / 20)
        except (ValueError, TypeError):
            pass

    # Domain as tag
    domain = raw.get("domain", "")
    if domain:
        tags = list(entry.tags)
        tags.append(f"via:{domain}")
        overrides["tags"] = tags

    # Source country → location
    country = raw.get("sourcecountry", "")
    if country:
        overrides["locations"] = [country]

    # seendate → timestamp (GDELT format: YYYYMMDDTHHmmSS)
    seendate = raw.get("seendate", "")
    if seendate and not entry.published:
        try:
            dt = datetime.strptime(seendate.replace("Z", ""), "%Y%m%dT%H%M%S")
            overrides["event_timestamp"] = dt.replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# USGS Earthquakes (GeoJSON)
# ---------------------------------------------------------------------------

def normalize_usgs_earthquake(entry: FetchedEntry) -> SourceOverrides | None:
    """USGS earthquake GeoJSON properties.

    Key fields: mag, place, time (epoch ms), tsunami, alert, felt, cdi, mmi,
    sig (significance 0-1000), title, type, url
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    mag = raw.get("mag")
    if mag is not None:
        try:
            mag_val = float(mag)
            # Construct informative title
            place = raw.get("place", "Unknown")
            overrides["title"] = f"M{mag_val:.1f} Earthquake — {place}"

            # Magnitude to confidence: M4.5=0.5, M6=0.7, M7+=0.9
            overrides["confidence"] = min(0.99, 0.3 + mag_val * 0.1)
        except (ValueError, TypeError):
            pass

    # Place → location
    place = raw.get("place", "")
    if place:
        # USGS format: "123km SSE of City, Country"
        overrides["locations"] = [place]

    # Epoch ms timestamp
    time_ms = raw.get("time")
    if time_ms:
        try:
            overrides["event_timestamp"] = datetime.fromtimestamp(
                int(time_ms) / 1000, tz=timezone.utc
            )
        except (ValueError, TypeError, OSError):
            pass

    # Tags from alert level and tsunami flag
    tags = list(entry.tags)
    alert = raw.get("alert", "")
    if alert:
        tags.append(f"alert:{alert}")
    if raw.get("tsunami", 0):
        tags.append("tsunami-potential")

    sig = raw.get("sig")
    if sig is not None:
        try:
            if int(sig) >= 600:
                tags.append("high-significance")
        except (ValueError, TypeError):
            pass

    if tags != list(entry.tags):
        overrides["tags"] = tags

    # GUID from USGS event code
    code = raw.get("code") or raw.get("ids", "").strip(",").split(",")[0]
    if code:
        overrides["guid"] = f"usgs:{code}"

    # URL
    url = raw.get("url", "")
    if url:
        overrides["source_url"] = url

    # Geometry → geo_coordinates (passed through from GeoJSON parser)
    geom = raw.get("_geometry", {})
    if geom.get("coordinates"):
        coords = geom["coordinates"]
        # GeoJSON is [lon, lat, depth]
        if len(coords) >= 2:
            overrides["geo_coordinates"] = [{
                "name": raw.get("place", "epicenter"),
                "lat": coords[1],
                "lon": coords[0],
            }]

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# NASA EONET (Earth Observatory Natural Event Tracker)
# ---------------------------------------------------------------------------

def normalize_eonet(entry: FetchedEntry) -> SourceOverrides | None:
    """NASA EONET event response.

    Key fields: id, title, categories[{id, title}], sources[{id, url}],
    geometry[{date, type, coordinates}]
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    # EONET categories → event category mapping
    cats = raw.get("categories", [])
    if cats:
        eonet_cat = cats[0].get("title", "").lower() if isinstance(cats[0], dict) else ""
        cat_map = {
            "wildfires": "disaster",
            "severe storms": "disaster",
            "volcanoes": "disaster",
            "floods": "disaster",
            "earthquakes": "disaster",
            "drought": "environment",
            "sea and lake ice": "environment",
            "temperature extremes": "environment",
            "landslides": "disaster",
            "snow": "disaster",
        }
        mapped = cat_map.get(eonet_cat, "environment")
        overrides["category"] = mapped

    # Tags from all categories
    tags = list(entry.tags)
    for cat in cats:
        if isinstance(cat, dict) and cat.get("title"):
            tags.append(cat["title"].lower().replace(" ", "-"))
    if tags != list(entry.tags):
        overrides["tags"] = tags

    # Most recent geometry → coordinates and timestamp
    geometry = raw.get("geometry", [])
    if geometry and isinstance(geometry, list):
        latest = geometry[-1] if isinstance(geometry[-1], dict) else {}
        coords = latest.get("coordinates", [])
        if coords and isinstance(coords[0], (int, float)):
            overrides["geo_coordinates"] = [{
                "name": raw.get("title", "event"),
                "lat": coords[1],
                "lon": coords[0],
            }]
        # Date from most recent geometry
        geom_date = latest.get("date", "")
        if geom_date:
            overrides["event_timestamp"] = _parse_iso(geom_date)

    # EONET id as GUID
    eonet_id = raw.get("id", "")
    if eonet_id:
        overrides["guid"] = f"eonet:{eonet_id}"

    # Sources → source URL
    sources = raw.get("sources", [])
    if sources and isinstance(sources[0], dict):
        overrides["source_url"] = sources[0].get("url", "")

    # Confidence high — NASA verified events
    overrides["confidence"] = 0.85

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# CISA Known Exploited Vulnerabilities
# ---------------------------------------------------------------------------

def normalize_cisa_kev(entry: FetchedEntry) -> SourceOverrides | None:
    """CISA KEV catalog entry.

    Key fields: cveID, vendorProject, product, vulnerabilityName,
    dateAdded, shortDescription, requiredAction, dueDate,
    knownRansomwareCampaignUse
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    cve = raw.get("cveID", "")
    vendor = raw.get("vendorProject", "")
    product = raw.get("product", "")
    vuln_name = raw.get("vulnerabilityName", "")

    if cve:
        overrides["guid"] = cve
        overrides["title"] = f"{cve}: {vendor} {product} — {vuln_name}" if vuln_name else f"{cve}: {vendor} {product}"

    overrides["summary"] = raw.get("shortDescription", "")

    # Date added
    date_added = raw.get("dateAdded", "")
    if date_added:
        overrides["event_timestamp"] = _parse_iso(date_added)

    # Tags
    tags = ["cisa-kev", "vulnerability"]
    if raw.get("knownRansomwareCampaignUse", "").lower() == "known":
        tags.append("ransomware")
    if vendor:
        tags.append(vendor.lower())
    overrides["tags"] = tags

    # Actors — vendor as impacted entity
    if vendor:
        overrides["actors"] = [vendor]

    # High confidence — CISA confirmed exploitation
    overrides["confidence"] = 0.95

    return SourceOverrides(**overrides)


# ---------------------------------------------------------------------------
# NWS / NOAA Weather Alerts (GeoJSON features)
# ---------------------------------------------------------------------------

def normalize_nws_alert(entry: FetchedEntry) -> SourceOverrides | None:
    """NWS/NOAA alert GeoJSON properties.

    Key fields: event, severity, certainty, urgency, areaDesc, headline,
    description, onset, expires, senderName
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    event = raw.get("event", "")
    severity = raw.get("severity", "")
    area = raw.get("areaDesc", "")

    if event:
        area_short = area.split(";")[0].strip() if area else ""
        if area_short:
            overrides["title"] = f"{severity} {event} — {area_short}" if severity else f"{event} — {area_short}"
        else:
            overrides["title"] = f"{severity} {event}" if severity else event

    if area:
        # NWS areaDesc is semicolon-separated list of areas
        overrides["locations"] = [a.strip() for a in area.split(";") if a.strip()]

    # Severity → confidence
    sev_map = {"Extreme": 0.95, "Severe": 0.85, "Moderate": 0.65, "Minor": 0.45}
    overrides["confidence"] = sev_map.get(severity, 0.5)

    # Headline as summary
    headline = raw.get("headline", "")
    if headline:
        overrides["summary"] = headline

    # Onset timestamp
    onset = raw.get("onset") or raw.get("effective", "")
    if onset:
        overrides["event_timestamp"] = _parse_iso(onset)

    # GUID from NWS ID
    nws_id = raw.get("id", "")
    if nws_id:
        overrides["guid"] = nws_id
        overrides["source_url"] = f"https://api.weather.gov/alerts/{nws_id}"

    # Tags
    tags = ["nws"]
    certainty = raw.get("certainty", "")
    urgency = raw.get("urgency", "")
    if certainty:
        tags.append(f"certainty:{certainty.lower()}")
    if urgency:
        tags.append(f"urgency:{urgency.lower()}")
    if event:
        tags.append(event.lower().replace(" ", "-"))
    overrides["tags"] = tags

    return SourceOverrides(**overrides)


# ---------------------------------------------------------------------------
# ReliefWeb Reports API
# ---------------------------------------------------------------------------

def normalize_reliefweb(entry: FetchedEntry) -> SourceOverrides | None:
    """ReliefWeb API report.

    Key fields: id, fields.title, fields.body, fields.date.original,
    fields.country[{name, iso3}], fields.source[{name}],
    fields.disaster_type[{name}], fields.primary_country{name}
    """
    raw = entry.raw_data
    if not raw:
        return None

    # ReliefWeb wraps everything in a `fields` dict
    fields = raw.get("fields", raw)
    overrides = {}

    # Countries → locations
    countries = fields.get("country", [])
    if countries:
        overrides["locations"] = [
            c["name"] if isinstance(c, dict) else str(c)
            for c in countries
        ]

    # Primary country
    primary = fields.get("primary_country", {})
    if isinstance(primary, dict) and primary.get("name"):
        locs = overrides.get("locations", [])
        if primary["name"] not in locs:
            locs.insert(0, primary["name"])
            overrides["locations"] = locs

    # Disaster types → tags
    dtypes = fields.get("disaster_type", [])
    if dtypes:
        tags = list(entry.tags)
        for dt in dtypes:
            name = dt["name"].lower() if isinstance(dt, dict) else str(dt).lower()
            tags.append(name.replace(" ", "-"))
        overrides["tags"] = tags

    # Sources → actors
    sources = fields.get("source", [])
    if sources:
        overrides["actors"] = [
            s["name"] if isinstance(s, dict) else str(s)
            for s in sources[:5]
        ]

    # Date
    date_info = fields.get("date", {})
    if isinstance(date_info, dict) and date_info.get("original"):
        overrides["event_timestamp"] = _parse_iso(date_info["original"])

    # GUID
    rw_id = raw.get("id")
    if rw_id:
        overrides["guid"] = f"reliefweb:{rw_id}"

    overrides["confidence"] = 0.8

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# IFRC GO API
# ---------------------------------------------------------------------------

def normalize_ifrc(entry: FetchedEntry) -> SourceOverrides | None:
    """IFRC GO emergency event.

    Key fields: id, name, dtype{id,name}, countries[{name,iso3}],
    disaster_start_date, num_affected, summary
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    # Disaster type → category
    dtype = raw.get("dtype", {})
    if isinstance(dtype, dict):
        dtype_name = dtype.get("name", "").lower()
        cat_map = {
            "earthquake": "disaster", "flood": "disaster",
            "cyclone": "disaster", "epidemic": "health",
            "fire": "disaster", "drought": "environment",
            "volcano": "disaster", "storm surge": "disaster",
            "cold wave": "disaster", "heat wave": "disaster",
        }
        for key, cat in cat_map.items():
            if key in dtype_name:
                overrides["category"] = cat
                break

    # Countries → locations
    countries = raw.get("countries", [])
    if countries:
        overrides["locations"] = [
            c["name"] if isinstance(c, dict) else str(c)
            for c in countries
        ]

    # Number affected → enriches title
    num_affected = raw.get("num_affected")
    name = raw.get("name", entry.title)
    if num_affected and name:
        overrides["title"] = f"{name} ({num_affected:,} affected)"

    # Disaster start date
    start_date = raw.get("disaster_start_date", "")
    if start_date:
        overrides["event_timestamp"] = _parse_iso(start_date)

    # GUID
    ifrc_id = raw.get("id")
    if ifrc_id:
        overrides["guid"] = f"ifrc:{ifrc_id}"

    overrides["confidence"] = 0.8

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# UCDP Georeferenced Events
# ---------------------------------------------------------------------------

def normalize_ucdp(entry: FetchedEntry) -> SourceOverrides | None:
    """UCDP GED event.

    Key fields: id, country, date_start, date_end, best (best estimate of deaths),
    type_of_violence (1=state, 2=non-state, 3=one-sided), side_a, side_b,
    where_coordinates (lat), where_coordinates (lon), source_article
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    # Violence type → tags and title enhancement
    vtype = raw.get("type_of_violence")
    vtype_map = {1: "state-based", 2: "non-state", 3: "one-sided"}
    violence_label = vtype_map.get(vtype, "armed")

    country = raw.get("country", "")
    deaths = raw.get("best", 0)
    side_a = raw.get("side_a", "")
    side_b = raw.get("side_b", "")

    if country:
        overrides["locations"] = [country]
        title_parts = [f"Armed conflict in {country}"]
        if deaths:
            title_parts.append(f"({deaths} killed)")
        if side_a:
            title_parts.append(f"— {side_a}")
            if side_b:
                title_parts.append(f"vs {side_b}")
        overrides["title"] = " ".join(title_parts)

    # Actors
    actors = []
    if side_a:
        actors.append(side_a)
    if side_b:
        actors.append(side_b)
    if actors:
        overrides["actors"] = actors

    # Deaths → confidence (more deaths = more significant)
    if deaths:
        try:
            d = int(deaths)
            overrides["confidence"] = min(0.99, 0.5 + math.log10(max(d, 1)) * 0.15)
        except (ValueError, TypeError):
            pass

    # Timestamp
    date_start = raw.get("date_start", "")
    if date_start:
        overrides["event_timestamp"] = _parse_iso(date_start)

    # Tags
    tags = ["ucdp", f"violence:{violence_label}"]
    overrides["tags"] = tags

    # GUID
    ucdp_id = raw.get("id")
    if ucdp_id:
        overrides["guid"] = f"ucdp:{ucdp_id}"

    # Coordinates
    lat = raw.get("latitude") or raw.get("where_coordinates")
    lon = raw.get("longitude")
    if lat and lon:
        try:
            overrides["geo_coordinates"] = [{
                "name": country or "conflict zone",
                "lat": float(lat),
                "lon": float(lon),
            }]
        except (ValueError, TypeError):
            pass

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# ACLED (Tier 2 — requires API key)
# ---------------------------------------------------------------------------

def normalize_acled(entry: FetchedEntry) -> SourceOverrides | None:
    """ACLED event.

    Key fields: event_id_cnty, event_date, event_type, sub_event_type,
    actor1, actor2, country, admin1, admin2, fatalities, latitude, longitude,
    notes, source
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    event_type = raw.get("event_type", "")
    sub_type = raw.get("sub_event_type", "")
    country = raw.get("country", "")
    admin1 = raw.get("admin1", "")
    fatalities = raw.get("fatalities", 0)
    actor1 = raw.get("actor1", "")
    actor2 = raw.get("actor2", "")

    # Event type → category
    type_map = {
        "Battles": "conflict",
        "Violence against civilians": "conflict",
        "Explosions/Remote violence": "conflict",
        "Riots": "social",
        "Protests": "social",
        "Strategic developments": "political",
    }
    if event_type:
        overrides["category"] = type_map.get(event_type, "conflict")

    # Build title
    loc = f"{admin1}, {country}" if admin1 else country
    if event_type:
        title = f"{event_type}: {sub_type}" if sub_type else event_type
        if loc:
            title = f"{title} in {loc}"
        if fatalities:
            title = f"{title} ({fatalities} killed)"
        overrides["title"] = title

    # Locations
    locations = []
    if country:
        locations.append(country)
    if admin1:
        locations.append(admin1)
    if locations:
        overrides["locations"] = locations

    # Actors
    actors = []
    if actor1:
        actors.append(actor1)
    if actor2:
        actors.append(actor2)
    if actors:
        overrides["actors"] = actors

    # Fatalities → confidence
    if fatalities:
        try:
            f = int(fatalities)
            overrides["confidence"] = min(0.99, 0.5 + math.log10(max(f, 1)) * 0.15)
        except (ValueError, TypeError):
            pass
    else:
        overrides["confidence"] = 0.6

    # Date
    date_str = raw.get("event_date", "")
    if date_str:
        overrides["event_timestamp"] = _parse_iso(date_str)

    # Tags
    tags = ["acled"]
    if sub_type:
        tags.append(sub_type.lower().replace(" ", "-").replace("/", "-"))
    overrides["tags"] = tags

    # GUID
    acled_id = raw.get("event_id_cnty") or raw.get("data_id")
    if acled_id:
        overrides["guid"] = f"acled:{acled_id}"

    # Coordinates
    lat = raw.get("latitude")
    lon = raw.get("longitude")
    if lat and lon:
        try:
            overrides["geo_coordinates"] = [{
                "name": loc or "event",
                "lat": float(lat),
                "lon": float(lon),
            }]
        except (ValueError, TypeError):
            pass

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# NASA FIRMS (Tier 2 — requires API key)
# ---------------------------------------------------------------------------

def normalize_firms(entry: FetchedEntry) -> SourceOverrides | None:
    """NASA FIRMS fire hotspot.

    Key fields: latitude, longitude, brightness, scan, track, acq_date,
    acq_time, satellite, confidence, version, frp (fire radiative power)
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    lat = raw.get("latitude")
    lon = raw.get("longitude")
    brightness = raw.get("brightness") or raw.get("bright_ti4")
    frp = raw.get("frp", 0)
    confidence = raw.get("confidence", "")
    satellite = raw.get("satellite", "")

    # Build title from location
    if lat and lon:
        overrides["title"] = f"Fire hotspot at {lat:.2f}°, {lon:.2f}°"
        if frp:
            overrides["title"] += f" (FRP: {frp} MW)"

    # Confidence from FIRMS confidence field
    # FIRMS confidence: "nominal", "low", "high" or numeric 0-100
    if confidence:
        conf_str = str(confidence).lower()
        if conf_str in ("high", "h"):
            overrides["confidence"] = 0.9
        elif conf_str in ("nominal", "n"):
            overrides["confidence"] = 0.7
        elif conf_str in ("low", "l"):
            overrides["confidence"] = 0.4
        else:
            try:
                overrides["confidence"] = min(0.99, int(confidence) / 100)
            except ValueError:
                pass

    # Timestamp
    acq_date = raw.get("acq_date", "")
    acq_time = raw.get("acq_time", "")
    if acq_date:
        ts_str = f"{acq_date}T{acq_time}:00Z" if acq_time else f"{acq_date}T00:00:00Z"
        overrides["event_timestamp"] = _parse_iso(ts_str)

    # Tags
    tags = ["firms", "fire"]
    if satellite:
        tags.append(f"sat:{satellite.lower()}")
    overrides["tags"] = tags

    # Coordinates
    if lat and lon:
        try:
            overrides["geo_coordinates"] = [{
                "name": "fire hotspot",
                "lat": float(lat),
                "lon": float(lon),
            }]
        except (ValueError, TypeError):
            pass

    overrides["category"] = "disaster"

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# NVD (National Vulnerability Database, Tier 2)
# ---------------------------------------------------------------------------

def normalize_nvd(entry: FetchedEntry) -> SourceOverrides | None:
    """NVD CVE entry.

    Key fields: cve{id, descriptions[{lang, value}], metrics{cvssMetricV31[{cvssData{baseScore}}]},
    published, lastModified, vulnStatus}
    """
    raw = entry.raw_data
    if not raw:
        return None

    overrides = {}

    cve_data = raw.get("cve", raw)  # Might be nested or flat
    cve_id = cve_data.get("id", "")

    if cve_id:
        overrides["guid"] = cve_id

    # Description
    descriptions = cve_data.get("descriptions", [])
    for desc in descriptions:
        if isinstance(desc, dict) and desc.get("lang", "en") == "en":
            overrides["summary"] = desc.get("value", "")
            break

    # CVSS score → confidence
    metrics = cve_data.get("metrics", {})
    # Try v3.1 first, then v3.0, then v2
    for metric_key in ("cvssMetricV31", "cvssMetricV30", "cvssMetricV2"):
        metric_list = metrics.get(metric_key, [])
        if metric_list and isinstance(metric_list[0], dict):
            cvss_data = metric_list[0].get("cvssData", {})
            score = cvss_data.get("baseScore")
            if score is not None:
                # CVSS 0-10 → confidence 0.3-0.99
                overrides["confidence"] = min(0.99, 0.3 + float(score) * 0.07)
                break

    # Title from CVE ID + first part of description
    summary = overrides.get("summary", "")
    if cve_id:
        short = summary[:80] + "..." if len(summary) > 80 else summary
        overrides["title"] = f"{cve_id}: {short}" if short else cve_id

    # Published date
    published = cve_data.get("published", "")
    if published:
        overrides["event_timestamp"] = _parse_iso(published)

    # Tags
    tags = ["nvd", "vulnerability"]
    status = cve_data.get("vulnStatus", "")
    if status:
        tags.append(f"status:{status.lower()}")
    overrides["tags"] = tags

    return SourceOverrides(**overrides) if overrides else None


# ---------------------------------------------------------------------------
# Frankfurter Exchange Rates (special: single entry from rates object)
# ---------------------------------------------------------------------------

def normalize_frankfurter(entry: FetchedEntry) -> SourceOverrides | None:
    """Frankfurter API returns a single object with rates, not a list.

    raw_data = {"base": "EUR", "date": "2026-03-13", "rates": {"USD": 1.08, ...}}
    """
    raw = entry.raw_data
    if not raw or "rates" not in raw:
        return None

    overrides = {}
    base = raw.get("base", "EUR")
    date = raw.get("date", "")
    rates = raw.get("rates", {})

    # Key rates for title
    key_currencies = ["USD", "GBP", "JPY", "CNY", "CHF"]
    rate_strs = []
    for curr in key_currencies:
        if curr in rates:
            rate_strs.append(f"{curr}={rates[curr]}")

    overrides["title"] = f"Exchange rates ({base}): {', '.join(rate_strs)}"
    overrides["summary"] = f"{len(rates)} currency rates from ECB. Base: {base}, Date: {date}"

    if date:
        overrides["event_timestamp"] = _parse_iso(date)

    overrides["confidence"] = 0.95
    overrides["tags"] = ["forex", "ecb", "exchange-rates"]
    overrides["guid"] = f"frankfurter:{base}:{date}"

    return SourceOverrides(**overrides)


# ===========================================================================
# Dispatcher — maps source name patterns to normalizers
# ===========================================================================

_SOURCE_NORMALIZERS: list[tuple[re.Pattern, callable]] = [
    (re.compile(r"GDELT", re.I), normalize_gdelt),
    (re.compile(r"USGS.*(Earthquake|Seismic|Significant)", re.I), normalize_usgs_earthquake),
    (re.compile(r"NASA EONET", re.I), normalize_eonet),
    (re.compile(r"CISA.*Vulnerabilit", re.I), normalize_cisa_kev),
    (re.compile(r"NWS|NOAA|Storm Events", re.I), normalize_nws_alert),
    (re.compile(r"ReliefWeb", re.I), normalize_reliefweb),
    (re.compile(r"IFRC", re.I), normalize_ifrc),
    (re.compile(r"UCDP", re.I), normalize_ucdp),
    (re.compile(r"ACLED", re.I), normalize_acled),
    (re.compile(r"FIRMS|NASA FIRMS", re.I), normalize_firms),
    (re.compile(r"NVD|National Vulnerability", re.I), normalize_nvd),
    (re.compile(r"Frankfurter", re.I), normalize_frankfurter),
]


def get_source_normalizer(source_name: str):
    """Return a source-specific normalizer function if one exists, else None."""
    for pattern, func in _SOURCE_NORMALIZERS:
        if pattern.search(source_name):
            return func
    return None


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _parse_iso(raw: str) -> datetime | None:
    """Quick ISO-ish timestamp parser."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S.%fZ",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue
    return None

"""Signal normalizer — converts FetchedEntry to Signal schema.

Handles category inference, timestamp parsing, and source-specific mappings.
"""

from __future__ import annotations

import calendar
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from uuid import UUID

from legba.shared.schemas.signals import Signal, SignalCategory, create_signal

from .fetcher import FetchedEntry
from .source_normalizers import get_source_normalizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# spaCy NER — lazy-loaded for entity extraction
# ---------------------------------------------------------------------------

_nlp = None


def _get_nlp():
    """Lazy-load spaCy model on first use."""
    global _nlp
    if _nlp is None:
        try:
            import spacy
            _nlp = spacy.load("en_core_web_sm")
            logger.info("spaCy en_core_web_sm loaded for NER")
        except Exception as e:
            logger.warning("spaCy unavailable, NER extraction disabled: %s", e)
            _nlp = False  # Sentinel: tried and failed, don't retry
    return _nlp if _nlp is not False else None


def extract_entities_ner(text: str) -> tuple[list[str], list[str]]:
    """Extract person/org names and locations from text using spaCy NER.

    Returns (actors, locations). Both lists are capped at 10 entries.
    Only call when source-level extraction yielded no actors/locations.
    """
    if not text or len(text) < 20:
        return [], []

    nlp = _get_nlp()
    if nlp is None:
        return [], []

    doc = nlp(text[:1000])  # Cap input length for performance

    actors: list[str] = []
    locations: list[str] = []
    for ent in doc.ents:
        if ent.label_ in ("PERSON", "ORG", "NORP"):
            name = ent.text.strip()
            if len(name) > 2 and name not in actors:
                actors.append(name)
        elif ent.label_ in ("GPE", "LOC", "FAC"):
            name = ent.text.strip()
            if len(name) > 2 and name not in locations:
                locations.append(name)

    return actors[:10], locations[:10]


# ---------------------------------------------------------------------------
# Category inference from title/summary keywords
# ---------------------------------------------------------------------------

_CATEGORY_RULES: list[tuple[SignalCategory, re.Pattern]] = [
    (SignalCategory.CONFLICT, re.compile(
        r"\b(attack|strike|bomb|missile|war|clash|fighting|military|troops|"
        r"airstrike|offensive|invasion|killed|casualties|ceasefire|insurgent|"
        r"militia|shelling|drone strike|combat|armed|hostil|weapon|"
        r"threat|threaten|retaliat|escalat|siege|blockade|ambush|raid|assault|"
        r"sniper|artillery|naval|convoy|deploy|incursion|rebel|guerrilla|"
        r"separatist|extremist|terror)\b", re.I)),
    (SignalCategory.DISASTER, re.compile(
        r"\b(earthquake|tsunami|flood|hurricane|typhoon|cyclone|tornado|"
        r"wildfire|eruption|volcano|landslide|avalanche|drought|famine|"
        r"magnitude|seismic|storm surge|devastat|catastroph|"
        r"warning|alert|blizzard|hail|ice storm|heat wave|fire weather|"
        r"freezing|squall|dust storm|surge|mudslide|sinkhole)\b", re.I)),
    (SignalCategory.HEALTH, re.compile(
        r"\b(outbreak|pandemic|epidemic|virus|disease|WHO|vaccine|"
        r"infection|cases|deaths|hospital|health crisis|malaria|cholera|"
        r"ebola|bird flu|avian|mpox|covid|tuberculosis|measles)\b", re.I)),
    (SignalCategory.ECONOMIC, re.compile(
        r"\b(GDP|inflation|trade|tariff|sanction|economy|market|stock|"
        r"recession|currency|debt|IMF|World Bank|commodity|oil price|"
        r"interest rate|fiscal|monetary|deficit|surplus|unemployment|"
        r"price|cost|spend|budget|invest|bank|fund|export|import|"
        r"supply chain|shortage|subsid|manufactur|industri|labor|wage|"
        r"employ|growth|contraction|downturn|boom|rally|crash|"
        r"bitcoin|crypto|fintech)\b", re.I)),
    (SignalCategory.POLITICAL, re.compile(
        r"\b(election|president|parliament|legislation|treaty|diplomat|"
        r"summit|minister|government|opposition|protest|demonstrat|"
        r"referendum|coup|impeach|policy|bilateral|UN|NATO|EU|"
        r"trump|biden|putin|modi|xi jinping|macron|leader|prime minister|"
        r"chancellor|king|queen|prince|crown|regime|sanction|condemn|vow|"
        r"pledge|announce|warn|threaten|statement|response|tension|crisis|"
        r"ally|alliance|negotiate|agreement|deal|talks|meeting|"
        r"unilateral|ceasefire|truce)\b", re.I)),
    (SignalCategory.TECHNOLOGY, re.compile(
        r"\b(cyber|hack|breach|vulnerability|CVE|malware|ransomware|"
        r"zero-day|APT|exploit|CISA|infrastructure attack|data leak)\b", re.I)),
    (SignalCategory.ENVIRONMENT, re.compile(
        r"\b(climate|emission|deforestation|pollution|carbon|renewable|"
        r"biodiversity|conservation|environmental|glacier|ice sheet|"
        r"sea level|ozone|species extinct)\b", re.I)),
    (SignalCategory.SOCIAL, re.compile(
        r"\b(human rights|refugee|migration|displacement|humanitarian|"
        r"civil liberties|press freedom|censorship|minority|indigenous|"
        r"protest|rally|march|unrest|riot|demonstration|strike action|"
        r"labor dispute|discrimination|equality|justice|poverty|inequality|"
        r"civil society|ngo|aid|assistance)\b", re.I)),
]


def infer_category(title: str, summary: str = "", source_category: str = "") -> SignalCategory:
    """Infer signal category from text content.

    Priority: source-level category > keyword matching > OTHER
    """
    # If source has a declared category, use it
    if source_category:
        try:
            return SignalCategory(source_category.lower())
        except ValueError:
            pass

    text = f"{title} {summary}"
    best_category = SignalCategory.OTHER
    best_score = 0

    for category, pattern in _CATEGORY_RULES:
        matches = pattern.findall(text)
        if len(matches) > best_score:
            best_score = len(matches)
            best_category = category

    return best_category


# ---------------------------------------------------------------------------
# Timestamp parsing
# ---------------------------------------------------------------------------

_TIMESTAMP_FORMATS = [
    "%Y-%m-%dT%H:%M:%S%z",
    "%Y-%m-%dT%H:%M:%SZ",
    "%Y-%m-%dT%H:%M:%S.%f%z",
    "%Y-%m-%dT%H:%M:%S.%fZ",
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%d",
    "%a, %d %b %Y %H:%M:%S %z",
    "%a, %d %b %Y %H:%M:%S %Z",
    "%d %b %Y %H:%M:%S %z",
    "%B %d, %Y",
    "%d %B %Y",
    "%Y%m%dT%H%M%S",          # GDELT seendate format
    "%Y/%m/%d %H:%M:%S",
    "%m/%d/%Y %H:%M:%S",
    "%m/%d/%Y",
    "%d-%m-%Y",
]


def parse_timestamp(raw: str) -> datetime | None:
    """Best-effort timestamp parsing across common formats."""
    if not raw:
        return None

    raw = raw.strip()

    for fmt in _TIMESTAMP_FORMATS:
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except ValueError:
            continue

    # Last resort: dateutil
    try:
        from dateutil.parser import parse as dateutil_parse
        dt = dateutil_parse(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        pass

    return None


def _struct_time_to_datetime(st) -> datetime | None:
    """Convert a feedparser time.struct_time to a timezone-aware datetime."""
    if st is None:
        return None
    try:
        ts = calendar.timegm(st)
        return datetime.fromtimestamp(ts, tz=timezone.utc)
    except (TypeError, ValueError, OverflowError, OSError):
        return None


def _sanity_check(dt: datetime | None) -> datetime | None:
    """Discard timestamps that are too far in the future or past."""
    if dt is None:
        return None
    now = datetime.now(timezone.utc)
    # Ensure timezone-aware comparison
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    if dt > now + timedelta(days=1):
        return None  # Future date (>1 day ahead)
    if dt < now - timedelta(days=365):
        return None  # More than 1 year in the past
    return dt


# RSS date fields to try, in priority order (feedparser struct_time fields)
_RSS_STRUCT_FIELDS = ("published_parsed", "updated_parsed", "created_parsed")

# API/JSON raw_data date fields to try, in priority order
_API_DATE_FIELDS = (
    "dateTime", "datetime", "date_time",       # Event Registry, etc.
    "seendate", "seen_date",                    # GDELT
    "event_date", "eventDate",                  # Various APIs
    "published", "publishedAt", "published_at", # Common
    "pubDate",
    "timestamp", "time",
    "date",
    "created_at", "createdAt",
    "updated_at", "updatedAt",
    "acq_date",                                 # NASA FIRMS
    "date_start",                               # UCDP
    "disaster_start_date",                      # IFRC
    "onset", "effective",                       # NWS
    "dateAdded",                                # CISA
)


def _extract_entry_timestamp(entry: FetchedEntry) -> datetime | None:
    """Aggressively extract the best event timestamp from a FetchedEntry.

    Tries, in order:
    1. feedparser struct_time fields from raw_data (most reliable for RSS)
    2. entry.published string field
    3. Additional string date fields from raw_data
    4. Epoch-millisecond 'time' field (USGS style)

    All results are sanity-checked before returning.
    """
    raw = entry.raw_data or {}

    # 1. feedparser struct_time fields (pre-parsed, most reliable for RSS)
    for field in _RSS_STRUCT_FIELDS:
        st = raw.get(field)
        if st is not None:
            dt = _sanity_check(_struct_time_to_datetime(st))
            if dt is not None:
                return dt

    # 2. The entry.published string (already extracted by fetcher)
    if entry.published:
        dt = _sanity_check(parse_timestamp(entry.published))
        if dt is not None:
            return dt

    # 3. Try additional string date fields from raw_data
    for field in _API_DATE_FIELDS:
        val = raw.get(field)
        if val is None:
            continue
        if isinstance(val, str) and val.strip():
            dt = _sanity_check(parse_timestamp(val.strip()))
            if dt is not None:
                return dt
        elif isinstance(val, (int, float)):
            # Epoch seconds or milliseconds
            try:
                ts = float(val)
                # If > 1e12, assume milliseconds
                if ts > 1e12:
                    ts = ts / 1000
                dt = _sanity_check(
                    datetime.fromtimestamp(ts, tz=timezone.utc)
                )
                if dt is not None:
                    return dt
            except (ValueError, TypeError, OverflowError, OSError):
                continue

    # 4. Check nested 'date' dict (ReliefWeb style: {"original": "...", "created": "..."})
    date_obj = raw.get("date")
    if isinstance(date_obj, dict):
        for sub_key in ("original", "created", "changed"):
            sub_val = date_obj.get(sub_key, "")
            if sub_val and isinstance(sub_val, str):
                dt = _sanity_check(parse_timestamp(sub_val))
                if dt is not None:
                    return dt

    return None


# ---------------------------------------------------------------------------
# Core normalizer
# ---------------------------------------------------------------------------

def normalize_entry(
    entry: FetchedEntry,
    *,
    source_id: UUID | None = None,
    source_name: str = "",
    source_category: str = "",
    source_language: str = "en",
) -> Signal:
    """Convert a FetchedEntry into a Signal object.

    If a source-specific normalizer exists (matched by source_name), it runs
    after generic normalization and overrides fields it knows about.

    Args:
        entry: Raw fetched entry from any source type
        source_id: UUID of the registered source
        source_name: Registered source name (for custom normalizer dispatch)
        source_category: Source-level category hint
        source_language: Source-level language

    Returns:
        Signal ready for dedup check and storage
    """
    title = (entry.title or "").strip()
    summary = (entry.summary or "").strip()

    # Skip entries with no usable title
    if not title and not summary:
        title = "(untitled)"

    category = infer_category(title, summary, source_category)
    timestamp = _extract_entry_timestamp(entry)

    # Extract actors/locations from tags if available
    actors: list[str] = list(entry.authors) if entry.authors else []
    locations: list[str] = []
    tags: list[str] = [t for t in entry.tags if t]

    # NER fallback: if no actors/locations from structured fields, try spaCy
    if not actors and not locations:
        ner_text = f"{title} {summary}" if summary else title
        ner_actors, ner_locations = extract_entities_ner(ner_text)
        if ner_actors:
            actors = ner_actors
        if ner_locations:
            locations = ner_locations

    # Cognitive architecture: confidence is no longer hardcoded at 0.5.
    # The service layer computes composite confidence using the hybrid gatekeeper
    # formula (source_reliability * classification_confidence * modifier).
    # We use None-coalesced 0.5 here as a safe default that gets overwritten.
    sig = create_signal(
        title=title,
        summary=summary,
        event_timestamp=timestamp,
        source_id=source_id,
        source_url=entry.link or "",
        category=category,
        confidence=0.3,  # low default — overwritten by service.py composite scoring
        actors=actors,
        locations=locations,
        tags=tags,
        language=source_language,
        guid=entry.guid or "",
    )

    # Apply source-specific overrides if available
    if source_name:
        normalizer = get_source_normalizer(source_name)
        if normalizer:
            try:
                overrides = normalizer(entry)
                if overrides:
                    for key, value in overrides.as_dict().items():
                        if key == "category":
                            # Convert string back to enum
                            try:
                                value = SignalCategory(value)
                            except ValueError:
                                continue
                        setattr(sig, key, value)
            except Exception as e:
                logger.warning("Source normalizer failed for %s: %s", source_name, e)

    # Signal provenance: start tracking processing lineage via prov: tag transport
    prov = {
        "raw_source": source_name or (entry.source_type if hasattr(entry, 'source_type') else ""),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "normalized_by": "rss_normalizer",
    }
    sig.tags = list(sig.tags) + [f"prov:{json.dumps(prov)}"]

    return sig

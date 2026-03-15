"""Event normalizer — converts FetchedEntry to Event schema.

Handles category inference, timestamp parsing, and source-specific mappings.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from uuid import UUID

from legba.shared.schemas.events import Event, EventCategory, create_event

from .fetcher import FetchedEntry
from .source_normalizers import get_source_normalizer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Category inference from title/summary keywords
# ---------------------------------------------------------------------------

_CATEGORY_RULES: list[tuple[EventCategory, re.Pattern]] = [
    (EventCategory.CONFLICT, re.compile(
        r"\b(attack|strike|bomb|missile|war|clash|fighting|military|troops|"
        r"airstrike|offensive|invasion|killed|casualties|ceasefire|insurgent|"
        r"militia|shelling|drone strike|combat|armed|hostil|weapon|"
        r"threat|threaten|retaliat|escalat|siege|blockade|ambush|raid|assault|"
        r"sniper|artillery|naval|convoy|deploy|incursion|rebel|guerrilla|"
        r"separatist|extremist|terror)\b", re.I)),
    (EventCategory.DISASTER, re.compile(
        r"\b(earthquake|tsunami|flood|hurricane|typhoon|cyclone|tornado|"
        r"wildfire|eruption|volcano|landslide|avalanche|drought|famine|"
        r"magnitude|seismic|storm surge|devastat|catastroph|"
        r"warning|alert|blizzard|hail|ice storm|heat wave|fire weather|"
        r"freezing|squall|dust storm|surge|mudslide|sinkhole)\b", re.I)),
    (EventCategory.HEALTH, re.compile(
        r"\b(outbreak|pandemic|epidemic|virus|disease|WHO|vaccine|"
        r"infection|cases|deaths|hospital|health crisis|malaria|cholera|"
        r"ebola|bird flu|avian|mpox|covid|tuberculosis|measles)\b", re.I)),
    (EventCategory.ECONOMIC, re.compile(
        r"\b(GDP|inflation|trade|tariff|sanction|economy|market|stock|"
        r"recession|currency|debt|IMF|World Bank|commodity|oil price|"
        r"interest rate|fiscal|monetary|deficit|surplus|unemployment|"
        r"price|cost|spend|budget|invest|bank|fund|export|import|"
        r"supply chain|shortage|subsid|manufactur|industri|labor|wage|"
        r"employ|growth|contraction|downturn|boom|rally|crash|"
        r"bitcoin|crypto|fintech)\b", re.I)),
    (EventCategory.POLITICAL, re.compile(
        r"\b(election|president|parliament|legislation|treaty|diplomat|"
        r"summit|minister|government|opposition|protest|demonstrat|"
        r"referendum|coup|impeach|policy|bilateral|UN|NATO|EU|"
        r"trump|biden|putin|modi|xi jinping|macron|leader|prime minister|"
        r"chancellor|king|queen|prince|crown|regime|sanction|condemn|vow|"
        r"pledge|announce|warn|threaten|statement|response|tension|crisis|"
        r"ally|alliance|negotiate|agreement|deal|talks|meeting|"
        r"unilateral|ceasefire|truce)\b", re.I)),
    (EventCategory.TECHNOLOGY, re.compile(
        r"\b(cyber|hack|breach|vulnerability|CVE|malware|ransomware|"
        r"zero-day|APT|exploit|CISA|infrastructure attack|data leak)\b", re.I)),
    (EventCategory.ENVIRONMENT, re.compile(
        r"\b(climate|emission|deforestation|pollution|carbon|renewable|"
        r"biodiversity|conservation|environmental|glacier|ice sheet|"
        r"sea level|ozone|species extinct)\b", re.I)),
    (EventCategory.SOCIAL, re.compile(
        r"\b(human rights|refugee|migration|displacement|humanitarian|"
        r"civil liberties|press freedom|censorship|minority|indigenous|"
        r"protest|rally|march|unrest|riot|demonstration|strike action|"
        r"labor dispute|discrimination|equality|justice|poverty|inequality|"
        r"civil society|ngo|aid|assistance)\b", re.I)),
]


def infer_category(title: str, summary: str = "", source_category: str = "") -> EventCategory:
    """Infer event category from text content.

    Priority: source-level category > keyword matching > OTHER
    """
    # If source has a declared category, use it
    if source_category:
        try:
            return EventCategory(source_category.lower())
        except ValueError:
            pass

    text = f"{title} {summary}"
    best_category = EventCategory.OTHER
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
) -> Event:
    """Convert a FetchedEntry into an Event object.

    If a source-specific normalizer exists (matched by source_name), it runs
    after generic normalization and overrides fields it knows about.

    Args:
        entry: Raw fetched entry from any source type
        source_id: UUID of the registered source
        source_name: Registered source name (for custom normalizer dispatch)
        source_category: Source-level category hint
        source_language: Source-level language

    Returns:
        Event ready for dedup check and storage
    """
    title = (entry.title or "").strip()
    summary = (entry.summary or "").strip()

    # Skip entries with no usable title
    if not title and not summary:
        title = "(untitled)"

    category = infer_category(title, summary, source_category)
    timestamp = parse_timestamp(entry.published)

    # Extract actors/locations from tags if available
    actors: list[str] = list(entry.authors) if entry.authors else []
    locations: list[str] = []
    tags: list[str] = [t for t in entry.tags if t]

    event = create_event(
        title=title,
        summary=summary,
        event_timestamp=timestamp,
        source_id=source_id,
        source_url=entry.link or "",
        category=category,
        confidence=0.5,
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
                                value = EventCategory(value)
                            except ValueError:
                                continue
                        setattr(event, key, value)
            except Exception as e:
                logger.warning("Source normalizer failed for %s: %s", source_name, e)

    return event

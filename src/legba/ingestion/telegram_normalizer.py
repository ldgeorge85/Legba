"""Telegram message → Signal normalizer.

Converts TelegramMessage objects to the standard Signal schema
so they flow through the same dedup/store/cluster pipeline as
RSS and API signals.
"""

from __future__ import annotations

import logging
import re
from uuid import UUID

from legba.shared.schemas.signals import create_signal

from .normalizer import extract_entities_ner, infer_category
from .telegram import TelegramMessage

logger = logging.getLogger(__name__)

# Max title length extracted from message text
_MAX_TITLE_CHARS = 200


def _extract_title(text: str) -> str:
    """Extract a title from Telegram message text.

    Uses first sentence (up to period/newline) or first N chars.
    """
    # Try first sentence
    for sep in [". ", ".\n", "\n\n", "\n"]:
        idx = text.find(sep)
        if 20 < idx < _MAX_TITLE_CHARS:
            return text[:idx].strip()

    # Fall back to first N chars
    title = text[:_MAX_TITLE_CHARS].strip()
    if len(text) > _MAX_TITLE_CHARS:
        # Cut at last word boundary
        last_space = title.rfind(" ")
        if last_space > _MAX_TITLE_CHARS // 2:
            title = title[:last_space]
    return title


def _extract_urls(text: str) -> list[str]:
    """Extract URLs from message text."""
    return re.findall(r'https?://[^\s<>"]+', text)


def normalize_telegram_message(
    msg: TelegramMessage,
    source_id: UUID,
    source_name: str,
    source_category: str = "",
    source_language: str = "en",
) -> Signal:
    """Convert a Telegram message to a Signal.

    Args:
        msg: TelegramMessage from the fetcher
        source_id: UUID of the source in the sources table
        source_name: Human-readable source name
        source_category: Default category from source config
        source_language: Language code from source config

    Returns:
        Signal object ready for dedup + storage.
    """
    title = _extract_title(msg.text)

    # Build GUID for dedup tier 1
    guid = f"tg:{msg.channel}:{msg.message_id}"

    # Build source URL for dedup tier 2
    source_url = f"telegram://@{msg.channel}/{msg.message_id}"

    # Category inference: use source default, then try keyword-based
    inferred = infer_category(title, msg.text[:500], source_category)
    category = inferred or source_category or "other"

    # NER extraction for actors and locations
    actors, locations = extract_entities_ner(msg.text[:1000])

    # Confidence based on engagement (views/forwards suggest wider distribution)
    base_confidence = 0.4
    if msg.views > 10000:
        base_confidence = 0.6
    elif msg.views > 1000:
        base_confidence = 0.5
    if msg.forwards > 100:
        base_confidence = min(base_confidence + 0.1, 0.7)

    signal = create_signal(
        title=title,
        source_id=source_id,
        source_url=source_url,
        category=category,
        event_timestamp=msg.date,
        language=source_language,
        confidence=base_confidence,
        guid=guid,
    )

    # Enrich signal fields
    signal.full_content = msg.text
    signal.summary = msg.text[:500] if len(msg.text) > 500 else msg.text
    if actors:
        signal.actors = actors
    if locations:
        signal.locations = locations

    return signal

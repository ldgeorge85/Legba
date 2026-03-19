"""Telegram channel fetcher — curated channel monitoring via Telethon.

Fetches new messages from configured Telegram channels and converts
them to FetchedEntry objects for the standard ingestion pipeline.

Not a firehose — each channel is a configured source with a handle,
category, and reliability score, same as RSS/API sources.

Requires: TELEGRAM_API_ID, TELEGRAM_API_HASH, TELEGRAM_SESSION_PATH
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

# Lazy import Telethon to avoid hard dependency
_TelegramClient = None


def _get_client_class():
    global _TelegramClient
    if _TelegramClient is None:
        try:
            from telethon import TelegramClient
            _TelegramClient = TelegramClient
        except ImportError:
            logger.warning("telethon not installed — Telegram ingestion disabled")
            _TelegramClient = False
    return _TelegramClient if _TelegramClient is not False else None


@dataclass
class TelegramMessage:
    """A single message from a Telegram channel."""
    text: str
    date: datetime
    message_id: int
    channel: str
    views: int = 0
    forwards: int = 0


class TelegramFetcher:
    """Fetches new messages from configured Telegram channels.

    Uses Telethon (MTProto client). Session file persists across restarts
    so no re-authentication is needed after initial setup.
    """

    def __init__(self):
        self._client = None
        self._available = False
        self._api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
        self._api_hash = os.getenv("TELEGRAM_API_HASH", "")
        self._session_path = os.getenv("TELEGRAM_SESSION_PATH", "/shared/telegram.session")

    async def connect(self) -> bool:
        """Connect to Telegram API. Returns True if successful."""
        if not self._api_id or not self._api_hash:
            logger.info("Telegram not configured (TELEGRAM_API_ID/HASH missing)")
            return False

        ClientClass = _get_client_class()
        if not ClientClass:
            return False

        try:
            self._client = ClientClass(self._session_path, self._api_id, self._api_hash)
            await self._client.connect()

            if not await self._client.is_user_authorized():
                logger.warning(
                    "Telegram session not authorized. Run telegram_auth.py first."
                )
                await self._client.disconnect()
                self._client = None
                return False

            self._available = True
            logger.info("Telegram connected (session: %s)", self._session_path)
            return True
        except Exception as e:
            logger.warning("Telegram connection failed: %s", e)
            self._client = None
            return False

    @property
    def available(self) -> bool:
        return self._available and self._client is not None

    async def fetch_channel(
        self,
        handle: str,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[TelegramMessage]:
        """Fetch messages from a channel since the given timestamp.

        Args:
            handle: Channel handle (e.g., "@ryaborig" or "ryaborig")
            since: Only fetch messages after this time (default: last 1h)
            limit: Max messages to fetch per call

        Returns:
            List of TelegramMessage objects, oldest first.
        """
        if not self.available:
            return []

        # Normalize handle
        handle = handle.lstrip("@")
        if handle.startswith("telegram://"):
            handle = handle.replace("telegram://", "").lstrip("@")

        if not since:
            from datetime import timedelta
            since = datetime.now(timezone.utc) - timedelta(hours=1)

        try:
            entity = await self._client.get_entity(handle)
            messages = []

            async for msg in self._client.iter_messages(
                entity,
                offset_date=since,
                reverse=True,
                limit=limit,
            ):
                # Skip media-only messages, service messages, and empty
                if not msg.text:
                    continue

                messages.append(TelegramMessage(
                    text=msg.text,
                    date=msg.date.replace(tzinfo=timezone.utc) if msg.date.tzinfo is None else msg.date,
                    message_id=msg.id,
                    channel=handle,
                    views=msg.views or 0,
                    forwards=msg.forwards or 0,
                ))

            logger.info("Telegram: %s — %d messages since %s", handle, len(messages), since.isoformat())
            return messages

        except Exception as e:
            logger.warning("Telegram fetch failed for %s: %s", handle, e)
            return []

    async def close(self):
        """Disconnect from Telegram API."""
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
            self._available = False

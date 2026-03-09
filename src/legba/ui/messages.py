"""
Message infrastructure for the Operator Console.

MessageStore  — Redis sorted-set wrapper for conversation history.
UINatsClient  — Lightweight NATS wrapper for publish / pull.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

import nats
from nats.js.api import ConsumerConfig, DeliverPolicy

from ..shared.schemas.comms import InboxMessage, OutboxMessage

log = logging.getLogger(__name__)

# Redis keys
_ZSET_KEY = "legba:ui:messages"
_DEDUP_KEY = "legba:ui:messages:ids"

# NATS constants
_STREAM = "LEGBA_HUMAN"
_INBOUND_SUBJECT = "legba.human.inbound"
_OUTBOUND_SUBJECT = "legba.human.outbound"
_CONSUMER_NAME = "ui-outbound"


# ------------------------------------------------------------------
# MessageStore
# ------------------------------------------------------------------

class MessageStore:
    """Persist conversation messages in a Redis sorted set (scored by epoch-ms)."""

    def __init__(self, redis_client: Any | None = None):
        self._redis = redis_client
        self._fallback: list[dict] = []

    async def store_inbound(self, msg: InboxMessage) -> None:
        """Persist an operator→agent message."""
        entry = {
            "direction": "inbound",
            "id": msg.id,
            "timestamp": msg.timestamp.isoformat(),
            "content": msg.content,
            "priority": msg.priority.value,
            "requires_response": msg.requires_response,
        }
        await self._store(entry)

    async def store_outbound(self, msg: OutboxMessage) -> None:
        """Persist an agent→operator message."""
        entry = {
            "direction": "outbound",
            "id": msg.id,
            "timestamp": msg.timestamp.isoformat(),
            "content": msg.content,
            "cycle_number": msg.cycle_number,
            "in_reply_to": msg.in_reply_to,
        }
        await self._store(entry)

    async def get_thread(self, limit: int = 200) -> list[dict]:
        """Return conversation messages in chronological order."""
        if self._redis is None:
            entries = self._fallback
        else:
            try:
                raw = await self._redis.zrange(_ZSET_KEY, 0, -1)
                entries = [json.loads(r) for r in raw]
            except Exception:
                entries = self._fallback
        # Filter out empty/junk messages
        entries = [e for e in entries if e.get("content", "").strip() not in ("", "{}")]
        return entries[-limit:]

    # -- internals --

    async def _store(self, entry: dict) -> None:
        msg_id = entry["id"]
        score = _epoch_ms(entry["timestamp"])
        payload = json.dumps(entry)

        if self._redis is None:
            if not any(e["id"] == msg_id for e in self._fallback):
                self._fallback.append(entry)
            return
        try:
            already = await self._redis.sismember(_DEDUP_KEY, msg_id)
            if already:
                return
            await self._redis.zadd(_ZSET_KEY, {payload: score})
            await self._redis.sadd(_DEDUP_KEY, msg_id)
        except Exception:
            if not any(e["id"] == msg_id for e in self._fallback):
                self._fallback.append(entry)


def _epoch_ms(iso_str: str) -> float:
    """Convert ISO timestamp to epoch milliseconds for sorted-set score."""
    from datetime import datetime, timezone

    dt = datetime.fromisoformat(iso_str)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp() * 1000


# ------------------------------------------------------------------
# UINatsClient
# ------------------------------------------------------------------

class UINatsClient:
    """Minimal NATS client for the UI: publish inbound, pull outbound."""

    def __init__(self, url: str = "nats://localhost:4222"):
        self._url = url
        self._nc: nats.NATS | None = None
        self._js: Any | None = None
        self._pull_sub: Any | None = None

    @property
    def available(self) -> bool:
        return self._nc is not None and self._nc.is_connected

    async def connect(self) -> None:
        try:
            self._nc = await nats.connect(self._url)
            self._js = self._nc.jetstream()
            # Create a durable PULL consumer (not push!) with deliver_policy=all
            # so it picks up messages published before the consumer was created.
            # Using pull_subscribe ensures compatibility with drain_outbound's fetch().
            try:
                info = await self._js.consumer_info(_STREAM, _CONSUMER_NAME)
                # If existing consumer is push-bound, delete and recreate as pull
                if getattr(info, 'push_bound', False):
                    await self._js.delete_consumer(_STREAM, _CONSUMER_NAME)
                    raise Exception("recreate as pull")
            except Exception:
                self._pull_sub = await self._js.pull_subscribe(
                    _OUTBOUND_SUBJECT,
                    durable=_CONSUMER_NAME,
                    stream=_STREAM,
                    config=ConsumerConfig(
                        durable_name=_CONSUMER_NAME,
                        deliver_policy=DeliverPolicy.ALL,
                        ack_wait=30,
                    ),
                )
            log.info("UINatsClient connected to %s", self._url)
        except Exception as exc:
            log.warning("UINatsClient: NATS unavailable (%s)", exc)
            self._nc = None
            self._js = None

    async def close(self) -> None:
        if self._nc and not self._nc.is_closed:
            await self._nc.close()
        self._nc = None
        self._js = None

    async def publish_inbound(self, msg: InboxMessage) -> None:
        """Publish an operator message to the inbound subject."""
        if not self.available:
            log.warning("NATS unavailable — message not published")
            return
        payload = msg.model_dump_json().encode()
        await self._js.publish(_INBOUND_SUBJECT, payload)
        log.info("Published inbound message %s", msg.id)

    async def _get_pull_sub(self):
        """Get or create the cached pull subscription."""
        if self._pull_sub is None:
            self._pull_sub = await self._js.pull_subscribe(
                _OUTBOUND_SUBJECT,
                durable=_CONSUMER_NAME,
                stream=_STREAM,
            )
        return self._pull_sub

    async def drain_outbound(self) -> list[OutboxMessage]:
        """Pull any pending outbound messages from the durable consumer."""
        if not self.available:
            return []
        messages: list[OutboxMessage] = []
        try:
            sub = await self._get_pull_sub()
            try:
                batch = await sub.fetch(batch=50, timeout=0.5)
            except nats.errors.TimeoutError:
                batch = []
            for raw in batch:
                try:
                    out = OutboxMessage.model_validate_json(raw.data)
                    # Skip empty/junk messages
                    if out.content and out.content.strip() not in ("", "{}"):
                        messages.append(out)
                except Exception:
                    # Handle raw messages (e.g. {"raw": "..."} or plain text)
                    try:
                        import json, uuid
                        from datetime import datetime, timezone
                        data = json.loads(raw.data)
                        content = data.get("raw") or data.get("content") or str(data)
                        if content and content.strip() not in ("", "{}"):
                            out = OutboxMessage(
                                id=str(uuid.uuid4()),
                                timestamp=datetime.now(timezone.utc),
                                content=content,
                            )
                            messages.append(out)
                    except Exception:
                        pass  # truly unparseable — skip
                try:
                    await raw.ack()
                except Exception:
                    pass  # ack failure is non-fatal
        except Exception as exc:
            log.debug("drain_outbound: %s", exc)
            self._pull_sub = None  # Reset on error so next call recreates
        return messages

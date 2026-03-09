"""
NATS + JetStream Client Wrapper

Async client for the Legba event bus. Handles:
- Connection lifecycle (connect / close / reconnect)
- Human comms (drain inbound, publish outbound)
- Data subject pub/sub
- JetStream stream management
- Queue summary for ORIENT context

Degrades gracefully if NATS is unavailable — returns empty results,
logs the failure, never crashes a cycle.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import nats
from nats.aio.client import Client as NatsConn
from nats.js import JetStreamContext
from nats.errors import (
    ConnectionClosedError,
    NoRespondersError,
    TimeoutError as NatsTimeoutError,
)

from ...shared.schemas.comms import (
    InboxMessage,
    NatsMessage,
    OutboxMessage,
    QueueSummary,
    StreamInfo,
)

logger = logging.getLogger(__name__)

# Default stream configurations
HUMAN_STREAM = "LEGBA_HUMAN"
HUMAN_INBOUND = "legba.human.inbound"
HUMAN_OUTBOUND = "legba.human.outbound"

DATA_STREAM_PREFIX = "LEGBA_DATA"
DATA_SUBJECT_PREFIX = "legba.data"
EVENTS_SUBJECT_PREFIX = "legba.events"
ALERTS_SUBJECT_PREFIX = "legba.alerts"


class LegbaNatsClient:
    """
    Async NATS client for the Legba agent and supervisor.

    Usage:
        client = LegbaNatsClient("nats://nats:4222")
        await client.connect()
        ...
        await client.close()
    """

    def __init__(self, url: str = "nats://localhost:4222", connect_timeout: int = 10):
        self._url = url
        self._connect_timeout = connect_timeout
        self._nc: NatsConn | None = None
        self._js: JetStreamContext | None = None
        self._available = False

    @property
    def available(self) -> bool:
        return self._available and self._nc is not None and self._nc.is_connected

    async def connect(self) -> bool:
        """Connect to NATS. Returns True if successful."""
        try:
            self._nc = await nats.connect(
                self._url,
                connect_timeout=self._connect_timeout,
                max_reconnect_attempts=3,
                reconnect_time_wait=1,
            )
            self._js = self._nc.jetstream()
            self._available = True
            await self._ensure_human_streams()
            logger.info("NATS connected: %s", self._url)
            return True
        except Exception as e:
            logger.warning("NATS unavailable (%s): %s", self._url, e)
            self._available = False
            return False

    async def close(self) -> None:
        """Close the NATS connection."""
        if self._nc and self._nc.is_connected:
            try:
                await self._nc.drain()
            except Exception:
                pass
            self._nc = None
            self._js = None
            self._available = False

    # ------------------------------------------------------------------
    # Human comms (replaces inbox.json / outbox.json)
    # ------------------------------------------------------------------

    async def publish_human_inbound(self, message: InboxMessage) -> bool:
        """Publish a message from the operator to the agent's inbound queue."""
        if not self.available:
            return False
        try:
            payload = message.model_dump_json().encode()
            await self._js.publish(HUMAN_INBOUND, payload)
            return True
        except Exception as e:
            logger.error("Failed to publish human inbound: %s", e)
            return False

    async def drain_human_inbound(self) -> list[InboxMessage]:
        """
        Drain all pending messages from the human inbound queue.

        Called by the agent during WAKE. Returns all pending messages
        and acknowledges them so they won't be redelivered.
        """
        if not self.available:
            return []
        try:
            consumer_name = "agent-wake"
            # Get or create an ephemeral pull consumer
            try:
                psub = await self._js.pull_subscribe(
                    HUMAN_INBOUND,
                    durable=consumer_name,
                    stream=HUMAN_STREAM,
                )
            except Exception:
                return []

            messages: list[InboxMessage] = []
            # Fetch in batches until empty
            while True:
                try:
                    batch = await psub.fetch(batch=50, timeout=1)
                    for msg in batch:
                        try:
                            data = json.loads(msg.data.decode())
                            messages.append(InboxMessage(**data))
                        except Exception:
                            pass  # skip malformed
                        await msg.ack()
                    if len(batch) < 50:
                        break
                except NatsTimeoutError:
                    break
                except Exception:
                    break

            return messages
        except Exception as e:
            logger.error("Failed to drain human inbound: %s", e)
            return []

    async def publish_human_outbound(self, message: OutboxMessage) -> bool:
        """Publish a message from the agent to the operator's outbound queue."""
        if not self.available:
            return False
        try:
            payload = message.model_dump_json().encode()
            await self._js.publish(HUMAN_OUTBOUND, payload)
            return True
        except Exception as e:
            logger.error("Failed to publish human outbound: %s", e)
            return False

    async def drain_human_outbound(self) -> list[OutboxMessage]:
        """
        Drain all pending messages from the agent's outbound queue.

        Called by the supervisor to read agent responses.
        """
        if not self.available:
            return []
        try:
            consumer_name = "supervisor-read"
            try:
                psub = await self._js.pull_subscribe(
                    HUMAN_OUTBOUND,
                    durable=consumer_name,
                    stream=HUMAN_STREAM,
                )
            except Exception:
                return []

            messages: list[OutboxMessage] = []
            while True:
                try:
                    batch = await psub.fetch(batch=50, timeout=1)
                    for msg in batch:
                        try:
                            data = json.loads(msg.data.decode())
                            messages.append(OutboxMessage(**data))
                        except Exception:
                            pass
                        await msg.ack()
                    if len(batch) < 50:
                        break
                except NatsTimeoutError:
                    break
                except Exception:
                    break

            return messages
        except Exception as e:
            logger.error("Failed to drain human outbound: %s", e)
            return []

    # ------------------------------------------------------------------
    # Data / event pub/sub (agent tools)
    # ------------------------------------------------------------------

    async def publish(self, subject: str, payload: dict[str, Any],
                      headers: dict[str, str] | None = None) -> bool:
        """Publish a message to any NATS subject."""
        if not self.available:
            return False
        try:
            data = json.dumps(payload, default=str).encode()
            hdrs = headers or {}
            await self._js.publish(subject, data, headers=hdrs if hdrs else None)
            return True
        except NoRespondersError:
            # Subject has no JetStream stream — publish on core NATS
            try:
                data = json.dumps(payload, default=str).encode()
                await self._nc.publish(subject, data)
                return True
            except Exception as e:
                logger.error("Failed to publish to %s: %s", subject, e)
                return False
        except Exception as e:
            logger.error("Failed to publish to %s: %s", subject, e)
            return False

    async def subscribe_recent(
        self,
        subject: str,
        limit: int = 10,
        stream: str | None = None,
    ) -> list[NatsMessage]:
        """
        Fetch recent messages from a subject (JetStream).

        Fetches last `limit` messages. Does NOT ack — peek only.
        """
        if not self.available:
            return []
        try:
            # Determine stream
            target_stream = stream
            if not target_stream:
                # Try to find stream for subject
                target_stream = await self._find_stream_for_subject(subject)
                if not target_stream:
                    return []

            # Create an ephemeral ordered consumer to get last N messages
            sub = await self._js.subscribe(
                subject,
                ordered_consumer=True,
                stream=target_stream,
            )

            messages: list[NatsMessage] = []
            try:
                while len(messages) < limit:
                    try:
                        msg = await sub.next_msg(timeout=1)
                        payload = json.loads(msg.data.decode()) if msg.data else {}
                        messages.append(NatsMessage(
                            subject=msg.subject,
                            payload=payload,
                            sequence=msg.reply.split(".")[-1] if msg.reply else None,
                        ))
                    except NatsTimeoutError:
                        break
            finally:
                await sub.unsubscribe()

            # Return last N (most recent)
            return messages[-limit:]
        except Exception as e:
            logger.error("Failed to subscribe to %s: %s", subject, e)
            return []

    # ------------------------------------------------------------------
    # Stream management
    # ------------------------------------------------------------------

    async def create_stream(
        self,
        name: str,
        subjects: list[str],
        max_msgs: int = 10000,
        max_bytes: int = 100 * 1024 * 1024,  # 100MB
        max_age: int = 7 * 24 * 60 * 60,  # 7 days in seconds
    ) -> dict[str, Any]:
        """Create or update a JetStream stream."""
        if not self.available:
            return {"error": "NATS unavailable"}
        try:
            from nats.js.api import StreamConfig, RetentionPolicy

            config = StreamConfig(
                name=name,
                subjects=subjects,
                max_msgs=max_msgs,
                max_bytes=max_bytes,
                max_age=max_age,  # seconds (nats-py converts to ns)
                retention=RetentionPolicy.LIMITS,
            )
            info = await self._js.add_stream(config)
            return {
                "name": info.config.name,
                "subjects": list(info.config.subjects or []),
                "messages": info.state.messages,
                "bytes": info.state.bytes,
            }
        except Exception as e:
            return {"error": str(e)}

    async def list_streams(self) -> list[StreamInfo]:
        """List all JetStream streams."""
        if not self.available:
            return []
        try:
            streams = []
            streams_iter = await self._js.streams_info()
            for info in streams_iter:
                streams.append(StreamInfo(
                    name=info.config.name,
                    subjects=list(info.config.subjects or []),
                    messages=info.state.messages,
                    bytes=info.state.bytes,
                    consumer_count=info.state.consumer_count,
                ))
            return streams
        except Exception as e:
            logger.error("Failed to list streams: %s", e)
            return []

    async def queue_summary(self) -> QueueSummary:
        """Build a summary of pending messages for ORIENT context."""
        if not self.available:
            return QueueSummary()
        try:
            streams = await self.list_streams()
            human_pending = 0
            data_streams = []
            total_data = 0

            for s in streams:
                if s.name == HUMAN_STREAM:
                    human_pending = s.messages
                else:
                    data_streams.append(s)
                    total_data += s.messages

            return QueueSummary(
                human_pending=human_pending,
                data_streams=data_streams,
                total_data_messages=total_data,
            )
        except Exception as e:
            logger.error("Failed to build queue summary: %s", e)
            return QueueSummary()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _ensure_human_streams(self) -> None:
        """Create the human comms stream if it doesn't exist."""
        try:
            from nats.js.api import StreamConfig, RetentionPolicy

            config = StreamConfig(
                name=HUMAN_STREAM,
                subjects=[HUMAN_INBOUND, HUMAN_OUTBOUND],
                max_age=30 * 24 * 60 * 60,  # 30 days in seconds
                retention=RetentionPolicy.LIMITS,
            )
            await self._js.add_stream(config)
        except Exception as e:
            logger.warning("Failed to ensure human stream: %s", e)

    async def _find_stream_for_subject(self, subject: str) -> str | None:
        """Find which stream handles a given subject."""
        try:
            info = await self._js.find_stream_name_by_subject(subject)
            return info
        except Exception:
            return None

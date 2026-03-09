"""
Human communication channel schemas.

Inbox: supervisor → agent (human messages)
Outbox: agent → supervisor (agent responses)
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessagePriority(str, Enum):
    NORMAL = "normal"        # Agent sees it, may or may not respond
    URGENT = "urgent"        # Agent should prioritize and respond
    DIRECTIVE = "directive"  # Overrides current cycle, must respond


class InboxMessage(BaseModel):
    """A message from the human operator to the agent."""

    id: str  # UUID string
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    content: str
    priority: MessagePriority = MessagePriority.NORMAL
    requires_response: bool = False
    metadata: dict[str, Any] = Field(default_factory=dict)


class OutboxMessage(BaseModel):
    """A message from the agent to the human operator."""

    id: str  # UUID string
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    in_reply_to: str | None = None  # References inbox message ID
    content: str
    cycle_number: int = 0
    metadata: dict[str, Any] = Field(default_factory=dict)


class Inbox(BaseModel):
    """Container for inbox messages (serialized to /shared/inbox.json)."""

    messages: list[InboxMessage] = Field(default_factory=list)


class Outbox(BaseModel):
    """Container for outbox messages (serialized to /shared/outbox.json)."""

    messages: list[OutboxMessage] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# NATS message schemas (Phase 8)
# ---------------------------------------------------------------------------


class NatsMessage(BaseModel):
    """A message on a NATS data/event subject (not human comms)."""

    subject: str
    payload: dict[str, Any] = Field(default_factory=dict)
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    headers: dict[str, str] = Field(default_factory=dict)
    sequence: int | None = None  # JetStream sequence number (if durable)


class StreamInfo(BaseModel):
    """Summary info for a JetStream stream."""

    name: str
    subjects: list[str] = Field(default_factory=list)
    messages: int = 0
    bytes: int = 0
    consumer_count: int = 0
    created: datetime | None = None


class QueueSummary(BaseModel):
    """Summary of pending messages across NATS subjects for ORIENT context."""

    human_pending: int = 0
    data_streams: list[StreamInfo] = Field(default_factory=list)
    total_data_messages: int = 0

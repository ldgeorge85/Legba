"""
Human Communication Manager

Handles the inbox/outbox protocol between the human operator and the agent.

Primary transport: NATS + JetStream (durable messaging).
Fallback: file-based inbox/outbox (for backwards compatibility and when NATS is unavailable).

The supervisor publishes to legba.human.inbound (or writes inbox.json).
The agent drains inbound during WAKE, publishes to legba.human.outbound (or writes outbox.json).
The supervisor drains outbound to display responses (or reads outbox.json).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..shared.schemas.comms import (
    Inbox,
    InboxMessage,
    MessagePriority,
    Outbox,
    OutboxMessage,
)
from ..agent.comms.nats_client import LegbaNatsClient


class CommsManager:
    """
    Manages human <-> agent communication.

    Uses NATS when available, falls back to file-based inbox/outbox.
    """

    def __init__(self, shared_path: str = "/shared", nats_client: LegbaNatsClient | None = None):
        self._shared = Path(shared_path)
        self._shared.mkdir(parents=True, exist_ok=True)
        self._nats = nats_client

    @property
    def nats_available(self) -> bool:
        return self._nats is not None and self._nats.available

    @property
    def inbox_path(self) -> Path:
        return self._shared / "inbox.json"

    @property
    def outbox_path(self) -> Path:
        return self._shared / "outbox.json"

    async def send_message_async(
        self,
        content: str,
        priority: MessagePriority = MessagePriority.NORMAL,
        requires_response: bool = False,
    ) -> InboxMessage:
        """Send a message from the human to the agent (async, NATS-preferred)."""
        message = InboxMessage(
            id=str(uuid4()),
            content=content,
            priority=priority,
            requires_response=requires_response,
        )

        if self.nats_available:
            await self._nats.publish_human_inbound(message)
        else:
            self._file_send(message)

        return message

    def send_message(
        self,
        content: str,
        priority: MessagePriority = MessagePriority.NORMAL,
        requires_response: bool = False,
    ) -> InboxMessage:
        """Send a message from the human to the agent (sync, file-based fallback)."""
        message = InboxMessage(
            id=str(uuid4()),
            content=content,
            priority=priority,
            requires_response=requires_response,
        )
        self._file_send(message)
        return message

    def send_directive(self, content: str) -> InboxMessage:
        """Send a directive (highest priority, requires response). Sync file-based."""
        return self.send_message(
            content=content,
            priority=MessagePriority.DIRECTIVE,
            requires_response=True,
        )

    async def send_directive_async(self, content: str) -> InboxMessage:
        """Send a directive (highest priority, requires response). Async NATS-preferred."""
        return await self.send_message_async(
            content=content,
            priority=MessagePriority.DIRECTIVE,
            requires_response=True,
        )

    async def read_outbox_async(self) -> list[OutboxMessage]:
        """Read messages from the agent's outbox (async, NATS-preferred)."""
        if self.nats_available:
            messages = await self._nats.drain_human_outbound()
            if messages:
                # Clear file too so stale messages don't accumulate
                self._file_read_outbox(clear=True)
                return messages
        # Fallback to file
        return self._file_read_outbox(clear=True)

    def read_outbox(self, clear: bool = True) -> list[OutboxMessage]:
        """Read messages from the agent's outbox (sync, file-based)."""
        return self._file_read_outbox(clear=clear)

    # ------------------------------------------------------------------
    # File-based I/O (fallback)
    # ------------------------------------------------------------------

    def _file_send(self, message: InboxMessage) -> None:
        """Append message to inbox file."""
        inbox = self._read_inbox()
        inbox.messages.append(message)
        self.inbox_path.write_text(inbox.model_dump_json(indent=2))

    def _file_read_outbox(self, clear: bool = True) -> list[OutboxMessage]:
        """Read and optionally clear the outbox file."""
        if not self.outbox_path.exists():
            return []
        try:
            data = json.loads(self.outbox_path.read_text())
            outbox = Outbox(**data)
        except Exception:
            return []
        messages = outbox.messages
        if clear and messages:
            self.outbox_path.write_text(Outbox().model_dump_json(indent=2))
        return messages

    def _read_inbox(self) -> Inbox:
        """Read existing inbox or return empty."""
        if not self.inbox_path.exists():
            return Inbox()
        try:
            data = json.loads(self.inbox_path.read_text())
            return Inbox(**data)
        except Exception:
            return Inbox()

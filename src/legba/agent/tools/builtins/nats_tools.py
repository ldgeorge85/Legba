"""
NATS Agent Tools

Publish, subscribe, stream management, and queue summary.
Wired to the live LegbaNatsClient by cycle.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ....shared.schemas.tools import ToolDefinition, ToolParameter

if TYPE_CHECKING:
    from ...comms.nats_client import LegbaNatsClient
    from ...tools.registry import ToolRegistry


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

NATS_PUBLISH_DEF = ToolDefinition(
    name="nats_publish",
    description="Publish a message to a NATS subject. Use for data feeds, events, "
                "alerts, and inter-process communication.",
    parameters=[
        ToolParameter(name="subject", type="string",
                      description="NATS subject (e.g. legba.data.cves, legba.events.scan_complete)"),
        ToolParameter(name="payload", type="string",
                      description="JSON payload to publish"),
        ToolParameter(name="headers", type="string",
                      description="Optional JSON object of headers",
                      required=False),
    ],
)

NATS_SUBSCRIBE_DEF = ToolDefinition(
    name="nats_subscribe",
    description="Fetch recent messages from a NATS subject. Returns the last N messages "
                "from a JetStream stream. Does not consume (peek only).",
    parameters=[
        ToolParameter(name="subject", type="string",
                      description="NATS subject to read from"),
        ToolParameter(name="limit", type="number",
                      description="Max messages to return (default 10)",
                      required=False),
        ToolParameter(name="stream", type="string",
                      description="JetStream stream name (auto-detected if omitted)",
                      required=False),
    ],
)

NATS_CREATE_STREAM_DEF = ToolDefinition(
    name="nats_create_stream",
    description="Create or update a JetStream stream for durable message delivery. "
                "Streams provide persistence, replay, and consumer groups.",
    parameters=[
        ToolParameter(name="name", type="string",
                      description="Stream name (e.g. LEGBA_CVE_FEED)"),
        ToolParameter(name="subjects", type="string",
                      description="Comma-separated subjects this stream captures "
                                  "(e.g. legba.data.cves,legba.data.cves.>)"),
        ToolParameter(name="max_msgs", type="number",
                      description="Max messages to retain (default 10000)",
                      required=False),
        ToolParameter(name="max_bytes", type="number",
                      description="Max total bytes (default 100MB)",
                      required=False),
        ToolParameter(name="max_age_days", type="number",
                      description="Max message age in days (default 7)",
                      required=False),
    ],
)

NATS_QUEUE_SUMMARY_DEF = ToolDefinition(
    name="nats_queue_summary",
    description="Get a summary of all NATS streams and pending message counts. "
                "Shows data stream sizes, human queue depth, and total messages.",
    parameters=[],
)

NATS_LIST_STREAMS_DEF = ToolDefinition(
    name="nats_list_streams",
    description="List all JetStream streams with subjects, message counts, and sizes.",
    parameters=[],
)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

def register(registry: ToolRegistry, *, nats: LegbaNatsClient) -> None:
    """Register all NATS tools with the given registry."""

    async def publish_handler(args: dict) -> str:
        if not nats.available:
            return "NATS is not connected. Message not sent."
        subject = args.get("subject", "")
        if not subject:
            return "Error: subject is required"
        try:
            payload = json.loads(args.get("payload", "{}"))
        except json.JSONDecodeError:
            payload = {"raw": args.get("payload", "")}
        headers = None
        if args.get("headers"):
            try:
                headers = json.loads(args["headers"])
            except json.JSONDecodeError:
                pass
        ok = await nats.publish(subject, payload, headers=headers)
        if ok:
            return f"Published to {subject}"
        return f"Failed to publish to {subject} (NATS may be unavailable)"

    async def subscribe_handler(args: dict) -> str:
        if not nats.available:
            return "NATS is not connected."
        subject = args.get("subject", "")
        if not subject:
            return "Error: subject is required"
        limit = int(args.get("limit", 10))
        stream = args.get("stream")
        messages = await nats.subscribe_recent(subject, limit=limit, stream=stream)
        if not messages:
            return f"No messages on {subject}"
        result = []
        for m in messages:
            result.append({
                "subject": m.subject,
                "payload": m.payload,
                "timestamp": m.timestamp.isoformat(),
            })
        return json.dumps(result, indent=2)

    async def create_stream_handler(args: dict) -> str:
        if not nats.available:
            return "NATS is not connected."
        name = args.get("name", "")
        if not name:
            return "Error: name is required"
        subjects_str = args.get("subjects", "")
        subjects = [s.strip() for s in subjects_str.split(",") if s.strip()]
        if not subjects:
            return "Error: subjects is required"
        max_msgs = int(args.get("max_msgs", 10000))
        max_bytes = int(args.get("max_bytes", 100 * 1024 * 1024))
        max_age_days = int(args.get("max_age_days", 7))
        result = await nats.create_stream(
            name=name,
            subjects=subjects,
            max_msgs=max_msgs,
            max_bytes=max_bytes,
            max_age=max_age_days * 24 * 60 * 60,
        )
        return json.dumps(result, indent=2)

    async def queue_summary_handler(args: dict) -> str:
        if not nats.available:
            return json.dumps({"error": "NATS is not connected", "human_pending": 0, "total_data_messages": 0, "data_streams": []})
        summary = await nats.queue_summary()
        result = {
            "human_pending": summary.human_pending,
            "total_data_messages": summary.total_data_messages,
            "data_streams": [
                {"name": s.name, "subjects": s.subjects, "messages": s.messages, "bytes": s.bytes}
                for s in summary.data_streams
            ],
        }
        return json.dumps(result, indent=2)

    async def list_streams_handler(args: dict) -> str:
        if not nats.available:
            return "NATS is not connected."
        streams = await nats.list_streams()
        if not streams:
            return "No JetStream streams"
        result = []
        for s in streams:
            result.append({
                "name": s.name,
                "subjects": s.subjects,
                "messages": s.messages,
                "bytes": s.bytes,
                "consumers": s.consumer_count,
            })
        return json.dumps(result, indent=2)

    registry.register(NATS_PUBLISH_DEF, publish_handler)
    registry.register(NATS_SUBSCRIBE_DEF, subscribe_handler)
    registry.register(NATS_CREATE_STREAM_DEF, create_stream_handler)
    registry.register(NATS_QUEUE_SUMMARY_DEF, queue_summary_handler)
    registry.register(NATS_LIST_STREAMS_DEF, list_streams_handler)

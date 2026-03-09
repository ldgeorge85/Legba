"""
Legba Operator CLI

Send messages to the agent and read responses.

Primary transport: NATS (async, durable).
Fallback: file-based inbox/outbox (when NATS is unavailable).

Usage:
    python -m legba.supervisor.cli send "Your message here"
    python -m legba.supervisor.cli send --urgent "Urgent message"
    python -m legba.supervisor.cli send --directive "Override current work and do this"
    python -m legba.supervisor.cli read
    python -m legba.supervisor.cli status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from ..shared.config import LegbaConfig
from ..shared.schemas.comms import MessagePriority
from ..agent.comms.nats_client import LegbaNatsClient
from .comms import CommsManager


async def _get_comms(args: argparse.Namespace) -> CommsManager:
    """Build a CommsManager with optional NATS connection."""
    config = LegbaConfig.from_env()

    if args.shared:
        shared_path = args.shared
    else:
        shared_path = config.paths.shared

    nats_client = LegbaNatsClient(
        url=config.nats.url,
        connect_timeout=config.nats.connect_timeout,
    )
    await nats_client.connect()

    return CommsManager(shared_path, nats_client=nats_client), nats_client


async def cmd_send(args: argparse.Namespace) -> None:
    """Send a message to the agent."""
    content = " ".join(args.message)
    if not content:
        print("Error: empty message", file=sys.stderr)
        sys.exit(1)

    comms, nats_client = await _get_comms(args)
    try:
        if args.directive:
            msg = await comms.send_directive_async(content)
            transport = "NATS" if comms.nats_available else "file"
            print(f"Directive sent via {transport}: id={msg.id}")
        else:
            priority = MessagePriority.URGENT if args.urgent else MessagePriority.NORMAL
            msg = await comms.send_message_async(
                content=content,
                priority=priority,
                requires_response=args.respond or args.urgent,
            )
            transport = "NATS" if comms.nats_available else "file"
            print(f"Message sent via {transport}: id={msg.id} priority={priority.value}")
    finally:
        await nats_client.close()


async def cmd_read(args: argparse.Namespace) -> None:
    """Read agent responses from the outbox."""
    comms, nats_client = await _get_comms(args)
    try:
        messages = await comms.read_outbox_async()
        if not messages:
            print("(no messages from agent)")
            return

        for msg in messages:
            ts = msg.timestamp.strftime("%Y-%m-%d %H:%M:%S") if hasattr(msg, 'timestamp') else "?"
            reply = f" (reply to {msg.in_reply_to})" if msg.in_reply_to else ""
            print(f"[cycle {msg.cycle_number}]{reply} {ts}")
            print(f"  {msg.content}")
            print()
    finally:
        await nats_client.close()


async def cmd_status(args: argparse.Namespace) -> None:
    """Show current comms and queue status."""
    comms, nats_client = await _get_comms(args)
    try:
        # NATS status
        if comms.nats_available:
            print(f"NATS: connected ({nats_client._url})")
            summary = await nats_client.queue_summary()
            print(f"  Human queue: {summary.human_pending} message(s)")
            print(f"  Data streams: {len(summary.data_streams)}, "
                  f"total messages: {summary.total_data_messages}")
            for s in summary.data_streams:
                print(f"    {s.name}: {s.messages} msgs, "
                      f"subjects={s.subjects}")
        else:
            print("NATS: unavailable (using file-based comms)")

        # File-based status (always show for completeness)
        inbox = comms._read_inbox()
        inbox_count = len(inbox.messages)
        print(f"\nFile inbox:  {inbox_count} pending message(s)")
        if inbox_count:
            for m in inbox.messages:
                print(f"  [{m.priority.value}] {m.content[:80]}")

        outbox_path = comms.outbox_path
        if outbox_path.exists():
            try:
                data = json.loads(outbox_path.read_text())
                outbox_count = len(data.get("messages", []))
            except Exception:
                outbox_count = 0
        else:
            outbox_count = 0
        print(f"File outbox: {outbox_count} unread response(s)")

        # Check for recent heartbeat
        shared = comms._shared
        response_path = shared / "response.json"
        if response_path.exists():
            try:
                resp = json.loads(response_path.read_text())
                print(f"\nLast heartbeat: cycle {resp.get('cycle_number', '?')}, "
                      f"status={resp.get('status', '?')}")
                if resp.get("cycle_summary"):
                    print(f"  Summary: {resp['cycle_summary'][:100]}")
            except Exception:
                pass
        else:
            print("\nLast heartbeat: (none)")
    finally:
        await nats_client.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="legba-cli",
        description="Legba operator CLI — communicate with the agent",
    )
    parser.add_argument(
        "--shared", default=None,
        help="Path to shared directory (default: from config)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # send
    p_send = sub.add_parser("send", help="Send a message to the agent")
    p_send.add_argument("message", nargs="+", help="Message text")
    p_send.add_argument("--urgent", action="store_true", help="Mark as urgent priority")
    p_send.add_argument("--directive", action="store_true",
                        help="Send as directive (highest priority, requires response)")
    p_send.add_argument("--respond", action="store_true",
                        help="Request a response from the agent")

    # read
    p_read = sub.add_parser("read", help="Read agent responses")
    p_read.add_argument("--keep", action="store_true",
                        help="Don't clear outbox after reading")

    # status
    sub.add_parser("status", help="Show comms and queue status")

    args = parser.parse_args()

    if args.command == "send":
        asyncio.run(cmd_send(args))
    elif args.command == "read":
        asyncio.run(cmd_read(args))
    elif args.command == "status":
        asyncio.run(cmd_status(args))


if __name__ == "__main__":
    main()

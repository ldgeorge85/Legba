"""Telegram session bootstrap — interactive first-time auth.

Run once to create the session file:
    python -m legba.ingestion.telegram_auth

Requires TELEGRAM_API_ID and TELEGRAM_API_HASH env vars.
Prompts for phone number and verification code.
Session file is saved to TELEGRAM_SESSION_PATH (default: /shared/telegram.session).
"""

import asyncio
import os
import sys


async def main():
    api_id = int(os.getenv("TELEGRAM_API_ID", "0"))
    api_hash = os.getenv("TELEGRAM_API_HASH", "")
    session_path = os.getenv("TELEGRAM_SESSION_PATH", "/shared/telegram.session")

    if not api_id or not api_hash:
        print("Error: TELEGRAM_API_ID and TELEGRAM_API_HASH must be set")
        sys.exit(1)

    try:
        from telethon import TelegramClient
    except ImportError:
        print("Error: telethon not installed. Run: pip install telethon")
        sys.exit(1)

    print(f"Session path: {session_path}")
    print(f"API ID: {api_id}")
    print()

    client = TelegramClient(session_path, api_id, api_hash)
    await client.start()

    me = await client.get_me()
    print(f"Authenticated as: {me.first_name} {me.last_name or ''} (@{me.username or 'no-username'})")
    print(f"Session saved to: {session_path}")
    print("Telegram ingestion is ready to use.")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())

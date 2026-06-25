#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Mint the base64 Telethon SESSION BLOB for the ``telegram_channel`` source.

The :class:`TelegramChannelSourceHandler` resolves three vault secrets at
configure time — ``source.telegram.api_id``, ``source.telegram.api_hash`` and
``source.telegram.session`` — the last being a BASE64-ENCODED Telethon SQLite
session FILE (``telegram.py:_materialize_session`` decodes it back to disk and
Telethon opens it as SQLite). That session encodes the authorized login of a
REAL Telegram account; Telegram requires an INTERACTIVE login (the account's
phone number + the login code Telegram sends, plus a 2FA password if set) to
mint it — which is why it is generated OUT-OF-BAND by this helper rather than
by the runtime.

SECURITY — the blob grants FULL ACCESS to that Telegram account. Treat it like a
password. It belongs in ``.env`` (gitignored) as ``TELEGRAM_SESSION_B64`` →
loaded into the credential vault by ``scripts/bringup_vault_load.py``. NEVER
commit it; never paste it anywhere public.

USAGE (interactive — you must type the phone + the code Telegram texts you):

    export TELEGRAM_API_ID=...        # from https://my.telegram.org (you have these)
    export TELEGRAM_API_HASH=...
    python3 scripts/telethon_auth.py

    # copy the printed `TELEGRAM_SESSION_B64=...` line into .env, then run
    # scripts/bringup_vault_load.py to push all three into the vault.

Requires telethon (``pip install telethon`` on the host, OR run inside a
legba[telegram] runtime image — the runtime image now ships telethon).
"""
from __future__ import annotations

import base64
import os
import sys
import tempfile


def _need(name: str, env: str) -> str:
    v = os.environ.get(env)
    if v:
        return v.strip()
    try:
        return input(f"{name} ({env} not set in env) > ").strip()
    except EOFError:
        print(f"ERROR: {env} not set and no TTY to prompt.", file=sys.stderr)
        raise SystemExit(2)


def main() -> int:
    try:
        from telethon.sync import TelegramClient  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover — env guard
        print(
            "telethon is not installed. `pip install telethon`, or run this "
            "inside a legba[telegram] runtime image.",
            file=sys.stderr,
        )
        return 2

    api_id_raw = _need("Telegram API ID", "TELEGRAM_API_ID")
    api_hash = _need("Telegram API hash", "TELEGRAM_API_HASH")
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("TELEGRAM_API_ID must be an integer.", file=sys.stderr)
        return 2

    tmp_dir = tempfile.mkdtemp(prefix="legba-tg-auth-")
    session_path = os.path.join(tmp_dir, "telegram.session")

    print(
        "\nConnecting to Telegram. You'll be asked for the account's phone "
        "number, then the login code Telegram sends to it (and a 2FA password "
        "if the account has one).\n",
        file=sys.stderr,
    )
    # telethon.sync: `with TelegramClient(...)` runs the full interactive
    # .start() auth flow and leaves an authorized SQLite session on disk.
    with TelegramClient(session_path, api_id, api_hash) as client:
        me = client.get_me()
        who = getattr(me, "username", None) or getattr(me, "first_name", "?")
        print(f"Authorized as: {who} (id={getattr(me, 'id', '?')})", file=sys.stderr)

    with open(session_path, "rb") as fh:
        blob = base64.b64encode(fh.read()).decode("ascii")

    # The blob is the durable artifact — wipe the on-disk session.
    for p in (session_path, tmp_dir):
        try:
            os.remove(p) if os.path.isfile(p) else os.rmdir(p)
        except OSError:
            pass

    print("\n# ---- add to .env (gitignored — NEVER commit) ----", file=sys.stderr)
    print(f"TELEGRAM_SESSION_B64={blob}")
    print(
        f"\n# blob is {len(blob)} chars. Ensure TELEGRAM_API_ID + "
        "TELEGRAM_API_HASH are also in .env, then run "
        "`python3 scripts/bringup_vault_load.py`.",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

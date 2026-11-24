# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The FIRST OWNER — a one-time command run on the host (A-1).

    docker compose exec legba-registry python -m legba.data.users_bootstrap \\
        --email you@example.org [--name "Your Name"]

It asks for the password twice on the terminal (no echo) and creates one
active ``admin`` account with ``users.is_owner`` set, its password stored as
an argon2id hash (``legba.data.users.create_owner``). Nothing secret passes
through the command line, the environment, the repository or a log line:

  * the password is read with ``getpass`` (or, for automation, one line on
    standard input with ``--password-stdin``), never from an argument;
  * no invite token is minted, so there is no link to leak;
  * the only output is the new account's id and email.

It refuses when an owner already exists (it runs once), when the email has an
account, and when migration 0255 has not been applied. It does not need the
sign-in switch to be on: create the owner first, then turn the switch on.
Further people are invited from the console (``POST /api/v1/auth/invite``).
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import sys
from typing import Callable, TextIO

from . import users as _users


def read_password(
    *, from_stdin: bool, stdin: TextIO | None = None,
    prompt: Callable[[str], str] = getpass.getpass,
) -> str:
    """The password: one stdin line, or typed twice without echo."""
    if from_stdin:
        line = (stdin or sys.stdin).readline()
        return line.rstrip("\r\n")
    first = prompt("Password for the owner account: ")
    second = prompt("The same password again: ")
    if first != second:
        raise _users.AuthRefused(
            "password_mismatch", "the two passwords differ", status=400,
        )
    return first


async def bootstrap_owner(
    conn: object, *, email: str, password: str, display_name: str = "",
) -> _users.User:
    """Create the owner on ``conn`` (an asyncpg connection)."""
    has_col = await conn.fetchval(  # type: ignore[attr-defined]
        "SELECT 1 FROM information_schema.columns WHERE table_schema = 'public' "
        "AND table_name = 'users' AND column_name = 'is_owner'",
    )
    if has_col is None:
        raise _users.AuthRefused(
            "migration_missing",
            "users.is_owner is missing; apply migration 0255 first "
            "(python -m legba.data.migrate)",
            status=409,
        )
    return await _users.create_owner(
        conn, email=email, password=password, display_name=display_name,
    )


async def _run(args: argparse.Namespace, password: str) -> _users.User:
    import asyncpg

    from .config import PostgresConfig

    conn = await asyncpg.connect(PostgresConfig.from_env().dsn)
    try:
        return await bootstrap_owner(
            conn, email=args.email, password=password, display_name=args.name,
        )
    finally:
        await conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m legba.data.users_bootstrap",
        description="Create the instance owner (one time).",
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default="", help="display name (optional)")
    parser.add_argument(
        "--password-stdin", action="store_true",
        help="read the password from one line of standard input",
    )
    args = parser.parse_args(argv)
    try:
        password = read_password(from_stdin=args.password_stdin)
        _users.check_password_policy(password)
        user = asyncio.run(_run(args, password))
    except _users.AuthRefused as exc:
        print(f"refused ({exc.code}): {exc.message}", file=sys.stderr)
        return 1
    print(f"owner created: id={user.id} email={user.email} role=admin")
    print("Next: set LEGBA_AUTH_SIGNIN=1 and the service tokens, then recreate "
          "(docs/OPERATING_YOUR_INSTANCE.md, 'Turning on sign-in').")
    return 0


if __name__ == "__main__":
    sys.exit(main())

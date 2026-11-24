# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W11-F259 — a redirected page's receipt is linked to its request.

The receipt writer keys a version by the address the bytes came FROM (the
final URL of a redirect). Roll 79's Federal Register request was redirected
to unblock.federalregister.gov, so the lookup under the requested address
found nothing and the request's ``result_version_id`` stayed null. The run
now falls back to the final address, matched on the page's own body hash,
whether the page landed or was refused.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from legba.runtime import intake_run as IR

REQ = "https://www.federalregister.gov/documents/2026/11/04/2026-22472/determination-with-respect-to-yemen"
WALL = "https://unblock.federalregister.gov"
SHA = "ea2b86cd" * 8


class _Conn:
    def __init__(self, versions: dict[tuple[str, str | None], int]) -> None:
        self.versions = versions
        self.calls: list[tuple[str, tuple[Any, ...]]] = []

    async def fetchval(self, sql: str, *args: Any) -> Any:
        self.calls.append((sql, args))
        if "fetch_drift_observations" in sql:
            return 0
        if "sha256" in sql:
            return self.versions.get((args[0], args[1]))
        return self.versions.get((args[0], None))


class _Pool:
    def __init__(self, conn: _Conn) -> None:
        self.conn = conn

    def acquire(self) -> Any:
        conn = self.conn

        class _CM:
            async def __aenter__(self) -> _Conn:
                return conn

            async def __aexit__(self, *exc: Any) -> None:
                return None

        return _CM()


NOW = datetime(2026, 11, 14, 5, 38, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_the_version_under_the_final_address_with_the_same_body_is_the_requests() -> None:
    conn = _Conn({(WALL, SHA): 4210})
    vid, drift = await IR._version_after(_Pool(conn), REQ, NOW, final=WALL, sha256=SHA)
    assert (vid, drift) == (4210, 0)


@pytest.mark.asyncio
async def test_a_different_body_under_the_final_address_is_never_linked() -> None:
    conn = _Conn({(WALL, "0" * 64): 4209})
    vid, _ = await IR._version_after(_Pool(conn), REQ, NOW, final=WALL, sha256=SHA)
    assert vid is None


@pytest.mark.asyncio
async def test_the_requested_address_wins_and_no_fallback_runs_without_a_redirect() -> None:
    conn = _Conn({(REQ, None): 4211, (WALL, SHA): 4210})
    vid, _ = await IR._version_after(_Pool(conn), REQ, NOW, final=WALL, sha256=SHA)
    assert vid == 4211
    conn = _Conn({(WALL, SHA): 4210})
    vid, _ = await IR._version_after(_Pool(conn), REQ, NOW, final=REQ, sha256=SHA)
    assert vid is None and not any("sha256" in sql for sql, _ in conn.calls)

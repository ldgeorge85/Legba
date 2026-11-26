# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A LIVE observation id that Consult cites
from ``live_series`` resolves in the cited-refs census as an observation of
class ``live`` — not ``fabricated``. The census reads ``observations`` by id
whatever its origin_class, so the bare ``observation_id`` the tool returns
(and lists under ``refs``) is a resolvable citation. Pure: a fake pool that
answers the census's id lookups.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from uuid import uuid4

import pytest

from legba.runtime.substrate_cited_refs import classify_cited_refs

LIVE_OBS = uuid4()


class _Conn:
    async def fetch(self, sql: str, ids):
        if "FROM observations" in sql and "origin_class" in sql:
            return [{"id": i, "origin_class": "live"} for i in ids if i == LIVE_OBS]
        return []


class _Pool:
    @asynccontextmanager
    async def acquire(self):
        yield _Conn()


@pytest.mark.asyncio
async def test_a_live_observation_cited_from_live_series_resolves_as_live() -> None:
    out = await classify_cited_refs(_Pool(), refs=[str(LIVE_OBS)])
    assert out["by_origin_class"] == {"live": 1}
    assert out["by_kind"].get("observation") == 1
    assert not out.get("fabricated") and not out.get("malformed")

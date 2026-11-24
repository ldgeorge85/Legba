# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Roll 74: secondary country codes reach the PERSISTED ``signals.geo``.

Lane N-O changed the host promote step; roll 73 (lane N-L) moved the signal
write into ``runtime/sinks/signal.py``. This drives the real chain the source
actor runs — the geocode handler under an official source's context, the host
promote step (:func:`promote_payload_columns`, the function ``dapr_host``'s
enrichment stage calls), then the document sink's ``write_canonical_signal`` —
and reads the row back from Postgres. Only the geocoder's network backend is
stubbed.
"""

from __future__ import annotations

import json

import asyncpg
import pycountry
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig
from legba.data.filters._contract import FilterContext
from legba.data.filters._geo_precedence import promote_payload_columns
from legba.data.filters.geocode import GeocodeConfig, GeocodeHandler, GeocodeResult
from legba.data.sources._contract import Signal
from legba.runtime.sinks.signal import write_canonical_signal


class _Backend:
    name = "stub"

    async def geocode(self, query):
        try:
            iso = pycountry.countries.lookup(query).alpha_2
        except LookupError:
            return None
        return GeocodeResult(country=query, country_iso2=iso, country_iso3=None,
                             region=None, municipality=None, address=None,
                             lat=1.0, lon=2.0, precision="country", source="stub")

    async def reachable(self):
        return True


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    yield c
    await c.close()


async def _enrich_and_write(conn, payload, ctx, source_id):
    h = GeocodeHandler(GeocodeConfig(precision="country"), backend=_Backend())
    sig = await h.transform(Signal(source_id=source_id, payload=payload), ctx)
    promote_payload_columns(sig)
    written = await write_canonical_signal(
        conn, sig, source_version="v1", owner_tenant="default", access_class="public",
    )
    assert written is not None
    row = await conn.fetchrow("SELECT geo, payload FROM signals WHERE id = $1", sig.signal_id)
    payload_out = row["payload"]
    if isinstance(payload_out, str):
        payload_out = json.loads(payload_out)
    return list(row["geo"] or []), payload_out


@pytest.mark.integration
@pytest.mark.asyncio
async def test_official_issuer_and_subject_both_persist_on_signals_geo(conn):
    payload = {"title": "Treasury Sanctions Iran-Based Oil Network",
               "raw_body": "OFAC designated six entities in Iran and the United Arab "
                           "Emirates.", "language": "en"}
    ctx = FilterContext(target_id="s", scope_geo=["US"], scope_languages=["en"],
                        source_class="official")
    geo, pl = await _enrich_and_write(conn, payload, ctx, "source.test_roll74.ofac")
    try:
        assert geo[0] == "IR"
        assert set(geo) == {"IR", "US", "AE"}
        assert pl["geo"]["derivation"]["US"] == ["declared_by_source"]
        assert pl["geo"]["countries"][0] == "IR"
    finally:
        await conn.execute("DELETE FROM signals WHERE source_id = 'source.test_roll74.ofac'")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_general_news_persists_one_country_as_before(conn):
    payload = {"title": "Treasury Sanctions Iran-Based Oil Network", "language": "en"}
    ctx = FilterContext(target_id="s", scope_geo=[], scope_languages=["en"],
                        source_class="reporting")
    geo, _ = await _enrich_and_write(conn, payload, ctx, "source.test_roll74.news")
    try:
        assert geo == ["IR"]
    finally:
        await conn.execute("DELETE FROM signals WHERE source_id = 'source.test_roll74.news'")

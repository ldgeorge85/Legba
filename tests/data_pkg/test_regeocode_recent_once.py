# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/regeocode_recent_once.py``, the bounded one-shot re-tag.

* PURE (``assess_row``): the live FR / BA mis-tags re-derive to the declared
  country; a handler-set code (GDELT) is kept; a row whose answer needs the
  gazetteer is skipped; a stored-untagged row the publisher fallback alone
  would tag is skipped unless asked; a fixed row re-assesses unchanged.
* DB-backed (``integration``, container suite): dry-run writes nothing;
  ``--apply`` backs up first, updates geo + payload.geo, NULLs indexed_at,
  stays inside the window, and is idempotent (the backup keeps the FIRST,
  pre-change values).
"""

from __future__ import annotations

import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.config import PostgresConfig

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "regeocode_recent_once.py"
_spec = importlib.util.spec_from_file_location("regeocode_recent_once", _SCRIPT)
rro = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = rro
_spec.loader.exec_module(rro)

_FR_BIS = {
    "geo": {"source": "nominatim", "country_iso2": "FR", "lat": 46.6, "lon": 1.88},
    "title": "Agency Information Collection Activities; Comment Request",
    "raw_body": "Paperwork Reduction Act notice (September 24, 2026, 91 FR 60505).",
    "language": "en", "publisher_origin": ["US"],
}
_BA_BMVG = {
    "geo": {"source": "nominatim", "country_iso2": "BA", "lat": 44.3, "lon": 17.6},
    "title": "Der Bau von Kasernen wird schneller und unkomplizierter",
    "raw_body": '<img src="https://www.bmvg.de/x/32CBAA36A2A7FDD56BA9105E76AFE320/g.jpg" />',
    "language": "de", "publisher_origin": ["DE"],
}


async def _assess(payload, geo, *, source_class="official", scope_geo=("US",), **kw):
    return await rro.assess_row(payload, list(geo), source_id="source.t",
                                source_class=source_class, scope_geo=list(scope_geo),
                                scope_languages=[payload.get("language") or "en"], **kw)


@pytest.mark.asyncio
async def test_federal_register_fr_retags_to_us():
    d = await _assess(_FR_BIS, ["FR"])
    assert d.action == "changed" and d.new_geo == ["US"]
    assert d.geo_block["derivation"] == {"US": ["declared_by_source"]}


@pytest.mark.asyncio
async def test_bmvg_ba_retags_to_de():
    d = await _assess(_BA_BMVG, ["BA"], scope_geo=("DE",))
    assert d.action == "changed" and d.new_geo == ["DE"]


@pytest.mark.asyncio
async def test_fixed_row_reassesses_unchanged():
    d = await _assess(_FR_BIS, ["FR"])
    fixed = {**_FR_BIS, "geo": d.geo_block}
    again = await _assess(fixed, d.new_geo)
    assert again.action == "unchanged"


@pytest.mark.asyncio
async def test_handler_set_code_is_kept():
    """A GDELT row: the handler's structured block (no geocoder source) and
    its code stay; nothing is re-derived over it."""
    payload = {"geo": {"country_iso2": "IN", "lat": 28.4, "lon": 77.3},
               "title": "SINGAPORE: fight in Faridabad, Haryana, India"}
    d = await _assess(payload, ["IN"], source_class=None, scope_geo=())
    assert d.action == "unchanged"


@pytest.mark.asyncio
async def test_place_query_needing_the_gazetteer_is_skipped():
    payload = {"geo": {"source": "nominatim", "country_iso2": "FR", "lat": 1, "lon": 2},
               "title": "Storm reaches Kinmen County", "language": "en",
               "entities": [{"class": "location", "text": "Kinmen County"}]}
    d = await _assess(payload, ["FR"], source_class="reporting", scope_geo=())
    assert d.action == "skip_needs_backend"


@pytest.mark.asyncio
async def test_hold_title_losses_keeps_a_country_the_title_names():
    """NER mislabelled "US House" as a person and the guard
    blanked it, so the re-derive dropped US. A later change fixed the guard (the
    country inside a mislabelled institution stays), so that row now
    re-derives UNCHANGED; --hold-title-losses still holds a row whose re-derive
    drops a country the unmasked title names (the guard's own Jim Jordan)."""
    payload = {"geo": {"source": "nominatim", "country_iso2": "US", "lat": 1, "lon": 2},
               "title": "US House blocks bid to impeach the defence secretary",
               "language": "en", "entities": [{"class": "person", "text": "US House"}]}
    d = await _assess(payload, ["US"], source_class="reporting", scope_geo=())
    assert d.action == "unchanged" and d.new_geo == ["US"]
    jordan = {"geo": {"source": "nominatim", "country_iso2": "JO", "lat": 1, "lon": 2},
              "title": "Republicans elect Jim Jordan as minority leader",
              "language": "en", "entities": [{"class": "person", "text": "Jim Jordan"}]}
    d = await _assess(jordan, ["JO"], source_class="reporting", scope_geo=())
    assert d.action == "changed" and "JO" not in d.new_geo
    held = await _assess(jordan, ["JO"], source_class="reporting", scope_geo=(),
                         hold_title_losses=True)
    assert held.action == "skip_title_loss"
    # A real fix (FR off a citation) is not held: the text never names France.
    d = await _assess(_FR_BIS, ["FR"], hold_title_losses=True)
    assert d.action == "changed" and d.new_geo == ["US"]


@pytest.mark.asyncio
async def test_untagged_row_only_the_fallback_would_tag_is_skipped_unless_asked():
    payload = {"title": "Coalition talks snarl", "language": "en",
               "publisher_origin": ["IL"]}
    d = await _assess(payload, [], source_class="reporting", scope_geo=("IL",))
    assert d.action == "skip_fallback_only"
    d = await _assess(payload, [], source_class="reporting", scope_geo=("IL",),
                      include_fallback=True)
    assert d.action == "changed" and d.new_geo == ["IL"]
    assert d.geo_fallback["basis"] == "publisher_origin"


# ---------------------------------------------------------------------------
# DB-backed (container suite)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    _CREATED_SOURCES.clear()
    yield c
    # Retire exactly the rows the tests wrote. The source head below is an
    # active `official` row with a scope, so left behind it joins every
    # global candidate-source read in the session DB (shuffled nightly seeds
    # 1026012366 / 537394567: test_collection_requirements' pollution guard
    # named source.t_<tag>.fr and errored all 15 of its tests).
    if _CREATED_SOURCES:
        ids = list(_CREATED_SOURCES)
        await c.execute("DELETE FROM signals WHERE source_id = ANY($1::text[])", ids)
        await c.execute(
            "DELETE FROM source_descriptors WHERE descriptor_id = ANY($1::text[]) AND owner = 't'", ids)
        _CREATED_SOURCES.clear()
    # The script's backup table is no migration's object: leave the session
    # DB as migrated (the schema-hygiene check reads it).
    await c.execute(f"DROP TABLE IF EXISTS {rro.BACKUP_TABLE}")
    await c.close()


# Source ids `_descriptor` wrote during the current test; the `conn` fixture
# deletes them (and their signals) at teardown.
_CREATED_SOURCES: set[str] = set()


async def _descriptor(conn, source_id, *, source_class, geo):
    _CREATED_SOURCES.add(source_id)
    body = {"scope": {"source_class": source_class, "geo": geo, "languages": ["en"]}}
    await conn.execute(
        """INSERT INTO source_descriptors
               (descriptor_id, version, schema_uri, is_head, kind, state, owner, name, body)
           VALUES ($1, 'v1', 'legba/source/1.0.0', true, 'rss', 'active', 't', $1, $2::jsonb)
           ON CONFLICT DO NOTHING""",
        source_id, json.dumps(body),
    )


async def _signal(conn, source_id, *, geo, payload, created_at):
    sid = uuid4()
    await conn.execute(
        """INSERT INTO signals (id, source_id, modality, payload, content_hash, geo,
                                indexed_at, created_at)
           VALUES ($1, $2, 'text', $3::jsonb, $4, $5::text[], NOW(), $6)""",
        sid, source_id, json.dumps(payload), f"h_{uuid4().hex}", geo, created_at,
    )
    return sid


@pytest.mark.integration
@pytest.mark.asyncio
async def test_apply_backs_up_retags_and_is_idempotent(conn):
    tag = uuid4().hex[:8]
    src = f"source.t_{tag}.fr"
    await _descriptor(conn, src, source_class="official", geo=["US"])
    now = datetime.now(tz=timezone.utc)
    since, until = now - timedelta(days=1), now + timedelta(minutes=1)
    bad = await _signal(conn, src, geo=["FR"], payload=_FR_BIS, created_at=now)
    old = await _signal(conn, src, geo=["FR"], payload=_FR_BIS,
                        created_at=now - timedelta(days=3))           # outside the window
    ok = await _signal(conn, src, geo=["US"], payload={
        "title": "Notice of OFAC Sanctions Action", "language": "en",
        "geo": {"source": "declared", "country_iso2": "US", "countries": ["US"],
                "derivation": {"US": ["declared_by_source"]}}}, created_at=now)

    dry = await rro.run(conn, since=since, until=until, quiet=True)
    assert {c["id"] for c in dry["changes"]} == {str(bad)}
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", bad) == ["FR"]
    assert await conn.fetchval("SELECT to_regclass($1)", rro.BACKUP_TABLE) is None \
        or await conn.fetchval(f"SELECT count(*) FROM {rro.BACKUP_TABLE} WHERE id=$1", bad) == 0

    res = await rro.run(conn, since=since, until=until, apply=True, quiet=True)
    assert res["changed"] == 1
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", bad) == ["US"]
    assert await conn.fetchval("SELECT indexed_at FROM signals WHERE id=$1", bad) is None
    pl = json.loads(await conn.fetchval("SELECT payload FROM signals WHERE id=$1", bad))
    assert pl["geo"]["derivation"] == {"US": ["declared_by_source"]}
    backup = await conn.fetchrow(f"SELECT geo, payload_geo FROM {rro.BACKUP_TABLE} WHERE id=$1", bad)
    assert backup["geo"] == ["FR"] and json.loads(backup["payload_geo"])["country_iso2"] == "FR"
    # Out-of-window and already-right rows untouched.
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", old) == ["FR"]
    assert await conn.fetchval("SELECT geo FROM signals WHERE id=$1", ok) == ["US"]
    assert await conn.fetchval("SELECT indexed_at FROM signals WHERE id=$1", ok) is not None

    again = await rro.run(conn, since=since, until=until, apply=True, quiet=True)
    assert again["changed"] == 0
    backup = await conn.fetchrow(f"SELECT geo FROM {rro.BACKUP_TABLE} WHERE id=$1", bad)
    assert backup["geo"] == ["FR"]                      # the FIRST values are kept

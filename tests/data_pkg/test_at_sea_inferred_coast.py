# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""An incident at sea names the coasts it lies off.

An authoritative point that lies in no country under NO country
(``payload.geo.at_sea``, empty ``signals.geo``). This adds the honest
relation beside it: ``payload.geo.inferred`` = the coastal states whose
bundled 110m outline is within 200 km, nearest first, at most three, each
marked ``basis: nearest_coast`` with its distance. The relation is never a
filing: ``signals.geo`` stays empty, ``countries`` / ``derivation`` are
untouched, and the promote step does not lift it.

The desks then read it: the slice's
desk geo test (``signal_slice_scope.desk_geo_sql``) admits an unfiled at-sea
row whose ``inferred`` coasts name a desk code, and the rendered row says
"at sea, about N km off X ... not an event in X". The persisted-row tests
below pin both: the Hormuz incident reaches the Oman, UAE and Iran desks and
the Hormuz shipping-lane desk; the open-ocean incident reaches no desk.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

import asyncpg
import pytest
import yaml

from legba.data.config import PostgresConfig
from legba.data.filters import _geo_precedence as prec
from legba.data.filters._contract import FilterContext
from legba.data.filters._country_geometry import (
    NEAREST_COAST_MAX_KM,
    NEAREST_COAST_MAX_STATES,
    coastal_states_near,
)
from legba.data.filters.geocode import GeocodeConfig, GeocodeHandler
from legba.data.sources._contract import Signal
from legba.data.sources.json_api_mapping import JsonApiGeometry, apply_geometry

REPO = Path(__file__).resolve().parents[2]
HORMUZ = (26.433333, 56.486111)      # the UKMTO fixture point, 20 NM north of Oman


def _ukmto_payload(lat: float = HORMUZ[0], lon: float = HORMUZ[1]) -> dict[str, Any]:
    payload: dict[str, Any] = {"title": "Attack: Strait of Hormuz — Tanker",
                               "raw_body": "An incident 20NM north of Oman, a tanker was struck."}
    apply_geometry(payload, {"locationLatitude": lat, "locationLongitude": lon,
                             "place": "Strait of Hormuz"},
                   JsonApiGeometry(lat_path="locationLatitude", lon_path="locationLongitude",
                                   place_path="place"))
    return payload


class _NoBackend:
    name = "stub"

    async def geocode(self, query: str):
        raise AssertionError("an authoritative at-sea point never runs the text ladder")

    async def reachable(self) -> bool:
        return True


def _fctx() -> FilterContext:
    return FilterContext(target_id="t", target_version="v", filter_id="geocode",
                         logger=logging.getLogger("test.n_s.coast"))


async def _geocoded(payload: dict[str, Any], **sig: Any) -> Signal:
    s = Signal(source_id="source.ukmto.incidents", payload=payload, tags=["maritime"], **sig)
    out = await GeocodeHandler(GeocodeConfig(), backend=_NoBackend()).transform(s, _fctx())
    return prec.promote_payload_columns(out)


# ---------------------------------------------------------------------------
# The geometry
# ---------------------------------------------------------------------------


def test_constants_are_declared() -> None:
    assert NEAREST_COAST_MAX_KM == 200.0 and NEAREST_COAST_MAX_STATES == 3


@pytest.mark.parametrize("lat, lon, nearest, members", [
    (*HORMUZ, "OM", {"OM", "IR", "AE"}),           # the strait
    (12.6, 43.4, "YE", {"YE", "DJ", "ER"}),        # Bab el-Mandeb
    (27.3, 50.2, "SA", {"SA"}),                    # the Gulf off Jubail
])
def test_coastal_states_near_a_strait(lat, lon, nearest, members) -> None:
    near = coastal_states_near(lat, lon)
    assert near[0][0] == nearest
    assert members <= {iso for iso, _ in near}
    assert len(near) <= NEAREST_COAST_MAX_STATES
    assert [km for _, km in near] == sorted(km for _, km in near)
    assert all(0 < km <= NEAREST_COAST_MAX_KM for _, km in near)


def test_open_ocean_names_no_coast() -> None:
    assert coastal_states_near(30.0, -40.0) == []
    assert coastal_states_near(None, None) == []  # type: ignore[arg-type]


def test_distance_bound_is_respected() -> None:
    wide = coastal_states_near(*HORMUZ, max_km=1000, limit=10)
    tight = coastal_states_near(*HORMUZ, max_km=30)
    assert len(wide) > 3 and all(km <= 30 for _, km in tight)
    assert [iso for iso, _ in tight] == [iso for iso, km in wide if km <= 30][:3]


# ---------------------------------------------------------------------------
# The payload field
# ---------------------------------------------------------------------------


async def test_at_sea_point_records_inferred_coasts_and_files_no_country() -> None:
    out = await _geocoded(_ukmto_payload())
    geo = out.payload["geo"]
    assert geo["at_sea"] is True and geo["countries"] == [] and "country_iso2" not in geo
    assert geo["location_name"] == "Strait of Hormuz"
    inferred = geo["inferred"]
    assert inferred and inferred[0]["iso2"] == "OM"
    assert all(e["basis"] == prec.NEAREST_COAST and isinstance(e["distance_km"], float)
               for e in inferred)
    assert prec.inferred_coastal_states(geo) == [e["iso2"] for e in inferred]
    # Never a filing.
    assert out.geo == [] and prec.geo_derivation(out.payload) == {}
    assert prec.promoted_geo_codes(geo) == []


async def test_open_ocean_point_records_an_empty_relation() -> None:
    out = await _geocoded(_ukmto_payload(lat=-30.0, lon=-20.0))
    assert out.payload["geo"]["at_sea"] is True and out.payload["geo"]["inferred"] == []


def test_inferred_is_read_only_from_an_at_sea_block() -> None:
    assert prec.inferred_coastal_states({"country_iso2": "OM", "inferred": [{"iso2": "OM"}]}) == []
    assert prec.inferred_coastal_states(None) == []


# ---------------------------------------------------------------------------
# Persisted rows, and the desks as they select today
# ---------------------------------------------------------------------------


class _TargetConn:
    """The real connection, with the target-descriptor head lookup answered
    from a descriptor file (the slice reader's only non-signals read)."""

    def __init__(self, conn: asyncpg.Connection, body: dict[str, Any]) -> None:
        self._conn, self._body = conn, body

    async def fetchrow(self, sql: str, *a: Any) -> Any:
        if "FROM target_descriptors" in sql:
            return {"body": self._body}
        return await self._conn.fetchrow(sql, *a)

    async def fetch(self, sql: str, *a: Any) -> Any:
        return await self._conn.fetch(sql, *a)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._conn, name)


def _descriptor_body(path: str) -> dict[str, Any]:
    return yaml.safe_load((REPO / "descriptors" / path).read_text())


@pytest.fixture
async def conn(migrated_pg: PostgresConfig):
    c = await asyncpg.connect(migrated_pg.dsn)
    try:
        yield c
    finally:
        await c.close()


BAB_EL_MANDEB = (12.6, 43.4)
OPEN_OCEAN = (30.0, -40.0)


# ---------------------------------------------------------------------------
# The rendered row and the SQL shape (pure)
# ---------------------------------------------------------------------------

_IN_COUNTRY_ROW = {
    "id": "x", "geo": ["OM"], "source_url": "https://example.test/a",
    "title": "Port of Duqm expands",
    "produced_at": "2026-11-13 08:00:00+00:00",
    "data": {"title": "Port of Duqm expands", "summary": "The port grew.",
             "geo": {"country_iso2": "OM"}},
}

#: The pre-N-X render of an in-country row — the bytes must not move.
_IN_COUNTRY_GOLDEN = (
    "[1] Port of Duqm expands\n"
    "    ingested=2026-11-13 08:00:00+00:00 source=https://example.test/a\n"
    "    snippet=The port grew."
)


def test_in_country_row_renders_byte_identically() -> None:
    from legba.data.analysts.slice_render import _render_signal, _render_user_prompt

    assert _render_signal(1, _IN_COUNTRY_ROW) == _IN_COUNTRY_GOLDEN
    prompt = _render_user_prompt([_IN_COUNTRY_ROW], "country_om", run_date="2026-11-13")
    assert "at sea" not in prompt and prompt.endswith(_IN_COUNTRY_GOLDEN)


def test_at_sea_row_is_marked_in_plain_words() -> None:
    from legba.data.analysts.slice_render import (
        _AT_SEA_LEGEND, _render_signal, _render_user_prompt, at_sea_line,
    )

    row = {**_IN_COUNTRY_ROW, "geo": [], "data": {
        "title": "Attack: Strait of Hormuz — Tanker", "summary": "A tanker was struck.",
        "geo": {"at_sea": True, "countries": [], "location_name": "Strait of Hormuz",
                "inferred": [{"iso2": "OM", "basis": "nearest_coast", "distance_km": 16.2},
                             {"iso2": "AE", "basis": "nearest_coast", "distance_km": 46.0},
                             {"iso2": "IR", "basis": "nearest_coast", "distance_km": 67.4}]}}}
    assert at_sea_line(row) == (
        "    location=at sea (Strait of Hormuz), about 16 km off Oman, 46 km off "
        "United Arab Emirates, 67 km off Iran (nearest coasts inferred from the "
        "reported coordinates; not an event in Oman, United Arab Emirates or Iran)")
    rendered = _render_signal(1, row).splitlines()
    assert rendered[2] == at_sea_line(row) and rendered[3].startswith("    snippet=")
    assert _AT_SEA_LEGEND.strip() in _render_user_prompt([row], "lane_hormuz", run_date="x")
    ocean = {**row, "data": {**row["data"], "geo": {"at_sea": True, "inferred": []}}}
    assert at_sea_line(ocean) == (
        "    location=at sea, no coast within 200 km (not an event in any country)")
    # A FILED row is never marked, whatever its payload says.
    assert at_sea_line({**row, "geo": ["OM"]}) is None


def test_desk_geo_sql_shape() -> None:
    from legba.data.signal_slice_scope import desk_geo_sql, homonym_slice_filter

    sql = desk_geo_sql(3)
    assert sql.startswith("geo && $3::text[] OR (tags @> ARRAY['at_sea']::text[]")
    assert "ANY($3::text[])" in sql and "$4" not in sql
    # The GEO-v2 recovery leg's marker survives the wrap.
    assert f"NOT ({sql})".startswith("NOT (geo &&")
    hf = homonym_slice_filter(["GE", "UA"])
    if hf.active:
        plain, unfiled = hf.sql([]), hf.sql([], admit_unfiled=True)
        assert unfiled[0] == "(geo = '{}'::text[] OR " + plain[0][1:]


async def _slice(conn, body: dict[str, Any]) -> dict[Any, dict[str, Any]]:
    from legba.runtime.actor_substrate_slice import _read_substrate_slice
    from tests.runtime.test_thematic_target_slice import _minimal_inline_descriptor

    rows = await _read_substrate_slice(
        _TargetConn(conn, body), descriptor=_minimal_inline_descriptor(), target_filter="desk")
    return {r["id"]: r for r in rows if r.get("id") is not None}


async def test_persisted_at_sea_rows_reach_the_desks_of_their_coasts(conn) -> None:
    """An at-sea incident reaches the country desks and shipping-lane desks of
    the coasts it lies off, marked as at sea in the rendered row; storage is
    unchanged; an open-ocean incident reaches no country desk; an ordinary
    in-country row renders with no location line."""
    from legba.data.analysts.slice_render import _render_signal, at_sea_line
    from legba.runtime.sinks.signal import write_canonical_signal

    tenant = f"ns_sea_{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    at_sea = await _geocoded(_ukmto_payload(), fetched_at=now, content_hash=f"sea-{uuid4().hex}")
    mandeb = await _geocoded(_ukmto_payload(*BAB_EL_MANDEB), fetched_at=now,
                             content_hash=f"mandeb-{uuid4().hex}")
    ocean = await _geocoded(_ukmto_payload(*OPEN_OCEAN), fetched_at=now,
                            content_hash=f"ocean-{uuid4().hex}")
    assert ocean.payload["geo"]["inferred"] == []
    # Control: the same incident text filed under Oman by an ordinary geocode.
    on_land = Signal(source_id="source.ns.control", fetched_at=now, geo=["OM"],
                     content_hash=f"land-{uuid4().hex}",
                     payload={"title": "Attack: Strait of Hormuz — Tanker",
                              "geo": {"country_iso2": "OM"}})
    try:
        for s in (at_sea, mandeb, ocean, on_land):
            assert await write_canonical_signal(conn, s, source_version="v", owner_tenant=tenant)
        row = await conn.fetchrow("SELECT geo, tags, payload FROM signals WHERE id = $1",
                                  at_sea.signal_id)
        payload = json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
        # Storage is unchanged: no filing, the tag, the relation.
        assert row["geo"] == [] and prec.AT_SEA_TAG in row["tags"]
        stored = payload["geo"]["inferred"]
        assert stored[0]["iso2"] == "OM" and stored[0]["basis"] == "nearest_coast"

        desks = {iso: {"scope": {"geo": [iso]}, "sources": []} for iso in ("OM", "AE", "IR")}
        hormuz = _descriptor_body("target_lane_hormuz.yaml")
        red_sea = _descriptor_body("target_lane_red_sea.yaml")
        for name, body in [*desks.items(), ("lane_hormuz", hormuz)]:
            got = await _slice(conn, body)
            assert at_sea.signal_id in got, name
            assert mandeb.signal_id not in got and ocean.signal_id not in got, name
            r = got[at_sea.signal_id]
            assert r["geo"] == [], name                    # never a filing in the slice
            line = at_sea_line(r)
            assert line is not None, name
            assert line.startswith("    location=at sea (Strait of Hormuz), about ")
            assert "km off Oman" in line and "not an event in Oman" in line
            assert line in _render_signal(1, r)
        oman = await _slice(conn, desks["OM"])
        assert on_land.signal_id in oman
        assert on_land.signal_id not in await _slice(conn, desks["IR"])
        # The in-country row renders with no location line.
        assert at_sea_line(oman[on_land.signal_id]) is None
        assert "location=" not in _render_signal(1, oman[on_land.signal_id])
        # The Red Sea lane admits the Bab el-Mandeb incident, not Hormuz.
        got = await _slice(conn, red_sea)
        assert mandeb.signal_id in got and at_sea.signal_id not in got
        assert "not an event in" in (at_sea_line(got[mandeb.signal_id]) or "")
        # Open ocean: a desk of any coast never sees it.
        for iso in ("OM", "AE", "IR", "YE", "DJ", "ER", "US", "PT", "MA"):
            assert ocean.signal_id not in await _slice(
                conn, {"scope": {"geo": [iso]}, "sources": []}), iso
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)


async def test_watermark_offers_an_at_sea_row_to_one_run_only(conn) -> None:
    """The desk watermark treats an at-sea row like any other: in the window
    once, then excluded from the next run's overlap by its id."""
    from datetime import timedelta

    from legba.data.signal_slice_scope import desk_geo_sql
    from legba.data.slice_coverage import SliceWindow, window_clauses
    from legba.runtime.sinks.signal import write_canonical_signal

    tenant = f"ns_sea_{uuid4().hex[:8]}"
    now = datetime.now(tz=timezone.utc)
    at_sea = await _geocoded(_ukmto_payload(), fetched_at=now, content_hash=f"wm-{uuid4().hex}")
    try:
        assert await write_canonical_signal(conn, at_sea, source_version="v", owner_tenant=tenant)
        for seen, expect in (([], 1), ([str(at_sea.signal_id)], 0)):
            params: list[Any] = []
            window = SliceWindow(start=now - timedelta(hours=1), end=now, basis="watermark",
                                 seen_ids=seen)
            clauses = window_clauses(window, params)
            params.append(["OM"])
            clauses.append(f"({desk_geo_sql(len(params))})")
            params.append(tenant)
            clauses.append(f"owner_tenant = ${len(params)}")
            n = await conn.fetchval(
                f"SELECT count(*) FROM signals WHERE {' AND '.join(clauses)}", *params)
            assert n == expect, seen
    finally:
        await conn.execute("DELETE FROM signals WHERE owner_tenant = $1", tenant)

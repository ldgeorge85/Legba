# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W9 VIEW-1 — the per-profile projection, end to end on the real driver.

The four parts (Review A §2.2d, the ``graph_arcs`` precedent):

  * the view (``profiles/<id>/projection/view.yaml``) loads through a CLOSED
    model, and the packaged export (``profile_projection.json``) is every
    profile's view resolved against its manifest — stale is red here;
  * the builder (``deterministic_handlers/profile_projector.py``) fills
    ``profile_projection`` from the view + a live-shaped substrate (the frame
    family and its member heads, each unit's own descriptor head, the
    crosswalk rows), deterministically, one plane per transaction, with the
    ``_meta`` receipt carrying ``projected_at``;
  * the route (``registry/projection_api.py``) refuses BY NAME —
    ``projection_disabled`` (flag off, and a profile with no view),
    ``projection_empty``, ``projection_stale`` (with its threshold),
    ``plane_unknown``, ``section_undeclared`` — through the real binding path
    (router factory + deps + ASGI), never ``found=False``;
  * PLANE ISOLATION: two planes on one table never bleed — a rebuild of one
    leaves the other byte-for-byte, and a read of one never returns the other's
    rows;
  * the CROSSWALK resolves a profile value and leaves an unmapped one alone;
  * the ONE reader: the absence route's scope resolution, flag ON over a fresh
    plane equals flag OFF on today's data (byte-identical output), and the read
    is real (a different scope table changes the answer).

Every row this module writes is deleted in its teardown (the W7-F5 / W9 rule).
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from legba.data import profile_projection as pp
from legba.data.analysts.deterministic_handlers import profile_projector
from legba.data.config import PostgresConfig
from legba.data.profile import PROFILE_ENV, ProfileError, load_profile
from legba.data.registry import absence_api
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.projection_api import build_projection_router
from legba.data.registry.source_freshness import cadence_interval_minutes

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO = REPO_ROOT / "profiles" / "geopolitics"
SANCTIONS = REPO_ROOT / "profiles" / "sanctions_export_control"
UI_SRC = REPO_ROOT / "legba-ui-v3" / "src"
_SRC = str(REPO_ROOT / "src")

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import gen_profile_projection  # noqa: E402

_ROUTE = "/api/v1/registry/projection"
#: The test's own synthetic plane (never a profile's).
PLANE_B = "viewtest_b"
#: Owner/version on every descriptor row this module writes.
_OWNER = "test_view1_projection"
_VER = "view1v1"
#: Target heads this module seeds (live-shaped ids, unique to this module).
TARGETS = {
    "country_g20_v1zz": ["g20", "region_europe"],
    "country_watch_v1zy": ["watch", "region_mena"],
}
#: The absence-equality desk.
DESK = "country_watch_view1absence"


# ---------------------------------------------------------------------------
# Pure — the view, the export, the crosswalk, the flag
# ---------------------------------------------------------------------------


def test_the_packaged_export_is_every_profiles_view():
    """The images carry ``src/`` only: they read the packaged catalog. A view
    or manifest edit without a regenerate is red here."""
    assert gen_profile_projection.OUT == pp.PACKAGED_EXPORT
    assert pp.PACKAGED_EXPORT.read_text(encoding="utf-8") == gen_profile_projection.render(None), (
        "stale: run PYTHONPATH=src python3 scripts/gen_profile_projection.py"
    )
    assert gen_profile_projection.main(["--check"]) == 0
    doc = json.loads(pp.PACKAGED_EXPORT.read_text(encoding="utf-8"))
    assert doc["schema"] == pp.EXPORT_SCHEMA
    assert doc["default_plane"] == "geopolitics"
    assert set(doc["planes"]) == {"geopolitics", "sanctions_export_control"}
    # the sanctions skeleton declares no projection: a plane with no view
    assert doc["planes"]["sanctions_export_control"]["view"] is None
    geo = doc["planes"]["geopolitics"]["view"]
    assert set(geo["sections"]) == set(pp.SECTIONS)
    assert geo["derive"] == {
        "subject_groups": {"kinds": ["region", "thematic"]},
        "units": {"cadence_from": "descriptor_head"},
        "crosswalk": {"namespace": "geopolitics"},
    }


def _write_view(tmp_path: pathlib.Path, body: dict) -> pathlib.Path:
    data = yaml.safe_load((GEO / "profile.yaml").read_text(encoding="utf-8"))
    data.update(descriptors_dir="descriptors", lifecycle=None, prompts=None)
    data["rosters"].pop("source_catalog", None)
    data["rosters"].pop("gazetteer", None)  # CANON-1: the gazetteer file is not copied
    data.pop("constants", None)  # SPLIT-3: inline instance data the copy does not need (validated against ui.frames)
    data.pop("subjects", None)  # SUBJECT-1: the label style; the platform default applies
    d = tmp_path / "geopolitics"
    (d / "descriptors").mkdir(parents=True)
    (d / "projection").mkdir()
    (d / "projection" / "view.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")
    (d / "profile.yaml").write_text(yaml.safe_dump(data), encoding="utf-8")
    return d


@pytest.mark.parametrize("body, err", [
    ({"schema": "legba/projection_view/1", "no_such_key": 1}, "no_such_key"),
    ({"schema": "legba/projection_view/9"}, "schema"),
    ({"schema": "legba/projection_view/1", "tiers": "ui.somewhere"}, "tiers"),
    ({"schema": "legba/projection_view/1",
      "bands": [{"id": "a", "label": "A", "question": "q"}, {"id": "a", "label": "B", "question": "q"}]},
     "repeat"),
    ({"schema": "legba/projection_view/1",
      "scope": {"banding_analyst": "x", "audit_analyst": "y", "unit_grace_multiple": 0}}, "greater"),
])
def test_the_view_is_closed(tmp_path, body, err):
    with pytest.raises(ValidationError, match=err):
        pp.load_view(load_profile(_write_view(tmp_path, body)))


def test_a_profile_reads_only_its_own_crosswalk_namespace(tmp_path):
    prof = load_profile(_write_view(tmp_path, {
        "schema": "legba/projection_view/1", "crosswalk": {"namespace": "sanctions"},
    }))
    with pytest.raises(ProfileError, match="not the profile's own"):
        pp.resolve_plane(prof)


def test_two_profiles_cannot_declare_one_plane():
    geo = load_profile(GEO)
    with pytest.raises(ProfileError, match="declared by both"):
        pp.export_planes([geo, geo], default_plane="geopolitics")


def _ts(rel: str) -> str:
    return (UI_SRC / rel).read_text(encoding="utf-8")


def test_the_geopolitics_view_reproduces_todays_literals():
    """The plane's static sections ARE today's constants: the Morning Read's
    BANDS, the mobile tier order/labels/producers, the gap strip's units and
    grace, and the absence route's own scope constants."""
    view = pp.resolve_plane(load_profile(GEO))["view"]
    sec = {s: [(r["key"], r["value"]) for r in rows] for s, rows in view["sections"].items()}

    bands_ts = re.search(r"export const BANDS: readonly BandSpec\[\] = \[(.*?)\n\]",
                         _ts("lib/morningReadBands.ts"), re.S).group(1)
    ts_bands = re.findall(r"\{ id: '([a-z]+)', label: '([^']+)', question: '([^']+)' \}", bands_ts)
    assert ts_bands and [(k, v["label"], v["question"]) for k, v in sec["bands"]] == ts_bands

    # UI-B2 inverted the tier half: the phone surface no longer spells the
    # tiers (mobile/mobileModel.ts reads the profile's frames through
    # readTiers(), or this plane's `tiers` when it answers), so the section is
    # checked against the manifest the UI export carries — and today's order,
    # labels and producers stay pinned byte for byte here.
    frames = load_profile(GEO).ui.frames
    assert [k for k, _ in sec["tiers"]] == [f.id for f in frames] == [
        "world", "assessment", "region", "country", "thematic"]
    assert {k: v["label"] for k, v in sec["tiers"]} == {f.id: f.label for f in frames} == {
        "world": "World", "assessment": "Assessment", "region": "Region",
        "country": "Country", "thematic": "Thematic"}
    assert {k: v["producer"] for k, v in sec["tiers"]} == {f.id: f.producer for f in frames} == {
        "world": "world_assessor", "assessment": "world_assessment",
        "region": "region_composition", "country": "country_composition",
        "thematic": "escalation_composition"}
    mobile = _ts("mobile/mobileModel.ts")
    assert "readTiers()" in mobile and "TIER_ORDER" not in mobile and "READ_ANALYSTS" not in mobile

    scope = dict(sec["scope"])
    assert tuple(scope["units"]) == absence_api.BOUNDED_UNITS
    assert scope["banding_analyst"] == absence_api.BANDING_ANALYST_ID
    assert scope["audit_analyst"] == absence_api.AUDIT_ANALYST_ID
    assert scope["unit_grace_multiple"] == absence_api.UNIT_GRACE_MULTIPLE
    strip = _ts("lib/gapStripModel.ts")
    assert float(re.search(r"GAP_STRIP_GRACE_MULTIPLE = ([0-9.]+)", strip).group(1)) == scope["unit_grace_multiple"]
    assert [k for k, _ in sec["units"]] == list(absence_api.BOUNDED_UNITS)

    lab = dict(sec["labels"])
    assert lab["read_title"] == "World Assessment" and lab["map.title"] == "World Map"
    assert lab["unscoped_subject"] == "No desk (world / unscoped)"
    assert lab["landing"] == "morning_read"


def test_crosswalk_resolves_a_profile_value_and_leaves_an_unmapped_one_alone():
    seed = yaml.safe_load((REPO_ROOT / "descriptors" / "vocabulary_crosswalk.yaml").read_text())
    assert seed["family"] == pp.CROSSWALK_FAMILY
    pairs = [pp.crosswalk_pair(e["value"], e["parent"], e.get("notes")) for e in seed["entries"]]
    assert all(pairs)
    assert pp.resolve_crosswalk(pairs, "geopolitics.band", "elevated") == ("band", "elevated")
    assert pp.resolve_crosswalk(pairs, "geopolitics.source_layer", "official") == ("source_layer", "official")
    # unmapped: no row → None, the caller leaves the value alone
    assert pp.resolve_crosswalk(pairs, "geopolitics.band", "critical") is None
    assert pp.resolve_crosswalk(pairs, "sanctions.programme", "SDGT") is None
    # a row that is not a well-formed pair is skipped, never guessed at
    for value, parent in [("x:y", "band:elevated"), ("geopolitics.band:elevated", "geopolitics.band:x"),
                          ("geopolitics.band:elevated", None), ("geopolitics.band:", "band:x"),
                          ("geopolitics.band:elevated", "crosswalk:x")]:
        assert pp.crosswalk_pair(value, parent) is None


def test_flag_and_max_age_parse(monkeypatch):
    monkeypatch.delenv(pp.PROFILE_PROJECTION_ENV, raising=False)
    assert pp.profile_projection_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, on)
        assert pp.profile_projection_enabled() is True
    monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "0")
    assert pp.profile_projection_enabled() is False
    monkeypatch.delenv(pp.PROFILE_PROJECTION_MAX_AGE_ENV, raising=False)
    assert pp.projection_max_age_seconds() == pp.DEFAULT_MAX_AGE_SECONDS == 7200.0
    monkeypatch.setenv(pp.PROFILE_PROJECTION_MAX_AGE_ENV, "60")
    assert pp.projection_max_age_seconds() == 60.0
    monkeypatch.setenv(pp.PROFILE_PROJECTION_MAX_AGE_ENV, "nonsense")
    assert pp.projection_max_age_seconds() == 7200.0


class _NoQueryConn:
    """Any query is a failure: flag off must issue none."""

    def __getattr__(self, name):
        raise AssertionError(f"flag off issued conn.{name}")


async def test_scope_resolution_flag_off_issues_no_query(monkeypatch):
    monkeypatch.delenv(pp.PROFILE_PROJECTION_ENV, raising=False)
    default = absence_api._SCOPE_CONSTANTS
    assert await pp.scope_resolution(_NoQueryConn(), default=default) is default


def test_legba_profile_selects_its_own_plane_from_disk():
    cat = pp._select(env={PROFILE_ENV: str(SANCTIONS)})
    assert cat.default_plane == "sanctions_export_control"
    assert cat.view("sanctions_export_control") is None
    assert pp._select(env={}).origin.startswith("packaged:")


def test_the_route_and_the_leaf_import_without_the_runtime_stack() -> None:
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.registry.projection_api as api
        import legba.data.profile_projection as leaf
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, "analyst/runtime package reachable: %r" % leaked
        assert api.PROJECTION_API_VERSION == "2026-10/view1"
        assert leaf.plane_catalog().default_plane == "geopolitics"
        print("OK")
        """
    )
    env = {k: v for k, v in os.environ.items() if k != PROFILE_ENV}
    env["PYTHONPATH"] = os.pathsep.join(p for p in (_SRC, os.environ.get("PYTHONPATH", "")) if p)
    result = subprocess.run([sys.executable, "-c", probe], capture_output=True, text=True,
                            timeout=120, env=env)
    assert result.returncode == 0, f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
    assert "OK" in result.stdout


def test_the_projector_is_a_registered_deterministic_sub_handler():
    from legba.data.analysts import deterministic
    from legba.data.option_catalog.handler_options import known_option_names
    from legba.data.provenance.kinds import TRACE_ONLY

    assert deterministic.SUB_HANDLERS["profile_projector"] is profile_projector.handle
    assert deterministic.OUTPUT_KIND_BY_SUB_HANDLER["profile_projector"] is TRACE_ONLY
    assert known_option_names("profile_projector") == ("dry_run",)
    body = yaml.safe_load((REPO_ROOT / "descriptors" / "analyst_profile_projector.yaml").read_text())
    assert body["identity"]["state"] == "draft"
    assert body["method"]["budget_tokens_per_day"] == 0
    assert body["method"]["sub_handler"] == "profile_projector"


# ---------------------------------------------------------------------------
# The real driver — fixtures (every row written here is deleted in teardown)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig, monkeypatch):
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    monkeypatch.delenv(pp.PROFILE_PROJECTION_MAX_AGE_ENV, raising=False)
    monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "1")
    pp.refresh()
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=3)
    planes = ["geopolitics", PLANE_B]
    vocab_added: list[tuple[str, str]] = []
    await _purge(p, planes)
    try:
        yield SimpleNamespace(pool=p, vocab_added=vocab_added, planes=planes)
    finally:
        await _purge(p, planes)
        async with p.acquire() as conn:
            for fam, val in vocab_added:
                await conn.execute(
                    "DELETE FROM vocabulary_entries WHERE family = $1 AND value = $2", fam, val)
        await p.close()
        pp.refresh()


async def _purge(p, planes) -> None:
    async with p.acquire() as conn:
        await conn.execute("DELETE FROM public.profile_projection WHERE plane = ANY($1::text[])", planes)
        await conn.execute("DELETE FROM public.target_descriptors WHERE owner = $1", _OWNER)
        await conn.execute("DELETE FROM public.analyst_descriptors WHERE owner = $1", _OWNER)
        await conn.execute("DELETE FROM public.analyst_outputs WHERE target_id = $1", DESK)


async def _seed_vocab(env, family: str, value: str, *, parent=None, aliases=(), notes=None) -> None:
    async with env.pool.acquire() as conn:
        added = await conn.fetchval(
            "INSERT INTO vocabulary_entries (family, value, schema_uri, introduced, notes, aliases, parent) "
            "VALUES ($1, $2, 'legba/vocabulary/1.0.0', now(), $3, $4::text[], $5) "
            "ON CONFLICT (family, value) DO NOTHING RETURNING value",
            family, value, notes, list(aliases), parent,
        )
    if added is not None:
        env.vocab_added.append((family, value))


async def _seed_substrate(env) -> None:
    """A live-shaped substrate: the committed frame + crosswalk seed files, two
    member desks tagged like the live ones, and a descriptor head for every
    unit but one (live cadence: "0 1,13 * * *")."""
    for name in ("vocabulary_frame.yaml", "vocabulary_crosswalk.yaml"):
        seed = yaml.safe_load((REPO_ROOT / "descriptors" / name).read_text(encoding="utf-8"))
        for e in seed["entries"]:
            await _seed_vocab(env, seed["family"], e["value"], parent=e.get("parent"),
                              aliases=e.get("aliases") or (), notes=e.get("notes"))
    async with env.pool.acquire() as conn:
        for tid, tags in TARGETS.items():
            await conn.execute(
                "INSERT INTO public.target_descriptors (descriptor_id, version, schema_uri, is_head, "
                "state, owner, name, body) VALUES ($1, $2, 'legba/target/2.0.0', TRUE, 'active', $3, $1, "
                "$4::jsonb) ON CONFLICT DO NOTHING",
                tid, _VER, _OWNER, json.dumps({"scope": {"tags": tags}}),
            )
        for unit in absence_api.BOUNDED_UNITS[:-1]:
            await _analyst_head(conn, unit, "0 1,13 * * *")


async def _analyst_head(conn, analyst: str, cron: str) -> None:
    await conn.execute(
        "INSERT INTO public.analyst_descriptors (descriptor_id, version, schema_uri, is_head, kind, "
        "state, owner, name, body) VALUES ($1, $2, 'legba/analyst/2.0.0', TRUE, 'inline_target', "
        "'active', $3, $1, $4::jsonb) ON CONFLICT DO NOTHING",
        analyst, _VER, _OWNER, json.dumps({"cadence": {"fallback_schedule": cron}}),
    )


def _geo_entry() -> dict:
    return pp.plane_catalog().entry("geopolitics")


async def _rows(env, plane: str) -> list[tuple]:
    async with env.pool.acquire() as conn:
        return [tuple(r) for r in await conn.fetch(
            "SELECT section, key, ord, value::text, projected_at FROM public.profile_projection "
            "WHERE plane = $1 ORDER BY section, ord, key", plane)]


# ---------------------------------------------------------------------------
# The builder
# ---------------------------------------------------------------------------


@pytest.mark.integration
async def test_the_builder_fills_the_plane_from_the_view_and_the_substrate(pool):
    await _seed_substrate(pool)
    async with pool.pool.acquire() as conn:
        receipt = await profile_projector.build_plane(conn, _geo_entry())
        rows = await pp.read_plane(conn, "geopolitics")
        state, meta = await pp.projection_state(conn, "geopolitics")
    assert state == pp.PROJECTION_OK
    assert meta["row_count"] == receipt["row_count"] == sum(len(v) for v in rows.values())
    assert meta["projected_at"] is not None and meta["age_seconds"] < 60
    assert meta["view_digest"] == _geo_entry()["view"]["digest"]
    assert set(rows) == set(pp.SECTIONS)

    # static sections: exactly what the view resolved, in its order
    static = _geo_entry()["view"]["sections"]
    for s in ("tiers", "labels", "bands", "scope"):
        assert [(r["key"], r["value"]) for r in rows[s]] == [(r["key"], r["value"]) for r in static[s]]
        assert [r["ord"] for r in rows[s]] == list(range(len(static[s])))

    # units: the roster, each with its OWN head's cadence — null, never a
    # guessed default, for the unit with no head (named in the receipt)
    units = {r["key"]: r["value"] for r in rows["units"]}
    assert list(units) == list(absence_api.BOUNDED_UNITS)
    async with pool.pool.acquire() as conn:
        heads = {r["descriptor_id"]: r["s"] for r in await conn.fetch(
            "SELECT descriptor_id, body->'cadence'->>'fallback_schedule' AS s FROM analyst_descriptors "
            "WHERE is_head AND descriptor_id = ANY($1::text[])", list(units))}
    for unit, v in units.items():
        assert v["schedule"] == heads.get(unit)
        want = cadence_interval_minutes(heads[unit]) / 60.0 if heads.get(unit) else None
        assert v["cadence_hours"] == (round(want, 4) if want else None)
    last = absence_api.BOUNDED_UNITS[-1]
    if last not in heads:
        assert units[last]["cadence_hours"] is None
        assert last in meta["unmeasured"]["units"]
    assert units["escalation"]["scored"] is True and units[last]["scored"] is False

    # subject groups: the frame family's region + thematic subtrees with members
    groups = {r["key"]: r["value"] for r in rows["subject_groups"]}
    assert {"region", "region_mena", "region_europe", "thematic", "escalation_composition"} <= set(groups)
    assert groups["region_mena"]["parent"] == "region"
    assert groups["region_mena"]["member_rule"] == ["tag:region_mena"]
    assert "country_watch_v1zy" in groups["region_mena"]["members"]
    assert "country_g20_v1zz" in groups["region_europe"]["members"]
    assert "country_g20_v1zz" not in groups["region_mena"]["members"]
    assert {"country_g20_v1zz", "country_watch_v1zy"} <= set(groups["region"]["members"])
    assert groups["escalation_composition"]["member_analysts"] == ["escalation_composition"]
    assert groups["escalation_composition"]["members"] == []

    # the crosswalk rows under the profile's own namespace
    cw = {r["key"]: r["value"] for r in rows["crosswalk"]}
    assert cw["geopolitics.band:elevated"]["to_family"] == "band"
    assert cw["geopolitics.band:elevated"]["to_value"] == "elevated"
    assert all(k.startswith("geopolitics.") for k in cw)


@pytest.mark.integration
async def test_two_builds_of_one_snapshot_are_row_identical(pool):
    await _seed_substrate(pool)
    async with pool.pool.acquire() as conn:
        await profile_projector.build_plane(conn, _geo_entry())
        first = await _rows(pool, "geopolitics")
        await profile_projector.build_plane(conn, _geo_entry())
        second = await _rows(pool, "geopolitics")

    def strip(rows):  # projected_at and the receipt's build_seconds move
        out = []
        for section, key, ord_, value, _at in rows:
            if section == pp.META_SECTION:
                value = json.loads(value)
                value.pop("build_seconds")
            out.append((section, key, ord_, value))
        return out

    assert strip(first) == strip(second)
    # one build, one projected_at on every row
    assert len({r[4] for r in second}) == 1


@pytest.mark.integration
async def test_dry_run_writes_nothing_and_flag_off_is_a_no_op(pool, monkeypatch):
    deps = SimpleNamespace(pg_pool=pool.pool)
    res = await profile_projector.handle([], {"dry_run": True}, deps)
    data = res.finding.data
    assert data["dry_run"] is True and [r["plane"] for r in data["planes"]] == ["geopolitics"]
    assert data["planes_without_view"] == ["sanctions_export_control"]
    assert await _rows(pool, "geopolitics") == []
    monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "0")
    res = await profile_projector.handle([], {}, deps)
    assert res.finding.data["projection_enabled"] is False
    assert await _rows(pool, "geopolitics") == []
    monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "1")
    res = await profile_projector.handle([], {}, deps)
    assert res.finding.data["planes"][0]["row_count"] > 0
    assert await _rows(pool, "geopolitics") != []


def _plane_b_entry(question: str) -> dict:
    return {"profile": "viewtest", "plane": PLANE_B, "view": {
        "digest": f"b-{question}",
        "sections": {"bands": [{"key": "only", "value": {"label": "Only", "question": question}}]},
        "derive": {},
    }}


@pytest.mark.integration
async def test_two_planes_on_one_table_never_bleed(pool):
    await _seed_substrate(pool)
    async with pool.pool.acquire() as conn:
        await profile_projector.build_plane(conn, _plane_b_entry("first?"))
        await profile_projector.build_plane(conn, _geo_entry())
        b_before = await _rows(pool, PLANE_B)
        # rebuilding A leaves B byte-for-byte; rebuilding B leaves A
        await profile_projector.build_plane(conn, _geo_entry())
        assert await _rows(pool, PLANE_B) == b_before
        a_before = await _rows(pool, "geopolitics")
        await profile_projector.build_plane(conn, _plane_b_entry("second?"))
        assert await _rows(pool, "geopolitics") == a_before
        a = await pp.read_plane(conn, "geopolitics")
        b = await pp.read_plane(conn, PLANE_B)
    assert set(b) == {"bands"} and b["bands"][0]["value"]["question"] == "second?"
    assert [r["key"] for r in a["bands"]] == ["changed", "checked", "gaps", "due"]
    assert "only" not in {r["key"] for r in a["bands"]}


# ---------------------------------------------------------------------------
# The route — the real binding path; refusals by name
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def client(pool):
    os.environ.pop(API_TOKEN_ENV, None)  # dev mode — any bearer accepted
    deps = SimpleNamespace(descriptor_registry=SimpleNamespace(pg=pool.pool))
    app = FastAPI()
    app.state.registry_deps = deps
    app.include_router(build_projection_router(deps), prefix="/api/v1/registry")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as c:
        yield c


@pytest.mark.integration
async def test_refusal_projection_disabled_by_the_flag(client, monkeypatch):
    monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "0")
    r = await client.get(f"{_ROUTE}/geopolitics")
    assert r.status_code == 409
    assert r.json()["detail"]["state"] == "projection_disabled"
    assert pp.PROFILE_PROJECTION_ENV in r.json()["detail"]["reason"]


@pytest.mark.integration
async def test_refusal_projection_disabled_for_a_profile_with_no_view(client):
    r = await client.get(f"{_ROUTE}/sanctions_export_control")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["state"] == "projection_disabled"
    assert "declares no projection" in d["reason"]


@pytest.mark.integration
async def test_refusal_plane_unknown_names_the_known_planes(client):
    r = await client.get(f"{_ROUTE}/no_such_plane")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["state"] == "plane_unknown"
    assert d["planes"] == ["geopolitics", "sanctions_export_control"]


@pytest.mark.integration
async def test_refusal_projection_empty_before_the_first_build(client):
    for path in (f"{_ROUTE}/geopolitics", f"{_ROUTE}/geopolitics/scope"):
        r = await client.get(path)
        assert r.status_code == 409
        assert r.json()["detail"]["state"] == "projection_empty"


@pytest.mark.integration
async def test_refusal_projection_stale_states_its_threshold(client, pool, monkeypatch):
    async with pool.pool.acquire() as conn:
        await profile_projector.build_plane(conn, _geo_entry())
        await conn.execute(
            "UPDATE public.profile_projection SET projected_at = now() - interval '3 hours' "
            "WHERE plane = 'geopolitics'")
    r = await client.get(f"{_ROUTE}/geopolitics/bands")
    assert r.status_code == 409
    d = r.json()["detail"]
    assert d["state"] == "projection_stale"
    assert d["max_age_seconds"] == 7200.0 and d["age_seconds"] > 7200
    assert pp.PROFILE_PROJECTION_MAX_AGE_ENV in d["reason"] and "7200" in d["reason"]
    # the threshold is the env's: a wider one reads it fresh again
    monkeypatch.setenv(pp.PROFILE_PROJECTION_MAX_AGE_ENV, str(4 * 3600))
    assert (await client.get(f"{_ROUTE}/geopolitics/bands")).status_code == 200


@pytest.mark.integration
async def test_a_fresh_plane_reads_with_its_build_stamp(client, pool):
    async with pool.pool.acquire() as conn:
        await profile_projector.build_plane(conn, _geo_entry())
    r = await client.get(f"{_ROUTE}/geopolitics")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["state"] == "ok" and body["plane"] == "geopolitics" and body["profile"] == "geopolitics"
    assert body["projected_at"] and body["age_seconds"] is not None
    assert body["max_age_seconds"] == 7200.0
    assert body["view_digest"] == _geo_entry()["view"]["digest"]
    assert list(body["sections"]) == list(pp.SECTIONS)
    assert [row["key"] for row in body["sections"]["bands"]] == ["changed", "checked", "gaps", "due"]

    r = await client.get(f"{_ROUTE}/geopolitics/tiers")
    assert r.status_code == 200
    tiers = r.json()
    assert tiers["section"] == "tiers"
    assert [(row["key"], row["value"]["label"]) for row in tiers["rows"]][:2] == [
        ("world", "World"), ("assessment", "Assessment")]

    r = await client.get(f"{_ROUTE}/geopolitics/no_such_section")
    assert r.status_code == 404
    d = r.json()["detail"]
    assert d["state"] == "section_undeclared" and d["sections"] == list(pp.SECTIONS)


@pytest.mark.integration
async def test_an_unmeasured_section_says_so_rather_than_reading_empty(client, pool, monkeypatch):
    """A section the build could not measure is a 200 with its reason, never
    a quiet empty list: here the view's kinds are not registered at all."""
    async with pool.pool.acquire() as conn:
        # an EMPTY family is named too, before any frame row exists
        empty = await profile_projector.build_plane(conn, _geo_entry(), dry_run=True)
    if "subject_groups" in empty["unmeasured"]:
        assert "frame family is" in empty["unmeasured"]["subject_groups"]
    await _seed_substrate(pool)
    entry = json.loads(json.dumps(_geo_entry()))
    entry["view"]["derive"]["subject_groups"] = {"kinds": ["viewtest_no_such_kind"]}
    async with pool.pool.acquire() as conn:
        receipt = await profile_projector.build_plane(conn, entry)
    assert "subject_groups" in receipt["unmeasured"]
    r = await client.get(f"{_ROUTE}/geopolitics/subject_groups")
    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == [] and "viewtest_no_such_kind" in body["unmeasured"]


# ---------------------------------------------------------------------------
# The ONE reader: the absence route's scope resolution
# ---------------------------------------------------------------------------


async def _seed_desk(conn, now: datetime) -> None:
    """A desk with one stale unit, one current unit and a scorecard — every
    unit kind the scope table shapes is exercised."""
    await conn.execute(
        "INSERT INTO public.analyst_outputs (id, kind, title, body, confidence, data, target_id, "
        "analyst_id, produced_at, schema_uri) VALUES ($1, 'scorecard', 'view1 card', '', 1.0, "
        "'{\"data\": {\"bands\": {\"dimensions\": {}}}}'::jsonb, $2, $3, $4, "
        "'iglu:legba/scorecard/jsonschema/1-0-0')",
        uuid4(), DESK, absence_api.BANDING_ANALYST_ID, now - timedelta(hours=3),
    )
    for unit, age in ((absence_api.BOUNDED_UNITS[0], 40), (absence_api.BOUNDED_UNITS[1], 1)):
        await conn.execute(
            "INSERT INTO public.analyst_outputs (id, kind, title, body, confidence, data, target_id, "
            "analyst_id, produced_at, schema_uri) VALUES ($1, 'finding', 'view1 read', '', 0.8, "
            "'{}'::jsonb, $2, $3, $4, 'iglu:legba/finding/jsonschema/1-0-0')",
            uuid4(), DESK, unit, now - timedelta(hours=age),
        )
    await _analyst_head(conn, absence_api.BANDING_ANALYST_ID, "40 4 * * *")


@pytest.mark.integration
async def test_the_absence_scope_read_flag_on_equals_flag_off_on_todays_data(pool, monkeypatch):
    await _seed_substrate(pool)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    async with pool.pool.acquire() as conn:
        await _seed_desk(conn, now)
        monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "0")
        off = await absence_api.read_absences(conn, scope=DESK, now=now, limit_per_kind=50)

        monkeypatch.setenv(pp.PROFILE_PROJECTION_ENV, "1")
        await profile_projector.build_plane(conn, _geo_entry())
        table = await pp.scope_resolution(conn, default=absence_api._SCOPE_CONSTANTS, now=now)
        assert table.origin.startswith("projection:geopolitics@"), table
        on = await absence_api.read_absences(conn, scope=DESK, now=now, limit_per_kind=50)
        assert on.model_dump_json() == off.model_dump_json()
        assert {a.subject for a in on.absences if a.kind == "not_collected"} == set(
            absence_api.BOUNDED_UNITS[2:])

        # the read is REAL: a scope table naming fewer units changes the answer
        await conn.execute(
            "UPDATE public.profile_projection SET value = $1::jsonb "
            "WHERE plane = 'geopolitics' AND section = 'scope' AND key = 'units'",
            json.dumps(list(absence_api.BOUNDED_UNITS[:3])),
        )
        narrowed = await absence_api.read_absences(conn, scope=DESK, now=now, limit_per_kind=50)
        assert {a.subject for a in narrowed.absences if a.kind == "not_collected"} == {
            absence_api.BOUNDED_UNITS[2]}

        # a stale plane falls back to the constants — the flag-off answer again
        await conn.execute(
            "UPDATE public.profile_projection SET projected_at = now() - interval '3 hours' "
            "WHERE plane = 'geopolitics'")
        stale = await absence_api.read_absences(conn, scope=DESK, now=now, limit_per_kind=50)
        assert stale.model_dump_json() == off.model_dump_json()

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""PROFILE-1 — the instance profile manifest, its resolver, and what reads it.

``src/legba/data/profile.py`` loads ``profiles/<id>/profile.yaml``; the
descriptor walker and ``deploy/deploy.sh`` take ``--profile``; the default is
``profiles/geopolitics``, which must reproduce today's bring-up exactly. This
file proves:

  * both profiles load, and the closed model refuses an unknown key at every
    level (plus the other load-time refusals);
  * the resolver's precedence: ``--profile`` > ``LEGBA_PROFILE`` > default;
  * the geopolitics profile IS today's tree: its walker plan equals the
    walker's own default plan, its seeds are deploy.sh's old literal, its
    rosters are what the code reads (ROSTER-1: ``legba.data.rosters``; the
    code spells no copy);
  * the geopolitics ``profile_ui.json`` export equals a committed golden, every
    string of which is the PROFILE's (UI-B2 inverted the check: the TypeScript
    reads the export and spells no label of its own), and the committed
    ``lib/profile_ui.json`` is that export;
  * the sanctions skeleton validates, and a ``--dry-run`` of the walker with
    ``--profile profiles/sanctions_export_control`` against an EMPTY, freshly
    migrated database plans every one of its descriptors and writes nothing.
"""
from __future__ import annotations

import json
import os
import pathlib
import re
import sys
from uuid import uuid4

import pytest
import pytest_asyncio
import yaml
from pydantic import ValidationError

from legba.data.profile import (
    DEFAULT_PROFILE,
    PROFILE_ENV,
    Profile,
    ProfileError,
    export_ui,
    load_profile,
    resolve_profile,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
GEO = REPO_ROOT / "profiles" / "geopolitics"
SANCTIONS = REPO_ROOT / "profiles" / "sanctions_export_control"
UI_SRC = REPO_ROOT / "legba-ui-v3" / "src"
GOLDEN = REPO_ROOT / "tests" / "fixtures" / "profile" / "profile_ui_geopolitics.golden.json"
COMMITTED_UI_JSON = UI_SRC / "lib" / "profile_ui.json"
DEPLOY_SH = REPO_ROOT / "deploy" / "deploy.sh"

_SCRIPTS_DIR = REPO_ROOT / "scripts"
_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(_SCRIPTS_DIR))
try:
    import bringup_register_descriptors as walker  # noqa: E402
    import gen_profile_ui  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)


def _ts(rel: str) -> str:
    return (UI_SRC / rel).read_text(encoding="utf-8")


def _manifest(path: pathlib.Path) -> dict:
    return yaml.safe_load((path / "profile.yaml").read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# loading + refusals
# ---------------------------------------------------------------------------


def test_both_profiles_load():
    geo = load_profile(GEO)
    assert geo.id == "geopolitics"
    assert geo.descriptors_path == (REPO_ROOT / "descriptors").resolve()
    assert geo.lifecycle_path == (REPO_ROOT / "descriptors" / "LIFECYCLE.md").resolve()
    assert geo.retired_path == (REPO_ROOT / "descriptors" / "RETIRED.md").resolve()
    assert geo.prompts_path == (GEO / "prompts").resolve()  # W9 SPLIT-2
    assert geo.prompts_package == "legba_geopolitics"
    assert geo.source_catalog_path == (GEO / "rosters" / "source_catalog.yaml").resolve()
    assert geo.top_floor == "world_assessor"
    assert geo.ui.map.geometry == "iso2"

    sanctions = load_profile(SANCTIONS / "profile.yaml")   # the manifest file itself also loads
    assert sanctions.id == "sanctions_export_control"
    assert sanctions.descriptors_path == (SANCTIONS / "descriptors").resolve()
    assert sanctions.lifecycle_path is None and sanctions.prompts_path is None
    assert sanctions.prompts_package is None
    assert sanctions.source_catalog_path is None
    assert sanctions.top_floor is None                     # `top_floor: none`
    assert sanctions.ui.map.geometry == "none"
    assert [k.id for k in sanctions.ui.subject.kinds] == ["regime", "counterparty"]
    assert sanctions.seeds == ()


def _copy_profile(tmp_path: pathlib.Path, src: pathlib.Path, mutate, name: str | None = None) -> pathlib.Path:
    data = _manifest(src)
    data["descriptors_dir"] = "descriptors"
    data["lifecycle"] = None
    data["prompts"] = None
    data["rosters"].pop("source_catalog", None)
    data["rosters"].pop("gazetteer", None)  # CANON-1: the gazetteer file is not copied
    data.pop("projection", None)
    data.pop("constants", None)  # SPLIT-3: inline instance data the copy does not need (validated against ui.frames)
    data.pop("subjects", None)  # SUBJECT-1: the label style; the platform default applies
    mutate(data)
    d = tmp_path / (name or src.name)
    d.mkdir()
    (d / "descriptors").mkdir()
    (d / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


@pytest.mark.parametrize(
    "where",
    ["top", "ui", "ui.subject", "ui.frames[0]", "ui.map", "ui.labels", "rosters", "rosters.units[0]"],
)
def test_an_unknown_key_is_refused_at_every_level(tmp_path, where):
    def mutate(data):
        node = data
        for part in where.split("."):
            m = re.fullmatch(r"(\w+)\[(\d+)\]", part)
            node = node[m.group(1)][int(m.group(2))] if m else (node if part == "top" else node[part])
        node["no_such_key"] = 1

    d = _copy_profile(tmp_path, GEO, mutate)
    with pytest.raises(ValidationError, match="no_such_key"):
        load_profile(d)


def test_the_copied_manifest_itself_loads(tmp_path):
    """The control for the refusal tests: an unmutated copy is valid."""
    assert load_profile(_copy_profile(tmp_path, GEO, lambda d: None)).id == "geopolitics"


@pytest.mark.parametrize(
    "mutate, err",
    [
        (lambda d: d["ui"]["frames"][2].update(parent="nowhere"), "parent names no frame"),
        (lambda d: d["ui"]["frames"][0].update(parent="country"), "cycle"),
        (lambda d: d.update(top_floor="nobody_produces_this"), "not the producer"),
        (lambda d: d["rosters"].update(scored_subject_tags=["g20", "elsewhere"]), "not in subject_tags"),
        (lambda d: d["rosters"].update(scored_units=["escalation", "elsewhere"]), "not in units"),
        (lambda d: d.update(seeds=["world_baseline", "world_baseline"]), "repeat"),
        (lambda d: d["ui"]["map"].update(geometry="mercator"), "geometry"),
        (lambda d: d.update(id="Not A Slug"), "id"),
    ],
)
def test_inconsistent_manifests_are_refused(tmp_path, mutate, err):
    d = _copy_profile(tmp_path, GEO, mutate)
    with pytest.raises(ValidationError, match=err):
        load_profile(d)


def test_load_time_refusals(tmp_path):
    with pytest.raises(ProfileError, match="no profile manifest"):
        load_profile(tmp_path)
    wrong_dir = _copy_profile(tmp_path, GEO, lambda d: None, name="not_geopolitics")
    with pytest.raises(ProfileError, match="is not its directory's name"):
        load_profile(wrong_dir)
    d = _copy_profile(tmp_path, GEO, lambda d: d.update(descriptors_dir="missing"))
    with pytest.raises(ProfileError, match="descriptors_dir names no directory"):
        load_profile(d)
    with pytest.raises(ProfileError, match="not loaded from a directory"):
        Profile.model_validate(_manifest(GEO)).descriptors_path   # noqa: B018


def test_the_projection_key_and_its_refusals(tmp_path):
    """W9 VIEW-1 — ``projection: {view, plane}``: the geopolitics profile
    declares its view; the sanctions skeleton declares none (its plane is its
    id, and every reader of it refuses ``projection_disabled``); the key is
    closed, its plane a slug, and its view file must exist."""
    geo = load_profile(GEO)
    assert geo.projection is not None and geo.plane == "geopolitics"
    assert geo.projection_view_path == (GEO / "projection" / "view.yaml").resolve()
    sanctions = load_profile(SANCTIONS)
    assert sanctions.projection is None and sanctions.plane == "sanctions_export_control"

    def with_projection(**proj):
        return lambda d: d.update(projection={"view": "projection/view.yaml", "plane": "geopolitics", **proj})

    d = _copy_profile(tmp_path, GEO, with_projection(), name="geopolitics")
    with pytest.raises(ProfileError, match="projection.view names no file"):
        load_profile(d)
    (d / "projection").mkdir()
    (d / "projection" / "view.yaml").write_text("schema: legba/projection_view/1\n", encoding="utf-8")
    assert load_profile(d).plane == "geopolitics"
    for i, (proj, err) in enumerate([({"no_such_key": 1}, "no_such_key"), ({"plane": "Not A Slug"}, "plane")]):
        root = tmp_path / f"case{i}"
        root.mkdir()
        bad = _copy_profile(root, GEO, with_projection(**proj), name="geopolitics")
        with pytest.raises(ValidationError, match=err):
            load_profile(bad)


# ---------------------------------------------------------------------------
# the resolver
# ---------------------------------------------------------------------------


def test_resolver_precedence(tmp_path):
    env = {PROFILE_ENV: "profiles/from_env"}
    assert resolve_profile("profiles/from_cli", env=env, root=tmp_path) == tmp_path / "profiles/from_cli"
    assert resolve_profile(None, env=env, root=tmp_path) == tmp_path / "profiles/from_env"
    assert resolve_profile("", env=env, root=tmp_path) == tmp_path / "profiles/from_env"
    assert resolve_profile(None, env={}, root=tmp_path) == tmp_path / DEFAULT_PROFILE
    assert resolve_profile(None, env={PROFILE_ENV: ""}, root=tmp_path) == tmp_path / DEFAULT_PROFILE
    assert resolve_profile(str(GEO), env=env, root=tmp_path) == GEO          # absolute stays
    assert DEFAULT_PROFILE == "profiles/geopolitics"
    assert load_profile(resolve_profile(None, env={}, root=REPO_ROOT)).id == "geopolitics"


def test_deploy_sh_takes_profile_with_the_same_default():
    text = DEPLOY_SH.read_text(encoding="utf-8")
    assert f'PROFILE="${{LEGBA_PROFILE:-{DEFAULT_PROFILE}}}"' in text
    assert "--profile)" in text and "--profile=*)" in text
    assert re.search(
        r'run_registrar scripts/bringup_register_descriptors\.py --profile "\$\{PROFILE\}"', text
    ), "deploy.sh must pass the profile to the walker"
    # the seed list comes from the profile unless LEGBA_SEED_SOURCES overrides it
    assert 'SEED_SOURCES="${LEGBA_SEED_SOURCES:-}"' in text
    assert "profile_query seeds" in text


# ---------------------------------------------------------------------------
# the geopolitics profile IS today's tree
# ---------------------------------------------------------------------------


def _plan_key(planned):
    return [(p.family, p.descriptor_id, p.origin, p.body) for p in planned]


def test_geopolitics_profile_plans_exactly_todays_walk():
    walker.register_kinds_in_process()
    today, today_excluded = walker.plan()
    via_profile, via_excluded = walker.plan(profile=load_profile(GEO))
    assert len(today) > 250
    assert _plan_key(via_profile) == _plan_key(today)
    assert via_excluded == today_excluded


def test_geopolitics_bringup_lists_equal_todays_literals():
    geo = load_profile(GEO)
    # deploy.sh's SEED_SOURCES default before PROFILE-1
    assert geo.seeds == ("world_baseline", "sipri_arms_transfers")
    assert geo.rosters.credibility_seeds == walker.CREDIBILITY_MODULES
    from legba.data.seed import ADAPTERS

    assert set(geo.seeds) <= set(ADAPTERS), "every profile seed must name a registered adapter"


def test_geopolitics_rosters_are_what_the_code_reads():
    """ROSTER-1: the code reads the rosters through ``legba.data.rosters``;
    the five desk-roster SQL copies bind them as ``?| $n::text[]`` and spell
    no tag, and the unit-roster constants are built from the helpers."""
    from legba.data import rosters
    from legba.data.analysts.deterministic_handlers import scorecard_banding
    from legba.data.registry.absence_api import BOUNDED_UNITS

    r = load_profile(GEO).rosters
    assert rosters.subject_roster_tags() == r.subject_tags == ("g20", "watch", "supply_chain")
    assert rosters.scored_roster_tags() == r.scored_subject_tags == ("g20", "watch")
    assert tuple(u.id for u in r.units) == BOUNDED_UNITS == rosters.unit_roster()
    assert tuple(r.scored_units) == scorecard_banding.DIMENSIONS == rosters.scored_unit_roster()
    src = REPO_ROOT / "src" / "legba" / "data" / "analysts"
    for rel in ("composition_slice.py",
                "deterministic_handlers/scorecard_producer.py",
                "deterministic_handlers/alert_trigger_scan.py",
                "deterministic_handlers/desk_baseline.py",
                "crossroads_detectors.py"):
        text = (src / rel).read_text(encoding="utf-8")
        assert "array['g20'" not in text and '{"g20"' not in text, rel
    for u in r.units:
        body = yaml.safe_load((REPO_ROOT / "descriptors" / f"analyst_{u.id}.yaml").read_text(encoding="utf-8"))
        assert body["method"].get("bounded_question"), f"analyst_{u.id}.yaml carries no bounded_question"
        assert u.question is None, "a written unit's question lives in its descriptor, not here"


# ---------------------------------------------------------------------------
# the UI export: golden + today's literals + the committed file
# ---------------------------------------------------------------------------


def test_geopolitics_export_equals_the_golden():
    assert export_ui(load_profile(GEO)) == GOLDEN.read_text(encoding="utf-8")


def test_the_committed_profile_ui_json_is_the_default_profiles_export():
    assert gen_profile_ui.OUT == COMMITTED_UI_JSON
    assert COMMITTED_UI_JSON.read_text(encoding="utf-8") == gen_profile_ui.render(DEFAULT_PROFILE), (
        "stale: run PYTHONPATH=src python3 scripts/gen_profile_ui.py"
    )
    assert gen_profile_ui.main(["--check", "--profile", DEFAULT_PROFILE]) == 0
    # a second profile's export differs, so --check names the stale file for it
    assert gen_profile_ui.main(["--check", "--profile", "profiles/sanctions_export_control"]) == 1


def test_every_golden_string_is_the_profiles():
    """UI-D1 §9, INVERTED by UI-B2. The golden used to be read back out of the
    TypeScript the labels came from; the TypeScript now READS the export, so
    the golden is asserted against its SOURCE — the profile manifest — string
    by string (``gen_profile_ui.py --check`` proves the committed file is that
    export byte for byte; ``lib/profileUi.test.ts`` keeps the shell's
    byte-for-byte label snapshot, unchanged)."""
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    ui = _manifest(GEO)["ui"]

    assert g["profile"] == "geopolitics" and g["plane"] == "geopolitics"
    assert g["top_floor"] == _manifest(GEO)["top_floor"]
    assert g["subject"] == ui["subject"]
    assert g["unit_noun"] == ui["unit_noun"]
    assert g["landing"] == ui["landing"]
    assert g["map"] == ui["map"]
    assert g["labels"] == ui["labels"]
    assert [(f["id"], f["label"], f.get("parent"), f["producer"]) for f in g["frames"]] == [
        (f["id"], f["label"], f.get("parent"), f["producer"]) for f in ui["frames"]
    ]
    assert [(s["id"], s["label"], s["question"]) for s in g["stances"]] == [
        (s["id"], s["label"], s["question"]) for s in ui["stances"]
    ]
    assert [(m["id"], m["label"]) for m in g["missions"]] == [(m["id"], m["label"]) for m in ui["missions"]]
    rosters = _manifest(GEO)["rosters"]
    assert g["rosters"]["subject_tags"] == rosters["subject_tags"]
    assert g["rosters"]["scored_subject_tags"] == rosters["scored_subject_tags"]


# The sites that used to SPELL a profile label, and the profile read each one
# makes now (UI-B2). A site that re-spells the label fails here.
_LABEL_READERS = {
    "panel-registry/registry.ts": ["PROFILE_UI.map.title", "PROFILE_UI.labels.read_title"],
    "panels/system/Wall.tsx": ["PROFILE_UI.labels.overview_title"],
    "lib/morningReadBands.ts": ["PROFILE_UI.labels.unscoped_subject"],
    "mobile/components/ReportsList.tsx": ["rosterLabel(roster)"],
    "mobile/mobileModel.ts": ["readTiers()"],
    "components/ObjectRail.tsx": ["label: PROFILE_UI.subject.plural", "label: PROFILE_UI.unit_noun.plural"],
}


def _code(src: str) -> str:
    """The TypeScript with its comments stripped (a comment may cite a label)."""
    return re.sub(r"^\s*//.*$", "", re.sub(r"/\*.*?\*/", "", src, flags=re.S), flags=re.M)


def test_the_typescript_carries_no_profile_label_literal():
    """UI-B2: every exported label is READ by the shell, never spelled there."""
    g = json.loads(GOLDEN.read_text(encoding="utf-8"))
    literals = [
        g["map"]["title"], g["labels"]["read_title"], g["labels"]["overview_title"],
        g["labels"]["unscoped_subject"], *g["labels"]["roster"].values(),
        *(f["label"] for f in g["frames"]), *(f["producer"] for f in g["frames"]),
    ]
    for rel, reads in _LABEL_READERS.items():
        code = _code(_ts(rel))
        for needle in reads:
            assert needle in code, f"{rel} no longer reads {needle}"
        for lit in literals:
            for quoted in (f"'{lit}'", f'"{lit}"', f">{lit}<"):
                assert quoted not in code, f"{rel} spells the profile label {quoted}"
    # the subject-roster regexes are gone: no exemplar id shape is parsed
    for rel in ("lib/deskNames.ts", "components/ObjectRail.tsx", "mobile/components/ReportsList.tsx"):
        assert not re.search(r"country_\(|\^country_|country_g20_|country_watch_", _code(_ts(rel))), rel


@pytest.mark.parametrize("profile_dir", [GEO, SANCTIONS])
def test_stance_and_mission_ids_are_the_shells(profile_dir):
    """Stances and missions are label overrides keyed by the shell's own ids."""
    stance_ids = set(re.findall(r"'(\w+)'", re.search(r"export type WorkspaceId = ([^\n]+)",
                                                       _ts("lib/workspaces.ts")).group(1)))
    mission_ids = set(re.findall(r"'(\w+)'", re.search(r"export type MissionId = ([^\n]+)",
                                                        _ts("lib/missions.ts")).group(1)))
    ui = load_profile(profile_dir).ui
    assert {s.id for s in ui.stances} <= stance_ids
    assert {m.id for m in ui.missions} <= mission_ids
    assert ui.landing in stance_ids


# ---------------------------------------------------------------------------
# the sanctions skeleton
# ---------------------------------------------------------------------------


def test_sanctions_skeleton_carries_its_case():
    s = load_profile(SANCTIONS)
    assert len(s.rosters.units) == 5 and all(u.question for u in s.rosters.units)
    assert s.rosters.scored_subject_tags == ()                # no scorecard declared
    planned, excluded = walker.plan(profile=s)
    assert excluded == []
    targets = [p for p in planned if p.family == "target"]
    sources = [p for p in planned if p.family == "source"]
    assert 10 <= len(targets) <= 14 and all(p.descriptor_id.startswith("regime_") for p in targets)
    assert not [p for p in planned if p.family == "analyst"]   # INST-2 writes the units
    assert {"source.federalregister.ofac", "source.opensanctions.bulk"} <= {p.descriptor_id for p in sources}
    walker.register_kinds_in_process()
    for p in planned:
        model = walker.validate(p)
        assert "regime" in model.scope.tags or p.family == "source"
        own = not (s.descriptors_path / pathlib.Path(p.origin).name).is_symlink()
        if own:
            assert model.identity.state.value == "draft", f"{p.origin} must ship draft"
    # nothing of the exemplar's catalog or credibility seeds rides along
    assert all(p.origin != walker.CATALOG_ORIGIN for p in planned)
    assert s.rosters.credibility_seeds == ()
    # the export is well-formed and says "no top floor" explicitly
    doc = json.loads(export_ui(s))
    assert doc["top_floor"] is None and doc["map"] == {"geometry": "none"}


_ADMIN_DSN = "postgresql://legba:legba@127.0.0.1:5432/postgres"


@pytest_asyncio.fixture
async def empty_db():
    """A dedicated, freshly migrated database with no descriptor heads, dropped afterwards."""
    import asyncpg

    from legba.data.config import PostgresConfig
    from legba.data.migrate import apply_primary_migrations

    name = f"legba_profile_{uuid4().hex[:10]}"
    conn = await asyncpg.connect(_ADMIN_DSN)
    try:
        await conn.execute(f'CREATE DATABASE "{name}"')
    finally:
        await conn.close()
    cfg = PostgresConfig(host="127.0.0.1", port=5432, user="legba", password="legba", database=name)
    try:
        await apply_primary_migrations(cfg)
        yield cfg
    finally:
        conn = await asyncpg.connect(_ADMIN_DSN)
        try:
            await conn.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                f"WHERE datname='{name}' AND pid <> pg_backend_pid()"
            )
            await conn.execute(f'DROP DATABASE IF EXISTS "{name}"')
        finally:
            await conn.close()


async def _counts(cfg) -> dict[str, int]:
    from legba.data.postgres import PostgresStore
    from legba.data.registry.descriptor import Family

    pg = PostgresStore(cfg)
    await pg.connect()
    try:
        async with pg.acquire() as conn:
            out = {fam: await conn.fetchval(f"SELECT count(*) FROM {Family(fam).table}")
                   for fam in walker.WALKED_FAMILIES}
            out["vocabulary_entries"] = await conn.fetchval("SELECT count(*) FROM vocabulary_entries")
            out["source_credibility"] = await conn.fetchval("SELECT count(*) FROM source_credibility")
        return out
    finally:
        await pg.close()


async def test_sanctions_dry_run_on_an_empty_db_plans_every_descriptor(empty_db, monkeypatch, capsys):
    """The CLI path: env-selected database (``open_registry`` → ``from_env``),
    the profile resolved from ``--profile``, ``--dry-run``. Every planned
    descriptor prints ``register``; nothing is written."""
    monkeypatch.setenv("LEGBA_DATA_PG_HOST", "127.0.0.1")
    monkeypatch.setenv("LEGBA_DATA_PG_PORT", "5432")
    monkeypatch.setenv("LEGBA_DATA_PG_DB", empty_db.database)
    before = await _counts(empty_db)
    assert sum(before[f] for f in walker.WALKED_FAMILIES) == 0

    profile = load_profile(resolve_profile("profiles/sanctions_export_control", root=REPO_ROOT))
    planned, _ = walker.plan(profile=profile)
    capsys.readouterr()
    code = await walker._amain(True, None, profile)
    out = capsys.readouterr().out
    assert code == 0, out
    assert "DRY RUN — nothing written" in out
    registers = re.findall(r"^\s+\+\s+register\s+(\w+)/(\S+)", out, re.M)
    assert sorted(registers) == sorted((p.family, p.descriptor_id) for p in planned)
    assert f"summary: register={len(planned)}" in out
    assert "source_credibility: 0 seed host(s) would be inserted" in out
    assert await _counts(empty_db) == before


def test_walker_cli_parses_profile(monkeypatch):
    seen = {}

    async def fake_amain(dry_run, families, profile=None, ids=None):
        seen.update(dry_run=dry_run, families=families, profile=profile, ids=ids)
        return 0

    monkeypatch.setattr(walker, "_amain", fake_amain)
    assert walker.main(["--dry-run", "--profile", "profiles/sanctions_export_control"]) == 0
    assert seen["dry_run"] is True and seen["profile"].id == "sanctions_export_control"
    monkeypatch.delenv(PROFILE_ENV, raising=False)
    assert walker.main([]) == 0
    assert seen["profile"].id == "geopolitics" and seen["dry_run"] is False
    monkeypatch.setenv(PROFILE_ENV, "profiles/sanctions_export_control")
    assert walker.main([]) == 0
    assert seen["profile"].id == "sanctions_export_control"

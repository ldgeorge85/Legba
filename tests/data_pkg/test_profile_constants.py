# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W10 SPLIT-3 — the instance constants are profile data, read through one leaf.

``profiles/<id>/profile.yaml`` ``constants`` (closed) carries the drain's tier
order, the external-truth strata, the subject frame, the anchor and auto-merge
classes, the translation languages, the adjacency edge families and the
applicability tags; ``legba.data.profile_constants`` is the one reader, with
the fcbafb2e literals as its by-name fallback when no profile is loadable.

  * the manifest block is closed and validated against ``ui.frames``;
  * the packaged export is the default profile's (drift test) and equals both
    the profile and the fallback literals;
  * every site that spelled a literal reads today's value (sha256 of each
    literal at the pin fcbafb2e) — and the sanctions skeleton's ``none``
    refuses by name at every reader;
  * ``/eval/subject_scorecard`` serves what ``/eval/country_scorecard`` serves,
    byte for byte, through the ASGI app;
  * the W7-F3 frame-prefix leftovers: ``LEGBA_FRAME_FAMILY`` off issues the
    legacy statements only; on, the ``frame`` family decides, and on the live
    topology the two answers are equal;
  * the 32-desk equality: every live desk resolves to the same frame, tier and
    anchor as at the pin.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import pathlib
import sys
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any

import pytest
import yaml
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import ValidationError

from legba.data import _entity_candidates as entity_candidates
from legba.data import _frame_anchor as frame_anchor
from legba.data import frames
from legba.data import profile_constants as pc
from legba.data.analysts import composition_slice as slice_
from legba.data.analysts.deterministic_handlers import _coverage_floor_scan as coverage_floor
from legba.data.analysts.deterministic_handlers import _external_audit_queue as audit_queue
from legba.data.analysts.deterministic_handlers import desk_baseline
from legba.data.analysts.deterministic_handlers import reenrich_ner, reenrich_translation
from legba.data.analysts.deterministic_handlers import situation_clustering
from legba.data.filters import ner
from legba.data.profile import ProfileError, load_profile
from legba.data.provenance import external_truth
from legba.data.registry.v3_api import build_v3_router
from legba.runtime import target_resolution

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
GEO = REPO_ROOT / "profiles" / "geopolitics"
SANCTIONS = REPO_ROOT / "profiles" / "sanctions_export_control"
ROSTER = REPO_ROOT / "tests" / "fixtures" / "bringup" / "live_head_roster_2026-10-04.json"
DESCRIPTORS = REPO_ROOT / "descriptors"

_HAD_PG_DB = "LEGBA_DATA_PG_DB" in os.environ
sys.path.insert(0, str(REPO_ROOT / "scripts"))
try:
    import bringup_register_descriptors as walker  # noqa: E402
    import bringup_tag_targets as tag_targets  # noqa: E402
    import gen_profile_constants  # noqa: E402
finally:
    if not _HAD_PG_DB:
        os.environ.pop("LEGBA_DATA_PG_DB", None)

_NOW = datetime(2026, 10, 4, tzinfo=timezone.utc)

#: sha256 (canonical JSON: sets sorted, tuples as lists, keys sorted) of each
#: literal this lane moved into the profile, measured on the pin fcbafb2e.
_PIN_DIGESTS = {
    "_external_audit_queue._TIER_RANK": "f37bc2223fa84a7596d8453a20a01fbcd021a372a511759a6ad8ab3633161af9",
    "external_truth._DESK_TIER_BY_ANALYST": "abca62d1b597ff77f0865f58685f2809a948726ea80d6fc4f6c9908b12f5d799",
    "external_truth.VERDICT_AGGREGATE_SQL": "dbb33b7d0a8ed596702e6c896304fc16fda6e04e60d7f5cc4fc47801d3124eea",
    "reenrich_translation._DEFAULT_TRANSLATE_LANGS": "f6ee20066a8aae4077aa9fddd59dd9629654a0152950758f52335b2f3312bf20",
    "reenrich_ner._DEFAULT_TRANSLATE_LANGS": "f6ee20066a8aae4077aa9fddd59dd9629654a0152950758f52335b2f3312bf20",
    "ner._NON_LATIN_TRANSLATE_LANGS": "cd3c2e1dbbdc4fbcd0e3d111e9eea91494c0280e591d7c2fc81589c946b24292",
    "journal_assessor._TRANSLATE_LANGS": "cd3c2e1dbbdc4fbcd0e3d111e9eea91494c0280e591d7c2fc81589c946b24292",
    "desk_baseline._ADJACENCY_EDGES": "0403fc4635357f75de7b42c364f1142f67ebf0e86c6e65bc6dc7d2e6b7f4472e",
    "bringup_tag_targets.REGION_BY_ISO2": "554826a7a8dfe27592835323ca884eb12298986d0b4a7313bae1316d6ee83e4b",
    "bringup_tag_targets.WATCH_TAGS_BY_ISO2": "717be0d118baaa2e783672dece37f10c15085d44749b4ab9e53eada2bf829da5",
}

#: The 32 live desks (the roster fixture's ``country_*`` heads).
_PIN_DESKS = {
    *((f"country_g20_{i.lower()}", i) for i in
      "AR AU BR CA CN DE FR GB ID IN IT JP KR MX RU SA TR US ZA".split()),
    *((f"country_watch_{i.lower()}", i) for i in
      "IL IR UA TW KP PK SD ML BF NE CD MM HT".split()),
}


def _canon(v: Any) -> Any:
    if isinstance(v, (set, frozenset)):
        return sorted(_canon(x) for x in v)
    if isinstance(v, (list, tuple)):
        return [_canon(x) for x in v]
    if isinstance(v, dict):
        return {k: _canon(x) for k, x in v.items()}
    return v


def _digest(v: Any) -> str:
    return hashlib.sha256(json.dumps(_canon(v), sort_keys=True).encode()).hexdigest()


@pytest.fixture(autouse=True)
def _flag_off(monkeypatch):
    monkeypatch.delenv(frames.FRAME_FAMILY_ENV, raising=False)


@pytest.fixture
def restore_constants():
    yield
    pc.refresh()


def _manifest(src: pathlib.Path) -> dict[str, Any]:
    return yaml.safe_load((src / "profile.yaml").read_text(encoding="utf-8"))


def _profile_copy(tmp_path: pathlib.Path, mutate, src: pathlib.Path = GEO) -> pathlib.Path:
    data = _manifest(src)
    data["descriptors_dir"] = str(REPO_ROOT / "descriptors")
    data["lifecycle"] = None
    data["prompts"] = None
    data["rosters"].pop("source_catalog", None)
    data.pop("projection", None)
    data["rosters"].pop("gazetteer", None)  # CANON-1: the gazetteer file is not copied
    data.pop("subjects", None)  # SUBJECT-1: the label style; the platform default applies
    mutate(data)
    d = tmp_path / src.name
    d.mkdir()
    (d / "profile.yaml").write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return d


# ---------------------------------------------------------------------------
# The manifest block
# ---------------------------------------------------------------------------


def test_both_profiles_declare_their_constants():
    geo = load_profile(GEO).constants
    assert geo.audit_tier_order == ("world", "thematic", "assessment", "country")
    assert geo.subject_frame == "country"
    assert geo.anchor_class == "country" and geo.auto_merge_class == "country"
    assert len(geo.translation_languages) == 11 and len(geo.adjacency["land_border"]) == 20
    sanctions = load_profile(SANCTIONS).constants
    assert sanctions.subject_frame == "regime"
    assert sanctions.anchor_class is None and sanctions.auto_merge_class is None  # `none`
    assert sanctions.audit_tier_order == () and sanctions.desk_tiers == ()
    assert sanctions.translation_languages == () and sanctions.adjacency == {}


@pytest.mark.parametrize(
    "mutate, err",
    [
        (lambda d: d["constants"].update(no_such_key=1), "no_such_key"),
        (lambda d: d["constants"]["desk_tiers"][0].update(no_such_key=1), "no_such_key"),
        (lambda d: d["constants"].update(audit_tier_order=["world", "nowhere"]), "audit_tier_order names nowhere"),
        (lambda d: d["constants"].update(subject_frame="nowhere"), "subject_frame names no ui.frames"),
        (lambda d: d["constants"].update(audit_tier_order=["world", "world"]), "repeat"),
        (lambda d: d["constants"]["desk_tiers"].append({"analyst": "world_assessor", "tier": "x"}), "repeat"),
        (lambda d: d["constants"].update(translation_languages=["EN"]), "translation_languages"),
        (lambda d: d["constants"]["adjacency"]["land_border"].append(["US", "us"]), "self-edges"),
        (lambda d: d["constants"]["adjacency"]["land_border"].append(["BR", "AR"]), "edges repeat"),
    ],
)
def test_the_constants_block_is_closed_and_consistent(tmp_path, mutate, err):
    with pytest.raises(ValidationError, match=err):
        load_profile(_profile_copy(tmp_path, mutate))


def test_a_profile_without_the_block_declares_none(tmp_path):
    """Left out ⇒ none / empty — never the exemplar's values."""
    bare = load_profile(_profile_copy(tmp_path, lambda d: d.pop("constants")))
    cs = pc.constant_set(bare)
    assert cs.anchor_class is None and cs.auto_merge_class is None and cs.subject_frame is None
    assert cs.audit_tier_producers == () and cs.desk_tiers == () and cs.adjacency == ()


# ---------------------------------------------------------------------------
# The packaged export and the fallback
# ---------------------------------------------------------------------------


def test_the_packaged_export_is_the_default_profiles():
    assert gen_profile_constants.OUT == pc.PACKAGED_EXPORT
    assert pc.PACKAGED_EXPORT.read_text(encoding="utf-8") == gen_profile_constants.render(None), (
        "stale: run PYTHONPATH=src python3 scripts/gen_profile_constants.py"
    )
    assert gen_profile_constants.main(["--check"]) == 0
    assert gen_profile_constants.main(["--check", "--profile", "profiles/sanctions_export_control"]) == 1


def test_the_packaged_export_equals_the_profile_and_the_fallback_literals():
    packaged = pc._from_export(pc.PACKAGED_EXPORT)
    assert packaged.origin.startswith("packaged:")
    profile = pc.constant_set(load_profile(GEO))
    assert replace(packaged, origin="") == replace(profile, origin="")
    assert replace(packaged, origin="") == replace(pc.FALLBACK, origin="")


def test_an_unloaded_profile_reads_the_fallback_by_name(tmp_path, caplog):
    with caplog.at_level(logging.WARNING, logger="legba.data.profile_constants"):
        cs = pc._select({}, export=tmp_path / "absent.json")
    assert cs is pc.FALLBACK and cs.origin == "fallback:literal"
    assert "profile_constants.unloaded" in caplog.text


def test_the_fallback_names_each_sites_literal():
    """Each fallback value IS the literal its site spelled at the pin."""
    fb = pc.FALLBACK
    ranked = {a: len(fb.audit_tier_producers) - i for i, a in enumerate(fb.audit_tier_producers)}
    assert _digest(ranked) == _PIN_DIGESTS["_external_audit_queue._TIER_RANK"]
    assert _digest(dict(fb.desk_tiers)) == _PIN_DIGESTS["external_truth._DESK_TIER_BY_ANALYST"]
    assert _digest(fb.translation_languages) == _PIN_DIGESTS["reenrich_translation._DEFAULT_TRANSLATE_LANGS"]
    assert _digest(dict(fb.adjacency)["land_border"]) == _PIN_DIGESTS["desk_baseline._ADJACENCY_EDGES"]
    assert (fb.subject_frame, fb.subject_producer, fb.anchor_class, fb.auto_merge_class) == (
        "country", "country_composition", "country", "country",
    )


def test_legba_profile_selects_the_directory_and_a_broken_export_is_refused(tmp_path):
    cs = pc._select({"LEGBA_PROFILE": str(SANCTIONS)})
    assert cs.profile == "sanctions_export_control" and cs.origin.startswith("profile:")
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"schema": "legba/profile_constants/0"}), encoding="utf-8")
    with pytest.raises(ProfileError, match="profile_constants/1"):
        pc._select({}, export=bad)


# ---------------------------------------------------------------------------
# Every site reads today's value
# ---------------------------------------------------------------------------


def test_the_moved_literals_are_todays_values():
    from legba.data.analysts import journal_assessor

    got = {
        "_external_audit_queue._TIER_RANK": audit_queue._TIER_RANK,
        "external_truth._DESK_TIER_BY_ANALYST": dict(external_truth._DESK_TIER_BY_ANALYST),
        "external_truth.VERDICT_AGGREGATE_SQL": external_truth.VERDICT_AGGREGATE_SQL,
        "reenrich_translation._DEFAULT_TRANSLATE_LANGS": reenrich_translation._DEFAULT_TRANSLATE_LANGS,
        "reenrich_ner._DEFAULT_TRANSLATE_LANGS": reenrich_ner._DEFAULT_TRANSLATE_LANGS,
        "ner._NON_LATIN_TRANSLATE_LANGS": ner._NON_LATIN_TRANSLATE_LANGS,
        "journal_assessor._TRANSLATE_LANGS": journal_assessor._TRANSLATE_LANGS,
        "desk_baseline._ADJACENCY_EDGES": desk_baseline._ADJACENCY_EDGES,
        "bringup_tag_targets.REGION_BY_ISO2": tag_targets.REGION_BY_ISO2,
        "bringup_tag_targets.WATCH_TAGS_BY_ISO2": tag_targets.WATCH_TAGS_BY_ISO2,
    }
    assert {k: _digest(v) for k, v in got.items()} == _PIN_DIGESTS
    assert audit_queue._TIER_RANK == {
        "world_assessor": 4, "escalation_composition": 3,
        "world_assessment": 2, "country_composition": 1,
    }
    assert frame_anchor.FrameAnchorConfig().class_gate == "country"
    assert coverage_floor._ANCHOR_CLASS == "country"
    assert entity_candidates._AUTO_MERGE_CLASS == "country"
    assert pc.subject_producer() == "country_composition"
    assert situation_clustering._target_for_category("country_g20_ar", None) == "country_g20_ar"
    assert situation_clustering._target_for_category("lane_hormuz", "x") == "x"


def test_the_sanctions_skeleton_refuses_by_name(restore_constants, caplog):
    pc.refresh(SANCTIONS)
    assert pc.audit_tier_rank() == {} and pc.desk_tier_by_analyst() == {}
    assert external_truth.desk_tier_case_sql(pc.desk_tier_by_analyst()) == "'other'"
    assert pc.anchor_class() is None and pc.auto_merge_class() is None
    assert pc.translation_languages() == () and pc.top_floor() is None
    with caplog.at_level(logging.INFO):
        assert pc.adjacency_edges("land_border") == ()
        assert frame_anchor.candidate_polities(["Iran", "France"], class_gate=None) == ()
    assert "adjacency_undeclared family=land_border" in caplog.text
    assert "frame_anchor.no_anchor_class" in caplog.text
    # the subject resolver homes the instance's own subjects, not the exemplar's
    assert situation_clustering._target_for_category("regime_ofac_iran", None) == "regime_ofac_iran"
    assert situation_clustering._target_for_category("country_g20_us", None) is None


def test_the_tier_case_renders_any_table():
    sql = external_truth.desk_tier_case_sql({"a": "x", "bb": "y"})
    assert sql.splitlines()[0] == "CASE g.analyst_id"
    assert "WHEN 'a'   THEN 'x'" in sql and "WHEN 'bb'  THEN 'y'" in sql
    assert sql.endswith("ELSE 'other'\n           END")


def test_the_coverage_fold_reads_the_anchor_class(monkeypatch):
    rows = [{"surface": "Iran", "ner_class": "GPE", "n_signals": 4, "n_days": 2}]
    assert len(coverage_floor.cluster_entities(rows, home_blob="")) == 1
    monkeypatch.setattr(coverage_floor, "_ANCHOR_CLASS", None)
    assert coverage_floor.cluster_entities(rows, home_blob="") == {}


class _AliasConn:
    def __init__(self, rows):
        self.rows = rows

    async def fetch(self, sql, *args):
        return self.rows if "ANY($1::text[])" in sql else []


@pytest.mark.parametrize("cls, band", [("country", "auto_merge"), (None, "gray")])
def test_the_auto_merge_band_reads_the_profiles_class(monkeypatch, cls, band):
    monkeypatch.setattr(entity_candidates, "_AUTO_MERGE_CLASS", cls)
    conn = _AliasConn([
        {"id": "m1", "canonical_name": "Myanmar", "entity_class": "country",
         "geo_country": None, "norm": "myanmar", "deg": 4},
        {"id": "m2", "canonical_name": "Burma", "entity_class": "country",
         "geo_country": None, "norm": "burma", "deg": 1},
    ])
    pairs = asyncio.run(entity_candidates.generate_candidates(conn, exact_limit=10, trgm_limit=0))
    assert [p.band for p in pairs] == [band]


# ---------------------------------------------------------------------------
# /eval/subject_scorecard — one handler, two paths
# ---------------------------------------------------------------------------


class _ScriptConn:
    def __init__(self, results):
        self._results = list(results)
        self.calls: list[tuple[str, tuple]] = []

    async def fetch(self, sql, *args):
        self.calls.append((sql, args))
        return self._results.pop(0)


class _Pool:
    def __init__(self, conn):
        self._conn = conn

    def acquire(self):
        conn = self._conn

        class _Ctx:
            async def __aenter__(self):
                return conn

            async def __aexit__(self, *exc):
                return False

        return _Ctx()


class _Deps:
    def __init__(self, conn):
        class _Reg:
            pass

        self.descriptor_registry = _Reg()
        self.descriptor_registry.pg = _Pool(conn)


def _scripted() -> _ScriptConn:
    excluded = "11111111-1111-1111-1111-111111111111"
    card = {
        "target_id": "country_g20_us", "id": "22222222-2222-2222-2222-222222222222",
        "produced_at": _NOW,
        "data": {"title": "t", "body": "b", "data": {"bands": {
            "generated_at": "2026-10-04T00:00:00+00:00", "floors": {"faith_floor": 0.5},
            "dimensions": {"escalation": {"band": "insufficient-evidence", "basis": [],
                                          "reason": "low-faithfulness"}},
            "composition": {"present": True, "basis": []},
        }, "method_version": "m1"}},
    }
    comp = {"target_id": "country_g20_us", "derived_from": [excluded], "data": {"data": {
        "citations": [{"ref_id": excluded, "ref_kind": "finding", "source": "escalation"}],
    }}}
    return _ScriptConn([[card], [comp]])


def test_the_subject_scorecard_alias_serves_the_old_route_byte_for_byte(monkeypatch):
    # the main checkout's .env puts the registry bearer into the test container;
    # this ASGI rig sends none, so the gate must be off (the consult-write rig idiom)
    monkeypatch.delenv("LEGBA_REGISTRY_API_TOKEN", raising=False)

    async def get(path):
        conn = _scripted()
        app = FastAPI()
        app.include_router(build_v3_router(deps=_Deps(conn)), prefix="/api/v1/v3")  # type: ignore[arg-type]
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
            r = await c.get(path, params={"target_id": "country_g20_us"})
        return r, conn.calls

    old, old_calls = asyncio.run(get("/api/v1/v3/eval/country_scorecard"))
    new, new_calls = asyncio.run(get("/api/v1/v3/eval/subject_scorecard"))
    assert old.status_code == new.status_code == 200
    assert old.content == new.content
    assert old_calls == new_calls
    body = old.json()
    assert body[0]["target_id"] == "country_g20_us" and len(body[0]["disagreements"]) == 1
    # the reconciliation reads the profile's subject producer, bound as $2
    sql, args = old_calls[1]
    assert "analyst_id = $2" in sql and args == (["country_g20_us"], "country_composition")


# ---------------------------------------------------------------------------
# The W7-F3 frame-prefix leftovers, on the live topology
# ---------------------------------------------------------------------------


def _live_targets() -> list[dict[str, Any]]:
    heads = json.loads(ROSTER.read_text())["heads"]
    states = {h["id"]: h["state"] for h in heads
              if h["family"] == "target" and h["state"] != "retired"}
    out = []
    for p in sorted(DESCRIPTORS.glob("target_*.yaml")):
        body = yaml.safe_load(p.read_text())
        did = body["identity"]["id"]
        if did in states:
            out.append({"descriptor_id": did, "name": body["identity"].get("name") or did,
                        "state": states[did], "body": body,
                        "tags": list((body.get("scope") or {}).get("tags") or [])})
    assert {t["descriptor_id"] for t in out} == set(states)
    return out


def _seed_rows() -> list[dict[str, Any]]:
    return [
        {"family": s.family, "value": s.value, "schema_uri": "legba/vocabulary/1.0.0",
         "introduced": _NOW, "deprecated": None, "notes": s.notes,
         "aliases": list(s.aliases), "parent": s.parent}
        for s in walker.load_vocabulary_seeds() if s.family == frames.FRAME_FAMILY
    ]


class _Topology:
    """Answers the frame leaf's, the slice's and the target resolver's reads
    from the live topology, recording every statement."""

    def __init__(self, vocab_rows):
        self.targets = _live_targets()
        self.vocab_rows = vocab_rows
        self.calls: list[str] = []

    def _members(self, pred, *cols):
        rows = [t for t in self.targets if t["state"] == "active" and pred(t)]
        return sorted(({c: t[c] for c in cols} for t in rows), key=lambda r: r["descriptor_id"])

    async def fetch(self, sql, *args):
        self.calls.append(sql)
        if sql is frames._FRAME_ROWS_SQL:
            return [r for r in self.vocab_rows if r["family"] == args[0]]
        if sql in (target_resolution._TAG_MEMBERS_SQL, slice_._REGION_MEMBERS_SQL):
            return self._members(lambda t: args[0] in t["tags"] and t["descriptor_id"] != args[0],
                                 "descriptor_id")
        if sql is frames._LEGACY_MEMBER_ROSTER_SQL:
            return self._members(lambda t: args[0] in t["tags"] and t["descriptor_id"] != args[0],
                                 "descriptor_id", "name")
        if sql is frames._MEMBER_ROSTER_SQL:
            return self._members(lambda t: set(args[0]) & set(t["tags"])
                                 and t["descriptor_id"] not in args[1], "descriptor_id", "name")
        if sql is target_resolution._GEO_MEMBERS_SQL:
            return self._members(
                lambda t: set(args[1]) & set((t["body"].get("scope") or {}).get("geo") or [])
                and t["descriptor_id"] != args[0], "descriptor_id")
        if sql is target_resolution._GAZETTEER_SQL:  # a two-row gazetteer
            known = {"%iran%": "IR", "%hormuz%": "OM"}
            return [{"iso2": known[p]} for p in args[0] if p in known]
        raise AssertionError(f"unexpected statement: {sql[:80]!r}")

    async def fetchrow(self, sql, *args):
        self.calls.append(sql)
        assert sql is target_resolution._HEAD_TARGET_SQL
        hit = [t for t in self.targets if t["descriptor_id"] == args[0]]
        return {"descriptor_id": hit[0]["descriptor_id"], "body": hit[0]["body"]} if hit else None


_REGIONS = ("region_africa", "region_americas", "region_europe", "region_indo_pacific", "region_mena")


def _ids() -> list[str]:
    return [t["descriptor_id"] for t in _live_targets()]


def test_flag_off_the_region_switch_is_the_prefix_and_reads_nothing():
    conn = _Topology(_seed_rows())
    for tid in [*_ids(), None, "", "region", "region_zz"]:
        assert asyncio.run(slice_.is_region_target(conn, tid)) is slice_._is_region_target(tid)
    assert conn.calls == []


def test_flag_on_the_region_switch_reads_the_frame_family(monkeypatch):
    monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
    conn = _Topology(_seed_rows())
    # on the live topology the family and the prefix agree on every head
    for tid in _ids():
        assert asyncio.run(slice_.is_region_target(conn, tid)) is slice_._is_region_target(tid), tid
    assert set(conn.calls) == {frames._FRAME_ROWS_SQL}
    # the family is authoritative: an unregistered region_* id is no frame, a
    # registered frame of the region kind is one whatever its id, the kind
    # itself and a thematic lane are not
    extra = _seed_rows() + [{
        "family": "frame", "value": "sahel", "schema_uri": "legba/vocabulary/1.0.0",
        "introduced": _NOW, "deprecated": None, "notes": "Region — Sahel",
        "aliases": ["tag:sahel"], "parent": "frame:region",
    }]
    conn = _Topology(extra)
    assert asyncio.run(slice_.is_region_target(conn, "region_zz")) is False
    assert asyncio.run(slice_.is_region_target(conn, "sahel")) is True
    assert asyncio.run(slice_.is_region_target(conn, "region")) is False
    assert asyncio.run(slice_.is_region_target(conn, "escalation_composition")) is False
    # an empty family falls back to the prefix
    conn = _Topology([])
    assert asyncio.run(slice_.is_region_target(conn, "region_zz")) is True
    assert asyncio.run(slice_.is_region_target(conn, "country_g20_us")) is False


def test_target_resolution_rung_one_flag_off_is_the_id_as_tag_read():
    conn = _Topology(_seed_rows())
    for tid in _REGIONS:
        members = asyncio.run(target_resolution._tag_members(conn, tid))
        assert members
    assert set(conn.calls) == {target_resolution._TAG_MEMBERS_SQL}


def test_target_resolution_rung_one_flag_on_equals_flag_off_on_the_live_topology(monkeypatch):
    off = {tid: asyncio.run(target_resolution.resolve_member_targets(_Topology(_seed_rows()), tid))
           for tid in [*_REGIONS, "situation_iran_war", "country_g20_us", "lane_hormuz"]}
    monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
    for tid, want in off.items():
        conn = _Topology(_seed_rows())
        assert asyncio.run(target_resolution.resolve_member_targets(conn, tid)) == want, tid
        if tid in _REGIONS:
            # a registered frame resolves through its member rule
            assert frames._MEMBER_ROSTER_SQL in conn.calls
            assert target_resolution._TAG_MEMBERS_SQL not in conn.calls
        else:
            # an id the family does not register keeps the id-as-tag convention
            assert target_resolution._TAG_MEMBERS_SQL in conn.calls


# ---------------------------------------------------------------------------
# The 32-desk equality
# ---------------------------------------------------------------------------


def _region_of(conn, desk: str) -> set[str]:
    return {r for r in _REGIONS
            if desk in asyncio.run(frames.resolve_members(
                r, conn, catalog=None if frames.frame_family_enabled() else frames.FrameCatalog(()),
                legacy=slice_.LEGACY_FRAMES))}


def test_every_desk_resolves_to_the_same_frame_tier_and_anchor_as_at_the_pin(monkeypatch):
    desks = tag_targets.desks()
    assert set(desks) == _PIN_DESKS and len(desks) == 32
    live = {t["descriptor_id"] for t in _live_targets()}
    assert {d for d, _ in desks} <= live
    off = {d: _region_of(_Topology(_seed_rows()), d) for d, _ in desks}
    monkeypatch.setenv(frames.FRAME_FAMILY_ENV, "1")
    on = {d: _region_of(_Topology(_seed_rows()), d) for d, _ in desks}
    tier = pc.desk_tier_by_analyst()[pc.subject_producer()]
    for did, iso2 in desks:
        # frame: the one region the pin's table filed it under, both ways
        assert off[did] == on[did] == {tag_targets.REGION_BY_ISO2[iso2]}, did
        # tier: a subject, never a frame; homed on itself; the country stratum
        assert asyncio.run(slice_.is_region_target(_Topology(_seed_rows()), did)) is False
        assert situation_clustering._target_for_category(did, None) == did
        assert tier == "country" and audit_queue._TIER_RANK[pc.subject_producer()] == 1
    # anchor: the class every desk's frames are anchored on
    assert frame_anchor.FrameAnchorConfig().class_gate == pc.anchor_class() == "country"


def test_the_tag_tables_follow_the_profile(tmp_path):
    """W9-F4: the tables are read off the profile — a profile that declares no
    applicability tags assigns none; the group tags are the frame seed's."""
    no_flags = load_profile(_profile_copy(tmp_path, lambda d: d["constants"].update(applicability_tags=[])))
    region, watch = tag_targets.tag_tables(no_flags)
    assert region == tag_targets.REGION_BY_ISO2 and watch == {}
    assert tag_targets.tags_for_iso2("IR", (region, watch)) == ["region_mena"]
    assert tag_targets.tags_for_iso2("IR") == [
        "region_mena", "nuclear_watch", "conflict_active", "sanctions_regime"]
    assert set(tag_targets.REGION_TAGS) == set(_REGIONS)
    text = (REPO_ROOT / "scripts" / "bringup_tag_targets.py").read_text(encoding="utf-8")
    assert '"region_mena"' not in text and '"AR": ' not in text


def test_the_scripts_tables_reproduce_the_pin_desk_by_desk():
    """The applicability tags sit in each desk's file in the order the old
    table spelled them, so the merge plan on a fresh head is unchanged."""
    for did, iso2 in tag_targets.desks():
        body = yaml.safe_load((DESCRIPTORS / f"target_{did}.yaml").read_text())
        plan = tag_targets.build_plan(did, iso2, {"scope": {"tags": ["geopolitical", "news"]}})
        assert plan.add_tags == tag_targets.tags_for_iso2(iso2)
        assert set(plan.add_tags) <= set(body["scope"]["tags"])

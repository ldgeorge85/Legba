# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-6 — Wave-A draft SourceDescriptors: schema + convention validation.

Validates the 41 committed Wave-A ``descriptors/source_*.yaml`` feeds the way
the registrar loads them (scripts/bringup_register_wave_a_sources.py::_load =
yaml.safe_load + placeholder version + ``SourceDescriptor.model_validate(
strict=False)``), then through the PRODUCTION unwrap + per-kind handler
``config_schema`` (the same transform ``build_source_handler`` applies), and
against the Wave-A conventions:

  * kind ``rss`` or ``json_api``; ships ``state: draft`` (bulk registration ->
    NO live actor; operator activates);
  * a cadence schedule is present (so an operator flip to active never trips the
    active-poll-needs-schedule validator);
  * declared geo / language / source_class match the intended values;
  * every feed carries the ``wave_a`` scope tag;
  * house rule — NO Chinese state-media route EXCEPT ``source.cgtn.world`` (the
    CN desk, knowingly ingested as labeled ``state_media``);
  * ids are unique, disjoint from the rest of the roster (other descriptor files
    + the S-1 embedded catalog), and every file is registered by the registrar.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.schemas.lifecycle import LifecycleState
from legba.data.schemas.source import SourceDescriptor
from legba.runtime.source_factory import (
    _unwrap_factory_dict,
    discover_source_kinds,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
DESCRIPTORS_DIR = REPO_ROOT / "descriptors"

# (filename, id, kind, source_class, geo, language) — the 41 Wave-A feeds.
WAVE_A_FILES: list[tuple[str, str, str, str, list[str], str]] = [
    ("source_who_disease_outbreak_news.yaml", "source.who.disease_outbreak_news", "json_api", "official", [], "en"),
    ("source_eia_today_in_energy.yaml", "source.eia.today_in_energy", "rss", "official", ["US"], "en"),
    ("source_cgtn_world.yaml", "source.cgtn.world", "rss", "state_media", ["CN"], "en"),
    ("source_timesofisrael.yaml", "source.timesofisrael.news", "rss", "reporting", ["IL"], "en"),
    ("source_jpost.yaml", "source.jpost.frontpage", "rss", "reporting", ["IL"], "en"),
    ("source_taipeitimes.yaml", "source.taipeitimes.news", "rss", "reporting", ["TW"], "en"),
    ("source_dailynk.yaml", "source.dailynk.english", "rss", "reporting", ["KP"], "en"),
    ("source_38north.yaml", "source.38north.analysis", "rss", "analysis", ["KP"], "en"),
    ("source_nhk_world.yaml", "source.nhk.world_news", "json_api", "reporting", ["JP"], "en"),
    ("source_japantimes.yaml", "source.japantimes.news", "rss", "reporting", ["JP"], "en"),
    ("source_agenciabrasil.yaml", "source.agenciabrasil.english", "rss", "reporting", ["BR"], "en"),
    ("source_mexiconewsdaily.yaml", "source.mexiconewsdaily.news", "rss", "reporting", ["MX"], "en"),
    ("source_batimes.yaml", "source.batimes.news", "rss", "reporting", ["AR"], "en"),
    ("source_elpais_english.yaml", "source.elpais.english", "rss", "reporting", ["ES"], "en"),
    ("source_aawsat.yaml", "source.aawsat.english", "rss", "reporting", ["SA"], "en"),
    ("source_middleeasteye.yaml", "source.middleeasteye.news", "rss", "reporting", [], "en"),
    ("source_antara.yaml", "source.antara.english", "rss", "reporting", ["ID"], "en"),
    ("source_meduza.yaml", "source.meduza.english", "rss", "reporting", ["RU"], "en"),
    ("source_rferl.yaml", "source.rferl.news", "rss", "reporting", [], "en"),
    ("source_abc_australia.yaml", "source.abc_au.justin", "rss", "reporting", ["AU"], "en"),
    ("source_cbc_world.yaml", "source.cbc.world", "rss", "reporting", ["CA"], "en"),
    ("source_ansa.yaml", "source.ansa.english", "rss", "reporting", ["IT"], "en"),
    ("source_isw.yaml", "source.isw.assessments", "json_api", "analysis", [], "en"),
    ("source_stategov_press.yaml", "source.stategov.press_releases", "rss", "official", ["US"], "en"),
    ("source_un_press.yaml", "source.un.press", "rss", "official", [], "en"),
    ("source_kremlin.yaml", "source.kremlin.english", "rss", "official", ["RU"], "en"),
    ("source_euvsdisinfo.yaml", "source.euvsdisinfo.cases", "rss", "analysis", [], "en"),
    ("source_dfrlab.yaml", "source.dfrlab.reports", "rss", "analysis", [], "en"),
    ("source_breakingdefense.yaml", "source.breakingdefense.news", "rss", "reporting", [], "en"),
    ("source_defensenews.yaml", "source.defensenews.news", "rss", "reporting", [], "en"),
    ("source_navalnews.yaml", "source.navalnews.news", "rss", "reporting", [], "en"),
    ("source_oilprice.yaml", "source.oilprice.news", "rss", "reporting", [], "en"),
    ("source_rigzone.yaml", "source.rigzone.news", "rss", "reporting", [], "en"),
    ("source_worldnuclearnews.yaml", "source.worldnuclearnews.news", "rss", "reporting", [], "en"),
    ("source_armscontrol.yaml", "source.armscontrol.news", "rss", "analysis", [], "en"),
    ("source_guardian_world.yaml", "source.guardian.world", "rss", "reporting", [], "en"),
    ("source_euronews.yaml", "source.euronews.news", "rss", "reporting", [], "en"),
    ("source_lemonde_english.yaml", "source.lemonde.english", "rss", "reporting", ["FR"], "en"),
    ("source_spiegel_international.yaml", "source.spiegel.international", "rss", "reporting", ["DE"], "en"),
    ("source_bangkokpost.yaml", "source.bangkokpost.topstories", "rss", "reporting", ["TH"], "en"),
    ("source_dawn.yaml", "source.dawn.home", "rss", "reporting", ["PK"], "en"),
]

# The one descriptor allowed to route a Chinese state-media outlet (CN desk).
_CN_DESK_ID = "source.cgtn.world"

# Chinese state-media hostnames barred from any NON-CN-desk route (house rule).
_CN_STATE_MEDIA_HOSTS = (
    "xinhua", "globaltimes", "chinadaily", "cctv", "people.cn",
    "peopledaily", "cgtn", "chinanews",
)


def _load_body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load_descriptor(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_load_body(name), strict=False)


def _config_url(desc: SourceDescriptor) -> str:
    """The outbound URL, whichever kind — rss (``url``) / json_api (``url_template``)."""
    cfg = _unwrap_factory_dict(desc.config)
    return str(cfg.get("url") or cfg.get("url_template") or "")


def test_all_files_present_and_count():
    assert len(WAVE_A_FILES) == 41
    for fname, *_ in WAVE_A_FILES:
        assert (DESCRIPTORS_DIR / fname).is_file(), f"missing {fname}"


@pytest.mark.parametrize("fname,sid,kind,klass,geo,lang", WAVE_A_FILES)
def test_descriptor_validates_and_conventions(fname, sid, kind, klass, geo, lang):
    desc = _load_descriptor(fname)
    assert desc.identity.id == sid
    assert desc.identity.kind == kind
    # Ships draft → bulk registration creates no live actor on a fresh rig.
    assert desc.identity.state == LifecycleState.DRAFT, (
        f"{fname}: Wave-A feeds must ship draft (operator-activated)"
    )
    assert desc.acquisition == "poll"
    # A cadence schedule is present so an operator flip to active never trips
    # the active-poll-needs-schedule validator.
    assert desc.cadence is not None and desc.cadence.schedule is not None
    assert list(desc.scope.geo) == geo
    assert list(desc.scope.languages) == [lang]
    assert desc.scope.source_class == klass
    # Batch namespace tag — lets selectors / audits identify the Wave-A slice.
    assert "wave_a" in desc.scope.tags


@pytest.mark.parametrize("fname,sid,kind,klass,geo,lang", WAVE_A_FILES)
def test_config_parses_through_handler_schema(fname, sid, kind, klass, geo, lang):
    """Config parses through the production unwrap + the per-kind handler
    ``config_schema`` (what ``build_source_handler`` does), and points at an
    external https/http endpoint (never an internal sidecar host)."""
    registry = discover_source_kinds()
    assert kind in registry, f"handler kind {kind!r} not discovered"
    desc = _load_descriptor(fname)
    # Raises on any schema violation (bad path, bad url_template placeholder, ...).
    registry[kind].config_schema(**_unwrap_factory_dict(desc.config))
    url = _config_url(desc)
    assert url.startswith(("http://", "https://")), f"{fname}: bad url {url!r}"


@pytest.mark.parametrize("fname,sid,kind,klass,geo,lang", WAVE_A_FILES)
def test_state_media_labeling_and_cn_desk_rule(fname, sid, kind, klass, geo, lang):
    """state_media is reserved for the CN desk (CGTN); no other Wave-A route
    may point at a Chinese state-media outlet."""
    desc = _load_descriptor(fname)
    url = _config_url(desc).lower()
    if sid == _CN_DESK_ID:
        assert desc.scope.source_class == "state_media"
        assert desc.scope.geo == ["CN"]
        assert "state_media" in desc.scope.tags
        return
    for host in _CN_STATE_MEDIA_HOSTS:
        assert host not in url, (
            f"{fname}: barred Chinese state-media host {host!r} in {url!r}"
        )


def test_ids_unique_and_disjoint_from_roster():
    """Wave-A ids are internally unique and collide with nothing already in the
    roster — the other descriptor files OR the S-1 embedded catalog."""
    wave_ids = [sid for _, sid, *_ in WAVE_A_FILES]
    assert len(wave_ids) == len(set(wave_ids)), "duplicate Wave-A ids"

    wave_files = {f for f, *_ in WAVE_A_FILES}
    existing: set[str] = set()
    for path in DESCRIPTORS_DIR.glob("source_*.yaml"):
        if path.name in wave_files:
            continue
        body = yaml.safe_load(path.read_text())
        existing.add(body["identity"]["id"])

    catalog = (REPO_ROOT / "scripts" / "bringup_register_source_catalog.py").read_text()
    existing.update(re.findall(r'id="(source\.[a-z0-9_.]+)"', catalog))

    collisions = sorted(set(wave_ids) & existing)
    assert not collisions, f"Wave-A ids collide with existing roster: {collisions}"


def test_registrar_covers_every_wave_a_file():
    """The registrar's SOURCE_FILES list is exactly the on-disk Wave-A set, and
    its credibility seeds have unique hosts + catalog-valid tiers."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "wave_a_registrar",
        REPO_ROOT / "scripts" / "bringup_register_wave_a_sources.py",
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    assert set(mod.SOURCE_FILES) == {f for f, *_ in WAVE_A_FILES}
    assert len(mod.SOURCE_FILES) == len(set(mod.SOURCE_FILES))

    hosts = [c[0] for c in mod.CREDIBILITY_SEEDS]
    assert len(hosts) == len(set(hosts)), "duplicate credibility-seed hosts"
    valid_tiers = {"wire", "gov", "aggregator", "thinktank", "social"}
    assert all(c[3] in valid_tiers for c in mod.CREDIBILITY_SEEDS)
    # Every seeded host resolves from at least one descriptor's URL.
    urls = " ".join(_config_url(_load_descriptor(f)).lower() for f, *_ in WAVE_A_FILES)
    for host in hosts:
        assert host in urls, f"seeded host {host!r} matches no Wave-A feed URL"

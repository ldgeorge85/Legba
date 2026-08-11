# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""B-7 — the 2026-08-03 source batch: schema + convention validation.

Validates the six committed ``descriptors/`` feeds of the AP-re-route +
Niger-coverage batch the way the registrar loads them
(scripts/bringup_register_source_batch_2026_08.py::_load = yaml.safe_load +
placeholder version + ``SourceDescriptor.model_validate(strict=False)``), then
through the PRODUCTION unwrap + the ``rss`` handler ``config_schema`` (the same
transform ``build_source_handler`` applies), and against the batch conventions:

  * kind ``rss``; ships ``state: draft`` (bulk registration -> NO live actor);
  * a cadence schedule is present, on a minute NOT already claimed by another
    committed source (the house staggering convention);
  * ``news`` tag + ``owner_tenant: shared`` — that pair plus kind ``rss`` is
    literally what the country/region desks' ``source_selector`` asks for
    (verified read-only against the live ``country_watch_ne`` target: selector
    ``{kinds: [rss], tags: [news], owner_tenant: shared}``, then per-signal
    narrowing via ``geo_match(["NE"])``), so a feed missing either is invisible
    to every desk;
  * every feed carries the ``source_batch_2026_08`` scope tag;
  * ``source_class: reporting`` for all six — none is state media;
  * THE GEO RULE, which is the one that actually bites: ``scope.geo`` is the
    PUBLISHER'S origin, promoted into the indexed ``geo`` column only as a
    post-enrichment fallback (data/sources/baseline.py C2). A multi-country
    feed declaring a country there fallback-stamps that country onto every
    un-geocoded story. So the global/regional feeds MUST declare ``geo: []``
    and only the genuinely domestic Nigerien outlets may declare ``[NE]``;
  * ids are unique and collide with nothing already in the roster;
  * the registrar's SOURCE_FILES is exactly this on-disk set, its credibility
    seeds have unique hosts on the live tier vocabulary, and it does NOT try to
    push the separately-handled rfa.korea edit through the forward-only FSM.
"""
from __future__ import annotations

import importlib.util
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
REGISTRAR = REPO_ROOT / "scripts" / "bringup_register_source_batch_2026_08.py"

# (filename, id, geo, language, url) — the six 2026-08 batch feeds.
BATCH_FILES: list[tuple[str, str, list[str], str, str]] = [
    (
        "source_rsshub_apnews_world.yaml",
        "source.rsshub.apnews.world",
        [],
        "en",
        "http://rsshub:1200/apnews/nav/world-news",
    ),
    (
        "source_actuniger_politique.yaml",
        "source.actuniger.politique",
        ["NE"],
        "fr",
        "https://www.actuniger.com/component/joomrss/politique.feed?Itemid=101",
    ),
    (
        "source_actuniger_societe.yaml",
        "source.actuniger.societe",
        ["NE"],
        "fr",
        "https://www.actuniger.com/component/joomrss/societe.feed?Itemid=101",
    ),
    (
        "source_studiokalangou.yaml",
        "source.studiokalangou.news",
        ["NE"],
        "fr",
        "https://www.studiokalangou.org/feed/",
    ),
    (
        "source_sahel_intelligence.yaml",
        "source.sahelintelligence.news",
        [],
        "fr",
        "https://sahel-intelligence.com/feed/",
    ),
    (
        "source_france24_afrique.yaml",
        "source.france24.afrique",
        [],
        "fr",
        "https://www.france24.com/fr/afrique/rss",
    ),
]

BATCH_TAG = "source_batch_2026_08"

# The one batch feed that still routes through the co-located RSSHub sidecar.
_SIDECAR_FILE = "source_rsshub_apnews_world.yaml"

# Tier vocabulary actually in use on the live source_credibility table.
_CREDIBILITY_TIERS = {"wire", "gov", "thinktank", "social", "aggregator"}


def _load_body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load_descriptor(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_load_body(name), strict=False)


def _registrar_module():
    spec = importlib.util.spec_from_file_location("batch_2026_08_registrar", REGISTRAR)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_all_batch_files_present():
    assert len(BATCH_FILES) == 6
    for fname, *_ in BATCH_FILES:
        assert (DESCRIPTORS_DIR / fname).is_file(), f"missing {fname}"


@pytest.mark.parametrize("fname,sid,geo,lang,url", BATCH_FILES)
def test_descriptor_validates_and_conventions(fname, sid, geo, lang, url):
    desc = _load_descriptor(fname)
    assert desc.identity.id == sid
    assert desc.identity.kind == "rss"
    # Ships draft → bulk registration creates no live actor on a fresh rig.
    assert desc.identity.state == LifecycleState.DRAFT, (
        f"{fname}: batch feeds must ship draft (operator-activated)"
    )
    assert desc.acquisition == "poll"
    # A cadence schedule is present so an operator flip to active never trips
    # the active-poll-needs-schedule validator.
    assert desc.cadence is not None and desc.cadence.schedule is not None
    assert list(desc.scope.geo) == geo
    assert list(desc.scope.languages) == [lang]
    assert desc.scope.source_class == "reporting"
    assert BATCH_TAG in desc.scope.tags


@pytest.mark.parametrize("fname,sid,geo,lang,url", BATCH_FILES)
def test_desk_auto_wire_triple(fname, sid, geo, lang, url):
    """kind ``rss`` + tag ``news`` + ``owner_tenant: shared`` is exactly what a
    country/region desk's ``source_selector`` matches on. Miss any leg of that
    triple and the feed is registered, healthy, polling — and read by nobody.
    """
    desc = _load_descriptor(fname)
    assert desc.identity.kind == "rss", f"{fname}: desks select kind=rss"
    assert "news" in desc.scope.tags, f"{fname}: desks select tag=news"
    assert desc.scope.owner_tenant == "shared", (
        f"{fname}: desks select owner_tenant=shared"
    )


@pytest.mark.parametrize("fname,sid,geo,lang,url", BATCH_FILES)
def test_config_parses_and_url_matches(fname, sid, geo, lang, url):
    """Config parses through the production unwrap + the rss handler's
    ``config_schema`` (what ``build_source_handler`` does) and points at the
    exact verified endpoint."""
    registry = discover_source_kinds()
    assert "rss" in registry
    desc = _load_descriptor(fname)
    parsed = registry["rss"].config_schema(**_unwrap_factory_dict(desc.config))
    assert parsed.url == url, f"{fname}: expected {url!r}, got {parsed.url!r}"
    if fname == _SIDECAR_FILE:
        assert parsed.url.startswith("http://rsshub:1200/"), (
            f"{fname}: the AP re-route rides the co-located sidecar"
        )
    else:
        assert parsed.url.startswith("https://"), (
            f"{fname}: native feeds are fetched directly over https"
        )
        assert "rsshub" not in parsed.url, (
            f"{fname}: native feed must carry no sidecar dependency"
        )


@pytest.mark.parametrize("fname,sid,geo,lang,url", BATCH_FILES)
def test_publisher_origin_geo_rule(fname, sid, geo, lang, url):
    """``scope.geo`` is the PUBLISHER'S origin, not the story's subject, and
    data/sources/baseline.py promotes it into the indexed ``geo`` column as a
    post-enrichment FALLBACK. Declaring a country on a multi-country feed
    therefore mis-stamps every story that failed to geocode in-body — the shape
    of bug that once had one country desk pulling a whole wire's world output.
    So: only the genuinely domestic Nigerien outlets may claim [NE]; the
    global/regional feeds must be empty. Whichever way, the ``geocode``
    enrichment stage is what actually puts a country on the signal.
    """
    desc = _load_descriptor(fname)
    domestic_ne = {"source.actuniger.politique", "source.actuniger.societe",
                   "source.studiokalangou.news"}
    if sid in domestic_ne:
        assert list(desc.scope.geo) == ["NE"], (
            f"{fname}: a Nigerien outlet's publisher origin IS NE — the fallback "
            "is what rescues domestic stories that name only a locality"
        )
    else:
        assert list(desc.scope.geo) == [], (
            f"{fname}: multi-country feed must not declare a publisher-origin "
            "country — it would fallback-stamp un-geocoded stories"
        )
    kinds = {stage.kind for stage in desc.pipeline.enrichment}
    assert "geocode" in kinds, f"{fname}: geo comes from in-body geocode, not scope"


def test_cadence_minutes_do_not_collide_with_the_roster():
    """House convention: fixed-minute hourly sources are staggered. A new batch
    landing on a minute another source already owns would bunch the poll fleet.
    """
    batch = {f for f, *_ in BATCH_FILES}
    taken: dict[str, str] = {}
    for path in sorted(DESCRIPTORS_DIR.glob("source_*.yaml")):
        if path.name in batch:
            continue
        body = yaml.safe_load(path.read_text())
        raw = (((body.get("cadence") or {}).get("schedule") or {}).get("raw") or "")
        minute = raw.split(" ")[0] if raw else ""
        if minute.isdigit():
            taken.setdefault(minute, path.name)

    for fname, *_ in BATCH_FILES:
        body = _load_body(fname)
        raw = body["cadence"]["schedule"]["raw"]
        minute = raw.split(" ")[0]
        assert minute.isdigit(), f"{fname}: expected a fixed-minute cron, got {raw!r}"
        assert minute not in taken, (
            f"{fname}: cron minute {minute} already claimed by {taken.get(minute)}"
        )


def test_batch_minutes_are_internally_unique():
    minutes = []
    for fname, *_ in BATCH_FILES:
        minutes.append(_load_body(fname)["cadence"]["schedule"]["raw"].split(" ")[0])
    assert len(minutes) == len(set(minutes)), f"duplicate cron minutes in batch: {minutes}"


def test_ids_unique_and_disjoint_from_roster():
    """Batch ids are internally unique and collide with nothing already in the
    roster — the other descriptor files OR the S-1 embedded catalog."""
    batch_ids = [sid for _, sid, *_ in BATCH_FILES]
    assert len(batch_ids) == len(set(batch_ids)), "duplicate batch ids"

    batch_files = {f for f, *_ in BATCH_FILES}
    existing: set[str] = set()
    for path in DESCRIPTORS_DIR.glob("source_*.yaml"):
        if path.name in batch_files:
            continue
        body = yaml.safe_load(path.read_text())
        existing.add(body["identity"]["id"])

    catalog = (REPO_ROOT / "scripts" / "bringup_register_source_catalog.py").read_text()
    existing.update(re.findall(r'id="(source\.[a-z0-9_.]+)"', catalog))

    collisions = sorted(set(batch_ids) & existing)
    assert not collisions, f"batch ids collide with existing roster: {collisions}"


def test_registrar_covers_every_batch_file():
    """The registrar's SOURCE_FILES list is exactly the on-disk batch set, and
    its credibility seeds have unique hosts on the live tier vocabulary."""
    mod = _registrar_module()
    assert sorted(mod.SOURCE_FILES) == sorted(f for f, *_ in BATCH_FILES)

    hosts = [h for h, *_ in mod.CREDIBILITY_SEEDS]
    assert len(hosts) == len(set(hosts)), f"duplicate credibility hosts: {hosts}"
    for host, score, rationale, tier, _state_aff in mod.CREDIBILITY_SEEDS:
        assert 0.0 <= score <= 1.0, f"{host}: score {score} out of range"
        assert tier in _CREDIBILITY_TIERS, f"{host}: off-vocabulary tier {tier!r}"
        assert rationale.strip(), f"{host}: empty rationale"


def test_registrar_excludes_the_live_rfa_korea_edit():
    """source_rsshub_rfa_korea.yaml was re-pointed in the same commit, but its
    LIVE head is already ``active`` and ``register_descriptor`` only walks
    FORWARD along the lifecycle FSM — pushing a file that declares ``draft``
    through this registrar would report ``no legal FSM path active -> draft``
    instead of updating anything. It must ride the registry's normal update
    path, so it stays out of SOURCE_FILES on purpose.
    """
    mod = _registrar_module()
    assert "source_rsshub_rfa_korea.yaml" not in mod.SOURCE_FILES
    assert "rfa_korea" in REGISTRAR.read_text(), (
        "the carve-out must be documented in the registrar, not just implied"
    )


def test_apnews_world_is_a_new_route_not_a_frozen_hub():
    """The five AP country hubs froze upstream and stay paused; this descriptor
    must be the section-navigation route, not another /apnews/topics/ hub."""
    desc = _load_descriptor(_SIDECAR_FILE)
    url = _unwrap_factory_dict(desc.config)["url"]
    assert url.endswith("/apnews/nav/world-news")
    assert "/apnews/topics/" not in url, (
        "the /apnews/topics/ hub pages are the surface that froze — do not re-add one"
    )
    assert "rsshub_lane" in desc.scope.tags, "still rides the sidecar; keep the lane tag"

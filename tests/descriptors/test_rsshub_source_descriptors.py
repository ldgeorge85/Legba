# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A7 — RSSHub-lane draft SourceDescriptors: schema + convention validation.

Validates the eight committed ``descriptors/source_rsshub_*.yaml`` feeds that
still route through the sidecar, the way the registrar loads them
(scripts/bringup_register_rsshub_sources.py::_load = yaml.safe_load +
placeholder version + ``SourceDescriptor.model_validate(strict=False)``),
then through the PRODUCTION unwrap + per-kind handler ``config_schema`` (the
same transform ``build_source_handler`` applies), and against the
RSSHub-lane conventions:

  * kind ``rss``, ships ``state: draft`` (bulk registration → NO live actor);
  * config URL points at the co-located sidecar ``http://rsshub:1200/<route>``;
  * ``source_class: reporting`` for every feed — NONE is ``state_media`` (the
    house rule allows state media only for the CN desk; these are non-G7 watch
    desks) and NONE routes a Chinese state outlet.

TWO of the original ten curated feeds have since been PROMOTED off the sidecar
onto verified native RSS. Both kept their id/filename deliberately (only
``config.url`` and, for one of them, ``scope`` changed — see each descriptor's
own header comment), so both are excluded from ``RSSHUB_FILES`` and the
sidecar-pointing / ``rsshub_lane``-tag conventions below, and each is covered
by its own test at the bottom of this module:

  * ``source_rsshub_rfi_afrique.yaml`` (2026-07-28) — RSSHub's own upstream
    scrape of ``/rfi/fr/afrique`` started 503-ing (an RSSHub-side breakage) and
    RFI publishes a native feed for that section, so it now polls
    ``https://www.rfi.fr/fr/afrique/rss`` directly.
  * ``source_rsshub_rfa_korea.yaml`` (2026-08-03) — reported "frozen since
    07-28" after 151 consecutive empty polls, but the route was never broken:
    ``/rfa/english/news/korea`` returned 200 with 38 dated items matching the
    upstream section page EXACTLY, and that section had simply published
    nothing since 21 July (RFA's Korea service now emits two or three articles
    a month). The fix was scope, not plumbing — it now polls RFA's native
    whole-service English feed ``https://www.rfa.org/english/rss2.xml``, which
    contains every Korea item plus six other Indo-Pacific beats. Its
    ``scope.geo`` also dropped from ``["KP"]`` to ``[]``: on a pan-Asian feed a
    declared publisher origin would fallback-stamp North Korea onto
    un-geocoded Tibet/Uyghur/Pacific stories.

The one file NOT to confuse with these: ``source_rsshub_apnews_world.yaml`` is
a NEW 2026-08-03 sidecar feed (the AP re-route) and is validated by
``tests/descriptors/test_source_batch_2026_08.py`` with the rest of its batch,
not here.
"""
from __future__ import annotations

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

# (filename, expected geo, expected language) — the eight feeds still routed
# through the RSSHub sidecar (rfi_afrique and rfa_korea moved off it — see the
# module docstring).
RSSHUB_FILES: list[tuple[str, list[str], str]] = [
    ("source_rsshub_apnews_niger.yaml", ["NE"], "en"),
    ("source_rsshub_apnews_taiwan.yaml", ["TW"], "en"),
    ("source_rsshub_focustaiwan.yaml", ["TW"], "en"),
    ("source_rsshub_apnews_haiti.yaml", ["HT"], "en"),
    ("source_rsshub_rfi_ameriques.yaml", [], "fr"),
    ("source_rsshub_apnews_drcongo.yaml", ["CD"], "en"),
    ("source_rsshub_aljazeera_drcongo.yaml", ["CD"], "en"),
    ("source_rsshub_apnews_north_korea.yaml", ["KP"], "en"),
]

# The two promoted-to-native feeds, tracked separately so
# test_all_ten_files_present still asserts on the full original set of files.
_RFI_AFRIQUE_FILE = "source_rsshub_rfi_afrique.yaml"
_RFA_KOREA_FILE = "source_rsshub_rfa_korea.yaml"
_PROMOTED_FILES = (_RFI_AFRIQUE_FILE, _RFA_KOREA_FILE)

# Chinese state-media hostnames barred from any non-CN desk route (house rule).
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


def test_all_ten_files_present():
    for fname, _, _ in RSSHUB_FILES:
        assert (DESCRIPTORS_DIR / fname).is_file(), f"missing {fname}"
    for fname in _PROMOTED_FILES:
        assert (DESCRIPTORS_DIR / fname).is_file(), f"missing {fname}"
    assert len(RSSHUB_FILES) + len(_PROMOTED_FILES) == 10, (
        "the original A7 curated set was ten feeds; a file leaving the sidecar "
        "list must land in _PROMOTED_FILES, not vanish"
    )


@pytest.mark.parametrize("fname,geo,lang", RSSHUB_FILES)
def test_descriptor_validates_and_conventions(fname: str, geo: list[str], lang: str):
    desc = _load_descriptor(fname)
    assert desc.identity.kind == "rss"
    # Ships draft → bulk registration creates no live actor on a fresh rig.
    assert desc.identity.state == LifecycleState.DRAFT, (
        f"{fname}: RSSHub-lane feeds must ship draft (operator-activated)"
    )
    assert desc.acquisition == "poll"
    # A cadence schedule is present so an operator flip to active never trips
    # the active-poll-needs-schedule validator.
    assert desc.cadence is not None and desc.cadence.schedule is not None
    assert list(desc.scope.geo) == geo
    assert list(desc.scope.languages) == [lang]
    # `news` tag drives auto-wiring to the country/region desks (which select
    # kind=rss + tag=news), then Subscription.geo narrows per signal.
    assert "news" in desc.scope.tags
    assert "rsshub_lane" in desc.scope.tags
    # House rule: no state_media for these non-CN desks.
    assert desc.scope.source_class == "reporting"


@pytest.mark.parametrize("fname,geo,lang", RSSHUB_FILES)
def test_config_parses_and_points_at_sidecar(fname: str, geo: list[str], lang: str):
    """Config parses through the production unwrap + the rss handler's
    ``config_schema`` (what ``build_source_handler`` does), and the URL targets
    the co-located RSSHub sidecar over the compose network."""
    registry = discover_source_kinds()
    assert "rss" in registry
    desc = _load_descriptor(fname)
    parsed = registry["rss"].config_schema(**_unwrap_factory_dict(desc.config))
    assert parsed.url.startswith("http://rsshub:1200/"), (
        f"{fname}: must poll the sidecar, got {parsed.url!r}"
    )


@pytest.mark.parametrize("fname,geo,lang", RSSHUB_FILES)
def test_no_chinese_state_media_route(fname: str, geo: list[str], lang: str):
    """No non-CN desk route may point at a Chinese state-media outlet."""
    desc = _load_descriptor(fname)
    url = _unwrap_factory_dict(desc.config)["url"].lower()
    for host in _CN_STATE_MEDIA_HOSTS:
        assert host not in url, f"{fname}: barred Chinese state-media host {host!r} in {url!r}"


def test_descriptor_ids_unique_and_namespaced():
    # Include the promoted-to-native files too — their ids/filenames were
    # deliberately left unchanged (see their header comments + the module
    # docstring above), so they still belong in the uniqueness + namespace
    # check even though they're excluded from the sidecar-convention checks.
    fnames = [f for f, _, _ in RSSHUB_FILES] + list(_PROMOTED_FILES)
    ids = [_load_descriptor(f).identity.id for f in fnames]
    assert len(ids) == len(set(ids)), "duplicate descriptor ids"
    assert all(i.startswith("source.rsshub.") for i in ids)


# ---------------------------------------------------------------------------
# source_rsshub_rfi_afrique.yaml — promoted off the sidecar 2026-07-28
# ---------------------------------------------------------------------------


def test_rfi_afrique_is_now_a_native_feed():
    """RSSHub's own upstream scrape of /rfi/fr/afrique started 503-ing
    (an RSSHub-side breakage — see the descriptor's header comment), and RFI
    publishes a verified native RSS feed for that section, so this
    descriptor no longer depends on the sidecar. It keeps every OTHER
    RSSHub-lane convention (kind, draft state, tags minus rsshub_lane,
    source_class, no Chinese state media) — only the sidecar dependency
    itself is gone.
    """
    desc = _load_descriptor(_RFI_AFRIQUE_FILE)
    assert desc.identity.kind == "rss"
    assert desc.identity.state == LifecycleState.DRAFT
    assert desc.acquisition == "poll"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    assert list(desc.scope.geo) == []
    assert list(desc.scope.languages) == ["fr"]
    assert "news" in desc.scope.tags
    assert "rsshub_lane" not in desc.scope.tags, (
        "no longer routes through the sidecar — the tag would be misleading"
    )
    assert desc.scope.source_class == "reporting"

    registry = discover_source_kinds()
    parsed = registry["rss"].config_schema(**_unwrap_factory_dict(desc.config))
    assert parsed.url == "https://www.rfi.fr/fr/afrique/rss", (
        f"expected the verified native RFI feed, got {parsed.url!r}"
    )
    assert not parsed.url.startswith("http://rsshub:1200/"), (
        "sidecar dependency should be fully removed"
    )

    url = parsed.url.lower()
    for host in _CN_STATE_MEDIA_HOSTS:
        assert host not in url, f"barred Chinese state-media host {host!r} in {url!r}"


# ---------------------------------------------------------------------------
# source_rsshub_rfa_korea.yaml — promoted off the sidecar 2026-08-03
# ---------------------------------------------------------------------------


def test_rfa_korea_is_now_a_native_whole_service_feed():
    """The "frozen since 07-28" report was an upstream CADENCE fact, not a
    broken route: ``/rfa/english/news/korea`` still returned 200 with 38 dated
    items matching the upstream section page exactly, and that section had
    published nothing since 21 July. Retirement was therefore wrong (a working
    route with unique DPRK reporting), and so was leaving it alone (a section
    emitting twice a month gives the desk nothing most weeks). It now polls
    RFA's NATIVE whole-service English feed, which contains every Korea item
    plus six other Indo-Pacific beats — and drops the sidecar dependency with
    it, the same move rfi_afrique made. It keeps every OTHER RSSHub-lane
    convention (kind, draft state, tags minus rsshub_lane, source_class, no
    Chinese state media).
    """
    desc = _load_descriptor(_RFA_KOREA_FILE)
    assert desc.identity.kind == "rss"
    assert desc.identity.state == LifecycleState.DRAFT
    assert desc.acquisition == "poll"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    assert list(desc.scope.languages) == ["en"]
    assert "news" in desc.scope.tags
    assert "rsshub_lane" not in desc.scope.tags, (
        "no longer routes through the sidecar — the tag would be misleading"
    )
    assert desc.scope.source_class == "reporting"

    registry = discover_source_kinds()
    parsed = registry["rss"].config_schema(**_unwrap_factory_dict(desc.config))
    assert parsed.url == "https://www.rfa.org/english/rss2.xml", (
        f"expected the verified native whole-service RFA feed, got {parsed.url!r}"
    )
    assert not parsed.url.startswith("http://rsshub:1200/"), (
        "sidecar dependency should be fully removed"
    )

    url = parsed.url.lower()
    for host in _CN_STATE_MEDIA_HOSTS:
        assert host not in url, f"barred Chinese state-media host {host!r} in {url!r}"


def test_rfa_korea_geo_is_empty_on_the_pan_asian_feed():
    """``scope.geo`` is the PUBLISHER'S origin, promoted into the indexed geo
    column only as a post-enrichment fallback (data/sources/baseline.py C2).
    Now that this descriptor carries all of RFA English — Korea, Myanmar, South
    China Sea, Tibet, Uyghur, China, Pacific — a declared ``["KP"]`` would
    fallback-stamp North Korea onto every un-geocoded Tibet or Pacific story.
    The KP desk still reaches the Korea items through the standard auto-wire
    plus its own per-signal ``geo_match(["KP"])`` predicate, fed by the
    ``geocode`` enrichment stage.
    """
    desc = _load_descriptor(_RFA_KOREA_FILE)
    assert list(desc.scope.geo) == [], (
        "a pan-Asian feed must not declare a publisher-origin country"
    )
    kinds = {stage.kind for stage in desc.pipeline.enrichment}
    assert "geocode" in kinds, "geo now comes from in-body geocode, not scope"

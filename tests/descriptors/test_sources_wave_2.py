# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""sources_wave_2 — draft SourceDescriptors: schema + convention validation.

Validates the ten committed ``descriptors/source_*.yaml`` feeds registered to
close gaps named in ``planning/COVERAGE_VS_SELECTION_AUDIT_2026-09-07.md``
(the "sources-wave-2" brief: OHCHR, UN News Africa/Middle East/Peace &
Security, Kyiv Independent, Kyiv Post, GroundUp, TimesLive-or-Daily-Maverick,
Sudan Tribune, Radio Dabanga, a China wire/business outlet, one India
business/policy outlet).

Three named candidates were researched live 2026-09-07 and NOT registered —
each is documented in its substitute's own header comment and in the
sources-wave-2 report, not re-litigated here:

  * OHCHR — its only discoverable feed (ohchr.org/en/rss.xml) serves stale
    taxonomy "Topics" pages, not press releases; fails the 7-day-recency bar.
    UN News (Africa / Peace & Security) is the practical substitute — it
    syndicates OHCHR/HRC material as regular wire items.
  * Kyiv Post — every path tried (root, /rss, /rss.xml, /feed) returned
    HTTP 403 to this fetcher; no RSS URL or RSSHub route found.
  * Sudan Tribune — every path tried, including the bare homepage, returned
    HTTP 403; a categorical site-level block. Sudan War Monitor (named
    directly in the audit's own remedy list alongside Sudan Tribune) is the
    substitute.

Validated the way the registrar would load them (yaml.safe_load + placeholder
version + ``SourceDescriptor.model_validate(strict=False)``), then through
the PRODUCTION unwrap + the ``rss`` handler ``config_schema`` (the same
transform ``build_source_handler`` applies), and against the batch
conventions:

  * kind ``rss``; ships ``state: draft`` (bulk registration -> NO live actor);
  * a cadence schedule is present;
  * declared geo / language / source_class match the intended values;
  * every feed carries the ``sources_wave_2`` scope tag;
  * ids are unique and collide with nothing already in the roster;
  * every file is mapped to the audit item it closes (documentation pin, not
    a behavioral assertion — keeps the gap rationale from drifting silently
    out of sync with the files).
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

# (filename, id, source_class, geo, languages) — the ten sources_wave_2 feeds.
WAVE_2_FILES: list[tuple[str, str, str, list[str], list[str]]] = [
    ("source_un_news_africa.yaml", "source.un_news.africa", "official", [], ["en"]),
    ("source_un_news_middle_east.yaml", "source.un_news.middle_east", "official", [], ["en"]),
    ("source_un_news_peace_security.yaml", "source.un_news.peace_security", "official", [], ["en"]),
    ("source_kyiv_independent.yaml", "source.kyivindependent.news", "reporting", ["UA"], ["en"]),
    ("source_groundup.yaml", "source.groundup.news", "reporting", ["ZA"], ["en"]),
    ("source_dailymaverick.yaml", "source.dailymaverick.news", "reporting", ["ZA"], ["en"]),
    ("source_dabangasudan.yaml", "source.dabanga.sudan", "reporting", ["SD"], ["en"]),
    ("source_sudanwarmonitor.yaml", "source.sudanwarmonitor.news", "analysis", ["SD"], ["en", "ar"]),
    ("source_scmp_china.yaml", "source.scmp.china", "reporting", ["CN"], ["en"]),
    ("source_rbi_press.yaml", "source.rbi.press_releases", "official", ["IN"], ["en"]),
]

# Each file mapped to the audit item(s) it closes
# (planning/COVERAGE_VS_SELECTION_AUDIT_2026-09-07.md, §1 item table).
CLOSES_AUDIT_ITEM: dict[str, str] = {
    "source_un_news_africa.yaml": "#4/#5 — Sudan UN FFM report / Kalogi strikes (OHCHR substitute)",
    "source_un_news_middle_east.yaml": "UN News topic-feed set named in the brief",
    "source_un_news_peace_security.yaml": "#4 — Sudan UN FFM report (Security Council / Secretariat statements)",
    "source_kyiv_independent.yaml": "#12 — SBU-HUR shootout / Kyiv security-service politics",
    "source_groundup.yaml": "#6 — Durban refugee-camp raid (English-language ZA reporting)",
    "source_dailymaverick.yaml": "#6 — Durban refugee-camp raid (ZA politics/investigative angle)",
    "source_dabangasudan.yaml": "#5 — Sudan / Kalogi strikes (South Kordofan ground reporting)",
    "source_sudanwarmonitor.yaml": "#5 — Sudan / Kalogi strikes (Sudan Tribune substitute)",
    "source_scmp_china.yaml": "#7 — China sanctions confrontation / rare-earth halts / Meta-Manus block",
    "source_rbi_press.yaml": "#8 — RBI Ashok Sahakari Bank action (GDELT-stub, zero-wire-citation carry)",
}


def _load_body(name: str) -> dict[str, Any]:
    body = yaml.safe_load((DESCRIPTORS_DIR / name).read_text())
    body.setdefault("identity", {})["version"] = "0" * 16
    return body


def _load_descriptor(name: str) -> SourceDescriptor:
    return SourceDescriptor.model_validate(_load_body(name), strict=False)


def _config_url(desc: SourceDescriptor) -> str:
    cfg = _unwrap_factory_dict(desc.config)
    return str(cfg.get("url") or "")


def test_all_files_present_and_count():
    assert len(WAVE_2_FILES) == 10
    for fname, *_ in WAVE_2_FILES:
        assert (DESCRIPTORS_DIR / fname).is_file(), f"missing {fname}"


@pytest.mark.parametrize("fname,sid,klass,geo,langs", WAVE_2_FILES)
def test_descriptor_validates_and_conventions(fname, sid, klass, geo, langs):
    desc = _load_descriptor(fname)
    assert desc.identity.id == sid
    assert desc.identity.kind == "rss"
    # Ships draft -> bulk registration creates no live actor on a fresh rig.
    assert desc.identity.state == LifecycleState.DRAFT, (
        f"{fname}: sources_wave_2 feeds must ship draft (operator-activated)"
    )
    assert desc.acquisition == "poll"
    assert desc.cadence is not None and desc.cadence.schedule is not None
    assert list(desc.scope.geo) == geo
    assert list(desc.scope.languages) == langs
    assert desc.scope.source_class == klass
    assert "sources_wave_2" in desc.scope.tags


@pytest.mark.parametrize("fname,sid,klass,geo,langs", WAVE_2_FILES)
def test_config_parses_through_handler_schema(fname, sid, klass, geo, langs):
    """Config parses through the production unwrap + the ``rss`` handler
    ``config_schema`` (what ``build_source_handler`` does), and points at an
    external https/http endpoint."""
    registry = discover_source_kinds()
    assert "rss" in registry
    desc = _load_descriptor(fname)
    registry["rss"].config_schema(**_unwrap_factory_dict(desc.config))
    url = _config_url(desc)
    assert url.startswith(("http://", "https://")), f"{fname}: bad url {url!r}"


def test_ids_unique_and_disjoint_from_roster():
    """sources_wave_2 ids are internally unique and collide with nothing
    already in the roster — the other descriptor files OR the S-1 embedded
    catalog."""
    wave_ids = [sid for _, sid, *_ in WAVE_2_FILES]
    assert len(wave_ids) == len(set(wave_ids)), "duplicate sources_wave_2 ids"

    wave_files = {f for f, *_ in WAVE_2_FILES}
    existing: set[str] = set()
    for path in DESCRIPTORS_DIR.glob("source_*.yaml"):
        if path.name in wave_files:
            continue
        body = yaml.safe_load(path.read_text())
        existing.add(body["identity"]["id"])

    catalog = (REPO_ROOT / "scripts" / "bringup_register_source_catalog.py").read_text()
    existing.update(re.findall(r'id="(source\.[a-z0-9_.]+)"', catalog))

    collisions = sorted(set(wave_ids) & existing)
    assert not collisions, f"sources_wave_2 ids collide with existing roster: {collisions}"


def test_every_file_mapped_to_an_audit_gap():
    """Documentation pin: every registered file names the audit item it
    closes, so the gap rationale can't drift silently out of sync."""
    wave_files = {f for f, *_ in WAVE_2_FILES}
    assert set(CLOSES_AUDIT_ITEM) == wave_files


def test_state_media_house_rule_holds():
    """None of the ten routes a Chinese state-media outlet — the CGTN-only
    CN-desk exception (source_cgtn_world.yaml) is untouched by this batch;
    SCMP is deliberately classed reporting, not state_media (see its own
    header comment)."""
    cn_state_media_hosts = (
        "xinhua", "globaltimes", "chinadaily", "cctv", "people.cn",
        "peopledaily", "cgtn", "chinanews",
    )
    for fname, sid, klass, *_ in WAVE_2_FILES:
        desc = _load_descriptor(fname)
        url = _config_url(desc).lower()
        assert klass != "state_media", f"{fname}: sources_wave_2 ships no state_media routes"
        for host in cn_state_media_hosts:
            assert host not in url, f"{fname}: barred Chinese state-media host {host!r} in {url!r}"


def test_ohchr_kyivpost_sudantribune_not_registered():
    """The three candidates named in the brief that were researched and
    rejected on live validation stay unregistered — no placeholder/stub
    descriptor for any of them (house no-stubs rule)."""
    for fname in (
        "source_ohchr.yaml",
        "source_ohchr_press.yaml",
        "source_kyivpost.yaml",
        "source_kyiv_post.yaml",
        "source_sudantribune.yaml",
        "source_sudan_tribune.yaml",
    ):
        assert not (DESCRIPTORS_DIR / fname).exists(), f"{fname} should not exist — see report"

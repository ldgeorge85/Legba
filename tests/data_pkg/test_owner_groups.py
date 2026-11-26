# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Owner resolution at the finest real voice, the owner map, and the re-derive
script's plan. Pure: every publisher, channel and domain here is invented."""

from __future__ import annotations

import importlib.util
import pathlib

import pytest

from legba.data import origins as O
from legba.data.owner_map import OwnerMapError, load_owner_map, parse_owner_map

REPO = pathlib.Path(__file__).resolve().parents[2]


def _tg(*channels):
    return {"config": {"channels": {"raw": list(channels)}}}


def _item(**kw):
    return O.derive_item_origin(kw.pop("sid", "source.tg"), kw.pop("kind", "telegram_channel"),
                                kw.pop("body", _tg("@a", "@b")), **kw)


# --- per channel -----------------------------------------------------------

def test_each_channel_of_one_bundle_is_its_own_owner():
    a = _item(payload={"channel": {"username": "NorthavenPort"}})
    b = _item(payload={"channel": {"username": "harbour_blog"}})
    assert (a.group_key, a.origin_kind, a.rule) == ("telegram:northavenport", "telegram_channel",
                                                    "telegram_item_channel")
    assert b.group_key == "telegram:harbour_blog" and a.group_key != b.group_key


def test_channel_falls_back_to_the_post_link_then_the_numeric_id():
    assert _item(payload={}, canonical_url="https://t.me/Some_Chan/17").group_key == "telegram:some_chan"
    assert _item(payload={"channel": {"id": 42}}).group_key == "telegram:id42"
    assert _item(payload={}) is None   # nothing names a channel: the source group stands


def test_the_bundle_source_origin_is_unchanged_as_the_fallback():
    d = O.derive_source("source.tg", "telegram_channel", _tg("@a", "@b"))
    assert d.group_key == "telegram_bundle:source.tg"


# --- aggregator by domain --------------------------------------------------

@pytest.mark.parametrize("kind, body", [
    ("gdelt_files", {"config": {}}),
    ("json_api", {"config": {"url_template": {"raw": "https://api.gdeltproject.org/api/v2/doc/doc?q=x"}}}),
])
def test_an_aggregator_item_is_owned_by_its_article_publisher(kind, body):
    d = O.derive_item_origin("source.agg", kind, body,
                             canonical_url="https://www.northaven-courier.example/2026/story.html")
    assert (d.group_key, d.origin_kind, d.rule) == (
        "host:northaven-courier.example", "publisher_domain", "aggregator_item_domain")


def test_an_aggregator_item_without_a_url_uses_the_reported_domain():
    d = O.derive_item_origin("source.agg", "gdelt_files", {}, payload={"item": {"domain": "news.example.co.uk"}})
    assert d.group_key == "host:example.co.uk"


def test_an_aggregator_item_joins_the_same_publishers_own_feed_group():
    feed = O.derive_source("source.nc", "rss", {"config": {"url": {"raw": "https://feeds.northaven-courier.example/x"}}})
    item = O.derive_item_origin("source.agg", "gdelt_files", {}, canonical_url="https://northaven-courier.example/a")
    assert item.group_key == feed.group_key


def test_the_owner_map_can_declare_another_aggregator():
    m = parse_owner_map({"aggregators": ["source.roundup"]})
    assert O.derive_item_origin("source.roundup", "rss", {}, canonical_url="https://x.example/a") is None
    assert O.derive_item_origin("source.roundup", "rss", {}, canonical_url="https://x.example/a",
                                owner_map=m).group_key == "host:x.example"


def test_an_ordinary_source_has_no_finer_voice():
    body = {"config": {"url": {"raw": "https://www.northaven-courier.example/f.xml"}}}
    assert O.derive_item_origin("source.rss", "rss", body, canonical_url="https://elsewhere.example/a") is None


# --- multi-feed collapse ---------------------------------------------------

def test_departments_of_an_umbrella_host_are_separate_and_a_departments_feeds_collapse():
    def feed(path):
        return O.derive_source("s." + path[-6:], "rss", {"config": {"url": {"raw": f"https://www.gov.uk{path}.atom"}}})
    a = feed("/government/organisations/ministry-of-ports")
    a2 = O.derive_source("s.other", "rss", {"config": {"url": {"raw": "https://www.gov.uk/government/organisations/ministry-of-ports/feed"}}})
    b = feed("/government/organisations/office-of-tides")
    assert a.group_key == a2.group_key == "host:gov.uk/ministry-of-ports"
    assert b.group_key == "host:gov.uk/office-of-tides"
    assert feed("/foreign-travel-advice").group_key == "host:gov.uk"


# --- the override map ------------------------------------------------------

MAP = parse_owner_map({
    "groups": {"host:courier.net": "host:courier.example", "host:gov.uk/ministry-of-ports": "org:northaven_gov",
               "host:gov.uk/office-of-tides": "org:northaven_gov"},
    "channels": {"@NorthavenPort": "host:courier.example"},
    "sources": {"source.travel": "org:northaven_gov"},
    "labels": {"org:northaven_gov": "Northaven Government"},
})


def test_the_map_joins_one_publisher_on_two_domains():
    a = O.derive_source("s.a", "rss", {"config": {"url": {"raw": "https://courier.example/f"}}}, owner_map=MAP)
    b = O.derive_source("s.b", "rss", {"config": {"url": {"raw": "https://www.courier.net/f"}}}, owner_map=MAP)
    assert a.group_key == b.group_key == "host:courier.example"
    assert (a.rule, b.rule) == ("publisher_host", "owner_map")
    assert b.detail["derived_group"] == "host:courier.net"


def test_the_map_joins_departments_a_channel_and_a_named_source():
    d1 = O.derive_source("s.1", "rss", {"config": {"url": {"raw": "https://www.gov.uk/government/organisations/ministry-of-ports.atom"}}}, owner_map=MAP)
    d2 = O.derive_source("s.2", "rss", {"config": {"url": {"raw": "https://www.gov.uk/government/organisations/office-of-tides.atom"}}}, owner_map=MAP)
    assert d1.group_key == d2.group_key == "org:northaven_gov"
    assert d1.group_label == "Northaven Government"
    assert O.derive_source("source.travel", "rss", {"config": {"url": {"raw": "https://www.gov.uk/foreign-travel-advice.atom"}}},
                           owner_map=MAP).group_key == "org:northaven_gov"
    ch = _item(payload={"channel": {"username": "northavenport"}}, owner_map=MAP)
    assert (ch.group_key, ch.rule) == ("host:courier.example", "owner_map")
    art = O.derive_item_origin("s.agg", "gdelt_files", {}, canonical_url="https://courier.net/a", owner_map=MAP)
    assert art.group_key == "host:courier.example"


def test_the_map_applies_to_ledger_hosts_and_an_empty_map_changes_nothing():
    assert O.derive_ledger_host("courier.net", owner_map=MAP).group_key == "host:courier.example"
    assert O.derive_ledger_host("courier.net").group_key == "host:courier.net"


@pytest.mark.parametrize("doc", [
    {"groups": {"host:a.example": "not a key"}}, {"nope": 1}, {"aggregators": "x"}, ["list"],
])
def test_a_malformed_map_is_refused(doc):
    with pytest.raises(OwnerMapError):
        parse_owner_map(doc)


def test_the_tracked_example_parses_and_an_absent_default_is_empty(tmp_path, monkeypatch):
    m = load_owner_map(REPO / "seeds" / "owner_map.example.yaml")
    assert m.groups and m.channels == {"northavenport": "host:northaven-courier.example"}
    monkeypatch.delenv("LEGBA_OWNER_MAP", raising=False)
    monkeypatch.setattr("legba.data.owner_map.DEFAULT_OWNER_MAP_PATH", tmp_path / "none.yaml")
    assert not load_owner_map()
    with pytest.raises(OwnerMapError):
        load_owner_map(tmp_path / "none.yaml")


# --- the script's plan, and idempotence -------------------------------------

def _script():
    spec = importlib.util.spec_from_file_location("derive_owner_groups", REPO / "scripts" / "derive_owner_groups.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _derived():
    return [
        O.derive_source("s.a", "rss", {"config": {"url": {"raw": "https://courier.example/f"}}}),
        O.derive_source("s.b", "rss", {"config": {"url": {"raw": "https://feeds.courier.example/g"}}}),
        O.derive_source("s.tg", "telegram_channel", _tg("@a", "@b")),
        _item(payload={"channel": {"username": "a"}}),
        _item(payload={"channel": {"username": "b"}}),
        O.derive_item_origin("s.agg", "gdelt_files", {}, canonical_url="https://courier.example/x"),
    ]


def test_the_plan_counts_before_and_after_and_a_second_pass_changes_nothing():
    sc = _script()
    derived = _derived()
    s = sc.summarize(derived, {"telegram_channel:a": 5, "telegram_channel:b": 2, "publisher_domain:courier.example": 9}, {})
    assert s["before"]["origins"] == 0
    assert s["after"]["sources"] == 3 and s["after"]["source_owners"] == 2   # courier feeds collapse
    assert s["telegram_channels"] == {"channels": 2, "owners": 2, "items": 7}
    assert s["changes"]["inserted"] == 6
    # Pretend the first pass was applied: the same derivation is now all "unchanged".
    stored = {d.origin_key: {"origin_key": d.origin_key, "origin_kind": d.origin_kind,
                             "group_key": d.group_key, "rule": d.rule, "corrected": False}
              for d in derived}
    again = sc.summarize(derived, {}, stored)
    assert again["changes"] == {"inserted": 0, "moved": 0, "corrected_kept": 0, "unchanged": 6}
    assert again["before"]["owner_groups"] == again["after"]["owner_groups"]


def test_an_admin_corrected_origin_is_kept_by_the_plan():
    sc = _script()
    derived = _derived()
    stored = {"source:s.a": {"origin_key": "source:s.a", "origin_kind": "source", "group_key": "org:elsewhere",
                             "rule": "admin_correction", "corrected": True}}
    s = sc.summarize(derived, {}, stored)
    assert s["changes"]["corrected_kept"] == 1
    assert s["_after"]["source:s.a"] == "org:elsewhere"


def test_every_new_rule_is_declared():
    assert set(O.RULES) >= {"telegram_item_channel", "aggregator_item_domain", "owner_map"}


@pytest.mark.parametrize("host, domain", [
    ("news.harbour.com.bz", "harbour.com.bz"),
    ("www.harbour.co.bw", "harbour.co.bw"),
    ("ports.gov.fj", "ports.gov.fj"),
    ("harbour.com.bz", "harbour.com.bz"),
    ("www.harbour.com", "harbour.com"),
])
def test_a_generic_label_under_a_country_code_is_a_suffix(host, domain):
    assert O.registrable_domain(host) == domain

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Origin-group derivation (data model), pure. Every descriptor here is
invented (the Northaven family of fictional publishers)."""

from __future__ import annotations

import pytest

from legba.data import origins as O


@pytest.mark.parametrize("host, domain", [
    ("feeds.northaven-courier.example", "northaven-courier.example"),
    ("www.northaven-courier.example", "northaven-courier.example"),
    ("news.bbc.co.uk", "bbc.co.uk"),
    ("www.gov.uk", "gov.uk"),
    ("www.harbour.europa.eu", "harbour.europa.eu"),
    ("cert.ssi.gouv.fr", "ssi.gouv.fr"),
    ("localhost", "localhost"),
    ("10.0.0.7", None),
    ("", None),
    (None, None),
])
def test_registrable_domain(host, domain):
    assert O.registrable_domain(host) == domain


def _rss(url: str, **scope):
    return {"scope": {"source_class": "reporting", **scope}, "config": {"url": {"raw": url}}}


def test_two_feeds_of_one_publisher_are_one_group():
    a = O.derive_source("source.northaven.port", "rss", _rss("https://www.northaven-courier.example/port.xml"))
    b = O.derive_source("source.northaven.politics", "rss", _rss("https://feeds.northaven-courier.example/pol"))
    assert a.group_key == b.group_key == "host:northaven-courier.example"
    assert a.rule == b.rule == "publisher_host"
    assert a.origin_key == "source:source.northaven.port"


def test_a_claim_source_groups_by_its_declared_claimant_whatever_the_host():
    body = _rss("https://mirror.example/feed", source_class="claim",
                claim={"claimant": "Grey Lantern", "signed_off_by": "x"})
    d = O.derive_source("source.greylantern.mirror", "rss", body)
    assert d.group_key == "claimant:grey-lantern"
    assert d.rule == "claim_declared_claimant"


def test_telegram_one_channel_and_a_bundle():
    one = O.derive_source("source.tg.one", "telegram_channel",
                          {"config": {"channels": {"raw": ["@NorthavenPort"]}}})
    assert (one.group_key, one.rule) == ("telegram:northavenport", "telegram_channel")
    many = O.derive_source("source.tg.many", "telegram_channel",
                           {"config": {"channels": {"raw": ["@a_chan", "@b_chan"]}}})
    assert (many.group_key, many.rule) == ("telegram_bundle:source.tg.many", "telegram_bundle")
    assert many.detail["channels"] == 2


def test_a_proxied_feed_groups_by_the_publisher_the_route_names():
    d = O.derive_source("source.rsshub.northaven", "rss", _rss("http://rsshub:1200/northavenwire/topics/port"))
    assert (d.group_key, d.rule) == ("publisher:northavenwire", "proxied_publisher")


def test_no_address_is_its_own_group():
    d = O.derive_source("source.files.northaven", "gdelt_files", {"config": {"lookback_minutes": {"raw": 15}}})
    assert (d.group_key, d.rule) == ("descriptor:source.files.northaven", "descriptor_self")


def test_a_ledger_host_shares_the_publishers_group():
    led = O.derive_ledger_host("archive.northaven-courier.example")
    feed = O.derive_source("source.northaven.port", "rss", _rss("https://www.northaven-courier.example/port.xml"))
    assert led.group_key == feed.group_key
    assert led.rule == "licence_ledger_host"
    assert led.origin_key == "ledger_host:archive.northaven-courier.example"


def test_a_knowledge_set_is_its_own_group():
    d = O.derive_knowledge_set("world_baseline")
    assert (d.origin_key, d.group_key, d.rule) == (
        "knowledge_set:world_baseline", "set:world_baseline", "knowledge_set")


def test_every_rule_used_is_declared():
    assert set(O.RULES) >= {
        "claim_declared_claimant", "telegram_channel", "telegram_bundle", "proxied_publisher",
        "publisher_host", "licence_ledger_host", "knowledge_set", "descriptor_self",
        "admin_correction",
    }

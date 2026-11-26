# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A valueless query key that IS the item's identifier is kept.

Live 11-13: every lefaso.net SPIP article (``/spip.php?article<N>``, 1,503
signals / 1,398 distinct articles) canonicalised to ``/spip.php`` and tier 1
aliased them all into one. The rule keeps a valueless key carrying a digit
(bare, sorted); a digit-free valueless key (a flag, a tracking marker) is
still dropped.

The equivalence proof runs the N-S equivalence corpus (every variant class:
tracking params, order, ``?ref=``, ``?article<N>``, ``www.``, ports, slugs,
IDN, degenerate strings) through the canonicaliser twice — as it now is, and
with the identifier branch disabled, which IS the prior code path byte for
byte (the branch is the only change) — and asserts the canonical form moves
for exactly the URLs that carry a digit-bearing valueless key, and no other.
"""
from __future__ import annotations

import random
from unittest import mock
from urllib.parse import parse_qsl, urlsplit

import pytest

from legba.data import _url_canon as U
from legba.data._url_canon import canonical_url, canonical_url_hash
from tests.data_pkg.test_ingest_dedupe_url_hash_equivalence import _corpus


def _prior(url: str) -> str:
    with mock.patch.object(U, "_valueless_identifier_keys", lambda q: []):
        return U.canonical_url(url)


@pytest.mark.parametrize(("url", "canon"), [
    ("https://lefaso.net/spip.php?article149046", "https://lefaso.net/spip.php?article149046"),
    ("https://www.lefaso.net/spip.php?article149046=", "https://lefaso.net/spip.php?article149046"),
    ("http://lefaso.net/spip.php?article149046#forum", "http://lefaso.net/spip.php?article149046"),
    ("https://lefaso.net/spip.php?utm_source=rss&article149046",
     "https://lefaso.net/spip.php?article149046&utm_source=rss"),
    ("https://site.example/spip.php?breve42", "https://site.example/spip.php?breve42"),
    ("https://site.example/spip.php?p123", "https://site.example/spip.php?p123"),
    ("https://sawatdeenetwork.com/member.php?234-christianpfc",
     "https://sawatdeenetwork.com/member.php?234-christianpfc"),
])
def test_an_identifier_key_is_kept(url, canon):
    assert canonical_url(url) == canon
    assert canonical_url(canon) == canon                       # idempotent


@pytest.mark.parametrize(("url", "canon"), [
    ("https://eia.gov/todayinenergy/detail.php?id", "https://eia.gov/todayinenergy/detail.php"),
    ("https://redwatch.org.au/a?orig_query", "https://redwatch.org.au/a"),
    ("https://x.example/a?amp&ref=", "https://x.example/a"),
    ("https://x.example/a?b=2&a=1", "https://x.example/a?a=1&b=2"),
])
def test_a_flag_or_tracking_key_is_still_dropped(url, canon):
    assert canonical_url(url) == canon


def test_distinct_spip_articles_no_longer_collapse():
    a, b = "https://lefaso.net/spip.php?article149046", "https://lefaso.net/spip.php?article149049"
    assert canonical_url_hash(a) != canonical_url_hash(b)
    assert canonical_url_hash(a) == canonical_url_hash("https://www.LEFASO.net/spip.php?article149046#x")


def _has_identifier_key(url: str) -> bool:
    try:
        query = urlsplit(url.strip()).query
    except ValueError:
        return False
    # The documented rule: a valueless key carrying a digit, identifier-shaped.
    return any(k and v == "" and U._IDENTIFIER_KEY_RE.match(k)
               for k, v in parse_qsl(query, keep_blank_values=True))


@pytest.mark.parametrize("seed", [20261113, 7, 99])
def test_only_identifier_key_urls_change_on_the_equivalence_corpus(seed):
    urls = _corpus(random.Random(seed), 900)
    moved = {u for u in urls if canonical_url(u) != _prior(u)}
    expected = {u for u in urls if _has_identifier_key(u)}
    assert moved == expected
    # every moved URL is the corpus's ``?article<N>`` class, nothing else
    assert moved and all("?article" in u or "&article" in u for u in moved)

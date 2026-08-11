# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-4 R4 F3/F4 — the article-id URL identity and the URL-embedded-date read.

F3 (the slug-rewrite class): the same Dawn article id served under every
headline the story has worn defeated every canonical-URL dedupe surface —
"``23fc1b12`` and ``278ee1c6`` are the SAME Dawn article (``/news/2020547/``)
at two different slugs." The rule that closes it was MEASURED before it
shipped (2026-08-10, the 80,000 newest stored canonical_urls, 74,251
distinct): it fired on 19,060 and created exactly 62 new collapse groups,
every one the same-article-id re-headline class, zero cross-story collapses.
These tests pin the rule's firing shape AND its refusals — the refusals are
the safety argument.

F4 (the stale-canonical class): a fresh Yonhap signal carrying a 2025-dated
article id. ``url_embedded_date`` is the parse half of the report's "audit
canonical_url against body dates"; ``scripts/audit_canonical_url_dates.py``
is the read-only audit built on it.

NEW FILE deliberately: the sibling data_pkg suites are under concurrent
test-hygiene work.
"""
from __future__ import annotations

from datetime import date

from legba.data._url_canon import canonical_url, url_embedded_date
from legba.data.analysts.deterministic_handlers import claim_watch as cw


# ---------------------------------------------------------------------------
# F3 — where the rule fires (the measured defect class)
# ---------------------------------------------------------------------------


def test_the_r4_dawn_pair_is_one_document():
    """The report's own F3 exhibit: /news/2020547/ at two slugs."""
    a = canonical_url(
        "https://www.dawn.com/news/2020547/ajk-elections-ppp-again-alleges-"
        "rigging-as-polling-under-way-in-2-muzaffarabad-constituencies"
    )
    b = canonical_url(
        "https://www.dawn.com/news/2020547/ajk-elections-voting-concludes-in-"
        "two-constituencies-as-ppp-again-alleges-rigging"
    )
    assert a == b == "https://dawn.com/news/2020547"


def test_bare_id_and_slugged_id_agree():
    """gdnonline serves both shapes for one article (live-measured pair)."""
    assert canonical_url("https://gdnonline.com/Details/1402094") == (
        canonical_url(
            "https://gdnonline.com/Details/1402094/IQRA-Private-School-"
            "students-excel-in-SSC-II-exams"
        )
    )


def test_trailing_slash_after_the_id_is_the_same_identity():
    assert canonical_url("https://ex.com/story/9320396/") == canonical_url(
        "https://ex.com/story/9320396/some-headline/"
    )


def test_id_after_date_segments_still_fires():
    """presstv shape: /Detail/YYYY/MM/DD/<id>/<slug> — the id governs."""
    a = canonical_url("https://presstv.ir/Detail/2026/07/18/772525/Ayatollah-")
    b = canonical_url(
        "https://presstv.ir/Detail/2026/07/18/772525/US-violations-of-MoU"
    )
    assert a == b == "https://presstv.ir/Detail/2026/07/18/772525"


def test_idempotent():
    once = canonical_url("https://www.dawn.com/news/2020547/some-slug")
    assert canonical_url(once) == once


# ---------------------------------------------------------------------------
# F3 — where the rule REFUSES (the safety half)
# ---------------------------------------------------------------------------


def test_a_bare_trailing_id_is_untouched_byte_for_byte():
    """t.me posts end in a numeric id with no slug — nothing to strip, and
    the stored identity must not move."""
    url = "https://t.me/ansarollah1/426510"
    assert canonical_url(url) == url


def test_date_shaped_segments_never_read_as_article_ids():
    """A YYYYMMDD (or YYYYMM) segment is a publication date shared by that
    whole day's output — collapsing on it would mint FALSE duplicates, the
    destructive direction this module refuses."""
    for url in (
        "https://ex.com/20260806/some-story",
        "https://ex.com/202608/some-story",
        "https://ex.com/2026/08/06/some-story",
    ):
        assert canonical_url(url) == url


def test_deep_hierarchy_after_the_id_is_untouched():
    """More than one segment after the number — shape unknown, hands off
    (the mainichi /articles/YYYYMMDD/p2g/00m/... class)."""
    url = "https://mainichi.jp/articles/20260806/p2g/00m/0in/033000c"
    assert canonical_url(url) == url
    url2 = "https://ex.com/123456/section/some-story"
    assert canonical_url(url2) == url2


def test_short_numbers_and_numeric_trailers_are_untouched():
    for url in (
        "https://ex.com/article/12345",  # 5 digits — below the id floor
        "https://ex.com/123456/789",  # a second number is not a slug
    ):
        assert canonical_url(url) == url


def test_query_identity_is_preserved():
    a = canonical_url("https://ex.com/news/2020547/slug?page=2")
    b = canonical_url("https://ex.com/news/2020547/slug?page=3")
    assert a != b  # the rule is path-only; a query variant stays distinct


# ---------------------------------------------------------------------------
# F3 — the matcher's batch dedupe now keys on the same identity
# ---------------------------------------------------------------------------


def _row(url):
    return {"canonical_url": url}


def test_claim_watch_batch_dedupe_sees_through_the_slug():
    """``dedupe_by_canonical_url`` had drifted from CW-7: it compared stored
    BYTES while every other dedupe surface compared canonical identity. The
    R4 F3 pair now collapses in-batch, newest kept, drop counted."""
    old = _row("https://www.dawn.com/news/2020547/old-headline")
    other = _row("https://ex.com/other/999999/story")
    new = _row("https://dawn.com/news/2020547/new-headline")
    kept, dropped = cw.dedupe_by_canonical_url([old, other, new])
    assert kept == [other, new]
    assert kept[1] is new  # the NEWEST occurrence survives, identity-checked
    assert dropped == 1


def test_claim_watch_batch_dedupe_still_never_keys_absent_urls():
    rows = [_row(None), _row(""), _row("   ")]
    kept, dropped = cw.dedupe_by_canonical_url(rows)
    assert kept == rows and dropped == 0


# ---------------------------------------------------------------------------
# F4 — the URL-embedded date read
# ---------------------------------------------------------------------------


def test_the_r4_yonhap_exhibit_reads_its_2025_date():
    """The report's F4 row: a fresh Aug-2026 body under a 2025-dated wire id."""
    assert url_embedded_date(
        "https://en.yna.co.kr/view/AEN20250710005751315"
    ) == date(2025, 7, 10)


def test_date_segment_and_dashed_slug_shapes():
    assert url_embedded_date(
        "https://presstv.ir/Detail/2026/07/18/772525/x"
    ) == date(2026, 7, 18)
    assert url_embedded_date(
        "https://ex.com/news/2026-08-05-something-happened"
    ) == date(2026, 8, 5)


def test_no_claim_reads_as_no_date():
    """An article id is not a date, an invalid date is not a date, and an
    absent claim is not a claim — a fabricated parse here would put false
    rows in the F4 audit."""
    assert url_embedded_date("https://dawn.com/news/2020547/slug") is None
    assert url_embedded_date("https://ex.com/a/20260230/x") is None  # Feb 30
    assert url_embedded_date("https://ex.com/plain/path") is None
    assert url_embedded_date("") is None


def test_digits_inside_a_longer_run_never_fabricate_a_date():
    """The compact pattern anchors at the digit run's START — a date-shaped
    substring in the middle of an id must not read as a claim."""
    assert url_embedded_date("https://ex.com/id/91520260806001") is None

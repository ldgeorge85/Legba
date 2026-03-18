"""
Unit tests for the ingestion dedup module.

No external services required — Postgres pool is mocked throughout.
"""

from __future__ import annotations

import pytest
from unittest.mock import AsyncMock

from legba.ingestion.dedup import (
    DedupEngine,
    DedupResult,
    _jaccard,
    _strip_title,
    _title_words,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pool(
    fetchrow_side_effect=None,
    fetch_side_effect=None,
) -> AsyncMock:
    """Build a mock asyncpg pool with configurable fetch behaviour."""
    pool = AsyncMock()
    if fetchrow_side_effect is not None:
        pool.fetchrow.side_effect = fetchrow_side_effect
    else:
        pool.fetchrow.return_value = None
    if fetch_side_effect is not None:
        pool.fetch.side_effect = fetch_side_effect
    else:
        pool.fetch.return_value = []
    return pool


def _engine(pool=None, cache_titles: list[str] | None = None) -> DedupEngine:
    """Create a DedupEngine with an optional pre-populated title cache."""
    eng = DedupEngine(pool or _make_pool(), cache_size=500)
    if cache_titles:
        for t in cache_titles:
            eng.add_to_cache(t)
    return eng


# ===========================================================================
# Title normalization — _strip_title
# ===========================================================================


class TestStripTitle:
    """Unit tests for source-suffix and prefix stripping."""

    def test_strips_bbc_suffix(self):
        assert _strip_title("UK economy grows faster than expected - BBC News") == \
            "UK economy grows faster than expected"

    def test_strips_reuters_suffix(self):
        assert _strip_title("Oil prices surge amid Middle East tensions - Reuters") == \
            "Oil prices surge amid Middle East tensions"

    def test_strips_al_jazeera_suffix_pipe(self):
        assert _strip_title("Gaza ceasefire talks stall | Al Jazeera") == \
            "Gaza ceasefire talks stall"

    def test_strips_cnn_suffix(self):
        assert _strip_title("Hurricane makes landfall in Florida - CNN") == \
            "Hurricane makes landfall in Florida"

    def test_strips_guardian_suffix(self):
        assert _strip_title("Brexit deal faces new hurdles - The Guardian") == \
            "Brexit deal faces new hurdles"

    def test_strips_nyt_suffix(self):
        assert _strip_title("Senate passes infrastructure bill - The New York Times") == \
            "Senate passes infrastructure bill"

    def test_strips_ap_news_suffix(self):
        assert _strip_title("Wildfires rage across California - AP News") == \
            "Wildfires rage across California"

    def test_strips_yahoo_news_suffix(self):
        assert _strip_title("Markets tumble on inflation fears - Yahoo News") == \
            "Markets tumble on inflation fears"

    def test_strips_em_dash_separator(self):
        assert _strip_title("Title here \u2014 BBC News") == "Title here"

    def test_strips_en_dash_separator(self):
        assert _strip_title("Title here \u2013 Reuters") == "Title here"

    def test_case_insensitive_suffix(self):
        assert _strip_title("Some headline - bbc news") == "Some headline"

    def test_strips_breaking_prefix(self):
        assert _strip_title("Breaking: Major earthquake hits Turkey") == \
            "Major earthquake hits Turkey"

    def test_strips_live_updates_prefix(self):
        assert _strip_title("Live updates: Election results coming in") == \
            "Election results coming in"

    def test_strips_watch_prefix(self):
        assert _strip_title("Watch: SpaceX rocket launch") == "SpaceX rocket launch"

    def test_strips_exclusive_prefix(self):
        assert _strip_title("Exclusive: CEO reveals plans") == "CEO reveals plans"

    def test_strips_both_prefix_and_suffix(self):
        result = _strip_title("Breaking: Major event happens - BBC News")
        assert result == "Major event happens"

    def test_empty_title(self):
        assert _strip_title("") == ""

    def test_title_with_no_known_suffix(self):
        assert _strip_title("A normal title about something") == \
            "A normal title about something"

    def test_title_that_is_only_whitespace(self):
        assert _strip_title("   ") == ""

    def test_internal_dashes_not_stripped(self):
        # Only trailing source suffixes should be removed
        result = _strip_title("US-China trade war escalates further")
        assert result == "US-China trade war escalates further"


# ===========================================================================
# _title_words
# ===========================================================================


class TestTitleWords:
    """Unit tests for title word extraction / normalization."""

    def test_basic_word_extraction(self):
        words = _title_words("Russia launches new offensive in Ukraine")
        assert "russia" in words
        assert "launches" in words
        assert "ukraine" in words
        # Stopwords removed
        assert "in" not in words
        assert "new" in words  # "new" is not in the stopword list

    def test_strips_suffix_before_extraction(self):
        words = _title_words("Oil prices surge - Reuters")
        assert "reuters" not in words

    def test_strips_punctuation(self):
        words = _title_words('Market "crashes" amid (panic)!')
        assert "crashes" in words
        assert "panic" in words

    def test_removes_single_char_words(self):
        words = _title_words("A B C real words here")
        # Single character words (after stripping) should be excluded
        assert "real" in words
        assert "words" in words

    def test_empty_title_returns_empty_set(self):
        assert _title_words("") == set()

    def test_all_stopwords_returns_empty_set(self):
        assert _title_words("a the of in on at to for") == set()


# ===========================================================================
# _jaccard
# ===========================================================================


class TestJaccard:
    """Unit tests for Jaccard similarity computation."""

    def test_identical_sets(self):
        s = {"russia", "launches", "offensive"}
        assert _jaccard(s, s) == 1.0

    def test_completely_disjoint(self):
        a = {"russia", "launches", "offensive"}
        b = {"markets", "tumble", "inflation"}
        assert _jaccard(a, b) == 0.0

    def test_partial_overlap(self):
        a = {"russia", "launches", "offensive", "ukraine"}
        b = {"russia", "new", "offensive", "kyiv"}
        # Intersection: {russia, offensive} = 2
        # Union: {russia, launches, offensive, ukraine, new, kyiv} = 6
        assert _jaccard(a, b) == pytest.approx(2 / 6)

    def test_empty_first_set(self):
        assert _jaccard(set(), {"a", "b"}) == 0.0

    def test_empty_second_set(self):
        assert _jaccard({"a", "b"}, set()) == 0.0

    def test_both_empty(self):
        assert _jaccard(set(), set()) == 0.0


# ===========================================================================
# Tier 1 — GUID dedup
# ===========================================================================


class TestGuidDedup:
    """Tier 1: GUID exact match via single-entry check()."""

    @pytest.mark.asyncio
    async def test_matching_guid_detected(self):
        pool = _make_pool()
        # First fetchrow call (GUID lookup) returns a match
        pool.fetchrow.return_value = {"title": "Existing article"}
        eng = DedupEngine(pool)

        result = await eng.check("guid-123", "", "Some title")

        assert result.is_duplicate is True
        assert result.match_tier == "guid"
        assert result.matched_title == "Existing article"

    @pytest.mark.asyncio
    async def test_no_guid_skips_tier1(self):
        pool = _make_pool()
        pool.fetchrow.return_value = None
        eng = DedupEngine(pool)

        result = await eng.check("", "", "Some title")

        assert result.is_duplicate is False
        # GUID query should not have been called with empty guid
        # (fetchrow might be called for URL check, but not GUID)

    @pytest.mark.asyncio
    async def test_guid_no_match_falls_through(self):
        pool = _make_pool()
        pool.fetchrow.return_value = None
        eng = DedupEngine(pool)

        result = await eng.check("guid-xyz", "", "Some title")

        assert result.is_duplicate is False

    @pytest.mark.asyncio
    async def test_guid_db_error_falls_through(self):
        """DB errors should not crash — just skip the tier."""
        pool = _make_pool()
        pool.fetchrow.side_effect = Exception("connection lost")
        eng = DedupEngine(pool)

        result = await eng.check("guid-123", "", "Some title")

        # Should not raise; falls through to no-match
        assert result.is_duplicate is False


# ===========================================================================
# Tier 2 — source_url dedup
# ===========================================================================


class TestSourceUrlDedup:
    """Tier 2: source_url exact match via single-entry check()."""

    @pytest.mark.asyncio
    async def test_matching_url_detected(self):
        pool = _make_pool()
        # First call (GUID) returns None, second call (URL) returns a match
        pool.fetchrow.side_effect = [
            None,  # GUID lookup
            {"title": "URL match article"},  # URL lookup
        ]
        eng = DedupEngine(pool)

        result = await eng.check("", "https://example.com/article", "Some title")

        assert result.is_duplicate is True
        assert result.match_tier == "source_url"
        assert result.matched_title == "URL match article"

    @pytest.mark.asyncio
    async def test_no_url_skips_tier2(self):
        pool = _make_pool()
        pool.fetchrow.return_value = None
        eng = DedupEngine(pool)

        result = await eng.check("", "", "Some title")

        assert result.is_duplicate is False

    @pytest.mark.asyncio
    async def test_guid_match_short_circuits_url(self):
        """If GUID matches, source_url should not even be checked."""
        pool = _make_pool()
        pool.fetchrow.return_value = {"title": "GUID match"}
        eng = DedupEngine(pool)

        result = await eng.check("guid-1", "https://example.com", "Title")

        assert result.match_tier == "guid"
        # fetchrow called only once (for GUID) — no URL query
        assert pool.fetchrow.call_count == 1


# ===========================================================================
# Tier 4 — Jaccard title similarity
# ===========================================================================


class TestJaccardTitleDedup:
    """Tier 4: Jaccard title similarity against in-memory cache."""

    @pytest.mark.asyncio
    async def test_identical_title_detected(self):
        eng = _engine(cache_titles=[
            "Russia launches new military offensive in eastern Ukraine",
        ])

        result = await eng.check(
            "", "",
            "Russia launches new military offensive in eastern Ukraine",
        )

        assert result.is_duplicate is True
        assert result.match_tier == "title_similarity"
        assert result.similarity == 1.0

    @pytest.mark.asyncio
    async def test_slightly_different_title_detected(self):
        eng = _engine(cache_titles=[
            "Russia launches new military offensive in eastern Ukraine",
        ])

        # Same story, minor wording change
        result = await eng.check(
            "", "",
            "Russia launches fresh military offensive in east Ukraine",
        )

        assert result.is_duplicate is True
        assert result.match_tier == "title_similarity"
        assert result.similarity > 0.5

    @pytest.mark.asyncio
    async def test_completely_different_title_not_duplicate(self):
        eng = _engine(cache_titles=[
            "Russia launches new military offensive in eastern Ukraine",
        ])

        result = await eng.check(
            "", "",
            "Apple announces new MacBook Pro with M5 chip at WWDC",
        )

        assert result.is_duplicate is False

    @pytest.mark.asyncio
    async def test_source_suffix_stripping_enables_match(self):
        """Titles that differ only by source suffix should be detected."""
        eng = _engine(cache_titles=[
            "Oil prices surge amid Middle East tensions - Reuters",
        ])

        result = await eng.check(
            "", "",
            "Oil prices surge amid Middle East tensions - BBC News",
        )

        assert result.is_duplicate is True
        assert result.match_tier == "title_similarity"
        # After stripping, titles are identical
        assert result.similarity == 1.0

    @pytest.mark.asyncio
    async def test_short_title_uses_lower_threshold(self):
        """Titles with <= 5 content words use 0.4 threshold instead of 0.5."""
        eng = _engine(cache_titles=[
            "Market crash fears grow",
        ])

        # 3 of 4 content words overlap = 0.6 Jaccard — above 0.4 threshold
        result = await eng.check("", "", "Market crash fears intensify")

        assert result.is_duplicate is True
        assert result.similarity >= 0.4

    @pytest.mark.asyncio
    async def test_short_title_below_threshold_not_duplicate(self):
        """Even with the lower 0.4 threshold, poor overlap is rejected."""
        eng = _engine(cache_titles=[
            "Market crash today",
        ])

        result = await eng.check("", "", "Economy grows rapidly")

        assert result.is_duplicate is False

    @pytest.mark.asyncio
    async def test_title_all_stopwords_skips_jaccard(self):
        """Titles that reduce to zero content words skip Jaccard entirely."""
        eng = _engine(cache_titles=[
            "Russia launches offensive",
        ])

        # All stopwords — no content words after filtering
        result = await eng.check("", "", "the of a in")

        assert result.is_duplicate is False

    @pytest.mark.asyncio
    async def test_empty_title_skips_jaccard(self):
        eng = _engine(cache_titles=[
            "Some cached title",
        ])

        result = await eng.check("", "", "")

        assert result.is_duplicate is False


# ===========================================================================
# Cross-tier fallback order
# ===========================================================================


class TestCrossTierFallback:
    """Verify tiers are checked in the correct priority order."""

    @pytest.mark.asyncio
    async def test_guid_takes_priority_over_url_and_title(self):
        """GUID match should win even if URL and title would also match."""
        pool = _make_pool()
        pool.fetchrow.return_value = {"title": "GUID match"}
        eng = DedupEngine(pool)
        eng.add_to_cache("Matching title here with many words for Jaccard")

        result = await eng.check(
            "guid-1",
            "https://example.com/article",
            "Matching title here with many words for Jaccard",
        )

        assert result.match_tier == "guid"

    @pytest.mark.asyncio
    async def test_url_takes_priority_over_title(self):
        """URL match should win if GUID doesn't match."""
        pool = _make_pool()
        # GUID lookup returns None, URL lookup returns match
        pool.fetchrow.side_effect = [
            None,
            {"title": "URL match"},
        ]
        eng = DedupEngine(pool)
        eng.add_to_cache("Matching title here with many words for Jaccard")

        result = await eng.check(
            "no-match-guid",
            "https://example.com/article",
            "Matching title here with many words for Jaccard",
        )

        assert result.match_tier == "source_url"

    @pytest.mark.asyncio
    async def test_title_similarity_used_when_guid_and_url_miss(self):
        """When no GUID or URL match, title similarity should kick in."""
        pool = _make_pool()
        pool.fetchrow.return_value = None
        eng = DedupEngine(pool)
        eng.add_to_cache("Russia launches massive new offensive in eastern Ukraine")

        result = await eng.check(
            "no-match",
            "https://no-match.com",
            "Russia launches massive new offensive in eastern Ukraine",
        )

        assert result.match_tier == "title_similarity"


# ===========================================================================
# Batch dedup — check_batch (against existing DB)
# ===========================================================================


class TestCheckBatch:
    """Batch dedup against existing signals in DB."""

    @pytest.mark.asyncio
    async def test_batch_guid_match(self):
        pool = _make_pool()
        pool.fetch.side_effect = [
            # GUID bulk query result
            [{"guid": "g1", "title": "Existing G1"}],
            # URL bulk query result
            [],
        ]
        eng = DedupEngine(pool)

        entries = [
            ("g1", "", "Title 1"),
            ("g2", "", "Title 2"),
        ]
        results = await eng.check_batch(entries)

        assert results[0].is_duplicate is True
        assert results[0].match_tier == "guid"
        assert results[1].is_duplicate is False

    @pytest.mark.asyncio
    async def test_batch_url_match(self):
        pool = _make_pool()
        pool.fetch.side_effect = [
            # GUID bulk query result
            [],
            # URL bulk query result
            [{"source_url": "https://example.com/a", "title": "Existing URL"}],
        ]
        eng = DedupEngine(pool)

        entries = [
            ("", "https://example.com/a", "Title A"),
            ("", "https://example.com/b", "Title B"),
        ]
        results = await eng.check_batch(entries)

        assert results[0].is_duplicate is True
        assert results[0].match_tier == "source_url"
        assert results[1].is_duplicate is False

    @pytest.mark.asyncio
    async def test_batch_title_similarity(self):
        pool = _make_pool()
        pool.fetch.return_value = []
        eng = DedupEngine(pool)
        eng.add_to_cache("Russia launches offensive in eastern Ukraine regions")

        entries = [
            ("", "", "Russia launches offensive in eastern Ukraine regions"),
            ("", "", "Apple announces new MacBook with M5 processor"),
        ]
        results = await eng.check_batch(entries)

        assert results[0].is_duplicate is True
        assert results[0].match_tier == "title_similarity"
        assert results[1].is_duplicate is False

    @pytest.mark.asyncio
    async def test_batch_guid_priority_over_url(self):
        """GUID match should prevent URL check within batch processing."""
        pool = _make_pool()
        pool.fetch.side_effect = [
            [{"guid": "g1", "title": "GUID match"}],
            [{"source_url": "https://example.com/a", "title": "URL match"}],
        ]
        eng = DedupEngine(pool)

        entries = [
            ("g1", "https://example.com/a", "Some title"),
        ]
        results = await eng.check_batch(entries)

        # GUID wins
        assert results[0].match_tier == "guid"

    @pytest.mark.asyncio
    async def test_batch_db_error_handled_gracefully(self):
        """DB errors in batch queries should not crash."""
        pool = _make_pool()
        pool.fetch.side_effect = Exception("connection lost")
        eng = DedupEngine(pool)

        entries = [("g1", "https://example.com", "Some title")]
        results = await eng.check_batch(entries)

        # No crash; everything falls through to non-duplicate
        assert len(results) == 1
        assert results[0].is_duplicate is False


# ===========================================================================
# Batch internal dedup — check_batch_internal (within same batch)
# ===========================================================================


class TestCheckBatchInternal:
    """Intra-batch dedup: detect duplicates among entries in the same batch."""

    @pytest.mark.asyncio
    async def test_duplicate_guid_within_batch(self):
        eng = _engine()

        entries = [
            ("g1", "", "Title A"),
            ("g1", "", "Title B"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert 1 in dupes
        assert dupes[1].is_duplicate is True
        assert dupes[1].match_tier == "batch_guid"
        assert 0 not in dupes  # First occurrence is not a dupe

    @pytest.mark.asyncio
    async def test_duplicate_url_within_batch(self):
        eng = _engine()

        entries = [
            ("", "https://example.com/article", "Title A"),
            ("", "https://example.com/article", "Title B"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert 1 in dupes
        assert dupes[1].match_tier == "batch_source_url"

    @pytest.mark.asyncio
    async def test_duplicate_title_within_batch(self):
        eng = _engine()

        entries = [
            ("", "", "Russia launches massive offensive in eastern Ukraine"),
            ("", "", "Russia launches massive offensive in eastern Ukraine"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert 1 in dupes
        assert dupes[1].match_tier == "batch_title"
        assert dupes[1].similarity == 1.0

    @pytest.mark.asyncio
    async def test_similar_titles_within_batch(self):
        eng = _engine()

        entries = [
            ("", "", "Russia launches massive offensive in eastern Ukraine"),
            ("", "", "Russia launches large offensive in east Ukraine"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert 1 in dupes
        assert dupes[1].match_tier == "batch_title"
        assert dupes[1].similarity > 0.5

    @pytest.mark.asyncio
    async def test_no_duplicates_in_batch(self):
        eng = _engine()

        entries = [
            ("g1", "https://a.com", "Russia launches offensive in Ukraine"),
            ("g2", "https://b.com", "Apple releases new MacBook Pro M5"),
            ("g3", "https://c.com", "NASA discovers water on distant exoplanet"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert len(dupes) == 0

    @pytest.mark.asyncio
    async def test_batch_guid_priority_over_title(self):
        """GUID match within batch should take priority."""
        eng = _engine()

        entries = [
            ("g1", "", "Russia launches offensive in eastern Ukraine"),
            ("g1", "", "Russia launches offensive in eastern Ukraine"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert dupes[1].match_tier == "batch_guid"

    @pytest.mark.asyncio
    async def test_only_first_kept_rest_are_dupes(self):
        """With 3 identical entries, #0 is kept, #1 and #2 are dupes."""
        eng = _engine()

        entries = [
            ("g1", "", "Title"),
            ("g1", "", "Title"),
            ("g1", "", "Title"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert 0 not in dupes
        assert 1 in dupes
        assert 2 in dupes

    @pytest.mark.asyncio
    async def test_source_suffix_stripping_in_batch(self):
        """Titles differing only by source suffix should match within batch."""
        eng = _engine()

        entries = [
            ("", "", "Oil prices surge amid tensions - Reuters"),
            ("", "", "Oil prices surge amid tensions - BBC News"),
        ]
        dupes = await eng.check_batch_internal(entries)

        assert 1 in dupes
        assert dupes[1].match_tier == "batch_title"


# ===========================================================================
# add_to_cache
# ===========================================================================


class TestAddToCache:
    """Test the in-memory cache management."""

    def test_add_title_to_cache(self):
        eng = DedupEngine(_make_pool(), cache_size=5)
        eng.add_to_cache("New headline about something important")

        assert len(eng._title_cache) == 1
        words, title = eng._title_cache[0]
        assert "headline" in words
        assert title == "New headline about something important"

    def test_cache_size_limit(self):
        eng = DedupEngine(_make_pool(), cache_size=3)
        for i in range(5):
            eng.add_to_cache(f"Unique headline number {i} about something")

        assert len(eng._title_cache) == 3
        # Most recent should be first
        assert eng._title_cache[0][1] == "Unique headline number 4 about something"

    def test_empty_title_words_not_added(self):
        """Titles that reduce to no content words should not be cached."""
        eng = DedupEngine(_make_pool(), cache_size=10)
        eng.add_to_cache("the of a")  # all stopwords

        assert len(eng._title_cache) == 0


# ===========================================================================
# load_cache
# ===========================================================================


class TestLoadCache:
    """Test the async cache loading from Postgres."""

    @pytest.mark.asyncio
    async def test_load_cache_populates_titles(self):
        pool = _make_pool()
        pool.fetch.return_value = [
            {"title": "First headline about important events"},
            {"title": "Second headline about other things"},
        ]
        eng = DedupEngine(pool, cache_size=100)

        await eng.load_cache()

        assert len(eng._title_cache) == 2

    @pytest.mark.asyncio
    async def test_load_cache_skips_empty_titles(self):
        pool = _make_pool()
        pool.fetch.return_value = [
            {"title": "Real headline here with words"},
            {"title": ""},
            {"title": None},
        ]
        eng = DedupEngine(pool, cache_size=100)

        await eng.load_cache()

        assert len(eng._title_cache) == 1

    @pytest.mark.asyncio
    async def test_load_cache_handles_db_error(self):
        pool = _make_pool()
        pool.fetch.side_effect = Exception("connection refused")
        eng = DedupEngine(pool, cache_size=100)

        await eng.load_cache()

        # Should not raise; cache should be empty
        assert eng._title_cache == []


# ===========================================================================
# DedupResult
# ===========================================================================


class TestDedupResult:
    """Basic tests for the DedupResult dataclass."""

    def test_defaults(self):
        r = DedupResult()
        assert r.is_duplicate is False
        assert r.match_tier == ""
        assert r.matched_title == ""
        assert r.similarity == 0.0

    def test_custom_values(self):
        r = DedupResult(
            is_duplicate=True,
            match_tier="guid",
            matched_title="Some title",
            similarity=0.0,
        )
        assert r.is_duplicate is True
        assert r.match_tier == "guid"

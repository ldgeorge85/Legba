"""3-tier event deduplication engine.

Extracted from agent event_tools.py, adapted for batch processing.

Tiers:
  1. GUID exact match (RSS <guid> or Atom <id>)
  2. source_url exact match
  3. Adaptive Jaccard title similarity (0.4 for short titles, 0.5 for normal)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

import asyncpg

logger = logging.getLogger(__name__)

_STOPWORDS = frozenset(
    "a an the of in on at to for and or is was by from with this that "
    "it its be are were has had have been not but they their them he she "
    "his her we our you your as".split()
)


def _title_words(title: str) -> set[str]:
    """Normalize title to a set of content words for Jaccard comparison."""
    words = set()
    for w in title.lower().split():
        w = w.strip(".,;:!?\"'()[]{}—–-")
        if w and len(w) > 1 and w not in _STOPWORDS:
            words.add(w)
    return words


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard similarity between two word sets."""
    if not a or not b:
        return 0.0
    intersection = len(a & b)
    union = len(a | b)
    return intersection / union if union > 0 else 0.0


@dataclass
class DedupResult:
    """Result of dedup check for a single entry."""

    is_duplicate: bool = False
    match_tier: str = ""  # "guid", "source_url", "title_similarity"
    matched_title: str = ""
    similarity: float = 0.0


class DedupEngine:
    """Stateful dedup engine with in-memory title cache for batch efficiency.

    Loads recent event titles from Postgres at startup, then checks new
    entries against the cache without per-entry DB queries for tier 3.
    Tiers 1 and 2 still hit the DB (indexed lookups, fast).
    """

    def __init__(self, pool: asyncpg.Pool, cache_size: int = 500):
        self._pool = pool
        self._cache_size = cache_size
        # In-memory cache: list of (title_words_set, original_title)
        self._title_cache: list[tuple[set[str], str]] = []

    async def load_cache(self) -> None:
        """Load recent event titles into memory for Jaccard dedup."""
        try:
            rows = await self._pool.fetch(
                "SELECT title FROM events ORDER BY created_at DESC LIMIT $1",
                self._cache_size,
            )
            self._title_cache = [
                (_title_words(row["title"]), row["title"])
                for row in rows
                if row["title"]
            ]
            logger.info("Dedup cache loaded: %d titles", len(self._title_cache))
        except Exception as e:
            logger.error("Failed to load dedup cache: %s", e)
            self._title_cache = []

    def add_to_cache(self, title: str) -> None:
        """Add a newly stored event title to the in-memory cache."""
        words = _title_words(title)
        if words:
            self._title_cache.insert(0, (words, title))
            # Trim to cache size
            if len(self._title_cache) > self._cache_size:
                self._title_cache = self._title_cache[:self._cache_size]

    async def check(self, guid: str, source_url: str, title: str) -> DedupResult:
        """Check if an entry is a duplicate using the 3-tier strategy.

        Args:
            guid: RSS/Atom GUID (may be empty)
            source_url: Article URL (may be empty)
            title: Entry title

        Returns:
            DedupResult with is_duplicate=True if duplicate found.
        """
        # Tier 1: GUID exact match
        if guid:
            try:
                row = await self._pool.fetchrow(
                    "SELECT title FROM events WHERE guid = $1 LIMIT 1", guid,
                )
                if row:
                    return DedupResult(
                        is_duplicate=True,
                        match_tier="guid",
                        matched_title=row["title"],
                    )
            except Exception:
                pass

        # Tier 2: source_url exact match
        if source_url:
            try:
                row = await self._pool.fetchrow(
                    "SELECT title FROM events WHERE source_url = $1 LIMIT 1", source_url,
                )
                if row:
                    return DedupResult(
                        is_duplicate=True,
                        match_tier="source_url",
                        matched_title=row["title"],
                    )
            except Exception:
                pass

        # Tier 3: Jaccard title similarity (in-memory)
        if title:
            words = _title_words(title)
            if words:
                threshold = 0.4 if len(words) <= 5 else 0.5
                for cached_words, cached_title in self._title_cache:
                    sim = _jaccard(words, cached_words)
                    if sim >= threshold:
                        return DedupResult(
                            is_duplicate=True,
                            match_tier="title_similarity",
                            matched_title=cached_title,
                            similarity=sim,
                        )

        return DedupResult(is_duplicate=False)

    async def check_batch_internal(self, entries: list[tuple[str, str, str]]) -> dict[int, DedupResult]:
        """Check entries against each other within a batch (prevents same-batch dupes).

        Args:
            entries: List of (guid, source_url, title) tuples

        Returns:
            Dict mapping entry index to DedupResult for duplicates found within the batch.
        """
        seen_guids: dict[str, int] = {}
        seen_urls: dict[str, int] = {}
        seen_titles: list[tuple[int, set[str], str]] = []
        duplicates: dict[int, DedupResult] = {}

        for i, (guid, source_url, title) in enumerate(entries):
            # Check GUID within batch
            if guid and guid in seen_guids:
                duplicates[i] = DedupResult(
                    is_duplicate=True,
                    match_tier="batch_guid",
                    matched_title=entries[seen_guids[guid]][2],
                )
                continue

            # Check URL within batch
            if source_url and source_url in seen_urls:
                duplicates[i] = DedupResult(
                    is_duplicate=True,
                    match_tier="batch_source_url",
                    matched_title=entries[seen_urls[source_url]][2],
                )
                continue

            # Check title within batch
            words = _title_words(title)
            if words:
                threshold = 0.4 if len(words) <= 5 else 0.5
                for j, cached_words, cached_title in seen_titles:
                    sim = _jaccard(words, cached_words)
                    if sim >= threshold:
                        duplicates[i] = DedupResult(
                            is_duplicate=True,
                            match_tier="batch_title",
                            matched_title=cached_title,
                            similarity=sim,
                        )
                        break

            if i not in duplicates:
                if guid:
                    seen_guids[guid] = i
                if source_url:
                    seen_urls[source_url] = i
                if words:
                    seen_titles.append((i, words, title))

        return duplicates

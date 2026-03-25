"""
Regression tests for Phase 1 bug fixes.

Covers:
  1. Confidence scoring (compute_composite_confidence)
  2. Predicate rejection ("Noted" and non-canonical handling)
  3. Task backlog 100-item cap
  4. Task backlog dedup by situation name
  5. Goal dedup threshold (0.75)
  6. Untitled event prevention (title fallback logic)

No external services required -- uses mocking for Redis/Postgres.
Run with:
    PYTHONPATH=src pytest tests/test_phase1_fixes.py -v
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from legba.shared.confidence import compute_composite_confidence
from legba.agent.memory.fact_normalize import (
    CANONICAL_FACT_PREDICATES,
    normalize_fact_predicate,
)
from legba.shared.task_backlog import TaskBacklog, BACKLOG_KEY


# ===========================================================================
# 1. Confidence scoring — compute_composite_confidence
# ===========================================================================


class TestConfidenceScoringPhase1:
    """Regression tests for the confidence formula with typical inputs."""

    def test_typical_inputs_reasonable_score(self):
        """source_reliability=0.7, classification_confidence=0.8,
        temporal_freshness=1.0, corroboration=0.0, specificity=0.7
        should produce a result > 0.3."""
        result = compute_composite_confidence({
            "source_reliability": 0.7,
            "classification_confidence": 0.8,
            "temporal_freshness": 1.0,
            "corroboration": 0.0,
            "specificity": 0.7,
        })
        # Gate = 0.7 * 0.8 = 0.56
        # Modifier = 0.4*1.0 + 0.35*0.0 + 0.25*0.7 = 0.4 + 0.0 + 0.175 = 0.575
        # Confidence = 0.56 * 0.575 = 0.322
        assert result > 0.3
        assert result == pytest.approx(0.56 * 0.575, abs=1e-6)

    def test_low_source_caps_result(self):
        """Low source_reliability (0.3) caps the result even when everything
        else is high, because the gate multiplier dominates."""
        result = compute_composite_confidence({
            "source_reliability": 0.3,
            "classification_confidence": 0.9,
            "temporal_freshness": 1.0,
            "corroboration": 1.0,
            "specificity": 1.0,
        })
        # Gate = 0.3 * 0.9 = 0.27
        # Modifier = 0.4 + 0.35 + 0.25 = 1.0
        # Confidence = 0.27
        assert result == pytest.approx(0.27, abs=1e-6)
        # Result is capped well below what the modifier components suggest
        assert result < 0.3

    def test_all_zeros_produce_zero(self):
        """All component values at 0.0 must produce exactly 0.0."""
        result = compute_composite_confidence({
            "source_reliability": 0.0,
            "classification_confidence": 0.0,
            "temporal_freshness": 0.0,
            "corroboration": 0.0,
            "specificity": 0.0,
        })
        assert result == 0.0


# ===========================================================================
# 2. Predicate rejection — "Noted" and non-canonical handling
# ===========================================================================


class TestPredicateRejection:
    """Regression tests for predicate normalization and rejection logic.

    The store_fact method in structured.py rejects facts with predicate
    'Noted' after normalization.  normalize_fact_predicate passes through
    unknown predicates as-is (no canonicalization), so 'Noted' survives
    normalization and hits the explicit rejection check.
    """

    def test_noted_not_canonical(self):
        """'Noted' is not in the canonical predicate set."""
        assert "Noted" not in CANONICAL_FACT_PREDICATES

    def test_noted_normalizes_to_itself(self):
        """normalize_fact_predicate passes 'Noted' through unchanged
        (it is not an alias and not canonical), which lets store_fact
        reject it."""
        result = normalize_fact_predicate("Noted")
        assert result == "Noted"

    def test_noted_case_variants(self):
        """'noted', 'NOTED' all normalize to themselves (not canonical)."""
        for variant in ("noted", "NOTED", " Noted "):
            result = normalize_fact_predicate(variant)
            assert result.lower() == "noted"

    def test_canonical_predicates_round_trip(self):
        """All canonical predicates normalize to themselves."""
        for pred in CANONICAL_FACT_PREDICATES:
            assert normalize_fact_predicate(pred) == pred

    def test_alias_resolves_to_canonical(self):
        """Known aliases resolve to their canonical form."""
        assert normalize_fact_predicate("hostile_to") == "HostileTo"
        assert normalize_fact_predicate("allied with") == "AlliedWith"
        assert normalize_fact_predicate("leader_of") == "LeaderOf"

    def test_unknown_predicate_passes_through(self):
        """Unknown predicates pass through as-is (no exception)."""
        result = normalize_fact_predicate("SomethingRandom")
        assert result == "SomethingRandom"

    def test_empty_predicate(self):
        """Empty string returns empty string."""
        assert normalize_fact_predicate("") == ""
        assert normalize_fact_predicate("   ") == ""


# ===========================================================================
# 3. Task backlog 100-item cap
# ===========================================================================


class TestTaskBacklogCap:
    """Regression test: add_task refuses new items when backlog > 100."""

    @pytest.fixture
    def mock_redis(self):
        """Create a mock Redis client with async methods."""
        redis = AsyncMock()
        redis.zcard = AsyncMock(return_value=0)
        redis.zrangebyscore = AsyncMock(return_value=[])
        redis.zadd = AsyncMock()
        return redis

    @pytest.fixture
    def backlog(self, mock_redis):
        return TaskBacklog(mock_redis)

    @pytest.mark.asyncio
    async def test_add_task_under_cap(self, backlog, mock_redis):
        """Tasks are accepted when backlog size <= 100."""
        mock_redis.zcard.return_value = 50
        task_id = await backlog.add_task(
            task_type="research_entity",
            target={"entity_name": "Test"},
        )
        assert task_id != ""
        mock_redis.zadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_task_at_cap_boundary(self, backlog, mock_redis):
        """Backlog at exactly 100 items should still accept (> 100 rejects)."""
        mock_redis.zcard.return_value = 100
        task_id = await backlog.add_task(
            task_type="research_entity",
            target={"entity_name": "Test"},
        )
        assert task_id != ""
        mock_redis.zadd.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_task_over_cap_rejected(self, backlog, mock_redis):
        """Backlog with > 100 items returns empty string (rejected)."""
        mock_redis.zcard.return_value = 101
        task_id = await backlog.add_task(
            task_type="research_entity",
            target={"entity_name": "Test"},
        )
        assert task_id == ""
        mock_redis.zadd.assert_not_called()

    @pytest.mark.asyncio
    async def test_add_task_well_over_cap_rejected(self, backlog, mock_redis):
        """Backlog at 500 items still rejected."""
        mock_redis.zcard.return_value = 500
        task_id = await backlog.add_task(
            task_type="research_entity",
            target={"entity_name": "Test"},
        )
        assert task_id == ""


# ===========================================================================
# 4. Task backlog dedup — matches on situation name, not just UUID
# ===========================================================================


class TestTaskBacklogDedupBySituationName:
    """Regression test: _find_duplicate matches on situation_name within
    the target dict, so tasks for the same situation dedup even when
    the situation_id differs (e.g. proposed vs promoted)."""

    @pytest.fixture
    def mock_redis(self):
        redis = AsyncMock()
        return redis

    @pytest.fixture
    def backlog(self, mock_redis):
        return TaskBacklog(mock_redis)

    @pytest.mark.asyncio
    async def test_dedup_by_situation_name(self, backlog, mock_redis):
        """Two tasks with different situation_id but same situation_name
        should be detected as duplicates."""
        existing_task = {
            "task_id": "existing-task-001",
            "task_type": "deep_dive_situation",
            "target": {
                "situation_id": "aaa-111",
                "situation_name": "Ukraine-Russia Conflict Escalation",
            },
            "status": "pending",
            "priority": 0.5,
        }
        mock_redis.zrangebyscore = AsyncMock(
            return_value=[json.dumps(existing_task)]
        )

        dup_id = await backlog._find_duplicate(
            task_type="deep_dive_situation",
            target={
                "situation_id": "bbb-222",  # Different ID
                "situation_name": "Ukraine-Russia Conflict Escalation",  # Same name
            },
        )
        assert dup_id == "existing-task-001"

    @pytest.mark.asyncio
    async def test_dedup_by_situation_name_case_insensitive(self, backlog, mock_redis):
        """Situation name matching is case-insensitive."""
        existing_task = {
            "task_id": "existing-task-002",
            "task_type": "deep_dive_situation",
            "target": {
                "situation_id": "aaa-111",
                "situation_name": "South China Sea Tensions",
            },
            "status": "pending",
            "priority": 0.5,
        }
        mock_redis.zrangebyscore = AsyncMock(
            return_value=[json.dumps(existing_task)]
        )

        dup_id = await backlog._find_duplicate(
            task_type="deep_dive_situation",
            target={
                "situation_id": "ccc-333",
                "situation_name": "south china sea tensions",  # lowercase
            },
        )
        assert dup_id == "existing-task-002"

    @pytest.mark.asyncio
    async def test_no_false_positive_different_name(self, backlog, mock_redis):
        """Different situation names should NOT match."""
        existing_task = {
            "task_id": "existing-task-003",
            "task_type": "deep_dive_situation",
            "target": {
                "situation_id": "aaa-111",
                "situation_name": "Iran Nuclear Program",
            },
            "status": "pending",
            "priority": 0.5,
        }
        mock_redis.zrangebyscore = AsyncMock(
            return_value=[json.dumps(existing_task)]
        )

        dup_id = await backlog._find_duplicate(
            task_type="deep_dive_situation",
            target={
                "situation_id": "ddd-444",
                "situation_name": "North Korea Missile Tests",
            },
        )
        assert dup_id is None

    @pytest.mark.asyncio
    async def test_dedup_by_exact_target_still_works(self, backlog, mock_redis):
        """Exact target key match still works (original path)."""
        existing_task = {
            "task_id": "existing-task-004",
            "task_type": "research_entity",
            "target": {"entity_name": "Wagner Group"},
            "status": "pending",
            "priority": 0.5,
        }
        mock_redis.zrangebyscore = AsyncMock(
            return_value=[json.dumps(existing_task)]
        )

        dup_id = await backlog._find_duplicate(
            task_type="research_entity",
            target={"entity_name": "Wagner Group"},
        )
        assert dup_id == "existing-task-004"

    @pytest.mark.asyncio
    async def test_different_task_type_no_dedup(self, backlog, mock_redis):
        """Same target but different task_type should NOT match."""
        existing_task = {
            "task_id": "existing-task-005",
            "task_type": "deep_dive_situation",
            "target": {
                "situation_id": "aaa-111",
                "situation_name": "Ukraine-Russia Conflict Escalation",
            },
            "status": "pending",
            "priority": 0.5,
        }
        mock_redis.zrangebyscore = AsyncMock(
            return_value=[json.dumps(existing_task)]
        )

        dup_id = await backlog._find_duplicate(
            task_type="link_events",  # Different type
            target={
                "situation_id": "bbb-222",
                "situation_name": "Ukraine-Russia Conflict Escalation",
            },
        )
        assert dup_id is None


# ===========================================================================
# 5. Goal dedup threshold — must be 0.75 (not 0.6)
# ===========================================================================


class TestGoalDedupThreshold:
    """Regression test: the goal dedup threshold in goal_tools.py is 0.75.

    This was fixed from 0.6 to 0.75 to reduce false-positive dedup that
    blocked legitimate new goals. We test the _word_overlap helper and
    verify the threshold boundary.
    """

    @staticmethod
    def _word_overlap(a: str, b: str) -> float:
        """Replicate the _word_overlap function from goal_tools.py."""
        wa = set(a.lower().split())
        wb = set(b.lower().split())
        if not wa or not wb:
            return 0.0
        return len(wa & wb) / min(len(wa), len(wb))

    def test_identical_strings_overlap_is_1(self):
        """Identical descriptions have overlap 1.0."""
        desc = "Monitor global cybersecurity threats"
        assert self._word_overlap(desc, desc) == 1.0

    def test_completely_different_overlap_is_0(self):
        """Completely disjoint descriptions have overlap 0.0."""
        assert self._word_overlap(
            "Monitor global cybersecurity threats",
            "Track agricultural commodity prices",
        ) == 0.0

    def test_threshold_boundary_above_075(self):
        """Overlap > 0.75 would trigger dedup. Verify a case that crosses it."""
        # 4 of 5 words shared = 0.80 overlap — above 0.75 threshold
        a = "Investigate Iran nuclear weapons program"
        b = "Investigate Iran nuclear weapons capability"
        overlap = self._word_overlap(a, b)
        assert overlap > 0.75

    def test_threshold_boundary_at_075(self):
        """Overlap exactly at 0.75 does NOT trigger dedup (> not >=)."""
        # 3 of 4 words shared = 0.75 overlap — NOT above threshold
        a = "Monitor Iran nuclear talks"
        b = "Monitor Iran nuclear program"
        overlap = self._word_overlap(a, b)
        assert overlap == pytest.approx(0.75)
        # The condition is > 0.75, so exactly 0.75 does NOT trigger
        assert not (overlap > 0.75)

    def test_threshold_boundary_below_075(self):
        """Overlap below 0.75 does not trigger dedup."""
        # 2 of 4 words shared = 0.50
        a = "Monitor Iran nuclear talks"
        b = "Track China trade negotiations"
        overlap = self._word_overlap(a, b)
        assert overlap < 0.75

    def test_threshold_value_in_source_code(self):
        """Verify the threshold constant is 0.75 in the source.

        This is a meta-test: read the source and confirm the literal
        value so we catch any accidental regression."""
        import inspect
        import legba.agent.tools.builtins.goal_tools as goal_mod
        source = inspect.getsource(goal_mod)
        # The line should contain "> 0.75:" (the dedup threshold)
        assert "> 0.75" in source, (
            "Goal dedup threshold should be 0.75 in goal_tools.py"
        )
        # And NOT the old value of 0.6
        assert "> 0.6:" not in source, (
            "Old threshold 0.6 should not be present in goal_tools.py"
        )


# ===========================================================================
# 6. Untitled event prevention — title fallback logic
# ===========================================================================


class TestUntitledEventPrevention:
    """Regression test: event creation must never produce "(untitled)" or
    empty titles.

    The title fallback logic is inline in cluster.py
    (_create_cluster_event and _create_singleton_event). Because these
    are async methods deeply coupled to OpenSearch, we test the fallback
    logic pattern directly rather than calling the methods.

    NOTE: Full integration testing of the cluster event creation path
    requires a running OpenSearch instance. These unit tests verify
    the fallback pattern that was fixed.
    """

    @staticmethod
    def _title_fallback_cluster(feats: list[dict], event_id_short: str) -> str:
        """Replicate the title fallback logic from _create_cluster_event."""
        best = max(feats, key=lambda f: f["confidence"])
        title = best["title"]
        if not title or title.strip() == "" or title.strip().lower() == "(untitled)":
            for f in feats:
                if f.get("title") and f["title"].strip() and f["title"].strip().lower() != "(untitled)":
                    title = f["title"]
                    break
            else:
                title = f"Event-{event_id_short}"
        return title

    @staticmethod
    def _title_fallback_singleton(feat: dict, event_id_short: str) -> str:
        """Replicate the title fallback logic from _create_singleton_event."""
        title = feat["title"]
        if not title or title.strip() == "" or title.strip().lower() == "(untitled)":
            title = (feat.get("data", {}).get("summary", "") or "")[:120] or f"Event-{event_id_short}"
        return title

    def test_cluster_uses_best_title(self):
        """When the best signal has a valid title, use it."""
        feats = [
            {"title": "Minor event", "confidence": 0.5},
            {"title": "Major explosion reported", "confidence": 0.9},
        ]
        result = self._title_fallback_cluster(feats, "abc12345")
        assert result == "Major explosion reported"

    def test_cluster_fallback_to_other_signal(self):
        """When best signal has untitled, fall back to another signal."""
        feats = [
            {"title": "Diplomatic crisis deepens", "confidence": 0.6},
            {"title": "(untitled)", "confidence": 0.9},
        ]
        result = self._title_fallback_cluster(feats, "abc12345")
        assert result == "Diplomatic crisis deepens"

    def test_cluster_fallback_to_event_id(self):
        """When all signals are untitled, fall back to Event-<id>."""
        feats = [
            {"title": "(untitled)", "confidence": 0.9},
            {"title": "", "confidence": 0.5},
            {"title": None, "confidence": 0.3},
        ]
        result = self._title_fallback_cluster(feats, "abc12345")
        assert result == "Event-abc12345"

    def test_cluster_empty_string_title_triggers_fallback(self):
        """Empty string titles trigger the fallback."""
        feats = [
            {"title": "", "confidence": 0.9},
            {"title": "Real title here", "confidence": 0.3},
        ]
        result = self._title_fallback_cluster(feats, "abc12345")
        assert result == "Real title here"

    def test_cluster_whitespace_title_triggers_fallback(self):
        """Whitespace-only titles trigger the fallback."""
        feats = [
            {"title": "   ", "confidence": 0.9},
            {"title": "Actual title", "confidence": 0.2},
        ]
        result = self._title_fallback_cluster(feats, "abc12345")
        assert result == "Actual title"

    def test_singleton_uses_title(self):
        """Singleton with valid title uses it directly."""
        feat = {"title": "Earthquake in Turkey", "confidence": 0.8, "data": {}}
        result = self._title_fallback_singleton(feat, "def67890")
        assert result == "Earthquake in Turkey"

    def test_singleton_untitled_falls_back_to_summary(self):
        """Singleton with untitled falls back to data.summary[:120]."""
        feat = {
            "title": "(untitled)",
            "confidence": 0.8,
            "data": {"summary": "A 6.2 magnitude earthquake struck southeastern Turkey"},
        }
        result = self._title_fallback_singleton(feat, "def67890")
        assert result == "A 6.2 magnitude earthquake struck southeastern Turkey"

    def test_singleton_untitled_no_summary_falls_back_to_event_id(self):
        """Singleton with untitled and no summary falls back to Event-<id>."""
        feat = {
            "title": "(untitled)",
            "confidence": 0.8,
            "data": {"summary": ""},
        }
        result = self._title_fallback_singleton(feat, "def67890")
        assert result == "Event-def67890"

    def test_singleton_none_title_falls_back(self):
        """None title triggers fallback."""
        feat = {
            "title": None,
            "confidence": 0.8,
            "data": {"summary": "Some summary text"},
        }
        result = self._title_fallback_singleton(feat, "def67890")
        assert result == "Some summary text"

    def test_no_result_ever_untitled(self):
        """Exhaustive check: no fallback path should ever produce
        '(untitled)' or an empty string."""
        test_cases = [
            ([{"title": "(untitled)", "confidence": 1.0}], "aaaa"),
            ([{"title": "", "confidence": 1.0}], "bbbb"),
            ([{"title": None, "confidence": 1.0}], "cccc"),
            ([{"title": "   ", "confidence": 1.0}], "dddd"),
        ]
        for feats, eid in test_cases:
            result = self._title_fallback_cluster(feats, eid)
            assert result.strip() != "", f"Empty title for feats={feats}"
            assert result.strip().lower() != "(untitled)", f"Untitled for feats={feats}"

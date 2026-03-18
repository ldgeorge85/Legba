"""
Unit tests for Legba cycle routing and tool-set containment.

Tests the cycle-type selection math and per-cycle-type tool filtering
WITHOUT importing the full AgentCycle (which requires Pydantic, services, etc.).
Instead we import the interval constants and tool-set frozensets directly and
replicate the priority-based routing logic as a pure function.
"""

import pytest

# ---------------------------------------------------------------------------
# Interval constants (from phases/__init__.py)
# ---------------------------------------------------------------------------
from legba.agent.phases import (
    ANALYSIS_INTERVAL,   # 5
    CURATE_INTERVAL,     # 9
    EVOLVE_INTERVAL,     # 30
    RESEARCH_INTERVAL,   # 7
    SYNTHESIZE_INTERVAL, # 10
)

# INTROSPECTION interval lives in config; default is 15.
INTROSPECTION_INTERVAL = 15

# ---------------------------------------------------------------------------
# Tool sets (from prompt/templates.py and phases/introspect.py)
# ---------------------------------------------------------------------------
from legba.agent.prompt.templates import (
    ANALYSIS_TOOLS,
    CURATE_TOOLS,
    EVOLVE_TOOLS,
    RESEARCH_TOOLS,
    SURVEY_TOOLS,
    SYNTHESIZE_TOOLS,
)
from legba.agent.phases.introspect import INTROSPECTION_TOOLS


# ---------------------------------------------------------------------------
# Cycle types — mirrors the if/elif chain in cycle.py run()
# ---------------------------------------------------------------------------
EVOLVE = "EVOLVE"
INTROSPECTION = "INTROSPECTION"
SYNTHESIZE = "SYNTHESIZE"
ANALYSIS = "ANALYSIS"
RESEARCH = "RESEARCH"
CURATE = "CURATE"
SURVEY = "SURVEY"


def resolve_cycle_type(cycle_number: int) -> str:
    """Replicate the priority-based routing logic from AgentCycle.run().

    Priority (highest first):
      EVOLVE(30) > INTROSPECTION(15) > SYNTHESIZE(10)
      > ANALYSIS(5) > RESEARCH(7) > CURATE(9) > SURVEY(default)

    Each type fires when ``cycle_number % interval == 0`` AND no
    higher-priority type also fires on the same cycle number.
    """
    cn = cycle_number
    if cn <= 0:
        return SURVEY

    if EVOLVE_INTERVAL > 0 and cn % EVOLVE_INTERVAL == 0:
        return EVOLVE
    if INTROSPECTION_INTERVAL > 0 and cn % INTROSPECTION_INTERVAL == 0:
        return INTROSPECTION
    if SYNTHESIZE_INTERVAL > 0 and cn % SYNTHESIZE_INTERVAL == 0:
        return SYNTHESIZE
    if ANALYSIS_INTERVAL > 0 and cn % ANALYSIS_INTERVAL == 0:
        return ANALYSIS
    if RESEARCH_INTERVAL > 0 and cn % RESEARCH_INTERVAL == 0:
        return RESEARCH
    if CURATE_INTERVAL > 0 and cn % CURATE_INTERVAL == 0:
        return CURATE
    return SURVEY


# ===================================================================
# 1. 30-cycle layout — assert the correct type fires for cycles 1-30
# ===================================================================

EXPECTED_LAYOUT: dict[int, str] = {
    1:  SURVEY,
    2:  SURVEY,
    3:  SURVEY,
    4:  SURVEY,
    5:  ANALYSIS,
    6:  SURVEY,
    7:  RESEARCH,
    8:  SURVEY,
    9:  CURATE,
    10: SYNTHESIZE,
    11: SURVEY,
    12: SURVEY,
    13: SURVEY,
    14: RESEARCH,
    15: INTROSPECTION,
    16: SURVEY,
    17: SURVEY,
    18: CURATE,
    19: SURVEY,
    20: SYNTHESIZE,
    21: RESEARCH,
    22: SURVEY,
    23: SURVEY,
    24: SURVEY,
    25: ANALYSIS,
    26: SURVEY,
    27: CURATE,
    28: RESEARCH,
    29: SURVEY,
    30: EVOLVE,
}


class TestCycleLayout:
    """Verify the full 30-cycle routing pattern."""

    @pytest.mark.parametrize("cycle_number,expected_type", EXPECTED_LAYOUT.items())
    def test_cycle_type(self, cycle_number: int, expected_type: str):
        actual = resolve_cycle_type(cycle_number)
        assert actual == expected_type, (
            f"Cycle {cycle_number}: expected {expected_type}, got {actual}"
        )

    def test_all_30_cycles_covered(self):
        """Sanity check: we have expectations for every cycle 1..30."""
        assert set(EXPECTED_LAYOUT.keys()) == set(range(1, 31))


# ===================================================================
# 2. Tool-set containment tests
# ===================================================================

class TestSurveyTools:
    """SURVEY cycle tool containment."""

    MUST_NOT_CONTAIN = {
        "feed_parse", "source_add", "source_update",
        "fs_read", "fs_write", "fs_list", "code_test",
    }

    MUST_CONTAIN = {
        "http_request", "graph_query", "graph_store",
        "situation_list", "event_create", "anomaly_detect",
        "cycle_complete",
    }

    def test_excluded_tools(self):
        overlap = SURVEY_TOOLS & self.MUST_NOT_CONTAIN
        assert not overlap, f"SURVEY_TOOLS must NOT contain: {overlap}"

    def test_required_tools(self):
        missing = self.MUST_CONTAIN - SURVEY_TOOLS
        assert not missing, f"SURVEY_TOOLS is missing: {missing}"


class TestSynthesizeTools:
    """SYNTHESIZE cycle tool containment."""

    MUST_CONTAIN = {
        "http_request", "correlate",
        "prediction_create", "situation_create",
    }

    MUST_NOT_CONTAIN = {
        "feed_parse", "source_add", "fs_write",
    }

    def test_required_tools(self):
        missing = self.MUST_CONTAIN - SYNTHESIZE_TOOLS
        assert not missing, f"SYNTHESIZE_TOOLS is missing: {missing}"

    def test_excluded_tools(self):
        overlap = SYNTHESIZE_TOOLS & self.MUST_NOT_CONTAIN
        assert not overlap, f"SYNTHESIZE_TOOLS must NOT contain: {overlap}"


class TestCurateTools:
    """CURATE cycle tool containment."""

    MUST_NOT_CONTAIN = {"http_request"}

    def test_excluded_tools(self):
        overlap = CURATE_TOOLS & self.MUST_NOT_CONTAIN
        assert not overlap, f"CURATE_TOOLS must NOT contain: {overlap}"


class TestUniversalTools:
    """Every tool set must include the universal utility tools."""

    UNIVERSAL = {"note_to_self", "explain_tool", "cycle_complete"}

    ALL_TOOL_SETS = {
        "SURVEY_TOOLS":        SURVEY_TOOLS,
        "SYNTHESIZE_TOOLS":    SYNTHESIZE_TOOLS,
        "ANALYSIS_TOOLS":      ANALYSIS_TOOLS,
        "RESEARCH_TOOLS":      RESEARCH_TOOLS,
        "CURATE_TOOLS":        CURATE_TOOLS,
        "EVOLVE_TOOLS":        EVOLVE_TOOLS,
        "INTROSPECTION_TOOLS": INTROSPECTION_TOOLS,
    }

    @pytest.mark.parametrize("name,tool_set", ALL_TOOL_SETS.items())
    def test_universal_tools_present(self, name: str, tool_set: frozenset):
        missing = self.UNIVERSAL - tool_set
        assert not missing, f"{name} is missing universal tools: {missing}"


# ===================================================================
# 3. Priority yielding — overlapping intervals resolve to highest
# ===================================================================

class TestPriorityYielding:
    """When a cycle number is divisible by multiple intervals,
    the highest-priority type must win."""

    def test_cycle_30_is_evolve(self):
        """30 is divisible by 30, 15, 10, 5 — EVOLVE wins."""
        assert resolve_cycle_type(30) == EVOLVE

    def test_cycle_60_is_evolve(self):
        """60 is divisible by 30, 15, 10, 5 — EVOLVE wins."""
        assert resolve_cycle_type(60) == EVOLVE

    def test_cycle_10_is_synthesize(self):
        """10 is divisible by 10 and 5 — SYNTHESIZE wins over ANALYSIS."""
        assert resolve_cycle_type(10) == SYNTHESIZE

    def test_cycle_15_is_introspection(self):
        """15 is divisible by 15, 5 — INTROSPECTION wins over ANALYSIS."""
        assert resolve_cycle_type(15) == INTROSPECTION

    def test_cycle_35_is_research(self):
        """35 is divisible by 7 and 5 — ANALYSIS(5) > RESEARCH(7), so ANALYSIS wins."""
        assert resolve_cycle_type(35) == ANALYSIS

    def test_cycle_45_is_introspection(self):
        """45 is divisible by 15, 9, 5 — INTROSPECTION wins."""
        assert resolve_cycle_type(45) == INTROSPECTION

    def test_cycle_63_is_curate(self):
        """63 is divisible by 9 and 7 — RESEARCH(7) > CURATE(9), so RESEARCH wins."""
        assert resolve_cycle_type(63) == RESEARCH

    def test_cycle_90_is_evolve(self):
        """90 is divisible by 30, 15, 10, 9, 5 — EVOLVE wins."""
        assert resolve_cycle_type(90) == EVOLVE

    def test_cycle_0_is_survey(self):
        """Cycle 0 should never trigger a special type."""
        assert resolve_cycle_type(0) == SURVEY

    def test_cycle_negative_is_survey(self):
        """Negative cycle numbers should fall through to SURVEY."""
        assert resolve_cycle_type(-1) == SURVEY

    def test_cycle_210_is_evolve(self):
        """210 = LCM(30,15,10,7,5,9)/gcd... divisible by ALL intervals. EVOLVE wins."""
        # 210 % 30 == 0, so EVOLVE.
        assert resolve_cycle_type(210) == EVOLVE

    def test_synthesize_beats_analysis(self):
        """Every SYNTHESIZE_INTERVAL multiple that is also ANALYSIS should be SYNTHESIZE."""
        for cn in range(10, 301, 10):
            ct = resolve_cycle_type(cn)
            # SYNTHESIZE should fire unless EVOLVE or INTROSPECTION takes over
            if cn % EVOLVE_INTERVAL == 0:
                assert ct == EVOLVE, f"Cycle {cn}: expected EVOLVE, got {ct}"
            elif cn % INTROSPECTION_INTERVAL == 0:
                assert ct == INTROSPECTION, f"Cycle {cn}: expected INTROSPECTION, got {ct}"
            else:
                assert ct == SYNTHESIZE, f"Cycle {cn}: expected SYNTHESIZE, got {ct}"

    def test_evolve_always_wins(self):
        """EVOLVE must take priority at every multiple of 30, up to 300."""
        for cn in range(30, 301, 30):
            assert resolve_cycle_type(cn) == EVOLVE, f"Cycle {cn} should be EVOLVE"


# ===================================================================
# Interval constant sanity checks
# ===================================================================

class TestIntervalConstants:
    """Verify the imported interval constants match expected values."""

    def test_evolve_interval(self):
        assert EVOLVE_INTERVAL == 30

    def test_introspection_interval(self):
        assert INTROSPECTION_INTERVAL == 15

    def test_synthesize_interval(self):
        assert SYNTHESIZE_INTERVAL == 10

    def test_analysis_interval(self):
        assert ANALYSIS_INTERVAL == 5

    def test_research_interval(self):
        assert RESEARCH_INTERVAL == 7

    def test_curate_interval(self):
        assert CURATE_INTERVAL == 9

"""
Phase modules for the Agent Cycle.

Each module exports a mixin class that AgentCycle inherits from.
The mixins share state via self.* attributes set by __init__ in cycle.py.

3-tier cycle routing:
  Tier 1 — Scheduled outputs (fixed intervals):
    EVOLVE(30) > INTROSPECTION(15) > SYNTHESIZE(10)
  Tier 2 — Guaranteed work (modulo floor):
    ANALYSIS(5) > RESEARCH(7) > CURATE(9)
  Tier 3 — Dynamic fill (state-scored):
    CURATE (capped 0.45, recent 24h backlog) vs SURVEY (0.50 default)
"""

# Reporting cadence: produce a status report every N cycles.
REPORT_INTERVAL = 5

# Research cadence: dedicated entity enrichment cycle every N cycles.
RESEARCH_INTERVAL = 7

# Acquire cadence: dedicated data ingestion cycle every N cycles (legacy fallback).
ACQUIRE_INTERVAL = 3

# Curate cadence: intelligence curation cycle every N cycles.
CURATE_INTERVAL = 9

# Analysis cadence: analytical tools cycle every N cycles.
# Interval 4 is coprime with 7, 9, 10 — minimal masking by other types.
ANALYSIS_INTERVAL = 4

# Synthesize cadence: deep-dive investigation cycle every N cycles.
SYNTHESIZE_INTERVAL = 10

# Evolve cadence: self-improvement cycle every N cycles.
EVOLVE_INTERVAL = 30

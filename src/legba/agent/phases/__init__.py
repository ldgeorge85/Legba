"""
Phase modules for the Agent Cycle.

Each module exports a mixin class that AgentCycle inherits from.
The mixins share state via self.* attributes set by __init__ in cycle.py.

Cycle type routing (priority order, highest first):
  EVOLVE(30) > INTROSPECTION(15) > SYNTHESIZE(10) > ANALYSIS(5)
  > RESEARCH(7) > CURATE(9) > SURVEY(default)
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
ANALYSIS_INTERVAL = 5

# Synthesize cadence: deep-dive investigation cycle every N cycles.
SYNTHESIZE_INTERVAL = 10

# Evolve cadence: self-improvement cycle every N cycles.
EVOLVE_INTERVAL = 30

# Dynamic CURATE promotion: if uncurated signal backlog exceeds this, promote next SURVEY to CURATE.
CURATE_BACKLOG_THRESHOLD = 100

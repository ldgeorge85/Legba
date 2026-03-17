"""
Phase modules for the Agent Cycle.

Each module exports a mixin class that AgentCycle inherits from.
The mixins share state via self.* attributes set by __init__ in cycle.py.
"""

# Reporting cadence: produce a status report every N cycles.
REPORT_INTERVAL = 5

# Research cadence: dedicated entity enrichment cycle every N cycles.
RESEARCH_INTERVAL = 5

# Acquire cadence: dedicated data ingestion cycle every N cycles.
ACQUIRE_INTERVAL = 3

# Curate cadence: intelligence curation cycle every N cycles (replaces ACQUIRE when ingestion active).
CURATE_INTERVAL = 3

# Analysis cadence: analytical tools cycle every N cycles.
ANALYSIS_INTERVAL = 10

# Evolve cadence: self-improvement cycle every N cycles.
EVOLVE_INTERVAL = 30

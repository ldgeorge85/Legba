"""
Legba Agent — Main Entry Point

Runs a single cycle and exits. The supervisor manages the lifecycle.

Usage:
    python -m legba.agent.main
"""

from __future__ import annotations

import asyncio
import sys

from ..shared.config import LegbaConfig
from .cycle import AgentCycle


async def run_cycle() -> int:
    """Run a single agent cycle. Returns exit code."""
    config = LegbaConfig.from_env()

    cycle = AgentCycle(config)
    response = await cycle.run()

    if response.status == "error":
        print(f"[agent] Cycle {response.cycle_number} FAILED: {response.error}", file=sys.stderr)
        return 1

    print(f"[agent] Cycle {response.cycle_number} completed: {response.cycle_summary[:100]}")
    return 0


def main() -> None:
    """Entry point for `python -m legba.agent.main`."""
    try:
        exit_code = asyncio.run(run_cycle())
        sys.exit(exit_code)
    except KeyboardInterrupt:
        print("\n[agent] Interrupted", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"[agent] Fatal error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

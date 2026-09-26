# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 option specs — Program 5 lane 1's ``inquiry_yield`` sub-handler.

Split from ``handler_options.py`` only for the module-size gate (it sits
exactly at its 1,660-line ceiling — see ``tests/test_module_size_gate.py``);
the catalog still owns this entry and imports this leaf, the same
``handler_options_events.py`` / ``handler_options_programs.py`` split idiom.
"""

from __future__ import annotations

#: inquiry_yield reads NO operator knob — the window is fixed at
#: WINDOW_DAYS=7 (a weekly instrument by construction, per
#: PROGRAM5_INQUIRY_DESIGN_2026-09-24.md §4) and the two read caps
#: (``_MAX_LEDGER_ROWS`` / ``_MAX_FINDING_ROWS``) are safety bounds, not
#: operator-facing tuning. The X-1 catalog requires the explicit empty
#: declaration (the receipt_anchor precedent) so an undeclared knob can never
#: be read silently.
INQUIRY_YIELD_OPTIONS: tuple = ()

#: Spread into ``HANDLER_OPTIONS`` by ``handler_options.py`` as one line, the
#: same ``EVENTS_CATALOG`` / ``*_OPTIONS`` idiom. Keys are the
#: ``deterministic.SUB_HANDLERS`` names.
INQUIRY_CATALOG: dict[str, tuple] = {
    "inquiry_yield": INQUIRY_YIELD_OPTIONS,
}

__all__ = ["INQUIRY_YIELD_OPTIONS", "INQUIRY_CATALOG"]

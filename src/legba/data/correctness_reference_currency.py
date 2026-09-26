# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""REFERENCE CURRENCY — how old a reference may be and still be graded against.

One question, one definition, in a module with NOTHING but the standard
library behind it:

    reference_age_days(as_of, window_end)   how stale is it, in days
    reference_grace_days(options)           how stale may it be

── WHY THIS IS ITS OWN FILE, AND WHY IT IS A LEAF ──────────────────────────

These two lived in `analysts.deterministic_handlers.correctness_grader`, which
is the right home for the *policy* and the wrong home for the *import*. Two
callers need them, and they sit on opposite sides of a deployment boundary:

  * `correctness_grader` — the runtime handler that grades and writes
    `unit_correctness.reference_age_days` (migration 0197);
  * `registry.unit_correctness_api` — the READ route that puts the number on
    the surface and must describe the same row the same way.

The registry image is deliberately SLIM: it carries no runtime analyst
dependencies. Importing the grader from a route module reaches, transitively,
`feedparser` and the rest of the ingest stack, and the deployed registry
answered `GET /units/{target_id}/correctness` with a 500 —
`ModuleNotFoundError: No module named 'feedparser'`. A DEFERRED import did not
help and could not: deferring moves *when* the import graph is walked, never
*how far* it reaches. The only fix that holds is a module whose graph stops
here.

So: no `httpx`, no `asyncpg`, no `feedparser`, no sibling `legba` import, not
even a convenience one. `tests/data_pkg/test_unit_correctness_api.py::
test_the_read_route_imports_without_the_runtime_dependency_stack` poisons
`sys.modules['feedparser']` and imports the route in a subprocess, so the next
convenient import is caught here rather than in production.

── AND WHY NOT SIMPLY COPY THE TWO FUNCTIONS ───────────────────────────────

Because "stale" is a measurement-integrity word and two copies of it drift on
the first edit. A badge telling a reader a number rests on an expired
reference while the grader is still grading against it is the surface
explaining a decision nobody made. `correctness_grader` imports and re-exports
these names, so `correctness_grader.reference_grace_days` still resolves for
its own tests and for `reference_builder.grader_grace_days()`; the grader
remains the place the behaviour is DOCUMENTED, and this is the place it is
defined.
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

#: The operator's knob: how many days past ``window_end`` a reference is still
#: current. Env WINS over the descriptor option (see below).
REFERENCE_GRACE_ENV: str = "LEGBA_GRADER_REFERENCE_GRACE_DAYS"
DEFAULT_REFERENCE_GRACE_DAYS: int = 7

_SECONDS_PER_DAY: float = 86_400.0


def non_neg_int(raw: Any, default: Any) -> Any:
    """A NON-negative-int knob (zero is a real setting), or ``default``."""
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value >= 0 else default


def reference_grace_days(options: Mapping[str, Any] | None = None) -> int:
    """How many days past ``window_end`` a reference is still current.

    ``LEGBA_GRADER_REFERENCE_GRACE_DAYS`` WINS over the descriptor option, which
    wins over the 7-day default. The env is deliberately on top: how long a
    number may keep resting on an ageing reference is a measurement-integrity
    decision, and a descriptor PUT — an API call any holder of the registry
    token can make — must not be able to widen it silently.

    An unparseable or negative env falls back rather than being read as
    "forever": a typo must fail toward measuring against a reference somebody
    can still defend.
    """
    raw = (os.getenv(REFERENCE_GRACE_ENV) or "").strip()
    if raw:
        parsed = non_neg_int(raw, None)
        if parsed is not None:
            return int(parsed)
        logger.warning(
            "correctness_grader.reference_grace_unparseable raw=%r — falling "
            "back to the descriptor option or the %d-day default",
            raw, DEFAULT_REFERENCE_GRACE_DAYS,
        )
    # The literal read, spelled out: the X-1 catalog's reachability sweep
    # (``tests/data_pkg/test_handler_options_x1.py``) proves a declared knob is
    # real by grepping for ``options.get("<name>")`` in the handler package,
    # and a knob it cannot see is a knob an operator cannot trust.
    # ``correctness_grader`` re-exports this function, so the literal below is
    # the one the catalog resolves through.
    options = options or {}
    return int(non_neg_int(
        options.get("reference_grace_days"), DEFAULT_REFERENCE_GRACE_DAYS,
    ))


def reference_age_days(as_of: datetime, window_end: Any) -> float | None:
    """``as_of - window_end`` in days, floored at zero. ``None`` if unknown.

    Fractional by design — a sweep 4 h 20 m past a window's close is 0.18 days
    stale, and rounding that to a whole day would make every same-day read look
    either fresh or a day old. Floored at zero because a read INSIDE the window
    is not negatively aged; it is aged zero, and the column is the age of the
    reference the number rests on, not a signed offset.
    """
    if not isinstance(window_end, datetime):
        return None
    end = (
        window_end if window_end.tzinfo
        else window_end.replace(tzinfo=timezone.utc)
    )
    return round(
        max(0.0, (as_of - end).total_seconds()) / _SECONDS_PER_DAY, 6
    )


def reference_is_stale(age_days: float | None, grace_days: int | None = None) -> bool:
    """Is a reference of this age past the grace in force?

    The one predicate both sides ask, so neither has to remember whether the
    comparison is ``>`` or ``>=``. An unknown age is NOT stale — "we cannot
    tell" and "it has expired" are different answers, and only one of them
    should stop a number from being shown.
    """
    if age_days is None:
        return False
    grace = reference_grace_days() if grace_days is None else grace_days
    return float(age_days) > float(grace)


__all__ = [
    "DEFAULT_REFERENCE_GRACE_DAYS",
    "REFERENCE_GRACE_ENV",
    "non_neg_int",
    "reference_age_days",
    "reference_grace_days",
    "reference_is_stale",
]

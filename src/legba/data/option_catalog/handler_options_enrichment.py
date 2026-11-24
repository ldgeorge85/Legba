# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 option specs — the ``reenrich_pending`` sweep (W11-F118), the
``verify_backfill`` sweep (W11-F84), and the ``evidence_refetch`` backlog
drain (W11-F138).

Split out only for the module-size gate: ``handler_options_catalog.py`` sits
just under the gate's 1,500-line new-entrant threshold, so a new entry lands
here and the catalog splices it in on one line, the same
``handler_options_inquiry.py`` / ``handler_options_events.py`` idiom.
"""

from __future__ import annotations

from .handler_options_base import OptionSpec, _pos_int, _window_days

#: reenrich_pending — the per-run bound on rows pulled from the
#: ``payload.enrichment_pending`` pool (the SELECT LIMIT; default 200). Each
#: row is one run of its source's enrichment chain (hosted NLP, $0); capped at
#: 5000 so a fat-fingered value cannot turn a tick into a full-backlog walk.
REENRICH_PENDING_OPTIONS: tuple = (
    _pos_int(
        "pending_batch",
        "Rows marked enrichment_pending re-enriched per run, oldest first (default 200).",
        maximum=5000,
    ),
)

#: verify_backfill — the per-run bound on critique-less cited findings verified
#: (the SELECT LIMIT; default 50; each row is one judge pass, so capped at the
#: script's own --apply ceiling, 500), and the minimum finding age in minutes
#: (default 30 — a younger finding's verify leg may still be running; floored at
#: 10 so a fat-fingered value cannot race the live critic, capped at the
#: sweep's 7-day lookback).
VERIFY_BACKFILL_OPTIONS: tuple = (
    _pos_int(
        "backfill_batch",
        "Critique-less cited findings verified per run, oldest first (default 50).",
        maximum=500,
    ),
    OptionSpec(
        "min_age_minutes", "int",
        "Only findings older than this many minutes are backfilled (default 30).",
        minimum=10, maximum=10080,
    ),
)

#: evidence_refetch — the W11-F138 textless-backlog drain for hosts whose
#: fetch policy changed (sources/_fetch_policy's impersonate=on set,
#: timesofisrael.com + jpost.com at ship). ``refetch_max_per_tick`` is the
#: per-run re-fetch budget (each row is a real egress fetch through the
#: archiver's own leg, so capped); ``refetch_window_days`` bounds how far
#: back the sweep reaches (the defect is measured over the last 30 days, so
#: the shipped default is 30 — deeper windows re-litigate older history and
#: belong behind an operator's own value).
EVIDENCE_REFETCH_OPTIONS: tuple = (
    _pos_int(
        "refetch_max_per_tick",
        "Textless policy-host signals re-fetched per run, oldest first "
        "(default 50; each is one egress fetch, capped at 500).",
        maximum=500,
    ),
    _window_days(
        "refetch_window_days",
        "Only signals fetched within this many days are re-fetched "
        "(default 30 — the window the challenge-block defect was measured "
        "over).",
    ),
)

#: Spread into ``HANDLER_OPTIONS`` by ``handler_options_catalog.py`` as one
#: line. Keys are the ``deterministic.SUB_HANDLERS`` names.
ENRICHMENT_CATALOG: dict[str, tuple] = {
    "reenrich_pending": REENRICH_PENDING_OPTIONS,
    "verify_backfill": VERIFY_BACKFILL_OPTIONS,
    "evidence_refetch": EVIDENCE_REFETCH_OPTIONS,
}

__all__ = [
    "ENRICHMENT_CATALOG",
    "EVIDENCE_REFETCH_OPTIONS",
    "REENRICH_PENDING_OPTIONS",
    "VERIFY_BACKFILL_OPTIONS",
]

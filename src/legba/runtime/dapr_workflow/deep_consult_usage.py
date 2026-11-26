# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The deep consult's per-call receipts and usage totals (roll-86 follow-up).

A chat turn's row carries ``usage``: the run's totals (calls, tokens in and
out, elapsed, cost or why it is not priced) and one receipt per model call
(``consult_turn_stamps.call_receipts``), read off the R11 run account
(``data/run_accounting``) that the chat path binds. The deep workflow bound no
account: its stages run as Dapr activities on the worker, each its own
``asyncio.run``, and every provider call recorded into nothing, so a deep turn had ``call_receipts`` null and no
route or token counts.

Here each LLM stage (plan, analyze) runs under its OWN account
(:func:`accounted_stage`) and hands its calls on in its output, already cut to
receipts — no prompt or completion text ever enters the workflow history. The
analyze stage folds them into ONE receipt (:func:`deep_usage`) in the shape
``SpendGuard.receipt`` gives a chat turn; synthesize stores it on the finding
(``data.deep_consult.usage``) and the status poll writes it on the turn
through the same ``usage_receipt`` a chat turn uses.

Totals are the receipts' own sums, so the turn's numbers and its receipts can
never disagree. A key the workflow history does not carry (a run in flight
across the deploy) reads as no stage usage, never as zero calls.
"""

from __future__ import annotations

import time
from typing import Any, Awaitable, Callable, Mapping

from ...data.analysts.consult_spend_guard import price_table_for
from ...data.registry.consult_turn_stamps import call_receipts
from ...data.run_accounting import (
    bind_run_accounting,
    current_llm_calls,
    reset_run_accounting,
)

__all__ = ["STAGE_USAGE_KEY", "accounted_stage", "deep_usage"]

#: The key a stage's output carries its calls under.
STAGE_USAGE_KEY = "stage_usage"


async def accounted_stage(
    stage: str, body: Callable[[], Awaitable[dict[str, Any]]],
) -> dict[str, Any]:
    """Run one stage body under a fresh run account; return its output with
    ``stage_usage[stage]`` = ``{call_receipts, est_cost_usd, seconds}``.

    The account is the stage's own (bound here, reset in ``finally``), so the
    consult loop the analyze stage drives also sees a bound account and its own
    ``call_receipts`` fill as a chat turn's do.
    """
    token = bind_run_accounting()
    started = time.monotonic()
    try:
        out = await body()
        raw = current_llm_calls()
    finally:
        reset_run_accounting(token)
    cost = 0.0
    for entry in raw:
        if isinstance(entry, Mapping):
            try:
                cost += float(entry.get("cost_estimate_usd") or 0.0)
            except (TypeError, ValueError):
                pass
    usage = dict(out.get(STAGE_USAGE_KEY) or {}) if isinstance(out, dict) else {}
    usage[stage] = {
        "call_receipts": call_receipts(raw) or [],
        "est_cost_usd": round(cost, 6),
        "seconds": round(time.monotonic() - started, 3),
    }
    if isinstance(out, dict):
        out[STAGE_USAGE_KEY] = usage
    return out


def deep_usage(
    stage_usage: Mapping[str, Any] | None, llm: Any, *, acquire_seconds: Any = None,
) -> dict[str, Any] | None:
    """One receipt for the deep run, in ``SpendGuard.receipt``'s shape.

    ``None`` when no stage carried usage (a run started before this landed):
    the turn then keeps its ``deep_consult_usage_not_carried`` label.
    """
    stages = [s for s in (stage_usage or {}).values() if isinstance(s, Mapping)]
    if not stages:
        return None
    receipts: list[dict[str, Any]] = []
    cost = 0.0
    elapsed = 0.0
    for s in stages:
        receipts.extend(r for r in (s.get("call_receipts") or []) if isinstance(r, Mapping))
        cost += float(s.get("est_cost_usd") or 0.0)
        elapsed += float(s.get("seconds") or 0.0)
    if isinstance(acquire_seconds, (int, float)):
        elapsed += float(acquire_seconds)
    cfg = getattr(llm, "_cfg", None)
    name = getattr(getattr(cfg, "model_name", None), "raw", None)
    sub = getattr(llm, "subprovider", None)
    return {
        "calls": len(receipts),
        "input_tokens": sum(int(r.get("prompt_tokens") or 0) for r in receipts),
        "output_tokens": sum(int(r.get("completion_tokens") or 0) for r in receipts),
        "est_cost_usd": round(cost, 4),
        "model": name.strip() if isinstance(name, str) and name.strip() else (str(sub) if sub else None),
        "subprovider": str(sub) if sub else None,
        "elapsed_s": round(elapsed, 1),
        "priced": bool(price_table_for(llm)),
        "call_receipts": receipts,
    }

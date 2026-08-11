# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-245 budget_ledger write helpers + provider-agnostic cost lookup.

Per `docs/REVIEW_2026_05_20.md` §4.7 — wire `cost_estimate_usd` through
from the per-provider `PRICE_TABLE`s into the substrate `budget_ledger`
row at write time, so operators can see $ next to tokens from the start
of Phase 6 analyst fan-out.

Public surface:

  * ``compute_cost_usd(provider, model, prompt_tokens, completion_tokens,
        *, reasoning_tokens=0, cache_read_tokens=0, cache_write_tokens=0)
        -> Decimal``
    Dispatches on provider name to the corresponding handler's
    ``PRICE_TABLE`` and returns the USD cost as a ``Decimal`` (six decimal
    places of precision — micro-dollar). Returns ``Decimal('0')`` when the
    provider is unknown or the model is missing from the table (which is
    legitimate for self-hosted vLLM whose ``PRICE_TABLE`` is empty by
    design).

  * ``record_budget(conn, *, analyst_id, analyst_version, provider, model,
        prompt_tokens, completion_tokens, ...) -> BudgetLedgerRow``
    Upserts the (analyst_id, analyst_version, bucket=today_utc) row,
    accumulating tokens + cost + run count. Idempotent under retries iff
    the caller dedupes their own retry stream (the row is keyed on
    (analyst_id, analyst_version, bucket) — a second call for the same
    bucket *adds* on top of the existing total; budget accounting is
    inherently additive).

The provider dispatch is intentionally a small import-time map rather
than a registry lookup — the LLM provider set is closed at three
(anthropic / openai / vllm) and we want a write-side helper that does
not depend on registry import-graph or runtime config. Adding a new
provider here is a one-liner alongside its handler module.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Mapping, Sequence

from typing import TYPE_CHECKING

import asyncpg

# ModelPrice is type-annotation-only here (file has `from __future__ import
# annotations`, so all annotations are lazy strings). Importing it at
# runtime would trigger ``legba.data.stack.llm.__init__.py`` to load, which
# eagerly pulls the provider handlers and through them httpx — and httpx's
# cookie machinery subclasses ``urllib.request.Request`` at module load,
# which Temporal's workflow sandbox rejects. The TYPE_CHECKING guard keeps
# the schema available for static analysis without dragging the LLM stack
# into the import chain of every consumer.
if TYPE_CHECKING:
    from ..stack.llm.pricing import ModelPrice  # noqa: F401


# ---------------------------------------------------------------------------
# Provider price-table dispatch
# ---------------------------------------------------------------------------

# Keyed by provider/subprovider name (matches ``LLMProviderHandler.subprovider``).
# Self-hosted vLLM legitimately has an empty PRICE_TABLE — that's fine; the
# dispatch still hits this map and ``estimate_cost``-style logic returns
# Decimal('0'). The $ figure is still computed, just zero.
#
# Lazy-loaded to keep the LLM provider handlers out of the eager
# import chain. The handler modules import ``httpx`` for their async
# clients; httpx's cookie handling subclasses ``urllib.request.Request``
# at module-load time, which Temporal's workflow sandbox blocks
# (RestrictedWorkflowAccessError on urllib.request.Request.__mro_entries__).
# Anything that runs inside the Temporal worker (including code that
# transitively imports ``legba.runtime`` → ``dapr_actors`` →
# ``data.provenance`` → ``budget`` → here) would otherwise fail
# workflow validation. The lazy boundary keeps the cost-computation
# helper importable from anywhere without pulling httpx through.
_PRICE_TABLES_CACHE: dict[str, Mapping[str, ModelPrice]] | None = None


def _get_price_tables() -> dict[str, Mapping[str, ModelPrice]]:
    global _PRICE_TABLES_CACHE
    if _PRICE_TABLES_CACHE is None:
        from ..stack.llm.anthropic import AnthropicProviderHandler
        from ..stack.llm.openai import OpenAIProviderHandler
        from ..stack.llm.vllm import VLLMProviderHandler
        _PRICE_TABLES_CACHE = {
            AnthropicProviderHandler.subprovider: AnthropicProviderHandler.PRICE_TABLE,
            OpenAIProviderHandler.subprovider:    OpenAIProviderHandler.PRICE_TABLE,
            VLLMProviderHandler.subprovider:      VLLMProviderHandler.PRICE_TABLE,
        }
    return _PRICE_TABLES_CACHE


def _lookup_price(model: str, table: Mapping[str, ModelPrice]) -> ModelPrice | None:
    """Mirror ``stack.llm.base.estimate_cost`` resolution: exact key, then
    prefix-match against the table (providers ship minor revisions on a
    base model name, e.g. ``claude-opus-4-7-20260301`` vs ``claude-opus-4-7``).
    """
    price = table.get(model)
    if price is not None:
        return price
    for key, entry in table.items():
        if model.startswith(key):
            return entry
    return None


def compute_cost_usd(
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    *,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> Decimal:
    """Compute LLM call cost in USD from token counts using the provider's
    PRICE_TABLE.

    Returns ``Decimal('0')`` when:
      * the provider name isn't in the dispatch map, or
      * the model isn't priced in that provider's table (e.g. self-hosted
        vLLM whose table is intentionally empty).

    All math goes through ``Decimal`` so micro-dollar precision survives
    repeated accumulation in the substrate column (NUMERIC(12,6)). The
    handler-side ``cost_estimate_usd`` field on ``LLMUsage`` is float; that
    one's a snapshot per call. This function is the write-side authority.
    """
    table = _get_price_tables().get(provider)
    if table is None:
        return Decimal("0")

    price = _lookup_price(model, table)
    if price is None:
        return Decimal("0")

    per_m = Decimal("1000000")
    cost = (
        (Decimal(int(prompt_tokens)) * Decimal(repr(price.input_per_m)) / per_m)
        + (Decimal(int(completion_tokens)) * Decimal(repr(price.output_per_m)) / per_m)
        + (Decimal(int(cache_read_tokens)) * Decimal(repr(price.cache_read_per_m)) / per_m)
        + (Decimal(int(cache_write_tokens)) * Decimal(repr(price.cache_write_per_m)) / per_m)
        + (Decimal(int(reasoning_tokens)) * Decimal(repr(price.reasoning_per_m)) / per_m)
    )
    # Quantize to 6 decimal places to match NUMERIC(12,6).
    return cost.quantize(Decimal("0.000001"))


# ---------------------------------------------------------------------------
# budget_ledger row writer
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BudgetLedgerRow:
    """The row state after a ``record_budget`` call.

    All counts and cost reflect the *accumulated* total for the day-bucket
    after this write — not the increment from the current call. Lets the
    caller flush a daily summary log without a second SELECT.
    """

    analyst_id: str
    analyst_version: str
    bucket: date
    tokens_used: int
    runs: int
    cost_usd: Decimal             # operator-stamped column (kept as-is)
    cost_estimate_usd: Decimal    # the L-245 derived column
    last_updated: datetime


def _today_utc() -> date:
    return datetime.now(tz=timezone.utc).date()


async def record_budget(
    conn: asyncpg.Connection,
    *,
    analyst_id: str,
    analyst_version: str,
    provider: str,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    reasoning_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
    bucket: date | None = None,
    runs_increment: int = 1,
) -> BudgetLedgerRow:
    """Upsert this analyst's daily budget bucket with tokens + cost.

    Behavior:
      * Resolves ``cost_estimate_usd`` via ``compute_cost_usd`` against the
        provider's ``PRICE_TABLE`` — unknown providers / unpriced models
        contribute 0 to the cost column (but tokens still accumulate).
      * Adds to the existing (analyst_id, analyst_version, bucket) row if
        present; inserts a new one otherwise.
      * ``runs_increment`` is added to ``runs`` (default 1 = one LLM call =
        one run for the purpose of this ledger). Pass 0 for accounting
        adjustments that shouldn't bump the run counter.
      * Returns the post-write totals so callers can log without a SELECT.

    Concurrency: the underlying ``INSERT … ON CONFLICT … DO UPDATE`` is
    atomic at the row level. Two concurrent calls against the same bucket
    serialize on the PK and produce a correct sum.
    """
    bucket = bucket or _today_utc()
    tokens_inc = int(prompt_tokens) + int(completion_tokens) + int(reasoning_tokens)
    cost_estimate_inc = compute_cost_usd(
        provider,
        model,
        prompt_tokens,
        completion_tokens,
        reasoning_tokens=reasoning_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )

    # asyncpg maps NUMERIC ↔ Decimal; pass the Decimal directly. NOW() on
    # the DB side keeps last_updated authoritative (clock skew immunity).
    row = await conn.fetchrow(
        """
        INSERT INTO budget_ledger (
            analyst_id, analyst_version, bucket,
            tokens_used, runs, cost_estimate_usd, last_updated
        )
        VALUES ($1, $2, $3, $4, $5, $6, NOW())
        ON CONFLICT (analyst_id, analyst_version, bucket) DO UPDATE
        SET tokens_used       = budget_ledger.tokens_used + EXCLUDED.tokens_used,
            runs              = budget_ledger.runs + EXCLUDED.runs,
            cost_estimate_usd = budget_ledger.cost_estimate_usd + EXCLUDED.cost_estimate_usd,
            last_updated      = NOW()
        RETURNING analyst_id, analyst_version, bucket,
                  tokens_used, runs, cost_usd, cost_estimate_usd, last_updated
        """,
        analyst_id,
        analyst_version,
        bucket,
        tokens_inc,
        int(runs_increment),
        cost_estimate_inc,
    )
    assert row is not None  # RETURNING from INSERT/UPDATE always produces a row

    return BudgetLedgerRow(
        analyst_id=row["analyst_id"],
        analyst_version=row["analyst_version"],
        bucket=row["bucket"],
        tokens_used=int(row["tokens_used"]),
        runs=int(row["runs"]),
        cost_usd=Decimal(row["cost_usd"]),
        cost_estimate_usd=Decimal(row["cost_estimate_usd"]),
        last_updated=row["last_updated"],
    )


# ---------------------------------------------------------------------------
# S-4 — the judge leg's own ledger dimension
# ---------------------------------------------------------------------------

#: Prefix stamped into ``budget_ledger.analyst_version`` for judge rows.
#:
#: ``budget_ledger`` is keyed ``(analyst_id, analyst_version, bucket)`` and has
#: no provider/model columns, so ``analyst_version`` is the only place a second
#: DIMENSION can live without a migration that repartitions a live table. A
#: judge row therefore lands as ``(desk_analyst_id, "judge:<component_id>",
#: today)``, which gets three properties at once:
#:
#:   * it does NOT collide with the generation row (whose analyst_version is a
#:     descriptor content-hash — a value that can never begin with this prefix);
#:   * it stays INVISIBLE to per-analyst enforcement, which reads
#:     ``WHERE analyst_id = $1 AND analyst_version = $2`` against the descriptor
#:     version — so metering the judge cannot start throttling desks;
#:   * it IS visible to the global governor, which sums ``tokens_used`` over the
#:     whole bucket — which is exactly the accounting P3 found missing.
#:
#: Per-desk attribution is preserved by analyst_id; per-call detail lives in the
#: run's ``analyst_traces.llm_calls`` receipt.
JUDGE_LEDGER_VERSION_PREFIX = "judge:"


def judge_ledger_version(component_id: str) -> str:
    """The ``analyst_version`` a judge component's ledger rows are keyed under."""
    return f"{JUDGE_LEDGER_VERSION_PREFIX}{component_id}"


def is_judge_ledger_version(analyst_version: str) -> bool:
    """Whether a ``budget_ledger`` row belongs to the judge population.

    The read-side predicate for splitting the ledger — a reporting query wanting
    generation-only totals filters these out, and one wanting judge cost filters
    them in.
    """
    return str(analyst_version or "").startswith(JUDGE_LEDGER_VERSION_PREFIX)


async def record_judge_calls_budget(
    conn: asyncpg.Connection,
    *,
    analyst_id: str,
    calls: "Sequence[Mapping[str, Any]]",
    bucket: date | None = None,
) -> list[BudgetLedgerRow]:
    """Meter a run's JUDGE LLM calls into ``budget_ledger``. Returns the rows.

    ``calls`` are ``run_accounting`` records (the ones
    :meth:`LLMProviderHandler._account_call` produced) — each carrying
    ``component_id``, ``subprovider``, ``model`` and token counts. They are
    grouped by ``(component_id, subprovider, model)`` so one run writes ONE
    upsert per distinct judge target, not one per claim partition; a
    long finding can partition into many judge calls and they must not become
    many round-trips.

    ``runs`` accumulates the judge CALL count (not the analyst run count), which
    is the denominator that makes tokens-per-judge-call readable — the figure
    that was unobtainable while the leg was unmetered.

    Cost resolves through the same :func:`compute_cost_usd` price-table dispatch
    every other row uses. An unpriced model contributes 0 to cost while its
    tokens still accumulate — the honest, pre-existing behaviour for self-hosted
    and not-yet-priced endpoints, not a judge-specific fudge.

    Calls with no usable token counts (a failed call — status != success — has
    none) are skipped for the ledger: they are already evidenced per-call in the
    trace receipt, and inventing a zero-token row would misreport the run count.
    """
    if not calls:
        return []

    grouped: dict[tuple[str, str, str], dict[str, int]] = {}
    for call in calls:
        component_id = str(call.get("component_id") or "")
        if not component_id:
            # Nothing to key a dimension on. The call is still in the receipt.
            continue
        prompt = int(call.get("prompt_tokens") or 0)
        completion = int(call.get("completion_tokens") or 0)
        reasoning = int(call.get("reasoning_tokens") or 0)
        if prompt + completion + reasoning <= 0:
            continue
        key = (
            component_id,
            str(call.get("subprovider") or ""),
            str(call.get("model") or ""),
        )
        acc = grouped.setdefault(
            key,
            {"prompt": 0, "completion": 0, "reasoning": 0,
             "cache_read": 0, "cache_write": 0, "calls": 0},
        )
        acc["prompt"] += prompt
        acc["completion"] += completion
        acc["reasoning"] += reasoning
        acc["cache_read"] += int(call.get("cache_read_tokens") or 0)
        acc["cache_write"] += int(call.get("cache_write_tokens") or 0)
        acc["calls"] += 1

    rows: list[BudgetLedgerRow] = []
    for (component_id, subprovider, model), acc in grouped.items():
        rows.append(
            await record_budget(
                conn,
                analyst_id=analyst_id,
                analyst_version=judge_ledger_version(component_id),
                provider=subprovider,
                model=model,
                prompt_tokens=acc["prompt"],
                completion_tokens=acc["completion"],
                reasoning_tokens=acc["reasoning"],
                cache_read_tokens=acc["cache_read"],
                cache_write_tokens=acc["cache_write"],
                bucket=bucket,
                runs_increment=acc["calls"],
            )
        )
    return rows


__all__ = [
    "BudgetLedgerRow",
    "JUDGE_LEDGER_VERSION_PREFIX",
    "compute_cost_usd",
    "is_judge_ledger_version",
    "judge_ledger_version",
    "record_budget",
    "record_judge_calls_budget",
]

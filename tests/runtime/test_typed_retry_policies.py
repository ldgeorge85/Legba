# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Tests for typed retry policies per failure mode (Phase 5 hardening item 5).

The runtime classifies in-flight exceptions into three buckets:

  * transient — :class:`TransientLLMFailure` (5xx, 429, network).
  * budget    — :class:`BudgetExhausted`.
  * hard      — :class:`HardLLMFailure` (4xx, validation, auth) +
                everything else (defensive).

Each bucket has its own policy declared via the descriptor's
``method.retry`` block. Defaults preserve the prior behavior:

  * transient: 3 attempts, exponential backoff up to 60 s.
  * budget:    pause_until_next_window (cooldown until next bucket start).
  * hard:      dlq_and_alert (single attempt, no retry).

These tests exercise the policy classification + delay computation
helpers directly. The end-to-end "actor retries n times then gives up"
test is covered by the spike integration suite under
``test_spike_integration.py`` — those tests run against a real Dapr
sidecar and would double-cover the surface here.
"""

from __future__ import annotations

import pytest

# Warm the registry side of the legba.data.stack.llm ↔
# legba.data.provenance.budget cycle FIRST so the package init chain
# resolves in the right order (see tests/data_pkg/test_stack_llm.py for
# the same workaround).
from legba.data.registry.credentials import MissingSecretError  # noqa: F401

from legba.data.stack.llm import (
    BudgetExhausted,
    HardLLMFailure,
    TransientLLMFailure,
)
from legba.data.schemas.analyst import (
    BudgetRetryPolicy,
    HardRetryPolicy,
    RetryBlock,
    TransientRetryPolicy,
)
from legba.runtime.dapr_actors import (
    _classify_exception,
    _retry_delay_seconds,
)


# ---------------------------------------------------------------------------
# _classify_exception — exception → bucket name
# ---------------------------------------------------------------------------


def test_classify_budget_exhausted() -> None:
    assert _classify_exception(BudgetExhausted("over cap")) == "budget"


def test_classify_transient_llm_failure() -> None:
    assert _classify_exception(TransientLLMFailure("429", status=429)) == "transient"


def test_classify_hard_llm_failure() -> None:
    assert _classify_exception(HardLLMFailure("401", status=401)) == "hard"


def test_classify_unknown_exception_is_hard() -> None:
    """Unrecognized exceptions classify conservatively as hard."""
    assert _classify_exception(ValueError("anything")) == "hard"
    assert _classify_exception(RuntimeError("anything")) == "hard"


# ---------------------------------------------------------------------------
# RetryBlock defaults match the prior implicit behavior
# ---------------------------------------------------------------------------


def test_retry_block_defaults() -> None:
    b = RetryBlock()
    assert b.transient.max_attempts == 3
    assert b.transient.backoff == "exponential"
    assert b.transient.max_delay_seconds == 60.0
    assert b.budget.strategy == "pause_until_next_window"
    assert b.hard.strategy == "dlq_and_alert"
    assert b.hard.max_attempts == 1


def test_retry_block_accepts_custom_transient() -> None:
    b = RetryBlock(
        transient=TransientRetryPolicy(
            max_attempts=5,
            backoff="linear",
            initial_delay_seconds=2.0,
            max_delay_seconds=20.0,
        ),
    )
    assert b.transient.max_attempts == 5
    assert b.transient.backoff == "linear"
    # Other blocks default.
    assert b.budget.strategy == "pause_until_next_window"


def test_retry_block_accepts_demote_strategy() -> None:
    b = RetryBlock(
        budget=BudgetRetryPolicy(strategy="demote_and_continue"),
    )
    assert b.budget.strategy == "demote_and_continue"


# ---------------------------------------------------------------------------
# _retry_delay_seconds — exponential / linear / constant
# ---------------------------------------------------------------------------


def test_exponential_backoff_doubles() -> None:
    p = TransientRetryPolicy(
        backoff="exponential",
        initial_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=100.0,
    )
    assert _retry_delay_seconds(p, attempt=1) == 1.0  # 1 * 2^0
    assert _retry_delay_seconds(p, attempt=2) == 2.0  # 1 * 2^1
    assert _retry_delay_seconds(p, attempt=3) == 4.0  # 1 * 2^2
    assert _retry_delay_seconds(p, attempt=4) == 8.0  # 1 * 2^3


def test_exponential_capped_at_max_delay() -> None:
    p = TransientRetryPolicy(
        backoff="exponential",
        initial_delay_seconds=1.0,
        multiplier=2.0,
        max_delay_seconds=10.0,
    )
    # 1 * 2^7 = 128 — should clamp to 10.
    assert _retry_delay_seconds(p, attempt=8) == 10.0


def test_linear_backoff_scales_with_attempt() -> None:
    p = TransientRetryPolicy(
        backoff="linear",
        initial_delay_seconds=2.0,
        multiplier=1.0,
        max_delay_seconds=100.0,
    )
    assert _retry_delay_seconds(p, attempt=1) == 2.0   # 2 * 1
    assert _retry_delay_seconds(p, attempt=2) == 4.0   # 2 * 2
    assert _retry_delay_seconds(p, attempt=3) == 6.0   # 2 * 3


def test_constant_backoff_ignores_attempt() -> None:
    p = TransientRetryPolicy(
        backoff="constant",
        initial_delay_seconds=5.0,
        multiplier=2.0,
        max_delay_seconds=100.0,
    )
    assert _retry_delay_seconds(p, attempt=1) == 5.0
    assert _retry_delay_seconds(p, attempt=5) == 5.0


# ---------------------------------------------------------------------------
# Schema-level validation — strategies / kinds are constrained
# ---------------------------------------------------------------------------


def test_invalid_backoff_mode_rejected() -> None:
    with pytest.raises(Exception):
        TransientRetryPolicy(backoff="random_walk")


def test_invalid_budget_strategy_rejected() -> None:
    with pytest.raises(Exception):
        BudgetRetryPolicy(strategy="silently_succeed")


def test_invalid_hard_strategy_rejected() -> None:
    with pytest.raises(Exception):
        HardRetryPolicy(strategy="resurrect")


# ---------------------------------------------------------------------------
# MethodBlock integration — retry block lands on the descriptor
# ---------------------------------------------------------------------------


def test_method_block_carries_retry_block() -> None:
    from legba.data.schemas.analyst import MethodBlock

    m = MethodBlock(
        kind="llm_single_turn",
        prompt_module="path:to:module",
        retry=RetryBlock(
            transient=TransientRetryPolicy(max_attempts=5, max_delay_seconds=30.0),
            budget=BudgetRetryPolicy(strategy="demote_and_continue"),
            hard=HardRetryPolicy(strategy="dlq_and_alert", max_attempts=1),
        ),
    )
    assert m.retry.transient.max_attempts == 5
    assert m.retry.budget.strategy == "demote_and_continue"
    assert m.retry.hard.strategy == "dlq_and_alert"


def test_method_block_defaults_retry_when_absent() -> None:
    from legba.data.schemas.analyst import MethodBlock

    m = MethodBlock(kind="llm_single_turn", prompt_module="path:to:module")
    assert m.retry.transient.max_attempts == 3   # default
    assert m.retry.budget.strategy == "pause_until_next_window"
    assert m.retry.hard.strategy == "dlq_and_alert"


# ---------------------------------------------------------------------------
# Retry-loop simulation — ensure attempt-counter matches policy
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_transient_retry_loop_honors_max_attempts() -> None:
    """Simulate the inner retry loop: a TransientLLMFailure raises every time;
    the loop should attempt exactly ``max_attempts`` times.
    """
    import asyncio

    transient_policy = TransientRetryPolicy(
        max_attempts=4,
        backoff="constant",
        initial_delay_seconds=0.001,
        max_delay_seconds=0.001,
    )

    attempts_made = 0

    async def run_method(inputs, options, deps):
        nonlocal attempts_made
        attempts_made += 1
        raise TransientLLMFailure(f"attempt {attempts_made}", status=503)

    # Mirror the actor's retry loop body (minus the actor scaffolding).
    last_exc = None
    for attempt in range(1, transient_policy.max_attempts + 1):
        try:
            await run_method([], {}, None)
        except BaseException as exc:
            last_exc = exc
            bucket = _classify_exception(exc)
            if bucket != "transient":
                break
            if attempt >= transient_policy.max_attempts:
                break
            delay = _retry_delay_seconds(transient_policy, attempt)
            await asyncio.sleep(delay)

    assert attempts_made == 4
    assert isinstance(last_exc, TransientLLMFailure)


@pytest.mark.asyncio
async def test_hard_failure_short_circuits_transient_retry() -> None:
    """A hard exception breaks out of the transient retry loop on attempt 1."""
    transient_policy = TransientRetryPolicy(max_attempts=10)

    attempts_made = 0

    async def run_method(inputs, options, deps):
        nonlocal attempts_made
        attempts_made += 1
        raise HardLLMFailure("401 unauthorized", status=401)

    last_exc = None
    for attempt in range(1, transient_policy.max_attempts + 1):
        try:
            await run_method([], {}, None)
        except BaseException as exc:
            last_exc = exc
            bucket = _classify_exception(exc)
            if bucket != "transient":
                break

    assert attempts_made == 1, (
        "Hard failures must NOT consume transient retry budget — "
        "the runtime classifies hard once and DLQs"
    )
    assert isinstance(last_exc, HardLLMFailure)


@pytest.mark.asyncio
async def test_budget_failure_short_circuits_transient_retry() -> None:
    """A budget exception breaks out of the transient retry loop on attempt 1."""
    transient_policy = TransientRetryPolicy(max_attempts=10)

    attempts_made = 0

    async def run_method(inputs, options, deps):
        nonlocal attempts_made
        attempts_made += 1
        raise BudgetExhausted("over per-analyst cap")

    last_exc = None
    for attempt in range(1, transient_policy.max_attempts + 1):
        try:
            await run_method([], {}, None)
        except BaseException as exc:
            last_exc = exc
            bucket = _classify_exception(exc)
            if bucket != "transient":
                break

    assert attempts_made == 1
    assert isinstance(last_exc, BudgetExhausted)

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
    _transient_retry_delay,
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


# ---------------------------------------------------------------------------
# A provider Retry-After floors the transient wait
# ---------------------------------------------------------------------------


def test_transient_delay_without_retry_after_is_the_policy_delay() -> None:
    """No header carried: exactly ``_retry_delay_seconds`` (byte-identical)."""
    p = TransientRetryPolicy()  # 1 s exponential, 3 attempts
    exc = TransientLLMFailure("503", status=503)
    for attempt in (1, 2):
        delay, ra = _transient_retry_delay(p, attempt, exc)
        assert delay == _retry_delay_seconds(p, attempt)
        assert ra is None


def test_transient_delay_honours_gateway_23s_retry_after() -> None:
    """The live case: 429 + Retry-After 23 vs the default 1 s, 2 s policy."""
    p = TransientRetryPolicy()
    exc = TransientLLMFailure(
        "vllm 429: too many concurrent requests", status=429, retry_after=23.0
    )
    assert _transient_retry_delay(p, 1, exc) == (23.0, 23.0)
    assert _transient_retry_delay(p, 2, exc) == (23.0, 23.0)


def test_transient_delay_keeps_a_longer_policy_delay() -> None:
    """``max(policy, retry_after)`` — a policy already waiting longer wins."""
    p = TransientRetryPolicy(backoff="constant", initial_delay_seconds=45.0)
    exc = TransientLLMFailure("429", status=429, retry_after=23.0)
    assert _transient_retry_delay(p, 1, exc) == (45.0, 23.0)


def test_transient_delay_caps_retry_after() -> None:
    """A Retry-After carried uncapped (another raise site) is capped at 120 s."""
    from legba.data.stack.llm.base import RETRY_AFTER_CAP_S

    p = TransientRetryPolicy()
    exc = TransientLLMFailure("429", status=429, retry_after=3600.0)
    assert _transient_retry_delay(p, 1, exc) == (RETRY_AFTER_CAP_S, RETRY_AFTER_CAP_S)
    assert RETRY_AFTER_CAP_S == 120.0


def test_transient_delay_reads_retry_after_through_an_explicit_cause() -> None:
    """A kind that re-raises ``from`` the client error keeps the instruction."""
    p = TransientRetryPolicy()
    inner = TransientLLMFailure("429", status=429, retry_after=23.0)
    try:
        try:
            raise inner
        except TransientLLMFailure as e:
            raise TransientLLMFailure("analyst call failed") from e
    except TransientLLMFailure as outer:
        assert _transient_retry_delay(p, 1, outer) == (23.0, 23.0)


@pytest.mark.parametrize("bad", [None, -1.0, float("nan"), float("inf"), True, "23"])
def test_transient_delay_ignores_unusable_retry_after(bad) -> None:
    p = TransientRetryPolicy()
    exc = TransientLLMFailure("429", status=429)
    exc.retry_after = bad
    assert _transient_retry_delay(p, 1, exc) == (1.0, None)


@pytest.mark.asyncio
async def test_retry_loop_with_retry_after_keeps_the_attempt_count(monkeypatch) -> None:
    """Mirror of the actor loop: Retry-After stretches the waits, never the count."""
    import asyncio

    slept: list[float] = []

    async def _fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(asyncio, "sleep", _fake_sleep)
    transient_policy = TransientRetryPolicy()  # 3 attempts, 1 s / 2 s
    attempts_made = 0

    async def run_method(inputs, options, deps):
        nonlocal attempts_made
        attempts_made += 1
        raise TransientLLMFailure("429", status=429, retry_after=23.0)

    for attempt in range(1, transient_policy.max_attempts + 1):
        try:
            await run_method([], {}, None)
        except BaseException as exc:
            if _classify_exception(exc) != "transient":
                break
            if attempt >= transient_policy.max_attempts:
                break
            delay, _ra = _transient_retry_delay(transient_policy, attempt, exc)
            await asyncio.sleep(delay)

    assert attempts_made == 3
    assert slept == [23.0, 23.0]


def test_actor_loop_uses_the_retry_after_delay_and_logs_it() -> None:
    """Source guard: the live actor loop calls the helper and logs the value.

    W11-F133 moved the loop verbatim into ``actor_retry._invoke_with_transient_retry``;
    the actor must call it, and the helper must keep the Retry-After contract.
    """
    import inspect

    from legba.runtime import actor_retry
    from legba.runtime.dapr_actors import AnalystActor

    actor_src = inspect.getsource(AnalystActor)
    assert "_invoke_with_transient_retry(" in actor_src
    assert "policy=transient_policy" in actor_src
    src = inspect.getsource(actor_retry._invoke_with_transient_retry)
    assert "_transient_retry_delay(policy, attempt, exc)" in src
    assert "retry_after_s=%s" in src
    assert "delay = _retry_delay_seconds(" not in src + actor_src

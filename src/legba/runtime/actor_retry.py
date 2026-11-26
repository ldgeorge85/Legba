# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Retry classification cluster (G) — extracted from dapr_actors.py (#93), behavior-preserving move."""

from __future__ import annotations

import asyncio
import logging
import math
from typing import Any, Awaitable, Callable

# The retry loop below was moved out of ``dapr_actors``; it logs under that
# module's logger so its ``transient_retry`` lines keep their logger name.
logger = logging.getLogger("legba.runtime.dapr_actors")


def _classify_exception(exc: BaseException) -> str:
    """Bucket an in-flight exception into transient / budget / hard.

    Resolution order (most specific first):

      * :class:`BudgetExhausted`   → ``"budget"``.
      * :class:`TransientLLMFailure` (5xx, 429, network) → ``"transient"``.
      * :class:`HardLLMFailure` (4xx, validation, auth)  → ``"hard"``.
      * Anything else → ``"hard"`` (conservative — unrecognized failures
        DLQ rather than retry indefinitely).

    Source handlers and filter chains throw bare ``Exception`` subclasses
    rather than typed LLM failures; we still pessimistically classify
    those as ``hard`` so a noisy upstream doesn't loop forever.  Source
    handlers that WANT transient behavior should subclass
    :class:`TransientLLMFailure` (or its sibling source-side class once
    it lands).

    Imports are lazy to avoid a circular-import cycle through
    ``legba.data.stack.llm.*`` (which itself imports from provenance,
    which imports from stack.llm).
    """
    # Lazy import — see docstring.
    from ..data.stack.llm.base import (
        BudgetExhausted,
        HardLLMFailure,
        TransientLLMFailure,
    )

    if isinstance(exc, BudgetExhausted):
        return "budget"
    if isinstance(exc, TransientLLMFailure):
        return "transient"
    if isinstance(exc, HardLLMFailure):
        return "hard"
    return "hard"


def _retry_delay_seconds(
    policy: Any,
    attempt: int,
) -> float:
    """Compute the wait-before-next-attempt for a transient retry policy.

    ``attempt`` is 1-indexed (first retry = 1). The policy carries
    ``backoff`` mode + ``initial_delay_seconds`` + ``multiplier`` +
    ``max_delay_seconds``.

    Modes:

      * exponential: initial * multiplier**(attempt-1)
      * linear:      initial * attempt
      * constant:    initial
    """
    initial = float(getattr(policy, "initial_delay_seconds", 1.0))
    multiplier = float(getattr(policy, "multiplier", 2.0))
    max_delay = float(getattr(policy, "max_delay_seconds", 60.0))
    mode = str(getattr(policy, "backoff", "exponential"))
    if mode == "linear":
        delay = initial * attempt
    elif mode == "constant":
        delay = initial
    else:
        delay = initial * (multiplier ** max(0, attempt - 1))
    return min(delay, max_delay)


def _exception_retry_after(exc: BaseException) -> float | None:
    """The provider's ``Retry-After`` carried on a transient failure, capped.

    Reads ``retry_after`` (seconds, set by the LLM HTTP layer from the 429/503
    header) off ``exc`` or, when a kind re-raised it, off its explicit
    ``__cause__`` chain. ``None`` when nothing carries a usable value -- the
    policy schedule then stands alone, byte-identical to before W11-F101.
    """
    from ..data.stack.llm.base import RETRY_AFTER_CAP_S  # lazy -- see above

    seen = 0
    cur: BaseException | None = exc
    while cur is not None and seen < 5:
        raw = getattr(cur, "retry_after", None)
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            value = float(raw)
            if math.isfinite(value) and value >= 0:
                return min(value, RETRY_AFTER_CAP_S)
        cur = cur.__cause__
        seen += 1
    return None


def _transient_retry_delay(
    policy: Any,
    attempt: int,
    exc: BaseException,
) -> tuple[float, float | None]:
    """The wait before the next transient attempt, and the Retry-After honoured.

    The gateway answers 429 with ``Retry-After: 23`` while the default
    policy waits 1 s, 2 s -- three attempts inside four seconds never clear a
    23 s window. When the failure carries the server's instruction the delay
    is ``max(policy delay, retry_after)``, the latter capped at
    ``RETRY_AFTER_CAP_S``; the attempt count is untouched. Without it the
    delay is exactly :func:`_retry_delay_seconds`.
    """
    delay = _retry_delay_seconds(policy, attempt)
    retry_after = _exception_retry_after(exc)
    if retry_after is not None:
        delay = max(delay, retry_after)
    return delay, retry_after


def _effective_transient_attempts(policy: Any, kind_deps: Any) -> int:
    """How many times the actor may run ``run_method`` for one request.

    The descriptor's ``method.retry.transient.max_attempts`` — EXCEPT for a
    streaming, user-facing run (its deps carry a ``step_publish``: an on-demand
    consult with a live request stream), which runs ONCE. W11-F133, consult
    6cd91190 (2026-10-29): the forced final failed with a transient network
    error after 19 minutes and the loop below re-ran the WHOLE consult from
    scratch on the same request id — a second render_prompt, 19 more rounds,
    162 tool calls, re-spent with nobody asked — and that run died too. A
    consult's failure (or its partial) is the operator's to see and re-ask;
    the descriptors say so as well (``max_attempts: 1``), this guard holds it
    for a descriptor that does not.
    """
    attempts = int(getattr(policy, "max_attempts", 3) or 1)
    if getattr(kind_deps, "step_publish", None) is not None:
        return 1
    return max(1, attempts)


async def _invoke_with_transient_retry(
    invoke: Callable[[], Awaitable[Any]],
    *,
    policy: Any,
    max_attempts: int,
    actor_id: str,
    on_attempt: Callable[[int], None] | None = None,
) -> Any:
    """Run ``invoke`` up to ``max_attempts`` times, retrying TRANSIENT failures only.

    Moved verbatim from the analyst actor's run loop so the attempt
    budget is testable without an actor. Non-transient failures and the last
    attempt's failure propagate to the caller's classifier; ``on_attempt`` is
    told each attempt's 1-based number before it runs.
    """
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if on_attempt is not None:
            on_attempt(attempt)
        try:
            return await invoke()
        except BaseException as exc:
            last_exc = exc
            bucket = _classify_exception(exc)
            if bucket != "transient":
                # Non-transient — let the outer handler classify.
                raise
            if attempt >= max_attempts:
                # Out of attempts — raise so the outer handler records
                # HARD_FAIL / DLQ per hard_policy.
                raise
            # A provider Retry-After floors the wait.
            delay, ra_s = _transient_retry_delay(policy, attempt, exc)
            logger.warning(
                "dapr_actors.analyst.transient_retry actor_id=%s "
                "attempt=%d/%d delay_s=%.2f retry_after_s=%s err=%s",
                actor_id, attempt, max_attempts, delay, ra_s, exc,
            )
            await asyncio.sleep(delay)
    # Defensive — the loop either returns or re-raises.
    if last_exc is not None:
        raise last_exc
    raise RuntimeError("transient retry loop ran zero attempts")

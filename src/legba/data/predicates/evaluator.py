# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Runtime evaluation + budget enforcement.

Spec §3 sandbox limits:

  * No I/O — enforced by Starlark natively (no syscall surface exposed).
  * No imports / no ``load()`` — Starlark parser rejects ``import``;
    ``load()`` requires an explicit ``FileLoader`` which we never supply.
  * No ``while`` loops — Starlark reserves the keyword; parse-time reject.
  * No recursion — enforced at compile time by the source-text gate
    (`def` is banned outright, so user-defined functions cannot exist).
  * Bounded value growth — host-side caps in the helper layer (sets and
    lists round-tripped through ``_as_set`` / ``_as_list`` stay small).
  * Step cap — spec calls for 50k steps; ``starlark-pyo3`` does not expose
    ``SetMaxExecutionSteps`` on its Evaluator handle (still absent as of the
    installed build). Tracked as L-242 followup.
  * STRUCTURAL cost bound (the thread-safe enforcement) — predicates are
    single boolean EXPRESSIONS with no ``while``/``def``/recursion/``import``
    (grammar gate in ``compiler._check_source_gate``) AND a compile-time
    source-LENGTH cap (``compiler._MAX_SOURCE_CHARS``). A bounded-length
    expression with no iteration has a bounded AST and therefore bounded
    evaluation cost on ANY thread — so per-eval cost is bounded BEFORE
    execution, deterministically, regardless of which thread evaluates. This
    is the real off-main-thread guarantee.
  * VALUE / MATERIALIZATION cost bound (the other thread-safe enforcement) —
    the structural bound caps the AST *shape*, but a short source can still
    iterate/materialize a huge collection (``[x for x in range(99999999)]`` is
    structurally tiny). ``compiler._check_iteration_cost`` bounds the literal
    size of ``range(N)`` and sequence-repeats ``[...]*N`` at COMPILE time
    (``compiler._MAX_ITERATION_COUNT``) and rejects variable ranges, so a
    materializing predicate is refused before it ever runs — closing the gap
    the post-hoc wall-clock timer below cannot cover off-thread.
  * Wall-clock budget — 5 ms default, enforced here via ``signal.SIGALRM`` ON
    THE MAIN THREAD ONLY (SIGALRM cannot be delivered to worker threads).
    Off-thread evaluations (the actor worker-thread hot path) get best-effort
    post-hoc enforcement — the timer is measured AFTER ``func()`` returns, so a
    long materialization completes before it can fire; the STRUCTURAL and
    VALUE/MATERIALIZATION bounds above — not this timer — are what actually
    cap off-thread predicate cost.
  * Memory cap — host-side per-value caps in the helper layer; full
    process memory cap not enforced at the predicate boundary.

Public entry: ``run_with_budget(callable, budget)``.
"""

from __future__ import annotations

import os
import signal
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from .errors import PredicateBudgetExceeded


# Default wall-clock budget per spec §3
DEFAULT_WALL_CLOCK_MS = 5.0


def _default_wall_clock_ms() -> float:
    """Resolve the default wall-clock budget for an :class:`EvalBudget`.

    Production default is the spec §3 ``DEFAULT_WALL_CLOCK_MS`` (5 ms).
    ``LEGBA_PREDICATE_WALL_CLOCK_MS`` overrides it for environments where
    wall-clock is not a faithful proxy for predicate cost — specifically the
    test suite, where a cold first ``starlark.eval`` under full-suite CPU
    load (scheduler preemption counts against wall-clock) made the 5 ms
    SIGALRM fire nondeterministically (the ``test_mentions_*`` /
    ctx-parity / applicability-predicate order-dependent flake family).
    The env var is read per-construction so a caller that builds an
    explicit ``EvalBudget(wall_clock_ms=...)`` — every budget-enforcement
    test does — is untouched, and an unset/invalid/non-positive value
    falls back to the production default. This is a budget *envelope*
    knob, not a semantics change: enforcement still runs identically.
    """
    raw = os.environ.get("LEGBA_PREDICATE_WALL_CLOCK_MS", "").strip()
    if not raw:
        return DEFAULT_WALL_CLOCK_MS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_WALL_CLOCK_MS
    return value if value > 0 else DEFAULT_WALL_CLOCK_MS


@dataclass(frozen=True)
class EvalBudget:
    """Per-evaluation budget envelope.

    Only ``wall_clock_ms`` is enforced today; ``max_steps`` and
    ``max_memory_bytes`` carry forward to L-242 followup once the
    underlying binding exposes the corresponding setters.

    ``wall_clock_ms`` defaults to :data:`DEFAULT_WALL_CLOCK_MS` (5 ms,
    spec §3) unless ``LEGBA_PREDICATE_WALL_CLOCK_MS`` overrides it — see
    :func:`_default_wall_clock_ms`.
    """

    wall_clock_ms: float = field(default_factory=_default_wall_clock_ms)
    max_steps: int = 50_000
    max_memory_bytes: int = 1 * 1024 * 1024  # 1 MiB


class _BudgetTimeout(BaseException):
    """Internal sentinel used to bridge SIGALRM → PredicateBudgetExceeded.

    Subclasses BaseException so user-side ``except Exception`` clauses in
    helper code cannot accidentally swallow it.
    """

    def __init__(self, budget_ms: float) -> None:
        super().__init__(f"wall-clock budget {budget_ms} ms exceeded")
        self.budget_ms = budget_ms


def _signal_handler_factory(budget_ms: float) -> Callable[[int, Any], None]:
    def _handler(signum: int, frame: Any) -> None:
        raise _BudgetTimeout(budget_ms)
    return _handler


def _on_main_thread() -> bool:
    return threading.current_thread() is threading.main_thread()


def run_with_budget(
    func: Callable[[], Any],
    *,
    budget: EvalBudget,
) -> Any:
    """Run ``func()`` under the supplied budget.

    Main-thread path uses ``signal.SIGALRM`` + ``signal.setitimer`` to fire
    a timeout exception mid-evaluation. This works for any synchronous
    helper that ever returns to the Python interpreter — which is every
    helper we expose, since they're pure Python functions called by the
    Rust evaluator across the FFI boundary.

    Off-thread path is best-effort: we measure elapsed time before/after
    ``func()`` and raise ``PredicateBudgetExceeded`` post-hoc if the budget
    was breached. The work already ran in that case; the value is just
    not returned. This is a known limitation tracked as L-242 followup.
    """
    wall_ms = budget.wall_clock_ms

    if _on_main_thread():
        return _run_with_sigalrm(func, wall_ms)
    return _run_off_thread(func, wall_ms)


def _run_with_sigalrm(func: Callable[[], Any], wall_ms: float) -> Any:
    # Convert ms → seconds for setitimer; clamp tiny values to a minimum
    # tick to avoid the timer firing before setup completes.
    seconds = max(wall_ms / 1000.0, 0.0001)
    prev_handler = signal.getsignal(signal.SIGALRM)
    prev_itimer = signal.getitimer(signal.ITIMER_REAL)
    handler = _signal_handler_factory(wall_ms)
    signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    started = time.perf_counter()
    try:
        return func()
    except _BudgetTimeout as exc:
        elapsed_ms = (time.perf_counter() - started) * 1000.0
        raise PredicateBudgetExceeded(
            f"predicate exceeded wall-clock budget {wall_ms:.3f} ms",
            limit="wall_clock_ms",
            budget_value=wall_ms,
            observed_value=elapsed_ms,
        ) from exc
    except BaseException as exc:
        # Some Starlark exceptions wrap the host-raised _BudgetTimeout
        # (the Rust evaluator catches Python exceptions raised inside a
        # bound callable and re-raises them as StarlarkError). Detect that
        # case via the chained __cause__ / context.
        if _is_budget_chain(exc):
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            raise PredicateBudgetExceeded(
                f"predicate exceeded wall-clock budget {wall_ms:.3f} ms",
                limit="wall_clock_ms",
                budget_value=wall_ms,
                observed_value=elapsed_ms,
            ) from exc
        raise
    finally:
        # Restore prior signal state. Note that ITIMER_REAL state restoration
        # is a no-op if the timer already fired (setitimer with 0 disables).
        signal.setitimer(signal.ITIMER_REAL, *prev_itimer)
        signal.signal(signal.SIGALRM, prev_handler)


def _run_off_thread(func: Callable[[], Any], wall_ms: float) -> Any:
    """Off-main-thread evaluation path. Best-effort timing check."""
    started = time.perf_counter()
    result = func()
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    if elapsed_ms > wall_ms:
        raise PredicateBudgetExceeded(
            f"predicate exceeded wall-clock budget {wall_ms:.3f} ms "
            f"(off-thread post-hoc detection — work already executed)",
            limit="wall_clock_ms",
            budget_value=wall_ms,
            observed_value=elapsed_ms,
        )
    return result


def _is_budget_chain(exc: BaseException) -> bool:
    """Walk an exception's __cause__/__context__ chain to find a _BudgetTimeout."""
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        if isinstance(cur, _BudgetTimeout):
            return True
        seen.add(id(cur))
        # starlark-pyo3 wraps Python exceptions via __cause__ in some cases
        # and string-embeds them in the StarlarkError message otherwise. The
        # message-embedded form is detectable via class name; check both.
        if cur.__cause__ is not None:
            cur = cur.__cause__
        else:
            cur = cur.__context__
    # Fallback: check the string form
    msg = str(exc)
    return "_BudgetTimeout" in msg or "wall-clock budget" in msg

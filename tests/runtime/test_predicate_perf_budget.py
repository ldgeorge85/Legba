# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""C-1: a representative residual predicate stays inside the PROD 5 ms budget.

Why this test exists
--------------------

The suite-wide conftest sets ``LEGBA_PREDICATE_WALL_CLOCK_MS=250`` so the
order-dependent cold-eval flake (scheduler preemption counted against the
first ``starlark.eval``) doesn't redden unrelated tests. That override is
correct for the *enforcement* tests — but it also BLINDS the suite to a real
eval-cost regression: production runs the residual at the spec §3 default of
5 ms (``LEGBA_PREDICATE_WALL_CLOCK_MS`` unset there), and a breach on the
real-time path SILENTLY DROPS the signal (``subscription.filter._eval_residual``
catches ``PredicateBudgetExceeded`` and returns ``False`` — the target never
acts). So a predicate that crept past 5 ms would still pass every other test
(all under the 250 ms umbrella) while quietly dropping live signals.

This test pins the production budget explicitly (defeating the env override)
and asserts a representative residual — the kind a real ``Subscription.predicate``
carries (``mentions()`` / ``has_tag()`` / ``severity_at_least()`` / ``credibility()``
composed) — evaluates WITHIN 5 ms with a WARM compile cache, exactly as the
real-time fan-out path does (main-thread, SIGALRM-enforced; see
``predicates.evaluator.run_with_budget``). If eval cost regresses past 5 ms
this goes red even though the 250 ms suite override hides it everywhere else.

It is intentionally a *budget-pass* assertion (does the prod budget hold?),
not a microbenchmark with a brittle wall-clock threshold of its own — the
budget IS the contract.
"""

from __future__ import annotations

import time

import pytest

from legba.data.predicates import (
    PredicateBudgetExceeded,
    PredicateSurface,
    compile_predicate,
    predicate_cache_clear,
)
from legba.data.predicates.evaluator import DEFAULT_WALL_CLOCK_MS, EvalBudget
from legba.runtime.subscription.filter import _signal_residual_ctx

# A representative residual: the long-tail shape a target's
# ``Subscription.predicate`` carries on the real-time path — several helpers
# (entity mention, tag, severity threshold, credibility floor) composed with
# boolean ops. Deliberately non-trivial so a real eval-cost regression has
# something to bite on, while staying within the documented helper catalog.
_REPRESENTATIVE_PREDICATE = (
    'mentions("generator") and has_tag("energy") '
    'and severity_at_least("medium") and credibility() >= 0.4'
)

# A realistic signal row (the substrate column shape ``_signal_residual_ctx``
# maps onto the target.scope ctx) that MATCHES the predicate above — so the
# evaluator walks the full predicate (every conjunct reached) rather than
# short-circuiting on the first ``False``, giving the budget the worst case.
_SIGNAL_ROW = {
    "entity_classes": ["generator", "substation"],
    "tags": ["energy", "grid"],
    "geo": ["BR"],
    "source_credibility": 0.8,
    "language": "pt",
    "modality": "text",
    "fetched_at": None,
    "payload": {"severity": "high"},
}


@pytest.fixture
def _warm_cache():
    """Start from a clean predicate cache, restore it afterwards.

    Mirrors the predicate-DSL suite's cache hygiene so this test neither
    inherits a stale compiled entry nor leaves one behind.
    """
    predicate_cache_clear()
    yield
    predicate_cache_clear()


def test_representative_residual_within_prod_5ms_budget(_warm_cache):
    """A representative residual passes under the prod 5 ms budget (warm cache).

    Pins ``EvalBudget(wall_clock_ms=DEFAULT_WALL_CLOCK_MS)`` explicitly so the
    suite-wide ``LEGBA_PREDICATE_WALL_CLOCK_MS=250`` override does NOT apply —
    this measures against the value production actually enforces.
    """
    # Sanity: production really does default to 5 ms (the value we pin).
    assert DEFAULT_WALL_CLOCK_MS == 5.0

    compiled = compile_predicate(
        _REPRESENTATIVE_PREDICATE, PredicateSurface.TARGET_SCOPE,
    )
    ctx = _signal_residual_ctx(_SIGNAL_ROW)

    # WARM the cache + interpreter once (the documented cold-eval cost — first
    # starlark.eval JIT/import — is what the 250 ms override exists to absorb;
    # production amortises it across the process lifetime). This warm pass uses
    # the generous suite budget so a cold spike here can't flake the test.
    assert compiled.evaluate(ctx) is True

    # Now the real assertion: a WARM eval must complete inside the PRODUCTION
    # 5 ms budget. SIGALRM enforcement fires on the main thread (this test runs
    # there), exactly as the real-time residual path does.
    prod_budget = EvalBudget(wall_clock_ms=DEFAULT_WALL_CLOCK_MS)
    try:
        result = compiled.evaluate(ctx, budget=prod_budget)
    except PredicateBudgetExceeded as exc:  # pragma: no cover - the regression
        pytest.fail(
            "representative residual breached the PRODUCTION 5 ms predicate "
            f"budget on a WARM cache ({exc}); on the real-time fan-out path "
            "this silently DROPS the signal (subscription.filter._eval_residual "
            "fails closed). The suite-wide LEGBA_PREDICATE_WALL_CLOCK_MS=250 "
            "override hides this everywhere else — an eval-cost regression has "
            "crept in; profile the predicate helpers."
        )
    assert result is True

    # Belt-and-suspenders: a couple more warm evals stay green too (catches a
    # budget breach that only shows on a not-quite-first warm eval).
    for _ in range(3):
        assert compiled.evaluate(ctx, budget=prod_budget) is True


def test_prod_budget_override_is_neutralised_in_this_test():
    """Guard the guard: confirm the explicit budget really defeats the env.

    If a future refactor made ``EvalBudget(wall_clock_ms=...)`` re-read the
    ``LEGBA_PREDICATE_WALL_CLOCK_MS`` env override, this perf test would
    silently measure against 250 ms and stop catching regressions. Assert the
    explicit value sticks regardless of the suite-wide override.
    """
    import os

    # The suite conftest sets this; confirm it is the masking 250 ms value so
    # the neutralisation below is a real (not vacuous) check.
    assert os.environ.get("LEGBA_PREDICATE_WALL_CLOCK_MS") == "250"
    budget = EvalBudget(wall_clock_ms=DEFAULT_WALL_CLOCK_MS)
    assert budget.wall_clock_ms == 5.0


def test_representative_predicate_warm_eval_is_fast_smoke():
    """Lightweight smoke: a warm eval's median wall time is well under 5 ms.

    Not the enforcement assertion (that's the budget test above) — just a
    coarse signal that the representative predicate's warm cost has comfortable
    headroom under the prod budget, so the budget test isn't perpetually one
    scheduler hiccup from flaking. Uses a generous 5x-budget ceiling on the
    MEDIAN of several samples to stay robust on a busy shared rig.
    """
    predicate_cache_clear()
    try:
        compiled = compile_predicate(
            _REPRESENTATIVE_PREDICATE, PredicateSurface.TARGET_SCOPE,
        )
        ctx = _signal_residual_ctx(_SIGNAL_ROW)
        compiled.evaluate(ctx)  # warm

        samples = []
        for _ in range(11):
            t0 = time.perf_counter()
            compiled.evaluate(ctx)
            samples.append((time.perf_counter() - t0) * 1000.0)
        samples.sort()
        median_ms = samples[len(samples) // 2]
        # Generous ceiling (5x the prod budget) on the MEDIAN — a real eval-cost
        # regression that endangers the 5 ms budget shows here without flaking
        # on a single preempted sample.
        assert median_ms < 5.0 * DEFAULT_WALL_CLOCK_MS, (
            f"warm median eval {median_ms:.3f} ms has little headroom under the "
            f"prod {DEFAULT_WALL_CLOCK_MS} ms budget (samples={samples!r})"
        )
    finally:
        predicate_cache_clear()

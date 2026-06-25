# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Unit tests for the Starlark predicate DSL (L-104 / L-242).

Covers:

  * Helper catalog — each of the 19 helpers exercised on at least one happy
    path + one edge case.
  * Compile path — example predicates from the spec parse + bind; bad
    sources fail with actionable error messages; surface-bound helper
    allow-list enforced.
  * Sandbox — banned constructs rejected; SIGALRM-based wall-clock budget
    triggers PredicateBudgetExceeded.
  * Cache — same source compiles once; different sources compile
    separately; counters increment as expected.
  * Schema integration — TargetScope / SubscriptionTargets / CadenceBlock
    refuse bad predicates at validation time.
  * Performance — sub-500-µs per-eval microbench (loose; spec target is
    10 µs, our default wraps each call in a SIGALRM setitimer which adds
    overhead).
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import pytest

from legba.data.predicates import (
    HELPER_CATALOG,
    HELPER_CATALOG_VERSION,
    HELPER_NAMES,
    CompiledPredicate,
    PredicateBudgetExceeded,
    PredicateCompilationError,
    PredicateRuntimeError,
    PredicateSurface,
    compile_predicate,
    predicate_cache_clear,
    predicate_cache_info,
    surface_helpers,
)
from legba.data.predicates.evaluator import (
    DEFAULT_WALL_CLOCK_MS,
    EvalBudget,
    run_with_budget,
)
from legba.data.predicates.helpers import (
    SURFACE_ANALYST_SUBSCRIPTION,
    SURFACE_CADENCE_TRIGGER,
    SURFACE_SOURCE_FILTER,
    SURFACE_TARGET_SCOPE,
)


# ---------------------------------------------------------------------------
# Catalog shape
# ---------------------------------------------------------------------------


def test_catalog_has_at_least_19_helpers():
    assert len(HELPER_CATALOG) >= 19
    # 19 named in spec §2 + contains_any (5c thematic free-text) = 20.
    assert len(HELPER_CATALOG) == 20, (
        f"expected 20 helpers, got {len(HELPER_CATALOG)}: {sorted(HELPER_CATALOG)}"
    )


def test_catalog_names_match_spec():
    expected = {
        "mentions", "mentions_any", "geo_match", "geo_in",
        "org_match", "has_tag", "has_any_tag", "severity_at_least",
        "recent", "signal_age_hours", "credibility", "entity_class_in",
        "scope_geo", "scope_entity_classes", "target_id", "target_kind",
        "abstraction_level", "event_type", "event_payload_get",
        "contains_any",   # 5c — free-text thematic matching
    }
    assert set(HELPER_NAMES) == expected


def test_catalog_version_is_semver():
    parts = HELPER_CATALOG_VERSION.split(".")
    assert len(parts) == 3 and all(p.isdigit() for p in parts)


# ---------------------------------------------------------------------------
# Helper happy + edge tests
# ---------------------------------------------------------------------------
#
# We exercise each helper by compiling a tiny predicate that references it,
# then evaluating against a tailored ctx. Helper factories are total — they
# never raise — so edge cases verify the "missing ctx → conservative
# default" behaviour documented in helpers.py.


@pytest.fixture(autouse=True)
def _clear_cache_between_tests():
    """Each test starts with a fresh cache; otherwise the cache hit/miss
    counts assertions interfere across tests."""
    predicate_cache_clear()
    yield
    predicate_cache_clear()


# Explicit ctx contracts for the helper-semantics tests below. The DEFAULT
# contract per surface is what PRODUCTION ctx-builders provide
# (helpers.SURFACE_CTX_CONTRACTS) — the cadence.trigger surface has NO
# production evaluator yet, and a few tests exercise helper paths
# (signal.org_match precompute) that production does not feed. Those tests
# declare their rig's ctx explicitly, which is exactly the escape hatch a
# future ctx-builder uses.
_TRIG_RIG_CTX = frozenset({
    "event.type", "event.payload", "event.severity",
    "event.timestamp", "event.tags",
})
_SCOPE_RIG_CTX = frozenset(
    {"signal.org_match", "signal.geo_match"}
) | frozenset({
    "signal.entity_classes", "signal.tags", "signal.geo_provenance",
    "signal.credibility", "signal.language", "signal.modality",
    "signal.event_timestamp", "signal.ingested_at",
    "signal.severity", "signal.classification_scores",
    "target.scope_geo", "target.tags",
})


def _eval_scope(src: str, ctx: dict) -> bool:
    return compile_predicate(
        src, PredicateSurface.TARGET_SCOPE, ctx_contract=_SCOPE_RIG_CTX
    ).evaluate(ctx)


def _eval_sub(src: str, ctx: dict) -> bool:
    return compile_predicate(src, PredicateSurface.ANALYST_SUBSCRIPTION).evaluate(ctx)


def _eval_trig(src: str, ctx: dict) -> bool:
    return compile_predicate(
        src, PredicateSurface.CADENCE_TRIGGER, ctx_contract=_TRIG_RIG_CTX
    ).evaluate(ctx)


def test_mentions_happy_and_missing():
    ctx = {"signal": {"entity_classes": ["generator", "substation"]}}
    assert _eval_scope('mentions("generator")', ctx) is True
    assert _eval_scope('mentions("politician")', ctx) is False
    # Missing entity_classes → False
    assert _eval_scope('mentions("generator")', {}) is False


def test_mentions_any():
    ctx = {"signal": {"entity_classes": ["regulator"]}}
    assert _eval_scope('mentions_any(["generator", "regulator"])', ctx) is True
    assert _eval_scope('mentions_any(["x", "y"])', ctx) is False
    # Empty list → False (no overlap)
    assert _eval_scope("mentions_any([])", ctx) is False


def test_geo_match_precomputed():
    assert _eval_scope("geo_match()", {"signal": {"geo_match": True}}) is True
    assert _eval_scope("geo_match()", {"signal": {"geo_match": False}}) is False


def test_geo_match_fallback_computes_from_overlap():
    ctx = {
        "signal": {"geo_provenance": ["BR", "AR"]},
        "target": {"scope_geo": ["BR", "CL"]},
    }
    assert _eval_scope("geo_match()", ctx) is True
    ctx_no = {
        "signal": {"geo_provenance": ["US"]},
        "target": {"scope_geo": ["BR"]},
    }
    assert _eval_scope("geo_match()", ctx_no) is False


def test_geo_match_explicit_code_list():
    # The per-target arg form the discovery / G20 registrars emit
    # (``geo_match(["BR"])``). Works on the residual path where only the signal
    # is in scope (no target context) — overlaps signal geo with the literal.
    ctx = {"signal": {"geo_provenance": ["BR", "AR"]}}
    assert _eval_scope('geo_match(["BR"])', ctx) is True
    assert _eval_scope('geo_match(["US"])', ctx) is False
    assert _eval_scope('geo_match([])', ctx) is False


def test_geo_in():
    ctx = {"signal": {"geo_provenance": ["BR"]}}
    assert _eval_scope('geo_in(["BR", "AR"])', ctx) is True
    assert _eval_scope('geo_in(["US"])', ctx) is False


def test_org_match_precomputed():
    # Runtime semantics under a rig contract that DOES feed signal.org_match.
    assert _eval_scope("org_match()", {"signal": {"org_match": True}}) is True
    # Missing → False (no overlap)
    assert _eval_scope("org_match()", {}) is False


def test_org_match_refused_under_production_contract():
    """G4: no production ctx-builder feeds org_match's required keys, so the
    DEFAULT (production) contract refuses it at compile time instead of
    registering a predicate that silently never matches."""
    with pytest.raises(PredicateCompilationError, match="org_match"):
        compile_predicate("org_match()", PredicateSurface.TARGET_SCOPE)


def test_has_tag_unions_across_contexts():
    # Same helper available on every surface; tags from any ctx slot count.
    ctx_signal = {"signal": {"tags": ["energy"]}}
    assert _eval_scope('has_tag("energy")', ctx_signal) is True
    assert _eval_scope('has_tag("steel")', ctx_signal) is False
    # Subscription surface: target.tags
    ctx_target = {"target": {"tags": ["energy"]}}
    assert _eval_sub('has_tag("energy")', ctx_target) is True


def test_has_any_tag():
    ctx = {"signal": {"tags": ["press_release", "energy"]}}
    assert _eval_scope('has_any_tag(["x", "energy"])', ctx) is True
    assert _eval_scope('has_any_tag(["x", "y"])', ctx) is False


def test_severity_at_least():
    ctx = {"signal": {"severity": "high"}}
    assert _eval_scope('severity_at_least("medium")', ctx) is True
    assert _eval_scope('severity_at_least("critical")', ctx) is False
    # Trigger surface picks up event.payload.severity
    ctx_evt = {"event": {"payload": {"severity": "CRITICAL"}}}
    assert _eval_trig('severity_at_least("high")', ctx_evt) is True
    # Unknown level → False (defensive)
    assert _eval_scope('severity_at_least("nonsense")', ctx) is False


def test_recent_iso_and_age():
    # Recent signal: 1 hour ago → recent(1) True, recent(0) False
    now = datetime.now(tz=timezone.utc)
    ctx = {"signal": {"event_timestamp": (now - timedelta(hours=1)).isoformat()}}
    assert _eval_scope("recent(1)", ctx) is True
    assert _eval_scope("recent(0)", ctx) is False
    # Ancient → False
    ctx_old = {"signal": {"event_timestamp": (now - timedelta(days=400)).isoformat()}}
    assert _eval_scope("recent(7)", ctx_old) is False


def test_signal_age_hours():
    now = datetime.now(tz=timezone.utc)
    ctx = {"signal": {"event_timestamp": (now - timedelta(hours=2)).isoformat()}}
    # Use a comparison to coerce the float helper into a bool predicate
    assert _eval_scope("signal_age_hours() >= 1.0", ctx) is True
    assert _eval_scope("signal_age_hours() < 10.0", ctx) is True
    # Missing → 0.0
    assert _eval_scope("signal_age_hours() == 0.0", {}) is True


def test_credibility():
    ctx = {"signal": {"credibility": 0.7}}
    assert _eval_scope("credibility() >= 0.5", ctx) is True
    assert _eval_scope("credibility() > 0.9", ctx) is False
    # Missing → 0.0 → False
    assert _eval_scope("credibility() > 0.0", {}) is False


def test_entity_class_in():
    ctx = {"signal": {"entity_classes": ["generator"]}}
    assert _eval_scope('entity_class_in(["generator", "substation"])', ctx) is True
    assert _eval_scope('entity_class_in(["politician"])', ctx) is False


def test_scope_geo_subscription_helper():
    ctx = {"target": {"scope_geo": ["BR", "AR"]}}
    assert _eval_sub('scope_geo()[0] == "BR"', ctx) is True
    assert _eval_sub("len(scope_geo()) >= 1", ctx) is True
    # Missing → []
    assert _eval_sub("len(scope_geo()) == 0", {}) is True


def test_scope_entity_classes_subscription_helper():
    ctx = {"target": {"scope_entity_classes": ["generator", "substation"]}}
    assert _eval_sub('"generator" in scope_entity_classes()', ctx) is True


def test_target_id_kind_abstraction_level():
    ctx = {
        "target": {
            "id": "india_energy",
            "kind": "country_news",
            "abstraction_level": "L1",
        }
    }
    assert _eval_sub('target_id() == "india_energy"', ctx) is True
    assert _eval_sub('target_kind() == "country_news"', ctx) is True
    assert _eval_sub('abstraction_level() == "L1"', ctx) is True
    # Missing → "" (still truthy-comparable)
    assert _eval_sub('target_id() == ""', {}) is True


def test_event_type_and_payload():
    ctx = {
        "event": {
            "type": "situation.escalation",
            "payload": {"severity": "high", "nested": {"k": "v"}},
        }
    }
    assert _eval_trig('event_type() == "situation.escalation"', ctx) is True
    assert _eval_trig('event_payload_get("severity") == "high"', ctx) is True
    assert _eval_trig('event_payload_get("nested.k") == "v"', ctx) is True
    # Missing path → None
    assert _eval_trig('event_payload_get("nope.path") == None', ctx) is True
    # Empty path → None
    assert _eval_trig('event_payload_get("") == None', ctx) is True


# ---------------------------------------------------------------------------
# Spec example predicates (§7) — compile clean
# ---------------------------------------------------------------------------


def test_spec_example_target_scope_compiles():
    # NOTE: the spec §7 example references org_match(), which no production
    # ctx-builder can feed — under the default contract it is refused (see
    # test_org_match_refused_under_production_contract). The rig contract
    # declares the precomputed keys so the example's RUNTIME semantics stay
    # covered.
    src = (
        'mentions_any(["generator", "substation", "transmission_line", '
        '"regulator", "utility", "energy_event"]) '
        "and (geo_match() or org_match()) "
        "and credibility() >= 0.4"
    )
    cp = compile_predicate(
        src, PredicateSurface.TARGET_SCOPE, ctx_contract=_SCOPE_RIG_CTX
    )
    assert isinstance(cp, CompiledPredicate)
    # Evaluating against a matching ctx should yield True.
    ctx = {
        "signal": {
            "entity_classes": ["generator"],
            "geo_match": True,
            "credibility": 0.8,
        }
    }
    assert cp.evaluate(ctx) is True


def test_spec_example_subscription_compiles():
    src = (
        'has_tag("energy") and len(scope_geo()) >= 1 '
        'and scope_geo()[0] in ["BR", "AR", "CL", "PE", "CO", "MX"]'
    )
    cp = compile_predicate(src, PredicateSurface.ANALYST_SUBSCRIPTION)
    ctx = {"target": {"tags": ["energy"], "scope_geo": ["BR"]}}
    assert cp.evaluate(ctx) is True
    ctx_no = {"target": {"tags": ["energy"], "scope_geo": ["US"]}}
    assert cp.evaluate(ctx_no) is False


def test_spec_example_cadence_trigger_compiles():
    # No production cadence-trigger evaluator exists, so the DEFAULT contract
    # refuses helper-bearing triggers (asserted below); the rig contract
    # keeps the example's runtime semantics covered.
    src = (
        'event_type() == "situation.escalation" '
        'and severity_at_least("high") '
        "and recent(1)"
    )
    with pytest.raises(PredicateCompilationError, match="cannot be fed"):
        compile_predicate(src, PredicateSurface.CADENCE_TRIGGER)
    cp = compile_predicate(
        src, PredicateSurface.CADENCE_TRIGGER, ctx_contract=_TRIG_RIG_CTX
    )
    now = datetime.now(tz=timezone.utc)
    ctx = {
        "event": {
            "type": "situation.escalation",
            "severity": "critical",
            "timestamp": now.isoformat(),
        }
    }
    assert cp.evaluate(ctx) is True


def test_spec_example_source_filter_compiles():
    # The only production source.filter compiler is the discovery relabel
    # engine, which binds bare label identifiers — no helper ctx. Under the
    # DEFAULT contract a helper-bearing source.filter predicate is refused
    # loudly (asserted below); the rig contract keeps runtime semantics
    # covered for when a real source-filter evaluator lands.
    src = 'not has_tag("press_release") and credibility() >= 0.5 and recent(30)'
    with pytest.raises(PredicateCompilationError, match="cannot be fed"):
        compile_predicate(src, PredicateSurface.SOURCE_FILTER)
    cp = compile_predicate(
        src,
        PredicateSurface.SOURCE_FILTER,
        ctx_contract=frozenset({
            "signal.tags", "signal.credibility", "signal.event_timestamp",
        }),
    )
    now = datetime.now(tz=timezone.utc)
    ctx = {
        "signal": {
            "tags": [],
            "credibility": 0.6,
            "event_timestamp": now.isoformat(),
        }
    }
    assert cp.evaluate(ctx) is True


# ---------------------------------------------------------------------------
# Sandbox — error paths
# ---------------------------------------------------------------------------


def test_syntax_error_surfaces_with_useful_message():
    with pytest.raises(PredicateCompilationError) as ei:
        compile_predicate("mentions(", PredicateSurface.TARGET_SCOPE)
    # Starlark span included in the chained message
    msg = str(ei.value)
    assert "parse failed" in msg or "Parse error" in msg


def test_banned_def_rejected_at_compile():
    with pytest.raises(PredicateCompilationError, match="def"):
        compile_predicate(
            'def f(): return True', PredicateSurface.TARGET_SCOPE
        )


def test_banned_load_rejected_at_compile():
    with pytest.raises(PredicateCompilationError, match="load"):
        compile_predicate(
            'load("foo.bzl", "bar")', PredicateSurface.TARGET_SCOPE
        )


def test_banned_lambda_rejected_at_compile():
    with pytest.raises(PredicateCompilationError, match="lambda"):
        compile_predicate(
            "(lambda x: x)(True)", PredicateSurface.TARGET_SCOPE
        )


def test_banned_while_rejected_at_compile():
    # Starlark already reserves the keyword; we re-emit a tagged error too.
    with pytest.raises(PredicateCompilationError):
        compile_predicate(
            "x = 1\nwhile True:\n  x = x + 1", PredicateSurface.TARGET_SCOPE
        )


def test_banned_import_rejected_at_compile():
    with pytest.raises(PredicateCompilationError):
        compile_predicate("import os", PredicateSurface.TARGET_SCOPE)


def test_io_attempt_fails():
    # Operators have no I/O surface — `open` isn't bound. Starlark resolves
    # unknown identifiers at eval; we expect either a compile-time helper
    # error (if the name regex flags it) or a runtime error.
    with pytest.raises((PredicateCompilationError, PredicateRuntimeError)):
        cp = compile_predicate('open("/etc/passwd", "r")', PredicateSurface.TARGET_SCOPE)
        cp.evaluate({})


# ---------------------------------------------------------------------------
# Sandbox — compile-time ITERATION / MATERIALIZATION bound (S-2)
# ---------------------------------------------------------------------------
#
# The grammar gate bans `for` *statements* but permits comprehensions, and the
# 4 KiB source-length cap bounds AST shape but not collection size. So a short
# source could materialize a huge collection off-thread before the post-hoc
# wall-clock budget can fire. `compiler._check_iteration_cost` bounds the
# literal size of `range(N)` and sequence-repeats at COMPILE time.


def test_comprehension_over_large_range_rejected_at_compile():
    """The review's proof vector: `[x for x in range(99999999)]` is
    structurally tiny but materializes 99M elements off-thread. It must be
    refused at compile, before it is ever parsed/evaluated."""
    with pytest.raises(PredicateCompilationError, match="iteration cap"):
        compile_predicate(
            "len([x for x in range(99999999)]) > 0",
            PredicateSurface.TARGET_SCOPE,
        )


def test_bare_large_range_rejected_at_compile():
    with pytest.raises(PredicateCompilationError, match="iteration cap"):
        compile_predicate("len(range(100001)) > 0", PredicateSurface.TARGET_SCOPE)


def test_large_sequence_repeat_rejected_at_compile():
    # `[0]*N` and the mirror `N*[0]` both materialize N elements.
    with pytest.raises(PredicateCompilationError, match="iteration cap"):
        compile_predicate("len([0]*10000000) > 0", PredicateSurface.TARGET_SCOPE)
    with pytest.raises(PredicateCompilationError, match="iteration cap"):
        compile_predicate("len(1000000*[0]) > 0", PredicateSurface.TARGET_SCOPE)


def test_large_string_repeat_rejected_at_compile():
    with pytest.raises(PredicateCompilationError, match="iteration cap"):
        compile_predicate('len("x"*9999999) > 0', PredicateSurface.TARGET_SCOPE)


def test_variable_range_rejected_at_compile():
    """A non-literal range bound can't be statically sized — refuse it (no
    legitimate predicate iterates a variable range)."""
    with pytest.raises(PredicateCompilationError, match="non-literal bound"):
        compile_predicate(
            "len([x for x in range(target_id())]) > 0",
            PredicateSurface.TARGET_SCOPE,
        )


def test_iteration_bound_is_at_compile_not_eval():
    """The DoS predicate must be rejected at COMPILE time — never reaching
    `evaluate()` — so the cost is never paid, on any thread. (Contrast the
    post-hoc wall-clock budget, which only observes cost after the fact.)"""
    import time as _time

    t0 = _time.perf_counter()
    with pytest.raises(PredicateCompilationError):
        compile_predicate(
            "len([x for x in range(99999999)]) > 0",
            PredicateSurface.TARGET_SCOPE,
        )
    elapsed_ms = (_time.perf_counter() - t0) * 1000.0
    # Compile-time rejection is a regex scan — orders of magnitude faster than
    # the ~450 ms the 99M-element comprehension takes to materialize.
    assert elapsed_ms < 50.0, f"compile rejection took {elapsed_ms:.1f} ms"


def test_legit_small_iteration_still_compiles():
    """The bound must NOT break legitimate predicates: comprehensions over
    ctx/literal lists, small literal ranges, and small repeats all compile."""
    # Comprehension over a small literal list (the `all(p.isdigit() for ...)`
    # shape) — well under the cap.
    cp = compile_predicate(
        'all([n > 0 for n in [1, 2, 3]])', PredicateSurface.TARGET_SCOPE
    )
    assert cp.evaluate({}) is True
    # Small literal range + small repeat — both under the cap, both compile.
    assert compile_predicate(
        "len(range(50)) == 50", PredicateSurface.TARGET_SCOPE
    ).evaluate({}) is True
    assert compile_predicate(
        "len([0]*10) == 10", PredicateSurface.TARGET_SCOPE
    ).evaluate({}) is True


def test_iteration_cap_boundary():
    """At-cap compiles; one over the cap is rejected."""
    from legba.data.predicates.compiler import _MAX_ITERATION_COUNT

    # Exactly at the cap is allowed.
    compile_predicate(
        f"len(range({_MAX_ITERATION_COUNT})) >= 0", PredicateSurface.TARGET_SCOPE
    )
    # One past the cap is refused.
    with pytest.raises(PredicateCompilationError, match="iteration cap"):
        compile_predicate(
            f"len(range({_MAX_ITERATION_COUNT + 1})) >= 0",
            PredicateSurface.TARGET_SCOPE,
        )


def test_surface_bound_helper_rejected_off_surface():
    # event_payload_get is only available on cadence.trigger; using it on
    # target.scope must raise at compile time.
    with pytest.raises(PredicateCompilationError, match="event_payload_get"):
        compile_predicate(
            'event_payload_get("severity") == "HIGH"',
            PredicateSurface.TARGET_SCOPE,
        )
    # scope_geo is only available on analyst.subscription
    with pytest.raises(PredicateCompilationError, match="scope_geo"):
        compile_predicate("len(scope_geo()) > 0", PredicateSurface.CADENCE_TRIGGER)


def test_empty_source_rejected():
    with pytest.raises(PredicateCompilationError):
        compile_predicate("", PredicateSurface.TARGET_SCOPE)
    with pytest.raises(PredicateCompilationError):
        compile_predicate("   \n  \n  ", PredicateSurface.TARGET_SCOPE)
    with pytest.raises(PredicateCompilationError):
        compile_predicate("# just a comment", PredicateSurface.TARGET_SCOPE)


def test_non_string_source_rejected():
    with pytest.raises(PredicateCompilationError):
        compile_predicate(None, PredicateSurface.TARGET_SCOPE)  # type: ignore[arg-type]


def test_unknown_surface_rejected():
    with pytest.raises(PredicateCompilationError):
        compile_predicate("True", "nope.surface")


# ---------------------------------------------------------------------------
# Budget enforcement
# ---------------------------------------------------------------------------


def test_wall_clock_budget_main_thread_via_sigalrm():
    """Direct ``run_with_budget`` call: SIGALRM fires mid-Python-sleep."""
    def slow():
        time.sleep(0.3)
        return True

    with pytest.raises(PredicateBudgetExceeded) as ei:
        run_with_budget(slow, budget=EvalBudget(wall_clock_ms=50.0))
    assert ei.value.limit == "wall_clock_ms"
    assert ei.value.budget_value == 50.0
    # observed_value populated post-hoc
    assert ei.value.observed_value is not None
    assert ei.value.observed_value >= 50.0


def test_wall_clock_budget_through_predicate_evaluate():
    """Patch a slow callable into the catalog and confirm evaluate() raises."""
    import legba.data.predicates.helpers as helpers_mod

    def _slow_factory(ctx):
        def slow(arg):
            time.sleep(0.3)
            return True
        return slow

    original = helpers_mod.HELPER_CATALOG["mentions"]
    helpers_mod.HELPER_CATALOG["mentions"] = (_slow_factory, ("target.scope",))
    try:
        predicate_cache_clear()
        cp = compile_predicate('mentions("x")', PredicateSurface.TARGET_SCOPE)
        with pytest.raises(PredicateBudgetExceeded) as ei:
            cp.evaluate({}, budget=EvalBudget(wall_clock_ms=30.0))
        assert ei.value.limit == "wall_clock_ms"
    finally:
        helpers_mod.HELPER_CATALOG["mentions"] = original
        predicate_cache_clear()


def test_default_wall_clock_budget_value():
    # Spec §3 mandates a 5 ms default; we surface it as a module-level const.
    assert DEFAULT_WALL_CLOCK_MS == 5.0


# ---------------------------------------------------------------------------
# LRU cache
# ---------------------------------------------------------------------------


def test_cache_hit_for_same_source_and_surface():
    predicate_cache_clear()
    src = 'mentions("generator") and credibility() >= 0.4'
    cp1 = compile_predicate(src, PredicateSurface.TARGET_SCOPE)
    cp2 = compile_predicate(src, PredicateSurface.TARGET_SCOPE)
    assert cp1 is cp2
    info = predicate_cache_info()
    assert info["size"] == 1
    assert info["hits"] == 1
    assert info["misses"] == 1


def test_cache_separates_by_surface():
    predicate_cache_clear()
    # Same source string, different surfaces → distinct entries.
    # `has_tag` is allowed on every surface, so the same source compiles
    # on both target.scope and analyst.subscription.
    src = 'has_tag("energy")'
    cp_scope = compile_predicate(src, PredicateSurface.TARGET_SCOPE)
    cp_sub = compile_predicate(src, PredicateSurface.ANALYST_SUBSCRIPTION)
    assert cp_scope is not cp_sub
    info = predicate_cache_info()
    assert info["size"] == 2
    assert info["hits"] == 0
    assert info["misses"] == 2


def test_cache_separates_by_source():
    predicate_cache_clear()
    compile_predicate('mentions("a")', PredicateSurface.TARGET_SCOPE)
    compile_predicate('mentions("b")', PredicateSurface.TARGET_SCOPE)
    info = predicate_cache_info()
    assert info["size"] == 2
    assert info["misses"] == 2


def test_cache_clear_resets_counters():
    compile_predicate('mentions("a")', PredicateSurface.TARGET_SCOPE)
    predicate_cache_clear()
    info = predicate_cache_info()
    assert info == {
        "size": 0, "maxsize": 10_000,
        "hits": 0, "misses": 0, "evictions": 0,
    }


# ---------------------------------------------------------------------------
# Surface-helpers metadata
# ---------------------------------------------------------------------------


def test_surface_helpers_partitioning():
    target_scope = surface_helpers(SURFACE_TARGET_SCOPE)
    cadence = surface_helpers(SURFACE_CADENCE_TRIGGER)
    sub = surface_helpers(SURFACE_ANALYST_SUBSCRIPTION)
    src_filter = surface_helpers(SURFACE_SOURCE_FILTER)

    # has_tag available everywhere
    assert "has_tag" in target_scope
    assert "has_tag" in cadence
    assert "has_tag" in sub
    assert "has_tag" in src_filter

    # event_payload_get only cadence
    assert "event_payload_get" in cadence
    assert "event_payload_get" not in target_scope
    assert "event_payload_get" not in sub

    # mentions only signal-scoped
    assert "mentions" in target_scope
    assert "mentions" in src_filter
    assert "mentions" not in cadence
    assert "mentions" not in sub

    # scope_geo only subscription
    assert "scope_geo" in sub
    assert "scope_geo" not in target_scope


# ---------------------------------------------------------------------------
# Schema validator integration
# ---------------------------------------------------------------------------


def test_target_scope_rejects_bad_predicate():
    # Source-first pivot: TargetScope is a discriminated union — construct the
    # concrete GeoScope variant (predicate validation lives on _ScopeBase).
    from legba.data.schemas.target import GeoScope

    with pytest.raises(Exception) as ei:
        GeoScope(
            geo=["BR"],
            languages=["en"],
            entity_classes=["entity"],
            predicate="mentions(",  # syntax error
        )
    assert "predicate" in str(ei.value).lower()
    assert "compile" in str(ei.value).lower()


def test_target_scope_accepts_good_predicate():
    from legba.data.schemas.target import GeoScope

    scope = GeoScope(
        geo=["BR"],
        languages=["en"],
        entity_classes=["generator"],
        predicate='mentions("generator") and geo_match()',
    )
    assert scope.predicate is not None


def test_target_scope_rejects_off_surface_helper():
    from legba.data.schemas.target import GeoScope

    with pytest.raises(Exception, match="event_payload_get"):
        GeoScope(
            geo=["BR"],
            languages=["en"],
            entity_classes=["entity"],
            predicate='event_payload_get("severity") == "HIGH"',
        )


def test_subscription_targets_rejects_bad_predicate():
    from legba.data.schemas.analyst import SubscriptionTargets

    with pytest.raises(Exception, match="predicate"):
        SubscriptionTargets(predicate="def f(): return True")


def test_subscription_targets_accepts_good_predicate():
    from legba.data.schemas.analyst import SubscriptionTargets

    s = SubscriptionTargets(
        predicate='has_tag("energy") and scope_geo()[0] == "BR"',
    )
    assert s.predicate is not None


def test_cadence_trigger_rejects_bad_predicate():
    from legba.data.schemas.analyst import CadenceBlock

    with pytest.raises(Exception, match="trigger"):
        CadenceBlock(trigger="while True: pass")


def test_cadence_trigger_refuses_helper_trigger_without_evaluator():
    """G4: no production evaluator feeds cadence.trigger ctx, so a
    helper-bearing trigger is refused at REGISTRATION (loud) instead of
    registering and silently never firing. When the trigger-event evaluator
    lands and declares its keys in SURFACE_CTX_CONTRACTS, this flips back."""
    from legba.data.schemas.analyst import CadenceBlock

    with pytest.raises(Exception, match="cannot be fed"):
        CadenceBlock(
            trigger='event_type() == "situation.escalation" and severity_at_least("high")',
        )


def test_cadence_trigger_accepts_helper_free_predicate():
    from legba.data.schemas.analyst import CadenceBlock

    c = CadenceBlock(trigger="True")
    assert c.trigger is not None


# ---------------------------------------------------------------------------
# Performance (loose)
# ---------------------------------------------------------------------------


def test_typical_predicate_under_500_microseconds():
    """Spec target is <10 µs; SIGALRM overhead pushes us higher in CI.

    The 500 µs ceiling is intentionally loose — it catches catastrophic
    regressions (e.g., recompiling on every call) without flaking under
    timing jitter. The microbench at module-load time prints the actual
    figure; tighten this threshold once we have CI baselines.

    Isolation note (B4): in the single-process full-suite run this test
    shares the box with everything else, so a GC pause or scheduler
    preemption inside one timing window can spike a single average past
    the ceiling even though per-eval cost is unchanged. We therefore take
    the BEST (minimum) of several windows rather than a single average —
    the standard anti-jitter microbench technique. This does NOT weaken
    the assertion: a real regression (recompile-per-call is ~hundreds of
    µs on every iteration) blows past 500 µs on every window, so the
    minimum still catches it; only transient jitter is filtered out.
    """
    src = 'mentions("generator") and (geo_match() or org_match())'
    cp = compile_predicate(
        src, PredicateSurface.TARGET_SCOPE, ctx_contract=_SCOPE_RIG_CTX
    )
    ctx = {
        "signal": {
            "entity_classes": ["generator"],
            "geo_match": True,
        }
    }
    # Warmup (JIT-like — the Starlark eval has internal caches; also pays
    # the one-time Rust evaluator / SIGALRM-setitimer init so the measured
    # windows reflect steady-state, not cold-compile, cost).
    for _ in range(50):
        cp.evaluate(ctx)

    n = 200
    windows = 5
    best_us = float("inf")
    for _ in range(windows):
        t0 = time.perf_counter()
        for _ in range(n):
            cp.evaluate(ctx)
        best_us = min(best_us, (time.perf_counter() - t0) / n * 1e6)
    assert best_us < 500.0, (
        f"best per-eval window was {best_us:.1f} µs (>= 500 µs ceiling)"
    )

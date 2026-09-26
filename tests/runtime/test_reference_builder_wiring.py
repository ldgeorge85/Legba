# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R2 — the reference builder's deps wiring, and the OPTION PATH to its handler.

Two properties, and the second one is the reason this file is not just a
deps-wiring test.

**THE WIRING.** Two legs under this handler's OWN keys: the core-plane model and
the ``web_access`` pack binding. Both degrade to absent, and the handler records
a named status rather than raising — with one deliberate asymmetry: no model is
a degradation, no web binding is a REFUSAL, because a reference built without
the open web is not independent of the reads it grades.

**THE OPTION PATH, which is where G1 drew blood.** G1's on-demand run stamped
``as_of=now`` although the method body carried ``as_of=19:30Z`` — the defect
recorded as D1. The body's value never reached the handler. A concurrent lane is
fixing how method-body options reach deterministic sub-handlers; this file
asserts the CONTRACT that fix must satisfy, from both ends, so it holds whichever
lane lands first:

  * every key ``scripts/reference_build_now.py`` puts in the body is a DECLARED
    option in the X-1 catalog (so the runtime's option merge accepts rather than
    rejects it) — which is a test of the script against the catalog, and would
    have caught a typo in either;
  * the descriptor's own options survive the REAL merge
    (``dapr_actors._merge_descriptor_options``, the function the runtime calls,
    not a reimplementation of it);
  * a value already in the run options — which is what a forced method body
    becomes — WINS over the descriptor's default, because the merge is
    ``setdefault``. That is the precise property D1 violated;
  * ``as_of`` is the same key name the correctness grader reads, so an operator
    pinning a fortnight passes ONE value to both jobs.
"""

from __future__ import annotations

import argparse
import ast
from pathlib import Path
from typing import Any

import pytest
import yaml

from legba.data.analysts.deterministic_handlers.reference_builder import (
    DEFAULT_CADENCE_DAYS,
    DEFAULT_MAX_TARGETS_PER_RUN,
    DEFAULT_RETRY_BACKOFF_HOURS,
    DEFAULT_WINDOW_DAYS,
    LLM_DEPS_EXTRA_KEY,
    WEB_BINDING_DEPS_EXTRA_KEY,
)
from legba.data.analysts.handler_options import (
    HANDLER_OPTIONS,
    known_option_names,
)
from legba.data.schemas.analyst import (
    AnalystDescriptor,
    AnalystIdentity,
    AnalystKind,
    CadenceBlock,
    MappingBlock,
    MethodBlock,
    SubscriptionBlock,
    TypeSignature,
)
from legba.data.schemas.lifecycle import LifecycleState
from legba.runtime.analyst_deps_kinds import (
    wire_reference_builder_kind_deps,
)
from legba.runtime.deps import StandardDeps

_CORE = "llm.primary.openai_compat"
_REPO = Path(__file__).resolve().parents[2]
_DESCRIPTOR_PATH = _REPO / "descriptors" / "analyst_reference_builder.yaml"
_SCRIPT_PATH = _REPO / "scripts" / "reference_build_now.py"


class _Stub:
    def __init__(self, component_id: str) -> None:
        self.component_id = component_id


async def _stub_secrets(_secret_id: str) -> bytes:
    return b"stub-secret-bytes"


async def _resolve_llm() -> Any:
    return _Stub(_CORE)


def _descriptor(*, with_llm: bool = True, packs=None) -> AnalystDescriptor:
    # MethodBlock.llm is a dict, never None — "no llm block" is the EMPTY one,
    # which is how a descriptor declines a model leg.
    llm: dict[str, Any] = {}
    if with_llm:
        llm = {
            "primary": {"factory_kind": "stack_ref", "raw": _CORE,
                        "expected_family": "llm_provider"},
        }
    return AnalystDescriptor(
        identity=AnalystIdentity(
            id="reference_builder",
            name="Reference builder",
            schema_uri="legba/analyst/1.0.0",
            version="0" * 16,
            kind=AnalystKind.DETERMINISTIC,
            type_signature=TypeSignature(
                input_type="legba.x.In", output_type="legba.x.Out",
            ),
            state=LifecycleState.ACTIVE,
            owner="test",
        ),
        subscription=SubscriptionBlock(),
        mapping=MappingBlock(),
        method=MethodBlock(
            kind="deterministic",
            impl="legba.data.analysts.deterministic:run_method",
            sub_handler="reference_builder",
            llm=llm,
        ),
        cadence=CadenceBlock(fallback_schedule="0 0 1 1 *"),
        action_packs=packs if packs is not None else [],
    )


def _deps() -> StandardDeps:
    return StandardDeps(
        pg_pool=object(),  # type: ignore[arg-type]
        nats_publish=None,
        secrets_resolve=_stub_secrets,
    )


async def _wire(descriptor, *, component_id=_CORE, registry_client=None):
    async def _wire_det(desc, deps, resolve, *, component_id, extra_key,
                        purpose):
        from dataclasses import replace

        assert purpose == "reference_builder"
        return replace(
            deps, extras={**dict(deps.extras), extra_key: _Stub(component_id)}
        )

    return await wire_reference_builder_kind_deps(
        descriptor, _deps(),
        registry_client=registry_client,
        resolve_llm=_resolve_llm,
        component_id=component_id,
        wire_deterministic_llm=_wire_det,
    )


# ---------------------------------------------------------------------------
# the wiring
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_core_plane_wires_under_this_handlers_own_key():
    deps = await _wire(_descriptor())
    assert deps.extras[LLM_DEPS_EXTRA_KEY].component_id == _CORE


@pytest.mark.asyncio
async def test_the_two_extras_keys_are_this_handlers_alone():
    """Distinct by construction. Handing another handler's binding to this one —
    or this one's to another — is the class of bug that makes an 'independent'
    reference quietly dependent."""
    from legba.data.analysts.deterministic_handlers import desk_reference as DR
    from legba.data.analysts.deterministic_handlers import correctness_grader as CG

    mine = {LLM_DEPS_EXTRA_KEY, WEB_BINDING_DEPS_EXTRA_KEY}
    theirs = {
        DR.LLM_DEPS_EXTRA_KEY, DR.WEB_BINDING_DEPS_EXTRA_KEY,
        CG.F0_DEPS_EXTRA_KEY, CG.F2_DEPS_EXTRA_KEY, CG.F3_DEPS_EXTRA_KEY,
    }
    assert mine & theirs == set()


@pytest.mark.asyncio
async def test_no_llm_block_degrades_to_no_model_rather_than_raising():
    deps = await _wire(_descriptor(with_llm=False), component_id=None)
    assert LLM_DEPS_EXTRA_KEY not in deps.extras


@pytest.mark.asyncio
async def test_no_registry_client_leaves_the_web_binding_absent():
    deps = await _wire(_descriptor(), registry_client=None)
    assert WEB_BINDING_DEPS_EXTRA_KEY not in deps.extras


@pytest.mark.asyncio
async def test_a_descriptor_that_does_not_grant_web_access_gets_no_binding():
    """The grant leg. Not granting the pack is a valid configuration, and it
    yields nothing — silently and correctly at THIS layer; the handler is where
    the absence becomes a loud `no_web` refusal."""
    deps = await _wire(_descriptor(packs=[]), registry_client=object())
    assert WEB_BINDING_DEPS_EXTRA_KEY not in deps.extras


# ---------------------------------------------------------------------------
# the descriptor
# ---------------------------------------------------------------------------


def _descriptor_body() -> dict[str, Any]:
    return yaml.safe_load(_DESCRIPTOR_PATH.read_text())


def test_the_descriptor_ships_draft_and_binds_this_sub_handler():
    body = _descriptor_body()
    assert body["identity"]["id"] == "reference_builder"
    assert body["identity"]["kind"] == "deterministic"
    assert body["identity"]["state"] == "draft"
    assert body["method"]["sub_handler"] == "reference_builder"


def test_the_descriptor_grants_web_access_and_nothing_else():
    body = _descriptor_body()
    assert [p["pack_id"] for p in body["action_packs"]] == ["web_access"]


def test_the_descriptor_binds_the_core_plane_and_no_paid_family():
    """There is no paid route in this analyst's path to gate, which is why it
    carries no spend-ceiling env. That claim has to be true in the file."""
    llm = _descriptor_body()["method"]["llm"]
    assert set(llm) == {"primary"}
    assert llm["primary"]["raw"] == _CORE


def test_every_descriptor_option_is_a_declared_knob():
    """An option the catalog does not know is DROPPED by the real merge with a
    warning, which is how a descriptor comes to document a volume it is not
    running."""
    declared = set(known_option_names("reference_builder"))
    assert declared, "reference_builder is not in the X-1 catalog at all"
    for key in _descriptor_body()["method"]["options"]:
        assert key in declared, f"{key} is not a declared reference_builder knob"


def test_the_shipped_descriptor_states_the_in_source_defaults():
    """The file documents the shipped volume rather than implying it. If a
    default moves in code and not here, the descriptor starts lying."""
    options = _descriptor_body()["method"]["options"]
    assert options["max_targets_per_run"] == DEFAULT_MAX_TARGETS_PER_RUN
    assert options["window_days"] == DEFAULT_WINDOW_DAYS
    assert options["cadence_days"] == DEFAULT_CADENCE_DAYS
    assert options["cadence_days"] <= options["window_days"], (
        "cadence must not exceed the window, or an as-of stamp can fall in a "
        "GAP between consecutive references. Overlap (cadence < window) is "
        "deliberate: the grader resolves the newest window_end, so a stamp in "
        "two windows reads the fresher reference; with grace 7 and cadence 7 "
        "no read ever meets a stale reference (ops 2026-09-17)."
    )


def test_no_fence_is_settable_from_a_descriptor():
    """The fetched-URL manifest, the date gate, the tier allowlist and the span
    check are not knobs. A descriptor PUT is an API call any holder of the
    registry token can make; lowering the bar a reference must clear is not that
    kind of decision."""
    declared = set(known_option_names("reference_builder"))
    forbidden = {
        "date_gate", "skip_date_gate", "allow_undated", "tier_allowlist",
        "allowlist_hosts", "skip_span_verification", "span_check",
        "require_fetched_url", "block_after",
    }
    assert declared & forbidden == set()


# ---------------------------------------------------------------------------
# THE OPTION PATH — the property G1's D1 defect violated
# ---------------------------------------------------------------------------


def _load_script():
    """Import ``reference_build_now.py`` as a module, without running main()."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_reference_build_now", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


def _script_body_keys() -> set[str]:
    """Every option key ``reference_build_now.py`` writes into ``body['options']``.

    Read out of the SOURCE by AST rather than by running it, so this test needs
    no Dapr sidecar and still tracks the real script.
    """
    tree = ast.parse(_SCRIPT_PATH.read_text())
    keys: set[str] = set()
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == "options"
            and isinstance(node.slice, ast.Constant)
            and isinstance(node.slice.value, str)
        ):
            keys.add(node.slice.value)
    return keys


def test_the_forced_body_nests_every_knob_under_options():
    """THE ENVELOPE, and it is not cosmetic. ``dapr_actors`` merges ONE channel
    into a sub-handler's option mapping: ``body['options']``. A knob at the top
    level of the body is ignored silently — no error, no log line — which is
    exactly how G1's D1 defect hid, stamping ``as_of=now`` while the body
    carried a past stamp. The G1-fix lane settled this shape and this pins that
    the reference builder's own door speaks it."""
    module = _load_script()
    args = argparse.Namespace(
        target=["country_watch_il"],
        as_of="2026-09-16T19:30:00+00:00",
        dry_run=True, tool_call_cap=40, max_targets=2,
    )
    body = module.build_body(args)

    assert set(body) == {"trigger_kind", "options"}, (
        "only the actor's own contract may sit at the top level; everything "
        "else there is silently discarded"
    )
    assert body["trigger_kind"] == "method"
    options = body["options"]
    assert options["reference_targets"] == ["country_watch_il"]
    assert options["as_of"] == "2026-09-16T19:30:00+00:00"
    assert options["reference_dry_run"] is True
    assert options["tool_call_cap"] == 40
    assert options["max_targets_per_run"] == 2


def test_a_body_with_no_knobs_carries_no_options_object_at_all():
    """The plain forced run — what the cadence would do — must not send an empty
    ``options``, so a bare force is byte-identical to the scheduled path."""
    module = _load_script()
    args = argparse.Namespace(
        target=None, as_of=None, dry_run=False,
        tool_call_cap=None, max_targets=None,
    )
    assert module.build_body(args) == {"trigger_kind": "method"}


def test_every_key_the_on_demand_script_sends_is_a_declared_option():
    """The script against the catalog. A typo in either end is REJECTED at the
    runtime boundary with a warning and the handler default stands — which is
    exactly the silent no-op G1's D1 was."""
    declared = set(known_option_names("reference_builder"))
    sent = _script_body_keys() - {"trigger_kind"}  # the actor's own contract
    assert sent, "the script sends no options at all — did the body shape change?"
    assert sent <= declared, f"undeclared keys in the forced body: {sent - declared}"


def test_the_as_of_key_is_shared_with_the_correctness_grader():
    """One value pins both jobs to one fortnight. Two names for one instant is
    how a reference and the grading of it drift apart."""
    assert "as_of" in known_option_names("reference_builder")
    assert "as_of" in known_option_names("correctness_grader")


def test_the_target_key_is_distinct_from_the_graders_so_neither_can_be_confused():
    """Shared CONCEPT, separate key. ``grader_targets`` bounds what is graded;
    ``reference_targets`` bounds what is built, and a run that passed one to the
    other would silently do nothing."""
    assert "reference_targets" in known_option_names("reference_builder")
    assert "grader_targets" not in known_option_names("reference_builder")


def test_the_descriptors_options_survive_the_REAL_runtime_merge():
    from legba.runtime.dapr_actors import _merge_descriptor_options

    body = _descriptor_body()
    descriptor = _descriptor()
    descriptor.method.options = dict(body["method"]["options"])

    options: dict[str, Any] = {"sub_handler": "reference_builder"}
    receipt = _merge_descriptor_options(options, descriptor, actor_id="test")

    assert receipt is not None
    assert receipt["status"] == "applied", receipt["rejected"]
    assert receipt["rejected"] == []
    assert options["max_targets_per_run"] == DEFAULT_MAX_TARGETS_PER_RUN
    assert options["tool_call_cap"] == body["method"]["options"]["tool_call_cap"]


def test_a_forced_method_body_WINS_over_the_descriptor_default():
    """THE D1 CONTRACT. The merge is ``setdefault``, so a value the runtime has
    already placed — which is what a forced method body becomes — must survive.
    G1's on-demand run stamped ``as_of=now`` although the body carried a past
    stamp; this is the assertion that failure would not have passed."""
    from legba.runtime.dapr_actors import _merge_descriptor_options

    body = _descriptor_body()
    descriptor = _descriptor()
    descriptor.method.options = dict(body["method"]["options"])

    # exactly what scripts/reference_build_now.py PUTs
    options: dict[str, Any] = {
        "sub_handler": "reference_builder",
        "reference_targets": ["country_watch_il"],
        "as_of": "2026-09-16T19:30:00+00:00",
        "reference_dry_run": True,
    }
    _merge_descriptor_options(options, descriptor, actor_id="test")

    assert options["reference_targets"] == ["country_watch_il"]
    assert options["as_of"] == "2026-09-16T19:30:00+00:00"
    assert options["reference_dry_run"] is True
    # and the descriptor still fills in everything the body did not name
    assert options["cadence_days"] == body["method"]["options"]["cadence_days"]


def test_the_handler_reads_the_merged_values_back_out():
    """Closing the loop: the keys the merge produces are the literals the
    handler reads. The X-1 reachability sweep proves a declared knob is real by
    finding its literal read in the module; this proves the ROUND TRIP."""
    from legba.data.analysts.deterministic_handlers import reference_builder as RB

    merged = {
        "as_of": "2026-09-16T19:30:00+00:00",
        "reference_targets": ["country_watch_il"],
    }
    assert RB.resolve_t0(merged).isoformat() == "2026-09-16T19:30:00+00:00"

    # The handler MODULE FAMILY, not just the entry module: R2-FIX(2) moved the
    # roster, the attempt ledger and the ordering into ``_reference_roster`` to
    # keep the entry module under its size gate, and a knob read there is just
    # as read. Scanning only ``reference_builder.py`` would have passed this
    # test on the accident that ``retry_backoff_hours`` is also a receipt key.
    handlers = (
        _REPO / "src" / "legba" / "data" / "analysts" / "deterministic_handlers"
    )
    source = "\n".join(
        path.read_text()
        for path in [handlers / "reference_builder.py",
                     *sorted(handlers.glob("_reference_*.py"))]
    )
    for key in known_option_names("reference_builder"):
        if key == "t0":
            continue
        assert f'"{key}"' in source, f"{key} is declared but never read"


def test_the_on_demand_door_reaches_the_D6_loop_through_the_REAL_merge():
    """R2-FIX-3 on the binding path, end to end and with no new knob.

    ``build_body`` -> the runtime's own ``_merge_descriptor_options`` -> the
    literals ``build_reference`` reads -> ``run_loop``'s D6 signature. The
    yield fixes ride the SHIPPED options, so an operator forcing the 06:43Z
    build again today gets the manifest commit turn without changing anything
    on the descriptor — which is why this track needs no registry-first roll."""
    import inspect

    from legba.data.analysts.deterministic_handlers import reference_builder as RB
    from legba.data.analysts.deterministic_handlers import _reference_loop as LOOP
    from legba.runtime.dapr_actors import _merge_descriptor_options

    module = _load_script()
    args = argparse.Namespace(
        target=["country_g20_au"], as_of="2026-09-17T06:43:01+00:00",
        dry_run=True, tool_call_cap=None, max_targets=None,
    )
    body = module.build_body(args)

    descriptor = _descriptor()
    descriptor.method.options = dict(_descriptor_body()["method"]["options"])
    options: dict[str, Any] = {"sub_handler": "reference_builder"}
    options.update(body["options"])
    receipt = _merge_descriptor_options(options, descriptor, actor_id="test")

    assert receipt["rejected"] == [], "D6 added no undeclared knob"
    assert options["reference_targets"] == ["country_g20_au"]
    assert options["reference_dry_run"] is True
    assert RB.resolve_t0(options).isoformat() == "2026-09-17T06:43:01+00:00"

    # the window the merged options imply is the one the loop is handed, and
    # the loop takes it — that parameter is what the manifest's IN WINDOW
    # verdict is computed against.
    signature = inspect.signature(LOOP.run_loop).parameters
    assert "window_start" in signature and "window_end" in signature
    assert "window_start=window_start.date()" in (
        _REPO / "src" / "legba" / "data" / "analysts"
        / "deterministic_handlers" / "reference_builder.py"
    ).read_text()


def test_the_new_outcome_is_a_named_status_the_scheduler_knows():
    """``empty_commit_with_material`` must yield the hour like any other failed
    build, or a country the model will not write up is retried every tick."""
    from legba.data.analysts.deterministic_handlers import _reference_roster as R
    from legba.data.analysts.deterministic_handlers._reference_manifest import (
        EMPTY_COMMIT_WITH_MATERIAL,
    )

    assert EMPTY_COMMIT_WITH_MATERIAL in R.FAILED_STATUSES
    assert EMPTY_COMMIT_WITH_MATERIAL in R.ATTEMPTED_STATUSES


def test_the_option_catalog_entry_is_registered_under_the_sub_handler_name():
    assert "reference_builder" in HANDLER_OPTIONS


# ---------------------------------------------------------------------------
# D4 — THE BUILD MUST FIT THE PLANE THAT RUNS IT (ops 2026-09-17)
#
# READ THIS BEFORE CHANGING A NUMBER BELOW, because the incident report and the
# code disagree about one thing and the disagreement is load-bearing.
#
# The 00:43Z build produced TWO warnings reading
# `actor_turn.budget_exceeded op=reconcile.activate ... timeout=20s`. Those are
# NOT a budget on the run. They are the RECONCILER's per-heal deadline
# (`actor_turn.heal_timeout_seconds()`, 20 s) expiring because the AnalystActor's
# turn was held by the 927.9 s build and Dapr actors are turn-based with
# reentrancy disabled, so the queued ENSURE_ACTIVE could not start. Their
# consequence is a SKIPPED heal, retried next resync and counted by
# `HealBreaker` — not a cooldown, not a failed run. NOTHING in the runtime
# bounds an analyst `run` turn: there is no budget for a build to be "under",
# and no useful build could ever be under 20 s anyway.
#
# What actually took the lane down for two hours was the PER-ANALYST DAY
# BUCKET. 4,510,885 tokens against this descriptor's own
# `budget_tokens_per_day: 4000000` made the 01:43 tick's precall check return
# `exhausted`; the default `pause_until_next_window` strategy stamped
# `cooldown_until = now + BudgetRetryPolicy.cooldown_seconds` (3600 s) with no
# log line at all, the 02:43 tick lost the race by 0.99 s and no-op'd
# `reason=cooldown scope=global`, and the watchdog called it a stall.
#
# So the caps are asserted against the bounds that EXIST and against the one
# that actually fired. A cap that fits those fits the actor plane; a cap
# "under the actor-turn budget" would be a cap under 20 s, which is not a build.
# ---------------------------------------------------------------------------


def _cron_period_seconds(expr: str) -> float:
    """Seconds between consecutive fires of a 5-field cron, the same way the
    watchdog's own cadence check derives it."""
    from croniter import croniter
    from datetime import datetime, timezone

    base = datetime(2026, 9, 17, 0, 0, tzinfo=timezone.utc)
    it = croniter(expr, base)
    first = it.get_next(datetime)
    return (it.get_next(datetime) - first).total_seconds()


def test_the_build_cap_default_fits_the_actor_plane():
    from legba.data.analysts.deterministic_handlers import reference_builder as RB
    from legba.data.schemas.analyst import BudgetRetryPolicy
    from legba.runtime import actor_turn

    body = _descriptor_body()
    options = body["method"]["options"]
    cadence = body["cadence"]

    # the descriptor and the code agree, or the descriptor documents a volume
    # nobody is running
    assert options["build_max_seconds"] == RB.DEFAULT_BUILD_MAX_SECONDS
    assert options["build_max_tokens"] == RB.DEFAULT_BUILD_MAX_TOKENS

    cap_s = float(RB.DEFAULT_BUILD_MAX_SECONDS)

    # 1. BELOW THE TICK PERIOD. A build that outlives its own cadence is a
    #    build whose next tick queues behind it on a turn-based actor — which
    #    is what held this actor for 15.5 minutes.
    period_s = _cron_period_seconds(cadence["fallback_schedule"])
    assert cap_s < period_s, (
        f"a build may not outlive its own {period_s:.0f}s tick period"
    )

    # 2. BELOW THE DESCRIPTOR'S OWN CADENCE COOLDOWN, so a build can never
    #    still be running when its cooldown lapses.
    assert cap_s < float(cadence["cooldown_seconds"])

    if RB.DEFAULT_BUDGET_TOKENS_PER_DAY == 0:
        return  # unbudgeted deployment (operator, 2026-09-17): no budget cooldown exists to fit under
    # 3. BELOW THE BUDGET COOLDOWN IT WOULD TRIGGER. A build longer than the
    #    pause it can cause cannot be reasoned about at all.
    assert cap_s < float(BudgetRetryPolicy().cooldown_seconds)

    # 4. WELL ABOVE THE ONE GOOD LIVE BUILD (Ukraine, 294 s / 5 developments)
    #    and the two R1 runs (410 s, 498 s). A cap that cuts a converging build
    #    is not a fix, it is the same failure with a shorter receipt.
    assert cap_s >= 420.0

    # 5. THE TOKEN CEILING CANNOT EXHAUST THE DAY BUCKET, and leaves room for
    #    more than one build a day — the roster is 32 targets on a 7-day
    #    cadence, so a day that holds one build never catches up.
    day_bucket = int(body["method"]["budget_tokens_per_day"])
    assert RB.DEFAULT_BUILD_MAX_TOKENS < day_bucket
    assert RB.DEFAULT_BUILD_MAX_TOKENS * 3 <= day_bucket, (
        "at most two builds a day fits under the bucket — the 32-target "
        "roster on a 7-day cadence needs ~4.6"
    )
    # and the run that caused the incident would NOT fit under it
    assert 4_510_885 > RB.DEFAULT_BUILD_MAX_TOKENS

    # 6. THE RECONCILER'S DEADLINES ARE NOT A RUN BUDGET, pinned so the next
    #    reader does not re-derive the wrong conclusion from the warning text.
    #    Both bound ops inside an ACTIVATE turn / a reconcile heal; neither is
    #    reachable from a run, and no build could be under either.
    assert actor_turn.heal_timeout_seconds() < cap_s
    assert actor_turn.turn_op_timeout_seconds() < cap_s


def test_the_two_walls_are_declared_knobs_and_the_descriptor_states_them():
    """Both are settable so an operator can SHRINK a build on a loaded box, and
    both are bounded so nobody can widen one past what the plane carries."""
    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    declared = set(known_option_names("reference_builder"))
    assert {"build_max_seconds", "build_max_tokens"} <= declared

    specs = {s.name: s for s in HANDLER_OPTIONS["reference_builder"]}
    # a wall-clock cap past an hour is not a wall
    assert specs["build_max_seconds"].maximum == 3600
    assert specs["build_max_seconds"].minimum == 1
    assert specs["build_max_tokens"].minimum == 1

    options = _descriptor_body()["method"]["options"]
    assert "build_max_seconds" in options and "build_max_tokens" in options


# ---------------------------------------------------------------------------
# R2-FIX(2) — THE RETRY FENCE (ops 2026-09-17)
#
# The scheduler ordered the roster by each target's newest SUCCESSFUL reference.
# A failed build writes no ``unit_references`` row, so a target that cannot be
# built never moved: country_g20_ar (3 of 16 pages usable to this lane) took two
# consecutive builds and would have taken every hourly tick after them, while 29
# other due targets were never built at all. The order is now by last ATTEMPT,
# read off the lane's own receipts, and a failure buys the target a rest.
# ---------------------------------------------------------------------------


def test_the_retry_backoff_is_a_declared_knob_the_descriptor_states():
    from legba.data.analysts.handler_options import HANDLER_OPTIONS

    assert "retry_backoff_hours" in set(known_option_names("reference_builder"))
    spec = {s.name: s for s in HANDLER_OPTIONS["reference_builder"]}[
        "retry_backoff_hours"
    ]
    # 0 is LEGAL and is the disable — a fence nobody can turn off is a fence
    # nobody can measure (the same rule the domain blocklist honours).
    assert spec.minimum == 0
    assert spec.maximum is not None and spec.maximum <= 8760
    options = _descriptor_body()["method"]["options"]
    assert options["retry_backoff_hours"] == DEFAULT_RETRY_BACKOFF_HOURS


def test_the_backoff_env_WINS_over_the_merged_descriptor_value(monkeypatch):
    """THE INVERSE precedence, asserted through the REAL merge.

    ``build_max_seconds`` and ``build_max_tokens`` are volumes an operator sizes
    once, so the descriptor wins over their envs. This one is an INCIDENT lever
    — "the lane is stuck on one country, let it move on" — at a moment when a
    descriptor PUT plus a re-register plus a fresh actor record is exactly the
    ceremony that cannot be afforded. So the env wins, and the receipt names
    which source it read.
    """
    from legba.data.analysts.deterministic_handlers import (
        _reference_roster as ROSTER,
    )
    from legba.runtime.dapr_actors import _merge_descriptor_options

    descriptor = _descriptor()
    descriptor.method.options = dict(_descriptor_body()["method"]["options"])
    options: dict[str, Any] = {"sub_handler": "reference_builder"}
    _merge_descriptor_options(options, descriptor, actor_id="test")
    assert options["retry_backoff_hours"] == DEFAULT_RETRY_BACKOFF_HOURS

    monkeypatch.delenv(ROSTER.RETRY_BACKOFF_ENV, raising=False)
    assert ROSTER.retry_backoff_hours(options["retry_backoff_hours"]) == (
        float(DEFAULT_RETRY_BACKOFF_HOURS), "retry_backoff_hours"
    )
    monkeypatch.setenv(ROSTER.RETRY_BACKOFF_ENV, "6")
    assert ROSTER.retry_backoff_hours(options["retry_backoff_hours"]) == (
        6.0, ROSTER.RETRY_BACKOFF_ENV
    )


def test_the_backoff_leaves_room_for_the_roster_to_cycle():
    """The fence must not become the schedule. A 32-target roster on an hourly
    tick needs a full pass in well under the cadence, so a target that failed
    can rest a day and still be retried inside its own cadence window."""
    body = _descriptor_body()["method"]["options"]
    hours_per_cycle = 32 / int(body["max_targets_per_run"])
    assert body["retry_backoff_hours"] >= hours_per_cycle * 0.5, (
        "a backoff shorter than half a roster pass re-offers the failing "
        "target before the queue has moved"
    )
    assert body["retry_backoff_hours"] < body["cadence_days"] * 24, (
        "a backoff longer than the cadence means a failing target is skipped "
        "for a whole cycle rather than retried inside it"
    )


# ---------------------------------------------------------------------------
# R2-FIX(1) — THE BUILDER'S OWN WEB BUCKET (ops 2026-09-17)
#
# The ``web_access`` pack declares ``budget_account: web_access`` +
# ``max_invocations_per_hour: 120``, and ``Agency.run_pack_tool`` resolves the
# account as ``res.governor.budget_account or call.budget_account`` — the PACK's
# account beats the per-analyst one the binding passes, so every holder of the
# grant draws on ONE hourly bucket. Measured live over one two-hour window:
# ``standing_auditor`` 120 invocations, i.e. the entire cap. The 00:43Z builder
# tick then found the hour gone and 63 of its 67 tool calls came back "not
# admitted by the pack".
# ---------------------------------------------------------------------------


_PACK_PATH = _REPO / "descriptors" / "action_pack_web_access.yaml"


def _web_access_pack():
    """The SHIPPED pack, with the version the registry stamps at register time."""
    from legba.data.schemas.action_pack import ActionPack

    body = yaml.safe_load(_PACK_PATH.read_text())
    body["identity"]["version"] = "a" * 16
    return ActionPack.model_validate(body, strict=False)


def _resolve_for(descriptor_body: dict[str, Any]):
    """Run the REAL three-way resolution for one analyst's shipped grant."""
    from legba.data.analysts.agency.resolution import TargetScopeView, resolve_pack
    from legba.data.schemas.action_pack import ActionPackRef
    from legba.data.schemas.analyst import AnalystDescriptor

    descriptor = AnalystDescriptor.model_validate(descriptor_body)
    return resolve_pack(
        pack=_web_access_pack(),
        # exactly the shape ``external_audit_binding`` passes
        analyst_grants=[r.model_dump() for r in descriptor.action_packs],
        target_allows=[ActionPackRef(pack_id="web_access")],
        scope=TargetScopeView(target_id="country_g20_ar"),
    )


def test_the_builders_grant_resolves_to_its_OWN_budget_account():
    """THE CARVE-OUT, through the real resolver. ``governor_override`` is
    tightening-only, and ``_merge_governor`` lets ``budget_account`` follow the
    override because "re-targeting the ledger account is not a loosening" — so
    this buys a PRIVATE bucket and a cap still under the pack's own. It cannot
    be used to widen anything, which is why it is the right mechanism."""
    from legba.data.analysts.deterministic_handlers import reference_builder as RB

    res = _resolve_for(_descriptor_body())
    assert res.effective, res.reason
    assert res.governor.budget_account == RB.WEB_BUDGET_ACCOUNT
    assert res.governor.budget_account != "web_access", (
        "the builder is still sharing the auditor's bucket"
    )
    assert res.governor.max_invocations_per_hour == RB.WEB_INVOCATIONS_PER_HOUR
    # the pack's OWN caps that were not overridden still bind
    assert res.governor.api_rate_per_minute == _web_access_pack(
    ).governor.api_rate_per_minute


def test_the_override_is_a_TIGHTENING_and_never_raises_the_shared_bucket():
    """Two halves of one promise. The builder's cap must be at or under the
    pack's (the merge would silently clamp it anyway, so a descriptor asking for
    more would be a lie in the file); and the shared bucket every other analyst
    draws on must be untouched — raising it would just hand the auditor a
    bigger hour."""
    pack = _web_access_pack()
    override = _descriptor_body()["action_packs"][0]["governor_override"]

    assert override["max_invocations_per_hour"] <= pack.governor.max_invocations_per_hour
    assert pack.governor.budget_account == "web_access"
    assert pack.governor.max_invocations_per_hour == 1_000_000, (
        "the shared bucket moved — this fix carves out, it does not raise"
    )

    # and the sibling holders of the grant are still on it, unchanged
    for sibling in ("analyst_standing_auditor.yaml", "analyst_desk_reference.yaml"):
        body = yaml.safe_load((_REPO / "descriptors" / sibling).read_text())
        res = _resolve_for(body)
        assert res.governor.budget_account == "web_access", sibling
        assert res.governor.max_invocations_per_hour == 1_000_000, sibling


def test_one_whole_build_fits_the_builders_own_hour():
    """The cap is sized to the thing that runs under it: ``tool_call_cap``
    admitted calls, once an hour. Refused, blocklisted and cached calls never
    reach the binding, so the tool cap is a real ceiling rather than an
    estimate — but the headroom is what absorbs a retry."""
    from legba.data.analysts.deterministic_handlers import reference_builder as RB

    tool_call_cap = _descriptor_body()["method"]["options"]["tool_call_cap"]
    assert tool_call_cap <= RB.WEB_INVOCATIONS_PER_HOUR, (
        "a build cannot spend its tool budget inside its own hourly bucket"
    )
    assert RB.WEB_INVOCATIONS_PER_HOUR >= tool_call_cap * 1.2, "no headroom at all"


@pytest.mark.asyncio
async def test_the_wired_binding_carries_the_override_to_the_agency(monkeypatch):
    """THE BINDING PATH, not just the file. ``external_audit_binding`` builds
    ``analyst_grants`` from ``descriptor.action_packs``; if it dropped the
    override the resolver would never see it and the builder would be back on
    the shared bucket with a descriptor that says otherwise."""
    from legba.data.analysts.deterministic_handlers import reference_builder as RB
    from legba.data.schemas.action_pack import ActionPackRef
    from legba.data.schemas.analyst import AnalystDescriptor
    from legba.runtime.source_first_runtime import AGENCY_HOLDER

    class _RegistryStub:
        async def get_descriptor(self, pack_id, *, family):
            assert family == "action_pack"
            body = yaml.safe_load(_PACK_PATH.read_text())
            body["identity"]["version"] = "a" * 16
            return {"body": body}

        async def get_descriptor_typed(self, *a, **k):
            return None

    monkeypatch.setitem(AGENCY_HOLDER, "agency", object())
    monkeypatch.setitem(AGENCY_HOLDER, "tool_context", _Stub("ctx"))

    shipped = AnalystDescriptor.model_validate(_descriptor_body())
    descriptor = _descriptor(packs=list(shipped.action_packs))
    deps = await _wire(descriptor, registry_client=_RegistryStub())

    binding = deps.extras[WEB_BINDING_DEPS_EXTRA_KEY]
    assert binding.budget_account == "reference_builder"   # the per-analyst one …
    # … which the PACK's account would override, were it not overridden itself
    from legba.data.analysts.agency.resolution import TargetScopeView, resolve_pack

    res = resolve_pack(
        pack=binding.pack,
        analyst_grants=binding.analyst_grants,
        target_allows=[ActionPackRef(pack_id="web_access")],
        scope=TargetScopeView(target_id="country_g20_ar"),
    )
    assert res.effective, res.reason
    assert res.governor.budget_account == RB.WEB_BUDGET_ACCOUNT
    assert res.governor.max_invocations_per_hour == RB.WEB_INVOCATIONS_PER_HOUR


# ---------------------------------------------------------------------------
# R2-FIX(2) — the day bucket, sized to the cadence
# ---------------------------------------------------------------------------


def test_the_day_bucket_covers_the_cadence_it_is_scheduled_at():
    """THE ARITHMETIC THAT WAS NEVER DONE. 4,000,000 was sized for "a handful of
    builds" before the cadence was halved to 7 days. A 32-target roster on a
    7-day cadence needs ceil(32/7) = 5 builds a day and a build may spend 1.2M:
    5 x 1.2M = 6M, which 4M cannot hold. The lane was STRUCTURALLY guaranteed to
    hit a BUDGET_THROTTLED cooldown roughly daily, whatever else was fixed — and
    a cooldown is an hour of silence stamped with no log line, not a slow tick.

    This is the assertion that turns a cadence change, a bigger roster or a
    raised per-build ceiling into a RED TEST instead of a cooldown found in the
    logs three days later."""
    import math

    from legba.data.analysts.deterministic_handlers import reference_builder as RB

    body = _descriptor_body()
    options = body["method"]["options"]
    day_bucket = int(body["method"]["budget_tokens_per_day"])

    # the descriptor and the code agree
    assert day_bucket == RB.DEFAULT_BUDGET_TOKENS_PER_DAY
    assert options["cadence_days"] == RB.DEFAULT_CADENCE_DAYS

    builds_per_day = math.ceil(
        RB.BUDGET_SIZED_FOR_ROSTER / int(options["cadence_days"])
    )
    worst_case = builds_per_day * int(options["build_max_tokens"])
    if day_bucket == 0:
        # 0 (or absent) means NO per-analyst budget (runtime/budget.py): the
        # operator runs this deployment on cadence + endpoint capacity alone
        # (2026-09-17), so the cadence arithmetic has nothing to fit inside.
        return
    assert worst_case <= day_bucket, (
        f"{builds_per_day} builds/day x {options['build_max_tokens']:,} tokens "
        f"= {worst_case:,} against a {day_bucket:,} day bucket — this lane will "
        "cool down on its own cadence"
    )
    # and the margin is real rather than exactly-fits
    assert worst_case <= day_bucket * 0.85

    # the run that caused the incident would not have fitted the OLD bucket
    # either, which is the point: one build must never be able to spend a day
    assert int(options["build_max_tokens"]) * 2 <= day_bucket

    # the hourly tick can physically deliver that many builds
    assert builds_per_day <= 24 * int(options["max_targets_per_run"])

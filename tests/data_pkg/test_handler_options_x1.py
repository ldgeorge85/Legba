# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 — ``method.options`` is a REAL channel, and the catalog tells the truth.

The defect: ``MethodBlock`` had no ``options`` field and the runtime built the
handler ``options`` mapping from scratch at fire time, so every
``options.get(knob, DEFAULT)`` in every deterministic handler was unreachable
dead config. These tests hold the repair to four properties:

* **the catalog is not fiction** — every declared knob is genuinely read by the
  handler that claims it, every sub-handler has an entry, and no knob a handler
  reads is left undeclared (the sweep that would otherwise re-accrete dead
  config one handler at a time);
* **defaults are byte-identical** — a descriptor with no options block
  contributes NOTHING to the mapping and produces NO receipt entry;
* **degrade is loud** — unknown / reserved / private / out-of-range keys are
  dropped with a receipt note, never silently applied, never fatal;
* **the merge is the runtime's own** — the tests call
  ``dapr_actors._merge_descriptor_options``, the function the actor run path
  calls, not a re-implementation of it.

The end-to-end proof that a descriptor-set knob CHANGES OBSERVED BEHAVIOR lives
in ``test_alert_trigger_scan.py`` (it needs that module's ephemeral-DB rig):
``test_descriptor_options_reach_the_handler_and_change_the_cap``.
"""
from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Any

import pytest

from legba.data.analysts.deterministic import SUB_HANDLERS
from legba.data.analysts.handler_options import (
    HANDLER_OPTIONS,
    RESERVED_OPTION_KEYS,
    known_option_names,
    resolve_handler_options,
)
from legba.data.schemas.analyst import AnalystDescriptor, MethodBlock
from legba.runtime.dapr_actors import (
    HANDLER_OPTIONS_RECEIPT_PHASE,
    _attach_option_receipt,
    _merge_descriptor_options,
)


HANDLER_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "legba"
    / "data"
    / "analysts"
    / "deterministic_handlers"
)

#: ``options.get("key"...)`` / ``options["key"]`` in a handler module.
_OPTION_READ_RE = re.compile(
    r'options(?:\.get\(\s*|\[\s*)"([A-Za-z_][A-Za-z0-9_]*)"'
)


#: Sub-handlers whose ``handle`` delegates the option reads to a shared engine
#: or a sibling writer module. The catalog covers the DELEGATE's keys too, so
#: the reachability sweep has to follow the delegation.
_DELEGATES: dict[str, tuple[str, ...]] = {
    # both janitors execute one named row through the shared engine
    "signals_retention": ("_retention_sweep",),
    "analyst_traces_retention": ("_retention_sweep",),
    # the scoreboard drives forecast_acute's issue/resolve/pull writers,
    # handing them its own ``options`` verbatim
    "forecast_scoreboard": ("forecast_acute",),
}


def _module_for(sub_handler: str) -> str:
    return SUB_HANDLERS[sub_handler].__module__


def _option_reads_in_module(module_name: str) -> set[str]:
    leaf = module_name.rsplit(".", 1)[-1]
    return set(_OPTION_READ_RE.findall((HANDLER_DIR / f"{leaf}.py").read_text()))


def _option_reads_for(sub_handler: str) -> set[str]:
    """Every option key this sub-handler's code path actually reads, following
    delegation into shared engines."""
    reads = _option_reads_in_module(_module_for(sub_handler))
    for leaf in _DELEGATES.get(sub_handler, ()):
        reads |= _option_reads_in_module(leaf)
    return reads


# ---------------------------------------------------------------------------
# The catalog is not fiction
# ---------------------------------------------------------------------------


def test_every_registered_sub_handler_has_a_catalog_entry():
    """A sub-handler with no entry would make EVERY option on it 'unknown' —
    a silent regression back to dead config. An empty tuple is a legitimate,
    explicit entry; a MISSING key is not."""
    missing = sorted(set(SUB_HANDLERS) - set(HANDLER_OPTIONS))
    assert missing == [], (
        f"sub-handlers with no handler_options entry: {missing} — declare "
        "their knobs (or an explicit empty tuple)"
    )


def test_catalog_declares_no_sub_handler_that_does_not_exist():
    stale = sorted(set(HANDLER_OPTIONS) - set(SUB_HANDLERS))
    assert stale == [], f"catalog entries for unregistered sub-handlers: {stale}"


@pytest.mark.parametrize("sub_handler", sorted(HANDLER_OPTIONS))
def test_every_declared_knob_is_actually_read_by_its_handler(sub_handler):
    """The catalog claims an operator can move these. Prove the handler reads
    them — a declared-but-unread knob is dead config with extra steps."""
    if sub_handler not in SUB_HANDLERS:
        pytest.skip("covered by test_catalog_declares_no_sub_handler...")
    reads = _option_reads_for(sub_handler)
    for name in known_option_names(sub_handler):
        assert name in reads, (
            f"handler_options declares {sub_handler}.{name} but "
            f"{_module_for(sub_handler)} never reads options[{name!r}]"
        )


@pytest.mark.parametrize("sub_handler", sorted(SUB_HANDLERS))
def test_no_handler_knob_is_left_undeclared(sub_handler):
    """The X-1 sweep, enforced. Every non-reserved key a handler reads must be
    declared, or it is silently unreachable again — exactly the defect."""
    reads = _option_reads_for(sub_handler)
    declared = set(known_option_names(sub_handler))
    undeclared = sorted(
        k
        for k in reads
        if k not in declared
        and k not in RESERVED_OPTION_KEYS
        and not k.startswith("_")
    )
    assert undeclared == [], (
        f"{sub_handler} reads undeclared options {undeclared} — add them to "
        "HANDLER_OPTIONS (or to RESERVED_OPTION_KEYS if runtime-owned)"
    )


def test_no_declared_knob_collides_with_a_reserved_key():
    """A catalog entry shadowing a reserved key could never be applied (the
    reserved check runs first), so it would be a permanent, silent lie."""
    for sub_handler, specs in HANDLER_OPTIONS.items():
        for spec in specs:
            assert spec.name not in RESERVED_OPTION_KEYS, (
                f"{sub_handler}.{spec.name} collides with a reserved key"
            )
            assert not spec.name.startswith("_")
            assert spec.doc.strip(), f"{sub_handler}.{spec.name} has no doc"


def test_alert_trigger_scan_dead_knobs_from_the_survey_are_all_reachable():
    """The six knobs the X-1 survey named, by name."""
    declared = set(known_option_names("alert_trigger_scan"))
    for knob in (
        "per_desk_cap",
        "per_watch_cap",
        "effective_conf_floor",
        "finding_window_hours",
        "baseline_days",
        "baseline_sigma",
    ):
        assert knob in declared


# ---------------------------------------------------------------------------
# Resolution — accept
# ---------------------------------------------------------------------------


def test_accepts_declared_keys_with_valid_values():
    res = resolve_handler_options(
        "alert_trigger_scan",
        {"per_desk_cap": 7, "baseline_sigma": 2.5, "effective_conf_floor": 0.6},
    )
    assert res.accepted == {
        "per_desk_cap": 7,
        "baseline_sigma": 2.5,
        "effective_conf_floor": 0.6,
    }
    assert res.rejected == ()
    assert res.degraded is False


def test_int_is_accepted_where_a_float_is_declared():
    """YAML ``baseline_sigma: 2`` must not be rejected as 'not a float'."""
    res = resolve_handler_options("alert_trigger_scan", {"baseline_sigma": 2})
    assert res.accepted == {"baseline_sigma": 2.0}
    assert isinstance(res.accepted["baseline_sigma"], float)


def test_empty_options_resolve_to_nothing():
    for raw in (None, {}):
        res = resolve_handler_options("alert_trigger_scan", raw)
        assert res.accepted == {}
        assert res.rejected == ()


def test_str_list_and_choice_options_round_trip():
    res = resolve_handler_options(
        "narrative_mapper", {"statuses": ["contested", "surfaced"]}
    )
    assert res.accepted == {"statuses": ["contested", "surfaced"]}
    res = resolve_handler_options(
        "evidence_archiver", {"web_origin_license_gate": "inherit"}
    )
    assert res.accepted == {"web_origin_license_gate": "inherit"}


# ---------------------------------------------------------------------------
# Resolution — loud degrade
# ---------------------------------------------------------------------------


def test_unknown_key_is_dropped_loudly(caplog):
    with caplog.at_level(logging.WARNING):
        res = resolve_handler_options(
            "alert_trigger_scan", {"per_desk_cap": 2, "per_desk_kap": 9}
        )
    assert res.accepted == {"per_desk_cap": 2}
    assert [r.cause for r in res.rejected] == ["unknown_key"]
    assert res.rejected[0].key == "per_desk_kap"
    assert "handler_options.rejected" in caplog.text
    # The message names the alternatives, so the fix is one log line away.
    assert "per_desk_cap" in res.rejected[0].detail


def test_reserved_key_is_refused_so_a_descriptor_cannot_forge_provenance():
    res = resolve_handler_options(
        "alert_trigger_scan",
        {"analyst_id": "someone_else", "run_id": "x", "target_id": "ZZ"},
    )
    assert res.accepted == {}
    assert {r.cause for r in res.rejected} == {"reserved_key"}
    assert {r.key for r in res.rejected} == {"analyst_id", "run_id", "target_id"}


def test_private_key_is_refused():
    res = resolve_handler_options(
        "cross_source_coalesce", {"_test_embedder": "x"}
    )
    assert res.accepted == {}
    assert res.rejected[0].cause == "private_key"


def test_unknown_sub_handler_rejects_everything_but_never_raises(caplog):
    with caplog.at_level(logging.WARNING):
        res = resolve_handler_options("no_such_handler", {"a": 1})
    assert res.accepted == {}
    assert res.rejected[0].cause == "unknown_handler"


@pytest.mark.parametrize(
    "sub_handler,key,value,why",
    [
        ("alert_trigger_scan", "per_desk_cap", 0, "a cap must be positive"),
        ("alert_trigger_scan", "per_desk_cap", -3, "negative cap"),
        ("alert_trigger_scan", "per_desk_cap", 2.5, "a cap is an int"),
        ("alert_trigger_scan", "per_desk_cap", "3", "a numeric string is not an int"),
        ("alert_trigger_scan", "per_desk_cap", True, "bool is not a cap of 1"),
        ("alert_trigger_scan", "effective_conf_floor", 1.5, "a floor is in [0,1]"),
        ("alert_trigger_scan", "effective_conf_floor", -0.1, "a floor is in [0,1]"),
        ("alert_trigger_scan", "baseline_days", 1, "needs >= 2 buckets"),
        ("alert_trigger_scan", "finding_window_hours", 0, "a window is positive"),
        ("alert_trigger_scan", "geo_min_distinct_families", 1, "diversity needs 2+"),
        ("evidence_archiver", "timeout_seconds", 0, "strictly positive"),
        ("evidence_archiver", "web_origin_license_gate", "wide_open", "not a choice"),
        ("evidence_archiver", "forbid_license_classes", "not_a_list", "wrong shape"),
        ("evidence_archiver", "forbid_license_classes", [1, 2], "not strings"),
        ("narrative_mapper", "statuses", ["resolved"], "not in the CHECK vocabulary"),
        ("entity_gc", "run_dormant", 1, "a flag is a bool, not an int"),
        ("calibration_tracking", "bin_count", 1, "needs >= 2 bins"),
    ],
)
def test_invalid_values_are_dropped_not_applied(sub_handler, key, value, why):
    res = resolve_handler_options(sub_handler, {key: value})
    assert res.accepted == {}, f"{why}: {key}={value!r} must not be applied"
    assert res.rejected[0].cause == "invalid_value"
    assert res.rejected[0].key == key


def test_bucket_interval_is_choice_locked_because_it_reaches_sql():
    """``bucket_interval`` is interpolated into a time_bucket() call, so it is
    the one string option that must never be free-form."""
    ok = resolve_handler_options("anomaly_detection", {"bucket_interval": "1 hour"})
    assert ok.accepted == {"bucket_interval": "1 hour"}
    bad = resolve_handler_options(
        "anomaly_detection", {"bucket_interval": "1 hour') OR true --"}
    )
    assert bad.accepted == {}
    assert bad.rejected[0].cause == "invalid_value"


def test_one_bad_key_never_taints_its_valid_siblings():
    res = resolve_handler_options(
        "alert_trigger_scan",
        {"per_desk_cap": 4, "baseline_sigma": "loads", "bogus": 1},
    )
    assert res.accepted == {"per_desk_cap": 4}
    assert {r.key for r in res.rejected} == {"baseline_sigma", "bogus"}


# ---------------------------------------------------------------------------
# Schema — registration-time structural gate
# ---------------------------------------------------------------------------


def _method(**kw: Any) -> MethodBlock:
    base = {"kind": "deterministic", "impl": "legba.x:run_method"}
    base.update(kw)
    return MethodBlock(**base)


def test_method_options_defaults_to_empty_and_is_optional():
    assert _method().options == {}


def test_method_options_accepted_on_a_deterministic_kind():
    assert _method(options={"per_desk_cap": 3}).options == {"per_desk_cap": 3}


def test_method_options_on_a_non_deterministic_kind_is_a_descriptor_level_check():
    """QW1-B moved the "which kinds may carry options" gate UP to the DESCRIPTOR
    validator, which can see ``identity.kind`` — a :class:`MethodBlock` alone
    cannot tell an ``inline_target`` unit (which now DOES read options) from a
    composition (which does not). The block itself no longer refuses; the rule
    is enforced, unchanged in spirit, by
    ``AnalystDescriptor._check_options_kind`` — asserted in
    ``tests/data_pkg/test_unit_grounding.py``
    (``test_a_kind_with_no_catalog_still_cannot_carry_options``)."""
    block = MethodBlock(kind="llm_single_turn", prompt_module="a:b", options={"x": 1})
    assert block.options == {"x": 1}


def test_method_options_refuses_private_and_nested_values():
    with pytest.raises(ValueError, match="private"):
        _method(options={"_hook": 1})
    with pytest.raises(ValueError, match="JSON scalar"):
        _method(options={"nested": {"a": 1}})


def test_method_options_accepts_flat_scalar_lists():
    assert _method(options={"units": ["a", "b"]}).options == {"units": ["a", "b"]}


def test_unknown_key_warns_at_registration_but_never_refuses(caplog):
    """Registry rows outlive code: a knob renamed in a later release must not
    brick activation for every descriptor still carrying the old name."""
    body = _descriptor_body(options={"per_desk_kap": 9})
    with caplog.at_level(logging.WARNING):
        desc = _descriptor(body)
    assert desc.method.options == {"per_desk_kap": 9}
    assert "handler_options.rejected" in caplog.text
    assert "@register" in caplog.text


DESCRIPTOR_DIR = Path(__file__).resolve().parents[2] / "descriptors"


def _descriptor_body(*, options: dict[str, Any] | None = None) -> dict[str, Any]:
    """The SHIPPED ``alert_trigger_scan`` descriptor, optionally given options.

    Deliberately the real YAML rather than a hand-built stub: the byte-identical
    proof is only worth anything if it runs against the descriptor production
    actually registers.
    """
    import yaml

    body = yaml.safe_load(
        (DESCRIPTOR_DIR / "analyst_alert_trigger_scan.yaml").read_text()
    )
    # The registry stamps the real content hash at register time.
    body["identity"]["version"] = "0" * 16
    if options is not None:
        body["method"]["options"] = options
    return body


def _descriptor(body: dict[str, Any]) -> AnalystDescriptor:
    """Rehydrate exactly as the registry does (``strict=False`` per the
    L-111 pattern in ``DescriptorStore.get_typed``) — stored JSONB carries
    string enum values that need coercion."""
    return AnalystDescriptor.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# The runtime merge — THE channel that was missing
# ---------------------------------------------------------------------------


def _runtime_options(sub_handler: str = "alert_trigger_scan") -> dict[str, Any]:
    """The mapping the actor builds before the merge point."""
    return {
        "analyst_id": "alert_trigger_scan",
        "analyst_version": "abc",
        "run_id": "r-1",
        "sub_handler": sub_handler,
    }


def test_no_options_block_is_byte_identical_to_before():
    """THE regression guard. A descriptor with no ``method.options`` must leave
    the mapping EXACTLY as the runtime built it and produce NO receipt — so
    every handler's own ``options.get(key, DEFAULT)`` resolves to the same
    in-source constant it did before this field existed."""
    desc = _descriptor(_descriptor_body())
    options = _runtime_options()
    before = dict(options)

    receipt = _merge_descriptor_options(options, desc, actor_id="a")

    assert options == before
    assert receipt is None
    # ...and no receipt means no trace step either.
    result = _FakeResult()
    _attach_option_receipt(result, receipt)
    assert result.intermediate_steps == []


def test_descriptor_option_lands_in_the_mapping_the_handler_reads():
    desc = _descriptor(
        _descriptor_body(options={"per_desk_cap": 1, "baseline_sigma": 3.0})
    )
    options = _runtime_options()

    receipt = _merge_descriptor_options(options, desc, actor_id="a")

    assert options["per_desk_cap"] == 1
    assert options["baseline_sigma"] == 3.0
    assert receipt["status"] == "applied"
    assert receipt["phase"] == HANDLER_OPTIONS_RECEIPT_PHASE
    assert receipt["applied"] == {"per_desk_cap": 1, "baseline_sigma": 3.0}
    assert receipt["rejected"] == []


def test_runtime_and_forced_run_values_outrank_the_descriptor():
    """Precedence: runtime provenance > an explicit forced-run option > the
    descriptor. An operator forcing a one-off run must not be silently
    overridden by the standing config."""
    desc = _descriptor(
        _descriptor_body(options={"per_desk_cap": 1, "analyst_id": "spoofed"})
    )
    options = _runtime_options()
    options["per_desk_cap"] = 9  # as a forced-run payload option would arrive

    _merge_descriptor_options(options, desc, actor_id="a")

    assert options["per_desk_cap"] == 9
    assert options["analyst_id"] == "alert_trigger_scan"


def test_bad_descriptor_option_degrades_loudly_and_is_receipt_noted(caplog):
    desc = _descriptor(
        _descriptor_body(options={"per_desk_cap": 0, "nonsense": 1})
    )
    options = _runtime_options()

    with caplog.at_level(logging.WARNING):
        receipt = _merge_descriptor_options(options, desc, actor_id="a")

    # Dropped — the handler default stands, nothing nonsensical applied.
    assert "per_desk_cap" not in options
    assert "nonsense" not in options
    assert receipt["status"] == "degraded"
    assert receipt["applied"] == {}
    causes = {r["cause"] for r in receipt["rejected"]}
    assert causes == {"invalid_value", "unknown_key"}
    assert "handler_options.degraded" in caplog.text

    # ...and the note is durable: it rides analyst_traces.intermediate_steps.
    result = _FakeResult()
    _attach_option_receipt(result, receipt)
    assert len(result.intermediate_steps) == 1
    step = result.intermediate_steps[0]
    assert step["phase"] == HANDLER_OPTIONS_RECEIPT_PHASE
    assert step["status"] == "degraded"
    assert {r["key"] for r in step["rejected"]} == {"per_desk_cap", "nonsense"}


def test_receipt_appends_to_existing_steps_without_clobbering_them():
    result = _FakeResult()
    result.intermediate_steps = [{"phase": "scan"}]
    _attach_option_receipt(result, {"phase": HANDLER_OPTIONS_RECEIPT_PHASE})
    assert [s["phase"] for s in result.intermediate_steps] == [
        "scan",
        HANDLER_OPTIONS_RECEIPT_PHASE,
    ]


def test_merge_resolves_the_catalog_by_sub_handler_not_analyst_id():
    """Two descriptors may share a sub-handler under different ids; the knobs
    belong to the HANDLER."""
    body = _descriptor_body(options={"ttl_days": 45})
    body["identity"]["id"] = "traces_janitor_alt"
    body["method"]["sub_handler"] = "analyst_traces_retention"
    desc = _descriptor(body)
    options = _runtime_options(sub_handler="analyst_traces_retention")

    receipt = _merge_descriptor_options(options, desc, actor_id="a")

    assert options["ttl_days"] == 45
    assert receipt["sub_handler"] == "analyst_traces_retention"


class _FakeResult:
    """Stands in for AnalystMethodResult's ``intermediate_steps`` surface."""

    def __init__(self) -> None:
        self.intermediate_steps: list[dict[str, Any]] = []


# ---------------------------------------------------------------------------
# Wiring guard — the actor body must actually CALL the merge
#
# The tests above drive `_merge_descriptor_options` directly, because
# `AnalystActor.run` needs a daprd sidecar no in-process rig provides. That
# leaves one failure mode uncovered and it is the worst one: a correct,
# well-tested function that production never invokes. These pin the call
# sites in the actor body itself, in the right ORDER.
# ---------------------------------------------------------------------------


def _actor_run_source() -> str:
    import inspect

    from legba.runtime.dapr_actors import AnalystActor

    return inspect.getsource(AnalystActor.run)


def test_actor_run_body_calls_the_merge():
    assert "_merge_descriptor_options(" in _actor_run_source(), (
        "the actor run path must call the merge — an unwired channel is the "
        "X-1 defect restored"
    )


def test_actor_run_body_attaches_the_receipt_after_dispatch():
    src = _actor_run_source()
    assert "_attach_option_receipt(" in src
    assert src.index("_merge_descriptor_options(") < src.index(
        "_invoke_run_method("
    ), "options must be merged BEFORE the handler is invoked"
    assert src.index("_invoke_run_method(") < src.index(
        "_attach_option_receipt("
    ), "the receipt is stamped on the result the handler returned"


def test_merge_happens_after_the_payload_options_passthrough():
    """Precedence is enforced by ORDER + setdefault, so the order is the
    contract: a forced run's explicit option must already be in the mapping
    when the descriptor's block is merged over it."""
    src = _actor_run_source()
    assert src.index('payload.get("options")') < src.index(
        "_merge_descriptor_options("
    )

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X-1 Stage 1 — the escalation edge's ACTION is config-driven.

The escalation edge is the one threshold→action rule that actually runs in
production (785 settled invocations on the live ledger), and three of its four
dimensions were already open: the metric is computed, the THRESHOLD rides the
pack tool config, the delivery fans out through real sinks. The ACTION was a
hardcoded ``"escalate"`` string literal at the fire site — even though the
config dict that could name another tool was already ``extra="allow"`` and read
from the registry DB row.

These are the pure tests over :func:`resolve_escalation_action`: the shipped
descriptor's default, a valid selection, and the loud degrade. The end-to-end
proof that a configured action reaches the tool dispatcher through the full
three-way gate + governor lives in ``test_agency_binding.py`` (it needs that
module's migrated-Postgres rig):
``test_escalate_action_tool_config_selects_a_different_tool``.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml

from legba.data.analysts.agency.binding import (
    DEFAULT_ESCALATION_ACTION,
    ESCALATION_ACTION_CONFIG_KEY,
    EscalationBinding,
    resolve_escalation_action,
)
from legba.data.schemas.action_pack import ActionPack

_DESCRIPTORS = Path(__file__).resolve().parents[3] / "descriptors"


def _load_pack(fname: str) -> ActionPack:
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


def _escalate_tool_config(pack: ActionPack) -> dict:
    for t in pack.tools:
        if t.name == "escalate":
            return dict(t.config or {})
    raise AssertionError("the escalate_finding pack must declare an escalate tool")


# ---------------------------------------------------------------------------
# The default is the old literal, byte for byte
# ---------------------------------------------------------------------------


def test_binding_default_is_the_literal_it_replaced():
    """``EscalationBinding`` constructed the pre-Stage-1 way must still invoke
    ``escalate`` — every existing call site keeps its behavior."""
    assert DEFAULT_ESCALATION_ACTION == "escalate"
    binding = EscalationBinding(binding=None)  # type: ignore[arg-type]
    assert binding.action_tool == "escalate"
    assert binding.action_degraded is None


def test_shipped_escalate_pack_resolves_to_escalate():
    """The SHIPPED descriptor declares no ``action_tool``, so the live system's
    behavior is unchanged by Stage 1."""
    pack = _load_pack("action_pack_escalate.yaml")
    cfg = _escalate_tool_config(pack)
    assert ESCALATION_ACTION_CONFIG_KEY not in cfg, (
        "the shipped pack must not pin an action — the default IS the contract"
    )
    action, note = resolve_escalation_action(cfg, pack)
    assert (action, note) == ("escalate", None)


def test_absent_config_and_absent_pack_both_resolve_to_the_default():
    assert resolve_escalation_action(None, None) == ("escalate", None)
    assert resolve_escalation_action({}, None) == ("escalate", None)


def test_gates_are_still_read_from_the_same_config():
    """Stage 1 must not disturb the threshold leg it sits next to."""
    cfg = _escalate_tool_config(_load_pack("action_pack_escalate.yaml"))
    assert cfg["confidence_gate"] == 0.85
    assert cfg["severity_gate"] == "high"


# ---------------------------------------------------------------------------
# Selection + validation against the pack's live tool list
# ---------------------------------------------------------------------------


def test_configured_action_naming_a_real_pack_tool_is_honored():
    """An operator adds ``create_incident`` to the pack and selects it — the
    exact ``PUT /descriptors/…`` edit Stage 1 makes possible, no rebuild."""
    pack = _load_pack("action_pack_incident_response.yaml")
    tool_names = {t.name for t in pack.tools}
    assert {"escalate", "create_incident"} <= tool_names

    action, note = resolve_escalation_action(
        {ESCALATION_ACTION_CONFIG_KEY: "create_incident"}, pack
    )
    assert action == "create_incident"
    assert note is None


def test_unknown_action_degrades_loudly_to_the_default(caplog):
    """A typo must not take the escalation edge offline: the operator keeps
    getting paged through the default channel while the system shouts."""
    pack = _load_pack("action_pack_escalate.yaml")
    with caplog.at_level(logging.ERROR):
        action, note = resolve_escalation_action(
            {ESCALATION_ACTION_CONFIG_KEY: "escalte"}, pack, log_context="unit"
        )
    assert action == "escalate"
    assert note is not None
    assert "escalte" in note
    # The note names what the pack DOES have, so the fix is one read away.
    assert "escalate" in note
    assert "escalation_action.degraded" in caplog.text


def test_action_naming_a_tool_on_a_DIFFERENT_pack_is_still_refused():
    """Validation is against THIS pack's tool list — ``create_incident`` exists
    in the registry but not on ``escalate_finding``, so selecting it there is a
    misconfiguration, not a cross-pack invocation."""
    pack = _load_pack("action_pack_escalate.yaml")
    action, note = resolve_escalation_action(
        {ESCALATION_ACTION_CONFIG_KEY: "create_incident"}, pack
    )
    assert action == "escalate"
    assert note is not None


@pytest.mark.parametrize("bad", ["", "   "])
def test_empty_action_degrades_rather_than_invoking_a_nameless_tool(bad):
    pack = _load_pack("action_pack_escalate.yaml")
    action, note = resolve_escalation_action(
        {ESCALATION_ACTION_CONFIG_KEY: bad}, pack
    )
    assert action == "escalate"
    assert note is not None


def test_non_string_action_is_coerced_then_validated():
    """``extra="allow"`` means the config can hold anything; a number names no
    tool, so it degrades rather than blowing up the binding build."""
    pack = _load_pack("action_pack_escalate.yaml")
    action, note = resolve_escalation_action({ESCALATION_ACTION_CONFIG_KEY: 7}, pack)
    assert action == "escalate"
    assert note is not None


# ---------------------------------------------------------------------------
# Wiring guard — the literal is gone from the fire site, and the host resolves
#
# The whole defect was a hardcoded string at one call site. A config surface
# that the fire site does not read would be the same defect with more code, so
# pin both ends.
# ---------------------------------------------------------------------------


def test_fire_site_no_longer_hardcodes_the_tool_name():
    import inspect

    from legba.runtime.actor_output_emit import _maybe_escalate_finding

    src = inspect.getsource(_maybe_escalate_finding)
    assert 'run_tool(\n        action_tool,' in src.replace("\r", ""), (
        "the escalation fire site must dispatch the RESOLVED action, not a "
        "string literal"
    )
    assert 'run_tool("escalate"' not in src
    assert "config_note" in src, "a degrade must reach the delivery ledger"


def test_host_resolves_the_action_when_building_the_binding():
    import inspect

    from legba.runtime import dapr_host

    src = inspect.getsource(dapr_host)
    assert "resolve_escalation_action(" in src
    assert "action_tool=esc_action" in src
    assert "action_degraded=esc_action_note" in src


def test_channel_emitter_carries_the_note_onto_the_audit_row():
    import inspect

    from legba.data.analysts.agency.tools import ChannelEmitter, _emit_to_channels

    assert "config_note" in inspect.getsource(_emit_to_channels)
    assert "config_note" in inspect.getsource(
        ChannelEmitter._write_delivery_audit
    )


def test_degrade_note_rides_the_binding_for_the_delivery_ledger():
    pack = _load_pack("action_pack_escalate.yaml")
    action, note = resolve_escalation_action(
        {ESCALATION_ACTION_CONFIG_KEY: "nope"}, pack
    )
    binding = EscalationBinding(
        binding=None,  # type: ignore[arg-type]
        action_tool=action,
        action_degraded=note,
    )
    assert binding.action_tool == "escalate"
    assert "nope" in binding.action_degraded

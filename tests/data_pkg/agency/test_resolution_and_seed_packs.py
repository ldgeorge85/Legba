# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P-11 pure-unit coverage — three-way resolution, governor merge, seed packs.

No DB / NATS — exercises the resolution algebra + governor-tightening merge +
that the three seed-pack descriptor YAMLs parse against the FROZEN ActionPack
schema. Fast; complements the DB-backed hard-gate acceptance.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml

from legba.data.schemas.action_pack import ActionPack, ActionPackRef, PackGovernor
from legba.data.analysts.agency import (
    TargetScopeView,
    resolve_pack,
    scope_view_from_target,
)

_DESCRIPTORS = Path(__file__).resolve().parents[3] / "descriptors"
_SEED_PACKS = [
    ("action_pack_media_processing.yaml", "media_processing", ["process_media"]),
    ("action_pack_incident_response.yaml", "incident_response", ["escalate", "create_incident"]),
    # A-3: the governed consult tool surface + the D1 example pack.
    # (action_pack_discovery.yaml is RETIRED per F-1 — kept on disk for
    # registry history, no longer registered or asserted as a seed pack.)
    ("action_pack_substrate_read.yaml", "substrate_read",
     ["search_signals", "query_facts", "inspect_entity", "vector_search",
      "query_nexuses", "query_hypotheses", "get_timeline", "compare_targets",
      # #99 — graph query tools over the reified nexus property graph.
      "query_paths", "find_proxy_chains", "query_brokers",
      # Palette expansion — finished-intelligence reads (consult + GATHER) +
      # navigation readers (consult-only, but still pack-governed).
      "list_findings", "list_situations", "query_predictions",
      "list_targets", "list_sources"]),
    ("action_pack_escalate.yaml", "escalate_finding", ["escalate"]),
    # S6: external evidence + operator-gated write-back packs.
    ("action_pack_web_access.yaml", "web_access", ["web_fetch", "web_search"]),
    ("action_pack_propose_facts.yaml", "propose_facts",
     ["propose_fact", "request_source", "open_question"]),
]


def _pack(pid, *, tools, tags=None, predicate=None, governor=None) -> ActionPack:
    body = {
        "identity": {
            "id": pid, "name": pid, "schema_uri": "legba/action_pack/1.0.0",
            "version": "a" * 16, "state": "active", "owner": "p11",
            "created": datetime.now(timezone.utc).isoformat(),
        },
        "tools": [{"name": t} for t in tools],
        "applies_to_tags": tags or [],
    }
    if predicate:
        body["applicability_predicate"] = predicate
    if governor:
        body["governor"] = governor
    return ActionPack.model_validate(body, strict=False)


# ---------------------------------------------------------------------------
# Three-way resolution algebra
# ---------------------------------------------------------------------------


def test_three_legs_all_pass():
    pack = _pack("p", tools=["t"], tags=["media"])
    scope = TargetScopeView(target_id="x", tags=["media", "news"])
    r = resolve_pack(
        pack=pack, analyst_grants=[ActionPackRef(pack_id="p")],
        target_allows=[ActionPackRef(pack_id="p")], scope=scope)
    assert r.effective and r.reason == "ok"


@pytest.mark.parametrize("grants,allows,tags,leg", [
    ([], [ActionPackRef(pack_id="p")], ["media"], "granted"),
    ([ActionPackRef(pack_id="p")], [], ["media"], "allowed"),
    ([ActionPackRef(pack_id="p")], [ActionPackRef(pack_id="p")], ["nomatch"], "applicable"),
])
def test_each_leg_can_deny(grants, allows, tags, leg):
    pack = _pack("p", tools=["t"], tags=["media"])
    scope = TargetScopeView(target_id="x", tags=tags)
    r = resolve_pack(pack=pack, analyst_grants=grants, target_allows=allows, scope=scope)
    assert not r.effective
    assert not getattr(r, leg)


def test_applicability_predicate_pass_and_fail():
    pack = _pack("p", tools=["t"], predicate='has_tag("incident")')
    grants = [ActionPackRef(pack_id="p")]
    allows = [ActionPackRef(pack_id="p")]
    ok = resolve_pack(pack=pack, analyst_grants=grants, target_allows=allows,
                      scope=TargetScopeView(target_id="x", tags=["incident"]))
    assert ok.effective
    no = resolve_pack(pack=pack, analyst_grants=grants, target_allows=allows,
                      scope=TargetScopeView(target_id="x", tags=["weather"]))
    assert not no.applicable


def test_universal_pack_no_constraints_applies_everywhere():
    pack = _pack("p", tools=["t"])             # no tags, no predicate
    r = resolve_pack(
        pack=pack, analyst_grants=[ActionPackRef(pack_id="p")],
        target_allows=[ActionPackRef(pack_id="p")],
        scope=TargetScopeView(target_id="x", tags=[]))
    assert r.effective


# ---------------------------------------------------------------------------
# Governor merge (tightening only)
# ---------------------------------------------------------------------------


def test_governor_override_tightens_never_loosens():
    pack = _pack("p", tools=["t"], tags=["media"],
                 governor={"api_rate_per_minute": 10, "max_cost_usd_per_day": 5.0})
    scope = TargetScopeView(target_id="x", tags=["media"])
    # target tightens rate to 3; analyst tries to LOOSEN cost to 100 (ignored).
    r = resolve_pack(
        pack=pack,
        analyst_grants=[ActionPackRef(
            pack_id="p", governor_override=PackGovernor(max_cost_usd_per_day=100.0))],
        target_allows=[ActionPackRef(
            pack_id="p", governor_override=PackGovernor(api_rate_per_minute=3))],
        scope=scope)
    assert r.effective
    assert r.governor.api_rate_per_minute == 3        # tightened
    assert r.governor.max_cost_usd_per_day == 5.0     # loosening rejected (min wins)


def test_scope_view_from_target_dict_and_obj():
    body = {
        "identity": {"id": "t1", "abstraction_level": "L2"},
        "scope": {"tags": ["a", "b"], "geo": ["US"], "entity_classes": ["org"],
                  "domain": "geo"},
    }
    v = scope_view_from_target(body)
    assert v.target_id == "t1" and v.tags == ["a", "b"] and v.domain == "geo"
    assert v.abstraction_level == "L2"


# ---------------------------------------------------------------------------
# Seed-pack YAMLs parse against the FROZEN ActionPack schema
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("fname,pid,tools", _SEED_PACKS)
def test_seed_pack_yaml_parses(fname, pid, tools):
    body = yaml.safe_load((_DESCRIPTORS / fname).read_text())
    body["identity"]["version"] = "0" * 16        # registry stamps the real hash
    pack = ActionPack.model_validate(body, strict=False)
    assert pack.identity.id == pid
    assert {t.name for t in pack.tools} == set(tools)
    # Every seed pack declares a governor with at least one cap.
    g = pack.governor
    assert any(v is not None for v in (
        g.max_invocations_per_hour, g.api_rate_per_minute,
        g.max_cost_usd_per_day, g.max_sources_per_window))


def test_retired_discovery_pack_still_parses_as_retired():
    # F-1: the file stays for registry history but must carry state=retired
    # so a stray re-registration cannot resurrect a tool with no handler.
    body = yaml.safe_load((_DESCRIPTORS / "action_pack_discovery.yaml").read_text())
    body["identity"]["version"] = "0" * 16
    pack = ActionPack.model_validate(body, strict=False)
    assert str(pack.identity.state) in ("retired", "LifecycleState.RETIRED")


def test_escalate_pack_gate_config_and_channel():
    body = yaml.safe_load((_DESCRIPTORS / "action_pack_escalate.yaml").read_text())
    body["identity"]["version"] = "0" * 16
    pack = ActionPack.model_validate(body, strict=False)
    esc = next(t for t in pack.tools if t.name == "escalate")
    assert esc.config["severity_gate"] == "high"
    assert float(esc.config["confidence_gate"]) == 0.85
    assert [c.name for c in pack.channels] == ["escalations"]
    assert pack.applies_to_tags == ["g20"]


def test_incident_pack_has_channels():
    body = yaml.safe_load((_DESCRIPTORS / "action_pack_incident_response.yaml").read_text())
    body["identity"]["version"] = "0" * 16
    pack = ActionPack.model_validate(body, strict=False)
    assert {c.name for c in pack.channels} == {"ops_alert", "soc_stream"}
    assert {c.kind for c in pack.channels} == {"alert", "nats_stream"}


# ---------------------------------------------------------------------------
# substrate_read: the THREE surfaces of the consult/agency tool palette must
# agree — the in-code tuple, the descriptor YAML, and the registered handlers.
# Drift here is exactly the class of bug the palette-expansion review caught
# (a tool advertised to the planner / in _KNOWN_TOOLS but absent from the pack
# → the GOVERNED consult path blocks it as unknown_tool).
# ---------------------------------------------------------------------------


def test_substrate_read_tuple_descriptor_handlers_agree():
    from legba.data.analysts.agency.substrate_read import (
        SUBSTRATE_READ_TOOLS,
        register_substrate_read_tools,
    )
    from legba.data.analysts.agency.tools import ToolRegistry

    tuple_names = set(SUBSTRATE_READ_TOOLS)

    # 1. descriptor YAML tool names == the in-code tuple
    body = yaml.safe_load(
        (_DESCRIPTORS / "action_pack_substrate_read.yaml").read_text()
    )
    descriptor_names = {t["name"] for t in body["tools"]}
    assert descriptor_names == tuple_names, (
        "descriptor action_pack_substrate_read.yaml tools != SUBSTRATE_READ_TOOLS "
        f"(only in descriptor: {descriptor_names - tuple_names}; "
        f"only in tuple: {tuple_names - descriptor_names})"
    )

    # 2. every tuple name has a registered handler, and no extras are registered
    reg = ToolRegistry()
    register_substrate_read_tools(reg)
    registered = set(reg.names)
    assert registered == tuple_names, (
        "register_substrate_read_tools handlers != SUBSTRATE_READ_TOOLS "
        f"(only registered: {registered - tuple_names}; "
        f"only in tuple: {tuple_names - registered})"
    )


def test_substrate_read_covers_consult_known_tools():
    """Every consult tool that is NOT one of the few non-substrate primitives
    must be a substrate_read pack tool — because the PRODUCTION consult loop is
    governed through this pack, a _KNOWN_TOOLS member absent from the pack is
    blocked as unknown_tool at runtime (the list_targets/list_sources defect)."""
    from legba.data.analysts.consult_on_demand import _KNOWN_TOOLS
    from legba.data.analysts.agency.substrate_read import SUBSTRATE_READ_TOOLS

    missing = set(_KNOWN_TOOLS) - set(SUBSTRATE_READ_TOOLS)
    assert not missing, (
        "consult _KNOWN_TOOLS entries with no substrate_read pack tool — the "
        f"governed consult path will block these as unknown_tool: {sorted(missing)}"
    )

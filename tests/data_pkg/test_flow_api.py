# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""N-J — the Flow canvas's read-only routes and the graph derivation.

Fixtures are SHAPED from live descriptor heads read on 2026-11-13 (a desk unit,
a composition, a journal head, a deterministic handler, a paused lens, a
source, a target, a pack) with their prompts and long fields cut down; no live
calls are made. Pins:

  * derivation — every edge names its basis; paused heads are drawn; unknown
    families, kinds and non-object bodies survive; references to missing heads
    become placeholder nodes; undeclared relations are listed, not drawn;
    the result is deterministic;
  * the matchers and code facts mirror the runtime's (parity tests);
  * configured vs sent sampling per route;
  * run shaping returns sizes and ids, never payloads; the body fetch is capped;
  * the routes through ASGI (bearer-gated, 400/404 answers), the server wiring,
    and the slim-image import guard.
"""
from __future__ import annotations

import copy
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from legba.data.registry import flow_graph as fg
from legba.data.registry import flow_params as fp
from legba.data.registry import flow_runs as fr
from legba.data.registry.api import API_TOKEN_ENV
from legba.data.registry.flow_api import build_flow_router

_SRC = pathlib.Path(__file__).resolve().parents[2] / "src"


def test_import_path_is_this_tree():
    assert pathlib.Path(fg.__file__).resolve().is_relative_to(_SRC.resolve())


# ---------------------------------------------------------------------------
# fixtures shaped from live heads
# ---------------------------------------------------------------------------

_LLM = {"factory_kind": "stack_ref", "expected_family": "llm_provider"}


def _analyst(aid, kind, *, state="active", method_kind="llm_planner", targets=None, others=(),
             grounding=(), packs=(), outputs=(), llm=None, sub_handler=None, prompt_module=None,
             system_prompt=None, trigger=None):
    return {
        "family": "analyst", "descriptor_id": aid, "version": "a" * 64, "kind": kind,
        "state": state, "name": aid.replace("_", " ").title(),
        "body": {
            "identity": {"id": aid, "kind": kind, "state": state, "name": aid, "owner": "t",
                         "version": "a" * 64, "inherits": [], "schema_uri": "legba/analyst/1.0.0",
                         "type_signature": {"deps_type": "legba.runtime.deps.StandardDeps",
                                            "input_type": "legba.runtime.SignalList",
                                            "output_type": "legba.runtime.Finding"}},
            "subscription": {"targets": targets, "substrate": {"direct_queries": False},
                             "other_analysts": [{"id": o, "data_types": ["finding"], "time_window": "336h"}
                                                for o in others]},
            "method": {"kind": method_kind, "llm": llm or {}, "impl": None, "sub_handler": sub_handler,
                       "prompt_module": prompt_module, "system_prompt": system_prompt,
                       "options": {}, "retries": 2, "timeout_seconds": 180,
                       "budget_tokens_per_day": 0, "budget_tokens_per_run": None},
            "cadence": {"trigger": trigger, "cooldown_seconds": 39600, "fallback_schedule": "0 7,19 * * *"},
            "outputs": list(outputs),
            "action_packs": [{"pack_id": p, "governor_override": None} for p in packs],
            "grounding": {"enabled": True, "sources": list(grounding), "max_facts": 30} if grounding else None,
            "eval": {"judge": None, "rubric": None},
            "mapping": {"fields": [], "schema_drift_policy": "warn_and_continue"},
        },
    }


DESK = _analyst(
    "escalation", "inline_target",
    targets={"predicate": 'has_tag("g20") or has_tag("watch")', "data_types": ["signal"],
             "time_window": "72h", "id_list": None},
    grounding=("substrate", "graph_structure"), packs=("escalate_finding",),
    llm={"primary": {"raw": "llm.primary.openai_compat", **_LLM},
         "verify": {"raw": "llm.primary.openai_compat", **_LLM},
         "max_tokens": 4096, "temperature": 1.0},
    system_prompt="WHO YOU ARE. You are the ESCALATION RISK unit …",
)
COMPOSITION = _analyst(
    "country_composition", "meta_findings_synthesizer",
    targets={"predicate": 'has_tag("g20") or has_tag("watch")', "data_types": ["finding"],
             "time_window": "336h", "id_list": None},
    others=("escalation", "military_posture"),
    prompt_module="legba.prompts.meta_findings_synthesizer.v1",
)
VOICE = _analyst("country_assessment", "meta_findings_synthesizer", others=("country_composition",),
                 outputs=({"kind": "a2a_skill", "config": {"skill_id": "intelligence.country"}},))
JOURNAL = _analyst(
    "journal_assessor", "journal_assessor", packs=("journal_read",),
    grounding=("substrate", "situations"), prompt_module="legba.prompts.journal_assessor:JOURNAL_SYSTEM",
    llm={"primary": {"raw": "llm.primary.openai_compat", **_LLM}, "max_tokens": 16000},
)
LENS_PAUSED = _analyst("lens_left", "journal_assessor", state="paused", packs=("journal_read",),
                       prompt_module="legba_geopolitics.lens_left:LENS_SYSTEM")
DETERMINISTIC = _analyst("signal_summarizer", "deterministic", method_kind="deterministic",
                         sub_handler="signal_summarizer",
                         llm={"primary": {"raw": "llm.primary.openai_compat", **_LLM}})
CONSULT = _analyst("consult_default", "consult_on_demand", method_kind="react_loop",
                   llm={"primary": {"raw": "llm.anthropic.opus_4_7", **_LLM}, "max_tokens": 32768})
RETIRED = _analyst("country_assessor", "country_assessor", state="retired")


def _source(sid, tags, *, kind="rss", state="active", tenant="shared", policy="open", geo=()):
    return {
        "family": "source", "descriptor_id": sid, "version": "b" * 64, "kind": kind, "state": state,
        "name": sid,
        "body": {"identity": {"id": sid, "kind": kind}, "scope": {
            "geo": list(geo), "tags": list(tags), "languages": ["en"], "owner_tenant": tenant},
            "cadence": {"schedule": {"raw": "32 3-23/6 * * *", "factory_kind": "cron"},
                        "jitter_seconds": 30, "cooldown_seconds": 60},
            "output": {"delivery": "lossy"}, "subscription_policy": policy},
    }


SOURCES = [
    _source("source.wire.one", ["news", "wire"]),
    _source("source.wire.two", ["news"], geo=["CN"]),
    _source("source.locked", ["news"], policy="allowlist"),
    _source("source.thinktank", ["analysis"]),
    _source("source.paused", ["news"], state="paused"),
]


def _target(tid, tags, *, sources=None, packs=()):
    return {
        "family": "target", "descriptor_id": tid, "version": "c" * 64, "kind": None, "state": "active",
        "name": tid,
        "body": {"identity": {"id": tid, "name": tid, "abstraction_level": "L1"},
                 "scope": {"geo": [], "tags": list(tags), "entity_classes": ["country"]},
                 "sources": sources if sources is not None else [
                     {"source_id": None, "subscription": {},
                      "source_selector": {"tags": ["news"], "kinds": ["rss"], "geo": [], "languages": [],
                                          "predicate": None, "owner_tenant": None}}],
                 "outputs": [], "allowed_action_packs": [{"pack_id": p} for p in packs],
                 "coordination": {"publishes": [], "subscribes_to": []}},
    }


TARGETS = [
    _target("country_g20_cn", ["g20", "country"], packs=("escalate_finding",)),
    _target("country_watch_ir", ["watch"]),
    _target("lane_suez", ["supply_chain"], sources=[{"source_id": "source.thinktank"}]),
]

PACKS = [{
    "family": "action_pack", "descriptor_id": "escalate_finding", "version": "d" * 64, "kind": None,
    "state": "active", "name": "Escalate Finding",
    "body": {"identity": {"id": "escalate_finding"},
             "tools": [{"name": "escalate", "impl": None, "async_job": False}],
             "channels": [{"kind": "alert", "name": "escalations",
                           "config": {"subject": "channels.escalations"}}]},
}]

STACK = {
    "llm.primary.openai_compat": {"model": "fast", "state": "active", "max_tokens": 16384},
    "llm.anthropic.opus_4_7": {"model": "claude-opus-5-5", "state": "active", "max_tokens": 8192},
}


def _rows(*extra):
    return [DESK, COMPOSITION, VOICE, JOURNAL, LENS_PAUSED, DETERMINISTIC, CONSULT, RETIRED,
            *SOURCES, *TARGETS, *PACKS, *extra]


def _graph(*extra):
    return fg.derive_graph(copy.deepcopy(_rows(*extra)), stack_models=STACK)


def _edges(g, basis_prefix):
    return {(e["source"], e["target"]) for e in g["edges"] if e["basis"].startswith(basis_prefix)}


# ---------------------------------------------------------------------------
# derivation
# ---------------------------------------------------------------------------


def test_only_active_and_paused_heads_are_drawn():
    g = _graph()
    ids = {n["id"] for n in g["nodes"]}
    assert "analyst:lens_left" in ids
    assert next(n for n in g["nodes"] if n["id"] == "analyst:lens_left")["state"] == "paused"
    assert "analyst:country_assessor" not in ids  # retired
    assert "source:source.paused" in ids


def test_every_edge_names_its_basis_and_relation():
    g = _graph()
    assert g["edges"]
    for e in g["edges"]:
        assert e["basis"] and e["relation"] and e["source"] and e["target"]
        assert e["id"] == f"{e['source']}->{e['target']}#{e['basis']}"


def test_subscription_other_analysts_edges():
    g = _graph()
    assert _edges(g, "subscription.other_analysts") == {
        ("analyst:escalation", "analyst:country_composition"),
        ("analyst:military_posture", "analyst:country_composition"),
        ("analyst:country_composition", "analyst:country_assessment"),
    }


def test_reference_to_a_missing_head_becomes_a_placeholder_not_a_crash():
    g = _graph()
    mp = next(n for n in g["nodes"] if n["id"] == "analyst:military_posture")
    assert mp["kind"] == "unresolved"
    assert any("no active or paused analyst head" in u for u in mp["undeclared"])
    assert g["report"]["unresolved_refs"][0]["id"] == "analyst:military_posture"


def test_predicate_binds_targets_to_analysts():
    g = _graph()
    binds = _edges(g, "subscription.targets.predicate")
    assert ("target:country_g20_cn", "analyst:escalation") in binds
    assert ("target:country_watch_ir", "analyst:escalation") in binds
    assert ("target:lane_suez", "analyst:escalation") not in binds
    e = next(e for e in g["edges"] if e["source"] == "target:country_g20_cn" and e["target"] == "analyst:escalation")
    assert e["data_types"] == ["signal"] and e["relation"] == "binds"


def test_selector_resolves_open_sources_only_with_runtime_semantics():
    g = _graph()
    feeds = _edges(g, "target.sources")
    into_cn = {s for s, t in feeds if t == "target:country_g20_cn"}
    # news+rss, open, shared tenant: both wires and the paused one; never the locked one.
    assert into_cn == {"source:source.wire.one", "source:source.wire.two", "source:source.paused"}
    assert ("source:source.thinktank", "target:lane_suez") in feeds
    explicit = next(e for e in g["edges"] if e["target"] == "target:lane_suez")
    assert explicit["basis"] == "target.sources[0].source_id"


def test_tools_grounding_grants_and_sinks():
    g = _graph()
    assert ("analyst:escalation", "pack:escalate_finding") in _edges(g, "action_packs")
    assert ("target:country_g20_cn", "pack:escalate_finding") in _edges(g, "target.allowed_action_packs")
    assert ("pack:escalate_finding", "sink:alert:escalations") in _edges(g, "action_pack.channels")
    assert ("store:grounding:substrate", "analyst:escalation") in _edges(g, "grounding.sources")
    assert ("analyst:country_assessment", "sink:a2a_skill:intelligence.country") in _edges(g, "outputs")
    pack = next(n for n in g["nodes"] if n["id"] == "pack:escalate_finding")
    assert pack["tools"] == [{"name": "escalate", "impl": None, "async_job": False}]
    # journal_read is offered but not an active head here → placeholder.
    assert next(n for n in g["nodes"] if n["id"] == "pack:journal_read")["kind"] == "unresolved"


def test_undeclared_relations_are_listed_not_drawn():
    g = _graph()
    by = {n["id"]: n for n in g["nodes"]}
    assert fg.UNDECLARED_CONSUMERS in by["analyst:country_assessment"]["undeclared"]  # a2a sink ≠ consumer
    assert fg.UNDECLARED_CONSUMERS not in by["analyst:escalation"]["undeclared"]  # composition subscribes
    assert any("method.sub_handler names code (signal_summarizer)" in u
               for u in by["analyst:signal_summarizer"]["undeclared"])
    assert any("prompt text lives in code" in u for u in by["analyst:journal_assessor"]["undeclared"])
    assert fg.UNDECLARED_STORE_WRITER in by["store:grounding:substrate"]["undeclared"]
    # Nothing in the graph claims a store an analyst writes.
    assert not any(e["relation"] == "writes" for e in g["edges"])
    inex = {r["relation"]: r["nodes"] for r in g["report"]["inexpressible"]}
    assert inex["analyst → store written (tables, side tables, indexes)"] == 7
    assert inex["deterministic handler → what it reads"] == 1


def test_unknown_family_kind_and_non_object_body_survive():
    weird = [
        {"family": "wiring", "descriptor_id": "w1", "version": "e" * 64, "kind": None, "state": "active",
         "name": "w", "body": {"anything": [1, 2, {"deep": True}]}},
        _analyst("future_thing", "kind_from_the_future", llm={"primary": {"raw": 3}}),
        {"family": "analyst", "descriptor_id": "broken", "version": "f" * 64, "kind": "x",
         "state": "active", "name": None, "body": "not json at all"},
        {"family": "analyst", "descriptor_id": "listy", "version": "f" * 64, "kind": "x",
         "state": "paused", "name": "l", "body": [1, 2, 3]},
    ]
    g = _graph(*weird)
    by = {n["id"]: n for n in g["nodes"]}
    assert by["wiring:w1"]["kind"] == "unknown"
    assert by["wiring:w1"]["fields"] == {"anything": [1, 2, {"deep": True}]}
    assert g["report"]["unknown_family_nodes"] == 1
    assert by["analyst:future_thing"]["group"] == "analyst:kind_from_the_future"
    assert by["analyst:broken"]["fields"] == "not json at all"
    assert by["analyst:listy"]["fields"] == [1, 2, 3]


def test_unevaluable_predicate_is_reported_not_raised():
    bad = _analyst("bad_pred", "inline_target", targets={"predicate": "has_tag(", "data_types": []})
    g = _graph(bad)
    assert any(p["analyst"] == "analyst:bad_pred" for p in g["report"]["predicate_errors"])
    assert not any(e["target"] == "analyst:bad_pred" for e in g["edges"])


def test_derivation_is_deterministic_and_order_independent():
    a = fg.derive_graph(copy.deepcopy(_rows()), stack_models=STACK)
    b = fg.derive_graph(list(reversed(copy.deepcopy(_rows()))), stack_models=STACK)
    assert json.dumps(a, sort_keys=True, default=str) == json.dumps(b, sort_keys=True, default=str)


def test_node_carries_route_cadence_scope_and_full_fields():
    n = next(n for n in _graph()["nodes"] if n["id"] == "analyst:escalation")
    assert n["model_route"]["primary"] == {"component_id": "llm.primary.openai_compat", "model": "fast",
                                           "component_state": "active"}
    assert n["cadence"]["schedule"] == "0 7,19 * * *"
    assert n["target_scope"]["predicate"].startswith("has_tag")
    assert n["fields"]["method"]["system_prompt"].startswith("WHO YOU ARE")
    assert n["version"] == "a" * 64 and n["group"] == "analyst:inline_target"


def test_scopes():
    g = _graph()
    fam = fg.apply_scope(g, "family:analyst:meta_findings_synthesizer")
    core = {n["id"] for n in fam["nodes"] if not n["context"]}
    assert core == {"analyst:country_composition", "analyst:country_assessment"}
    assert "analyst:escalation" in {n["id"] for n in fam["nodes"]}  # neighbour, as context
    chain = {n["id"] for n in fg.apply_scope(g, "target:country_g20_cn")["nodes"]}
    assert {"source:source.wire.one", "target:country_g20_cn", "analyst:escalation",
            "analyst:country_composition", "analyst:country_assessment",
            "pack:escalate_finding", "sink:alert:escalations"} <= chain
    assert "target:lane_suez" not in chain
    nb = {n["id"] for n in fg.apply_scope(g, "node:analyst:country_composition", depth=1)["nodes"]}
    assert nb >= {"analyst:escalation", "analyst:country_assessment", "target:country_g20_cn"}
    assert "sink:a2a_skill:intelligence.country" not in nb
    for bad in ("nope:x", "target:missing", "family:none", "node:none"):
        with pytest.raises(ValueError):
            fg.apply_scope(g, bad)


# ---------------------------------------------------------------------------
# parity with the runtime's own code
# ---------------------------------------------------------------------------


def test_selector_match_parity_with_runtime_sourceref():
    from legba.data.schemas.source import SourceSelector
    from legba.runtime.subscription.sourceref import SourceRow, _scope_match

    sels = [{"tags": ["news"]}, {"tags": ["news", "wire"], "kinds": ["rss"]}, {"geo": ["CN"]},
            {"languages": ["fr"]}, {"owner_tenant": "default"}, {}]
    rows = [("a", "rss", "shared", ["news", "wire"], ["CN"], ["en"]),
            ("b", "json_api", "default", ["news"], [], ["fr"]),
            ("c", "rss", "other", ["news"], [], ["en"])]
    for s in sels:
        sel = SourceSelector(**s)
        for sid, kind, tenant, tags, geo, langs in rows:
            row = SourceRow(source_id=sid, kind=kind, state="active", owner_tenant=tenant, scope_tags=tags,
                            scope_geo=geo, scope_languages=langs, subscription_policy="open",
                            allowed_targets=[], allowed_tenants=[])
            mine = fg.source_scope_match(s, {"kind": kind, "owner_tenant": tenant, "tags": tags,
                                             "geo": geo, "languages": langs}, target_tenant="default")
            assert mine == _scope_match(sel, row, target_tenant="default"), (s, sid)


def test_subprovider_rule_parity_with_runtime():
    from legba.runtime.analyst_deps_builder import infer_llm_subprovider

    for cid in ("llm.primary.openai_compat", "llm.anthropic.opus_4_7", "llm.decide.gateway_typed_choice",
                "llm.openai.gpt", "llm.verify.gateway_judge.openai_compat", "x.y"):
        assert fp.infer_subprovider(cid) == infer_llm_subprovider(cid, endpoint=None), cid


def test_code_facts_match_the_code_they_mirror():
    from legba.data.stack.llm.anthropic import AnthropicProviderHandler
    from legba.data.stack.llm.vllm import VLLMProviderHandler

    assert VLLMProviderHandler._DEFAULT_TEMPERATURE == fp.VLLM_DEFAULT_TEMPERATURE
    assert tuple(AnthropicProviderHandler.TEMPERATURE_DEPRECATED_PREFIXES) == fp.ANTHROPIC_TEMPERATURE_DROPPED_PREFIXES
    vsrc = (_SRC / "legba/data/stack/llm/vllm.py").read_text()
    from legba.data.analysts import _llm_budget as budget
    from legba.data.stack.llm import vllm

    # The off switch the basis names, and the input-budget arithmetic it mirrors.
    assert vllm.SEND_MAX_TOKENS_ENV == "LEGBA_LLM_SEND_MAX_TOKENS" and "_send_switched_off()" in vsrc
    assert fp.INPUT_BUDGET_DEFAULT == budget.DEFAULT_INPUT_TOKEN_BUDGET
    assert fp.INPUT_BUDGET_OUTPUT_RESERVE_MAX_SHARE == budget.OUTPUT_RESERVE_MAX_SHARE
    assert fp.INPUT_BUDGET_MARGIN == budget.DERIVED_MARGIN_TOKENS
    for sites in fp.HARD_CODED_TEMPERATURE.values():
        for _leg, val, path in sites:
            assert val == 1.0
            assert re.search(r"temperature=HOUSE_TEMPERATURE\b", (_SRC / "legba" / path).read_text()), path
    assert "temperature=HOUSE_TEMPERATURE" in (_SRC / "legba/data/provenance/judge_transport.py").read_text()
    deps = (_SRC / "legba/runtime/analyst_deps_builder.py").read_text()
    assert '_read_method_llm_option(descriptor, "temperature", default=HOUSE_TEMPERATURE)' in deps
    assert "temperature: float = HOUSE_TEMPERATURE" in (_SRC / "legba/data/analysts/consult_on_demand.py").read_text()


# ---------------------------------------------------------------------------
# configured vs sent
# ---------------------------------------------------------------------------


def test_vllm_route_temperature_sent_max_tokens_not_sent():
    p = fp.sampling_params("escalation", DESK["body"], STACK)
    assert p["configured"]["max_tokens"] == 4096 and p["configured"]["temperature"] == 1.0
    primary = next(s for s in p["sent"] if s["role"] == "primary")
    assert primary["subprovider"] == "vllm" and primary["model"] == "fast"
    assert primary["temperature_sent"] == 1.0 and primary["max_tokens_sent"] is None
    assert "NOT SENT" in primary["max_tokens_basis"]
    verify = next(s for s in p["sent"] if s["role"] == "verify")
    assert verify["temperature_sent"] == 1.0 and "judge_transport" in verify["temperature_basis"]
    assert p["cannot_see"]


def test_kind_default_and_hard_coded_sites():
    j = fp.sampling_params("journal_assessor", JOURNAL["body"], STACK)
    assert j["sent"][0]["temperature_sent"] == 1.0 and "kind default" in j["sent"][0]["temperature_basis"]
    d = fp.sampling_params("signal_summarizer", DETERMINISTIC["body"], STACK)
    assert d["hard_coded"] == [{"leg": "summary", "temperature_sent": 1.0,
                                "file": "data/analysts/deterministic_handlers/signal_summarizer.py"}]
    assert d["sent"][0]["temperature_sent"] == 1.0  # handler default when the descriptor sets none


def test_anthropic_route_drops_temperature_and_sends_max_tokens():
    c = fp.sampling_params("consult_default", CONSULT["body"], STACK)
    s = c["sent"][0]
    assert s["subprovider"] == "anthropic" and s["model"] == "claude-opus-5-5"
    assert s["temperature_sent"] is None and s["temperature_basis"].startswith("DROPPED")
    assert s["max_tokens_sent"] == 128_000  # the model's maximum, not the descriptor's 32768 (no output caps)


# ---------------------------------------------------------------------------
# run shaping
# ---------------------------------------------------------------------------

_T0 = datetime(2026, 11, 13, 6, 57, 35, tzinfo=timezone.utc)
RUN_ID = uuid.UUID("1a816a54-0f60-4c1e-a8ec-9e8797d81b7e")
OUT_ID = uuid.UUID("2b816a54-0f60-4c1e-a8ec-9e8797d81b7e")

TRACE = {
    "run_id": RUN_ID, "analyst_id": "escalation", "analyst_version": "a" * 64,
    "target_id": "country_g20_cn", "cadence_trigger": "coalesced_fire", "status": "success",
    "run_started_at": _T0, "run_ended_at": _T0 + timedelta(seconds=23),
    "receipt_hash": "r1", "prev_receipt_hash": "r0", "prompt_sha256": "p" * 64,
    "intermediate_steps": json.dumps([
        {"kind": "envelope", "phase": "wake"},
        {"kind": "deterministic", "phase": "orient", "in_count": 53, "slice_coverage": {"read": 53}},
        {"kind": "inject_preamble", "phase": "ground", "preamble_text": "A" * 900},
        {"kind": "llm_call", "phase": "reason", "tokens": 27929},
    ]),
    "llm_calls": json.dumps([
        {"at": "2026-11-13T06:57:58Z", "model": "fast", "status": "success", "duration_ms": 22394,
         "component_id": "llm.primary.openai_compat", "prompt_tokens": 26626, "completion_tokens": 1303,
         "total_tokens": 27929, "finish_reason": "stop", "completion_text": "{\"title\": \"secret body\"}"},
        {"at": "2026-11-13T06:57:59Z", "leg": "verify_judge", "model": "gemma",
         "component_id": "llm.verify.gateway_judge.openai_compat", "total_tokens": 4749,
         "completion_tokens": 184, "finish_reason": "length", "completion_text": "x"},
    ]),
    "tool_calls": json.dumps([{"at": "2026-11-13T06:57:50Z", "name": "escalate", "pack": "escalate_finding",
                               "status": "completed", "args": {"severity": "high"}, "duration_ms": 12}]),
    "input_row_refs": [uuid.uuid4() for _ in range(60)],
    "output_row_refs": [OUT_ID],
    "prompt_rendered": "[SYSTEM]\nYou are a senior all-source intelligence analyst." + "x" * 100,
    "input_payload": json.dumps({"prompt_tail": {"text": "tail", "chars": 4, "total_chars": 95966,
                                                 "sha256": "p" * 64}}),
    "output_payload": json.dumps({"big": "y" * 70_000}),
    "error_payload": None,
    "input_payload_bytes": 120, "output_payload_bytes": 70_020,
}
OUTPUT_ROW = {"table": "analyst_outputs", "id": OUT_ID, "kind": "finding", "title": "t",
              "schema_uri": "iglu:legba/finding/jsonschema/1-0-0", "produced_at": _T0, "bytes": 900,
              "honesty_flags": None,
              "data": json.dumps({"body": "b", "data": {"level_indicator_demotions": [], "citations": [1]}})}
CRITIQUE_ROW = {"id": uuid.uuid4(), "produced_at": _T0, "analyzed_output_id": str(OUT_ID),
                "data": json.dumps({"scores": {"faithfulness": 0.9}, "overall_score": 0.9,
                                    "judge_route": "gemma", "data": {"verification": {"verdicts": 3}}})}


def test_run_detail_is_sizes_and_ids_not_payloads():
    d = fr.run_detail(TRACE, [OUTPUT_ROW], [CRITIQUE_ROW])
    assert d["duration_ms"] == 23000 and d["tokens"] == 27929 + 4749
    assert d["sequence_label"] == "observed, not declared"
    assert [s["kind"] for s in d["steps"]] == ["envelope", "deterministic", "inject_preamble", "llm_call"]
    assert d["steps"][1]["slice_coverage"] == {"_bytes": len(json.dumps({"read": 53}))}
    assert d["steps"][2]["preamble_text"].endswith("[900 chars]")
    assert d["llm_calls"][0]["completion_chars"] == len("{\"title\": \"secret body\"}")
    assert "secret body" not in json.dumps(d)
    assert d["llm_calls"][1]["finish_reason"] == "length"
    assert d["tool_calls"][0]["args_bytes"] > 0 and "high" not in json.dumps(d["tool_calls"])
    assert d["input_refs"]["count"] == 60 and len(d["input_refs"]["ids"]) == fr.INPUT_REF_LIST_CAP
    out = d["outputs"][0]
    assert out["gates"] == {"data.data.level_indicator_demotions": []}
    assert out["judge_critiques"][0]["data.scores"] == {"faithfulness": 0.9}
    assert out["judge_critiques"][0]["data.data.verification"] == {"verdicts": 3}


def test_body_part_is_capped_and_bounds_checked():
    b = fr.body_part(TRACE, "output_payload", None)
    assert b["truncated"] and len(b["text"].encode()) <= fr.BODY_CAP_BYTES and b["bytes"] > fr.BODY_CAP_BYTES
    s = fr.body_part(TRACE, "step", 2)
    assert not s["truncated"] and "preamble_text" in s["text"]
    assert fr.body_part(TRACE, "prompt_tail", None)["text"].count("95966") == 1
    with pytest.raises(IndexError):
        fr.body_part(TRACE, "llm_call", 9)
    with pytest.raises(KeyError):
        fr.body_part(TRACE, "body_of_everything", None)


def test_run_summary_and_observed_calls():
    s = fr.run_summary({**TRACE, "n_tool_calls": 1})
    assert s["tokens"] == 27929 + 4749 and s["finish_reasons"] == {"stop": 1, "length": 1}
    obs = fr.observed_calls([TRACE, TRACE])
    judge = next(o for o in obs if o["leg"] == "verify_judge")
    assert judge["calls"] == 2 and judge["finish_reasons"] == {"length": 2}


# ---------------------------------------------------------------------------
# routes through ASGI
# ---------------------------------------------------------------------------


class FakeConn:
    """Answers each query by its ``/* flow:<name> */`` tag."""

    def __init__(self):
        self.seen: list[str] = []

    def _tag(self, sql):
        m = re.search(r"/\* flow:(\w+) \*/", sql)
        assert m, sql
        self.seen.append(m.group(1))
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|CREATE|DROP|ALTER)\b", sql, re.I), sql
        return m.group(1)

    async def fetch(self, sql, *args):
        tag = self._tag(sql)
        if tag == "graph_rows":
            return [dict(r, body=json.dumps(r["body"])) for r in _rows() if r["state"] in ("active", "paused")]
        if tag == "stack":
            return [{"component_id": k, "state": v["state"], "model": v["model"],
                     "max_tokens": str(v["max_tokens"]), "timeout_seconds": "240"} for k, v in STACK.items()]
        if tag == "versions":
            return [{"version": "a" * 64, "state": "active", "is_head": True, "created_at": _T0},
                    {"version": "9" * 64, "state": "retired", "is_head": False, "created_at": _T0 - timedelta(days=3)}]
        if tag == "recent_runs":
            return [dict(TRACE, n_tool_calls=1)]
        if tag == "output_rows":
            return [OUTPUT_ROW]
        if tag == "critiques":
            return [CRITIQUE_ROW]
        raise AssertionError(tag)

    async def fetchrow(self, sql, *args):
        tag = self._tag(sql)
        if tag == "latest_prompt":
            return {"run_id": RUN_ID, "run_started_at": _T0, "analyst_version": "a" * 64,
                    "prompt_rendered": TRACE["prompt_rendered"], "prompt_sha256": "p" * 64,
                    "input_payload": json.dumps({"prompt_tail": {"text": "tail", "total_chars": 95966}}),
                    "bulk_bundle_id": None, "bulk_columns": None}
        if tag == "latest_steps":
            return {"run_id": RUN_ID, "run_started_at": _T0, "status": "success",
                    "intermediate_steps": TRACE["intermediate_steps"], "output_row_refs": [OUT_ID]}
        if tag in ("run", "run_body"):
            return dict(TRACE, bulk_bundle_id=None, bulk_columns=None) if args[0] == RUN_ID else None
        if tag == "version_body":
            return ({"descriptor_id": args[0], "version": args[1], "state": "retired", "is_head": False,
                     "created_at": _T0, "body": json.dumps({"identity": {"id": args[0]}, "old": True})}
                    if args[1] == "9" * 64 else None)
        raise AssertionError(tag)


class _Pool:
    def __init__(self, conn):
        self.conn = conn

    @asynccontextmanager
    async def acquire(self):
        yield self.conn


class _Deps:
    def __init__(self, conn):
        self.descriptor_registry = type("R", (), {"pg": _Pool(conn)})()


async def _get(path, headers=None, conn=None):
    app = FastAPI()
    app.include_router(build_flow_router(_Deps(conn or FakeConn())), prefix="/api/v1/v3")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        return await c.get("/api/v1/v3" + path, headers=headers or {})


@pytest.mark.asyncio
async def test_graph_route(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    r = await _get("/flow/graph")
    assert r.status_code == 200, r.text
    body = r.json()
    assert {"nodes", "edges", "report", "scope", "timings_ms"} <= set(body)
    assert not any("position" in n or "x" in n for n in body["nodes"])  # no layout in the payload
    lite = (await _get("/flow/graph?fields=false&scope=family:analyst:inline_target")).json()
    assert all(n["fields"] is None for n in lite["nodes"]) and lite["scope"] == "family:analyst:inline_target"
    assert (await _get("/flow/graph?scope=bogus:1")).status_code == 400


@pytest.mark.asyncio
async def test_node_route_for_an_analyst(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    r = await _get("/flow/node/analyst:escalation")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["node"]["id"] == "analyst:escalation" and b["edges_in"] and b["edges_out"]
    assert [v["state"] for v in b["versions"]] == ["active", "retired"]
    assert b["prompt"]["declared"]["system_prompt"].startswith("WHO YOU ARE")
    assert b["prompt"]["rendered"]["text"].startswith("[SYSTEM]") and b["prompt"]["rendered"]["total_chars"] == 95966
    assert "most recent run" in b["prompt"]["rendered"]["basis"]
    assert b["output_contract"]["observed_rows"][0]["schema_uri"] == "iglu:legba/finding/jsonschema/1-0-0"
    assert b["params"]["sent"][0]["max_tokens_sent"] is None and b["params"]["observed"]["calls"]
    assert b["recent_runs"][0]["run_id"] == str(RUN_ID)
    assert b["observed_steps"]["label"] == "observed, not declared"
    assert (await _get("/flow/node/analyst:nobody")).status_code == 404


@pytest.mark.asyncio
async def test_node_route_for_a_synthetic_node_and_a_version(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    s = (await _get("/flow/node/store:grounding:substrate")).json()
    assert s["node"]["kind"] == "store" and "versions" not in s and len(s["edges_out"]) >= 2
    v = await _get(f"/flow/version/analyst:escalation?version={'9' * 64}")
    assert v.status_code == 200 and v.json()["fields"]["old"] is True
    assert (await _get(f"/flow/version/analyst:escalation?version={'8' * 64}")).status_code == 404
    assert (await _get("/flow/version/sink:alert:x?version=abcdef")).status_code == 404


@pytest.mark.asyncio
async def test_run_routes(monkeypatch):
    monkeypatch.delenv(API_TOKEN_ENV, raising=False)
    r = await _get(f"/flow/run/{RUN_ID}")
    assert r.status_code == 200 and r.json()["outputs"][0]["judge_critiques"]
    assert (await _get("/flow/run/not-a-uuid")).status_code == 400
    assert (await _get(f"/flow/run/{uuid.uuid4()}")).status_code == 404
    b = await _get(f"/flow/run/{RUN_ID}/body?part=llm_call&index=0")
    assert b.status_code == 200 and "secret body" in b.json()["text"]  # the capped body fetch is the one place
    assert (await _get(f"/flow/run/{RUN_ID}/body?part=llm_call&index=7")).status_code == 404
    assert (await _get(f"/flow/run/{RUN_ID}/body?part=nope")).status_code == 400


@pytest.mark.asyncio
async def test_routes_are_bearer_gated(monkeypatch):
    monkeypatch.setenv(API_TOKEN_ENV, "nj-flow-test-token")
    for path in ("/flow/graph", "/flow/node/analyst:escalation", f"/flow/run/{RUN_ID}"):
        assert (await _get(path)).status_code == 401, path
    ok = await _get("/flow/graph", {"Authorization": "Bearer nj-flow-test-token"})
    assert ok.status_code == 200


def test_server_wires_the_router():
    src = (_SRC / "legba" / "data" / "registry" / "server.py").read_text()
    assert 'build_flow_router(deps), prefix="/api/v1/v3"' in src


def test_flow_routes_import_without_the_runtime_stack():
    """The registry image is SLIM: the routes must import with the runtime-only
    packages poisoned, and must not reach analyst or runtime modules."""
    probe = textwrap.dedent(
        """
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client", "dapr", "temporalio", "dspy"):
            sys.modules[blocked] = None
        import legba.data.registry.flow_api as api
        import legba.data.registry.flow_graph as g
        g.derive_graph([{"family": "analyst", "descriptor_id": "a", "state": "active",
                         "body": {"subscription": {"targets": {"predicate": 'has_tag("x")'}}}},
                        {"family": "target", "descriptor_id": "t", "state": "active",
                         "body": {"scope": {"tags": ["x"]}}}])
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.data.analysts") or m.startswith("legba.runtime"))
        assert not leaked, leaked
        assert api.build_flow_router is not None
        print("OK")
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, timeout=120,
        env={**os.environ, "PYTHONPATH": os.pathsep.join(
            p for p in (str(_SRC), os.environ.get("PYTHONPATH", "")) if p)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK" in result.stdout

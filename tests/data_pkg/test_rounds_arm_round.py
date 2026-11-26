# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""``scripts/bakeoff/arm_round.py`` without a network.

The route assertion refuses a priced, an Anthropic and a non-gateway plane
before any call; the thinking alias is overlaid on the READ row only (a GET,
nothing written); the run root must be under the gitignored ``planning/``;
the call ledger refuses past its cap; the blind-seal check and the probe
summary read a record the way the report does.
"""
from __future__ import annotations

import asyncio
import pathlib
import sys
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))
sys.path.insert(0, str(ROOT / "src"))

import arm_round  # noqa: E402

CORE = arm_round.CORE_COMPONENT
JUDGE = "llm.verify.gateway_judge.openai_compat"
GATEWAY = "gateway.example.invalid"


@pytest.fixture(autouse=True)
def _gateway_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(arm_round.GATEWAY_ENDPOINT_ENV, f"https://{GATEWAY}/v1")


def _facts(**kw: Any) -> dict[str, Any]:
    base = {"subprovider": "vllm", "model_name": "m", "thinking_model_name": None,
            "endpoint_host": GATEWAY, "component_price": None,
            "config_price_fields": {}, "price_table_entries": 0}
    base.update(kw)
    return base


def test_routes_pass_when_unpriced_gateway_and_core():
    out = arm_round.assert_routes({CORE: _facts(), JUDGE: _facts()})
    assert out[CORE]["component_unpriced"] and out[JUDGE]["endpoint_is_gateway"]


@pytest.mark.parametrize("comp,bad", [
    (CORE, {"config_price_fields": {"price_input_per_m": 3.0}}),
    (CORE, {"subprovider": "anthropic"}),
    (CORE, {"price_table_entries": 2}),
    (JUDGE, {"component_price": "ModelPrice(1, 1)"}),
    (JUDGE, {"endpoint_host": "api.anthropic.com"}),
    ("llm.anthropic.opus", {}),
])
def test_routes_refuse_priced_anthropic_or_off_gateway(comp, bad):
    facts = {CORE: _facts(), comp: _facts(**bad)}
    with pytest.raises(SystemExit, match="ROUTE ASSERTION FAILED"):
        arm_round.assert_routes(facts)


def test_the_gateway_host_comes_from_the_core_endpoint_and_fails_closed(monkeypatch):
    assert arm_round.gateway_host() == GATEWAY
    monkeypatch.delenv(arm_round.GATEWAY_ENDPOINT_ENV)
    assert arm_round.gateway_host() == ""
    with pytest.raises(SystemExit, match="ROUTE ASSERTION FAILED"):
        arm_round.assert_routes({CORE: _facts(), JUDGE: _facts()})


def test_a_decision_plane_is_held_to_the_same_route_checks():
    dec = "llm.decide.gateway_typed_choice"
    out = arm_round.assert_routes({CORE: _facts(), dec: _facts(subprovider="decide")})
    assert all(out[dec].values())
    with pytest.raises(SystemExit, match="ROUTE ASSERTION FAILED"):
        arm_round.assert_routes({CORE: _facts(), dec: _facts(subprovider="vllm")})
    with pytest.raises(SystemExit, match="ROUTE ASSERTION FAILED"):
        arm_round.assert_routes({CORE: _facts(), dec: _facts(subprovider="decide",
                                                             config_price_fields={"price_input_per_m": 1})})
    # a handler without a component price method still yields facts
    h = SimpleNamespace(subprovider="decide", _cfg=None, PRICE_TABLE={})
    assert arm_round.route_facts(dec, h)["component_price"] is None


def test_root_must_be_under_planning(tmp_path):
    with pytest.raises(SystemExit, match="under"):
        arm_round._under_planning(tmp_path)
    assert arm_round._under_planning(ROOT / "planning" / "x").name == "x"


def test_call_ledger_refuses_past_its_cap(tmp_path):
    led = arm_round.CallLedger(tmp_path / "calls.json", 2)
    assert [led.take(), led.take(), led.take()] == [True, True, False]
    assert arm_round.CallLedger(tmp_path / "calls.json", 2).used() == 2


def test_overlay_adds_the_thinking_alias_to_the_read_row_only():
    calls: list[str] = []
    row = {"body": {"config": {"model_name": {"raw": "m"}}}}

    class Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"body": {"config": dict(row["body"]["config"])}}

    class Client:
        async def get(self, path, **kw):
            calls.append(path)
            return Resp()

    inner = SimpleNamespace(_api_prefix="/api/v1/registry", _headers=dict,
                            _ensure_client=lambda: asyncio.sleep(0, Client()))
    reg = arm_round.OverlayRegistry(inner, CORE, "alias-x")

    async def go(path):
        c = await reg._ensure_client()
        return (await c.get(path)).json()

    got = asyncio.run(go(f"/api/v1/registry/stack/{CORE}"))
    assert got["body"]["config"]["thinking_model_name"] == {"factory_kind": "text", "raw": "alias-x"}
    other = asyncio.run(go(f"/api/v1/registry/stack/{JUDGE}"))
    assert "thinking_model_name" not in other["body"]["config"]
    assert row == {"body": {"config": {"model_name": {"raw": "m"}}}}  # the source is untouched
    assert not hasattr(Client, "put") and not hasattr(Client, "post")


def test_verify_blind_flags_a_sibling_handed_inside_a_blind_phase():
    rec = {"phases": [{"path": "main/open", "shape": "blind", "status": "closed"}],
           "steps": [{"key": "main/open/a#0", "path": "main/open", "status": "sealed",
                      "sha256": "x", "handed": []},
                     {"key": "main/open/b#0", "path": "main/open", "status": "sealed",
                      "sha256": "y", "handed": [{"ref": "main/open/a#0"}]}]}
    out = arm_round.verify_blind(rec)
    assert not out["verified"] and "handed sibling" in out["problems"][0]
    rec["steps"][1]["handed"] = []
    assert arm_round.verify_blind(rec)["verified"]


def test_probe_summary_pairs_and_tool_counts():
    res = []
    for i, size in enumerate((100, 200, 300, 400)):
        res.append({"prompt": f"p{i}", "thinking": False, "ok": True, "duration_s": 10.0,
                    "prompt_chars": size, "reasoning_tokens": 0})
        res.append({"prompt": f"p{i}", "thinking": True, "ok": True, "duration_s": 30.0,
                    "prompt_chars": size, "reasoning_tokens": 500})
    res.append({"prompt": "t", "thinking": True, "ok": True, "tools_offered": True,
                "tool_calls": ["search_signals"], "duration_s": 5, "prompt_chars": 1})
    s = arm_round.summarise_probe(res)
    assert s["paired"]["pairs"] == 4 and s["paired"]["on_over_off_p50"] == 3.0
    assert s["on/all"]["reasoning_tokens_p50"] == 500.0
    assert s["tools"]["on"]["with_tool_calls"] == 1

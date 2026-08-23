# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Per-component pricing + client concurrency on the vLLM-family handler
(#22 / #21, 2026-08-15).

The vllm handler serves every ``.openai_compat`` component — the self-hosted
primary AND the hosted PAYG judge lanes (Cerebras, OpenRouter) — through one
empty class-level ``PRICE_TABLE``, so every hosted call's receipt carried
``cost_estimate_usd: 0.0`` and the 2026-08-03 judge outage arrived as a
``402 payment_required`` nothing had been counting toward. Pricing is now
PER-COMPONENT (``price_input_per_m`` / ``price_output_per_m`` on the config).

The load-bearing assertions: a priced component stamps real dollars on its
receipts; an unpriced component is BYTE-IDENTICAL to before the fields
existed ($0.00, self-hosted posture); and ``max_concurrent`` actually bounds
in-flight wire calls while absent means today's unlimited behavior.
"""
from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from legba.data.registry.credentials import MissingSecretError
from legba.data.schemas import LLMProvider, LLMProviderConfig, Property
from legba.data.stack.llm.base import LLMProviderHandler
from legba.data.stack.llm.vllm import VLLMProviderHandler

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))


# ---------------------------------------------------------------------------
# Fakes (the test_stack_llm shapes, self-contained)
# ---------------------------------------------------------------------------


class _FakeResolver:
    def __init__(self, secrets: dict[str, bytes]):
        self._secrets = secrets

    async def verify_exists(self, secret_id: str) -> bool:
        return secret_id in self._secrets

    async def resolve(self, secret_id: str) -> bytes:
        if secret_id not in self._secrets:
            raise MissingSecretError(secret_id)
        return self._secrets[secret_id]


@dataclass
class _FakeCtx:
    instance_id: str
    instance_version: str
    config: LLMProviderConfig
    secrets: _FakeResolver

    def telemetry(self):
        class _T:
            def log(self, level, msg, /, **fields): ...
            def event(self, name, payload=None): ...
            def span(self, name, /, **attrs):
                class _S:
                    def __enter__(self):
                        return self

                    def __exit__(self, *exc):
                        return False

                return _S()

        return _T()


def _cfg(**extra: Any) -> LLMProviderConfig:
    return LLMProviderConfig(
        api_endpoint=Property.Text.of("https://api.cerebras.ai"),
        api_key=Property.Secret.of("test.api_key"),
        model_name=Property.Text.of("gemma-4-31b"),
        max_tokens=Property.Number.of(1024, minimum=1, maximum=200000),
        **extra,
    )


async def _configured(handler: VLLMProviderHandler, cfg: LLMProviderConfig):
    ctx = _FakeCtx(
        instance_id="llm.judge.cerebras_gemma4_31b.openai_compat",
        instance_version="0" * 16,
        config=cfg,
        secrets=_FakeResolver({"test.api_key": b"sk-test"}),
    )
    await handler.on_configure(ctx)
    return handler


def _vllm_body(prompt_tokens: int, completion_tokens: int) -> dict:
    return {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
        },
    }


# ---------------------------------------------------------------------------
# Per-component pricing
# ---------------------------------------------------------------------------


async def test_priced_component_stamps_real_dollars():
    """1M in at $0.99 + 0.5M out at $1.49 = $1.735 — the Cerebras lane's
    receipts finally carry the number the 402 was counting."""
    h = await _configured(
        VLLMProviderHandler(),
        _cfg(
            price_input_per_m=Property.Number.of(0.99),
            price_output_per_m=Property.Number.of(1.49),
        ),
    )
    resp = h._parse_response(  # noqa: SLF001
        _vllm_body(1_000_000, 500_000), model="gemma-4-31b"
    )
    assert resp.usage.cost_estimate_usd == pytest.approx(1.735)


async def test_unpriced_component_is_byte_identical_to_before():
    """No price fields = the self-hosted posture = $0.00, exactly the
    pre-#22 behavior (PRICE_TABLE is empty and stays empty)."""
    h = await _configured(VLLMProviderHandler(), _cfg())
    resp = h._parse_response(  # noqa: SLF001
        _vllm_body(1_000_000, 500_000), model="gemma-4-31b"
    )
    assert resp.usage.cost_estimate_usd == 0.0
    assert VLLMProviderHandler.PRICE_TABLE == {}


async def test_one_sided_price_costs_the_other_side_at_zero():
    """A free-input promo lane is a real thing; a half-priced receipt beats a
    silently unpriced one."""
    h = await _configured(
        VLLMProviderHandler(),
        _cfg(price_output_per_m=Property.Number.of(2.0)),
    )
    resp = h._parse_response(  # noqa: SLF001
        _vllm_body(1_000_000, 1_000_000), model="gemma-4-31b"
    )
    assert resp.usage.cost_estimate_usd == pytest.approx(2.0)


async def test_zero_price_is_a_price_not_an_absence():
    """The OpenRouter :free lanes pin 0/0 explicitly — the receipt costs
    $0.00 through the PRICED path, so flipping to a paid lane is a two-field
    registry PUT, not a code change."""
    h = await _configured(
        VLLMProviderHandler(),
        _cfg(
            price_input_per_m=Property.Number.of(0),
            price_output_per_m=Property.Number.of(0),
        ),
    )
    assert h._component_price() is not None  # noqa: SLF001
    resp = h._parse_response(  # noqa: SLF001
        _vllm_body(1_000_000, 1_000_000), model="whatever:free"
    )
    assert resp.usage.cost_estimate_usd == 0.0


def test_config_without_new_fields_still_validates():
    """Every existing registry row lacks the four new optional fields; they
    must parse unchanged (None), or a registry recreate bricks the fleet."""
    cfg = LLMProviderConfig.model_validate(
        {
            "api_endpoint": {"factory_kind": "text", "raw": "https://x"},
            "api_key": {"factory_kind": "secret", "raw": "k"},
            "model_name": {"factory_kind": "text", "raw": "m"},
            "max_tokens": {"factory_kind": "number", "raw": 100},
        },
        strict=False,
    )
    assert cfg.price_input_per_m is None
    assert cfg.price_output_per_m is None
    assert cfg.daily_burn_alert_usd is None
    assert cfg.max_concurrent is None


# ---------------------------------------------------------------------------
# Client concurrency (max_concurrent)
# ---------------------------------------------------------------------------


class _ConcurrencyProbe:
    """Counts overlapping in-flight calls through the base ``_call_chat``."""

    def __init__(self):
        self.current = 0
        self.peak = 0
        self.calls = 0

    async def record(self, payload):
        self.calls += 1
        self.current += 1
        self.peak = max(self.peak, self.current)
        await asyncio.sleep(0.01)
        self.current -= 1
        return _vllm_body(10, 5)

    def as_base_call(self):
        """A plain function (rebinds per-instance through the descriptor
        protocol, which a bound method would not)."""

        async def _call(handler_self, payload):
            return await self.record(payload)

        return _call


async def test_max_concurrent_bounds_inflight_wire_calls(monkeypatch):
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(
        LLMProviderHandler, "_call_chat", probe.as_base_call(), raising=True
    )
    h = await _configured(
        VLLMProviderHandler(), _cfg(max_concurrent=Property.Number.of(2))
    )
    await asyncio.gather(
        *(h.chat_complete([{"role": "user", "content": "q"}]) for _ in range(6))
    )
    assert probe.calls == 6
    assert probe.peak == 2


async def test_absent_max_concurrent_is_unlimited_as_today(monkeypatch):
    probe = _ConcurrencyProbe()
    monkeypatch.setattr(
        LLMProviderHandler, "_call_chat", probe.as_base_call(), raising=True
    )
    h = await _configured(VLLMProviderHandler(), _cfg())
    await asyncio.gather(
        *(h.chat_complete([{"role": "user", "content": "q"}]) for _ in range(6))
    )
    assert probe.calls == 6
    assert probe.peak == 6


# ---------------------------------------------------------------------------
# The tree payloads (bringup_register_stack)
# ---------------------------------------------------------------------------


def _bringup_components():
    import bringup_register_stack as brs

    return dict(brs.COMPONENTS)


def test_seeded_llm_payloads_validate_against_the_schema():
    """The judge lanes' pricing seeds and the primary's max_concurrent must
    parse against the typed model, or the registrar POST 422s on a fresh
    bringup."""
    comps = _bringup_components()
    for comp_id, body in comps.items():
        if not body["schema_uri"].startswith("legba/stack/llm_provider/"):
            continue
        payload = dict(body)
        payload["version"] = "0" * 16
        LLMProvider.model_validate(payload, strict=False)


def test_cerebras_lane_is_priced_and_ceilinged():
    cfg = _bringup_components()[
        "llm.judge.cerebras_gemma4_31b.openai_compat"
    ]["config"]
    assert cfg["price_input_per_m"]["raw"] == pytest.approx(0.99)
    assert cfg["price_output_per_m"]["raw"] == pytest.approx(1.49)
    assert cfg["daily_burn_alert_usd"]["raw"] == pytest.approx(10.0)


def test_openrouter_free_lanes_pin_zero_and_never_page():
    comps = _bringup_components()
    for comp_id in (
        "llm.judge.nemotron3_super.openai_compat",
        "llm.judge.nemotron3_ultra.openai_compat",
    ):
        cfg = comps[comp_id]["config"]
        assert cfg["price_input_per_m"]["raw"] == 0
        assert cfg["price_output_per_m"]["raw"] == 0
        # Absent ceiling = the burn gauge never pages a lane that cannot burn.
        assert "daily_burn_alert_usd" not in cfg
        assert cfg["model_name"]["raw"].endswith(":free")


def test_primary_carries_the_headroom_concurrency_default():
    cfg = _bringup_components()["llm.primary.openai_compat"]["config"]
    assert cfg["max_concurrent"]["raw"] == 12
    # Self-hosted: NO price fields — receipts stay $0.00.
    assert "price_input_per_m" not in cfg
    assert "price_output_per_m" not in cfg

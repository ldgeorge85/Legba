# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The runtime wiring of the contract-v2 desk shadow: OFF unless the
switch names the desk; ON only with both seat descriptors registered and the
unpriced core route; every refusal logged and the desk untouched."""
from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Any

from legba.data.desk_rows_shadow import QUESTION_TOKEN, SEAT_ID, SHADOW_ENV, DeskRowsShadow
from legba.data.statement_seats import SEAT_COMPONENT, VERIFIER_SEAT_ID
from legba.runtime.desk_rows_shadow_wiring import build_rows_shadow


def _descriptor(analyst_id: str = "energy_security", kind: str = "inline_target") -> Any:
    return SimpleNamespace(identity=SimpleNamespace(id=analyst_id, kind=kind),
                           method=SimpleNamespace(bounded_question="How much pressure?"))


class _Registry:
    def __init__(self, rows: dict[str, Any]) -> None:
        self.rows = rows
        self.asked: list[str] = []

    async def get_descriptor(self, descriptor_id: str, *, family: str, version: str | None = None):
        self.asked.append(descriptor_id)
        return self.rows.get(descriptor_id)


def _seat(prompt: str) -> dict[str, Any]:
    return {"body": {"method": {"system_prompt": prompt}}}


class _Raw:
    def __init__(self, raw: str) -> None:
        self.raw = raw


class _CoreLLM:
    PRICE_TABLE: dict[str, Any] = {}

    def __init__(self, model: str = "plain-alias", endpoint: str = "http://core.invalid/v1") -> None:
        self._cfg = SimpleNamespace(api_endpoint=_Raw(endpoint), model_name=_Raw(model))

    def _component_price(self):
        return None


def _build(registry: Any, llm: Any, descriptor: Any = None, pool: Any = "pool") -> Any:
    async def resolve(component_id: str) -> Any:
        assert component_id == SEAT_COMPONENT
        return llm
    return asyncio.run(build_rows_shadow(descriptor or _descriptor(), registry_client=registry,
                                         resolve_component=resolve, pg_pool=pool))


FULL = {SEAT_ID: _seat(f"rows for {QUESTION_TOKEN}"), VERIFIER_SEAT_ID: _seat("verify")}


def test_off_by_default_and_asks_nothing(monkeypatch):
    monkeypatch.delenv(SHADOW_ENV, raising=False)
    reg = _Registry(FULL)
    assert _build(reg, _CoreLLM()) is None and reg.asked == []
    monkeypatch.setenv(SHADOW_ENV, "escalation")
    assert _build(reg, _CoreLLM()) is None and reg.asked == []


def test_on_for_a_named_desk_with_the_seats_and_the_core_route(monkeypatch):
    monkeypatch.setenv(SHADOW_ENV, "energy_security")
    shadow = _build(_Registry(FULL), _CoreLLM())
    assert isinstance(shadow, DeskRowsShadow)
    assert shadow.prompts.rows_system == "rows for How much pressure?"
    assert shadow.prompts.verifier_system == "verify" and shadow.pg == "pool"


def test_every_refusal_is_logged_and_leaves_the_shadow_off(monkeypatch, caplog):
    monkeypatch.setenv(SHADOW_ENV, "energy_security")
    caplog.set_level(logging.WARNING)
    assert _build(_Registry({SEAT_ID: _seat("x")}), _CoreLLM()) is None
    assert _build(_Registry(FULL), _CoreLLM(), descriptor=_descriptor(kind="deterministic")) is None
    assert _build(_Registry(FULL), _CoreLLM(), pool=None) is None
    priced = _CoreLLM()
    priced._component_price = lambda: 1.0  # type: ignore[method-assign]
    assert _build(_Registry(FULL), priced) is None                    # not the unpriced core route
    reasons = [r.getMessage() for r in caplog.records if "desk_rows_shadow.unavailable" in r.getMessage()]
    assert len(reasons) == 4
    assert any("seat_descriptor_not_registered" in m for m in reasons)
    assert any("not_a_desk" in m for m in reasons) and any("CoreRouteError" in m for m in reasons)

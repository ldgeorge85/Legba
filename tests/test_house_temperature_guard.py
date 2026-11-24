# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The house-temperature guard (2026-11-13): temperature 1.0 on every model call.

Static half: no numeric temperature literal anywhere in ``src/legba`` outside
``legba/shared/sampling.py`` (call keywords, parameter defaults, dataclass
fields, ``*TEMPERATURE*`` constants, ``"temperature"`` dict entries, and
``default=`` of a ``"temperature"`` option read).

Wire half: the request body each client wrapper builds carries 1.0 (or, on the
Anthropic route, no temperature at all for the model lines that reject it).

Pure; no database.
"""
from __future__ import annotations

import ast
import asyncio
from pathlib import Path

import pytest

from legba.shared.sampling import (
    HOUSE_TEMPERATURE,
    SAMPLING_REGIME,
    validate_house_temperature,
)

_SRC = Path(__file__).resolve().parents[1] / "src" / "legba"
_ALLOWED = {_SRC / "shared" / "sampling.py"}


def _num(node: ast.AST | None) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, (int, float))
        and not isinstance(node.value, bool)
    )


def _is_temp_name(name: str) -> bool:
    return "temperature" in name.lower()


def _violations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(), filename=str(path))
    out: list[str] = []

    def bad(node: ast.AST, what: str) -> None:
        out.append(f"{path.relative_to(_SRC)}:{node.lineno} {what}")

    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg and _is_temp_name(node.arg) and _num(node.value):
            bad(node.value, f"{node.arg}= numeric literal")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            a = node.args
            pos = a.posonlyargs + a.args
            for arg, dflt in zip(pos[len(pos) - len(a.defaults):], a.defaults):
                if _is_temp_name(arg.arg) and _num(dflt):
                    bad(dflt, f"parameter {arg.arg} default numeric")
            for arg, dflt in zip(a.kwonlyargs, a.kw_defaults):
                if _is_temp_name(arg.arg) and _num(dflt):
                    bad(dflt, f"parameter {arg.arg} default numeric")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                nm = t.id if isinstance(t, ast.Name) else (t.attr if isinstance(t, ast.Attribute) else "")
                if nm and _is_temp_name(nm) and _num(node.value):
                    bad(node.value, f"{nm} = numeric literal")
        elif isinstance(node, ast.Dict):
            for k, v in zip(node.keys, node.values):
                if isinstance(k, ast.Constant) and k.value == "temperature" and _num(v):
                    bad(v, '"temperature": numeric literal')
        elif isinstance(node, ast.Call):
            # _read_method_llm_option(descriptor, "temperature", default=0.2) and kin
            if any(isinstance(a, ast.Constant) and a.value == "temperature" for a in node.args):
                for kw in node.keywords:
                    if kw.arg == "default" and _num(kw.value):
                        bad(kw.value, '"temperature" option default numeric')
    return out


def test_no_numeric_temperature_literal_in_src():
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        if path in _ALLOWED:
            continue
        found.extend(_violations(path))
    assert not found, (
        "a temperature literal bypasses the house constant "
        "(legba.shared.sampling.HOUSE_TEMPERATURE):\n" + "\n".join(found)
    )


def test_the_house_constant_is_one():
    assert HOUSE_TEMPERATURE == 1.0
    assert SAMPLING_REGIME == "temperature-1.0"


def test_the_constant_module_is_stdlib_only_leaf():
    tree = ast.parse((_SRC / "shared" / "sampling.py").read_text())
    mods = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods |= {a.name.split(".")[0] for a in n.names}
        elif isinstance(n, ast.ImportFrom):
            mods.add((n.module or "").split(".")[0])
    assert mods <= {"__future__", "typing"}


def test_validate_house_temperature():
    assert validate_house_temperature(1) == 1.0
    assert validate_house_temperature("1.0") == 1.0
    for badv in (0.0, 0.2, 0.7, "abc"):
        with pytest.raises(ValueError, match="temperature"):
            validate_house_temperature(badv, where="method.llm.temperature")


# ---------------------------------------------------------------------------
# Descriptor save refuses anything but 1.0
# ---------------------------------------------------------------------------


def _method(llm: dict):
    from legba.data.schemas.analyst import MethodBlock

    return MethodBlock(kind="deterministic", impl="x:y", llm=llm)


def test_descriptor_method_llm_temperature_other_than_one_is_refused():
    _method({})
    _method({"temperature": 1.0})
    for t in (0.0, 0.2):
        with pytest.raises(ValueError, match="not allowed"):
            _method({"temperature": t})
        with pytest.raises(ValueError, match="narrate.temperature"):
            _method({"narrate": {"temperature": t}})


def test_every_shipped_descriptor_declares_only_the_house_temperature():
    import yaml

    root = Path(__file__).resolve().parents[1] / "descriptors"
    for p in sorted(root.glob("*.yaml")):
        doc = yaml.safe_load(p.read_text())
        llm = ((doc or {}).get("method") or {}).get("llm") or {} if isinstance(doc, dict) else {}
        t = llm.get("temperature") if isinstance(llm, dict) else None
        assert t is None or float(t) == HOUSE_TEMPERATURE, p.name


# ---------------------------------------------------------------------------
# The request each wrapper builds
# ---------------------------------------------------------------------------


def test_vllm_body_defaults_to_the_house_temperature():
    from legba.data.stack.llm.vllm import VLLMProviderHandler

    h = VLLMProviderHandler()
    for t in (None, HOUSE_TEMPERATURE):
        body = h._build_chat_payload(  # noqa: SLF001
            messages=[{"role": "user", "content": "hi"}], system=None, tools=None,
            model="fast", max_tokens=16, temperature=t, reasoning_effort=None,
        )
        assert body["temperature"] == HOUSE_TEMPERATURE


def test_anthropic_body_carries_the_house_temperature_or_drops_it():
    from legba.data.stack.llm.anthropic import AnthropicProviderHandler

    h = AnthropicProviderHandler()
    kw = dict(
        messages=[{"role": "user", "content": "hi"}], system=None, tools=None,
        max_tokens=16, temperature=HOUSE_TEMPERATURE, reasoning_effort=None,
    )
    assert h._build_chat_payload(model="claude-sonnet-4-6", **kw)["temperature"] == HOUSE_TEMPERATURE  # noqa: SLF001
    for m in ("claude-opus-4-8", "claude-opus-5", "claude-fable-5"):
        assert "temperature" not in h._build_chat_payload(model=m, **kw)  # noqa: SLF001


def test_openai_body_carries_the_house_temperature():
    from legba.data.stack.llm.openai import OpenAIProviderHandler

    h = OpenAIProviderHandler()
    body = h._build_chat_payload(  # noqa: SLF001
        messages=[{"role": "user", "content": "hi"}], system=None, tools=None,
        model="gpt-4.1", max_tokens=16, temperature=HOUSE_TEMPERATURE, reasoning_effort=None,
    )
    assert body["temperature"] == HOUSE_TEMPERATURE


def test_translate_gateway_body_carries_the_house_temperature():
    from legba.data.stack.nlp_service.gateway import GatewayNlpClient

    c = GatewayNlpClient(endpoint="http://x", api_key="k", translate_model="m")
    seen: dict = {}

    async def fake(method, path, body, meta=None):  # noqa: ANN001
        seen.update(body)
        return {"choices": [{"message": {"content": "ok"}}], "usage": {}, "model": "m"}

    c._request = fake  # type: ignore[method-assign]  # noqa: SLF001
    try:
        asyncio.run(c._translate_chunk("hola", "es", "en"))  # noqa: SLF001
    except Exception:  # the answer cleaning may refuse the stub; the body is what is pinned
        pass
    assert seen["temperature"] == HOUSE_TEMPERATURE

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Sampling parameters — CONFIGURED (the descriptor) vs SENT (the wire).

The Flow inspector shows both, because they differ: a descriptor's
``method.llm.max_tokens`` (on the consult kinds, an output estimate) is never
sent on any route — the wire carries the alias's own declared maximum, or
nothing; several call sites hard-code ``temperature``.
Nothing here is guessed from documentation: every rule is a CODE FACT with the
file it lives in, and ``tests/data_pkg/test_flow_api.py`` pins each one
against the code it mirrors (the subprovider rule against
``analyst_deps_builder.infer_llm_subprovider``, the vLLM default temperature,
the Anthropic temperature-drop prefixes, every hard-coded site's literal).

What this module CANNOT see, and says so on every answer:

* the runtime container's ``LEGBA_LLM_SEND_MAX_TOKENS`` off switch (it lives in
  another process; the registry's environment is not the runtime's);
* the gateway's own behaviour (its default output tokens for an alias whose
  maximum the component does not declare, fixed sampling on some aliases) —
  that is the far side of the wire, not in code;
* the temperature actually sent on a given call: ``analyst_traces.llm_calls``
  does not record it. What a trace does record (model, finish reason, token
  counts) is returned beside this, labelled observed.

Registry-slim: stdlib only (plus the stdlib-only leaf ``legba.shared.sampling``).
"""
from __future__ import annotations

from typing import Any, Mapping

from legba.shared.model_output_limits import anthropic_max_output_tokens
from legba.shared.sampling import HOUSE_TEMPERATURE

#: ``vllm.py`` ``VLLMProviderHandler._DEFAULT_TEMPERATURE`` — sent when the
#: caller passes ``temperature=None``.
VLLM_DEFAULT_TEMPERATURE = HOUSE_TEMPERATURE

#: ``anthropic.py`` ``AnthropicProviderHandler.TEMPERATURE_DEPRECATED_PREFIXES``.
ANTHROPIC_TEMPERATURE_DROPPED_PREFIXES = ("claude-opus-4-8", "claude-opus-5", "claude-fable-5")

#: Kind-level temperature defaults applied when the descriptor sets none.
KIND_TEMPERATURE_DEFAULTS: dict[str, tuple[float, str]] = {
    "inline_target": (HOUSE_TEMPERATURE, "runtime/analyst_deps_builder.py (_read_method_llm_option default=HOUSE_TEMPERATURE)"),
    "journal_assessor": (HOUSE_TEMPERATURE, "runtime/analyst_deps_builder.py (_read_method_llm_option default=HOUSE_TEMPERATURE)"),
    "inquiry": (HOUSE_TEMPERATURE, "runtime/analyst_deps_builder.py (_read_method_llm_option default=HOUSE_TEMPERATURE)"),
    "consult_on_demand": (HOUSE_TEMPERATURE, "data/analysts/consult_on_demand.py (ConsultOnDemandDeps.temperature=HOUSE_TEMPERATURE)"),
}

#: Call sites that pass a literal temperature, bypassing the descriptor.
#: analyst id → [(leg, value, file)]. A parity test greps each file for the
#: literal, so a site that changes turns the test red instead of this table
#: going stale.
HARD_CODED_TEMPERATURE: dict[str, list[tuple[str, float, str]]] = {
    "signal_summarizer": [("summary", HOUSE_TEMPERATURE, "data/analysts/deterministic_handlers/signal_summarizer.py")],
    "fact_contention_arbiter": [("arbitration", HOUSE_TEMPERATURE, "data/analysts/deterministic_handlers/fact_contention_arbiter.py")],
    "competing_hypotheses": [("consistency matrix", HOUSE_TEMPERATURE, "data/analysts/competing_hypotheses.py")],
    "desk_reference": [("labelling", HOUSE_TEMPERATURE, "data/analysts/deterministic_handlers/desk_reference.py")],
    "claim_watch": [("bearing gate", HOUSE_TEMPERATURE, "data/analysts/deterministic_handlers/bearing_gate.py")],
}

#: The faithfulness judge leg every LLM finding gets (``verify`` role).
VERIFY_JUDGE_TEMPERATURE = (HOUSE_TEMPERATURE, "data/provenance/judge_transport.py (judge call temperature=HOUSE_TEMPERATURE, the house rule)")


def infer_subprovider(component_id: str | None) -> str:
    """Mirror of ``runtime/analyst_deps_builder.infer_llm_subprovider`` for the
    id-pattern rungs (the endpoint-host rungs need the component's endpoint;
    every live LLM component resolves on an id rung)."""
    cid = (component_id or "").lower()
    if ".decide." in cid:
        return "decide"
    if ".anthropic." in cid or cid.startswith("llm.anthropic"):
        return "anthropic"
    if cid.endswith(".openai_compat"):
        return "vllm"
    if ".openai." in cid or cid.startswith("llm.openai"):
        return "openai"
    return "vllm"


def _raw(v: Any) -> Any:
    if isinstance(v, Mapping) and "raw" in v:
        return v.get("raw")
    return v


def _num(v: Any) -> float | int | None:
    v = _raw(v)
    return v if isinstance(v, (int, float)) and not isinstance(v, bool) else None


def _sent_for_route(
    role: str,
    component_id: str,
    comp: Mapping[str, Any],
    *,
    configured_temp: float | None,
    temp_basis: str,
    configured_max: int | None,
) -> dict[str, Any]:
    sub = infer_subprovider(component_id)
    model = comp.get("model")
    temp, tbasis = configured_temp, temp_basis
    if role == "verify":
        temp, tbasis = VERIFY_JUDGE_TEMPERATURE
    out: dict[str, Any] = {
        "role": role, "component_id": component_id, "subprovider": sub, "model": model,
    }
    if sub in ("vllm", "openai"):
        sent_t = temp if temp is not None else VLLM_DEFAULT_TEMPERATURE
        out["temperature_sent"] = sent_t
        out["temperature_basis"] = (
            tbasis if temp is not None
            else "caller passed none → handler default 1.0 (data/stack/llm/vllm.py _DEFAULT_TEMPERATURE)")
        if sub == "vllm":
            out.update(openai_compat_limits(comp))
        else:
            out["max_tokens_sent"] = None
            out["max_tokens_basis"] = (
                "NOT SENT on the OpenAI route (data/stack/llm/openai.py): the API's own default, "
                "the model's maximum, applies; never the call's or the descriptor's value")
    elif sub == "anthropic":
        dropped = isinstance(model, str) and model.startswith(ANTHROPIC_TEMPERATURE_DROPPED_PREFIXES)
        out["temperature_sent"] = None if dropped else temp
        out["temperature_basis"] = (
            f"DROPPED: model {model!r} is on anthropic.py TEMPERATURE_DEPRECATED_PREFIXES" if dropped
            else tbasis)
        out["max_tokens_sent"] = anthropic_max_output_tokens(model) if isinstance(model, str) else None
        out["max_tokens_basis"] = ("ALWAYS SENT on the Anthropic route (the API requires it): the model's "
                                   "maximum output (shared/model_output_limits.py), never the call's or "
                                   "the descriptor's value — no output caps; less only when the counted "
                                   "input leaves less room in the context window (an API validity "
                                   "constraint; the call's receipt records max_tokens_sent)")
    else:
        out["temperature_sent"] = None
        out["temperature_basis"] = f"{sub} route: no chat sampling (data/stack/llm/decide.py)"
        out["max_tokens_sent"] = None
        out["max_tokens_basis"] = f"{sub} route: not a chat completion"
    out["top_p_sent"] = None
    out["top_p_basis"] = "never passed by an analyst call site; sent only if a caller supplies it"
    out["thinking_sent"] = None
    out["thinking_basis"] = ("no caller passes reasoning_effort; vllm.py ignores it; the alias "
                             "decides thinking on the gateway side")
    return out


def _alias_limit_line(alias: Any, value: int | None, unresolved: str | None) -> str:
    if value is not None:
        return f"{alias}: SENT {value:,} (the alias's declared maximum output)"
    if unresolved is not None:
        return (f"{alias}: declared as {unresolved}, unset in the registry's environment — "
                "the runtime resolves it, and fails loudly if it is unset there too")
    return (f"{alias}: NOT SENT — the component declares no maximum for this alias, so the call "
            "relies on the endpoint's default for it")


def openai_compat_limits(comp: Mapping[str, Any]) -> dict[str, Any]:
    """The output and input limits a call on an OpenAI-compatible component runs
    under, per alias, from the component's declared values (``vllm.py``)."""
    model, thinking = comp.get("model"), comp.get("thinking_model")
    unresolved = comp.get("limits_unresolved") or {}
    plain = _num(comp.get("max_output_tokens"))
    deep = _num(comp.get("thinking_max_output_tokens")) if thinking else None
    window = _num(comp.get("context_window_tokens"))
    lines = [_alias_limit_line(model, plain, unresolved.get("max_output_tokens"))]
    if thinking:
        lines.append(_alias_limit_line(
            thinking, deep, unresolved.get("thinking_max_output_tokens")))
    lines.append(
        "never the call's or the descriptor's value; less than the maximum only when the "
        "counted input leaves less room in the context window (the call's receipt then "
        "records max_tokens_sent and max_tokens_limited_by); LEGBA_LLM_SEND_MAX_TOKENS=0 in "
        "the runtime container turns sending off (data/stack/llm/vllm.py)")
    out: dict[str, Any] = {
        "max_tokens_sent": plain,
        "max_tokens_sent_thinking": deep,
        "thinking_model": thinking,
        "max_tokens_basis": "; ".join(lines),
        "context_window_tokens": window,
    }
    budget, basis = derived_input_budget(window, [v for v in (plain, deep) if v])
    out["input_budget_tokens"] = budget
    out["input_budget_basis"] = basis
    return out


#: ``analysts/_llm_budget`` constants, mirrored (this module is registry-slim);
#: ``tests/data_pkg/test_flow_api.py`` pins them equal.
INPUT_BUDGET_DEFAULT = 32000
INPUT_BUDGET_OUTPUT_RESERVE_MAX_SHARE = 0.5
INPUT_BUDGET_MARGIN = 4096


def derived_input_budget(window: int | None, maxima: list[int]) -> tuple[int | None, str]:
    """The input budget the runtime derives for this route when
    ``LEGBA_LLM_INPUT_TOKEN_BUDGET`` is unset in its container (that override
    is not visible from here)."""
    if not window:
        return None, ("no context window declared: the runtime uses the window the endpoint "
                      "lists, else LEGBA_LLM_INPUT_TOKEN_BUDGET, else "
                      f"{INPUT_BUDGET_DEFAULT:,} (analysts/_llm_budget.py)")
    share = int(window * INPUT_BUDGET_OUTPUT_RESERVE_MAX_SHARE)
    reserve = min(max(maxima), share) if maxima else share
    budget = window - reserve - INPUT_BUDGET_MARGIN
    return budget, (f"window {window:,} - output reserve {reserve:,} - margin "
                    f"{INPUT_BUDGET_MARGIN:,} (analysts/_llm_budget.py); "
                    "LEGBA_LLM_INPUT_TOKEN_BUDGET in the runtime container overrides it")


def sampling_params(
    analyst_id: str,
    body: Mapping[str, Any],
    stack_models: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """``{configured, sent[], hard_coded[], cannot_see[]}`` for one analyst."""
    method = body.get("method") if isinstance(body.get("method"), Mapping) else {}
    llm = method.get("llm") if isinstance(method.get("llm"), Mapping) else {}
    ident = body.get("identity") if isinstance(body.get("identity"), Mapping) else {}
    kind = str(ident.get("kind") or "")
    conf_temp = _num(llm.get("temperature"))
    conf_max = _num(llm.get("max_tokens"))
    configured = {
        "temperature": conf_temp,
        "max_tokens": conf_max,
        "top_p": _num(llm.get("top_p")),
        "budget_tokens_per_run": method.get("budget_tokens_per_run"),
        "budget_tokens_per_day": method.get("budget_tokens_per_day"),
        "timeout_seconds": method.get("timeout_seconds"),
        "routes": {
            role: _raw(v) for role, v in sorted(llm.items())
            if isinstance(v, Mapping) and v.get("factory_kind") == "stack_ref"
        },
    }
    if conf_temp is not None:
        temp, basis = float(conf_temp), "descriptor method.llm.temperature, passed through"
    elif kind in KIND_TEMPERATURE_DEFAULTS:
        temp, basis = KIND_TEMPERATURE_DEFAULTS[kind]
        basis = f"descriptor sets none → kind default {temp} ({basis})"
    else:
        temp, basis = None, "descriptor sets none; the handler decides (no kind default known)"
    sent = [
        _sent_for_route(role, str(cid), stack_models.get(str(cid)) or {},
                        configured_temp=temp, temp_basis=basis, configured_max=conf_max)
        for role, cid in configured["routes"].items() if isinstance(cid, str)
    ]
    hard = [
        {"leg": leg, "temperature_sent": val, "file": f}
        for leg, val, f in HARD_CODED_TEMPERATURE.get(analyst_id, [])
    ]
    return {
        "configured": configured,
        "sent": sent,
        "hard_coded": hard,
        "cannot_see": [
            "LEGBA_LLM_SEND_MAX_TOKENS (off switch) and LEGBA_LLM_INPUT_TOKEN_BUDGET "
            "(override) in the runtime container (another process)",
            "the gateway's per-alias defaults (used where no maximum is declared) and fixed "
            "sampling (far side of the wire)",
            "the temperature actually sent per call (analyst_traces.llm_calls does not record it)",
        ],
    }


__all__ = [
    "ANTHROPIC_TEMPERATURE_DROPPED_PREFIXES",
    "INPUT_BUDGET_DEFAULT",
    "INPUT_BUDGET_MARGIN",
    "INPUT_BUDGET_OUTPUT_RESERVE_MAX_SHARE",
    "HARD_CODED_TEMPERATURE",
    "KIND_TEMPERATURE_DEFAULTS",
    "VLLM_DEFAULT_TEMPERATURE",
    "derived_input_budget",
    "infer_subprovider",
    "openai_compat_limits",
    "sampling_params",
]

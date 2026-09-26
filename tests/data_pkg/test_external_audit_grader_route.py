# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L1 — WHICH GRADER GRADED THIS, on the row and in the body.

THE INCIDENT (2026-09-18 -> 09-20). OpenRouter removed
``mistralai/mistral-large-2512``. The standing auditor's grader component
pointed at it, every grader call 404'd, and for 2.7 days the auditor wrote
critique rows whose provenance line read:

    grader: mistral (model unrecorded)

Three separate holes made that sentence possible, and this module pins all
three shut:

  1. ``grader_model_name`` was sourced ONLY from ``response.usage.model``. A
     call that RAISED never reached that line at all, so the rows produced by
     a dead route were exactly the rows with no model on them — the inverse of
     what an operator needs.
  2. A provider that does not echo the model back left the field empty even on
     success. The configured handler always knows what it called.
  3. The roll-up carried the FAMILY and not the COMPONENT. "mistral" stayed
     true of a model that no longer existed; the stack ref an operator would
     go and repoint appeared nowhere on the row or in the body.

The family label stays — it is the cross-family fence's own vocabulary.
"""

from __future__ import annotations

import asyncio
from typing import Any

from legba.data.analysts.deterministic_handlers import (
    _external_audit_claims as claims,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_grader as grader,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_width as width,
)
from legba.data.analysts.deterministic_handlers import (
    _external_audit_width_writes as writes,
)

COMPONENT = "llm.judge.openrouter_mistral_large.openai_compat"
CONFIGURED_MODEL = "mistralai/mistral-medium-3.1"


# ---------------------------------------------------------------------------
# Doubles: the shapes the REAL handler presents
# ---------------------------------------------------------------------------


class _FactoryValue:
    """``LLMProviderConfig`` fields are FactoryValues — the model id lives at
    ``.raw``, which is what the handler puts on the wire."""

    def __init__(self, raw: str) -> None:
        self.raw = raw


class _Config:
    def __init__(self, model: str) -> None:
        self.model_name = _FactoryValue(model)


class _Usage:
    def __init__(self, model: str = "") -> None:
        self.model = model
        self.prompt_tokens = 0
        self.completion_tokens = 0


class _Response:
    def __init__(self, content: str, *, model: str = "", provider: str = ""):
        self.content = content
        self.usage = _Usage(model)
        self.raw_response = {"provider": provider} if provider else {}


class _Handler:
    """An ``LLMProviderHandler`` stand-in: configured, and answering."""

    def __init__(self, *, response: Any = None, raises: Exception | None = None):
        self._cfg = _Config(CONFIGURED_MODEL)
        self._instance_id = COMPONENT
        self._response = response
        self._raises = raises

    async def chat_complete(self, *a: Any, **kw: Any) -> Any:
        if self._raises is not None:
            raise self._raises
        return self._response


def _claim(text: str = "Exports rose 4% in August.") -> claims.WidthClaim:
    return claims.WidthClaim(
        claim_text=text, population="assembly_span", graded_output_id="o",
        analyst_id="country_composition", origin_head_id="h",
        start=0, end=len(text),
    )


def _envelope() -> grader.EvidenceEnvelope:
    return grader.EvidenceEnvelope(
        query="q",
        results=({"url": "https://news.example/a", "title": "t",
                  "snippet": "s"},),
        search_status={"status": "completed", "liveness": "unverified"},
        provider="searxng",
    )


def _grade(**kw: Any) -> Any:
    return asyncio.run(grader.grade_claim(
        kw.pop("llm"), _claim(), _envelope(),
        grader_family="mistral", grader_component_id=COMPONENT, **kw,
    ))


# ---------------------------------------------------------------------------
# 1) configured_model_id — the fallback that makes "unrecorded" unreachable
# ---------------------------------------------------------------------------


def test_configured_model_id_reads_the_handler_the_call_will_use():
    assert grader.configured_model_id(_Handler()) == CONFIGURED_MODEL


def test_configured_model_id_never_guesses_and_never_raises():
    """An unconfigured or foreign object yields "" — which still renders
    honestly — rather than a guess or an exception inside a verdict path."""

    class _Unconfigured:
        _cfg = None

    for obj in (None, object(), _Unconfigured(), "not a handler"):
        assert grader.configured_model_id(obj) == ""


def test_configured_model_id_accepts_a_plain_model_attribute():
    class _Simple:
        model = "gpt-oss-120b"

    assert grader.configured_model_id(_Simple()) == "gpt-oss-120b"


# ---------------------------------------------------------------------------
# 2) The verdict row carries the model — on success, on failure, on timeout
# ---------------------------------------------------------------------------


def test_the_response_model_wins_when_the_provider_echoes_one():
    """The response is the first authority: it is what actually ran."""
    llm = _Handler(response=_Response(
        '{"verdict":"NOT_FOUND","rationale":"r","evidence":[]}',
        model="mistralai/mistral-medium-3.1-2026", provider="DeepInfra",
    ))
    g = _grade(llm=llm)
    assert g.grader_model_name == "mistralai/mistral-medium-3.1-2026"
    assert g.grader_served_by == "DeepInfra"


def test_a_silent_provider_falls_back_to_the_configured_model():
    """The hole that made "model unrecorded" possible on a SUCCESSFUL call."""
    llm = _Handler(response=_Response(
        '{"verdict":"NOT_FOUND","rationale":"r","evidence":[]}'
    ))
    g = _grade(llm=llm)
    assert g.grader_model_name == CONFIGURED_MODEL
    # No fallback for served_by: that field means "who actually answered".
    assert g.grader_served_by == ""


def test_a_DEAD_ROUTE_still_names_the_model_it_tried():
    """THE 09-18 ROW. The call raises (upstream removed the model), so the
    verdict is UNCHECKED/grader_unavailable — and the row must still say WHICH
    component and WHICH model, because those two facts are the entire fix."""
    llm = _Handler(raises=RuntimeError("404 model not found"))
    g = _grade(llm=llm)
    assert g.verdict == "UNCHECKED"
    assert g.unchecked_reason == "grader_unavailable"
    assert g.grader_component_id == COMPONENT
    assert g.grader_model_name == CONFIGURED_MODEL
    assert g.grader_served_by == ""
    row = g.as_dict()
    assert row["grader_component_id"] == COMPONENT
    assert row["grader_model_name"] == CONFIGURED_MODEL


def test_a_TIMEOUT_also_names_the_model_it_tried():
    llm = _Handler(raises=asyncio.TimeoutError())
    g = _grade(llm=llm)
    assert g.verdict == "UNCHECKED"
    assert g.unchecked_reason == "timeout"
    assert g.grader_model_name == CONFIGURED_MODEL


def test_the_dead_route_log_names_the_component_and_the_model(caplog):
    """The WARNING that was the ONLY trace for 2.7 days used to carry neither
    the component nor the model, so no log search could name the dead route."""
    llm = _Handler(raises=RuntimeError("404 model not found"))
    with caplog.at_level("WARNING"):
        _grade(llm=llm)
    line = "\n".join(r.getMessage() for r in caplog.records)
    assert "external_audit.grade_failed" in line
    assert COMPONENT in line
    assert CONFIGURED_MODEL in line


# ---------------------------------------------------------------------------
# 3) The roll-up and the critique body carry the whole route
# ---------------------------------------------------------------------------


def _rollup_with(model: str = CONFIGURED_MODEL) -> dict[str, Any]:
    g = grader.WidthGrade(
        claim=_claim(), verdict="NOT_FOUND", rater_role=grader.RATER_PRIMARY,
        grader_family="mistral", grader_component_id=COMPONENT,
        grader_model_name=model,
    )
    return width.build_read_rollup("11111111-1111-4111-8111-111111111111", [g])


def test_the_rollup_carries_the_component_id_not_just_the_family():
    rollup = _rollup_with()
    assert rollup["grader_family"] == "mistral"
    assert rollup["grader_component_id"] == COMPONENT
    assert rollup["grader_model_name"] == CONFIGURED_MODEL


def test_the_critique_body_names_component_and_model():
    """The literal string this lane exists to delete: "grader: mistral (model
    unrecorded)". Both halves of the route are on the line now."""
    g = grader.WidthGrade(
        claim=_claim(), verdict="NOT_FOUND", rater_role=grader.RATER_PRIMARY,
        grader_family="mistral", grader_component_id=COMPONENT,
        grader_model_name=CONFIGURED_MODEL,
    )
    output_id = "11111111-1111-4111-8111-111111111111"
    payload = writes.build_width_critique_payload(
        output_id, _rollup_with(), [g],
    )
    line = next(
        ln for ln in payload.body.splitlines() if ln.strip().startswith("grader:")
    )
    assert "mistral" in line                 # the family label STAYS
    assert COMPONENT in line                 # the stack ref to repoint
    assert CONFIGURED_MODEL in line          # what was on the wire
    assert "model unrecorded" not in line
    # ``judge_model`` — the existing critique provenance column — agrees.
    assert payload.judge_model == CONFIGURED_MODEL


def test_an_unresolved_route_renders_honestly_rather_than_blank():
    """No invention: a roll-up with no component says so, in words."""
    g = grader.WidthGrade(
        claim=_claim(), verdict="NOT_FOUND", rater_role=grader.RATER_PRIMARY,
        grader_family="mistral",
    )
    output_id = "11111111-1111-4111-8111-111111111111"
    rollup = width.build_read_rollup(output_id, [g])
    assert rollup["grader_component_id"] == ""
    payload = writes.build_width_critique_payload(output_id, rollup, [g])
    line = next(
        ln for ln in payload.body.splitlines() if ln.strip().startswith("grader:")
    )
    assert "component unresolved" in line
    assert "model unrecorded" in line


# ---------------------------------------------------------------------------
# 4) The tick hands the LIVE binding to the drain (the clamp's whole point)
# ---------------------------------------------------------------------------


def test_run_width_tick_passes_the_live_binding_to_the_drain(monkeypatch):
    """The resolution is only worth anything if the tick actually reaches it.

    The clamp reads ``binding.pack.governor`` — so the binding has to arrive at
    ``plan_drain``. Asserted at the seam rather than by reading the source: a
    refactor that dropped the argument would re-create the starvation
    silently, since the fallback (env, then uncapped) looks healthy.
    """
    seen: dict[str, Any] = {}
    real_plan_drain = width.plan_drain

    def _spy(state, **kw):
        seen.update(kw)
        return real_plan_drain(state, **kw)

    monkeypatch.setattr(width, "plan_drain", _spy)

    class _Conn:
        async def fetchrow(self, *a, **kw):
            return None

        async def fetch(self, *a, **kw):
            return []

        async def execute(self, *a, **kw):
            return None

    class _Acquire:
        async def __aenter__(self):
            return _Conn()

        async def __aexit__(self, *a):
            return False

    class _Pool:
        def acquire(self):
            return _Acquire()

    class _Gov:
        max_invocations_per_hour = 1_000_000

    class _Pack:
        governor = _Gov()
        identity = None

    class _Binding:
        pack = _Pack()

    binding = _Binding()
    asyncio.run(width.run_width_tick(
        pool=_Pool(), options={}, binding=binding, grader=None,
        audit_rater=None, grader_family="mistral",
        grader_component_id=COMPONENT, rater_family="llama",
        rater_component_id="llm.audit.openrouter_llama33_70b.openai_compat",
        trigger_class="external_audit", pipeline_version="test/1",
    ))
    assert seen.get("binding") is binding

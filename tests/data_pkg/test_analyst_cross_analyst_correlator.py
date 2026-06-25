# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""L-177 cross_analyst_correlator analyst-kind unit tests.

Scope per the task brief:

  * Synthesize outputs from three analysts — two agreeing, one
    contradicting — and assert the correlator picks the contradiction and
    cites the contradicting analyst-id pair.
  * Test doubles live only at the LLM boundary (per the no-mocks rule).

These are pure-Python unit tests — no substrate containers, no Dapr
runtime, no LLM service. The LLM is a deterministic stub conforming to the
:class:`LLMHandlerLike` Protocol; it inspects the user prompt and returns a
canned JSON envelope. The kind module under test does the JSON parsing,
output-id validation, and downgrade enforcement.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping
from uuid import UUID, uuid4

import pytest

from legba.data.analysts.cross_analyst_correlator import (
    KIND_NAME,
    CrossAnalystCorrelatorDeps,
    CrossAnalystCorrelatorRunner,
    LLMHandlerLike,
    _DEFAULT_SYSTEM_PROMPT,
    _coerce_correlation,
    _coerce_uuid,
    _orient,
    _output_row_summary,
    _render_user_prompt,
    run_method,
)
from legba.data.provenance.models import FindingPayload
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# LLM test doubles — boundary mocks only
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 120
    completion_tokens = 60
    reasoning_tokens = 0


class _Response:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()
        self.finish_reason = "stop"
        self.tool_calls: list[Any] = []


class CannedLLM:
    """LLMHandlerLike stub that returns a fixed JSON payload."""

    subprovider = "test"

    def __init__(self, payload: dict[str, Any] | str) -> None:
        self.payload = payload
        self.last_messages: list[Mapping[str, Any]] | None = None
        self.last_system: str | None = None
        self.last_max_tokens: int | None = None
        self.last_temperature: float | None = None
        self.call_count = 0

    async def chat_complete(
        self,
        messages: list[Mapping[str, Any]],
        *,
        max_tokens: int | None = None,
        temperature: float | None = None,
        system: str | None = None,
        **kwargs: Any,
    ) -> Any:
        self.call_count += 1
        self.last_messages = messages
        self.last_system = system
        self.last_max_tokens = max_tokens
        self.last_temperature = temperature
        content = (
            self.payload
            if isinstance(self.payload, str)
            else json.dumps(self.payload)
        )
        return _Response(content)


class _LLMRaises:
    """LLMHandlerLike stub that raises on call — used to assert re-raise."""

    subprovider = "test"

    async def chat_complete(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("upstream LLM failure (deliberate)")


# ---------------------------------------------------------------------------
# Fixture: 3 synthetic analyst outputs (2 agreeing, 1 contradicting)
# ---------------------------------------------------------------------------
#
# Scenario: three analysts each emitted a finding about Brazil energy
# infrastructure on the same day:
#   * br_grid_v1  — finding: "Itaipu turbine upgrade completed on May 19"
#   * br_news_v2  — finding: "Itaipu turbine upgrade completed on May 19"
#                    (AGREES with br_grid_v1 — same claim, different analyst)
#   * br_sat_obs  — finding: "Itaipu turbines OFFLINE per May 19 satellite
#                    imagery; no upgrade performed."
#                    (CONTRADICTS the two above — same target, opposite claim)


_OUTPUT_ID_AGREE_A = uuid4()
_OUTPUT_ID_AGREE_B = uuid4()
_OUTPUT_ID_CONTRA = uuid4()

_ANALYST_AGREE_A = "br_grid_v1"
_ANALYST_AGREE_B = "br_news_v2"
_ANALYST_CONTRA = "br_sat_obs"


@pytest.fixture
def three_analyst_slice() -> list[dict[str, Any]]:
    """Three analyst outputs about the same target. Two agree, one contradicts."""
    return [
        {
            "id": _OUTPUT_ID_AGREE_A,
            "analyst_id": _ANALYST_AGREE_A,
            "analyst_version": "ff" * 8,
            "kind": "finding",
            "target_id": "india_energy_infra",
            "title": "Itaipu turbine upgrade completed",
            "body": (
                "Itaipu Binacional confirmed completion of the May 19 turbine "
                "upgrade. Grid telemetry shows full output restored."
            ),
            "confidence": 0.86,
            "tags": ["energy", "brazil", "upgrade_complete"],
            "produced_at": "2026-05-20T08:00:00Z",
        },
        {
            "id": _OUTPUT_ID_AGREE_B,
            "analyst_id": _ANALYST_AGREE_B,
            "analyst_version": "ee" * 8,
            "kind": "finding",
            "target_id": "india_energy_infra",
            "title": "Brazilian press confirms Itaipu upgrade complete",
            "body": (
                "Multiple Brazilian outlets report the May 19 Itaipu turbine "
                "upgrade as complete. Petrobras spokesperson confirmed."
            ),
            "confidence": 0.78,
            "tags": ["energy", "brazil", "upgrade_complete"],
            "produced_at": "2026-05-20T09:30:00Z",
        },
        {
            "id": _OUTPUT_ID_CONTRA,
            "analyst_id": _ANALYST_CONTRA,
            "analyst_version": "dd" * 8,
            "kind": "finding",
            "target_id": "india_energy_infra",
            "title": "Itaipu turbines OFFLINE per May 19 satellite imagery",
            "body": (
                "Satellite thermal imagery from May 19 shows Itaipu turbine "
                "halls cold; no operational signature consistent with the "
                "claimed upgrade completion. Upgrade did NOT happen."
            ),
            "confidence": 0.81,
            "tags": ["energy", "brazil", "satellite", "anomaly"],
            "produced_at": "2026-05-20T10:15:00Z",
        },
    ]


# ---------------------------------------------------------------------------
# Helpers for assertions
# ---------------------------------------------------------------------------


def _ids_equal(a: list[str], b: list[str]) -> bool:
    """Order-insensitive UUID-list equality."""
    return sorted(a) == sorted(b)


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_kind_name_is_correct() -> None:
    assert KIND_NAME == "cross_analyst_correlator"


def test_runner_protocol_shape() -> None:
    """``LLMHandlerLike`` accepts CannedLLM via structural typing."""
    llm = CannedLLM(payload={})
    assert isinstance(llm, LLMHandlerLike)


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_coerce_uuid_round_trips() -> None:
    u = uuid4()
    assert _coerce_uuid(u) == u
    assert _coerce_uuid(str(u)) == u
    assert _coerce_uuid("not-a-uuid") is None
    assert _coerce_uuid(None) is None


def test_output_row_summary_extracts_canonical_fields(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    p = _output_row_summary(three_analyst_slice[0])
    assert p["analyst_id"] == _ANALYST_AGREE_A
    assert p["target_id"] == "india_energy_infra"
    assert "Itaipu" in p["title"]
    assert p["confidence"] == pytest.approx(0.86)
    assert "energy" in p["tags"]
    assert p["output_id"] == str(_OUTPUT_ID_AGREE_A)


def test_output_row_summary_handles_dict_payload_form() -> None:
    """Row coming from a dedicated table may have payload-shaped data."""
    row = {
        "id": uuid4(),
        "analyst_id": "x",
        "kind": "prediction",
        "payload": {
            "hypothesis": "Markets will rally",
            "confidence": 0.7,
            "tags": ["macro"],
            "target_id": "global_macro",
        },
    }
    p = _output_row_summary(row)
    # Title falls back to payload.thesis/hypothesis when row.title is absent.
    assert "Markets" in p["title"]
    assert p["target_id"] == "global_macro"
    assert p["confidence"] == pytest.approx(0.7)
    assert "macro" in p["tags"]


def test_orient_returns_projected_uuids_and_analyst_set(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    projected, derived_from, analyst_ids = _orient(three_analyst_slice)
    assert len(projected) == 3
    # All three analyst_ids surface for the prompt's blind-spot detector.
    assert analyst_ids == {
        _ANALYST_AGREE_A,
        _ANALYST_AGREE_B,
        _ANALYST_CONTRA,
    }
    # Lineage list contains exactly the three input UUIDs.
    assert set(derived_from) == {
        _OUTPUT_ID_AGREE_A,
        _OUTPUT_ID_AGREE_B,
        _OUTPUT_ID_CONTRA,
    }
    # Newest-first ordering — the contradicting row was produced last.
    assert projected[0]["output_id"] == str(_OUTPUT_ID_CONTRA)


def test_render_user_prompt_lists_analyst_ids_and_outputs(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    projected, _, analyst_ids = _orient(three_analyst_slice)
    prompt = _render_user_prompt(projected, analyst_ids)
    for aid in (_ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA):
        assert aid in prompt
    for oid in (_OUTPUT_ID_AGREE_A, _OUTPUT_ID_AGREE_B, _OUTPUT_ID_CONTRA):
        assert str(oid) in prompt
    # Distinct-analyst-ids header is present so the LLM can ground the
    # blind-spot detector.
    assert "Distinct analyst_ids" in prompt
    assert "Number of analyst outputs: 3" in prompt


def test_default_system_prompt_frames_three_detectors() -> None:
    """The system prompt must instruct the LLM on the three correlation types
    in priority order so a small model keeps discrimination sharp."""
    sp = _DEFAULT_SYSTEM_PROMPT
    # Priority-order phrasing — contradiction first, blind_spot second,
    # agreement third.  These regexes match the canonical priority lines.
    assert re.search(r"1\.\s+CONTRADICTION", sp)
    assert re.search(r"2\.\s+BLIND_SPOT", sp)
    assert re.search(r"3\.\s+AGREEMENT", sp)
    # Strict-JSON instruction.
    assert "strict JSON" in sp
    # All three correlation_type values appear in the schema.
    for ct in ("contradiction", "agreement", "blind_spot"):
        assert ct in sp
    # ≥2 distinct analyst_ids required for contradiction/agreement.
    assert "two distinct analyst_id" in sp


# ---------------------------------------------------------------------------
# _coerce_correlation — JSON parsing + UUID validation + downgrade enforcement
# ---------------------------------------------------------------------------


def test_coerce_correlation_happy_path_contradiction() -> None:
    raw = json.dumps({
        "correlation_type": "contradiction",
        "title": "Itaipu upgrade — sat-obs contradicts grid+news",
        "body": (
            "br_grid_v1 and br_news_v2 both claim the May 19 Itaipu turbine "
            "upgrade completed; br_sat_obs reports satellite thermal imagery "
            "showing the turbines offline. Mutually exclusive — operator "
            "should resolve."
        ),
        "referenced_outputs": [
            str(_OUTPUT_ID_AGREE_A),
            str(_OUTPUT_ID_CONTRA),
        ],
        "referenced_analyst_ids": [_ANALYST_AGREE_A, _ANALYST_CONTRA],
        "confidence": 0.82,
        "tags": ["energy", "brazil", "resolution_required"],
    })
    valid_oids = {
        str(_OUTPUT_ID_AGREE_A),
        str(_OUTPUT_ID_AGREE_B),
        str(_OUTPUT_ID_CONTRA),
    }
    valid_aids = {_ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA}
    f = _coerce_correlation(
        raw,
        fallback_title="fallback",
        valid_output_ids=valid_oids,
        valid_analyst_ids=valid_aids,
    )
    assert isinstance(f, FindingPayload)
    assert f.data["correlation_type"] == "contradiction"
    assert _ids_equal(
        f.data["referenced_outputs"],
        [str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_CONTRA)],
    )
    assert _ids_equal(
        f.data["referenced_analyst_ids"],
        [_ANALYST_AGREE_A, _ANALYST_CONTRA],
    )
    assert f.confidence == pytest.approx(0.82)
    # Tag stamping: every finding gets correlation:<type>.
    assert "correlation:contradiction" in f.tags


def test_coerce_correlation_stamps_meta_and_contributing_analysts() -> None:
    """The audit fix: _coerce_correlation must stamp data.meta=True +
    data.contributing_analysts (the slice analyst set), mirroring
    meta_findings_synthesizer._coerce_finding. The value is the full
    contributing set passed in, NOT the LLM-cited referenced_analyst_ids."""
    raw = json.dumps({
        "correlation_type": "agreement",
        "title": "agreement",
        "body": "...",
        "referenced_outputs": [str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_AGREE_B)],
        "referenced_analyst_ids": [_ANALYST_AGREE_A, _ANALYST_AGREE_B],
        "confidence": 0.7,
    })
    f = _coerce_correlation(
        raw,
        fallback_title="fb",
        valid_output_ids={str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_AGREE_B)},
        valid_analyst_ids={_ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA},
        contributing_analysts=[_ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA],
    )
    assert f.data.get("meta") is True
    assert f.data.get("contributing_analysts") == [
        _ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA,
    ]
    # contributing_analysts (full slice) is broader than the cited set.
    assert _ANALYST_CONTRA not in f.data["referenced_analyst_ids"]


def test_coerce_correlation_stamps_meta_on_malformed_fallback() -> None:
    """Even the malformed-JSON fallback finding carries the meta marks so a
    DLQ/unstructured correlator output still satisfies the meta contract."""
    f = _coerce_correlation(
        "not json",
        fallback_title="fb",
        valid_output_ids=set(),
        valid_analyst_ids=set(),
        contributing_analysts=["analyst.x"],
    )
    assert "unstructured" in f.tags
    assert f.data.get("meta") is True
    assert f.data.get("contributing_analysts") == ["analyst.x"]


def test_coerce_correlation_strips_hallucinated_uuids() -> None:
    """Refs not present in the slice are dropped silently."""
    bogus = str(uuid4())
    raw = json.dumps({
        "correlation_type": "agreement",
        "title": "agreement",
        "body": "...",
        "referenced_outputs": [
            str(_OUTPUT_ID_AGREE_A),
            bogus,
            str(_OUTPUT_ID_AGREE_B),
        ],
        "referenced_analyst_ids": [
            _ANALYST_AGREE_A,
            "halucinated_analyst",
            _ANALYST_AGREE_B,
        ],
        "confidence": 0.7,
    })
    f = _coerce_correlation(
        raw,
        fallback_title="fb",
        valid_output_ids={
            str(_OUTPUT_ID_AGREE_A),
            str(_OUTPUT_ID_AGREE_B),
            str(_OUTPUT_ID_CONTRA),
        },
        valid_analyst_ids={
            _ANALYST_AGREE_A,
            _ANALYST_AGREE_B,
            _ANALYST_CONTRA,
        },
    )
    # Hallucinated UUID + analyst_id are stripped; real refs remain.
    assert bogus not in f.data["referenced_outputs"]
    assert "halucinated_analyst" not in f.data["referenced_analyst_ids"]
    assert _ids_equal(
        f.data["referenced_outputs"],
        [str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_AGREE_B)],
    )


def test_coerce_correlation_downgrades_unsupported_contradiction() -> None:
    """A contradiction that survives validation with <2 distinct analyst_ids
    must be downgraded to blind_spot and tagged so the operator can see it."""
    raw = json.dumps({
        "correlation_type": "contradiction",
        "title": "phantom contradiction",
        "body": "...",
        "referenced_outputs": [str(_OUTPUT_ID_AGREE_A)],
        "referenced_analyst_ids": [_ANALYST_AGREE_A],
        "confidence": 0.5,
    })
    f = _coerce_correlation(
        raw,
        fallback_title="fb",
        valid_output_ids={str(_OUTPUT_ID_AGREE_A)},
        valid_analyst_ids={_ANALYST_AGREE_A},
    )
    assert f.data["correlation_type"] == "blind_spot"
    assert "downgraded_from_contradiction" in f.tags
    assert "correlation:blind_spot" in f.tags


def test_coerce_correlation_unknown_type_defaults_to_blind_spot() -> None:
    raw = json.dumps({
        "correlation_type": "vibe_check",
        "title": "?",
        "body": "...",
        "referenced_outputs": [],
        "referenced_analyst_ids": [],
        "confidence": 0.4,
    })
    f = _coerce_correlation(
        raw, fallback_title="fb",
        valid_output_ids=set(), valid_analyst_ids=set(),
    )
    assert f.data["correlation_type"] == "blind_spot"
    assert "unknown_correlation_type" in f.tags


def test_coerce_correlation_malformed_json_unstructured() -> None:
    f = _coerce_correlation(
        "not json at all",
        fallback_title="cross-analyst correlation",
        valid_output_ids=set(),
        valid_analyst_ids=set(),
    )
    assert "unstructured" in f.tags
    # Raw response stays in data for audit.
    assert "raw_llm_response" in f.data


def test_coerce_correlation_strips_markdown_fence() -> None:
    """LLMs sometimes wrap JSON in ```json ... ``` — we must strip it."""
    inner = {
        "correlation_type": "agreement",
        "title": "x",
        "body": "y",
        "referenced_outputs": [str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_AGREE_B)],
        "referenced_analyst_ids": [_ANALYST_AGREE_A, _ANALYST_AGREE_B],
        "confidence": 0.9,
    }
    raw = "```json\n" + json.dumps(inner) + "\n```"
    f = _coerce_correlation(
        raw,
        fallback_title="fb",
        valid_output_ids={
            str(_OUTPUT_ID_AGREE_A),
            str(_OUTPUT_ID_AGREE_B),
        },
        valid_analyst_ids={_ANALYST_AGREE_A, _ANALYST_AGREE_B},
    )
    assert f.data["correlation_type"] == "agreement"
    assert len(f.data["referenced_analyst_ids"]) == 2


# ---------------------------------------------------------------------------
# run_method — the integration this task explicitly tests for
# ---------------------------------------------------------------------------


async def test_run_method_identifies_contradiction_and_cites_pair(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    """The headline test from the brief.

    Given three analysts (2 agreeing, 1 contradicting) and an LLM that
    correctly applies the priority order from the system prompt, the
    correlator must:

      * Return ``correlation_type == "contradiction"``;
      * Cite the contradicting analyst-id pair (one from the agreeing
        cluster + the contradicting analyst);
      * Cite both UUIDs in ``referenced_outputs``.
    """
    canned = {
        "correlation_type": "contradiction",
        "title": "br_sat_obs contradicts br_grid_v1 + br_news_v2 on Itaipu upgrade",
        "body": (
            "br_grid_v1 and br_news_v2 report the May 19 Itaipu turbine "
            "upgrade completed; br_sat_obs reports satellite thermal "
            "imagery showing the turbines offline. Mutually exclusive."
        ),
        # The LLM cites the contradicting pair (one from each side).
        "referenced_outputs": [
            str(_OUTPUT_ID_AGREE_A),
            str(_OUTPUT_ID_CONTRA),
        ],
        "referenced_analyst_ids": [_ANALYST_AGREE_A, _ANALYST_CONTRA],
        "confidence": 0.84,
        "tags": ["energy", "brazil", "resolution_required"],
    }
    llm = CannedLLM(payload=canned)
    deps = CrossAnalystCorrelatorDeps(llm=llm)
    options = {
        "analyst_id": "global_correlator_v1",
        "analyst_version": "01" * 8,
        "run_id": uuid4(),
    }

    result = await run_method(three_analyst_slice, options, deps)

    assert isinstance(result, AnalystMethodResult)
    assert isinstance(result.finding, FindingPayload)

    # 1. Contradiction was identified.
    assert result.finding.data["correlation_type"] == "contradiction"

    # 2. The contradicting analyst-id PAIR is cited (the headline assertion
    #    from the brief).
    cited_analysts = set(result.finding.data["referenced_analyst_ids"])
    assert _ANALYST_CONTRA in cited_analysts, (
        f"contradicting analyst {_ANALYST_CONTRA!r} must be cited; "
        f"got {cited_analysts}"
    )
    # The pair must include at least one of the agreeing analysts.
    assert cited_analysts & {_ANALYST_AGREE_A, _ANALYST_AGREE_B}, (
        f"contradiction must cite an analyst from the agreeing cluster; "
        f"got {cited_analysts}"
    )
    assert len(cited_analysts) >= 2

    # 3. The referenced_outputs name the same two outputs.
    cited_outputs = set(result.finding.data["referenced_outputs"])
    assert str(_OUTPUT_ID_CONTRA) in cited_outputs
    assert cited_outputs & {str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_AGREE_B)}

    # 4. Lineage tag stamped.
    assert "correlation:contradiction" in result.finding.tags
    assert "analyst:global_correlator_v1" in result.finding.tags

    # 4b. Second-order-finding schema contract (the audit gap): the correlator
    #     is a meta producer, so it MUST stamp data.meta=True and
    #     data.contributing_analysts covering the FULL slice's analyst set —
    #     the same contract meta_findings_synthesizer honours. Without this the
    #     correlator's lone live output carried contributing_analysts=NULL.
    assert result.finding.data.get("meta") is True
    contributing = set(result.finding.data.get("contributing_analysts") or [])
    assert contributing == {_ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA}, (
        f"contributing_analysts {contributing} must cover all three slice "
        f"analysts (independent of the narrower referenced_analyst_ids)"
    )

    # 5. Token usage flowed through to the AnalystMethodResult.usage dict
    #    so the budget enforcer can record it.
    assert result.usage["prompt_tokens"] == 120
    assert result.usage["completion_tokens"] == 60

    # 6. The LLM saw the canonical system prompt + a user prompt that listed
    #    the three analysts.
    assert llm.call_count == 1
    assert llm.last_system is not None
    assert "CONTRADICTION" in llm.last_system
    user_text = llm.last_messages[0]["content"]
    for aid in (_ANALYST_AGREE_A, _ANALYST_AGREE_B, _ANALYST_CONTRA):
        assert aid in user_text


async def test_run_method_accepts_bare_llm_handler_for_back_compat(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    """Mirrors :func:`inline_target.run_method` — bare-LLM-handler ``deps``
    is accepted for the spike-era back-compat path."""
    llm = CannedLLM(payload={
        "correlation_type": "agreement",
        "title": "agreement on Itaipu upgrade",
        "body": "br_grid_v1 and br_news_v2 converge.",
        "referenced_outputs": [str(_OUTPUT_ID_AGREE_A), str(_OUTPUT_ID_AGREE_B)],
        "referenced_analyst_ids": [_ANALYST_AGREE_A, _ANALYST_AGREE_B],
        "confidence": 0.75,
    })
    result = await run_method(three_analyst_slice, {}, llm)
    assert result.finding.data["correlation_type"] == "agreement"


async def test_run_method_empty_input_emits_blind_spot_noop() -> None:
    """Defensive: empty inputs → blind_spot diagnostic finding (no LLM call)."""
    llm = CannedLLM(payload={})
    deps = CrossAnalystCorrelatorDeps(llm=llm)
    result = await run_method([], {"analyst_id": "cor"}, deps)
    assert result.finding.data["correlation_type"] == "blind_spot"
    assert "empty_slice" in result.finding.tags
    # No LLM was invoked.
    assert llm.call_count == 0


async def test_run_method_propagates_llm_errors() -> None:
    """Per kind_contracts §7: failure semantics live in the runtime; the kind
    must re-raise so the runtime can classify (transient vs hard)."""
    deps = CrossAnalystCorrelatorDeps(llm=_LLMRaises())
    with pytest.raises(RuntimeError, match="upstream LLM failure"):
        await run_method(
            [
                {
                    "id": uuid4(),
                    "analyst_id": "x",
                    "title": "t",
                    "produced_at": "2026-01-01T00:00:00Z",
                }
            ],
            {"analyst_id": "cor"},
            deps,
        )


async def test_runner_wrapper_two_arg_call_matches_run_method(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    """``CrossAnalystCorrelatorRunner.__call__(inputs, options)`` matches the
    spike's :class:`LLMAnalystRunner` shape so :meth:`AnalystActor.run` in
    ``runtime/dapr_actors.py`` (which calls ``run_method(inputs, options)``
    with two args) keeps working unchanged."""
    canned = {
        "correlation_type": "contradiction",
        "title": "t",
        "body": "b",
        "referenced_outputs": [
            str(_OUTPUT_ID_AGREE_B),
            str(_OUTPUT_ID_CONTRA),
        ],
        "referenced_analyst_ids": [_ANALYST_AGREE_B, _ANALYST_CONTRA],
        "confidence": 0.7,
    }
    llm = CannedLLM(payload=canned)
    runner = CrossAnalystCorrelatorRunner(llm)
    # Two-arg signature — same shape AnalystActor.run uses.
    result = await runner(three_analyst_slice, {"analyst_id": "cor"})
    assert isinstance(result, AnalystMethodResult)
    assert result.finding.data["correlation_type"] == "contradiction"
    # Default temperature is the low-discrimination 0.1 (vs inline_target's 0.2).
    assert llm.last_temperature == pytest.approx(0.1)


async def test_run_method_strips_hallucinated_uuids_end_to_end(
    three_analyst_slice: list[dict[str, Any]],
) -> None:
    """Integration form of the UUID-stripping unit test: a hallucinated UUID
    in the LLM's response is not retained in the substrate-bound finding."""
    bogus = str(uuid4())
    canned = {
        "correlation_type": "contradiction",
        "title": "x",
        "body": "y",
        "referenced_outputs": [
            str(_OUTPUT_ID_CONTRA),
            bogus,
            str(_OUTPUT_ID_AGREE_A),
        ],
        "referenced_analyst_ids": [
            _ANALYST_CONTRA,
            "phantom_analyst",
            _ANALYST_AGREE_A,
        ],
        "confidence": 0.75,
    }
    deps = CrossAnalystCorrelatorDeps(llm=CannedLLM(payload=canned))
    result = await run_method(three_analyst_slice, {"analyst_id": "c"}, deps)
    assert bogus not in result.finding.data["referenced_outputs"]
    assert "phantom_analyst" not in result.finding.data["referenced_analyst_ids"]

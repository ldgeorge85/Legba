# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""J2 (2026-08-15) — the verify-path judge SAMPLING gate + the J1 component.

Phase J of FORWARD_PLAN_2026-08-15 §1 moves the faithfulness judge to a
rate-limited free tier, so verification becomes SAMPLED. These tests pin the
gate's whole contract:

  * DETERMINISM — the decision is a hash of the finding id vs the rate:
    the same id yields the same verdict on every call, every process, every
    replay. No RNG anywhere.
  * THE ALWAYS-LIST — compositions + world + journal are judged regardless of
    the rate (the default membership), an explicit list overrides, an explicit
    empty list clears.
  * THE UNSAMPLED STATE — an unselected finding publishes
    ``judge_status='unsampled'`` (honest, never an error), keeps the
    deterministic floor under the PROVISIONAL ceiling, spends ZERO judge
    tokens, and still publishes a real ``overall_score`` float (the SQL
    laterals contract — thirteen laterals filter on that key being non-null).
  * RATE EDGES — 0.0 gates everything not always-listed; 1.0 gates nothing;
    an absent rate is NO gate (byte-identical pre-J2 behavior).
  * THE J1 COMPONENT — the OpenRouter Nemotron judge component's registration
    shape (id → vllm handler, vault ref, timeout, model), and the vault
    loader's OPENROUTER_API_KEY mapping.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
from uuid import UUID, uuid4

import pytest

from legba.data.provenance.judge_assessability import (
    JUDGE_SAMPLE_ALWAYS_DEFAULT,
    JUDGE_STATUS_UNSAMPLED,
    JudgeSamplingPolicy,
    PROVISIONAL_SCORE_CEILING,
    build_faithfulness_critique_payload,
    judge_sample_unit,
)
from legba.data.provenance.verify import verify_finding_faithfulness

REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_sample_unit_is_deterministic_and_bounded():
    fid = uuid4()
    u = judge_sample_unit(fid)
    assert 0.0 <= u < 1.0
    # Same id, same coordinate — across calls and across representations
    # (UUID object, its str, a case-varied hex spelling).
    assert judge_sample_unit(fid) == u
    assert judge_sample_unit(str(fid)) == u
    assert judge_sample_unit(str(fid).upper()) == u


def test_sample_unit_spreads_over_the_population():
    """Not a distribution test — a sanity pin that the hash actually varies
    (a constant coordinate would make any rate all-or-nothing)."""
    ids = [UUID(int=i) for i in range(200)]
    units = [judge_sample_unit(i) for i in ids]
    assert len(set(units)) == len(units)
    inside = sum(1 for u in units if u < 0.10)
    # 10% of 200 = 20 expected; allow a wide band, refuse degenerate.
    assert 5 <= inside <= 45


def test_same_finding_id_same_verdict_every_time():
    policy = JudgeSamplingPolicy(
        finding_id=str(uuid4()), kind="inline_target", rate=0.5,
    )
    first = policy.should_judge()
    assert all(policy.should_judge() == first for _ in range(20))


def test_decision_matches_the_documented_hash_rule():
    fid = str(uuid4())
    u = judge_sample_unit(fid)
    # judged iff coordinate < rate — pin the comparison direction with rates
    # straddling this id's own coordinate.
    below = JudgeSamplingPolicy(finding_id=fid, kind="inline_target", rate=u * 0.5)
    above = JudgeSamplingPolicy(
        finding_id=fid, kind="inline_target", rate=min(1.0, u + (1.0 - u) * 0.5),
    )
    assert below.should_judge() is False
    assert above.should_judge() is True


# ---------------------------------------------------------------------------
# The always-list
# ---------------------------------------------------------------------------


def test_default_always_list_is_compositions_world_and_journal():
    """The tops of the synthesis tower: the composition kinds (country /
    region / escalation / WORLD all ride meta_findings_synthesizer; the
    correlator + situation tracker grade through the same composition verify
    path) and the journal family."""
    assert set(JUDGE_SAMPLE_ALWAYS_DEFAULT) == {
        "meta_findings_synthesizer",
        "cross_analyst_correlator",
        "situation_tracker",
        "journal_assessor",
    }


@pytest.mark.parametrize("kind", sorted(JUDGE_SAMPLE_ALWAYS_DEFAULT))
def test_always_kinds_are_judged_at_rate_zero(kind):
    policy = JudgeSamplingPolicy(finding_id=str(uuid4()), kind=kind, rate=0.0)
    assert policy.should_judge() is True


def test_unit_kind_is_not_in_the_default_always_list():
    policy = JudgeSamplingPolicy(
        finding_id=str(uuid4()), kind="inline_target", rate=0.0,
    )
    assert policy.should_judge() is False


def test_always_list_matches_analyst_id_too():
    """The option is 'kinds/analyst classes' — a specific unit can be named."""
    policy = JudgeSamplingPolicy(
        finding_id=str(uuid4()),
        kind="inline_target",
        analyst_id="escalation",
        rate=0.0,
        always=("escalation",),
    )
    assert policy.should_judge() is True


def test_explicit_empty_always_list_clears_the_default():
    """The deliberate two-step for sampling a composition: set a rate AND
    clear the membership."""
    policy = JudgeSamplingPolicy(
        finding_id=str(uuid4()),
        kind="meta_findings_synthesizer",
        rate=0.0,
        always=(),
    )
    assert policy.should_judge() is False


def test_explicit_always_list_replaces_not_extends_the_default():
    policy = JudgeSamplingPolicy(
        finding_id=str(uuid4()),
        kind="journal_assessor",
        rate=0.0,
        always=("some_other_kind",),
    )
    assert policy.should_judge() is False


# ---------------------------------------------------------------------------
# Rate edges
# ---------------------------------------------------------------------------


def test_rate_none_is_no_gate():
    assert JudgeSamplingPolicy(finding_id=str(uuid4())).should_judge() is True


def test_rate_one_judges_everything():
    for _ in range(50):
        assert (
            JudgeSamplingPolicy(
                finding_id=str(uuid4()), kind="inline_target", rate=1.0,
            ).should_judge()
            is True
        )


def test_rate_zero_judges_nothing_not_always_listed():
    for _ in range(50):
        assert (
            JudgeSamplingPolicy(
                finding_id=str(uuid4()), kind="inline_target", rate=0.0,
            ).should_judge()
            is False
        )


# ---------------------------------------------------------------------------
# The unsampled report + critique contract
# ---------------------------------------------------------------------------


class _MustNotBeCalledJudge:
    """A judge handler whose invocation is the test failure: an unsampled row
    must spend ZERO judge tokens — the V-B stage-2 absence path included."""

    subprovider = "vllm"

    async def chat_complete(self, *a, **k):  # pragma: no cover — the trap
        raise AssertionError("the judge was called on an UNSAMPLED finding")


def _unsampled_policy(kind: str = "inline_target") -> JudgeSamplingPolicy:
    return JudgeSamplingPolicy(finding_id=str(uuid4()), kind=kind, rate=0.0)


async def test_unsampled_report_contract(monkeypatch):
    """judge_status='unsampled', floor verdict, PROVISIONAL ceiling, real
    overall_score, no judge call — with the judge flag ON and a handler wired,
    which is exactly the live shape."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1]. Charlie confirmed the strike [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
        judge_llm=_MustNotBeCalledJudge(),
        judge_sampling=_unsampled_policy(),
    )
    assert report.judge_status == JUDGE_STATUS_UNSAMPLED
    # Deliberately not selected ≠ unavailable: no soft-fail reason, no error.
    assert report.judge_unavailable_reason is None
    assert report.provisional is True
    assert report.counters.get("judge_unsampled") == 1
    d = report.as_dict()
    # THE SQL LATERALS CONTRACT: overall_score is a real float, never null —
    # capped by the provisional ceiling because no judge graded it.
    assert d["overall_score"] is not None
    assert d["overall_score"] <= PROVISIONAL_SCORE_CEILING
    assert d["judge_status"] == "unsampled"
    # A scored (assessable) floor run still publishes its tally.
    assert d["score_state"] == "scored"
    assert d["faithfulness_score"] is not None


async def test_unsampled_is_never_error_even_when_the_judge_is_down(monkeypatch):
    """Population membership must not depend on judge health: an unsampled row
    reads 'unsampled' even with the flag off / nothing wired."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1]. Charlie confirmed it [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
        judge_llm=None,
        judge_sampling=_unsampled_policy(),
    )
    assert report.judge_status == JUDGE_STATUS_UNSAMPLED
    assert report.judge_unavailable_reason is None


async def test_sampled_finding_keeps_todays_path(monkeypatch):
    """A SELECTED finding goes down the existing judge ladder unchanged — with
    the flag off that is the labelled deterministic floor, not 'unsampled'."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1]. Charlie confirmed it [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
        judge_llm=None,
        judge_sampling=JudgeSamplingPolicy(
            finding_id=str(uuid4()), kind="inline_target", rate=1.0,
        ),
    )
    assert report.judge_status == "deterministic"
    assert report.judge_unavailable_reason == "flag_off"
    assert "judge_unsampled" not in report.counters


async def test_no_policy_is_byte_identical_to_before(monkeypatch):
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    kwargs = dict(
        body="Alpha struck Bravo base on Monday [1]. Charlie confirmed it [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
    )
    ungated = await verify_finding_faithfulness(**kwargs)
    explicit_none = await verify_finding_faithfulness(**kwargs, judge_sampling=None)
    assert ungated.as_dict() == explicit_none.as_dict()


async def test_unsampled_critique_payload_contract(monkeypatch):
    """The persisted row: overall_score populated (the gate JOIN key), the
    'unsampled' + 'provisional' labels visible, the stamp on the block."""
    monkeypatch.setenv("LEGBA_VERIFY_LLM_JUDGE", "1")
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1]. Charlie confirmed it [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
        judge_llm=_MustNotBeCalledJudge(),
        judge_sampling=_unsampled_policy(),
    )
    payload = build_faithfulness_critique_payload(
        report, analyzed_output_id=uuid4(),
    )
    assert payload["overall_score"] is not None
    assert payload["overall_score"] <= PROVISIONAL_SCORE_CEILING
    assert "unsampled" in payload["tags"]
    assert "provisional" in payload["tags"]
    verification = payload["data"]["verification"]
    assert verification["judge_status"] == "unsampled"
    assert verification["judge_unavailable_reason"] is None
    assert verification["provisional"] is True
    assert verification["overall_score"] is not None
    # The population split key rides the same block (the RUST-1 stamp).
    assert verification["judge_pipeline_version"] == "2026-08-21/1"

    from legba.data.provenance.models import CritiquePayload

    CritiquePayload.model_validate(payload)


# ---------------------------------------------------------------------------
# The gauge: unsampled rows are not judge-health evidence
# ---------------------------------------------------------------------------


def test_judge_availability_sql_excludes_the_unsampled_stratum():
    """At any rate below the 0.80 floor, counting deliberately-unsampled rows
    in the denominator would page a permanent fake outage — the gauge must
    measure the population the gate SELECTED."""
    from legba.data.registry.production_gauge_integrity import _JUDGE_SQL

    assert "'unsampled'" in _JUDGE_SQL
    # The denominator (critiques) and the wired count both carry the filter.
    assert _JUDGE_SQL.count("<> 'unsampled'") >= 2


# ---------------------------------------------------------------------------
# Descriptor option wiring (the X-1 kind-catalog lane)
# ---------------------------------------------------------------------------


def test_kind_catalog_accepts_the_sampling_options():
    from legba.data.analysts.handler_options import resolve_kind_options

    for kind in (
        "inline_target",
        "meta_findings_synthesizer",
        "cross_analyst_correlator",
        "situation_tracker",
        "journal_assessor",
    ):
        res = resolve_kind_options(
            kind,
            {"judge_sample_rate": 0.10,
             "judge_sample_always": ["journal_assessor", "escalation"]},
        )
        assert res.rejected == (), (kind, res.rejected)
        assert res.accepted["judge_sample_rate"] == 0.10
        assert res.accepted["judge_sample_always"] == [
            "journal_assessor", "escalation",
        ]


def test_kind_catalog_accepts_an_explicit_empty_always_list():
    from legba.data.analysts.handler_options import resolve_kind_options

    res = resolve_kind_options(
        "meta_findings_synthesizer", {"judge_sample_always": []},
    )
    assert res.rejected == ()
    assert res.accepted["judge_sample_always"] == []


@pytest.mark.parametrize("bad", [-0.1, 1.5, "0.1", True])
def test_kind_catalog_rejects_out_of_range_rates(bad):
    from legba.data.analysts.handler_options import resolve_kind_options

    res = resolve_kind_options("inline_target", {"judge_sample_rate": bad})
    assert "judge_sample_rate" not in res.accepted


def test_unit_descriptors_carry_the_tree_default_rate():
    """The 12 verify-bearing inline_target units ship judge_sample_rate 0.10 —
    the tree default the J-c train PUTs live."""
    import yaml

    units = [
        "corpus_researcher", "country_assessor", "cross_doc_corroborator",
        "disruption_status", "economic_coercion", "energy_security",
        "escalation", "internal_stability", "leadership_transition",
        "military_posture", "narrative_coordination", "proliferation_watch",
    ]
    for unit in units:
        body = yaml.safe_load(
            (REPO_ROOT / "descriptors" / f"analyst_{unit}.yaml").read_text()
        )
        assert body["identity"]["kind"] == "inline_target", unit
        assert "verify" in body["method"]["llm"], unit
        assert body["method"]["options"]["judge_sample_rate"] == 0.10, unit


# ---------------------------------------------------------------------------
# J1 — the OpenRouter Nemotron judge component + the vault mapping
# ---------------------------------------------------------------------------


def _load_script(name: str):
    path = REPO_ROOT / "scripts" / f"{name}.py"
    assert path.is_file(), path
    spec = importlib.util.spec_from_file_location(f"_j1_probe_{name}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[f"_j1_probe_{name}"] = mod
    spec.loader.exec_module(mod)
    return mod


JUDGE_COMPONENT_ID = "llm.judge.openrouter_nemotron120b.openai_compat"


def test_component_registration_shape():
    """The registrar carries the J1 judge component with the exact wiring the
    plan names: OpenRouter base, the free Nemotron model id, the vault ref
    (never a value), a 120s timeout, and the cheap tier."""
    reg = _load_script("bringup_register_stack")
    bodies = {cid: body for cid, body in reg.COMPONENTS}
    assert JUDGE_COMPONENT_ID in bodies
    body = bodies[JUDGE_COMPONENT_ID]
    assert body["id"] == JUDGE_COMPONENT_ID
    assert body["schema_uri"] == "legba/stack/llm_provider/1.0.0"
    cfg = body["config"]
    assert cfg["api_endpoint"]["raw"] == "https://openrouter.ai/api/v1"
    assert cfg["model_name"]["raw"] == "nvidia/nemotron-3-super-120b-a12b:free"
    assert cfg["timeout_seconds"]["raw"] == 120
    assert cfg["tier"]["raw"] == "cheap"
    # The credential is a VAULT REFERENCE by id — never a value.
    assert cfg["api_key"]["factory_kind"] == "secret"
    assert cfg["api_key"]["raw"] == "llm.judge.openrouter.api_key"


def test_component_body_validates_against_the_stack_schema():
    from legba.data.schemas.stack import LLMProvider

    reg = _load_script("bringup_register_stack")
    body = dict(dict(reg.COMPONENTS)[JUDGE_COMPONENT_ID])
    body["version"] = "0" * 16  # the registrar stamps this at POST time
    provider = LLMProvider.model_validate(body, strict=False)
    assert provider.config.model_name.raw == "nvidia/nemotron-3-super-120b-a12b:free"


def test_component_id_routes_to_the_vllm_handler():
    """NAMING IS LOAD-BEARING: `.openai_compat` suffix → the vLLM handler
    (OpenAI Chat-Completions + Bearer — what OpenRouter speaks). `.openai.`
    would route to the OpenAI handler's wrong wire shape."""
    from legba.runtime.analyst_deps_builder import infer_llm_subprovider

    assert (
        infer_llm_subprovider(
            JUDGE_COMPONENT_ID, endpoint="https://openrouter.ai/api/v1",
        )
        == "vllm"
    )


def test_trailing_v1_endpoint_is_normalized_not_doubled():
    """The stored base ends in /v1; the handler's base client strips it before
    prepending /v1/chat/completions — pin the defensive strip that makes the
    stored form safe."""
    src = (
        REPO_ROOT / "src" / "legba" / "data" / "stack" / "llm" / "base.py"
    ).read_text()
    assert 'endswith("/v1")' in src


def test_vault_loader_maps_the_openrouter_key():
    vault = _load_script("bringup_vault_load")
    mapping = dict(vault.MAPPING)
    assert mapping["llm.judge.openrouter.api_key"] == "OPENROUTER_API_KEY"

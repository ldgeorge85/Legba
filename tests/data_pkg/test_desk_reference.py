# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""A-1 — the out-of-plane desk reference writer, and the F-2 gate.

DB-free coverage of the five things the design makes CODE rather than prompt
(the query table, the URL fence, the liveness ladder over all five documented
``web_tools`` outcomes, the rotation, the SUBSTRATE FENCE), plus the F-2
byte-identity proof and three ephemeral-DB lifecycle tests driven through
``deterministic.run_method`` with ``options.sub_handler='desk_reference'`` — the
REAL binding path, never a direct module call.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

import asyncpg
import pytest
import pytest_asyncio

from legba.data.analysts import deterministic
from legba.data.analysts.deterministic import (
    OUTPUT_KIND_BY_SUB_HANDLER,
    SUB_HANDLERS,
)
from legba.data.analysts import research_regime as rr
from legba.data.analysts.deterministic_handlers import (
    _reference_gap_dispatch as rgd,
)
from legba.data.analysts.deterministic_handlers import desk_reference as dr
from legba.data.analysts.deterministic_handlers import unit_correctness_scorer as ucs
from legba.data.config import PostgresConfig
from legba.runtime.analyst_method import AnalystMethodResult


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


def test_registered_in_dispatch_table():
    assert "desk_reference" in SUB_HANDLERS
    assert OUTPUT_KIND_BY_SUB_HANDLER["desk_reference"].value == "finding"


def test_verify_exempt_because_an_instrument_is_not_a_claim():
    """It must never enter the verify/judge population, or this design would
    itself add to the ~84% of LLM calls that are the system watching itself."""
    from legba.data.provenance.kinds import (
        STRUCTURAL_VERIFY_EXEMPT_ANALYSTS,
        verify_exempt_reason,
    )

    assert "desk_reference" in STRUCTURAL_VERIFY_EXEMPT_ANALYSTS
    assert verify_exempt_reason("desk_reference") == "structural"


def test_descriptor_validates_and_binds_a_third_family():
    """The shipped descriptor round-trips the real schema, and its LLM ref is
    neither the writer's family nor the judge's.

    A ``gpt-oss-120b`` reference against ``gpt-oss-120b`` desks measures the
    family's shared blind spots and calls them agreement; a nemotron reference
    shares a family with the faithfulness judge. The ref is a DESCRIPTOR field
    precisely so it can be re-pointed to a third route without a code edit.
    """
    import pathlib

    import yaml

    from legba.data.schemas.analyst import AnalystDescriptor

    root = pathlib.Path(__file__).resolve().parents[2]
    body = yaml.safe_load(
        (root / "descriptors" / "analyst_desk_reference.yaml").read_text()
    )
    body.setdefault("identity", {})["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert desc.identity.id == "desk_reference"
    assert desc.identity.kind == "deterministic"
    assert desc.identity.state == "draft"
    assert desc.method.sub_handler == "desk_reference"
    # META — no per-target fan-out.
    assert desc.subscription.targets is None
    llm = desc.method.llm
    primary = llm["primary"] if isinstance(llm, dict) else llm.primary
    raw = primary["raw"] if isinstance(primary, dict) else primary.raw
    # 2026-09-05: the third-family slot was repointed from the unfunded Cerebras Gemma
    # to OpenRouter Mistral Large 3. The property that matters ("not the writer's
    # plane, not the judge's") is held by the fence tests; this pins the SHIPPED ref
    # so a silent drift back to a dead key is loud.
    assert raw == "llm.judge.openrouter_mistral_large.openai_compat", raw
    assert "llm.primary" not in raw, "the reference must not share the WRITER's family"
    assert "nemotron" not in raw, "the reference must not share the JUDGE's family"
    assert "anthropic" not in raw.lower()
    assert any(
        (p["pack_id"] if isinstance(p, dict) else p.pack_id) == "web_access"
        for p in desc.action_packs
    )


def test_every_declared_option_is_read_by_the_handler():
    """The X-1 contract, checked here too: a declared knob nobody reads is dead
    config with extra steps, and the descriptor documents these as the cost
    controls an operator turns."""
    from legba.data.analysts.handler_options import known_option_names

    source = __import__("pathlib").Path(dr.__file__).read_text()
    for name in known_option_names("desk_reference"):
        assert f'options.get("{name}")' in source, name


# ---------------------------------------------------------------------------
# 1. The query — code, not prompt
# ---------------------------------------------------------------------------


def test_query_is_built_from_the_fixed_table_and_is_replayable():
    q = dr.build_query("Israel", "escalation", datetime(2026, 9, 5).date())
    assert q == (
        "Israel escalation risk military tension conflict 2026-09-05"
    )


def test_every_bounded_unit_has_a_query_term_entry():
    """A unit with no entry is SKIPPED, never searched with an improvised
    query — a model-chosen query is a query that can quietly stop asking about
    the uncomfortable thing."""
    assert set(dr.BOUNDED_UNITS) == set(dr.UNIT_QUERY_TERMS)
    assert len(dr.UNIT_QUERY_TERMS) == 9
    for unit, terms in dr.UNIT_QUERY_TERMS.items():
        assert terms.strip() and terms == terms.strip(), unit


def test_an_unknown_unit_or_empty_subject_yields_no_query():
    assert dr.build_query("Israel", "not_a_unit", datetime(2026, 9, 5).date()) == ""
    assert dr.build_query("", "escalation", datetime(2026, 9, 5).date()) == ""


def test_subject_uses_the_newsroom_spelling_not_the_gazetteer():
    """The gazetteer alone is wrong on the wire: GB returns "United Kingdom"
    and the reporting says "Britain"; TR returns "Türkiye" and it says
    "Turkey". A query built from the gazetteer asks a different question."""
    assert dr.target_subject(
        target_id="country_g20_tr", name=None, geo=["TR"],
        iso_names={"TR": "Türkiye"},
    ) == "Turkey"
    assert dr.target_subject(
        target_id="country_g20_gb", name=None, geo=["GB"],
        iso_names={"GB": "United Kingdom"},
    ) == "Britain"
    # A multi-geo lane has no single country -> its own descriptor name.
    assert dr.target_subject(
        target_id="lane_hormuz", name="Lane — Strait of Hormuz",
        geo=["IR", "OM", "AE"], iso_names={},
    ) == "Lane — Strait of Hormuz"
    # No name either -> a de-slugged id. Deterministic in every branch.
    assert dr.target_subject(
        target_id="flow_energy_shipping", name=None, geo=[], iso_names={},
    ) == "flow energy shipping"


# ---------------------------------------------------------------------------
# 3. The SUBSTRATE FENCE
# ---------------------------------------------------------------------------


def test_the_prompt_builder_is_substrate_free():
    """ZERO substrate rows may enter the reference prompt (design D-d). If our
    own feeds seeded the reference, the collection diff would be circular by
    construction: the instrument would be asking whether we collected what we
    told it about."""
    dr.assert_prompt_builder_is_substrate_free()


def test_the_fence_catches_a_substrate_name(monkeypatch):
    """The guard is real, not decorative: give it a function that touches
    ``deps.pg_pool`` and it must raise."""

    def build_reference_prompt(
        *, bounded_question, subject, window_start, window_end, results
    ):
        rows = deps.pg_pool  # noqa: F821 — the breach under test
        return f"{bounded_question}{rows}"

    monkeypatch.setattr(dr, "build_reference_prompt", build_reference_prompt)
    with pytest.raises(RuntimeError, match="substrate accessor"):
        dr.assert_prompt_builder_is_substrate_free()


def test_the_fence_catches_a_new_parameter(monkeypatch):
    """A new keyword is how a slice gets in WITHOUT naming a forbidden
    accessor, so the signature is pinned too."""

    def build_reference_prompt(
        *, bounded_question, subject, window_start, window_end, results,
        slice_rows=(),
    ):
        return f"{bounded_question}{slice_rows}"

    monkeypatch.setattr(dr, "build_reference_prompt", build_reference_prompt)
    with pytest.raises(RuntimeError, match="signature changed"):
        dr.assert_prompt_builder_is_substrate_free()


def test_the_prompt_carries_the_question_the_window_and_the_results_only():
    now = datetime(2026, 9, 5, 4, 22, tzinfo=timezone.utc)
    prompt = dr.build_reference_prompt(
        bounded_question="What is this country's near-term escalation risk?",
        subject="Israel",
        window_start=now - timedelta(hours=24),
        window_end=now,
        results=[
            {"title": "Strike reported", "url": "https://a.example/1",
             "snippet": "A strike was reported.", "published_at": "2026-09-04"},
        ],
    )
    assert "near-term escalation risk" in prompt
    assert "Israel" in prompt
    assert "https://a.example/1" in prompt
    assert "2026-09-05T04:22" in prompt


# ---------------------------------------------------------------------------
# 4. The URL fence
# ---------------------------------------------------------------------------

_ALLOWED = ["https://a.example/1", "https://b.example/2"]


def _reply(items: list[dict[str, Any]]) -> str:
    return json.dumps({"items": items})


def _item(**kw: Any) -> dict[str, Any]:
    base = {
        "headline": "Strike reported",
        "sentence": "A strike was reported on 2026-09-04.",
        "entities": ["Iran"],
        "urls": ["https://a.example/1"],
        "published_at": "2026-09-04",
        "materiality": "high",
    }
    base.update(kw)
    return base


def test_an_off_result_url_is_dropped_and_counted_never_repaired():
    """THE ZERO-FP ARM. An item whose URLs did not come from the search result
    set never enters either diff — it is dropped WHOLE, not repaired by
    stripping the bad URL, because an item whose evidence we cannot locate is
    not evidence."""
    items, fenced, err = dr.parse_items_reply(
        _reply([
            _item(),
            _item(headline="Fabricated", urls=["https://evil.example/x"]),
            # Mixed: one good URL and one invented. Still dropped WHOLE.
            _item(headline="Half-fabricated",
                  urls=["https://a.example/1", "https://evil.example/y"]),
        ]),
        allowed_urls=_ALLOWED, cap=5,
    )
    assert err == ""
    assert [i["headline"] for i in items] == ["Strike reported"]
    assert fenced == 2


def test_an_item_with_no_urls_is_fenced_too():
    """The reference is a URL-bearing artefact by construction (D-e): an
    unsourced item cannot be re-checked, so it is not a reference item."""
    items, fenced, _ = dr.parse_items_reply(
        _reply([_item(urls=[])]), allowed_urls=_ALLOWED, cap=5
    )
    assert items == [] and fenced == 1


def test_the_fence_ignores_a_trailing_slash_but_nothing_cleverer():
    """The fence is an EQUALITY check against a set we handed the model seconds
    earlier. Anything cleverer than whitespace-and-slash starts admitting URLs
    the model EDITED, which is the failure it exists to catch."""
    items, fenced, _ = dr.parse_items_reply(
        _reply([_item(urls=["https://a.example/1/"])]),
        allowed_urls=_ALLOWED, cap=5,
    )
    assert len(items) == 1 and fenced == 0
    items, fenced, _ = dr.parse_items_reply(
        _reply([_item(urls=["https://a.example/1?utm=x"])]),
        allowed_urls=_ALLOWED, cap=5,
    )
    assert items == [] and fenced == 1


def test_items_are_capped_and_ordinals_are_dense():
    items, _fenced, _err = dr.parse_items_reply(
        _reply([_item(headline=f"h{i}") for i in range(9)]),
        allowed_urls=_ALLOWED, cap=5,
    )
    assert len(items) == 5
    assert [i["ordinal"] for i in items] == [1, 2, 3, 4, 5]


def test_materiality_is_code_validated_against_the_vocabulary():
    items, _f, _e = dr.parse_items_reply(
        _reply([_item(materiality="EXTREMELY HIGH")]),
        allowed_urls=_ALLOWED, cap=5,
    )
    assert items[0]["materiality"] in dr.MATERIALITY_VOCABULARY


def test_an_unparseable_reply_degrades_to_empty_with_a_reason():
    for raw in ("", "I cannot help with that.", "{not json"):
        items, fenced, err = dr.parse_items_reply(
            raw, allowed_urls=_ALLOWED, cap=5
        )
        assert items == [] and fenced == 0 and err, raw


def test_json_wrapped_in_prose_is_still_recovered():
    items, _f, err = dr.parse_items_reply(
        "Here you go:\n" + _reply([_item()]) + "\nHope that helps.",
        allowed_urls=_ALLOWED, cap=5,
    )
    assert err == "" and len(items) == 1


# ---------------------------------------------------------------------------
# 5. The liveness ladder — all five documented web_tools outcomes
# ---------------------------------------------------------------------------


def _outcome(**kw: Any):
    base = dict(
        admitted=True, block_cause=None, tool_status="completed", error="",
        output={},
    )
    base.update(kw)
    return dr.classify_search_outcome(**base)


def test_liveness_ladder_outcome_1_served():
    status, results, _bits = _outcome(
        output={"results": [{"url": "https://a.example/1"}], "degraded": False,
                "count": 1},
    )
    assert status == dr.STATUS_OK and len(results) == 1


def test_liveness_ladder_outcome_2_verified_empty_is_nothing_material():
    """``count=0`` with ``supports_absence_claim=true`` — the control probe
    verified the engine set is answering, so the empty is REAL for this query.
    An honest ``nothing_material`` reference, and it IS scorable."""
    status, results, _b = _outcome(
        output={"results": [], "count": 0, "status": "empty_verified",
                "supports_absence_claim": True},
    )
    assert status == dr.STATUS_NOTHING_MATERIAL
    assert results == []
    assert status in dr.SCORABLE_STATUSES


def test_liveness_ladder_outcome_3_partial_service_declines():
    """Usable hits, but ``supports_absence_claim=false``: the missing engines
    could have carried exactly the story the desk skipped. A metric computed
    over a reference that may be missing its most important item is worse than
    no metric."""
    status, results, _b = _outcome(
        output={"results": [{"url": "https://a.example/1"}], "degraded": True,
                "count": 1, "unresponsive_engines": ["x"],
                "supports_absence_claim": False},
    )
    assert status == dr.STATUS_DEGRADED
    assert status not in dr.SCORABLE_STATUSES
    assert results, "a degraded search still carries its hits onto the receipt"


def test_liveness_ladder_outcome_4_degraded_no_results():
    status, _r, bits = _outcome(
        tool_status="failed",
        error="search_degraded_no_results: the provider reported PARTIAL service",
        output={"deferral": {"defer": True}},
    )
    assert status == dr.STATUS_DEGRADED
    assert status not in dr.SCORABLE_STATUSES
    assert "deferral" in bits


def test_liveness_ladder_outcome_5_liveness_unverified():
    status, _r, _b = _outcome(
        tool_status="failed",
        error="search_liveness_unverified: a control probe could not show the "
              "engine set answering",
    )
    assert status == dr.STATUS_UNVERIFIED
    assert status not in dr.SCORABLE_STATUSES


def test_a_gate_block_and_an_unlicensed_empty_are_both_unverified():
    """"The search did not happen" is a DIFFERENT fact from "the world was
    quiet", and the only honest thing to do with it is decline."""
    status, _r, bits = _outcome(admitted=False, block_cause="governor_budget")
    assert status == dr.STATUS_UNVERIFIED
    assert bits["gate_block_cause"] == "governor_budget"
    # A completed-but-empty result with NO absence licence.
    status, _r, _b = _outcome(output={"results": [], "count": 0})
    assert status == dr.STATUS_UNVERIFIED


def test_exactly_two_statuses_are_scorable():
    assert dr.SCORABLE_STATUSES == {dr.STATUS_OK, dr.STATUS_NOTHING_MATERIAL}


# ---------------------------------------------------------------------------
# The rotation
# ---------------------------------------------------------------------------


def test_rotation_is_deterministic_and_wraps():
    pairs = [(f"t{i}", "escalation") for i in range(5)]
    taken, cursor = dr.rotate_pairs(pairs, cursor=0, take=2)
    assert taken == pairs[:2] and cursor == 2
    taken, cursor = dr.rotate_pairs(pairs, cursor=cursor, take=2)
    assert taken == pairs[2:4] and cursor == 4
    taken, cursor = dr.rotate_pairs(pairs, cursor=cursor, take=2)
    assert taken == [pairs[4], pairs[0]] and cursor == 1


def test_a_cap_at_or_above_the_population_takes_everything_and_resets():
    """v1's shape: 38 pairs against a 40-pair cap, so one run covers all of them
    and the cursor cannot drift."""
    pairs = [(f"t{i}", "escalation") for i in range(38)]
    taken, cursor = dr.rotate_pairs(pairs, cursor=7, take=40)
    assert taken == pairs and cursor == 0


def test_rotation_degrades_on_an_empty_population():
    assert dr.rotate_pairs([], cursor=3, take=5) == ([], 3)


def test_the_utc_day_window_is_a_stable_per_day_key():
    """``window_start`` is the day boundary rather than ``now - 24h``, because
    it is the third column of migration 0191's unique index — that is what makes
    "one reference per (target, unit, UTC day)" a schema fact."""
    a, _ = dr.utc_day_window(
        datetime(2026, 9, 5, 4, 22, tzinfo=timezone.utc), window_hours=24
    )
    b, _ = dr.utc_day_window(
        datetime(2026, 9, 5, 23, 59, tzinfo=timezone.utc), window_hours=24
    )
    assert a == b == datetime(2026, 9, 5, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# The flag
# ---------------------------------------------------------------------------


def test_flag_defaults_off(monkeypatch):
    monkeypatch.delenv(dr.ENABLED_ENV, raising=False)
    assert dr.reference_enabled() is False
    for on in ("1", "true", "YES", "on"):
        monkeypatch.setenv(dr.ENABLED_ENV, on)
        assert dr.reference_enabled() is True
    monkeypatch.setenv(dr.ENABLED_ENV, "0")
    assert dr.reference_enabled() is False


def test_the_pipeline_stamp_is_its_own_and_pools_with_nothing():
    """A reference is different evidence about a different question from a
    faithfulness verdict or an external-audit verdict; a mean across two of them
    describes a population that never existed."""
    from legba.data.analysts.deterministic_handlers.standing_auditor import (
        EXTERNAL_AUDIT_PIPELINE_VERSION,
    )
    from legba.data.provenance.verify import JUDGE_PIPELINE_VERSION

    assert dr.REFERENCE_PIPELINE_VERSION not in (
        JUDGE_PIPELINE_VERSION, EXTERNAL_AUDIT_PIPELINE_VERSION
    )
    assert dr.REFERENCE_LABELED_BY.startswith(dr.REFERENCE_LABELED_BY_PREFIX)


# ---------------------------------------------------------------------------
# F-2 — the labeled_by clause, and the BYTE-IDENTITY PROOF
# ---------------------------------------------------------------------------


def test_f2_the_scorer_read_is_gated_on_the_labeled_by_prefix():
    assert "labeled_by" in ucs._LABELS_SQL
    assert "NOT LIKE" in ucs._LABELS_SQL
    assert ucs.REFERENCE_LABELED_BY_PREFIX == dr.REFERENCE_LABELED_BY_PREFIX


def test_f2_the_operator_axis_excludes_both_machine_prefixes():
    """The A-3 sampler writes its sampling frame into ``correctness_labels``
    too, and an UNGRADED sample is not an operator verdict."""
    from legba.data import correctness_axis

    assert correctness_axis.MACHINE_LABELED_BY_PREFIXES == (
        "desk_reference/", "desk_reference_sample/",
    )
    for prefix in correctness_axis.MACHINE_LABELED_BY_PREFIXES:
        assert f"NOT LIKE '{prefix}%'" in correctness_axis.UNIT_LABELS_SQL
        assert f"NOT LIKE '{prefix}%'" in correctness_axis.ONE_UNIT_LABELS_SQL


# ---------------------------------------------------------------------------
# Ephemeral-DB lifecycle — through the REAL binding path
# ---------------------------------------------------------------------------

_TARGET = "country_watch_zz_ref"
_UNIT = "escalation"


@pytest_asyncio.fixture
async def pg_pool(migrated_pg: PostgresConfig):
    pool = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=4)
    yield pool
    await pool.close()


@pytest_asyncio.fixture
async def clean_slate(pg_pool):
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM unit_reference_labels WHERE labeled_by LIKE $1",
            f"{dr.REFERENCE_LABELED_BY_PREFIX}%",
        )
        await conn.execute(
            "DELETE FROM correctness_labels "
            "WHERE finding_snapshot->>'instrument' = 'desk_reference'"
        )
        await conn.execute(
            "DELETE FROM alert_trigger_watermarks WHERE trigger_class = $1",
            dr.WATERMARK_CLASS,
        )
        await conn.execute(
            "DELETE FROM analyst_outputs WHERE target_id = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM analyst_traces WHERE target_id = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM target_descriptors WHERE descriptor_id = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM analyst_descriptors WHERE descriptor_id = $1", _UNIT
        )
        # A-4's two products, scoped to this file's own desk / writer.
        await conn.execute(
            "DELETE FROM collection_requirements WHERE desk = $1", _TARGET
        )
        await conn.execute(
            "DELETE FROM hypotheses WHERE analyst_id = $1",
            rgd.DISPATCH_ANALYST_ID,
        )
        await conn.execute(
            "DELETE FROM signals WHERE source_id = $1", _SLICE_SOURCE
        )
    yield


class _Deps:
    def __init__(self, pool: Any, extras: dict[str, Any] | None = None) -> None:
        self.pg_pool = pool
        self.extras = dict(extras or {})


class _FakeLLM:
    """A bounded stand-in for the out-of-plane model. Records what it was asked
    so the substrate fence can be checked on the LIVE prompt, not only on the
    builder's source."""

    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.prompts: list[str] = []

    async def chat_complete(self, messages, **kw: Any):
        self.prompts.append(messages[0]["content"])

        class _Usage:
            model = "fake-gemma-4-31b"

        class _Resp:
            content = self.reply
            usage = _Usage()

        return _Resp()


class _FakeBinding:
    """A stand-in for the ``web_access`` AgencyToolBinding."""

    def __init__(self, output: dict[str, Any], *, status: str = "completed",
                 error: str = "", admitted: bool = True) -> None:
        self.output = output
        self.status = status
        self.error = error
        self.admitted = admitted
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, name: str, args: dict[str, Any]):
        self.calls.append({"tool": name, **args})
        outer = self

        class _Result:
            status = outer.status
            error = outer.error
            output = outer.output

        class _Outcome:
            admitted = outer.admitted
            block_cause = None
            tool_result = _Result()

        return _Outcome()


_SEARCH_OK = {
    "results": [
        {"title": "Iran strike reported", "url": "https://a.example/1",
         "snippet": "Iran was reported to have struck a site.",
         "published_at": "2026-09-04"},
        {"title": "Tehran responds", "url": "https://b.example/2",
         "snippet": "Tehran responded to the reports."},
    ],
    "degraded": False,
    "count": 2,
    "provider": "search.searxng.local",
}


async def _seed_unit_descriptor(conn: Any) -> None:
    """The test DB is a fresh migrated DB with no REGISTERED analyst descriptors
    (those land at bringup, not from a migration), so the handler's bounded-
    question lookup would drop the unit and the census would be empty. Seed one
    with a live method.bounded_question, exactly as the 09-05 PUT did."""
    await conn.execute(
        """
        INSERT INTO analyst_descriptors
            (descriptor_id, version, is_head, kind, state, owner, name,
             schema_uri, type_signature, inherits, body)
        VALUES ($1, $2, TRUE, 'inline_target', 'active', 'test_a1', $1,
                'legba/analyst/1.0.0', '{}'::jsonb, '{}', $3::jsonb)
        ON CONFLICT DO NOTHING
        """,
        _UNIT, "0" * 16,
        json.dumps({
            "method": {
                "bounded_question": (
                    "What is this country's near-term escalation risk, and "
                    "where is it going?"
                )
            }
        }),
    )


async def _seed_target(conn: Any) -> None:
    await conn.execute(
        """
        INSERT INTO target_descriptors
            (descriptor_id, version, is_head, state, owner, body, schema_uri,
             name, abstraction_level, inherits)
        VALUES ($1, $2, TRUE, 'active', 'test_a1', $3::jsonb,
                'legba/target/2.0.0', 'Test Desk', 'L1', '{}')
        ON CONFLICT DO NOTHING
        """,
        _TARGET, "0" * 16,
        json.dumps({
            "identity": {"id": _TARGET, "name": "Test Desk"},
            "scope": {"geo": ["IL"]},
        }),
    )


async def _seed_head(conn: Any, *, title: str, body: str) -> UUID:
    row_id = uuid4()
    await conn.execute(
        """
        INSERT INTO analyst_outputs
            (id, analyst_id, analyst_version, run_id, target_id, kind, title,
             body, confidence, data, produced_at, schema_uri)
        VALUES ($1, $2, $3, $4, $5, 'finding', $6, $7, 1.0, '{}'::jsonb,
                now(), $8)
        """,
        row_id, _UNIT, "0" * 16, uuid4(), _TARGET, title, body,
        "iglu:legba/finding/jsonschema/1-0-0",
    )
    return row_id


async def _run(pool: Any, *, extras: dict[str, Any] | None = None, **opts: Any):
    """Drive the handler through ``deterministic.run_method`` — the REAL binding
    path the runtime uses, never a direct module call."""
    options = {
        "sub_handler": "desk_reference",
        "analyst_id": "desk_reference",
        "run_id": str(uuid4()),
        "reference_targets": [_TARGET],
        **opts,
    }
    result = await deterministic.run_method([], options, _Deps(pool, extras))
    assert isinstance(result, AnalystMethodResult)
    return result


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flag_off_writes_absolutely_nothing(pg_pool, clean_slate, monkeypatch):
    """LIFECYCLE 1 — TOTAL BYTE-IDENTITY WHEN OFF.

    No row, no search, no LLM call, no watermark. The only artefact is a receipt
    saying the flag is off, and the descriptor's own ``state: draft`` keeps even
    that out of the substrate until an operator activates it.
    """
    monkeypatch.delenv(dr.ENABLED_ENV, raising=False)
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
    binding = _FakeBinding(_SEARCH_OK)
    llm = _FakeLLM(_reply([_item()]))
    result = await _run(pg_pool, extras={
        dr.LLM_DEPS_EXTRA_KEY: llm,
        dr.WEB_BINDING_DEPS_EXTRA_KEY: binding,
    })
    assert result.finding.data["enabled"] is False
    assert binding.calls == [], "a disabled instrument must not spend a search"
    assert llm.prompts == [], "a disabled instrument must not spend a model call"
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_reference_labels WHERE labeled_by LIKE $1",
            f"{dr.REFERENCE_LABELED_BY_PREFIX}%",
        ) == 0
        assert await conn.fetchval(
            "SELECT count(*) FROM alert_trigger_watermarks WHERE trigger_class=$1",
            dr.WATERMARK_CLASS,
        ) == 0
    assert result.usage["prompt_tokens"] == 0


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_reference_row_lands_with_its_fence_and_stamp(
    pg_pool, clean_slate, monkeypatch
):
    """LIFECYCLE 2 — the row, the fence, the stamp, and the prompt's fence.

    One search, one bounded model call, one row on migration 0057's table with
    0191's columns populated; the off-result item dropped and counted; and the
    LIVE prompt checked for substrate leakage rather than only the builder's
    source.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_head(conn, title="Coalition politics", body="Nothing here.")
        # A trace so the pair census sees the (target, unit) pair as live.
        await conn.execute(
            """
            INSERT INTO analyst_traces
                (run_id, analyst_id, analyst_version, target_id, cadence_trigger,
                 status, run_started_at, receipt_hash)
            VALUES ($1, $2, $3, $4, 'test', 'success', now(), 'h')
            """,
            uuid4(), _UNIT, "0" * 16, _TARGET,
        )
    binding = _FakeBinding(_SEARCH_OK)
    llm = _FakeLLM(_reply([
        _item(),
        _item(headline="Fabricated", urls=["https://evil.example/x"]),
    ]))
    result = await _run(pg_pool, extras={
        dr.LLM_DEPS_EXTRA_KEY: llm,
        dr.WEB_BINDING_DEPS_EXTRA_KEY: binding,
    })
    assert result.finding.data["enabled"] is True
    assert len(binding.calls) == 1
    assert binding.calls[0]["tool"] == "web_search"
    # The query is the fixed table's, replayable, and carries the date.
    assert "escalation risk military tension conflict" in binding.calls[0]["query"]

    # THE LIVE PROMPT carries the results and NOTHING from our substrate.
    assert len(llm.prompts) == 1
    prompt = llm.prompts[0]
    assert "https://a.example/1" in prompt
    assert "Coalition politics" not in prompt, (
        "our own desk head reached the reference prompt — the collection diff "
        "would be circular"
    )

    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT * FROM unit_reference_labels WHERE labeled_by LIKE $1",
            f"{dr.REFERENCE_LABELED_BY_PREFIX}%",
        )
    assert row is not None
    assert row["unit_analyst_id"] == _UNIT
    assert row["target_id"] == _TARGET
    assert row["status"] == dr.STATUS_OK
    assert row["labeled_by"] == dr.REFERENCE_LABELED_BY
    assert row["window_start"] is not None
    items = json.loads(row["items"]) if isinstance(row["items"], str) else row["items"]
    assert [i["headline"] for i in items] == ["Strike reported"]
    prov = (
        json.loads(row["provenance"]) if isinstance(row["provenance"], str)
        else row["provenance"]
    )
    assert prov["items_url_fenced"] == 1
    assert prov["pipeline_version"] == dr.REFERENCE_PIPELINE_VERSION
    assert prov["model"] == "fake-gemma-4-31b"
    assert prov["query"] == binding.calls[0]["query"]


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_second_run_the_same_day_writes_no_second_row(
    pg_pool, clean_slate, monkeypatch
):
    """LIFECYCLE 3 — one reference per (target, unit, UTC day).

    The design's D-a grain, enforced by migration 0191's partial unique index
    rather than by a handler convention. The handler ALSO reads the day's rows
    first, so the re-run skips before it burns a paid out-of-plane call — the
    index would reject the write anyway and discovering that with a search is
    waste an ungraded instrument cannot afford.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_head(conn, title="A head", body="Body.")
        await conn.execute(
            """
            INSERT INTO analyst_traces
                (run_id, analyst_id, analyst_version, target_id, cadence_trigger,
                 status, run_started_at, receipt_hash)
            VALUES ($1, $2, $3, $4, 'test', 'success', now(), 'h')
            """,
            uuid4(), _UNIT, "0" * 16, _TARGET,
        )
    extras = {
        dr.LLM_DEPS_EXTRA_KEY: _FakeLLM(_reply([_item()])),
        dr.WEB_BINDING_DEPS_EXTRA_KEY: _FakeBinding(_SEARCH_OK),
    }
    await _run(pg_pool, extras=extras)
    second_binding = _FakeBinding(_SEARCH_OK)
    second_llm = _FakeLLM(_reply([_item()]))
    await _run(pg_pool, extras={
        dr.LLM_DEPS_EXTRA_KEY: second_llm,
        dr.WEB_BINDING_DEPS_EXTRA_KEY: second_binding,
    })
    assert second_binding.calls == [], (
        "the re-run spent a search on a pair it had already referenced today"
    )
    assert second_llm.prompts == []
    async with pg_pool.acquire() as conn:
        assert await conn.fetchval(
            "SELECT count(*) FROM unit_reference_labels WHERE labeled_by LIKE $1",
            f"{dr.REFERENCE_LABELED_BY_PREFIX}%",
        ) == 1


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_degraded_search_writes_an_honest_row_and_never_a_zero(
    pg_pool, clean_slate, monkeypatch
):
    """LIFECYCLE 4 — a search outage must never read as a quiet world.

    The row lands with ``status='unverified_liveness'`` and zero items, the
    model is never called (there is nothing to write a reference FROM), and both
    diffs decline for that pair — ``None``, never ``0.0``.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_head(conn, title="A head", body="Body.")
        await conn.execute(
            """
            INSERT INTO analyst_traces
                (run_id, analyst_id, analyst_version, target_id, cadence_trigger,
                 status, run_started_at, receipt_hash)
            VALUES ($1, $2, $3, $4, 'test', 'success', now(), 'h')
            """,
            uuid4(), _UNIT, "0" * 16, _TARGET,
        )
    llm = _FakeLLM(_reply([_item()]))
    result = await _run(pg_pool, extras={
        dr.LLM_DEPS_EXTRA_KEY: llm,
        dr.WEB_BINDING_DEPS_EXTRA_KEY: _FakeBinding(
            {"deferral": {"defer": True}}, status="failed",
            error="search_liveness_unverified: control probe could not answer",
        ),
    })
    assert llm.prompts == [], "no reference can be written from a dead search"
    async with pg_pool.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT status, items FROM unit_reference_labels "
            "WHERE labeled_by LIKE $1", f"{dr.REFERENCE_LABELED_BY_PREFIX}%",
        )
    assert row["status"] == dr.STATUS_UNVERIFIED
    gauge = result.finding.data.get("attention_gauge") or {}
    key = f"{_TARGET}|{_UNIT}"
    assert gauge[key]["declined"] is True
    assert gauge[key]["attention_rate"] is None
    coll = result.finding.data["collection_gauge"][key]
    assert coll["collection_recall"] is None


@pytest.mark.integration
@pytest.mark.asyncio
async def test_f2_scorer_output_is_byte_identical_with_machine_rows_present(
    pg_pool, clean_slate
):
    """**THE F-2 BYTE-IDENTITY PROOF.**

    ``unit_correctness_scorer``'s secondary axis has reported ``None`` every day
    of its life because ``unit_reference_labels`` held one row for a retired
    analyst with zero ``canonical_source_ids``. A-1 now fills that table daily
    with rows that DO carry real ``canonical_source_ids`` — and without the
    ``labeled_by`` clause the axis would silently start reporting a MACHINE
    number under the key built for OPERATOR labels.

    So: run the scorer, insert a machine reference row with real
    ``canonical_source_ids`` for a unit it scores, run it again, and assert the
    two payloads are byte-identical as JSON.
    """
    scorer_units = ["escalation"]
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        # Seeded BEFORE the first score deliberately: the ONLY thing that
        # changes between the two payloads must be the machine reference row.
        head = await _seed_head(conn, title="A head", body="Body.")

    async def _score() -> str:
        result = await deterministic.run_method(
            [],
            {"sub_handler": "unit_correctness_scorer",
             "analyst_id": "unit_correctness_scorer",
             "run_id": str(uuid4()), "units": scorer_units},
            _Deps(pg_pool),
        )
        return json.dumps(result.finding.data, sort_keys=True, default=str)

    before = await _score()

    async with pg_pool.acquire() as conn:
        sig_ids = [uuid4(), uuid4()]
        await conn.execute(
            """
            INSERT INTO unit_reference_labels
                (id, unit_analyst_id, target_id, reference_answer,
                 canonical_source_ids, labeled_by, window_start, window_end,
                 items, status, provenance)
            VALUES ($1, $2, $3, $4, $5::uuid[], $6, $7, $8, '[]'::jsonb, 'ok',
                    '{}'::jsonb)
            """,
            uuid4(), "escalation", _TARGET, "A machine reference.", sig_ids,
            dr.REFERENCE_LABELED_BY,
            datetime(2026, 9, 5, tzinfo=timezone.utc),
            datetime(2026, 9, 6, tzinfo=timezone.utc),
        )
        # And a sample row in correctness_labels, which the operator axis's own
        # clause must exclude for the same reason.
        await conn.execute(
            """
            INSERT INTO correctness_labels
                (id, finding_id, unit_analyst_id, target_id, label, labeled_by,
                 finding_snapshot)
            VALUES ($1, $2, $3, $4, 'correct', $5, $6::jsonb)
            """,
            uuid4(), head, "escalation", _TARGET,
            "desk_reference_sample/" + dr.REFERENCE_PIPELINE_VERSION,
            json.dumps({"instrument": "desk_reference"}),
        )

    after = await _score()
    assert before == after, (
        "writing desk_reference rows CHANGED unit_correctness_scorer's output — "
        "the F-2 labeled_by gate is not holding, and a machine-authored number "
        "is now reporting under the operator-label key"
    )

    # And prove the gate is what did it: without the clause the row IS visible.
    async with pg_pool.acquire() as conn:
        visible = await conn.fetch(
            "SELECT id FROM unit_reference_labels WHERE unit_analyst_id = $1",
            "escalation",
        )
        gated = await conn.fetch(
            ucs._LABELS_SQL, "escalation",
            f"{dr.REFERENCE_LABELED_BY_PREFIX}%",
        )
    assert len(visible) >= 1, "the machine row must genuinely exist"
    assert len(gated) < len(visible), "the clause excluded nothing"


# ---------------------------------------------------------------------------
# A-4 — THE REFERENCE-GAP DISPATCH, through the REAL binding path
#
# Every test below drives `deterministic.run_method({"sub_handler":
# "desk_reference", ...})` against a live pool — never a direct module call —
# because A-4's whole contract is about what the SWEEP does, and a helper-level
# test cannot see the flag read, the gap_sink wiring or the receipt splice.
# ---------------------------------------------------------------------------


@pytest.fixture
def dispatch_on(monkeypatch):
    monkeypatch.setenv(rgd.ENABLED_ENV, "1")
    # Leg 2 is ADDITIONALLY gated on the research regime: dispatching into a
    # backlog whose researcher has no web leg is a question nothing can answer.
    monkeypatch.setenv(rr.RESEARCH_EVIDENCE_ENV, rr.REGIME_SUBSTRATE)


@pytest.fixture
def dispatch_off(monkeypatch):
    monkeypatch.delenv(rgd.ENABLED_ENV, raising=False)


async def _seed_reference_row(
    conn: Any,
    *,
    items: list[dict[str, Any]],
    status: str = dr.STATUS_OK,
    unit: str = _UNIT,
    when: datetime | None = None,
) -> UUID:
    """One machine reference row for the window the next sweep will diff.

    A-4 fires on the diff, not on the write, so a seeded reference plus an
    EMPTY slice is the whole precondition: every item is uncollected, because
    there is no signal for any collection arm to match.
    """
    now = when or datetime.now(timezone.utc)
    window_start, window_end = dr.utc_day_window(now, window_hours=24)
    row_id = uuid4()
    await conn.execute(
        """
        INSERT INTO unit_reference_labels
            (id, unit_analyst_id, target_id, reference_answer, labeled_by,
             window_start, window_end, items, status, provenance)
        VALUES ($1,$2,$3,$4,$5,$6,$7,$8::jsonb,$9,'{}'::jsonb)
        """,
        row_id, unit, _TARGET, "A machine reference.", dr.REFERENCE_LABELED_BY,
        window_start, window_end, json.dumps(items), status,
    )
    return row_id


_SLICE_SOURCE = "source.a4.slice_probe"


async def _seed_slice_row(conn: Any, *, url: str, unit: str = _UNIT) -> UUID:
    """One signal, reachable as the desk's SLICE the way the diff reads it:
    ``analyst_traces.input_row_refs`` for the latest successful run of the
    (target, unit) pair inside the window, joined to ``signals``. NOT
    ``prompt_rendered`` — the diff deliberately never reads that."""
    signal_id = uuid4()
    await conn.execute(
        """
        INSERT INTO signals
            (id, source_id, source_version, produced_by_kind, fetched_at,
             owner_tenant, modality, retention_class, payload, raw_provenance,
             geo, tags, entity_classes, content_hash, derived_from, schema_uri,
             canonical_url)
        VALUES ($1,$2,'v1','test',now(),'t','text','standard',$3::jsonb,
                '{}'::jsonb,'{IL}','{}','{}',$4,'{}',
                'iglu:legba/signal/jsonschema/1-0-0',$5)
        """,
        signal_id, _SLICE_SOURCE,
        json.dumps({
            "title": "Border clash reported overnight",
            "summary": "Lebanon and Israel exchanged fire.",
            "entities": [{"text": "Lebanon", "confidence": 0.9}],
        }),
        str(signal_id), url,
    )
    await conn.execute(
        """
        INSERT INTO analyst_traces
            (run_id, analyst_id, analyst_version, target_id, cadence_trigger,
             status, run_started_at, receipt_hash, input_row_refs)
        VALUES ($1,$2,$3,$4,'test','success',now(),'h',$5::uuid[])
        """,
        uuid4(), unit, "0" * 16, _TARGET, [signal_id],
    )
    return signal_id


def _ref_item(**kw: Any) -> dict[str, Any]:
    """A PERSISTED reference item — the post-parse shape, entity_folds stamped
    by the handler through the shared canon."""
    base = {
        "ordinal": 1,
        "headline": "Border clash reported",
        "sentence": "Lebanon reported a border clash overnight.",
        "entities": ["Lebanon"],
        "entity_folds": ["lebanon"],
        "urls": ["https://a.example/1"],
        "published_at": "2026-09-05",
        "materiality": "high",
    }
    base.update(kw)
    return base


async def _requirements(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT * FROM collection_requirements WHERE desk = $1 "
        "ORDER BY natural_key",
        _TARGET,
    )


async def _open_questions(conn: Any) -> list[Any]:
    return await conn.fetch(
        "SELECT id, thesis, target_id, status, diagnostic_evidence "
        "FROM hypotheses WHERE analyst_id = $1 ORDER BY produced_at, id",
        rgd.DISPATCH_ANALYST_ID,
    )


def test_the_dispatch_flag_defaults_off_and_is_its_own(monkeypatch):
    """A-4 is gated SEPARATELY from A-1: the instrument can be measured for a
    while before it is allowed to create work, which is the whole reason the
    design gave it its own flag."""
    monkeypatch.delenv(rgd.ENABLED_ENV, raising=False)
    assert rgd.dispatch_enabled() is False
    assert rgd.ENABLED_ENV != dr.ENABLED_ENV
    for on in ("1", "true", "yes", "on", "TRUE", " On "):
        monkeypatch.setenv(rgd.ENABLED_ENV, on)
        assert rgd.dispatch_enabled() is True, on
    for off in ("0", "", "off", "no", "maybe"):
        monkeypatch.setenv(rgd.ENABLED_ENV, off)
        assert rgd.dispatch_enabled() is False, off


def test_the_off_payload_is_exactly_one_key():
    """The byte-identity contract, stated once in code so the call site cannot
    quietly widen it."""
    assert rgd.empty_payload() == {"enabled": False}


@pytest.mark.integration
@pytest.mark.asyncio
async def test_flag_off_writes_nothing_and_leaves_the_payload_byte_identical(
    pg_pool, clean_slate, monkeypatch, dispatch_off
):
    """THE FLAG-OFF BYTE-IDENTITY PROOF, on the path where it matters.

    The sweep runs with a reference row carrying an UNCOLLECTED, high-
    materiality item — the exact input A-4 fires on — and the payload is
    captured. Then A-4's own block is stripped and the remainder is compared,
    key for key and byte for byte, against the same run with the block removed:
    with the flag off, the ONLY difference A-4 makes to this analyst's finding
    is the added ``reference_gap_dispatch = {"enabled": false}``.

    The comparison is against the SECOND arm rather than a frozen literal on
    purpose: a literal would have to be re-blessed on every unrelated gauge
    change and would stop meaning anything.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        ref_id = await _seed_reference_row(conn, items=[_ref_item()])

    result = await _run(pg_pool)
    data = result.finding.data

    # The one added key, and it says exactly what the design says it says.
    assert data["reference_gap_dispatch"] == {"enabled": False}

    # Every OTHER key is untouched — including the gauge that DID see the gap.
    rest = {k: v for k, v in data.items() if k != "reference_gap_dispatch"}
    gauge = rest["collection_gauge"][f"{_TARGET}|{_UNIT}"]
    assert gauge["n_collected"] == 0 and gauge["n_anchorable"] == 1
    assert gauge["uncollected"][0]["headline"] == "Border clash reported"

    # Nothing was written by either leg.
    async with pg_pool.acquire() as conn:
        assert await _requirements(conn) == []
        assert await _open_questions(conn) == []
        # And the reference row itself is untouched.
        assert await conn.fetchval(
            "SELECT status FROM unit_reference_labels WHERE id = $1", ref_id
        ) == dr.STATUS_OK

    # Re-run with the flag still off: the payload MINUS A-4's block is stable,
    # byte for byte as JSON, which is the property the contract actually names.
    second = await _run(pg_pool)
    again = {k: v for k, v in second.finding.data.items()
             if k != "reference_gap_dispatch"}
    strip = ("ran_at", "window_start", "window_end")

    def _norm(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: ("<T>" if k in strip else _norm(v)) for k, v in obj.items()}
        if isinstance(obj, list):
            return [_norm(v) for v in obj]
        return obj

    assert json.dumps(_norm(rest), sort_keys=True, default=str) == json.dumps(
        _norm(again), sort_keys=True, default=str
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_an_uncollected_item_writes_a_requirement_and_opens_a_question(
    pg_pool, clean_slate, monkeypatch, dispatch_on
):
    """BOTH LEGS, end to end, through the real dispatcher.

    One uncollected, material item; one ``collection_requirements`` row with
    ``origin='reference_gap'`` citing the reference row; one ``open_question``
    carrying the shared marker vocabulary and the target's geo.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        ref_id = await _seed_reference_row(conn, items=[_ref_item()])

    result = await _run(pg_pool)
    block = result.finding.data["reference_gap_dispatch"]
    assert block["enabled"] is True
    assert block["candidates"] == 1 and block["selected"] == 1
    assert block["requirements_written"] == 1
    assert block["requirements_existing"] == 0
    assert block["questions_opened"] == 1
    assert block["questions_existing"] == 0
    assert block["failures"] == []

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
        questions = await _open_questions(conn)

    assert len(rows) == 1
    r = rows[0]
    assert r["natural_key"] == f"reference_gap:{_TARGET}:{_UNIT}:lebanon"
    assert r["origin"] == "reference_gap"
    assert r["dimension"] == _UNIT
    assert r["evidence_kind"] == "unit_reference_label"
    assert r["evidence_id"] == ref_id
    assert r["status"] == "proposed"
    assert r["suggested_fetch_url"] == "https://a.example/1"
    assert "Border clash reported" in r["topic"]
    # No feed in this DB matches -> honest unfillable, and the row is STILL
    # written (an unfillable requirement is itself intelligence).
    assert r["fillable"] is False
    assert r["unfillable_reason"] == "no_known_feed"

    assert len(questions) == 1
    q = questions[0]
    assert q["status"] == "open_question"
    assert q["target_id"] == _TARGET
    assert "REFERENCE GAP" in q["thesis"]
    # The CANONICAL spelling in the prose (item_names, in lockstep with the
    # folds) while every KEY stays built on the fold.
    assert "about Lebanon" in q["thesis"]
    marker = json.loads(q["diagnostic_evidence"])[0]
    assert marker["entity_fold"] == "lebanon"
    assert marker["origin"] == "harvest"
    assert marker["harvest_class"] == "reference_gap"
    # The desk's ISO geo — the researcher's ONLY reachability key.
    assert marker["geo"] == ["IL"]
    assert marker["evidence_id"] == str(ref_id)
    assert marker["unit"] == _UNIT


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_collected_item_is_not_a_gap_and_dispatches_nothing(
    pg_pool, clean_slate, monkeypatch, dispatch_on
):
    """THE NEGATIVE CONTROL. The same item, with a slice row the C1 URL arm
    matches exactly: collected, therefore not a gap, therefore no requirement
    and no question. A-4 must fire on the diff's verdict, not on the reference.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_reference_row(conn, items=[_ref_item()])
        await _seed_slice_row(conn, url="https://a.example/1")

    result = await _run(pg_pool)
    block = result.finding.data["reference_gap_dispatch"]
    assert block["candidates"] == 0 and block["requirements_written"] == 0
    assert block["questions_opened"] == 0
    gauge = result.finding.data["collection_gauge"][f"{_TARGET}|{_UNIT}"]
    assert gauge["n_collected"] == 1
    async with pg_pool.acquire() as conn:
        assert await _requirements(conn) == []
        assert await _open_questions(conn) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_low_materiality_gap_is_measured_but_never_dispatched(
    pg_pool, clean_slate, monkeypatch, dispatch_on
):
    """The gauge counts every uncollected item; only the material ones become
    WORK. The two numbers are deliberately different and the receipt shows
    both, so a reader can never mistake "not dispatched" for "not measured"."""
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_reference_row(conn, items=[_ref_item(materiality="low")])

    result = await _run(pg_pool)
    block = result.finding.data["reference_gap_dispatch"]
    assert block["candidates"] == 1 and block["selected"] == 0
    assert block["requirements_written"] == 0 and block["questions_opened"] == 0
    # ...but the gauge still measured it.
    gauge = result.finding.data["collection_gauge"][f"{_TARGET}|{_UNIT}"]
    assert gauge["n_collected"] == 0 and gauge["n_anchorable"] == 1
    async with pg_pool.acquire() as conn:
        assert await _requirements(conn) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_re_detection_tomorrow_writes_no_second_row_or_question(
    pg_pool, clean_slate, monkeypatch, dispatch_on
):
    """THE IDEMPOTENCY PROPERTY, through the sweep. A still-uncollected item
    seen again tomorrow — a NEW reference row for a NEW window — re-derives the
    same natural_key and the same containment key, and creates nothing."""
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_reference_row(conn, items=[_ref_item()])

    first = await _run(pg_pool)
    assert first.finding.data["reference_gap_dispatch"]["requirements_written"] == 1

    # Tomorrow: the same gap, a different reference row.
    async with pg_pool.acquire() as conn:
        await conn.execute(
            "DELETE FROM unit_reference_labels WHERE target_id = $1", _TARGET
        )
        second_ref = await _seed_reference_row(conn, items=[_ref_item()])
    second = await _run(pg_pool)
    block = second.finding.data["reference_gap_dispatch"]
    assert block["candidates"] == 1 and block["selected"] == 1
    assert block["requirements_written"] == 0
    assert block["requirements_existing"] == 1
    assert block["questions_opened"] == 0
    assert block["questions_existing"] == 1
    assert block["failures"] == []

    async with pg_pool.acquire() as conn:
        rows = await _requirements(conn)
        questions = await _open_questions(conn)
    assert len(rows) == 1 and len(questions) == 1
    # The row still cites the FIRST reference — the requirement is the gap's
    # identity, not the latest sighting of it.
    assert rows[0]["evidence_id"] != second_ref


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_degraded_reference_declines_and_dispatches_nothing(
    pg_pool, clean_slate, monkeypatch, dispatch_on
):
    """A non-scorable status short-circuits BOTH ratios to None — and A-4 rides
    that verdict rather than second-guessing it. Dispatching research off a
    reference the liveness ladder could not verify would be work created from
    an unreliable instrument, which is the failure the ladder exists to catch.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_reference_row(
            conn, items=[_ref_item()], status=dr.STATUS_UNVERIFIED
        )

    result = await _run(pg_pool)
    block = result.finding.data["reference_gap_dispatch"]
    assert block["candidates"] == 0
    gauge = result.finding.data["collection_gauge"][f"{_TARGET}|{_UNIT}"]
    assert gauge["declined"] is True
    async with pg_pool.acquire() as conn:
        assert await _requirements(conn) == []
        assert await _open_questions(conn) == []


@pytest.mark.integration
@pytest.mark.asyncio
async def test_a_side_write_failure_is_counted_and_never_fails_the_sweep(
    pg_pool, clean_slate, monkeypatch, dispatch_on
):
    """DEGRADE-NOT-BREAK, through the sweep. The gauge is this analyst's
    durable product; a dispatch failure lands in the receipt, not in an
    exception, and the finding is still returned with its measurement intact.
    """
    monkeypatch.setenv(dr.ENABLED_ENV, "1")
    async with pg_pool.acquire() as conn:
        await _seed_target(conn)
        await _seed_unit_descriptor(conn)
        await _seed_reference_row(conn, items=[_ref_item()])

    async def _boom(*_a: Any, **_k: Any) -> int:
        raise RuntimeError("collection_requirements is gone")

    monkeypatch.setattr(rgd.collection_gap, "write_requirements", _boom)

    result = await _run(pg_pool)
    block = result.finding.data["reference_gap_dispatch"]
    assert block["requirements_written"] == 0
    assert any("collection_requirements is gone" in f for f in block["failures"])
    # The OTHER leg still ran — losing one is no reason to lose the other.
    assert block["questions_opened"] == 1
    # And the measurement is intact.
    gauge = result.finding.data["collection_gauge"][f"{_TARGET}|{_UNIT}"]
    assert gauge["n_anchorable"] == 1

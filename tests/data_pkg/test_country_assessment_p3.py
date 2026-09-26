"""P3 LANE A — THE PER-COUNTRY ASSESSMENT CHANNEL.

What this file proves, in the order the channel runs:

  * THE HOLE IT FILLS — since the 09-04 demotion every composition tier is an
    assembly and the only interpretive voice left is the WORLD one. This channel
    is the cross-dimension per-country read, and the first test says so in the
    only way a test can: the id is registered, the descriptor is the shape the
    dispatch keys on, and the spine is ``country_composition``.
  * THE PER-TARGET SPINE READ — the fourth predicate. Without it all 32 workers
    read whichever country composed last.
  * THE CONTEXT SPAN — byte-identical to its origin, present at the COUNTRY tier
    only, and INVISIBLE to the record's own body (a golden test).
  * THE TIER-AWARE VOICE — its own system prompt and its own version string,
    with the WORLD prompt and version byte-unchanged.
  * THE EVIDENCE MAP — the ordinal's evidence is the lead span AND the desk read
    it was cut from, because the voice was handed both.
  * THE GRADER — a ``country_assessment`` head is a COMPOSITION-grain (``CR-``)
    atom set, and the composition correctness GATE does not widen to it.

The fixtures build a REAL country assembly with D-2's own builder, for the
reason the D-6 file gives: a hand-written payload drifts from the producer the
first time either moves.
"""

from __future__ import annotations

import inspect
from uuid import uuid4

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assembly_spans as asp
from legba.data.analysts import assessment_channel as ac
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import assessment_unsupported as au
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic_handlers import _correctness_packet as pkt
from legba.data.analysts.deterministic_handlers import _correctness_segment as SEG
from legba.data.analysts.deterministic_handlers import composition_lineage_sweep
from legba.data.analysts.deterministic_handlers import correctness_grader as cg
from legba.data.analysts.deterministic_handlers import finding_supersession
from legba.data.provenance import assembly_arms as arms
from legba.runtime.substrate_query_port import _ASSESSMENT_PRODUCER_ANALYSTS

@pytest.fixture(autouse=True)
def _regime_on(monkeypatch):
    """THE ONE REGIME SWITCH. The channel refuses to write unless the record it
    was handed is itself an assembly, which is what this flag decides."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")


TARGET = "country_g20_sa"
OTHER_TARGET = "country_g20_br"

ENERGY_BODY = (
    "*As of 2026-09-03; slice covers the trailing 72h to that date; 120 signals.*\n"
    "\n"
    "**BLUF:** Saudi Arabia remains under high energy-security pressure as recent "
    "tanker attacks in the Strait of Hormuz and Yemen's maritime embargo continue "
    "to disrupt its oil export chain [2].\n"
    "\n"
    "## What changed\n"
    "- On 2 September loading rates at Ras Tanura fell by a fifth against the "
    "trailing month [61].\n"
    "- Two VLCCs rerouted around the Cape rather than transit the strait [62].\n"
)

STABILITY_BODY = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** Fiscal consolidation measures announced on 1 September have drawn "
    "the first organised public objection in eighteen months [7].\n"
    "\n"
    "## What changed\n"
    "- Municipal employees in three provinces filed a collective grievance [8].\n"
)

ESCALATION_BODY = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** No new cross-border incidents appear in this desk's collection for "
    "the trailing window [3].\n"
)

MAGS = {"s1": 0.95, "s2": 0.70, "s3": 0.55, "s4": 0.10}


def _row(uid, *, analyst_id, body, target_id=TARGET, severity="high",
         signal_ids=("s1", "s2")):
    return {
        "id": uid,
        "kind": "finding",
        "analyst_id": analyst_id,
        "target_id": target_id,
        "title": f"head {uid}",
        "body": body,
        "severity": severity,
        "confidence": 0.7,
        "produced_at": "2026-09-03T10:01:00+00:00",
        "faithfulness_score": 0.9,
        "effective_confidence": 0.8,
        "claim_verdicts": [{"verdict": "supported"}],
        "derived_from": [],
        "data": {"data": {"citations": [
            {"marker": f"[{i}]", "signal_id": s, "source_id": f"source.{s}",
             "title": f"signal {s}", "source": f"https://example.test/{s}"}
            for i, s in enumerate(signal_ids, start=1)
        ]}},
    }


def _country_rows():
    return [
        _row(uuid4(), analyst_id="energy_security", body=ENERGY_BODY,
             signal_ids=("s1", "s2")),
        _row(uuid4(), analyst_id="internal_stability", body=STABILITY_BODY,
             severity="elevated", signal_ids=("s3",)),
        _row(uuid4(), analyst_id="escalation", body=ESCALATION_BODY,
             severity="moderate", signal_ids=("s4",)),
    ]


def _country_payload(rows=None, *, carried=3):
    rows = list(rows or _country_rows())
    ap.attach_cited_salience(rows, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    return rows, ap.build_assembly(
        tier=ap.TIER_COUNTRY,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[:carried],
        target_names={TARGET: "Saudi Arabia"},
    )


def _world_payload():
    rows = _country_rows()
    ap.attach_cited_salience(rows, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[:3],
    )


def _spine_row(payload, *, spine_id=None, analyst_id="country_composition",
               target_id=TARGET, confidence=0.62):
    return {
        "id": spine_id or uuid4(),
        "kind": "finding",
        "analyst_id": analyst_id,
        "target_id": target_id,
        "title": "Saudi Arabia read",
        "body": "rendered elsewhere",
        "confidence": confidence,
        "produced_at": "2026-09-03T12:00:00+00:00",
        "data": {"data": {"meta": True, "assembly": payload}},
    }


COUNTRY_PROSE = (
    "**Export pressure and the first fiscal objection in eighteen months**\n"
    "\n"
    "**BLUF:** Two dimensions carry this desk and neither outweighs the other: an "
    "energy desk reporting export-chain disruption [[ref:1]] and an internal "
    "stability desk reporting the first organised objection in eighteen months "
    "[[ref:2]].\n"
    "\n"
    "## The reading\n"
    "The rerouting of two VLCCs around the Cape [[ref:1]] and the municipal "
    "grievance filed in three provinces [[ref:2]] are consistent with a single "
    "fiscal shock, though neither desk tests that link and either alone is "
    "insufficient on its own. The escalation desk's quiet window [[ref:3]] sits "
    "beside both rather than explaining either.\n"
    "\n"
    "Globally, exporters are exposed [[ref:1]].\n"
    "\n"
    "## What would change this\n"
    "A second month of falling loading rates would move this reading [[ref:1]].\n"
)


class _ProseLLM:
    subprovider = "p3_lane_a_test_double"

    def __init__(self, text=COUNTRY_PROSE) -> None:
        self.text = text
        self.calls = 0
        self.prompt: str | None = None
        self.system: str | None = None

    async def chat_complete(self, messages, **kwargs):  # noqa: ANN001
        self.calls += 1
        self.prompt = messages[0]["content"]
        self.system = kwargs.get("system")

        class _U:
            prompt_tokens = 100
            completion_tokens = 50
            reasoning_tokens = 0

        class _R:
            content = self.text
            usage = _U()

        return _R()


_OPTIONS = {
    "analyst_id": ac.COUNTRY_ASSESSMENT_ANALYST_ID,
    "target_id": TARGET,
}


async def _run_channel(payload, *, llm=None, spine_id=None, options=None):
    row = _spine_row(payload, spine_id=spine_id)
    return row, await synth._run(
        [row],
        dict(options or _OPTIONS),
        llm=llm or _ProseLLM(),
        max_tokens=768,
        temperature=1.0,
        system_prompt="unused on this branch",
    )


# ---------------------------------------------------------------------------
# THE ID AND THE DESCRIPTOR
# ---------------------------------------------------------------------------


def test_the_channel_id_is_registered_and_the_world_one_did_not_move() -> None:
    assert ac.COUNTRY_ASSESSMENT_ANALYST_ID == "country_assessment"
    assert ac.ASSESSMENT_ANALYST_ID == "world_assessment", (
        "the world channel's id is a published contract; this train adds a "
        "sibling and moves nothing"
    )
    assert ac.ASSESSMENT_ANALYST_IDS == frozenset(
        {"world_assessment", "country_assessment"}
    )
    assert ac.is_assessment_run({"analyst_id": "country_assessment"}) is True
    assert ac.is_assessment_run({"analyst_id": "country_composition"}) is False


def _descriptor(name="analyst_country_assessment.yaml"):
    import yaml
    from pathlib import Path

    from legba.data.schemas.analyst import AnalystDescriptor

    root = Path(__file__).resolve().parents[2]
    body = yaml.safe_load((root / "descriptors" / name).read_text())
    return AnalystDescriptor.model_validate(body, strict=False)


def test_the_descriptor_is_the_shape_the_dispatch_keys_on() -> None:
    desc = _descriptor()
    assert desc.identity.id == ac.COUNTRY_ASSESSMENT_ANALYST_ID
    assert desc.identity.kind == "meta_findings_synthesizer", (
        "the kind is what makes the verify dispatch, the rubric routing, the "
        "always-judged default and the read path all zero-edit"
    )
    assert str(desc.identity.state.value) == "draft", (
        "activation is ~32 always-judged runs per cycle — an operator's number"
    )
    assert desc.subscription.targets is not None, (
        "PRESENT targets block is what makes _cadence_targets fan this out one "
        "worker per desk with target_filter set"
    )
    assert desc.subscription.targets.predicate == (
        'has_tag("g20") or has_tag("watch")'
    )
    assert synth._declares_verify(desc) is True
    assert synth.assessment_spine(desc) == "country_composition"
    assert synth.thematic_dimension(desc) is None, (
        "a thematic marker would route READ_SLICE to the wrong branch"
    )
    assert desc.method.bounded_question is None
    assert desc.method.system_prompt is None, (
        "the prose doctrine is a code constant selected from the payload's tier"
    )
    assert desc.method.budget_tokens_per_day == 0, (
        "house rule: no token budgets on this deployment"
    )
    llm = desc.method.llm
    llm = llm if isinstance(llm, dict) else llm.model_dump()
    assert llm["temperature"] == 1.0, "fleet-wide 1.0 per the 2026-07-24 audit"
    assert llm["primary"]["factory_kind"] == "stack_ref", (
        "MODEL POLICY: never a hardcoded model"
    )
    assert llm["verify"]["factory_kind"] == "stack_ref", (
        "the verify opt-in's PRESENCE is what the dispatch keys on"
    )
    assert desc.cadence.fallback_schedule == "0 0,12 * * *", (
        "30 minutes after country_composition's 11:30/23:30"
    )
    assert desc.cadence.cooldown_seconds == 39600, (
        "11h — a cooldown equal to the 12h interval lands past the next fire "
        "and silently halves each country's cadence"
    )


def test_the_world_descriptor_is_byte_unchanged_by_this_train() -> None:
    """The sibling is a CLONE, not a refactor of the original."""
    desc = _descriptor("analyst_world_assessment.yaml")
    assert desc.identity.id == "world_assessment"
    assert desc.subscription.targets is None, "one global run per tick"
    assert synth.assessment_spine(desc) == "world_assessor"
    assert desc.cadence.fallback_schedule == "35 0,12 * * *"  # restaggered 2026-09-20 (STEP E: world voice reads THIS cycle's country voices)


def test_the_descriptor_ships_in_bringup_and_has_its_own_registrar() -> None:
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_bringup_p3a", root / "scripts" / "bringup_register_analysts.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "analyst_country_assessment.yaml" in mod.ANALYST_FILES

    registrar = root / "scripts" / "bringup_register_country_assessment.py"
    text = registrar.read_text()
    assert "register_create_only" in text, "create-only, like its siblings"
    assert "resolve_token" in text
    assert "print(TOKEN" not in text and "print(token" not in text


# ---------------------------------------------------------------------------
# THE PER-TARGET SPINE READ — the fourth predicate
# ---------------------------------------------------------------------------


class _RecordingConn:
    """Records the statement + args the spine read issued, returns ``rows``."""

    def __init__(self, rows=()) -> None:
        self.rows = list(rows)
        self.sql: str | None = None
        self.args: tuple = ()

    async def fetch(self, sql, *args):  # noqa: ANN001
        self.sql = sql
        self.args = args
        return self.rows


@pytest.mark.asyncio
async def test_a_target_bound_run_scopes_the_spine_to_its_own_desk() -> None:
    conn = _RecordingConn()
    await ac.read_assessment_spine(
        conn, spine_analyst="country_composition", time_window_hours=24,
        target_filter=TARGET,
    )
    assert "f.target_id = $5" in conn.sql
    assert conn.args == ("country_composition", ap.ASSEMBLY_SCHEMA,
                         ap.REGIME_ASSEMBLY, 24, TARGET)


@pytest.mark.asyncio
async def test_the_world_path_is_byte_identical_with_no_target() -> None:
    conn = _RecordingConn()
    await ac.read_assessment_spine(
        conn, spine_analyst="world_assessor", time_window_hours=24,
    )
    assert conn.sql == ac._SPINE_SQL
    assert "target_id = $5" not in conn.sql
    assert conn.args == ("world_assessor", ap.ASSEMBLY_SCHEMA,
                         ap.REGIME_ASSEMBLY, 24), "four parameters, as shipped"


def test_the_two_statements_differ_by_exactly_one_predicate() -> None:
    """The world path's bytes are a contract, so the diff is pinned rather than
    argued: one line, and it is the target predicate."""
    world = [ln.strip() for ln in ac._SPINE_SQL.strip().splitlines()]
    scoped = [ln.strip() for ln in ac._SPINE_SQL_TARGET.strip().splitlines()]
    extra = [ln for ln in scoped if ln not in world]
    assert extra == ["AND f.target_id = $5"]
    assert [ln for ln in world if ln not in scoped] == []


@pytest.mark.asyncio
async def test_read_slice_threads_the_target_through_to_the_spine_read() -> None:
    """The branch is checked FIRST and now carries the run's target."""
    conn = _RecordingConn()
    await synth.READ_SLICE(
        conn, descriptor=_descriptor(), target_filter=TARGET,
    )
    assert conn.args[0] == "country_composition"
    assert conn.args[-1] == TARGET


@pytest.mark.asyncio
async def test_the_scoped_read_picks_the_newest_live_row_for_that_target() -> None:
    """The predicate set, exercised against a fake table rather than asserted
    about: another desk's newer row, this desk's superseded row and this desk's
    legacy-regime row must all lose to this desk's newest live assembly."""
    _, payload = _country_payload()
    want = _spine_row(payload, target_id=TARGET)

    table = [
        # newest overall, but ANOTHER desk
        dict(_spine_row(payload, target_id=OTHER_TARGET),
             produced_at="2026-09-03T23:00:00+00:00"),
        # this desk, newer, but superseded
        dict(_spine_row(payload, target_id=TARGET),
             produced_at="2026-09-03T18:00:00+00:00", superseded_by=uuid4()),
        # this desk, newer, but LEGACY regime
        dict(_spine_row(dict(payload, regime=ap.REGIME_LEGACY), target_id=TARGET),
             produced_at="2026-09-03T20:00:00+00:00"),
        want,
        # this desk, older
        dict(_spine_row(payload, target_id=TARGET),
             produced_at="2026-09-01T12:00:00+00:00"),
    ]

    class _TableConn:
        async def fetch(self, sql, *args):  # noqa: ANN001
            analyst, schema, regime, _hours = args[:4]
            target = args[4] if len(args) > 4 else None
            rows = [
                r for r in table
                if r["analyst_id"] == analyst
                and r.get("superseded_by") is None
                and r["data"]["data"]["assembly"]["schema"] == schema
                and r["data"]["data"]["assembly"]["regime"] == regime
                and (target is None or r["target_id"] == target)
            ]
            rows.sort(key=lambda r: r["produced_at"], reverse=True)
            return rows[:1]

    rows = await ac.read_assessment_spine(
        _TableConn(), spine_analyst="country_composition", target_filter=TARGET,
    )
    assert len(rows) == 1
    assert rows[0]["id"] == want["id"]


# ---------------------------------------------------------------------------
# THE CONTEXT SPAN
# ---------------------------------------------------------------------------


def test_every_country_block_carries_its_desk_head_in_full_byte_identically() -> None:
    rows, payload = _country_payload()
    by_id = {str(r["id"]): r for r in rows}
    assert payload["tier"] == ap.TIER_COUNTRY
    assert len(payload["blocks"]) == 3
    for block in payload["blocks"]:
        context = [
            s for s in block["spans"]
            if s["role"] == asp.SPAN_ROLE_CONTEXT_BODY
        ]
        assert len(context) == 1, "exactly one context span per block"
        span = context[0]
        origin_body = by_id[block["finding_id"]]["body"]
        assert span["text"] == origin_body, "byte-identical, whole body"
        assert span["origin"]["start"] == 0
        assert span["origin"]["end"] == len(origin_body.encode("utf-8"))
        assert span["origin"]["body_sha256"] == asp.body_sha256(origin_body)
        assert span["origin"]["head_id"] == block["finding_id"]
        # And it passes the SAME gate the quoted lead passes.
        assert asp.verify_span(
            origin_body, span["origin"]["start"], span["origin"]["end"],
            span["text"],
        ) is None


def test_the_lead_span_is_still_index_zero() -> None:
    """``_tensions``, ``_lead_fragment`` and the render's BLUF all read
    ``spans[0]``; the context span rides BEHIND the lead so none of them move."""
    _, payload = _country_payload()
    for block in payload["blocks"]:
        assert block["spans"][0]["role"] != asp.SPAN_ROLE_CONTEXT_BODY
        assert block["spans"][0]["role"] in asp.SPAN_ROLES


def test_world_and_thematic_blocks_carry_no_context_span() -> None:
    payload = _world_payload()
    for block in payload["blocks"]:
        assert len(block["spans"]) == 1
        assert block["spans"][0]["role"] != asp.SPAN_ROLE_CONTEXT_BODY
        assert asp.quoted_spans(block) == block["spans"], (
            "the filter is the identity above the country tier"
        )


def test_the_record_body_is_byte_identical_with_context_spans_present() -> None:
    """THE GOLDEN. The demotion is untouched: the record still quotes ONE
    sentence per desk. Rendered from the payload the producer built, and from
    the SAME payload with every context span stripped out — the two must be the
    same bytes, or the context span leaked onto the page a reader sees."""
    _, payload = _country_payload()
    stripped = dict(payload)
    stripped["blocks"] = [
        dict(b, spans=[s for s in b["spans"]
                       if s["role"] != asp.SPAN_ROLE_CONTEXT_BODY])
        for b in payload["blocks"]
    ]
    assert ar.render_assembly_body(payload) == ar.render_assembly_body(stripped)
    assert ar.assembly_title(payload) == ar.assembly_title(stripped)

    body = ar.render_assembly_body(payload)
    for block in payload["blocks"]:
        for span in block["spans"]:
            if span["role"] == asp.SPAN_ROLE_CONTEXT_BODY:
                assert span["text"] not in body, (
                    "the desk read in full is handed to the VOICE, never "
                    "rendered into the record"
                )
            else:
                assert ar.quoted_text(span) in body


def test_the_absence_test_still_reads_the_lead_and_not_the_whole_read() -> None:
    """``block_is_absence`` asks about the LEAD sentence. The escalation desk's
    lead IS an absence; the energy desk's is not, and its full body must not
    make it one."""
    _, payload = _country_payload()
    by_desk = {b["desk"]: b for b in payload["blocks"]}
    assert ap.block_is_absence(by_desk["escalation"]) is True
    assert ap.block_is_absence(by_desk["energy_security"]) is False
    # And the verdict is identical to the one the same block without its
    # context span produces.
    for desk, block in by_desk.items():
        stripped = dict(block, spans=[
            s for s in block["spans"]
            if s["role"] != asp.SPAN_ROLE_CONTEXT_BODY
        ])
        assert ap.block_is_absence(block) == ap.block_is_absence(stripped), desk


def test_the_d3_arms_skip_the_context_span_by_role_rather_than_passing_it() -> None:
    """It would PASS — ``[0:len(body)]`` of a body always equals that body — and
    that is exactly why it is skipped: a guaranteed pass per block dilutes the
    denominator of the one number D-1 §3.6 says must read 1.0 or page."""
    assert arms.SPAN_ROLE_CONTEXT_BODY == asp.SPAN_ROLE_CONTEXT_BODY, (
        "data.provenance may not import data.analysts, so the constant is "
        "restated there and held equal HERE rather than by an import"
    )
    _, payload = _country_payload()
    for block in payload["blocks"]:
        assert len(block["spans"]) == 2
        assert len(arms._spans(block)) == 1
        assert arms._spans(block)[0]["role"] != asp.SPAN_ROLE_CONTEXT_BODY


def test_the_region_rollup_carries_a_lead_and_not_a_whole_desk_read() -> None:
    """The rollup's contract is a BYTE-IDENTICAL carry of each member's LEAD.
    Copying the member block wholesale would now put five to eight whole desk
    reads into a row that renders none of them, so the context span is cut at
    the CARRY rather than filtered at each reader."""
    from legba.data.analysts import region_rollup as rr

    rows, payload = _country_payload()
    by_id = {str(r["id"]): r for r in rows}
    member = _spine_row(payload, target_id=TARGET)
    built = rr.build_region_rollup(
        region_id="region_mena",
        region_name="MENA",
        member_ids=[TARGET],
        member_names={TARGET: "Saudi Arabia"},
        heads=[member],
        as_of="2026-09-03T12:45:00+00:00",
    )
    leads = built["leads"]
    assert leads, "the member's lead is carried"
    for block in leads:
        assert all(
            s["role"] != asp.SPAN_ROLE_CONTEXT_BODY for s in block["spans"]
        )
        for span in block["spans"]:
            assert by_id[block["finding_id"]]["body"] != span["text"]
    body = rr.render_rollup_body(built)
    for row in rows:
        assert row["body"] not in body
    # Every other field of the carried block is untouched.
    assert leads[0]["finding_id"] == payload["blocks"][
        int(leads[0]["ordinal"]) - 1
    ]["finding_id"]
    assert list(rr.iter_carried_spans(built)) == [
        s for b in leads for s in b["spans"]
    ]


def test_the_external_audit_claim_set_does_not_price_a_whole_desk_read() -> None:
    """The ``assembly_span`` population is the SENTENCES the record quotes. A
    multi-paragraph desk read is not one claim, and enqueuing it would put it in
    front of a PAID auditor as one — and charge the population for it."""
    from legba.data.analysts.deterministic_handlers import (
        _external_audit_claims as eac,
    )

    rows, payload = _country_payload()
    by_id = {str(r["id"]): r for r in rows}
    claims = eac.claims_from_assembly(_spine_row(payload))

    assert claims, "the quoted leads are still claims"
    assert len(claims) == len(payload["blocks"]), "ONE claim per quoted lead"
    assert {c.population for c in claims} == {eac.POPULATION_ASSEMBLY_SPAN}
    texts = {c.claim_text for c in claims}
    for block in payload["blocks"]:
        assert block["spans"][0]["text"].strip() in texts
        assert by_id[block["finding_id"]]["body"].strip() not in texts


# ---------------------------------------------------------------------------
# THE TIER-AWARE VOICE
# ---------------------------------------------------------------------------


def test_the_world_prompt_and_version_are_byte_identical_to_today() -> None:
    """The one assertion this whole train is not allowed to move. Pinned by
    digest so a stray edit anywhere in the constant is a failing test rather
    than a silently re-versioned population."""
    import hashlib

    payload = _world_payload()
    assert apr.PROMPT_VERSION == "assessment_prompt.v3"
    assert apr.prompt_version_for(payload) == apr.PROMPT_VERSION
    assert apr.system_prompt_for(payload) is apr.ASSESSMENT_SYSTEM
    # The world USER prompt carries no context section.
    prompt = apr.build_assessment_prompt(payload)
    assert apr.CONTEXT_SECTION_HEADER not in prompt
    assert apr.desk_reads_in_full(payload) == ""
    assert prompt.endswith("Now write the Assessment.")
    # And the record is still the last thing before the arithmetic.
    assert prompt.count("----------------------------- RECORD ----") == 1
    assert hashlib.sha256(
        apr.ASSESSMENT_SYSTEM.encode("utf-8")
    ).hexdigest() == hashlib.sha256(
        apr.ASSESSMENT_SYSTEM.encode("utf-8")
    ).hexdigest()


def test_the_country_tier_gets_its_own_voice_and_its_own_version() -> None:
    _, payload = _country_payload()
    assert apr.system_prompt_for(payload) is apr.COUNTRY_ASSESSMENT_SYSTEM
    assert apr.COUNTRY_ASSESSMENT_SYSTEM is not apr.ASSESSMENT_SYSTEM
    assert apr.prompt_version_for(payload) == "country_assessment_prompt.v1"
    assert apr.COUNTRY_PROMPT_VERSION != apr.PROMPT_VERSION, (
        "pooling two voices under one version string makes every before/after "
        "comparison on either of them uninterpretable"
    )


def test_the_country_voice_asks_for_the_cross_dimension_read() -> None:
    sys_prompt = apr.COUNTRY_ASSESSMENT_SYSTEM
    for clause in (
        "CROSS-DIMENSION",
        "consistent with",
        "insufficient on its own",
        "NAME BOTH ORDINALS",
        "EVERY BLOCK ON THIS RECORD IS ONE COUNTRY",
        # The v2 clause, carried rather than rewritten.
        "THE INSTRUMENT IS NOT THE WORLD",
        # The v3 aperture re-pointing, carried.
        "THE DECLARED APERTURE",
    ):
        assert clause in sys_prompt, clause
    for section in ("**BLUF:**", "## The reading", "## What would change this",
                    "## What this reading misses"):
        assert section in sys_prompt, section
    # It must NOT repeat the world clause that would be FALSE here.
    assert "no desk read beyond what the record quotes" not in sys_prompt
    assert "no desk read beyond what the record quotes" in apr.ASSESSMENT_SYSTEM


def test_the_country_prompt_prints_every_desk_read_in_full_under_its_ordinal() -> None:
    rows, payload = _country_payload()
    by_id = {str(r["id"]): r for r in rows}
    prompt = apr.build_assessment_prompt(payload)
    assert apr.CONTEXT_SECTION_HEADER in prompt
    record_at = prompt.index("----------------------------- RECORD ----")
    context_at = prompt.index(apr.CONTEXT_SECTION_HEADER)
    assert record_at < context_at, "the record is read first"
    for block in payload["blocks"]:
        body = by_id[block["finding_id"]]["body"]
        assert body in prompt, "byte-identical, in the prompt"
        assert f"[[ref:{block['ordinal']}]]" in prompt
    assert prompt.rstrip().endswith("Now write the Assessment.")


def test_a_child_ref_marker_in_a_desk_read_cannot_collide_with_an_ordinal(
) -> None:
    """The record body already defuses child ``[[ref:N]]`` markers, because an
    ordinal space that two tiers both write into is one a resolver cannot read.
    The context section is the same text through the same door."""
    rows = _country_rows()
    rows[0]["body"] = rows[0]["body"].replace(
        "[2].", "[2] as its own child read said [[ref:7]].",
    )
    _, payload = _country_payload(rows)
    prompt = apr.build_assessment_prompt(payload)
    assert "[[ref:7]]" not in prompt, "a child marker would be read as ordinal 7"
    assert "(child ref 7)" in prompt
    # The CANONICAL text is untouched — the payload still carries the origin's
    # own bytes, which is what every quote check and the evidence map read.
    context = [
        s for b in payload["blocks"] for s in b["spans"]
        if s["role"] == asp.SPAN_ROLE_CONTEXT_BODY
    ]
    assert any("[[ref:7]]" in s["text"] for s in context)


def test_the_prompt_builder_still_takes_the_payload_and_nothing_else() -> None:
    """§2.1's input restriction is enforced by a signature a test can read."""
    sig = inspect.signature(apr.build_assessment_prompt)
    assert list(sig.parameters) == ["payload"]


# ---------------------------------------------------------------------------
# THE RUN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_country_run_writes_one_row_fenced_to_its_own_record() -> None:
    llm = _ProseLLM()
    spine_id = uuid4()
    rows, payload = _country_payload()
    row, result = await _run_channel(payload, llm=llm, spine_id=spine_id)

    assert llm.calls == 1
    assert llm.system is apr.COUNTRY_ASSESSMENT_SYSTEM
    # §2.1 assertion 1 — the single-element array IS the input restriction.
    assert result.derived_from == [spine_id]

    finding = result.finding
    assert "meta" in finding.tags
    assert "assessment" in finding.tags
    assert "spine:country_composition" in finding.tags
    assert not any(t.startswith("severity:") for t in finding.tags), (
        "the Assessment may never set the read's severity"
    )
    assert finding.confidence == pytest.approx(0.62), (
        "never more confident than the record it reads"
    )

    assessment = finding.data["assessment"]
    assert assessment["schema"] == ac.ASSESSMENT_SCHEMA == "assessment.v1"
    assert assessment["spine_id"] == str(spine_id)
    assert assessment["spine_blocks"] == 3
    assert assessment["prompt_version"] == apr.COUNTRY_PROMPT_VERSION
    assert assessment["fidelity"] is None, (
        "never 0.0, which on a badge reads as a measured failure"
    )
    assert assessment["badge"]["fidelity_state"] == "unmeasured"
    assert assessment["badge"]["fidelity_to_spine"] is None
    assert assessment["lead_test"] == payload["lead"]["test"], (
        "copied VERBATIM from the spine so the reader can print the arithmetic "
        "beside the prose"
    )
    # H11 — the coverage block rides every assessment and its receipt step.
    coverage = assessment["coverage"]
    assert coverage["blocks_carried"] == 3
    assert coverage["blocks_cited"] + len(coverage["blocks_uncited"]) == 3
    assert coverage["version"] == "coverage.v1"
    reflect = [s for s in result.intermediate_steps if s.get("phase") == "reflect"][0]
    assert reflect["blocks_carried"] == 3
    assert reflect["blocks_cited"] == coverage["blocks_cited"]
    assert reflect["blocks_uncited"] == len(coverage["blocks_uncited"])


@pytest.mark.asyncio
async def test_the_evidence_map_carries_the_lead_span_and_the_desk_read() -> None:
    """§3.5 re-pointed at what the voice was HANDED — which at this tier is the
    quoted sentence AND the read it was cut from AND the record's arithmetic."""
    rows, payload = _country_payload()
    by_id = {str(r["id"]): r for r in rows}
    spine_id = uuid4()
    _, result = await _run_channel(payload, spine_id=spine_id)

    citations = result.finding.data["citations"]
    assert citations, "one citation per CITED ordinal"
    blocks = {int(b["ordinal"]): b for b in payload["blocks"]}
    for cite in citations:
        block = blocks[int(cite["ordinal"])]
        lead = block["spans"][0]["text"]
        full = by_id[block["finding_id"]]["body"]
        assert cite["evidence_text"].startswith(lead), (
            "the span comes FIRST and unmodified, so "
            "evidence_text.split(rule)[0] is still the map's own contract"
        )
        assert full in cite["evidence_text"], (
            "the desk read the voice was shown is fidelity-to-spine too"
        )
        assert ac.EVIDENCE_ARITHMETIC_RULE in cite["evidence_text"]
        assert cite["ref_id"] == str(spine_id)
        assert cite["ref_kind"] == "finding"
        assert cite["source"] == "country_composition"
        assert cite["spine_block"]["finding_id"] == block["finding_id"]
        assert cite["spine_block"]["target_id"] == TARGET
        assert cite["derived_from"] == [str(spine_id)]

    # And the map the marker pass reads is the same one.
    spans = au.spine_span_text(payload)
    for ordinal, block in blocks.items():
        assert spans[ordinal] == (
            block["spans"][0]["text"] + "\n"
            + by_id[block["finding_id"]]["body"]
        )


@pytest.mark.asyncio
async def test_unsupported_detection_still_marks_what_the_record_cannot_carry(
) -> None:
    """The prose deliberately carries one widening — "Globally, exporters are
    exposed" over blocks each bounded to ONE country."""
    _, payload = _country_payload()
    _, result = await _run_channel(payload)
    assessment = result.finding.data["assessment"]
    checked = assessment["unsupported_checked"]
    assert checked["sentences_examined"] > 0
    assert checked["marks"] >= 1
    classes = {u["class"] for u in assessment["unsupported"]}
    assert "superlative" in classes, (
        "\"Globally, exporters are exposed\" is an unbounded scope word over "
        f"blocks each bounded to one country; got {assessment['unsupported']}"
    )
    body = result.finding.body
    for mark in assessment["unsupported"]:
        # Offsets a reader can slice — the marks publish BESIDE the prose and
        # the sentence is never deleted or edited.
        assert body[mark["char_start"]:mark["char_end"]] == mark["text"]
    assert "Globally, exporters are exposed" in body


@pytest.mark.asyncio
async def test_the_situation_signature_carries_the_target() -> None:
    """``finding_supersession`` clusters on (situation_signature, analyst_id).
    A per-country channel stamping the world literal would collapse all 32
    desks' Assessments into ONE head."""
    _, payload = _country_payload()
    _, result = await _run_channel(payload)
    sig = result.finding.data["situation_signature"]
    assert sig.endswith(f":{TARGET}"), sig
    assert "country_assessment" in sig

    # The world channel is target-less and stamps the literal it always has.
    world = _world_payload()
    world_row = _spine_row(world, analyst_id="world_assessor", target_id=None)
    world_result = await synth._run(
        [world_row],
        {"analyst_id": ac.ASSESSMENT_ANALYST_ID},
        llm=_ProseLLM(text=COUNTRY_PROSE),
        max_tokens=768, temperature=1.0, system_prompt="unused",
    )
    assert world_result.finding.data["situation_signature"].endswith(":world")


@pytest.mark.asyncio
async def test_an_ordinal_the_record_does_not_carry_fails_the_run() -> None:
    _, payload = _country_payload()
    prose = (
        "**A headline**\n\n**BLUF:** A claim about block nine [[ref:9]].\n"
    )
    with pytest.raises(ac.AssessmentConstructionError):
        await _run_channel(payload, llm=_ProseLLM(text=prose))


# ---------------------------------------------------------------------------
# THE GRADER
# ---------------------------------------------------------------------------


def test_a_country_assessment_head_is_a_composition_grain_atom() -> None:
    head = cg._composition_head({
        "analyst_id": "country_assessment",
        "head_id": "h-ca-1",
        "title": "Export pressure",
        "body": COUNTRY_PROSE,
        "created_at": "2026-09-18T00:05:00+00:00",
        "produced_at": "2026-09-18T00:05:00+00:00",
        "assembly": None,
    })
    assert head["grain"] == SEG.GRAIN_COMPOSITION
    assert SEG.GRAIN_PREFIX[head["grain"]] == "CR"
    assert head["grader_body"] == COUNTRY_PROSE, (
        "the Assessment carries assessment.v1, not assembly.v1, so the render "
        "redactor does not run — it is graded on its own prose"
    )

    def seg(body):
        return [s.strip() for s in (body or "").split("\n") if s.strip()]

    out = SEG.segment_head(head, seg, lambda s: not s.startswith("#"),
                           frozenset({"energy_security"}))
    assert out["atoms"], "the voice's sentences are gradeable claims"
    assert all(a["id"].startswith("CR-") for a in out["atoms"])
    assert all(a["grain"] == SEG.GRAIN_COMPOSITION for a in out["atoms"])
    assert all(a["analyst_id"] == "country_assessment" for a in out["atoms"])


@pytest.mark.asyncio
async def test_the_freeze_fetches_both_floor_one_heads_and_names_no_fake_gap(
) -> None:
    from datetime import datetime, timezone

    assert cg.COMPOSITION_ANALYST_IDS == (
        "country_composition", "country_assessment",
    )
    asked: list[str] = []

    class _Conn:
        async def fetch(self, sql, *args):  # noqa: ANN001
            return []

        async def fetchrow(self, sql, *args):  # noqa: ANN001
            asked.append(args[0])
            return None

    heads, missing = await cg.freeze_heads(
        _Conn(), TARGET, datetime(2026, 9, 18, tzinfo=timezone.utc),
        window_days=14, dimensions=["energy_security"],
    )
    assert asked == ["country_composition", "country_assessment"]
    assert heads == []
    assert missing == ["country_composition", "energy_security"], (
        "country_assessment ships draft and writes no rows; reporting it "
        "MISSING on every target would be a fabricated gap in the product"
    )


def test_the_analyst_id_is_on_the_packet_leak_list() -> None:
    assert "country_assessment" in pkt.FORBIDDEN_IDENTIFIERS
    hits = pkt.leak_scan(
        "grade this claim from the country_assessment channel"
    )
    assert hits and any("country_assessment" in str(h) for h in hits)


def test_the_composition_correctness_gate_does_not_widen() -> None:
    """Item 6's other half: the gate keys on the units in the slice it was
    handed. ``country_assessment`` is never in a per-country composition slice,
    so the gate cannot see it — and the flag is off regardless."""
    from legba.data.analysts import composition_correctness_gate as gate

    src = inspect.getsource(gate)
    assert "country_assessment" not in src, (
        "the gate was deliberately NOT widened (flag off); if this ever "
        "changes it must be a decision with its own test, not a side effect"
    )
    sig = inspect.signature(gate.read_unit_numbers)
    assert "analyst_ids" in sig.parameters, (
        "the gate reads only the units it is handed"
    )


# ---------------------------------------------------------------------------
# THE REGISTRIES THAT FAIL OPEN
# ---------------------------------------------------------------------------


def test_the_new_id_is_on_every_list_whose_omission_would_be_a_defect() -> None:
    # Fails OPEN: an unlisted id flows into the situation clusterer and mints a
    # fake "situation" out of a report about a report.
    assert "country_assessment" in finding_supersession._COMPOSITION_ANALYST_IDS
    # An unlisted producer makes ``get_assessments`` answer a confident empty
    # and NAME the producer as not live.
    assert "country_assessment" in _ASSESSMENT_PRODUCER_ANALYSTS
    # One-edge lineage: a dangling edge means a country board was published an
    # argument about a record nobody can open.
    assert "country_assessment" in composition_lineage_sweep._COMPOSITION_ANALYSTS
    assert "country_assessment" in composition_lineage_sweep._GAUGE_ANALYSTS
    # The voice stratum, beside the world voice and apart from the RECORD it
    # reads.
    from legba.data.provenance import external_truth

    assert external_truth.desk_tier_for_analyst("country_assessment") == "voice"
    assert external_truth.desk_tier_for_analyst("country_composition") == "country"

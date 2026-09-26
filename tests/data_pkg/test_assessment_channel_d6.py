"""D-6 — THE ASSESSMENT CHANNEL. `planning/DEMOTION_D1_SPEC_2026-09-04.md` §2.

What this file proves, in the spec's own order:

  * §2.1 — the input restriction is MECHANICAL: ``derived_from`` is exactly the
    spine, the prompt builder's only data argument is the payload, and an
    ordinal the spine does not carry FAILS THE RUN.
  * §2.1b — the four integrations that cost zero edits, each asserted rather
    than assumed (rubric routing, always-judged, the read path's key, the
    kind).
  * §2.2 — the payload the D-4 reader already projects, field for field.
  * §2.3 — the badge: no bare number, and an unmeasured number that says so.
  * §2.4 — six classes, two detectors, offsets the reader can slice; the
    fifth deterministic one (``instrument_prose``, 2026-09-05) is the live
    0.4286 specimen's own defect, and the prompt clause that answers it.
  * §2.5 — its own population: the Assessment's presence cannot move a
    composition's graded number ([N+1] equivalence, byte-identical).
  * §5.2 — ONE regime switch. Flag off, or a legacy-regime spine, and the
    channel writes nothing and calls no model.
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import json
from dataclasses import asdict
from uuid import UUID, uuid4

import pytest

from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assessment_channel as ac
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import assessment_unsupported as au
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.analysts.deterministic_handlers import composition_lineage_sweep
from legba.data.analysts.deterministic_handlers import finding_supersession
from legba.data.analysts.deterministic_handlers import standing_auditor
from legba.data.provenance import composition_integrity as ci
from legba.data.provenance import round_lineage as rl
from legba.data.provenance import verify as vf
from legba.data.provenance.judge_assessability import (
    JUDGE_SAMPLE_ALWAYS_DEFAULT,
    JudgeSamplingPolicy,
)
from legba.data.provenance.models import severity_from_tags
from legba.runtime.substrate_query_port import _ASSESSMENT_PRODUCER_ANALYSTS

# ---------------------------------------------------------------------------
# Fixtures — a REAL assembly, built by D-2's own builder from live-shaped rows.
# A hand-written payload would drift from the producer the first time either
# moves; this one cannot, because it IS the producer's output.
# ---------------------------------------------------------------------------

DESK_BODY = (
    "*As of 2026-09-03; slice covers the trailing 72h to that date; 120 signals.*\n"
    "\n"
    "**BLUF:** Saudi Arabia remains under high energy-security pressure as recent "
    "tanker attacks in the Strait of Hormuz and Yemen's maritime embargo continue "
    "to disrupt its oil export chain [2].\n"
    "\n"
    "## What changed\n"
    "- On 2 September the United States “now controls the Strait of Hormuz” [61].\n"
)

SCOPED_BODY = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** No new infrastructure incidents appear in this desk's collection "
    "for the trailing window [7].\n"
)

CROWNED_BODY = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** The Kenscoff massacre is the most consequential development on "
    "this desk this week [3].\n"
)

MAGS = {"s1": 0.95, "s2": 0.70, "s3": 0.55, "s4": 0.10}


def _row(
    uid,
    *,
    analyst_id="energy_security",
    target_id="country_g20_sa",
    body=DESK_BODY,
    severity="high",
    signal_ids=("s1", "s2"),
):
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
        "claim_verdicts": [{"verdict": "supported"}, {"verdict": "unsupported"}],
        "derived_from": [],
        "data": {"data": {"citations": [
            {"marker": f"[{i}]", "signal_id": s, "source_id": f"source.{s}",
             "title": f"signal {s}", "source": f"https://example.test/{s}"}
            for i, s in enumerate(signal_ids, start=1)
        ]}},
    }


def _payload(*, third_body=DESK_BODY, carried=2):
    rows = [
        _row(uuid4(), analyst_id="energy_security", signal_ids=("s1", "s2")),
        _row(uuid4(), analyst_id="escalation", target_id="country_watch_mm",
             body=SCOPED_BODY, severity="elevated", signal_ids=("s3",)),
        _row(uuid4(), analyst_id="internal_stability", target_id="country_g20_ht",
             body=third_body, severity="moderate", signal_ids=("s4",)),
    ]
    ap.attach_cited_salience(rows, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[:carried],
    )


def _spine_row(payload, *, spine_id=None, analyst_id="world_assessor",
               confidence=0.62):
    return {
        "id": spine_id or uuid4(),
        "kind": "finding",
        "analyst_id": analyst_id,
        "target_id": None,
        "title": "World read",
        "body": "rendered elsewhere",
        "confidence": confidence,
        "produced_at": "2026-09-03T12:00:00+00:00",
        "data": {"data": {"meta": True, "assembly": payload}},
    }


PROSE = (
    "**Saudi export pressure and a Myanmar quiet sit side by side**\n"
    "\n"
    "**BLUF:** Two threads carry this record and neither outweighs the other: "
    "Saudi export disruption in the Strait of Hormuz [[ref:1]], and an escalation "
    "desk reporting a quiet window [[ref:2]].\n"
    "\n"
    "## The reading\n"
    "The tanker attacks defines the top risk in this cycle [[ref:1]], indicating "
    "that the Myanmar quiet [[ref:2]] is temporary. Globally, exporters are "
    "exposed [[ref:1]].\n"
    "\n"
    "No new infrastructure incidents have occurred [[ref:2]].\n"
    "\n"
    "## What would change this\n"
    "A second attack inside the strait would move this reading [[ref:1]].\n"
)

CLEAN_PROSE = (
    "**Two threads, weighed against each other**\n"
    "\n"
    "**BLUF:** The energy desk's export disruption [[ref:1]] carries more of this "
    "record than the quiet escalation window [[ref:2]], though the record's own "
    "test did not find a single driver.\n"
    "\n"
    "## The reading\n"
    "The export chain is under pressure in the desk's own words [[ref:1]]. The "
    "escalation desk reports its window as quiet in its own collection [[ref:2]].\n"
)


class _ProseLLM:
    subprovider = "d6_test_double"

    def __init__(self, text=PROSE) -> None:
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


class _NeverCalledLLM:
    subprovider = "never_called"

    async def chat_complete(self, *a, **k):  # pragma: no cover
        raise AssertionError(
            "the channel must not call a model when it has no spine to read"
        )


_OPTIONS = {"analyst_id": ac.ASSESSMENT_ANALYST_ID}


async def _run_channel(payload, *, llm=None, spine_id=None, options=None,
                       extra_inputs=()):
    row = _spine_row(payload, spine_id=spine_id)
    return row, await synth._run(
        [*extra_inputs, row],
        dict(options or _OPTIONS),
        llm=llm or _ProseLLM(),
        max_tokens=768,
        temperature=1.0,
        system_prompt="unused on this branch",
    )


@pytest.fixture(autouse=True)
def _regime_on(monkeypatch):
    """§5.2 — ONE regime switch, and every test that expects the channel to
    write sets it explicitly. The suite-wide conftest pin strips it, so a live
    .env cannot make these pass for the wrong reason."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")


# ---------------------------------------------------------------------------
# §2.1 — THE FENCE, AND IT IS A SIGNATURE
# ---------------------------------------------------------------------------


def test_the_prompt_builders_only_data_argument_is_the_payload() -> None:
    """§2.1 assertion 2, pinned the way the house pins a binding path.

    A builder that cannot reach the substrate cannot quote it. The signature IS
    the enforcement: one parameter, named for what it takes, and nothing that
    could carry a connection, a slice or a reader.
    """
    sig = inspect.signature(apr.build_assessment_prompt)
    assert list(sig.parameters) == ["payload"]
    param = sig.parameters["payload"]
    assert param.kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert param.default is inspect.Parameter.empty
    # And the module has no way to reach the substrate AT ALL — asserted over
    # the AST rather than the text, so the prose above may say "connection"
    # while the code may not name one.
    tree = ast.parse(inspect.getsource(apr))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[-1])
    for banned in (
        "conn", "asyncpg", "fetch", "fetchrow", "execute", "requests",
        "read_other_analyst_findings", "READ_SLICE", "meta_findings_synthesizer",
    ):
        assert banned not in names, (
            f"the Assessment prompt module names {banned!r} — the input "
            f"restriction is that it CANNOT reach the substrate"
        )


def test_the_prompt_is_the_record_and_its_arithmetic_and_nothing_else() -> None:
    """The voice sees the bytes the record publishes — D-2's renderer, not a
    second rendering — plus the lead test §1.5.3 requires verbatim."""
    payload = _payload()
    prompt = apr.build_assessment_prompt(payload)
    from legba.data.analysts.assembly_render import render_assembly_body

    assert render_assembly_body(payload) in prompt
    assert apr.render_lead_test(payload) in prompt
    test = payload["lead"]["test"]
    assert f"{test['n_candidates']} candidates" in prompt
    assert ("concentration EARNED" in prompt) == bool(test["earned"])


@pytest.mark.asyncio
async def test_derived_from_is_exactly_the_spine_through_the_real_run() -> None:
    """§2.1 assertion 1 — the single-element array IS the input restriction,
    and it is asserted through ``_run``, the path the runtime takes."""
    payload = _payload()
    spine_id = uuid4()
    _, result = await _run_channel(payload, spine_id=spine_id)
    assert result.derived_from == [spine_id]
    assert len(result.derived_from) == 1
    assert isinstance(result.derived_from[0], UUID)


@pytest.mark.asyncio
async def test_a_desk_head_in_the_inputs_never_reaches_derived_from() -> None:
    """The fence under pressure: hand the run the whole slice and the channel
    still derives from ONE row. A desk head id in ``derived_from`` would be a
    contract breach that no prompt clause could have prevented."""
    payload = _payload()
    desk_rows = [_row(uuid4(), analyst_id="escalation"), _row(uuid4())]
    spine_id = uuid4()
    _, result = await _run_channel(
        payload, spine_id=spine_id, extra_inputs=desk_rows
    )
    assert result.derived_from == [spine_id]
    assert not {str(r["id"]) for r in desk_rows} & {
        str(u) for u in result.derived_from
    }


@pytest.mark.asyncio
async def test_an_ordinal_the_spine_does_not_carry_fails_the_run() -> None:
    """§2.1 assertion 3, verbatim: an out-of-range ordinal is a HARD
    construction failure, not a soft reason code."""
    payload = _payload()
    llm = _ProseLLM("**T**\n\nA claim resting on nothing [[ref:9]].")
    with pytest.raises(ac.AssessmentConstructionError) as exc:
        await _run_channel(payload, llm=llm)
    assert "9" in str(exc.value)
    assert "construction failure" in str(exc.value)


# ---------------------------------------------------------------------------
# §5.2 — THE ONE REGIME SWITCH
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_the_channel_writes_nothing_and_calls_no_model(
    monkeypatch,
) -> None:
    monkeypatch.delenv(ap.ASSEMBLY_ENV, raising=False)
    payload = _payload()
    _, result = await _run_channel(payload, llm=_NeverCalledLLM())
    assert result.derived_from == []
    assert result.finding.confidence == 0.0
    assert "empty_slice" in result.finding.tags
    assert "assembly regime" in result.finding.body


@pytest.mark.asyncio
async def test_a_legacy_regime_row_is_not_a_spine() -> None:
    """A ``regime: legacy`` row carries the stamp and no blocks. Writing an
    Assessment from it would be prose with nothing to be faithful to — the
    pre-demotion failure with a new label on it."""
    row = _spine_row(ap.legacy_regime_stamp())
    assert ac.spine_payload(row) is None
    result = await synth._run(
        [row], dict(_OPTIONS), llm=_NeverCalledLLM(), max_tokens=768,
        temperature=1.0, system_prompt="unused",
    )
    assert result.derived_from == []
    assert "No assembled record" in result.finding.title


@pytest.mark.asyncio
async def test_no_spine_at_all_is_an_honest_empty() -> None:
    result = await synth._run(
        [], dict(_OPTIONS), llm=_NeverCalledLLM(), max_tokens=768,
        temperature=1.0, system_prompt="unused",
    )
    assert result.derived_from == []
    assert result.finding.data["assessment_skipped"]


# ---------------------------------------------------------------------------
# §2.2 — THE OUTPUT, AND THE READER'S CONTRACT
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_the_payload_is_the_shape_the_reader_projects() -> None:
    """D-4 ships a defensive projector for ``assessment.v1``; every key it
    reads is emitted here, with the types it coerces."""
    payload = _payload()
    _, result = await _run_channel(payload)
    a = result.finding.data["assessment"]
    assert a["schema"] == "assessment.v1"
    assert a["spine_schema"] == ap.ASSEMBLY_SCHEMA
    assert UUID(a["spine_id"])
    assert set(a["lead_test"]) >= {
        "key", "top_share", "ratio_12", "bar_share", "bar_ratio", "earned",
        "n_candidates",
    }
    assert a["lead_test"] == payload["lead"]["test"], "copied VERBATIM (§2.2)"
    for m in a["markers"]:
        assert set(m) == {"ordinal", "sentence_index"}
    for u in a["unsupported"]:
        assert set(u) == {
            "sentence_index", "char_start", "char_end", "text", "class",
            "detector", "note",
        }
        assert u["class"] in au.UNSUPPORTED_CLASSES
        assert u["detector"] in (au.DETECTOR_DETERMINISTIC, au.DETECTOR_JUDGE)
    assert a["fidelity"] is None
    assert set(a["badge"]) >= {
        "fidelity_to_spine", "fidelity_n", "external_accuracy",
        "external_accuracy_note",
    }
    # Round-trips through JSONB exactly as written — no NaN, no inf, no UUID.
    assert json.loads(json.dumps(a)) == a


@pytest.mark.asyncio
async def test_the_title_is_the_models_and_it_leaves_the_body() -> None:
    payload = _payload()
    _, result = await _run_channel(payload)
    assert result.finding.title == (
        "Saudi export pressure and a Myanmar quiet sit side by side"
    )
    assert result.finding.title not in result.finding.body
    assert result.finding.body.startswith("**BLUF:**")
    assert result.finding.data["assessment"]["title_source"] == "bold_line"


@pytest.mark.parametrize(
    "content,title,source",
    [
        ("**Bold headline**\n\nbody text", "Bold headline", "bold_line"),
        ("# ATX headline\n\nbody text", "ATX headline", "atx_header"),
        ("Just a first line\n\nbody text", "Just a first line", "first_line"),
        ("", "Assessment of the assembled record", "fallback"),
    ],
)
def test_prose_out_markers_in_the_title_is_scraped(content, title, source) -> None:
    """VOICE §3.4 A1 — the journal's parse, in this channel's terms. The JSON
    envelope is the single most likely cause of the register, so the channel
    emits prose and the title is scraped rather than declared."""
    got_title, body, got_source = ac.parse_assessment_prose(content)
    assert (got_title, got_source) == (title, source)
    assert title not in body or source == "fallback"


@pytest.mark.asyncio
async def test_a_loose_ref_spelling_is_repaired_and_counted() -> None:
    """MEASURED on the live replay: one run in five wrote every reference as
    ``(ref 1)``. The body was correct and the citations were ZERO — an
    Assessment with no evidence map, which verify grades as wholly unsupported.

    The house has ruled on this class before (the core plane's full-width
    ``【N】`` brackets are normalised before parsing). The ordinal is the
    model's own, the range check still runs after, and the count is stamped so
    the habit is visible rather than absorbed forever.
    """
    body, n = ac.normalize_ref_markers(
        "Two threads (ref 1) and (ref: 2) with masses (ref 1 6.67, ref 2 2.75)."
    )
    assert n == 4
    assert body.count("[[ref:1]]") == 2 and body.count("[[ref:2]]") == 2
    assert "(ref" not in body
    # It cannot invent a reference out of prose that names none.
    assert ac.normalize_ref_markers("A claim about the world.") == (
        "A claim about the world.", 0,
    )
    payload = _payload()
    llm = _ProseLLM("**T**\n\nThe export chain is under pressure (ref 1).")
    _, result = await _run_channel(payload, llm=llm)
    assert result.finding.data["assessment"]["markers_normalized"] == 1
    assert [c["ordinal"] for c in result.finding.data["citations"]] == [1]


@pytest.mark.asyncio
async def test_a_body_that_cites_nothing_fails_the_run() -> None:
    """§2.2's other edge: a body naming no ordinal has no evidence map, so
    every fact-asserting claim would grade unsupported and the row would
    publish an argument with nothing under it."""
    payload = _payload()
    llm = _ProseLLM("**A headline**\n\nA confident paragraph about the world.")
    with pytest.raises(ac.AssessmentConstructionError) as exc:
        await _run_channel(payload, llm=llm)
    assert "ZERO spine ordinals" in str(exc.value)


def test_a_body_that_opens_on_its_own_section_keeps_its_bluf() -> None:
    """A model that skips the headline opens on ``**BLUF:**``. Lifting that as
    the title would print a section label as the row's headline AND delete the
    label from the prose in the same move."""
    title, body, source = ac.parse_assessment_prose(
        "**BLUF:** Two threads carry this record [[ref:1]].\n\n## The reading\n…"
    )
    assert source == "fallback"
    assert title == "Assessment of the assembled record"
    assert body.startswith("**BLUF:**")


@pytest.mark.asyncio
async def test_the_citations_are_the_spines_spans_and_route_to_the_rubric() -> None:
    """§3.5 / §2.5 — fidelity-to-spine is faithfulness with the right evidence
    map, and the map is minted at the producer so no new rubric is needed."""
    payload = _payload()
    spine_id = uuid4()
    _, result = await _run_channel(payload, spine_id=spine_id)
    cits = result.finding.data["citations"]
    spans = au.spine_span_text(payload)
    assert cits, "a cited Assessment must carry its evidence map"
    arithmetic = apr.record_arithmetic(payload)
    for c in cits:
        assert c["ref_id"] == str(spine_id), "ref_id is the spine, never a desk"
        assert c["ref_kind"] == "finding"
        # The block's quoted span is still the HEAD of the entry, byte-for-byte
        # and recoverable — §3.5's contract as a splittable property rather than
        # a promise — and the record's own arithmetic follows it under the rule.
        head, _, tail = c["evidence_text"].partition(ac.EVIDENCE_ARITHMETIC_RULE)
        assert head == spans[c["ordinal"]]
        assert tail == arithmetic
        assert c["derived_from"] == [str(spine_id)]
    assert vf._uses_subclaim_convention(cits) is True


@pytest.mark.asyncio
async def test_the_evidence_map_carries_the_records_own_arithmetic() -> None:
    """2026-09-05 — THE LIVE DEFECT, as an assertion.

    The first Assessment the channel ever published graded **0.00**: seven
    claims, none supported, six of them faithful. The voice is handed THREE
    things (§1.1) — the header, the rendered record, and the record's own
    arithmetic, which the prompt introduces as *"facts about the record, handed
    to you. You may quote them"* — and the evidence map carried only the second.
    So "the ledger notes six reads sit below the verification floor" and
    "top-share 1.000" were quoted accurately off the record's own page and
    resolved against nothing, and the only verdict a faithfulness judge can
    return for a claim whose truthmaker is absent from its evidence is
    *unsupported*.

    Every number the prompt hands the voice is now IN the map it is graded
    against. This is a re-mint, not a widening: the arithmetic is computed from
    ``payload``, which is still the only argument either function has."""
    payload = _payload()
    _, result = await _run_channel(payload)
    blob = "\n".join(
        c["evidence_text"] for c in result.finding.data["citations"]
    )
    for line in apr.record_arithmetic(payload).splitlines():
        assert line in blob, f"the voice was handed {line!r} and cannot cite it"
    assert apr.render_lead_test(payload) in blob


def test_the_arithmetic_in_the_map_is_not_read_as_a_desk_sentence() -> None:
    """THE FALSE-POSITIVE DIRECTION, and it is the one that would matter.

    ``composition_integrity`` grades a body against the cited sub-claim's WORDS —
    ``asserts_desk_negative`` and ``absence_scope_laundered`` both walk
    ``desk_absence_sentences`` of the cited head — so a longer ``evidence_text``
    could in principle manufacture an absence sentence that the record never
    carried, which is precisely the class this whole train exists to remove.

    It does not: the arithmetic block contributes NO absence sentence, and the
    BLUF the desk-verdict checks read is unchanged. (``has_collection_denominator
    _scope`` does return True over the whole block, on the word "examined" in the
    tension line — which is why the assertion below is on the SENTENCE walk the
    checks actually perform, not on the predicate they never call on it.)"""
    payload = _payload()
    arithmetic = apr.record_arithmetic(payload)
    assert ci.desk_absence_sentences(arithmetic) == []
    for ordinal, span in au.spine_span_text(payload).items():
        combined = ac.assessment_evidence_text(span, arithmetic)
        assert ci.desk_absence_sentences(combined) == ci.desk_absence_sentences(span)
        assert ci.desk_verdict_text(combined) == ci.desk_verdict_text(span)


def test_an_empty_arithmetic_block_leaves_the_map_byte_identical() -> None:
    """The additive direction, stated as an equality: nothing to add means
    nothing added, so the D-6 shape is what a payload with no counters gets."""
    assert ac.assessment_evidence_text("the desk's words", "") == "the desk's words"


@pytest.mark.asyncio
async def test_the_channel_never_sets_severity_or_out_confidences_the_record(
) -> None:
    """§2.2 — the read's BLUF, severity and confidence are the RECORD's columns.
    Severity reaches the column through exactly one path (``severity_from_tags``)
    and the absence of that tag is the enforcement."""
    payload = _payload()
    _, result = await _run_channel(payload)
    assert severity_from_tags(result.finding.tags) is None
    assert not any(t.startswith("severity:") for t in result.finding.tags)
    assert result.finding.confidence == 0.62
    row = _spine_row(payload, confidence=0.30)
    result2 = await synth._run(
        [row], dict(_OPTIONS), llm=_ProseLLM(), max_tokens=768, temperature=1.0,
        system_prompt="unused",
    )
    assert result2.finding.confidence == 0.30, (
        "the channel is never more confident than the record it reads"
    )


@pytest.mark.asyncio
async def test_it_folds_on_its_own_signature_and_never_mints_a_situation(
) -> None:
    payload = _payload()
    _, result = await _run_channel(payload)
    sig = result.finding.data["situation_signature"]
    assert sig == "composition:world_assessment:world"
    assert sig.startswith(finding_supersession._COMPOSITION_RAW_SIG_PREFIX)
    assert (
        ac.ASSESSMENT_ANALYST_ID
        in finding_supersession._COMPOSITION_ANALYST_IDS
    ), "an unlisted id FAILS OPEN here — a report would mint a fake situation"


# ---------------------------------------------------------------------------
# §2.4 — THE UNSUPPORTED-BY-SPINE MARKER
# ---------------------------------------------------------------------------


def _marks(prose_body, payload=None):
    return au.find_unsupported(prose_body, payload or _payload())


def test_the_rank_class_fires_on_a_comparison_the_record_never_made() -> None:
    marks, _ = _marks(
        "The tanker attacks defines the top risk in this cycle [[ref:1]]."
    )
    hit = [m for m in marks if m["class"] == au.UNSUPPORTED_RANK]
    assert len(hit) == 1
    assert hit[0]["text"] == "defines the top risk in this cycle"
    assert "bounded to one target and one question" in hit[0]["note"]


def test_the_superlative_class_fires_on_a_world_word_over_bounded_blocks(
) -> None:
    marks, _ = _marks("Globally, exporters are exposed [[ref:1]].")
    hit = [m for m in marks if m["class"] == au.UNSUPPORTED_SUPERLATIVE]
    assert len(hit) == 1
    assert hit[0]["text"] == "Globally"


def test_coverage_counts_the_blocks_the_voice_left_out_and_names_them() -> None:
    """H11 (2026-09-24). Three blocks carried, two cited: the third is named,
    and the mass share weighs the omission by the record's own arithmetic."""
    from legba.data.analysts import assessment_coverage as cov

    payload = {
        "blocks": [
            {"ordinal": 1, "target_id": "country_watch_ua", "desk": "escalation", "cited_mass": 3.0},
            {"ordinal": 2, "target_id": "country_watch_sa", "desk": "energy_security", "cited_mass": 1.0},
            {"ordinal": 3, "target_id": "country_watch_cn", "desk": "internal_stability", "cited_mass": 1.0},
        ],
        "lead": {"kind": "co_leads", "block_ordinals": [1, 2], "test": {}},
    }
    body = (
        "**Two threads**\n\n**BLUF:**\nUkraine's raids [[ref:1]] and the fuel strain "
        "in Saudi Arabia [[ref:2]] lead this cycle.\n\nMore on Ukraine [[ref:1]]."
    )
    markers = [{"ordinal": 1, "sentence_index": 2}, {"ordinal": 2, "sentence_index": 2},
               {"ordinal": 1, "sentence_index": 3}]
    c = cov.coverage_of(payload, markers, body)
    assert c["version"] == "coverage.v1"
    assert c["blocks_carried"] == 3 and c["blocks_cited"] == 2
    assert c["blocks_uncited"] == [3]
    assert c["uncited_labels"] == ["country_watch_cn"]
    assert c["blocks_cited_mass_share"] == pytest.approx(0.8)
    assert c["lead_kind"] == "co_leads" and c["lead_ordinals"] == [1, 2]
    assert c["lead_named"] is True, "the BLUF names both co-leads"


def test_coverage_lead_check_fails_when_the_bluf_demotes_a_co_lead() -> None:
    from legba.data.analysts import assessment_coverage as cov

    payload = {
        "blocks": [{"ordinal": 1, "desk": "a", "cited_mass": 2.0},
                   {"ordinal": 2, "desk": "b", "cited_mass": 2.0}],
        "lead": {"kind": "co_leads", "block_ordinals": [1, 2], "test": {}},
    }
    body = "**Headline**\n\nOnly the first thread [[ref:1]] leads.\n\nThe second [[ref:2]] follows."
    markers = [{"ordinal": 1, "sentence_index": 1}, {"ordinal": 2, "sentence_index": 2}]
    c = cov.coverage_of(payload, markers, body)
    assert c["lead_named"] is False
    assert c["blocks_uncited"] == [] and c["uncited_labels"] == []
    assert c["blocks_cited_mass_share"] == pytest.approx(1.0)


def test_coverage_is_honest_on_a_record_that_crowned_nothing_or_carries_no_mass() -> None:
    from legba.data.analysts import assessment_coverage as cov

    payload = {"blocks": [{"ordinal": 1, "desk": "a"}], "lead": {"kind": "none", "block_ordinals": [], "test": {}}}
    c = cov.coverage_of(payload, [], "No ordinal here at all.")
    assert c["lead_named"] is None, "nothing crowned: not a failed check"
    assert c["blocks_cited_mass_share"] is None, "no mass to share: unmeasured, never 0.0"
    assert c["blocks_cited"] == 0 and c["blocks_uncited"] == [1]


def test_the_uncited_class_marks_the_whole_sentence_that_names_no_ordinal() -> None:
    """H8 (2026-09-24). The world voice's 00:37Z read lost one of seven
    checkable sentences to a roster sentence written with no ordinal —
    unsupported by construction. The mark is the whole sentence: there is no
    clause to isolate when the defect is the missing warrant."""
    marks, checked = _marks(
        "The strait pressure is rising [[ref:1]]. The following 26 roster "
        "units were not carried: Argentina, Australia, Brazil."
    )
    hit = [m for m in marks if m["class"] == au.UNSUPPORTED_UNCITED]
    assert len(hit) == 1
    assert hit[0]["text"] == (
        "The following 26 roster units were not carried: Argentina, "
        "Australia, Brazil."
    )
    assert hit[0]["detector"] == au.DETECTOR_DETERMINISTIC
    assert "ordinal fence" in hit[0]["note"]
    assert checked["by_class"][au.UNSUPPORTED_UNCITED] == 1
    assert au.UNSUPPORTED_UNCITED in checked["deterministic_classes"]
    assert au.UNSUPPORTED_UNCITED not in checked["judge_classes"]


def test_the_uncited_class_exempts_the_headline_labels_and_headings() -> None:
    """The OUTPUT clause asks for a bold headline; the body shape carries bold
    labels and section headings. None of those is a claim."""
    marks, _ = _marks(
        "**Two threads, neither crowned**\n\n**BLUF:**\n"
        "Ukraine's raids lead this cycle [[ref:1]].\n\n"
        "## What would change this\n\n"
        "A confirmed restoration would ease [[ref:2]].\n\n- \n"
    )
    assert not [m for m in marks if m["class"] == au.UNSUPPORTED_UNCITED]


def test_a_fully_cited_body_carries_no_uncited_mark_and_the_class_is_still_counted(
) -> None:
    marks, checked = _marks("Exporters are exposed [[ref:1]] and say so [[ref:2]].")
    assert not [m for m in marks if m["class"] == au.UNSUPPORTED_UNCITED]
    assert checked["by_class"][au.UNSUPPORTED_UNCITED] == 0
    assert checked["version"] == "unsupported.v5"


def test_the_causal_class_fires_only_when_two_ordinals_are_welded() -> None:
    welded, _ = _marks(
        "The strait pressure [[ref:1]], indicating the quiet [[ref:2]] is "
        "temporary."
    )
    assert [m["class"] for m in welded] == [au.UNSUPPORTED_CAUSAL_LINK]
    single, _ = _marks("The strait pressure is driving exports down [[ref:1]].")
    assert not [m for m in single if m["class"] == au.UNSUPPORTED_CAUSAL_LINK], (
        "one ordinal is one block relayed — there is nothing to weld"
    )


def test_the_scope_widening_class_reuses_the_module_that_owns_it() -> None:
    """It IS ``absence_scope_laundered``, run with the spine's spans as the
    cited heads — reuse, not a second opinion."""
    marks, _ = _marks("No new infrastructure incidents have occurred [[ref:2]].")
    hit = [m for m in marks if m["class"] == au.UNSUPPORTED_SCOPE_WIDENING]
    assert len(hit) == 1
    assert "COLLECTION" in hit[0]["note"]
    kept, _ = _marks(
        "No new infrastructure incidents appear in this desk's collection "
        "[[ref:2]]."
    )
    assert not [
        m for m in kept if m["class"] == au.UNSUPPORTED_SCOPE_WIDENING
    ], "a channel that KEEPS the qualifier laundered nothing"


def test_the_instrument_class_fires_on_the_live_specimen_that_failed() -> None:
    """THE 2026-09-05 CLASS, pinned on the sentences that actually failed.

    The first natural post-fix live Assessment (2026-09-06 00:15Z) graded
    ``citation_support`` **0.4286** — 3 supported of 7 checkable — and all four
    ``soft_fail`` claims were one class: the voice narrating the record's own
    machinery. This module found ZERO marks on that body. A marker surface that
    publishes a checked zero over the exact defect that failed the read is the
    M-11 shape in a new costume, so each of the four shapes is asserted here.
    """
    for prose in (
        "This single, high-mass (9.10) read leads the cycle [[ref:1]].",
        "Its cited mass and verification score are considerably lower "
        "[[ref:2]].",
        "The remaining six reads consist mainly of meta-statements about read "
        "counts [[ref:1]].",
        "The tier does not contain granular data on export volumes [[ref:1]].",
    ):
        marks, _ = _marks(prose)
        hit = [m for m in marks if m["class"] == au.UNSUPPORTED_INSTRUMENT_PROSE]
        assert len(hit) == 1, f"unmarked instrument prose: {prose!r}"
        assert "not the world" in hit[0]["note"]


def test_the_instrument_class_survives_the_core_planes_own_punctuation() -> None:
    """The live body writes ``high‑mass`` with U+2011, and 58.2% of graded
    claims carry it (``text_fold``'s banner). The fold maps it to a hyphen but
    is NOT length-preserving, so the tolerance lives in the PATTERN and the
    offset is still taken against the raw sentence — proven by slicing it back
    the way the reader does."""
    body = (
        "A well‑verified read with high‑mass sits above the rest "
        "[[ref:1]]."
    )
    marks, _ = _marks(body)
    hit = [m for m in marks if m["class"] == au.UNSUPPORTED_INSTRUMENT_PROSE]
    assert len(hit) == 1
    assert au.mark_text(body, hit[0]) == hit[0]["text"]
    assert "‑" in hit[0]["text"]


def test_the_instrument_class_reads_the_plural_the_replay_caught() -> None:
    """THE N=5 REPLAY'S OWN FINDING, pinned so it cannot come back.

    The second replay spine wrote *"their verification scores are lower"* and
    the class did not fire: the lexicon holds the singular and the trailing
    word-boundary refuses the plural. The fix is a trailing ``s?`` in the
    pattern rather than a second copy of every entry, and this is the case that
    bought it."""
    marks, _ = _marks("Their verification scores are considerably lower [[ref:2]].")
    assert [m["class"] for m in marks] == [au.UNSUPPORTED_INSTRUMENT_PROSE]


# ---------------------------------------------------------------------------
# G3 (2026-09-06) — THE APERTURE CLASS AND THE RECORD'S DECLARED APERTURE.
#
# D-6 §6.1's second follow-on. The body shape MANDATES a blind-spot section and
# the voice was handed only COUNTS to write it from, so the N=5 replay caught it
# guessing ("provides no coverage of Central Asia or broader South America" on a
# record naming neither). The record knew the real answer — the drop ledger
# carries the desk, target and head title of every read it saw and did not carry
# — and discarded it at the prompt boundary.
# ---------------------------------------------------------------------------

APERTURE_GUESS = (
    "**Two threads**\n"
    "\n"
    "**BLUF:** Export pressure carries this record [[ref:1]].\n"
    "\n"
    "## What this reading misses\n"
    "This record provides no coverage of Central Asia or broader South America "
    "[[ref:1]].\n"
)


def test_the_declared_aperture_names_the_drop_ledgers_own_rows() -> None:
    """THE REPAIR ITSELF: names, not counts.

    ``_payload()`` carries three candidates and two blocks, so exactly one desk
    read was seen and left below the cut — and the aperture block says WHICH,
    with the desk, the target and the head's own title, which is what a voice
    needs to write a blind spot instead of inventing one.
    """
    payload = _payload()
    block = apr.declared_aperture(payload)
    dropped = (payload["drops"]["not_selected"] or [])[0]
    assert "THE DECLARED APERTURE" in block
    assert dropped["desk"] in block and str(dropped["target_id"]) in block
    assert dropped["title"][:40] in block
    # It rides into the arithmetic, which is what puts it in the EVIDENCE MAP.
    assert block in apr.record_arithmetic(payload)


def test_the_declared_aperture_is_in_the_evidence_map_and_in_the_prompt() -> None:
    """The two surfaces that make a blind-spot sentence gradeable at all: the
    voice is shown the names, and the grader is shown the same bytes."""
    payload = _payload()
    block = apr.declared_aperture(payload)
    assert block in apr.build_assessment_prompt(payload)
    entries = ac.build_assessment_citations(
        payload, spine_id=str(uuid4()), spine_analyst="world_assessor",
        markers=[{"ordinal": 1}],
    )
    assert entries
    for entry in entries:
        assert block in entry["evidence_text"]


def test_the_aperture_class_fires_on_the_replays_own_guess() -> None:
    """THE SPECIMEN, verbatim from the N=5 replay's 09-04 12:00Z arm-B body."""
    marks, _ = _marks(APERTURE_GUESS)
    hit = [m for m in marks if m["class"] == au.UNSUPPORTED_APERTURE_UNROSTERED]
    assert len(hit) == 1
    assert "Central Asia" in hit[0]["text"]
    assert "appears NOWHERE in this record" in hit[0]["note"]


def test_a_blind_spot_the_record_DID_declare_is_not_marked() -> None:
    """The direction that matters: naming a real dropped unit is the behaviour
    this class exists to reward, and it must cost nothing."""
    payload = _payload()
    dropped = payload["drops"]["not_selected"][0]
    body = (
        "**Two threads**\n"
        "\n"
        "**BLUF:** Export pressure carries this record [[ref:1]].\n"
        "\n"
        "## What this reading misses\n"
        f"The {dropped['desk']} read on {dropped['target_id']} was ranked "
        "below the cut, so nothing here speaks to it [[ref:1]].\n"
    )
    marks, _ = _marks(body, payload)
    assert [
        m for m in marks if m["class"] == au.UNSUPPORTED_APERTURE_UNROSTERED
    ] == []


def test_the_aperture_class_runs_in_the_aperture_section_only() -> None:
    """Elsewhere in the body a proper noun is a claim about what the record
    SAYS, and the ordinal fence plus the ``fact`` class already own that. The
    same sentence under a different heading is untouched."""
    body = (
        "**Two threads**\n"
        "\n"
        "**BLUF:** Export pressure carries this record [[ref:1]].\n"
        "\n"
        "## The reading\n"
        "This record provides no coverage of Central Asia or broader South "
        "America [[ref:1]].\n"
    )
    marks, _ = _marks(body)
    assert [
        m for m in marks if m["class"] == au.UNSUPPORTED_APERTURE_UNROSTERED
    ] == []


def test_a_name_the_desks_own_span_carries_is_relayed_not_flagged() -> None:
    """The relay idiom, stated generously on purpose (see
    :func:`aperture_vocabulary`): a name the record itself prints is a name the
    record has seen, and marking it would tax a sentence for using the record's
    own words. The desk body names the Strait of Hormuz."""
    body = (
        "**Two threads**\n"
        "\n"
        "**BLUF:** Export pressure carries this record [[ref:1]].\n"
        "\n"
        "## What this reading misses\n"
        "Nothing here reaches past the Strait of Hormuz into the wider Gulf "
        "littoral [[ref:1]].\n"
    )
    marks, _ = _marks(body)
    flagged = [
        m for m in marks if m["class"] == au.UNSUPPORTED_APERTURE_UNROSTERED
    ]
    assert not any("Hormuz" in m["text"] for m in flagged)


def test_a_sentence_quoting_the_ledgers_own_handle_is_grounded_the_replay_caught(
) -> None:
    """THE N=5 REPLAY'S OWN FINDING, and it was a false fire on the best
    aperture sentence the train produced.

    Arm B's 09-06 body wrote *"The tier omitted reads on Russia
    (country_g20_ru), Britain (country_g20_gb), Haiti (country_watch_ht) … that
    were seen and ranked below the cut"* — five real uncovered units, in the
    world's nouns, each with the record's own handle beside it. The drop ledger
    carries the SLUG and never the human name, so "Russia" resolved against
    nothing and the class marked exactly the prose it exists to reward.

    The fix is the LEDGER-GROUNDING exemption rather than a gazetteer: a
    blind-spot sentence carrying a declared unit's identifier has grounded
    itself in the ledger, and the proper nouns beside that identifier are its
    human reading. A gazetteer would be a second world model inside a fence
    whose whole point is that the record is the only one.
    """
    payload = _payload()
    dropped = payload["drops"]["not_selected"][0]
    body = (
        "**Two threads**\n"
        "\n"
        "**BLUF:** Export pressure carries this record [[ref:1]].\n"
        "\n"
        "## What this reading misses\n"
        f"The tier omitted its read on Haiti ({dropped['target_id']}), which "
        "was seen and ranked below the cut [[ref:1]].\n"
    )
    marks, _ = _marks(body, payload)
    assert [
        m for m in marks if m["class"] == au.UNSUPPORTED_APERTURE_UNROSTERED
    ] == [], "the ledger's own handle grounds the human name beside it"
    # The identifier set is NARROWER than the vocabulary on purpose: a handle is
    # something a voice can only have got from the record, so this exemption
    # cannot be earned by ordinary prose the way a shared English word can.
    idents = au.aperture_unit_identifiers(payload)
    assert dropped["target_id"] in idents
    assert "asia" not in idents and "the" not in idents


def test_the_aperture_vocabulary_is_the_records_three_surfaces() -> None:
    """Roster, drop ledger, carried blocks — and the blocks' quoted spans. The
    fence and the prompt read the SAME bytes, which is what stops the voice
    being marked against a vocabulary it was never shown."""
    payload = _payload()
    vocab = au.aperture_vocabulary(payload)
    assert "hormuz" in vocab, "the desks' own quoted words"
    assert "energy_security" in vocab or "energy" in vocab, "a carried desk"
    dropped = payload["drops"]["not_selected"][0]
    assert dropped["desk"].split("_")[0] in vocab, "a drop-ledger desk"
    assert "asia" not in vocab


# ---------------------------------------------------------------------------
# 2026-09-08 — THE ROSTER RENDERED WHOLE (live defect, two draws).
#
# Amendment 5 (WORLD_READ_CONSISTENCY, deployed 09-07 05:56Z) persisted
# ``coverage_roster`` on the world spine, which turned ON ``declared_aperture``'s
# roster branch for the first time. The branch rendered the roster through
# ``_named`` — the cap sized for TITLE-BEARING drop-ledger rows — so a 33-unit
# list of bare machine handles reached the evidence map as five names and
# "and 28 more".
#
# The voice completed it, on both post-Amendment-5 draws that wrote the section,
# and completed it CORRECTLY: all 25 uncarried units, exactly (roster minus the
# eight carried), reconstructed from the ISO pattern. 16 and 17 of those names
# were nowhere in the evidence map, so the judge graded the sentence
# ``judge_contradicted`` (HARD, 09-07 12:16Z) and ``judge_unsupported``
# (09-08 00:15Z) — correctly, on the bytes it was shown.
#
# Meanwhile ``aperture_vocabulary`` fences the section to the WHOLE roster and
# says in terms that the fence and the prompt "cannot drift". They had.
# ---------------------------------------------------------------------------

#: The live 2026-09-08 12:00Z world spine's declared roster, verbatim.
LIVE_ROSTER = (
    "country_g20_ar", "country_g20_au", "country_g20_br", "country_g20_ca",
    "country_g20_cn", "country_g20_de", "country_g20_fr", "country_g20_gb",
    "country_g20_id", "country_g20_in", "country_g20_it", "country_g20_jp",
    "country_g20_kr", "country_g20_mx", "country_g20_ru", "country_g20_sa",
    "country_g20_tr", "country_g20_us", "country_g20_za", "country_watch_bf",
    "country_watch_cd", "country_watch_ht", "country_watch_il",
    "country_watch_ir", "country_watch_kp", "country_watch_ml",
    "country_watch_mm", "country_watch_ne", "country_watch_pk",
    "country_watch_sd", "country_watch_tw", "country_watch_ua",
    "escalation_composition",
)

#: The eight the live record carried. Roster minus these is the 25 the voice
#: enumerated from world knowledge because the map would not print them.
LIVE_CARRIED = (
    "escalation_composition", "country_watch_il", "country_g20_sa",
    "country_watch_ir", "country_g20_ru", "country_watch_sd",
    "country_watch_ua", "country_g20_us",
)


def _rostered_payload(*, carried=8):
    """The live world shape: a 33-unit roster, every unit in the basis, 8 carried.

    ``_payload()`` passes no roster at all, which is why the drift below went
    unmeasured for two trains: with an empty ``coverage_roster`` the branch that
    carries the defect never runs.
    """
    rows = []
    for unit in LIVE_ROSTER:
        rows.append(
            _row(
                uuid4(),
                analyst_id="country_composition",
                target_id=unit,
                body=DESK_BODY,
                severity="high",
                signal_ids=("s1",),
            )
        )
    by_unit = {r["target_id"]: r for r in rows}
    ordered = [by_unit[u] for u in LIVE_CARRIED] + [
        r for r in rows if r["target_id"] not in LIVE_CARRIED
    ]
    ap.attach_cited_salience(ordered, {"s1": 0.95})
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-08T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[:carried],
        coverage=[{"unit": u, "status": "in_basis"} for u in LIVE_ROSTER],
        coverage_roster=list(LIVE_ROSTER),
    )


def test_the_declared_roster_is_rendered_whole_not_five_and_a_promise() -> None:
    """THE DEFECT, at the byte the judge reads.

    Before: ``declared roster (33 units): country_g20_ar; country_g20_au;
    country_g20_br; country_g20_ca; country_g20_cn; and 28 more.`` — five names
    and an invitation to write the other 28, which is what the voice did.
    """
    block = apr.declared_aperture(_rostered_payload())
    missing = [u for u in LIVE_ROSTER if u not in block]
    assert missing == [], (
        "every declared unit is a name the fence marks against, so every one "
        f"of them has to be a name the map prints; absent: {missing}"
    )
    assert "and 28 more" not in block
    assert f"declared roster ({len(LIVE_ROSTER)} units)" in block


def test_the_fence_and_the_map_read_one_list_the_promise_made_good() -> None:
    """``aperture_vocabulary``'s docstring says ``declared_aperture`` "renders
    precisely this set, so the fence and the prompt cannot drift: the voice is
    marked against the same bytes it was handed". The cap made that false — the
    fence held 33 identifiers and the map printed 5. This is the sentence, as an
    assertion."""
    payload = _rostered_payload()
    block = apr.declared_aperture(payload).lower()
    unshown = sorted(
        ident
        for ident in au.aperture_unit_identifiers(payload)
        if ident not in block
    )
    assert unshown == [], (
        "an identifier the deterministic fence accepts and the evidence map "
        f"withholds is a sentence the voice may write and the judge must fail: {unshown}"
    )


def test_the_live_hard_fail_specimen_now_has_its_truthmakers() -> None:
    """THE LIVE 2026-09-07 12:16Z SENTENCE, verbatim, and it was CORRECT.

    ``judge_contradicted`` — HARD — with the judge's own recorded detail
    quoting the aperture block back: *"contradicted by a verbatim evidence
    span: '* of those, every declared unit reached the basis this cycle.'"*.
    Every unit the sentence names is a real uncarried unit of the record's own
    roster; 16 of them were nowhere in the evidence map.
    """
    payload = _rostered_payload()
    block = apr.declared_aperture(payload)
    named = [u for u in LIVE_ROSTER if u not in LIVE_CARRIED]
    assert len(named) == 25
    assert [u for u in named if u not in block] == []


def test_the_basis_line_no_longer_reads_as_an_empty_aperture() -> None:
    """The other half of the same hard fail.

    "Reached the basis" is CANDIDACY. On a record that ranked 33 and carried 8
    it is true of all 33 and says nothing about carriage, and left bare it read
    — to the judge, and reasonably — as "nothing is missing", which refutes the
    section the body shape mandates. The record knows the difference; the map
    now states it, and names the carried set so the subtraction is checkable.
    """
    block = apr.declared_aperture(_rostered_payload())
    assert "CANDIDACY, not carriage" in block
    assert f"this record CARRIED {len(LIVE_CARRIED)}" in block
    for unit in LIVE_CARRIED:
        assert unit in block


def test_the_rendered_roster_stays_inside_the_evidence_map_budget() -> None:
    """THE CEILING IS A TEST, not a slice — see ``APERTURE_ROSTER_BUDGET_CHARS``.

    A roster that outgrows the budget must fail loudly rather than silently
    truncate back into the invitation this train removed. The live 33-unit
    roster renders at 579 bytes; the block is byte-identical in every citation,
    so at ``BLOCK_CAP`` = 8 it is paid eight times.
    """
    rendered = apr._named_units(LIVE_ROSTER)
    assert len(rendered) <= apr.APERTURE_ROSTER_BUDGET_CHARS, (
        "the roster outgrew the budget this decision was made against; make it "
        "again with the new number rather than truncating into a guess"
    )


def test_a_rostered_arithmetic_block_is_still_not_a_desk_sentence() -> None:
    """THE FALSE-POSITIVE DIRECTION, re-run on the branch that now has bytes in
    it. ``composition_integrity`` walks the cited head's absence sentences, so a
    longer aperture block could in principle manufacture a desk negative the
    record never carried — the class this whole train exists to remove."""
    payload = _rostered_payload()
    arithmetic = apr.record_arithmetic(payload)
    assert ci.desk_absence_sentences(arithmetic) == []
    for _ordinal, span in au.spine_span_text(payload).items():
        combined = ac.assessment_evidence_text(span, arithmetic)
        assert ci.desk_absence_sentences(combined) == ci.desk_absence_sentences(span)
        assert ci.desk_verdict_text(combined) == ci.desk_verdict_text(span)


def test_the_voice_and_the_judge_are_shown_the_same_roster() -> None:
    """P1's CONTRACT, as an assertion — and the one that would have caught this.

    ``fa3d2d85`` put the record's arithmetic into every citation's
    ``evidence_text`` for exactly one reason: what the voice is told has to be
    what the judge can check. Amendment 5 (2026-09-07) widened one side of that
    and not the other — its new ``## Coverage`` section put all 33 roster units
    into the rendered body the prompt carries, while the aperture block kept
    showing the judge five of them. Measured on the live spines: 33/33 visible
    to the voice, 5 visible to the judge, and the two draws that relayed the
    names lost the sentence (one HARD).

    So the invariant is not "the aperture block is long enough". It is that a
    roster unit the PROMPT names is a roster unit the EVIDENCE MAP names.
    """
    payload = _rostered_payload()
    prompt = apr.build_assessment_prompt(payload)
    arithmetic = apr.record_arithmetic(payload)
    in_prompt = [u for u in LIVE_ROSTER if u in prompt]
    assert in_prompt, "the fixture must actually put the roster in front of the voice"
    withheld = [u for u in in_prompt if u not in arithmetic]
    assert withheld == [], (
        "a unit the voice is shown and the judge is not is a sentence the "
        f"channel invites and the grader must fail: {withheld}"
    )
    # And the map the citations actually carry is the arithmetic, so the same
    # holds at the surface the judge reads.
    entries = ac.build_assessment_citations(
        payload, spine_id=str(uuid4()), spine_analyst="world_assessor",
        markers=[{"ordinal": 1}],
    )
    assert entries
    for entry in entries:
        assert all(u in entry["evidence_text"] for u in in_prompt)


def test_a_record_with_no_roster_is_byte_identical_to_the_shipped_block() -> None:
    """THE INERTNESS HALF. Every tier that publishes no ``coverage_roster`` —
    which is every tier but the world assembly — takes the else-branch, and this
    train did not touch one byte of it."""
    block = apr.declared_aperture(_payload())
    assert "unit roster: not computed at this grain" in block
    assert "declared roster" not in block
    assert "CARRIED" not in block


def test_a_counter_the_record_publishes_is_not_instrument_prose() -> None:
    """THE ARITHMETIC EXEMPTION, and it is the class's whole boundary.

    P1 (``fa3d2d85``) put the record's arithmetic block behind every ordinal's
    ``evidence_text``, so the counters the record publishes about itself ARE
    gradeable — the live body's "Six reads fell below the verification floor"
    graded *supported* in the same read whose per-block numbers did not. The
    aperture section is MANDATED to name those counts, so marking them would tax
    exactly the sentence the body shape asks for.
    """
    payload = _payload()
    assert "below the verification floor" in apr.record_arithmetic(payload)
    marks, _ = _marks(
        "Six reads sit below the verification floor, and what they would have "
        "shown of the export chain is not on this page [[ref:1]].",
        payload,
    )
    assert not [
        m for m in marks if m["class"] == au.UNSUPPORTED_INSTRUMENT_PROSE
    ], "a counter the record hands the voice is relayed, and it is in the map"


def test_the_instrument_class_reports_itself_in_the_checked_block() -> None:
    """A deterministic class that the counter block does not name is a class the
    row cannot be audited for. Both the 09-05 class and the 09-06 one."""
    marks, checked = _marks(
        "Its cited mass is considerably lower [[ref:2]]."
    )
    assert au.UNSUPPORTED_INSTRUMENT_PROSE in checked["deterministic_classes"]
    assert au.UNSUPPORTED_APERTURE_UNROSTERED in checked["deterministic_classes"]
    assert checked["by_class"][au.UNSUPPORTED_INSTRUMENT_PROSE] == len(
        [m for m in marks if m["class"] == au.UNSUPPORTED_INSTRUMENT_PROSE]
    )
    # G3: the aperture class's DENOMINATOR is published too. A checked zero over
    # a four-word vocabulary and one over a nine-hundred-word vocabulary are
    # different facts, and §1.6 rule 2 applies to this class as to the others.
    assert checked["aperture_vocabulary_words"] > 0
    # o4: and the UNIT denominator the aperture_guess class is fenced to, for
    # the same reason — narrower than the vocabulary, and a different fact.
    assert au.UNSUPPORTED_APERTURE_GUESS in checked["deterministic_classes"]
    assert checked["aperture_units_declared"] >= 0
    assert checked["version"] == "unsupported.v5", (
        "a lexicon change that does not move the version is invisible in the "
        "data"
    )


def test_the_clause_is_in_the_prompt_and_the_version_moved_with_it() -> None:
    """E-2 / N2 — D-6 §7.6's one prompt clause, pinned where it actually
    reaches the model.

    The Assessment's system prompt is a CODE CONSTANT, not
    ``method.system_prompt`` (``run_assessment`` passes ``ASSESSMENT_SYSTEM``
    and the descriptor declares none), so this is the only place a pin can hold
    it. Both halves are asserted: the voice may READ the arithmetic to decide,
    and it must not NARRATE the instrument; and the aperture section is aimed at
    the world rather than at the tier."""
    system = apr.ASSESSMENT_SYSTEM
    assert "THE INSTRUMENT IS NOT THE WORLD" in system
    assert "It is your instrument panel. It is not your subject." in system
    assert "--- THE RECORD THIS BLOCK SITS IN ---" in system, (
        "the voice is told the exact rule its arithmetic evidence rides under"
    )
    # The aperture stays a section, and it speaks about the world.
    assert "## What this reading misses" in system
    assert "it is about the WORLD, not about this instrument" in system
    # D-6's voice contract is otherwise intact.
    assert "FACT vs PERSPECTIVE" in system
    assert "THREADS, WEIGHTED, UNCROWNED" in system
    assert apr.PROMPT_VERSION == "assessment_prompt.v3", (
        "a voice change invisible in the data is one nobody can attribute"
    )


@pytest.mark.asyncio
async def test_the_voice_prompt_is_code_and_a_descriptor_PUT_cannot_move_it(
) -> None:
    """WHY THE CLAUSE SHIPS AS A DEPLOY AND NOT AS A PROMPT-ONLY PUT.

    ``deps.system_prompt`` is where a descriptor's ``method.system_prompt`` AND
    an operator-promoted GEPA candidate both land (``analyst_deps_builder``), and
    ``_run`` forwards it to every other branch. This branch DISCARDS it: the
    model is handed ``assessment_prompts.ASSESSMENT_SYSTEM`` and nothing else.

    So a prompt-only PUT onto the live ``world_assessment`` head would be inert
    — the bytes would sit in the registry, never reach the model, and make the
    prompt manifest report a prompt that is not the live one. Asserted rather
    than reasoned about, because the deploy plan depends on it, and because R4's
    FZ-3 freezes descriptor PUTs to this analyst.
    """
    payload = _payload()
    llm = _ProseLLM(CLEAN_PROSE)
    await _run_channel(payload, llm=llm, options={
        "analyst_id": ac.ASSESSMENT_ANALYST_ID,
    })
    assert llm.system == apr.ASSESSMENT_SYSTEM
    assert llm.system != "unused on this branch", (
        "the descriptor/GEPA prompt reaches this branch and is dropped — a PUT "
        "cannot deliver this channel's voice"
    )


def test_a_ranking_word_the_desk_itself_used_is_relayed_not_flagged() -> None:
    """§2.4's exemption, and it is the difference between a channel that may
    quote a desk's judgement and one that may only paraphrase it."""
    payload = _payload(third_body=CROWNED_BODY, carried=3)
    marks, _ = _marks(
        "The Kenscoff massacre is the most consequential development on this "
        "desk this week [[ref:3]].",
        payload,
    )
    assert not [m for m in marks if m["class"] == au.UNSUPPORTED_RANK]


def test_the_records_own_earned_lead_is_not_an_unsupported_ranking() -> None:
    """The ONE ranking the record performs is the earned-lead test. When it
    fires and the voice crowns exactly the block it crowned, the ranking is the
    record's arithmetic relayed — and a mark there would contradict a number
    printed on the same page."""
    payload = _payload()
    payload["lead"] = {
        "kind": "earned_single", "block_ordinals": [1],
        "test": {**payload["lead"]["test"], "earned": True},
    }
    marks, _ = _marks(
        "Saudi export disruption dominates this record [[ref:1]].", payload
    )
    assert not [m for m in marks if m["class"] == au.UNSUPPORTED_RANK]
    # …and the exemption is exactly as wide as the verdict: a sentence that
    # crowns a block the test did NOT crown is still a claim of its own.
    marks2, _ = _marks(
        "The Myanmar quiet dominates this record [[ref:2]].", payload
    )
    assert [m["class"] for m in marks2] == [au.UNSUPPORTED_RANK]


def test_a_denied_causal_link_is_not_a_causal_link() -> None:
    """Live specimen: "treated as independent strands rather than mutually
    reinforcing dynamics" is the record's own zero-tension finding being
    relayed."""
    marks, _ = _marks(
        "The observations [[ref:1]] and [[ref:2]] are treated as independent "
        "strands rather than mutually reinforcing dynamics."
    )
    assert not [m for m in marks if m["class"] == au.UNSUPPORTED_CAUSAL_LINK]


def test_a_refusal_to_rank_is_not_a_ranking_claim() -> None:
    """The uncrowned bottom line is the shape the contract ASKS for on a cycle
    whose lead test did not fire. Marking it would tax the honest sentence."""
    marks, _ = _marks(
        "Two threads carry this record and neither outweighs the other "
        "[[ref:1]] [[ref:2]]."
    )
    assert not [m for m in marks if m["class"] == au.UNSUPPORTED_RANK]


def test_marks_land_on_their_own_text_the_way_the_reader_slices() -> None:
    """The reader marks inline from these offsets and runs no second detector,
    so the row and the page cannot disagree — as long as the offsets are in the
    reader's unit."""
    marks, _ = _marks(PROSE)
    assert marks
    for m in marks:
        assert au.mark_text(PROSE, m) == m["text"]
        assert m["text"] == m["text"].strip()
        assert not m["text"].endswith("[[ref")


def test_offsets_are_utf16_code_units_not_code_points() -> None:
    """The reader slices in JavaScript, whose index unit is the UTF-16 code
    unit. One astral character before a mark and code-point offsets move every
    later mark left by one — silently."""
    body = "🛰 Globally, exporters are exposed [[ref:1]]."
    marks, _ = _marks(body)
    assert marks
    for m in marks:
        assert au.mark_text(body, m) == m["text"]
    assert au.utf16_offset(body, 1) == 2


def test_marks_never_overlap_and_arrive_in_reading_order() -> None:
    """``markBody`` keeps the first of two overlapping marks and drops the rest
    SILENTLY, so a row carrying an overlap would publish a mark the page never
    shows. Resolved here, and the suppression is counted."""
    marks, checked = _marks(PROSE)
    starts = [m["char_start"] for m in marks]
    assert starts == sorted(starts)
    for a, b in zip(marks, marks[1:]):
        assert a["char_end"] <= b["char_start"]
    assert checked["overlaps_suppressed"] >= 0
    assert checked["marks"] == len(marks)


def test_the_zero_is_a_CHECKED_zero() -> None:
    """§1.6 rule 2, one tier up: "nothing flagged" that does not say what it
    checked is the M-11 defect in a new costume."""
    body = (
        "**BLUF:** The energy desk reports export disruption in its own words "
        "[[ref:1]].\n"
    )
    marks, checked = _marks(body)
    assert marks == []
    assert checked["sentences_examined"] >= 1
    assert checked["judge_state"] == au.JUDGE_STATE_NOT_RUN
    assert "has not landed" in checked["judge_note"]
    assert checked["deterministic_classes"] == list(
        au.UNSUPPORTED_DETERMINISTIC_CLASSES
    )
    assert checked["judge_classes"] == [au.UNSUPPORTED_FACT]
    assert checked["by_class"][au.UNSUPPORTED_FACT] == 0


def test_the_judge_seam_accepts_the_fact_class_and_says_it_ran() -> None:
    """D-3's arm has somewhere to land: the ``fact`` class is a merge, not a
    rebuild, and ``judge_state`` distinguishes "found nothing" from "did not
    look"."""
    body = "The reactor shutdown continues [[ref:1]]."
    judge_mark = {
        "sentence_index": 0, "char_start": 4, "char_end": 21,
        "text": "reactor shutdown", "class": au.UNSUPPORTED_FACT,
        "detector": au.DETECTOR_JUDGE, "note": "No spine span states it.",
    }
    marks, checked = au.find_unsupported(
        body, _payload(), judge_marks=[judge_mark], judge_ran=True,
    )
    assert [m["class"] for m in marks] == [au.UNSUPPORTED_FACT]
    assert checked["judge_state"] == au.JUDGE_STATE_RAN
    assert checked["by_class"][au.UNSUPPORTED_FACT] == 1


# ---------------------------------------------------------------------------
# §2.3 — THE BADGE
# ---------------------------------------------------------------------------


def test_the_round_lineage_is_pinned_and_append_only() -> None:
    """F-6 — the numbers have no DB home, so they live here, frozen, with their
    source file. A badge that changes retroactively is a badge nobody can
    audit."""
    assert [r.round for r in rl.ROUND_LINEAGE] == ["R1", "R2", "R3"]
    r3 = rl.newest_for(rl.POPULATION_COMPOSITION_PROSE)
    assert (r3.round, r3.claim_accuracy, r3.n) == ("R3", 0.4805, 77)
    assert r3.date == "2026-08-29"
    assert r3.source_file.endswith("PROOF_ROUND_2026-08-29/VERDICT.md")
    assert all(r.inter_rater is None for r in rl.ROUND_LINEAGE), (
        "no round computed one; a fabricated kappa on a badge is the exact "
        "class of number this program exists to stop"
    )
    assert "NOT COMMENSURABLE" in rl.ROUND_LINEAGE[0].notes


def test_the_badge_never_shows_a_bare_number() -> None:
    badge = rl.assessment_badge()
    ext = badge["external_accuracy"]
    assert ext["value"] == 0.4805 and ext["n"] == 77
    assert ext["population"] == rl.POPULATION_COMPOSITION_PROSE
    assert badge["external_accuracy_note"] == rl.PRE_ASSEMBLY_NOTE
    assert "PRE-assembly" in badge["external_accuracy_note"]


def test_an_unmeasured_fidelity_says_unmeasured_never_zero() -> None:
    """0.00 on a badge reads as a measured failure. D-3's arm has not landed,
    so the honest render is "not measured yet" — and the reader already has
    that branch."""
    badge = rl.assessment_badge()
    assert badge["fidelity_to_spine"] is None
    assert badge["fidelity_state"] == rl.FIDELITY_UNMEASURED
    measured = rl.assessment_badge(fidelity_to_spine=0.9375, fidelity_n=8)
    assert measured["fidelity_to_spine"] == 0.9375
    assert measured["fidelity_state"] == rl.FIDELITY_MEASURED


def test_the_channels_own_lane_is_empty_until_r5() -> None:
    """No round has ever graded this channel against the world. The absence is
    the point, and it is why the badge borrows a number WITH its borrowed-ness
    stated."""
    assert rl.newest_for(rl.POPULATION_ASSESSMENT) is None
    assert rl.external_accuracy(rl.POPULATION_ASSESSMENT) is None
    assert "own_accuracy" not in rl.assessment_badge()


# ---------------------------------------------------------------------------
# §2.5 — ITS OWN POPULATION ([N+1] EQUIVALENCE)
# ---------------------------------------------------------------------------


def _composition_finding(n: int) -> dict:
    """A composition row in the shape verify grades: prose with ``[[ref:N]]``
    markers over sub-claim citations."""
    return {
        "analyst_id": "country_composition",
        "body": (
            f"*As of 2026-09-03.*\n\n**BLUF:** The energy desk records export "
            f"disruption in window {n} [[ref:1]].\n\n## The picture\n"
            f"A second desk reports a quiet window [[ref:2]].\n"
        ),
        "citations": [
            {"marker": "[[ref:1]]", "ordinal": 1, "ref_id": str(uuid4()),
             "ref_kind": "finding", "effective_confidence": 0.8,
             "evidence_text": (
                 "The energy desk records export disruption in window "
                 f"{n}."
             )},
            {"marker": "[[ref:2]]", "ordinal": 2, "ref_id": str(uuid4()),
             "ref_kind": "finding", "effective_confidence": 0.7,
             "evidence_text": "A second desk reports a quiet window."},
        ],
    }


async def _report(row) -> dict:
    report = await vf.verify_finding_faithfulness(
        body=row["body"], citations=row["citations"], judge_llm=None,
        finding_confidence=0.7, title="t",
    )
    return asdict(report)


@pytest.mark.asyncio
async def test_n_plus_one_equivalence_the_channel_cannot_move_a_composition(
) -> None:
    """§2.5 — the Assessment is graded as its own population.

    The [N+1] idiom: grade N existing composition rows, then grade the same N
    with an Assessment row present in the run, and assert the N reports are
    BYTE-IDENTICAL. This is the proof that the channel's arrival cannot move a
    composition's number — the grading path is per-row and the Assessment's
    row-level key (its ``analyst_id``) is what every pooling reader groups by.

    What it does NOT claim: that some future aggregation cannot pool them
    anyway. The tree has no per-analyst composition-faithfulness mean today
    (the one broad judge cube groups by day / provider / pipeline stamp and
    takes an optional single-analyst filter), so the pooling bar has to be
    stated in the D-3 stamp's lineage entry in words, which §3.7 requires.
    """
    rows = [_composition_finding(i) for i in range(5)]
    before = [await _report(r) for r in rows]

    payload = _payload()
    _, result = await _run_channel(payload)
    assessment = {
        "analyst_id": ac.ASSESSMENT_ANALYST_ID,
        "body": result.finding.body,
        "citations": result.finding.data["citations"],
    }
    assessment_report = await _report(assessment)

    after = [await _report(r) for r in rows]
    assert json.dumps(after, sort_keys=True) == json.dumps(before, sort_keys=True)

    # And the populations are separable by the only key that matters.
    graded = [(r["analyst_id"], rep) for r, rep in zip(rows, after)]
    graded.append((assessment["analyst_id"], assessment_report))
    by_analyst: dict[str, list[float]] = {}
    for analyst_id, rep in graded:
        by_analyst.setdefault(analyst_id, []).append(rep["faithfulness_score"])
    assert set(by_analyst) == {"country_composition", ac.ASSESSMENT_ANALYST_ID}
    comp = by_analyst["country_composition"]
    assert sum(comp) / len(comp) == sum(
        r["faithfulness_score"] for r in before
    ) / len(before)
    assert len(by_analyst[ac.ASSESSMENT_ANALYST_ID]) == 1


@pytest.mark.asyncio
async def test_fidelity_to_spine_grades_through_the_existing_machinery() -> None:
    """§2.5 — "the existing faithfulness machinery grades it correctly with no
    new rubric", because the evidence map is the spine's spans. Asserted by
    running the REAL verify path over a real channel output."""
    payload = _payload()
    _, result = await _run_channel(payload, llm=_ProseLLM(CLEAN_PROSE))
    report = await vf.verify_finding_faithfulness(
        body=result.finding.body,
        citations=result.finding.data["citations"],
        judge_llm=None,
        finding_confidence=result.finding.confidence,
        title=result.finding.title,
    )
    assert report.checkable_claims > 0, (
        "a channel whose prose is unassessable would have no fidelity to grade"
    )
    assert 0.0 <= report.faithfulness_score <= 1.0
    assert report.judge_status == "deterministic"


def test_the_assessment_is_always_judged_without_a_config_edit() -> None:
    """§2.1b(3) — 'D-6 confirms'. It matches on the KIND, and the id branch is
    there too, so neither a kind rename nor an id rename silently samples the
    channel out."""
    assert "meta_findings_synthesizer" in JUDGE_SAMPLE_ALWAYS_DEFAULT
    policy = JudgeSamplingPolicy(
        finding_id=str(uuid4()),
        kind="meta_findings_synthesizer",
        analyst_id=ac.ASSESSMENT_ANALYST_ID,
        rate=0.0,
    )
    assert policy.should_judge() is True


# ---------------------------------------------------------------------------
# §1.1 / §2.1b — THE FOUR LISTS, EXHAUSTIVELY
# ---------------------------------------------------------------------------


def test_every_hand_maintained_composition_list_names_the_channel() -> None:
    assert ac.ASSESSMENT_ANALYST_ID in _ASSESSMENT_PRODUCER_ANALYSTS
    assert ac.ASSESSMENT_ANALYST_ID in composition_lineage_sweep._COMPOSITION_ANALYSTS
    assert ac.ASSESSMENT_ANALYST_ID in composition_lineage_sweep._GAUGE_ANALYSTS
    assert ac.ASSESSMENT_ANALYST_ID in standing_auditor.DESK_ANALYST_IDS
    assert (
        ac.ASSESSMENT_ANALYST_ID in finding_supersession._COMPOSITION_ANALYST_IDS
    )


def test_the_two_stale_composition_ids_were_fixed_in_the_same_pass() -> None:
    """§1.1 — the list said region "joins this set when that leg lands" while
    region and thematic had been writing rows for two months. An unlisted id
    makes ``get_assessments`` answer a confident empty and NAME the producer as
    not live."""
    for stale in ("region_composition", "escalation_composition"):
        assert stale in _ASSESSMENT_PRODUCER_ANALYSTS


# ---------------------------------------------------------------------------
# THE DESCRIPTOR
# ---------------------------------------------------------------------------


def _descriptor():
    import yaml
    from pathlib import Path

    from legba.data.schemas.analyst import AnalystDescriptor

    root = Path(__file__).resolve().parents[2]
    body = yaml.safe_load(
        (root / "descriptors" / "analyst_world_assessment.yaml").read_text()
    )
    return AnalystDescriptor.model_validate(body, strict=False)


def test_the_descriptor_is_the_shape_the_dispatch_keys_on() -> None:
    desc = _descriptor()
    assert desc.identity.id == ac.ASSESSMENT_ANALYST_ID
    assert desc.identity.kind == "meta_findings_synthesizer", (
        "the kind is what makes the verify dispatch, the rubric routing, the "
        "always-judged default and the read path all zero-edit"
    )
    assert desc.identity.state == "draft", (
        "the record it reads does not exist in production until the cutover; "
        "draft registers it and creates no actor"
    )
    assert desc.subscription.targets is None, "one global run per tick"
    assert synth._declares_verify(desc) is True
    assert synth.assessment_spine(desc) == "world_assessor"
    assert synth.thematic_dimension(desc) is None, (
        "a thematic marker would route READ_SLICE to the wrong branch"
    )
    assert desc.method.bounded_question is None
    assert desc.method.system_prompt is None, (
        "the prose doctrine is a code constant, so a voice change is a diff"
    )
    assert desc.cadence.fallback_schedule == "35 0,12 * * *"  # restaggered 2026-09-20 (STEP E: world voice reads THIS cycle's country voices)


def test_the_descriptor_ships_in_bringup() -> None:
    import importlib.util
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "_bringup", root / "scripts" / "bringup_register_analysts.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert "analyst_world_assessment.yaml" in mod.ANALYST_FILES


@pytest.mark.asyncio
async def test_read_slice_takes_the_spine_branch_and_reads_one_row() -> None:
    """The branch is checked FIRST, and it has to be: a target-less,
    verify-declaring descriptor with no marker falls through to the WORLD
    branch and would quietly read the region/country slice."""
    seen: list[tuple] = []

    class _Conn:
        async def fetch(self, sql, *params):  # noqa: ANN001
            seen.append((sql, params))
            return []

        async def fetchrow(self, *a, **k):  # pragma: no cover
            raise AssertionError("the spine read issues exactly one query")

    rows = await synth.READ_SLICE(
        _Conn(), descriptor=_descriptor(), target_filter=None,
    )
    assert rows == []
    assert len(seen) == 1, "one row, one query"
    sql, params = seen[0]
    assert "superseded_by IS NULL" in sql
    assert "LIMIT 1" in sql
    assert params[0] == "world_assessor"
    assert params[1] == ap.ASSEMBLY_SCHEMA
    assert params[2] == ap.REGIME_ASSEMBLY
    assert params[3] == 24


# ---------------------------------------------------------------------------
# 2026-09-08 — THE TENSION RENDERED WHOLE (live defect, three sentences).
#
# The counters travelled and the CONTENT did not. Measured over the four
# post-G3 world spines: the ``statement`` reached the evidence map on 0 of the 2
# tension-bearing ones, ``b_ref.span`` on 0 of 2, and ``b_ref.target_id`` on 1
# of 2 — incidentally, via the drop ledger.
#
# So the voice is handed a tension in the rendered body, told it may neither
# invent one nor dissolve one, and graded against a map holding a COUNT. Every
# tension sentence the channel has written failed: ``soft_fail`` on 2026-09-06
# 12:15Z and 2026-09-07 00:15Z live, and a third in the 09-07 00:00Z replay.
# ---------------------------------------------------------------------------

TENSION_BLUF = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** {bluf} [1].\n"
)


def _tension_row(uid, *, target_id, bluf, sigs=("s1",)):
    return {
        "id": uid,
        "kind": "finding",
        "analyst_id": "country_composition",
        "target_id": target_id,
        "title": f"head {uid}",
        "body": TENSION_BLUF.format(bluf=bluf),
        "severity": "high",
        "confidence": 0.7,
        "produced_at": "2026-09-03T10:01:00+00:00",
        "faithfulness_score": 0.9,
        "effective_confidence": 0.8,
        "claim_verdicts": [{"verdict": "supported"}],
        "derived_from": [],
        "data": {"data": {"citations": [
            {"marker": f"[{i}]", "signal_id": s, "source_id": f"source.{s}",
             "title": f"signal {s}", "source": f"https://example.test/{s}"}
            for i, s in enumerate(sigs, start=1)
        ]}},
    }


def _tension_payload():
    """A live-shaped world assembly carrying BOTH tension populations.

    Built by ``build_assembly`` itself — the real ``_tensions`` walk over real
    BLUF spans, not hand-written tension dicts — so the fixture exercises the
    detector's own output shape. Two carried blocks that point opposite ways
    give the ``carried_pair`` arm (``b_carried`` True, ``b`` an ordinal); a
    third row below the cut that conflicts with the lead gives the
    ``carried_vs_dropped`` arm (``b_carried`` False, ``b_ref`` a dropped head
    with its own ``span``).
    """
    rows = [
        _tension_row(
            uuid4(), target_id="country_g20_ru",
            bluf="Russia's energy-security pressure is pushing escalation risk "
                 "upward with a sharp rise in refinery outages",
        ),
        _tension_row(
            uuid4(), target_id="country_watch_kp",
            bluf="North Korea's energy-security pressure is easing with a "
                 "marked drop in supply disruptions", sigs=("s2",),
        ),
        _tension_row(
            uuid4(), target_id="country_g20_cn",
            bluf="China's energy-security pressure remains high with no sign "
                 "of easing", sigs=("s3",),
        ),
    ]
    ap.attach_cited_salience(rows, {"s1": 0.95, "s2": 0.70, "s3": 0.50})
    ordered = sorted(rows, key=ap.order_key)
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[:2],
    )


def test_the_fixture_carries_both_tension_populations() -> None:
    """The fixture is only worth anything if the DETECTOR built it."""
    payload = _tension_payload()
    tensions = payload["tensions"]
    assert len(tensions) >= 2, tensions
    kinds = {t["kind"] for t in tensions}
    assert kinds == {"carried_pair", "carried_vs_dropped"}, kinds
    assert payload["tension_checked"]["pairs_found_carried"] >= 1
    assert payload["tension_checked"]["pairs_found_uncarried"] >= 1


def test_the_declared_tension_puts_the_statement_in_the_evidence_map() -> None:
    """THE DEFECT, at the byte the judge reads.

    Before this train the map carried "1 declared out of 228 pairs examined"
    and nothing about WHICH pair or what either side said, so a sentence
    relaying the record's own tension had no truthmaker and could only grade
    unsupported. Three did, live.
    """
    payload = _tension_payload()
    arithmetic = apr.record_arithmetic(payload)
    for t in payload["tensions"][:apr.TENSION_RENDER_CAP]:
        statement = " ".join(str(t["statement"]).split())
        assert statement in arithmetic, (
            "the statement names both sides and the shared dimension; it is "
            f"the truthmaker a tension sentence rests on: {statement!r}"
        )


def _sides(payload):
    """``(head_id, span)`` for both sides of every rendered tension."""
    blocks = {int(b["ordinal"]): b for b in payload["blocks"]}
    out = []
    for t in payload["tensions"][:apr.TENSION_RENDER_CAP]:
        a_block = blocks[int(t["a"]["ordinal"])]
        out.append((
            str(a_block["finding_id"]),
            " ".join(str(a_block["spans"][0]["text"]).split()),
        ))
        if t.get("b_ref"):
            out.append((
                str(t["b_ref"]["finding_id"]),
                " ".join(str(t["b_ref"]["span"]).split()),
            ))
        else:
            b_block = blocks[int(t["b"]["ordinal"])]
            out.append((
                str(b_block["finding_id"]),
                " ".join(str(b_block["spans"][0]["text"]).split()),
            ))
    return out


def test_the_declared_tension_names_both_heads_and_quotes_what_is_safe(
) -> None:
    """BOTH REFS, with their head ids — and each side's own words WHEN quoting
    them does not manufacture a desk negative.

    The head id always rides: it is the drill target, and it is what makes
    "which pair did the detector choose" answerable from the map alone. The
    words ride unless the guard in ``_tension_side_line`` stops them, in which
    case the block says so in terms rather than dropping the side silently.
    """
    payload = _tension_payload()
    arithmetic = apr.record_arithmetic(payload)
    sides = _sides(payload)
    assert sides
    for head_id, span in sides:
        assert head_id in arithmetic, (
            "a side the map cannot name is a pairing the judge cannot check"
        )
        quoted = span[:apr.TENSION_SPAN_CHARS] in arithmetic
        assert quoted or apr.TENSION_SPAN_WITHHELD in arithmetic, (
            f"neither the words nor the withheld notice rode for {head_id}"
        )
    # at least one side's words DO ride — the guard must not be swallowing the
    # whole point of the train
    assert any(
        span[:apr.TENSION_SPAN_CHARS] in arithmetic for _h, span in sides
    )


def test_a_quoted_side_never_manufactures_a_desk_negative() -> None:
    """THE FALSE-POSITIVE GUARD, and the measurement that forced it.

    ``composition_integrity.fold`` DOES run on this channel — it routes on the
    ``[[ref:N]]`` sub-claim convention, which the Assessment uses — and it reads
    each citation's whole ``evidence_text`` as the cited desk head's own text.
    Rendered raw, the uncarried side's ``b_ref.span`` is an absence sentence on
    BOTH live tension-bearing spines ("no new supply disruptions …" on 09-07
    00:00Z, "no new developments easing …" on 09-08 12:00Z), and it moved the
    absence set on 8 of 8 ordinals — handing ``asserts_desk_negative`` a denial
    made by a read this record did not carry, against every claim in the body.

    Cross-tier tensions are declared precisely when one side reports a change
    and the other reports none, so this is the COMMON shape here.
    """
    payload = _tension_payload()
    arithmetic = apr.record_arithmetic(payload)
    assert ci.desk_absence_sentences(apr.declared_tensions(payload)) == []
    for _ordinal, span in au.spine_span_text(payload).items():
        combined = ac.assessment_evidence_text(span, arithmetic)
        assert ci.desk_absence_sentences(combined) == ci.desk_absence_sentences(span), (
            "the record's arithmetic must never add a sentence the cited desk "
            "will be read as having denied"
        )
    # the guard actually FIRED on this fixture — otherwise the test is vacuous
    assert apr.TENSION_SPAN_WITHHELD in arithmetic


def test_the_withheld_notice_is_not_itself_an_absence_sentence() -> None:
    """The stand-in has to survive the same walker it exists to satisfy — the
    aperture block's "not computed at this grain" constraint, again."""
    assert ci.desk_absence_sentences(apr.TENSION_SPAN_WITHHELD) == []


def test_the_declared_tension_states_carriage_and_the_why_class() -> None:
    """W-1's asymmetry, on the surface the JUDGE reads.

    A cross-tier pair is evidence for the drop ledger, not a conflict this
    record published, and the voice got that exact distinction wrong on
    2026-09-06 12:15Z. The map now says which side was carried and why the
    other was not.
    """
    payload = _tension_payload()
    arithmetic = apr.record_arithmetic(payload)
    assert "a read this record did NOT carry" in arithmetic
    whys = {
        str(t.get("b_why") or "") for t in payload["tensions"] if t.get("b_why")
    }
    assert whys, "the fixture must carry a cross-tier arm with a why-class"
    for why in whys:
        assert why in arithmetic
    carried = [t for t in payload["tensions"] if t.get("b_carried")]
    assert carried
    assert "carried block [[ref:" in arithmetic


def test_the_tension_section_is_purely_additive() -> None:
    """BYTE-IDENTICAL EVERYWHERE ELSE, and this is the assertion that says so.

    Removing the rendered section from a tension-bearing record's arithmetic
    must give back exactly the block the same record renders with no tensions —
    so this train adds bytes and moves none.
    """
    payload = _tension_payload()
    with_tensions = apr.record_arithmetic(payload)
    without = apr.record_arithmetic({**payload, "tensions": []})
    section = apr.declared_tensions(payload)
    assert section and section in with_tensions
    assert with_tensions.replace("\n" + section, "") == without


def test_a_record_with_no_tensions_is_byte_identical_to_the_shipped_block(
) -> None:
    """THE INERTNESS HALF. Two of the four post-G3 live spines declared no
    tension at all (09-07 12:00Z, 09-08 00:00Z) and their maps must not move by
    one byte — they are this train's byte-identity control in the replay."""
    payload = _payload()
    assert not payload.get("tensions")
    assert apr.declared_tensions(payload) == ""
    block = apr.record_arithmetic(payload)
    assert "THE DECLARED TENSIONS" not in block
    assert "carried block [[ref:" not in block
    assert "the uncarried other side" not in block
    # and no stray blank line where the section would have gone
    assert "\n\n" not in block


def test_the_tension_render_cap_counts_the_remainder_it_does_not_hide_it(
) -> None:
    """``_tensions`` is UNBOUNDED — it walks all 228 pairs — so the block needs a
    ceiling. The remainder is COUNTED (the ``_named`` precedent), because a
    truncated list that does not say it is truncated is one a voice completes.
    """
    payload = _tension_payload()
    many = list(payload["tensions"])
    while len(many) <= apr.TENSION_RENDER_CAP:
        many.append(dict(many[0]))
    payload = {**payload, "tensions": many}
    block = apr.declared_tensions(payload)
    extra = len(many) - apr.TENSION_RENDER_CAP
    assert f"and {extra} further declared" in block
    assert f"tension 1 of {len(many)}" in block
    assert f"tension {apr.TENSION_RENDER_CAP + 1} of" not in block


def test_the_rendered_tensions_stay_inside_the_evidence_map_budget() -> None:
    """THE CEILING IS A TEST, not a slice — the
    ``APERTURE_ROSTER_BUDGET_CHARS`` precedent. The block rides eight times, so
    a record whose tensions outgrow the budget must fail loudly and get this
    decision made again with the real number in hand.
    """
    payload = _tension_payload()
    many = list(payload["tensions"]) * 8
    rendered = apr.declared_tensions({**payload, "tensions": many})
    assert len(rendered) <= apr.TENSION_BUDGET_CHARS, (
        "the tension block outgrew the budget this decision was made against; "
        f"make it again with the new number ({len(rendered)}) in hand"
    )


def test_a_tension_bearing_arithmetic_block_is_not_a_desk_sentence() -> None:
    """THE FALSE-POSITIVE DIRECTION, on a branch that now quotes desk prose.

    This section puts two desks' own BLUF sentences into the arithmetic block,
    and ``composition_integrity`` walks a cited head's absence and verdict
    sentences — so the block could in principle manufacture a desk negative the
    record never carried. That is the class this whole train exists to remove,
    so it gets asserted rather than assumed.
    """
    payload = _tension_payload()
    arithmetic = apr.record_arithmetic(payload)
    assert ci.desk_absence_sentences(arithmetic) == []
    for _ordinal, span in au.spine_span_text(payload).items():
        combined = ac.assessment_evidence_text(span, arithmetic)
        assert ci.desk_absence_sentences(combined) == ci.desk_absence_sentences(span)
        assert ci.desk_verdict_text(combined) == ci.desk_verdict_text(span)


def test_the_voice_and_the_judge_are_shown_the_same_tension() -> None:
    """P1's CONTRACT, for the tension half — the invariant the roster train
    asserted for the roster. A tension the PROMPT names is a tension the
    EVIDENCE MAP names, because what the voice is told has to be what the judge
    can check."""
    payload = _tension_payload()
    prompt = apr.build_assessment_prompt(payload)
    arithmetic = apr.record_arithmetic(payload)
    statements = [
        " ".join(str(t["statement"]).split())
        for t in payload["tensions"][:apr.TENSION_RENDER_CAP]
    ]
    in_prompt = [s for s in statements if s in " ".join(prompt.split())]
    assert in_prompt, "the fixture must put the tension in front of the voice"
    withheld = [s for s in in_prompt if s not in arithmetic]
    assert withheld == [], (
        "a tension the voice is shown and the judge is not is the sentence "
        f"this channel invites and the grader must fail: {withheld}"
    )
    entries = ac.build_assessment_citations(
        payload, spine_id=str(uuid4()), spine_analyst="world_assessor",
        markers=[{"ordinal": 1}],
    )
    assert entries
    for entry in entries:
        assert all(s in entry["evidence_text"] for s in in_prompt)


def test_the_assessment_system_prompt_is_byte_identical_and_frozen() -> None:
    """FROZEN FOR R4. This train is producer-side and ``payload``-only: it moves
    the EVIDENCE MAP and not the voice's instructions, so the system prompt and
    its version must not move by one byte. The hash is the assertion."""
    digest = hashlib.sha256(apr.ASSESSMENT_SYSTEM.encode()).hexdigest()
    assert digest == (
        "df76febb6be1d9e42f0481455c3bf9d20ccb785d679bcd4cd82b866ff1408d60"
    ), "ASSESSMENT_SYSTEM is frozen; a change here needs a stamp"
    assert len(apr.ASSESSMENT_SYSTEM.encode()) == 14912
    assert apr.PROMPT_VERSION == "assessment_prompt.v3"

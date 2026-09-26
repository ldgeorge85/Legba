# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""STEP E — THE WORLD VOICE READS THE COUNTRY VOICES.

THE DEFECT, measured 2026-09-20. The COUNTRY voice (32 countries x 2/day)
averages judge 0.86 over 126 LLM-judged reads. The WORLD voice (2/day) swings
0.50-0.95, and its 12:15Z read spent its argument narrating the record's own
arithmetic — "its verification score (0.87) and high severity", "smaller cited
mass (2.99)", "the arithmetic deems" — instead of the world. The v2 clause (THE
INSTRUMENT IS NOT THE WORLD) and the v3 clause (NAME BOTH BLOCKS) already forbid
exactly those sentences, so a third clause was the obvious move and the wrong
one: the world voice's whole input was EIGHT DESK SENTENCES, one carried up from
each country assembly. A voice with nothing to argue FROM argues about its page.

THE REPAIR is P3-A's, one tier up, with one word changed. P3-A gave the country
voice each desk's read in FULL because a lead sentence per desk cannot carry a
cross-DIMENSION argument; a lead sentence per country cannot carry a
cross-COUNTRY one. So a carried world block now also carries that country's own
``country_assessment`` — the 0.86 prose — as a ``context_body`` span.

What this file proves, in the order the tier runs:

  * THE READ — the country assessment written FROM this candidate row
    (``derived_from[0]``), or the target's newest inside 24h with the FALLBACK
    RECORDED; a THEMATIC candidate gets nothing, which is the live shape
    (record ``ae07ce8c``, 2026-09-20: 8 blocks, 7 country, 1 escalation).
  * THE SPAN — byte-identical through the same gate, its origin the ASSESSMENT
    head while the block's own origin stays the DESK head, and INVISIBLE to the
    record's body (a golden test).
  * THE VOICE — ``assessment_prompt.v4`` and its own system constant when the
    record carries context; the v3 prompt and version BYTE-IDENTICAL when it
    does not.
  * THE EVIDENCE MAP — the ordinal's evidence is the desk sentence AND the
    country read, and the citation carries ``context_origin_id`` so the drill
    can open it.
  * THE ARMS — unmoved, and passing because the context span is skipped by ROLE
    rather than because it would pass anyway.
"""

from __future__ import annotations

import copy
from uuid import uuid4

import pytest

from legba.data.analysts import assembly_carry as acar
from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assembly_spans as asp
from legba.data.analysts import assessment_channel as ac
from legba.data.analysts import assessment_prompts as apr
from legba.data.analysts import assessment_unsupported as au
from legba.data.analysts import composition_citations as cc
from legba.data.analysts import composition_slice as cs
from legba.data.analysts import meta_findings_synthesizer as synth
from legba.data.provenance import assembly_arms as AA


@pytest.fixture(autouse=True)
def _regime_on(monkeypatch):
    """THE ONE REGIME SWITCH — the channel refuses to write unless the record it
    was handed is itself an assembly."""
    monkeypatch.setenv(ap.ASSEMBLY_ENV, "1")


UA = "country_watch_ua"
RU = "country_g20_ru"
SA = "country_g20_sa"

# Cut to the live shape: em dash, curly apostrophe, the desk's own `[N]` wire
# markers, a leading as-of stamp.
ESCALATION_BODY = (
    "*As of 2026-09-20; slice covers the trailing 72h to that date.*\n"
    "\n"
    "**BLUF:** Long-range strikes on three oblast capitals resumed on "
    "18 September after a nine-day pause [1] [2].\n"
    "\n"
    "## What changed\n"
    "- On 18 September the strike tempo returned to its August mean [1].\n"
)

ENERGY_BODY = (
    "*As of 2026-09-20.*\n"
    "\n"
    "**BLUF:** Crude loadings at the two Black Sea terminals fell by a fifth "
    "against the trailing month [3].\n"
)

POSTURE_BODY = (
    "*As of 2026-09-20.*\n"
    "\n"
    "**BLUF:** Two additional brigades moved to the western military district "
    "in the week to 19 September [4].\n"
)

# The country ASSESSMENTS — the country voice's own prose, which is the text
# STEP E puts in front of the world voice. Hedged, cross-dimension, with the
# country's OWN [[ref:N]] markers pointing at the country's own record.
UA_ASSESSMENT = (
    "**BLUF:** Two dimensions carry Ukraine this window and neither outweighs "
    "the other: a resumed strike tempo [[ref:1]] and a fifth off Black Sea "
    "crude loadings [[ref:2]].\n"
    "\n"
    "## The reading\n"
    "The return to the August strike mean [[ref:1]] and the loading shortfall "
    "[[ref:2]] are consistent with a single interdiction campaign, though "
    "neither desk tests that link and either alone is insufficient on its own.\n"
)

RU_ASSESSMENT = (
    "**BLUF:** Force generation is the story in Russia this window "
    "[[ref:1]].\n"
    "\n"
    "## The reading\n"
    "Two brigades moving west [[ref:1]] sits beside, rather than explains, the "
    "export picture [[ref:2]].\n"
)

SA_ASSESSMENT = (
    "**BLUF:** Saudi Arabia is quiet on every dimension this desk carries "
    "[[ref:1]].\n"
)

MAGS = {"s1": 0.95, "s2": 0.70, "s3": 0.55, "s4": 0.40, "s5": 0.06}


# ---------------------------------------------------------------------------
# FIXTURES — REAL assemblies, built by D-2's own builder
# ---------------------------------------------------------------------------


def _desk_row(analyst_id, body, signal_ids, *, target_id, severity="high"):
    return {
        "id": str(uuid4()),
        "kind": "finding",
        "analyst_id": analyst_id,
        "target_id": target_id,
        "title": f"{analyst_id} head",
        "body": body,
        "severity": severity,
        "confidence": 0.7,
        "produced_at": "2026-09-20T10:01:00+00:00",
        "faithfulness_score": 0.90,
        "effective_confidence": 0.80,
        "claim_verdicts": [{"verdict": "supported"}],
        "data": {"data": {"citations": [
            {"marker": f"[{i}]", "signal_id": s, "source_id": f"source.{s}",
             "title": f"signal {s}", "source": f"https://example.test/{s}"}
            for i, s in enumerate(signal_ids, start=1)
        ]}},
    }


def _child_citations(desk_rows, payload):
    """The child's ``data.data.citations`` in the shape the producer writes —
    the bridge ``carried_origin_record`` lifts."""
    by_id = {r["id"]: r for r in desk_rows}
    out = []
    for block in payload["blocks"]:
        src = by_id[block["finding_id"]]
        out.append({
            "marker": f"[[ref:{block['ordinal']}]]",
            "ordinal": block["ordinal"],
            "ref_id": block["finding_id"],
            "ref_kind": "finding",
            "source": src["analyst_id"],
            "target_id": src["target_id"],
            "title": src["title"],
            "produced_at": src["produced_at"],
            "evidence_text": src["body"][: cc.MAX_EVIDENCE_TEXT_CHARS],
            "effective_confidence": 0.80,
            "derived_from": [],
        })
    return out


def _country(target_id, desks):
    """One COUNTRY assembly + the composition row the world slice reads back."""
    rows = [_desk_row(a, b, s, target_id=target_id) for a, b, s in desks]
    ap.attach_cited_salience(rows, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    payload = ap.build_assembly(
        tier=ap.TIER_COUNTRY,
        as_of="2026-09-20T11:30:00+00:00",
        candidates=ordered,
        carried=ordered,
    )
    row = {
        "id": str(uuid4()),
        "kind": "finding",
        "analyst_id": cs.COUNTRY_COMPOSITION_ANALYST_ID,
        "target_id": target_id,
        "derived_from": [r["id"] for r in rows],
        "_child_signal_ids": [
            str(c["signal_id"])
            for r in rows for c in r["data"]["data"]["citations"]
        ],
        "title": ar.assembly_title(payload),
        "body": ar.render_assembly_body(payload),
        "severity": ap.assembly_severity(payload["blocks"]),
        "confidence": 0.62,
        "produced_at": "2026-09-20T11:50:00+00:00",
        "faithfulness_score": 0.95,
        "effective_confidence": 0.90,
        "claim_verdicts": [{"verdict": "supported"}],
        "data": {"data": {
            "assembly": payload,
            "citations": _child_citations(rows, payload),
        }},
    }
    return rows, payload, row


def _thematic():
    """The world's one legal cross-region object — and it gets NO context."""
    rows = [_desk_row("escalation", ESCALATION_BODY, ("s1", "s2"),
                      target_id=UA)]
    ap.attach_cited_salience(rows, MAGS)
    payload = ap.build_assembly(
        tier=ap.TIER_THEMATIC,
        as_of="2026-09-20T08:30:00+00:00",
        candidates=rows,
        carried=rows,
    )
    row = {
        "id": str(uuid4()),
        "kind": "finding",
        "analyst_id": "escalation_composition",
        "target_id": None,
        "derived_from": [r["id"] for r in rows],
        "_child_signal_ids": ["s1", "s2"],
        "title": ar.assembly_title(payload),
        "body": ar.render_assembly_body(payload),
        "severity": "high",
        "confidence": 0.6,
        "produced_at": "2026-09-20T08:30:00+00:00",
        "data": {"data": {
            "assembly": payload,
            "citations": _child_citations(rows, payload),
        }},
    }
    return rows, payload, row


def _three_countries():
    """The 3-country world spine this file runs the channel over."""
    _, _, ua = _country(UA, [
        ("escalation", ESCALATION_BODY, ("s1", "s2")),
        ("energy_security", ENERGY_BODY, ("s3",)),
    ])
    _, _, ru = _country(RU, [
        ("military_posture", POSTURE_BODY, ("s4",)),
        ("energy_security", ENERGY_BODY, ("s5",)),
    ])
    _, _, sa = _country(SA, [
        ("energy_security", ENERGY_BODY, ("s5",)),
    ])
    return [ua, ru, sa]


def _assessment_body(row):
    return {UA: UA_ASSESSMENT, RU: RU_ASSESSMENT, SA: SA_ASSESSMENT}[
        row["target_id"]
    ]


def _context_heads(candidates, *, match=cs.CONTEXT_MATCH_THIS_RECORD):
    """What ``resolve_country_assessment_context`` returns, by hand — keyed on
    the CANDIDATE ROW ID, which is what a carried block publishes as
    ``via_head_id``."""
    return {
        r["id"]: {
            "head_id": str(uuid4()),
            "body": _assessment_body(r),
            "target_id": r["target_id"],
            "produced_at": "2026-09-20T12:05:00+00:00",
            "match": match,
        }
        for r in candidates
        if r["analyst_id"] == cs.COUNTRY_COMPOSITION_ANALYST_ID
    }


def _world(candidates, *, context_heads=None):
    pooled = {
        r["id"]: r["_child_signal_ids"]
        for r in candidates if r.get("_child_signal_ids")
    }
    ap.attach_cited_salience(candidates, MAGS, pooled)
    ordered = sorted(candidates, key=ap.order_key)
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-20T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[: ap.BLOCK_CAP],
        target_names={UA: "Ukraine", RU: "Russia", SA: "Saudi Arabia"},
        context_heads=context_heads,
    )


def _world_citations(candidates, payload):
    """The WORLD row's citations — they name the CANDIDATE, as they do live."""
    by_id = {r["id"]: r for r in candidates}
    out = []
    for block in payload["blocks"]:
        src = by_id[block["via_head_id"]]
        out.append({
            "marker": f"[[ref:{block['ordinal']}]]",
            "ordinal": block["ordinal"],
            "ref_id": src["id"],
            "ref_kind": "finding",
            "source": src["analyst_id"],
            "target_id": src["target_id"],
            "title": src["title"],
            "produced_at": src["produced_at"],
            "evidence_text": src["body"][: cc.MAX_EVIDENCE_TEXT_CHARS],
            "effective_confidence": 0.90,
            "derived_from": [],
        })
    return out


# ---------------------------------------------------------------------------
# THE READ — composition_slice.resolve_country_assessment_context
# ---------------------------------------------------------------------------


class _AssessmentTableConn:
    """A fake ``analyst_outputs`` the two statements are run against, rather
    than asserted about. Implements exactly the predicates the SQL declares."""

    def __init__(self, table) -> None:
        self.table = list(table)
        self.statements: list[str] = []

    async def fetch(self, sql, *args):  # noqa: ANN001
        self.statements.append(sql)
        analyst = args[0]
        rows = [
            r for r in self.table
            if r["kind"] == "finding"
            and r["analyst_id"] == analyst
            and r.get("superseded_by") is None
            and r["body"] != ""
        ]
        if "a.derived_from[1] = ANY" in sql:
            keys = set(args[1])
            rows = [r for r in rows if r["derived_from"][:1]
                    and r["derived_from"][0] in keys]
            fold = "spine"
        else:
            keys = set(args[1])
            rows = [r for r in rows if r["target_id"] in keys]
            # The window bound, honoured rather than ignored.
            rows = [r for r in rows if r["_age_hours"] <= args[2]]
            fold = "target"
        rows.sort(key=lambda r: (r["produced_at"], r["id"]), reverse=True)
        seen, out = set(), []
        for r in rows:
            key = r["derived_from"][0] if fold == "spine" else r["target_id"]
            if key in seen:
                continue
            seen.add(key)
            out.append({
                "spine_id": r["derived_from"][0] if r["derived_from"] else None,
                "id": r["id"],
                "target_id": r["target_id"],
                "body": r["body"],
                "produced_at": r["produced_at"],
            })
        return out


def _assessment_row(spine_id, target_id, body, produced_at, *, age_hours=1,
                    **over):
    return {
        "_age_hours": age_hours,
        "id": str(uuid4()),
        "kind": "finding",
        "analyst_id": cs.COUNTRY_ASSESSMENT_ANALYST_ID,
        "target_id": target_id,
        "body": body,
        "derived_from": [spine_id] if spine_id else [],
        "produced_at": produced_at,
        "superseded_by": None,
        **over,
    }


@pytest.mark.asyncio
async def test_the_read_prefers_the_assessment_written_from_this_record() -> None:
    candidates = _three_countries()
    want = {
        r["id"]: _assessment_row(
            r["id"], r["target_id"], _assessment_body(r), "12:05",
        )
        for r in candidates
    }
    # Decoys: a NEWER assessment of the same target from a different record, a
    # critique row under the same analyst id (4 of the 5 newest live rows on
    # 2026-09-20 were critiques), and a superseded head.
    table = list(want.values()) + [
        _assessment_row(str(uuid4()), UA, "a neighbouring record", "12:30"),
        _assessment_row(candidates[0]["id"], UA, "faithfulness verify", "12:40",
                        kind="critique"),
        _assessment_row(candidates[1]["id"], RU, "superseded", "12:50",
                        superseded_by=str(uuid4())),
    ]
    conn = _AssessmentTableConn(table)

    got = await cs.resolve_country_assessment_context(conn, candidates)

    assert set(got) == {r["id"] for r in candidates}
    for r in candidates:
        assert got[r["id"]]["head_id"] == want[r["id"]]["id"], (
            "the assessment whose derived_from[0] IS this candidate — the "
            "channel's own contract read back, not a freshness guess"
        )
        assert got[r["id"]]["body"] == _assessment_body(r)
        assert got[r["id"]]["match"] == cs.CONTEXT_MATCH_THIS_RECORD
    assert len(conn.statements) == 1, "no fallback read when nothing is missing"


@pytest.mark.asyncio
async def test_the_fallback_is_the_targets_newest_and_it_is_RECORDED() -> None:
    """The live shape on 2026-09-20: a country composed again after its voice
    ran, so no assessment names the new record. The country's CURRENT read is
    still the best account of it there is — carried, and LABELLED."""
    candidates = _three_countries()
    ua = candidates[0]
    newer = _assessment_row(str(uuid4()), UA, UA_ASSESSMENT, "12:30")
    older = _assessment_row(str(uuid4()), UA, "yesterday's Ukraine read", "12:10")
    table = [newer, older] + [
        _assessment_row(r["id"], r["target_id"], _assessment_body(r), "12:05")
        for r in candidates[1:]
    ]
    conn = _AssessmentTableConn(table)

    got = await cs.resolve_country_assessment_context(conn, candidates)

    assert got[ua["id"]]["head_id"] == newer["id"]
    assert got[ua["id"]]["body"] == UA_ASSESSMENT
    assert got[ua["id"]]["match"] == cs.CONTEXT_MATCH_TARGET_RECENT, (
        "a fallback silently presented as an exact match is a currency claim "
        "the record cannot support"
    )
    assert got[candidates[1]["id"]]["match"] == cs.CONTEXT_MATCH_THIS_RECORD
    assert len(conn.statements) == 2, "the fallback read fires, once"


@pytest.mark.asyncio
async def test_a_country_with_no_voice_at_all_gets_nothing_not_a_neighbours() -> None:
    candidates = _three_countries()
    conn = _AssessmentTableConn([
        _assessment_row(candidates[0]["id"], UA, UA_ASSESSMENT, "12:05"),
    ])
    got = await cs.resolve_country_assessment_context(conn, candidates)
    assert set(got) == {candidates[0]["id"]}


@pytest.mark.asyncio
async def test_the_fallback_read_is_bounded_by_the_window() -> None:
    candidates = _three_countries()
    conn = _AssessmentTableConn([])
    await cs.resolve_country_assessment_context(conn, candidates, window_hours=24)
    assert conn.statements[-1] is cs._COUNTRY_ASSESSMENT_BY_TARGET_SQL
    assert "make_interval(hours => $3)" in conn.statements[-1]
    assert cs.COUNTRY_ASSESSMENT_CONTEXT_WINDOW_HOURS == (
        ac.DEFAULT_SPINE_WINDOW_HOURS
    ), (
        "an assessment older than the channel's own spine window is one the "
        "channel would refuse to write today"
    )


@pytest.mark.asyncio
async def test_no_candidates_issues_no_query() -> None:
    conn = _AssessmentTableConn([])
    assert await cs.resolve_country_assessment_context(conn, []) == {}
    assert conn.statements == []


def test_the_two_statements_differ_by_their_key_and_the_window() -> None:
    """The ``_SPINE_SQL`` / ``_SPINE_SQL_TARGET`` discipline: the diff is pinned
    rather than argued, so a reader can see what the fallback loosens."""
    exact = [ln.strip() for ln in
             cs._COUNTRY_ASSESSMENT_BY_SPINE_SQL.strip().splitlines()]
    recent = [ln.strip() for ln in
              cs._COUNTRY_ASSESSMENT_BY_TARGET_SQL.strip().splitlines()]
    for shared in (
        "WHERE a.kind = 'finding'",
        "AND a.analyst_id = $1",
        "AND a.superseded_by IS NULL",
        "AND a.body <> ''",
    ):
        assert shared in exact and shared in recent
    assert "AND a.derived_from[1] = ANY($2::uuid[])" in exact
    assert "AND a.target_id = ANY($2::text[])" in recent
    assert "AND a.produced_at >= NOW() - make_interval(hours => $3)" in recent
    assert not any("produced_at >=" in ln for ln in exact), (
        "an EXACT match needs no freshness bound: it names this very record"
    )


def test_the_analyst_id_is_the_channels_own_spelling() -> None:
    assert cs.COUNTRY_ASSESSMENT_ANALYST_ID == ac.COUNTRY_ASSESSMENT_ANALYST_ID
    assert cs.COUNTRY_ASSESSMENT_ANALYST_ID in ac.ASSESSMENT_ANALYST_IDS


def test_the_stamp_round_trips_and_an_unstamped_slice_is_an_honest_empty() -> None:
    rows = [{"id": "a"}, {"id": "b"}]
    cs.stamp_country_assessment_context(rows, {"x": {"head_id": "h"}})
    assert all(cs.COUNTRY_ASSESSMENT_CONTEXT_ROW_KEY in r for r in rows)
    assert cs.country_assessment_context_of(rows) == {"x": {"head_id": "h"}}
    assert cs.country_assessment_context_of([{"id": "a"}]) == {}


# ---------------------------------------------------------------------------
# THE SPAN
# ---------------------------------------------------------------------------


def test_every_carried_world_block_carries_its_country_read_in_full() -> None:
    candidates = _three_countries()
    heads = _context_heads(candidates)
    payload = _world(candidates, context_heads=heads)

    assert payload["tier"] == ap.TIER_WORLD
    assert len(payload["blocks"]) == 3
    for block in payload["blocks"]:
        ctx = asp.context_spans(block)
        assert len(ctx) == 1, "exactly one context span per carried block"
        span = ctx[0]
        want = heads[block["via_head_id"]]
        assert span["text"] == want["body"], "byte-identical, whole body"
        assert span["origin"]["start"] == 0
        assert span["origin"]["end"] == len(want["body"].encode("utf-8"))
        assert span["origin"]["body_sha256"] == asp.body_sha256(want["body"])
        assert span["origin"]["head_id"] == want["head_id"]
        # THROUGH THE SAME GATE every quoted span passes — byte-identity is
        # proved here, not asserted.
        assert asp.verify_span(
            want["body"], span["origin"]["start"], span["origin"]["end"],
            span["text"],
        ) is None


def test_the_blocks_own_origin_is_STILL_the_desk_head() -> None:
    """The context's origin is a DIFFERENT row from the block's, and the block
    names both rather than letting a reader infer one from the other."""
    candidates = _three_countries()
    heads = _context_heads(candidates)
    payload = _world(candidates, context_heads=heads)

    for block in payload["blocks"]:
        lead = asp.quoted_spans(block)[0]
        want = heads[block["via_head_id"]]
        assert lead["origin"]["head_id"] == block["finding_id"], (
            "quote fidelity here is the SAME depth-1 test against the SAME "
            "desk body it has always been"
        )
        assert lead["origin"]["head_id"] != want["head_id"]
        assert block[ap.CONTEXT_ORIGIN_KEY] == want["head_id"]
        assert block[ap.CONTEXT_MATCH_KEY] == cs.CONTEXT_MATCH_THIS_RECORD
        assert (
            asp.context_spans(block)[0]["origin"]["head_id"]
            == block[ap.CONTEXT_ORIGIN_KEY]
        ), "the published id IS the id the byte-identity gate checked"


def test_the_lead_span_stays_at_index_zero() -> None:
    """``_tensions`` / ``_lead_fragment`` / the BLUF all read ``spans[0]``."""
    candidates = _three_countries()
    payload = _world(candidates, context_heads=_context_heads(candidates))
    for block in payload["blocks"]:
        assert block["spans"][0]["role"] != asp.SPAN_ROLE_CONTEXT_BODY
        assert block["spans"][-1]["role"] == asp.SPAN_ROLE_CONTEXT_BODY


def test_a_thematic_candidate_gets_no_context_span() -> None:
    """Live 2026-09-20 on world record ``ae07ce8c``: 8 blocks carried, 7 from
    country assemblies, ordinal 1 from ``escalation_composition``. The filter is
    the CANDIDATE'S ANALYST, never "did a match turn up" — the fallback would
    otherwise have attached Ukraine's country voice to a thematic block."""
    _, _, thematic = _thematic()
    candidates = _three_countries() + [thematic]
    heads = _context_heads(candidates)
    assert thematic["id"] not in heads

    payload = _world(candidates, context_heads=heads)
    by_via = {b["via_head_id"]: b for b in payload["blocks"]}
    assert thematic["id"] in by_via, "the thematic block is still carried"
    tblock = by_via[thematic["id"]]
    assert asp.context_spans(tblock) == []
    assert ap.CONTEXT_ORIGIN_KEY not in tblock
    assert ap.CONTEXT_MATCH_KEY not in tblock
    assert sum(1 for b in payload["blocks"] if asp.context_spans(b)) == 3


def test_no_context_heads_builds_the_payload_that_shipped() -> None:
    """The inertness property, as byte-identity: STEP E off is D-5's world."""
    candidates = _three_countries()
    before = _world(copy.deepcopy(candidates))
    after = _world(copy.deepcopy(candidates), context_heads={})
    assert before == after
    for block in before["blocks"]:
        assert asp.context_spans(block) == []
        assert asp.quoted_spans(block) == block["spans"]


def test_the_country_tier_is_untouched_by_the_world_parameter() -> None:
    """``context_heads`` is a WORLD-tier join. A country assembly handed one —
    by a caller that does not know the difference — ignores it, because its
    context is its own desk body and comes from the row it is cutting."""
    rows = [_desk_row("escalation", ESCALATION_BODY, ("s1",), target_id=UA)]
    ap.attach_cited_salience(rows, MAGS)
    kw = dict(
        tier=ap.TIER_COUNTRY, as_of="2026-09-20T11:30:00+00:00",
        candidates=rows, carried=rows,
    )
    plain = ap.build_assembly(**kw)
    handed = ap.build_assembly(**kw, context_heads={rows[0]["id"]: {
        "head_id": str(uuid4()), "body": UA_ASSESSMENT, "match": "x",
    }})
    assert plain == handed
    assert asp.context_spans(plain["blocks"][0])[0]["origin"]["head_id"] == (
        rows[0]["id"]
    ), "the country tier's context is its OWN desk head, as P3-A shipped"


def test_an_empty_assessment_body_fails_LOUD_never_a_hole_in_the_read() -> None:
    candidates = _three_countries()
    heads = _context_heads(candidates)
    heads[candidates[0]["id"]]["body"] = "   "
    with pytest.raises(ap.AssemblyConstructionError) as exc:
        _world(candidates, context_heads=heads)
    assert "country assessment" in str(exc.value)


# ---------------------------------------------------------------------------
# THE RECORD BODY NEVER MOVES — the golden
# ---------------------------------------------------------------------------


def test_the_rendered_world_body_is_byte_identical_with_and_without_context() -> None:
    candidates = _three_countries()
    plain = _world(copy.deepcopy(candidates))
    withctx = _world(copy.deepcopy(candidates),
                     context_heads=_context_heads(candidates))

    assert ar.render_assembly_body(withctx) == ar.render_assembly_body(plain)
    assert ar.assembly_title(withctx) == ar.assembly_title(plain)
    assert ap.assembly_confidence(withctx) == ap.assembly_confidence(plain)
    assert ap.assembly_tags(withctx) == ap.assembly_tags(plain)

    body = ar.render_assembly_body(withctx)
    for head in _context_heads(candidates).values():
        assert head["body"].split("\n")[0] not in body
    assert "[[ref:1]]" in body, "the record's OWN ordinals are still rendered"


def test_the_context_body_is_invisible_to_every_quotation_consumer() -> None:
    candidates = _three_countries()
    payload = _world(candidates, context_heads=_context_heads(candidates))
    for block in payload["blocks"]:
        assert len(asp.quoted_spans(block)) == 1
        assert len(AA._spans(block)) == 1
        assert len(acar.quoted_spans(block)) == 1
        assert asp.is_context_span(block["spans"][-1]) is True


def test_a_country_voice_that_quotes_a_connective_cannot_take_the_run_down() -> None:
    """THE GUARD'S DENOMINATOR IS THE QUOTATIONS. A ``context_body`` span is
    never quoted and never rendered, and ``desk_reads_in_full`` puts it through
    ``quoted_text``, which DEFUSES the child markers the guard exists to stop.
    Charging a construction error for a sentence nobody publishes would take the
    whole world run down for a country voice quoting its own record."""
    candidates = _three_countries()
    heads = _context_heads(candidates)
    heads[candidates[0]["id"]]["body"] = (
        "**BLUF:** 3 reads lead this cycle, weighted and uncrowned: "
        "[[ref:5]], [[ref:1]], [[ref:4]] — and here is what I make of them.\n"
    )
    payload = _world(candidates, context_heads=heads)
    assert len(payload["blocks"]) == 3

    # ...and the guard still FIRES on a quotation, which is what it is for.
    bad = _world(copy.deepcopy(candidates))
    bad["blocks"][0]["spans"][0]["text"] = (
        "No single read leads this cycle; 8 reads carried below, in order."
    )
    assert acar.connective_leak(bad["blocks"][0]["spans"][0]["text"]) is not None


# ---------------------------------------------------------------------------
# THE VOICE — v4, and v3 byte-unchanged without context
# ---------------------------------------------------------------------------


def test_a_world_record_with_context_gets_the_v5_voice_and_version() -> None:
    """H8 (2026-09-24): the world voice with context is v5 — v4 plus the two
    measured clauses. v4 stays exported, byte-identical, for the before/after."""
    candidates = _three_countries()
    payload = _world(candidates, context_heads=_context_heads(candidates))

    assert apr.has_context_spans(payload) is True
    assert apr.prompt_version_for(payload) == "assessment_prompt.v5"
    assert apr.prompt_version_for(payload) == apr.PROMPT_VERSION_V5
    assert apr.system_prompt_for(payload) is apr.WORLD_ASSESSMENT_SYSTEM_V5
    assert apr.PROMPT_VERSION_V4 == "assessment_prompt.v4"


def test_v5_is_v4_plus_the_two_H8_clauses_and_nothing_else() -> None:
    """The 00:37Z read: two negative-synthesis sentences contradicted, one
    uncited roster sentence. v5 names both failures; v4 does not."""
    v4, v5 = apr.WORLD_ASSESSMENT_SYSTEM_V4, apr.WORLD_ASSESSMENT_SYSTEM_V5
    for clause in (
        "NEGATIVE SYNTHESIS IS NOT A FACT",
        "THE UNCITED SENTENCE IS MARKED, IN PUBLIC",
        "does not intersect",
        "roster sentence",
    ):
        assert clause in v5 and clause not in v4
    # Everything v4 says, v5 still says — the clauses were inserted, not edited in.
    head, _, tail = v4.partition("REGISTER. Specificity is the craft target")
    assert v5.startswith(head)
    assert v5.endswith(tail)
    assert "DECIDES ORDER, NOT MEANING" in v5 and "THE CROSS-COUNTRY RULE" in v5


def test_a_world_record_WITHOUT_context_is_byte_identical_to_v3() -> None:
    """The reversibility property, and it is the whole rollout plan: stop
    stamping context and the world voice is v3 again, with the version string on
    the row saying which side of the change it sits on."""
    candidates = _three_countries()
    plain = _world(candidates)

    assert apr.has_context_spans(plain) is False
    assert apr.prompt_version_for(plain) == apr.PROMPT_VERSION
    assert apr.prompt_version_for(plain) == "assessment_prompt.v3"
    assert apr.system_prompt_for(plain) is apr.ASSESSMENT_SYSTEM
    assert apr.desk_reads_in_full(plain) == ""
    assert "READS IN FULL" not in apr.build_assessment_prompt(plain)


def test_the_country_voice_and_the_v3_world_voice_are_BYTE_UNCHANGED() -> None:
    """This train moves the world voice. It moves nothing else, and a hash is
    the only honest way to say so."""
    import hashlib

    def _h(s):
        return hashlib.sha256(s.encode("utf-8")).hexdigest()

    assert apr.PROMPT_VERSION == "assessment_prompt.v3"
    assert apr.COUNTRY_PROMPT_VERSION == "country_assessment_prompt.v1"
    assert apr.CONTEXT_SECTION_HEADER == "THE DESK READS IN FULL"
    assert apr.COUNTRY_SECTION_HEADER == "THE COUNTRY READS IN FULL"
    # The four constants are four distinct voices; neither v4 nor v5 is a copy.
    assert len({
        _h(apr.ASSESSMENT_SYSTEM),
        _h(apr.COUNTRY_ASSESSMENT_SYSTEM),
        _h(apr.WORLD_ASSESSMENT_SYSTEM_V4),
        _h(apr.WORLD_ASSESSMENT_SYSTEM_V5),
    }) == 4


def test_the_v4_prompt_hands_over_the_country_reads_under_their_ordinals() -> None:
    candidates = _three_countries()
    heads = _context_heads(candidates)
    payload = _world(candidates, context_heads=heads)

    section = apr.desk_reads_in_full(payload)
    assert section.startswith(apr.COUNTRY_SECTION_HEADER + ".")
    assert apr.CONTEXT_SECTION_HEADER not in section, (
        "the world voice is not handed desk reads and is not told it is"
    )
    assert all(
        b["target_name"] == b["target_id"] for b in payload["blocks"]
    ), (
        "a carried block's target_name is the CHILD's, and live world blocks "
        "carry the slug on all eight (record ae07ce8c, 2026-09-20) — so the "
        "header has to name the country off the RECORD's own 7f map"
    )
    for block in payload["blocks"]:
        subject = {UA: "Ukraine", RU: "Russia", SA: "Saudi Arabia"}[
            block["target_id"]
        ]
        assert (
            f"--- [[ref:{block['ordinal']}]] {subject} "
            f"({block['target_id']}) — the country's own assessment ---"
        ) in section

    prompt = apr.build_assessment_prompt(payload)
    assert "-------------------- COUNTRY READS IN FULL ---" in prompt
    assert "DESK READS IN FULL" not in prompt
    # The record comes FIRST; the reads arrive once the ordinals are established.
    assert prompt.index("----- RECORD -----") < prompt.index(
        apr.COUNTRY_SECTION_HEADER
    )
    assert prompt.index(apr.record_arithmetic(payload)) < prompt.index(
        apr.COUNTRY_SECTION_HEADER
    )


def test_the_country_voices_own_markers_are_DEFUSED_in_the_world_prompt() -> None:
    """A country read's ``[[ref:2]]`` points at ITS record. Left live it would
    collide with this record's ordinal space and be resolved by the marker
    resolver as a reference to a world block."""
    candidates = _three_countries()
    payload = _world(candidates, context_heads=_context_heads(candidates))
    section = apr.desk_reads_in_full(payload)

    assert "[[ref:1]]" in UA_ASSESSMENT
    assert "consistent with a single interdiction campaign" in section
    assert "(child ref 1)" in section, "defused, not deleted"
    # The only live markers in the section are the ordinal HEADERS this record
    # minted — three blocks, three headers.
    assert section.count("[[ref:") == 3


def test_the_v4_prompt_says_the_arithmetic_decides_ORDER_not_MEANING() -> None:
    """The measured failure, named in the clause aimed at it: the 12:15Z read
    wrote "its verification score (0.87)", "smaller cited mass (2.99)" and "the
    arithmetic deems"."""
    sys = apr.WORLD_ASSESSMENT_SYSTEM_V4
    assert "DECIDES ORDER, NOT MEANING" in sys
    assert "THE INSTRUMENT IS NOT THE WORLD" in sys, "the v2 clause, kept"
    assert "NAME BOTH ORDINALS IN THAT SENTENCE" in sys, "the v3 shape, kept"
    for phrase in ("cited mass", "verification score", 'the arithmetic "deems"'):
        assert phrase in sys
    assert "THE CROSS-COUNTRY RULE" in sys
    assert "ordinal RANK" in sys
    # The sections a reader who learned the other two bands already knows.
    for section in ("'**BLUF:**'", "'## The reading'",
                    "'## What would change this'",
                    "'## What this reading misses'"):
        assert section in apr.WORLD_ASSESSMENT_BODY_SHAPE_V4


def test_the_prompt_builder_still_has_exactly_one_data_argument() -> None:
    """§2.1's input restriction is enforced by a signature a test can read. STEP
    E widens what the payload CARRIES and not what the builder may reach for."""
    import inspect

    sig = inspect.signature(apr.build_assessment_prompt)
    assert list(sig.parameters) == ["payload"]
    assert list(inspect.signature(apr.system_prompt_for).parameters) == ["payload"]
    assert list(inspect.signature(apr.prompt_version_for).parameters) == ["payload"]


# ---------------------------------------------------------------------------
# THE EVIDENCE MAP
# ---------------------------------------------------------------------------


def test_the_evidence_map_carries_the_country_read_not_just_the_sentence() -> None:
    """The judge grades a world claim against the COUNTRY VOICE'S FULL TEXT,
    because that is what the world voice was handed. No clause was added for it
    — ``spine_span_text`` joins a block's spans, and that join is the
    definition."""
    candidates = _three_countries()
    heads = _context_heads(candidates)
    payload = _world(candidates, context_heads=heads)

    spans = au.spine_span_text(payload)
    for block in payload["blocks"]:
        lead = asp.quoted_spans(block)[0]["text"]
        body = heads[block["via_head_id"]]["body"]
        assert spans[block["ordinal"]] == f"{lead}\n{body}"
        assert body in spans[block["ordinal"]]

    plain = _world(copy.deepcopy(candidates))
    for block in plain["blocks"]:
        assert au.spine_span_text(plain)[block["ordinal"]] == (
            block["spans"][0]["text"]
        ), "a payload with no context is byte-identical to D-6's map"


def test_the_citation_carries_context_origin_id_so_the_drill_can_open_it() -> None:
    candidates = _three_countries()
    heads = _context_heads(candidates)
    payload = _world(candidates, context_heads=heads)
    spine_id = str(uuid4())

    cits = ac.build_assessment_citations(
        payload, spine_id=spine_id, spine_analyst="world_assessor",
        markers=[{"ordinal": b["ordinal"]} for b in payload["blocks"]],
    )

    by_ordinal = {c["ordinal"]: c for c in cits}
    for block in payload["blocks"]:
        c = by_ordinal[block["ordinal"]]
        want = heads[block["via_head_id"]]
        assert c["ref_id"] == spine_id, "still the row this channel READ"
        assert c["context_origin_id"] == want["head_id"]
        assert c["context_match"] == cs.CONTEXT_MATCH_THIS_RECORD
        assert c["spine_block"]["finding_id"] == block["finding_id"], (
            "spine_block STILL points at the DESK head through the carried "
            "block's origin — two origins, stated as two"
        )
        assert c["context_origin_id"] != c["spine_block"]["finding_id"]
        assert want["body"] in c["evidence_text"]
        # The D-6 §3.5 contract, still recoverable.
        assert c["evidence_text"].split(ac.EVIDENCE_ARITHMETIC_RULE)[0] == (
            au.spine_span_text(payload)[block["ordinal"]]
        )


def test_a_citation_with_no_context_is_byte_identical_to_what_shipped() -> None:
    candidates = _three_countries()
    plain = _world(candidates)
    spine_id = str(uuid4())
    cits = ac.build_assessment_citations(
        plain, spine_id=spine_id, spine_analyst="world_assessor",
        markers=[{"ordinal": 1}],
    )
    assert "context_origin_id" not in cits[0], (
        "ABSENT, never null — a reader cannot mistake 'no country voice' for "
        "'a country voice with no id'"
    )
    assert "context_match" not in cits[0]


# ---------------------------------------------------------------------------
# THE ARMS
# ---------------------------------------------------------------------------


def test_the_arms_pass_on_a_world_record_that_carries_country_reads() -> None:
    """Not a fake pass: the context span is skipped BY ROLE, so arm 1's
    denominator stays exactly the QUOTATIONS the record makes. A span spanning a
    whole body passes by construction, and auditing it would move a 3-block
    read's fidelity from 2/3 to 5/6 for the same single real failure."""
    candidates = _three_countries()
    payload = _world(candidates, context_heads=_context_heads(candidates))
    citations = _world_citations(candidates, payload)

    res = AA.audit(payload, citations)
    assert [f.reason for f in res.findings] == []
    assert AA.quote_fidelity_score(res) == 1.0
    assert res.counters["assembly_carried_origins_bridged"] == 3
    assert res.counters.get("assembly_quote_origin_unresolved", 0) == 0
    assert res.counters.get("assembly_attribution_head_unresolved", 0) == 0
    # THE DENOMINATOR, which is the whole of "not a fake pass": three blocks
    # carry two spans each and the arm checks THREE, not six. A span spanning a
    # whole body passes by construction, so a sixth guaranteed pass would move
    # this read's fidelity from 2/3 to 5/6 for the same single real failure.
    assert [len(b["spans"]) for b in payload["blocks"]] == [2, 2, 2]
    assert res.counters["assembly_quote_spans_checked"] == 3
    assert res.counters["assembly_quote_spans_ok"] == 3
    assert res.counters["assembly_scope_spans_checked"] == 3


def test_the_skip_is_a_SKIP_and_the_arm_still_fires_on_a_quotation() -> None:
    """The one test that separates "skipped by role" from "would have passed
    anyway": corrupt each span in turn and watch which one the arm charges."""
    candidates = _three_countries()
    payload = _world(candidates, context_heads=_context_heads(candidates))
    citations = _world_citations(candidates, payload)

    payload["blocks"][0]["spans"][-1]["text"] = "CORRUPTED — not the assessment"
    assert [f.reason for f in AA.audit(payload, citations).findings] == [], (
        "the context span is not audited, so corrupting it charges nothing"
    )

    payload["blocks"][0]["spans"][0]["text"] = "CORRUPTED QUOTE"
    assert [f.reason for f in AA.audit(payload, citations).findings] == [
        AA.QUOTE_NOT_CONTAINED
    ], "and the arm is still WATCHING the quotation it was written for"


def test_the_audit_is_the_SAME_with_and_without_the_context_spans() -> None:
    """The denominator does not move, which is the property that keeps the one
    number D-1 §3.6 says must read 1.0 meaning what it says."""
    candidates = _three_countries()
    plain = _world(copy.deepcopy(candidates))
    withctx = _world(copy.deepcopy(candidates),
                     context_heads=_context_heads(candidates))
    cits = _world_citations(candidates, plain)

    a, b = AA.audit(plain, cits), AA.audit(withctx, cits)
    assert a.counters == b.counters
    assert [f.reason for f in a.findings] == [f.reason for f in b.findings]


def test_the_role_string_is_pinned_across_the_package_boundary() -> None:
    """``data.provenance`` may not import ``data.analysts``, so the two
    spellings are held equal by a test rather than by an import."""
    assert AA.SPAN_ROLE_CONTEXT_BODY == asp.SPAN_ROLE_CONTEXT_BODY
    assert asp.CONTEXT_SPAN_ROLES == frozenset({asp.SPAN_ROLE_CONTEXT_BODY})


# ---------------------------------------------------------------------------
# THE RUN — end to end over a 3-country world spine
# ---------------------------------------------------------------------------


WORLD_PROSE = (
    "**One interdiction campaign, read from two capitals**\n"
    "\n"
    "**BLUF:** Two country reads carry this window and neither outweighs the "
    "other: Ukraine's resumed strike tempo [[ref:1]] and Russia's westward "
    "force generation [[ref:2]].\n"
    "\n"
    "## The reading\n"
    "Ukraine's read treats the return to the August strike mean as escalatory "
    "[[ref:1]], and Russia's read puts two brigades into the western district "
    "in the same week [[ref:2]]; the two are consistent with a single "
    "escalation cycle, though neither country read tests that link and either "
    "alone is insufficient on its own. Saudi Arabia's read describes a quiet "
    "window [[ref:3]] and sits beside both rather than qualifying either.\n"
    "\n"
    "## What would change this\n"
    "A second week at the August strike mean would move this reading "
    "[[ref:1]].\n"
)


class _ProseLLM:
    subprovider = "step_e_test_double"

    def __init__(self, text=WORLD_PROSE) -> None:
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


def _spine_row(payload, *, spine_id=None):
    return {
        "id": spine_id or uuid4(),
        "kind": "finding",
        "analyst_id": "world_assessor",
        "target_id": None,
        "title": "World read",
        "body": "rendered elsewhere",
        "confidence": 0.62,
        "produced_at": "2026-09-20T12:00:00+00:00",
        "data": {"data": {"meta": True, "assembly": payload}},
    }


@pytest.mark.asyncio
async def test_the_channel_writes_from_the_country_reads_and_nothing_else() -> None:
    candidates = _three_countries()
    heads = _context_heads(candidates)
    payload = _world(candidates, context_heads=heads)
    spine_id = uuid4()
    row = _spine_row(payload, spine_id=spine_id)
    llm = _ProseLLM()

    result = await synth._run(
        [row],
        {"analyst_id": ac.ASSESSMENT_ANALYST_ID},
        llm=llm,
        max_tokens=768,
        temperature=1.0,
        system_prompt="unused on this branch",
    )

    assert llm.calls == 1
    assert llm.system is apr.WORLD_ASSESSMENT_SYSTEM_V5
    for head in heads.values():
        first = head["body"].split("\n")[0].replace("[[ref:", "(child ref ")
        assert first.split("(child ref")[0].strip()[:40] in llm.prompt

    # §2.1 assertion 1 — the single-element array IS the input restriction.
    assert result.derived_from == [spine_id]

    assessment = result.finding.data["assessment"]
    assert assessment["spine_id"] == str(spine_id)
    assert assessment["prompt_version"] == apr.PROMPT_VERSION_V5
    assert assessment["spine_blocks"] == 3

    cits = result.finding.data["citations"]
    assert {c["ordinal"] for c in cits} == {1, 2, 3}
    assert all(c["ref_id"] == str(spine_id) for c in cits)
    assert all(c["derived_from"] == [str(spine_id)] for c in cits)
    want_ids = {h["head_id"] for h in heads.values()}
    assert {c["context_origin_id"] for c in cits} == want_ids
    assert all(
        c["context_match"] == cs.CONTEXT_MATCH_THIS_RECORD for c in cits
    )
    # The country voice's words are what the judge will grade against.
    by_ordinal = {c["ordinal"]: c for c in cits}
    for block in payload["blocks"]:
        assert heads[block["via_head_id"]]["body"] in (
            by_ordinal[block["ordinal"]]["evidence_text"]
        )

    assert "assessment" in result.finding.tags
    assert not any(t.startswith("severity:") for t in result.finding.tags)


@pytest.mark.asyncio
async def test_the_same_spine_without_context_runs_the_v3_voice_unchanged() -> None:
    candidates = _three_countries()
    payload = _world(candidates)
    row = _spine_row(payload)
    llm = _ProseLLM()

    result = await synth._run(
        [row], {"analyst_id": ac.ASSESSMENT_ANALYST_ID}, llm=llm,
        max_tokens=768, temperature=1.0, system_prompt="unused",
    )

    assert llm.system is apr.ASSESSMENT_SYSTEM
    assert result.finding.data["assessment"]["prompt_version"] == (
        apr.PROMPT_VERSION
    )
    assert all(
        "context_origin_id" not in c for c in result.finding.data["citations"]
    )


# ---------------------------------------------------------------------------
# THE SYNTHESIZER THREADS THE STAMP
# ---------------------------------------------------------------------------


def test_the_assembler_reads_the_context_off_the_slice_rows() -> None:
    """The ``unit_names_of`` idiom: resolved where a connection exists, stamped
    onto the rows, read back by the DB-less ``_run``."""
    import inspect

    src = inspect.getsource(synth._run)
    assert "context_heads=_slice.country_assessment_context_of(inputs)" in src
    assert "context_heads" in inspect.signature(ap.build_assembly).parameters


def test_the_world_country_slice_stamps_on_both_of_its_arms() -> None:
    import inspect

    src = inspect.getsource(cs._assemble_world_country_slice)
    assert src.count("stamp_country_assessment_context(") == 2, (
        "the no-roster arm assembles a world record too, and a world voice "
        "handed one sentence per country is the defect either way"
    )
    assert "resolve_country_assessment_context(conn, country_rows)" in src, (
        "country_rows, NEVER combined — a thematic candidate has no country "
        "voice behind it and must not be given one through the fallback"
    )


def test_the_v4_voice_is_a_LEAF_and_every_old_name_still_resolves() -> None:
    """The module-size gate's own remedy, pinned. ``assessment_prompts`` held
    three voices and crossed the 1,500-line entry threshold when the third
    arrived, so the v4 voice moved to a sibling and is RE-EXPORTED — every
    caller reaches it at the name it has always used."""
    from legba.data.analysts import assessment_prompt_world_v4 as v4

    assert apr.WORLD_ASSESSMENT_SYSTEM_V4 is v4.WORLD_ASSESSMENT_SYSTEM_V4
    assert apr.PROMPT_VERSION_V4 is v4.PROMPT_VERSION_V4
    assert apr.COUNTRY_SECTION_HEADER is v4.COUNTRY_SECTION_HEADER
    assert apr.WORLD_ASSESSMENT_BODY_SHAPE_V4 is v4.WORLD_ASSESSMENT_BODY_SHAPE_V4
    # The SELECTORS stay behind: choosing a voice means seeing all three.
    for name in ("system_prompt_for", "prompt_version_for", "has_context_spans"):
        assert hasattr(apr, name)
        assert not hasattr(v4, name), (
            "the leaf holds one voice and no logic — it cannot run anything"
        )


def test_the_shared_standards_block_moved_WITHOUT_moving_a_byte() -> None:
    """``_STANDARDS`` is interpolated by all three voices, so it moved to
    ``_tradecraft`` when the third voice got its own file. Two voice files
    reading one constant is a fact about the CHANNEL, not about either voice —
    and the three system prompts are unchanged to the character."""
    from legba.data.analysts import _tradecraft as tc

    assert apr._STANDARDS is tc.ASSESSMENT_STANDARDS
    assert tc.ASSESSMENT_STANDARDS.startswith("ANALYTIC STANDARDS,")
    for system in (apr.ASSESSMENT_SYSTEM, apr.COUNTRY_ASSESSMENT_SYSTEM,
                   apr.WORLD_ASSESSMENT_SYSTEM_V4):
        assert tc.ASSESSMENT_STANDARDS in system


def test_the_carry_rule_and_its_context_live_in_ONE_module() -> None:
    """``assembly_carry``'s banner is "what a tier does with an input that
    ALREADY carries words", and STEP E's attachment is exactly that — so it
    lives there and ``assembly_payload`` re-exports it, the way it already
    re-exports ``carry_block`` and the leading-negation corpus."""
    assert ap.attach_country_context is acar.attach_country_context
    assert ap.CONTEXT_ORIGIN_KEY is acar.CONTEXT_ORIGIN_KEY
    assert ap.CONTEXT_MATCH_KEY is acar.CONTEXT_MATCH_KEY

    block = {"ordinal": 1, "spans": [{"role": "bluf", "text": "x"}]}
    assert acar.attach_country_context(block, None) is block
    assert len(block["spans"]) == 1, "no context handed over ⇒ a NO-OP"
    assert ap.CONTEXT_ORIGIN_KEY not in block


def test_the_d6_replay_harness_cannot_silently_measure_nothing() -> None:
    """The harness plants an arm by rebinding ``apr.ASSESSMENT_SYSTEM``. STEP E
    makes that name CONDITIONAL — a context-carrying world spine resolves to v4
    — so a planted arm over such a spine would run v4 on BOTH sides and produce
    a clean-looking replay of nothing. It fails loud instead."""
    from pathlib import Path

    src = (
        Path(__file__).resolve().parents[2] / "scripts" / "d6_clause_replay.py"
    ).read_text()
    assert "_apr.has_context_spans(payload)" in src
    assert "would measure nothing" in src
    assert src.index("has_context_spans(payload)") < src.index(
        "_apr.ASSESSMENT_SYSTEM = system"
    ), "the check has to run BEFORE the rebind, or it checks a planted tree"


def test_the_descriptor_records_the_v4_input_and_moves_NO_field() -> None:
    """The channel's warrant is that it has ONE input. STEP E widens what that
    one row CARRIES and not what the descriptor may reach for, so the file gains
    prose and nothing else — including the measured timing fact that decides
    which of the two context rules fires live."""
    import yaml
    from pathlib import Path

    from legba.data.schemas.analyst import AnalystDescriptor

    path = (
        Path(__file__).resolve().parents[2]
        / "descriptors" / "analyst_world_assessment.yaml"
    )
    desc = AnalystDescriptor.model_validate(yaml.safe_load(path.read_text()),
                                            strict=False)
    assert desc.identity.id == ac.ASSESSMENT_ANALYST_ID
    assert desc.subscription.targets is None, "one global run per tick"
    assert synth.assessment_spine(desc) == "world_assessor"
    assert desc.cadence.fallback_schedule == "35 0,12 * * *", (
        "RESTAGGERED 2026-09-20 (the follow-through of this train's own "
        "measurement: 0 of 7 country voices existed at :00; they land :03-:17) "
        "— world_assessor moved to :20 and this voice to :35 so both read THIS "
        "cycle's country reads; any other move is a cadence decision"
    )
    assert desc.method.system_prompt is None, (
        "the voice is a CODE constant selected from the record, never a "
        "descriptor field a PUT could change without a test seeing it"
    )
    assert desc.method.budget_tokens_per_day == 0, "house rule"

    text = path.read_text()
    assert "WORLD_ASSESSMENT_SYSTEM_V4" in text
    assert "assessment_prompt.v4" in text
    assert "assessment_of_target_recent" in text, (
        "the descriptor names which rule fires live, because the tower's "
        "stagger currently makes the FALLBACK the working path"
    )


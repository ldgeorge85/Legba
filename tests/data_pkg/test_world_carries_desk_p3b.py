# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""P3-B — THE WORLD CARRIES THE DESK BLOCK; IT DOES NOT RE-QUOTE THE CHILD.

THE DEFECT, measured live 2026-09-18. Under the assembly regime the world
assembler's candidates are COUNTRY assemblies (``country_composition``) and
THEMATIC ones (``escalation_composition``). ``build_assembly`` cut each
candidate's span with ``assembly_spans.extract_lead_span``, which reads the
candidate's rendered ``**BLUF:**`` line — and on an assembled child whose lead
was not EARNED that line is the child's own CONNECTIVE:

    "3 reads lead this cycle, weighted and uncrowned: [[ref:5]], [[ref:1]], [[ref:4]]."
    "No single read leads this cycle; 8 reads carried below, in order."

In 14 world reads over seven days, 2 to 7 of the 8 blocks quoted such a line;
today's world BLUF was one of them. The render defused the child's markers to
``(child ref 5), (child ref 1), (child ref 4)`` and the ``world_assessment``
voice then read those as WORLD ordinals and wrote a false structural claim
about its own record.

THE REPAIR is the rule the region rollup already shipped and stated in its own
banner — *carries block objects; it does not re-quote* — extracted to
``assembly_carry`` so both tiers use ONE function. What this file proves:

  * a candidate carrying an ``assembly.v1`` payload contributes a CARRIED DESK
    BLOCK: the child's own block object, byte-identical, re-numbered;
  * EARNED child ⇒ the child's crowned block; otherwise ⇒ its argmax-mass block
    with absence claims set aside, by the rollup's own rule;
  * ``origin.head_id`` is the DESK head, so quote fidelity stays the depth-1
    test it has always been — and D-3's arms READ 1.000 on a carried block,
    resolving it through the payload's own ``carried_origins`` bridge;
  * the rendered world body never contains ``(child ref`` and never quotes a
    connective;
  * a DESK head and a LEGACY-regime candidate take the old ``extract_lead_span``
    path, unchanged;
  * and a connective that reaches a span anyway CANNOT BE CONSTRUCTED.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from legba.data.analysts import assembly_carry as ac
from legba.data.analysts import assembly_payload as ap
from legba.data.analysts import assembly_render as ar
from legba.data.analysts import assembly_spans as asp
from legba.data.analysts import composition_citations as cc
from legba.data.analysts import region_rollup as rr
from legba.data.provenance import assembly_arms as AA

# Cut from live desk heads so the fixtures fail the way the real rows do —
# em dash, curly apostrophe, U+2011 and the desk's own `[N]` wire markers.
ESCALATION_BODY = (
    "*As of 2026-09-03; slice covers the trailing 72h to that date.*\n"
    "\n"
    "**BLUF:** A coordinated escalation narrative links recent U.S. strikes on "
    "Iran—including the deadly wedding attack—to Tehran’s retaliatory "
    "messaging and hard‑line threats [1] [2].\n"
    "\n"
    "## What changed\n"
    "- On 2 September the Strait of Hormuz closed to commercial traffic [1].\n"
)

ABSENCE_BODY = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** No coordinated narrative is evident in Iran’s collected "
    "reporting over the trailing window.\n"
)

ECONOMIC_BODY = (
    "*As of 2026-09-03.*\n"
    "\n"
    "**BLUF:** Tanker war‑risk premiums doubled after the second attack on a "
    "Panamanian hull [3].\n"
)

#: ``s3`` is deliberately the HEAVIEST signal and it sits under the ABSENCE
#: block — the Indonesia shape Amendment 4a was written for. A carry that
#: ignored the absence exclusion would quote "No coordinated narrative is
#: evident" into the world read.
MAGS = {"s1": 0.95, "s2": 0.70, "s3": 2.55, "s4": 0.40, "s5": 0.06}


def _desk_row(
    uid: str,
    analyst_id: str,
    body: str,
    signal_ids: tuple[str, ...],
    *,
    severity: str = "high",
    target_id: str = "country_watch_ir",
    produced_at: str = "2026-09-03T10:01:00+00:00",
) -> dict:
    """A first-order DESK head — the thing a country assembly quotes."""
    return {
        "id": uid,
        "analyst_id": analyst_id,
        "target_id": target_id,
        "title": f"head {uid}",
        "body": body,
        "severity": severity,
        "produced_at": produced_at,
        "faithfulness_score": 0.90,
        "effective_confidence": 0.80,
        "claim_verdicts": [{"verdict": "supported"}],
        "data": {
            "data": {
                "citations": [
                    {
                        "marker": f"[{i}]",
                        "signal_id": s,
                        "source_id": f"source.{s}",
                        "title": f"signal {s}",
                        "source": f"https://example.test/{s}",
                    }
                    for i, s in enumerate(signal_ids, start=1)
                ]
            }
        },
    }


def _assemble(rows: list[dict], *, tier: str = ap.TIER_COUNTRY) -> dict:
    ap.attach_cited_salience(rows, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    return ap.build_assembly(
        tier=tier,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered,
        carried=ordered[: ap.BLOCK_CAP],
    )


def _child_citations(desk_rows: list[dict], payload: dict) -> list[dict]:
    """The child's ``data.data.citations`` in the shape the producer writes.

    ``_build_composition_citation``'s own fields — one per block, ``ref_id``
    IS the block's ``finding_id`` and ``evidence_text`` is the desk head's body,
    capped. This is the bridge the carry lifts; building it any other way here
    would test a shape the fleet does not write.
    """
    by_id = {r["id"]: r for r in desk_rows}
    out = []
    for block in payload["blocks"]:
        src = by_id[block["finding_id"]]
        out.append(
            {
                "marker": f"[[ref:{block['ordinal']}]]",
                "ordinal": block["ordinal"],
                "ref_id": block["finding_id"],
                "ref_kind": "finding",
                "source": src["analyst_id"],
                "target_id": src["target_id"],
                "title": src["title"],
                "produced_at": src["produced_at"],
                "evidence_text": src["body"][:cc.MAX_EVIDENCE_TEXT_CHARS],
                "effective_confidence": 0.80,
                "derived_from": [],
            }
        )
    return out


def _composition_row(
    desk_rows: list[dict],
    payload: dict,
    *,
    analyst_id: str = "country_composition",
    target_id: str | None = "country_watch_ir",
    produced_at: str = "2026-09-03T12:00:00+00:00",
) -> dict:
    """An assembled COMPOSITION head, as the world slice reads it back."""
    return {
        "id": str(uuid4()),
        "analyst_id": analyst_id,
        "target_id": target_id,
        # The lineage the world's `derived_from` and the pooled-salience walk
        # both key on: a composition head cites FINDINGS, not wire items.
        "derived_from": [r["id"] for r in desk_rows],
        "_child_signal_ids": [
            str(c["signal_id"])
            for r in desk_rows
            for c in r["data"]["data"]["citations"]
        ],
        "title": ar.assembly_title(payload),
        "body": ar.render_assembly_body(payload),
        "severity": ap.assembly_severity(payload["blocks"]),
        "produced_at": produced_at,
        "faithfulness_score": 0.95,
        "effective_confidence": 0.90,
        "claim_verdicts": [{"verdict": "supported"}],
        "data": {
            "data": {
                "assembly": payload,
                "citations": _child_citations(desk_rows, payload),
            }
        },
    }


def _unearned_child() -> tuple[list[dict], dict, dict]:
    """A country assembly whose lead is NOT earned — three desks, flat day."""
    rows = [
        _desk_row(str(uuid4()), "escalation", ESCALATION_BODY, ("s1", "s2")),
        _desk_row(str(uuid4()), "narrative_coordination", ABSENCE_BODY, ("s3",)),
        _desk_row(
            str(uuid4()), "economic_coercion", ECONOMIC_BODY, ("s4",),
            severity="moderate",
        ),
    ]
    payload = _assemble(rows)
    return rows, payload, _composition_row(rows, payload)


def _earned_child() -> tuple[list[dict], dict, dict]:
    """A country assembly whose concentration test CROWNED one block."""
    rows = [
        _desk_row(str(uuid4()), "escalation", ESCALATION_BODY, ("s1", "s2")),
        _desk_row(
            str(uuid4()), "economic_coercion", ECONOMIC_BODY, ("s4",),
            severity="moderate",
        ),
    ]
    # ``MIN_LEAD_CANDIDATES`` is 8, so the POOL needs eight members for a lead
    # to be earnable at all. Six thin desks fill it; they are candidates, not
    # carries, which is exactly the live shape (a roster wider than BLOCK_CAP).
    filler = [
        _desk_row(
            str(uuid4()), f"thin_desk_{i}", ECONOMIC_BODY, ("s5",),
            severity="low",
        )
        for i in range(6)
    ]
    ap.attach_cited_salience(rows + filler, MAGS)
    ordered = sorted(rows, key=ap.order_key)
    payload = ap.build_assembly(
        tier=ap.TIER_COUNTRY,
        as_of="2026-09-03T12:00:00+00:00",
        candidates=ordered + sorted(filler, key=ap.order_key),
        carried=ordered,
    )
    return rows, payload, _composition_row(rows, payload)


def _world(candidates: list[dict]) -> dict:
    # `cited_mass.v1` on a COMPOSITION candidate is POOLED from its children's
    # citations one hop down (`row_signal_ids`), which is what
    # `assembly_salience.resolve_child_signal_ids` does live. Passing it here
    # keeps the world's candidate masses — the lead test's denominator — the
    # numbers the fleet actually ranks on rather than a flat pool of zeros.
    pooled = {
        r["id"]: r["_child_signal_ids"]
        for r in candidates
        if r.get("_child_signal_ids")
    }
    ap.attach_cited_salience(candidates, MAGS, pooled)
    ordered = sorted(candidates, key=ap.order_key)
    return ap.build_assembly(
        tier=ap.TIER_WORLD,
        as_of="2026-09-03T13:00:00+00:00",
        candidates=ordered,
        carried=ordered[: ap.BLOCK_CAP],
    )


def _world_citations(candidates: list[dict], payload: dict) -> list[dict]:
    """The WORLD row's citations — they name the CANDIDATE, as they do live.

    This is the whole reason the payload publishes its own bridge: the drill
    target is the country assembly (``derived_from`` is unchanged), and nothing
    in this list can resolve the desk head a carried block quotes.
    """
    by_id = {r["id"]: r for r in candidates}
    out = []
    for block in payload["blocks"]:
        src = by_id[block["via_head_id"]]
        out.append(
            {
                "marker": f"[[ref:{block['ordinal']}]]",
                "ordinal": block["ordinal"],
                "ref_id": src["id"],
                "ref_kind": "finding",
                "source": src["analyst_id"],
                "target_id": src["target_id"],
                "produced_at": src["produced_at"],
                "evidence_text": src["body"][:cc.MAX_EVIDENCE_TEXT_CHARS],
            }
        )
    return out


# ---------------------------------------------------------------------------
# THE DEFECT ITSELF — what the old path quoted, and what the new one carries
# ---------------------------------------------------------------------------


def test_the_childs_own_bluf_is_a_connective_and_not_a_claim() -> None:
    """The specimen, pinned. Without this the rest of the file tests nothing."""
    _, payload, row = _unearned_child()
    assert payload["lead"]["kind"] == ap.LEAD_NONE
    bluf = [ln for ln in row["body"].split("\n") if ln.startswith("**BLUF:**")][0]
    assert "No single read leads this cycle" in bluf
    # …and the OLD path would have cut exactly that line as the world's span.
    legacy_span = asp.extract_lead_span(row, head_id=row["id"])
    assert ac.connective_leak(legacy_span["text"]) is not None


def test_an_unearned_child_contributes_its_argmax_mass_desk_block() -> None:
    desk_rows, child, row = _unearned_child()
    world = _world([row])
    block = world["blocks"][0]

    # The block IS the child's — chosen by mass, absence set aside.
    carried, via_ordinal, why = ac.carried_lead_block(child)
    assert why == ac.CARRY_BY_MASS
    assert block["carry_reason"] == ac.CARRY_BY_MASS
    assert block["via_ordinal"] == via_ordinal
    assert block["via_head_id"] == row["id"]
    assert block["ordinal"] == 1  # re-numbered into the WORLD's space

    # …and the words are the DESK's sentence, not the child's connective.
    assert block["spans"][0]["text"] == carried["spans"][0]["text"]
    assert "escalation narrative" in block["spans"][0]["text"]
    assert ac.connective_leak(block["spans"][0]["text"]) is None

    # DEPTH-1: the origin is the desk head, not the candidate row.
    desk_id = block["spans"][0]["origin"]["head_id"]
    assert desk_id == block["finding_id"]
    assert desk_id in {r["id"] for r in desk_rows}
    assert desk_id != row["id"]


def test_the_carried_block_is_byte_identical_apart_from_the_carry_keys() -> None:
    """Nothing is rewritten — offsets, sha256, question, target and attribution
    all still describe the desk head that wrote the sentence."""
    _, child, row = _unearned_child()
    world = _world([row])
    block = world["blocks"][0]
    source, _, _ = ac.carried_lead_block(child)

    moved = {"ordinal", "via_head_id", "via_ordinal", "carry_reason"}
    assert set(block) - set(source) == {"via_head_id", "via_ordinal", "carry_reason"}
    for key in set(source) - moved - {"spans"}:
        assert block[key] == source[key], key
    # P3-A × P3-B: the world carries the QUOTATIONS byte-identical; a country
    # block's ``context_body`` span (the country voice's context) stays at the
    # country tier, so the carried spans are the child's quoted spans exactly.
    assert block["spans"] == list(asp.quoted_spans(source))
    assert all(not asp.is_context_span(sp) for sp in block["spans"])
    assert block["spans"][0]["origin"]["body_sha256"] == (
        source["spans"][0]["origin"]["body_sha256"]
    )

    # A deep copy, so the world payload can never mutate the candidate row.
    block["spans"][0]["text"] = "tampered"
    assert source["spans"][0]["text"] != "tampered"


def test_an_earned_child_contributes_the_block_it_crowned() -> None:
    _, child, row = _earned_child()
    assert child["lead"]["kind"] == ap.LEAD_EARNED_SINGLE
    crowned = child["lead"]["block_ordinals"][0]

    block = _world([row])["blocks"][0]
    assert block["carry_reason"] == ac.CARRY_EARNED_LEAD
    assert block["via_ordinal"] == crowned
    by_ordinal = {b["ordinal"]: b for b in child["blocks"]}
    assert block["spans"][0]["text"] == by_ordinal[crowned]["spans"][0]["text"]


def test_a_thematic_candidate_is_carried_by_the_same_rule() -> None:
    """``escalation_composition`` is the world's one legal cross-region object
    and it assembles too — D-5 §4.2 says retiring the region tier must not
    quietly retire the thematic lane, and neither may this repair."""
    desk_rows, child, _ = _unearned_child()
    thematic = _composition_row(
        desk_rows,
        child,
        analyst_id="escalation_composition",
        target_id=None,
    )
    block = _world([thematic])["blocks"][0]
    assert block["via_head_id"] == thematic["id"]
    assert block["carry_reason"] == ac.CARRY_BY_MASS
    assert ac.connective_leak(block["spans"][0]["text"]) is None
    assert block["finding_id"] in {r["id"] for r in desk_rows}


# ---------------------------------------------------------------------------
# THE RENDERED BODY — the surface the voice and the reader actually see
# ---------------------------------------------------------------------------


def test_the_world_body_never_quotes_a_connective_or_a_child_ref() -> None:
    _, _, one = _unearned_child()
    _, _, two = _earned_child()
    body = ar.render_assembly_body(_world([one, two]))

    assert "(child ref" not in body
    quoted = [ln for ln in body.split("\n") if ln.startswith(">")]
    assert quoted, "the record carried no quoted span at all"
    for line in quoted:
        assert ac.connective_leak(line) is None, line
    # The world's OWN BLUF is a connective when its own lead is unearned — that
    # is the tier's voice and it is allowed. What is forbidden is a connective
    # inside a BLOCKQUOTE, i.e. attributed to a desk head.
    assert all(not ln.startswith(">") for ln in body.split("\n") if "[[ref:" in ln and ln.startswith("**BLUF:**"))


def test_the_defuse_is_now_a_no_op_on_every_carried_span() -> None:
    """``_defuse_child_ref_markers`` stays in the renderer as a GUARD. The carry
    is what makes it inert: a desk body has no ``[[ref:`` in it."""
    _, _, row = _unearned_child()
    world = _world([row])
    for block in world["blocks"]:
        for span in block["spans"]:
            assert ar.quoted_text(span) == span["text"]


# ---------------------------------------------------------------------------
# D-3 — the arms resolve a carried block against the DESK head
# ---------------------------------------------------------------------------


def test_the_arms_read_quote_fidelity_1_000_on_a_carried_block() -> None:
    _, _, row = _unearned_child()
    world = _world([row])
    res = AA.audit(world, _world_citations([row], world))

    assert [f.reason for f in res.findings] == []
    assert AA.quote_fidelity_score(res) == 1.0
    assert res.counters["assembly_carried_origins_bridged"] == 1
    assert res.counters.get("assembly_quote_origin_unresolved", 0) == 0
    assert res.counters.get("assembly_attribution_head_unresolved", 0) == 0


def test_the_bridge_resolves_the_desk_head_and_the_citations_cannot() -> None:
    """The reason the payload publishes a bridge at all, stated as a test: the
    row's citations name the CANDIDATE (that is the drill target and
    ``derived_from`` is unchanged), so without the bridge every carried block
    would cost the read an ``attribution_head_unresolved``."""
    _, _, row = _unearned_child()
    world = _world([row])
    citations = _world_citations([row], world)
    assert {c["ref_id"] for c in citations} == {row["id"]}

    bridge = world[AA.CARRIED_ORIGINS_KEY]
    assert [b["ref_id"] for b in bridge] == [
        b["finding_id"] for b in world["blocks"]
    ]

    stripped = {k: v for k, v in world.items() if k != AA.CARRIED_ORIGINS_KEY}
    unbridged = AA.audit(stripped, citations)
    assert any(
        f.reason == AA.ATTRIBUTION_HEAD_UNRESOLVED for f in unbridged.findings
    )


def test_a_child_that_published_no_citation_is_declined_never_fabricated() -> None:
    """A bridge assembled out of the block it is supposed to check would make
    ARM 3 compare a block against a copy of itself."""
    _, _, row = _unearned_child()
    row["data"]["data"]["citations"] = []
    world = _world([row])
    assert AA.CARRIED_ORIGINS_KEY not in world
    res = AA.audit(world, _world_citations([row], world))
    assert res.counters["assembly_attribution_head_unresolved"] == 1


def test_the_arms_are_byte_identical_on_a_payload_that_carries_nothing() -> None:
    """The inertness property, as a return value: a country assembly publishes
    no bridge key and its audit is the one that shipped."""
    desk_rows, child, _ = _unearned_child()
    citations = _child_citations(desk_rows, child)
    assert AA.CARRIED_ORIGINS_KEY not in child
    res = AA.audit(child, citations)
    assert "assembly_carried_origins_bridged" not in res.counters
    assert AA.quote_fidelity_score(res) == 1.0


# ---------------------------------------------------------------------------
# THE LEGACY PATH, AND THE GATE THAT SAYS A CONNECTIVE IS NEVER A SPAN
# ---------------------------------------------------------------------------


def test_a_desk_head_candidate_still_takes_the_extract_lead_span_path() -> None:
    row = _desk_row(str(uuid4()), "escalation", ESCALATION_BODY, ("s1", "s2"))
    block = _world([row])["blocks"][0]
    assert ac.child_assembly(row) is None
    assert "via_head_id" not in block
    assert block["finding_id"] == row["id"]
    assert block["spans"] == [asp.extract_lead_span(
        row,
        head_id=row["id"],
        scope_predicate=None,
    ) | {"scope_tokens": block["spans"][0]["scope_tokens"]}]


def test_a_legacy_regime_candidate_is_not_carried_from() -> None:
    """§5.2 — the flag-off arm must stay byte-for-byte what it was, and a
    legacy row carries the regime stamp with no blocks behind it."""
    row = _desk_row(str(uuid4()), "escalation", ESCALATION_BODY, ("s1", "s2"))
    row["data"]["data"]["assembly"] = ap.legacy_regime_stamp()
    assert ac.child_assembly(row) is None
    block = _world([row])["blocks"][0]
    assert "via_head_id" not in block
    assert block["spans"][0]["origin"]["head_id"] == row["id"]


def test_a_rollup_candidate_is_not_carried_from() -> None:
    row = _desk_row(str(uuid4()), "escalation", ESCALATION_BODY, ("s1", "s2"))
    row["data"]["data"]["assembly"] = {
        "schema": ap.ASSEMBLY_SCHEMA, "regime": ap.REGIME_ROLLUP,
    }
    assert ac.child_assembly(row) is None


def test_a_connective_that_reaches_a_span_cannot_be_constructed() -> None:
    """THE GATE (D-1 §3.6's posture, applied to the connective vocabulary). The
    residue case: a composed body that the carry rule did not recognise —
    here, an assembled child mislabelled ``legacy`` — falls through to
    ``extract_lead_span`` and cuts the child's own lead line. That read is not
    published; it RAISES.
    """
    _, child, row = _unearned_child()
    row["data"]["data"]["assembly"] = ap.legacy_regime_stamp()
    with pytest.raises(ap.AssemblyConstructionError) as exc:
        _world([row])
    assert "CONNECTIVE" in str(exc.value)
    assert "No single read leads this cycle" in str(exc.value)


def test_the_gate_catches_the_co_lead_spelling_too() -> None:
    """A prefix test would pass it: the line opens with a COUNT."""
    assert ac.connective_leak(
        "3 reads lead this cycle, weighted and uncrowned: "
        "[[ref:5]], [[ref:1]], [[ref:4]]."
    ) == "lead this cycle, weighted and uncrowned"
    assert ac.connective_leak("Tanker premiums doubled overnight.") is None


# ---------------------------------------------------------------------------
# THE ARITHMETIC THE CARRY MUST NOT MOVE
# ---------------------------------------------------------------------------


def test_the_lead_test_still_reads_the_candidate_rows_mass() -> None:
    """Only the quoted words change. ``earned_lead``'s denominator is the
    CANDIDATE row's ``cited_mass`` — the country assembly's, pooled from its
    children — and a carried block's own salience is the desk head's. Reading
    the block here would silently re-base the crown."""
    _, _, one = _unearned_child()
    _, _, two = _earned_child()
    world = _world([one, two])
    masses = sorted((ap._mass(r) for r in (one, two)), reverse=True)
    assert all(m > 0.0 for m in masses), "the pooled key must actually score"
    expected = ap.earned_lead(masses)
    assert world["lead"]["test"]["n_candidates"] == expected["n_candidates"]
    assert world["lead"]["test"]["top_share"] == expected["top_share"]
    assert world["lead"]["test"]["ratio_12"] == expected["ratio_12"]
    # …and the carried blocks' own salience is NOT that number.
    assert [b["salience"]["cited_mass"] for b in world["blocks"]] != masses


def test_the_drop_ledger_and_the_invariants_still_key_on_the_candidates() -> None:
    _, _, one = _unearned_child()
    _, _, two = _earned_child()
    world = _world([one, two])
    assert world["drops"]["counts"]["candidates"] == 2
    assert world["drops"]["counts"]["carried"] == len(world["blocks"])
    assert [b["ordinal"] for b in world["blocks"]] == [1, 2]
    assert {b["via_head_id"] for b in world["blocks"]} == {one["id"], two["id"]}


# ---------------------------------------------------------------------------
# ONE DEFINITION — the pins that keep the two tiers from drifting apart
# ---------------------------------------------------------------------------


def test_the_region_rollup_and_the_world_select_with_one_function() -> None:
    assert rr._lead_block is ac.carried_lead_block
    assert rr._is_absence_scoped is ap.block_is_absence is ac.block_is_absence
    assert rr.CARRY_REASONS is ac.CARRY_REASONS


def test_the_guarded_phrases_are_members_of_the_render_vocabulary() -> None:
    """The guard cannot import the vocabulary it guards (the renderer imports
    the assembler imports the carry), so the membership is pinned here."""
    for phrase in ac.LEAD_CONNECTIVE_PHRASES:
        assert phrase in ar.CONNECTIVES, phrase


def test_the_bridge_width_matches_the_citation_capture_width() -> None:
    assert ac.MAX_ORIGIN_EVIDENCE_CHARS == cc.MAX_EVIDENCE_TEXT_CHARS


def test_the_duplicated_markers_agree_with_the_provenance_side() -> None:
    assert ac.ASSEMBLY_SCHEMA == ap.ASSEMBLY_SCHEMA == AA.ASSEMBLY_SCHEMA
    assert ac.REGIME_ASSEMBLY == ap.REGIME_ASSEMBLY
    assert ac.LEAD_EARNED_SINGLE == ap.LEAD_EARNED_SINGLE
    assert ac.CARRIED_ORIGINS_KEY == AA.CARRIED_ORIGINS_KEY == ap.CARRIED_ORIGINS_KEY


def test_the_dropped_side_of_a_tension_is_carried_too() -> None:
    """``tensions[].b_ref.span`` is published verbatim and the voice reads it as
    something the uncarried read SAID. On an assembled child that sentence was
    the connective; now it is the block the child would have contributed."""
    _, child, row = _unearned_child()
    carried, _, _ = ac.carried_lead_block(child)
    assert ac.carried_verdict_text(row) == carried["spans"][0]["text"]
    assert ac.connective_leak(ac.carried_verdict_text(row)) is None


def test_a_desk_head_dropped_row_still_reads_its_own_verdict() -> None:
    """The country tier's dropped rows are desk heads, so this function IS
    ``desk_verdict_text`` on every call it makes."""
    from legba.data.provenance.composition_integrity import desk_verdict_text

    row = _desk_row(str(uuid4()), "escalation", ESCALATION_BODY, ("s1",))
    assert ac.carried_verdict_text(row) == desk_verdict_text(row["body"])

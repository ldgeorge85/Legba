# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""H2 — COMPOSITION-LAYER INTEGRITY, pinned on the sentences that ordered it.

Every fixture below is VERBATIM from the CORRECTNESS-R2 round (2026-08-25): ten
country reads graded against the world by independent analysts, whose attribution
ranked caveat stripping at the composition second by mechanism mass. The
composition bodies and the unit-head bodies are the graded packets' own text, and
each defect fixture carries the grader's verdict id so the sentence and the
judgement that condemned it stay attached to each other.

THE THREE DEFECTS (each must FAIL its arm):

* ``JP CR-405f8996`` / ``CR-9c20f91d`` — scope deletion. The desk wrote *"in this
  desk's collection"*; the composition deleted it and made the result its BLUF.
  All three of the JP lane's ``inaccurate`` verdicts trace to that one deletion.
* ``GB CR-dcadfbba`` — the desk cited for its own negation. *"The
  military-posture read confirms ... increasing ... expanding"* over a head whose
  verdict is *"No material change ... remains unchanged"*. The GB grader names
  this inside the product's own packet: *"the composition says military posture
  is expanding while the military_posture desk says it is unchanged"*.
* ``GB CR-36010d52`` — a claim the named desk does not make. *"the
  internal-stability read notes only modest, isolated protests"* over a head that
  reports *"No unrest or coup-related activity"* at all.
* ``IR CR-4356e595`` — words in the desk's mouth. *"'smart-defence' doctrine"*
  attributed to a military_posture read that, per the grader, *"speaks only of a
  'doctrinal shift'"*.

THE CREDITS (each must PASS — over-firing is the expensive error):

* ``UA CR-1bf4f3f1`` — the round's clearest composition-layer CREDIT: the head
  asserts a *"~60 %"* chance Zelensky is forced out and the composition declines
  to carry the number. The grader: *"the composition is MORE disciplined than the
  unit head it draws on ... a figure the open record does not support and which
  the composition wisely did not carry."* A check that charges this is worse than
  no check.
* ``UA CR-7277d880`` and ``JP CR-3952e5b0`` — the two ``accurate`` verdicts in
  their lanes.
* The IR energy and economic-coercion attributions, which relay their desks
  correctly (including a quoted operation name that IS in the cited read).
* The JP BLUF WITH the qualifier restored — the fix has to clear the check, or
  the check is not pointing at a fixable thing.

THE DECLINED CASE, pinned as a behaviour rather than a bug:

* ``IR CR-13044838`` — the "composition-INVERTED" sentence, *"simultaneously
  tightening maritime control"*, against a military_posture head that opens
  *"toward tighter control ... through a joint Iran-Oman corridor"* and reports
  the corridor and a mine-clearance project as the window's change. Both poles
  live in one desk read. The deterministic arm DECLINES it (the ambivalence
  guard), and the judge rubric names it — with this sentence — instead. Both
  halves are asserted below, because "the mechanical check does not fire here"
  is a decision, not an omission.
"""
from __future__ import annotations

import legba.data.provenance.composition_integrity as ci
from legba.data.provenance.verify import (
    FAIL_CLASS_HARD,
    FAIL_CLASS_SOFT,
    fail_class_for_reason,
    verify_finding_faithfulness,
)

# ---------------------------------------------------------------------------
# THE GRADED TEXT — verbatim from stage2_{JP,GB,IR,UA}.json (CORRECTNESS-R2).
# Typographic forms are the packets' own (U+2011 non-breaking hyphens inside
# desk names, curly quotes around the coinage): the normaliser is under test
# here too, and a fixture that silently ASCII-fied them would test nothing.
# ---------------------------------------------------------------------------

JP_HEAD_NARRATIVE = (
    "*As of 2026-08-25; slice covers the trailing 72h to that date; 120 "
    "signals.*\n\n"
    "**BLUF:** No coordinated narrative appears in this desk's collection for "
    "Japan through 25 Aug 2026, with coverage reflecting organic reporting of "
    "multiple unrelated topics.\n\n"
    "## What changed\n\n"
    "- No material change since the prior read on 24 Aug 2026 04:06 UTC, which "
    "also found no coordinated narrative [121].\n"
)

#: JP CR-405f8996 (inaccurate) and CR-9c20f91d (inaccurate) — the BLUF and its
#: restatement, both citing the SAME desk-scoped head.
JP_COMPOSITION = (
    "*As of 25 August 2026; composed from 7 unit reads, latest 13:01 UTC.*\n\n"
    "**BLUF:** No coordinated narrative appears in Japan’s information "
    "environment in the latest 72‑hour slice, indicating reporting remains "
    "fragmented [[ref:2]].\n\n"
    "## The picture\n"
    "The information environment shows no sign of a coordinated narrative, "
    "reinforcing the view that current coverage is organic and unaligned "
    "[[ref:2]].\n"
)

#: The same BLUF with the qualifier KEPT — the fix must clear the check.
JP_COMPOSITION_FIXED = (
    "**BLUF:** No coordinated narrative appears in this desk's collection for "
    "Japan in the latest 72‑hour slice, indicating reporting remains "
    "fragmented [[ref:2]].\n"
)

GB_HEAD_MILITARY = (
    "*As of 2026-08-25; slice covers the trailing 3 days to that date; 120 "
    "signals.*\n\n"
    "**BLUF:** No material change; the United Kingdom’s standing offensive "
    "support to Ukraine remains unchanged in this window.\n\n"
    "## What changed\n"
    "- No new confirmed materiel deliveries, basing changes, or deployment "
    "movements are observed compared with the prior window ledger entry for 24 "
    "August 2026 that recorded the UK’s SCALP missile data transfer to "
    "Ukraine [121].\n\n"
    "## Why it matters\n"
    "Announced plans to share additional missile blueprints reinforce the UK's "
    "political commitment but, lacking observable deliveries or deployment "
    "changes, do not alter the standing posture. Consequently, the UK continues "
    "to project offensive support capacity without expanding its tangible force "
    "structure or basing footprint.\n"
)

GB_HEAD_INTERNAL = (
    "*As of 2026-08-25; slice covers the trailing 72h to that date; 120 "
    "signals.*\n\n"
    "**BLUF:** No unrest or coup‑related activity appears in this desk’s "
    "collection through 25 August 2026.\n\n"
    "## Why it matters\n\n"
    "The absence of any reported mass protests, security‑force defections, "
    "elite purges, or state‑repression means there is no new trigger to alter "
    "the United Kingdom’s already low coup‑vulnerability baseline.\n"
)

GB_HEAD_ENERGY = (
    "**BLUF:** The United Kingdom is under high energy‑security pressure, "
    "driven by a confirmed Iranian‑linked cyber‑attack that shut down a "
    "generator and disrupted rail services, while expanding data‑centre "
    "demand adds strain to the power grid.\n"
)

#: GB CR-dcadfbba (the packet-internal contradiction the grader names) and
#: CR-36010d52 (the claim the internal-stability desk does not make).
GB_COMPOSITION = (
    "The military‑posture read confirms that the UK is modestly increasing "
    "offensive support to Ukraine by transferring SCALP‑missile component "
    "data, expanding its standing posture toward greater weapon‑provision "
    "capability [[ref:6]].\n\n"
    "While the internal‑stability read notes only modest, isolated protests, "
    "the overall situation remains under control [[ref:5]].\n"
)

#: The GB energy attribution, which relays its desk correctly.
GB_COMPOSITION_CLEAN = (
    "The energy‑security desk reports that a confirmed Iranian‑linked "
    "cyber‑attack on 23 August shut down a generating unit for four days, "
    "pushing national energy‑security pressure to a high level [[ref:3]].\n"
)

IR_HEAD_MILITARY = (
    "*As of 2026-08-25; slice covers the trailing 72h to that date; 120 "
    "signals.*\n\n"
    "**BLUF:** Iran is moving its maritime doctrine toward tighter control of "
    "the Strait of Hormuz through a joint Iran‑Oman corridor, advancing a "
    "previously declared offensive stance.\n\n"
    "## What changed\n"
    "- The window adds concrete steps to the doctrinal shift recorded in the "
    "ledger: Iran and Oman agreed on a temporary navigational corridor and "
    "joint mine‑clearance project (reported on 25 August 2026)[1], and "
    "discussed a permanent route (reported on 25 August 2026)[5].\n"
)

IR_HEAD_ECONOMIC = (
    "**BLUF:** The United States has imposed a fresh wave of sweeping sanctions "
    "on Iran, intensifying the high‑severity economic coercion pressure the "
    "regime faces.\n\n"
    "## What changed\n"
    "- On 24 August 2026, Treasury Secretary Scott Bessent unveiled “Operation "
    "Economic Outcast,” a new sanctions package targeting roughly 60 Iranian "
    "entities, individuals and vessels across five sectors [2].\n"
)

IR_HEAD_ENERGY = (
    "**BLUF:** Iran continues to face high energy‑security pressure as the "
    "Hormuz blockade tightens and new U.S. sanctions intensify economic and "
    "shipping constraints.\n"
)

#: IR CR-4356e595 — "'smart-defence' doctrine" put in the desk's mouth.
IR_COMPOSITION = (
    "In parallel, the military‑posture desk notes that Iran has legislated "
    "fees on vessels transiting the Strait of Hormuz, translating its "
    "“smart‑defence” doctrine into an operational tool that can be used to "
    "deter and financially leverage foreign shipping [[ref:6]].\n"
)

#: The two IR attributions that relay their desks correctly — including a quoted
#: operation name that IS in the cited read.
IR_COMPOSITION_CLEAN = (
    "The energy‑security desk reports that the Strait of Hormuz is "
    "effectively sealed, curtailing Iran’s oil export capacity and driving a "
    "sharp depreciation of the rial, which deepens fiscal strain [[ref:1]].\n\n"
    "The economic‑coercion desk confirms that the United States launched "
    "“Operation Economic Outcast,” a new sanctions package targeting dozens of "
    "Iranian entities, vessels and sectors, further isolating Iran’s trade "
    "channels [[ref:4]].\n"
)

#: IR CR-13044838 — the "composition-INVERTED" sentence. Markerless synthesis,
#: and the arm's declared blind spot (see the module docstring).
IR_COMPOSITION_INVERSION = (
    "Together these developments depict a nation under severe "
    "energy‑security stress while simultaneously tightening maritime control "
    "and modestly boosting its strategic weaponry.\n"
)

UA_HEAD_LEADERSHIP = (
    "*As of 2026-08-25; slice covers the trailing 72 h to that date; 120 "
    "signals.*\n\n"
    "**BLUF:** There is a moderate (~60 %) chance that President Volodymyr "
    "Zelensky could be forced to resign or see his cabinet reshuffled within "
    "the next 30 days.\n"
)

UA_HEAD_ECONOMIC = (
    "**BLUF:** Ukraine is intensifying weaponized commodity coercion by "
    "striking Russian energy and logistics assets with drones, maintaining "
    "pressure despite Kremlin warnings.\n"
)

#: UA CR-1bf4f3f1 (accurate, and the round's composition-layer CREDIT) and
#: CR-7277d880 (accurate).
UA_COMPOSITION_CREDIT = (
    "Elevated domestic pressure on President Zelensky, highlighted by calls for "
    "wartime elections and the potential reshuffling of his cabinet, signals an "
    "increased leadership‑transition risk that could destabilize Ukraine’s "
    "political leadership in the coming weeks [[ref:1]].\n\n"
    "Simultaneously, Ukraine is weaponising economic coercion by targeting "
    "Russian energy and logistics assets with drones, thereby inflicting "
    "revenue losses on the Russian war‑economy [[ref:5]].\n"
)

JP_HEAD_ENERGY = (
    "**BLUF:** Japan remains under high energy‑security pressure, now driven "
    "chiefly by a sharp surge in electricity prices amid an intense heat‑wave "
    "and Middle‑East gas supply constraints [47][58].\n"
)

#: JP CR-3952e5b0 (accurate) — "the most precise sentence in the read".
JP_COMPOSITION_CLEAN = (
    "At the same time, Japan is under high energy‑security pressure as "
    "day‑ahead electricity prices jumped about 20 % to ¥25.18/kWh, the "
    "highest since January 2023 [[ref:6]].\n"
)


def _cite(ordinal: int, source: str, evidence: str) -> dict[str, object]:
    """One composition citation in the PRODUCTION shape.

    Mirrors ``cross_analyst_correlator``'s citation builder exactly: the
    ``[[ref:N]]`` marker, ``ref_kind='finding'`` (which is what routes verify to
    the sub-claim floor), ``evidence_text`` = the cited output's own body
    captured at synth time, and ``source`` = the producing analyst id. Nothing
    here is invented for the test — this is the row the check reads in
    production, which is the point of testing through the real entrypoint.
    """
    return {
        "marker": f"[[ref:{ordinal}]]",
        "ordinal": ordinal,
        "ref_id": f"00000000-0000-0000-0000-{ordinal:012d}",
        "ref_kind": "finding",
        "evidence_text": evidence,
        "source": source,
        "title": f"{source} read",
    }


JP_CITATIONS = [_cite(2, "narrative_coordination", JP_HEAD_NARRATIVE),
                _cite(6, "energy_security", JP_HEAD_ENERGY)]
GB_CITATIONS = [_cite(3, "energy_security", GB_HEAD_ENERGY),
                _cite(5, "internal_stability", GB_HEAD_INTERNAL),
                _cite(6, "military_posture", GB_HEAD_MILITARY)]
IR_CITATIONS = [_cite(1, "energy_security", IR_HEAD_ENERGY),
                _cite(4, "economic_coercion", IR_HEAD_ECONOMIC),
                _cite(6, "military_posture", IR_HEAD_MILITARY)]
UA_CITATIONS = [_cite(1, "leadership_transition", UA_HEAD_LEADERSHIP),
                _cite(5, "economic_coercion", UA_HEAD_ECONOMIC)]


def _reasons(report) -> list[str]:
    return [s.reason for s in report.unsupported_spans]


# ---------------------------------------------------------------------------
# THE THREE DEFECTS — through the REAL binding path
# (``verify_finding_faithfulness``), not the detector functions, because the
# defect is only real if it survives segmentation, the sub-claim floor, the fold
# order and the ledger.
# ---------------------------------------------------------------------------


async def test_jp_scope_deletion_fails(monkeypatch) -> None:
    """JP CR-405f8996 + CR-9c20f91d — the smoking gun, both sentences."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=JP_COMPOSITION, citations=JP_CITATIONS, target_id="country_g20_jp",
    )
    laundered = [
        s for s in report.unsupported_spans
        if s.reason == ci.ABSENCE_SCOPE_LAUNDERED
    ]
    # BOTH the BLUF and its restatement: the grader counted them separately and
    # so does the ledger — three of the JP lane's verdicts came off one deletion.
    assert len(laundered) == 2, _reasons(report)
    assert report.counters["composition_integrity_absence_scope_laundered"] == 2
    # The detail must NAME the scoped sentence the composition stripped, or the
    # ledger row is an accusation with no exhibit.
    assert "this desk's collection" in laundered[0].detail
    assert "[[ref:2]]" in laundered[0].detail
    # It is a ledger row with a class, not a log line.
    row = next(
        v for v in report.claim_verdicts if v.reason == ci.ABSENCE_SCOPE_LAUNDERED
    )
    assert row.verdict == FAIL_CLASS_SOFT
    assert "Japan’s information environment" in row.text


async def test_the_time_bound_is_not_the_scope_the_composition_deleted() -> None:
    """The JP BLUF carries "in the latest 72-hour SLICE", and "slice" is in the
    SHARED collection-scope lexicon — which is why the shared predicate cannot be
    the equivalence test and this module carries its own."""
    from legba.data.provenance.verify import _has_collection_scope

    bluf = (
        "No coordinated narrative appears in Japan’s information environment "
        "in the latest 72‑hour slice."
    )
    assert _has_collection_scope(bluf.lower()) is True      # the shared lexicon
    assert ci.has_collection_denominator_scope(bluf) is False  # the real test
    assert ci.has_collection_denominator_scope(
        "No coordinated narrative appears in this desk's collection."
    ) is True


async def test_gb_false_attribution_is_the_only_hard_class(monkeypatch) -> None:
    """GB CR-dcadfbba + CR-36010d52 — the desk cited FOR ITS OWN NEGATION, and a
    second desk cited for the presence of what it records as absent."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=GB_COMPOSITION, citations=GB_CITATIONS, target_id="country_g20_gb",
    )
    reasons = _reasons(report)
    assert ci.ATTRIBUTION_DIRECTION_CONFLICT in reasons, reasons
    assert ci.ATTRIBUTION_ASSERTS_DESK_NEGATIVE in reasons, reasons
    assert fail_class_for_reason(ci.ATTRIBUTION_DIRECTION_CONFLICT) == FAIL_CLASS_HARD
    conflict = next(
        s for s in report.unsupported_spans
        if s.reason == ci.ATTRIBUTION_DIRECTION_CONFLICT
    )
    # The V-D rule: a hard verdict must point at the thing it convicts on. BOTH
    # poles, verbatim, from the two documents.
    assert "INCREASE" in conflict.detail and "UNCHANGED" in conflict.detail
    assert conflict.markers == [6]
    negative = next(
        s for s in report.unsupported_spans
        if s.reason == ci.ATTRIBUTION_ASSERTS_DESK_NEGATIVE
    )
    # It binds to the desk sentence that actually DENIES protests — the head's
    # BLUF denies "unrest or coup-related activity", and the sentence that names
    # protests is the one below it. Naming the wrong sentence would be an
    # accusation with the wrong exhibit.
    assert "protest" in negative.detail
    assert "The absence of any reported mass protests" in negative.detail
    assert negative.markers == [5]


async def test_ir_words_in_the_desks_mouth_fails(monkeypatch) -> None:
    """IR CR-4356e595 — 'smart-defence' appears nowhere in the cited read."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=IR_COMPOSITION, citations=IR_CITATIONS, target_id="country_g20_ir",
    )
    assert ci.ATTRIBUTION_UNGROUNDED_QUOTE in _reasons(report)
    span = next(
        s for s in report.unsupported_spans
        if s.reason == ci.ATTRIBUTION_UNGROUNDED_QUOTE
    )
    assert "smart-defence" in span.detail
    assert report.counters["composition_integrity_attribution_ungrounded_quote"] == 1


async def test_every_defect_costs_exactly_one_denominator_slot(monkeypatch) -> None:
    """The fold arithmetic, pinned: a violation is one more checkable-but-
    unsupported claim and nothing else — the ``_fold_guard_spans`` shape, so a
    soft failure raised here costs what a soft failure raised anywhere costs."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    dirty = await verify_finding_faithfulness(
        body=JP_COMPOSITION, citations=JP_CITATIONS, target_id="country_g20_jp",
    )
    clean = await verify_finding_faithfulness(
        body=JP_COMPOSITION_FIXED, citations=JP_CITATIONS,
        target_id="country_g20_jp",
    )
    assert dirty.checkable_claims - dirty.supported_claims >= 2
    assert dirty.faithfulness_score < clean.faithfulness_score


# ---------------------------------------------------------------------------
# THE CREDITS — no over-firing. These are the tests that decide whether the
# check is worth having.
# ---------------------------------------------------------------------------

_H2_REASONS = set(ci.FAIL_CLASSES)


async def test_the_ua_tempered_coup_odds_credit_passes(monkeypatch) -> None:
    """UA CR-1bf4f3f1 — the head asserts "~60 %", the composition declines to
    carry it and says only "increased ... risk". The grader called that MORE
    disciplined than the desk. A check that charges the fix is worse than no
    check, so this is the fixture that constrains the direction arm hardest."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=UA_COMPOSITION_CREDIT, citations=UA_CITATIONS,
        target_id="country_g20_ua",
    )
    assert not (_H2_REASONS & set(_reasons(report))), _reasons(report)


async def test_correct_attributions_pass_and_are_counted_clean(monkeypatch) -> None:
    """Four attributed clauses that relay their desks faithfully — including a
    QUOTED operation name that IS in the cited read, which is what separates the
    grounding arm from a blanket ban on quotation."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    for body, cites, target in (
        (GB_COMPOSITION_CLEAN, GB_CITATIONS, "country_g20_gb"),
        (IR_COMPOSITION_CLEAN, IR_CITATIONS, "country_g20_ir"),
    ):
        report = await verify_finding_faithfulness(
            body=body, citations=cites, target_id=target,
        )
        assert not (_H2_REASONS & set(_reasons(report))), (target, _reasons(report))
        assert report.counters["composition_integrity_attributions_clean"] >= 1


#: BF — the sweep's ONE false hard fail, and the case that fixed the polarity
#: table. The desk's BLUF is an ABSENCE ("No NEW internal-stability threats");
#: its very next line records "a modest RISE in elite fracture", which is
#: precisely what the composition relays. Reading "no new" as an UNCHANGED
#: verdict convicted a faithful sentence, so the absence idioms came out of the
#: direction families. This fixture is the ratchet on that.
BF_HEAD_INTERNAL = (
    "*As of 2026-08-24; slice covers the trailing 3 days to that date; 1 "
    "signals.*\n"
    "**BLUF:** No new internal‑stability threats are observed in this desk’s "
    "collection through 24 August 2026.\n\n"
    "## What changed\n"
    "No material change since the prior internal‑stability reads that recorded "
    "a modest rise in elite fracture (see window ledger) [2].\n"
)

BF_COMPOSITION_CREDIT = (
    "The internal‑stability desk notes a modest increase in elite fracture "
    "while no mass protests, strikes, or security‑force defections have been "
    "observed, leaving the overall stability assessment at a moderate level "
    "[[ref:1]].\n"
)


async def test_bf_faithful_relay_is_not_a_direction_conflict(monkeypatch) -> None:
    """BF — measured as a FALSE HARD FAIL on the ten-composition sweep, and the
    reason the absence idioms are not direction poles.

    "No NEW internal-stability threats are observed" is a negative about
    THREATS, not a verdict that elite fracture held its level — and the desk
    says "a modest RISE in elite fracture" one line down, which is exactly what
    the composition relayed. A negative is not a direction verdict.
    """
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=BF_COMPOSITION_CREDIT,
        citations=[_cite(1, "internal_stability", BF_HEAD_INTERNAL)],
        target_id="country_g20_bf",
    )
    assert not (_H2_REASONS & set(_reasons(report))), _reasons(report)
    assert "no new" not in str(
        ci.direction_poles("No new internal-stability threats are observed.")
    )
    assert ci.direction_poles("No new threats are observed.") == {}


def test_the_subject_gate_holds_the_hard_class() -> None:
    """Two direction words in one paragraph are not a contradiction unless they
    point at ONE subject — the third guard on the brick's only hard class."""
    assert ci.direction_conflict(
        "the rial is declining sharply against the dollar",
        "UK offensive support to Ukraine remains unchanged",
    ) is None
    assert ci.direction_conflict(
        "UK offensive support to Ukraine is expanding",
        "UK offensive support to Ukraine remains unchanged",
    ) is not None


async def test_the_accurate_jp_sentence_passes(monkeypatch) -> None:
    """JP CR-3952e5b0 — "the most precise sentence in the read"."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=JP_COMPOSITION_CLEAN, citations=JP_CITATIONS,
        target_id="country_g20_jp",
    )
    assert not (_H2_REASONS & set(_reasons(report))), _reasons(report)


async def test_keeping_the_qualifier_clears_the_check(monkeypatch) -> None:
    """The fix must pass, or the check is not pointing at a fixable thing — and
    the rule it asks for is the one the DESK already obeys."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=JP_COMPOSITION_FIXED, citations=JP_CITATIONS,
        target_id="country_g20_jp",
    )
    assert ci.ABSENCE_SCOPE_LAUNDERED not in _reasons(report)
    # Seen and not charged — the pass side has to be countable, or a quiet
    # violation counter is indistinguishable from a check that never ran.
    assert report.counters["composition_integrity_absence_claims_seen"] == 1


async def test_a_unit_finding_is_byte_identical(monkeypatch) -> None:
    """The whole brick is inert without the ``[[ref:N]]`` sub-claim convention:
    a unit finding's citations resolve to SIGNALS, the evidence map is empty and
    no arm can route. Same body, same words, unit citation shape."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    unit_cites = [{"marker": "[2]", "signal_id": "s-2"}]
    body = JP_COMPOSITION.replace("[[ref:2]]", "[2]")
    report = await verify_finding_faithfulness(
        body=body, citations=unit_cites, target_id="country_g20_jp",
    )
    assert not (_H2_REASONS & set(_reasons(report)))
    assert not any(c.startswith("composition_integrity_") for c in report.counters)


async def test_a_desk_the_composition_does_not_cite_decides_nothing(monkeypatch):
    """An attribution naming a desk that resolves to no cited head is COUNTED and
    dropped, never guessed: putting the wrong head behind an attribution would
    manufacture exactly the defect this brick exists to catch."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body=GB_COMPOSITION, citations=[
            _cite(3, "energy_security", GB_HEAD_ENERGY),
            _cite(9, "proliferation_watch", "**BLUF:** Nothing of note.\n"),
        ],
        target_id="country_g20_gb",
    )
    assert not (_H2_REASONS & set(_reasons(report)))
    assert report.counters["composition_integrity_desk_unresolved"] == 2


# ---------------------------------------------------------------------------
# THE DECLINED CASE — the deterministic/judge split, asserted from both sides.
# ---------------------------------------------------------------------------


async def test_the_ir_inversion_is_declined_by_the_ambivalence_guard() -> None:
    """IR CR-13044838. The composition synthesised a corridor opening as
    "tightening maritime control" — and the cited desk's OWN read holds both
    poles: its BLUF says "tighter control", its What-changed reports the corridor
    and the mine-clearance project. No lexical test can honestly say which one
    the composition inverted, so the arm declines rather than guessing.

    Pinned as a BEHAVIOUR because a future widening of the polarity table would
    otherwise silently start hard-failing an internally ambivalent desk read.
    """
    poles = ci.direction_poles(ci.desk_verdict_text(IR_HEAD_MILITARY))
    assert "tighten" in poles and "open" in poles       # both, one sentence
    assert ci.direction_conflict(IR_COMPOSITION_INVERSION, IR_HEAD_MILITARY) is None
    assert ci.direction_conflict(
        IR_COMPOSITION_INVERSION, ci.desk_verdict_text(IR_HEAD_MILITARY)
    ) is None


def test_the_judge_rubric_carries_what_the_checks_decline() -> None:
    """§2 — what a lexical test cannot decide, the judge is TOLD, by name and
    with the graded sentences. All four shapes, and the IR sentence explicitly
    handed over."""
    rubric = ci.RUBRIC
    assert "SHAPE A. SCOPE DELETED" in rubric
    assert "THIS DESK'S COLLECTION" in rubric and "INFORMATION ENVIRONMENT" in rubric
    assert "SHAPE B. THE DESK CITED FOR ITS OWN NEGATION" in rubric
    assert "remains UNCHANGED" in rubric
    assert "SHAPE C. A CLAIM THE NAMED DESK DOES NOT MAKE" in rubric
    assert "smart-defence" in rubric
    assert "SHAPE D. DIRECTION INVERTED ACROSS THE LAYER" in rubric
    assert "TIGHTENING maritime control" in rubric
    assert "MINE-CLEARANCE" in rubric
    # It must also say what is NOT a violation, or it is a licence to over-fail.
    assert "SUPPORTED" in rubric and "ordinary summarisation" in rubric


def test_the_rubric_reaches_the_composition_judge_prompt() -> None:
    """The rubric is only doctrine if it is in the prompt. Pinned at the wiring:
    verify's composition lead concatenates it, and the UNIT lead does not."""
    import inspect

    import legba.data.provenance.verify as verify

    src = inspect.getsource(verify._run_judge)
    assert "composition_integrity.RUBRIC" in src
    # It sits on the SUB-CLAIM branch only — a unit judge prompt is unchanged.
    head, _, tail = src.partition("composition_integrity.RUBRIC")
    assert "_uses_subclaim_convention(citations)" in head
    assert "UNIT branch" in tail


# ---------------------------------------------------------------------------
# THE DETECTORS, directly — the cases the end-to-end fixtures cannot isolate.
# ---------------------------------------------------------------------------


def test_desk_verdict_is_the_bluf_not_the_whole_body() -> None:
    """Load-bearing, and the reason the direction arm is usable at all: the GB
    head's BODY contains "expanding" (it says the UK is NOT doing it), so a
    polarity test over the whole read would see the composition's own pole in
    the desk and suppress every comparison."""
    assert "expanding" in GB_HEAD_MILITARY
    verdict = ci.desk_verdict_text(GB_HEAD_MILITARY)
    assert verdict.startswith("No material change")
    assert "expanding" not in verdict
    # And the pole it yields is the DIRECTION word "unchanged", not the absence
    # idiom "No material change" beside it — the BF lesson, pinned here too.
    assert ci.direction_poles(verdict) == {"unchanged": "unchanged"}


def test_attribution_shape_survives_the_packets_typography() -> None:
    """The composition prose uses U+2011 non-breaking hyphens inside desk names;
    the desk slug must still resolve to the analyst id."""
    parsed = ci.attributed_clause(
        "The military‑posture read confirms that the UK is increasing support."
    )
    assert parsed is not None
    slug, content = parsed
    assert slug == "military_posture"
    assert content.startswith("the UK is increasing")
    assert ci.attributed_clause("Russian media’s coordinated narrative frames") is None
    assert ci.attributed_clause("Energy prices rose 20 %.") is None


def test_the_ledger_continuity_bullet_is_not_a_desk_negative() -> None:
    """The subtlest thing in this brick, and it broke it in BOTH directions.

    Every desk head opens its ledger with *"No material change since the prior
    read ... which also found no coordinated narrative"*. That sentence matches
    the absence grammar, shares the JP claim's whole subject, and carries no
    collection scope — so read as a desk negative it presented as the desk
    having ALREADY published the claim unscoped, and suppressed the real
    laundering sitting one paragraph above it. V-G2 settled what that shape is:
    a DIFF between two assessments, not a claim about the world. This module
    therefore drops exactly what V-B's router drops, and the two cannot drift.
    """
    sentences = ci.desk_absence_sentences(JP_HEAD_NARRATIVE)
    assert len(sentences) == 1
    assert sentences[0].startswith("**BLUF:** No coordinated narrative")
    assert not any("No material change" in s for s in sentences)
    # And the JP verdict survives it — the guard that suppresses a laundering
    # when a desk said it unscoped is still live, just no longer fooled.
    detail = ci.absence_scope_laundered(
        "**BLUF:** No coordinated narrative appears in Japan’s information "
        "environment in the latest 72‑hour slice [[ref:2]].",
        {2: JP_HEAD_NARRATIVE}, target_id="country_g20_jp",
    )
    assert detail is not None and "coordinated" in detail


def test_a_desk_that_published_it_unscoped_itself_suppresses_the_charge() -> None:
    """The composition cannot launder what the desk already published as a world
    fact — that defect is W31's, one layer up, and charging it twice would make
    the composition pay for the desk's phrasing."""
    unscoped_head = (
        "**BLUF:** No coordinated narrative is evident in Japan's information "
        "environment through 25 August 2026.\n"
    )
    assert ci.absence_scope_laundered(
        "**BLUF:** No coordinated narrative appears in Japan’s information "
        "environment [[ref:2]].",
        {2: unscoped_head}, target_id="country_g20_jp",
    ) is None


def test_a_composition_relaying_the_denial_pays_nothing() -> None:
    """"the internal-stability read reports no unrest in its collection" is the
    correct relay of the same head that condemned the GB sentence."""
    assert ci.asserts_desk_negative(
        "no unrest or protests appear in that desk's collection",
        GB_HEAD_INTERNAL,
    ) is None


def test_the_arms_never_double_charge_one_claim() -> None:
    """One claim, at most one violation — a sentence wrong in two ways costs what
    a sentence wrong in one way costs."""
    claim = (
        "The military‑posture read confirms that the UK is increasing "
        "offensive support, translating its “smart‑defence” doctrine into "
        "policy [[ref:6]]."
    )

    class _Rep:
        counters: dict[str, int] = {}
        checkable_claims = 0
        supported_claims = 0
        unsupported_spans: list = []
        claim_verdicts: list = []
        judge_status = "deterministic"
        judge_unavailable_reason = None
        confidence_ceiling = None
        branch_scores: dict = {}
        score_denominator = None
        score_state = None
        score_state_reason = None

        def bump(self, name, n=1):
            self.counters[name] = self.counters.get(name, 0) + n

    out = ci.fold(
        _Rep(), body=claim,
        citations=[_cite(6, "military_posture", GB_HEAD_MILITARY)],
    )
    assert len(out.unsupported_spans) == 1
    assert out.unsupported_spans[0].reason == ci.ATTRIBUTION_DIRECTION_CONFLICT


def test_the_fold_is_total_on_junk() -> None:
    """Degrade-not-drop: a malformed citation list or body never breaks a pass."""
    class _Rep:
        counters: dict[str, int] = {}
        checkable_claims = 0
        supported_claims = 0
        unsupported_spans: list = []
        claim_verdicts: list = []
        judge_status = "deterministic"
        judge_unavailable_reason = None
        confidence_ceiling = None
        branch_scores: dict = {}
        score_denominator = None
        score_state = None
        score_state_reason = None

        def bump(self, name, n=1):
            self.counters[name] = self.counters.get(name, 0) + n

    for body, cites in (
        ("", None), ("x", []), (JP_COMPOSITION, "not-a-list"),
        (JP_COMPOSITION, [None, 5, {"marker": "[[ref:2]]"}]),
    ):
        assert ci.fold(_Rep(), body=body, citations=cites) is not None


def test_every_counter_this_brick_can_bump_is_declared() -> None:
    """The receipts are enumerable from code (the V-G8 fidelity rule) rather than
    by grepping for ``bump(``."""
    import inspect
    import re as _re

    src = inspect.getsource(ci)
    # Two emission shapes: a direct ``bump("...")`` for the receipts, and a
    # ``counter="..."`` kwarg for the four violations (which ``_fold_soft``
    # bumps, so the literal never appears next to ``bump``).
    bumped = set(_re.findall(r'bump\(\s*"([a-z_]+)"', src))
    bumped |= set(_re.findall(r'counter="(composition_integrity_[a-z_]+)"', src))
    assert bumped == set(ci.COUNTERS), bumped ^ set(ci.COUNTERS)

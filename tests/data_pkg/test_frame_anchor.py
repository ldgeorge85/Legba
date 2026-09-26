# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""R1-a — the amended anchor bar, clause by clause.

The bar is four conjuncts and a cap, and each one is here in isolation because
each one is load-bearing for a DIFFERENT reason: drop the class gate and every
desk grows a ``United Nations`` frame; drop the home exclusion and the loudest
cluster on every desk is a permanent false positive; drop the recurrence bar and
the negative control (JP/North Korea) mints a key; drop the ubiquity ceiling and
``United States`` takes 41 of 92 keys. A test per clause is not thoroughness for
its own sake — it is the only way a later retune can tell WHICH property it just
broke.

Three of these tests are the amendment's pre-registered predictions in fixture
form: the **distinct-finding cliff** (P-2's shape — the bar must separate IL
from JP, and the cliff between K=4 and K=5 is the negative control's), the
**ubiquity ceiling** (P-3), and the **batched matcher's exactness** (P-6). The
live replay that scores all eight is ``scripts/r1a_anchor_replay.py``.
"""
from __future__ import annotations

import ast
import inspect
import random
from datetime import datetime, timedelta, timezone

import asyncpg
import pytest
import pytest_asyncio

from legba.data import _frame_anchor as fa
from legba.data._polity_match import (
    home_prose,
    is_home_country,
    represented_by,
)
from legba.data.config import PostgresConfig

NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)

#: Every country desk the live fleet runs (2026-09-06), ISO2 -> the newsroom
#: spelling the canon produces. The same pin ``test_coverage_floor_scan``
#: carries, for the same reason: the home exclusion is the one clause whose
#: failure makes EVERY desk a permanent false positive.
_FLEET = {
    "AR": "Argentina", "AU": "Australia", "BR": "Brazil", "CA": "Canada",
    "CN": "China", "DE": "Germany", "FR": "France", "GB": "United Kingdom",
    "ID": "Indonesia", "IN": "India", "IT": "Italy", "JP": "Japan",
    "KR": "South Korea", "MX": "Mexico", "RU": "Russia", "SA": "Saudi Arabia",
    "TR": "Turkey", "US": "United States", "ZA": "South Africa",
    "BF": "Burkina Faso", "CD": "Democratic Republic of the Congo",
    "HT": "Haiti", "IL": "Israel", "IR": "Iran", "KP": "North Korea",
    "ML": "Mali", "MM": "Myanmar", "NE": "Niger", "PK": "Pakistan",
    "SD": "Sudan", "TW": "Taiwan", "UA": "Ukraine",
}


def _finding(idx: int, title: str = "", body: str = "", *, day: int = 0):
    """One member row in the live projection's shape (Q-A′)."""
    return {
        "id": f"f{idx:04d}",
        "target_id": "country_g20_jp",
        "analyst_id": "military_posture",
        "title": title,
        "body": body,
        "produced_at": NOW - timedelta(days=day),
    }


def _cfg(**kw):
    return fa.FrameAnchorConfig(**{"min_anchor_days": 0, **kw})


def _desk(scope, quiet: int = 60):
    """A desk pool for the ubiquity denominator.

    ``anchors_for`` REQUIRES one, so every fixture states what the desk is
    rather than letting the scope stand in for it. ``quiet`` rows name no
    polity, so a candidate named in all of ``scope`` still sits far below the
    ceiling and the clause under test is the one the test names.
    """
    return list(scope) + [
        _finding(9000 + i, title="Quiet window",
                 body="Routine tracking of the window.", day=i % 14)
        for i in range(quiet)
    ]


# ---------------------------------------------------------------------------
# CLAUSE 1 — the class gate
# ---------------------------------------------------------------------------


def test_the_class_gate_drops_institutions_and_keeps_polities():
    """``United Nations``, ``European Union`` and ``NATO`` are the three the
    amendment names, and the gate drops them STRUCTURALLY — the canon calls all
    three ``organization`` — rather than by a curated stoplist that would have
    to learn each new institution."""
    gated = fa.candidate_polities(
        ["Iran", "United Nations", "European Union", "NATO", "Palestine"]
    )
    assert gated == ("Iran", "Palestine")


def test_the_gate_keeps_158_of_the_canons_160_keys():
    """Amendment §2.4's live count, pinned. A gate that dropped materially more
    would be refusing real polities; one that dropped fewer would be letting an
    institution through."""
    assert len(fa.DEFAULT_CANDIDATES) == 160
    assert len(fa.candidate_polities()) == 158
    assert set(fa.DEFAULT_CANDIDATES) - set(fa.candidate_polities()) == {
        "European Union", "United Nations",
    }


def test_a_candidate_is_canonicalized_before_it_is_kept():
    """A caller may hand over a surface; what is kept (and what a register line
    prints) is the canonical name, de-duplicated."""
    assert fa.candidate_polities(["US", "USA", "Iranian"]) == (
        "United States", "Iran",
    )


def test_the_class_gate_is_a_knob_but_widening_it_is_a_design_change():
    """``class_gate`` is declared, so it must be READ; design §3.2 refuses to
    widen it in v1 and this test records what widening would admit."""
    orgs = fa.candidate_polities(
        ["NATO", "Iran"], class_gate="organization"
    )
    assert "NATO" in orgs and "Iran" not in orgs


# ---------------------------------------------------------------------------
# CLAUSE 2 — the home exclusion, over all 32 fleet desks
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("iso2,name", sorted(_FLEET.items()))
def test_no_fleet_desk_can_anchor_on_its_own_country(iso2, name):
    """The clause pinned per desk. A desk's own country is in nearly every one
    of its findings; if it survived the gate it would take the top slot on
    every dimension of that desk, for ever."""
    blob = home_prose([iso2])
    assert is_home_country(name, blob), "fixture drift: the blob must self-match"
    assert name not in fa.candidate_polities(home_blob=blob)


@pytest.mark.parametrize(
    "home_iso,foreign",
    [
        # Fold CONTAINMENT gets both of these wrong in the unsafe direction.
        ("NE", "Nigeria"), ("SD", "South Sudan"),
        # And each desk must still see its real second story.
        ("IL", "Iran"), ("TR", "Israel"), ("JP", "North Korea"),
        ("UA", "Russia"), ("SA", "Yemen"),
    ],
)
def test_a_neighbour_survives_the_home_exclusion(home_iso, foreign):
    """The candidate is offered EXPLICITLY, so this measures the exclusion
    clause and not the canon's coverage — the two are separate facts and
    ``test_the_default_candidate_set_is_the_canons_alias_keys`` owns the
    other one."""
    assert foreign in fa.candidate_polities(
        [foreign], home_blob=home_prose([home_iso])
    )


def test_the_default_candidate_set_is_the_canons_alias_keys():
    """AND IT HAS A HOLE, which is recorded here rather than papered over.

    :data:`DEFAULT_CANDIDATES` is every canonical name the shared canon's
    ALIAS and DEMONYM maps produce — 160 of them. A country the canon knows no
    alias or demonym for is therefore not in the default set at all: ``South
    Sudan`` is the live example, and a desk whose second story it was would
    silently never anchor.

    This is not a defect in the bar (``represented_by`` matches it perfectly
    when it is offered) and it is not a reason to hand-keep a second list —
    that is exactly the second-set-of-false-positives ``_polity_match`` exists
    to prevent. It is a reason to prefer amendment §2.6's recommended second
    step: narrow candidates per desk to the polities the coverage floor's
    ``_ENTITY_AGG_SQL`` already nominates from LIVE NER surfaces, which has no
    such hole. R1-b takes that step; R1-a records the cost of not taking it.
    """
    assert "Sudan" in fa.DEFAULT_CANDIDATES
    assert "South Sudan" not in fa.DEFAULT_CANDIDATES
    assert fa.candidate_polities(["South Sudan"]) == ("South Sudan",)
    rows = [_finding(i, body="South Sudan talks stall.", day=i)
            for i in range(6)]
    pool = _desk(rows)
    assert "South Sudan" not in [
        a.polity for a in fa.anchors_for(rows, desk_rows=pool, config=_cfg())
    ]
    named = fa.anchors_for(
        rows, desk_rows=pool, config=_cfg(), candidates=["South Sudan"])
    assert [a.polity for a in named] == ["South Sudan"]


def test_the_home_exclusion_removes_exactly_one_candidate():
    """IL loses Israel and nothing else — the exclusion is a scalpel, and a
    clause that quietly removed neighbours would silently disarm the bar."""
    il = set(fa.candidate_polities(home_blob=home_prose(["IL"])))
    assert set(fa.candidate_polities()) - il == {"Israel"}


# ---------------------------------------------------------------------------
# CLAUSE 3 — the recurrence bar, and THE DISTINCT-FINDING CLIFF
# ---------------------------------------------------------------------------


def test_the_distinct_finding_cliff_is_the_negative_controls_cliff():
    """THE JP SHAPE, and the whole reason the amendment re-based the bar.

    Twenty findings on one dimension, every one of them citing the same handful
    of signals — which is what live findings do, their ``derived_from`` being
    one shared 120-row recency slice — but only FOUR of them actually SAYING
    North Korea. The design's signal bar reads the shared pool and anchors; the
    amended bar reads the authored prose and does not.

    The cliff is between 4 and 5 and it is the negative control's: at K=5 JP
    mints nothing, at K=4 it mints one key. That is what fixes K at 5.
    """
    rows = [
        _finding(i, title="Regional posture review",
                 body="Routine tracking of maritime activity.", day=i % 7)
        for i in range(16)
    ]
    rows += [
        _finding(100 + i, title="Regional posture review",
                 body="North Korea test-fires a ballistic missile.", day=i)
        for i in range(4)
    ]
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(min_anchor_findings=5)) == []
    at_four = fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(min_anchor_findings=4))
    assert [a.polity for a in at_four] == ["North Korea"]
    assert at_four[0].evidence_mass == 4


def test_evidence_mass_counts_DISTINCT_findings_not_mentions():
    """Amendment §2.5. A finding that names the polity nine times is one
    finding; a signal count could not order a register because it is
    desk-uniform, and a mention count would let one voluble analyst mint a
    frame on their own."""
    loud = "Palestine. " * 9
    rows = [_finding(i, body=loud, day=i) for i in range(5)]
    rows.append(_finding(99, body=loud, day=0))
    rows[-1]["id"] = rows[0]["id"]  # the same finding read twice
    anchors = fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())
    assert [(a.polity, a.evidence_mass) for a in anchors] == [("Palestine", 5)]


def test_a_row_with_no_id_still_counts_as_its_own_finding():
    """An unidentifiable row is a fact about our bookkeeping. Silently dropping
    it would under-count and silently merging it would over-count; it counts as
    itself."""
    rows = [_finding(i, body="Palestine advances.", day=i) for i in range(5)]
    for r in rows:
        r["id"] = None
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())[0].evidence_mass == 5


def test_the_bar_reads_title_AND_body_not_title_alone():
    """Amendment §1.4's result, in fixture form: a title-only bar is a NO-OP on
    R-1's founding case, because IL's desks do not write Palestine in their
    heads. Body-only and title-only rows must both count."""
    body_only = [
        _finding(i, title="Escalation review",
                 body="Displacement from Gaza continues across Palestine.",
                 day=i)
        for i in range(5)
    ]
    assert fa.anchors_for(body_only, desk_rows=_desk(body_only), config=_cfg())[0].polity == "Palestine"
    title_only = [
        _finding(i, title="Palestine displacement widens", body="", day=i)
        for i in range(5)
    ]
    assert fa.anchors_for(title_only, desk_rows=_desk(title_only), config=_cfg())[0].polity == "Palestine"


def test_the_body_projection_is_bounded_by_max_body_chars():
    """The bound is applied in the module, not only in R1-b's SQL, so the bar
    cannot quietly depend on which read produced its rows."""
    rows = [
        _finding(i, title="Review", body=("x" * 500) + " Palestine", day=i)
        for i in range(5)
    ]
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(max_body_chars=6000))
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(max_body_chars=100)) == []


# ---------------------------------------------------------------------------
# CLAUSE 4 — D-t, the ubiquity ceiling
# ---------------------------------------------------------------------------


def _desk_with_boilerplate(n_dimension: int = 8, n_desk: int = 40):
    """A desk where ``United States`` is wire boilerplate (95%) and ``Yemen``
    is a real second story (~30%) — the live shape amendment §2.2 measured."""
    desk = []
    for i in range(n_desk):
        body = "The United States responded."
        if i % 3 == 0:
            body += " Yemen's ports remain contested."
        desk.append(_finding(i, title="Desk read", body=body, day=i % 14))
    scope = desk[:n_dimension]
    return scope, desk


def test_a_ubiquitous_polity_never_takes_a_key():
    """D-t, and it is not optional: without it ``United States`` anchors 41 of
    92 minted keys and takes the top slot on nearly every dimension of every
    desk. A story every dimension already carries is not a missing second
    story."""
    scope, desk = _desk_with_boilerplate()
    anchors = fa.anchors_for(scope, desk_rows=desk, config=_cfg())
    assert "United States" not in [a.polity for a in anchors]


def test_the_real_second_story_survives_the_same_ceiling():
    """The ceiling must refuse boilerplate WITHOUT refusing the thing the
    repair exists to find. UA/Russia at 68% is the live closest approach
    (amendment F-10)."""
    scope, desk = _desk_with_boilerplate()
    anchors = fa.anchors_for(scope, desk_rows=desk, config=_cfg(
        min_anchor_findings=3))
    assert [a.polity for a in anchors] == ["Yemen"]
    assert anchors[0].desk_ubiquity == pytest.approx(0.35, abs=0.02)


def test_the_ceiling_is_strictly_below_which_is_r1ds_reading_too():
    """``share < ceiling`` PASSES. It matters that this matches
    ``_frame_content.mint_floor_twin``'s ``>= _TWIN_MAX_UBIQUITY`` drop: R1-d's
    pre-flip twin and this bar must select the SAME population or the post-flip
    p50 comparison is a change of denominator wearing the name of a change in
    the world."""
    desk = [_finding(i, body="Palestine.", day=i % 14) for i in range(4)]
    desk += [_finding(100 + i, body="quiet", day=i) for i in range(1)]
    # 4 of 5 = 0.8
    assert fa.anchors_for(desk[:4], desk_rows=desk,
                          config=_cfg(min_anchor_findings=4,
                                      max_anchor_ubiquity=0.8)) == []
    assert fa.anchors_for(desk[:4], desk_rows=desk,
                          config=_cfg(min_anchor_findings=4,
                                      max_anchor_ubiquity=0.81))


def test_the_ubiquity_denominator_is_the_DESK_not_the_scope():
    """Amendment P-3's named failure mode is "ubiquity on the wrong
    denominator". A polity that saturates one dimension but is rare on the desk
    ANCHORS; the same counts read against the dimension alone would refuse
    it."""
    scope = [_finding(i, body="Yemen escalates.", day=i % 14) for i in range(6)]
    desk = scope + [
        _finding(100 + i, body="unrelated", day=i % 14) for i in range(60)
    ]
    on_desk = fa.anchors_for(scope, desk_rows=desk, config=_cfg())
    assert [a.polity for a in on_desk] == ["Yemen"]
    assert on_desk[0].desk_ubiquity == pytest.approx(6 / 66, abs=1e-4)
    # Read against the scope alone (6 of 6 = 1.0) it would be refused.
    assert fa.anchors_for(scope, desk_rows=scope, config=_cfg()) == []


def test_a_refused_anchor_does_not_consume_a_slot():
    """The order of operations, which is load-bearing. §2.1 makes every clause
    a conjunct of ``anchor`` and applies the cap to the SURVIVORS; the
    appendix's Q-A′ prose lists the cap first, which would let boilerplate
    occupy a slot on nearly every dimension and then be dropped from it."""
    scope, desk = _desk_with_boilerplate()
    for i, row in enumerate(scope):
        row["body"] += " Qatar and Lebanon and Syria are named."
    anchors = fa.anchors_for(scope, desk_rows=desk, config=_cfg(
        min_anchor_findings=3, max_anchors_per_scope=3))
    assert len(anchors) == 3
    assert "United States" not in [a.polity for a in anchors]


# ---------------------------------------------------------------------------
# The cap and the ordering
# ---------------------------------------------------------------------------


def test_anchors_are_capped_worst_first_by_evidence_mass():
    """"Worst" is the coverage floor's own sense of it — the biggest thing
    nobody is carrying. Ties break on ``anchor_days`` then on the name, so the
    same rows give the same key set in a different process."""
    rows = []
    for n, polity in ((9, "Qatar"), (7, "Lebanon"), (6, "Syria"), (5, "Iran")):
        rows += [
            _finding(len(rows) + i, body=f"{polity} is named.", day=i % 14)
            for i in range(n)
        ]
    anchors = fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(max_anchors_per_scope=3))
    assert [a.polity for a in anchors] == ["Qatar", "Lebanon", "Syria"]
    assert [a.evidence_mass for a in anchors] == [9, 7, 6]


def test_a_zero_cap_mints_nothing_and_does_not_raise():
    rows = [_finding(i, body="Palestine.", day=i) for i in range(9)]
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(max_anchors_per_scope=0)) == []


# ---------------------------------------------------------------------------
# The days clause and the clock (D-k)
# ---------------------------------------------------------------------------


def test_min_anchor_days_counts_distinct_authoring_days():
    """Amendment §2.3 re-bases the knob onto distinct ``produced_at`` days
    among the naming findings. Five findings on two days do not clear a clause
    about spread."""
    same_day = [_finding(i, body="Palestine.", day=1) for i in range(6)]
    assert fa.anchors_for(same_day, desk_rows=_desk(same_day), config=fa.FrameAnchorConfig()) == []
    spread = [_finding(i, body="Palestine.", day=i) for i in range(6)]
    assert fa.anchors_for(spread, desk_rows=_desk(spread), config=fa.FrameAnchorConfig())


def test_an_undated_row_contributes_no_day():
    """The strict direction: a row that cannot say when it was written cannot
    testify to recurrence over time."""
    rows = [_finding(i, body="Palestine.", day=i) for i in range(6)]
    for r in rows:
        r["produced_at"] = None
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=fa.FrameAnchorConfig()) == []
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())[0].anchor_days == 0


def test_newest_evidence_at_is_an_input_and_never_produced_at():
    """D-k. The evidence clock is a WORLD clock the product cannot wind.
    Dropping the signal walk from the bar does not license substituting the
    finding's own ``produced_at`` — that is the M-1 bookkeeping-for-evidence
    defect, and it would be invisible in the output."""
    rows = [_finding(i, body="Palestine.", day=i) for i in range(6)]
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())[0].newest_evidence_at is None
    clock = datetime(2026, 9, 6, 8, 11, 26, tzinfo=timezone.utc)
    carried = fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg(), newest_evidence_at={"Palestine": clock},
    )[0]
    assert carried.newest_evidence_at == clock
    assert carried.newest_evidence_at not in {r["produced_at"] for r in rows}


# ---------------------------------------------------------------------------
# THE FOLD — one site, MECH-6
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "Palestine", "palestine", "PALESTINE", "Palestinians",
        "Palestine's borders", "Palestine’s borders",
        "Palestine.", "(Palestine)", "—Palestine—",
        "Pa­lestine",  # SOFT HYPHEN, which the fold deletes
    ],
)
def test_the_fold_reads_every_spelling_as_the_same_naming(written):
    """Every text enters through ``_polity_match.normalize_prose``, which folds
    at the one MECH-6 site. No second matcher and no new alias map lives here."""
    rows = [_finding(i, body=f"Report: {written}", day=i) for i in range(5)]
    assert [a.polity for a in fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())] == [
        "Palestine"
    ]


def test_diacritics_and_case_fold_to_the_same_polity():
    rows = [_finding(i, body="Türkiye and TURKEY", day=i) for i in range(5)]
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())[0].polity == "Turkey"


def test_a_curated_proxy_is_still_refused():
    """``Houthi -> Yemen`` stays refused (amendment §2.1): SA/Yemen anchors on
    the WORD Yemen appearing in SA's own findings, not on a proxy somebody
    curated."""
    rows = [_finding(i, body="Houthi forces struck.", day=i) for i in range(9)]
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg()) == []


# ---------------------------------------------------------------------------
# THE BATCHED MATCHER — P-6
# ---------------------------------------------------------------------------


def _live_shaped_rows(n: int = 220):
    """Rows with the live corpus's shape: ~1.8 kB bodies, a citation block, a
    long tail of polities, and prose that names nothing at all."""
    rng = random.Random(20260906)
    polities = [
        "Palestine", "Iran", "Lebanon", "Syria", "Qatar", "Turkey", "Russia",
        "Ukraine", "Yemen", "North Korea", "United States", "China", "Egypt",
        "Jordan", "Saudi Arabia", "Pakistan", "Iraq", "Israel", "France",
    ]
    filler = (
        "Analysts assess that the trajectory remains contested. Reporting "
        "across the window is uneven and the picture is partial. "
    )
    rows = []
    for i in range(n):
        named = rng.sample(polities, rng.randint(0, 4))
        surfaces = []
        for p in named:
            surfaces.append(
                rng.choice([p, p.upper(), p.lower(), f"{p}'s posture",
                            f"{p}’s posture"])
            )
        body = filler * rng.randint(4, 9) + " ".join(surfaces)
        body += " [1] AFP, [2] Reuters, [3] Bloomberg."
        rows.append(_finding(
            i, title=rng.choice(["Escalation review", f"{named[0]} in focus"
                                 if named else "Quiet window"]),
            body=body, day=i % 14,
        ))
    return rows


def test_batched_matcher_agrees_with_represented_by():
    """P-6, on 220 live-shaped rows against the whole gated candidate set.

    A nested ``represented_by`` loop is the reference implementation and it is
    the thing the fleet cannot afford (13.26 s over 254 IL findings, ≈823 s/run
    fleet-wide). The batched path must return EXACTLY its answer — not nearly,
    and not "in the cases we thought of" — or the amendment's §2.6 speedup is
    bought with a behaviour change nobody measured.
    """
    rows = _live_shaped_rows(220)
    candidates = fa.candidate_polities(home_blob=home_prose(["IL"]))
    index = fa.PolitySurfaceIndex(candidates)

    batched = {
        (polity, key)
        for polity, keys in fa.naming_findings(
            rows, index, max_body_chars=6000).items()
        for key in keys
    }
    reference = set()
    for row in rows:
        prose = fa.finding_prose(row, max_body_chars=6000)
        for polity in candidates:
            if represented_by(polity, prose) is not None:
                reference.add((polity, row["id"]))

    assert batched == reference
    assert reference, "fixture drift: the reference found nothing to compare"


def test_the_batched_anchor_set_equals_the_nested_loop_anchor_set():
    """P-6 one level up: not just the hits, the ANCHORS. A matcher that agreed
    on mentions but disagreed on the distinct-finding count would still mint a
    different register."""
    rows = _live_shaped_rows(220)
    candidates = fa.candidate_polities(home_blob=home_prose(["IL"]))
    cfg = _cfg()

    reference: dict[str, set[str]] = {}
    for row in rows:
        prose = fa.finding_prose(row, max_body_chars=cfg.max_body_chars)
        for polity in candidates:
            if represented_by(polity, prose) is not None:
                reference.setdefault(polity, set()).add(row["id"])
    expected = sorted(
        (p, len(f)) for p, f in reference.items()
        if len(f) >= cfg.min_anchor_findings
        and len(f) / len(rows) < cfg.max_anchor_ubiquity
    )
    expected = sorted(expected, key=lambda x: (-x[1], x[0]))[:3]

    anchors = fa.anchors_for(
        rows, desk_rows=rows, config=cfg, candidates=candidates)
    assert [(a.polity, a.evidence_mass) for a in anchors] == expected


def test_the_index_exposes_its_candidates_in_order():
    index = fa.PolitySurfaceIndex(["Iran", "Iran", "Palestine"])
    assert index.names == ("Iran", "Palestine")


def test_names_in_folded_lets_a_caller_pay_the_fold_once():
    from legba.data._polity_match import normalize_prose

    index = fa.PolitySurfaceIndex(["Palestine", "Iran"])
    prose = "Palestine and Iranian proxies"
    assert index.names_in(prose) == index.names_in_folded(
        normalize_prose(prose))


# ---------------------------------------------------------------------------
# THE SIGNATURE SLOT — token, idempotence, counterfeits
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "polity,token",
    [
        ("Palestine", "palestine"),
        ("North Korea", "north_korea"),
        ("United States", "united_states"),
        ("  Côte d'Ivoire  ", "c_te_d_ivoire"),
        ("Democratic Republic of the Congo",
         "democratic_republic_of_the_congo"),
        ("", "_domestic"),
        (None, "_domestic"),
        ("   ", "_domestic"),
        ("---", "_domestic"),
    ],
)
def test_anchor_token_slugs(polity, token):
    assert fa.anchor_token(polity) == token


@pytest.mark.parametrize(
    "counterfeit",
    ["a#dim:b", "a|b", "x#evt:y", "sig:fake#dim:d#evt:e", "a#|#b"],
)
def test_the_token_cannot_counterfeit_a_separator(counterfeit):
    """The one drift that would silently undo the repair is a key that parses
    differently than it was written. ``#`` and ``|`` are both non-slug, so no
    polity name — however hostile — can spell a marker into the key."""
    token = fa.anchor_token(counterfeit)
    assert "#" not in token and "|" not in token and ":" not in token
    sig = fa.with_anchor("sig:topic#dim:military_posture", counterfeit)
    assert sig.count(fa._SIGNATURE_EVENT_MARKER) == 1
    assert fa.signature_anchor(sig) == token


def test_the_token_is_capped_and_never_ends_in_a_separator():
    token = fa.anchor_token("x" * 40 + " " + "y" * 40)
    assert len(token) == 64
    assert not token.endswith("_")


def test_with_anchor_is_idempotent():
    """Safe on a row of unknown vintage, which is how a half-migrated fleet
    would use it."""
    once = fa.with_anchor("sig:topic#dim:escalation", "Palestine")
    assert fa.with_anchor(once, "Iran") == once
    assert fa.with_anchor(once) == once


def test_with_anchor_leaves_an_explicit_sit_key_alone():
    """Mirrors ``with_dimension``: an explicit key is handed to the clusterer
    by its own producer and already carries that producer in its text."""
    assert fa.with_anchor("sit:composition:ar", "Russia") == "sit:composition:ar"
    assert fa.with_anchor("", "Russia") == ""


def test_no_anchor_mints_the_domestic_residue():
    """D-l's residue is a real frame, not an empty shell, so the flag-off /
    no-anchor path is the SAME call with the same idempotence."""
    sig = fa.with_anchor("sig:topic#dim:energy_security")
    assert sig.endswith("#evt:_domestic")
    assert fa.signature_anchor(sig) is None


def test_signature_anchor_has_exactly_one_owner():
    """``_frame_content`` shipped this parser with R1-d; a second spelling of
    "what anchor does this key carry" is a second set of answers."""
    from legba.data import _frame_content

    assert fa.signature_anchor is _frame_content.signature_anchor
    assert fa.DOMESTIC_ANCHOR is _frame_content.DOMESTIC_ANCHOR


# ---------------------------------------------------------------------------
# CONFIG — 7 knobs, env then option
# ---------------------------------------------------------------------------


_KNOB_PROBES = {
    "min_anchor_findings": ("7", 7),
    "max_anchor_ubiquity": ("0.5", 0.5),
    "max_anchors_per_scope": ("2", 2),
    "window_days": ("21", 21),
    "min_anchor_days": ("3", 3),
    "max_body_chars": ("900", 900),
    "class_gate": ("organization", "organization"),
}


def test_the_knob_surface_is_exactly_seven():
    """Amendment §2.3. ``min_anchor_signals``, ``min_anchor_share`` and
    ``min_entity_confidence`` retired with the signal walk — the bar reads no
    signals and no NER spans, and ``min_anchor_share`` was measured INERT."""
    names = set(fa.FrameAnchorConfig.__dataclass_fields__)
    assert names == set(_KNOB_PROBES)
    assert len(names) == 7
    for retired in (
        "min_anchor_signals", "min_anchor_share", "min_entity_confidence",
    ):
        assert retired not in names


def test_the_defaults_are_the_amendments():
    cfg = fa.FrameAnchorConfig()
    assert cfg.min_anchor_findings == 5
    assert cfg.max_anchor_ubiquity == 0.75
    assert cfg.max_anchors_per_scope == 3
    assert cfg.window_days == 14
    assert cfg.min_anchor_days == 5
    assert cfg.max_body_chars == 6000
    assert cfg.class_gate == "country"


@pytest.mark.parametrize("knob", sorted(_KNOB_PROBES))
def test_env_sets_the_knob_and_the_option_wins_over_env(knob, monkeypatch):
    """The house order, per knob: env is the base default, a descriptor option
    always wins. A knob that only reads one of the two is half-dead config."""
    raw, parsed = _KNOB_PROBES[knob]
    monkeypatch.setenv(fa.ENV_PREFIX + knob.upper(), raw)
    assert getattr(fa.config_from_options({}), knob) == parsed
    other = _KNOB_PROBES[knob][1]
    override = (
        "country" if knob == "class_gate"
        else (other + 1 if isinstance(other, int) else other + 0.1)
    )
    assert getattr(
        fa.config_from_options({fa.OPTION_PREFIX + knob: override}), knob
    ) == override


@pytest.mark.parametrize("knob", sorted(_KNOB_PROBES))
def test_every_knob_is_reachable_through_the_family_reader(knob):
    """The X-1 proof shape, run here because ``_frame_anchor`` is not yet a
    handler: the family's reader is CALLED with a probe and the value must land
    on the config it produces. R1-b declares the family in
    ``handler_options.py`` when it binds the bar; declaring it before anything
    reads it would put seven knobs in the catalog that cannot move anything."""
    _, parsed = _KNOB_PROBES[knob]
    got = fa.config_from_options({fa.OPTION_PREFIX + knob: parsed})
    assert getattr(got, knob) == parsed


def test_a_mistyped_knob_degrades_the_bar_and_never_raises(monkeypatch, caplog):
    """The ``CoverageFloorConfig._coerce`` contract: a bad value keeps the
    predecessor and says so. A mistyped knob must not take the clusterer
    offline."""
    monkeypatch.setenv(fa.ENV_PREFIX + "MIN_ANCHOR_FINDINGS", "not-a-number")
    with caplog.at_level("WARNING"):
        cfg = fa.config_from_options({})
    assert cfg.min_anchor_findings == 5
    assert "bad_option" in caplog.text


def test_no_options_means_the_defaults():
    assert fa.config_from_options(None) == fa.FrameAnchorConfig()
    assert fa.config_from_options({}) == fa.FrameAnchorConfig()


# ---------------------------------------------------------------------------
# Degenerate inputs
# ---------------------------------------------------------------------------


def test_an_empty_scope_anchors_on_nothing():
    assert fa.anchors_for([], desk_rows=[], config=_cfg()) == []
    assert fa.anchors_for(
        [_finding(0)], desk_rows=[_finding(0)], candidates=[], config=_cfg()
    ) == []


def test_the_desk_pool_has_no_default_and_must_be_stated():
    """THE DENOMINATOR IS NOT OPTIONAL, and neither available default is safe.

    Falling back to ``scope_rows`` makes the ceiling measure a dimension's
    share of itself — every anchor the scope establishes reads as 100%
    ubiquitous, so the bar over-refuses while D-t appears to work. Skipping the
    clause when no pool is given loses D-t altogether and lets ``United
    States`` take 41 of 92 keys. Both are invisible in the output, so the
    caller states the desk or gets a ``TypeError``: amendment P-3's falsifier
    is "ubiquity on the wrong denominator", and this is the clause that makes
    the wrong denominator unreachable rather than merely discouraged.
    """
    rows = [_finding(i, body="Palestine.", day=i) for i in range(9)]
    with pytest.raises(TypeError):
        fa.anchors_for(rows, config=_cfg())  # type: ignore[call-arg]
    # And stated wrongly (the scope as its own desk) it refuses, loudly in the
    # only way a pure function can: it mints nothing.
    assert fa.anchors_for(rows, desk_rows=rows, config=_cfg()) == []
    assert fa.anchors_for(rows, desk_rows=_desk(rows), config=_cfg())


# ---------------------------------------------------------------------------
# THE FENCE — D-n, narrowed by amendment §2.4
# ---------------------------------------------------------------------------


def test_frame_anchor_has_no_llm_and_no_model_labels():
    """D-n's AST guard, NARROWED by amendment §2.4, and the narrowing is the
    point of the docstring.

    The LLM ban is unchanged and unchangeable: the whole repair is that a frame
    stops being a model's label for itself, so a bar that asked a model what a
    frame was about would have conceded the argument before measuring it. The
    five PRODUCER SELF-DESCRIPTION keys — ``data.key_entities`` and its
    ``entities`` / ``actors`` / ``locations`` / ``geo`` siblings — stay
    forbidden for the same reason: they are the producer's structured account
    of itself.

    ``title`` and ``body`` are NOT banned, and the original D-n banned the
    title. It was narrowed because the ban was measured to be fatal: at MINT
    grain on titles alone IL/Palestine anchors on NOTHING at every threshold
    (amendment §1.4), K=2 included — a title-only anchor is a no-op on R-1's
    founding case, since the repair exists precisely because IL's desks do not
    write Palestine in their heads. A finding's title and body are the
    analyst's authored prose ABOUT THE WORLD; under the QUOTATION regime the
    desk sentence is the product. That is a different kind of thing from a
    model's structured self-description, and the substrate the original D-n
    presumed instead — the signal-entity walk — is desk-uniform by
    construction and cannot describe a frame at all.
    """
    tree = ast.parse(inspect.getsource(fa))
    imported: set[str] = set()
    consts: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(a.asname or a.name.split(".")[0] for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.update(node.module.split("."))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            consts.append(node.value)
    for banned in (
        "llm", "dspy", "litellm", "openai", "anthropic", "actor_critic",
        "prompts", "prompt_registry", "verify", "judge",
    ):
        assert banned not in imported, (
            f"the anchor bar imports {banned!r} — it reads authored prose, "
            "and a model has no part in deciding what a frame is about"
        )
    for banned in ("key_entities", "entities", "actors", "locations", "geo"):
        assert banned not in consts, (
            f"the bar names {banned!r} — that is the producer's structured "
            "self-description, which D-n forbids the mint path to read"
        )
    # And the narrowing is affirmative: the substrate IS title + body.
    source = inspect.getsource(fa)
    assert '"title"' in source and '"body"' in source


def test_the_bar_reads_no_key_the_projection_does_not_carry():
    """The STRUCTURAL half of the fence, and the stronger one. Whatever any
    future edit says above, the bar can only read the five keys it actually
    asks a row for."""
    seen: set[str] = set()

    class _Row(dict):
        def get(self, key, default=None):
            seen.add(key)
            return super().get(key, default)

    rows = [_Row(_finding(i, body="Palestine.", day=i)) for i in range(6)]
    fa.anchors_for(rows, desk_rows=rows, config=_cfg())
    assert seen <= {"id", "title", "body", "produced_at"}


def test_nothing_in_production_imports_the_anchor_module():
    """R1-a is a PURE ADDITION and this is the test that says so. Nothing under
    ``src/legba`` may import it in this train: the flag it will ride
    (``LEGBA_REGISTER_CONTENTFUL_FRAMES``) does not exist yet, and a module
    that were already wired in would make the design's §3.4 byte-identity table
    a claim rather than a fact.

    When R1-b lands, THIS test is the one that changes — deliberately, in the
    same diff as the import.
    """
    import pathlib

    root = pathlib.Path(fa.__file__).resolve().parents[3] / "legba"
    importers = sorted(
        str(p.relative_to(root))
        for p in root.rglob("*.py")
        if p.name != "_frame_anchor.py" and "_frame_anchor" in p.read_text(
            encoding="utf-8")
    )
    assert importers == [], (
        f"production modules reference _frame_anchor: {importers} — R1-a is a "
        "pure addition; the binding is R1-b's train"
    )


# ---------------------------------------------------------------------------
# THE SQL TWIN
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture
async def pool(migrated_pg: PostgresConfig):
    p = await asyncpg.create_pool(migrated_pg.dsn, min_size=1, max_size=2)
    yield p
    await p.close()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_anchor_token_python_and_sql_agree(pool):
    """THE DRIFT THAT WOULD SILENTLY UNDO THE REPAIR, one marker later.

    Migration 0193 appends ``#evt:_domestic`` in Postgres and every live tick
    computes the token in Python. A value the two spell differently does not
    fail loudly — it produces a SECOND frame for the same anchor under
    ``uq_situations_signature_analyst``, the exact duplicate-frame outcome the
    re-key exists to remove. So the twin is asserted row-for-row, including on
    inputs designed to break either implementation.

    Read-only: one ``SELECT`` per case, no table touched.
    """
    cases = [
        "Palestine", "North Korea", "United States", "Turkey", "Türkiye",
        "Côte d'Ivoire", "Democratic Republic of the Congo", "Timor-Leste",
        "Bosnia and Herzegovina", "  Palestine  ", "PALESTINE", "MiXeD CaSe",
        "has#hash", "has|pipe", "both#and|", "#evt:counterfeit",
        "", "   ", "---", "\tleading_tab", "x" * 80, "a-b.c_d",
        "São Tomé and Príncipe", "Åland", "ünïcödé",
    ]
    stmt = f"SELECT {fa.ANCHOR_TOKEN_SQL}"
    async with pool.acquire() as conn:
        for raw in cases:
            got = await conn.fetchval(stmt, raw)
            assert got == fa.anchor_token(raw), (
                f"token drift on {raw!r}: SQL={got!r} "
                f"Python={fa.anchor_token(raw)!r}"
            )
        assert await conn.fetchval(stmt, None) == fa.anchor_token(None)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_the_sql_twin_agrees_over_the_whole_canon(pool):
    """Every polity the bar can actually mint, not only the awkward ones."""
    stmt = f"SELECT {fa.ANCHOR_TOKEN_SQL}"
    async with pool.acquire() as conn:
        for name in fa.candidate_polities():
            assert await conn.fetchval(stmt, name) == fa.anchor_token(name)

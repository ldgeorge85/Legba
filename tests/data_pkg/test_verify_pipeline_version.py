# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The JUDGE PIPELINE VERSION stamp (2026-07-31) — the population SPLIT key.

The verify gate is the product's keystone, so every structural change to it
ships behind ONE version stamp on the critique (the MATCHER_VERSION idiom).
Anything reading faithfulness history — band calibration, the gold-set loop, the
correctness scorer, the scorecard, the two-panel readout's own dossier query —
partitions on it, so critiques graded under different verify pipelines are never
POOLED.

That mattered for the 2026-07-31 train (V-F + V-C + V-D + V-B were expected to
shift mean faithfulness UPWARD, a MEASUREMENT CORRECTION rather than findings
getting better), and it matters differently for the 2026-08-02 F-A PRECISION
train, where the shift is NOT one-way: hard-fail COUNT falls sharply while mean
faithfulness may dip slightly, because W1's tighter route withdraws the V-B
supported overrides that were certifying claims the slice check did not cover.
Fewer false hard fails AND fewer unearned passes is the intended shape — and
only the split key makes it legible as that instead of as a quality movement.
"""

from __future__ import annotations

import re
from uuid import uuid4

from legba.data.provenance.models import CritiquePayload
from legba.data.provenance.verify import (
    JUDGE_PIPELINE_VERSION,
    build_faithfulness_critique_payload,
    verify_finding_faithfulness,
)


def test_version_value_and_shape() -> None:
    """ONE bump per train, ``<train date>/<n>`` — the [N+1] TRANSPARENCY train
    shipping WITH its consumer repair (tasks #62 + #78).

    Bumped from ``2026-08-29/1`` (LRF) for a change whose EXPECTED SHIFT IS
    NONE — the only such entry in the lineage, and the reason the stamp is worth
    spending anyway is that "nothing moved" is a claim someone has to be able to
    check.

    THE STAMP WAS REASSIGNED, and the pin says so rather than quietly holding a
    different number than the branch it was proven on. This train was built and
    proven claiming ``2026-08-29/1``, then held; ``2026-08-29/1`` went to the
    in-flight length-response train, and this one re-stamped to
    ``2026-08-30/1``. Nothing about the proof changed with the number.

    IT IS NOW WORTH SPENDING, which it was not before. Held on its own branch
    this train fixed no measured defect; it ships here with the repair of the
    two consumers that DO misread the convention — ``export_api``, which stripped
    every non-signal citation from exported documents (and read the wrong
    nesting level, so it stripped ALL of them), and the v3 UI's
    ``citationsModel``, which rendered three grounding kinds as "Unresolved
    citation" over SUPPORTED evidence. Both now key on the ``marker_class`` /
    ``resolves_against`` marks this train adds. Neither consumer is in the
    grading path, so neither can move a verdict.

    THE MOTIVATION WAS RETRACTED BEFORE THE TRAIN SHIPPED, and the docstring
    says so because a pin that repeats a falsified number teaches it. The
    2026-08-27 DQ sweep called the ``[N+1]`` prior-read self-citation a 53.6%
    citation RED; sweep v2 (2026-08-29) showed the baseline had resolved markers
    against ``analyst_traces.input_row_refs``, a ``uuid[]`` of substrate row ids
    that cannot hold a grounding block. Re-measured against
    ``data->'data'->'citations'``: 0 of 6,556 markers unresolved, 0 of 1,079
    findings affected. There was no resolution debt.

    WHAT SHIPPED is legibility only: ``(prior read ref N)`` becomes a recognized
    marker syntax (one definition in ``citation_markers``, applied at BOTH write
    and verify time), and every desk-grounding citation carries
    ``marker_class`` / ``resolves_against`` so the row states which set its
    ordinal indexes instead of requiring the reader to know
    ``GROUNDING_REF_KINDS``. No reason string is added, renamed or
    reclassified; the fail-class table is untouched.

    EXPECTED SHIFT: NONE, proven — a 50-finding frozen corpus (325 citations, 23
    ``prior_read``, 201 graded claims) replays byte-identical per-claim verdicts
    across the change. The one uncovered surface is the prior-read block's
    rendered header, which gains a line and is ``evidence_text`` for its
    citation: the deterministic path reads that field for PRESENCE only, but the
    LLM judge sees the bytes. That is what this boundary is for.

    PRIOR STAMP, kept because the boundary is what makes it legible — LRF
    (``2026-08-29/1``), the LENGTH-RESPONSE FLATTENING plus the V-J1 U+2011
    activation: the verify pass changed what is IN THE DENOMINATOR — three
    rungs of ``_is_fact_asserting`` granted WHOLE-SPAN exemptions on a prefix
    (``_SYNTHESIS_PREFIXES``), a label (``is_assessment_scaffold``) or a
    substring (``_is_absence_claim``); each now EARNS its exemption clause by
    clause (+6,772 spans, 0 removed), and the archived hedged-conflict
    specimens replay 0/7 as shipped, 4/7 with the fold (pinned in
    ``test_verify_hedged_conflict.py``). Full prose in the module lineage.

    PRIOR-PRIOR STAMP — V-J, the HEDGED-CONFLICT train, bumped from ``2026-08-27/1`` (H1 + H2) because the
    verify pass gained one new counted soft reason and one prompt change, on two
    different routes:

      * V-J1, the DISCLOSED-AND-DOWNWEIGHTED CONFLICT guard. A composition
        sentence of the shape "a WEAKLY-SUPPORTED read says no-X, which CONFLICTS
        WITH the VERIFIED finding of X" was hard-failed by a row resolving to the
        same weak side the sentence already named and rejected — three times in
        the 08-27 census's 13-item sample, plus once on the generic route. The
        guard binds on BOTH: a new ``hedged_conflict`` V-B route class and a new
        soft reason ``judge_contradicted_hedged_conflict`` in the judge severity
        chain, ahead of V-I5 (whose scope-qualifier gate the generic-route
        specimen does not pass).
      * V-J2, two DOMAIN-COLLISION few-shots in the V-B stage-2 system prompt —
        the sanctions-type and civilian/military-procurement over-fires the same
        census itemized, which no lexical test can decide.

    V-J EXPECTED SHIFT: the hard/soft SPLIT moves and the score does not. V-J1 is a
    DEMOTION — the claim still fails, only the severity moves — so
    ``faithfulness_score`` cannot move because of it at all; V-J2 is the only arm
    that can withdraw a failure outright. Read
    ``hardfail_demoted_hedged_conflict`` (generic route) and
    ``absence_slice_route_excluded_hedged_conflict`` (absence route) as separate
    counters: they are separate levers on one defect.

    PRIOR STAMP, kept because the boundary is what makes it legible — H1, the
    REGISTER SELF-CORROBORATION guard, bumped from ``2026-08-25/1`` (#58, V-B
    title parity) because the verify pass
    now emits one new counted soft reason, ``register_self_corroboration``: a
    fact-asserting claim whose resolved citations are ALL
    ``ref_kind='situation_register'`` and which asserts CURRENCY or
    CORROBORATION about the world. This is CORRECTNESS-R2's largest
    single-mechanism ``inaccurate`` mass — the desks write "no material change"
    into the situation register, the register reports that back as intensity and
    recency, and the desks cite it as confirmation the event is live.

    EXPECTED SHIFT: narrow and one-directional. The guard is INERT on any
    finding that carries no register citation (most of the fleet); on findings
    that DO cite it, it can only ADD soft spans. Split on this stamp and read
    the new reason against the register-citing population ONLY — pooling it with
    non-citing findings dilutes exactly what it measures. See
    ``judge_pipeline_version.py``'s lineage entry for the full reasoning.

    SAME STAMP, SECOND TRAIN — H2, COMPOSITION INTEGRITY (they deploy
    together, one bump). Also from ``2026-08-25/1`` because the
    composition-integrity brick lands two changes that each move the
    composition population on their own: FOUR new span reasons folded into the
    faithfulness denominator (``absence_scope_laundered``,
    ``attribution_direction_conflict``, ``attribution_asserts_desk_negative``,
    ``attribution_ungrounded_quote``), and an additive block on the COMPOSITION
    JUDGE LEAD naming the four cross-layer failure shapes.

    EXPECTED SHIFT: composition-only and one-directional — each violation is one
    more checkable-but-unsupported claim, so an affected composition's
    faithfulness_score can only FALL; unit findings are inert by construction
    (no ``[[ref:N]]`` convention, empty evidence map, every arm a no-op).
    ``unscoped_absence_claim`` must NOT move: W31 and the scope-laundering arm
    are disjoint by construction, and a rise there means the two have started
    double-charging one span. See ``judge_pipeline_version.py``'s full lineage
    entry for the reasoning this split key exists to keep legible.
    """
    assert JUDGE_PIPELINE_VERSION == "2026-08-30/1"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}/\d+", JUDGE_PIPELINE_VERSION)


async def test_stamped_on_every_critique(monkeypatch) -> None:
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(
        body="Alpha struck Bravo base on Monday [1].\n",
        citations=[{"marker": "[1]", "signal_id": str(uuid4())}],
    )
    payload = build_faithfulness_critique_payload(report, analyzed_output_id=uuid4())
    verification = payload["data"]["verification"]
    assert verification["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    # The block still validates as a CritiquePayload (extra='forbid' at the top
    # level; ``data`` is open JSONB, which is where the stamp lives).
    CritiquePayload.model_validate(payload)


async def test_stamped_on_the_trace_envelope_too(monkeypatch) -> None:
    """``report.as_dict()`` is what the actor returns into the run trace — it
    records which verify pipeline produced the number, not only the critique."""
    monkeypatch.delenv("LEGBA_VERIFY_LLM_JUDGE", raising=False)
    report = await verify_finding_faithfulness(body="", citations=[])
    assert report.as_dict()["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION


def test_one_stamp_for_the_whole_train() -> None:
    """A single module constant — a per-call or per-kind stamp would let two
    findings from the same deploy land in different populations.

    2026-08-15: the constant + its lineage banner moved to the sibling
    ``judge_pipeline_version`` module (the size-gate seam); ``verify``
    re-exports it, so the ONE assignment lives there and the historical import
    surface (``from ...verify import JUDGE_PIPELINE_VERSION``) is unchanged.
    """
    import legba.data.provenance.judge_pipeline_version as JPV
    import legba.data.provenance.verify as V

    assert isinstance(V.JUDGE_PIPELINE_VERSION, str)
    # The re-export IS the module constant — one stamp, two import paths.
    assert V.JUDGE_PIPELINE_VERSION == JPV.JUDGE_PIPELINE_VERSION
    inspect = __import__("inspect")
    # Exactly one assignment, in the extracted module; verify only imports.
    assert inspect.getsource(JPV).count("JUDGE_PIPELINE_VERSION = ") == 1
    assert inspect.getsource(V).count("JUDGE_PIPELINE_VERSION = ") == 0


# ---------------------------------------------------------------------------
# THE READERS (2026-08-02)
#
# This module's own docstring has always claimed that "anything reading
# faithfulness history ... partitions on it". That claim was FALSE for the
# stamp's whole life: repo-wide the only occurrences were the two writes in
# verify.py, this test file, and prose in STATUS/CHANGELOG. The population
# split key split nothing, while band calibration and the correctness scorer
# pooled populations straight across the 07-30 judge swap that moved mean
# faithfulness +7pp.
#
# A stamp with no reader is decoration. These pin that it has one.
# ---------------------------------------------------------------------------


def test_the_split_key_has_readers_not_just_writers() -> None:
    """The exact defect: writers, a test, and no consumer anywhere."""
    import inspect

    from legba.data.analysts.deterministic_handlers import (
        band_calibration_tracker,
        unit_correctness_scorer,
    )

    for mod in (band_calibration_tracker, unit_correctness_scorer):
        src = inspect.getsource(mod)
        assert "JUDGE_PIPELINE_VERSION" in src, (
            f"{mod.__name__} aggregates faithfulness-derived verdicts and must "
            "partition on the judge pipeline, not pool across a judge swap"
        )
        # ...and it must actually reach the SQL, not merely be imported.
        assert "judge_pipeline_version" in src, mod.__name__


def test_band_calibration_reports_its_population_boundary() -> None:
    """Filtering silently would trade one dishonesty for another: the readout
    has to say which population it covers and what it dropped.

    2026-08-29 — and the population is now a SET. A rate computed over several
    pooled stamps that names only one of them is the same dishonesty pointed the
    other way, so the disclosure carries the whole pool, its size, the metric
    family the pooling was computed for, and what is still excluded.
    """
    from legba.data.analysts.deterministic_handlers import band_calibration_tracker

    summary = band_calibration_tracker.summarize_claims(
        [],
        lookback_days=365,
        population={
            "judge_pipeline_version": JUDGE_PIPELINE_VERSION,
            "judge_pipeline_versions": ["2026-08-15/1", "2026-08-20/1"],
            "excluded_pre_stamp": 7,
            "excluded_other_pipeline": 3,
            "pooling": {
                "metric_family": "faithfulness_score",
                "stamps": ["2026-08-15/1", "2026-08-20/1"],
                "stamp_count": 2,
                "widened_by": 1,
            },
        },
    )
    pop = summary["population"]
    assert pop["judge_pipeline_version"] == JUDGE_PIPELINE_VERSION
    assert pop["excluded_pre_stamp"] == 7
    assert pop["excluded_other_pipeline"] == 3
    # The pooled SET, not a single stamp.
    assert pop["judge_pipeline_versions"] == ["2026-08-15/1", "2026-08-20/1"]
    assert pop["pooling"]["stamp_count"] == 2
    assert pop["pooling"]["widened_by"] == 1
    assert pop["pooling"]["metric_family"] == "faithfulness_score"


def test_correctness_scorer_faithfulness_sql_filters_on_the_stamp() -> None:
    """The row's `data` column is the whole CritiquePayload dump, so the stamp
    is one level down at `data.data.verification` — an easy path to get wrong,
    and getting it wrong silently reverts to pooling everything."""
    from legba.data.analysts.deterministic_handlers import unit_correctness_scorer

    sql = unit_correctness_scorer._FAITHFULNESS_SQL
    assert "data->'data'->'verification'->>'judge_pipeline_version'" in sql
    # The exclusion counter reads the same path, so the two can never disagree.
    excluded = unit_correctness_scorer._FAITHFULNESS_EXCLUDED_SQL
    assert "data->'data'->'verification'->>'judge_pipeline_version'" in excluded


# ---------------------------------------------------------------------------
# THE STRUCTURED LINEAGE + LINEAGE-AWARE POOLING (2026-08-29)
#
# The prose lineage says, per train, which way the population is expected to
# move — and several entries say in terms that a particular family CANNOT move
# across their boundary. A reader that pools nothing threw that away with
# everything else, and the cost is measured: `n_scored = 0` on every band
# calibration run since 2026-08-04, because the stamp's ~2.3-day lifetime can
# never outlast a 14-day horizon (CAMPAIGN_2026-08-29 A-7).
#
# `STAMP_EXPECTED_SHIFTS` is the prose made machine-readable so a reader can
# pool exactly as far as the lineage licenses. These pin that it stays a
# faithful transcription, that it can only ever fail SAFE, and — the real point
# — what it actually yields on the live lineage.
# ---------------------------------------------------------------------------

_STAMP_HEADER_RE = re.compile(r"^#\s(\d{4}-\d{2}-\d{2}/\d+)\s+—", re.MULTILINE)


def _prose_stamps() -> list[str]:
    """Every stamp the PROSE lineage banner documents, in first-appearance
    order. One stamp may head two entries (the H1+H2 / LRF+V-J1 two-train
    precedent), so this de-duplicates while preserving order."""
    import inspect

    import legba.data.provenance.judge_pipeline_version as JPV

    found = _STAMP_HEADER_RE.findall(inspect.getsource(JPV))
    assert found, "the lineage banner's stamp headers stopped being parseable"
    return list(dict.fromkeys(found))


def test_every_prose_lineage_stamp_has_a_registry_entry() -> None:
    """THE EXHAUSTIVE PIN — the registry is a transcription of the prose above
    it, so a stamp documented in one and missing from the other is a defect in
    whichever was written second.

    Both directions matter. A prose stamp with no entry means a reader silently
    treats a real boundary as unregistered; a registry entry with no prose means
    someone declared a shift nobody has to justify in writing.
    """
    from legba.data.provenance.judge_pipeline_version import (
        STAMP_EXPECTED_SHIFTS,
        STAMP_LINEAGE,
    )

    prose = _prose_stamps()
    assert set(prose) == set(STAMP_EXPECTED_SHIFTS), (
        "prose lineage and STAMP_EXPECTED_SHIFTS disagree: "
        f"prose-only={sorted(set(prose) - set(STAMP_EXPECTED_SHIFTS))} "
        f"registry-only={sorted(set(STAMP_EXPECTED_SHIFTS) - set(prose))}"
    )
    # ORDER is load-bearing: pooling walks CONSECUTIVE stamps, so a registry in
    # the wrong order would pool across a boundary that is not adjacent at all.
    assert prose == list(STAMP_LINEAGE)
    assert list(STAMP_LINEAGE) == sorted(STAMP_LINEAGE), "lineage must be chronological"


def test_new_stamp_without_a_registry_entry_fails_loudly() -> None:
    """THE DRIFT GUARD. Bumping ``JUDGE_PIPELINE_VERSION`` without writing the
    train's expected shifts is the failure this exists to make impossible.

    Runtime already fails safe — an unregistered stamp pools with nothing — but
    silently degrading to the old broken behaviour is exactly how the split key
    spent its whole life with no reader. This says so out loud instead.
    """
    from legba.data.provenance.judge_pipeline_version import (
        METRIC_FAMILIES,
        SHIFT_MOVES,
        SHIFT_NONE,
        STAMP_EXPECTED_SHIFTS,
        STAMP_LINEAGE,
    )

    assert JUDGE_PIPELINE_VERSION in STAMP_EXPECTED_SHIFTS, (
        f"{JUDGE_PIPELINE_VERSION} was bumped without a STAMP_EXPECTED_SHIFTS "
        "entry: declare, per metric family, whether this train's boundary can "
        "move it ('none' only where the lineage says so in terms; 'moves' "
        "otherwise — pooling fails safe toward NOT pooling)"
    )
    # The current stamp is the HEAD of the lineage, not a middle entry.
    assert STAMP_LINEAGE[-1] == JUDGE_PIPELINE_VERSION

    # Every entry is total over the families and uses the closed vocabulary —
    # a missing family would read as SHIFT_MOVES, which is safe but silent.
    for stamp, shifts in STAMP_EXPECTED_SHIFTS.items():
        assert set(shifts) == set(METRIC_FAMILIES), stamp
        assert set(shifts.values()) <= {SHIFT_NONE, SHIFT_MOVES}, stamp


def _fake_lineage(monkeypatch, entries: dict[str, str]) -> None:
    """Point the pooling functions at a synthetic one-family lineage.

    ``entries`` is ``{stamp: shift}`` in lineage order, for the family "f".
    """
    import legba.data.provenance.judge_pipeline_version as JPV

    table = {s: {"f": v} for s, v in entries.items()}
    monkeypatch.setattr(JPV, "STAMP_EXPECTED_SHIFTS", table)
    monkeypatch.setattr(JPV, "STAMP_LINEAGE", tuple(table))


def test_pooling_is_transitive_across_declared_none_boundaries(monkeypatch) -> None:
    """Consecutive 'none' boundaries are ONE population — the whole run pools,
    not just the adjacent pair."""
    from legba.data.provenance.judge_pipeline_version import poolable_stamps

    _fake_lineage(monkeypatch, {"a": "moves", "b": "none", "c": "none", "d": "moves"})

    # b and c each declare no shift, so a, b and c are one population — reached
    # from any member of the run, walking both directions.
    assert poolable_stamps("a", "f") == ("a", "b", "c")
    assert poolable_stamps("b", "f") == ("a", "b", "c")
    assert poolable_stamps("c", "f") == ("a", "b", "c")


def test_a_declared_moves_boundary_is_a_hard_stop(monkeypatch) -> None:
    """The whole point of the split key survives: a declared shift is never
    stepped over, however many 'none' boundaries sit either side of it."""
    from legba.data.provenance.judge_pipeline_version import poolable_stamps

    _fake_lineage(monkeypatch, {"a": "moves", "b": "none", "c": "moves", "d": "none"})

    assert poolable_stamps("b", "f") == ("a", "b")
    assert poolable_stamps("d", "f") == ("c", "d")
    # Nothing on one side of c's boundary reaches the other.
    assert "c" not in poolable_stamps("b", "f")
    assert "b" not in poolable_stamps("d", "f")
    assert "d" not in poolable_stamps("a", "f")


def test_pooling_fails_safe_on_an_unknown_stamp_or_family(monkeypatch) -> None:
    """FAIL SAFE — an unregistered stamp, or a family nobody declared, pools
    with NOTHING. That is the pre-pooling behaviour: narrow, possibly empty,
    never a fabricated population."""
    from legba.data.provenance.judge_pipeline_version import (
        SHIFT_MOVES,
        expected_shift,
        poolable_stamps,
    )

    _fake_lineage(monkeypatch, {"a": "none", "b": "none"})

    # A stamp the registry has never heard of.
    assert poolable_stamps("zz-unregistered", "f") == ("zz-unregistered",)
    assert expected_shift("zz-unregistered", "f") == SHIFT_MOVES
    # A registered stamp, but a family it declares nothing about.
    assert expected_shift("a", "undeclared_family") == SHIFT_MOVES
    assert poolable_stamps("a", "undeclared_family") == ("a",)


def test_the_first_stamp_never_pools_into_the_unstamped_era(monkeypatch) -> None:
    """A pre-stamp (NULL) claim is a real population and an unlabelled one: no
    lineage entry describes the boundary into it, so nothing may cross it. The
    backward walk stops at the head of the lineage even if the first stamp
    declares 'none'."""
    from legba.data.provenance.judge_pipeline_version import poolable_stamps

    _fake_lineage(monkeypatch, {"a": "none", "b": "none"})

    pool = poolable_stamps("a", "f")
    assert pool == ("a", "b")
    assert None not in pool and "" not in pool


def test_real_lineage_pooling_yield_for_the_score_family() -> None:
    """THE REAL-LINEAGE ANSWER, and it is a NEGATIVE RESULT worth stating.

    Computed over the actual lineage for ``faithfulness_score`` — the family
    band calibration and the correctness scorer both measure — the 14 live
    stamps collapse into 12 populations. Exactly two boundaries in the entire
    lineage are declared score-neutral, and both are historical:

      * ``2026-08-10/1`` (V-I1 guard 6) joins ``2026-08-09/1`` — a withdraw-only
        confirmation guard whose only declared shift is hard-fail COUNT, and a
        hard->soft demotion cannot move ``supported / checkable``.
      * ``2026-08-20/1`` (RUST-1) joins ``2026-08-15/1`` — "Mean faithfulness is
        UNCHANGED by construction (the demotion train never moves the score,
        only the severity label)".

    THE HEAD NOW POOLS ONE STEP — the update this pin's own last paragraph
    predicted. ``2026-08-30/1`` (the [N+1] transparency train, 201/201
    byte-identical equivalence proof) is the lineage's first all-none entry,
    so the head pool is ``{2026-08-29/1, 2026-08-30/1}``. Below LRF the
    boundary stays hard — 08-28/1, 08-27/1, 08-25/1, 08-21/1 all declare real
    score shifts — so band calibration's ``n_scored`` still does not move off
    zero for any window older than LRF: the residual defect remains stamp
    CADENCE, not reader design.

    This pin is EXPECTED to change again the next time a score-neutral train
    ships — the population widens by itself, and updating this list is how
    that becomes visible.
    """
    from legba.data.provenance.judge_pipeline_version import (
        METRIC_FAITHFULNESS_SCORE,
        STAMP_LINEAGE,
        poolable_stamps,
    )

    pools = list(
        dict.fromkeys(
            poolable_stamps(s, METRIC_FAITHFULNESS_SCORE) for s in STAMP_LINEAGE
        )
    )
    assert pools == [
        ("2026-07-31/1",),
        ("2026-08-02/1",),
        ("2026-08-03/1",),
        ("2026-08-04/1",),
        ("2026-08-05/1",),
        ("2026-08-09/1", "2026-08-10/1"),
        ("2026-08-15/1", "2026-08-20/1"),
        ("2026-08-21/1",),
        ("2026-08-25/1",),
        ("2026-08-27/1",),
        ("2026-08-28/1",),
        ("2026-08-29/1", "2026-08-30/1"),
    ]
    # 15 stamps -> 12 populations.
    assert len(STAMP_LINEAGE) == 15 and len(pools) == 12

    # THE HEAD WIDENS BY EXACTLY ONE: the transparency train pools with LRF
    # and with nothing older.
    assert poolable_stamps(JUDGE_PIPELINE_VERSION, METRIC_FAITHFULNESS_SCORE) == (
        "2026-08-29/1",
        JUDGE_PIPELINE_VERSION,
    )


def test_severity_and_census_families_pool_nothing_on_the_real_lineage() -> None:
    """Score-neutrality is family-specific and this proves the split is real:
    the two boundaries poolable for ``faithfulness_score`` are exactly the ones
    that MOVE the hard/soft split and the reason census, so a panel reading
    severity gets no pooling from them at all. One table, three answers."""
    from legba.data.provenance.judge_pipeline_version import (
        METRIC_REASON_CENSUS,
        METRIC_SEVERITY_SPLIT,
        STAMP_LINEAGE,
        poolable_stamps,
    )

    for family in (METRIC_SEVERITY_SPLIT, METRIC_REASON_CENSUS):
        for s in STAMP_LINEAGE:
            expected = (
                ("2026-08-29/1", "2026-08-30/1")
                if s in ("2026-08-29/1", "2026-08-30/1")
                else (s,)
            )
            # The one exception is the head pair: the [N+1] transparency train
            # (2026-08-30/1) declares NONE on every family — no reason strings,
            # no severity behavior, 201/201 byte-identical verdicts — so it
            # pools with LRF for severity/census too. Everything else is alone.
            assert poolable_stamps(s, family) == expected, (s, family)

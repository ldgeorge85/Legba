"""THE ROUND LINEAGE — where the badge's external number comes from (D-1 §2.3 / F-6).

THE HOLE THIS FILLS. The Assessment publishes with an accuracy badge, and the
badge is *"the one place a wrong number is worse than no number"*. There is no DB
home for an R-round result: ``correctness_labels`` holds 8 rows all-time and is a
different instrument, and the C-A grades live only in
``planning/PROOF_ROUND_*/scoring/*.jsonl``. Grepping ``src/`` for the round
numbers returns nothing but prose comments.

So the numbers live HERE, frozen and append-only, pinned by a test the way the
judge stamp's own lineage is. This is machinery plus five numbers per round — not
a corpus — which is why it belongs in the repo rather than behind the
no-seed-data rule (F-6, ratified).

TWO RULES THIS MODULE EXISTS TO ENFORCE, both stated as code rather than as
convention:

1. **A number never travels without its population.** :func:`external_accuracy`
   returns the value, its ``n``, its round, its date, its population and its
   lineage lane in ONE dict, and :data:`PRE_ASSEMBLY_NOTE` says in words which
   tier was graded. A caller cannot obtain the float on its own from this module.
2. **An unmeasured number says "unmeasured", never ``0.0``.** ``0.0`` on a badge
   reads as a measured failure. Until D-3's arm lands there is no
   fidelity-to-spine number at all, so :func:`assessment_badge` emits ``None``
   plus an explicit :data:`FIDELITY_UNMEASURED` state, and the reader already
   renders that case as *"fidelity to the spine is not measured yet"*.

WHY THE ASSESSMENT WEARS THE PROSE TIER'S NUMBER, AND FOR HOW LONG. R3's C-A was
measured on the composition PROSE tier — the tier this program demotes. The
Assessment is the surviving descendant of exactly that tier, so it is the only
external number that both exists and is honest about it. §2.3's schedule:

  * now → D-3 lands: R3's ``0.4805`` (n=77), labelled as the pre-assembly tier.
  * D-3 onward:      ``fidelity_to_spine``, live, its own population.
  * R5 onward:       both, side by side — R5 grades the Assessment's own lane.

The record's badge is a different object and is NOT built here: a composed read
carries quote fidelity, coverage completeness and a drop count, and deliberately
**no faithfulness score** (§2.3). The reader computes it from the payload beneath
it so it cannot disagree with the blocks it labels.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "FIDELITY_MEASURED",
    "FIDELITY_UNMEASURED",
    "POPULATION_ASSESSMENT",
    "POPULATION_COMPOSITION_PROSE",
    "PRE_ASSEMBLY_NOTE",
    "ROUND_LINEAGE",
    "STANDING_UNMEASURED",
    "RoundResult",
    "assessment_badge",
    "external_accuracy",
    "newest_for",
]


#: The graded populations this module knows about. A population string is part of
#: the badge's contract: the reader shows it, and :func:`newest_for` matches on
#: it, so two numbers measured on different things can never be swapped for each
#: other by accident.
#:
#: ``composition_prose`` — the pre-assembly composition body, written by a model
#: about its inputs. This is what R1/R2/R3 graded.
#: ``world_assessment`` — the Assessment channel's own rows. **Empty until R5.**
#: The absence of an entry is the point: the channel has never been graded
#: against the world, and the badge must not imply that it has.
POPULATION_COMPOSITION_PROSE: str = "composition_prose"
POPULATION_ASSESSMENT: str = "world_assessment"

#: The words that ride the external number wherever it is shown. Without them the
#: badge asserts that the Assessment scored 0.4805, which is false in the one
#: direction that matters: nobody has graded the Assessment at all.
PRE_ASSEMBLY_NOTE: str = (
    "graded against the world by human raters on the PRE-assembly prose tier"
)

FIDELITY_MEASURED: str = "measured"
FIDELITY_UNMEASURED: str = "unmeasured"

#: The state word when no STANDING number has been handed in (W-7). Mirrors
#: :data:`FIDELITY_UNMEASURED` deliberately — ``external_truth`` owns the four
#: standing states and composes them; this module only needs to be able to say
#: "nobody handed me one", and to say it as a word rather than as ``0.0``.
STANDING_UNMEASURED: str = "unmeasured"


@dataclass(frozen=True)
class RoundResult:
    """One measurement round, exactly as its VERDICT recorded it.

    ``inter_rater`` is ``None`` on every entry below and that is not an
    omission — R1–R3 ran a single grader per lane with a separate archiver /
    checker split, and no inter-rater coefficient was computed. A fabricated
    kappa on a badge would be the precise failure this whole program is about, so
    the field carries ``None`` and :func:`external_accuracy` never emits it.
    """

    round: str
    date: str
    population: str
    n: int
    claim_accuracy: float
    inter_rater: float | None
    notes: str
    source_file: str


#: APPEND-ONLY, OLDEST FIRST. A new round is a new tuple entry with its own
#: source file; an existing entry is never edited, because a badge that changes
#: retroactively is a badge nobody can audit. The ordering is load-bearing —
#: :func:`newest_for` reads the LAST match, the same convention
#: ``STAMP_LINEAGE`` uses.
ROUND_LINEAGE: tuple[RoundResult, ...] = (
    RoundResult(
        round="R1",
        date="2026-08-20",
        population=POPULATION_COMPOSITION_PROSE,
        n=61,
        claim_accuracy=0.893,
        inter_rater=None,
        notes=(
            "NOT COMMENSURABLE with R2/R3: graded under the 72-hour "
            "self-declared frame that R2 retired (PROOF_ROUND_2026-08-29/"
            "VERDICT.md §370 — 'Only R2<->R3 is a like-for-like C-A "
            "comparison'). Kept in the lineage because deleting a round is how "
            "a lineage starts lying; never pooled with the two below."
        ),
        source_file="planning/PROOF_ROUND_2026-08-20/VERDICT_DRAFT.md",
    ),
    RoundResult(
        round="R2",
        date="2026-08-25",
        population=POPULATION_COMPOSITION_PROSE,
        n=78,
        claim_accuracy=0.481,
        inter_rater=None,
        notes="C-A lane, composition prose, post-frame-retirement baseline.",
        source_file="planning/PROOF_ROUND_2026-08-25/VERDICT_DRAFT.md",
    ),
    RoundResult(
        round="R3",
        date="2026-08-29",
        population=POPULATION_COMPOSITION_PROSE,
        n=77,
        claim_accuracy=0.4805,
        inter_rater=None,
        notes=(
            "The D1 demotion rule fired on this number: C-A 0.4805 against R2's "
            "0.481 is FLAT (-0.0005), and PR-1(a)'s absolute bar of 0.75 was "
            "missed. This is the number the Assessment wears until its own lane "
            "is graded."
        ),
        source_file="planning/PROOF_ROUND_2026-08-29/VERDICT.md",
    ),
)

#: The lane letter the rounds grade factual accuracy under. It rides the badge so
#: a reader can find the number in the round's own VERDICT table.
CLAIM_ACCURACY_LANE: str = "C-A"


def newest_for(population: str) -> RoundResult | None:
    """The newest round graded on ``population``, or ``None``.

    ``None`` is a real answer and the caller must render it as one: for
    :data:`POPULATION_ASSESSMENT` it will keep returning ``None`` until R5, and
    "not graded yet" is the honest badge for a channel nobody has graded.
    """
    match = None
    for entry in ROUND_LINEAGE:
        if entry.population == population:
            match = entry
    return match


def external_accuracy(population: str = POPULATION_COMPOSITION_PROSE) -> dict[str, Any] | None:
    """The badge's ``external_accuracy`` block, or ``None`` when unmeasured.

    The shape is §2.3's verbatim: ``{value, n, round, as_of, population,
    lineage}``. Every consumer gets the number and its provenance in one object
    or gets nothing — there is deliberately no accessor that returns the float.
    """
    entry = newest_for(population)
    if entry is None:
        return None
    return {
        "value": entry.claim_accuracy,
        "n": entry.n,
        "round": entry.round,
        "as_of": entry.date,
        "population": entry.population,
        "lineage": CLAIM_ACCURACY_LANE,
    }


def assessment_badge(
    *,
    fidelity_to_spine: float | None = None,
    fidelity_n: int | None = None,
    standing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """``data.data.assessment.badge`` — §2.3's contract, built in one place.

    ``fidelity_to_spine`` stays ``None`` until D-3's arm exists. The
    ``fidelity_state`` field carries the same fact in a form SQL and a test can
    read without inferring it from a null: a row that says ``"unmeasured"``
    cannot be mistaken for a row whose arm ran and scored zero.

    The external number is attached ONLY with its note. If a future population
    ever loses its lineage entry, both keys vanish together rather than leaving a
    bare float on a badge.

    ``standing`` (W-7, design §3.4) is the ONE optional argument the standing
    external-truth loop adds, and it is deliberately a composed block rather than
    a float: ``external_truth.standing_accuracy()`` builds it, carrying the
    value, its ``n_decided``, its ``decided_rate``, its window, its population
    and its state WORD. This module stays DB-less — it never reads the ledger,
    it only wears the number a caller already read — which is the property that
    keeps ``ROUND_LINEAGE`` frozen and its pinning test green.

    THE FROZEN ROUND NUMBER IS NEVER REPLACED, ONLY JOINED. R3's 0.4805 keeps its
    key and its note whatever ``standing`` carries. When R5 grades this channel's
    own population, three numbers sit side by side and the reader can see the
    instrument change instead of a single figure quietly moving.
    """
    ext = external_accuracy(POPULATION_COMPOSITION_PROSE)
    measured = fidelity_to_spine is not None
    badge: dict[str, Any] = {
        "fidelity_to_spine": (
            None if not measured else round(float(fidelity_to_spine), 4)
        ),
        "fidelity_n": None if fidelity_n is None else int(fidelity_n),
        "fidelity_state": FIDELITY_MEASURED if measured else FIDELITY_UNMEASURED,
        "external_accuracy": ext,
        "external_accuracy_note": PRE_ASSEMBLY_NOTE if ext else None,
    }
    # The STANDING number (W-7). Absent until the auditor's ledger has decided
    # enough of this channel's own claims, and absent is a WORD: a badge that
    # renders "unmeasured" as 0.00 reads as a measured failure, which is the
    # discipline ``test_an_unmeasured_fidelity_says_unmeasured_never_zero``
    # already pins for the fidelity arm. ``standing_accuracy`` itself already
    # nulls its ``value`` whenever its own state is not "measured", so the badge
    # can never carry a rate the instrument did not earn.
    badge["standing_accuracy"] = dict(standing) if standing else None
    badge["standing_state"] = (
        str(standing.get("state") or STANDING_UNMEASURED)
        if standing else STANDING_UNMEASURED
    )
    # R5 opens the channel's own lane. Until it does, this stays None and the
    # badge shows the borrowed number WITH its borrowed-ness stated — never the
    # channel's own accuracy, because there isn't one.
    own = external_accuracy(POPULATION_ASSESSMENT)
    if own is not None:
        badge["own_accuracy"] = own
    return badge

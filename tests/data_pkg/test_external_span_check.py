# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""W-3 — the evidentiary contract, in code (EXTERNAL GRADING AT WIDTH §2.3).

Every test here names the round finding that motivates the gate it pins. That is
not decoration: the reason this module exists at all is that R2's scorer carried
the tier rule in prose and counted an inadmissible verdict anyway, and the C4
audit's one-line diagnosis — *"the string ``tier`` appears nowhere in the
scorer"* — is precisely what a test suite is for.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from legba.data import archive
from legba.data.analysts.deterministic_handlers import _external_audit_sampling as eas
from legba.data.provenance import external_span_check as esc
from legba.data.provenance.text_fold import FOLD_VERSION


WINDOW = {
    "oldest": "2026-08-20T00:00:00+00:00",
    "newest": "2026-08-29T12:00:00+00:00",
}
PAGE = (
    "Reuters, 27 August 2026 — The central bank raised its policy rate to 45 "
    "percent, its third increase this year, citing persistent inflation."
)
SPAN = "raised its policy rate to 45 percent"
IN_WINDOW = "2026-08-27T09:00:00+00:00"


def _claim(**kw):
    base = {
        "text": "The central bank raised its policy rate to 45 percent.",
        "decisive_span": SPAN,
        "proposed_verdict": esc.VERDICT_SUPPORTED,
    }
    base.update(kw)
    return esc.DecisiveClaim.coerce(base)


# ---------------------------------------------------------------------------
# THE VOCABULARY DRIFT GUARD
# ---------------------------------------------------------------------------


def test_the_verdict_vocabulary_matches_the_auditors_own() -> None:
    """The copies in this module are not left to trust.

    ``external_span_check`` mirrors ``_external_audit_sampling``'s verdict
    strings rather than importing them (that module lives under the handler
    package; this one has to stay importable from the registry image and from a
    bare test). The duplication buys nothing unless something asserts the two
    agree — this is that something.
    """
    assert esc.VERDICT_SUPPORTED == eas.VERDICT_SUPPORTED
    assert esc.VERDICT_CONTRADICTED == eas.VERDICT_CONTRADICTED
    assert esc.VERDICT_NOT_FOUND == eas.VERDICT_NOT_FOUND
    assert esc.VERDICT_UNCHECKED == eas.VERDICT_UNCHECKED
    # UNCHECKABLE is NEW (§0.5) and deliberately not in the shipped CHECKED set:
    # it is excluded from numerator and denominator both.
    assert esc.VERDICT_UNCHECKABLE not in eas.CHECKED_VERDICTS


# ---------------------------------------------------------------------------
# G-2 — THE DECISIVE SPAN DECIDES
# ---------------------------------------------------------------------------


def test_a_fabricated_span_fails() -> None:
    """R1/R2/R3's span sweeps (176/176, 215/215, 281/282) are why those rounds'
    verdicts survived audit. Today's auditor validates only that the URL appeared
    in the results — a page can be in the result set and not say this."""
    check = esc.check_span(
        _claim(decisive_span="cut its policy rate to 5 percent"),
        "https://www.reuters.com/markets/x",
        PAGE,
        IN_WINDOW,
        WINDOW,
    )
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert check.demoted is True
    assert check.span_resolved is False
    assert esc.DEMOTION_SPAN_UNRESOLVED in check.demotions
    assert "does not appear in the fetched page" in check.rationale
    # A span that did not resolve is never content-addressed as if it had.
    assert check.decisive_span_sha256 is None


def test_a_resolving_span_admits_the_verdict_and_survives_the_fold() -> None:
    """The comparison runs through the SHARED fold, both sides — the MECH-6
    class (U+2011 on one side, ASCII hyphen on the other) is exactly how a
    comparator decides the wrong way silently at fleet scale."""
    page = PAGE.replace("policy rate", "policy‑rate").replace("45", "４５")
    check = esc.check_span(
        _claim(decisive_span="raised its policy-rate to 45 percent"),
        "https://www.reuters.com/markets/x",
        page,
        IN_WINDOW,
        WINDOW,
    )
    assert check.span_resolved is True
    assert check.verdict == esc.VERDICT_SUPPORTED
    assert check.demoted is False
    assert check.fold_version == FOLD_VERSION


def test_no_span_at_all_is_no_decisive_verdict() -> None:
    check = esc.check_span(
        _claim(decisive_span=""),
        "https://www.reuters.com/markets/x",
        PAGE,
        IN_WINDOW,
        WINDOW,
    )
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert esc.DEMOTION_NO_SPAN in check.demotions


# ---------------------------------------------------------------------------
# G-1 — SOURCE TIER, AND F-3's VISIBLE UNKNOWN CLASS
# ---------------------------------------------------------------------------


def test_the_tier_registry_resolves_the_rounds_own_bounds() -> None:
    """Transcribed from the binding text every R2/R3 lane packet carried."""
    assert esc.source_tier_for_url("https://www.reuters.com/x").tier_class == "tier_2"
    assert esc.source_tier_for_url("https://data.imf.org/x").tier_class == "tier_1"
    assert esc.source_tier_for_url("https://treasury.gov/x").tier_class == "tier_1"
    assert esc.source_tier_for_url("https://ec.europa.eu/x").tier_class == "tier_1"
    assert esc.source_tier_for_url("https://news.bbc.co.uk/x").tier_class == "tier_2"
    # State-controlled outlets are REGISTERED at Tier 3, not left unknown: an
    # unknown domain is one nobody has looked at, and these have been.
    assert esc.source_tier_for_url("https://www.rt.com/x").tier_class == "tier_3"
    # Unparseable input decides nothing rather than guessing.
    assert esc.source_tier_for_url(None).tier_class == "tier_unknown"


def test_a_tier_3_only_decisive_demotes() -> None:
    """R2-C4 case #10 — the AR LNG major-absent, OVERTURNED as scored because
    ``score_r2.py`` never read ``source_tier``. At width that is 470 chances a
    day to repeat it."""
    check = esc.check_span(
        _claim(),
        "https://www.rt.com/business/x",
        PAGE,
        IN_WINDOW,
        WINDOW,
    )
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert esc.DEMOTION_TIER_3 in check.demotions
    assert check.source_tier == 3
    assert "Tier 3" in check.rationale


def test_tier_unknown_is_VISIBLE_and_is_not_demoted() -> None:
    """F-3, taken on the visible option (orchestrator ruling).

    The ruled default was Tier 3, which suppresses a true CONTRADICTED on a
    legitimate regional outlet SILENTLY. The alternative — publish a
    ``tier_unknown`` decisive class, counted and reported separately — suppresses
    nothing and hides nothing, and that is what ships.
    """
    check = esc.check_span(
        _claim(proposed_verdict=esc.VERDICT_CONTRADICTED),
        "https://www.some-regional-outlet.example/story",
        PAGE,
        IN_WINDOW,
        WINDOW,
    )
    assert check.verdict == esc.VERDICT_CONTRADICTED, "unknown must NOT demote"
    assert check.demoted is False
    assert check.demotions == ()
    assert check.tier_unknown is True
    assert check.tier_class == esc.TIER_CLASS_UNKNOWN
    assert check.source_tier is None, "no integer tier nobody measured"
    assert "UNREGISTERED domain" in check.rationale
    assert "counted in the tier_unknown class" in check.rationale


# ---------------------------------------------------------------------------
# G-3 — TIME ANCHORING
# ---------------------------------------------------------------------------


def test_an_out_of_window_source_demotes() -> None:
    """R2-C4 case #5: a decisive anchor on a rolling page *"stamped Update
    01/07/2026, seven weeks before T0"*."""
    check = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, "2026-07-01T00:00:00Z", WINDOW
    )
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert check.demoted is True
    assert esc.DEMOTION_OUT_OF_WINDOW in check.demotions
    assert check.unchecked_reason == esc.UNCHECKED_OUT_OF_WINDOW
    assert check.in_window is False


def test_a_source_published_after_the_read_demotes() -> None:
    """A source the read could not have seen cannot have supported it. This is
    the direction the R3 §4.7 finding runs in — declared spans 1-2 days inside a
    read graded and consumed as a 14-day country read."""
    after = "2026-09-04T00:00:00+00:00"
    check = esc.check_span(_claim(), "https://www.reuters.com/x", PAGE, after, WINDOW)
    assert esc.DEMOTION_OUT_OF_WINDOW in check.demotions


def test_the_blocks_produced_at_extends_the_upper_bound() -> None:
    """G-3's "plus the block's ``produced_at``": a read composed at 12:00Z may
    legitimately rest on a source published at 11:55Z that post-dates its newest
    consumed head."""
    later = "2026-08-30T11:55:00+00:00"
    without = esc.check_span(_claim(), "https://www.reuters.com/x", PAGE, later, WINDOW)
    assert esc.DEMOTION_OUT_OF_WINDOW in without.demotions
    with_produced = esc.check_span(
        _claim(),
        "https://www.reuters.com/x",
        PAGE,
        later,
        WINDOW,
        produced_at="2026-08-30T12:00:00+00:00",
    )
    assert with_produced.demoted is False
    assert with_produced.in_window is True


# ---------------------------------------------------------------------------
# THE GRACE KNOB (2026-09-06 follow-up) — G-3's before bound, made settable
# ---------------------------------------------------------------------------


def test_grace_zero_is_byte_identical_to_today() -> None:
    """The default. Passing ``grace_before_hours=0.0`` explicitly must produce
    the EXACT SAME :class:`SpanCheck` as never passing it at all — every field,
    not just the verdict."""
    without = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, "2026-07-01T00:00:00Z", WINDOW
    )
    with_zero = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, "2026-07-01T00:00:00Z", WINDOW,
        grace_before_hours=0.0,
    )
    assert with_zero.as_dict() == without.as_dict()
    # And the admitted case too, not only the demoted one.
    without_admit = esc.check_span(_claim(), "https://www.reuters.com/x", PAGE, IN_WINDOW, WINDOW)
    with_zero_admit = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, IN_WINDOW, WINDOW,
        grace_before_hours=0.0,
    )
    assert with_zero_admit.as_dict() == without_admit.as_dict()


def test_grace_admits_71_hours_before_the_window_and_rejects_73() -> None:
    """WINDOW's ``oldest`` is 2026-08-20T00:00:00Z. A 72h grace must admit a
    source 71h before it and still reject one 73h before — the boundary is at
    exactly the grace, not "somewhere nearby"."""
    just_inside = "2026-08-17T01:00:00+00:00"  # 71h before oldest
    just_outside = "2026-08-16T23:00:00+00:00"  # 73h before oldest

    admitted = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, just_inside, WINDOW,
        grace_before_hours=72.0,
    )
    assert admitted.verdict == esc.VERDICT_SUPPORTED
    assert admitted.demoted is False
    assert admitted.in_window is True

    rejected = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, just_outside, WINDOW,
        grace_before_hours=72.0,
    )
    assert rejected.verdict == esc.VERDICT_NOT_FOUND
    assert esc.DEMOTION_OUT_OF_WINDOW in rejected.demotions
    assert rejected.in_window is False

    # And WITHOUT grace, the 71h-before source is out of window too — the knob
    # is what moved it, not some other change.
    no_grace = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, just_inside, WINDOW,
    )
    assert esc.DEMOTION_OUT_OF_WINDOW in no_grace.demotions


def test_grace_never_moves_the_after_bound() -> None:
    """G-3's after bound is not a judgement call and grace must not touch it —
    a source published after ``newest`` demotes regardless of how large a
    BEFORE grace is configured."""
    after = "2026-09-04T00:00:00+00:00"
    check = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, after, WINDOW,
        grace_before_hours=1_000.0,
    )
    assert esc.DEMOTION_OUT_OF_WINDOW in check.demotions
    assert check.in_window is False


def test_grace_leaves_an_undated_source_unchanged() -> None:
    """No publish date means the question cannot be asked at all — a grace
    value changes nothing about that."""
    check = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, None, WINDOW,
        grace_before_hours=72.0,
    )
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert esc.DEMOTION_NO_PUBLISH_DATE in check.demotions
    assert check.unchecked_reason is None


def test_grace_leaves_tri_valued_semantics_unchanged_on_an_unmeasured_window() -> None:
    """An unmeasured window still declines to run the gate at all, whatever
    grace is configured — grace only ever adjusts a MEASURED bound."""
    check = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, IN_WINDOW, None,
        grace_before_hours=72.0,
    )
    assert check.demoted is False
    assert check.in_window is None
    assert check.window == {"from": None, "to": None}


def test_evidence_window_bounds_grace_before_hours_shifts_only_start() -> None:
    """Unit-level pin on the seam itself, one level below ``check_span``."""
    bounds = esc.evidence_window_bounds(WINDOW, grace_before_hours=24.0)
    assert bounds.start == esc.as_datetime(WINDOW["oldest"]) - timedelta(hours=24)
    assert bounds.end == esc.as_datetime(WINDOW["newest"])
    # A negative or unparsable grace is a no-op, never a silent narrowing.
    for bad in (-5.0, "not-a-number", None):
        no_op = esc.evidence_window_bounds(WINDOW, grace_before_hours=bad)
        assert no_op.start == esc.as_datetime(WINDOW["oldest"])


def test_in_evidence_window_grace_before_hours_forwards_to_bounds() -> None:
    just_inside = "2026-08-19T01:00:00+00:00"  # 23h before oldest
    assert esc.in_evidence_window(just_inside, WINDOW) is False
    assert esc.in_evidence_window(just_inside, WINDOW, grace_before_hours=24.0) is True


def test_an_unmeasured_window_does_not_run_the_gate() -> None:
    """An unmeasured window and an out-of-window source are different facts.
    Conflating them would demote every claim on a read with no datable head —
    ``evidence_window_span`` returns ``None`` for exactly that case."""
    check = esc.check_span(_claim(), "https://www.reuters.com/x", PAGE, IN_WINDOW, None)
    assert check.demoted is False
    assert check.in_window is None
    assert check.window == {"from": None, "to": None}


def test_an_undated_source_cannot_be_anchored() -> None:
    check = esc.check_span(_claim(), "https://www.reuters.com/x", PAGE, None, WINDOW)
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert esc.DEMOTION_NO_PUBLISH_DATE in check.demotions
    # It is NOT stamped out_of_window: the source was never placed, which is a
    # different fact from being placed outside.
    assert check.unchecked_reason is None


def test_every_failing_gate_is_named_not_just_the_first() -> None:
    """A source that is Tier 3 AND out of window is two problems, and a rationale
    naming one of them invites a fix that changes nothing."""
    check = esc.check_span(
        _claim(decisive_span="not on this page"),
        "https://www.rt.com/x",
        PAGE,
        "2026-07-01T00:00:00Z",
        WINDOW,
    )
    assert set(check.demotions) == {
        esc.DEMOTION_SPAN_UNRESOLVED,
        esc.DEMOTION_TIER_3,
        esc.DEMOTION_OUT_OF_WINDOW,
    }


def test_a_non_decisive_proposal_passes_through_ungated() -> None:
    """There is nothing to demote about a NOT_FOUND, and running the gates on one
    would invent a tier for a source nobody cited."""
    check = esc.check_span(
        _claim(proposed_verdict=esc.VERDICT_NOT_FOUND, decisive_span=""),
        "",
        "",
        None,
        WINDOW,
    )
    assert check.verdict == esc.VERDICT_NOT_FOUND
    assert check.demoted is False
    assert check.demotions == ()


# ---------------------------------------------------------------------------
# G-5 — THE DETERMINISTIC PRE-FILTER
# ---------------------------------------------------------------------------

#: R3's ONE non-resolving span in 282 — ML_A ``CR-2bbcd291``, quoted verbatim
#: from ``planning/PROOF_ROUND_2026-08-29/stage2_ML.json``. The VERDICT calls it
#: *"a self-referential provenance claim whose truth-maker is the packet, not the
#: world"*. Impact if dropped: C-A 0.4805 -> 0.4801.
ML_A_SPECIMEN = (
    "leadership_transition (29 August 01:05 UTC), energy_security "
    "(15 August 16:07 UTC), escalation (29 August 07:02 UTC), "
    "narrative_coordination (29 August 10:01 UTC), internal_stability "
    "(29 August 02:03 UTC), military_posture (27 August 17:03 UTC), "
    "economic_coercion (27 August 21:02 UTC) – all verified; "
    "proliferation_watch – gap; economic_coercion (29 August 09:02 UTC) "
    "– below verification floor; military_posture (29 August 05:01 UTC) "
    "– below verification floor; energy_security (29 August 04:00 UTC) "
    "– below verification floor."
)

#: R2-C4 case #4, the same class recurring one round earlier.
R2_C4_CASE_4 = "All eight principal units produced verified reads in this cycle."


def test_the_ML_A_self_referential_specimen_classifies_UNCHECKABLE() -> None:
    """The specimen is the whole argument for a PRE-search classifier.

    Searching this produces NOT_FOUND, every time, forever — and a NOT_FOUND is a
    statement about the search. At width this class appears ~30 times a day; let
    into the searched population it quietly deflates the decided rate and turns a
    truth number into a coverage number.
    """
    assert (
        esc.uncheckable_class_for_claim(ML_A_SPECIMEN)
        == esc.UNCHECKABLE_PROVENANCE
    )
    assert (
        esc.uncheckable_class_for_claim(R2_C4_CASE_4) == esc.UNCHECKABLE_PROVENANCE
    )


def test_a_real_world_claim_is_searchable() -> None:
    assert esc.uncheckable_class_for_claim(
        "The central bank raised its policy rate to 45 percent on 22 August."
    ) is None


def test_the_producers_own_scope_tokens_win_over_the_lexicon() -> None:
    """``spans[].scope_tokens`` is measured by the assembly; this lexicon only
    guesses. The measurement wins."""
    assert (
        esc.uncheckable_class_for_claim("Exports rose 4%.", scope_tokens=["collection"])
        == esc.UNCHECKABLE_SCOPE_BOUNDED
    )
    assert (
        esc.uncheckable_class_for_claim("Exports rose 4%.", perspective=True)
        == esc.UNCHECKABLE_PERSPECTIVE
    )


# ---------------------------------------------------------------------------
# THE GRADING ARCHIVE — its own path, never a signal (§0.11 / F-8)
# ---------------------------------------------------------------------------


def test_the_grading_archive_is_not_the_evidence_archive() -> None:
    """F-8. ``evidence_archive`` is keyed on ``signal_id``; routing grading pages
    through it would hand the desks the grader's own evidence as a slice input
    and close the exact loop this program exists to open."""
    digest = esc.grading_page_sha256(PAGE)
    ref = esc.grading_archive_ref(digest)
    assert ref.startswith(esc.GRADING_CAS_PREFIX)
    assert not ref.startswith(archive.CAS_PREFIX)
    assert archive.sha256_from_object_ref(ref) is None, (
        "the evidence archive's own parser must not recognise a grading ref"
    )
    root = Path("/var/lib/legba/archive")
    grading = esc.grading_archive_path(root, digest)
    assert esc.GRADING_ARCHIVE_SUBDIR in grading.parts
    assert grading != archive.cas_path(root, digest)


def test_the_archive_ref_rides_an_admitted_verdict(tmp_path: Path) -> None:
    check = esc.check_span(
        _claim(),
        "https://www.reuters.com/x",
        PAGE,
        IN_WINDOW,
        WINDOW,
        archive_root=tmp_path,
    )
    assert check.archive_ref == esc.grading_archive_ref(esc.grading_page_sha256(PAGE))
    written = esc.grading_archive_path(tmp_path, esc.grading_page_sha256(PAGE))
    assert written.read_text(encoding="utf-8") == PAGE
    # And the whole ledger projection is present, so a row can be written from it.
    row = check.as_dict()
    assert row["decisive_tier_class"] == "tier_2"
    assert row["archive_ref"] == check.archive_ref
    assert row["contract_version"] == esc.CONTRACT_VERSION


def test_archiving_never_raises_on_an_unwritable_root(tmp_path: Path) -> None:
    """A grading run that cannot archive is DEGRADED, not wrong."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory", encoding="utf-8")
    ref = esc.archive_grading_page(PAGE, root=blocker)
    assert ref == esc.grading_archive_ref(esc.grading_page_sha256(PAGE))


# ---------------------------------------------------------------------------
# TIME COERCION
# ---------------------------------------------------------------------------


def test_naive_timestamps_are_read_as_utc_not_local() -> None:
    got = esc.as_datetime("2026-08-27T09:00:00")
    assert got == datetime(2026, 8, 27, 9, 0, tzinfo=timezone.utc)
    assert esc.as_datetime("not a date") is None
    assert esc.as_datetime(None) is None
    aware = datetime(2026, 8, 27, tzinfo=timezone.utc) + timedelta(hours=3)
    assert esc.as_datetime(aware) == aware


# ---------------------------------------------------------------------------
# G-3 — THE WINDOW BASIS, FROM THIS SIDE OF THE SEAM (2026-09-07)
# ---------------------------------------------------------------------------
#
# `window_basis="evidence"` is implemented entirely in the auditor
# (`_external_audit_width.evidence_window`), which hands THIS module a window
# dict carrying three extra keys. Nothing here changed for it — and that is the
# contract these two tests pin, because "the enriched dict still resolves to
# exactly the same two bounds" is a promise of this module, not of its caller.

#: The evidence-rebased shape the auditor now hands in under `evidence`:
#: `oldest` moved, `newest` untouched, and the heads value KEPT for the record.
EVIDENCE_REBASED_WINDOW = {
    **WINDOW,
    "oldest": "2026-08-06T00:00:00+00:00",
    "basis": "evidence",
    "oldest_heads": WINDOW["oldest"],
    "evidence_signals": 4212,
}


def test_the_kept_heads_bound_can_never_become_a_bound() -> None:
    """`oldest_heads` is a RECORD, not an alias. If the resolver ever read it —
    or `basis`, or `evidence_signals` — the evidence basis would silently grade
    against the window it replaced."""
    bounds = esc.evidence_window_bounds(EVIDENCE_REBASED_WINDOW)
    assert bounds.start == datetime(2026, 8, 6, tzinfo=timezone.utc)
    assert bounds.end == datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    assert bounds.as_dict() == {
        "from": "2026-08-06T00:00:00+00:00", "to": "2026-08-29T12:00:00+00:00",
    }
    # And the unknown keys are inert rather than tolerated-by-accident: a dict
    # carrying ONLY them measures nothing at all.
    assert esc.evidence_window_bounds(
        {"basis": "evidence", "oldest_heads": WINDOW["oldest"],
         "evidence_signals": 9}
    ).measured is False


def test_an_evidence_rebased_window_admits_what_the_heads_window_demoted() -> None:
    """The whole point of the basis, proven at the gate that does the demoting:
    one source, one page, two windows, two verdicts — and no grace involved."""
    published = "2026-08-14T00:00:00+00:00"

    demoted = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, published, WINDOW,
    )
    assert demoted.verdict == esc.VERDICT_NOT_FOUND
    assert demoted.unchecked_reason == esc.UNCHECKED_OUT_OF_WINDOW
    # G-2 resolved it regardless — the demotion keeps its evidence.
    assert len(demoted.decisive_span_sha256 or "") == 64

    admitted = esc.check_span(
        _claim(), "https://www.reuters.com/x", PAGE, published,
        EVIDENCE_REBASED_WINDOW,
    )
    assert admitted.verdict == esc.VERDICT_SUPPORTED
    assert admitted.demoted is False
    assert admitted.in_window is True
    assert admitted.window == {
        "from": "2026-08-06T00:00:00+00:00", "to": "2026-08-29T12:00:00+00:00",
    }
    # The two share everything the basis does not touch — same span, same
    # content address, same tier.
    assert admitted.decisive_span_sha256 == demoted.decisive_span_sha256
    assert admitted.source_tier == demoted.source_tier

# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Module-size regrowth gate (CODE_CLEANUP_ANALYSIS_2026-08-02 phase 1A).

`runtime/dapr_actors.py` was decomposed in June 2026 — six modules, 2,367
lines extracted, 3,989 -> 2,641. Five weeks later it measured **3,735**: it
regrew +41% from the post-extraction floor, more than the extraction had
removed. The extraction was correct, executed, and completely undone,
because nothing in the tree noticed the file getting bigger again.

This test is that notice. It pins a LOC ceiling on every `src/legba` module
that was already >=1,500 lines when the gate was written, seeded at the
measured count plus ~10% headroom. Ordinary maintenance fits inside the
headroom. A file that grows past its ceiling turns this test red.

Three checks, all fail-loud:

  * **Ceiling breach** — a pinned module exceeded its ceiling. The fix is to
    EXTRACT a cohesive unit into a sibling module (see the section banners in
    the file — they are the author's own seams), not to raise the number.
    Raising a ceiling means editing this file, which is a visible, reviewable
    act in the diff; that visibility is the entire mechanism.
  * **New entrant** — a module crossed 1,500 lines without being pinned. It
    joins the list (with a ceiling) or it gets split. Without this check the
    gate would only ever police yesterday's monsters.
  * **Stale ceiling** — a module shrank far below its ceiling (a real
    extraction landed) and the ceiling was not re-seeded, so it no longer
    constrains anything. Phase 2 splits are expected to trip this and lower
    the ceiling in the same commit; that is the ratchet. The threshold is
    deliberately loose (50% headroom) so that trimming a few hundred lines
    does not nag.

Line counts use ``str.splitlines()`` over the decoded text, which matches
``wc -l`` for files with a trailing newline. Only tracked-on-disk
``src/legba/**/*.py`` is measured — tests, scripts and the UI are out of
scope, deliberately: this gate exists to protect the production modules
that everything else lands in.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "legba"

#: A module at or above this many lines must carry a pinned ceiling.
ENTRY_THRESHOLD = 1_500

#: A ceiling more than this multiple of the module's actual size has stopped
#: constraining it — re-seed it (see the stale-ceiling check).
STALE_CEILING_FACTOR = 1.5

#: Per-module LOC ceilings, keyed by path relative to ``src/legba``.
#:
#: Seeded 2026-08-02 from the 25 modules then >=1,500 lines, at the measured
#: count + ~10% rounded up to a multiple of 10. The trailing comment is the
#: seed measurement — keep it when you change a ceiling, so the diff shows
#: which way the file moved.
CEILINGS: dict[str, int] = {
    # LOWERED 2026-08-03 (K-1): the debt above was PAID. The absence-slice
    # subsystem named as the seam — the absence claim grammar, the country
    # gazetteer, the slice-row model + retained-slice loader, and the whole
    # stage-1 classifier — moved to ``data/provenance/absence_slice.py`` (903
    # lines), which ``verify.py`` imports ONE WAY and re-exports. The ratchet
    # closes on the new floor: this is the number the V-G train must fit under,
    # and the next extraction seam is the JUDGE subsystem (prompt registry +
    # ``_run_judge`` + the quote/severity rules).
    # LOWERED AGAIN 2026-08-27 (V-I): the JUDGE-VERDICT PARSING cluster —
    # ``_JudgeVerdictError``, ``_extract_json_objects``, ``_judge_reason`` and
    # ``_judge_detail`` — moved to ``data/provenance/judge_verdict_parsing.py``,
    # the judge subsystem's next brick, which ``verify.py`` imports ONE WAY and
    # re-exports. ``_is_uncited_world_baseline`` (V-G5) rode along as the
    # smallest adjacent self-contained helper once the cluster alone didn't
    # clear the margin. The severity DECISION (the fail-class table and
    # ``_DEMOTION_COUNTERS``) and the markerless-uncited FOLD stayed behind —
    # both manipulate report/ledger types this module owns. Ratchet closes on
    # the new floor. DO NOT RAISE.
    "data/provenance/verify.py": 5900,  # 5866 @ 2026-08-27 (V-I) — judge_verdict_parsing.py extracted (see banner above); 5853 @ 2026-08-05 — the V-D/W2/V-G1/V-G3 QUOTE RULES extracted to judge_quote_rules.py and the CITATION MARKER parsing/drift set to citation_markers.py (judge-subsystem bricks 4 and 5), ceiling re-seeded down twice in the V-I train.
    # LOWERED 2026-08-20 (FRAME-1): the C-TIER two-tier evidence subsystem — the
    # periphery GATHER, its worst-first selection and its render, plus the
    # row-reading primitives all three share — moved to
    # ``data/analysts/composition_window.py`` alongside the new admissibility-
    # window machinery (head ages, the coverage ledger, the newest-passing-head
    # routing), which the synthesizer imports ONE WAY and re-exports. The train
    # ADDED ~290 lines of composition behavior and the file still came down 57;
    # the ratchet closes on the new floor. Next seam in this file: the CONTINUITY
    # render (prior-read + situation-register lines + their selection), which is
    # FRAME-2's own surface.
    # LOWERED AGAIN 2026-08-20 (FRAME-2): the seam the note above NAMED was
    # taken. The composition's CONTINUITY section — the prior-read and
    # situation-register renders, their selection helpers and the whole
    # continuity/register constant vocabulary — moved to
    # ``data/analysts/window_ledger.py`` beside the WINDOW LEDGER that is now the
    # third block in that same section, and the synthesizer imports the lot ONE
    # WAY and re-exports it. The train ADDED a whole carry mechanism and the file
    # still came down 168 lines; the ratchet closes on the new floor. Next seam
    # in this file: the SLICE ASSEMBLY branches (``_assemble_world_region_slice``
    # / ``_assemble_thematic_unit_slice`` / their roster resolvers), which are a
    # cohesive "how a composition's inputs are gathered per mode" unit and the
    # largest remaining block that is not the prompt text itself.
    # LOWERED AGAIN 2026-08-21 (VOICE-4): the note above set the slice-assembly
    # seam aside as "not the prompt text itself" — this train took THE PROMPT
    # TEXT. The four composition system prompts, the legacy global-meta prompt
    # and every shared rule generator moved to
    # ``data/analysts/composition_prompts.py`` (1,005 lines), which the
    # synthesizer imports ONE WAY and re-exports, so ``synth._COMPOSITION_SYSTEM``
    # and the voice-contract pins resolve unchanged. The train ADDED ~8k chars of
    # doctrine to each of the four prompts and the file still came down 398
    # lines; the ratchet closes on the new floor. The slice-assembly seam named
    # above is STILL the next one here.
    # LOWERED AGAIN 2026-08-29 (the JSON-envelope leak): the file had regrown to
    # 5,248 against 5,250 — two lines of headroom — and the world-composition
    # leak fix had to land inside it. The OUTPUT-COERCION unit (``_coerce_finding``,
    # ``_looks_like_resolvable_evidence`` and the new degrade path) moved to
    # ``data/analysts/composition_coercion.py``, which the synthesizer imports ONE
    # WAY and re-exports, so both names resolve unchanged for every caller and
    # test. Pure, DB-free and LLM-free, so it tests without a slice. The train
    # ADDED the salvage-or-raise contract and the file still came down 117 lines;
    # the ratchet closes on the new floor. The slice-assembly seam named above is
    # STILL the next one here.
    "data/analysts/meta_findings_synthesizer.py": 5190,  # 5131 @ 2026-08-29 (envelope leak); 5192 @ 2026-08-21 (VOICE-4); 5585 @ 2026-08-20 (FRAME-2); 5750 @ 2026-08-20 (FRAME-1); 5279 @ 2026-08-02
    "runtime/dapr_actors.py": 4110,  # 3735 @ 2026-08-02
    "runtime/liveness_watchdog.py": 1660,  # 1507 @ 2026-08-30 — merge-wave entrant (honest-quiet dynamic window + prolonged-streak escalation)
    "data/analysts/inline_target.py": 3915,  # 3909 @ 2026-08-25 — the per-clean render RECEIPT (_slice_render_stats) extracted to slice_render.py beside the render it describes, paying for the task-#57 wire-pair collapse hook; ceiling re-seeded down again (was 3930)
    "runtime/substrate_query_port.py": 3910,  # 3551 @ 2026-08-02
    "data/analysts/journal_assessor.py": 3000,  # 2965 @ 2026-08-11 (leak guards extracted)
    "runtime/grounding.py": 3000,  # 2719 @ 2026-08-02
    "runtime/analyst_deps_builder.py": 2990,  # 2715 @ 2026-08-02
    "runtime/dapr_host.py": 2950,  # 2676 @ 2026-08-02
    # LOWERED 2026-08-03 (K-2): the API KERNEL — the B-2 bearer gate, the C3
    # ``sunset_headers`` stamp and the ``RegistryAPIDeps`` bundle + ``_get_deps``
    # — moved to the leaf ``data/registry/_deps.py`` (254 lines), which this
    # module imports ONE WAY and re-exports. The seam was not size, it was
    # COUPLING: 26 of the package's 50 modules imported this 2,500-line file for
    # four of those names. The ratchet closes on the new floor; the next seam is
    # ``build_router`` itself, which is ~1,400 of the remaining lines and splits
    # by route family (descriptors / stack / vault / dlq / audit / vocabulary).
    "data/registry/api.py": 2590,  # 2524 @ 2026-08-02; 2353 after K-2
    "data/filters/fact_extractor.py": 2620,  # 2381 @ 2026-08-02
    "data/analysts/deterministic_handlers/claim_watch.py": 2560,  # 2320 @ 2026-08-02
    # LOWERED 2026-08-27 (H3-GUARD): projecting the two new semantics stamps
    # (`banding_semantics`/`damping_semantics`) onto `CountryScorecard` pushed
    # this file 5 lines over the 2430 ceiling. The escalation-delivery route's
    # models + pure reducer — four models and two functions, used by exactly
    # ONE route (`GET /system/escalations`) and nothing else in this module —
    # were the smallest self-contained route-helper cluster left (the
    # scorecard-reconcile seam was already taken, B0-5). Moved to
    # `data/registry/escalation_delivery.py`, which this module imports ONE WAY
    # and re-exports under the historical private names, so every call site —
    # and `test_v3_escalations.py`, which imports these names off THIS module —
    # stayed byte-identical. The train added the two projected fields and the
    # file still came down 171 lines; the ratchet closes on the new floor.
    "data/registry/v3_api.py": 2300,  # 2259 @ 2026-08-27 (H3-GUARD); 2209 @ 2026-08-02
    "data/provenance/writes.py": 2340,  # 2126 @ 2026-08-02
    "data/_entity_canon.py": 2330,  # 2115 @ 2026-08-02
    "data/analysts/consult_on_demand.py": 2310,  # 2092 @ 2026-08-02
    "data/sources/telegram.py": 2270,  # 2062 @ 2026-08-02
    "runtime/dapr_workflow/gepa.py": 2250,  # 2044 @ 2026-08-02
    "data/analysts/deterministic_handlers/fact_contention_arbiter.py": 1980,  # 1798 @ 2026-08-02
    "runtime/source_actor.py": 1895,  # 1872 @ 2026-08-04 — discovery dispatch extracted to source_discovery_dispatch.py, ceiling re-seeded down
    "data/registry/descriptor.py": 1880,  # 1706 @ 2026-08-02
    "data/analysts/competing_hypotheses.py": 1840,  # 1666 @ 2026-08-02
    "data/filters/geocode.py": 1740,  # 1580 @ 2026-08-02
    "data/analysts/entity_researcher.py": 1740,  # 1580 @ 2026-08-02
    # 2026-08-29 (FRAME-3 steady-state guard + D2 90-day-wager daily page
    # budget + kill list). Two new sibling modules were extracted FIRST
    # (_steady_state_guard.py: the pure suppression classifier + its
    # guard-suppressed write path; _daily_page_budget.py: the budget
    # ranking/allocation + the kill-switch's shared advance-and-log path),
    # pulling ~340 lines of the new logic out before this measurement — what
    # remains in-file is handle()'s own orchestration wiring (five new
    # options, three new scan-result branches, the suppressed/killed/
    # budget-deferred receipt fields) and the module docstring's account of
    # the 2026-08-29 soak decision, neither of which has a clean further
    # extraction seam without separating the wiring from the handler it
    # wires. Seeded at the measured count + ~10%, the standard first-touch
    # allowance this file's own prior entries use.
    "data/analysts/deterministic_handlers/alert_trigger_scan.py": 2130,  # 1931 @ 2026-08-29; 1546 @ 2026-08-02
    # NEW ENTRANT 2026-08-29. alert_trigger_scan's FRAME-3 guard + D2 wager
    # (above) added five OptionSpec declarations — a flat, alphabetically-ish
    # grouped catalog of ~180 existing handlers' knobs with no per-handler
    # module boundary to split along (every entry already lives beside its
    # own handler's other options; the file's OWN section banners are the
    # only seams, and the alert_trigger_scan block they'd move with it is a
    # fraction of the total). NOT split for the same reason V-J1's entry
    # below gives for its own file. Seeded at measured + ~10%.
    "data/analysts/handler_options.py": 1670,  # 1518 @ 2026-08-29
    # NEW ENTRANT 2026-08-28 (V-J1). This module is itself the K-1 extraction of
    # verify.py's absence subsystem (903 lines then), and the hedged-conflict
    # guard put it over the 1,500 threshold: the predicate is ~45 lines and its
    # banner — the census, the three conjunctive conditions and the five
    # confirmed catches it must not reach — is the rest, which is the same
    # doctrine-beside-the-rule shape W1(e), V-G2, V-H4 and V-H5 already carry in
    # here. NOT split: it belongs beside the other route exclusions it is
    # ordered against, and there is no cohesive unit to move that would not
    # separate a rule from the exclusions it must stay consistent with. Seeded
    # at the measured count + ~10%, which is what a first-time entrant gets; the
    # next train pays for the next one.
    "data/provenance/absence_slice.py": 1720,  # 1558 @ 2026-08-28 (V-J1)
    # NEW ENTRANT 2026-08-29 (D5 standing external auditor). Five OptionSpec
    # declarations for the new `standing_auditor` sub-handler carried this over
    # the 1,500 threshold. NOT split: it is a flat catalog of ~180 handlers'
    # knobs with no per-handler module boundary to split along — every entry
    # already lives beside its own handler's other options, and the file's own
    # section banners are the only seams, each covering a fraction of the
    # total. Splitting it would also break the ONE property the X-1 catalog
    # exists for: a single dict a test can diff against SUB_HANDLERS to prove
    # no knob is unreachable. Seeded at measured + ~10%, the first-touch
    # allowance every prior entrant here got.
    #
    # NOTE for the merge: the unmerged `alert-suppression-guard` branch crosses
    # this same threshold in the same week for the same reason (its FRAME-3 +
    # D2 options) and pins the SAME number. Two trains, one ceiling — take
    # either side of the conflict.
}

_EXTRACT_DONT_RAISE = (
    "Do NOT raise the ceiling to make this pass. Extract a cohesive unit "
    "into a sibling module and re-seed the ceiling downward in the same "
    "commit — the section banners in these files are already the seams, and "
    "a split that re-exports the moved names from the original module is "
    "invisible to every importer (see planning/"
    "CODE_CLEANUP_ANALYSIS_2026-08-02.md section 4.2). If a ceiling raise is "
    "genuinely the right call, raising it here is a deliberate, reviewable "
    "line in the diff — say why in the commit message."
)


def _loc(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def _measured_src_modules() -> dict[str, int]:
    """Every ``src/legba/**/*.py`` on disk, keyed relative to ``src/legba``."""
    files = sorted(SRC_ROOT.rglob("*.py"))
    assert files, f"no python files found under {SRC_ROOT} — wrong checkout?"
    return {p.relative_to(SRC_ROOT).as_posix(): _loc(p) for p in files}


def test_pinned_modules_are_under_their_ceilings() -> None:
    """No pinned module may exceed its LOC ceiling."""
    measured = _measured_src_modules()
    breaches: list[str] = []
    for rel, ceiling in sorted(CEILINGS.items()):
        loc = measured.get(rel)
        if loc is None:
            continue  # handled by test_ceiling_list_has_no_stale_entries
        if loc > ceiling:
            breaches.append(
                f"src/legba/{rel}: {loc} lines > ceiling {ceiling} "
                f"(+{loc - ceiling})"
            )
    assert not breaches, (
        "Module-size ceiling breached — these files grew past the limit "
        "pinned by the phase-1A regrowth gate:\n  "
        + "\n  ".join(breaches)
        + "\n\n"
        + _EXTRACT_DONT_RAISE
    )


def test_no_unpinned_module_crosses_the_threshold() -> None:
    """A module that crosses 1,500 lines must join the pinned list."""
    measured = _measured_src_modules()
    entrants = [
        f"src/legba/{rel}: {loc} lines"
        for rel, loc in sorted(measured.items())
        if loc >= ENTRY_THRESHOLD and rel not in CEILINGS
    ]
    assert not entrants, (
        f"New module(s) crossed the {ENTRY_THRESHOLD}-line threshold without a "
        f"pinned ceiling:\n  "
        + "\n  ".join(entrants)
        + "\n\nSplit it, or add it to CEILINGS in "
        "tests/test_module_size_gate.py with its measured count + ~10%.\n\n"
        + _EXTRACT_DONT_RAISE
    )


def test_ceiling_list_has_no_stale_entries() -> None:
    """A ceiling must still point at a file, and must still constrain it.

    A module deleted or split away keeps a dead entry alive here; a module
    that shrank far below its ceiling (an extraction landed) is no longer
    gated at all. Both are the same failure — the number stopped tracking
    the code.
    """
    measured = _measured_src_modules()
    problems: list[str] = []
    for rel, ceiling in sorted(CEILINGS.items()):
        loc = measured.get(rel)
        if loc is None:
            problems.append(
                f"src/legba/{rel}: no such file — delete this entry "
                f"(or fix the path if the module moved)"
            )
            continue
        if loc * STALE_CEILING_FACTOR < ceiling:
            problems.append(
                f"src/legba/{rel}: {loc} lines against a ceiling of {ceiling} "
                f"— the file shrank, so re-seed the ceiling to ~{int(loc * 1.1)} "
                f"and keep the ratchet tight"
            )
    assert not problems, (
        "Stale entries in the module-size ceiling list:\n  "
        + "\n  ".join(problems)
        + "\n\nCeilings only work while they track the code. When a split "
        "lands, lower that file's ceiling in the same commit."
    )

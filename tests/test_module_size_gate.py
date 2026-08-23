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
    "data/provenance/verify.py": 5910,  # 5853 @ 2026-08-05 — the V-D/W2/V-G1/V-G3 QUOTE RULES extracted to judge_quote_rules.py and the CITATION MARKER parsing/drift set to citation_markers.py (judge-subsystem bricks 4 and 5, the seam named in the banner above), ceiling re-seeded down twice in the V-I train. DO NOT RAISE.
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
    "data/analysts/meta_findings_synthesizer.py": 5250,  # 5192 @ 2026-08-21 (VOICE-4); 5585 @ 2026-08-20 (FRAME-2); 5750 @ 2026-08-20 (FRAME-1); 5279 @ 2026-08-02
    "runtime/dapr_actors.py": 4110,  # 3735 @ 2026-08-02
    "data/analysts/inline_target.py": 3930,  # 3882 @ 2026-08-05 — slice rendering extracted to slice_render.py, ceiling re-seeded down
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
    "data/registry/v3_api.py": 2430,  # 2209 @ 2026-08-02
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
    "data/analysts/deterministic_handlers/alert_trigger_scan.py": 1710,  # 1546 @ 2026-08-02
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

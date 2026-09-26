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
    # LOWERED AGAIN 2026-09-04 (D-2, the assembler): the seam named above since
    # FRAME-2 was FINALLY TAKEN. The region / world / thematic SLICE-ASSEMBLY
    # branches (`_assemble_world_region_slice` / `_assemble_thematic_unit_slice`),
    # their roster + membership resolvers, `_is_region_target` and the whole
    # per-mode coverage vocabulary moved to `data/analysts/composition_slice.py`
    # (571 lines), which this module imports ONE WAY and re-exports — so
    # `synth.REGION_MODE_GAP`, `synth._assemble_world_region_slice` and every
    # test that reaches for them resolve unchanged. The one inverted dependency
    # (the assemblers' BASIS gather) is injected as `basis_reader` at the
    # `READ_SLICE` call sites, the same shape `composition_window.
    # read_floor_fallback_heads` already uses. The train ADDED the flag-gated
    # assembly wiring and the file still came down 341 lines; the ratchet closes
    # on the new floor. NEXT SEAM in this file: `_run` itself, which is ~1,150 of
    # the remaining lines and splits at the PROMPT-ASSEMBLY boundary (the
    # `_PromptBlockAssembler` splice and its ~12 `_blocks.add` calls are one
    # cohesive "what is this turn shown" unit, and under the assembly flag they
    # are on the branch that does not run).
    # LOWERED AGAIN 2026-09-04 (D-5 CASCADE): the cascade needed lines in a file
    # with 54 of them, and 54 lines of headroom is not a budget — the same
    # sentence D-2's handoff used. So the COVERAGE-RENDER seam was taken first:
    # ``_render_region_coverage_block`` / ``_render_world_aperture_block`` /
    # ``_render_desk_coverage_block`` moved to ``data/analysts/composition_slice.py``
    # beside the ``REGION_MODE_*`` / ``THEMATIC_MODE_*`` vocabulary they are the
    # only readers of, and the synthesizer imports them ONE WAY and re-exports
    # them. The train ADDED the whole region-rollup + world-over-countries wiring
    # and the file still came down 39 lines from D-2's floor; the ratchet closes
    # on the new floor. Next seam in this file is UNCHANGED and still named:
    # ``_run`` itself, splitting at the PROMPT-ASSEMBLY boundary (the
    # ``_PromptBlockAssembler`` splice and its ~12 ``_blocks.add`` calls), which
    # under the assembly/rollup regimes is on the branch that does not run.
    # LOWERED AGAIN 2026-09-06 (the PROMPT-ASSEMBLY extraction): the seam named
    # by BOTH notes above was finally taken, and it had to be — the file sat at
    # EXACTLY 4,820 against a 4,820 ceiling. Zero lines of headroom is not a
    # budget; it is a stop. ``_run``'s ``--- PLAN ---`` block (the base render,
    # the R2 contradiction detection over the shown ordinals, and the eleven
    # guarded ``_blocks.add`` splices), the ``_PromptBlockAssembler`` interface
    # with ``_BLOCK_APPEND`` / ``_BLOCK_PREPEND`` / ``_PROMPT_CHARS_PER_TOKEN``,
    # the three block renderers with no reader outside that splice
    # (``_render_contested_block`` / ``_render_contested_absent_line`` /
    # ``_render_freshness_advisory_block``) and the R2 ledger projection
    # ``_verified_claim_texts`` moved to
    # ``data/analysts/composition_prompt_assembly.py`` (606 lines), which this
    # module imports ONE WAY and re-exports — so ``synth._PromptBlockAssembler``,
    # ``synth._render_contested_block`` and every historical name resolve
    # unchanged, and no test was edited.
    #
    # The one inverted dependency is the RENDERERS themselves, and it is paid the
    # way D-2 paid the assemblers' basis gather: they are INJECTED, as a
    # ``PromptRenderers`` bundle ``_run`` builds at the call site from its own
    # globals. That is not style. ``test_composer_prompt_block_equivalence``
    # proves byte identity against the pre-C-4 ad-hoc splice by
    # ``monkeypatch.setattr(synth, name, spy)`` over its nine ``_BLOCK_FNS``;
    # resolving the renderers in the sibling would have made those patches
    # invisible and turned the load-bearing proof into a proof about nothing.
    # Injection keeps every lookup in this module's namespace at call time,
    # exactly where the inline ``lambda:`` closures did it.
    #
    # PURE REFACTOR: no behavior moved with the lines. Proven by replay over 12
    # real live composition rows (country / world / thematic, assembly regime and
    # legacy regime) — rendered prompt sha256 AND ``build_assembly`` payload
    # sha256 identical on the old and new paths, every row. The ratchet closes on
    # the new floor. NEXT SEAM in this file: the CITE resolution
    # (``_extract_ref_markers`` + the ordinal→citation walk + the continuity /
    # window-ledger / register citation shapes), ~200 lines that read
    # ``finding.body`` and the ordinal index and nothing else.
    # LOWERED AGAIN 2026-09-06 (the CITE extraction): three merges landed on this
    # file the same night — the rollup citation-ORDER fix, the carry-by-mass fix
    # and the world-read consistency fix. Each cleared 4,580 alone; together they
    # measured 4,612 (+32). The seam was already named by the entry above, on the
    # way out of the PROMPT-ASSEMBLY train: the CITE resolution, which reads
    # ``finding.body`` and the ordinal index and nothing else. It moved whole to
    # ``data/analysts/composition_citations.py`` (509 lines) — the two marker
    # grammars (``[[ref:N]]`` / ``[[contested:<uuid>]]``) with the two resolvers
    # that hold their shared drop-and-count honesty contract, the ONE citation
    # shape ``_build_composition_citation`` with the constants that bound it
    # (``MAX_EVIDENCE_TEXT_CHARS``, ``_FALLBACK_BASIS_CITATIONS_CAP``), and
    # ``_run``'s whole ``--- CITE ---`` block: the ordinal INDEX (basis →
    # periphery → continuity, or the rollup's own roster order), the walk with its
    # window-ledger / situation-register / prior-read citation shapes, and the A2
    # unmarked-basis fallback. ``_coerce_uuid`` moved with them — a
    # zero-dependency leaf whose two heaviest readers are in that set, and moving
    # it rather than injecting it is what keeps the sibling's imports strictly
    # one-directional. The synthesizer imports the lot ONE WAY and re-exports it,
    # so ``synth._extract_ref_markers``, ``synth._build_composition_citation``,
    # ``synth._coerce_uuid``, ``synth.MAX_EVIDENCE_TEXT_CHARS`` and every other
    # historical name resolve unchanged; ``__all__`` is byte-identical and NO test
    # file was edited (only the two ceilings below).
    #
    # The one inverted dependency is ``_render_situation_register_lines``, and it
    # is paid the way the PROMPT-ASSEMBLY unit paid its renderers: INJECTED from
    # ``_run``'s namespace at the call site. ``test_composer_prompt_block_
    # equivalence`` corrupts that name with ``monkeypatch.setattr(synth, ...)``;
    # resolving it in the sibling would make the patch invisible to the one path
    # that calls it.
    #
    # PURE REFACTOR: no behavior moved with the lines. All eight moved
    # definitions are character-identical to ``d954825d`` by an AST source-segment
    # pass, and the moved ``_run`` block is character-identical after the dedent
    # out of its ``if is_composition:`` guard plus the two documented parameter
    # renames. Proven live by replay over the last 16 real composition rows
    # (country / region / world) — rendered prompt sha256, ``build_assembly`` /
    # rollup payload sha256, the ``citations`` array byte-for-byte and the finding
    # envelope identical on the old and new paths, every row; a negative control
    # that perturbs one moved line turns it red. The ratchet closes on the new
    # floor. NEXT SEAM in this file: the C-TIER periphery / basis gather now fed
    # by ``composition_slice.world_admissible_analyst_ids()``, and after it the
    # rollup-assembly call site around ``_rollup.assemble_region_rollup``.
    "data/analysts/meta_findings_synthesizer.py": 4360,  # 4311 @ 2026-09-06 (CITE); 4525 @ 2026-09-06 (PROMPT-ASSEMBLY); 4767 @ 2026-09-04 (D-5 cascade); 4790 @ 2026-09-04 (D-2 assembler); 5131 @ 2026-08-29 (envelope leak); 5192 @ 2026-08-21 (VOICE-4); 5585 @ 2026-08-20 (FRAME-2); 5750 @ 2026-08-20 (FRAME-1); 5279 @ 2026-08-02
    # PINNED FROM BIRTH 2026-09-06, BELOW the entry threshold — a deliberate
    # deviation from this file's own rule, made at the extraction's direction.
    # The house precedent is the opposite (``composition_slice.py`` at 571 and
    # ``region_rollup.py`` at 775 carry no entry, because the gate only REQUIRES
    # one at 1,500). The reason to deviate here: this module is where the
    # synthesizer's next ~300 lines of prompt work will land by construction —
    # every new composition block is an ``add`` call and a renderer — so it is
    # the one sibling with a known growth vector, and a ceiling from birth costs
    # nothing while it holds. Seeded at measured + ~10%, the first-touch
    # allowance. If a train needs the room, the seam is the same one this file's
    # entries always name: move the renderers out from under the splice.
    "data/analysts/composition_prompt_assembly.py": 660,  # 606 @ 2026-09-06
    # PINNED FROM BIRTH 2026-09-06 (the CITE extraction), BELOW the entry
    # threshold, for the same reason the entry above deviates: this module has a
    # KNOWN growth vector. Every new citation shape is another branch in the walk
    # (the window ledger and the situation register each added one), and the
    # verify side keeps asking the citation to carry more — ``evidence_text``,
    # ``effective_confidence``, ``derived_from``, ``produced_at`` all arrived
    # after the shape was written. Seeded at measured + ~10%, the first-touch
    # allowance. If a train needs the room, the seam is the CONTESTED half:
    # ``_CONTESTED_MARKER_RE`` + ``_extract_contested_markers`` answer a
    # different question (a uuid against an allowed set, not an ordinal against a
    # range) and have exactly one caller, in ``_run``'s CONTESTED block.
    "data/analysts/composition_citations.py": 560,  # 509 @ 2026-09-06 (CITE, birth)
    # PINNED FROM BIRTH 2026-09-06 (R1-a), same deviation and the same reason.
    # ``_frame_anchor.py`` is a bar with a KNOWN growth vector: R1-b binds it to
    # ``situation_clustering`` and the design already names what would land here
    # if it landed anywhere (the retirement sweep, the name legs, the D-k clock
    # read) — the R1-b row explicitly routes those to ``_frame_content.py``
    # instead, and this ceiling is what keeps that routing honest. Seeded at
    # measured + ~10%. If a train needs the room, the seam is the matcher:
    # ``PolitySurfaceIndex`` is self-contained and moves next door cleanly.
    "data/_frame_anchor.py": 790,  # 718 @ 2026-09-06 (R1-a, birth)
    "runtime/dapr_actors.py": 4110,  # 3735 @ 2026-08-02
    "runtime/liveness_watchdog.py": 1660,  # 1507 @ 2026-08-30 — merge-wave entrant (honest-quiet dynamic window + prolonged-streak escalation)
    "data/analysts/inline_target.py": 3915,  # 3909 @ 2026-08-25 — the per-clean render RECEIPT (_slice_render_stats) extracted to slice_render.py beside the render it describes, paying for the task-#57 wire-pair collapse hook; ceiling re-seeded down again (was 3930)
    "runtime/substrate_query_port.py": 3910,  # 3551 @ 2026-08-02
    "data/analysts/journal_assessor.py": 3000,  # 2723 @ 2026-09-23 (H10 — the PROPOSE phase [_propose_phase/_propose_phase_prompt], its constants, and _JOURNAL_PROPOSE_TOOL_SCHEMAS extracted verbatim to journal_propose_phase.py and re-exported, after H10's write-time shape-validator prompt text pushed the module 9 lines past the ceiling); 2859 @ 2026-09-09 (B5 — the ref-repair pre-REFLECT step + its honesty-flag/data wiring); 2828 @ 2026-09-09 (B2 — the [routine] DISCIPLINE line + honesty-flag wiring for T1.4); 2813 @ 2026-09-09 (B1 — the [instrument] window tag + DISCIPLINE line + the honesty-flag wiring for T1.2); 2767 @ 2026-09-09 (B0 — the priming-slice SELECTION machinery extracted verbatim to journal_slice.py and re-exported); 2831 @ 2026-09-09 (T1.0 — the REFLECT claim machinery extracted verbatim to journal_reflect.py and re-exported); 2965 @ 2026-08-11 (leak guards extracted)
    # BIRTH 2026-09-09 (T1.0): the REFLECT claim machinery pulled out of
    # journal_assessor.py (see its ceiling comment above) when three follow-on
    # lanes were about to add to both the REFLECT pass and slice selection.
    # 184 lines at birth — the module-size gate's own STALE_CEILING_FACTOR
    # (1.5x) caps how much headroom a freshly-seeded small module can carry
    # (a flat +300 here would immediately trip the stale-ceiling check), so
    # this pins near that cap rather than the full +300 asked for. T1.1/T1.2/
    # T1.4 land here — re-seed this ceiling upward (with real headroom) as
    # each one lands, same commit, same as every other extraction in this file.
    # RAISED 2026-09-09 (B1/B2/B5): the instrument/routine honesty-flag
    # detectors (_flag_instrument_as_report / _flag_routine_as_signal) and
    # the ref-repair machinery (_repair_ref_markers / _hamming / _hex32)
    # landed here, 184 -> 348. Seeded at measured + ~10%, rounded up.
    "data/analysts/journal_reflect.py": 390,  # 348 @ 2026-09-09 (B1/B2/B5); 184 @ 2026-09-09 (T1.0 birth)
    # BIRTH 2026-09-09 (B0, the T2.2 seam taken early): the priming-slice
    # SELECTION machinery — _select_journal_slice, _salience_ordered,
    # _slice_recency_key, and the render-cap/fresh-reserve constants — pulled
    # out of journal_assessor.py so the B1 (T1.2 instrument label) and B2
    # (T1.4 routine label) lanes have a small, focused module to grow instead
    # of adding to journal_assessor.py's own ceiling. 100 lines at birth;
    # pinned near the module-size gate's own STALE_CEILING_FACTOR (1.5x) cap
    # the same way journal_reflect.py was. Re-seed upward (same commit) as
    # B1/B2 land their labelling logic here.
    # RAISED 2026-09-09 (B1, T1.2): instrument-row labelling
    # (_is_instrument_row / _row_raw_title / _labeled_journal_slice) landed
    # here, 100 -> 159. B2 (T1.4 routine label) lands next, same shape.
    "data/analysts/journal_slice.py": 230,  # 159 @ 2026-09-09 (B1); 100 @ 2026-09-09 (B0 birth)
    # BIRTH 2026-09-10 (T2.2, the cluster-first slice): the journal's SECOND
    # selection axis — anchor+geo grouping over the candidate pool, the
    # mass ranking, the cluster-first window fill, the '▸' header render and
    # the context-only desk roster. It lives beside journal_slice.py rather
    # than inside it because journal_slice.py's own ceiling has ~20 lines of
    # headroom and this is 540 lines of new machinery; splitting also keeps
    # B0's byte-identity proof pointed at a module the flag-on path never
    # edits. Well under the gate's 1,500-line ENTRY_THRESHOLD — pinned anyway,
    # the same deliberate deviation journal_slice.py / journal_reflect.py made,
    # because this module has a named growth vector (the P3 relation-judge lane
    # reads the same clusters). Seeded at measured + ~10%.
    "data/analysts/journal_clusters.py": 760,  # 690 @ 2026-09-10 (T2.2 birth)
    "runtime/grounding.py": 3000,  # 2719 @ 2026-08-02
    # LOWERED 2026-09-06 (extract-deps-and-options): the two per-kind WIRING
    # blocks the 09-05 merge wave added — standing_auditor's (D5) LLM leg +
    # web_access leg + W-4 grader wiring, and desk_reference's (A-1) matching
    # two-leg wiring — moved to the new `runtime/analyst_deps_kinds.py` leaf,
    # which this module imports ONE WAY and re-exports, so
    # `analyst_deps_builder.wire_standing_auditor_kind_deps` /
    # `.wire_desk_reference_kind_deps` resolve and every existing call site
    # (`_build_deterministic`) stays byte-identical. `_wire_deterministic_llm`
    # and `build_llm_handler_from_stack_component` are INJECTED into the leaf
    # as callables (the same shape `external_grader_route.wire_external_grader`
    # already uses) rather than imported back, so the new module depends on
    # nothing in this one. Ratchet closes on the new floor.
    "runtime/analyst_deps_builder.py": 3000,  # 2984 @ 2026-09-06 (standing_auditor + desk_reference WIRING extracted to analyst_deps_kinds.py); 2715 @ 08-02; 3035 @ 09-05 merge wave (W-4 grader + A-1 desk_reference wiring)
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
    # 2026-09-20: the pass PLANNER (fingerprint, budget, prior-group prefetch)
    # went to the sibling fact_contention_pass.py; what landed here is the
    # call-site wiring plus the prose that records why the hourly pass was
    # holding its actor turn for a median 651 s. +170 over the old ceiling's
    # headroom, re-seeded once at the new measurement.
    "data/analysts/deterministic_handlers/fact_contention_arbiter.py": 2070,  # 1968 @ 2026-09-20 (was 1980, # 1798 @ 2026-08-02)
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
    # LOWERED 2026-09-06 (extract-deps-and-options): the three per-feature
    # blocks the 09-05 merge wave added — external grading width (W-9),
    # desk_reference (A-1), research_measurement (R-D) — moved to the new
    # `data/analysts/handler_options_programs.py`, which THIS module imports
    # (with underscore-aliased names, so the public surface — everything
    # `dir(handler_options)` shows minus underscores — is unchanged) and
    # splices back into `HANDLER_OPTIONS` at the same keys: `desk_reference`
    # and `research_measurement` verbatim, `standing_auditor` as its
    # pre-existing five-spec D5 entry followed by the six-spec width block, so
    # every key, spec and order in the catalog is byte-identical (verified by
    # a full field-level JSON diff against the pre-extraction catalog). The
    # sibling imports `OptionSpec` + the terse constructor helpers back from
    # this module rather than duplicating them — safe because this module is
    # its ONLY importer, so the partial circular import always resolves; see
    # `handler_options_programs.py`'s own docstring. Ratchet closes on the new
    # floor.
    # LOWERED 2026-09-24 (H15, wave G). The file sat AT its 1,660 ceiling:
    # every lane that added a knob or a kind fought the line count. Both
    # assembled catalogs — `HANDLER_OPTIONS` and `ANALYST_KIND_OPTIONS` — plus
    # their catalog-local spec constructors (`_nonneg_int` / `_unit_float` /
    # `_nonneg_float` / `_pos_float` / `_flag` / `_edge_families`) and the
    # splice-in of the events/inquiry/programs sibling catalogs moved to the
    # new `data/analysts/handler_options_catalog.py` (1,327 lines — under the
    # 1,500 new-entrant threshold, no ceiling needed there), which THIS module
    # imports and re-exports both dicts from under the same names, so every
    # existing importer and `dir(handler_options)`'s declared `__all__` stay
    # unchanged. Byte-identical: `tests/data_pkg/test_handler_options_catalog_split.py`
    # pins a SHA-256 fingerprint of both dicts taken from THIS pin (79fd1fd9)
    # before the split. What remains here is the `OptionSpec` MACHINERY only
    # — the reserved-key table, the two dataclasses, the resolve/degrade
    # traversal and the two `known_*_option_names` lookups. Ratchet closes on
    # the new floor (362 @ 2026-09-24, seeded +~10% rounded to a multiple of
    # 10). DO NOT re-widen this file with catalog entries — they belong in
    # `handler_options_catalog.py` now.
    "data/analysts/handler_options.py": 400,  # 362 @ 2026-09-24 (H15 — HANDLER_OPTIONS/ANALYST_KIND_OPTIONS assembly extracted to handler_options_catalog.py); 1630 @ 2026-09-06 (width + desk_reference + research_measurement extracted to handler_options_programs.py); 1518 @ 08-29; 1699 @ 09-05 merge wave (width + desk_reference + research_measurement knobs)
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
    # NEW ENTRANT 2026-09-05 (the VERIFY-REGIME FIX, stamp 2026-09-05/1). D-3
    # shipped this brick at 1,406 — deliberately under the threshold, because
    # ``verify.py`` had ~130 lines of ceiling and the arms are ~700 of logic.
    # The regime fix added ~140: ARM 2's scope-token predicate and its argument,
    # and ``regrade_to_arms`` — the rule that an assembly row's headline is the
    # arms' ratio — with the paragraph explaining why deferring to the arms is
    # the honest fix and skipping the legacy branches is not. NOT split, and the
    # alternative was worse in the one way that matters: the regrade could have
    # been a sixth call site in ``verify.py`` (49 lines of headroom, ceiling
    # explicitly marked DO NOT RAISE), and putting it there would have separated
    # the rule from the arms whose arithmetic it publishes. Seeded at the
    # measured count + ~10%, which is what a first-time entrant gets; the next
    # train pays for the next one. The named seam if it grows again: ARM 4
    # (selection honesty + the drop-ledger enum) is self-contained and reads the
    # payload only.
    "data/provenance/assembly_arms.py": 1710,  # 1569 @ 2026-09-05
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
    #
    # NEW ENTRANT 2026-09-06 (G3, the weighted-comparison licence). The G3
    # lineage entry — one stamp, both required paragraphs, the per-family shift
    # declaration and the counter census — carried this from 1,455 to 1,583.
    # NOTHING WAS RAISED: this module had no ceiling, and this is the entrant
    # allowance the gate's own text offers ("it joins the list (with a ceiling)
    # or it gets split").
    #
    # NOT SPLIT, and the reason is what the file IS. It is ~1,150 lines of PROSE
    # LINEAGE — one entry per stamp, each arguing in writing why a population
    # boundary exists — followed by the structured registry transcribed from it.
    # The two are held together by ``test_every_prose_lineage_stamp_has_a_registry
    # _entry``, THE EXHAUSTIVE PIN, which reads the prose with
    # ``inspect.getsource`` on this one module and asserts set-equality with
    # ``STAMP_EXPECTED_SHIFTS``. Splitting the registry off would put the pin's
    # two halves in two modules and make the prose reachable only by a
    # cross-module source read — weakening the one guard that stops a stamp being
    # declared without being justified. A code split cannot pay for a prose file.
    #
    # THE NAMED SEAM, if it grows again: the OLDEST entries. This banner is
    # append-only and every entry below ``2026-08-15/1`` describes a pipeline no
    # live row is graded under; moving the pre-August half to
    # ``judge_pipeline_lineage_archive.py`` and having ``_prose_stamps`` read BOTH
    # (archive first, so lineage order survives) keeps the exhaustive pin total
    # while halving this file. That is a train of its own, not a rider on a
    # stamp-bearing one.
    # RAISED 2026-09-20 (PARTIAL VERDICTS + THE EVIDENCE ENVELOPE), 1750 ->
    # 1930, and it is the SECOND raise on the same terms the entrant note above
    # sets out. The two stamp-bearing trains since G3 each added one prose entry
    # and one registry entry — 09-08/1 carried the file 1,583 -> 1,713 and
    # 09-20/1 carries it 1,713 -> 1,855 — and neither added a line of CODE: the
    # module's only executable surface is still ``expected_shift`` /
    # ``poolable_stamps`` over a dict, unchanged since 2026-08-29. THE EXTRACT-
    # DON'T-RAISE RULE HAS NOTHING TO EXTRACT HERE and the banner above says why
    # in terms — a code split cannot pay for a prose file, and splitting the
    # registry off would put the two halves of ``test_every_prose_lineage_stamp_
    # has_a_registry_entry`` in two modules. The NAMED SEAM is unchanged and is
    # still a train of its own: archive every entry below ``2026-08-15/1``
    # (pipelines no live row is graded under) to
    # ``judge_pipeline_lineage_archive.py`` and have ``_prose_stamps`` read
    # both. ~75 lines of headroom, which is half a stamp entry — the next train
    # that needs the other half is the one that should take the seam.
    "data/provenance/judge_pipeline_version.py": 1930,  # 1855 @ 2026-09-20
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

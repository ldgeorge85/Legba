# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Shared PROGRAM_FLAG pin table for flags exercised from BOTH
``tests/data_pkg`` and ``tests/runtime``.

An earlier isolation pass pinned
``tests/data_pkg/conftest.py``'s ``_PROGRAM_FLAG_DEFAULTS`` to the shipped
default for every LEGBA_* program flag the live ``.env`` leaks into the test
process (see that module's docstring for the mechanism — candidate 3 of
``legba.data.config._load_env()``'s search always resolves to the operator's
LIVE file, on this host, from any worktree). That pass deliberately left four
flags out of the ``tests/data_pkg`` pin, because their code path is ALSO
exercised from ``tests/runtime`` — a data_pkg-only pin would have been a
false sense of closure (a leak still open in the sibling suite).

This module is the single source of truth for those entries (four at birth,
five since GEO ROUTING v2, six since NATIVE TOOL ROUNDS) so neither
conftest carries a second copy of the table:

  * ``tests/data_pkg/conftest.py`` splices this tuple onto the end of its own
    ``_PROGRAM_FLAG_DEFAULTS`` (under a dated comment header) so the existing
    16-entry pin grows by exactly this tuple's length, unchanged in mechanism.
  * ``tests/runtime/conftest.py`` imports this tuple directly for its own
    autouse pin fixture — scoped to exactly these, since the other 16
    ``tests/data_pkg`` entries gate behavior ``tests/runtime`` never touches.

Each entry: ``(env var, shipped/code default, one line on why + where the
default is asserted in code)``.
"""

from __future__ import annotations

RUNTIME_SHARED_PROGRAM_FLAG_DEFAULTS: tuple[tuple[str, str, str], ...] = (
    ("LEGBA_VERIFY_LLM_JUDGE", "off (unset/falsey)",
     "verify.py:_llm_judge_enabled (~L1216-1226) — code-default OFF, "
     "deterministic judge-unavailable floor; read from tests/data_pkg "
     "(the verify.py test family) AND "
     "tests/runtime/test_verify_judge_wiring.py"),
    ("LEGBA_JUDGE_STACK_REF", "unset (empty string — no global override)",
     "analyst_deps_builder.py:resolve_judge_route_from_llm_block rung 1 "
     "(~L2686-2688) — an empty env falls through the ladder to "
     "method.llm.judge / .verify / .primary; read from tests/data_pkg "
     "(test_judge_profile_resolution_pinned.py, "
     "test_external_grading_width.py) AND tests/runtime "
     "(test_verify_judge_wiring.py, test_judge_metering.py)"),
    ("LEGBA_WORLD_CONTEXT_DISABLED_UNITS", "unset (empty set — no unit disabled)",
     "rag_rollback.py:world_context_disabled_units (~L205-217) — env list "
     "unions with persisted state, empty when unset; tested exclusively "
     "under tests/runtime (test_rag_rollback.py, "
     "test_grounding_world_context.py)"),
    ("LEGBA_RAG_ROLLBACK_STATE", "unset (no state file — empty persisted state)",
     "rag_rollback.py:_state_path / _load_state (~L188-202) — no path means "
     "no state file is read, degrading to {}; tested exclusively under "
     "tests/runtime (test_rag_rollback.py, "
     "test_grounding_world_context.py)"),
    # 2026-09-07, GEO ROUTING v2. Flag-off byte-identity is the R4 contract for
    # this program, so a leaked ON from the live .env would silently change
    # which signals enter every desk's slice — the exact blast radius the flag
    # exists to hold shut. Pinned in BOTH suites: the reader is
    # runtime/actor_substrate_slice.py (tests/runtime) and the rule module is
    # data/_geo_routing.py (tests/data_pkg).
    ("LEGBA_SLICE_GEO_V2", "off (unset/falsey)",
     "_geo_routing.py:slice_geo_v2_enabled — code-default OFF; only the "
     "values in _TRUTHY turn it on, so an ambiguous value stays off. Read "
     "from tests/data_pkg (test_geo_routing_v2.py) AND tests/runtime "
     "(test_actor_substrate_slice_scope.py)"),
    # 2026-09-16, NATIVE TOOL ROUNDS. The orchestrator flips this ON at deploy,
    # so once it is live the operator's .env carries it — and candidate 3 of
    # _load_env() resolves to that LIVE file from any worktree. Unpinned, every
    # flag-off byte-identity test in this program would start running the
    # native protocol and pass or fail for reasons unrelated to what it asserts.
    ("LEGBA_AGENCY_NATIVE_TOOLS", "off (unset/falsey)",
     "gather_native.py:native_tools_enabled — code-default OFF; only the "
     "values in _TRUTHY turn it on. Read from tests/data_pkg "
     "(test_native_tool_rounds.py, the GATHER loop) AND tests/runtime "
     "(the actor path that drives it)"),
    # 2026-11-14, D-1 SOURCE DRIFT. The desk slice's "source text changed"
    # mark is a flag-off byte-identity flag like GEO v2: a leaked ON from the
    # live .env would add a line to every marked row of every desk prompt. The
    # reader is runtime/actor_substrate_slice.py, the mark and render live in
    # data/ (evidence_drift_slice.py, slice_render.py).
    ("LEGBA_SLICE_SOURCE_DRIFT_MARK", "off (unset/falsey)",
     "evidence_drift_slice.py:slice_drift_mark_enabled — code-default OFF; "
     "read from tests/data_pkg (test_evidence_drift_classify.py, "
     "test_evidence_drift_db.py) AND the runtime slice reader"),
    # 2026-11-14, D-2 DRIFT ASSESSMENT. The model step and the tracked
    # auto-close are default-OFF switches read by data/ (the assessment
    # store) and by the runtime drift loop; a leaked ON from the live .env
    # would call a model handler / close flags inside any test that runs a
    # drift pass.
    ("LEGBA_DRIFT_ASSESS_MODEL", "off (unset/falsey)",
     "evidence_drift_assess_store.py:model_enabled — code-default OFF; read from "
     "tests/data_pkg (test_evidence_drift_assess*.py) AND runtime/evidence_drift_loop.py"),
    ("LEGBA_DRIFT_AUTO_CLOSE", "off (unset/falsey)",
     "evidence_drift_assess_store.py:auto_close_enabled — code-default OFF; read from "
     "tests/data_pkg (test_evidence_drift_assess*.py) AND runtime/evidence_drift_loop.py"),
    # 2026-11-14, D-1 DESK ROWS SHADOW. A comma list of analyst ids whose desk
    # runs get a detached contract-v2 shadow (a model call + a ledger write).
    # Read by data/ (the launch in inline_target) and by the runtime deps
    # builder; a leaked value from the live .env would launch shadows inside
    # any desk test.
    ("LEGBA_DESK_ROWS_SHADOW", "unset (empty — no desk shadowed)",
     "desk_rows_shadow.py:shadow_analysts — code-default empty; read from "
     "tests/data_pkg (test_desk_rows_*.py, the inline_target run path) AND "
     "runtime/desk_rows_shadow_wiring.py"),
    # DM-A (2026-11-15): citations as rows. Every finding write (data_pkg's
    # writes tests AND the runtime's actor path) reads both.
    ("LEGBA_CITATION_ROWS", "on (unset — the dual write runs)",
     "citation_rows_store.py:citation_rows_enabled — code-default ON; a leaked "
     "0 would silently skip the rows inside every finding-write test"),
    ("LEGBA_CITATION_REFUSE_UNRESOLVED", "off (unset)",
     "citation_rows_store.py:refuse_unresolved_enabled — code-default OFF; a "
     "leaked ON would refuse every test finding with an id-less citation"),
)

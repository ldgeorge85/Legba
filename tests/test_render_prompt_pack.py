# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Pure-helper tests for ``scripts/render_prompt_pack.py``.

No DB, no registry, no docker: these pin the invariants that make the pack's
byte-faithfulness claims TRUE rather than asserted —

  * the replay digest is byte-identical to the live receipt pipeline
    (``run_accounting.prompt_digest`` over the base handler's wire translation);
  * the SELECT-only guard actually refuses writes;
  * the psql literal inliner round-trips the shapes the conn-shim feeds it;
  * trace selection prefers a fully-replayable trace for verification.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

import pytest


def _load_script():
    spec = importlib.util.spec_from_file_location(
        "render_prompt_pack", REPO_ROOT / "scripts" / "render_prompt_pack.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rpp = _load_script()


# ---------------------------------------------------------------------------
# The digest invariant — the whole verification story rests on this
# ---------------------------------------------------------------------------


def test_wire_digest_matches_live_receipt_pipeline():
    """``wire_digest`` must equal what the runtime records: the base handler
    prepends the system message (``_translate_messages``, wire_system=None)
    and ``run_accounting.prompt_digest`` hashes ``{"system": None,
    "messages": wire}``. If either side drifts, every MATCH/MISMATCH verdict
    in the pack is meaningless."""
    from legba.data.run_accounting import prompt_digest
    from legba.data.stack.llm.base import LLMProviderHandler

    system = "SYSTEM prompt with unicode — 【3】 and “quotes”"
    user = "USER prompt\nline two [1] snippet=…"
    wire, wire_system = LLMProviderHandler._translate_messages(
        object.__new__(LLMProviderHandler),  # default impl takes no state
        [{"role": "user", "content": user}], system=system,
    )
    assert wire_system is None
    live_sha, live_chars = prompt_digest(wire, wire_system)
    replay_sha, replay_chars = rpp.wire_digest(system, user)
    assert replay_sha == live_sha
    assert replay_chars == live_chars


# ---------------------------------------------------------------------------
# SELECT-only guard
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("sql", [
    "DELETE FROM signals",
    "UPDATE analyst_traces SET status='x'",
    "SELECT 1; DROP TABLE signals",
    "INSERT INTO analyst_outputs VALUES (1)",
    "  drop table x",
])
def test_assert_select_refuses_writes(sql):
    with pytest.raises(ValueError):
        rpp._assert_select(sql)


def test_assert_select_accepts_selects():
    rpp._assert_select("SELECT 1")
    rpp._assert_select("  WITH t AS (SELECT 1) SELECT * FROM t")


# ---------------------------------------------------------------------------
# psql literal inlining (the conn shim)
# ---------------------------------------------------------------------------


def test_sql_lit_shapes():
    assert rpp.sql_lit(None) == "NULL"
    assert rpp.sql_lit(True) == "TRUE"
    assert rpp.sql_lit(7) == "7"
    assert rpp.sql_lit("o'brien") == "'o''brien'"
    assert rpp.sql_lit(["a", "b'c"]) == "ARRAY['a', 'b''c']"


def test_psql_conn_inline_replaces_descending():
    # $10 must not be clobbered by the $1 substitution.
    sql = rpp.PsqlConn._inline(
        "SELECT $1, $2, $10",
        ["a", "b", 3, 4, 5, 6, 7, 8, 9, "ten"],
    )
    assert sql == "SELECT 'a', 'b', 'ten'"


# ---------------------------------------------------------------------------
# Trace classification / selection
# ---------------------------------------------------------------------------


def _trace(kept, derived, gather=False, blocks=False, run_id="r"):
    steps = [
        {"phase": "orient", "kind": "deterministic",
         "kept_count": kept, "derived_count": derived},
        {"phase": "plan", "kind": "render_prompt", "prompt_chars": 100},
    ]
    if gather:
        steps.append({"phase": "gather", "kind": "tool_call"})
    if blocks:
        steps.append({"phase": "ground", "kind": "desk_grounding_blocks",
                      "blocks": 2, "start_ordinal": kept + 1,
                      "block_chars": 10})
    return {"run_id": run_id, "intermediate_steps": steps, "llm_calls": []}


def test_pick_traces_prefers_fully_replayable_for_verification():
    dirty = _trace(10, 8, run_id="newest-dirty")           # structure rows
    gathered = _trace(10, 10, gather=True, run_id="gather")
    grounded = _trace(10, 10, blocks=True, run_id="blocks")
    clean = _trace(10, 10, run_id="older-clean")
    primary, verification = rpp.pick_traces([dirty, gathered, grounded, clean])
    assert primary["run_id"] == "newest-dirty"     # most recent is replayed
    assert verification["run_id"] == "older-clean"  # cleanest verifies bytes


def test_pick_traces_no_clean_candidate():
    primary, verification = rpp.pick_traces([_trace(10, 8, blocks=True)])
    assert primary is not None
    assert verification is None


def test_synthesis_llm_call_skips_judge_leg():
    trace = {"llm_calls": [
        {"status": "success", "prompt_sha256": "aaa"},
        {"status": "success", "leg": "verify_judge", "prompt_sha256": "bbb"},
    ]}
    call = rpp.synthesis_llm_call(trace)
    assert call["prompt_sha256"] == "aaa"

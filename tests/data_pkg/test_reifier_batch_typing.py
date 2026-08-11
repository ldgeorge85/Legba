# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 — the reifier's PRODUCTION typing path is batched (N=12, one typer).

The bake-off measured that batching's win is prompt tokens and that it is large:
the one-call-per-candidate path spends 1,462 prompt tokens per candidate because
it repeats the whole system preamble and the entire allowed-``rel_type``
vocabulary every time; at N=12 that falls to 297 (``docs/TYPING_BAKEOFF_
2026-08-03.md`` §7.3). These tests pin the run-loop policy that makes that real:

  * N candidates cost ONE call;
  * verdicts land on the right pair (idx correlation, never position);
  * a call that under-answers costs the unanswered candidates a single retry —
    never the batch;
  * a transport failure degrades the batch and the sweep continues;
  * there is exactly ONE typer, because κ = 0.589 (the model's agreement with
    ITSELF) leaves no headroom for a router.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from legba.data.analysts import relationship_reifier as rr
from legba.data.analysts.relationship_reifier import (
    DEFAULT_BATCH_SIZE,
    SINGLE_TYPER_RATIONALE,
    ReifierDeps,
    run_method,
)
from legba.data.analysts.relationship_typing_batch import BATCH_SYSTEM_PROMPT

pytestmark = pytest.mark.asyncio


class _Usage:
    prompt_tokens = 100
    completion_tokens = 50
    reasoning_tokens = 0


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()


class _BatchLLM:
    """Answers a batch prompt with one verdict per ``CANDIDATE n`` block.

    ``drop`` omits those idx values from the array (the under-answer case);
    ``raise_on`` makes the Nth call raise (the transport-failure case).
    """

    subprovider = "stub"

    def __init__(
        self,
        *,
        drop: set[int] | None = None,
        raise_on: set[int] | None = None,
        accept: bool = True,
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._drop = drop or set()
        self._raise_on = raise_on or set()
        self._accept = accept

    async def chat_complete(self, messages, **kw):
        n = len(self.calls)
        self.calls.append({"messages": messages, **kw})
        if n in self._raise_on:
            raise RuntimeError("plane down")
        prompt = messages[0]["content"]
        idxs = [
            int(line.split()[-2])
            for line in prompt.splitlines()
            if line.startswith("--- CANDIDATE ")
        ]
        if not idxs:  # the single-candidate retry prompt — one object, no array
            return _Resp(json.dumps(self._verdict(None)))
        out = [self._verdict(i) for i in idxs if i not in self._drop]
        return _Resp(json.dumps(out))

    def _verdict(self, idx: int | None) -> dict[str, Any]:
        body: dict[str, Any] = {
            "related": self._accept,
            "rel_type": "AlliedWith",
            "intent": "supportive",
            "channel": "direct",
            "confidence": 0.7,
            "rationale": "stub",
        }
        if idx is not None:
            body["idx"] = idx
        return body


def _inputs(n: int) -> list[dict[str, Any]]:
    return [
        {
            "source_entity": f"Alpha{i}",
            "target_entity": f"Beta{i}",
            "evidence_text": f"Alpha{i} and Beta{i} signed a pact.",
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# batching
# ---------------------------------------------------------------------------


async def test_twelve_candidates_cost_one_call():
    llm = _BatchLLM()
    res = await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    assert len(llm.calls) == 1, "N=12 must be ONE call, not twelve"
    assert res.finding.data["typed"] == 12
    assert res.finding.data["accepted"] == 12


async def test_batches_are_chunked_at_the_configured_size():
    llm = _BatchLLM()
    await run_method(_inputs(25), {}, ReifierDeps(llm=llm, batch_size=12))
    assert len(llm.calls) == 3, "25 candidates at N=12 is 12 + 12 + 1"


async def test_default_batch_size_is_the_measured_twelve():
    assert DEFAULT_BATCH_SIZE == 12
    assert ReifierDeps(llm=_BatchLLM()).batch_size == 12


async def test_batch_size_one_restores_the_per_candidate_shape():
    llm = _BatchLLM()
    await run_method(_inputs(4), {}, ReifierDeps(llm=llm, batch_size=1))
    assert len(llm.calls) == 4


async def test_the_batch_prompt_states_the_vocabulary_once():
    """The whole point of the token win: one preamble, one rel_type list, N
    candidates."""
    llm = _BatchLLM()
    await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    prompt = llm.calls[0]["messages"][0]["content"]
    assert prompt.count("Allowed rel_type values:") == 1
    assert prompt.count("--- CANDIDATE ") == 12
    assert llm.calls[0]["system"] == BATCH_SYSTEM_PROMPT


async def test_prompt_substance_is_preserved():
    """Batching restates the reifier's contract; it does not relax it."""
    llm = _BatchLLM()
    await run_method(_inputs(3), {}, ReifierDeps(llm=llm))
    user_prompt = llm.calls[0]["messages"][0]["content"]
    # the CLOSED vocabulary reaches the model in full, exactly once
    for rel in rr.ALLOWED_REL_TYPES:
        assert rel in user_prompt, f"{rel} vanished from the batch prompt"
    assert "INTERMEDIARY rule" in BATCH_SYSTEM_PROMPT
    assert "VERBATIM" in BATCH_SYSTEM_PROMPT
    # the closed intent + channel vocabularies survive verbatim
    for token in ("supportive", "hostile", "dual-use", "neutral"):
        assert token in BATCH_SYSTEM_PROMPT
    for token in ("direct", "proxy", "covert", "institutional"):
        assert token in BATCH_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# correlation + degradation
# ---------------------------------------------------------------------------


async def test_verdicts_are_correlated_by_idx_not_position():
    """A model that answers out of order must still land each verdict on its own
    pair. The stub echoes idx, and the run must accept all twelve."""

    class _Shuffled(_BatchLLM):
        async def chat_complete(self, messages, **kw):
            resp = await super().chat_complete(messages, **kw)
            arr = json.loads(resp.content)
            if isinstance(arr, list):
                resp.content = json.dumps(list(reversed(arr)))
            return resp

    llm = _Shuffled()
    res = await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    assert res.finding.data["accepted"] == 12
    assert res.finding.data["degraded"] == 0


async def test_under_answered_candidates_are_retried_singly_not_lost():
    """Three idx dropped from the array ⇒ three single-candidate retries, and
    every candidate still gets a verdict. Never a batch-abort."""
    llm = _BatchLLM(drop={2, 5, 9})
    res = await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    assert len(llm.calls) == 1 + 3, "one batch call plus one retry per dropped idx"
    assert res.finding.data["typed"] == 12
    assert res.finding.data["degraded"] == 0


async def test_a_retry_that_also_fails_is_counted_degraded_not_silent():
    class _NeverAnswersRetry(_BatchLLM):
        async def chat_complete(self, messages, **kw):
            prompt = messages[0]["content"]
            if "--- CANDIDATE " not in prompt:  # the retry
                self.calls.append({"retry": True})
                return _Resp("not json at all")
            return await super().chat_complete(messages, **kw)

    llm = _NeverAnswersRetry(drop={1, 3})
    res = await run_method(_inputs(6), {}, ReifierDeps(llm=llm))
    assert res.finding.data["degraded"] == 2
    assert res.finding.data["typed"] == 4
    assert "degraded" in res.finding.tags


async def test_a_transport_failure_degrades_the_batch_and_the_sweep_continues():
    """The first batch's call raises; the second must still run. Degrade-not-drop
    lifted to the batch level."""
    llm = _BatchLLM(raise_on={0})
    res = await run_method(_inputs(24), {}, ReifierDeps(llm=llm, batch_size=12))
    assert len(llm.calls) == 2, "the sweep must not abort on one failed batch"
    assert res.finding.data["degraded"] == 12
    assert res.finding.data["accepted"] == 12


async def test_model_rejections_are_counted_separately_from_failures():
    """`rejected` is the typer saying no; `degraded` is the typer not answering.
    Conflating them is what let a dead-row window look like a working one."""
    llm = _BatchLLM(accept=False)
    res = await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    assert res.finding.data["typed"] == 12
    assert res.finding.data["rejected"] == 12
    assert res.finding.data["accepted"] == 0
    assert res.finding.data["degraded"] == 0


async def test_usage_rolls_up_across_every_call_in_the_run():
    llm = _BatchLLM()
    res = await run_method(_inputs(24), {}, ReifierDeps(llm=llm, batch_size=12))
    assert res.usage["prompt_tokens"] == 200
    assert res.usage["completion_tokens"] == 100


async def test_completion_budget_scales_with_the_batch():
    llm = _BatchLLM()
    await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    # linear in N — truncation is the batch failure that costs the most
    # candidates per wasted token (§3.1: 280/verdict measured, not estimated)
    assert llm.calls[0]["max_tokens"] >= 12 * 280


# ---------------------------------------------------------------------------
# one typer
# ---------------------------------------------------------------------------


async def test_there_is_exactly_one_typer_and_no_escalation_ladder():
    """κ = 0.589 — the model does not agree with ITSELF on one candidate in five,
    so no difficulty signal exists to route on. ReifierDeps carries ONE llm
    handle; a second tier would have to appear here first."""
    fields = ReifierDeps.__dataclass_fields__
    llm_fields = [f for f in fields if "llm" in f or "model" in f]
    assert llm_fields == ["llm"], f"an escalation tier appeared: {llm_fields}"
    assert SINGLE_TYPER_RATIONALE == "kg2_kappa_0.589_self_agreement"


async def test_every_call_in_a_run_uses_the_same_handle():
    llm = _BatchLLM(drop={1})
    await run_method(_inputs(12), {}, ReifierDeps(llm=llm))
    assert len(llm.calls) == 2, "batch + retry, both on the one handle"


# ---------------------------------------------------------------------------
# the validators still bind through the batch
# ---------------------------------------------------------------------------


async def test_off_list_rel_type_is_still_refused_through_the_batch():
    class _OffList(_BatchLLM):
        def _verdict(self, idx):
            v = super()._verdict(idx)
            v["rel_type"] = "IsFriendsWith"
            return v

    res = await run_method(_inputs(6), {}, ReifierDeps(llm=_OffList()))
    assert res.finding.data["accepted"] == 0
    assert res.finding.data["rejected"] == 6


async def test_demonym_self_loop_is_dropped_before_it_costs_a_batch_slot():
    llm = _BatchLLM()
    rows = [{"source_entity": "Iran", "target_entity": "Iranian",
             "evidence_text": "x"}] + _inputs(2)
    res = await run_method(rows, {}, ReifierDeps(llm=llm))
    assert res.finding.data["skipped_endpoints"] == 1
    prompt = llm.calls[0]["messages"][0]["content"]
    assert prompt.count("--- CANDIDATE ") == 2


# ---------------------------------------------------------------------------
# descriptor-declared knobs (X-1 / QW1-B kind lane)
# ---------------------------------------------------------------------------

from pathlib import Path  # noqa: E402

import yaml  # noqa: E402

from legba.data.analysts.edge_qualification import (  # noqa: E402
    MIN_INDEPENDENT_SOURCES,
    RECOMMENDED_BAR,
)
from legba.data.analysts.handler_options import (  # noqa: E402
    ANALYST_KIND_OPTIONS,
    known_kind_option_names,
    resolve_kind_options,
)

DESCRIPTOR = (
    Path(__file__).resolve().parents[2]
    / "descriptors" / "analyst_relationship_reifier.yaml"
)


def _shipped_options() -> dict[str, Any]:
    body = yaml.safe_load(DESCRIPTOR.read_text())
    return dict(body["method"].get("options") or {})


async def test_the_kind_declares_an_option_catalog():
    """Without a catalog entry the descriptor's whole options block is refused
    at registration — the knobs would be unreachable config."""
    assert "relationship_reifier" in ANALYST_KIND_OPTIONS
    assert set(known_kind_option_names("relationship_reifier")) == {
        "max_candidates", "batch_size", "qualification_bar",
        "min_independent_sources",
    }


async def test_every_shipped_option_is_accepted_by_the_catalog():
    """The descriptor production actually runs, not a hand-built stand-in."""
    resolution = resolve_kind_options(
        "relationship_reifier", _shipped_options(), log_context="test"
    )
    assert resolution.rejected == (), resolution.rejected
    assert resolution.accepted == _shipped_options()


async def test_the_shipped_cap_matches_the_documented_arithmetic():
    """205 edges/day steady state is the target; the cap is sized for the DRAIN
    and steady state falls out of arrivals. Pin both so the descriptor comment
    and the number cannot drift apart."""
    opts = _shipped_options()
    per_run = opts["max_candidates"]
    runs_per_day = 2                      # cadence "45 */12 * * *"
    accept_rate = 0.468                   # measured, core 120B
    qualifying_arrivals_per_day = 450     # measured over the last week

    assert per_run * runs_per_day == 1200
    assert opts["batch_size"] == 12
    assert per_run * runs_per_day / opts["batch_size"] == 100  # LLM calls/day
    drain_edges = per_run * runs_per_day * accept_rate
    assert 550 <= drain_edges <= 575
    steady_edges = qualifying_arrivals_per_day * accept_rate
    assert 200 <= steady_edges <= 215, "steady state must land near 205/day"
    # ... and the whole day fits the provisioned token budget with headroom
    tokens_per_candidate = 557
    assert per_run * runs_per_day * tokens_per_candidate < 1_000_000


async def test_the_shipped_bar_is_the_recommended_one():
    opts = _shipped_options()
    assert opts["qualification_bar"] == RECOMMENDED_BAR
    assert opts["min_independent_sources"] == MIN_INDEPENDENT_SOURCES


async def test_descriptor_options_override_the_deps_defaults():
    """The end-to-end proof that a descriptor knob CHANGES OBSERVED BEHAVIOUR:
    batch_size=4 must chunk 12 candidates into three calls, not one."""
    llm = _BatchLLM()
    await run_method(_inputs(12), {"batch_size": 4}, ReifierDeps(llm=llm))
    assert len(llm.calls) == 3


async def test_absent_options_are_byte_identical_to_the_deps_defaults():
    llm_a, llm_b = _BatchLLM(), _BatchLLM()
    await run_method(_inputs(12), {}, ReifierDeps(llm=llm_a))
    await run_method(
        _inputs(12), {"batch_size": DEFAULT_BATCH_SIZE}, ReifierDeps(llm=llm_b)
    )
    assert len(llm_a.calls) == len(llm_b.calls) == 1


async def test_max_candidates_option_caps_the_no_pool_input_path():
    llm = _BatchLLM()
    res = await run_method(_inputs(30), {"max_candidates": 6}, ReifierDeps(llm=llm))
    assert res.finding.data["candidates"] == 6

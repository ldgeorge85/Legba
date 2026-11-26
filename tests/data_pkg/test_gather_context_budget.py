# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""the multi-round GATHER evidence cap, its pre-flight and its receipt.

On 2026-10-16 the corpus_researcher sent ~124,000 characters of evidence into
a 32,000-token input budget and got an empty completion back. These tests pin
the bound with fakes only (no database, no endpoint):

* the option is declared and validated, and the derived default's arithmetic;
* the slice is packed to its share of the cap on a multi-round run and only
  there;
* compaction fires at the cap, replaces older results oldest first and never
  the newest round, and leaves every [N] number, document id and citation
  entry exactly as an uncapped run produces them;
* the pre-flight refuses an over-budget round, and the run still writes;
* an empty GATHER completion is a named, counted step, never a clean ``done``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import pytest
import yaml

from legba.data.analysts import gather_context_budget as ctxb
from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.analysts.gather_native import NATIVE_TOOLS_ENV
from legba.data.analysts.gather_surface import _GATHER_SYSTEM_SUFFIX
from legba.data.analysts.inline_target import (
    InlineTargetDeps,
    _effective_system_prompt,
    _gather,
    _gather_system_suffix,
    run_method,
)
from legba.data.option_catalog.handler_options import (
    ANALYST_KIND_OPTIONS,
    resolve_kind_options,
)
from legba.data.option_catalog.handler_options_gather import EVIDENCE_CHAR_CAP_OPTION
from legba.data.stack.llm.vllm import VLLMProviderHandler

REPO = Path(__file__).resolve().parents[2]
BUDGET_ENV = "LEGBA_LLM_INPUT_TOKEN_BUDGET"

DOCS = [UUID(f"00000000-0000-4000-8000-00000000c{i:03d}") for i in range(1, 10)]
FINAL_JSON = json.dumps({
    "title": "Researched read",
    "body": "BLUF: the figure was 41.7% [1]. Assessed: it holds [2].",
    "confidence": 0.5,
    "evidence": [],
    "tags": ["severity:low", "topic:corpus_research"],
})


@pytest.fixture(autouse=True)
def _plane_defaults(monkeypatch):
    """The tree defaults, whatever the host's .env says (text protocol, 32k)."""
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "0")
    monkeypatch.setenv(BUDGET_ENV, "32000")


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 10
    completion_tokens = 5
    reasoning_tokens = 0


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()
        self.finish_reason = "stop"
        self.tool_calls: list[Any] = []


class ScriptedLLM:
    """Pops scripted replies; records every request's messages + system."""

    subprovider = "vllm"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
        self.requests: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.requests.append({
            "messages": json.loads(json.dumps(list(messages), default=str)),
            "system": kwargs.get("system"),
        })
        return _Resp(self._scripted.pop(0) if self._scripted else '{"done": true}')


class CorpusBinding:
    """search_corpus returns one long, distinct document per call."""

    pack_id = "substrate_read"

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name, args, **kw):
        self.calls.append((tool_name, dict(args)))
        i = len(self.calls)
        if tool_name == "search_corpus":
            out = {"rows": [{
                "id": str(DOCS[i - 1]), "score": 1.0,
                "source": {
                    "title": f"Corpus doc {i}",
                    "raw_body": f"Doc {i} reports output of {i}1.7% in 2025. " + "x" * 3600,
                    "canonical_url": f"https://example.invalid/doc/{i}",
                    "fetched_at": "2026-05-20T00:00:00+00:00",
                    "published_at": "2026-05-10T00:00:00+00:00",
                },
            }]}
        else:
            out = {"rows": [{"fact": f"value {i}", "n": 1234.5}]}
        return AgencyOutcome(
            admitted=True, pack_id=self.pack_id, tool_name=tool_name,
            tool_result=ToolResult(status="completed", output=out),
        )


def _search(q: str) -> str:
    return json.dumps({"tool": "search_corpus", "args": {"query": q, "size": 5}})


def _signal(i: int, chars: int = 1400) -> dict[str, Any]:
    return {
        "id": UUID(f"00000000-0000-4000-8000-0000000{i:05d}"),
        "title": f"Signal {i}",
        "produced_at": f"2026-05-{(i % 28) + 1:02d}T10:00:00+00:00",
        "source_url": f"https://example.invalid/news/{i}",
        "data": {"summary": f"Summary {i}.", "raw_body": f"Body {i} " + "y" * chars},
    }


async def _run_gather(llm, binding, *, ctx, max_rounds=4, user_prompt="USER PROMPT"):
    steps: list[dict[str, Any]] = []
    out = await _gather(
        InlineTargetDeps(llm=llm, max_rounds=max_rounds),
        binding=binding,
        user_prompt=user_prompt,
        target_id=None,
        analyst_id="corpus_researcher",
        steps=steps,
        tool_bindings={},
        gather_system=None,
        base_offset=0,
        context_budget=ctx,
    )
    return out, steps


def _tool_messages(request: Mapping[str, Any]) -> list[str]:
    return [m["content"] for m in request["messages"] if m.get("role") == "tool"]


# ---------------------------------------------------------------------------
# 1. The option — declared, validated; the derived default's arithmetic
# ---------------------------------------------------------------------------


def test_the_option_is_declared_on_the_inline_target_kind() -> None:
    names = {sp.name for sp in ANALYST_KIND_OPTIONS["inline_target"]}
    assert EVIDENCE_CHAR_CAP_OPTION == "evidence_char_cap" and EVIDENCE_CHAR_CAP_OPTION in names


@pytest.mark.parametrize("value", [0, 16_000, 129_204, 1_048_576])
def test_the_option_accepts_an_int_in_range(value: int) -> None:
    res = resolve_kind_options("inline_target", {EVIDENCE_CHAR_CAP_OPTION: value})
    assert res.accepted == {EVIDENCE_CHAR_CAP_OPTION: value} and not res.rejected


@pytest.mark.parametrize("value", [-1, True, 1.5, "120000", 1_048_577])
def test_the_option_refuses_a_malformed_value(value: Any) -> None:
    res = resolve_kind_options("inline_target", {EVIDENCE_CHAR_CAP_OPTION: value})
    assert EVIDENCE_CHAR_CAP_OPTION not in res.accepted
    assert [r.key for r in res.rejected] == [EVIDENCE_CHAR_CAP_OPTION]


def test_the_derived_cap_arithmetic() -> None:
    # 0.6 × B × 4 − the gather system prompt (the researcher's is ~28,082 chars).
    assert ctxb.derive_evidence_char_cap(28_082, 65_536) == 157_286 - 28_082 == 129_204
    assert ctxb.derive_evidence_char_cap(28_082, 32_000) == 76_800 - 28_082 == 48_718
    # The floor: a tiny budget or a huge prompt never derives a useless cap.
    assert ctxb.derive_evidence_char_cap(200_000, 32_000) == ctxb.MIN_EVIDENCE_CHARS
    ctx = ctxb.GatherContextBudget(129_204, 65_536, derived=True)
    assert ctx.slice_budget_tokens == int(129_204 * 0.6) // 4 == 19_380


def test_the_researcher_descriptor_derives_a_cap_and_still_parses() -> None:
    from legba.data.schemas.analyst import AnalystDescriptor

    body = yaml.safe_load(
        (REPO / "descriptors" / "analyst_corpus_researcher.yaml").read_text(encoding="utf-8")
    )
    body["identity"]["version"] = "0" * 16
    desc = AnalystDescriptor.model_validate(body, strict=False)
    assert not resolve_kind_options("inline_target", desc.method.options).rejected
    deps = InlineTargetDeps(llm=None, system_prompt=body["method"]["system_prompt"])
    system = _effective_system_prompt(deps) + _gather_system_suffix(
        web_fragments=["Cite the URL you fetched."], write_fragments=None,
        bound_tools=("web_search", "web_fetch", "web_evidence"),
    )
    ctx = ctxb.resolve_gather_context_budget(
        {}, gather_system=system, max_rounds=body["method"]["gather"]["max_rounds"],
        engaged=True,
    )
    assert ctx is not None and ctx.derived
    assert ctx.evidence_char_cap == int(0.6 * 32_000 * 4) - len(system)
    # The 10-16 run packed ~124,000 characters of slice; the share is far below.
    assert ctx.evidence_char_cap * ctxb.SLICE_SHARE < 40_000


def test_resolution_rules() -> None:
    sys_ = "S" * 1000
    r = ctxb.resolve_gather_context_budget
    assert r({}, gather_system=sys_, max_rounds=4, engaged=False) is None
    assert r({}, gather_system=sys_, max_rounds=1, engaged=True) is None  # single round
    derived = r({}, gather_system=sys_, max_rounds=2, engaged=True)
    assert derived is not None and derived.derived
    assert derived.evidence_char_cap == 76_800 - 1000
    assert r({"evidence_char_cap": 0}, gather_system=sys_, max_rounds=4, engaged=True) is None
    explicit = r({"evidence_char_cap": 9000}, gather_system=sys_, max_rounds=1, engaged=True)
    assert explicit is not None and not explicit.derived and explicit.evidence_char_cap == 9000
    for bad in (-5, "junk", True):
        got = r({"evidence_char_cap": bad}, gather_system=sys_, max_rounds=4, engaged=True)
        assert got is not None and got.derived


# ---------------------------------------------------------------------------
# 2. The slice is packed to its share — on a multi-round run only
# ---------------------------------------------------------------------------


async def _kept(options: Mapping[str, Any], max_rounds: int) -> tuple[int, int]:
    llm = ScriptedLLM(['{"done": true}', FINAL_JSON])
    result = await run_method(
        [_signal(i) for i in range(80)],
        {"target_id": None, "analyst_id": "corpus_researcher",
         "agency_binding": CorpusBinding(), **options},
        InlineTargetDeps(llm=llm, max_rounds=max_rounds),
    )
    orient = next(s for s in result.intermediate_steps if s["phase"] == "orient")
    return orient["kept_count"], len(llm.requests[0]["messages"][0]["content"])


async def test_the_slice_is_packed_to_its_share_of_the_cap() -> None:
    off_kept, _ = await _kept({"evidence_char_cap": 0}, 4)
    one_round_kept, _ = await _kept({}, 1)
    capped_kept, capped_chars = await _kept({"evidence_char_cap": 40_000}, 4)
    derived_kept, _ = await _kept({}, 4)
    # Off, and a single-round run with no option, pack to the whole budget.
    assert off_kept == one_round_kept == 80
    # Capped: the slice prompt fits its 60% share (plus the header lines).
    assert capped_kept < off_kept and capped_chars <= 40_000 * ctxb.SLICE_SHARE + 2_000
    assert derived_kept < off_kept


# ---------------------------------------------------------------------------
# 3. Compaction — at the cap, oldest first, never the newest round; citations
#    and numbers survive exactly
# ---------------------------------------------------------------------------


async def test_compaction_fires_at_the_cap_and_keeps_every_citation() -> None:
    script = [_search("q1"), _search("q2"), _search("q3"), '{"done": true}']
    plain_llm, capped_llm = ScriptedLLM(script), ScriptedLLM(script)
    (plain, _), (capped, steps) = await _run_gather(
        plain_llm, CorpusBinding(), ctx=None,
    ), await _run_gather(
        capped_llm, CorpusBinding(),
        ctx=ctxb.GatherContextBudget(6_000, 10**6, derived=False),
    )
    plain_ctx, _, plain_refs, _, plain_ext = plain
    capped_ctx, _, capped_refs, _, capped_ext = capped

    # The citation machinery is untouched: same [N] blocks, same entries, same
    # lineage, as a run that never compacted.
    assert capped_ctx == plain_ctx
    assert capped_ext == plain_ext and sorted(capped_ext) == [1, 2, 3]
    assert capped_refs == plain_refs
    for n in (1, 2, 3):
        assert f"[{n}]" in capped_ctx and capped_ext[n]["signal_id"] == str(DOCS[n - 1])

    # Round 3's request: result 1 is a digest, result 2 (the newest) is whole.
    r3 = _tool_messages(capped_llm.requests[2])
    d1 = json.loads(r3[0])
    assert d1["compacted"] is True and d1["tool"] == "search_corpus"
    assert d1["numbered_documents"] == [
        {"n": 1, "id": str(DOCS[0]), "title": "Corpus doc 1"},
    ]
    assert "Doc 2 reports output of 21.7%" in r3[1]
    # Round 4's request: results 1 and 2 are digests, result 3 is whole.
    r4 = _tool_messages(capped_llm.requests[3])
    assert [json.loads(b).get("compacted") for b in r4[:2]] == [True, True]
    assert json.loads(r4[1])["numbered_documents"][0]["n"] == 2
    assert "Doc 3 reports output of 31.7%" in r4[2]
    # The uncapped run carried every body whole, every round.
    assert all("compacted" not in b for b in _tool_messages(plain_llm.requests[3]))
    # The slice message and every assistant turn are never touched.
    assert capped_llm.requests[3]["messages"][0] == plain_llm.requests[3]["messages"][0]
    assert [m for m in capped_llm.requests[3]["messages"] if m["role"] == "assistant"] == [
        m for m in plain_llm.requests[3]["messages"] if m["role"] == "assistant"
    ]

    [receipt] = [s for s in steps if s["kind"] == ctxb.STEP_KIND]
    assert receipt["compactions"] == 2 and receipt["results_compacted"] == 2
    assert receipt["chars_saved"] > 6_000 and receipt["preflight_refused"] is False
    assert receipt["evidence_char_cap"] == 6_000 and receipt["cap_derived"] is False


def test_a_digest_keeps_the_head_of_a_non_signal_result() -> None:
    body = json.dumps({"rows": [{"fact": "GDP grew 3.2% to $1.94tn", "n": 1234.5}]})
    d = json.loads(ctxb.result_digest("query_facts", [], body + " " * 2000))
    assert d["compacted"] and d["tool"] == "query_facts" and "numbered_documents" not in d
    assert "3.2%" in d["head"] and "1234.5" in d["head"]


async def test_under_the_cap_the_requests_are_byte_identical() -> None:
    script = [_search("q1"), _search("q2"), '{"done": true}']
    a, b = ScriptedLLM(script), ScriptedLLM(script)
    (_, a_steps), (_, b_steps) = await _run_gather(a, CorpusBinding(), ctx=None), \
        await _run_gather(b, CorpusBinding(),
                          ctx=ctxb.GatherContextBudget(10**6, 10**6, derived=True))
    assert a.requests == b.requests
    assert a_steps == b_steps  # nothing fired, so no receipt either


# ---------------------------------------------------------------------------
# 4. The pre-flight — an over-budget round is not sent; the run still writes
# ---------------------------------------------------------------------------


async def test_the_preflight_refuses_an_over_budget_round_in_the_loop() -> None:
    system = _effective_system_prompt(InlineTargetDeps(llm=None)) + _GATHER_SYSTEM_SUFFIX
    first = ctxb.input_tokens_est(system, [{"role": "user", "content": "USER PROMPT"}])
    llm = ScriptedLLM([_search("q1"), _search("q2"), '{"done": true}'])
    ctx = ctxb.GatherContextBudget(10**6, first + 200, derived=False)

    (gathered, _, _, _, ext), steps = await _run_gather(llm, CorpusBinding(), ctx=ctx)

    assert len(llm.requests) == 1  # round 2 was never sent
    [refused] = [s for s in steps if s["kind"] == ctxb.PREFLIGHT_REFUSED_KIND]
    assert refused["round"] == 2 and refused["input_tokens_est"] > first + 200
    assert refused["input_token_budget"] == first + 200
    # What round 1 gathered is kept and citable; the synthesis is told to write.
    assert ext[1]["signal_id"] == str(DOCS[0])
    assert gathered.rstrip().endswith(ctxb.STOPPED_EARLY_NOTE)
    [receipt] = [s for s in steps if s["kind"] == ctxb.STEP_KIND]
    assert receipt["preflight_refused"] is True
    assert receipt["peak_input_tokens_est"] == refused["input_tokens_est"]


async def test_the_preflight_degrades_the_run_to_write_with_what_it_has(monkeypatch) -> None:
    async def _run(budget: int, script: list[str]) -> tuple[Any, ScriptedLLM]:
        monkeypatch.setenv(BUDGET_ENV, str(budget))
        llm = ScriptedLLM(script)
        result = await run_method(
            [_signal(1, 200)],
            {"target_id": None, "analyst_id": "corpus_researcher",
             "agency_binding": CorpusBinding(), "evidence_char_cap": 200_000},
            InlineTargetDeps(llm=llm, max_rounds=4),
        )
        return result, llm

    _, probe = await _run(10**6, [_search("q1"), '{"done": true}', FINAL_JSON])
    first = ctxb.input_tokens_est(probe.requests[0]["system"], probe.requests[0]["messages"])
    # Round 2 would ask for more; it is never sent, so the next reply is the finding.
    result, llm = await _run(first + 200, [_search("q1"), FINAL_JSON])

    kinds = [s["kind"] for s in result.intermediate_steps]
    assert ctxb.PREFLIGHT_REFUSED_KIND in kinds and ctxb.STEP_KIND in kinds
    assert len(llm.requests) == 2  # one gather round, then the synthesis
    synthesis = llm.requests[-1]["messages"][0]["content"]
    assert ctxb.STOPPED_EARLY_NOTE in synthesis and "[2]" in synthesis
    assert result.finding.title == "Researched read"


def test_the_synthesis_over_budget_step_is_named() -> None:
    ctx = ctxb.GatherContextBudget(10_000, 100, derived=False)
    step = ctxb.synthesis_over_budget_step(ctx, system="s" * 200, user_prompt="u" * 400)
    assert step == {"phase": "reason", "kind": ctxb.SYNTHESIS_OVER_BUDGET_KIND,
                    "input_tokens_est": 150, "input_token_budget": 100}
    assert ctxb.synthesis_over_budget_step(ctx, system="s", user_prompt="u") is None
    assert ctxb.synthesis_over_budget_step(None, system="s" * 900, user_prompt="") is None


# ---------------------------------------------------------------------------
# 5. The empty completion — named and counted, never a clean done
# ---------------------------------------------------------------------------


async def test_an_empty_text_round_is_a_named_counted_failure() -> None:
    llm = ScriptedLLM([_search("q1"), "   "])
    (_, _, _, _, ext), steps = await _run_gather(llm, CorpusBinding(), ctx=None)
    kinds = [s["kind"] for s in steps]
    assert "unparseable" not in kinds and "done" not in kinds
    [empty] = [s for s in steps if s["kind"] == ctxb.EMPTY_COMPLETION_KIND]
    assert empty == {"phase": "gather", "kind": "empty_completion", "round": 2,
                     "protocol": "text"}
    # No cap on this run, and the count is still there.
    assert steps[-1] == {"phase": "gather", "kind": ctxb.STEP_KIND, "empty_completions": 1}
    assert ext[1]["signal_id"] == str(DOCS[0])  # what was gathered survives


class _NativeEmptyLLM:
    """A core-plane reply with no text and no tool call (the overflow symptom)."""

    subprovider = "vllm"

    def __init__(self) -> None:
        self._parser = VLLMProviderHandler()

    async def chat_complete(self, messages, **kwargs):
        raw = {
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": None,
                            "reasoning_content": "..."},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 61000, "completion_tokens": 0,
                      "total_tokens": 61000},
        }
        return self._parser._parse_response(raw, model="core-120b")


async def test_an_empty_native_round_is_not_read_as_done(monkeypatch) -> None:
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    ctx = ctxb.GatherContextBudget(10**6, 10**6, derived=True)
    _, steps = await _run_gather(_NativeEmptyLLM(), CorpusBinding(), ctx=ctx)
    kinds = [s["kind"] for s in steps]
    assert "done" not in kinds
    [empty] = [s for s in steps if s["kind"] == ctxb.EMPTY_COMPLETION_KIND]
    assert empty["protocol"] == "native" and empty["round"] == 1
    [receipt] = [s for s in steps if s["kind"] == ctxb.STEP_KIND]
    assert receipt["empty_completions"] == 1 and receipt["compactions"] == 0


async def test_the_empty_round_reaches_the_run_trace() -> None:
    llm = ScriptedLLM(["", FINAL_JSON])
    result = await run_method(
        [_signal(1, 200)],
        {"target_id": None, "analyst_id": "corpus_researcher",
         "agency_binding": CorpusBinding()},
        InlineTargetDeps(llm=llm, max_rounds=4),
    )
    receipt = next(s for s in result.intermediate_steps if s["kind"] == ctxb.STEP_KIND)
    assert receipt["empty_completions"] == 1 and receipt["cap_derived"] is True
    assert any(s["kind"] == ctxb.EMPTY_COMPLETION_KIND for s in result.intermediate_steps)


async def test_a_run_over_budget_from_the_start_gathers_nothing_and_still_writes(
    monkeypatch,
) -> None:
    """Round 1 already over: no gather call is sent, the synthesis (the run's last
    call) is sent and stamped ``synthesis_over_budget``, and the finding lands."""
    monkeypatch.setenv(BUDGET_ENV, "100")
    llm = ScriptedLLM([FINAL_JSON])
    result = await run_method(
        [_signal(1, 200)],
        {"target_id": None, "analyst_id": "corpus_researcher",
         "agency_binding": CorpusBinding(), "evidence_char_cap": 50_000},
        InlineTargetDeps(llm=llm, max_rounds=4),
    )
    steps = result.intermediate_steps
    refused = next(s for s in steps if s["kind"] == ctxb.PREFLIGHT_REFUSED_KIND)
    assert refused["round"] == 1
    over = next(s for s in steps if s["kind"] == ctxb.SYNTHESIS_OVER_BUDGET_KIND)
    assert over["phase"] == "reason" and over["input_token_budget"] == 100
    assert len(llm.requests) == 1  # the synthesis only
    assert ctxb.STOPPED_EARLY_NOTE in llm.requests[0]["messages"][0]["content"]
    assert result.finding.title == "Researched read"

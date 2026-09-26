# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Native tool rounds in the GATHER loop — the gate and the new behaviour.

Two halves, and the first one is the gate.

**Flag-off identity, and flag-on identity for the tool-less path.** Five
profiles were driven through the real ``run_method`` / ``_gather`` chain at the
unmodified tree (commit 311a62d1) and their exact ``chat_complete`` request
lists, traces, findings, usage and lineage recorded under
``fixtures/native_tool_rounds/``. With ``LEGBA_AGENCY_NATIVE_TOOLS`` off, all
five must still reproduce byte for byte. With it ON, the three that bind NO
action pack — ``country_composition``, ``world_assessor``,
``world_assessment``, three of the five R4-frozen units — must ALSO reproduce
byte for byte, because a run with no tools to offer sends no ``tools`` and
takes no new branch. That is the whole safety argument for deploying this with
the flag up, and it is asserted rather than reasoned about.

*Re-pin, 2026-09-26.* The goldens were re-captured off the flag-OFF path at
a39286bc. The whole diff against the 311a62d1 recording was two unrelated
feature additions, each verified leaf by leaf before the re-pin — nothing in
the native-tools branch moved:

  * ``87ad6281`` (2026-09-24) added the two event-citation degrade counters,
    ``event_expand_failed`` and ``event_unresolved``, to the citation receipt
    step. They land as ``0`` on every one of these profiles (no event
    citations in play), so the new keys are the entire delta — in the three
    tool-less fixtures at ``steps[4]`` and in ``corpus_researcher`` at
    ``steps[7]``.
  * ``55497470`` (2026-09-25) added ``series_history`` and ``series_compare``
    to the substrate tool catalogue that ``gather_surface`` renders into the
    system prompt. That is two inserted lines, directly after the
    ``inspect_event`` line, in all three ``system`` strings of
    ``corpus_researcher`` and of ``journal_assessor_gather``. The tool-less
    profiles render no catalogue and are untouched by it.

Anything beyond those two is a real regression — re-capture only with the
diff explained the same way.

**The new behaviour**, driven on the shapes the live core plane actually
returns (``gpt-oss-120b`` on the core plane, probed 2026-09-16 — see
``planning/ROADMAP_POSITION_2026-09-05.md`` §8). The stubs hand those raw wire
dicts to the REAL ``VLLMProviderHandler._parse_response``, so the provider
parsing under test is production's, not a hand-rolled imitation:

  * a single native call dispatches through the governed binding and lands rows;
  * the three-call turn of 2026-09-15 03:37Z — which the text protocol executed
    ONE of — executes all three, in emitted order;
  * a tool that fails inside a batch does not sink the batch;
  * a FORCED call arrives with ``finish_reason=stop`` and is still executed,
    because the parser keys on ``message.tool_calls`` and never on the finish
    reason;
  * write tools serialize while reads overlap;
  * a provider with no proven native surface falls back to the JSON-text
    protocol untouched.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any, Mapping
from uuid import UUID

import pytest

from legba.data.analysts.agency.agency import AgencyOutcome
from legba.data.analysts.agency.tools import ToolResult
from legba.data.analysts.gather_native import NATIVE_TOOLS_ENV
from legba.data.analysts.inline_target import InlineTargetDeps, _gather, run_method
from legba.data.stack.llm.vllm import VLLMProviderHandler

pytestmark = [pytest.mark.asyncio]

FIXTURES = Path(__file__).parent / "fixtures" / "native_tool_rounds"

#: The header renderer (``slice_render._render_user_prompt``) stamps
#: ``Run date (as-of): <today UTC>`` with no threaded ``run_date`` override
#: reaching this call chain (``run_method`` / ``_gather`` never pass one), so
#: a byte-identity fixture captured on one day fails on every later one for a
#: reason that has nothing to do with the behaviour under test. Freeze that
#: one substring, on BOTH the freshly captured value and the loaded fixture,
#: rather than re-recording the goldens every morning.
_AS_OF_RE = re.compile(r"Run date \(as-of\): \d{4}-\d{2}-\d{2}")
_AS_OF_FROZEN = "Run date (as-of): <frozen-for-test>"


def _freeze_as_of(obj: Any) -> Any:
    """Recursively replace the rendered as-of date with a fixed sentinel."""
    if isinstance(obj, str):
        return _AS_OF_RE.sub(_AS_OF_FROZEN, obj)
    if isinstance(obj, list):
        return [_freeze_as_of(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _freeze_as_of(v) for k, v in obj.items()}
    return obj

SIG = [UUID(f"00000000-0000-4000-8000-0000000000{i:02d}") for i in range(1, 7)]
REF = UUID("00000000-0000-4000-8000-0000000000aa")
CORPUS_DOC = UUID("00000000-0000-4000-8000-0000000000bb")


# ---------------------------------------------------------------------------
# The recorded-baseline harness (identical to the capture that wrote the
# fixtures — the point is that this code path, not a paraphrase of it, is what
# the fixture pins).
# ---------------------------------------------------------------------------


def _row(i: int, title: str, produced_at: str) -> dict[str, Any]:
    return {
        "id": SIG[i],
        "title": title,
        "produced_at": produced_at,
        "source_url": f"https://example.invalid/news/{i}",
        "data": {"summary": f"Body text for {title}.", "raw_body": f"Full body {i}."},
    }


INPUTS_3 = [
    _row(0, "Itaipu hydro upgrade", "2026-05-19T14:00:00+00:00"),
    _row(1, "Wind capacity record", "2026-05-18T09:30:00+00:00"),
    _row(2, "Petrobras Q1 figures", "2026-05-17T10:00:00+00:00"),
]
INPUTS_5 = INPUTS_3 + [
    _row(3, "Regional grid interconnect", "2026-05-16T08:00:00+00:00"),
    _row(4, "Tariff review opens", "2026-05-15T07:00:00+00:00"),
]

FINAL_JSON = json.dumps({
    "title": "Composed read",
    "body": "BLUF: the grid held. [1] Assessed: capacity rose. [2]",
    "confidence": 0.62,
    "evidence": [str(SIG[0]), str(SIG[1])],
    "tags": ["energy"],
})


class _Usage:
    prompt_tokens = 100
    completion_tokens = 50
    reasoning_tokens = 0
    total_tokens = 150


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = _Usage()
        self.finish_reason = "stop"
        self.tool_calls: list[Any] = []
        self.raw_response: dict[str, Any] | None = None


class RecordingLLM:
    """Records the EXACT ``chat_complete`` kwargs of every call."""

    subprovider = "vllm"

    def __init__(self, scripted: list[str]) -> None:
        self._scripted = list(scripted)
        self.requests: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.requests.append({
            "messages": json.loads(json.dumps(list(messages), default=str)),
            "system": kwargs.get("system"),
            "tools": json.loads(json.dumps(kwargs.get("tools"), default=str)),
            "max_tokens": kwargs.get("max_tokens"),
            "temperature": kwargs.get("temperature"),
            "other_kwargs": sorted(
                k for k in kwargs
                if k not in ("system", "tools", "max_tokens", "temperature")
            ),
        })
        content = self._scripted.pop(0) if self._scripted else '{"done": true}'
        return _Resp(content)


class FakeBinding:
    def __init__(self, pack_id: str, output_for) -> None:
        self.pack_id = pack_id
        self._output_for = output_for
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def run_tool(self, tool_name, args, **kw):
        self.calls.append((tool_name, dict(args)))
        out = self._output_for(tool_name, args)
        if out is None:
            return AgencyOutcome(
                admitted=False, pack_id=self.pack_id, tool_name=tool_name,
                block_cause="not_allowed", detail="denied",
            )
        return AgencyOutcome(
            admitted=True, pack_id=self.pack_id, tool_name=tool_name,
            tool_result=ToolResult(status="completed", output=dict(out)),
        )


def _read_output(tool_name: str, args: Mapping[str, Any]):
    if tool_name == "search_corpus":
        return {"rows": [{
            "id": str(CORPUS_DOC), "score": 1.0,
            "source": {
                "title": "Corpus doc", "raw_body": "Long corpus body.",
                "canonical_url": "https://example.invalid/doc",
                "fetched_at": "2026-05-20T00:00:00+00:00",
                "published_at": "2026-05-10T00:00:00+00:00",
            },
        }]}
    return {"refs": [str(REF)], "rows": [{"x": 1}]}


def _web_output(tool_name: str, args: Mapping[str, Any]):
    return {"rows": [], "teaser_hits": [{"title": "t", "url": "u"}], "landed": 0}


async def _run_toolless(name: str, inputs, options) -> dict[str, Any]:
    llm = RecordingLLM([FINAL_JSON])
    result = await run_method(inputs, options, InlineTargetDeps(llm=llm))
    return _freeze_as_of({
        "profile": name,
        "requests": llm.requests,
        "steps": json.loads(json.dumps(result.intermediate_steps, default=str)),
        "finding": json.loads(result.finding.model_dump_json()),
        "usage": result.usage,
        "derived_from": sorted(str(u) for u in result.derived_from),
    })


async def _run_corpus_researcher() -> dict[str, Any]:
    llm = RecordingLLM([
        '{"tool": "web_evidence", "args": {"query": "Brazil sanctions"}}',
        '{"tool": "search_corpus", "args": {"query": "Brazil emergency", "size": 10}}\n'
        '{"tool": "search_corpus", "args": {"query": "Brazil sanctions", "size": 10}}\n'
        'Search for "Brazil emergency"\n'
        '{"tool": "web_search", "args": {"query": "Brazil emergency", "limit": 5}}',
        '{"done": true}',
        FINAL_JSON,
    ])
    read = FakeBinding("substrate_read", _read_output)
    web = FakeBinding("research", _web_output)
    result = await run_method(
        INPUTS_3,
        {
            "target_id": None,
            "analyst_id": "corpus_researcher",
            "gather_only": True,
            "agency_binding": read,
            "gather_tool_bindings": {"web_evidence": web},
            "gather_web_prompt_fragments": ["Cite the URL you fetched."],
        },
        InlineTargetDeps(llm=llm, max_rounds=3),
    )
    return _freeze_as_of({
        "profile": "corpus_researcher",
        "requests": llm.requests,
        "steps": json.loads(json.dumps(result.intermediate_steps, default=str)),
        "finding": json.loads(result.finding.model_dump_json()),
        "usage": result.usage,
        "derived_from": sorted(str(u) for u in result.derived_from),
        "read_binding_calls": json.loads(json.dumps(read.calls, default=str)),
        "web_binding_calls": json.loads(json.dumps(web.calls, default=str)),
    })


async def _run_journal_gather() -> dict[str, Any]:
    llm = RecordingLLM([
        '{"tool": "get_assessments", "args": {"limit": 5}}',
        '{"tool": "get_critic_scores", "args": {"limit": 3}}',
        '{"done": true}',
    ])
    read = FakeBinding(
        "journal_read", lambda n, a: {"refs": [str(REF)], "rows": [{"n": n}]},
    )
    steps: list[dict[str, Any]] = []
    ctx, usage, refs, gsteps, ext = await _gather(
        InlineTargetDeps(llm=llm, max_rounds=3),
        binding=read,
        user_prompt="JOURNAL PROMPT",
        target_id=None,
        analyst_id="journal_assessor",
        steps=steps,
        tool_bindings={},
        gather_system=None,
        extra_read_tools=("get_assessments", "get_critic_scores"),
        base_offset=0,
    )
    return _freeze_as_of({
        "profile": "journal_assessor_gather",
        "requests": llm.requests,
        "gathered_context": ctx,
        "usage": usage,
        "refs": sorted(str(r) for r in refs),
        "steps": json.loads(json.dumps(gsteps, default=str)),
        "citation_extension": json.loads(json.dumps(ext, default=str)),
        "read_binding_calls": json.loads(json.dumps(read.calls, default=str)),
    })


_TOOLLESS = {
    "country_composition": (
        INPUTS_3, {"target_id": "brazil", "analyst_id": "country_composition"},
    ),
    "world_assessor": (
        INPUTS_5, {"target_id": None, "analyst_id": "world_assessor"},
    ),
    "world_assessment": (
        INPUTS_5, {"target_id": None, "analyst_id": "world_assessment"},
    ),
}


def _fixture(name: str) -> dict[str, Any]:
    raw = json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))
    return _freeze_as_of(raw)


# ---------------------------------------------------------------------------
# THE GATE
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(_TOOLLESS))
async def test_flag_off_identity_toolless(monkeypatch, name):
    """Flag OFF: the three tool-less frozen units reproduce the recording
    exactly — request payloads, trace, finding, usage, lineage.

    The recording is the 311a62d1 capture as re-pinned at a39286bc; the two
    deltas that re-pin carried are named in the module docstring."""
    monkeypatch.delenv(NATIVE_TOOLS_ENV, raising=False)
    inputs, options = _TOOLLESS[name]
    assert await _run_toolless(name, inputs, options) == _fixture(name)


@pytest.mark.parametrize("name", sorted(_TOOLLESS))
async def test_flag_on_toolless_path_is_byte_identical(monkeypatch, name):
    """THE GATE. Flag ON, and the tool-less path is STILL byte-identical.

    A composition/assessment run binds no action pack, so it has no tools to
    offer; ``native_round_plan`` returns None on the empty tool set before it
    even looks at the provider, no ``tools`` key is sent, and no new branch is
    taken. This is what makes the flag safe to raise over the frozen units.
    """
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    inputs, options = _TOOLLESS[name]
    captured = await _run_toolless(name, inputs, options)
    assert captured == _fixture(name)
    assert all(r["tools"] is None for r in captured["requests"])


async def test_flag_off_identity_for_the_tool_using_units(monkeypatch):
    """Flag OFF: the two tool-USING units are byte-identical too — the JSON-text
    protocol is untouched by the restructure that made room for the native one.

    The corpus_researcher recording replays the live 2026-09-15 03:37Z turn that
    carried THREE tool objects plus narration: the text protocol executes ONE of
    them, and this fixture pins that (it is the defect, recorded, not a target).
    """
    monkeypatch.delenv(NATIVE_TOOLS_ENV, raising=False)
    assert await _run_corpus_researcher() == _fixture("corpus_researcher")
    assert await _run_journal_gather() == _fixture("journal_assessor_gather")


# ---------------------------------------------------------------------------
# Recorded core-plane shapes → the REAL provider parser
# ---------------------------------------------------------------------------


def _wire(
    *calls: tuple[str, dict[str, Any]],
    finish_reason: str = "tool_calls",
    text: str | None = None,
    reasoning: str = "The user wants evidence; I will call the tools.",
) -> dict[str, Any]:
    """One core-plane reply, in the wire shape the endpoint returns
    (2026-09-16 probe).

    ``reasoning_content`` is present on EVERY reply from that endpoint, which
    is exactly why the transcript is rebuilt from normalized fields.
    """
    return {
        "id": "chatcmpl-rec",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": text,
                "reasoning_content": reasoning,
                "tool_calls": [
                    {
                        "id": f"chatcmpl-tool-{i}",
                        "type": "function",
                        "function": {
                            "name": name,
                            "arguments": json.dumps(args),
                        },
                    }
                    for i, (name, args) in enumerate(calls)
                ],
            },
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 30000, "completion_tokens": 120,
                  "total_tokens": 30120},
    }


def _no_call_wire(text: str) -> dict[str, Any]:
    return {
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text,
                        "reasoning_content": "Enough gathered."},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 31000, "completion_tokens": 40,
                  "total_tokens": 31040},
    }


class CorePlaneLLM:
    """Replays recorded wire dicts through the production vLLM parser."""

    subprovider = "vllm"

    def __init__(self, wire: list[dict[str, Any]]) -> None:
        self._wire = list(wire)
        self._parser = VLLMProviderHandler()
        self.requests: list[dict[str, Any]] = []

    async def chat_complete(self, messages, **kwargs):
        self.requests.append({
            "messages": [dict(m) for m in messages],
            "tools": kwargs.get("tools"),
        })
        raw = self._wire.pop(0) if self._wire else _no_call_wire("done gathering")
        return self._parser._parse_response(raw, model="core-120b")


async def _native_gather(llm, *, read, tool_bindings=None, max_rounds=3):
    steps: list[dict[str, Any]] = []
    ctx, usage, refs, gsteps, ext = await _gather(
        InlineTargetDeps(llm=llm, max_rounds=max_rounds),
        binding=read,
        user_prompt="USER PROMPT",
        target_id="brazil",
        analyst_id="corpus_researcher",
        steps=steps,
        tool_bindings=tool_bindings or {},
        gather_system=None,
        base_offset=0,
    )
    return ctx, refs, gsteps, ext


async def test_native_round_executes_and_lands_rows(monkeypatch):
    """A native call dispatches through the governed binding, its rows become
    [N]-citable, its id extends lineage — and the model's reasoning_content
    never reaches the transcript."""
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    llm = CorePlaneLLM([_wire(("search_corpus", {"query": "Brazil", "size": 10}))])
    read = FakeBinding("substrate_read", _read_output)

    ctx, refs, gsteps, ext = await _native_gather(llm, read=read)

    assert read.calls == [("search_corpus", {"query": "Brazil", "size": 10})]
    assert CORPUS_DOC in refs
    assert ext[1]["signal_id"] == str(CORPUS_DOC)
    assert "SUBSTRATE INVESTIGATION" in ctx
    call_step = next(s for s in gsteps if s["kind"] == "tool_call")
    assert call_step["protocol"] == "native" and call_step["batch"] == 1
    assert call_step["ok"] is True and call_step["admitted"] is True
    # The tools went out as native specs...
    assert llm.requests[0]["tools"] is not None
    assert {t["function"]["name"] for t in llm.requests[0]["tools"]} >= {
        "search_corpus", "read_document", "list_findings",
    }
    # ...and the replayed turn carries the tool_call id, the tool result carries
    # the matching tool_call_id, and NOTHING carries reasoning_content.
    replay = llm.requests[1]["messages"]
    assistant = next(m for m in replay if m["role"] == "assistant")
    assert assistant["tool_calls"][0]["id"] == "chatcmpl-tool-0"
    tool_msg = next(m for m in replay if m["role"] == "tool")
    assert tool_msg["tool_call_id"] == "chatcmpl-tool-0"
    assert "reasoning_content" not in json.dumps(replay)
    assert "I will call the tools" not in json.dumps(replay)


async def test_multi_call_turn_executes_all_three_in_order(monkeypatch):
    """The 2026-09-15 turn the text protocol lost. Three calls, one turn, all
    three executed, in emitted order, folded as ONE round."""
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    llm = CorePlaneLLM([_wire(
        ("search_corpus", {"query": "Brazil emergency", "size": 10}),
        ("search_corpus", {"query": "Brazil sanctions", "size": 10}),
        ("query_facts", {"subject": "Brazil"}),
    )])
    read = FakeBinding("substrate_read", _read_output)

    _ctx, _refs, gsteps, _ext = await _native_gather(llm, read=read)

    assert [name for name, _ in read.calls] == [
        "search_corpus", "search_corpus", "query_facts",
    ]
    assert read.calls[0][1]["query"] == "Brazil emergency"
    assert read.calls[1][1]["query"] == "Brazil sanctions"
    call_steps = [s for s in gsteps if s["kind"] == "tool_call"]
    assert len(call_steps) == 3
    assert {s["round"] for s in call_steps} == {1}
    assert all(s["batch"] == 3 and s["protocol"] == "native" for s in call_steps)
    # Three results, in the same order, each correlated by its own id.
    replay = llm.requests[1]["messages"]
    tool_msgs = [m for m in replay if m["role"] == "tool"]
    assert [m["tool_call_id"] for m in tool_msgs] == [
        "chatcmpl-tool-0", "chatcmpl-tool-1", "chatcmpl-tool-2",
    ]


async def test_one_failing_tool_does_not_sink_the_batch(monkeypatch):
    """A blocked call in the middle of a batch returns its error to the model;
    the other two still ran and still landed."""
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    llm = CorePlaneLLM([_wire(
        ("search_corpus", {"query": "ok", "size": 5}),
        ("query_facts", {"subject": "blocked"}),
        ("list_findings", {"limit": 5}),
    )])

    def _out(name, args):
        return None if name == "query_facts" else _read_output(name, args)

    read = FakeBinding("substrate_read", _out)

    _ctx, refs, gsteps, _ext = await _native_gather(llm, read=read)

    call_steps = [s for s in gsteps if s["kind"] == "tool_call"]
    assert [s["ok"] for s in call_steps] == [True, False, True]
    assert [s["admitted"] for s in call_steps] == [True, False, True]
    assert CORPUS_DOC in refs and REF in refs
    bodies = [m["content"] for m in llm.requests[1]["messages"] if m["role"] == "tool"]
    assert "tool_blocked" in bodies[1] and "tool_blocked" not in bodies[0]


async def test_forced_call_with_finish_reason_stop_is_executed(monkeypatch):
    """The probe's sharpest finding: a FORCED call comes back with
    ``finish_reason=stop``. Keying on the finish reason would drop it."""
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")
    llm = CorePlaneLLM([_wire(
        ("read_document", {"doc_id": str(CORPUS_DOC)}), finish_reason="stop",
    )])
    read = FakeBinding("substrate_read", _read_output)

    _ctx, _refs, gsteps, _ext = await _native_gather(llm, read=read)

    assert read.calls == [("read_document", {"doc_id": str(CORPUS_DOC)})]
    assert [s["kind"] for s in gsteps][0] == "tool_call"


async def test_write_tools_serialize_while_reads_overlap(monkeypatch):
    """Reads in one turn overlap; writes never do.

    The write tools share one per-run writeback context and land rows later
    rounds read back, so an interleaved pair produces a transcript that does not
    match the substrate.
    """
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")

    class _ConcurrencyBinding:
        def __init__(self, pack_id: str) -> None:
            self.pack_id = pack_id
            self.live = 0
            self.peak = 0
            self.calls: list[str] = []

        async def run_tool(self, tool_name, args, **kw):
            self.calls.append(tool_name)
            self.live += 1
            self.peak = max(self.peak, self.live)
            await asyncio.sleep(0.02)
            self.live -= 1
            return AgencyOutcome(
                admitted=True, pack_id=self.pack_id, tool_name=tool_name,
                tool_result=ToolResult(status="completed", output={"rows": []}),
            )

    reads = _ConcurrencyBinding("substrate_read")
    llm = CorePlaneLLM([_wire(
        ("search_corpus", {"query": "a"}),
        ("query_facts", {"subject": "b"}),
        ("list_findings", {"limit": 3}),
    )])
    await _native_gather(llm, read=reads)
    assert reads.peak == 3, "three read-only calls should have overlapped"

    writes = _ConcurrencyBinding("propose_facts")
    llm2 = CorePlaneLLM([_wire(
        ("propose_fact", {"subject": "a", "predicate": "p", "value": "v",
                          "derived_from": [str(REF)]}),
        ("open_question", {"question": "q?", "derived_from": [str(REF)]}),
    )])
    await _native_gather(
        llm2,
        read=_ConcurrencyBinding("substrate_read"),
        tool_bindings={"propose_fact": writes, "open_question": writes},
    )
    assert writes.calls == ["propose_fact", "open_question"]
    assert writes.peak == 1, "write tools must not overlap"


async def test_fallback_engages_for_a_non_native_provider(monkeypatch):
    """A provider with no proven native tool surface keeps the JSON-text
    protocol, flag or no flag — and its trace carries no protocol key."""
    monkeypatch.setenv(NATIVE_TOOLS_ENV, "1")

    llm = RecordingLLM([
        '{"tool": "search_corpus", "args": {"query": "Brazil", "size": 10}}',
        '{"done": true}',
    ])
    llm.subprovider = "some_unproven_provider"
    read = FakeBinding("substrate_read", _read_output)

    _ctx, refs, gsteps, _ext = await _native_gather(llm, read=read)

    assert read.calls == [("search_corpus", {"query": "Brazil", "size": 10})]
    assert CORPUS_DOC in refs
    assert all(r["tools"] is None for r in llm.requests)
    assert all("protocol" not in s for s in gsteps)

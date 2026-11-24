# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Lane E0 — the run 1b harness on main: temperature, record format, the as-of arm.

Fakes only: a scripted LLM, a recording pool, no registry, no gateway, no
database. NO model is called. Pinned here:

* the harness temperature has ONE declared place (``HOUSE_TEMPERATURE``, 1.0),
  every in-process arm hands it to the loop, and a run directory opened at
  another temperature (run 1b's 0.2) refuses new answers;
* the record-only arm, re-run on the two practice questions through the real
  consult loop, writes run 1b's record format (every key a 1b C-record record
  carried) with its prompt byte-identical to run 1b's (sha256 below);
* ``B-open-asof`` runs arm B's frozen prompt (the pre-registered hash), only
  arm B's pack tools, and every tool call it makes reaches the reader with the
  run's as-of as ``known_at`` — a planner-supplied ``known_at`` is discarded.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import pathlib
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))
sys.path.insert(0, str(ROOT / "src"))

import arm_open  # noqa: E402
import common  # noqa: E402
import run_arm  # noqa: E402

from legba.shared.sampling import HOUSE_TEMPERATURE  # noqa: E402

T = datetime(2026, 11, 5, 7, tzinfo=timezone.utc)
#: run 1b's C-record prompt (provenance.prompt_sha256 on every 1b C-record record).
RECORD_PROMPT_SHA256 = "f1973bb30bf4687d3c8a2297fee2f7023a1b38aaf1094515456641a7dff6b524"
#: Every top-level key of a run 1b C-record record (structure only, no content).
RUN1B_RECORD_KEYS = frozenset({
    "arm", "as_of", "binding_calls", "call_models", "class", "confidence_source", "consult_judge", "cost",
    "created_at", "desk", "desk_display_name", "elapsed_s", "envelope_model", "forced_final", "label", "model",
    "packet", "post_t", "prior_window", "provenance", "question", "question_id", "raw_answer", "receipts",
    "refusal", "request", "rounds_used", "run_id", "schema", "served_model_probe", "stated_confidence",
    "status", "subprovider", "synthesis_status", "tools", "trace_steps", "unanswered_aspects", "usage", "window",
})


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Usage:
    prompt_tokens = 100
    completion_tokens = 20
    reasoning_tokens = 0


class _LLM:
    """Scripted LLM: one tool round, then a final answer. Records each call."""

    subprovider = "fake"

    def __init__(self, tool: str, args: dict[str, Any]) -> None:
        self.script = [json.dumps({"tool": tool, "args": args}),
                       json.dumps({"final": True, "answer": "Nothing in the record answers it.",
                                   "uncertainty": 0.5, "cited_refs": [], "unanswered_aspects": []})]
        self.calls: list[dict[str, Any]] = []

    async def chat_complete(self, messages, *, max_tokens=None, temperature=None, system=None, **kw):
        self.calls.append({"temperature": temperature, "system": system, "messages": messages})
        content = self.script.pop(0) if self.script else self.script_default()
        return SimpleNamespace(content=content, usage=_Usage(), raw_response={"model": "fake-served"})

    @staticmethod
    def script_default() -> str:
        return json.dumps({"final": True, "answer": "done", "uncertainty": 0.5, "cited_refs": []})

    async def on_deactivate(self, _x):
        return None


class _Pool:
    """Recording pool/connection; every read returns no rows."""

    def __init__(self) -> None:
        self.sql: list[tuple[str, tuple]] = []

    def acquire(self) -> "_Pool":
        return self

    async def __aenter__(self) -> "_Pool":
        return self

    async def __aexit__(self, *a) -> bool:
        return False

    async def fetch(self, sql, *a):
        self.sql.append((sql, a))
        return []

    async def fetchval(self, sql, *a):
        self.sql.append((sql, a))
        return ""

    async def fetchrow(self, sql, *a):
        self.sql.append((sql, a))
        return None


def _practice() -> list[Any]:
    return run_arm.load_practice(ROOT / "scripts" / "bakeoff" / "practice_questions.json")


def _run() -> dict[str, Any]:
    return {"run_id": "e0-test", "as_of": T.isoformat(), "design_sha256": "d" * 64,
            "temperature": {"value": arm_open.ROUTE_TEMPERATURE, "source": arm_open.TEMPERATURE_SOURCE}}


def _comp() -> dict[str, Any]:
    return {"model_name": "fast", "version": "v-test"}


# ---------------------------------------------------------------------------
# Temperature: one declared place, 1.0
# ---------------------------------------------------------------------------


def test_harness_temperature_is_the_house_constant():
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps

    assert arm_open.ROUTE_TEMPERATURE is HOUSE_TEMPERATURE
    assert HOUSE_TEMPERATURE == 1.0
    # B-open runs inside the actor at the consult route's default: the same constant.
    assert ConsultOnDemandDeps.__dataclass_fields__["temperature"].default is HOUSE_TEMPERATURE
    assert arm_open.TEMPERATURE_SOURCE == "legba.shared.sampling.HOUSE_TEMPERATURE"


def test_a_run_opened_at_another_temperature_refuses_new_answers():
    with pytest.raises(SystemExit, match="open a new run directory"):
        arm_open.refuse_mixed_temperature({"run_id": "1b-x"})  # run 1b's run.json: no temperature
    with pytest.raises(SystemExit):
        arm_open.refuse_mixed_temperature({"run_id": "x", "temperature": {"value": 0.2}})
    arm_open.refuse_mixed_temperature(_run())


def test_open_records_the_temperature_and_harness_version(tmp_path):
    run1 = tmp_path / "run1"
    run1.mkdir()
    (run1 / "run.json").write_text(json.dumps({"run_id": "r1", "as_of": T.isoformat()}))
    (run1 / "gold.json").write_text("{}")
    dz = SimpleNamespace(version="v1.0", sha256="d" * 64)
    run = arm_open.open_run1b(run1, tmp_path / "new", dz, "M1 baseline")
    assert run["temperature"] == {"value": 1.0, "source": arm_open.TEMPERATURE_SOURCE}
    assert run["harness_version"] == arm_open.ARM_OPEN_VERSION and run["purpose"] == "M1 baseline"
    arm_open.refuse_mixed_temperature(run)


# ---------------------------------------------------------------------------
# The record-only arm, re-run on the two practice questions (fakes), keeps its format
# ---------------------------------------------------------------------------


def test_record_prompt_is_byte_identical_to_run_1b():
    assert hashlib.sha256(arm_open.record_system_prompt().encode()).hexdigest() == RECORD_PROMPT_SHA256


@pytest.mark.parametrize("arm", arm_open.RECORD_ARMS)
def test_record_arm_on_practice_questions_writes_the_run_1b_format(monkeypatch, arm):
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    pool = _Pool()
    llms: list[_LLM] = []

    async def fake_deps(_pool):
        llm = _LLM("series_history", {"series_id": "s", "subject": "X", "from": "2020-01-01", "to": "2021-01-01"})
        llms.append(llm)
        tap = arm_open.ModelTap(llm)
        port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
        binding = arm_open.RecordBinding(port)
        deps = ConsultOnDemandDeps(llm=tap, substrate=port, max_tokens=32768, agency_binding=binding,
                                   temperature=arm_open.ROUTE_TEMPERATURE)
        return deps, binding, tap

    monkeypatch.setattr(arm_open, "build_record_deps", fake_deps)
    qs = _practice()
    assert len(qs) == 2
    for q in qs:
        rec = common.new_record(run=_run(), question=q, arm=arm, as_of=T)
        rec["desk_display_name"] = None
        rec["served_model_probe"] = {"before": {}, "after": {}}
        rec = asyncio.run(arm_open.arm_c_record(pool, pool, q, rec, T, _comp()))
        assert rec["status"] in ("answered", "partial"), rec.get("refusal")
        assert RUN1B_RECORD_KEYS <= set(rec), RUN1B_RECORD_KEYS - set(rec)
        assert rec["provenance"]["prompt_sha256"] == RECORD_PROMPT_SHA256
        assert rec["model"]["temperature"].startswith("1.0 ")
        # D22: the series read reached the port (no `unreachable`) through the router.
        assert rec["binding_calls"][0] == {"tool": "series_history", "failed": False}
    assert all(c["temperature"] == 1.0 for llm in llms for c in llm.calls)


# ---------------------------------------------------------------------------
# B-open-asof — arm B's frozen prompt and pack, every read cut at the run's as-of
# ---------------------------------------------------------------------------


class _CutPort:
    """Records every raw read and its kwargs."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __getattr__(self, name):
        async def reader(**kw):
            self.calls.append((name, kw))
            return {"rows": [], "refs": [], "count": 0}
        return reader


def test_known_at_binding_enforces_the_pack_and_the_cut():
    port = _CutPort()
    b = arm_open.KnownAtBinding(port, T)
    assert set(b.tools) == {"search_signals", "search_corpus", "read_document", "vector_search",
                            "list_targets", "list_sources"}
    out = asyncio.run(b.run_tool("list_findings", {"target_id": "x"}))
    assert out.admitted is False and out.block_cause == "not_in_pack"
    out = asyncio.run(b.run_tool("search_corpus", {"query": "q", "known_at": "2030-01-01", "scope_predicate": "p"}))
    assert out.admitted is True and port.calls[-1] == ("search_corpus", {
        "query": "q", "filters": None, "size": 10, "known_at": T})
    assert b.pack.identity.id == arm_open.B_PACK and b.pack.rules and b.pack.prompt_fragments


def test_asof_deps_use_the_frozen_arm_b_prompt_and_house_temperature():
    deps, binding, tap = arm_open.asof_deps(_LLM("search_signals", {"query": "x"}), _CutPort(), T)
    assert deps.temperature == 1.0 and deps.agency_binding is binding
    from legba.data.analysts.consult_bakeoff import variant_for_binding

    v = variant_for_binding(binding)
    assert hashlib.sha256(v.system_prompt.encode()).hexdigest() == arm_open.RAW_PROMPT_SHA256


def test_b_open_asof_end_to_end_reads_only_before_the_as_of(monkeypatch):
    import legba.runtime.analyst_deps_builder as adb

    llm = _LLM("search_signals", {"query": "drone strike", "known_at": "2030-01-01T00:00:00Z"})
    pool = _Pool()

    async def fake_llm(*a, **kw):
        return llm

    async def fake_backends(rc, secrets):
        return {"opensearch": object(), "qdrant": object(), "embedder": object(), "errors": {}}

    monkeypatch.setattr(adb, "build_llm_handler_from_stack_component", fake_llm)
    monkeypatch.setattr(arm_open, "build_asof_backends", fake_backends)
    q = _practice()[0]
    rec = common.new_record(run=_run(), question=q, arm=arm_open.ASOF_ARM, as_of=T)
    rec["served_model_probe"] = {"before": {}, "after": {}}
    rec = asyncio.run(arm_open.arm_b_open_asof(pool, pool, q, rec, T, _comp()))
    assert rec["status"] in ("answered", "partial"), rec.get("refusal")
    assert RUN1B_RECORD_KEYS - {"desk_display_name"} <= set(rec)
    assert rec["provenance"]["prompt_sha256"] == arm_open.RAW_PROMPT_SHA256
    assert rec["as_of_cut"]["known_at"] == T.isoformat()
    # the model saw arm B's frozen prompt at the house temperature
    assert all(c["temperature"] == 1.0 for c in llm.calls)
    from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS

    assert BAKEOFF_READ_PACKS[arm_open.B_PACK].system_prompt in (llm.calls[0]["system"] or "")
    # the one read it made carried the cut at T, not the planner's 2030 date
    fts = [(s, a) for s, a in pool.sql if "plainto_tsquery('simple', $1)) AS rank" in s]
    assert fts and "created_at < $2" in fts[0][0] and fts[0][1][1] == T


def test_b_open_asof_refuses_when_a_backend_is_missing(monkeypatch):
    async def fake_backends(rc, secrets):
        return {"opensearch": None, "qdrant": object(), "embedder": object(), "errors": {"opensearch": "x"}}

    monkeypatch.setattr(arm_open, "build_asof_backends", fake_backends)
    q = _practice()[0]
    rec = common.new_record(run=_run(), question=q, arm=arm_open.ASOF_ARM, as_of=T)
    rec = asyncio.run(arm_open.arm_b_open_asof(_Pool(), _Pool(), q, rec, T, _comp()))
    assert rec["status"] == "refused" and "opensearch" in rec["refusal"]["reason"]

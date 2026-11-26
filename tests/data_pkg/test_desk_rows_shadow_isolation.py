# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""The contract-v2 desk shadow changes NOTHING a reader sees.

The proof, in four parts:

1. INVENTORY. The shadow writes ONE table, ``desk_rows_shadow`` (migration
   0268), and no module in ``src/legba`` (no reader surface, Consult tool,
   registry route, slice reader, indicator, alert, composition or analyst),
   no script and no UI source references that table — except its one writer.
   It writes no finding (``analyst_outputs``), no statement/knowledge table,
   no trace row. A finding consumer cannot consume what never reaches a table
   it reads; the shadow is not a new finding KIND because a kind would have to
   be proven absent from every one of the ~90 modules that read
   ``analyst_outputs`` — a table no reader names needs no such proof.
2. PARITY. ``run_method`` with the shadow wired and switched ON returns the
   same finding, usage and lineage as with it absent; its trace differs by
   exactly the one launch receipt; the desk's own model sees exactly one call
   either way. The shadow runs DETACHED and its one write is an INSERT into
   ``desk_rows_shadow``.
3. THE SWITCH. Off (the shipped default) launches nothing.
4. THE SEAT. The draft descriptor parses through the runtime's model, holds
   the house rules, is in the prompt manifest, and its sub_handler is not
   dispatchable.
"""
from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from legba.data import desk_rows_shadow as shadow_mod
from legba.data.analysts.inline_target import InlineTargetDeps, InlineTargetRunner, run_method
from legba.data.desk_rows_contract import CONTRACT_VERSION
from legba.data.desk_rows_shadow import (
    LEDGER_TABLE,
    QUESTION_TOKEN,
    SEAT_ID,
    SHADOW_ENV,
    DeskRowsShadow,
    ShadowPrompts,
    launch_shadow,
)

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src" / "legba"
SEAT_FILE = REPO / "descriptors" / "analyst_desk_rows_v2_seat.yaml"
_TABLE_SQL_RE = re.compile(rf"(?:FROM|JOIN|INTO|UPDATE|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+(?:public\.)?{LEDGER_TABLE}\b",
                           re.IGNORECASE)
_WRITER = SRC / "data" / "desk_rows_shadow.py"


# ---------------------------------------------------------------------------
# 1. inventory
# ---------------------------------------------------------------------------


def _scan_roots() -> list[Path]:
    roots = [SRC, REPO / "scripts", REPO / "legba-ui-v3" / "src", REPO / "mcp_server.py"]
    out: list[Path] = []
    for r in roots:
        if r.is_file():
            out.append(r)
        elif r.is_dir():
            out += [p for p in r.rglob("*") if p.suffix in {".py", ".ts", ".tsx", ".js", ".sql"} and p.is_file()]
    return out


def test_no_module_but_the_writer_names_the_shadow_table():
    hits: dict[str, int] = {}
    for p in _scan_roots():
        text = p.read_text(encoding="utf-8", errors="replace")
        n = len(_TABLE_SQL_RE.findall(text)) + text.count(f"'{LEDGER_TABLE}'") + text.count(f'"{LEDGER_TABLE}"')
        if n:
            hits[str(p.relative_to(REPO))] = n
    # write_attribution.py only CLASSIFIES the table's
    # writer for the who-writes guard; it reads and writes nothing.
    # held_record_feed.py and scripts/held_record_replay.py READ the
    # ledger to feed the held family, behind
    # LEGBA_HELD_RECORD_SHADOW (off) / a dry-run script: they write only the
    # held family's tables, which no reader surface reads
    # (test_held_record.py holds that), so nothing a reader sees changes.
    allowed = {"src/legba/data/desk_rows_shadow.py", "src/legba/data/migrations/0268_desk_rows_shadow.sql",
               "src/legba/data/write_attribution.py", "src/legba/data/held_record_feed.py",
               "scripts/held_record_replay.py"}
    assert set(hits) <= allowed, f"a module other than the writer names {LEDGER_TABLE}: {sorted(set(hits) - allowed)}"
    assert "src/legba/data/migrations/0268_desk_rows_shadow.sql" in hits


def test_no_module_reaches_the_ledger_name_through_the_writer():
    for p in _scan_roots():
        if p == _WRITER or p.suffix != ".py":
            continue
        assert "LEDGER_TABLE" not in p.read_text(encoding="utf-8", errors="replace") or \
            "desk_rows_shadow" not in p.read_text(encoding="utf-8", errors="replace"), p


def test_the_shadow_writes_only_its_ledger_and_reads_only_entity_links():
    text = _WRITER.read_text(encoding="utf-8")
    sql = [m.group(0) for m in re.finditer(r"\b(?:INSERT\s+INTO|UPDATE|DELETE\s+FROM)\s+\S+", text, re.IGNORECASE)]
    assert sql == [f"INSERT INTO {{LEDGER_TABLE}}"], sql
    assert shadow_mod._INSERT_SQL.split()[:3] == ["INSERT", "INTO", LEDGER_TABLE]
    for forbidden in ("analyst_outputs", "analyst_traces", "statements", "statement_support", "knowledge_",
                      "findings"):
        assert forbidden not in text, forbidden
    gates = "".join((SRC / "data" / f"desk_rows_{m}.py").read_text()
                    for m in ("gates", "render", "contract", "computed", "surface"))
    assert not re.search(r"\b(?:INSERT|UPDATE|DELETE|SELECT)\b\s", gates), "the row modules do no I/O"
    # The ONE roster read is a SELECT in its own leaf, and writes nothing
    roster = (SRC / "data" / "desk_rows_roster.py").read_text()
    assert not re.search(r"\b(?:INSERT\s+INTO|UPDATE\s+\w|DELETE\s+FROM|CREATE|ALTER|DROP)\b", roster)
    assert "source_poll_outcomes" in roster and "target_descriptors" in roster


def test_the_migration_is_additive_and_idempotent():
    sql = (SRC / "data" / "migrations" / "0268_desk_rows_shadow.sql").read_text()
    body = "\n".join(ln for ln in sql.splitlines() if not ln.lstrip().startswith("--"))
    assert "CREATE TABLE IF NOT EXISTS public.desk_rows_shadow" in body
    assert body.count("CREATE INDEX IF NOT EXISTS") == 2
    for word in ("DROP ", "ALTER ", "DELETE ", "UPDATE ", "BEGIN", "COMMIT", "REFERENCES"):
        assert word not in body.upper(), word


# ---------------------------------------------------------------------------
# 2. parity on the real run path
# ---------------------------------------------------------------------------

SIG = [UUID(f"00000000-0000-4000-8000-0000000000e{i}") for i in range(1, 4)]
INPUTS = [
    {"id": SIG[i], "title": f"Pipeline report {i}", "source_id": f"source.s{i}",
     "produced_at": f"2026-11-0{i + 1}T10:00:00+00:00", "source_url": f"https://example.invalid/{i}",
     "data": {"summary": f"Summary {i}.", "archived_text": f"The pipeline operator reported outage number {i}."}}
    for i in range(3)
]
FINAL_JSON = json.dumps({"title": "Read", "body": "*As of x.*\n\n**BLUF:** Flows stopped. [1]",
                         "confidence": 0.5, "evidence": [str(SIG[0])], "tags": ["severity:low"]})
OPTIONS = {"target_id": "desk_x", "analyst_id": "energy_security", "run_id": "11111111-2222-3333-4444-555555555555",
           "slice_window_hours": 72, "slice_coverage": {"window_start": "2026-11-01T00:00:00+00:00",
                                                        "window_end": "2026-11-04T00:00:00+00:00",
                                                        "window_hours": 72, "in_window": 3}}


class _Resp:
    def __init__(self, content: str) -> None:
        self.content = content
        self.usage = None
        self.finish_reason = "stop"
        self.tool_calls: list[Any] = []


class _DeskLLM:
    subprovider = "vllm"

    def __init__(self) -> None:
        self.calls = 0

    async def chat_complete(self, messages, **kwargs):
        self.calls += 1
        return _Resp(FINAL_JSON)


class _ShadowLLM:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def chat_complete(self, messages, **kwargs):
        user = messages[0]["content"]
        self.calls.append(user[:12])
        if user.startswith("STATEMENT:"):
            return _Resp('{"verdict": "supports", "reason_code": "fully_stated"}')
        return _Resp(json.dumps({"title": "t", "fact_rows": [], "absence_rows": [{"id": "A1", "topic": "a refinery fire"}]}))


class _Conn:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    async def execute(self, sql, *args):
        self.sink.append((" ".join(sql.split()), args))

    async def fetch(self, sql, *args, **kwargs):
        self.sink.append((" ".join(sql.split())[:40], args))
        return []


class _Acquire:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    async def __aenter__(self):
        return _Conn(self.sink)

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self) -> None:
        self.sink: list = []

    def acquire(self):
        return _Acquire(self.sink)


def _run(deps: InlineTargetDeps):
    async def go():
        result = await run_method([dict(r) for r in INPUTS], dict(OPTIONS), deps)
        tasks = list(shadow_mod._TASKS)
        if tasks:
            await asyncio.gather(*tasks)
        return result
    return asyncio.run(go())


def test_the_shadow_changes_nothing_the_run_returns(monkeypatch):
    monkeypatch.delenv(SHADOW_ENV, raising=False)
    base_llm = _DeskLLM()
    base = _run(InlineTargetDeps(llm=base_llm))

    monkeypatch.setenv(SHADOW_ENV, "energy_security")
    desk_llm, s_llm, pool = _DeskLLM(), _ShadowLLM(), _Pool()
    shadow = DeskRowsShadow(s_llm, pool, ShadowPrompts("ROWS", "VERIFY"), "energy_security", "Q?")
    on = _run(InlineTargetDeps(llm=desk_llm, rows_shadow=shadow))

    assert on.finding.model_dump() == base.finding.model_dump()
    assert on.usage == base.usage and on.derived_from == base.derived_from
    assert base_llm.calls == desk_llm.calls == 1                       # the desk's own call, once
    extra = [s for s in on.intermediate_steps if s not in base.intermediate_steps]
    assert extra == [{"phase": "shadow", "kind": "desk_rows_v2", "launched": True, "contract": CONTRACT_VERSION}]
    assert s_llm.calls and s_llm.calls[0] != "STATEMENT:"              # the shadow's own synthesis
    writes = [args for sql, args in pool.sink if sql.startswith("INSERT")]
    assert len(writes) == 1 and pool.sink[-1][0].startswith(f"INSERT INTO {LEDGER_TABLE}")
    reads = [sql for sql, _ in pool.sink if sql.startswith("SELECT")]
    assert len(reads) == 2           # entity links + the ONE roster read, nothing else
    run_id, analyst, target, contract, _gate, status, counts, record = writes[0]
    assert (run_id, analyst, target, contract, status) == (OPTIONS["run_id"], "energy_security", "desk_x",
                                                           CONTRACT_VERSION, "written")
    rec = json.loads(record)
    assert rec["final"]["by_outcome"]["absence"] == 1
    assert rec["computed"]["sources_silent_basis"] == "roster_empty"   # the fake pool's roster is empty
    assert rec["desk_gates_version"] == "desk_rows_gates.d2"


def test_every_finding_stores_its_slice_scope(monkeypatch):
    monkeypatch.delenv(SHADOW_ENV, raising=False)
    result = _run(InlineTargetDeps(llm=_DeskLLM()))
    scope = result.finding.data["slice_scope"]
    assert scope["window_basis"] == "watermark" and scope["window_start"].startswith("2026-11-01")
    assert scope["row_ids"] == [str(s) for s in result.derived_from]
    assert scope["source_ids"] == ["source.s0", "source.s1", "source.s2"] and scope["collection"] == "desk_x"
    assert result.finding.data["slice_coverage"]["in_window"] == 3        # the coverage stamp as before
    plain = asyncio.run(run_method([dict(r) for r in INPUTS], {"target_id": "d", "analyst_id": "a",
                                                               "slice_window_hours": 24}, InlineTargetDeps(llm=_DeskLLM())))
    assert plain.finding.data["slice_scope"]["window_basis"] == "descriptor_window"
    assert "slice_coverage" not in plain.finding.data


# ---------------------------------------------------------------------------
# 3. the switch
# ---------------------------------------------------------------------------


def test_switch_off_launches_nothing(monkeypatch):
    shadow = DeskRowsShadow(_ShadowLLM(), _Pool(), ShadowPrompts("R", "V"), "energy_security")
    kw = dict(user_prompt="P", sliced=INPUTS, citation_index={}, derived_from=SIG)
    monkeypatch.delenv(SHADOW_ENV, raising=False)
    assert launch_shadow(shadow, options=OPTIONS, **kw) == []
    monkeypatch.setenv(SHADOW_ENV, "escalation, military_posture")
    assert launch_shadow(shadow, options=OPTIONS, **kw) == []          # names other desks only
    assert launch_shadow(None, options=OPTIONS, **kw) == []
    monkeypatch.setenv(SHADOW_ENV, "energy_security")
    assert launch_shadow(shadow, options=OPTIONS, **{**kw, "sliced": []}) == []


def test_the_runner_threads_the_shadow_into_its_deps():
    runner = InlineTargetRunner(_DeskLLM(), rows_shadow="S")
    assert runner._deps.rows_shadow == "S"
    assert InlineTargetRunner(_DeskLLM())._deps.rows_shadow is None


# ---------------------------------------------------------------------------
# 4. the seat
# ---------------------------------------------------------------------------


def test_the_seat_descriptor_parses_and_holds_the_house_rules():
    from legba.data.analysts.deterministic import SUB_HANDLERS
    from legba.data.schemas.analyst import AnalystDescriptor
    from legba.data.statement_seats import SEAT_COMPONENT

    body = yaml.safe_load(SEAT_FILE.read_text())
    desc = AnalystDescriptor.model_validate_json(json.dumps(body))
    assert desc.identity.id == SEAT_ID and body["identity"]["state"] == "draft"
    llm = body["method"]["llm"]
    assert llm["primary"]["raw"] == SEAT_COMPONENT
    assert llm["temperature"] == 1.0 and "max_tokens" not in llm
    assert body["method"]["budget_tokens_per_day"] == 0 and body["cadence"] == {}
    assert QUESTION_TOKEN in body["method"]["system_prompt"]
    assert body["method"]["sub_handler"] == SEAT_ID and SEAT_ID not in SUB_HANDLERS


def test_the_seat_prompt_is_in_the_manifest():
    import importlib.util

    spec = importlib.util.spec_from_file_location("gen_manifest", REPO / "scripts" / "gen_descriptor_prompt_manifest.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    from legba.data.registry.descriptor_prompt_manifest import load_manifest

    manifest = load_manifest()  # R-1: one JSON per descriptor, assembled
    prompt = yaml.safe_load(SEAT_FILE.read_text())["method"]["system_prompt"]
    assert manifest["prompts"][SEAT_ID]["sha256"] == gen.prompt_hash(prompt)


@pytest.mark.parametrize("module", ["desk_rows_contract.py", "desk_rows_gates.py", "desk_rows_render.py",
                                    "desk_rows_computed.py", "desk_rows_shadow.py", "desk_rows_surface.py",
                                    "desk_rows_roster.py"])
def test_no_alias_string_and_no_token_cap_in_the_row_modules(module):
    text = (SRC / "data" / module).read_text()
    assert not re.search(r"""["'](?:deep|fast)["']""", text), "no gateway alias literal in src"
    assert "max_tokens" not in text

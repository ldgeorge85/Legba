# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""X3/G0 — the bake-off harness, built (never run) to planning/reviews/BAKEOFF_DESIGN.md v1.0.

DB-free. The real-driver half (verified_only, the receipt scope, the governed
bindings over the DRAFT packs) is ``tests/runtime/test_bakeoff_verified_readers.py``.

What is pinned here:

* the arm variants — tool surfaces in lockstep with the DRAFT pack YAMLs, the
  prompt variants naming only their arm's tools and never ordering finished
  intelligence first, the read-pack resolution the runtime binds by;
* the DRAFT descriptors parse through the real schemas and are registered by
  no bringup script;
* the hard stop — the bake-off spend guard's meter (verified price, cache
  included), its stop rule (room for one round AND the synthesis), its
  refusal of a malformed meter, and the loop delivering the partial;
* the verified-only SQL — INNER join to the faithfulness verdict, floor after
  the fold, the coerce drop, no correlated probe, the floor constants mirrored;
* the harness — the frozen design loader (hash, census, prompts), the record
  shape of every arm, the paid gate, the normaliser's N1–N9 and its frozen
  fixtures (byte equality, design §5.4), the void rules, the blind sheet and
  the tell-test input (6 per arm, stratified, key sealed apart);
* the registry-slim import guard for the new leaf modules.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "bakeoff"))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import grade_sheet  # noqa: E402
import normalise  # noqa: E402

from legba.data import critic_fold as cf  # noqa: E402
from legba.data import verified_findings as vf  # noqa: E402
from legba.data.analysts import consult_bakeoff as bk  # noqa: E402
from legba.data.analysts import consult_bakeoff_prompts as bp  # noqa: E402
from legba.data.analysts.consult_system_prompt import _SYSTEM_PROMPT  # noqa: E402

DESCRIPTORS = ROOT / "descriptors"
FIXTURES = ROOT / "tests" / "data_pkg" / "fixtures" / "bakeoff"

ALL_TOOLS = (
    "search_signals", "query_facts", "inspect_entity", "vector_search", "search_context",
    "search_corpus", "read_document", "query_nexuses", "query_hypotheses", "get_timeline",
    "compare_targets", "query_paths", "find_proxy_chains", "query_brokers", "list_findings",
    "list_situations", "query_predictions", "list_targets", "list_sources", "belief_as_of",
    "query_events", "inspect_event", "series_history", "series_compare",
)


def _yaml(name: str) -> dict:
    return yaml.safe_load((DESCRIPTORS / name).read_text())


# ---------------------------------------------------------------------------
# Arm variants, prompts, descriptors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pack_file", [
    "action_pack_substrate_read_raw.yaml", "action_pack_substrate_read_verified.yaml",
])
def test_pack_yaml_and_variant_tool_surface_are_one(pack_file):
    body = _yaml(pack_file)
    pid = body["identity"]["id"]
    assert body["identity"]["state"] == "draft"
    assert tuple(t["name"] for t in body["tools"]) == bk.BAKEOFF_READ_PACKS[pid].tools


def test_raw_arm_is_exactly_the_six_raw_readers():
    assert set(bk.BAKEOFF_READ_PACKS[bk.RAW_PACK_ID].tools) == {
        "search_signals", "search_corpus", "read_document", "vector_search",
        "list_targets", "list_sources",
    }


def test_verified_pack_sets_the_scope_in_config():
    tools = {t["name"]: t["config"] for t in _yaml("action_pack_substrate_read_verified.yaml")["tools"]}
    assert tools["list_findings"] == {"verified_only": True}
    assert tools["belief_as_of"] == {"verified_only": True}
    assert tools["read_document"] == {"receipt_scoped": True}
    assert set(tools) == {"list_findings", "belief_as_of", "read_document", "list_targets"}


@pytest.mark.parametrize("prompt,arm_pack", [
    (bp.RAW_SYSTEM_PROMPT, bk.RAW_PACK_ID), (bp.VERIFIED_SYSTEM_PROMPT, bk.VERIFIED_PACK_ID),
])
def test_prompt_variant_names_only_its_arm_tools(prompt, arm_pack):
    named = {t for t in ALL_TOOLS if re.search(rf"\b{t}\b", prompt)}
    assert named == set(bk.BAKEOFF_READ_PACKS[arm_pack].tools)
    assert "comes FIRST" not in prompt and "consult these FIRST" not in prompt
    assert "SURVEY THEN DRILL" not in prompt


def test_prompt_variants_share_the_stock_contract_byte_for_byte():
    head = _SYSTEM_PROMPT[: _SYSTEM_PROMPT.index("Available tools:")]
    tail_marker = "Answer quality:"
    for p in (bp.RAW_SYSTEM_PROMPT, bp.VERIFIED_SYSTEM_PROMPT):
        assert p.startswith(head)
        assert "OUTPUT CONTRACT —" in p and "<<<FINAL>>>" in p
        assert p[p.index(tail_marker):].startswith(_SYSTEM_PROMPT[_SYSTEM_PROMPT.index(tail_marker):][:200])
        assert "EVIDENCE FLOOR" in p and "WHAT WOULD CHANGE MY MIND" in p
    assert bp.prompt_sha256(bp.RAW_SYSTEM_PROMPT) != bp.prompt_sha256(bp.VERIFIED_SYSTEM_PROMPT)


@pytest.mark.parametrize("fname,pack", [
    ("analyst_consult_bakeoff_raw.yaml", bk.RAW_PACK_ID),
    ("analyst_consult_bakeoff_verified.yaml", bk.VERIFIED_PACK_ID),
])
def test_bakeoff_descriptors_parse_and_are_draft(fname, pack):
    from legba.data.schemas.analyst import AnalystDescriptor

    d = AnalystDescriptor.model_validate(_yaml(fname))
    assert d.identity.state.value == "draft"
    assert d.identity.kind == "consult_on_demand" or getattr(d.identity.kind, "value", None) == "consult_on_demand"
    assert [r.pack_id for r in d.action_packs] == [pack]
    assert d.method.budget_tokens_per_day == 0
    assert d.method.options == {"mint_watch_candidates": False, "context_ceiling_tokens": 160000}
    assert d.method.llm["primary"]["raw"] == "llm.primary.openai_compat"  # never bills without the override
    assert bk.consult_read_pack_for(d) == pack


def test_no_bringup_script_registers_the_draft_descriptors():
    names = ("action_pack_substrate_read_raw", "action_pack_substrate_read_verified",
             "analyst_consult_bakeoff_raw", "analyst_consult_bakeoff_verified")
    for script in (ROOT / "scripts").glob("bringup_*.py"):
        text = script.read_text()
        assert not any(n in text for n in names), script.name


def test_read_pack_resolution():
    stock = SimpleNamespace(action_packs=[{"pack_id": "substrate_read"}, {"pack_id": "web_access"}])
    assert bk.consult_read_pack_for(stock) == "substrate_read"
    both = SimpleNamespace(action_packs=[{"pack_id": bk.RAW_PACK_ID}, {"pack_id": bk.VERIFIED_PACK_ID}])
    with pytest.raises(ValueError):
        bk.consult_read_pack_for(both)
    binding = SimpleNamespace(pack=SimpleNamespace(identity=SimpleNamespace(id=bk.RAW_PACK_ID)))
    assert bk.variant_for_binding(binding).arm == "B"
    assert bk.variant_for_binding(None) is None


# ---------------------------------------------------------------------------
# Pack-configured handler scopes (fake port; the real gate is the runtime test)
# ---------------------------------------------------------------------------


class _Port:
    def __init__(self, receipt_ok: bool | None = True):
        self.calls: list[tuple[str, dict]] = []
        if receipt_ok is not None:
            self._ok = receipt_ok
        else:
            self.verified_receipt = None  # type: ignore[assignment]

    async def list_findings(self, **kw):
        self.calls.append(("list_findings", kw))
        return {"rows": [], "refs": []}

    async def belief_as_of(self, **kw):
        self.calls.append(("belief_as_of", kw))
        return {"rows": [], "refs": []}

    async def read_document(self, **kw):
        self.calls.append(("read_document", kw))
        return {"status": "found"}

    async def verified_receipt(self, *, doc_id):  # noqa: F811 — replaced per instance
        return self._ok


def _load_pack(fname):
    from legba.data.schemas.action_pack import ActionPack

    body = _yaml(fname)
    body["identity"]["version"] = "0" * 16
    return ActionPack.model_validate(body, strict=False)


def _call(tool, args, pack_id):
    from legba.data.analysts.agency.tools import ToolCall

    return ToolCall(pack_id=pack_id, tool_name=tool, args=args)


def test_verified_pack_forces_verified_only_on_both_finding_readers():
    from legba.data.analysts.agency.substrate_read import belief_as_of_tool, list_findings_tool
    from legba.data.analysts.agency.tools import ToolContext

    pack = _load_pack("action_pack_substrate_read_verified.yaml")
    port = _Port()
    asyncio.run(list_findings_tool(_call("list_findings", {"verified_only": False}, pack.identity.id),
                                   pack, ToolContext(substrate=port)))
    asyncio.run(belief_as_of_tool(_call("belief_as_of", {"as_of": "2026-08-05"}, pack.identity.id),
                                  pack, ToolContext(substrate=port)))
    assert [c[1].get("verified_only") for c in port.calls] == [True, True]


def test_stock_pack_never_passes_verified_only():
    from legba.data.analysts.agency.substrate_read import list_findings_tool
    from legba.data.analysts.agency.tools import ToolContext

    pack = _load_pack("action_pack_substrate_read.yaml")
    port = _Port()
    asyncio.run(list_findings_tool(_call("list_findings", {"verified_only": True}, "substrate_read"),
                                   pack, ToolContext(substrate=port)))
    assert "verified_only" not in port.calls[0][1]


@pytest.mark.parametrize("ok,expect", [(True, "completed"), (False, "failed")])
def test_receipt_scoped_read_document(ok, expect):
    from legba.data.analysts.agency.substrate_read import read_document_tool
    from legba.data.analysts.agency.tools import ToolContext

    pack = _load_pack("action_pack_substrate_read_verified.yaml")
    port = _Port(receipt_ok=ok)
    res = asyncio.run(read_document_tool(_call("read_document", {"doc_id": "x"}, pack.identity.id),
                                         pack, ToolContext(substrate=port)))
    assert res.status == expect
    assert bool(port.calls) is ok
    if not ok:
        assert "receipt_scope" in res.error


def test_receipt_scope_fails_loud_without_a_checker():
    from legba.data.analysts.agency.substrate_read import read_document_tool
    from legba.data.analysts.agency.tools import ToolContext

    pack = _load_pack("action_pack_substrate_read_verified.yaml")
    port = _Port(receipt_ok=None)
    res = asyncio.run(read_document_tool(_call("read_document", {"doc_id": "x"}, pack.identity.id),
                                         pack, ToolContext(substrate=port)))
    assert res.status == "failed" and "receipt_scope_unavailable" in res.error


# ---------------------------------------------------------------------------
# The hard stop
# ---------------------------------------------------------------------------

PRICE = {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25}


def _resp(prompt=1000, out=200, c_read=0, c_write=0):
    u = SimpleNamespace(prompt_tokens=prompt, completion_tokens=out, cache_read_tokens=c_read,
                        cache_write_tokens=c_write, cost_estimate_usd=0.0, model="m", reasoning_tokens=0)
    return SimpleNamespace(usage=u)


def test_meter_prices_provider_usage_with_cache():
    g = bk.spend_guard_for({bk.METER_KEY: {"hard_stop_usd": 5, "price_per_m": PRICE}},
                           stock_kwargs={}, max_output_tokens=32768)
    assert isinstance(g, bk.BakeoffSpendGuard)
    g.record(None, _resp(10_000, 1_000, 100_000, 20_000), {"prompt_tokens": 10_000, "completion_tokens": 1_000})
    expect = (10_000 * 5 + 1_000 * 25 + 100_000 * 0.5 + 20_000 * 6.25) / 1e6
    assert g.verified_usd == pytest.approx(expect)
    m = g.meter()
    assert m["cache_read_tokens"] == 100_000 and m["hard_stop_breached"] is False
    assert bk.METER_KEY in g.snapshot()


def test_stop_leaves_room_for_a_round_and_the_synthesis():
    g = bk.spend_guard_for({bk.METER_KEY: {"hard_stop_usd": 3, "price_per_m": PRICE}},
                           stock_kwargs={}, max_output_tokens=32768)
    g.record(None, _resp(5_000, 500), {"prompt_tokens": 5_000, "completion_tokens": 500})
    assert g.exhausted_reason() is None
    # A transcript that has grown to 100k tokens: one more round + synthesis no longer fit.
    g.record(None, _resp(100_000, 2_000), {"prompt_tokens": 100_000, "completion_tokens": 2_000})
    worst = g.next_call_worst_usd()
    assert g.verified_usd + 2 * worst >= 3
    assert "hard stop" in g.exhausted_reason()


def test_the_stock_ceilings_still_bind_a_bakeoff_run():
    g = bk.spend_guard_for({bk.METER_KEY: {"hard_stop_usd": 50, "price_per_m": PRICE}},
                           stock_kwargs={"max_input_tokens": 1000}, max_output_tokens=32768)
    g.record(None, _resp(2_000, 10), {"prompt_tokens": 2_000, "completion_tokens": 10})
    assert "input-token ceiling" in g.exhausted_reason()


@pytest.mark.parametrize("meter", [
    "five dollars", {"hard_stop_usd": 5}, {"hard_stop_usd": 0, "price_per_m": PRICE},
    {"hard_stop_usd": 5, "price_per_m": {**PRICE, "input": 0}},
    {"hard_stop_usd": 5, "price_per_m": {"input": 5, "output": 25}},
])
def test_a_malformed_meter_refuses_the_run(meter):
    with pytest.raises(ValueError):
        bk.spend_guard_for({bk.METER_KEY: meter}, stock_kwargs={}, max_output_tokens=1000)


def test_no_meter_is_the_stock_guard():
    from legba.data.analysts.consult_spend_guard import SpendGuard

    g = bk.spend_guard_for({}, stock_kwargs={"max_cost_usd": 3.0}, max_output_tokens=1000)
    assert type(g) is SpendGuard


def test_the_loop_stops_drilling_at_the_hard_stop_and_delivers_the_partial():
    """run_method with a tiny hard stop: one round, then the forced synthesis —
    the answer arrives and the trace names the stop."""
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method

    class _LLM:
        subprovider = "anthropic-test"

        def __init__(self):
            self.n = 0

        async def chat_complete(self, messages, **kw):
            self.n += 1
            content = (json.dumps({"tool": "list_targets", "args": {}}) if self.n == 1
                       else "<<<FINAL>>>\nuncertainty: 0.5\n\n## Bottom line\nPartial answer.")
            return SimpleNamespace(content=content, usage=_resp(60_000, 4_000).usage)

    class _Sub:
        async def list_targets(self, **kw):
            return {"rows": [], "refs": []}

    deps = ConsultOnDemandDeps(llm=_LLM(), substrate=_Sub(), max_tokens=8000)
    first = {"question": "q", "mode": "chat", "max_tool_rounds": 10,
             bk.METER_KEY: {"hard_stop_usd": 1.0, "price_per_m": PRICE}}
    result = asyncio.run(run_method([first], {}, deps))
    steps = result.intermediate_steps
    stop = [s for s in steps if s.get("kind") == "spend_ceiling_reached"]
    assert stop and "hard stop" in stop[0]["reason"]
    usage = result.consult_response.data["usage"]
    assert usage[bk.METER_KEY]["calls"] == 2  # one round + the synthesis, nothing more
    assert "Partial answer." in result.consult_response.answer


# ---------------------------------------------------------------------------
# The verified-only SQL
# ---------------------------------------------------------------------------


def test_verified_sql_is_the_admission_rule():
    sql = vf.verified_findings_sql("f.kind = 'finding' AND f.target_id = ANY($1::text[])", 2, 3)
    assert "FROM f JOIN v ON v.fid = f.id::text" in sql  # INNER, not LEFT
    assert "LEFT JOIN" not in sql
    assert "LEAST(f.confidence, v.faithfulness_score) >= $3" in sql
    assert sql.rstrip().endswith("LIMIT $2")              # the limit sits after the floor
    assert vf.COERCE_EXCLUSION in sql
    assert f"LIKE '{cf.FAITHFULNESS_TITLE_LIKE}'" in sql
    belief = vf.verified_belief_sql("f.kind = 'finding'", 2, 3, fold_verdicts="as_of")
    assert "WHERE va.verdict_score IS NOT NULL" in belief
    assert "LEAST(f.confidence, va.verdict_score) >= $3" in belief
    with pytest.raises(ValueError):
        vf.verified_belief_sql("x", 1, 2, fold_verdicts="best")
    receipt = vf.verified_receipt_sql()
    assert "derived_from @> ARRAY[$1::uuid]" in receipt


def test_no_correlated_critique_probe_in_the_new_sql():
    probe = re.compile(r"analyzed_output_id'\s*=\s*[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z0-9_]+::text")
    for sql in (vf.verified_findings_sql("true", 1, 2),
                vf.verified_belief_sql("true", 1, 2, fold_verdicts="latest"),
                vf.verified_receipt_sql()):
        assert not probe.search(sql)


def test_floor_constants_mirror_the_composition(monkeypatch):
    from legba.data.analysts import meta_findings_synthesizer as mfs

    assert vf.VERIFY_FLOOR_ENV == mfs.VERIFY_FLOOR_ENV
    assert vf.DEFAULT_VERIFY_FLOOR == mfs.DEFAULT_VERIFY_FLOOR
    monkeypatch.setenv(vf.VERIFY_FLOOR_ENV, "1.7")
    assert vf.verify_floor() == 1.0
    monkeypatch.setenv(vf.VERIFY_FLOOR_ENV, "nope")
    assert vf.verify_floor() == vf.DEFAULT_VERIFY_FLOOR


# ---------------------------------------------------------------------------
# The frozen design loader (a synthetic design; the real file is not in the tree)
# ---------------------------------------------------------------------------


def _synthetic_design(tmp_path, *, census=None, edit=None) -> pathlib.Path:
    census = census or design_mod.CENSUS
    prefix = {"HEADLINE": "HE", "LONG-MEMORY": "LM", "SLOW-CHANGE": "SC", "INSUFFICIENT": "IE"}
    rows = ["| ID | Class | Desk | Window | Gold | Question |", "|---|---|---|---|---|---|"]
    for k, n in census.items():
        for i in range(1, n + 1):
            window = {"HEADLINE": "T-7d..T", "LONG-MEMORY": "2026-07-01..T",
                      "SLOW-CHANGE": "T-42d..T vs prior 42d", "INSUFFICIENT": "as of T"}[k]
            gold = ("G-CORPUS (e1134b1d-df5d-4f1b-a1f6-d13b70d9a99f, 2026-07-14) + G-WIRE"
                    if k == "LONG-MEMORY" else "G-WIRE")
            rows.append(f"| {prefix[k]}{i:02d} | {k} | country_g20_cn | {window} | {gold} | Q {k} {i}? |")
    body = textwrap.dedent("""\
        # The bake-off
        **Version 9.9 · synthetic**
        ### Arm A — the scheduled tower, as served
        - **Sub-arm A-r:** the same packet with one fixed instruction: "Answer the question
          using only the packet; cite packet items; end with `Confidence: 0.xx`; if the packet
          does not answer it, say so." This is the tower plus a free reader.
        ### Arm B — frontier consult over the RAW corpus
        ### Arm C — frontier consult over VERIFIED findings only
        ### Arm D — a browsing frontier model with no Legba access
        ### Arm E — deterministic change detection + frontier interpretation
        - **Interpretation:** one Messages API call to the frontier model with no tools
          and one fixed prompt: "Answer the question from this change list and its receipts only; say which changes bear on it; end with `Confidence: 0.xx`."
        - **Generalisation:** x
        - **D:** "You are answering a question as of <T ISO>. Use web search and fetch. Question: <text>"
        - **E:** as in arm E above.
        """) + "\n".join(rows) + textwrap.dedent("""

        | Arm | Hard stop per question (Opus) |
        |---|---|
        | B | $5 |
        | C | $3 |
        | D | $3 |
        | E | $1 |

        Each stop delivers the partial.
        """)
    if edit:
        body = edit(body)
    raw = body.encode()
    stamp = hashlib.sha256(raw).hexdigest()
    p = tmp_path / "design.md"
    p.write_bytes(f"PREREG-SHA256: {stamp}\n".encode() + raw)
    return p


def test_design_loader_reads_the_hashed_bytes(tmp_path):
    p = _synthetic_design(tmp_path)
    stamp = p.read_text().split("\n", 1)[0].split()[-1]
    dz = design_mod.load_design(p, pinned=stamp)
    assert len(dz.questions) == 36
    assert dz.hard_stops == {"B": 5.0, "C": 3.0, "D": 3.0, "E": 1.0}
    assert dz.prompt_ar.startswith("Answer the question using only the packet")
    assert dz.prompt_d.endswith("Question: <text>")
    assert dz.question("LM01").anchors == ("e1134b1d-df5d-4f1b-a1f6-d13b70d9a99f",)
    t = datetime(2026, 10, 9, 12, tzinfo=timezone.utc)
    start, end = dz.question("SC01").window_bounds(t)
    assert (end - start).days == 42 and dz.question("SC01").prior_window(t)[0] < start


def test_design_loader_refuses_drift(tmp_path):
    p = _synthetic_design(tmp_path)
    with pytest.raises(design_mod.DesignError, match="not the version"):
        design_mod.load_design(p)  # the synthetic hash is not v1.0's
    tampered = p.read_bytes().replace(b"Q HEADLINE 1?", b"Q HEADLINE one?")
    p.write_bytes(tampered)
    with pytest.raises(design_mod.DesignError, match="edited after freeze"):
        design_mod.load_design(p, pinned="0" * 64)
    short = _synthetic_design(tmp_path, census={**design_mod.CENSUS, "HEADLINE": 8})
    stamp = short.read_text().split("\n", 1)[0].split()[-1]
    with pytest.raises(design_mod.DesignError, match="census"):
        design_mod.load_design(short, pinned=stamp)


def test_the_pinned_hash_is_v1_2():
    """v1.2 (2026-11-11): the normaliser revised under §5.4 (v1.1 2026-11-05: Opus 5.5, graders on the gateway)."""
    assert design_mod.PINNED_SHA256 == "47e364ce845701597b65fa10f648cd2451eb07c7490ea6f45431abfc2a3fa6ff"
    real = pathlib.Path("/usr/local/deployments/active/legba/planning/reviews/BAKEOFF_DESIGN.md")
    if real.is_file():  # the main checkout's planning tree (not mounted in the container)
        dz = design_mod.load_design(real)
        assert dz.sha256 == design_mod.PINNED_SHA256
        assert dz.version == "1.2"  # from the §10 log; the header line still reads 1.0
        assert dz.hard_stops == {"B": 5.0, "C": 3.0, "D": 3.0, "E": 1.0}
        assert len(dz.questions) == 36


def test_design_version_reads_the_version_log(tmp_path):
    log = ("\n## 10. Version log\n\n| Version | Date (UTC) | Change | sha256 |\n|---|---|---|---|\n"
           "| 1.0 | 2026-10-09 | initial freeze | superseded |\n| 1.1 | 2026-11-05 | re-pin | line 1 |\n")
    (tmp_path / "logged").mkdir()
    p = _synthetic_design(tmp_path / "logged", edit=lambda body: body + log)
    stamp = p.read_text().split("\n", 1)[0].split()[-1]
    assert design_mod.load_design(p, pinned=stamp).version == "1.1"
    plain = _synthetic_design(tmp_path)
    stamp = plain.read_text().split("\n", 1)[0].split()[-1]
    assert design_mod.load_design(plain, pinned=stamp).version == "9.9"  # no log: the header line


# ---------------------------------------------------------------------------
# Records, the paid gate
# ---------------------------------------------------------------------------

RECORD_KEYS = {
    "schema", "run_id", "question_id", "class", "desk", "question", "arm", "label", "as_of",
    "window", "prior_window", "status", "refusal", "raw_answer", "stated_confidence",
    "confidence_source", "receipts", "packet", "usage", "cost", "elapsed_s", "model",
    "request", "provenance", "created_at",
}


def _fixture_records():
    return {p.stem: json.loads(p.read_text()) for p in sorted((FIXTURES / "records").glob("*.json"))}


def test_every_arm_has_a_record_fixture_of_the_one_shape():
    recs = _fixture_records()
    assert {r["arm"] for r in recs.values()} == {"A", "A-r", "B", "C", "D", "E"}
    for name, r in recs.items():
        assert RECORD_KEYS <= set(r), name
        assert r["schema"] == common.RECORD_SCHEMA
        assert set(r["usage"]) >= {"input_tokens", "output_tokens", "cache_read_tokens",
                                   "cache_write_tokens", "tool_rounds", "calls"}
        assert set(r["cost"]) >= {"usd", "priced", "price_per_m", "hard_stop_usd", "hard_stop_breached"}
    # The paid arms refused at the call in G0 — the dry-run shape carries the request.
    for arm in ("B", "C", "D"):
        r = recs[f"P02__{arm}"]
        assert r["status"] == "dry_run" and r["refusal"]["reason"].startswith("dry_run")
        assert r["request"] and r["cost"]["usd"] is None
    assert recs["P02__B"]["request"]["body"]["inputs"][0]["bakeoff_meter"]["hard_stop_usd"] == 5.0
    assert recs["P02__C"]["request"]["body"]["inputs"][0]["bakeoff_meter"]["hard_stop_usd"] == 3.0
    assert recs["P02__B"]["request"]["body"]["inputs"][0]["llm_component_override"] == "llm.anthropic.opus_4_7"
    assert recs["P02__D"]["request"]["body"]["tools"][0]["max_uses"] == 15
    assert recs["P02__A"]["cost"] == {"usd": 0.0, "priced": False, "price_per_m": None,
                                      "hard_stop_usd": None, "hard_stop_breached": False}


def test_paid_gate():
    assert common.PaidGate(False, 100, None).refusal().startswith("dry_run")
    assert common.PaidGate(True, None, pathlib.Path("p")).refusal().startswith("dry_run")
    assert common.PaidGate(True, 100, None).refusal().startswith("dry_run")
    g = common.PaidGate(True, 10.0, pathlib.Path("p"))
    assert g.refusal() is None
    assert g.admits({"spent_usd": 4.0, "run_ceiling_usd": 10.0}, 5.0) is None
    assert g.admits({"spent_usd": 6.0, "run_ceiling_usd": 10.0}, 5.0).startswith("run_ceiling")


def test_prices_must_be_marked_verified(tmp_path):
    p = tmp_path / "prices.json"
    p.write_text(json.dumps({"models": {"m": {**PRICE}}}))
    with pytest.raises(RuntimeError, match="verified"):
        common.Prices.load(p, "m")
    p.write_text(json.dumps({"verified_against_invoice": True, "verified_by": "operator",
                             "models": {"m": {**PRICE}}, "web_search_per_1k_usd": 10}))
    pr = common.Prices.load(p, "m")
    assert pr.cost(input_tokens=1_000_000, output_tokens=0, searches=100) == pytest.approx(6.0)
    with pytest.raises(RuntimeError, match="no verified price row"):
        common.Prices.load(p, "other-model")


def test_paid_record_is_never_re_run(tmp_path):
    rec = json.loads((FIXTURES / "records" / "P02__D_synthetic.json").read_text())
    common.write_record(tmp_path, rec)
    assert common.existing_spent(tmp_path, rec["question_id"], "D") is True
    a = json.loads((FIXTURES / "records" / "P02__A.json").read_text())
    common.write_record(tmp_path, a)
    assert common.existing_spent(tmp_path, a["question_id"], "A") is False


# ---------------------------------------------------------------------------
# The normaliser — N1–N9 and the frozen fixtures (byte equality, §5.4)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", sorted(p.stem for p in (FIXTURES / "records").glob("*.json")))
def test_normaliser_reproduces_the_frozen_fixture_byte_for_byte(name):
    rec = json.loads((FIXTURES / "records" / f"{name}.json").read_text())
    expect = (FIXTURES / "normalised" / f"{name}.json").read_text(encoding="utf-8")
    assert normalise.dumps(normalise.normalise(rec)) == expect


def test_normaliser_rules_one_by_one():
    rec = {
        "question": "What did China announce?", "desk_display_name": "China", "stated_confidence": 0.6,
        "citation_map": {"[1]": "aaaaaaaa-0000-4000-8000-000000000001"},
        "receipts": [{"ref": "e1134b1d-df5d-4f1b-a1f6-d13b70d9a99f", "kind": "signal", "documents": [
            {"masthead": "DW", "date": "2026-07-14", "date_word": "published", "title": "T", "body": "B" * 4000}]}],
        "unanswered_aspects": ["the exact date"],
        "raw_answer": textwrap.dedent("""\
            # Title line
            *As of 2026-10-08T23:30:08; assembled from 8 reads*
            ## Bottom line
            Thin evidence: two calls returned three rows.
            China **likely** announced controls on 8 October 2026 【1】 (signal:e1134b1d-df5d-4f1b-a1f6-d13b70d9a99f).
            I searched the record with search_corpus. Legba's read names Beijing.
            - Rare earths [[ref:2]] [single-source]
            | cell a | 3 wire-folded |
            |---|---|
            search_failed: leadership_transition
            not observed in collected reporting
            **Confidence:** 0.6"""),
    }
    out = normalise.normalise(rec)
    text = out["text"]
    assert "Title line" not in text and "assembled from" not in text            # N5 title + as-of
    assert "[R1]" in text and "[R2]" in text and "【" not in text               # N1 + N2
    assert "The evidence gathered for this answer is thin." in text              # N3
    assert "(single source)" in text and "carried by 3 outlets" in text          # N3
    assert "Not established: leadership transition. Searches for it failed." in text  # N3 (v1.2)
    assert "not seen in the reporting reviewed" in text
    assert "Not established: the exact date." in text
    assert "I searched" not in text                                              # N4: no content → deleted
    assert "Beijing" in text and "Legba" not in text                             # N4: content kept, phrase cut
    assert "2026-10-08" in text and "8 October 2026" not in text                 # N8
    assert "likely" in text                                                      # N9: hedges kept
    assert "**" not in text and "|" not in text                                  # N5
    assert text.endswith("Stated confidence: 0.60") and text.count("onfidence:") == 1  # N7
    docs = out["receipts"][1]["documents"]
    assert docs[0]["date"] == "published 2026-07-14" and len(docs[0]["body"]) == 3000


def test_normaliser_v1_2_voice_and_labels():
    """v1.2 N3b + N5 labels: apparatus and voice neutralised; facts, numbers, dates, citations, hedges kept."""
    rec = {
        "question": "Did the strait reopen?", "desk_display_name": "Hormuz", "stated_confidence": None,
        "raw_answer": textwrap.dedent("""\
            ## Bottom line
            **BLUF:** The strait likely stayed closed through 4 November 2026 (country_watch_ir).
            ## What the verified record shows (observed)
            The verified record doesn't show a reopening. I couldn't find any transit count [1].
            The posture desk records no change in basing (event id: [2]).
            Verification: faithfulness=1.00.
            The change record does not answer this question; one indicator flipped (12 raw toggles).
            ## What would change my mind
            If you meant a wider window, a UKMTO notice of 40 transits would."""),
        "citation_map": {"[1]": "aaaaaaaa-0000-4000-8000-000000000001", "[2]": "aaaaaaaa-0000-4000-8000-000000000002"},
    }
    text = normalise.normalise(rec)["text"]
    for gone in ("Bottom line", "BLUF", "country_watch_ir", "verified record", "doesn't", "I couldn't",
                 "desk", "event id", "faithfulness", "change record", "flipped", "raw toggles", "you meant"):
        assert gone not in text, gone
    for kept in ("likely", "2026-11-04", "[R1]", "[R2]", "UKMTO", "40 transits", "(reported inconsistently 12 times)",
                 "The reporting reviewed does not show a reopening", "The reporting reviewed does not show any transit count [R1]",
                 "What would change the assessment:", "If the question meant a wider window"):
        assert kept in text, kept
    assert text.startswith("The strait likely stayed closed")


def test_normaliser_length_rule_keeps_the_best_300_words():
    filler = " ".join(f"Sentence {i} talks about weather in Lisbon and nothing else." for i in range(60))
    rec = {"question": "Did China announce export controls?", "desk_display_name": "China",
           "raw_answer": filler + " China announced export controls on gallium.", "stated_confidence": None}
    out = normalise.normalise(rec)
    assert out["cut"]["words_in"] > 300 >= out["cut"]["words_out"]
    assert "China announced export controls on gallium." in out["text"]
    assert out["text"].endswith("Stated confidence: none")


def test_normaliser_is_stdlib_only():
    src = (ROOT / "scripts" / "bakeoff" / "normalise.py").read_text()
    imports = set(re.findall(r"^(?:import|from) ([a-z_]+)", src, re.M))
    assert imports <= {"__future__", "argparse", "json", "math", "re", "sys", "unicodedata", "pathlib", "typing"}


# ---------------------------------------------------------------------------
# Void rules, the blind sheet, the tell test
# ---------------------------------------------------------------------------


def _dz(tmp_path):
    p = _synthetic_design(tmp_path)
    stamp = p.read_text().split("\n", 1)[0].split()[-1]
    return design_mod.load_design(p, pinned=stamp)


def _answered(arm, qid, klass, text):
    return {"schema": common.RECORD_SCHEMA, "question_id": qid, "class": klass, "arm": arm, "label": arm,
            "status": "answered", "raw_answer": text, "stated_confidence": 0.5, "receipts": [],
            "question": "q", "as_of": "2026-10-09T00:00:00+00:00"}


def test_void_rules(tmp_path):
    dz = _dz(tmp_path)
    gold = {"questions": {
        "HE01": {"signed_by": "op", "met": True}, "HE02": {"signed_by": "op", "met": False},
        "IE01": {"signed_by": "op", "absence_proof": "searched", "authoritative_answer_exists": True},
        "IE02": {"signed_by": "op", "absence_proof": "searched"},
    }}
    recs = [{"question_id": "LM01", "arm": "B", "status": "refused",
             "refusal": {"reason": "gold_missing: anchor not in corpus"}},
            {"question_id": "HE01", "arm": "A", "status": "refused",
             "refusal": {"reason": "void_for_A: the export refused the brief"}}]
    void, arm_void, descriptive = grade_sheet.void_rules(list(dz.questions), gold, recs)
    assert "HE01" not in void and "IE02" not in void
    assert void["HE02"].startswith("the gold builder could not meet")
    assert void["IE01"].startswith("an authoritative answer exists")
    assert void["HE03"].startswith("gold_missing")       # unsigned/missing gold → void
    assert void["LM01"].startswith("gold_missing")
    assert arm_void == {("HE01", "A"): "void_for_A: the export refused the brief"}
    assert "HEADLINE" in descriptive                       # > 2 void in the class


def test_blind_sheet_seals_the_key_apart(tmp_path):
    dz = _dz(tmp_path)
    gold = {"questions": {"HE01": {"signed_by": "op", "met": True, "gold_atoms": ["a"]}}}
    recs = [_answered(arm, "HE01", "HEADLINE", f"Answer from {arm} about Taiwan.") for arm in "ABCDE"]
    recs.append({**_answered("B", "HE01", "HEADLINE", "stock"), "label": "B*", "arm": "B"})
    sheet, key = grade_sheet.build_sheet(dz, recs, gold, seed=7)
    assert len(sheet["items"]) == 5
    assert all("arm" not in it and "label" not in it for it in sheet["items"])
    assert {k["arm"] for k in key["items"].values()} == set("ABCDE")
    assert all(re.fullmatch(r"[a-z0-9]{6}", it["blind_id"]) for it in sheet["items"])
    assert sheet["excluded"] == [{"question_id": "HE01", "arm": "B", "reason": "not the pre-registered arm"}]
    again, _ = grade_sheet.build_sheet(dz, recs, gold, seed=7)
    assert [i["blind_id"] for i in again["items"]] == [i["blind_id"] for i in sheet["items"]]
    out = tmp_path / "sheet"
    with pytest.raises(grade_sheet.SheetRefused):
        grade_sheet._seal(key, out / "key.json", out)
    sha = grade_sheet._seal(key, tmp_path / "sealed" / "key.json", out)
    assert len(sha) == 64
    with pytest.raises(grade_sheet.SheetRefused, match="dry run"):
        grade_sheet.build_sheet(dz, [*recs, {**recs[0], "status": "dry_run"}], gold, seed=7)


def test_tell_test_input_is_stratified_six_per_arm(tmp_path):
    dz = _dz(tmp_path)
    recs = []
    for arm in "ABCDE":
        for klass, pre in (("HEADLINE", "HE"), ("LONG-MEMORY", "LM"), ("SLOW-CHANGE", "SC"), ("INSUFFICIENT", "IE")):
            for i in range(1, 4):
                recs.append(_answered(arm, f"{pre}{i:02d}", klass, f"{arm} answer {klass} {i}."))
    test, key = grade_sheet.build_tell_test(dz, recs, seed=11)
    assert len(test["items"]) == 30
    assert [a["arm"] for a in test["arms"]] == list("ABCDE")
    by_arm: dict[str, list[str]] = {}
    for v in key["items"].values():
        by_arm.setdefault(v["arm"], []).append(v["class"])
    for arm, classes in by_arm.items():
        assert len(classes) == 6
        assert sorted(classes.count(c) for c in grade_sheet.CLASS_ORDER) == [1, 1, 2, 2], arm
    assert all("arm" not in it for it in test["items"])
    ok, acc = grade_sheet.tell_test_passes({b: v["arm"] for b, v in key["items"].items()}, key)
    assert (ok, acc) == (False, 1.0)
    ok, _ = grade_sheet.tell_test_passes({}, key)
    assert ok


def test_tell_test_retest_excludes_the_opened_answers(tmp_path):
    dz = _dz(tmp_path)
    recs = []
    for arm in "ABCDE":
        for klass, pre in (("HEADLINE", "HE"), ("LONG-MEMORY", "LM"), ("SLOW-CHANGE", "SC"), ("INSUFFICIENT", "IE")):
            for i in range(1, 5):
                recs.append(_answered(arm, f"{pre}{i:02d}", klass, f"{arm} answer {klass} {i}."))
    _, first = grade_sheet.build_tell_test(dz, recs, seed=11)
    opened = {(v["question_id"], v["arm"]) for v in first["items"].values()}
    test, key = grade_sheet.build_tell_test(dz, recs, seed=12, exclude=opened)
    assert len(test["items"]) == 30
    assert not opened & {(v["question_id"], v["arm"]) for v in key["items"].values()}


def test_tell_test_refuses_a_thin_arm(tmp_path):
    dz = _dz(tmp_path)
    recs = [_answered("A", "HE01", "HEADLINE", "x")]
    with pytest.raises(grade_sheet.SheetRefused):
        grade_sheet.build_tell_test(dz, recs, seed=1)


# ---------------------------------------------------------------------------
# Arm E's rules over the debounce lane's reader (pure)
# ---------------------------------------------------------------------------


def test_arm_e_applies_the_design_rules_on_top_of_the_reader():
    import arm_e

    def row(ind, frm, to, raw, backfill=False):
        return {"target_id": "d", "source_analyst_id": "u", "indicator_id": ind, "statement": ind,
                "from_status": frm, "to_status": to, "raw_flips_between": raw, "backfill": backfill,
                "onset_at": None, "confirmed_at": None, "onset_finding_id": None, "confirmed_finding_id": None}

    rows = [row("clean_up", "not_observed", "triggered", 1),
            row("clean_down", "triggered", "not_observed", 1, backfill=True),
            row("wobbled", "not_observed", "triggered", 3),
            row("toggler", "not_observed", "triggered", 1)]
    out = {c["indicator_id"]: c for c in arm_e.classify(rows, {("d", "u", "toggler"): 11})}
    assert out["clean_up"]["classification"] == "activated"
    assert out["clean_down"]["classification"] == "cleared" and out["clean_down"]["history"]
    assert out["wobbled"]["classification"] == "wobbled"
    assert out["toggler"]["classification"] == "unstable"
    text = arm_e.render_packet(list(out.values()), [{"desk": "d", "unit": "u", "indicator_id": "quiet",
                                                     "statement": "quiet one", "status": "not_observed"}], {})
    assert "quiet one" in text and "no change" in text
    assert "NOT CHANGES" in text and "11 raw toggles" in text


def test_arm_e_output_cap_keeps_one_call_under_the_stop():
    import arm_e

    cap = arm_e.output_cap("x" * 30_000, PRICE, 1.0)
    input_usd = (30_000 / arm_e.CHARS_PER_TOKEN_PESSIMISTIC) * PRICE["input"] / 1e6
    assert input_usd + cap * PRICE["output"] / 1e6 <= 1.0
    assert arm_e.output_cap("x" * 600_000, PRICE, 1.0) is None  # refused, never squeezed


def test_arm_d_bounds_and_archiver(tmp_path):
    import arm_d

    body = arm_d.request_body("m", "p", searches_left=15, fetches_left=8)
    assert body["tools"][0] == {"type": "web_search_20260209", "name": "web_search", "max_uses": 15}
    assert "temperature" not in body and "thinking" not in body
    assert arm_d.request_body("m", "p", searches_left=0, fetches_left=0)["tools"] == []
    assert arm_d.worst_request_usd(PRICE, 10.0, 15, 8, 0) > 0

    async def fetch(url):
        html = b'<html><title>Page</title><meta property="article:published_time" content="2026-10-11T00:00:00Z">x</html>'
        return 200, html, "text/html"

    as_of = datetime(2026, 10, 9, tzinfo=timezone.utc)
    rec = asyncio.run(arm_d.archive(["https://example.org/a"], tmp_path, as_of, fetch))
    assert rec[0]["archive"]["after_as_of"] == "2026-10-11T00:00:00Z"
    sha = rec[0]["archive"]["sha256"]
    assert (tmp_path / "archive" / f"{sha}.bin").is_file()


# ---------------------------------------------------------------------------
# Registry-slim import guard for the new leaves
# ---------------------------------------------------------------------------


def test_the_verified_findings_leaf_imports_in_the_slim_image():
    """``legba.data.verified_findings`` is the one new module outside the analyst
    package (the port reads it; a registry route may). It must stay a leaf: no
    analyst kind, no runtime, no runtime-only package. The consult-arm modules
    live in ``legba.data.analysts`` and are runtime-side by construction."""
    script = textwrap.dedent("""
        import sys
        for blocked in ("feedparser", "telethon", "warcio", "aiobotocore",
                        "pycountry", "networkx", "qdrant_client"):
            sys.modules[blocked] = None
        import legba.data.verified_findings as vf
        leaked = sorted(m for m in sys.modules
                        if m.startswith("legba.runtime") or m.startswith("legba.data.analysts"))
        assert not leaked, leaked
        assert vf.verify_floor() >= 0
        print("OK")
    """)
    r = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120,
                       env={**os.environ, "PYTHONPATH": os.pathsep.join(
                           p for p in (str(ROOT / "src"), os.environ.get("PYTHONPATH", "")) if p)})
    assert r.returncode == 0 and "OK" in r.stdout, r.stderr


# ---------------------------------------------------------------------------
# W11-G1 — bake-off readiness after the consult-plane changes of rolls 55–57
# ---------------------------------------------------------------------------

#: The Opus 5.5 list price (design v1.1 §7; the prices file of 2026-11-05).
OPUS55 = {"input": 4.0, "output": 20.0, "cache_read": 0.20, "cache_write": 5.0}


@pytest.mark.parametrize("fname", [
    "analyst_consult_bakeoff_raw.yaml", "analyst_consult_bakeoff_verified.yaml",
])
def test_bakeoff_descriptors_run_once_and_validate_under_the_walker_stamp(fname):
    """``method.retry.transient.max_attempts: 1`` exactly as consult_default
    carries it (consult 6cd91190): the actor never re-runs (re-bills) a
    question. Validated as the walker validates it, version ``"0"*16``."""
    from legba.data.schemas.analyst import AnalystDescriptor

    body = _yaml(fname)
    body["identity"]["version"] = "0" * 16
    d = AnalystDescriptor.model_validate(body)
    assert d.identity.state.value == "draft"
    assert d.method.retry.transient.max_attempts == 1
    stock = _yaml("analyst_consult_default.yaml")["method"]["retry"]
    assert body["method"]["retry"] == stock
    text = (DESCRIPTORS / fname).read_text()
    assert "6cd91190" in text and "W11-G1" in text


def _stack_yaml(name):
    from legba.data.schemas.stack import LLMProvider

    body = _yaml(name)
    payload = {**body, "version": "0" * 16}
    return body, LLMProvider.model_validate(payload, strict=False)


def _registrar_files():
    import ast

    tree = ast.parse((ROOT / "scripts" / "bringup_register_stack.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "_STACK_DESCRIPTOR_FILES" for t in node.targets
        ):
            return [e.value for e in node.value.elts]
    return []


def test_the_frontier_component_mirror_pins_opus_5_5():
    body, m = _stack_yaml("stack_component_llm_anthropic_opus_4_7.yaml")
    fable, _ = _stack_yaml("stack_component_llm_anthropic_fable_5_1.yaml")
    assert m.id == "llm.anthropic.opus_4_7" == common.FRONTIER_COMPONENT
    assert m.config.model_name.raw == "claude-opus-5-5" == common.FRONTIER_MODEL
    assert m.config.api_key.raw == "llm.anthropic.api_key" == fable["config"]["api_key"]["raw"]
    assert m.config.timeout_seconds.raw == 300
    assert set(body["config"]) == set(fable["config"])  # the fable file's shape
    assert "historical" in (DESCRIPTORS / "stack_component_llm_anthropic_opus_4_7.yaml").read_text()
    listed = _registrar_files()
    assert "stack_component_llm_anthropic_opus_4_7.yaml" in listed
    # ONE home for the body: the registrar no longer carries an inline copy.
    src = (ROOT / "scripts" / "bringup_register_stack.py").read_text()
    assert '"id": "llm.anthropic.opus_4_7"' not in src
    assert 'os.environ.get("LEGBA_CONSULT_MODEL_NAME"' not in src


def test_the_gold_drafter_component_is_a_draft_gemma_think_mirror():
    from legba.runtime.external_grader_route import GRADER_FAMILY_BY_COMPONENT

    body, m = _stack_yaml("stack_component_llm_verify_innoai_gemma_think.yaml")
    gemma, _ = _stack_yaml("stack_component_llm_verify_innoai_gemma.yaml")
    assert m.id == grade_sheet.GOLD_DRAFTER_COMPONENT
    assert m.state.value == "draft"
    assert m.config.model_name.raw == grade_sheet.GOLD_DRAFTER_ALIAS == "gemma-think"
    for k in ("api_endpoint", "api_key", "tier", "max_concurrent"):
        assert body["config"][k] == gemma["config"][k], k
    assert "stack_component_llm_verify_innoai_gemma_think.yaml" in _registrar_files()
    # Same family as the Gemma judge: the fence refuses it as a grader.
    assert GRADER_FAMILY_BY_COMPONENT[m.id] == GRADER_FAMILY_BY_COMPONENT[
        "llm.verify.innoai_gemma.openai_compat"] == "google_gemma"
    _, grader = _stack_yaml("stack_component_llm_judge_innoai_lightning_think.yaml")
    assert grader.id == grade_sheet.GRADER_COMPONENT
    assert grader.config.model_name.raw == grade_sheet.GRADER_ALIAS == "lightning-think"


def _listing(alias, **extra):
    return {"object": "list", "data": [{"id": "fast", "object": "model"},
                                       {"id": alias, "object": "model", **extra}]}


def _grader_inputs(grader_extra=None, drafter_extra=None, grader_version="a" * 16):
    comps = {
        grade_sheet.GRADER_COMPONENT: {"model_name": "lightning-think", "version": grader_version,
                                       "state": "active", "api_endpoint": "https://gw/v1"},
        grade_sheet.GOLD_DRAFTER_COMPONENT: {"model_name": "gemma-think", "version": "b" * 16,
                                             "state": "active", "api_endpoint": "https://gw/v1"},
    }
    listings = {
        grade_sheet.GRADER_COMPONENT: _listing("lightning-think", **(grader_extra or {})),
        grade_sheet.GOLD_DRAFTER_COMPONENT: _listing("gemma-think", **(drafter_extra or {})),
    }
    return comps, listings


def test_pin_grader_records_the_served_model_and_voids_a_route_change():
    comps, listings = _grader_inputs({"root": "nvidia/nemotron-lightning-x"})
    pin = grade_sheet.grader_pin(comps, listings, probed_at="T1")
    assert pin["primary_grader"]["served_model"] == "nvidia/nemotron-lightning-x"
    assert pin["gold_drafter"]["served_model"] == "gemma-think"  # no root: the alias
    assert pin["second_rater"] == "operator"
    run = {}
    assert grade_sheet.grading_refusal(run) is not None  # not pinned yet
    assert grade_sheet.apply_pin(run, pin) == []
    assert run["grader_model_served"] == "nvidia/nemotron-lightning-x"
    assert grade_sheet.grading_refusal(run) is None
    # A re-probe of the same route is clean …
    assert grade_sheet.apply_pin(run, grade_sheet.grader_pin(comps, listings, probed_at="T2")) == []
    # … a different model behind the alias voids the run.
    comps2, listings2 = _grader_inputs({"root": "nvidia/nemotron-lightning-y"})
    drift = grade_sheet.apply_pin(run, grade_sheet.grader_pin(comps2, listings2, probed_at="T3"))
    assert drift and "served_model" in drift[0]
    assert run["grader_model_served"] == "nvidia/nemotron-lightning-x"  # the pin never moves
    assert "void" in grade_sheet.grading_refusal(run)


def test_pin_grader_refuses_a_wrong_alias_or_a_missing_route():
    comps, listings = _grader_inputs()
    comps[grade_sheet.GRADER_COMPONENT]["model_name"] = "lightning"  # think off
    with pytest.raises(grade_sheet.SheetRefused, match="pins the alias"):
        grade_sheet.grader_pin(comps, listings, probed_at="T")
    comps, listings = _grader_inputs()
    listings[grade_sheet.GRADER_COMPONENT] = {"data": [{"id": "fast"}]}
    with pytest.raises(grade_sheet.SheetRefused, match="lists no"):
        grade_sheet.grader_pin(comps, listings, probed_at="T")


def test_the_consult_invoke_carries_the_meter_and_the_frontier_override():
    import run_arm

    q = design_mod.Question("HE04", "HEADLINE", "country_g20_cn", "T-7d..T", "G-WIRE", "Did China?")
    as_of = datetime(2026, 11, 5, 12, tzinfo=timezone.utc)
    body = run_arm.consult_invoke("B", q, as_of, hard_stop=5.0, price_per_m=OPUS55)
    first = body["inputs"][0]
    assert body["trigger_kind"] == "method" and first["mode"] == "chat"
    assert first["llm_component_override"] == "llm.anthropic.opus_4_7"
    assert first[bk.METER_KEY] == {"hard_stop_usd": 5.0, "price_per_m": OPUS55}
    assert first["max_tool_rounds"] == 10 and first["request_id"]
    # The guard the row arms is the bake-off one, at the row's prices.
    g = bk.spend_guard_for(first, stock_kwargs={}, max_output_tokens=32768)
    assert isinstance(g, bk.BakeoffSpendGuard) and g.price == OPUS55 and g.hard_stop_usd == 5.0


def test_the_harness_outwaits_the_registrys_derived_invoke_floor(monkeypatch):
    import run_arm
    from legba.data.registry import consult_runs

    monkeypatch.setenv("LEGBA_CONSULT_BUDGET_SECONDS", "3600")
    monkeypatch.setenv("LEGBA_CONSULT_ROUND_DEADLINE_SECONDS", "1800")
    monkeypatch.setenv("LEGBA_CONSULT_FINAL_FLOOR_SECONDS", "900")
    monkeypatch.delenv("LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS", raising=False)
    assert run_arm.INVOKE_TIMEOUT_S >= consult_runs.invoke_timeout_seconds()
    assert run_arm.INVOKE_TIMEOUT_S >= 4800


def test_the_frontier_drift_refusal():
    assert common.frontier_refusal({"model_name": "claude-opus-5-5"}) is None
    assert common.frontier_refusal({"model_name": "claude-opus-5"}).startswith("frontier_drift")


def test_arm_d_sends_the_served_model_the_web_tools_and_no_temperature():
    import arm_d
    import messages_api

    body = arm_d.request_body(common.FRONTIER_MODEL, "p", searches_left=15, fetches_left=8)
    assert body["model"] == "claude-opus-5-5"
    assert body["tools"] == [
        {"type": "web_search_20260209", "name": "web_search", "max_uses": 15},
        {"type": "web_fetch_20260209", "name": "web_fetch", "max_uses": 8,
         "max_content_tokens": arm_d.FETCH_MAX_CONTENT_TOKENS, "citations": {"enabled": True}},
    ]
    assert (messages_api.WEB_SEARCH_TOOL, messages_api.WEB_FETCH_TOOL) == (
        "web_search_20260209", "web_fetch_20260209")
    assert not {"temperature", "top_p", "top_k", "thinking"} & set(body)


# --- the consult loop under fakes, through the harness's record builder ------


@dataclass
class _U:
    prompt_tokens: int = 20_000
    completion_tokens: int = 1_000
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    total_tokens: int = 21_000
    cost_estimate_usd: float = 0.0
    model: str = "claude-opus-5-5"


@dataclass
class _TC:
    id: str
    name: str
    arguments: dict


@dataclass
class _R:
    content: str = ""
    finish_reason: str = "stop"
    tool_calls: list = field(default_factory=list)
    usage: _U = field(default_factory=_U)
    raw_response: dict | None = None


class _RawSub:
    async def search_signals(self, **kw):
        return {"count": 1, "rows": [{"id": "sig-1", "title": "t"}], "refs": []}

    async def list_targets(self, **kw):
        return {"rows": [], "refs": []}


ANSWER = "## Bottom line\n\n" + ("China announced a new export-control list. " * 40)


class _NativeLLM:
    """Round 1 calls a tool; round 2 streams ``ANSWER`` and then either returns
    it (``cut=None``), stalls past the round deadline (``"deadline"``), or dies
    with ``cut`` (an exception instance) after the text is out."""

    PRICE_TABLE: dict = {}

    def __init__(self, cut=None):
        self.subprovider = "anthropic"
        self.cut = cut
        self.calls = 0

    async def chat_complete(self, messages, **kw):
        from legba.data.stack.llm.stream_observer import publish_text_delta

        self.calls += 1
        if self.calls == 1:
            return _R(finish_reason="tool_calls",
                      tool_calls=[_TC("c0", "search_signals", {"query": "export controls"})])
        if not kw.get("tools"):
            return _R(content="<<<FINAL>>>\nuncertainty: 0.4\n\nforced answer")
        for i in range(0, len(ANSWER), 60):
            publish_text_delta(ANSWER[i:i + 60])
            await asyncio.sleep(0)
        if self.cut == "deadline":
            await asyncio.sleep(30)
        if self.cut is not None:
            raise self.cut
        return _R(content=ANSWER)


def _run_bakeoff(llm, **deps_kw):
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, run_method

    deps = ConsultOnDemandDeps(llm=llm, substrate=_RawSub(), max_tokens=8000, **deps_kw)
    first = {"question": "Did China announce export controls?", "mode": "chat", "max_tool_rounds": 10,
             "request_id": "r-1", bk.METER_KEY: {"hard_stop_usd": 5.0, "price_per_m": OPUS55}}
    return asyncio.run(run_method([first], {}, deps))


def _envelope(result):
    """Exactly what the actor returns for a chat consult (dapr_actors' chat short-circuit)."""
    return {"outcome": "success", "mode": "chat",
            "consult_response": result.consult_response.model_dump(mode="json"), "derived_from": []}


def _blank_record():
    return {"usage": {}, "cost": {}, "refusal": None}


def test_a_bakeoff_row_installs_the_meter_and_the_harness_charges_it():
    """(a) the guard rides a request carrying ``bakeoff_meter``: provider usage
    at the ROW's price, on the receipt; the harness records ``answered`` and
    charges the meter, not the table and not the stop."""
    import run_arm
    from legba.data.analysts import consult_on_demand as cod

    llm = _NativeLLM()
    result = _run_bakeoff(llm)
    meter = result.consult_response.data["usage"][bk.METER_KEY]
    per_call = (20_000 * 4.0 + 1_000 * 20.0) / 1e6
    assert meter["calls"] == 2 and meter["verified_usd"] == pytest.approx(2 * per_call)
    assert meter["price_per_m"] == OPUS55 and meter["next_call_worst_usd"] > 0
    rec = _blank_record()
    charged, _refs = run_arm.consult_record_from_result(
        rec, status_code=200, result=_envelope(result), text="", hard_stop=5.0)
    assert rec["status"] == "answered" and rec["synthesis_status"] == "complete"
    assert charged == pytest.approx(2 * per_call) and rec["cost"]["unmetered_cuts"] == 0
    assert "China announced a new export-control list." in rec["raw_answer"]
    # The wiring the runtime runs: run_method arms the guard off the request row.
    import inspect

    assert "_bk.spend_guard_for(" in inspect.getsource(cod.run_method)


def test_a_round_cut_by_its_deadline_is_recorded_partial_with_its_text_and_cost():
    """(b) the ``partial_final`` path: the text is the answer (banner kept apart),
    status ``partial`` — never ``error`` — and the cut call, unseen by the meter,
    is charged the meter's worst-case projection under the stop."""
    import run_arm

    llm = _NativeLLM(cut="deadline")
    result = _run_bakeoff(llm, round_deadline_seconds=0.2)
    assert llm.calls == 2  # no forced final bought for an answer already paid for
    data = result.consult_response.data
    assert data["synthesis_status"] == "partial"
    rec = _blank_record()
    charged, _ = run_arm.consult_record_from_result(
        rec, status_code=200, result=_envelope(result), text="", hard_stop=5.0)
    assert rec["status"] == "partial" and rec["refusal"] is None
    assert rec["raw_answer"].startswith("## Bottom line")
    assert rec["raw_answer"].strip() == ANSWER.strip()
    assert rec["partial_banner"].startswith("> **Synthesis interrupted")
    meter = data["usage"][bk.METER_KEY]
    assert meter["calls"] == 1  # the cut round never reached the meter …
    assert rec["cost"]["unmetered_cuts"] == 1  # … so the harness charges it
    assert charged == pytest.approx(meter["verified_usd"] + meter["next_call_worst_usd"])
    assert charged < 5.0


@pytest.mark.parametrize("exc", [
    "stream_interrupted", RuntimeError("connection reset by peer"),
])
def test_a_stream_cut_or_any_exception_with_text_delivers_the_partial(exc):
    """(c) W11-F133: a ``StreamInterruptedFailure`` — or ANY exception — after
    answer text delivers that text; no forced final; the harness records it."""
    import run_arm
    from legba.data.stack.llm.base import StreamInterruptedFailure

    if exc == "stream_interrupted":
        exc = StreamInterruptedFailure("stream closed without its terminator",
                                       partial_text=ANSWER, tool_use_started=False)
    llm = _NativeLLM(cut=exc)
    result = _run_bakeoff(llm)
    assert llm.calls == 2
    steps = result.consult_response.data["steps"]
    pf = [s for s in steps if s.get("kind") == "partial_final"]
    assert pf and pf[0]["reason"] == type(exc).__name__
    assert not [s for s in steps if s.get("kind") == "forced_final"]
    rec = _blank_record()
    charged, _ = run_arm.consult_record_from_result(
        rec, status_code=200, result=_envelope(result), text="", hard_stop=3.0)
    assert rec["status"] == "partial"
    assert "China announced a new export-control list." in rec["raw_answer"]
    assert rec["cost"]["unmetered_cuts"] == 1 and 0 < charged <= 3.0


def test_a_failed_actor_run_is_an_error_charged_its_full_stop():
    import run_arm

    rec = _blank_record()
    charged, refs = run_arm.consult_record_from_result(
        rec, status_code=200, result={"outcome": "hard_fail", "error": "boom"}, text="", hard_stop=5.0)
    assert rec["status"] == "error" and charged == 5.0 and refs == []
    rec = _blank_record()
    env = {"outcome": "success", "consult_response": {"answer": "x", "data": {"synthesis_status": "complete"}}}
    charged, _ = run_arm.consult_record_from_result(rec, status_code=200, result=env, text="", hard_stop=3.0)
    assert charged == 3.0 and rec["cost"]["meter_missing"] is True  # no meter: the stop


def test_the_cut_surcharge_never_exceeds_the_stop_nor_hides_a_breach():
    import run_arm

    def env(verified, worst, cuts):
        steps = [{"kind": "partial_final"}] * cuts
        return {"outcome": "success", "consult_response": {"answer": "a", "data": {
            "synthesis_status": "partial", "steps": steps,
            "usage": {bk.METER_KEY: {"verified_usd": verified, "next_call_worst_usd": worst}}}}}

    rec = _blank_record()
    charged, _ = run_arm.consult_record_from_result(rec, status_code=200, result=env(2.0, 4.0, 1), text="", hard_stop=3.0)
    assert charged == 3.0
    rec = _blank_record()
    charged, _ = run_arm.consult_record_from_result(rec, status_code=200, result=env(3.4, 4.0, 2), text="", hard_stop=3.0)
    assert charged == 3.4 and rec["cost"]["hard_stop_breached"] is True
    assert run_arm.unmetered_cuts([{"kind": "degraded_final", "reason": "no_budget_for_synthesis"},
                                   {"kind": "degraded_final", "reason": "synthesis_deadline_exceeded"},
                                   {"kind": "round_stream_interrupted"}]) == 2


def test_grading_refuses_an_unpinned_run_and_tell_score_reports_the_criterion(tmp_path):
    run_dir = tmp_path / "run"
    (run_dir / "records").mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"spent_usd": 0.0}))
    p = _synthetic_design(tmp_path)
    stamp = p.read_text().split("\n", 1)[0].split()[-1]
    real_load = design_mod.load_design
    try:
        grade_sheet.design_mod.load_design = lambda path: real_load(path, pinned=stamp)
        with pytest.raises(grade_sheet.SheetRefused, match="pin-grader"):
            grade_sheet.main(["tell-test", "--design", str(p), "--run-dir", str(run_dir), "--seed", "1",
                              "--out", str(tmp_path / "g"), "--key-out", str(tmp_path / "k.json")])
    finally:
        grade_sheet.design_mod.load_design = real_load
    key = {"seed": 7, "items": {f"id{i}": {"arm": "ABCDE"[i % 5]} for i in range(30)}}
    (tmp_path / "key.json").write_text(json.dumps(key))
    (tmp_path / "pred.json").write_text(json.dumps({f"id{i}": "A" for i in range(30)}))  # 6/30 = 20 %
    rc = grade_sheet.main(["tell-score", "--key", str(tmp_path / "key.json"),
                           "--predictions", str(tmp_path / "pred.json"), "--run-dir", str(run_dir)])
    assert rc == 0
    assert json.loads((run_dir / "run.json").read_text())["tell_tests"] == [
        {"accuracy": 0.2, "passed": True, "seed": 7}]
    (tmp_path / "pred.json").write_text(json.dumps({f"id{i}": "ABCDE"[i % 5] for i in range(30)}))
    assert grade_sheet.main(["tell-score", "--key", str(tmp_path / "key.json"),
                             "--predictions", str(tmp_path / "pred.json")]) == 3


def test_every_harness_chat_endpoint_goes_through_chat_url():
    """2026-11-05 practice dry run: arm A-r posted to `…/v1/v1/chat/completions` (404) because
    it appended `/v1/chat/completions` to a component endpoint that already ends in `/v1`.
    Every harness script builds its chat URL with `common.chat_url`, which handles both."""
    assert common.chat_url("https://gw/v1") == "https://gw/v1/chat/completions"
    assert common.chat_url("https://gw") == "https://gw/v1/chat/completions"
    assert common.chat_url("https://gw/v1/") == "https://gw/v1/chat/completions"
    root = pathlib.Path(common.__file__).parent
    for py in sorted(root.glob("*.py")):
        if py.name == "common.py":
            continue
        src = py.read_text(encoding="utf-8")
        assert "/chat/completions" not in src, f"{py.name} builds a chat URL by hand; use common.chat_url"

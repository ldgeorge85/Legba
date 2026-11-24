#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Run 1b — two DIAGNOSTIC arms on the free self-hosted core plane ($0).

Run 1 found arm B (the frontier model reading the raw corpus with tools) well
ahead of arm A (the published scheduled read), but B differs from A in both
model and access. Run 1b adds, over the same questions, as-of, gold, normaliser
and grader:

  B-open    EXACTLY arm B's procedure — the consult ACTOR
            ``analyst::consult_bakeoff_raw::<ver>`` (pack ``substrate_read_raw``,
            the arm's own prompt, 10 rounds, chat, no pins/case/writes) — with the
            per-request plane override set to the core plane
            (``llm.primary.openai_compat``, gateway alias ``fast``) instead of the
            frontier plane. ``B-open-2`` is an independent second sample.
  C-record  the SAME consult loop (``consult_on_demand.run_method``, the same
            code the actor runs) on the same core plane, restricted to the
            STRUCTURED RECORD: findings (all history), belief-as-of, facts,
            entities, events, curated observation series, targets. No raw signal
            text. The runtime only knows the two bake-off packs, so this arm runs
            the loop IN THIS PROCESS (no product code changed): a harness binding
            enforces the tool set (anything else is refused at dispatch), the
            prompt is assembled the way ``consult_bakeoff_prompts`` assembles the
            arms' prompts, and the tools are the production
            ``PostgresQdrantSubstrateQueryPort`` reads over a READ-ONLY pool.

ZERO PAID CALLS. The B-open request names the core plane as the override (the
actor fails closed if it cannot resolve it — it never falls back to the
frontier plane); C-record builds the core-plane handler directly. Both carry a
``bakeoff_meter`` at a NOMINAL $1/M on every token class with a $1,000 stop —
it never binds; it is there so the loop meters tokens and calls exactly as it
did for B. ``cost.usd`` is 0.

SERVED MODEL. The consult envelope reports the component's model (``fast``);
the gateway answers with an alias, not an upstream id. Every question is
bracketed by a canary: ``/v1/models`` entry for ``fast`` and one tiny chat call
whose response ``model`` is recorded (``served_model_probe``). C-record also
records the ``model`` every one of its calls came back with.

POST-T EXPOSURE. The raw readers have no date bound and run 1b runs a week
after T, so every record carries ``post_t``: how many cited refs entered the
corpus (signals.created_at / analyst_outputs.created_at / facts.created_at /
events.created_at) after T. The report reads it.

ON MAIN (lane E0, harness version ``ARM_OPEN_VERSION``). Three changes after
run 1b, none to a frozen piece (the arm prompts' hashes, the design pin, the
normaliser and the record prompt are byte-identical):

  * TEMPERATURE has ONE declared place: ``legba.shared.sampling.HOUSE_TEMPERATURE``
    (1.0). The consult actor's route default reads it (B-open), and the
    in-process arms pass :data:`ROUTE_TEMPERATURE`, which IS it. Run 1b ran at a
    0.2 code default (deviation D13); that is history, not a setting here.
  * ``B-open-asof`` — arm B's tools, pack fragments and frozen prompt, run in
    THIS process (the actor cannot take a cut) with every raw reader cut at the
    run's as-of (``known_at``, ``legba.data.raw_reader_known_at``): nothing
    that entered the record at or after T reaches the model.
  * ``C-record-2`` — an independent second sample of C-record. The series tools
    now dispatch through the production router (D22 fixed there).

LANE E2 (harness ``e2.1``). The record arms ``C-record`` / ``C-record-2`` read
the record AS OF T: every record reader is cut at the run's as-of (``known_at``,
classified in ``legba.data.raw_reader_known_at.RECORD_KNOWN_AT_CUTS``; each
record carries the classification as ``as_of_cut``). The prompt, tools and
loop are unchanged (the prompt sha is still run 1b's), so a C-record answer
from this harness differs from run 1b's in temperature and in what the arm can
see, nothing else.

USAGE
    python3 scripts/bakeoff/arm_open.py open  --run1 <run1 dir> --out <run dir> --design <D>
    python3 scripts/bakeoff/arm_open.py run   --out <run dir> --design <D> --arm B-open --all
    python3 scripts/bakeoff/arm_open.py run   --out <run dir> --design <D> --arm C-record --question <QID>
    python3 scripts/bakeoff/arm_open.py run   --out <run dir> --design <D> --arm B-open-asof --all
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402
import design as design_mod  # noqa: E402
import normalise  # noqa: E402
import run_arm  # noqa: E402

sys.path.insert(0, str(common.REPO_ROOT / "src"))
from legba.shared.sampling import HOUSE_TEMPERATURE  # noqa: E402

#: Bumped when the arms' procedure changes; stamped on run.json and every record.
#: e2.1: the record arms (C-record, C-record-2) read with the known_at cut at T.
ARM_OPEN_VERSION = "e2.1"
RUN1B_SCHEMA = "legba.bakeoff.run/1"
CORE_COMPONENT = "llm.primary.openai_compat"
CORE_ALIAS = "fast"
ASOF_ARM = "B-open-asof"
RECORD_ARMS = ("C-record", "C-record-2")
ARMS = ("B-open", "B-open-2", *RECORD_ARMS, ASOF_ARM)
B_ANALYST = "consult_bakeoff_raw"
B_PACK = "substrate_read_raw"
RECORD_PACK = "substrate_read_record"
RECORD_ANALYST = "consult_bakeoff_record"
#: THE harness temperature — not a number of its own: the house constant, which
#: is also ``ConsultOnDemandDeps.temperature``'s default, so B-open (inside the
#: actor) and the in-process arms run at the same declared value. Run 1b's 0.2
#: was a code default since replaced (D13); a new run cannot reproduce it here.
ROUTE_TEMPERATURE = HOUSE_TEMPERATURE
TEMPERATURE_SOURCE = "legba.shared.sampling.HOUSE_TEMPERATURE"
NOMINAL_METER = {"hard_stop_usd": 1000.0,
                 "price_per_m": {"input": 1.0, "output": 1.0, "cache_read": 1.0, "cache_write": 1.0}}
#: The structured record: every existing read tool over findings, facts,
#: entities, events and curated series. No raw text reader.
RECORD_TOOLS = (
    "list_findings", "belief_as_of", "query_facts", "inspect_entity",
    "query_events", "inspect_event", "series_history", "series_compare", "list_targets",
)
#: What the brief asked for that no existing read tool exposes (not built).
RECORD_UNAVAILABLE = (
    "desk_baselines sidecar table (per-desk trailing-rate baselines): no read tool; "
    "only the desk_baseline analyst's summary findings, via list_findings(analyst_id='desk_baseline')",
    "indicator_tracker signpost flip history (the series arm E reads): no consult read tool",
    "observation series outside the curated collections: series_history/series_compare read only "
    "the curated `observations` collections",
)
FINAL_SENTINEL_RE = re.compile(r"\s*<<<FINAL>>>\s*$")
#: The live runtime's consult knobs (read off the runtime container, 2026-11-13),
#: set in THIS process so the in-process loop runs on the same clocks and caps.
RUNTIME_CONSULT_ENV = {
    "LEGBA_CONSULT_BUDGET_SECONDS": "3600",
    "LEGBA_CONSULT_FINAL_FLOOR_SECONDS": "900",
    "LEGBA_CONSULT_ROUND_DEADLINE_SECONDS": "1800",
    "LEGBA_CONSULT_KEEP_FULL_ROUNDS": "1000",
    "LEGBA_CONSULT_NATIVE_BATCH_CAP": "5",
    "LEGBA_CONSULT_ROUND_RESULT_BYTES": "40000",
    "LEGBA_CONSULT_MAX_COST_USD_PER_RUN": "1000000",
    "LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN": "100000000",
}
EVENT_RECEIPT_SIGNALS = 5


# ---------------------------------------------------------------------------
# The run directory (a sibling of run 1; run 1 is never written)
# ---------------------------------------------------------------------------


DEFAULT_PURPOSE = "diagnostic extension of run 1 (not confirmatory): arms " + ", ".join(ARMS)


def open_run1b(run1: Path, out: Path, dz: Any, purpose: str = DEFAULT_PURPOSE) -> dict[str, Any]:
    out.mkdir(parents=True, exist_ok=True)
    path = out / "run.json"
    r1 = json.loads((run1 / "run.json").read_text(encoding="utf-8"))
    gold_src, gold_dst = run1 / "gold.json", out / "gold.json"
    if not gold_dst.exists():
        shutil.copyfile(gold_src, gold_dst)
    if common.sha256_file(gold_src) != common.sha256_file(gold_dst):
        raise SystemExit("run1b gold.json differs from run 1's — refusing")
    if path.is_file():
        return json.loads(path.read_text(encoding="utf-8"))
    run = {
        "schema": RUN1B_SCHEMA,
        "run_id": f"{ARM_OPEN_VERSION}-" + uuid4().hex[:10],
        "purpose": purpose,
        "parent_run": {"dir": str(run1), "run_id": r1["run_id"], "run_json_sha256": common.sha256_file(run1 / "run.json")},
        "as_of": r1["as_of"],
        "design_version": dz.version,
        "design_sha256": dz.sha256,
        "normaliser_sha256": common.sha256_file(Path(normalise.__file__)),
        "harness_git_sha": common.git_sha(),
        "gold_sha256": common.sha256_file(gold_dst),
        "harness_version": ARM_OPEN_VERSION,
        "temperature": {"value": ROUTE_TEMPERATURE, "source": TEMPERATURE_SOURCE},
        "run_ceiling_usd": 0.0,
        "spent_usd": 0.0,
        "questions_refused_at_ceiling": [],
        "created_at": common.utcnow().isoformat(),
    }
    common.write_json(path, run)
    return run


# ---------------------------------------------------------------------------
# The served-model canary (alias -> what the gateway says)
# ---------------------------------------------------------------------------


async def served_model_probe(comp: dict[str, Any], key: str) -> dict[str, Any]:
    import httpx

    out: dict[str, Any] = {"at": common.utcnow().isoformat(), "alias": comp["model_name"]}
    h = {"Authorization": f"Bearer {key}"}
    base = str(comp["api_endpoint"]).rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=60.0) as c:
            m = (await c.get(f"{base}/models" if base.endswith("/v1") else f"{base}/v1/models", headers=h)).json()
            entry = next((x for x in m.get("data") or [] if x.get("id") == comp["model_name"]), None)
            out["models_entry_served_model"] = (entry or {}).get("served_model")
            out["models_entry_sha256"] = hashlib.sha256(normalise.dumps(entry).encode()).hexdigest() if entry else None
            r = await c.post(common.chat_url(comp["api_endpoint"]), headers=h,
                             json=common.chat_body(comp["model_name"], [{"role": "user", "content": "Reply OK."}]))
            out["chat_response_model"] = r.json().get("model")
    except Exception as exc:  # noqa: BLE001 — a probe never fails a question
        out["error"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


# ---------------------------------------------------------------------------
# Post-T exposure of the cited refs
# ---------------------------------------------------------------------------


async def post_t_exposure(conn: Any, refs: list[str], as_of: Any) -> dict[str, Any]:
    ids = [r for r in dict.fromkeys(refs) if re.fullmatch(r"[0-9a-f-]{36}", r)]
    out: dict[str, Any] = {"cited": len(ids), "post_t": 0, "pre_t": 0, "unknown": 0, "post_t_refs": []}
    if not ids:
        return out
    seen: dict[str, Any] = {}
    for table in ("signals", "analyst_outputs", "facts", "events"):
        rows = await conn.fetch(f"SELECT id::text AS id, created_at FROM {table} WHERE id = ANY($1::uuid[])", ids)
        for r in rows:
            seen.setdefault(r["id"], (table, r["created_at"]))
    for i in ids:
        if i not in seen:
            out["unknown"] += 1
        elif seen[i][1] > as_of:
            out["post_t"] += 1
            out["post_t_refs"].append({"ref": i, "table": seen[i][0], "created_at": seen[i][1].isoformat()})
        else:
            out["pre_t"] += 1
    return out


# ---------------------------------------------------------------------------
# Receipts (findings -> signals as run 1; events -> top linked signals)
# ---------------------------------------------------------------------------


async def receipts_for(conn: Any, refs: list[str]) -> list[dict[str, Any]]:
    classified = await run_arm.classify_refs(conn, refs)
    unknown = [c["ref"] for c in classified if c["kind"] == "unknown"]
    ev_sigs: dict[str, list[str]] = {}
    fact_ids: set[str] = set()
    if unknown:
        rows = await conn.fetch(
            "SELECT event_id::text AS e, signal_id::text AS s FROM signal_event_links "
            "WHERE event_id = ANY($1::uuid[]) ORDER BY event_id, relevance DESC NULLS LAST", unknown)
        for r in rows:
            if len(ev_sigs.setdefault(r["e"], [])) < EVENT_RECEIPT_SIGNALS:
                ev_sigs[r["e"]].append(r["s"])
        fact_ids = {r["id"] for r in await conn.fetch(
            "SELECT id::text AS id FROM facts WHERE id = ANY($1::uuid[])", unknown)}
    base = await run_arm.resolve_receipts(conn, [c for c in classified if c["kind"] != "unknown"])
    docs = await common.resolve_signals(conn, [s for v in ev_sigs.values() for s in v])
    by_ref = {b["ref"]: b for b in base}
    out = []
    for c in classified:
        if c["kind"] != "unknown":
            out.append(by_ref[c["ref"]])
        elif c["ref"] in ev_sigs:
            out.append({"ref": c["ref"], "kind": "event",
                        "documents": [docs[s] for s in ev_sigs[c["ref"]] if s in docs]})
        else:
            out.append({"ref": c["ref"], "kind": "fact" if c["ref"] in fact_ids else "unknown", "documents": []})
    return out


# ---------------------------------------------------------------------------
# Filling a record from a consult envelope (shared by both arms)
# ---------------------------------------------------------------------------


def tool_census(steps: list[dict[str, Any]]) -> dict[str, Any]:
    calls = [s for s in steps or [] if s.get("kind") == "tool_call"]
    by_tool: dict[str, dict[str, int]] = {}
    for s in calls:
        t = by_tool.setdefault(str(s.get("tool")), {"calls": 0, "failed": 0, "blocked": 0})
        t["calls"] += 1
        if s.get("admitted") is False:
            t["blocked"] += 1
        elif s.get("ok") is False:
            t["failed"] += 1
    return {"calls": len(calls), "failed": sum(t["failed"] for t in by_tool.values()),
            "blocked": sum(t["blocked"] for t in by_tool.values()), "by_tool": by_tool}


async def fill_from_envelope(conn: Any, rec: dict[str, Any], *, status_code: int, result: dict[str, Any],
                             text: str, as_of: Any) -> None:
    _, refs = run_arm.consult_record_from_result(rec, status_code=status_code, result=result, text=text,
                                                 hard_stop=NOMINAL_METER["hard_stop_usd"])
    meter = dict(rec["cost"])
    rec["cost"] = {"usd": 0.0, "priced": False, "price_per_m": None, "hard_stop_usd": None,
                   "hard_stop_breached": False,
                   "token_meter": {"nominal_usd_at_1_per_m": meter.get("verified_usd"),
                                   "unmetered_cuts": meter.get("unmetered_cuts")}}
    raw = rec.get("raw_answer")
    if isinstance(raw, str) and FINAL_SENTINEL_RE.search(raw):
        rec["raw_answer"] = FINAL_SENTINEL_RE.sub("", raw)
        rec["final_sentinel_stripped"] = True
    data = ((result or {}).get("consult_response") or {}).get("data") or {}
    rec["tools"] = tool_census(data.get("steps") or [])
    rec["envelope_model"] = (data.get("usage") or {}).get("model")
    rec["subprovider"] = data.get("subprovider")
    rec["rounds_used"] = data.get("rounds_used")
    rec["forced_final"] = data.get("forced_final")
    rec["consult_judge"] = {k: (data.get("verification") or {}).get(k)
                            for k in ("judge_status", "checkable_claims", "supported_claims")}
    rec["trace_steps"] = data.get("steps")
    if refs:
        rec["receipts"] = await receipts_for(conn, refs)
    rec["post_t"] = await post_t_exposure(conn, refs, as_of)


# ---------------------------------------------------------------------------
# B-open — the consult actor, core plane
# ---------------------------------------------------------------------------


def b_open_invoke(q: Any, as_of: Any) -> dict[str, Any]:
    body = run_arm.consult_invoke("B", q, as_of, hard_stop=NOMINAL_METER["hard_stop_usd"],
                                  price_per_m=NOMINAL_METER["price_per_m"])
    body["inputs"][0]["llm_component_override"] = CORE_COMPONENT
    return body


async def arm_b_open(conn: Any, reg: common.Registry, q: Any, rec: dict[str, Any], as_of: Any,
                     comp: dict[str, Any]) -> dict[str, Any]:
    import httpx

    sys.path.insert(0, str(common.REPO_ROOT / "src"))
    from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS

    variant = BAKEOFF_READ_PACKS[B_PACK]
    rec["provenance"]["prompt_sha256"] = hashlib.sha256(variant.system_prompt.encode()).hexdigest()
    head = reg.descriptor_head("analyst", B_ANALYST)
    version = (head or {}).get("version") or ""
    grants = [p.get("pack_id") for p in ((head or {}).get("body") or {}).get("action_packs") or []]
    actor_id = f"analyst::{B_ANALYST}::{version[:16]}"
    rec["model"] = {"component": CORE_COMPONENT, "model_name": comp["model_name"], "version": comp["version"],
                    "temperature": f"{ROUTE_TEMPERATURE} (consult route default = {TEMPERATURE_SOURCE})"}
    if head is None or head.get("state") != "active" or grants != [B_PACK]:
        rec.update(status="refused", refusal={"reason": f"{B_ANALYST} not active with exactly {B_PACK}: "
                                                         f"{(head or {}).get('state')} {grants}"})
        return rec
    sidecar = os.environ.get("LEGBA_BAKEOFF_DAPR", "http://127.0.0.1:3500")
    url = f"{sidecar}/v1.0/actors/{run_arm.ACTOR_TYPE}/{actor_id}/method/run"
    body = b_open_invoke(q, as_of)
    rec["request"] = {"actor_id": actor_id, "invoke_url": url, "body": body,
                      "descriptor_state": head.get("state"), "descriptor_grants": grants}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(run_arm.INVOKE_TIMEOUT_S, connect=10.0)) as client:
            r = await client.put(url, json=body)
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        result = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        await fill_from_envelope(conn, rec, status_code=r.status_code, result=result, text=r.text, as_of=as_of)
    except Exception as exc:  # noqa: BLE001 — recorded
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        rec.update(status="error", refusal={"reason": f"invoke failed: {type(exc).__name__}: {exc}"})
    return rec


# ---------------------------------------------------------------------------
# C-record — the same loop, in process, over the structured record
# ---------------------------------------------------------------------------

RECORD_STRATEGY = """Strategy — READ THE RECORD, THEN DRILL:
  - These readers are the WHOLE of what this run can read: the platform's structured record — its findings (current AND superseded, any desk, any date), what it believed on a given date, the fact store and its entities, the event register, and the curated observation series. There is no raw document search and no article text on this run: you cannot read source documents.
  - Resolve the place or topic to a target_id with list_targets when the scope names one you need.
  - Survey WIDE first: in ONE round, batch list_findings(target_id=..., include_superseded=true) for the desk (page with offset; it is not limited to recent rows), query_events for the window, and query_facts on the key subject. Then drill: inspect_event, inspect_entity, belief_as_of, series_history.
  - Mind the window. The question names an as-of instant and often a window; use the tools' as_of / believed_as_of / since / until parameters, and do not treat anything recorded after the as-of as known.
  - Finish once the record answers the question or you have exhausted the useful reads; say what the record does not establish."""

RECORD_FRAGMENT = (
    "This run reads the platform's structured record (findings, facts, entities, events, curated series) "
    "and nothing else. Cite the ids the tools returned; a claim with no record row behind it is model "
    "knowledge and must be labelled as such."
)
RECORD_RULE = "Never fabricate substrate refs — cite only UUIDs the tools returned."


def record_system_prompt() -> str:
    sys.path.insert(0, str(common.REPO_ROOT / "src"))
    from legba.data.analysts import consult_bakeoff_prompts as P

    lines = [P._LIST_TARGETS if t == "list_targets" else P.stock_tool_line(t) for t in RECORD_TOOLS]
    return P._assemble(lines, RECORD_STRATEGY, thin_example="(list_findings, query_events)",
                       batch_example="inspect_event after query_events returns the ids")


class RecordBinding:
    """The harness's enforcement layer: only RECORD_TOOLS dispatch; every other
    name is refused (``tool_blocked``) exactly as the agency gate refuses a tool
    outside a pack. Dispatch is the production router (``_dispatch_tool``).

    ``known_at`` (lane E2): the run's as-of, set by :func:`arm_c_record` on
    every record arm, is forwarded to the router as the record-time cut — each
    record reader is classified in ``legba.data.raw_reader_known_at``
    (``RECORD_KNOWN_AT_CUTS``) and an unclassified one is refused by the router
    itself. A planner-supplied ``known_at`` is discarded; the planner cannot
    set or drop the cut. ``None`` (run 1b's procedure) dispatches uncut."""

    def __init__(self, port: Any, known_at: Any = None) -> None:
        self.port = port
        self.known_at = known_at
        self.pack = SimpleNamespace(identity=SimpleNamespace(id=RECORD_PACK),
                                    prompt_fragments=[RECORD_FRAGMENT], rules=[RECORD_RULE])
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, name: str, args: dict[str, Any]) -> Any:
        from legba.data.analysts.consult_tool_routing import _dispatch_tool

        args = dict(args)
        scope = args.pop("scope_predicate", None)
        args.pop("known_at", None)
        if name not in RECORD_TOOLS:
            self.calls.append({"tool": name, "blocked": True})
            return SimpleNamespace(admitted=False, block_cause="not_in_pack",
                                   detail=f"{name!r} is not a tool of {RECORD_PACK}", tool_result=None)
        out = await _dispatch_tool(self.port, name=name, args=args, scope_predicate=scope,
                                   known_at=self.known_at)
        failed = isinstance(out, dict) and set(out) == {"error"}
        self.calls.append({"tool": name, "failed": failed})
        return SimpleNamespace(admitted=True, block_cause=None, detail=None, tool_result=SimpleNamespace(
            status="failed" if failed else "ok", output=out, error=out.get("error") if failed else None))


class ModelTap:
    """Wraps the core-plane handler: records the model every call came back with."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.models: list[str] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def _note(self, resp: Any) -> None:
        raw = getattr(resp, "raw_response", None) or {}
        m = (raw.get("model") if isinstance(raw, dict) else None) or getattr(getattr(resp, "usage", None), "model", None)
        self.models.append(str(m))

    async def chat_complete(self, *a: Any, **kw: Any) -> Any:
        resp = await self._inner.chat_complete(*a, **kw)
        self._note(resp)
        return resp


async def build_record_deps(pool: Any) -> tuple[Any, RecordBinding, ModelTap]:
    sys.path.insert(0, str(common.REPO_ROOT / "src"))
    for k, v in RUNTIME_CONSULT_ENV.items():
        os.environ[k] = v
    from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS, BakeoffVariant
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, resolve_output_budget
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    # The variant lives in THIS process's copy of the map only (no product change).
    BAKEOFF_READ_PACKS[RECORD_PACK] = BakeoffVariant(arm="C-record", pack_id=RECORD_PACK, tools=RECORD_TOOLS,
                                                     system_prompt=record_system_prompt())

    async def secrets(secret_id: str) -> bytes:
        async with common.readonly_conn() as c:
            return (await common.resolve_secret(c, secret_id)).encode()

    rc = RegistryHTTPClient(base_url=os.environ.get("LEGBA_BAKEOFF_REGISTRY", "http://127.0.0.1:8090"),
                            token=os.environ.get("LEGBA_REGISTRY_API_TOKEN"))
    llm = await build_llm_handler_from_stack_component(CORE_COMPONENT, registry_client=rc, secrets_resolve=secrets)
    tap = ModelTap(llm)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None)
    binding = RecordBinding(port)
    deps = ConsultOnDemandDeps(llm=tap, substrate=port, max_tokens=resolve_output_budget(32768),
                               agency_binding=binding, temperature=ROUTE_TEMPERATURE)
    return deps, binding, tap


async def arm_c_record(conn: Any, pool: Any, q: Any, rec: dict[str, Any], as_of: Any,
                       comp: dict[str, Any]) -> dict[str, Any]:
    from legba.data.analysts.consult_on_demand import run_method
    from legba.data.raw_reader_known_at import KNOWN_AT_CUTS, KNOWN_AT_VERSION

    deps, binding, tap = await build_record_deps(pool)
    # E2: every record read is cut at the run's as-of (known_at = T).
    binding.known_at = as_of
    rec["as_of_cut"] = {"known_at": as_of.isoformat(), "version": KNOWN_AT_VERSION,
                        "classification": {t: {"kind": KNOWN_AT_CUTS[t].kind, "how": KNOWN_AT_CUTS[t].how}
                                           for t in RECORD_TOOLS}}
    rec["provenance"]["prompt_sha256"] = hashlib.sha256(record_system_prompt().encode()).hexdigest()
    rec["model"] = {"component": CORE_COMPONENT, "model_name": comp["model_name"], "version": comp["version"],
                    "temperature": f"{ROUTE_TEMPERATURE} ({TEMPERATURE_SOURCE}, as the consult route B-open runs on)"}
    first = {
        "question": run_arm.consult_question(q, as_of, [q.desk]),
        "scope_predicate": f'target_id == "{q.desk}"',
        "max_tool_rounds": run_arm.CONSULT_ROUNDS, "mode": "chat", "request_id": str(uuid4()),
        "messages": [], "bakeoff_meter": NOMINAL_METER,
    }
    options = {"analyst_id": RECORD_ANALYST, "context_ceiling_tokens": 160000, "mint_watch_candidates": False}
    rec["request"] = {"in_process": True, "inputs": [first], "options": options, "tools": list(RECORD_TOOLS),
                      "unavailable": list(RECORD_UNAVAILABLE)}
    t0 = time.monotonic()
    try:
        res = await run_method([first], options, deps)
        cr = getattr(res, "consult_response", None)
        result = {"outcome": "success", "mode": "chat",
                  "consult_response": cr.model_dump(mode="json") if cr is not None else None}
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        await fill_from_envelope(conn, rec, status_code=200, result=result, text="", as_of=as_of)
    except Exception as exc:  # noqa: BLE001 — recorded
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        rec.update(status="error", refusal={"reason": f"run_method failed: {type(exc).__name__}: {exc}"[:800]})
    rec["call_models"] = tap.models
    rec["binding_calls"] = binding.calls
    try:
        await deps.llm._inner.on_deactivate(None)  # close the handler's client if it opened one
    except Exception:  # noqa: BLE001
        pass
    return rec


# ---------------------------------------------------------------------------
# B-open-asof — arm B's raw readers, in process, cut at the run's as-of
# ---------------------------------------------------------------------------

#: Arm B's governed read pack as the repo declares it (tools, fragments, rules).
RAW_PACK_DESCRIPTOR = common.REPO_ROOT / "descriptors" / "action_pack_substrate_read_raw.yaml"
#: The frozen pre-registered arm-B prompt (tests/data_pkg/test_consult_turn_contract.py).
RAW_PROMPT_SHA256 = "e9511378b841866ff971abccbb1540ab9907851e73c2c350afffbed2037aa745"
#: Arm B's method options, as ``descriptors/analyst_consult_bakeoff_raw.yaml`` sets them.
RAW_OPTIONS = {"analyst_id": B_ANALYST, "context_ceiling_tokens": 160000, "mint_watch_candidates": False}
#: The raw readers' backends. The actor had all three; an in-process run without
#: one is not arm B's tool surface, so a missing backend refuses the question.
ASOF_BACKENDS = ("opensearch", "qdrant", "embedder")


def raw_pack() -> dict[str, Any]:
    """``{tools, prompt_fragments, rules}`` off arm B's pack descriptor."""
    import yaml

    body = yaml.safe_load(RAW_PACK_DESCRIPTOR.read_text(encoding="utf-8"))
    return {"tools": tuple(t["name"] for t in body["tools"]),
            "prompt_fragments": list(body.get("prompt_fragments") or []),
            "rules": list(body.get("rules") or [])}


class KnownAtBinding:
    """Arm B's pack, enforced in the harness, with the record-time cut.

    Only the raw pack's tools dispatch; every other name is refused
    (``not_in_pack``) as the agency gate refuses it. Dispatch is the production
    router with ``known_at`` set, so a cut reader gets the cut and a tool with
    no classified cut is refused by the router itself. The planner cannot set
    or drop ``known_at``: a ``known_at`` in its args is discarded.
    """

    def __init__(self, port: Any, known_at: Any, pack: dict[str, Any] | None = None) -> None:
        pack = pack or raw_pack()
        self.port = port
        self.known_at = known_at
        self.tools = tuple(pack["tools"])
        self.pack = SimpleNamespace(identity=SimpleNamespace(id=B_PACK),
                                    prompt_fragments=pack["prompt_fragments"], rules=pack["rules"])
        self.calls: list[dict[str, Any]] = []

    async def run_tool(self, name: str, args: dict[str, Any]) -> Any:
        from legba.data.analysts.consult_tool_routing import _dispatch_tool

        args = dict(args)
        scope = args.pop("scope_predicate", None)
        args.pop("known_at", None)
        if name not in self.tools:
            self.calls.append({"tool": name, "blocked": True})
            return SimpleNamespace(admitted=False, block_cause="not_in_pack",
                                   detail=f"{name!r} is not a tool of {B_PACK}", tool_result=None)
        out = await _dispatch_tool(self.port, name=name, args=args, scope_predicate=scope, known_at=self.known_at)
        failed = isinstance(out, dict) and set(out) == {"error"}
        cut = (out.get("known_at_cut") or {}) if isinstance(out, dict) else {}
        self.calls.append({"tool": name, "failed": failed,
                           "dropped_after_known_at": cut.get("dropped_after_known_at")})
        return SimpleNamespace(admitted=True, block_cause=None, detail=None, tool_result=SimpleNamespace(
            status="failed" if failed else "ok", output=out, error=out.get("error") if failed else None))


async def build_asof_backends(rc: Any, secrets: Any) -> dict[str, Any]:
    """The three raw-reader backends the actor's port carries, built guarded."""
    out: dict[str, Any] = {"opensearch": None, "qdrant": None, "embedder": None, "errors": {}}
    try:
        from legba.data.opensearch import OpenSearchStore

        out["opensearch"] = OpenSearchStore.from_env()
    except Exception as exc:  # noqa: BLE001 — recorded
        out["errors"]["opensearch"] = f"{type(exc).__name__}: {exc}"[:300]
    try:
        from legba.runtime.qdrant_factory import build_qdrant_client_from_stack_component

        out["qdrant"] = await build_qdrant_client_from_stack_component(
            os.environ.get("LEGBA_DATA_DEFAULT_VECTOR_STORE", "vector.qdrant.cluster_main"), registry_client=rc)
    except Exception as exc:  # noqa: BLE001 — recorded
        out["errors"]["qdrant"] = f"{type(exc).__name__}: {exc}"[:300]
    try:
        from legba.runtime.embedding_factory import build_embedding_service_from_stack_component

        out["embedder"] = await build_embedding_service_from_stack_component(
            os.environ.get("LEGBA_DATA_DEFAULT_EMBEDDING", "embed.primary.openai_compat"),
            registry_client=rc, secrets_resolve=secrets)
    except Exception as exc:  # noqa: BLE001 — recorded
        out["errors"]["embedder"] = f"{type(exc).__name__}: {exc}"[:300]
    return out


def asof_deps(llm: Any, port: Any, known_at: Any) -> tuple[Any, KnownAtBinding, ModelTap]:
    """The consult deps for B-open-asof: the core-plane handler, the cut binding,
    the house temperature. The frozen arm-B prompt is chosen by the loop itself
    (``variant_for_binding`` keys on the binding's pack id, ``substrate_read_raw``)."""
    for k, v in RUNTIME_CONSULT_ENV.items():
        os.environ[k] = v
    from legba.data.analysts.consult_bakeoff import BAKEOFF_READ_PACKS
    from legba.data.analysts.consult_on_demand import ConsultOnDemandDeps, resolve_output_budget

    prompt = BAKEOFF_READ_PACKS[B_PACK].system_prompt
    if hashlib.sha256(prompt.encode()).hexdigest() != RAW_PROMPT_SHA256:
        raise SystemExit("arm B's frozen prompt hash changed — refusing (pre-registered piece)")
    tap = ModelTap(llm)
    binding = KnownAtBinding(port, known_at)
    deps = ConsultOnDemandDeps(llm=tap, substrate=port, max_tokens=resolve_output_budget(32768),
                               agency_binding=binding, temperature=ROUTE_TEMPERATURE)
    return deps, binding, tap


async def arm_b_open_asof(conn: Any, pool: Any, q: Any, rec: dict[str, Any], as_of: Any,
                          comp: dict[str, Any]) -> dict[str, Any]:
    from legba.data.analysts.consult_on_demand import run_method
    from legba.data.raw_reader_known_at import KNOWN_AT_CUTS, KNOWN_AT_VERSION
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    async def secrets(secret_id: str) -> bytes:
        async with common.readonly_conn() as c:
            return (await common.resolve_secret(c, secret_id)).encode()

    rc = RegistryHTTPClient(base_url=os.environ.get("LEGBA_BAKEOFF_REGISTRY", "http://127.0.0.1:8090"),
                            token=os.environ.get("LEGBA_REGISTRY_API_TOKEN"))
    pack = raw_pack()
    rec["provenance"]["prompt_sha256"] = RAW_PROMPT_SHA256
    rec["model"] = {"component": CORE_COMPONENT, "model_name": comp["model_name"], "version": comp["version"],
                    "temperature": f"{ROUTE_TEMPERATURE} ({TEMPERATURE_SOURCE})"}
    rec["as_of_cut"] = {"known_at": as_of.isoformat(), "version": KNOWN_AT_VERSION,
                        "classification": {t: {"kind": KNOWN_AT_CUTS[t].kind, "how": KNOWN_AT_CUTS[t].how}
                                           for t in pack["tools"] if t in KNOWN_AT_CUTS}}
    backends = await build_asof_backends(rc, secrets)
    rec["backends"] = {k: backends[k] is not None for k in ASOF_BACKENDS} | {"errors": backends["errors"]}
    missing = [k for k in ASOF_BACKENDS if backends[k] is None]
    if missing:
        rec.update(status="refused", refusal={"reason": f"raw-reader backend(s) not reachable from this process: "
                                                         f"{missing} — not arm B's tool surface"})
        return rec
    llm = await build_llm_handler_from_stack_component(CORE_COMPONENT, registry_client=rc, secrets_resolve=secrets)
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=backends["qdrant"],
                                            embedder=backends["embedder"], opensearch_store=backends["opensearch"])
    deps, binding, tap = asof_deps(llm, port, as_of)
    first = {k: v for k, v in b_open_invoke(q, as_of)["inputs"][0].items() if k != "llm_component_override"}
    rec["request"] = {"in_process": True, "inputs": [first], "options": RAW_OPTIONS, "tools": list(pack["tools"]),
                      "known_at": as_of.isoformat()}
    t0 = time.monotonic()
    try:
        res = await run_method([first], dict(RAW_OPTIONS), deps)
        cr = getattr(res, "consult_response", None)
        result = {"outcome": "success", "mode": "chat",
                  "consult_response": cr.model_dump(mode="json") if cr is not None else None}
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        await fill_from_envelope(conn, rec, status_code=200, result=result, text="", as_of=as_of)
    except Exception as exc:  # noqa: BLE001 — recorded
        rec["elapsed_s"] = round(time.monotonic() - t0, 2)
        rec.update(status="error", refusal={"reason": f"run_method failed: {type(exc).__name__}: {exc}"[:800]})
    rec["call_models"] = tap.models
    rec["binding_calls"] = binding.calls
    rec["as_of_cut"]["dropped_after_known_at"] = sum(int(c.get("dropped_after_known_at") or 0)
                                                     for c in binding.calls)
    try:
        await llm.on_deactivate(None)
    except Exception:  # noqa: BLE001
        pass
    return rec


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


#: ``substrate_read_raw``'s governor: ``max_invocations_per_hour: 600`` over the
#: trailing hour (descriptors/action_pack_substrate_read_raw.yaml). A question
#: started near the cap has its tools refused ``over_rate`` mid-loop — that is
#: not arm B's procedure, so B-open waits for headroom before each question.
RAW_PACK_HOURLY_CAP = 600


async def wait_for_governor(conn: Any, headroom: int, *, poll_s: float = 30.0) -> int:
    while True:
        n = int(await conn.fetchval(
            "SELECT count(*) FROM action_pack_invocations WHERE pack_id = $1 "
            "AND occurred_at > now() - interval '1 hour'", B_PACK))
        if n + headroom <= RAW_PACK_HOURLY_CAP:
            return n
        print(f"governor: {n} {B_PACK} invocations in the trailing hour; waiting for {headroom} headroom", flush=True)
        await asyncio.sleep(poll_s)


def refuse_mixed_temperature(run: dict[str, Any]) -> None:
    """A run directory holds ONE sampling regime. Run 1b's directory was opened
    before the temperature had a declared place (its arms ran at 0.2, D13), so
    new answers never land beside its records: open a new run directory."""
    t = run.get("temperature") or {}
    if t.get("value") != ROUTE_TEMPERATURE:
        raise SystemExit(
            f"run {run.get('run_id')} was opened at temperature {t.get('value', 'unrecorded (run 1b: 0.2)')}; "
            f"this harness runs at {ROUTE_TEMPERATURE} ({TEMPERATURE_SOURCE}) — open a new run directory")


async def run_main(a: argparse.Namespace) -> int:
    import asyncpg

    common.load_env()
    dz = design_mod.load_design(a.design)
    out = Path(a.out)
    run = common.load_run(out)
    as_of = common.parse_as_of(run["as_of"])
    reg = common.Registry.from_env()
    comp = reg.stack_component(CORE_COMPONENT)
    if comp.get("model_name") != CORE_ALIAS:
        raise SystemExit(f"{CORE_COMPONENT} serves {comp.get('model_name')!r}, not {CORE_ALIAS!r} — refusing")
    if not str(comp.get("api_endpoint") or "").startswith("https://api.innoai.net"):
        raise SystemExit(f"{CORE_COMPONENT} points at {comp.get('api_endpoint')!r} — not the self-hosted gateway")
    if a.practice:
        questions = run_arm.load_practice(Path(a.practice))
        questions = [q for q in questions if not a.question or q.qid in a.question]
    else:
        questions = list(dz.questions) if a.all else [dz.question(x) for x in a.question or []]
    gold = json.loads((out / "gold.json").read_text(encoding="utf-8"))
    if a.skip_void:
        import grade_sheet

        void, _, _ = grade_sheet.void_rules(list(dz.questions), gold, [])
        questions = [q for q in questions if q.qid not in void]
    refuse_mixed_temperature(run)
    pool = None
    if a.arm in (*RECORD_ARMS, ASOF_ARM):
        pool = await asyncpg.create_pool(common._pg_dsn(), min_size=1, max_size=4, server_settings={  # noqa: SLF001
            "default_transaction_read_only": "on", "statement_timeout": "120000",
            "application_name": "legba_bakeoff_run1b"})
    try:
        async with common.readonly_conn() as conn:
            key = await common.resolve_secret(conn, comp["api_key_ref"])
            for q in questions:
                path = common.record_path(out, q.qid, a.arm)
                if path.is_file() and json.loads(path.read_text())["status"] in ("answered", "partial") and not a.redo:
                    print(f"{q.qid} {a.arm}: done — skipped")
                    continue
                if a.arm in ("B-open", "B-open-2") and a.governor_headroom:
                    await wait_for_governor(conn, a.governor_headroom)
                rec = common.new_record(run=run, question=q, arm=a.arm, as_of=as_of)
                rec["desk_display_name"] = run_arm.desk_display_name(reg, q.desk)
                rec["served_model_probe"] = {"before": await served_model_probe(comp, key)}
                reason = await run_arm.precheck(conn, reg, q)
                if reason:
                    rec.update(status="refused", refusal={"reason": reason})
                elif a.arm in ("B-open", "B-open-2"):
                    rec = await arm_b_open(conn, reg, q, rec, as_of, comp)
                elif a.arm == ASOF_ARM:
                    rec = await arm_b_open_asof(conn, pool, q, rec, as_of, comp)
                else:
                    rec = await arm_c_record(conn, pool, q, rec, as_of, comp)
                rec["harness_version"] = ARM_OPEN_VERSION
                rec["served_model_probe"]["after"] = await served_model_probe(comp, key)
                rec["cost"]["usd"] = 0.0
                common.write_record(out, rec)
                t = rec.get("tools") or {}
                print(f"{q.qid} {a.arm:<8} {rec['status']:<8} {rec.get('elapsed_s')}s tools={t.get('calls')} "
                      f"fail={t.get('failed')} blk={t.get('blocked')} postT={((rec.get('post_t') or {}).get('post_t'))}"
                      f"/{((rec.get('post_t') or {}).get('cited'))} {(rec.get('refusal') or {}).get('reason', '')[:160]}",
                      flush=True)
    finally:
        if pool is not None:
            await pool.close()
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="arms " + " / ".join(ARMS) + " on the core plane ($0)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    o = sub.add_parser("open")
    o.add_argument("--run1", required=True)
    o.add_argument("--out", required=True)
    o.add_argument("--design", required=True)
    o.add_argument("--purpose", default=DEFAULT_PURPOSE, help="recorded in run.json")
    r = sub.add_parser("run")
    r.add_argument("--out", required=True)
    r.add_argument("--design", required=True)
    r.add_argument("--arm", choices=ARMS, required=True)
    r.add_argument("--question", nargs="*")
    r.add_argument("--all", action="store_true")
    r.add_argument("--skip-void", action="store_true", help="leave out the questions run 1's gold voided")
    r.add_argument("--redo", action="store_true")
    r.add_argument("--governor-headroom", type=int, default=150,
                   help="B-open: start a question only with this many raw-pack invocations to spare (0 = off)")
    r.add_argument("--practice", help="a practice-question file (P01..): smoke runs only")
    a = ap.parse_args(argv)
    if a.cmd == "open":
        dz = design_mod.load_design(a.design)
        run = open_run1b(Path(a.run1), Path(a.out), dz, a.purpose)
        print(f"run {run['run_id']} as_of {run['as_of']} gold sha {run['gold_sha256'][:12]} "
              f"temperature {(run.get('temperature') or {}).get('value')}")
        return 0
    return asyncio.run(run_main(a))


if __name__ == "__main__":
    sys.exit(main())

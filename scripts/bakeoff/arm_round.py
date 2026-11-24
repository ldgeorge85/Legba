#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""arm_round.py — run any round spec as an arm (plan P4 §6, lane G-5).

One command runs a round spec (``src/legba/data/rounds/specs/*.yaml`` or any
spec file) on a question file at a cut time, N samples per question, through
the HARNESS HOST (``legba.data.rounds.harness.run_file``) with real seats:

* model and judge seats: :class:`legba.data.rounds.model_seats.ModelSeats` on
  handlers built from the live registry rows and the vault (read-only), the
  house temperature, no ``max_tokens``, thinking as each seat declares it;
* tools: :class:`legba.data.rounds.tool_binding.RoundTools` over the consult
  tool router with the ``known_at`` cut at the run's as-of time, the run's own
  rows excluded;
* substrate code seats: :class:`legba.data.rounds.substrate_seats.SubstrateSeats`
  over a READ-ONLY pool (``default_transaction_read_only``, statement timeout).

SAFETY. Before the first call a ROUTE ASSERTION runs on every plane the spec
names: the core component must pass ``statement_seats.assert_core_route`` (the
S-3/S-4 check: the seat component, not Anthropic, the self-hosted subprovider,
no component price, an empty price table); every judge plane must be an
unpriced gateway route (not Anthropic, the same subprovider, no price, an
empty price table, the gateway endpoint). Any failure stops the command before
a call is made. A response that reports a cost stops the run. A hard cap on
wire calls (``--call-cap``, a ledger file shared by every command run on the
same ``--root``) refuses the call that would exceed it. At most 4 calls are in
flight at once (``--max-parallel``, capped at 4 and at the spec's
``max_parallel``): the owner's instruction, "don't send too many concurrent".

THINKING ALIAS. The live core component may not declare its thinking alias
yet; the harness overlays ``thinking_model_name`` onto the row it READS (never
written back) from ``LEGBA_LLM_THINKING_MODEL_NAME`` — the bringup path's
variable. Unset: thinking seats fail loudly (the handler refuses).

OUTPUT (all under ``--root``, which must be under ``planning/`` — gitignored):
``<label>/manifest.json`` (spec + card sha256s, git sha, cut, temperature,
routes), ``<label>/<qid>/s<k>/`` (the harness's receipts, phases, run and
record), ``prompts/<sha>.json`` (every rendered seat prompt, for the thinking
probe), ``calls.json`` (the call ledger). Re-running the same command resumes:
finished steps are never re-run.

USAGE
    LEGBA_LLM_THINKING_MODEL_NAME=<alias> python3 scripts/bakeoff/arm_round.py run \\
        --spec src/legba/data/rounds/specs/stance_linear.yaml --label stance_linear \\
        --questions scripts/bakeoff/practice_questions.json --as-of 2026-11-05T07:00:00Z \\
        --samples 1 --root planning/reviews/rounds_smoke_2026-11-14
    python3 scripts/bakeoff/arm_round.py report --root planning/reviews/rounds_smoke_2026-11-14 --label stance_linear
    LEGBA_LLM_THINKING_MODEL_NAME=<alias> python3 scripts/bakeoff/arm_round.py probe-thinking \\
        --root planning/reviews/rounds_smoke_2026-11-14 --pairs 24 --tool-probes 3
    python3 scripts/bakeoff/arm_round.py calibrate --root <root> --label <l> --row-type verdict/2 \\
        --card cards/judges/support.md --plane llm.verify.innoai_gemma.openai_compat

JUDGE CALIBRATION (lane G-6). ``calibrate`` runs each judge card on each judge
plane over the planted positions (``legba.data.rounds.judge_planted``) and
writes catch / false-reject rates to ``<root>/judge_calibration.json``; ``run``
reads that book and the interpreter lets only judges at or above the floors
gate (the others are recorded as advisory). EGRESS: every command installs the
harness egress guard (``legba.data.rounds.egress``) before its first
connection: loopback, compose names that resolve to private addresses, and the
gateway host only.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import statistics
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))

import common  # noqa: E402

sys.path.insert(0, str(common.REPO_ROOT / "src"))

ARM_ROUND_VERSION = "g5.1"
CORE_COMPONENT = "llm.primary.openai_compat"
THINKING_ENV = "LEGBA_LLM_THINKING_MODEL_NAME"
GATEWAY_HOST = "api.innoai.net"
#: Tools bound in this harness: the classified read tools whose backends this
#: process builds without a model route (``vector_search`` needs the
#: embedding route and is not bound here; a seat that calls it is refused).
UNBOUND_TOOLS = frozenset({"vector_search"})
MAX_IN_FLIGHT = 4


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _under_planning(root: Path) -> Path:
    root = root.resolve()
    planning = (common.REPO_ROOT / "planning").resolve()
    if planning not in root.parents and root != planning:
        raise SystemExit(f"--root must be under {planning} (gitignored): got {root}")
    return root


def load_questions(path: Path) -> list[dict[str, Any]]:
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    qs = raw.get("questions") if isinstance(raw, dict) else raw
    out = []
    for q in qs or ():
        qid = q.get("qid") or q.get("id")
        if not qid or not q.get("desk") or not q.get("text"):
            raise SystemExit(f"question entries need qid, desk and text: {q!r}")
        out.append({"qid": str(qid), "desk": str(q["desk"]), "text": str(q["text"])})
    return out


class CallLedger:
    """A hard cap on wire calls, persisted under the root (shared by every
    command on the same root, across resumes)."""

    def __init__(self, path: Path, cap: int) -> None:
        self.path, self.cap = path, int(cap)
        self._lock = threading.Lock()

    def used(self) -> int:
        try:
            return int(json.loads(self.path.read_text())["used"])
        except (OSError, ValueError, KeyError):
            return 0

    def take(self) -> bool:
        with self._lock:
            n = self.used()
            if n >= self.cap:
                return False
            common.write_json(self.path, {"used": n + 1, "cap": self.cap,
                                          "updated_at": common.utcnow().isoformat()})
            return True


class OverlayRegistry:
    """A READ-ONLY view of the registry client that overlays the core
    component's ``thinking_model_name`` onto the row it reads (never written)."""

    def __init__(self, inner: Any, component_id: str, alias: str | None) -> None:
        self._inner, self._component, self._alias = inner, component_id, alias
        self._api_prefix = inner._api_prefix  # noqa: SLF001

    def _headers(self) -> dict[str, str]:
        return self._inner._headers()  # noqa: SLF001

    async def _ensure_client(self) -> Any:
        client = await self._inner._ensure_client()  # noqa: SLF001
        overlay = self

        class _Client:
            async def get(self, path: str, **kw: Any) -> Any:
                resp = await client.get(path, **kw)
                if not path.endswith(f"/stack/{overlay._component}") or not overlay._alias \
                        or resp.status_code >= 400:
                    return resp
                row = resp.json()
                cfg = ((row.get("body") or {}).get("config") or {})
                cfg["thinking_model_name"] = {"factory_kind": "text", "raw": overlay._alias}

                class _Resp:
                    status_code = resp.status_code
                    text = ""

                    @staticmethod
                    def json() -> Any:
                        return row
                return _Resp()

        return _Client()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


def compose_service_names() -> frozenset[str]:
    """The compose service names (the containers' DNS names inside the
    compose network) — the only names the egress guard lets resolve, and only
    to private addresses."""
    import yaml

    p = common.REPO_ROOT / "docker-compose.yml"
    try:
        return frozenset(str(k).lower() for k in (yaml.safe_load(p.read_text()) or {}).get("services") or {})
    except (OSError, yaml.YAMLError):
        return frozenset()


def guard_egress() -> Any:
    """Install the harness egress guard (loopback, private compose names, the
    gateway) before any connection is opened."""
    from legba.data.rounds.egress import EgressPolicy, install

    return install(EgressPolicy(gateway_hosts=frozenset({GATEWAY_HOST}),
                                container_names=compose_service_names()))


def egress_refused() -> list[dict[str, Any]]:
    """What the egress guard refused in this process (printed at the end)."""
    from legba.data.rounds.egress import installed

    pol = installed()
    return list(pol.refused) if pol is not None else []


# ---------------------------------------------------------------------------
# Routes: build the handlers and assert them BEFORE any call
# ---------------------------------------------------------------------------


def route_facts(component_id: str, handler: Any) -> dict[str, Any]:
    cfg = getattr(handler, "_cfg", None)
    raw = lambda k: getattr(getattr(cfg, k, None), "raw", None)  # noqa: E731
    endpoint = str(raw("api_endpoint") or "")
    pricer = getattr(handler, "_component_price", None)
    price = pricer() if callable(pricer) else None
    return {
        "component_id": component_id,
        "subprovider": getattr(handler, "subprovider", None),
        "model_name": raw("model_name"),
        "thinking_model_name": raw("thinking_model_name"),
        "endpoint_host": endpoint.split("://", 1)[-1].split("/", 1)[0],
        "component_price": repr(price) if price else None,
        "config_price_fields": {k: raw(k) for k in ("price_input_per_m", "price_output_per_m")
                                if raw(k) not in (None, 0, 0.0)},
        "price_table_entries": len(getattr(handler, "PRICE_TABLE", {}) or {}),
        "sends_max_tokens": False,
    }


def assert_routes(facts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """The S-3/S-4 core assertion, and the gateway assertion for every other
    plane. Raises SystemExit naming the first failure."""
    from legba.data.statement_seats import CoreRouteError, RouteFacts, assert_core_route

    out: dict[str, Any] = {}
    for comp, f in facts.items():
        if comp == CORE_COMPONENT:
            try:
                out[comp] = assert_core_route(RouteFacts(
                    component_id=comp, subprovider=str(f["subprovider"]),
                    model_name=str(f["model_name"]),
                    component_price=f["component_price"] or (f["config_price_fields"] or None),
                    price_table_entries=int(f["price_table_entries"])))
            except CoreRouteError as exc:
                raise SystemExit(f"ROUTE ASSERTION FAILED ({comp}): {exc}") from None
            continue
        # a DECISION plane (``.decide.`` id: POST /decide on the same gateway)
        # is held to the same checks with its own client kind
        want = "decide" if ".decide." in comp else "vllm"
        checks = {
            "component_not_anthropic": "anthropic" not in comp.lower(),
            "subprovider_is_gateway_compat": f["subprovider"] == want,
            "endpoint_is_gateway": f["endpoint_host"] == GATEWAY_HOST,
            "component_unpriced": f["component_price"] is None and not f["config_price_fields"],
            "price_table_empty": f["price_table_entries"] == 0,
        }
        bad = [k for k, ok in checks.items() if not ok]
        if bad:
            raise SystemExit(f"ROUTE ASSERTION FAILED ({comp}): {bad}")
        out[comp] = checks
    return out


async def _secrets(secret_id: str) -> bytes:
    async with common.readonly_conn() as c:
        return (await common.resolve_secret(c, secret_id)).encode()


async def build_planes(components: list[str]) -> tuple[dict[str, Any], dict[str, Any]]:
    from legba.runtime.analyst_deps_builder import build_llm_handler_from_stack_component
    from legba.runtime.registry_client import RegistryHTTPClient

    rc = RegistryHTTPClient(base_url=os.environ.get("LEGBA_BAKEOFF_REGISTRY", "http://127.0.0.1:8090"),
                            token=os.environ.get("LEGBA_REGISTRY_API_TOKEN"))
    alias = os.environ.get(THINKING_ENV, "").strip() or None
    reg = OverlayRegistry(rc, CORE_COMPONENT, alias)
    planes, facts = {}, {}
    for comp in components:
        h = await build_llm_handler_from_stack_component(comp, registry_client=reg, secrets_resolve=_secrets)
        planes[comp] = h
        facts[comp] = route_facts(comp, h)
    return planes, facts


async def build_port() -> tuple[Any, Any, Any]:
    """A read-only pool and the substrate port (Postgres + OpenSearch)."""
    import asyncpg

    from legba.data.opensearch import OpenSearchStore
    from legba.runtime.substrate_query_port import PostgresQdrantSubstrateQueryPort

    pool = await asyncpg.create_pool(common._pg_dsn(), min_size=1, max_size=4, server_settings={  # noqa: SLF001
        "default_transaction_read_only": "on", "statement_timeout": "60000",
        "application_name": "legba_round_harness"})
    async with pool.acquire() as c:
        if await c.fetchval("SHOW default_transaction_read_only") != "on":
            raise SystemExit("could not open a read-only pool; refusing to run")
    # The compose service name does not resolve on the host: the published
    # port is loopback-only. Never let a bare service name reach DNS.
    os.environ.setdefault("LEGBA_DATA_OPENSEARCH_HOST", "127.0.0.1")
    try:
        osx = OpenSearchStore.from_env()
    except Exception:  # noqa: BLE001 — search_corpus/read_document then fail per call
        osx = None
    port = PostgresQdrantSubstrateQueryPort(pg_pool=pool, qdrant_client=None, opensearch_store=osx)
    return pool, port, osx


def pack_tools() -> dict[str, tuple[str, ...]]:
    import yaml

    out = {}
    for p in sorted((common.REPO_ROOT / "descriptors").glob("action_pack_substrate_read*.yaml")):
        body = yaml.safe_load(p.read_text(encoding="utf-8"))
        out[body["identity"]["id"]] = tuple(t["name"] for t in body.get("tools") or ())
    return out


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def spec_manifest(spec: Any) -> dict[str, Any]:
    root = Path(spec.source).parent
    cards = {}
    for s in spec.seats.values():
        if isinstance(s.card, str):
            p = root / s.card
            cards[s.card] = common.sha256_file(p) if p.is_file() else None
    return {"id": spec.id, "version": spec.version, "sha256": spec.sha256,
            "source": str(Path(spec.source).resolve().relative_to(common.REPO_ROOT)),
            "cards": dict(sorted(cards.items())), "budgets": spec.budgets.model_dump(),
            "planes": sorted({s.plane for s in spec.seats.values() if s.plane})}


def _keep_prompt_fn(root: Path):
    d = root / "prompts"
    d.mkdir(parents=True, exist_ok=True)

    def keep(sha: str, seat: str, system: str, user: str) -> None:
        p = d / f"{sha}.json"
        if not p.exists():
            common.write_json(p, {"seat": seat, "system": system, "user": user})
    return keep


CALIBRATION_FILE = "judge_calibration.json"


def judge_standings(spec: Any, book: Any) -> dict[str, Any]:
    """The calibration rule for every judge seat of ``spec`` (what the
    interpreter will apply at its panels)."""
    from legba.data.rounds.calibration import card_sha256
    from legba.data.rounds.judge_render import render_version

    root = Path(spec.source).parent
    out = {}
    for sid, seat in spec.seats.items():
        if seat.kind != "judge" or not isinstance(seat.card, str):
            continue
        p = root / seat.card
        sha = card_sha256(p.read_text(encoding="utf-8")) if p.is_file() else None
        out[sid] = book.standing(sha, seat.plane, seat.output, render_version(seat.output)).as_dict()
    return out


def _run_main(a: argparse.Namespace) -> int:
    from legba.data.analysts.consult_tool_routing import _dispatch_tool
    from legba.data.raw_reader_known_at import KNOWN_AT_CUTS, KNOWN_AT_VERSION
    from legba.data.rounds.harness import run_file
    from legba.data.rounds.model_seats import LoopThread, ModelSeats
    from legba.data.rounds.spec import load_spec
    from legba.data.rounds.store import FileStore
    from legba.data.rounds.substrate_seats import SubstrateSeats
    from legba.data.rounds.tool_binding import RoundTools, exclusions_from_receipts
    from legba.shared.sampling import HOUSE_TEMPERATURE

    from legba.data.rounds.calibration import CalibrationBook

    common.load_env()
    root = _under_planning(Path(a.root))
    guard_egress()
    spec, refusals = load_spec(a.spec)
    if spec is None:
        raise SystemExit(f"spec refused: {refusals}")
    book = CalibrationBook.load(Path(a.calibration) if a.calibration else root / CALIBRATION_FILE)
    print("JUDGE STANDING:", json.dumps(judge_standings(spec, book), sort_keys=True), flush=True)
    as_of = common.parse_as_of(a.as_of)
    questions = load_questions(Path(a.questions))
    if a.question:
        questions = [q for q in questions if q["qid"] in set(a.question)]
    cap = max(1, min(MAX_IN_FLIGHT, int(a.max_parallel), spec.budgets.max_parallel))
    ledger = CallLedger(root / "calls.json", a.call_cap)
    loop = LoopThread()
    planes, facts = loop.run(build_planes(sorted({s.plane for s in spec.seats.values() if s.plane})))
    routes = assert_routes(facts)
    print("ROUTES ASSERTED:", json.dumps(routes, sort_keys=True, default=str), flush=True)
    if any(s.thinking == "on" for s in spec.seats.values()) and not facts[CORE_COMPONENT]["thinking_model_name"]:
        raise SystemExit(f"the spec has thinking seats and {THINKING_ENV} is unset — refusing")
    pool, port, _osx = loop.run(build_port())
    classified = {t: c.kind for t, c in KNOWN_AT_CUTS.items() if t not in UNBOUND_TOOLS}

    async def dispatch(name: str, args: Any, known_at: Any) -> Any:
        return await _dispatch_tool(port, name=name, args=args, scope_predicate=None, known_at=known_at)

    async def fetch(sql: str, *params: Any) -> list[Any]:
        async with pool.acquire() as c:
            async with c.transaction(readonly=True):
                return await c.fetch(sql, *params)

    label_dir = root / a.label
    manifest = {
        "schema": "legba.rounds.arm/1", "arm_round_version": ARM_ROUND_VERSION, "label": a.label,
        "spec": spec_manifest(spec), "git_sha": common.git_sha(), "as_of": as_of.isoformat(),
        "questions": {"file": str(a.questions), "sha256": common.sha256_file(Path(a.questions)),
                      "qids": [q["qid"] for q in questions]},
        "samples": a.samples, "max_in_flight": cap,
        "temperature": {"value": HOUSE_TEMPERATURE, "source": "legba.shared.sampling.HOUSE_TEMPERATURE"},
        "thinking_alias_source": f"env {THINKING_ENV} overlaid on the read row (not registered)",
        "known_at": {"version": KNOWN_AT_VERSION, "bound_tools": sorted(classified)},
        "routes": facts, "route_assertion": routes, "call_cap": a.call_cap,
        "max_tool_rounds": a.max_tool_rounds,
        "calibration": {"book": book.source, "records": len(book.records),
                        "judges": judge_standings(spec, book)},
    }
    label_dir.mkdir(parents=True, exist_ok=True)
    old = label_dir / "manifest.json"
    if old.is_file():
        prev = json.loads(old.read_text())
        if prev["spec"]["sha256"] != spec.sha256 or prev["as_of"] != manifest["as_of"]:
            raise SystemExit("this label was run with a different spec or cut — use a new --label")
    common.write_json(old, manifest)
    limiter = asyncio.Semaphore(cap)
    keep = _keep_prompt_fn(root)
    stop = threading.Event()

    def one(q: dict[str, Any], k: int) -> dict[str, Any]:
        out = label_dir / q["qid"] / f"s{k}"
        if stop.is_set():
            return {"qid": q["qid"], "sample": k, "skipped": "stopped"}
        tools = RoundTools(dispatch=dispatch, known_at=as_of, packs=pack_tools(), classified=classified,
                           exclusions=lambda: exclusions_from_receipts(FileStore(out).receipts()))

        def take() -> bool:
            return not stop.is_set() and ledger.take()

        models = ModelSeats(planes=planes, run=loop.run, tools=tools, max_tool_rounds=a.max_tool_rounds,
                            limiter=limiter, take_call=take, keep_prompts=keep)
        subs = SubstrateSeats(fetch=fetch, run=loop.run, as_of=as_of, subject=q["desk"], dispatch=dispatch)

        def provider(call: Any) -> Any:
            reply = subs(call) if call.seat.kind == "code" else models(call)
            for c in (reply.meta or {}).get("calls", ()):
                if c.get("cost_estimate_usd"):
                    stop.set()
                    raise SystemExit(f"a call reported a cost ({c}) — stopping")
            return reply

        inputs = {"subject": q["desk"], "question": q["text"], "as_of": as_of.isoformat()}
        t0 = time.monotonic()
        try:
            rec = run_file(a.spec, out, inputs, seats=provider, calibration=book)
        except Exception as exc:  # noqa: BLE001 - recorded, the other runs go on
            return {"qid": q["qid"], "sample": k, "error": f"{type(exc).__name__}: {exc}"[:400]}
        return {"qid": q["qid"], "sample": k, "state": rec["run"]["state"],
                "stop_reason": rec["run"]["stop_reason"], "wall_s": round(time.monotonic() - t0, 1),
                "totals": rec["totals"]}

    jobs = [(q, k) for q in questions for k in range(1, a.samples + 1)]
    try:
        with ThreadPoolExecutor(max_workers=cap) as ex:
            for res in ex.map(lambda j: one(*j), jobs):
                print(json.dumps(res, sort_keys=True), flush=True)
    finally:
        loop.run(pool.close())
        if _osx is not None and hasattr(_osx, "close"):
            try:
                loop.run(_osx.close())
            except Exception:  # noqa: BLE001
                pass
        for h in planes.values():
            try:
                loop.run(h.on_deactivate(None))
            except Exception:  # noqa: BLE001
                pass
        loop.close()
    print(f"calls used (ledger): {ledger.used()} / {ledger.cap}")
    print("EGRESS REFUSED:", json.dumps(egress_refused()), flush=True)
    return 0


# ---------------------------------------------------------------------------
# report — per-seat parse / gate / absence, totals vs the worst case
# ---------------------------------------------------------------------------


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    i = min(len(s) - 1, max(0, int(round(q * (len(s) - 1)))))
    return round(s[i], 2)


def verify_blind(rec: dict[str, Any]) -> dict[str, Any]:
    """Every blind phase closed with sealed members, and no member was handed
    a sibling's output (the receipts' handed refs)."""
    steps = rec["steps"]
    blind = [p for p in rec["phases"] if p["shape"] in ("blind", "rebuttal") or "~p" in p["path"]]
    problems = []
    for p in blind:
        members = [s for s in steps if s["path"] == p["path"] or s["path"].startswith(p["path"] + "/")]
        keys = {s["key"] for s in members}
        for s in members:
            if s["status"] == "sealed" and not s.get("sha256"):
                problems.append(f"{s['key']}: sealed without a seal")
            for h in s.get("handed") or ():
                if h.get("ref") in keys and p["shape"] == "blind":
                    problems.append(f"{s['key']} was handed sibling {h.get('ref')}")
        if p.get("status") != "closed":
            problems.append(f"{p['path']}: not closed")
    return {"blind_phases": len(blind), "problems": problems, "verified": not problems}


def report(root: Path, label: str) -> dict[str, Any]:
    d = root / label
    per_seat: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "attempts": 0, "sealed": 0, "released": 0, "failed": Counter(), "absent": Counter(),
        "gate_fail": Counter(), "gated": 0, "gate_pass": 0, "durations": [], "reasoning": [],
        "dropped_keys": 0, "tool_calls": 0, "repairs": 0, "repaired": 0})
    tools: dict[str, list[float]] = defaultdict(list)
    tool_fail: Counter = Counter()
    runs = []
    for rp in sorted(d.glob("*/s*/record.json")):
        rec = json.loads(rp.read_text())
        wire = [c for s in rec["steps"] for c in ((s.get("call") or {}).get("calls") or ())]
        if not wire:  # failed attempts keep their calls in the raw text
            for s in rec["steps"]:
                try:
                    m = json.loads(s.get("raw") or "{}").get("meta") or {}
                except (ValueError, AttributeError):
                    m = {}
                wire.extend(m.get("calls") or ())
        real = sum(float(c.get("duration_s") or 0) for c in wire)
        runs.append({"run": str(rp.parent.relative_to(d)), "state": rec["run"]["state"],
                     "stop_reason": rec["run"]["stop_reason"], "totals": rec["totals"],
                     "worst_case": rec["worst_case"], "wire_calls": len(wire),
                     "wire_seconds": round(real, 1), "tallies": [tally_summary(t) for t in rec["tallies"]],
                     "dissent": len(rec["dissent"]), "absent": rec["absent"],
                     "repairs": sum(1 for st in rec["steps"] if st.get("repair")),
                     "started_at": rec["run"].get("started_at"), "ended_at": rec["run"].get("ended_at"),
                     "blind": verify_blind(rec)})
        for s in rec["steps"]:
            row = per_seat[s["seat"]]
            if s["status"] == "absent":
                row["absent"][str(s.get("failure"))] += 1
                continue
            if s["attempt"] < 0:
                continue
            row["attempts"] += 1
            if s["status"] == "sealed":
                row["sealed"] += 1
                row["released"] += int(bool(s.get("released")))
            else:
                row["failed"][str(s.get("failure"))[:60]] += 1
            if s.get("gates"):
                row["gated"] += 1
                row["gate_pass"] += int(all(g.get("passed") for g in s["gates"]))
                for g in s["gates"]:
                    if not g.get("passed"):
                        row["gate_fail"][f"{g.get('gate')}:{g.get('reason')}"] += 1
            call = s.get("call") or {}
            row["dropped_keys"] += len(call.get("dropped_keys") or ())
            row["tool_calls"] += len(call.get("tool_calls") or ())
            if s.get("repair"):
                row["repairs"] += 1
                row["repaired"] += int(s["status"] == "sealed" and bool(s.get("released")))
            for tc in call.get("tool_calls") or ():
                if isinstance(tc, dict) and tc.get("tool"):
                    if tc.get("duration_s") is not None:
                        tools[str(tc["tool"])].append(float(tc["duration_s"]))
                    if tc.get("failed"):
                        tool_fail[f"{tc['tool']}:{tc['failed']}"] += 1
            for c in call.get("calls") or ():
                row["durations"].append(float(c.get("duration_s") or 0))
                if c.get("reasoning_tokens") is not None:
                    row["reasoning"].append(int(c["reasoning_tokens"]))
    seats = {}
    for k, v in sorted(per_seat.items()):
        seats[k] = {"attempts": v["attempts"], "parsed_sealed": v["sealed"], "released": v["released"],
                    "gated": v["gated"], "gate_pass": v["gate_pass"],
                    "failed": dict(v["failed"]), "absent": dict(v["absent"]),
                    "gate_fail": dict(v["gate_fail"]), "dropped_keys": v["dropped_keys"],
                    "tool_calls": v["tool_calls"], "repairs": v["repairs"], "repaired": v["repaired"],
                    "call_s_p50": _pct(v["durations"], 0.5),
                    "call_s_max": max(v["durations"]) if v["durations"] else None,
                    "reasoning_tokens_p50": _pct([float(x) for x in v["reasoning"]], 0.5)}
    tool_rows = {k: {"n": len(v), "p50_s": _pct(v, 0.5), "p95_s": _pct(v, 0.95), "max_s": max(v)}
                 for k, v in sorted(tools.items())}
    return {"label": label, "runs": runs, "seats": seats, "tools": tool_rows,
            "tool_failures": dict(tool_fail)}


def tally_summary(t: dict[str, Any]) -> dict[str, Any]:
    """One panel's tally, short: outcome, each candidate's outcome and votes,
    the removed, the advisory (uncalibrated) judges and their reasons."""
    return {"phase": t.get("phase"), "rule": t.get("rule"), "outcome": t.get("outcome"),
            "candidates": {str(c.get("candidate")).rsplit("/", 1)[-1]: {
                "outcome": c.get("outcome"), "votes": c.get("votes"), "removed": c.get("removed", False)}
                for c in t.get("candidates") or ()},
            "absent": t.get("absent"), "dissent": len(t.get("dissent") or ()),
            "advisory": [{k: a.get(k) for k in ("judge", "stage", "reason")} for a in t.get("advisory") or ()],
            "calibration_enforced": (t.get("calibration") or {}).get("enforced")}


# ---------------------------------------------------------------------------
# probe-thinking — the same rendered seat prompts, thinking off vs on
# ---------------------------------------------------------------------------


async def _probe_once(h: Any, system: str, user: str, thinking: bool, tools: Any = None) -> dict[str, Any]:
    from legba.data import run_accounting as ra
    from legba.data.stack.llm import tool_rounds as tr
    from legba.shared.sampling import HOUSE_TEMPERATURE

    tok = ra.bind_run_accounting()
    t0 = time.monotonic()
    out: dict[str, Any] = {"thinking": thinking, "tools_offered": bool(tools)}
    try:
        kw: dict[str, Any] = {"system": system, "temperature": HOUSE_TEMPERATURE, "thinking": thinking}
        if tools:
            kw["tools"] = tools
        resp = await h.chat_complete([{"role": "user", "content": user}], **kw)
        u = resp.usage
        out.update(ok=True, prompt_tokens=u.prompt_tokens, completion_tokens=u.completion_tokens,
                   reasoning_tokens=u.reasoning_tokens, finish_reason=resp.finish_reason,
                   served_model=(resp.raw_response or {}).get("model") if isinstance(resp.raw_response, dict) else None,
                   content_chars=len(resp.content or ""), cost=u.cost_estimate_usd,
                   tool_calls=[c.name for c in tr.parse_tool_calls(tr.PROVIDER_OPENAI_COMPAT, resp)] if tools else [])
    except Exception as exc:  # noqa: BLE001 — a failure is a measurement
        out.update(ok=False, error=f"{type(exc).__name__}: {str(exc)[:300]}",
                   http_status=getattr(exc, "status", None) if isinstance(getattr(exc, "status", None), int) else None)
    out["duration_s"] = round(time.monotonic() - t0, 3)
    acc = ra.current_llm_calls()
    ra.reset_run_accounting(tok)
    out["model_named"] = [c.get("model") for c in acc if isinstance(c, dict)]
    fb = [c.get("thinking_fallback") for c in acc if isinstance(c, dict) and c.get("thinking_fallback")]
    if fb:
        out["thinking_fallback"] = fb
    return out


async def _probe_main(a: argparse.Namespace) -> int:
    from legba.data.stack.llm import tool_rounds as tr

    common.load_env()
    root = _under_planning(Path(a.root))
    guard_egress()
    ledger = CallLedger(root / "calls.json", a.call_cap)
    planes, facts = await build_planes([CORE_COMPONENT])
    routes = assert_routes(facts)
    print("ROUTES ASSERTED:", json.dumps(routes, sort_keys=True, default=str), flush=True)
    if not facts[CORE_COMPONENT]["thinking_model_name"]:
        raise SystemExit(f"{THINKING_ENV} is unset — nothing to probe")
    h = planes[CORE_COMPONENT]
    prompts = []
    for p in sorted((root / "prompts").glob("*.json")):
        body = json.loads(p.read_text())
        prompts.append((len(body["system"]) + len(body["user"]), p.stem, body))
    prompts.sort()
    if not prompts:
        raise SystemExit("no saved seat prompts under --root/prompts (run a round first)")
    n = min(a.pairs, len(prompts))
    idx = sorted({round(i * (len(prompts) - 1) / max(1, n - 1)) for i in range(n)})
    chosen = [prompts[i] for i in idx]
    sem = asyncio.Semaphore(max(1, min(MAX_IN_FLIGHT, a.max_parallel)))
    results: list[dict[str, Any]] = []
    out_path = root / "thinking_probe.json"
    if out_path.is_file():
        results = json.loads(out_path.read_text()).get("results", [])
    done = {(r["prompt"], r["thinking"], r.get("tools_offered", False)) for r in results}

    async def pair(i: int, size: int, sha: str, body: dict[str, Any]) -> None:
        order = (False, True) if i % 2 == 0 else (True, False)
        for think in order:
            if (sha, think, False) in done:
                continue
            async with sem:
                if not ledger.take():
                    return
                r = await _probe_once(h, body["system"], body["user"], think)
            if r.get("cost"):
                raise SystemExit(f"a probe call reported a cost: {r}")
            r.update(prompt=sha, seat=body["seat"], prompt_chars=size)
            results.append(r)
            common.write_json(out_path, {"results": results})

    await asyncio.gather(*(pair(i, *c) for i, c in enumerate(chosen)))
    # tool calls on each alias: a seat prompt that offers the cut read tools
    tool_specs = tr.render_tools(tr.PROVIDER_OPENAI_COMPAT, tr.build_tool_specs(
        ["search_signals", "search_corpus", "read_document", "list_sources"]))
    tool_prompts = [c for c in prompts if "## Tools" in c[2]["system"]][: a.tool_probes] or prompts[: a.tool_probes]

    async def tool_pair(size: int, sha: str, body: dict[str, Any]) -> None:
        for think in (False, True):
            if (sha, think, True) in done:
                continue
            async with sem:
                if not ledger.take():
                    return
                r = await _probe_once(h, body["system"], body["user"], think, tools=tool_specs)
            r.update(prompt=sha, seat=body["seat"], prompt_chars=size)
            results.append(r)
            common.write_json(out_path, {"results": results})

    await asyncio.gather(*(tool_pair(*c) for c in tool_prompts))
    try:
        await h.on_deactivate(None)
    except Exception:  # noqa: BLE001
        pass
    print(json.dumps(summarise_probe(results), indent=1, sort_keys=True))
    print(f"calls used (ledger): {ledger.used()} / {ledger.cap}")
    print("EGRESS REFUSED:", json.dumps(egress_refused()), flush=True)
    return 0


def summarise_probe(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    plain = [r for r in results if not r.get("tools_offered")]
    sizes = sorted({r["prompt_chars"] for r in plain})
    cut = sizes[len(sizes) // 2] if sizes else 0
    for think in (False, True):
        for band, keep in (("all", lambda r: True), ("small", lambda r: r["prompt_chars"] < cut),
                           ("large", lambda r: r["prompt_chars"] >= cut)):
            rs = [r for r in plain if r["thinking"] is think and keep(r)]
            ok = [r for r in rs if r.get("ok")]
            d = [r["duration_s"] for r in ok]
            out[f"{'on' if think else 'off'}/{band}"] = {
                "n": len(rs), "ok": len(ok),
                "duration_p50_s": _pct(d, 0.5), "duration_p95_s": _pct(d, 0.95),
                "reasoning_tokens_p50": _pct([float(r.get("reasoning_tokens") or 0) for r in ok], 0.5),
                "reasoning_tokens_p95": _pct([float(r.get("reasoning_tokens") or 0) for r in ok], 0.95),
                "completion_tokens_p50": _pct([float(r.get("completion_tokens") or 0) for r in ok], 0.5),
                "prompt_tokens_p50": _pct([float(r.get("prompt_tokens") or 0) for r in ok], 0.5),
                "served_models": dict(Counter(str(r.get("served_model")) for r in ok)),
                "models_named": dict(Counter(m for r in rs for m in r.get("model_named") or ())),
                "failures": [r.get("error") for r in rs if not r.get("ok")],
                "fallbacks": [r.get("thinking_fallback") for r in rs if r.get("thinking_fallback")],
            }
    pairs = defaultdict(dict)
    for r in plain:
        if r.get("ok"):
            pairs[r["prompt"]][r["thinking"]] = r["duration_s"]
    ratios = [v[True] / v[False] for v in pairs.values() if True in v and False in v and v[False] > 0]
    out["paired"] = {"pairs": len(ratios), "on_over_off_p50": _pct(ratios, 0.5),
                     "on_over_off_p95": _pct(ratios, 0.95), "small_large_cut_chars": cut}
    tools = [r for r in results if r.get("tools_offered")]
    out["tools"] = {f"{'on' if t else 'off'}": {
        "n": len([r for r in tools if r["thinking"] is t]),
        "ok": len([r for r in tools if r["thinking"] is t and r.get("ok")]),
        "with_tool_calls": len([r for r in tools if r["thinking"] is t and r.get("tool_calls")]),
        "tool_names": dict(Counter(n for r in tools if r["thinking"] is t for n in r.get("tool_calls") or ())),
        "failures": [r.get("error") for r in tools if r["thinking"] is t and not r.get("ok")],
        "fallbacks": [r.get("thinking_fallback") for r in tools if r["thinking"] is t and r.get("thinking_fallback")],
    } for t in (False, True)}
    return out


# ---------------------------------------------------------------------------
# calibrate — each judge card on each judge plane, on the planted set
# ---------------------------------------------------------------------------


def _calibrate_one(models: Any, card: str, card_text: str, plane: str, row_type: str,
                   batch: list[Any], bi: int) -> tuple[dict[str, Any], list[tuple[Any, str]]]:
    """One judge call on one batch (one retry with the parse error, as a
    ``repair: reasons`` judge seat gets). Returns the call record and each
    candidate's counted outcome."""
    from legba.data.rounds import judge_planted as jp
    from legba.data.rounds.rows import RowTypeError, parse_row
    from legba.data.rounds.seats import SeatCall, SeatFailure
    from legba.data.rounds.spec import Seat
    from legba.data.rounds.tables import Handed
    from legba.data.rounds.tally import votes_of

    seat = Seat.model_validate({"kind": "judge", "reads": [{"from": "table", "view": "sentence_table"}],
                                "output": row_type, "card": card, "plane": plane})
    ids = tuple(it.item_id for it in batch)
    table = Handed("table", f"cal/table#{bi}", "table", "sentence_table/1", jp.table_of(batch))
    note = None
    rec: dict[str, Any] = {"batch": bi, "candidates": list(ids), "attempts": []}
    payload = None
    for attempt in range(2):
        call = SeatCall("calibration", "judge", seat, row_type, attempt, 0, "calibration", {},
                        (table,), card, card_text, None, None, ids, None, None, note)
        try:
            reply = models(call)
            payload = parse_row(row_type, reply.payload)
            rec["attempts"].append({"ok": True, "meta": dict(reply.meta or {})})
            break
        except (SeatFailure, RowTypeError) as exc:
            reason = getattr(exc, "reason", str(exc))
            rec["attempts"].append({"ok": False, "failure": str(reason)[:300]})
            note = {"attempt": attempt, "kind": "parse", "error": f"unparseable: {str(reason)[:400]}"}
            payload = None
    if payload is None:
        return rec, [(it, "unparsed") for it in batch]
    votes = votes_of("judge", payload, list(ids))
    rec["ranking"] = list(payload.get("ranking") or ())
    rec["outcomes"] = {c: v.outcome for c, v in votes.items()}
    return rec, [(it, votes[it.item_id].outcome) for it in batch]


def _calibrate_main(a: argparse.Namespace) -> int:
    from legba.data.rounds import judge_planted as jp
    from legba.data.rounds.calibration import CalibrationBook, card_sha256, save_book
    from legba.data.rounds.judge_render import render_version
    from legba.data.rounds.model_seats import LoopThread, ModelSeats

    common.load_env()
    root = _under_planning(Path(a.root))
    guard_egress()
    specs_dir = common.REPO_ROOT / "src" / "legba" / "data" / "rounds" / "specs"
    cards = {c: (specs_dir / c).read_text(encoding="utf-8") for c in a.card}
    ledger = CallLedger(root / "calls.json", a.call_cap)
    loop = LoopThread()
    planes, facts = loop.run(build_planes(sorted(set(a.plane))))
    routes = assert_routes(facts)
    print("ROUTES ASSERTED:", json.dumps(routes, sort_keys=True, default=str), flush=True)
    cap = max(1, min(MAX_IN_FLIGHT, int(a.max_parallel)))
    stop = threading.Event()

    def take() -> bool:
        return not stop.is_set() and ledger.take()

    models = ModelSeats(planes=planes, run=loop.run, tools=None, max_tool_rounds=0,
                        limiter=asyncio.Semaphore(cap), take_call=take)
    items = jp.calibration_set(per_kind=a.per_kind, controls=a.controls)
    batches = jp.batches(items)
    book_path = Path(a.book) if a.book else root / CALIBRATION_FILE
    book = CalibrationBook.load(book_path)
    records = [dict(r) for r in book.records]
    out_dir = root / "calibration" / a.label
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        for card, text in cards.items():
            for plane in a.plane:
                tag = f"{Path(card).stem}@{plane}@{a.row_type.replace('/', '_')}"
                calls: list[dict[str, Any]] = []
                scored: list[tuple[Any, str, list[str]]] = []
                with ThreadPoolExecutor(max_workers=cap) as ex:
                    for rec, outs in ex.map(lambda b: _calibrate_one(models, card, text, plane, a.row_type,
                                                                      b[1], b[0]), list(enumerate(batches))):
                        calls.append(rec)
                        for it, o in outs:
                            scored.append((it, o, rec.get("ranking") or []))
                        for at in rec["attempts"]:
                            for c in ((at.get("meta") or {}).get("calls") or ()):
                                if c.get("cost_estimate_usd"):
                                    stop.set()
                                    raise SystemExit(f"a calibration call reported a cost ({c}) — stopping")
                result = jp.score(scored)
                durations = [float(c.get("duration_s") or 0) for r in calls for at in r["attempts"]
                             for c in ((at.get("meta") or {}).get("calls") or ())]
                record = {"card": card, "card_sha256": card_sha256(text), "plane": plane,
                          "row_type": a.row_type, "render": render_version(a.row_type),
                          "measured_at": common.utcnow().isoformat(), "label": a.label,
                          "items": len(items), "calls": len(calls),
                          "unparsed": sum(1 for _, o, _ in scored if o == "unparsed"),
                          "call_s_p50": _pct(durations, 0.5), "call_s_max": max(durations) if durations else None,
                          **result}
                records = [r for r in records if not (r.get("card_sha256") == record["card_sha256"]
                                                      and r.get("plane") == plane
                                                      and r.get("row_type") == a.row_type
                                                      and r.get("render") == record["render"])] + [record]
                save_book(book_path, records)
                common.write_json(out_dir / f"{tag}.json", {"record": record, "calls": calls})
                print(json.dumps({"judge": tag, "catch": result["catch"], "false_reject": result["false_reject"],
                                  "ranking_pairs": result["ranking_pairs"], "unsure": result["unsure"],
                                  "unparsed": record["unparsed"],
                                  "may_gate": CalibrationBook.of([record]).standing(
                                      record["card_sha256"], plane, a.row_type, record["render"]).as_dict()},
                                 sort_keys=True), flush=True)
    finally:
        for h in planes.values():
            try:
                loop.run(h.on_deactivate(None))
            except Exception:  # noqa: BLE001
                pass
        loop.close()
    print(f"calls used (ledger): {ledger.used()} / {ledger.cap}")
    print("EGRESS REFUSED:", json.dumps(egress_refused()), flush=True)
    return 0


# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="arm_round.py", description=__doc__.split("\n")[1])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run a round spec as an arm")
    r.add_argument("--spec", required=True)
    r.add_argument("--label", required=True)
    r.add_argument("--questions", required=True)
    r.add_argument("--question", action="append", help="only these qids")
    r.add_argument("--as-of", required=True)
    r.add_argument("--samples", type=int, default=1)
    r.add_argument("--root", required=True)
    r.add_argument("--max-parallel", type=int, default=MAX_IN_FLIGHT)
    r.add_argument("--max-tool-rounds", type=int, default=2)
    r.add_argument("--call-cap", type=int, default=400)
    r.add_argument("--calibration", help=f"judge calibration book (default <root>/{CALIBRATION_FILE})")
    c = sub.add_parser("calibrate", help="judge catch / false-reject on the planted positions")
    c.add_argument("--root", required=True)
    c.add_argument("--label", required=True)
    c.add_argument("--card", action="append", required=True, help="card path under the specs folder")
    c.add_argument("--plane", action="append", required=True, help="judge plane (stack component id)")
    c.add_argument("--row-type", default="verdict/2", choices=("verdict/1", "verdict/2"))
    c.add_argument("--per-kind", type=int, default=3)
    c.add_argument("--controls", type=int, default=12)
    c.add_argument("--book", help=f"calibration book (default <root>/{CALIBRATION_FILE})")
    c.add_argument("--max-parallel", type=int, default=MAX_IN_FLIGHT)
    c.add_argument("--call-cap", type=int, default=400)
    p = sub.add_parser("probe-thinking", help="thinking off vs on on saved seat prompts")
    p.add_argument("--root", required=True)
    p.add_argument("--pairs", type=int, default=24)
    p.add_argument("--tool-probes", type=int, default=3)
    p.add_argument("--max-parallel", type=int, default=MAX_IN_FLIGHT)
    p.add_argument("--call-cap", type=int, default=400)
    s = sub.add_parser("report", help="per-seat parse/gate/absence; totals vs worst case")
    s.add_argument("--root", required=True)
    s.add_argument("--label", required=True)
    a = ap.parse_args(argv)
    if a.cmd == "run":
        return _run_main(a)
    if a.cmd == "probe-thinking":
        return asyncio.run(_probe_main(a))
    if a.cmd == "calibrate":
        return _calibrate_main(a)
    print(json.dumps(report(_under_planning(Path(a.root)), a.label), indent=1, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

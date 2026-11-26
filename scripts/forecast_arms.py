#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""forecast_arms.py — the comparison arms' numbers on the open forecasts.

For each OPEN, UNSEALED forecast question in the ledger (migration 0242) this
states one number per arm, all at ONE as-of and from the SAME evidence
packet (the design rule: same wires for every arm):

  * ``clef_arm``     — the gateway decision-model route
    (``llm.decide.gateway_typed_choice``, ``POST /decide``, $0): a yes/no choice
    whose ``probabilities["yes"]`` is the number. A decision model writes no
    text, so its rationale is absent (stated, not invented).
  * ``frontier_arm`` — the frontier route ``llm.anthropic.opus_4_7`` (it must
    serve ``claude-opus-5-5``, the bake-off's pin): one Messages call per
    question for ``{"probability", "rationale"}`` (≤ 120 words). PAID: refuses
    without ``--execute --ceiling-usd N`` and the operator-verified prices
    file (the bake-off's ``common.Prices``, imported, not copied); records the
    provider's usage and the cost per call; admits a call only while
    ``spent + that call's worst case <= ceiling`` and stops there, naming the
    questions not reached. A record already holding spend is never re-run.
  * ``tower``        — the scheduled analysts' own number: a finding produced
    in the 24 h to the as-of whose ``data.forecast.slug`` is the question's
    slug. None does today, so the tower arm is recorded ABSENT with that
    reason — never a number made up for it.

THE PACKET. The question, its resolver kind and spec, window, due date,
condition and void rules, then — per desk in the question's
``subject_keys`` — the desk brief exactly as the platform serves it
(``scripts/bakeoff/packet_a.py``: ``desk_brief`` + ``render``, the bake-off
arm A's packet). A desk whose brief cannot be built is a ``no_read:`` line.
The packet's sha256 and the prompt's sha256 ride on every number.

A question whose text still carries an ``<OPERATOR …>`` placeholder is
skipped: its wording is not final, and a number binds the words it was
stated on (the seal refuses a number stated on an earlier wording).

``--dry-run`` builds and prints the packets and the frontier cost bounds and
calls nothing, writes nothing, costs nothing. Otherwise every call lands a
JSON record under ``--out`` (default
``planning/reviews/forecast_arms/<as-of date>/``) and a ``forecast_numbers``
row through the ONE store (``legba.data.forecast_ledger.state_number``).

House rules: no max_tokens of our own and temperature 1.0 where a route takes
them. The Messages API requires ``max_tokens``: the request carries the
frontier COMPONENT's configured value (the route's default — what the
platform's own handler sends), and no temperature (the Opus 5.x line rejects
sampling parameters; ``stack/llm/anthropic.py``). The decide route takes
neither.

    python3 scripts/forecast_arms.py --dry-run
    python3 scripts/forecast_arms.py --arm clef_arm tower
    python3 scripts/forecast_arms.py --arm frontier_arm --execute --ceiling-usd 10 \\
        --prices planning/reviews/bakeoff_prices_2026-11-05.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE / "bakeoff"))
sys.path.insert(0, str(_HERE.parent / "src"))

import common  # noqa: E402 — the bake-off harness: env, registry, prices, the read-only conn
import messages_api  # noqa: E402
import packet_a  # noqa: E402

from legba.data import forecast_ledger as fl  # noqa: E402
from legba.data.schemas.legacy_name_digests import canonical_component_id  # noqa: E402

ARMS = ("clef_arm", "frontier_arm", "tower")
DECISION_COMPONENT = "llm.decide.gateway_typed_choice"
FRONTIER_COMPONENT = common.FRONTIER_COMPONENT
RATIONALE_WORDS = 120
#: Characters per token used for the worst-case INPUT bound (conservative:
#: English prose runs ~4; ids and markdown run lower).
CHARS_PER_TOKEN = 3.0
DEFAULT_PRICES = common.REPO_ROOT / "planning" / "reviews" / "bakeoff_prices_2026-11-05.json"
RUN_SCHEMA = "legba.forecast_arms.run/1"
RECORD_SCHEMA = "legba.forecast_arms.record/1"

FORECAST_PROMPT = """You are stating a probability on a forecast question that will be resolved against the world.

As of {as_of}. Read ONLY the evidence packet below (the question, how it resolves, and the platform's current reads for the desks it concerns). Do not search; use what you already know only where the packet is silent, and say so.

Reply with ONE JSON object and nothing else:
{{"probability": <the probability that the question resolves YES, a number between 0 and 1>, "rationale": "<at most 120 words: the two or three things that move the number most>"}}

{packet}"""
PROMPT_SHA256 = hashlib.sha256(FORECAST_PROMPT.encode("utf-8")).hexdigest()

#: The tower contract: a scheduled analyst states a number on a ledger
#: question by carrying ``data.forecast = {slug, p}`` on a finding.
TOWER_SQL = """
SELECT id::text AS id, analyst_id, produced_at,
       (data->'data'->'forecast'->>'p')::float8 AS p
  FROM analyst_outputs
 WHERE produced_at >= $2 AND produced_at <= $3
   AND data->'data'->'forecast'->>'slug' = $1
 ORDER BY produced_at DESC
 LIMIT 1
"""


# ---------------------------------------------------------------------------
# The packet
# ---------------------------------------------------------------------------


def question_header(row: dict[str, Any], as_of: datetime) -> str:
    spec = row.get("resolver_spec")
    lines = [
        "## The question",
        "",
        str(row["question"]),
        "",
        f"- resolver: {row['resolver_kind']} — {json.dumps(spec, sort_keys=True, ensure_ascii=False)}",
        f"- window: {row.get('window_start') or '—'} → {row.get('window_end') or '—'}",
        f"- due: {row['due_at']} (grace {row.get('grace_hours')} h)",
    ]
    if row.get("condition"):
        lines.append(f"- condition: {row['condition']}")
    if row.get("void_rules"):
        lines.append(f"- void if: {row['void_rules']}")
    lines.append(f"- evidence as of: {as_of.isoformat()}")
    return "\n".join(lines)


def build_packet(row: dict[str, Any], as_of: datetime,
                 brief: Callable[[str], dict[str, Any]]) -> tuple[str, dict[str, Any]]:
    """``(packet text, meta)`` — header + every desk's served brief."""
    briefs: list[dict[str, Any]] = []
    no_read: list[str] = []
    for desk in row.get("subject_keys") or []:
        try:
            briefs.append(brief(desk))
        except Exception as exc:  # noqa: BLE001 — a desk with no brief is a stated absence
            no_read.append(f"no_read: {desk} ({type(exc).__name__})")
    text, _segments, refs = packet_a.render(briefs) if briefs else ("", [], [])
    parts = [question_header(row, as_of), "## The evidence (the platform's current reads)"]
    parts.append(text or "no desk brief could be read for this question")
    parts += no_read
    packet = "\n\n".join(parts)
    meta = {
        "desks": list(row.get("subject_keys") or []),
        "desks_read": [b.get("desk") for b in briefs],
        "no_read": no_read,
        "cited_refs": len(refs),
        "chars": len(packet),
        "packet_sha256": hashlib.sha256(packet.encode("utf-8")).hexdigest(),
    }
    return packet, meta


def prompt_for(packet: str, as_of: datetime) -> str:
    return FORECAST_PROMPT.format(as_of=as_of.isoformat(), packet=packet)


# ---------------------------------------------------------------------------
# Replies
# ---------------------------------------------------------------------------


def parse_frontier_reply(text: str) -> tuple[float | None, str | None, str | None]:
    """``(p, rationale, problem)`` from the frontier's reply."""
    obj = common.extract_json_object(text)
    if obj is None:
        return None, None, "the reply carries no JSON object"
    try:
        p = float(obj.get("probability"))
    except (TypeError, ValueError):
        return None, None, "the reply's probability is not a number"
    if not 0.0 <= p <= 1.0:
        return None, None, f"the reply's probability {p} is outside 0..1"
    words = str(obj.get("rationale") or "").split()
    rationale = " ".join(words[:RATIONALE_WORDS]) or None
    problem = f"rationale cut at {RATIONALE_WORDS} words (was {len(words)})" if len(words) > RATIONALE_WORDS else None
    return p, rationale, problem


def decision_questions(row: dict[str, Any]) -> dict[str, Any]:
    spec = json.dumps(row.get("resolver_spec"), sort_keys=True, ensure_ascii=False)
    return {"resolves_yes": {
        "type": "choice",
        "instructions": (
            f"Will this forecast question resolve YES? {row['question']} "
            f"Resolver: {row['resolver_kind']} {spec}. Due {row['due_at']}."
        ),
        "criteria": {
            "yes": "the resolver will confirm the stated outcome by the due date",
            "no": "the resolver will not confirm the stated outcome by the due date",
        },
    }}


def decision_p(answer: dict[str, Any]) -> float | None:
    probs = answer.get("probabilities") or {}
    if "yes" in probs:
        return float(probs["yes"])
    conf = answer.get("confidence")
    if conf is None:
        return None
    return float(conf) if answer.get("choice") == "yes" else 1.0 - float(conf)


# ---------------------------------------------------------------------------
# The run ledger (money) and the records
# ---------------------------------------------------------------------------


@dataclass
class Run:
    out: Path
    as_of: datetime
    doc: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def open(cls, out: Path, as_of: datetime) -> "Run":
        out.mkdir(parents=True, exist_ok=True)
        path = out / "run.json"
        if path.is_file():
            doc = json.loads(path.read_text(encoding="utf-8"))
            if doc["as_of"] != as_of.isoformat():
                raise RuntimeError(f"{path} is a run at as-of {doc['as_of']}; one run, one as-of — use a new --out")
        else:
            doc = {"schema": RUN_SCHEMA, "as_of": as_of.isoformat(), "prompt_sha256": PROMPT_SHA256,
                   "ceiling_usd": None, "spent_usd": 0.0, "refused_at_ceiling": [],
                   "created_at": common.utcnow().isoformat()}
        run = cls(out=out, as_of=as_of, doc=doc)
        run.save()
        return run

    def save(self) -> None:
        common.write_json(self.out / "run.json", self.doc)

    def fix_ceiling(self, ceiling: float) -> None:
        if self.doc.get("ceiling_usd") not in (None, ceiling):
            raise RuntimeError(f"the run ceiling is fixed at ${self.doc['ceiling_usd']} (its first paid call)")
        self.doc["ceiling_usd"] = ceiling
        self.save()

    def record_path(self, slug: str, arm: str) -> Path:
        return self.out / f"{slug}__{arm}.json"

    def already_answered(self, slug: str, arm: str) -> bool:
        path = self.record_path(slug, arm)
        if not path.is_file():
            return False
        return json.loads(path.read_text(encoding="utf-8")).get("status") in ("answered", "unparsed")


def new_record(row: dict[str, Any], arm: str, as_of: datetime, meta: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": RECORD_SCHEMA, "slug": row["slug"], "question_id": row["id"], "arm": arm,
        "as_of": as_of.isoformat(), "question_sha256": row["content_sha256"],
        "packet": meta, "prompt_sha256": PROMPT_SHA256, "status": None, "refusal": None,
        "model": None, "request": None, "reply_text": None, "p": None, "rationale": None,
        "problem": None, "usage": None, "cost": None, "elapsed_s": None,
        "created_at": common.utcnow().isoformat(),
    }


# ---------------------------------------------------------------------------
# The arms
# ---------------------------------------------------------------------------

MessagesCall = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
DecideCall = Callable[[str, dict[str, Any]], Awaitable[dict[str, Any]]]


def frontier_body(model: str, max_tokens: int, prompt: str) -> dict[str, Any]:
    """The request: the route's configured max_tokens (required by the API),
    no temperature (the model rejects it), no thinking (the route's default)."""
    return {"model": model, "max_tokens": int(max_tokens), "messages": [{"role": "user", "content": prompt}]}


def worst_case_usd(prices: Any, prompt: str, max_tokens: int) -> float:
    est_in = int(len(prompt) / CHARS_PER_TOKEN) + 1
    return round((est_in * prices.input + int(max_tokens) * prices.output) / 1_000_000, 6)


@dataclass
class Frontier:
    model: str
    max_tokens: int
    version: str | None
    prices: Any | None
    call: MessagesCall | None
    refusal: str | None = None


async def frontier_arm(row: dict[str, Any], prompt: str, rec: dict[str, Any], run: Run,
                       fr: Frontier, ceiling: float | None) -> dict[str, Any]:
    rec["model"] = {"component": FRONTIER_COMPONENT, "model_name": fr.model, "version": fr.version,
                    "pinned_model": common.FRONTIER_MODEL}
    body = frontier_body(fr.model, fr.max_tokens, prompt)
    rec["request"] = {"body_sha256": hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest(),
                      "max_tokens": fr.max_tokens, "max_tokens_source": "the component's configured value",
                      "temperature": "not sent (rejected by the model line)"}
    worst = worst_case_usd(fr.prices, prompt, fr.max_tokens) if fr.prices else None
    rec["cost"] = {"worst_case_usd": worst, "usd": None,
                   "price_per_m": fr.prices.per_m() if fr.prices else None}
    refusal = fr.refusal
    # The paid gate, re-checked at the call site whatever the wiring says.
    if refusal is None and fr.call is None:
        refusal = "dry_run: no paid route wired (--execute was not given)"
    if refusal is None and (not ceiling or ceiling <= 0):
        refusal = "dry_run: no operator --ceiling-usd"
    if refusal is None and fr.prices is None:
        refusal = "dry_run: no verified --prices file"
    if refusal is None and worst is not None and ceiling is not None:
        spent = float(run.doc.get("spent_usd") or 0.0)
        if spent + worst > ceiling:
            refusal = (f"run_ceiling: ${spent:.4f} spent + this call's ${worst:.4f} worst case "
                       f"> the ${ceiling:.2f} ceiling — not reached")
            run.doc["refused_at_ceiling"].append(row["slug"])
            run.save()
    if refusal is not None:
        rec.update(status="dry_run" if refusal.startswith("dry_run") else "refused", refusal=refusal)
        return rec
    t0 = time.monotonic()
    try:
        resp = await fr.call(body)
    except Exception as exc:  # noqa: BLE001 — unseen spend is charged at the worst case
        run.doc["spent_usd"] = round(float(run.doc["spent_usd"]) + float(worst or 0), 6)
        run.save()
        rec.update(status="error", refusal=f"messages call failed: {type(exc).__name__}")
        rec["cost"]["usd"] = worst
        rec["cost"]["charged_at_worst_case"] = True
        return rec
    usage = messages_api.usage_of(resp)
    usd = fr.prices.cost(input_tokens=usage["input_tokens"], output_tokens=usage["output_tokens"],
                         cache_read=usage["cache_read_tokens"], cache_write=usage["cache_write_tokens"])
    run.doc["spent_usd"] = round(float(run.doc["spent_usd"]) + usd, 6)
    run.save()
    text = messages_api.text_of(resp)
    p, rationale, problem = parse_frontier_reply(text)
    rec.update(reply_text=text, p=p, rationale=rationale, problem=problem, usage=usage,
               elapsed_s=round(time.monotonic() - t0, 2),
               status="answered" if p is not None else "unparsed")
    rec["cost"]["usd"] = usd
    rec["model"]["response_model"] = resp.get("model")
    return rec


async def decision_arm(row: dict[str, Any], packet: str, rec: dict[str, Any],
                   decide: DecideCall | None, model: str | None, version: str | None,
                   dry_run: bool) -> dict[str, Any]:
    rec["model"] = {"component": DECISION_COMPONENT, "model_name": model, "version": version}
    questions = decision_questions(row)
    rec["request"] = {"questions": questions, "state_sha256": hashlib.sha256(packet.encode()).hexdigest()}
    rec["cost"] = {"usd": 0.0}
    if dry_run:
        rec.update(status="dry_run", refusal="dry_run: nothing is called")
        return rec
    t0 = time.monotonic()
    try:
        answers = await decide(packet, questions)
    except Exception as exc:  # noqa: BLE001 — a failed decision is an absence, named
        rec.update(status="error", refusal=f"decide failed: {type(exc).__name__}: {str(exc)[:200]}")
        return rec
    answer = answers.get("resolves_yes") or {}
    p = decision_p(answer)
    rec.update(p=p, elapsed_s=round(time.monotonic() - t0, 2), reply_text=json.dumps(answer, sort_keys=True),
               status="answered" if p is not None else "unparsed",
               problem="a decision model writes no rationale" if p is not None else "no probability in the answer")
    return rec


async def tower_arm(row: dict[str, Any], rec: dict[str, Any], conn: Any, as_of: datetime) -> dict[str, Any]:
    rec["model"] = {"source": "analyst_outputs data.forecast {slug, p}"}
    rec["cost"] = {"usd": 0.0}
    found = await conn.fetchrow(TOWER_SQL, row["slug"], as_of - timedelta(hours=24), as_of)
    if found is None or found["p"] is None:
        rec.update(status="absent", problem=(
            f"no scheduled analyst emitted a probability for {row['slug']} in the 24 h to "
            f"{as_of.isoformat()} (contract: a finding's data.forecast = {{slug, p}})"))
        return rec
    rec.update(status="answered", p=float(found["p"]),
               reply_text=f"analyst_output:{found['id']} ({found['analyst_id']})")
    return rec


def number_for(rec: dict[str, Any], arm: str, issuer_ref: str, as_of: datetime,
               record_ref: str) -> fl.ForecastNumber | None:
    """The ledger row for a record: a number, a stated absence, or nothing
    (a dry run / a refusal writes no row)."""
    if rec["status"] in ("dry_run", "refused"):
        return None
    detail = {k: rec.get(k) for k in ("model", "usage", "cost", "prompt_sha256", "problem")}
    detail["packet_sha256"] = (rec.get("packet") or {}).get("packet_sha256")
    if rec["status"] == "answered" and rec.get("p") is not None:
        return fl.ForecastNumber(issuer_kind=arm, issuer_ref=issuer_ref, p=rec["p"], as_of=as_of,
                                 rationale=rec.get("rationale"), rationale_ref=record_ref, detail=detail)
    reason = rec.get("problem") or rec.get("refusal") or rec["status"]
    return fl.ForecastNumber(issuer_kind=arm, issuer_ref=issuer_ref, p=None, as_of=as_of,
                             absent_reason=str(reason)[:500], rationale_ref=record_ref, detail=detail)


# ---------------------------------------------------------------------------
# The run
# ---------------------------------------------------------------------------


@dataclass
class Wiring:
    """Everything the run touches, injectable (tests pass fakes)."""

    rows: list[dict[str, Any]]
    brief: Callable[[str], dict[str, Any]]
    write_conn: Any
    read_conn: Any
    frontier: Frontier | None = None
    decide: DecideCall | None = None
    decision_model: str | None = None
    decision_version: str | None = None


async def stated_ref(conn: Any, question_id: str, arm: str, ref: str) -> str:
    """The issuer_ref to state this arm's number under: an earlier number of the
    same arm on this question stated under a RETIRED name of ``ref`` keeps its
    ref, so the restatement updates that row instead of adding a second number
    of the same arm (the ledger's unique key includes the ref)."""
    for n in await conn.fetch(fl._NUMBERS_FOR_QUESTION, str(question_id)):  # noqa: SLF001
        old = str(n["issuer_ref"])
        if n["issuer_kind"] == arm and old != ref and canonical_component_id(old) == ref:
            return old
    return ref


def open_unsealed(rows: list[dict[str, Any]], slugs: list[str] | None) -> list[dict[str, Any]]:
    return [r for r in rows if r["status"] == "open" and not r.get("sealed_at")
            and (not slugs or r["slug"] in slugs)]


async def run_arms(w: Wiring, *, arms: list[str], as_of: datetime, out: Path, dry_run: bool,
                   ceiling: float | None, slugs: list[str] | None = None,
                   echo: Callable[[str], None] = print) -> dict[str, Any]:
    run = None if dry_run else Run.open(out, as_of)
    if run is not None and ceiling is not None and "frontier_arm" in arms:
        run.fix_ceiling(ceiling)
    summary: dict[str, Any] = {"as_of": as_of.isoformat(), "dry_run": dry_run, "questions": [],
                               "skipped": [], "written": 0}
    for row in open_unsealed(w.rows, slugs):
        q = fl.ForecastQuestion.from_row(row)
        if q.owed_fields():
            summary["skipped"].append(f"{row['slug']}: wording not final ({', '.join(q.owed_fields())})")
            continue
        packet, meta = build_packet(row, as_of, w.brief)
        prompt = prompt_for(packet, as_of)
        entry: dict[str, Any] = {"slug": row["slug"], "packet": meta, "arms": {}}
        if dry_run:
            echo(f"===== {row['slug']} — packet {meta['packet_sha256'][:12]} ({meta['chars']} chars)\n{packet}\n")
        for arm in arms:
            if run is not None and run.already_answered(row["slug"], arm):
                entry["arms"][arm] = "already answered (record kept; never re-run)"
                continue
            rec = new_record(row, arm, as_of, meta)
            if arm == "frontier_arm":
                fr = w.frontier or Frontier("unconfigured", 0, None, None, None, "dry_run: no frontier route")
                rec = await frontier_arm(row, prompt, rec, run, fr, ceiling) if run is not None else \
                    await frontier_arm(row, prompt, rec, _DryRun(), fr, ceiling)
                ref = FRONTIER_COMPONENT
            elif arm == "clef_arm":
                rec = await decision_arm(row, packet, rec, w.decide, w.decision_model, w.decision_version, dry_run)
                ref = DECISION_COMPONENT
            else:
                rec = await tower_arm(row, rec, w.read_conn, as_of)
                ref = "tower"
            entry["arms"][arm] = {"status": rec["status"], "p": rec.get("p"),
                                  "worst_case_usd": (rec.get("cost") or {}).get("worst_case_usd"),
                                  "usd": (rec.get("cost") or {}).get("usd"), "refusal": rec.get("refusal")}
            if dry_run or run is None:
                continue
            path = run.record_path(row["slug"], arm)
            common.write_json(path, rec)
            try:
                record_ref = str(path.relative_to(common.REPO_ROOT))
            except ValueError:
                record_ref = str(path)
            ref = await stated_ref(w.write_conn, row["id"], arm, ref)
            n = number_for(rec, arm, ref, as_of, record_ref)
            if n is not None:
                outcome, _ = await fl.state_number(w.write_conn, row["id"], n,
                                                   written_by="rule:forecast_arms")
                entry["arms"][arm]["ledger"] = outcome
                summary["written"] += 1
        summary["questions"].append(entry)
    if run is not None:
        summary["spent_usd"] = run.doc["spent_usd"]
        summary["ceiling_usd"] = run.doc.get("ceiling_usd")
        summary["refused_at_ceiling"] = run.doc.get("refused_at_ceiling")
    return summary


class _DryRun:
    """A run ledger stand-in for --dry-run: nothing is saved."""

    doc = {"spent_usd": 0.0, "refused_at_ceiling": []}

    def save(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Live wiring
# ---------------------------------------------------------------------------


async def _live(a: argparse.Namespace, as_of: datetime) -> dict[str, Any]:  # pragma: no cover — live only
    import asyncpg
    import httpx

    from legba.data.stack.llm.decide import parse_answers, validate_questions

    common.load_env()
    reg = common.Registry.from_env()
    arms = list(a.arm)
    async with common.readonly_conn() as ro:
        rows = await fl.fetch_ledger(ro, limit=1000, now=as_of)
        frontier = decide = None
        decision_model = decision_version = None
        if "frontier_arm" in arms:
            comp = reg.stack_component(FRONTIER_COMPONENT)
            raw = (reg.get(f"/api/v1/registry/stack/{FRONTIER_COMPONENT}").get("body") or {}).get("config") or {}
            mt = raw.get("max_tokens")
            max_tokens = int(mt.get("raw") if isinstance(mt, dict) else mt or 8192)
            gate = common.PaidGate(a.execute, a.ceiling_usd, Path(a.prices) if a.prices else None)
            refusal = gate.refusal() or common.frontier_refusal(comp)
            prices = (common.Prices.load(Path(a.prices), comp["model_name"])
                      if a.prices and Path(a.prices).is_file() else None)
            call = None
            if refusal is None and not a.dry_run:
                key = await common.resolve_secret(ro, comp["api_key_ref"])

                async def call(body: dict[str, Any], _key: str = key, _ep: str = comp["api_endpoint"]) -> dict[str, Any]:
                    return await messages_api.post(_ep, _key, body)
            frontier = Frontier(str(comp["model_name"]), max_tokens, comp.get("version"), prices, call,
                                refusal if not a.dry_run else (refusal or "dry_run: nothing is called"))
        if "clef_arm" in arms and not a.dry_run:
            comp = reg.stack_component(DECISION_COMPONENT)
            key = await common.resolve_secret(ro, comp["api_key_ref"])
            decision_model, decision_version = str(comp["model_name"]), comp.get("version")
            url = f"{str(comp['api_endpoint']).rstrip('/')}/decide"

            async def decide(state: str, questions: dict[str, Any], _key: str = key) -> dict[str, Any]:
                validate_questions(questions)
                async with httpx.AsyncClient(timeout=httpx.Timeout(300.0, connect=15.0)) as c:
                    r = await c.post(url, json={"model": decision_model, "state": state, "questions": questions},
                                     headers={"Authorization": f"Bearer {_key}"})
                    r.raise_for_status()
                    return parse_answers(r.json(), questions)
        write_conn = None if a.dry_run else await asyncpg.connect(common._pg_dsn())
        try:
            w = Wiring(rows=rows, brief=lambda desk: packet_a.desk_brief(reg, desk), write_conn=write_conn,
                       read_conn=ro, frontier=frontier, decide=decide, decision_model=decision_model,
                       decision_version=decision_version)
            out = Path(a.out) if a.out else (
                common.REPO_ROOT / "planning" / "reviews" / "forecast_arms" / as_of.date().isoformat())
            return await run_arms(w, arms=arms, as_of=as_of, out=out, dry_run=a.dry_run,
                                  ceiling=a.ceiling_usd, slugs=a.slug)
        finally:
            if write_conn is not None:
                await write_conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="State the comparison arms' numbers on the open forecasts.")
    ap.add_argument("--arm", nargs="+", choices=ARMS, default=list(ARMS))
    ap.add_argument("--as-of", help="ISO instant every arm shares (default: now, to the minute)")
    ap.add_argument("--slug", nargs="*", help="only these questions")
    ap.add_argument("--out", help="record directory (default: a dated folder under the forecast_arms output directory)")
    ap.add_argument("--dry-run", action="store_true", help="print the packets and cost bounds; call and write nothing")
    ap.add_argument("--execute", action="store_true", help="allow the PAID frontier arm")
    ap.add_argument("--ceiling-usd", type=float, help="the operator's ceiling for this run's frontier spend")
    ap.add_argument("--prices", default=str(DEFAULT_PRICES), help="the operator-verified prices file")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:  # pragma: no cover — live only
    a = parse_args(argv)
    as_of = common.parse_as_of(a.as_of) if a.as_of else common.utcnow().replace(second=0, microsecond=0)
    summary = asyncio.run(_live(a, as_of))
    print(json.dumps(summary, indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())

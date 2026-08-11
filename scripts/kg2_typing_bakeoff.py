#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""K-G2 step 3 — the four-way batch-typing bake-off.

Runs the SAME deterministically-sampled candidates through several models via
:mod:`legba.data.analysts.relationship_typing_batch`, and records, per model:
verdicts, parse integrity, tokens, and wall time.

Roster (``--models``):

  ``core120b``   ``llm.primary.openai_compat``  — self-hosted gpt-oss-120b, the
                 plane every scheduled analyst already runs on. The reference.
  ``slm8b``      ``llm.verify.slm_8b`` — the self-hosted Llama-3.1-8B, never
                 previously asked to type an edge.
  ``nemotron``   OpenRouter free-tier Nemotron.
  ``gptoss``     OpenRouter gpt-oss-120b — the SAME weights as ``core120b``,
                 rented instead of self-hosted, so the pair isolates hosting
                 economics from model capability.
  ``gptoss20bfree`` OpenRouter gpt-oss-20b — the only genuinely free gpt-oss
                 tier OpenRouter publishes (see the report).

The two self-hosted planes are driven through the REAL in-tree provider handler
(``legba.data.stack.llm``) with credentials from the live vault, so the
measurement traverses production's own auth/request/parse path rather than a
bespoke client that might flatter or punish a model by accident. The OpenRouter
planes use a plain OpenAI-compatible client — they are not registered
production components and this task registers nothing.

Modes:
    --mode sweep   N-sweep on a fixed head-slice; finds the safe batch size.
    --mode main    the full sample at one N, identical for every model.

Free-tier discipline: ``--pace`` seconds between calls, exponential backoff on
429/5xx, and a bounded retry budget. If a model's daily cap blocks completion
the run stops and records how far it got — the scorer then intersects to the
candidates ALL models answered, because comparability beats volume.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import httpx  # noqa: E402

from legba.data.analysts.relationship_typing_batch import (  # noqa: E402
    BATCH_SYSTEM_PROMPT,
    DEFAULT_EVIDENCE_CHARS,
    BatchCandidate,
    build_batch_user_prompt,
    max_tokens_for_batch,
    parse_batch_response,
)

TEMPERATURE = 0.1  # relationship_reifier.DEFAULT_TEMPERATURE — typing wants determinism

OPENROUTER_BASE = "https://openrouter.ai/api/v1/chat/completions"


# ---------------------------------------------------------------------------
# Roster
# ---------------------------------------------------------------------------


@dataclass
class ModelSpec:
    key: str
    label: str
    transport: str            # "stack" (in-tree handler) | "openrouter"
    component_id: str = ""    # transport=stack
    model_id: str = ""        # transport=openrouter
    pace_seconds: float = 0.0
    max_retries: int = 4
    is_free: bool = False
    #: USD per million tokens (prompt, completion). 0/0 for self-hosted.
    price_prompt_musd: float = 0.0
    price_completion_musd: float = 0.0
    #: Extra request-body keys (OpenRouter's unified ``reasoning`` control).
    extra_body: dict[str, Any] = field(default_factory=dict)
    #: Multiplier on the computed completion budget. Models whose reasoning
    #: cannot be switched off spend the budget TWICE — once thinking, once
    #: answering — and truncate at the default reservation.
    token_multiplier: float = 1.0


ROSTER: dict[str, ModelSpec] = {
    "core120b": ModelSpec(
        key="core120b", label="core 120B (self-hosted gpt-oss-120b)",
        transport="stack", component_id="llm.primary.openai_compat",
        pace_seconds=0.0,
    ),
    "slm8b": ModelSpec(
        key="slm8b", label="slm_8b (self-hosted Llama-3.1-8B Q5_K_M)",
        transport="stack", component_id="llm.verify.slm_8b",
        pace_seconds=0.0,
    ),
    # Nemotron 3 defaults to thinking-on and, at N=12, spends the ENTIRE
    # completion budget reasoning and returns empty content (measured: 3,635
    # reasoning tokens, 0 verdicts, 77 s). Thinking-off is the documented fix
    # for this model family's latency and it restores content immediately
    # (measured: 2.4 s, 0 reasoning tokens, full array).
    "nemotron": ModelSpec(
        key="nemotron", label="Nemotron 3 Super 120B A12B (OpenRouter free, thinking-off)",
        transport="openrouter", model_id="nvidia/nemotron-3-super-120b-a12b:free",
        pace_seconds=4.0, is_free=True,
        extra_body={"reasoning": {"enabled": False}},
    ),
    # gpt-oss on OpenRouter REFUSES to disable reasoning ("Reasoning is
    # mandatory for this endpoint", HTTP 400), so the budget must cover the
    # think pass as well as the answer.
    "gptoss": ModelSpec(
        key="gptoss", label="gpt-oss-120b (OpenRouter, paid tier — same weights as core)",
        transport="openrouter", model_id="openai/gpt-oss-120b",
        pace_seconds=1.0, price_prompt_musd=0.037, price_completion_musd=0.17,
        token_multiplier=2.5,
    ),
    "gptoss20bfree": ModelSpec(
        key="gptoss20bfree", label="gpt-oss-20b (OpenRouter free)",
        transport="openrouter", model_id="openai/gpt-oss-20b:free",
        pace_seconds=4.0, is_free=True, token_multiplier=2.5,
    ),
}


# ---------------------------------------------------------------------------
# Transports
# ---------------------------------------------------------------------------


class StackTransport:
    """Drives a registered stack component through the in-tree LLM handler."""

    def __init__(self, component_id: str) -> None:
        self.component_id = component_id
        self._handler: Any = None
        self._model_name = ""
        self._vault: Any = None

    async def start(self) -> None:
        import asyncpg
        from legba.data.registry.credentials import CredentialVault
        from legba.data.stack.llm import LLM_HANDLERS
        from legba.data.schemas.stack import LLMProviderConfig
        from legba.runtime.analyst_deps_builder import (
            _BuilderHandlerContext,
            _SecretsResolverAdapter,
            infer_llm_subprovider,
        )

        dsn = (
            f"postgresql://{os.environ.get('LEGBA_DATA_PG_USER','legba')}:"
            f"{os.environ['LEGBA_DATA_PG_PASSWORD']}@"
            f"{os.environ.get('LEGBA_DATA_PG_HOST','postgres')}:5432/"
            f"{os.environ.get('LEGBA_DATA_PG_DB','legba')}"
        )
        conn = await asyncpg.connect(dsn)
        row = await conn.fetchrow(
            "SELECT version, body FROM stack_components "
            "WHERE component_id=$1 AND is_head",
            self.component_id,
        )
        await conn.close()
        if row is None:
            raise SystemExit(f"stack component {self.component_id!r} not found")
        body = json.loads(row["body"]) if isinstance(row["body"], str) else row["body"]
        cfg = LLMProviderConfig.model_validate(dict(body["config"]), strict=False)
        sub = infer_llm_subprovider(self.component_id, endpoint=cfg.api_endpoint.raw)
        handler_cls = LLM_HANDLERS[sub]
        self._handler = handler_cls()
        vault = CredentialVault.from_env()
        # from_env() builds an unconnected PostgresStore; the registry normally
        # shares an already-connected one.
        await vault.store.connect()
        self._vault = vault

        async def _resolve(secret_id: str) -> bytes:
            return await vault.resolve(secret_id)

        ctx = _BuilderHandlerContext(
            instance_id=self.component_id,
            instance_version=str(row["version"] or ""),
            config=cfg,
            secrets=_SecretsResolverAdapter(_resolve),
        )
        await self._handler.on_configure(ctx)
        self._model_name = cfg.model_name.raw

    async def complete(
        self, system: str, user: str, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        resp = await self._handler.chat_complete(
            [{"role": "user", "content": user}],
            system=system,
            max_tokens=max_tokens,
            temperature=TEMPERATURE,
        )
        u = getattr(resp, "usage", None)
        return (getattr(resp, "content", "") or ""), {
            "prompt_tokens": getattr(u, "prompt_tokens", 0) if u else 0,
            "completion_tokens": getattr(u, "completion_tokens", 0) if u else 0,
            "reasoning_tokens": getattr(u, "reasoning_tokens", 0) if u else 0,
        }

    async def close(self) -> None:
        if self._handler is not None:
            try:
                await self._handler.on_deactivate(None)  # type: ignore[arg-type]
            except Exception:
                pass
        if self._vault is not None:
            try:
                await self._vault.store.disconnect()
            except Exception:
                pass


class OpenRouterTransport:
    """Plain OpenAI-compatible client for OpenRouter."""

    def __init__(self, model_id: str, extra_body: dict[str, Any] | None = None) -> None:
        self.model_id = model_id
        self.extra_body = dict(extra_body or {})
        key = os.environ.get("OPENROUTER_API_KEY", "")
        if not key:
            raise SystemExit("OPENROUTER_API_KEY unset")
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(300.0, connect=30.0),
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                # OpenRouter attribution headers — identify the caller.
                "HTTP-Referer": "https://github.com/ldgeorge85/Legba",
                "X-Title": "Legba K-G2 typing bake-off",
            },
        )

    async def start(self) -> None:
        return None

    async def complete(
        self, system: str, user: str, max_tokens: int
    ) -> tuple[str, dict[str, int]]:
        payload = {
            "model": self.model_id,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "max_tokens": max_tokens,
            "temperature": TEMPERATURE,
            **self.extra_body,
        }
        r = await self._client.post(OPENROUTER_BASE, json=payload)
        if r.status_code != 200:
            raise TransportError(r.status_code, r.text[:600])
        data = r.json()
        if "error" in data and not data.get("choices"):
            raise TransportError(502, json.dumps(data["error"])[:600])
        choice = (data.get("choices") or [{}])[0]
        msg = choice.get("message") or {}
        content = msg.get("content") or ""
        if not content and msg.get("reasoning"):
            # A reasoning model that spent its whole budget thinking.
            content = ""
        usage = data.get("usage") or {}
        details = usage.get("completion_tokens_details") or {}
        return content, {
            "prompt_tokens": int(usage.get("prompt_tokens") or 0),
            "completion_tokens": int(usage.get("completion_tokens") or 0),
            "reasoning_tokens": int(details.get("reasoning_tokens") or 0),
        }

    async def close(self) -> None:
        await self._client.aclose()


class TransportError(RuntimeError):
    def __init__(self, status: int, body: str) -> None:
        super().__init__(f"HTTP {status}: {body}")
        self.status = status
        self.body = body


def make_transport(spec: ModelSpec):
    if spec.transport == "stack":
        return StackTransport(spec.component_id)
    return OpenRouterTransport(spec.model_id, spec.extra_body)


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


@dataclass
class CallRecord:
    batch_index: int
    n_requested: int
    n_recovered: int
    missing_idx: list[int] = field(default_factory=list)
    unexpected_idx: list[int] = field(default_factory=list)
    duplicate_idx: list[int] = field(default_factory=list)
    truncated: bool = False
    parse_ok: bool = False
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    wall_seconds: float = 0.0
    attempts: int = 1
    error: str | None = None


def to_batch_candidates(payloads: Sequence[dict], evidence_chars: int) -> list[BatchCandidate]:
    return [
        BatchCandidate(
            idx=int(p["idx"]),
            source=p["source_entity"],
            target=p["target_entity"],
            evidence_text=(p.get("evidence_text") or "")[:evidence_chars],
            facts=p.get("facts") or (),
            intermediaries=tuple(p.get("intermediaries") or ()),
            ref=p["id"],
        )
        for p in payloads
    ]


async def run_model(
    spec: ModelSpec,
    payloads: list[dict],
    *,
    batch_size: int,
    evidence_chars: int,
    outdir: Path,
    tag: str,
) -> dict[str, Any]:
    transport = make_transport(spec)
    await transport.start()
    gate_text = {int(p["idx"]): p.get("sports_gate_text") or "" for p in payloads}

    calls: list[CallRecord] = []
    all_verdicts: list[dict] = []
    raw_path = outdir / f"raw_{spec.key}_{tag}.jsonl"
    raw_fh = raw_path.open("w")
    t_model = time.monotonic()
    aborted: str | None = None

    batches = [
        payloads[i : i + batch_size] for i in range(0, len(payloads), batch_size)
    ]
    for bi, chunk in enumerate(batches):
        cands = to_batch_candidates(chunk, evidence_chars)
        user = build_batch_user_prompt(cands, evidence_chars=evidence_chars)
        budget = int(max_tokens_for_batch(len(cands)) * spec.token_multiplier)
        rec = CallRecord(batch_index=bi, n_requested=len(cands), n_recovered=0)

        raw = ""
        usage: dict[str, int] = {}
        t0 = time.monotonic()
        for attempt in range(1, spec.max_retries + 1):
            rec.attempts = attempt
            try:
                raw, usage = await transport.complete(
                    BATCH_SYSTEM_PROMPT, user, budget
                )
                rec.error = None
                break
            except TransportError as exc:
                rec.error = str(exc)[:400]
                retriable = exc.status in (408, 429, 500, 502, 503, 504)
                if not retriable or attempt == spec.max_retries:
                    break
                backoff = min(90.0, (2 ** attempt) * 3.0) + random.uniform(0, 2)
                print(f"  [{spec.key}] HTTP {exc.status}, backoff {backoff:.0f}s "
                      f"(attempt {attempt})", file=sys.stderr)
                await asyncio.sleep(backoff)
            except Exception as exc:  # transport/network
                rec.error = f"{type(exc).__name__}: {exc}"[:400]
                if attempt == spec.max_retries:
                    break
                await asyncio.sleep(min(60.0, (2 ** attempt) * 2.0))
        rec.wall_seconds = time.monotonic() - t0

        if rec.error and not raw:
            calls.append(rec)
            raw_fh.write(json.dumps({"batch": bi, "error": rec.error}) + "\n")
            # A hard daily-cap / auth failure will repeat on every remaining
            # batch — stop and let the scorer intersect on what completed.
            if "429" in rec.error or "402" in rec.error or "401" in rec.error:
                aborted = rec.error
                print(f"  [{spec.key}] ABORT after batch {bi}: {rec.error[:160]}",
                      file=sys.stderr)
                break
            continue

        parsed = parse_batch_response(raw, cands, sports_gate_text=gate_text)
        rec.n_recovered = parsed.recovered
        rec.missing_idx = parsed.missing_idx
        rec.unexpected_idx = parsed.unexpected_idx
        rec.duplicate_idx = parsed.duplicate_idx
        rec.truncated = parsed.truncated
        rec.parse_ok = parsed.parse_ok
        rec.prompt_tokens = usage.get("prompt_tokens", 0)
        rec.completion_tokens = usage.get("completion_tokens", 0)
        rec.reasoning_tokens = usage.get("reasoning_tokens", 0)
        calls.append(rec)

        raw_fh.write(json.dumps({
            "batch": bi, "idx": [c.idx for c in cands],
            "usage": usage, "raw": raw,
        }) + "\n")

        for v in parsed.verdicts:
            all_verdicts.append({
                "idx": v.idx, "id": v.ref,
                "source_entity": v.source, "target_entity": v.target,
                "accepted": v.accepted, "rel_type": v.rel_type,
                "polarity": v.polarity, "intent": v.intent,
                "channel": v.channel, "intermediary": v.intermediary,
                "confidence": v.confidence, "rationale": v.rationale,
                "reject_reason": v.reject_reason,
            })

        done = sum(c.n_requested for c in calls)
        print(f"  [{spec.key}] batch {bi+1}/{len(batches)} "
              f"recovered {parsed.recovered}/{len(cands)} "
              f"{'OK' if parsed.parse_ok else 'DEGRADED'} "
              f"{rec.wall_seconds:.1f}s  ({done}/{len(payloads)})",
              file=sys.stderr)
        if spec.pace_seconds and bi + 1 < len(batches):
            await asyncio.sleep(spec.pace_seconds)

    raw_fh.close()
    await transport.close()

    wall = time.monotonic() - t_model
    pt = sum(c.prompt_tokens for c in calls)
    ct = sum(c.completion_tokens for c in calls)
    rt = sum(c.reasoning_tokens for c in calls)
    requested = sum(c.n_requested for c in calls)
    recovered = sum(c.n_recovered for c in calls)
    ok_calls = sum(1 for c in calls if c.parse_ok)
    cost = (pt / 1e6) * spec.price_prompt_musd + (ct / 1e6) * spec.price_completion_musd

    summary = {
        "model": spec.key,
        "label": spec.label,
        "transport": spec.transport,
        "model_id": spec.model_id or spec.component_id,
        "tag": tag,
        "batch_size": batch_size,
        "evidence_chars": evidence_chars,
        "candidates_requested": requested,
        "verdicts_recovered": recovered,
        "recovery_rate": round(recovered / requested, 4) if requested else 0.0,
        "calls": len(calls),
        "calls_parse_ok": ok_calls,
        "call_parse_ok_rate": round(ok_calls / len(calls), 4) if calls else 0.0,
        "calls_truncated": sum(1 for c in calls if c.truncated),
        "calls_errored": sum(1 for c in calls if c.error),
        "prompt_tokens": pt,
        "completion_tokens": ct,
        "reasoning_tokens": rt,
        "total_tokens": pt + ct,
        "wall_seconds": round(wall, 1),
        "wall_per_edge": round(wall / recovered, 3) if recovered else None,
        "tokens_per_edge": round((pt + ct) / recovered, 1) if recovered else None,
        "usd_cost": round(cost, 6),
        "usd_per_1k_edges": round(cost / recovered * 1000, 4) if recovered else None,
        "accepted": sum(1 for v in all_verdicts if v["accepted"]),
        "rejected": sum(1 for v in all_verdicts if not v["accepted"]),
        "aborted": aborted,
    }
    (outdir / f"summary_{spec.key}_{tag}.json").write_text(json.dumps(summary, indent=2))
    (outdir / f"verdicts_{spec.key}_{tag}.json").write_text(
        json.dumps(all_verdicts, indent=1)
    )
    (outdir / f"calls_{spec.key}_{tag}.json").write_text(
        json.dumps([asdict(c) for c in calls], indent=1)
    )
    print(json.dumps(summary, indent=2), file=sys.stderr)
    return summary


async def amain() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--models", default="core120b,slm8b,nemotron,gptoss")
    ap.add_argument("--mode", choices=["main", "sweep"], default="main")
    ap.add_argument("--batch-size", type=int, default=12)
    ap.add_argument("--sweep-sizes", default="4,8,12,16,24")
    ap.add_argument("--sweep-n", type=int, default=48,
                    help="head-slice size used for the N-sweep")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--evidence-chars", type=int, default=DEFAULT_EVIDENCE_CHARS)
    ap.add_argument(
        "--interleave", action="store_true",
        help="round-robin the sample across strata so each BATCH is mixed. "
             "The sample is stratum-ordered on disk, which makes every batch "
             "homogeneous — a batch of all-junk candidates could bias a model "
             "toward rejecting (and vice versa). This is the control for that "
             "confound: verdicts stay keyed by idx, so results remain "
             "directly comparable to a non-interleaved run.",
    )
    args = ap.parse_args()

    d = Path(args.dir)
    payloads = json.loads((d / "sample_payloads.json").read_text())
    if args.interleave:
        buckets: dict[str, list[dict]] = {}
        for p in payloads:
            buckets.setdefault(p["stratum"], []).append(p)
        order = sorted(buckets)
        mixed: list[dict] = []
        for i in range(max(len(b) for b in buckets.values())):
            for s in order:
                if i < len(buckets[s]):
                    mixed.append(buckets[s][i])
        payloads = mixed
    if args.limit:
        payloads = payloads[: args.limit]
    outdir = d / "results"
    outdir.mkdir(parents=True, exist_ok=True)

    keys = [k.strip() for k in args.models.split(",") if k.strip()]
    for k in keys:
        if k not in ROSTER:
            raise SystemExit(f"unknown model {k!r}; known: {sorted(ROSTER)}")

    if args.mode == "sweep":
        slice_ = payloads[: args.sweep_n]
        for k in keys:
            for n in [int(x) for x in args.sweep_sizes.split(",")]:
                print(f"== sweep {k} N={n} ==", file=sys.stderr)
                try:
                    await run_model(ROSTER[k], slice_, batch_size=n,
                                    evidence_chars=args.evidence_chars,
                                    outdir=outdir, tag=f"sweepN{n}")
                except Exception as exc:
                    print(f"!! sweep {k} N={n} failed: {exc}", file=sys.stderr)
        return 0

    tag = "interleaved" if args.interleave else "main"
    for k in keys:
        print(f"== {tag} {k} N={args.batch_size} ==", file=sys.stderr)
        try:
            await run_model(ROSTER[k], payloads, batch_size=args.batch_size,
                            evidence_chars=args.evidence_chars,
                            outdir=outdir, tag=tag)
        except Exception as exc:
            print(f"!! main {k} failed: {type(exc).__name__}: {exc}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(amain()))

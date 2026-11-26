#!/usr/bin/env python3
# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""CUTOVER 2026-10-22 — move every model Legba calls from the self-hosted models
host to the model gateway.

HISTORICAL: this ran once. Do NOT re-run it with ``--apply`` on a live registry —
step 5 would point claim_watch's bearing gate back at the chat judge (the gate
moved to the decision model on 2026-11-05) and steps 2-3 would write new heads.

Dry-run by default: every step PROBES and PRINTS what it would change and changes
nothing. ``--apply`` performs the registry writes, in this order:

  0. gateway liveness — /models, one tiny chat on ``fast``, one embedding on
     ``embed-legacy`` (dim must be 1024). A dead backend stops the run here.
  1. the vault secret ``llm.gateway.api_key`` ← $LEGBA_GATEWAY_API_TOKEN (POST
     /vault/secrets; skipped when it already exists; the plaintext is never printed).
  2. ``llm.primary.openai_compat`` — a NEW HEAD of the same component id (99
     descriptor heads reference the id, none is edited): endpoint = the gateway,
     model = ``fast`` (the core chat model, thinking off), key = the vault ref.
  3. ``embed.primary.openai_compat`` — a new head: the gateway, ``embed-legacy``
     (bge-m3, 1024-d, the collection's dimension).
  4. the three gateway components from descriptors/ (Gemma judge, Nemotron think
     judge rung 2, Nemotron grader) — POST if absent, then PUT state=active.
  5. ``claim_watch``'s bearing gate → the Gemma component (one option on a new
     descriptor head, validated through AnalystDescriptor BEFORE the PUT).
  6. healthchecks on all five components.
  7. the .env block to apply by hand and the restart commands (printed, never written).

Every body is validated through the runtime schema (LLMProvider / EmbeddingService /
AnalystDescriptor) before any PUT — a descriptor that does not parse on the runtime
is a dead analyst, not a config change.

Usage (from the repo root, with the live env loaded and the runtime STOPPED):
    set -a; . ./.env; set +a
    python3 scripts/cutover_gateway.py            # dry-run: probes + the plan
    python3 scripts/cutover_gateway.py --apply    # the writes
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import sys
from pathlib import Path
from typing import Any

import httpx
import yaml

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent / "src"))
from _bringup_http import registry_base, registry_client  # noqa: E402
from _token import resolve_token  # noqa: E402

from legba.data.schemas.analyst import AnalystDescriptor  # noqa: E402
from legba.data.schemas.env_refs import resolve_env_value  # noqa: E402
from legba.data.schemas.stack import EmbeddingService, LLMProvider  # noqa: E402

#: The gateway base URL: LEGBA_GATEWAY_API_ENDPOINT, else the core plane's own
#: LEGBA_LLM_API_ENDPOINT (the same gateway since the cutover).
GATEWAY = (os.getenv("LEGBA_GATEWAY_API_ENDPOINT") or os.getenv("LEGBA_LLM_API_ENDPOINT", "")).rstrip("/")
SECRET_ID = "llm.gateway.api_key"
CORE_ID = "llm.primary.openai_compat"
EMBED_ID = "embed.primary.openai_compat"
JUDGE_ID = "llm.verify.gateway_judge.openai_compat"
JUDGE_RUNG2_ID = "llm.judge.gateway_rung2_think.openai_compat"
GRADER_ID = "llm.grader.gateway_third_family.openai_compat"
NEW_COMPONENT_FILES = {
    JUDGE_ID: "stack_component_llm_verify_gateway_judge.yaml",
    JUDGE_RUNG2_ID: "stack_component_llm_judge_gateway_rung2_think.yaml",
    GRADER_ID: "stack_component_llm_grader_gateway_third_family.yaml",
}
CORE_MODEL = "fast"
EMBED_MODEL = "embed-legacy"
EMBED_DIM = 1024

ENV_BLOCK = f"""
# ─── cutover 2026-10-22: models host → the model gateway ─────────────────────
LEGBA_LLM_API_ENDPOINT={GATEWAY}
LEGBA_LLM_MODEL_NAME={CORE_MODEL}
LEGBA_EMBED_MODEL_NAME={EMBED_MODEL}
LEGBA_JUDGE_STACK_REF={JUDGE_ID}
LEGBA_JUDGE_CHAIN_DEFAULT={JUDGE_RUNG2_ID}
LEGBA_JUDGE_WEAK_RUNGS=
LEGBA_CONSULT_JUDGE_STACK_REF={JUDGE_ID}
LEGBA_EXTERNAL_GRADER_STACK_REF={GRADER_ID}
# LEGBA_LLM_SEND_MAX_TOKENS stays UNSET: each alias's declared maximum output is sent, never a smaller number.
# LEGBA_NLP_API_ENDPOINT stays on the models host until legba-models is re-hosted (see the runbook).
"""


def say(msg: str) -> None:
    print(msg, flush=True)


# ── step 0: the gateway ─────────────────────────────────────────────────────
def probe_gateway(token: str) -> bool:
    ok = True
    h = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    with httpx.Client(timeout=90) as c:
        r = c.get(f"{GATEWAY}/models", headers=h)
        ids = sorted(m["id"] for m in r.json().get("data", [])) if r.status_code == 200 else []
        say(f"[0] /models http {r.status_code}: {ids}")
        served = {resolve_env_value(
                      yaml.safe_load((HERE.parent / "descriptors" / f).read_text())["config"]["model_name"]["raw"],
                      component_id=cid, field="model_name")
                  for cid, f in NEW_COMPONENT_FILES.items()}
        ok &= r.status_code == 200 and {CORE_MODEL, EMBED_MODEL} | served <= set(ids)
        r = c.post(f"{GATEWAY}/chat/completions", headers=h, json={
            "model": CORE_MODEL, "temperature": 1.0,
            "messages": [{"role": "user", "content": "Reply with the single word OK."}]})
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        text = (body.get("choices") or [{}])[0].get("message", {}).get("content", "") if r.status_code == 200 else body
        say(f"[0] chat {CORE_MODEL} http {r.status_code}: {str(text)[:80]!r}")
        ok &= r.status_code == 200
        r = c.post(f"{GATEWAY}/embeddings", headers=h, json={"model": EMBED_MODEL, "input": ["probe"]})
        dim = len(r.json()["data"][0]["embedding"]) if r.status_code == 200 else None
        say(f"[0] embeddings {EMBED_MODEL} http {r.status_code}: dim {dim}")
        ok &= dim == EMBED_DIM
    return ok


# ── helpers ─────────────────────────────────────────────────────────────────
def get_component(c: httpx.Client, cid: str) -> dict[str, Any] | None:
    r = c.get(f"/stack/{cid}")
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.json()


def put_component(c: httpx.Client, cid: str, body: dict[str, Any], apply: bool) -> None:
    if not apply:
        say(f"    would PUT /stack/{cid} (state={body.get('state')}, model={body['config'].get('model_name', {}).get('raw')})")
        return
    r = c.put(f"/stack/{cid}", json=body)
    if r.status_code == 409:
        say(f"    PUT /stack/{cid}: 409 {r.text[:120]} (already this content)")
        return
    r.raise_for_status()
    say(f"    PUT /stack/{cid} → version {r.json()['version'][:12]} state={r.json()['state']}")


def new_head(row: dict[str, Any], **changes: Any) -> dict[str, Any]:
    body = copy.deepcopy(row["body"])
    body["version"] = "0" * 16
    for dotted, value in changes.items():
        cur = body
        parts = dotted.split("__")
        for p in parts[:-1]:
            cur = cur.setdefault(p, {})
        if value is None:
            cur.pop(parts[-1], None)
        else:
            cur[parts[-1]] = value
    return body


# ── main ────────────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="perform the registry writes (default: dry-run)")
    ap.add_argument("--skip-gateway-probe", action="store_true", help="plan without the live gateway (never with --apply)")
    args = ap.parse_args()
    if args.apply and args.skip_gateway_probe:
        ap.error("--apply needs the live gateway probe")

    gw_token = os.getenv("LEGBA_GATEWAY_API_TOKEN", "").strip()
    if not gw_token:
        say("LEGBA_GATEWAY_API_TOKEN is not in the environment (set -a; . ./.env; set +a)")
        return 2
    if not GATEWAY:
        say("neither LEGBA_GATEWAY_API_ENDPOINT nor LEGBA_LLM_API_ENDPOINT is set")
        return 2
    mode = "APPLY" if args.apply else "DRY-RUN"
    say(f"== cutover to {GATEWAY} [{mode}]")

    if not args.skip_gateway_probe:
        if not probe_gateway(gw_token):
            say("[0] the gateway is not serving what the cutover needs — stopping here")
            return 1
    else:
        say("[0] gateway probe SKIPPED")

    base, token = registry_base(), resolve_token("operator")  # the vault + descriptor writes are operator-only routes
    with registry_client(base, token, timeout=60) as c:
        # 1. the secret
        r = c.get(f"/vault/secrets/{SECRET_ID}/exists"); r.raise_for_status()
        if r.json().get("exists"):
            say(f"[1] vault secret {SECRET_ID}: exists (kept)")
        elif args.apply:
            r = c.post("/vault/secrets", json={"secret_id": SECRET_ID, "plaintext": gw_token,
                                               "notes": "model gateway token (cutover 2026-10-22)"})
            r.raise_for_status()
            say(f"[1] vault secret {SECRET_ID}: registered v{r.json()['version']}")
        else:
            say(f"[1] vault secret {SECRET_ID}: would POST")

        # 2. the core plane
        core = get_component(c, CORE_ID)
        if core is None:
            say(f"[2] {CORE_ID} missing — this registry is not the live one"); return 1
        say(f"[2] {CORE_ID}: head v{core['version'][:12]} model={core['body']['config']['model_name']['raw']}")
        body = new_head(core,
                        name=f"`{CORE_MODEL}` via the model gateway (core plane; was gpt-oss-120b, self-hosted)",
                        config__api_endpoint__raw=GATEWAY,
                        config__model_name__raw=CORE_MODEL,
                        config__api_key__raw=SECRET_ID,
                        config__price_input_per_m=None, config__price_output_per_m=None)
        LLMProvider.model_validate(body)
        put_component(c, CORE_ID, body, args.apply)

        # 3. the embedder
        emb = get_component(c, EMBED_ID)
        if emb is None:
            say(f"[3] {EMBED_ID} missing"); return 1
        say(f"[3] {EMBED_ID}: head v{emb['version'][:12]} model={emb['body']['config']['model_name']['raw']} dim={emb['body']['config']['dim']['raw']}")
        body = new_head(emb,
                        name=f"bge-m3 `{EMBED_MODEL}` via the model gateway (1024-d; was self-hosted)",
                        config__endpoint__raw=GATEWAY,
                        config__model_name__raw=EMBED_MODEL,
                        config__api_key__raw=SECRET_ID,
                        config__dim__raw=EMBED_DIM)
        EmbeddingService.model_validate(body)
        put_component(c, EMBED_ID, body, args.apply)

        # 4. the three new components
        for cid, fname in NEW_COMPONENT_FILES.items():
            yb = yaml.safe_load((HERE.parent / "descriptors" / fname).read_text())
            LLMProvider.model_validate(yb)
            row = get_component(c, cid)
            if row is None:
                if args.apply:
                    r = c.post("/stack", json=yb); r.raise_for_status()
                    say(f"[4] {cid}: registered (draft)")
                    row = get_component(c, cid)
                else:
                    say(f"[4] {cid}: would POST (draft) then PUT state=active"); continue
            else:
                say(f"[4] {cid}: present (state={row['state']})")
            if row is not None and row["state"] != "active":
                body = new_head(row, state="active")
                LLMProvider.model_validate(body)
                put_component(c, cid, body, args.apply)

        # 5. claim_watch's bearing gate
        r = c.get("/descriptors/analyst/claim_watch"); r.raise_for_status()
        cw = r.json()
        opts = (cw["body"].get("method") or {}).get("options") or {}
        say(f"[5] claim_watch head v{cw['version'][:12]}: bearing_gate_ref={opts.get('bearing_gate_ref')!r}")
        if opts.get("bearing_gate_ref") != JUDGE_ID:
            body = copy.deepcopy(cw["body"])
            body.setdefault("method", {}).setdefault("options", {})["bearing_gate_ref"] = JUDGE_ID
            AnalystDescriptor.model_validate(body)   # parse on the runtime schema FIRST
            if args.apply:
                r = c.put("/descriptors/analyst/claim_watch", json=body); r.raise_for_status()
                say(f"[5] claim_watch → new head v{r.json()['version'][:12]} (bearing_gate_ref={JUDGE_ID})")
            else:
                say(f"[5] would PUT claim_watch with method.options.bearing_gate_ref={JUDGE_ID} (validated)")

        # 6. healthchecks
        if args.apply:
            for cid in (CORE_ID, EMBED_ID, JUDGE_ID, JUDGE_RUNG2_ID, GRADER_ID):
                r = c.post(f"/stack/{cid}/healthcheck")
                st = r.json() if r.status_code == 200 else {"state": f"http {r.status_code}"}
                say(f"[6] {cid}: {st.get('state')} {str(st.get('detail', ''))[:100]}")
        else:
            say("[6] healthchecks run after --apply")

    say("[7] .env block to apply by hand, then restart the runtime and the worker:")
    say(ENV_BLOCK)
    say("    docker compose -p legba up -d legba-runtime-dapr && sleep 20 && docker compose -p legba up -d legba-dapr-workflow-worker")
    say("    rm -f /etc/legba-watchdog.disabled   # when the first tick is clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())

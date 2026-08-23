# SPDX-FileCopyrightText: 2026 Lewis George
# SPDX-License-Identifier: AGPL-3.0-or-later
"""One-shot stack-component registrar for bring-up.

Registers the substrate-component descriptors needed for the Brazil
spike to ingest. Idempotent: components that already exist are reported
as 'already_registered' rather than mutated.
"""
from __future__ import annotations

import os
import sys
import httpx

import sys
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from _bringup_http import registry_base, registry_client  # noqa: E402
from _token import resolve_token  # noqa: E402


BASE = registry_base()
TOKEN = resolve_token()

# Model-serving endpoints + served model names are deployment config, not
# committed values. The defaults below are neutral placeholders; a real
# deployment sets these in its (gitignored) .env. The LLM and embedding model
# share one OpenAI-compatible vLLM endpoint; the NLP service is separate.
LLM_API_ENDPOINT = os.environ.get("LEGBA_LLM_API_ENDPOINT", "https://llm.example.internal")
NLP_API_ENDPOINT = os.environ.get("LEGBA_NLP_API_ENDPOINT", "https://nlp.example.internal")
LLM_MODEL_NAME = os.environ.get("LEGBA_LLM_MODEL_NAME", "gpt-oss-120b")
EMBED_MODEL_NAME = os.environ.get("LEGBA_EMBED_MODEL_NAME", "bge-m3")
# The Anthropic critic/consult model. Was a HARDCODED dead id
# (`claude-sonnet-4-20250514`) which Anthropic now 404s — every critic grade
# hard-failed for ~5 days (no trace, no critique). Make it env-overridable like
# the primary, with a CURRENT valid default. Heterogeneous to the OpenAI-compat
# primary plane (the critic's whole point); bump to an Opus id via env for a
# stronger judge. Keep this a live, valid Anthropic model id.
# 2026-06-24: operator bumped the whole Anthropic plane (consult + deep_consult
# + country_assessor + critics, all sharing the `llm.anthropic.opus_4_7`
# component id) to Opus 4.8 — the strongest judge. Default reflects that; still
# env-overridable. (Component id kept as `opus_4_7` to avoid re-pointing 6
# descriptors; the id is just a label — the model_name below is authoritative.)
CONSULT_MODEL_NAME = os.environ.get("LEGBA_CONSULT_MODEL_NAME", "claude-opus-4-8")


def _t(s: str) -> dict:
    return {"factory_kind": "text", "raw": s}


def _n(n: float | int) -> dict:
    return {"factory_kind": "number", "raw": n}


def _s(secret_id: str) -> dict:
    return {"factory_kind": "secret", "raw": secret_id}


def _list(items: list[str]) -> dict:
    return {"factory_kind": "list", "raw": items, "item_kind": "text"}


def _dd(default: str, options: list[str]) -> dict:
    return {"factory_kind": "dropdown_static", "raw": default, "options": options}


# Each entry: (component_id, body-without-version-stamp).
COMPONENTS: list[tuple[str, dict]] = [
    (
        "llm.primary.openai_compat",
        {
            "id": "llm.primary.openai_compat",
            "name": "gpt-oss-120b (OpenAI-compatible)",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                # Note: store the base host only. The LLM handler's
                # _chat_endpoint_path() prepends `/v1/...`; trailing `/v1`
                # would double up (`/v1/v1/...` → 404).
                "api_endpoint": _t(LLM_API_ENDPOINT),
                "api_key": _s("llm.primary.api_key"),
                "model_name": _t(LLM_MODEL_NAME),
                "max_tokens": _n(16384),
                "tier": _dd("primary", ["primary", "fallback", "cheap"]),
                # Client-side concurrency cap (#21 headroom): at most 12
                # in-flight completions from one process against the primary
                # plane. Self-hosted, so NO price_* fields — receipts cost
                # $0.00, the correct posture for our own GPUs.
                "max_concurrent": _n(12),
            },
        },
    ),
    (
        # Cross-family verify judge, Cerebras PAYG lane (registered live
        # 2026-07-30; tree payload added with #22 so the config — and now its
        # PRICING — has a reviewable home). Values mirror the live head row
        # as of 2026-08-15.
        "llm.judge.cerebras_gemma4_31b.openai_compat",
        {
            "id": "llm.judge.cerebras_gemma4_31b.openai_compat",
            "name": "Cerebras gemma-4-31b (cross-family verify judge, PAYG)",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "api_endpoint": _t("https://api.cerebras.ai"),
                "api_key": _s("llm.judge.cerebras.api_key"),
                "model_name": _t("gemma-4-31b"),
                "max_tokens": _n(16384),
                "timeout_seconds": _n(90),
                "tier": _dd("fallback", ["primary", "fallback", "cheap"]),
                # #22 spend metering — Cerebras list price for gemma-4-31b,
                # USD per 1M tokens, verified 2026-08-15 against two
                # independent trackers (artificialanalysis.ai, llmgateway.io).
                # Re-verify at cerebras.ai/pricing before trusting a burn
                # number across a provider price change.
                "price_input_per_m": _n(0.99),
                "price_output_per_m": _n(1.49),
                # Daily page ceiling for the llm_daily_burn gauge. Measured
                # judge volume 2026-08-15: ~9.4M in + 0.11M out tokens over
                # 7 days ≈ $1.4/day average, ~$4/day at full ~1,100-call
                # tilt — $10 pages on runaway, never on normal.
                "daily_burn_alert_usd": _n(10.0),
            },
        },
    ),
    (
        # Cross-family judge, OpenRouter FREE lane (Nemotron-3-super). The
        # `:free` model id is $0 by contract, so prices are pinned to 0 —
        # explicit zeros, not absent, so the day a PAID Nemotron lane
        # replaces this one the reviewer finds the two fields already
        # sitting here waiting for OpenRouter's listed per-1M numbers (and a
        # daily_burn_alert_usd alongside them). Mirrors the live head row.
        "llm.judge.nemotron3_super.openai_compat",
        {
            "id": "llm.judge.nemotron3_super.openai_compat",
            "name": "OpenRouter Nemotron-3-super-120b (cross-family judge, free lane)",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "api_endpoint": _t("https://openrouter.ai/api"),
                "api_key": _s("llm.judge.openrouter.api_key"),
                "model_name": _t("nvidia/nemotron-3-super-120b-a12b:free"),
                "max_tokens": _n(16384),
                "timeout_seconds": _n(120),
                "tier": _dd("fallback", ["primary", "fallback", "cheap"]),
                "price_input_per_m": _n(0),
                "price_output_per_m": _n(0),
                # No daily_burn_alert_usd: a $0 lane cannot burn, and absent
                # = never pages. Set it WITH the prices if a paid lane lands.
            },
        },
    ),
    (
        # OpenRouter Nemotron-3-ultra free lane (the 550B; endpoint 404s as
        # of 2026-08-15 but the component is live-registered for the 2x3
        # judge matrix). Same $0 pinning and the same where-the-numbers-go
        # note as the super lane above.
        "llm.judge.nemotron3_ultra.openai_compat",
        {
            "id": "llm.judge.nemotron3_ultra.openai_compat",
            "name": "OpenRouter Nemotron-3-ultra-550b (judge matrix, free lane)",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "api_endpoint": _t("https://openrouter.ai/api"),
                "api_key": _s("llm.judge.openrouter.api_key"),
                "model_name": _t("nvidia/nemotron-3-ultra-550b-a55b:free"),
                "max_tokens": _n(16384),
                "timeout_seconds": _n(120),
                "tier": _dd("fallback", ["primary", "fallback", "cheap"]),
                "price_input_per_m": _n(0),
                "price_output_per_m": _n(0),
            },
        },
    ),
    (
        "llm.anthropic.opus_4_7",
        {
            "id": "llm.anthropic.opus_4_7",
            "name": "Anthropic Claude Opus 4.8 (heterogeneous critic/consult judge)",
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                # Base host only — `_chat_endpoint_path` prepends `/v1/...`.
                "api_endpoint": _t("https://api.anthropic.com"),
                "api_key": _s("llm.anthropic.api_key"),
                "model_name": _t(CONSULT_MODEL_NAME),
                "max_tokens": _n(8192),
                # 2026-08-15: raised from the 60s schema default for the 32k
                # streamed consult budget. The handler streams every
                # generation, so httpx applies this per CHUNK (a live stream
                # resets it with every delta) — this is the stall ceiling,
                # sized for the worst prefill gap before message_start on a
                # long cached transcript, NOT a generation wall-time cap.
                "timeout_seconds": _n(300),
                "tier": _dd("fallback", ["primary", "fallback", "cheap"]),
            },
        },
    ),
    (
        # J1 (2026-08-15, FORWARD_PLAN §1) — the CROSS-FAMILY verify judge:
        # NVIDIA Nemotron 3 Super 120B A12B via OpenRouter's free tier. An
        # NVIDIA judge over the OpenAI-derived gpt-oss-120b producer plane
        # preserves the independence property the judge plane exists for.
        # Selected via the judge-route ladder rung 1
        # (LEGBA_JUDGE_STACK_REF=llm.judge.openrouter_nemotron120b.openai_compat);
        # registering it changes NOTHING until that env line lands.
        #
        # NAMING IS LOAD-BEARING: the id ENDS with `.openai_compat` so
        # infer_llm_subprovider routes it to the vLLM handler (OpenAI
        # Chat-Completions + Bearer — the wire shape OpenRouter speaks). Do
        # NOT rename it to contain `.openai.` — that routes to the OpenAI
        # handler, which rewrites max_tokens and injects reasoning_effort.
        #
        # The handler's _chat_endpoint_path() prepends `/v1/...` and the base
        # client strips a trailing `/v1` defensively, so this endpoint
        # normalizes to https://openrouter.ai/api on the wire. max_tokens
        # feeds the BudgetEnforcer estimate only — the vLLM handler does not
        # put it on the wire unless the caller opts in (send_max_tokens; the
        # opt-in exists if the free lane needs a cap). FREE-TIER LIMITS:
        # 20 RPM, ~50 requests/day (~1000/day after a $10 lifetime credit) —
        # the reason the verify path samples (J2). The key is the vault ref
        # loaded by bringup_vault_load.py from .env OPENROUTER_API_KEY.
        "llm.judge.openrouter_nemotron120b.openai_compat",
        {
            "id": "llm.judge.openrouter_nemotron120b.openai_compat",
            "name": (
                "NVIDIA Nemotron 3 Super 120B A12B "
                "(OpenRouter free — cross-family verify judge)"
            ),
            "schema_uri": "legba/stack/llm_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "api_endpoint": _t("https://openrouter.ai/api/v1"),
                "api_key": _s("llm.judge.openrouter.api_key"),
                "model_name": _t("nvidia/nemotron-3-super-120b-a12b:free"),
                "max_tokens": _n(16384),
                "timeout_seconds": _n(120),
                "tier": _dd("cheap", ["primary", "fallback", "cheap"]),
            },
        },
    ),
    (
        # Phase-4 architectural-correction (2026-05-22). The hosted vLLM
        # endpoint also serves the OpenAI-compatible /v1/embeddings route
        # against `bge-m3` (1024-dim) — same vLLM box, separate
        # smaller model. Reuses the same vault entry as the LLM provider
        # since auth is identical. Replaces the retired
        # `embed.local.bge_m3` stack component (L-205 reshape — the in-
        # process BGE-M3 + sentence-transformers path lived in
        # `src/legba/data/stack/embedding/bge_m3.py`).
        "embed.primary.openai_compat",
        {
            "id": "embed.primary.openai_compat",
            "name": "bge-m3 (vLLM /v1/embeddings)",
            "schema_uri": "legba/stack/embedding/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "endpoint": _t(LLM_API_ENDPOINT),
                "api_key": _s("llm.primary.api_key"),
                "model_name": _t(EMBED_MODEL_NAME),
                # Honor the same env the runtime reads (config.py signals_dim)
                # so a clone with a different embedder isn't silently 1024.
                "dim": _n(int(os.getenv("LEGBA_DATA_EMBED_DIM", "1024"))),
                "normalize": _dd("true", ["true", "false"]),
                "batch_size": _n(64),
            },
        },
    ),
    (
        # Phase-4 architectural-correction (2026-05-22). The hosted
        # Legba-models NLP service (translate / classify / extract /
        # summarize). The filter handlers (ner_multilingual, classify)
        # bind to this via Property.StackRef("nlp.local.legba_models").
        "nlp.local.legba_models",
        {
            "id": "nlp.local.legba_models",
            "name": "Legba-models NLP service (translate/classify/extract/summarize)",
            "schema_uri": "legba/stack/nlp_service/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "endpoint": _t(NLP_API_ENDPOINT),
                "api_user": _s("nlp.local.legba_models.api_user"),
                "api_pass": _s("nlp.local.legba_models.api_pass"),
                "timeout_seconds": _n(60),
                "translate_path": _t("/translate"),
                "classify_path": _t("/classify"),
                "extract_path": _t("/extract"),
                "summarize_path": _t("/summarize"),
                "health_path": _t("/health"),
            },
        },
    ),
    (
        "vector.qdrant.cluster_main",
        {
            "id": "vector.qdrant.cluster_main",
            "name": "Qdrant cluster (main)",
            "schema_uri": "legba/stack/vector_store/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "endpoint": _t("http://qdrant:6333"),
                "collection_prefix": _t("legba_"),
                "default_dim": _n(1024),
                "default_metric": _dd("cosine", ["cosine", "dot", "euclid"]),
            },
        },
    ),
    (
        "nats.cluster_main",
        {
            "id": "nats.cluster_main",
            "name": "NATS cluster (main, JetStream)",
            "schema_uri": "legba/stack/nats/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "servers": _list(["nats://nats:4222"]),
                "jetstream": _dd("enabled", ["enabled", "disabled"]),
            },
        },
    ),
    (
        "pg.cluster_main",
        {
            "id": "pg.cluster_main",
            "name": "Postgres + AGE (main substrate)",
            "schema_uri": "legba/stack/postgres/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "host": _t("postgres"),
                "port": _n(5432),
                "database": _t("legba"),
                "user": _t("legba"),
                "password": _s("pg.cluster_main.password"),
                "extensions": _list(["age"]),
                "pool_size": _n(10),
            },
        },
    ),
    (
        "proxy.local.none",
        {
            "id": "proxy.local.none",
            "name": "Local no-op proxy (dev)",
            "schema_uri": "legba/stack/proxy_pool/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "provider": _dd("none",
                                ["none", "bright_data", "oxylabs", "self_managed"]),
                "rotation": _dd("session",
                                ["session", "request", "sticky_30s", "sticky_5m"]),
            },
        },
    ),
    (
        # The DISCOVERY leg. Registering this component does NOT activate
        # search — an analyst still has to grant `web_access` AND its target
        # has to allow it (the three-way agency gate), and the `web_search`
        # ToolSpec has to carry a `provider` StackRef pointing here. Registered
        # so the component exists to point at; INERT until both are done.
        #
        # Endpoint is the compose-network service name. Two operator steps this
        # will NOT work without, both of which fail LOUDLY rather than silently:
        #   1. `docker compose --profile search up -d searxng`
        #   2. `searxng` on LEGBA_EGRESS_ALLOW_HOSTS — the SSRF egress guard
        #      refuses RFC-1918 targets, so every query returns egress_blocked
        #      until the single exact hostname is permitted (never a subnet).
        # And SearXNG's JSON format is OFF by default: without
        # `search.formats: [html, json]` every query returns HTML and the
        # handler raises "search response not JSON".
        "search.searxng.local",
        {
            "id": "search.searxng.local",
            "name": "SearXNG metasearch (local)",
            "schema_uri": "legba/stack/search_provider/1.0.0",
            "state": "active",
            "owner": "lewis@local",
            "config": {
                "subprovider": _dd(
                    "searxng",
                    ["searxng", "json", "firecrawl", "jina", "tavily", "brave",
                     "agent"],
                ),
                "endpoint": _t(
                    os.environ.get(
                        "LEGBA_SEARXNG_ENDPOINT", "http://searxng:8080/search",
                    )
                ),
                "timeout_seconds": _n(15),
                "max_results": _n(10),
                # Empty = the instance's own configured engine set. WHICH
                # engines survive sustained automated use is an empirical
                # first-week question measured by the control-query canary —
                # not a value to guess in a registrar.
                "engines": _list([]),
                "categories": _list(["general", "news"]),
                "language": _t(""),
                "results_key": _t("results"),
                "query_param": _t("q"),
            },
        },
    ),
]


def _ensure_pg_password_secret(client: httpx.Client) -> None:
    """Ensure the Postgres password SecretRef points at a vault entry."""
    secret_id = "pg.cluster_main.password"
    r = client.get(f"/vault/secrets/{secret_id}/exists")
    r.raise_for_status()
    if r.json().get("exists"):
        return
    # Bootstrap pg creds are `legba` per docker-compose substrate.
    r = client.post(
        "/vault/secrets",
        json={"secret_id": secret_id, "plaintext": "legba",
              "notes": "bootstrap pg password (docker-compose default)"},
    )
    if r.status_code not in (200, 201):
        raise RuntimeError(f"could not seed {secret_id}: {r.status_code} {r.text}")


def _strip_version(body: dict) -> dict:
    # The registry stamps the content-hash; we don't pre-set the version.
    return {k: v for k, v in body.items() if k != "version"}


def main() -> int:
    with registry_client(BASE, TOKEN, timeout=20) as client:
        _ensure_pg_password_secret(client)
        # Discover already-registered components.
        r = client.get("/stack")
        r.raise_for_status()
        existing = {row["component_id"] for row in r.json()}

        registered: list[str] = []
        skipped: list[str] = []
        failures: list[str] = []
        for comp_id, body in COMPONENTS:
            if comp_id in existing:
                skipped.append(comp_id)
                continue
            payload = _strip_version(body)
            # The schema requires a 16-64 hex version; the registry overwrites
            # it but pydantic-strict still checks the pattern. Use 16 zeros.
            payload["version"] = "0" * 16
            r = client.post("/stack", json=payload)
            if r.status_code not in (200, 201):
                failures.append(f"{comp_id}: HTTP {r.status_code} {r.text[:300]}")
                continue
            registered.append(comp_id)

        # Healthcheck every active component.
        health: dict[str, str] = {}
        all_ids = registered + skipped
        for comp_id in all_ids:
            try:
                r = client.post(f"/stack/{comp_id}/healthcheck")
                if r.status_code == 200:
                    health[comp_id] = r.json().get("state", "?")
                else:
                    health[comp_id] = f"HTTP {r.status_code}"
            except Exception as exc:
                health[comp_id] = f"error: {exc}"

        print("Registered:")
        for s in registered:
            print(f"  + {s}")
        print("Skipped (already present):")
        for s in skipped:
            print(f"  = {s}")
        print("Healthcheck:")
        for s, state in sorted(health.items()):
            print(f"  ? {s}: {state}")
        if failures:
            print("Failures:")
            for s in failures:
                print(f"  ! {s}")
            return 1
        return 0


if __name__ == "__main__":
    sys.exit(main())

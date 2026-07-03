# AI Models

This document covers every AI model Legba uses: which models, where they are
served, what each one is for, and how they are configured. New here? Start with
the [README](../README.md) and the [Tour](TOUR.md).

**Three roles of AI model** carry the acquisition, analysis, and verification
work, and **none of them run inside the Legba containers** — every model is
reached over HTTP against out-of-process model-serving hosts, with credentials
and endpoints resolved through the stack registry and credential vault (see
`ARCHITECTURE.md`, `ACQUISITION.md`).

**Contents:**
[1 The hosted `legba-models` NLP service](#1-the-hosted-legba-models-nlp-service) ·
[2 LLM providers](#2-llm-providers) ·
[3 The faithfulness verify judge](#3-the-faithfulness-verify-judge) ·
[4 Embeddings](#4-embeddings) ·
[5 The consult engine](#5-the-consult-engine) ·
[6 Media extraction](#6-media-extraction-future-seam) ·
[7 Knowledge grounding](#7-knowledge-grounding--mitigating-the-models-training-cutoff) ·
[Configuration reference](#configuration-reference) ·
[See also](#see-also)

The three roles are:

1. **Baseline NLP enrichment** — small, deterministic transformer models
   (translation, zero-shot classification, relation extraction, summarization)
   served by the `legba-models` service. A `SourceActor` uses these once per
   signal, at acquisition time, to enrich the single canonical signal before it
   fans out.
2. **LLM providers + embeddings** — the analysis plane. A self-hosted vLLM LLM
   (gpt-oss-120b) is the **core analyst plane** ($0, self-hosted) that runs the
   seven bounded reasoning units and the composition tower; a hosted Anthropic model
   (Claude Opus 4.8) is reserved for the **consult / deep-consult** kinds only
   (billed, used sparingly); an OpenAI-compatible embeddings model (`BAAI/bge-m3`,
   1024-dim) shares the vLLM box — it is also the embedder-through-port that backs the
   live `world_context` / `tradecraft` RAG corpora and ingest-dedupe vectors. `AnalystActor`s and the consult engine reach these
   through provider handlers bound to stack-component descriptors.
3. **The faithfulness verify judge** — an LLM that scores each cited finding for
   *faithfulness*: does each claim actually follow from its cited evidence? It
   powers the mandatory post-finding verify pass (§3). **Currently this judge is
   the SAME core reasoning model** (`llm.primary.openai_compat`, gpt-oss-120b)
   that generates the units and compositions — it is **NOT** cross-family. This
   is a deliberate, temporary choice: the earlier 8B cross-family judge
   (`llm.verify.slm_8b`, Llama-3.1-8B) proved too weak — harsh and mis-aimed — so
   the strong reasoning model runs the judging to prove the flow. **Known
   limitation:** a model verifying prose from the same model shares its blind
   spots, so the faithfulness signal is weaker than an independent cross-family
   judge; the deterministic citation-presence floor and the signed provenance
   chain still backstop it, and a dedicated reasoning judge model is planned. It
   measures **groundedness, not truth** — a well-cited claim can still be wrong
   about the world; the judge only checks that the prose is supported by the
   sources it cites.

This split matches the platform's planes (see `DESIGN.md`): the **acquisition
plane** uses the baseline NLP models; the **analysis plane** uses the LLM
providers and embeddings; the **verify pass** runs the faithfulness judge
(currently the same core model, not cross-family — see §3) over what the
analysis plane produced.

> **Hard rule — no litellm/dspy in the inference path.** litellm and dspy
> **never** ship in the runtime image or an analyst's inference path. The base
> runtime is dspy-free on purpose (`docker/Dockerfile.runtime`); dspy (with its
> litellm transitive dep) ships in **exactly one** image, the opt-in GEPA
> optimizer worker (`docker/Dockerfile.worker`), and even there litellm is never
> invoked — the GEPA loop drives a custom `dspy.BaseLM` (`LegbaProviderLM`) over
> the same `LLMProviderHandler` the rest of the system uses, so litellm is an
> inert transitive dependency. The prompt-module files under `src/legba/prompts/`
> import `dspy` at module top, but the analyst inference path guards with
> `importlib.util.find_spec("dspy")` and **degrades to a direct `chat_complete`**
> when dspy is absent (which it is, in production).

---

## 1. The hosted `legba-models` NLP service

`legba-models` is a FastAPI service on a GPU host (GPU 0, Tesla T4 16 GB) that
fronts four small transformer models behind a single HTTP surface. It does
deterministic NLP enrichment — there is no LLM in this service.

| Endpoint | Method | Model | HuggingFace ID | Purpose |
|----------|--------|-------|----------------|---------|
| `/translate` | POST | NLLB-200-distilled-600M | `facebook/nllb-200-distilled-600M` | Translate non-English text → English |
| `/classify`  | POST | DeBERTa-v3 zero-shot | `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` | Zero-shot topic classification |
| `/extract`   | POST | GLiREL-large | `jackboyla/glirel-large-v0` | Zero-shot relation extraction (S-P-O triples) |
| `/summarize` | POST | T5-small | `google-t5/t5-small` | One-line multi-text summary |
| `/health`    | GET  | — | — | Service + GPU-memory status |

Total VRAM is ~3.9 GB. The full request/response contract for each endpoint
lives in `legba-models/USAGE.md`.

### Access

- **External:** `https://nlp.example.internal`, HTTP Basic Auth over
  HTTPS.
- **Internal:** `http://legba-models:8700` from any container on the `fastchat`
  Docker network, no auth.

The service is registered as the `nlp.local.legba_models` stack component
(schema `legba/stack/nlp_service/1.0.0`; see
`scripts/bringup_register_stack.py`). Its config holds the endpoint, the four
endpoint paths, the health path, a `timeout_seconds` (default 60), and two
vault-backed secrets — `api_user` and `api_pass` — for the external Basic-Auth
path.

### How the runtime reaches it

Filter/enrichment handlers in the acquisition pipeline bind to this component
via a `Property.StackRef("nlp.local.legba_models")`. The runtime resolves
the StackRef, reads the config, decrypts the credentials from the vault, and
hands the handler an `NlpServiceClient`
(`src/legba/data/stack/nlp_service/client.py`).

`NlpServiceClient` is an async `httpx` client with one method per endpoint —
`translate()`, `classify()`, `extract()`, `summarize()`, `health()`. It uses
Basic Auth when `api_user`/`api_pass` are supplied and skips it for the
internal no-auth path. Failures are typed:

- **`NlpServiceUnavailable`** — network error / 5xx / non-401 4xx / non-JSON
  body. The acquisition-plane filter handlers convert this into a no-op
  pass-through: the signal flows downstream un-enriched and the handler's
  health flips to `degraded`.
- **`NlpServiceAuthError`** — HTTP 401, a distinct signal because operator
  action (vault rotation) is required.

This graceful degradation means a `legba-models` outage slows enrichment but
never stalls the pipeline.

### Where each endpoint is used

- **`/extract` — entity + relation enrichment.** The multilingual NER filter
  (`src/legba/data/filters/ner.py`) posts the signal's text to `/extract`, walks
  the returned subject-predicate-object triples, and maps each subject/object
  candidate to Legba's closed 9-value `entity_class` taxonomy (label-keyword
  heuristic, since GLiREL returns free-text entities without typed labels). GLiREL
  emits a **real per-relation confidence score** per extracted triple (live facts
  span ~0.75 / 0.80 / 0.92 / 0.95, not a synthetic constant), so relation
  confidence is genuine model output rather than a fixed sentinel. It
  annotates `signal.payload["entities"]`; classes outside the registry's
  vocabulary are dropped. These entities feed the indexed `entity_classes`
  column the subscription/fan-out layer pushes down to SQL, and the entity
  knowledge-graph that the `entity_resolution` analyst keeps current.
- **`/classify`** — zero-shot topic classification against a built-in
  9-category default set (`conflict`, `political`, `economic`, `health`,
  `environment`, `technology`, `disaster`, `social`, `sports`) or operator-
  supplied labels (`src/legba/data/filters/classify.py`).
- **`/translate`** — translates a non-English signal to English so downstream
  NER/classification/LLM steps see one language. Supported source languages:
  `ar`, `fa`, `he`, `ru`, `en`, `zh`, `fr`, `es`, `de`, `uk`, `tr`. All text
  inputs are truncated to 512 tokens at the model.
- **`/summarize`** — exposed by the NLP service but has **no production caller**
  today (situation / cluster titles are plain string operations, not model-
  summarized). Kept available for future use.

> **Note on language detection.** Per-signal language detection is **not** a
> hosted-model call. It runs in-process in the language-detect filter
> (`src/legba/data/filters/language_detect.py`) via `langdetect` (default) or
> `lingua` — sub-millisecond, no model download, no network round-trip. Only
> *translation* of a detected non-English signal hits the hosted NLLB model.

---

## 2. LLM providers

Legba speaks to three LLM provider families through a common handler base. Each
provider is a stack-component descriptor; the runtime instantiates the matching
handler, resolves the API key from the vault, and binds it to the analyst (or
consult engine) that declared it.

### The provider handlers

All three concrete handlers subclass `LLMProviderHandler`
(`src/legba/data/stack/llm/base.py`) and expose a single unified surface —
`chat_complete()` / `stream_complete()` returning `LLMResponse` /
`LLMUsage` / `LLMToolCall`. Analyst handlers author tool specs once in the
OpenAI Chat-Completions JSON-Schema shape; each provider translates to its
native wire shape. The registry of handlers is
`LLM_HANDLERS` in `src/legba/data/stack/llm/__init__.py`.

| Subprovider | Handler | Wire API | Auth | Notes |
|-------------|---------|----------|------|-------|
| `vllm` | `VLLMProviderHandler` (`vllm.py`) | `POST /v1/chat/completions` (OpenAI-compatible) | Bearer **or** HTTP Basic | Self-hosted gpt-oss-120b (core plane — and, currently, the faithfulness verify judge itself; see §3); the earlier self-hosted 8B (`llm.verify.slm_8b`) is no longer wired as the verify judge; zero-cost price table; coalesces vLLM multi-choice (reasoning + final) output. |
| `anthropic` | `AnthropicProviderHandler` (`anthropic.py`) | `POST /v1/messages` | `x-api-key` | Hoists system role to top-level `system`; required `max_tokens`; maps extended-thinking + cache tokens into `LLMUsage`. Consult/deep only. |
| `openai` | `OpenAIProviderHandler` (`openai.py`) | `POST /v1/chat/completions` | Bearer | Routes reasoning models (`gpt-5`, `o*`) to `max_completion_tokens` + `reasoning_effort`. Registered as a subprovider; no live component points at it today. |

Shared base behavior:

- **Auth is a switch.** `LLMProviderConfig` (`src/legba/data/schemas/stack.py`)
  supports **Bearer** (`api_key`) or **HTTP Basic** (`api_user` + `api_pass`);
  when both are present, Basic wins. The self-hosted 8B host
  (`llm.verify.slm_8b`, no longer wired as the verify judge) is fronted by Caddy
  Basic Auth, so it uses the Basic path; the 120b uses Bearer.
- **Retry/backoff** on `429`/`500`/`502`/`503`/`529` (honors `retry-after`),
  up to 3 retries; `TransientLLMFailure` vs `HardLLMFailure` classification so
  the runtime retries the right cases.
- **Lean health check** — TCP reachability of the endpoint host + vault key
  resolution only. The handler never burns paid (or self-hosted GPU) tokens on
  a poll; the next real `chat_complete` is the liveness probe.
- **Cost + budget hooks** — `LLMUsage` carries prompt/completion/reasoning/
  cache token counts, and `estimate_cost()` derives a USD estimate from a
  per-subprovider `PRICE_TABLE` (`pricing.py`). The self-hosted vLLM table is
  empty (zero list price) but tokens are still counted for budget envelopes.
  Anthropic/OpenAI tables carry list prices keyed by model-family prefix.

### Registered providers

`scripts/bringup_register_stack.py` registers the core LLM stack components
(schema `legba/stack/llm_provider/1.0.0`):

| Component id | Endpoint | Model | Role |
|--------------|----------|-------|------|
| `llm.primary.openai_compat` | `https://llm.example.internal` | `gpt-oss-120b` | core analyst plane (units + compositions + every scheduled analyst) |
| `llm.anthropic.opus_4_7` | `https://api.anthropic.com` | `claude-opus-4-8` | consult / deep-consult plane only |
| `llm.verify.slm_8b` | self-hosted 8B host (Caddy Basic Auth) | 8B model (Llama-3.1-8B, "legba-slm") | **NOT the current verify judge** — faithfulness verify now runs on the core model (§3); this 8B is retained for a future dedicated judge |

> **Component id vs model name.** The Anthropic id is historically
> `llm.anthropic.opus_4_7`, but the id is just a label — the config's
> `model_name` is authoritative, and it is `claude-opus-4-8`
> (env-overridable via `LEGBA_CONSULT_MODEL_NAME`). The id was left unchanged to
> avoid re-pointing every descriptor that references it.

> **Note on `llm.verify.slm_8b`.** This is the retired 8B cross-family judge
> component. As of 2026-07-01 the units + compositions declare
> `method.llm.verify: raw: llm.primary.openai_compat`, so faithfulness verify
> runs on the **core reasoning model**, not the 8B (§3). The 8B component may
> still be registered live via `PUT /stack/{id}`, but nothing points at it as the
> verify judge today; a dedicated reasoning judge is planned. If the verify
> component is absent or the flag is off, the verify pass falls back to its
> deterministic floor (§3).

The `LLMProviderConfig` holds `api_endpoint`, the vault credential(s)
(`api_key` **or** `api_user`/`api_pass`), `model_name`, `max_tokens`, and
`timeout_seconds`. Endpoints store the **base host only** — the handler's
`_chat_endpoint_path()` prepends the provider-specific path (`/v1/...`), and
the base client defensively strips a trailing `/v1` so both `https://host` and
`https://host/v1` configs resolve correctly.

### Resolution: descriptor → handler

An analyst descriptor names its LLM via `method.llm.primary`, a StackRef to a
registered component (and, for the units/compositions, `method.llm.verify` for
the judge). At analyst-actor activation
(`src/legba/runtime/analyst_deps_builder.py`):

1. `build_llm_handler_from_stack_component()` fetches the
   `/stack/{component_id}` row and re-parses the config into
   `LLMProviderConfig`.
2. `infer_llm_subprovider()` picks the handler class. There is no
   `subprovider` field on the config; the subprovider is derived from the
   component id and endpoint host, most-specific first: a `.anthropic.` /
   `llm.anthropic*` id → `anthropic`; an `.openai_compat` suffix → `vllm`; a
   `.openai.` / `llm.openai*` id → `openai`; then by endpoint hostname
   (`api.anthropic.com` → `anthropic`, `api.openai.com` → `openai`); otherwise
   **fall back to `vllm`** (the dominant self-hosted, OpenAI-compatible case —
   this is also how the self-hosted 8B component (`llm.verify.slm_8b`) resolves,
   with **no** special casing).
3. The handler class is looked up in `LLM_HANDLERS`, instantiated, and
   `on_configure`'d (resolves the vault secret, fetches the model list).
   `on_activate` — opening the HTTP pool — is left to the actor's lifecycle.

This is registry-resolved per-kind routing: a deterministic analyst can be
pointed at the self-hosted gpt-oss-120b while a high-value analyst is pointed at a
hosted Claude model, purely by which component its descriptor references —
no code change.

**Plane split (live policy).** The hosted Anthropic plane (`claude-opus-4-8`) is
reserved for the **consult / deep-consult** kinds only (billed — used sparingly).
**Every scheduled analyst** — the seven bounded reasoning units, the per-country /
per-region / world composition tower (plus the thematic `escalation_composition`),
the critic, and the deterministic maintenance analysts —
runs on the core OpenAI-compatible plane (`llm.primary.openai_compat`,
gpt-oss-120b). The critic runs there with `allow_self_correlated=true` (it is no
longer a cross-provider check; the faithfulness verify pass in §3 is now the
trust gate — though that pass currently also runs on the core model, not
cross-family; see §3). The core plane sends **no `max_tokens`** — output length is left to the
model's own budget — while the prompt **input** is bounded by
`LEGBA_LLM_INPUT_TOKEN_BUDGET` (default `32000`).

Whatever model an analyst is bound to carries a **training cutoff**. For
assessments that turn on current world state, that stale prior is corrected not by
the model but by **substrate knowledge grounding** — current facts injected into
the prompt at analysis time. See §7 for why this sits where it does relative to
the model.

---

## 3. The faithfulness verify judge

Every cited finding produced by a bounded reasoning unit (and every composition)
goes through a **mandatory faithfulness verify pass**
(`src/legba/data/provenance/verify.py`, `verify_finding_faithfulness(...)`). This
pass is what lets a report claim to
be *cited and checked* rather than merely generated.

The pass has two layers:

1. **A deterministic citation-presence floor — always on.** It checks every
   fact-asserting claim in a finding's prose against the resolved
   `data['citations']` bridge: a claim that asserts a fact with **no `[N]`
   marker**, or whose marker resolves to **no real `signal_id`**, is an
   UNSUPPORTED span. The floor score is the fraction of checkable claims that are
   supported. This layer needs no model and cannot be turned off.
2. **An optional LLM judge — currently the core reasoning model.** When the
   descriptor declares `method.llm.verify` (all seven units + every composition in
   the tower do) **and** the `LEGBA_VERIFY_LLM_JUDGE` flag is on, the runtime wires an LLM
   judge to refine per-claim verdicts. **As of 2026-07-01 that judge is the SAME
   core model** (`llm.primary.openai_compat`, gpt-oss-120b) that wrote the finding
   — it is **NOT** cross-family. This is a deliberate, temporary choice: the
   earlier 8B cross-family judge (`llm.verify.slm_8b`, Llama-3.1-8B) proved too
   weak — harsh and mis-aimed in the composition shake-down — so the strong
   reasoning model runs the judging to prove the flow. **Known limitation:** a
   model verifying prose from the same model shares its blind spots, so the
   faithfulness signal is weaker than an independent cross-family judge; the
   deterministic floor above and the signed provenance chain still backstop it,
   and a dedicated reasoning judge model is planned. It is a normal
   `LLMProviderHandler`, built through the same cached factory as every other LLM.

The result is a per-finding faithfulness score in `[0, 1]`, persisted as a
`critique`. At read time the runtime folds
`effective_confidence = min(confidence, faithfulness_score)`, which **gates a
visible low-confidence tier** — an unfaithful finding is demoted and its
sub-claims are excluded from the compositions (the per-country composition does an
INNER JOIN on the faithfulness critique), but a low score **never hard-deletes**
the row. A planted fabrication with no supporting citation is flagged unsupported.

**Honest behavior when the judge is unavailable.** If the flag is off, the
component is unregistered, or the judge host is unreachable, the pass **soft-fails**:
it degrades to the deterministic floor and labels the verdict
`judge-unavailable` — it **never fabricates a number**. So a run always yields a
real (if coarser) faithfulness verdict.

**Caveat — citation-marker variants.** Core-plane models sometimes emit
full-width or bracket-variant citation markers (`【3】` / `［3］`) instead of ASCII
`[3]`; the finding path normalizes these digit-wrapping variants before the
floor parses them, so a stylistic bracket choice does not silently zero out the
citation count (and the faithfulness score with it).

**Same yardstick reused by the optimizer.** The scoped GEPA experiment
(`unit_optimizer`, over the single `leadership_transition` unit) measures each
candidate prompt with a **real before/after faithfulness delta on this same
faithfulness judge** (currently the core `llm.primary.openai_compat` model, not
cross-family — see above). It stays `promotion_gate=human_gated` and can never
auto-promote on a degenerate, absent, or non-positive delta (a live measurement
read parent `0.34` → candidate `0.29`, i.e. **-0.05**, so it did not promote).
The old monolithic `country_optimizer` stays cadence-frozen (its descriptor is
still `state=active`; only its cadence is stopped). dspy for that experiment
lives only in the worker image (see the hard-rule callout at the top).

---

## 4. Embeddings

Embeddings come from the same vLLM box via its OpenAI-compatible
`/v1/embeddings` route, serving the `BAAI/bge-m3` model (1024-dim). It is a
separate, smaller model from the LLM but shares the host and the same vault key.

It is registered as the `embed.primary.openai_compat` stack component
(schema `legba/stack/embedding/1.0.0`) with endpoint
`https://llm.example.internal`, `model_name: bge-m3` (set via
`LEGBA_EMBED_MODEL_NAME`), `dim: 1024`, `normalize`, and `batch_size`.

The runtime builds a `HostedEmbeddingClient`
(`src/legba/runtime/embedding_factory.py`) from that component:
`build_embedding_service_from_stack_component()` fetches the row, parses
`EmbeddingServiceConfig`, resolves the (optional) api_key, and returns a client
satisfying the `EmbeddingService` Protocol — `async def embed(text) ->
list[float]`. The wire call is
`POST {endpoint}/v1/embeddings` with `{"model": ..., "input": text}` and a
Bearer header; the response's `data[0].embedding` vector is returned.

**Where embeddings are used:**

- **Dedup, semantic tier (Tier 3).** The four-tier dedupe filter
  (`src/legba/data/filters/dedupe.py`) embeds a candidate signal's title +
  summary and cosine-searches a per-target Qdrant collection (default
  threshold 0.92). It runs only when Tiers 1–2 (URL hash, content hash) miss,
  and only when an embedder + Qdrant are wired — operators can opt a target
  down to cheap-only tiers, which also skips creating the per-target Qdrant
  collection.
- **Consult `vector_search`.** The consult engine declares a `vector_search`
  tool for semantic search over signal embeddings, but it is a **declared seam**
  (`docs/SEAMS.md`): no production deps wire a vector store into the consult
  analyst today, so the tool is not live — it is the landing zone for
  embedding-backed retrieval when a vector store is wired in.

> **Future seam.** A vector-search path over the substrate's signal embeddings
> is the natural home for additional embedding-backed retrieval; the embedding
> client and `dim` contract are stable, so new consumers wrap the same client.

---

## 5. The consult engine

The consult engine is the on-demand ReAct analyst kind `consult_on_demand`
(`src/legba/data/analysts/consult_on_demand.py`). Unlike scheduled analysts it
has **no cadence** — it is dispatched on demand via an A2A skill
(`intelligence.consult_on_demand`), an MCP tool (`legba_consult`), or an
operator panel, each carrying a free-form `question` plus an optional
`scope_predicate`. Consult (and deep-consult) is the one place the hosted
Claude Opus 4.8 plane is used, so it is billed and used sparingly.

It runs a single-turn ReAct loop, capped at `MAX_TOOL_ROUNDS = 6` rounds plus
one forced final-synthesis turn:

1. **Plan** — render the system prompt + tool whitelist + the operator's
   question.
2. **Round** — the LLM emits strict JSON: either a tool call
   (`{"tool": ..., "args": ...}`), a batch of independent tool calls
   (`{"tools": [...]}`, up to `MAX_TOOLS_PER_BATCH`, run concurrently for one
   round's cost), or a final answer
   (`{"final": true, "answer": ..., "uncertainty": ..., "cited_refs": [...],
   "unanswered_aspects": [...]}`).
3. **Act** — a requested tool is dispatched against the substrate and its JSON
   result appended to the conversation.
4. **Loop** — back to Round, up to the cap, after which a final turn is forced
   with the tools withheld so the operator always gets a structured answer.

The tool whitelist is a set of **read-only substrate primitives** (`_KNOWN_TOOLS`,
16 today: `search_signals`, `query_facts`, `inspect_entity`, `query_nexuses`,
`query_hypotheses`, `get_timeline`, `compare_targets`, `query_paths`,
`find_proxy_chains`, `query_brokers`, `list_findings`, `list_situations`,
`query_predictions`, `list_targets`, `list_sources`, and `vector_search`).
`vector_search` is the one **non-live entry** — a designed seam pending
vector-store wiring (it dispatches only when a vector store is present). The kind
is a *read* over the substrate — write-back tools are deliberately excluded. In
production, consult is governed through the `substrate_read` action pack, so every
`_KNOWN_TOOLS` entry must also be present in that pack.

The LLM is resolved exactly like any other analyst — through `method.llm.primary`
→ a stack component → a provider handler — so the consult engine inherits the
same provider routing, vault auth, retry, and budget accounting as the rest of
the analysis plane. The result is a structured `ConsultResponsePayload` (answer,
uncertainty, cited substrate refs, unanswered aspects), wrapped as a
`FindingPayload` so it carries into the substrate through the standard finding
write path with full provenance.

---

## 6. Media extraction (future seam)

The acquisition baseline (`src/legba/data/sources/baseline.py`) has an
**eager media tier** that dispatches by signal modality to a `MediaExtractor`.
The plumbing (modality routing → extractor → derived signal re-entering the
fan-out with inherited geo/tags) is complete and tested, but it is a **declared
seam** (`docs/SEAMS.md`): with no real extraction endpoint configured
(`LEGBA_MEDIA_API_URL`), the path **refuses activation loudly** rather than
fabricating output — the former passthrough/echo example extractors were removed
outright (A-2 / the no-stub rule). The production extractors — hosted Whisper
(audio transcription), VLM (image/video captioning), and OCR — register against
the same `MediaExtractor` protocol and hosted-HTTP pattern as the NLP filters
above when endpoints come online. The async `process_media` job envelope exists
for the on-demand, analyst-driven tier. See `DESIGN.md` for the three media
tiers (reference / eager / on-demand).

**Deployable service (D2 PREP).** A deployable `legba-media/` service now ships
(the sibling of `legba-models/`): a FastAPI app exposing the exact
`/transcribe` `/caption` `/ocr` `/detect` `/health` contract the runtime media
client POSTs to, with a `media` compose profile and env wiring (set
`LEGBA_MEDIA_API_URL=http://legba-media:8800`). It is built to be **deployed +
tested easily**, but the live model is held as the stated seam: with no model
backend wired (`app/main.load_backends()` ships empty) every extraction endpoint
returns **HTTP 503** — `process_media` keeps refusing loudly, no fabricated row
lands. Wiring a real Whisper/VLM/OCR backend flips a kind from 503 to live with
no other change. See `legba-media/USAGE.md`.

---

## 7. Knowledge grounding — mitigating the model's training cutoff

Every LLM in the analysis plane carries a **training cutoff**: a date past which it
has no knowledge. For most analyst work that is fine — the model reasons over the
signal slice it is handed, which is fresh. But for an assessment that turns on
*current world state* — who currently holds an office, which alliances are in force,
the present state of an ongoing conflict — the model has to fall back on its prior,
and that prior can be stale and wrong. The live failure that motivated the fix: a
per-country assessor called the **current** US president a "former" president,
because the bound model's training data predates the 2024 election, and the signal
slice (recent headlines) rarely restates a standing background fact like "X is the
head of state." The model had no in-context correction, so it confidently asserted
the stale answer.

This is **not** fixed by swapping or fine-tuning the model — every model has *some*
cutoff, and the live topology routes the LLM-bearing analysts at the self-hosted
`gpt-oss-120b` (§2), whose cutoff is fixed. Instead, Legba **injects current facts
from the substrate at analysis time** as **Tier-1 grounding**: the platform's own
temporal `facts` (`valid_from` / `valid_until` / `superseded_by`) and typed
`nexuses` (each carrying a polarity sign) — sourced from **Wikidata** (the live
`wikidata_leaders` seed adapter) and the curated `world_baseline` adapter — are the
authoritative current-world-state store, and a **grounding** step injects the
relevant current facts into the prompt before the LLM call, framed to the model as
"AUTHORITATIVE CURRENT CONTEXT … treat as ground truth over any prior knowledge."
That framing is the in-prompt instruction to the model, not a platform truth-claim:
the substrate facts are only as current as the last seed run, and the vector-backed
Tier-2 free-text background — now LIVE — is a separate, non-citable preamble (caveat 3
below).

**Status.** Tier-1 structured grounding is live and opted-in on all **seven bounded
reasoning units** (`leadership_transition`, `energy_security`, `escalation`,
`narrative_coordination`, `internal_stability`, `military_posture`,
`economic_coercion`) — each declares a `grounding:` block. The injected
preamble folds in **accumulated** `facts`, polarity-signed `nexuses`, and a separate
clearly-labelled block of ongoing `situation` frames (e.g. "US head of government
Trump since 2025-01-20; US–Iran active conflict since 2026-02-28; NATO member
since 1949"), and each unit reads a **72h** raw-signal window — so a unit
integrates substrate state over time, not just today's headline slice. The per-country,
per-region, and world compositions do **not** ground directly; they compose over the units'
already-verified, already-grounded sub-claims. The retired `country_assessor`
monolith carried grounding too, but it is out of the active set (nothing in the
trusted product reads it). Tier-2 vector `world_context` is now **live** and staggered on
for `leadership_transition` + `internal_stability`.

Why this is the right place to explain it relative to the model:

- **Grounding corrects whatever cutoff the bound model has.** Because the correction
  is supplied at call time from the substrate, it is model-agnostic — point an
  analyst at a different LLM (or the same model a year later) and the grounding still
  hands it today's officeholders. The model is the reasoning engine; the substrate is
  the up-to-date factual context.
- **The ground truth stays auditable and is not the model's invention.** What gets
  injected is curated/seed `facts` rows with full provenance and temporal honesty
  (only rows where `superseded_by IS NULL AND (valid_until IS NULL OR valid_until >
  now())`, preferring `seed`/`curated` source), not free-text the model produced.
  Wikidata is the upstream source of record for the leader/alliance facts.
- **It degrades, never gates.** Grounding is an enrichment: a substrate read failure
  (or a thin slice that resolves nothing) leaves the prompt untouched and the run
  proceeds un-grounded. It is opt-in per analyst and token-capped.

**Honest caveats.** (1) **Self-consistency, not provider knowledge** — grounding
fixes the *current-facts* gap, not every reasoning error; the injected facts are only
as current as the last seed run. (2) **Bare-QID skip** — when Wikidata's label
service can't resolve an entity (the live case is Q22686 / Donald Trump, which has no
English label), the seed adapter resolves it via a `wbgetentities` label lookup with
an enwiki-sitelink fallback, and the resolver *skips any value that is still a bare
`Qxxxx`* so the model is never handed an unreadable id. (3) **Tier 2 is now LIVE** —
the vector `world_context` collection for free-text background the structured facts
can't carry is wired (the embedder-through-port L-114 landed) and pre-declarable on the
descriptor (`sources: [..., vector:world_context]`): the resolver retrieves from the
curated `world_context` Qdrant corpus (~293 chunks; a `tradecraft` corpus of ~1716
chunks also exists) through the stack embedder port (bge-m3, 1024-dim) as a separate,
non-citable preamble — opportunistic, relevance-floored, country-filtered,
degrade-not-drop when the corpus is empty. It is **staggered on** — enabled today for
`leadership_transition` + `internal_stability`; the other units resolve only the
structured `substrate` source, pending review-gated expansion.

The mechanism (the `GroundingBlock` descriptor field, the `SubstrateGroundingResolver`,
the `inline_target` GROUND phase, the seed adapters) is described in `DESIGN.md` §3.4
and `ANALYSIS.md` §7.9.

---

## Configuration reference

All AI-model wiring is declarative stack-component config, registered by
`scripts/bringup_register_stack.py` (the current faithfulness verify judge simply
reuses the already-registered `llm.primary.openai_compat` component; the retired
8B judge, if ever revived, is pointed live via a `PUT /stack/{id}` when the 8B
host comes up) and resolved at runtime through the
registry + vault. There are no AI-model env vars in the runtime path — secrets are
vault ids, endpoints are config fields.

| Stack component | Schema | What it serves |
|-----------------|--------|----------------|
| `llm.primary.openai_compat` | `legba/stack/llm_provider/1.0.0` | Self-hosted gpt-oss-120b LLM — core analyst plane (vLLM, OpenAI-compatible) |
| `llm.anthropic.opus_4_7` | `legba/stack/llm_provider/1.0.0` | Anthropic Claude `claude-opus-4-8` (hosted; consult/deep plane only) |
| `llm.verify.slm_8b` | `legba/stack/llm_provider/1.0.0` | Self-hosted 8B model (Llama-3.1-8B, "legba-slm"; HTTP Basic via Caddy) — the **retired** cross-family judge; **not** the current verify judge (faithfulness verify now runs on `llm.primary.openai_compat`, §3) |
| `embed.primary.openai_compat` | `legba/stack/embedding/1.0.0` | `bge-m3` embeddings (vLLM `/v1/embeddings`, 1024-dim) |
| `nlp.local.legba_models` | `legba/stack/nlp_service/1.0.0` | `legba-models` translate / classify / extract / summarize |

| Vault secret id | Used by |
|-----------------|---------|
| `llm.primary.api_key` | gpt-oss-120b LLM **and** `bge-m3` embeddings (shared box) |
| `llm.anthropic.api_key` | Anthropic provider (consult/deep) |
| `llm.verify.*` Basic-auth creds (`api_user` / `api_pass`) | the retired self-hosted 8B judge host (Caddy Basic Auth); unused while verify runs on the core model |
| `nlp.local.legba_models.api_user` / `.api_pass` | `legba-models` Basic Auth (external path) |

| Flag / env var | Effect |
|----------------|--------|
| `LEGBA_VERIFY_LLM_JUDGE` | Gates the optional LLM judge ON (currently the core `llm.primary.openai_compat` model, §3); off → verify pass runs the deterministic citation-presence floor only (labelled `judge-unavailable`) |
| `LEGBA_CONSULT_MODEL_NAME` | Overrides the consult/deep model name (default `claude-opus-4-8`) |
| `LEGBA_LLM_INPUT_TOKEN_BUDGET` | Caps the core-plane prompt **input** (default `32000`); the core plane sends no `max_tokens` on output |

---

## See also

- `ARCHITECTURE.md` — stack registry, credential vault, and the substrate
  (Qdrant for embeddings, Postgres/AGE for the entity graph).
- `ACQUISITION.md` — the acquisition plane and where the baseline NLP models
  sit in per-signal enrichment.
- `DESIGN.md` — the planes and where models sit.
- `ANALYSIS.md` — the faithfulness verify pass, the scorecard bands, and the
  skill scoreboard (each honest about no-skill / insufficient-sample results).
- `legba-models/USAGE.md` — the full `legba-models` HTTP API contract.

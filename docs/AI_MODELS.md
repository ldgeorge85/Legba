# AI Models

Legba is a source-first platform for automated analysis & knowledge fusion. Two classes of AI model carry
its acquisition and analysis work, and **none of them run inside the Legba
containers** — every model is reached over HTTP against out-of-process
model-serving hosts, with credentials and endpoints resolved through the stack
registry and credential vault (see `ARCHITECTURE.md`, `ACQUISITION.md`).

The two classes are:

1. **Baseline NLP enrichment** — small, deterministic transformer models
   (translation, zero-shot classification, relation extraction,
   summarization) served by the `legba-models` service. A `SourceActor`
   uses these once per signal, at acquisition time, to enrich the single
   canonical signal before it fans out.
2. **LLM providers + embeddings** — a self-hosted vLLM LLM (gpt-oss-120b,
   served under a deployment-configured model alias set via
   `LEGBA_LLM_MODEL_NAME`), an OpenAI-compatible embeddings model
   (`BAAI/bge-m3`), and optional hosted providers (Anthropic, OpenAI).
   `AnalystActor`s and the consult engine reach these through provider
   handlers bound to stack-component descriptors.

This split matches the platform's planes (see `DESIGN.md`): the **acquisition
plane** uses the baseline NLP models; the **analysis plane** uses the LLM
providers and embeddings.

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
| `vllm` | `VLLMProviderHandler` (`vllm.py`) | `POST /v1/chat/completions` (OpenAI-compatible) | Bearer | Self-hosted gpt-oss-120b; zero-cost price table; coalesces vLLM multi-choice (reasoning + final) output. |
| `anthropic` | `AnthropicProviderHandler` (`anthropic.py`) | `POST /v1/messages` | `x-api-key` | Hoists system role to top-level `system`; required `max_tokens`; maps extended-thinking + cache tokens into `LLMUsage`. |
| `openai` | `OpenAIProviderHandler` (`openai.py`) | `POST /v1/chat/completions` | Bearer | Routes reasoning models (`gpt-5`, `o*`) to `max_completion_tokens` + `reasoning_effort`. |

Shared base behavior:

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

`scripts/bringup_register_stack.py` registers these LLM stack components
(schema `legba/stack/llm_provider/1.0.0`):

| Component id | Endpoint | Model | Tier |
|--------------|----------|-------|------|
| `llm.primary.openai_compat` | `https://llm.example.internal` | `gpt-oss-120b` | primary |
| `llm.anthropic.opus_4_7` | `https://api.anthropic.com` | `claude-opus-4-8` | consult/deep plane |

The `LLMProviderConfig` (`src/legba/data/schemas/stack.py`) holds
`api_endpoint`, a vault `api_key` secret, `model_name`, `max_tokens`, and
`timeout_seconds`. Endpoints store the **base host only** — the handler's
`_chat_endpoint_path()` prepends the provider-specific path (`/v1/...`), and
the base client defensively strips a trailing `/v1` so both `https://host` and
`https://host/v1` configs resolve correctly.

### Resolution: descriptor → handler

An analyst descriptor names its LLM via `method.llm.primary`, a StackRef to a
registered component. At analyst-actor activation
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
   **fall back to `vllm`** (the dominant self-hosted, OpenAI-compatible case).
3. The handler class is looked up in `LLM_HANDLERS`, instantiated, and
   `on_configure`'d (resolves the vault secret, fetches the model list).
   `on_activate` — opening the HTTP pool — is left to the actor's lifecycle.

This is the registry-resolved per-kind routing: a deterministic analyst can be
pointed at the self-hosted gpt-oss-120b while a high-value analyst is pointed at a
hosted Claude/GPT model, purely by which component its descriptor references —
no code change. Today's live topology routes the LLM-bearing analysts at the
self-hosted `llm.primary.openai_compat` endpoint.

**Plane split (live policy, 2026-06).** The hosted Anthropic plane
(`claude-opus-4-8`) is reserved for the **consult / deep-consult** kinds only.
The **critic and every other analyst** run on the core OpenAI-compatible plane
(`llm.primary.openai_compat`); the critic runs there with
`allow_self_correlated=true` (it is no longer a cross-provider check). The core
plane sends **no `max_tokens`** — output length is left to the model's own budget
— while the prompt **input** is bounded by `LEGBA_LLM_INPUT_TOKEN_BUDGET`
(default `32000`). This heterogeneity boundary keeps consult/deep on a distinct
provider from the rest of the analysis plane.

Whatever model an analyst is bound to carries a **training cutoff**. For
assessments that turn on current world state, that stale prior is corrected not by
the model but by **substrate knowledge grounding** — current facts (sourced from
Wikidata) injected into the prompt at analysis time. See §6 for why this sits
where it does relative to the model.

---

## 3. Embeddings

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

## 4. The consult engine

The consult engine is the on-demand ReAct analyst kind `consult_on_demand`
(`src/legba/data/analysts/consult_on_demand.py`). Unlike scheduled analysts it
has **no cadence** — it is dispatched on demand via an A2A skill
(`intelligence.consult_on_demand`), an MCP tool (`legba_consult`), or an
operator panel, each carrying a free-form `question` plus an optional
`scope_predicate`.

It runs a single-turn ReAct loop, capped at `MAX_TOOL_ROUNDS = 6` rounds plus
one forced final-synthesis turn:

1. **Plan** — render the system prompt + tool whitelist + the operator's
   question.
2. **Round** — the LLM emits strict JSON: either a tool call
   (`{"tool": ..., "args": ...}`) or a final answer
   (`{"final": true, "answer": ..., "uncertainty": ..., "cited_refs": [...],
   "unanswered_aspects": [...]}`).
3. **Act** — a requested tool is dispatched against the substrate and its JSON
   result appended to the conversation.
4. **Loop** — back to Round, up to the cap, after which a final turn is forced
   with the tools withheld so the operator always gets a structured answer.

The tool whitelist is eight read-only substrate primitives, **seven live today**:
`search_signals`, `query_facts`, `inspect_entity`, plus the four agency read
tools `query_nexuses`, `query_hypotheses`, `get_timeline`, and `compare_targets`
— and `vector_search`, the eighth, embedding-backed tool above, which is a
**designed seam pending vector-store wiring** (it dispatches only when a vector
store is wired, otherwise it is not live). The kind is a *read* over the
substrate — write-back tools are deliberately excluded.

The LLM is resolved exactly like any other analyst — through `method.llm.primary`
→ a stack component → a provider handler — so the consult engine inherits the
same provider routing, vault auth, retry, and budget accounting as the rest of
the analysis plane. The result is a structured `ConsultResponsePayload` (answer,
uncertainty, cited substrate refs, unanswered aspects), wrapped as a
`FindingPayload` so it carries into the substrate through the standard finding
write path with full provenance.

---

## 5. Media extraction (future seam)

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

## 6. Knowledge grounding — mitigating the model's training cutoff

Every LLM in the analysis plane carries a **training cutoff**: a date past which it
has no knowledge. For most analyst work that is fine — the model reasons over the
signal slice it is handed, which is fresh. But for an assessment that turns on
*current world state* — who currently holds an office, which alliances are in force,
the present state of an ongoing conflict — the model has to fall back on its prior,
and that prior can be stale and wrong. The live failure that motivated the fix: the
country/world assessor called the **current** US president a "former" president,
because the bound model's training data predates the 2024 election, and the signal
slice (recent headlines) rarely restates a standing background fact like "X is the
head of state." The model had no in-context correction, so it confidently asserted
the stale answer.

This is **not** fixed by swapping or fine-tuning the model — every model has *some*
cutoff, and the live topology routes the LLM-bearing analysts at the self-hosted
`gpt-oss-120b` (§2), whose cutoff is fixed. Instead, Legba **injects current facts
from the substrate at analysis time** as **Tier-1 grounding**: the platform's own
temporal `facts` (`valid_from` / `valid_until` / `superseded_by`) and signed
`nexuses` — sourced from **Wikidata** (the live `wikidata_leaders` seed adapter) and
the curated `world_baseline` adapter — are the authoritative current-world-state
store, and an opt-in **grounding** step injects the relevant current facts into the
prompt before the LLM call, framed to the model as "AUTHORITATIVE CURRENT CONTEXT …
treat as ground truth over any prior knowledge." That framing is the in-prompt
instruction to the model, not a platform truth-claim: the substrate facts are only as
current as the last seed run, and the **vector-backed Tier-2 free-text background is a
designed future seam, not yet wired** (caveat 3 below). **Status (2026-06): Tier-1
structured grounding is live and opted-in on `world_assessor` / `country_assessor`;
Tier-2 vector `world_context` is designed-not-built.**

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
  proceeds un-grounded. It is opt-in per analyst (`world_assessor` /
  `country_assessor` today) and token-capped.

**Honest caveats.** (1) **Self-consistency, not provider knowledge** — grounding
fixes the *current-facts* gap, not every reasoning error; the injected facts are only
as current as the last seed run. (2) **Bare-QID skip** — when Wikidata's label
service can't resolve an entity (the live case is Q22686 / Donald Trump, which has no
English label), the seed adapter resolves it via a `wbgetentities` label lookup with
an enwiki-sitelink fallback, and the resolver *skips any value that is still a bare
`Qxxxx`* so the model is never handed an unreadable id. (3) **Tier 2 is a future
seam** — a vector `world_context` collection for free-text background the structured
facts can't carry is designed and pre-declarable on the descriptor
(`sources: [..., vector:world_context]`) but **not wired**; it needs the
embedder-through-port (L-114), so today only the structured `substrate` source acts.

The mechanism (the `GroundingBlock` descriptor field, the `SubstrateGroundingResolver`,
the `inline_target` GROUND phase, the seed adapters) is described in `DESIGN.md` §3.4
and `ANALYSIS.md` §7.9.

---

## Configuration reference

All AI-model wiring is declarative stack-component config, registered by
`scripts/bringup_register_stack.py` and resolved at runtime through the registry
+ vault. There are no AI-model env vars in the runtime path — secrets are vault
ids, endpoints are config fields.

| Stack component | Schema | What it serves |
|-----------------|--------|----------------|
| `llm.primary.openai_compat` | `legba/stack/llm_provider/1.0.0` | Self-hosted gpt-oss-120b LLM (vLLM, OpenAI-compatible) |
| `llm.anthropic.opus_4_7` | `legba/stack/llm_provider/1.0.0` | Anthropic Claude `claude-sonnet-4-6` (hosted; consult/deep plane only) |
| `embed.primary.openai_compat` | `legba/stack/embedding/1.0.0` | `bge-m3` embeddings (vLLM `/v1/embeddings`, 1024-dim) |
| `nlp.local.legba_models` | `legba/stack/nlp_service/1.0.0` | `legba-models` translate / classify / extract / summarize |

| Vault secret id | Used by |
|-----------------|---------|
| `llm.primary.api_key` | gpt-oss-120b LLM **and** `bge-m3` embeddings (shared box) |
| `llm.anthropic.api_key` | Anthropic provider |
| `nlp.local.legba_models.api_user` / `.api_pass` | `legba-models` Basic Auth (external path) |

---

## See also

- `ARCHITECTURE.md` — stack registry, credential vault, and the substrate
  (Qdrant for embeddings, Postgres/AGE for the entity graph).
- `ACQUISITION.md` — the acquisition plane and where the baseline NLP models
  sit in per-signal enrichment.
- `DESIGN.md` — the four planes and where models sit.
- `legba-models/USAGE.md` — the full `legba-models` HTTP API contract.

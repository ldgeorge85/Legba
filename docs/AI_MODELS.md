# AI Models in Legba

Legba uses seven distinct AI models across four services for inference, embedding, NLP enrichment, and continuous validation. None run inside the Legba containers — all are accessed over HTTP.

## Model Inventory

| Model | Type | Size | Hardware | Endpoint | Used By |
|-------|------|------|----------|----------|---------|
| GPT-OSS 120B (InnoGPT-1) | Causal LLM | 120B params | 2x A6000 48GB (tensor parallel) | ``<vllm-endpoint>/v1`` | Agent, Consultation |
| embedding-inno1 | Text embedding | — | Tesla T4 (shared GPU 0) | Same base URL, `/v1/embeddings` | Agent memory, Ingestion |
| NLLB-200-distilled-600M | Translation | 600M params | Tesla T4 (shared GPU 0) | ``<models-endpoint>`` | Ingestion |
| DeBERTa-v3 zero-shot | Classification | ~184M params | Tesla T4 (shared GPU 0) | Same as above | Ingestion |
| GLiREL-large | Relation extraction | ~350M params | Tesla T4 (shared GPU 0) | Same as above | Ingestion |
| T5-small | Summarization | 60M params | Tesla T4 (shared GPU 0) | Same as above | Ingestion |
| Llama 3.1 8B (Q5_K_M) | SLM | 8B params (quantized) | GPU 1 on ai1 | ``<slm-endpoint>/v1`` | Subconscious service |
| spaCy (en_core_web_trf) | NER | — | CPU | In-process | Ingestion (entity linking) |

## 1. Main LLM — GPT-OSS 120B

The agent's reasoning engine. Served by vLLM with tensor parallelism across two A6000 GPUs.

| Parameter | Value |
|-----------|-------|
| Model name | `InnoGPT-1` |
| Endpoint | `/v1/chat/completions` (OpenAI-compatible) |
| Temperature | `1.0` (hardcoded — GPT-OSS requires 1.0) |
| Top-p | `0.9` |
| Context window | 128k tokens (`LLM_MAX_CONTEXT_TOKENS`) |
| Max output tokens | 16,384 (`LLM_MAX_TOKENS`); not sent in payload by default (server manages budget) |
| Timeout | 180s |
| Throughput | ~42 tokens/sec, ~5.3 cycles/hour |
| Retries | 3 with exponential backoff on 429/500/502/503 |

**Call pattern:** Single-turn. Every LLM call sends one user message (system + user combined by `format.py`). No multi-turn conversation. Each step rebuilds the user message with accumulated tool results via a sliding window (last 8 tool interactions in full, older condensed to one-line summaries).

**Reasoning directive:** Prompts include `reasoning: high` in content, handled by the prompt assembler/templates. The model uses Harmony response format — output may contain channel markers like `<|channel|>final<|message|>...<|end|>` which are stripped by `strip_harmony_response()` before parsing.

**Key files:**
- `src/legba/agent/llm/provider.py` — VLLMProvider, the HTTP client
- `src/legba/agent/llm/client.py` — LLMClient, sliding window, tool loop
- `src/legba/agent/llm/format.py` — Harmony stripping, message formatting

## 2. Embedding Model — embedding-inno1

Generates 1024-dimensional vectors for semantic search and memory retrieval.

| Parameter | Value |
|-----------|-------|
| Model name | `embedding-inno1` |
| Endpoint | `/v1/embeddings` (OpenAI-compatible) |
| Dimensions | 1024 |
| Timeout | 30s |
| Hardware | Tesla T4, GPU 0 (shared with models service + Whisper) |

**Used in two places:**

1. **Agent episodic memory** — `LLMClient.generate_embedding()` embeds episode summaries and facts for Qdrant storage/retrieval. Called during `remember_episode`, `store_fact`, `recall_similar`, and `update_fact` tool invocations.

2. **Ingestion signal embeddings** — `ingestion/service.py` creates its own embedding HTTP client at startup. Embeds signal title + first 512 chars of summary, upserts to a `signals` Qdrant collection for semantic dedup and search.

Both paths hit the same `/v1/embeddings` endpoint. The embedding endpoint is separate from the LLM endpoint only if `EMBEDDING_API_BASE` is set; otherwise it falls back to `OPENAI_BASE_URL`.

## 3. GPU Models Service

Four small transformer models running on a single Tesla T4 via a FastAPI service (`legba-models`). Used exclusively by the ingestion pipeline for deterministic NLP enrichment — no LLM involved.

| Endpoint | Model | HuggingFace ID | Purpose | Latency | VRAM |
|----------|-------|----------------|---------|---------|------|
| `POST /translate` | NLLB-200-distilled-600M | `facebook/nllb-200-distilled-600M` | Non-English signal translation | ~650ms | ~1.2GB |
| `POST /classify` | DeBERTa-v3 zero-shot | `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` | Signal category classification | ~190ms | ~500MB |
| `POST /extract` | GLiREL-large | `jackboyla/glirel-large-v0` | Subject-predicate-object triple extraction | ~800ms | ~1.5GB |
| `POST /summarize` | T5-small | `google-t5/t5-small` | Cluster title summarization | ~600ms | ~250MB |

Total VRAM: ~3.9GB of 16GB. First call after startup is ~2s (model warmup).

**Client:** `src/legba/ingestion/models_client.py` — `ModelsClient` class with graceful degradation. If the service is unavailable, ingestion falls back to regex classification and raw titles. Health checks run periodically.

**Access:** Internal containers use `http://legba-models:8700` (no auth) on the `fastchat` Docker network. External access requires HTTP Basic Auth over HTTPS.

**Supported translation languages:** ar, fa, he, ru, en, zh, fr, es, de, uk, tr.

**Input truncation:** All text inputs truncated to 512 tokens at the model level. The client truncates to 1000-2000 chars before sending.

## 4. Subconscious SLM — Llama 3.1 8B

The subconscious service uses a small language model for continuous validation and enrichment between conscious agent cycles. Runs as a quantized (Q5_K_M) Llama 3.1 8B Instruct served via vLLM on a dedicated GPU.

| Parameter | Value |
|-----------|-------|
| Model | Llama 3.1 8B Instruct (Q5_K_M quantization) |
| Hardware | GPU 1 on ai1 |
| Throughput | ~40 tokens/sec |
| Temperature | 0.1 (deterministic validation) |
| Endpoint | `/v1/chat/completions` (OpenAI-compatible) |
| Structured output | `guided_json` (vLLM constrained decoding) |

**Tasks:** Signal quality assessment, entity resolution, classification refinement, fact corroboration, graph consistency checks, relationship validation. Runs three concurrent async loops (NATS consumer, timer, differential accumulator).

**Key file:** `src/legba/subconscious/service.py`

## 5. spaCy NER — en_core_web_trf

The ingestion pipeline uses spaCy's transformer-based NER model for deterministic entity extraction from signals. Runs in-process on CPU within the ingestion container — no external endpoint.

**Used for:** Batch entity linking during ingestion. Extracts person, organization, location, and other named entities from signal text, linking them to existing entity profiles in Postgres.

## 6. Hybrid LLM Routing (Prepped, Dormant)

Infrastructure exists to route specific cycle types to an alternate LLM provider (e.g., Anthropic Claude for ANALYSIS/INTROSPECTION, GPT-OSS for everything else). Not currently active in production.

**How it works:**

1. `LLMConfig.for_cycle_type()` reads `LLM_PROVIDER_MAP` env var (JSON dict mapping cycle type to provider name).
2. In the WAKE phase (`src/legba/agent/phases/wake.py`), after determining the cycle type, calls `for_cycle_type()`. If a mapping exists, constructs an alternate `LLMConfig` from `LLM_ALT_*` env vars.
3. The alternate config is used to create the `LLMClient` for that cycle.

**AnthropicProvider** (`src/legba/agent/llm/anthropic_provider.py`) is fully implemented:
- Uses `/v1/messages` with proper system role separation
- `max_tokens` required (default 16,384)
- `x-api-key` header authentication
- Normalizes Anthropic's `input_tokens`/`output_tokens` and `stop_reason` to match VLLMProvider's interface
- Default model: `claude-sonnet-4-20250514`, temperature 0.7
- No Harmony token handling needed

**Example activation** (not currently set):
```
LLM_PROVIDER_MAP={"ANALYSIS": "anthropic", "INTROSPECTION": "anthropic"}
LLM_ALT_API_BASE=https://api.anthropic.com
LLM_ALT_API_KEY=&lt;api-key&gt;
LLM_ALT_MODEL=claude-sonnet-4-20250514
LLM_ALT_TEMPERATURE=0.7
```

**Cost note:** An 18-cycle test with Claude Sonnet showed ~$900/day token cost. Not viable for continuous personal use; the hybrid approach would selectively use Claude only for high-value cycle types.

## 7. Consultation Engine

The operator-facing "Working" interface (`/consult`) uses its own LLM config, independent of the agent.

| Parameter | Default | Env Var |
|-----------|---------|---------|
| Provider | `anthropic` | `CONSULT_LLM_PROVIDER` |
| Model | `claude-sonnet-4-20250514` | `CONSULT_MODEL` |
| Temperature | `0.7` | `CONSULT_TEMPERATURE` |
| Timeout | `300s` | `CONSULT_TIMEOUT` |
| API Key | (from .env) | `CONSULT_API_KEY` |

The consultation engine reuses both `VLLMProvider` and `AnthropicProvider` but keeps its own conversation management (Redis sessions, 1-hour TTL, max 10 tool steps per exchange). It has access to Legba's knowledge stores via a subset of tools (event search, entity lookup, graph queries, etc.).

**Key file:** `src/legba/ui/consult.py`

## 8. Configuration Reference

### Primary LLM (Agent)

| Env Var | Default | Description |
|---------|---------|-------------|
| `LLM_PROVIDER` | `vllm` | Provider type: `vllm` or `anthropic` |
| `OPENAI_BASE_URL` | `http://localhost:8000/v1` | vLLM API base URL |
| `OPENAI_API_KEY` | (empty) | API key for vLLM endpoint |
| `OPENAI_MODEL` | `InnoGPT-1` | Model name passed in requests |
| `LLM_TEMPERATURE` | `1.0` | Sampling temperature (overridden to 1.0 in provider for GPT-OSS) |
| `LLM_TOP_P` | `0.9` | Top-p sampling |
| `LLM_MAX_TOKENS` | `4096` | Max output tokens (env default; code default 16,384) |
| `LLM_MAX_CONTEXT_TOKENS` | `128000` | Context window size |
| `LLM_TIMEOUT` | `180` | Request timeout in seconds |

### Embedding

| Env Var | Default | Description |
|---------|---------|-------------|
| `EMBEDDING_API_BASE` | (falls back to `OPENAI_BASE_URL`) | Embedding endpoint base URL |
| `EMBEDDING_API_KEY` | (falls back to `OPENAI_API_KEY`) | Embedding endpoint API key |
| `MEMORY_EMBEDDING_MODEL` | `embedding-inno1` | Embedding model name |
| `MEMORY_VECTOR_DIMENSIONS` | `1024` | Vector dimensions |

### GPU Models Service (Ingestion)

| Env Var | Default | Description |
|---------|---------|-------------|
| `MODELS_API_URL` | (empty) | Models service base URL |
| `MODELS_API_USER` | (empty) | Basic auth username |
| `MODELS_API_PASS` | (empty) | Basic auth password |

### Hybrid Routing (Dormant)

| Env Var | Default | Description |
|---------|---------|-------------|
| `LLM_PROVIDER_MAP` | (empty) | JSON dict: `{"CYCLE_TYPE": "provider"}` |
| `LLM_ALT_API_BASE` | (falls back to main) | Alternate provider base URL |
| `LLM_ALT_API_KEY` | (falls back to main) | Alternate provider API key |
| `LLM_ALT_MODEL` | (falls back to main) | Alternate provider model |
| `LLM_ALT_MAX_TOKENS` | (falls back to main) | Alternate max tokens |
| `LLM_ALT_TEMPERATURE` | `0.7` | Alternate temperature |
| `LLM_ALT_TIMEOUT` | (falls back to main) | Alternate timeout |

### Consultation

| Env Var | Default | Description |
|---------|---------|-------------|
| `CONSULT_LLM_PROVIDER` | (falls back to main `LLM_PROVIDER`) | Consultation provider |
| `CONSULT_API_BASE` | (falls back to main) | Consultation API base |
| `CONSULT_API_KEY` | (falls back to main) | Consultation API key |
| `CONSULT_MODEL` | (falls back to main) | Consultation model |
| `CONSULT_MAX_TOKENS` | (falls back to main) | Consultation max tokens |
| `CONSULT_TEMPERATURE` | (falls back to main) | Consultation temperature |
| `CONSULT_TIMEOUT` | (falls back to main) | Consultation timeout |

## 9. Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────┐
│                         &lt;gpu-host&gt;                     │
│                                                                     │
│  GPU 2+3 (2x A6000 48GB)          GPU 0 (Tesla T4 16GB)           │
│  ┌──────────────────────┐          ┌─────────────────────────────┐  │
│  │  vLLM (tensor parallel)│         │ embedding-inno1 (~1.3GB)   │  │
│  │  Model: InnoGPT-1     │         │ legba-models (~3.9GB)      │  │
│  │  /v1/chat/completions  │         │  ├─ NLLB-200 (translate)   │  │
│  │  /v1/embeddings        │         │  ├─ DeBERTa-v3 (classify)  │  │
│  └────────┬───────────────┘         │  ├─ GLiREL-large (extract)  │  │
│           │                         │  └─ T5-small (summarize)   │  │
│           │                         └──────┬──────────────────────┘  │
└───────────┼────────────────────────────────┼────────────────────────┘
            │                                │
    ┌───────┼────────────────────────────────┼───────────────┐
    │       │       Legba Containers         │               │
    │       │                                │               │
    │  ┌────▼─────────────┐            ┌─────▼────────────┐  │
    │  │  Agent            │            │  Ingestion        │  │
    │  │  ├─ LLM calls ────┼──► vLLM    │  ├─ translate ───┼──► models
    │  │  ├─ embeddings ───┼──► embed   │  ├─ classify ────┼──► models
    │  │  └─ tool loop     │            │  ├─ extract ─────┼──► models
    │  └───────────────────┘            │  ├─ summarize ───┼──► models
    │                                   │  └─ embeddings ──┼──► embed
    │  ┌───────────────────┐            └──────────────────┘  │
    │  │  UI / Consult     │                                  │
    │  │  └─ LLM calls ────┼──► vLLM or Anthropic API        │
    │  └───────────────────┘                                  │
    └─────────────────────────────────────────────────────────┘
```

**Agent** uses vLLM for all reasoning (single-turn chat completions) and vLLM's embedding endpoint for episodic memory vectors.

**Ingestion** uses the GPU models service for deterministic NLP enrichment (no LLM) and the embedding endpoint for signal vectors in Qdrant. All models-service calls degrade gracefully.

**Consultation** defaults to Anthropic Claude Sonnet but is configurable to use vLLM via env vars.

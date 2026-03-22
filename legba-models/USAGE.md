# Legba Models — API Usage & Integration Guide

**Service:** https://models.ai1.infra.innoscale.net
**Auth:** HTTP Basic Auth (same user list as sd.ai1, plus `legba` service account)
**Internal (no auth):** `http://legba-models:8700` from any container on the `fastchat` Docker network
**Deployed:** 2026-03-19
**Host:** ai1.infra.innoscale.net — GPU 0 (Tesla T4, 16GB)

---

## Service Account Credentials

For programmatic / ingestion use:
- **Username:** `legba`
- **Password:** `Lgb@M0d3ls!2026x`

---

## Endpoints

| Endpoint | Method | Purpose | Typical Latency |
|----------|--------|---------|-----------------|
| `/health` | GET | Health check + GPU memory stats | <5ms |
| `/translate` | POST | Translate non-English text → English | ~650ms |
| `/classify` | POST | Zero-shot text classification | ~190ms (warm) |
| `/extract` | POST | Relation/fact triple extraction | ~800ms |
| `/summarize` | POST | Multi-text summarization | ~600ms |

First call to each endpoint after startup is slower (~2s) due to model warmup. Subsequent calls hit the latencies above.

---

## Health Check

```bash
curl -u legba:Lgb@M0d3ls!2026x \
  https://models.ai1.infra.innoscale.net/health
```

Response:
```json
{
  "status": "ok",
  "gpu": true,
  "gpu_memory": {"allocated_mb": 3939.5, "reserved_mb": 3958.0},
  "models_loaded": true
}
```

---

## 1. Translation — POST /translate

Translates text from a source language to English (or another supported language) using NLLB-200-distilled-600M.

**When to call:** At ingestion time, if a signal's detected language is not English. Translate title + first 500 chars of content. Store both original and translated text. All downstream processing (NER, classification, clustering, LLM) sees the English version.

### Supported Languages

| Code | Language |
|------|----------|
| `ar` | Arabic |
| `fa` | Farsi / Persian |
| `he` | Hebrew |
| `ru` | Russian |
| `en` | English |
| `zh` | Chinese (Simplified) |
| `fr` | French |
| `es` | Spanish |
| `de` | German |
| `uk` | Ukrainian |
| `tr` | Turkish |

### Request

```bash
curl -u legba:Lgb@M0d3ls!2026x \
  -X POST https://models.ai1.infra.innoscale.net/translate \
  -H "Content-Type: application/json" \
  -d '{
    "text": "صواريخ باليستية أطلقتها إيران على أهداف إسرائيلية",
    "source_lang": "ar",
    "target_lang": "en"
  }'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | yes | — | Text to translate (truncated at 512 tokens) |
| `source_lang` | string | yes | — | ISO 639-1 code from table above |
| `target_lang` | string | no | `"en"` | Target language code |

### Response

```json
{
  "translated": "Ballistic missiles launched by Iran targeted Israeli targets.",
  "source_lang": "ar",
  "target_lang": "en",
  "ms": 646.8
}
```

### Integration Point

`ingestion/normalizer.py` — after fetch, before NER:

```python
async def translate_if_needed(self, signal):
    if signal.detected_language and signal.detected_language != "en":
        resp = await self._http.post(f"{self._models_url}/translate", json={
            "text": signal.title + " " + signal.content[:500],
            "source_lang": signal.detected_language,
        })
        if resp.status_code == 200:
            data = resp.json()
            signal.original_title = signal.title
            signal.original_content = signal.content
            signal.title = data["translated"]  # or split title/content
    return signal
```

---

## 2. Classification — POST /classify

Zero-shot classification using DeBERTa-v3. Classifies text against category labels without any fine-tuning — labels are passed as natural language hypotheses to an NLI model.

**When to call:** After normalization (and translation if applicable), classify each signal by its title (+ optionally first N chars of summary).

### Request — Default Categories

```bash
curl -u legba:Lgb@M0d3ls!2026x \
  -X POST https://models.ai1.infra.innoscale.net/classify \
  -H "Content-Type: application/json" \
  -d '{"text": "Iran launched ballistic missiles at Israeli military targets"}'
```

When `labels` is omitted, the server uses built-in categories and returns short names:

`conflict`, `political`, `economic`, `health`, `environment`, `technology`, `disaster`, `social`, `sports`

Response:
```json
{
  "category": "conflict",
  "confidence": 0.9889,
  "scores": {
    "conflict": 0.9889,
    "political": 0.0092,
    "disaster": 0.0003,
    "technology": 0.0003,
    "health": 0.0003,
    "environment": 0.0003,
    "economic": 0.0003,
    "social": 0.0002,
    "sports": 0.0002
  },
  "ms": 188.4
}
```

### Request — Custom Labels

Pass your own labels for flexible re-classification or sub-categorization:

```bash
curl -u legba:Lgb@M0d3ls!2026x \
  -X POST https://models.ai1.infra.innoscale.net/classify \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Federal Reserve raises interest rates by 25 basis points",
    "labels": ["monetary policy", "trade", "employment", "housing"]
  }'
```

Response:
```json
{
  "category": "monetary policy",
  "confidence": 0.9986,
  "scores": {
    "monetary policy": 0.9986,
    "trade": 0.0009,
    "employment": 0.0003,
    "housing": 0.0002
  },
  "ms": 188.4
}
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `text` | string | yes | — | Text to classify |
| `labels` | list[string] | no | server defaults | Custom category labels (natural language) |

### Integration Point

`ingestion/normalizer.py` — replaces `_classify_category()` regex function:

```python
async def classify(self, text: str) -> tuple[str, float]:
    resp = await self._http.post(f"{self._models_url}/classify", json={
        "text": text,
    })
    if resp.status_code == 200:
        data = resp.json()
        return data["category"], data["confidence"]
    return "other", 0.0  # fallback if service unavailable
```

---

## 3. Relation Extraction — POST /extract

Extracts structured (subject, predicate, object) triples from text using REBEL-large.

**When to call:** After NER, before storage. Extract triples from signal title + summary. Store as facts with `source_cycle=0` (ingestion-generated). Normalize predicates against existing vocabulary.

### Request

```bash
curl -u legba:Lgb@M0d3ls!2026x \
  -X POST https://models.ai1.infra.innoscale.net/extract \
  -H "Content-Type: application/json" \
  -d '{
    "text": "Barack Obama was born in Hawaii. He served as the 44th president of the United States."
  }'
```

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `text` | string | yes | Text to extract relations from (truncated at 512 tokens) |

### Response

```json
{
  "triples": [
    {"subject": "Barack Obama", "predicate": "place of birth", "object": "Hawaii"},
    {"subject": "Barack Obama", "predicate": "position held", "object": "president of the United States"}
  ],
  "ms": 808.9
}
```

### Integration Point

`ingestion/normalizer.py` or new `ingestion/enrichment.py` — after NER, before storage:

```python
async def extract_triples(self, text: str) -> list[dict]:
    resp = await self._http.post(f"{self._models_url}/extract", json={
        "text": text,
    })
    if resp.status_code == 200:
        data = resp.json()
        return data["triples"]  # list of {subject, predicate, object}
    return []
```

Each triple maps to a row in the facts table:
```sql
INSERT INTO facts (subject, predicate, object, source_cycle, source_signal_id)
VALUES (%s, %s, %s, 0, %s);
```

---

## 4. Summarization — POST /summarize

Generates a one-sentence summary from multiple texts using T5-small.

**When to call:** After clustering, when a cluster of 3+ signals forms an event. Summarize the concatenated titles to produce a clean event title.

### Request

```bash
curl -u legba:Lgb@M0d3ls!2026x \
  -X POST https://models.ai1.infra.innoscale.net/summarize \
  -H "Content-Type: application/json" \
  -d '{
    "texts": [
      "Earthquake strikes central Turkey, magnitude 6.2",
      "Rescue teams deployed across 3 provinces in Turkey",
      "Turkey earthquake death toll rises to 14, hundreds injured"
    ],
    "max_length": 64
  }'
```

| Field | Type | Required | Default | Description |
|-------|------|----------|---------|-------------|
| `texts` | list[string] | yes | — | List of texts to summarize (joined with ` \| ` internally) |
| `max_length` | int | no | `64` | Max tokens in generated summary |

### Response

```json
{
  "summary": "earthquake death toll rises to 14, hundreds injured.",
  "ms": 597.0
}
```

### Integration Point

`ingestion/cluster.py` — in `_create_event_from_cluster()`:

```python
async def summarize_cluster(self, signals: list) -> str:
    titles = [s.title for s in signals]
    resp = await self._http.post(f"{self._models_url}/summarize", json={
        "texts": titles,
    })
    if resp.status_code == 200:
        return resp.json()["summary"]
    # Fallback: use highest-confidence signal's title
    return max(signals, key=lambda s: s.confidence).title
```

---

## Ingestion Service Integration (Full Pattern)

Per the original spec in `new-models.md`, the integration goes into `ingestion/service.py`:

```python
import httpx

class ModelsClient:
    """HTTP client for the Legba models service. Graceful degradation on failure."""

    def __init__(self, base_url: str = "http://legba-models:8700"):
        self._url = base_url
        self._http = httpx.AsyncClient(base_url=base_url, timeout=30.0)
        self._available = False

    async def check_health(self) -> bool:
        try:
            resp = await self._http.get("/health")
            self._available = resp.status_code == 200 and resp.json().get("status") == "ok"
        except Exception:
            self._available = False
        return self._available

    @property
    def available(self) -> bool:
        return self._available

    async def translate_if_needed(self, signal):
        if not self._available or not signal.detected_language:
            return signal
        if signal.detected_language == "en":
            return signal
        try:
            resp = await self._http.post("/translate", json={
                "text": (signal.title + " " + signal.content[:500]).strip(),
                "source_lang": signal.detected_language,
            })
            if resp.status_code == 200:
                data = resp.json()
                signal.original_title = signal.title
                signal.title = data["translated"]
        except Exception:
            pass  # degrade gracefully
        return signal

    async def classify(self, text: str) -> tuple[str, float]:
        if not self._available:
            return "other", 0.0
        try:
            resp = await self._http.post("/classify", json={"text": text})
            if resp.status_code == 200:
                data = resp.json()
                return data["category"], data["confidence"]
        except Exception:
            pass
        return "other", 0.0

    async def extract_triples(self, text: str) -> list[dict]:
        if not self._available:
            return []
        try:
            resp = await self._http.post("/extract", json={"text": text})
            if resp.status_code == 200:
                return resp.json()["triples"]
        except Exception:
            pass
        return []

    async def summarize(self, texts: list[str]) -> str | None:
        if not self._available:
            return None
        try:
            resp = await self._http.post("/summarize", json={"texts": texts})
            if resp.status_code == 200:
                return resp.json()["summary"]
        except Exception:
            pass
        return None
```

Usage in `_process_signal()`:

```python
# After normalizer.normalize():
if self._models.available:
    signal = await self._models.translate_if_needed(signal)
    signal.category, signal.category_confidence = await self._models.classify(signal.title)
    signal.facts = await self._models.extract_triples(signal.title + " " + signal.summary)

# After clustering:
if cluster.signal_count >= 3 and self._models.available:
    summary = await self._models.summarize([s.title for s in cluster.signals])
    if summary:
        cluster.title = summary
```

**Graceful degradation:** If the models service is down, ingestion continues with regex classification and raw titles (current behavior). No hard dependency. Call `check_health()` periodically (e.g. every 60s) to detect recovery.

---

## Infrastructure Details

### GPU Layout (ai1.infra.innoscale.net)

| GPU | Device | Services | VRAM Used |
|-----|--------|----------|-----------|
| 0 | Tesla T4 (16GB) | Embeddings (~1.3GB) + Whisper STT (~3.3GB) + **Legba models (~3.9GB)** | ~8.6GB |
| 1 | Tesla T4 (16GB) | ComfyUI + Kokoro TTS | ~5.5GB |
| 2 | A6000 (48GB) | vLLM TP0 | ~45.5GB |
| 3 | A6000 (48GB) | vLLM TP1 | ~45.5GB |

### Models Loaded

| Model | HuggingFace ID | Purpose | VRAM |
|-------|---------------|---------|------|
| NLLB-200-distilled-600M | `facebook/nllb-200-distilled-600M` | Translation | ~1.2GB |
| DeBERTa-v3 zero-shot | `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` | Classification | ~500MB |
| REBEL-large | `Babelscape/rebel-large` | Relation extraction | ~1.5GB |
| T5-small | `google-t5/t5-small` | Summarization | ~250MB |

### File Paths on ai1

| Path | Contents |
|------|----------|
| `/usr/local/deployments/legba-models/` | Dockerfile, docker-compose.yml, entrypoint.sh, app/ |
| `/usr/local/deployments/caddy/etc/conf.d/models.ai1.infra.innoscale.net.caddy` | Caddy reverse proxy + basic auth |
| `/mnt/data/models/legba/venv/` | Python venv (torch 2.6.0+cu124, transformers, FastAPI) |
| `/mnt/data/models/legba/hf-cache/` | HuggingFace model weights (~5-6GB) |

### Container Management

```bash
cd /usr/local/deployments/legba-models

# Logs
docker logs legba-models --tail 50

# Restart
docker compose restart

# Rebuild (after code changes)
docker compose build && docker compose up -d

# Force fresh venv rebuild (after requirements change)
rm -rf /mnt/data/models/legba/venv
docker compose restart
```

### Notes

- **Classifier swap:** Original spec called for fine-tuned DistilBERT. Deployed DeBERTa-v3 zero-shot instead — works immediately without training data, flexible label set, comparable VRAM. Can swap to fine-tuned model later if needed; the zero-shot output can serve as training data.
- **Internal access:** From any container on the `fastchat` Docker network, use `http://legba-models:8700` (no auth needed). External access requires basic auth over HTTPS.
- **Log rotation:** Configured at 100MB x 3 files in docker-compose.yml.
- **Input truncation:** All text inputs are truncated to 512 tokens at the model level. For longer content, send title + first ~500 chars.

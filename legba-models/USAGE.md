# legba-models

For someone hosting Legba's NLP service. `legba-models` is a FastAPI service that runs four small
transformer models on a GPU (it falls back to CPU) behind one HTTP surface: translation, zero-shot
classification, relation extraction and summarisation, plus spaCy named entities. There is no LLM in
it. The Legba runtime calls it at ingest through the `nlp.local.legba_models` stack component
([docs/AI_MODELS.md](../docs/AI_MODELS.md)).

## Running it

The directory holds its own `Dockerfile`, `docker-compose.yml` and `entrypoint.sh`.

```bash
cd legba-models
docker network create fastchat     # the compose file joins this external network; or edit it
docker compose up -d --build
docker logs legba-models --tail 50
```

- It listens on port 8700 inside the container and publishes no host port; reach it from
  containers on the same network as `http://legba-models:8700`, or put a reverse proxy with TLS in
  front of it for another host.
- The compose file mounts `/mnt/data/models/legba` as `/data`. The first start creates a Python
  virtual environment there (PyTorch for CUDA 12.4 plus `app/requirements.txt`) and downloads the
  models (about 5–6 GB) before it accepts traffic. Change the mount if your disk is elsewhere.
- It reserves GPU 0 (`NVIDIA_VISIBLE_DEVICES=0`). Edit the `deploy.resources` block to use another.
- Logs rotate at 100 MB × 3.
- The entrypoint reinstalls packages when the hash of `app/requirements.txt` changes; to start
  clean, remove `/data/venv` and restart.

`docker-compose.slm.yml` adds an optional small chat model served by llama.cpp (`legba-slm`, on
`127.0.0.1:8701`); register it as a stack component if you use it.

## Authentication

- **In the service.** Set `LEGBA_MODELS_API_SECRET` on the container and every inference endpoint
  requires a matching `X-Models-Secret` header. Unset, the service has no auth of its own.
  `/health` is always open.
- **At a reverse proxy.** For access from another host, put HTTP basic auth and TLS in front.

The runtime's client sends basic auth and the `X-Models-Secret` header when they are configured
(`MODELS_API_USER`, `MODELS_API_PASS` and `MODELS_API_SECRET` in `.env.example`).

## Endpoints

| Endpoint | Method | Model | Purpose |
|---|---|---|---|
| `/health` | GET | | status, GPU availability and memory |
| `/translate` | POST | `facebook/nllb-200-distilled-1.3B` | translate to English or another supported language |
| `/classify` | POST | `MoritzLaurer/deberta-v3-base-zeroshot-v2.0` | zero-shot classification |
| `/extract` | POST | spaCy NER + `jackboyla/glirel-large-v0` | subject–predicate–object triples |
| `/ner` | POST | spaCy | named entities, split into actors and locations |
| `/summarize` | POST | `google-t5/t5-small` | one short summary of several texts |

Every response carries `ms`, the server-side time. The first call to each model after start is
slower while it warms up.

### `/health`

```json
{"status": "ok", "gpu": true, "gpu_memory": {"allocated_mb": 3939.5, "reserved_mb": 3958.0}, "models_loaded": true}
```

`status` is `loading` until the models are in memory.

### `/translate`

| Field | Required | Default | Meaning |
|---|---|---|---|
| `text` | yes | | text to translate; truncated at 512 tokens |
| `source_lang` | yes | | ISO 639-1 code |
| `target_lang` | no | `en` | ISO 639-1 code |

Supported codes: `ar`, `de`, `en`, `es`, `fa`, `fr`, `he`, `hi`, `id`, `it`, `ja`, `ko`, `nl`,
`pl`, `pt`, `ru`, `th`, `tr`, `uk`, `ur`, `vi`, `zh` (Simplified). An unsupported `source_lang` is an
error. The NLLB weights carry a non-commercial licence; check it against your use.

```bash
curl -s -X POST http://legba-models:8700/translate -H 'Content-Type: application/json' \
  -d '{"text": "Le port a rouvert mardi.", "source_lang": "fr"}'
# {"translated": "The port reopened on Tuesday.", "source_lang": "fr", "target_lang": "en", "ms": 210.4}
```

(Illustrative output.)

### `/classify`

| Field | Required | Default | Meaning |
|---|---|---|---|
| `text` | yes | | text to classify |
| `labels` | no | built-in categories | your own labels, as plain phrases |

Without `labels`, the answer uses short names: `conflict`, `political`, `economic`, `health`,
`environment`, `technology`, `disaster`, `social`. With `labels`, it returns your labels as given.
The response holds `category`, `confidence` and `scores` for every label.

### `/extract`

| Field | Required | Default | Meaning |
|---|---|---|---|
| `text` | yes | | text; the first 2,000 characters are used |
| `labels` | no | 20 built-in labels (`DEFAULT_RELATION_LABELS` in `app/main.py`) | relations to look for |
| `threshold` | no | 0.3 | minimum relation score |

spaCy finds the entities, then GLiREL scores relations between them. Text with fewer than two
entities returns no triples. Each triple has `subject`, `predicate`, `object` and, when the entity
can be traced, character offsets (`subject_start`, `subject_end`, `object_start`, `object_end`).

```bash
curl -s -X POST http://legba-models:8700/extract -H 'Content-Type: application/json' \
  -d '{"text": "Example Corp opened a plant in City A."}'
```

### `/ner`

Takes `text` (first 2,000 characters). Returns `entities` (text, spaCy label, offsets), plus
`actors` (people, organisations, groups) and `locations` (places and facilities).

### `/summarize`

| Field | Required | Default | Meaning |
|---|---|---|---|
| `texts` | yes | | texts, joined with a vertical bar before summarising |
| `max_length` | no | 64 | maximum tokens in the summary |

Input is truncated at 512 tokens.

## How Legba uses it

The runtime's NLP client (`src/legba/data/stack/nlp_service/client.py`) calls these endpoints from
the enrichment filters. When the service is unreachable, enrichment passes signals through
un-enriched and reports itself degraded; ingestion continues. An authentication failure is reported
separately, because the credential has to be fixed.

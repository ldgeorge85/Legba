<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# AI models

For someone connecting Legba to model endpoints. No model runs inside a Legba container. Every model
is reached over HTTP, and each endpoint is a **stack component**: a registry row naming the
endpoint, the model and a vault reference for the credential. Budgets and caps for each path are in
[TUNABLES.md](TUNABLES.md).

## Roles

| Role | What it does | Contract |
|---|---|---|
| chat model (the core plane) | every scheduled analyst: desks, compositions, voices, lenses, journal, reference builder; the default for consult | OpenAI-compatible `POST /v1/chat/completions` |
| embeddings | deduplication, semantic correlation, retrieval corpora, consult's vector search | OpenAI-compatible embeddings |
| NLP service | translation, zero-shot classification, relation extraction, summaries at ingest | the `legba-models` HTTP shapes (below) |
| faithfulness judge | grades every cited claim against its evidence | any chat component; by default the writer's own |
| graders | external audit, correctness grading, second rater | any chat component |
| consult | on-demand questions from the console | the core plane, or a hosted route a request picks |

## Connecting an OpenAI-compatible endpoint

Any server that speaks the OpenAI chat-completions API works for the chat, judge and grader roles:
vLLM, llama.cpp's server, a hosted router, or a hosted provider.

**At bring-up.** `scripts/bringup_register_stack.py` (run by `deploy/deploy.sh`) writes the core
components once from `.env`:

| Variable | Becomes |
|---|---|
| `LEGBA_LLM_API_ENDPOINT` | `llm.primary.openai_compat` `api_endpoint` (base host, no trailing `/v1`) |
| `LEGBA_LLM_MODEL_NAME` | its `model_name` |
| `LEGBA_LLM_THINKING_MODEL_NAME` | its `thinking_model_name` (optional, below) |
| `LEGBA_EMBED_MODEL_NAME` | the embedding component's model |
| `LEGBA_NLP_API_ENDPOINT` | `nlp.local.legba_models` endpoint |
| `OPENAI_API_KEY` | vault secret `llm.primary.api_key` (loaded by `scripts/bringup_vault_load.py`) |

Editing these variables later changes nothing; the registry row is the source of truth after
bring-up.

**Read at use time.** A component field may name an environment variable instead of a value:
`raw: "${env:NAME}"`. The registry stores the reference as written, so the stored body and its
hash are the same on every instance; the runtime and the registry read the value each time they
build the component. An unset or empty variable fails loudly, naming the variable and the
component; there is no default. The model-gateway components in `descriptors/` use this:

| Variable | Read by |
|---|---|
| `LEGBA_GATEWAY_API_ENDPOINT` | every gateway component's endpoint (base URL including `/v1`) |
| `LEGBA_GATEWAY_JUDGE_MODEL` | `llm.verify.gateway_judge.openai_compat` `model_name` |
| `LEGBA_GATEWAY_JUDGE_THINK_MODEL` | `llm.judge.gateway_rung2_think.openai_compat` `model_name` |
| `LEGBA_GATEWAY_GOLD_DRAFTER_MODEL` | `llm.verify.gateway_gold_drafter.openai_compat` `model_name` |
| `LEGBA_GATEWAY_GRADER_MODEL` | `llm.grader.gateway_third_family.openai_compat` `model_name` |
| `LEGBA_GATEWAY_DECIDE_MODEL` | `llm.decide.gateway_typed_choice` `model_name` |
| `LEGBA_GATEWAY_TRANSLATE_MODEL` | `nlp.gateway.bearer` `translate_model` |

A changed value takes effect when the component is next built: restart the services that use it.
A value written inline, without `${env:...}`, works as before.

**After bring-up.** Change or add a component with a registry PUT (or the console's settings panel):

```bash
curl -sX PUT -H "Authorization: Bearer $TOKEN" -H 'Content-Type: application/json' \
  $REG/api/v1/registry/stack/llm.judge.example.openai_compat -d @component.json
```

An LLM component (`schema_uri: legba/stack/llm_provider/1.0.0`) takes these `config` fields:

| Field | Meaning |
|---|---|
| `api_endpoint` | base URL; the handler adds `/v1/...` |
| `api_key`, or `api_user` + `api_pass` | vault references, never plaintext |
| `model_name` | the model name the server expects |
| `max_tokens` | the model's output limit; sent only where the provider requires it |
| `timeout_seconds` | per call, default 60 |
| `tier` | `primary`, `fallback` or `cheap` |
| `price_input_per_m`, `price_output_per_m` | USD per million tokens; absent means calls are recorded at zero cost |
| `daily_burn_alert_usd` | daily spend at which the production gauge pages |
| `max_concurrent` | in-flight calls per process (the core component uses the process gate instead) |
| `provider_ignore` | for routers: upstream providers this component must not be served by |
| `thinking_model_name` | the name that serves the same model with thinking on |
| `reasoning_budget_param`, `reasoning_budget_tokens` | an optional reasoning budget, set together |

Store the key first (`POST /api/v1/registry/vault/secrets`, see [RUNBOOK.md](RUNBOOK.md) §4), then
the component. A running actor keeps its old binding until its descriptor head changes or the
runtime is recreated.

**Which handler a component uses** is read from its id (`infer_llm_subprovider` in
`runtime/analyst_deps_builder.py`):

| Id contains | Handler | Wire API |
|---|---|---|
| `.decide.` | decision client | `POST /v1/decide`: a typed choice with a probability per option |
| `.anthropic.` or starts `llm.anthropic` | Anthropic | `POST /v1/messages`; sends an output ceiling, which the provider requires |
| ends `.openai_compat` | OpenAI-compatible | `POST /v1/chat/completions` |
| `.openai.` or starts `llm.openai` | OpenAI | `POST /v1/chat/completions`; reasoning models get their completion-token parameter |
| anything else | OpenAI-compatible | as above (or by endpoint host for `api.anthropic.com` / `api.openai.com`) |

All handlers retry rate limits and server errors with backoff and honour `Retry-After`, count
prompt, completion and reasoning tokens on the receipt, and send temperature 1.0. No handler sends a
caller's `max_tokens`. The Anthropic handler sends the model's documented maximum output. The
OpenAI-compatible handler sends the maximum output the stack component declares for the alias a call
names (`max_output_tokens`, and `thinking_max_output_tokens` for the thinking alias), less only when
the counted input leaves less room in the declared `context_window_tokens`; an alias with no declared
maximum gets no `max_tokens` and the endpoint's default applies. `LEGBA_LLM_SEND_MAX_TOKENS=0` turns
sending off. The declared window also sizes the input budgets (`LEGBA_LLM_INPUT_TOKEN_BUDGET`
overrides them when set).

**Pointing an analyst at a component.** A descriptor binds through `method.llm.primary`,
`method.llm.verify` or `method.llm.narrate`, each a stack reference. Component ids are stable names;
the model behind one is a config field.

## The `legba-models` NLP service

A small FastAPI service, usually on a GPU host, with no LLM in it ([legba-models/USAGE.md](../legba-models/USAGE.md)):

| Endpoint | Model | Purpose |
|---|---|---|
| `/translate` | NLLB-200 distilled 1.3B | non-English text to English |
| `/classify` | DeBERTa-v3 zero-shot | topic classification |
| `/extract` | spaCy NER + GLiREL large | subject–predicate–object triples |
| `/ner` | spaCy | named entities |
| `/summarize` | T5 small | one-line summaries |
| `/health` | | status and GPU memory |

It is the `nlp.local.legba_models` component. An unavailable service makes enrichment pass the
signal through un-enriched and mark itself degraded; ingestion does not stop. An authentication
failure is a separate error, because an admin has to fix the credential.

The runtime binds one NLP component, named by `LEGBA_NLP_COMPONENT_ID` (default
`nlp.local.legba_models`), read once at start. A second client (`provider: bearer_gateway` on the
component, `data/stack/nlp_service/gateway.py`) speaks the same contract to a gateway that serves
`/relations`, `/entities`, `/classify` and an OpenAI-compatible chat route; it translates through
the chat model the component names as `translate_model` and does not summarise.

## The faithfulness judge

Every cited finding, composition and voice goes through the verify pass: a deterministic check that
citations are present, then, with `LEGBA_VERIFY_LLM_JUDGE=1`, a judge model that reads each claim
beside the evidence it cites. The result is a faithfulness score in `[0, 1]`, stored as a critique
that records which model judged it and which pipeline version.
`effective_confidence = min(confidence, faithfulness)`. A composition only reads findings that have
a critique, and a low score demotes a claim without deleting it.

**Which route judges.** An analyst is judged only if its descriptor declares `method.llm.verify`
or `method.llm.judge`. The route is then `LEGBA_JUDGE_STACK_REF` if set, else `method.llm.judge`,
else `method.llm.verify`. The shipped descriptors set `verify` to the core component, so by default
the judge is the same model that wrote the claim. Set `LEGBA_JUDGE_STACK_REF` to a component from
another model family to change that for every judged analyst.

**When the judge route fails.** `method.llm.judge_chain` (or `LEGBA_JUDGE_CHAIN_DEFAULT`) lists
fallback rungs after the resolved route; the default is `llm.verify.slm_8b`, a small self-hosted
model, if your registry has it. A 401, 402, 403 or 404 moves the rest of that verify pass to the
next rung; a 429, 5xx or timeout retries on the same rung. A verdict from a later rung records
`judge_route = chain:<component_id>`. Rungs listed in `LEGBA_JUDGE_WEAK_RUNGS` mark their verdicts
`judge_chain.weak = true`, so accuracy figures do not pool them with the main judge. When every rung
fails, the critique is the citation floor with `verification.unverified_reason`, and readers show
"unverified since T".

A floor critique is not re-judged automatically when the route comes back.
`scripts/rejudge_floor_critiques.py` counts them (dry run) and re-judges them with
`--apply --limit N` ([RUNBOOK.md](RUNBOOK.md) §5). The watchdog pages at once when a hosted route
returns 401, 402, 403 or 404 (`llm_route_auth_failed`), and
`GET /v3/system/judge-stats/health` reports per judge component the newest success and failure and
`unverified_since`.

**Citations.** The parser accepts full-width brackets, reads `[11, 47]` as two markers, and moves a
marker written after a sentence's full stop back onto that sentence. The bounded units' prompts
require every factual sentence to end with a marker; the critique counts the sentences that do not,
reported per unit per day as `uncited` on `/system/judge-stats`.

## The decision shadow

With `bearing_shadow_ref` set on the `claim_watch` descriptor (unset by default), every pair the
bearing gate asked its chat model about ("does this item bear on that open question?") is asked
again of a second component, after the gate has written its edges. The shadow changes no decision.
Each pair lands in `decision_shadow_ledger` with both answers; `scripts/decision_shadow_report.py`
reports agreement, the confusion matrix and calibration.

## Embeddings

Vectors live in Qdrant. Changing the embedding model is a registry update followed by a re-embed of
the corpus; vectors from two models are not comparable.

## Consult

Consult and deep consult choose their plane per request from a server-side allowlist: the core
plane by default, a hosted component only when the request names it. A plane that cannot be used
raises an error; it never falls back silently. A turn runs a tool loop over read tools with a
wall-clock deadline, a visible cost, a stop that keeps the partial turn, and a final synthesis. Cost
ceilings are in [TUNABLES.md](TUNABLES.md) §3.

## Thinking

Some servers expose one model under two names, one with reasoning off and one with it on. Legba
chooses which name to send in one place, `src/legba/data/stack/llm/thinking.py`:

- **Per component.** `thinking_model_name` is the reasoning name. A component without it refuses a
  thinking call.
- **Per descriptor.** `method.llm.thinking: "on"` (quoted; bare YAML `on` is a boolean) makes the
  analyst's primary calls think. `method.gather.thinking` and `method.llm.narrate.thinking` choose
  for the gather rounds and the narrating voice separately. Thinking inside `verify` or `judge` is
  refused at registration. To see which descriptors think: `git grep -l 'thinking: "on"' descriptors`.
- **Consult** thinks on tool rounds, the final answer and deep consult's plan, not on repair or
  extraction calls. A request can turn it off for one turn.

Reasoning text is kept out of the answer and counted only as `reasoning_tokens`. Each call that
thinks carries `"thinking": true` on its receipt. If a thinking call fails for a reason the
reasoning name causes (tools refused, unsupported parameter, unknown model, unparseable reasoning,
other 400/422), it is retried once with the plain name; the retry's receipt carries
`thinking_fallback: <reason>` and the runtime raises the `thinking_fallback` alert
([ALERTING.md](ALERTING.md)). Transient errors, auth errors and context-length errors are not
retried this way.

## Concurrency on the core component

`src/legba/data/stack/llm/call_gate.py` holds one process-wide limit on in-flight calls to the core
component: `LEGBA_CORE_LLM_MAX_CONCURRENT` (default 12), of which
`LEGBA_CORE_LLM_INTERACTIVE_RESERVED` (default 2) only consult calls may use. Each class is first in,
first out; the gate never drops a call. A call that waited carries `gate_wait_ms`, a wait of a
second or more logs `llm.gate.waited`, and the runtime's `/healthz` reports `llm_gates`.

## Grounding

Every model has a training cutoff. Before a bounded unit runs, it reads current facts about the
entities and situations in its slice from the substrate and receives them as a non-citable preamble.
A failed read runs the unit without grounding. Vector-retrieval grounding is off except on units
that opt in, each with a per-run rollback guard ([OPERATING_YOUR_INSTANCE.md](OPERATING_YOUR_INSTANCE.md) §2).

## Media extraction

Signals with audio, image or video go to a media extractor by modality. The job plane and the
envelope are built; the `legba-media` service refuses every extraction (HTTP 503) until a model
backend is wired ([legba-media/USAGE.md](../legba-media/USAGE.md), [SEAMS.md](SEAMS.md) #1).

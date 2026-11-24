<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# AI models

Every model Legba uses, where it is served, what it is for, and how the runtime reaches it. No model
runs inside a Legba container: each is reached over HTTP against an out-of-process serving host, with
its endpoint and credentials resolved through the stack registry and the credential vault. Which plane
pays for which path is one table in [TUNABLES.md](TUNABLES.md).

## Three roles

1. **Baseline enrichment.** Small deterministic transformer models (translation, zero-shot
   classification, relation extraction, summarisation) behind the `legba-models` service. A source
   uses them once per signal, at acquisition, before the signal fans out.
2. **The analyst plane and embeddings.** A self-hosted OpenAI-compatible model is the core plane: it
   runs the bounded units, the compositions, the voices, the lenses, the journal, the reference
   builder and every other scheduled analyst, at no per-run cost; it is also the default plane for
   consult. An embedding model on the same host backs deduplication, semantic correlation and the
   retrieval corpora. Hosted Anthropic models serve consult and deep consult only, and only when a
   person explicitly picks one for a request.
3. **The graders.** The faithfulness judge scores every cited finding for whether each claim follows
   from its evidence. The external audit grader and its second rater check top-layer claims against
   live web search. The correctness grader's paid families score the desks against references. Each
   resolves its route from the registry, and a deployment points them at a different model family
   from the writer so the platform does not grade itself with the model that wrote the claim; the
   judge's fallback rung is the self-hosted small model (below).

## The `legba-models` service

A small FastAPI service on a GPU host fronting four models behind one HTTP surface. There is no LLM in
it.

| Endpoint | Model | Purpose |
|---|---|---|
| `/translate` | NLLB-200 distilled 1.3B | non-English text to English before extraction |
| `/classify` | DeBERTa-v3 zero-shot | topic classification |
| `/extract` | GLiREL large | zero-shot subject–predicate–object relation extraction, with a real per-relation confidence |
| `/summarize` | T5 small | one-line summaries |
| `/health` | | service and GPU-memory status |

It is registered as the `nlp.local.legba_models` stack component: endpoint, the four paths, the
health path, a timeout, and two vault-backed secrets for the external basic-auth route. Enrichment
handlers bind to it through a stack reference; the runtime resolves the reference, decrypts the
credentials and hands the handler an async client with one method per endpoint. Failures are typed:
an unavailable service makes the handler pass the signal through un-enriched and flips its health to
degraded, so an outage slows enrichment and never stalls the pipeline; an authentication failure is a
distinct error because it needs an admin to rotate the credential.

The extraction endpoint is where entities and relations come from: the multilingual NER filter posts
the signal text, walks the returned triples, maps each entity to the closed entity-class taxonomy, and
carries the extractor's confidence onto the fact. Non-Latin bodies go through translation first.

## NLP on the gateway

The same client contract runs on the innoai gateway when the component says `provider:
innoai_gateway`: `nlp.innoai.gateway` (`descriptors/stack_component_nlp_innoai_gateway.yaml`), a
Bearer `api_key` vault ref (`llm.innoai.api_key`), endpoint `https://api.innoai.net/v1`. The client
(`data/stack/nlp_service/gateway.py`) answers in the legba-models shapes, so the NER filter, the fact
extractor and the classify filter run on it unchanged.

| Client method | Gateway route | Covered |
|---|---|---|
| `extract` | `POST /relations`, model `ner` — one call, answered as `{"triples": [{subject, predicate, object}]}` | yes |
| `entities` | `POST /entities`, model `ner` | yes (no caller needs it; NER reads entities off the triples) |
| `classify` | `POST /classify`, model `classify` | yes; the zero-shot answer is read defensively, an unreadable shape is an unavailable call |
| `health` | `GET /models` | yes; a 200 model list is liveness |
| `translate` | `POST /chat/completions`, model `translate_model` (default `fast`) | yes — see below |
| `summarize` | none | **no** — the call raises the unavailable error without a request; the callers' degrade path takes it |

The entity labels default to the entity-class taxonomy less its `entity` catch-all (override:
`entity_labels`). Whatever separator the gateway keys a relation answer by, the predicate comes back as
the configured label, because the fact write path and the entity-class cues key on that surface. A 429
honours `Retry-After` up to 10 s (longer fails the call at once), 429 and 5xx are tried three times, a
4xx other than 401/403 is an unavailable call naming the field, and request text never reaches a log
line or an error.

Fact quality on `/relations`. The first gateway facts landed at the 0.5 floor as junk
("Melania Trump | operates in | Oleshky", "truce | signed agreement with | Oleshky"), so three component
knobs on `nlp.innoai.gateway` shape what `extract` returns; each takes effect on a registry PUT and a
runtime restart, no deploy, and the legba-models provider refuses all three:

| Knob | Default | What it does |
|---|---|---|
| `relation_confidence` | `min` | The triple's `confidence` from the two endpoint scores the gateway returns: `min`, `mean`, or `none` (no `confidence`; the fact path's 0.5 floor, the earlier behaviour). `min` because a relation is only as sure as its weaker end: a confident head cannot vouch for a doubtful tail, and `mean` lets one strong end lift a junk partner over the floor. `subject_confidence` / `object_confidence` still ride on each triple. |
| `relation_threshold` | `0.5` | Sent to `/relations` as `threshold`, and applied again in the client to the same score (the min under `none`), so the line holds whatever the server does with the field. 0.5 is the fact path's own floor: a scored gateway triple below it would land less sure than an unscored one. A triple with an unscored endpoint is never dropped by it. If the route answers 400/422 naming `threshold`, the client retries once without it, stops sending it, and filters in the client only, so one unknown field never costs the whole call. `0` turns it off. |
| `relation_labels` | derived | The labels asked for. Absent = every graph relationship type (`RELATIONSHIP_TYPES`, the seed of the `relationship_type` vocabulary the reifier and structural balance sign) less the generic `CoOccursWith`, in the write path's row-predicate form: `hostile to`, `located in`, `allied with`, `party to`, `targets`, `operates in`, `member of`, `leader of`, `conducted via`, `supplies weapons to`, `part of`, `affiliated with`, `involved in`. It replaces the twenty generic GLiREL labels, fifteen of which named relations no graph type reads (`signed agreement with`, `headquarters in`, `controls`, ...). Set a list to override; the old set is kept as `LEGACY_GLIREL_RELATION_LABELS`. |

A cheap junk gate runs first: head and tail must be different strings (case, spacing and edge
punctuation ignored), each at least two characters and not a bare stopword (an all-caps acronym such
as `US` or `WHO` is not one). Junk and below-threshold drops are counted on one debug line,
`nlp_gateway.extract hits= kept= dropped_junk= dropped_below_threshold=`, never the text; the fact
extractor's own gates still run after it. The pure parts live in
`data/stack/nlp_service/gateway_relations.py`.

Translation runs on the gateway's chat model, alias `fast` (MiMo-V2.6-Flash, thinking off,
multilingual), named on the component as `translate_model` (absent: `fast`; an empty value turns
translation off, the call raises the unavailable error and both backfill sweeps report
`skipped_no_nlp`; the field is refused on the legba-models provider). It replaces NLLB-200 and its
non-commercial licence. Each call is one completion with a strict system prompt — "Translate the
following Russian text into English. Output ONLY the translation, no preamble, no notes, preserve
names, numbers, dates and paragraph breaks; if the text is already English return it unchanged." —
the text as the user turn, temperature 1.0 and no `max_tokens`. The answer comes back in the legacy
shape (`translated`, `source_lang` echoed as given or `und`, `target_lang`, `ms`), so the NER filter's
translate-then-extract route, the `title_en` / `text_en` stamps at ingest and the translation backfill
sweep run unchanged. Code fences, a wrapping quote pair and a `Translation:` label are stripped; an
empty answer, or one more than three times its input in UTF-8 bytes (bytes, so an honest CJK title is
not refused), is an unavailable call and nothing is stamped — the row stays a backfill candidate. A
source language equal to the target returns the text without a call. Text over 6,000 characters is
split at paragraph, then line, then sentence boundaries, translated chunk by chunk and re-joined with
the original separators; the callers truncate first (2,000 characters at ingest, 4,000 in the sweep),
so a live call is one completion. Retries, `Retry-After` and the auth error are the client's own;
request text is never logged, the debug line carries counts, token usage and the `x-served-model`.

The runtime binds one NLP component, named by `LEGBA_NLP_COMPONENT_ID` (unset:
`nlp.local.legba_models`); see [TUNABLES](TUNABLES.md).

## LLM providers

Three provider families share one handler base. Each descriptor names its model through a stack
reference, and the handler translates the common chat-completion shape to the provider's wire API.

| Family | Wire API | Auth | Notes |
|---|---|---|---|
| OpenAI-compatible (vLLM and hosted routers) | `POST /v1/chat/completions` | bearer or basic | the core plane and every hosted judge and grader route |
| Anthropic | `POST /v1/messages` | API key | system role hoisted; the provider requires an output ceiling, which this handler alone sends |
| OpenAI | `POST /v1/chat/completions` | bearer | reasoning models routed to their completion-token parameter |
| Decision models | `POST /v1/decide` | bearer | typed choice questions about a free-text state, answered with a probability per option; no chat surface; routed by a component id containing `.decide.` |

Shared behaviour: retry with backoff on rate limits and server errors, honouring the retry-after
header; a lean health check of endpoint reachability plus vault-key presence; usage accounting for
prompt, completion and reasoning tokens with the cost hooks the budget plane reads. The core plane is
called with temperature 1.0 and no output ceiling; per-descriptor token budgets are zero everywhere.

The components on the reference deployment:

| Component | Model | Role |
|---|---|---|
| `llm.primary.openai_compat` | a self-hosted 120B-class model on vLLM | the core plane: every scheduled analyst |
| `embed.primary.openai_compat` | a self-hosted embedding model | deduplication, correlation, retrieval corpora |
| `llm.judge.openrouter_nemotron120b.openai_compat` | Nemotron 3 Super 120B | the faithfulness judge, on a paid route |
| `llm.judge.openrouter_mistral_large.openai_compat` | Mistral Medium 3.1 | the external audit grader; a correctness-grader family; a judge fallback rung only when opted in |
| `llm.audit.openrouter_llama33_70b.openai_compat` | Llama 3.3 70B | the external audit's second rater; a correctness-grader family |
| `llm.verify.slm_8b` | Llama 3.1 8B, self-hosted | the claim-watch post-match checker; the LAST rung of the judge fallback chain (a weak judge — see below), never the configured judge |
| `llm.decide.innoai_clef_flash` | clef-flash, a decision model on the gateway | the bearing gate's SHADOW (below), when `bearing_shadow_ref` names it; decides nothing |
| `llm.anthropic.opus_4_7`, `llm.anthropic.fable_5_1` | Anthropic's Claude | consult and deep consult, only when a request explicitly picks one; the core plane is the default |

Component ids are stable names; the model behind one is a descriptor field, so a model change is a
registry update, not a code change. The generated [release state](RELEASE_STATE.md) reports the
effective routes for a deployment.

## The faithfulness judge

Every cited finding from a unit, and every composition and voice, goes through the verify pass: an
always-on deterministic citation floor, plus a judge model that reads each claim beside the evidence
it cites and returns a verdict per claim. The result is a faithfulness score in `[0, 1]`, persisted as
a critique row that stamps which model judged it and which pipeline revision, so populations graded
under different instruments never pool. `effective_confidence = min(confidence, faithfulness)`; the
composition read slice inner-joins the critique, so an unverified sub-claim never enters a
composition, and a low score demotes a claim to a visible tier rather than deleting it.

When the judge route is unavailable the critique is stamped as deterministic, published provisional
under a ceiling, never a fabricated pass. The judge route is repointable by one env line; pointing it
back at the core plane costs same-model grading.

**The judge fallback chain.** The resolution ladder picks ONE judge route; the failure chain
(`runtime/judge_fallback_chain.py`) says what happens when that route is GONE. A descriptor declares
`method.llm.judge_chain: [ref, …]`; rung 1 is always the ladder's resolved route, and with no key the
platform default follows it: the self-hosted 8B (`llm.verify.slm_8b`, $0) alone. **The default
fallback is the $0 in-house rung; a paid second rung is opt-in** — a descriptor lists it in
`method.llm.judge_chain` (e.g. `[llm.judge.openrouter_mistral_large.openai_compat,
llm.verify.slm_8b]`), or the deployment does in `LEGBA_JUDGE_CHAIN_DEFAULT`, so an outage of the
configured judge never silently moves its volume onto another paid model. A call whose failure is route-gone — 401 or 403 (key), 402 or an
insufficient-credits body (account empty), 404 (model retired) — moves to the next rung for the rest
of that verify pass; a 429, a 5xx or a timeout stays on its rung under the transport shim's
unchanged retry. A verdict a later rung wrote is stamped `judge_route = chain:<component_id>`,
`judge_llm_ref` and `judge_model` name that rung, and `verification.judge_chain` records what was
tried. When every rung failed the critique is the floor and carries `verification.unverified_reason`
(the last rung's class, status and time); readers render it as "unverified since T (judge
unavailable: 402)". When a paid second rung is opted in and bills the same router account as the
judge (as both hosted judges do on the reference deployment), an empty account takes out both and the
8B is what answers.

**The 8B rung is a WEAK judge.** It was tried as THE judge on 2026-07-01 and dropped as too weak —
harsh and mis-aimed. As a last rung it still beats the floor, which checks that citations exist and
not that they support the prose; but its verdicts carry `judge_chain.weak = true` and its own model
id, so an accuracy figure never pools them with hosted verdicts unannounced. Its server's context
window and output cap (2,048 tokens on the reference component) are smaller than a long judge
prompt; a call it cannot fit fails as an ordinary error and the critique stays an honest floor.

**What an outage leaves behind.** A floor critique written while every rung was down is NOT
re-judged when a route recovers — a recovered judge grades only what is written after. An admin
inventories the debt with `scripts/rejudge_floor_critiques.py` (a read-only dry run, counts by UTC
day) and repays it with `--apply --limit N`. That re-runs the critic's verify pass over each finding's
stored citations against the current judge chain. It writes an answered verdict as a new critique
that closes the floor (`superseded_by`), and it never writes a second floor (SEAMS #69, resolved). The watchdog pages a hosted
route's 401/402/403/404 at once (`llm_route_auth_failed`), and
`GET /v3/system/judge-stats/health` reports per judge component the newest success and failure, the
failure class, the current run of failures, and `unverified_since` — the earliest critique of the
current floor-only streak, NULL when the judge is answering. Core-plane models sometimes emit full-width citation
brackets; the parser normalises them before matching, reads a bracketed list (`[11, 47]`) as one
marker per member, and moves a marker written after a sentence's full stop or closing quote back
onto that sentence, so a marker never cites the sentence after it. Every failed span, judge or floor,
carries the markers its sentence carried. The bounded units' prompts carry one shared rule: every
sentence that states a fact ends with its marker, the BLUF included
(`_tradecraft.UNIT_CITED_SENTENCE_RULE`); the critique stamps each unit read with how many of its fact
sentences carry none, and `/system/judge-stats` reports that share per unit per day as `uncited`. The
same yardstick scores the prompt self-optimiser's experiments.

## The decision shadow

A decision model is measured before it may decide anything. With `bearing_shadow_ref` set on the
`claim_watch` descriptor (unset by default), every pair the bearing gate asked its chat model about
(does this news item bear on that open question?) is asked again of a decision model, clef-flash on the
gateway (`POST /decide`, one `choice` question `bears`, criteria yes/no). The state it gets is the
exact row the gate judged: the desk line, the thesis and the item. It runs after the gate, the confirm
leg and the edge writes are done, and it moves NO decision: the gate's verdicts and the edges written
are identical with the shadow on or off. Each pair lands in `decision_shadow_ledger` (migration 0238)
with the gate's verdict, the shadow's choice, `p_yes`, confidence and latency, or the shadow's error
class; a failure is counted on the run receipt (`bearing_shadow_*`), never raised.
`scripts/decision_shadow_report.py` reads the agreement, the 2x2 confusion, the calibration by `p_yes`
decile and the mean latency over a window, per analyst and overall. The calls carry
`leg = bearing_shadow` on `analyst_traces.llm_calls`; with the gate at its 200-call cap the run's
200-entry receipt is already full, so the ledger, not the receipt, is the shadow's record.

## Embeddings

The embedding model backs three things: the semantic tier of the four-tier deduplication filter,
the consult engine's vector search tool, and the retrieval corpora that ground the units and the
consult path. Vectors live in Qdrant. The embedder is reached through the same stack-reference
mechanism as the LLMs, so swapping it is a registry update followed by a re-embed of the corpus.

## Consult

Consult and deep consult are the one path that can bill per call. Each request chooses its plane
through a model picker against a server-side allowlist (`core`, `opus`, `fable`): the free core plane
by default, a hosted Anthropic route only when the request names it.
A chosen plane that cannot be honoured raises rather than silently downgrading. The engine runs a
tool loop over governed read tools with a visible cost, a wall-clock deadline, a stop that persists
the partial turn, and a synthesis step that is never discarded. On the core plane a consult turn
thinks (next section).

## Thinking on the core plane

The gateway serves the core model under two aliases: `fast` (thinking off) and `deep` (the same
model with thinking on). The gateway chooses thinking by alias only, so the switch Legba controls is
which alias a call names, and it is decided in one place, `src/legba/data/stack/llm/thinking.py`:

- **A call** passes `thinking=True` to `chat_complete`. The handler sends the component's
  `thinking_model_name` (on `llm.primary.openai_compat`: `deep`) in place of `model_name`. The alias
  is registry data; no call site names one, and `tests/test_gateway_alias_literal_guard.py` fails on
  a literal alias at a call site. A component without `thinking_model_name` refuses a thinking call
  loudly.
- **A descriptor** declares `method.llm.thinking: "on"` (quote it: an unquoted YAML `on` is a
  boolean). For every kind except consult, the analyst's primary handler then thinks on every call
  unless a call says otherwise.
- **Per call role.** Where a descriptor already separates its calls, each block can choose for its
  own calls, and a block that says nothing inherits `method.llm.thinking`:
  `method.gather.thinking` decides the GATHER tool rounds (`inline_target`, `journal_assessor`,
  `inquiry`), and `method.llm.narrate.thinking` decides the VOICE (the field notes, the narrate
  and its retry, the inquiry coda; `journal_assessor`, `inquiry`). Everything else on the primary
  handler (synthesis, the journal's propose round) follows `method.llm.thinking`. The decision is
  `ThinkingPlan.for_role` (`src/legba/shared/thinking_choice.py`); the deps builder binds each
  phase's handler with it. Registration refuses a per-block choice on a kind that has no such
  calls, and any `thinking` inside `verify` or `judge`: the judge route can be repointed
  (`LEGBA_JUDGE_STACK_REF`, the judge chain) onto a plane with no thinking alias.
- **Consult** (`consult_default`, `deep_consult` declare it) applies it per call role
  (`src/legba/data/analysts/consult_thinking.py`). Tool rounds, the forced final, a recovery
  synthesis and deep consult's plan think. The contract's repair call and deep consult's candidate
  extraction do not. The settle-point judge runs on a different family and is unchanged. A turn on a
  plane with no thinking alias (the paid pick) runs as before, and its trace records a `thinking`
  step saying why. A bake-off arm never thinks, and the bake-off arm descriptors declare nothing.
  A request row's `thinking: "off"` turns one turn off; the front door has no control for it.

Thinking never changes temperature (1.0 is sent as before; the vendor forces it in thinking mode) and
never adds a `max_tokens`. Reasoning streams as `reasoning_content` deltas. Those deltas are kept off
the answer text, the live answer frames and the answer length. They are counted only as
`reasoning_tokens`, from the usage block's `completion_tokens_details`. Every call that thinks
carries `"thinking": true` on its `analyst_traces.llm_calls` receipt, beside the model it named.
Reasoning tokens are part of `completion_tokens` on this wire, so the per-call budget report and the
`budget_ledger` count them once; only the Anthropic handler, which reports thinking separately, adds
them. The gateway's reasoning-budget parameter is not known yet. A component can declare it
(`reasoning_budget_param` + `reasoning_budget_tokens`), and nothing is sent while it is unset
([SEAMS](SEAMS.md) #88).

**Which seats think** (switched on by descriptor; all on the core component):

| Seat | Calls a day | What thinks | What does not |
|---|---|---|---|
| `world_assessment`, `country_assessment` | ≈2, ≈64 | the one synthesis call per run | the verify judge (another component) |
| `competing_hypotheses` | ≈48 | the hypothesis generation and the consistency matrix | — |
| `fact_contention_arbiter` | ≈1 | the near-tie tie-break | — |
| `journal_assessor`, `chronicle_assessor`, the active lenses (`lens_baserate`, `lens_capability`, `lens_diff`, `lens_intent`, `lens_trend`) | 1–18 | the voice: field notes, narrate, its retry | GATHER rounds, the propose round |
| `inquiry_pilot`, `crossroads` | ≈10 each | the coda (the voice) | GATHER rounds |
| the ten desks (`narrative_coordination`, `internal_stability`, `leadership_transition`, `economic_coercion`, `energy_security`, `escalation`, `military_posture`, `proliferation_watch`, `disruption_status`, `cyber_watch`) | 0.3–66 | the synthesis call | GATHER rounds, if a target ever binds a read pack |

The paused voice lenses (`lens_centre`, `lens_isolationist`, `lens_left`, `lens_militarist`,
`lens_pragmatist`, `lens_right`) are unchanged. The high-volume typing and scoring seats
(`relationship_reifier`, `correctness_grader`, `signal_salience`, `signal_summarizer`,
`reference_builder`, `situation_tracker`, `claim_watch`, `entity_researcher`,
`contrary_evidence_pass`) do not think.

**The safety net.** A thinking call that fails for a reason the thinking alias causes is retried
once on the plain alias (`call_with_thinking_fallback` in `thinking.py`, used by every
`chat_complete`). The reasons are a closed list: `tools_refused` (a 400/422 naming tools on a call
that offered them), `unsupported_parameter`, `alias_unknown` (a 404, or a 400 naming the model),
`reasoning_parse` (a body or reasoning stream that does not parse) and `request_rejected` (any other
400/422). A transient failure (429, 5xx, a dropped stream, a timeout), an auth refusal, a prompt
over the context length and a component with no alias are not retried. The failed call's receipt
keeps `"thinking": true`; the retry's receipt carries `thinking_fallback: <reason>`; the run's steps
get a `thinking_fallback` step when the kind bound them; `llm.thinking_fallback` is logged at
WARNING; and the runtime raises the `thinking_fallback` alert (logged only, see
[ALERTING](ALERTING.md)). A retry never retries again.

**One concurrency bound per process** (`src/legba/data/stack/llm/call_gate.py`). Every call to the
core component, from every handler instance and every event loop in a container, holds one slot of a
process-wide gate around the network hop. The defaults are `LEGBA_CORE_LLM_MAX_CONCURRENT` = 12 and
`LEGBA_CORE_LLM_INTERACTIVE_RESERVED` = 2. Consult and deep consult calls are *interactive*; all
others are *scheduled*. Scheduled calls never hold the reserved slots, a freed slot goes to a waiting
interactive call first, and each class is first in, first out. The gate never drops a call and never
caps a token. A call that waited carries `gate_wait_ms` on its receipt, a wait of a second or more
logs `llm.gate.waited`, and the runtime's `/healthz` reports `llm_gates` (in flight, queued, peaks,
wait totals). On the core component this replaces the row's per-instance `max_concurrent`. Other
components keep their own per-instance `max_concurrent`.

## Knowledge grounding

Whatever model a unit is bound to carries a training cutoff. Structured grounding is live and opted in
on every bounded unit: before a run, the unit reads the temporal substrate for the entities and
situations in its slice and receives current facts as a non-citable preamble, so stale priors are
overridden by what the platform has actually observed. It corrects whatever cutoff the bound model
has, it stays auditable because the ground truth is substrate rows rather than the model's memory,
and it degrades rather than gates: a substrate read failure runs the unit ungrounded. Vector retrieval
grounding is a guarded pilot on a few units with a per-run rollback guard, because firing it has
thickened the low-faithfulness tail before.

## Media extraction

Signals with non-text modality are dispatched to a media extractor by modality. The job plane, the
envelope and the derived-signal loop are built; the extraction service ships refusing rather than
fabricating until real model backends sit behind it. See [SEAMS.md](SEAMS.md).

## Configuration

Every model route is a stack component descriptor under `descriptors/stack_component_*.yaml`,
registered through the registry API, with credentials in the vault. An analyst binds to one through
`method.llm.primary`, `method.llm.verify` or `method.llm.narrate`, each a stack reference. The judge
route, the grader ceiling and every cap are in [TUNABLES.md](TUNABLES.md); the operating practice for
changing a model, measuring before and after, and rolling back is in
[OPERATING_YOUR_INSTANCE.md](OPERATING_YOUR_INSTANCE.md).

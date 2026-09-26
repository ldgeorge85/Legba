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
   runs the nine bounded units, the compositions, the voices, the lenses, the journal, the reference
   builder and every other scheduled analyst, at no per-run cost. An embedding model on the same host
   backs deduplication, semantic correlation and the retrieval corpora. A hosted Anthropic model serves
   consult and deep consult only, and only when the operator presses.
3. **The graders.** The faithfulness judge scores every cited finding for whether each claim follows
   from its evidence. The external audit grader and its second rater check top-layer claims against
   live web search. The correctness grader's paid families score the desks against references. All run
   on hosted cross-family routes, so the platform never grades itself with the model that wrote the
   claim.

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
distinct error because it needs an operator to rotate the credential.

The extraction endpoint is where entities and relations come from: the multilingual NER filter posts
the signal text, walks the returned triples, maps each entity to the closed entity-class taxonomy, and
carries the extractor's confidence onto the fact. Non-Latin bodies go through translation first.

## LLM providers

Three provider families share one handler base. Each descriptor names its model through a stack
reference, and the handler translates the common chat-completion shape to the provider's wire API.

| Family | Wire API | Auth | Notes |
|---|---|---|---|
| OpenAI-compatible (vLLM and hosted routers) | `POST /v1/chat/completions` | bearer or basic | the core plane and every hosted judge and grader route |
| Anthropic | `POST /v1/messages` | API key | system role hoisted; the provider requires an output ceiling, which this handler alone sends |
| OpenAI | `POST /v1/chat/completions` | bearer | reasoning models routed to their completion-token parameter |

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
| `llm.judge.openrouter_mistral_large.openai_compat` | Mistral Medium 3.1 | the external audit grader; a correctness-grader family |
| `llm.audit.openrouter_llama33_70b.openai_compat` | Llama 3.3 70B | the external audit's second rater; a correctness-grader family |
| `llm.verify.slm_8b` | Llama 3.1 8B, self-hosted | the claim-watch post-match checker; not the verify judge |
| `llm.anthropic.opus_4_7`, `llm.anthropic.fable_5_1` | Anthropic's Claude | consult and deep consult, on the operator's press; each request may pick the free core plane instead |

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
back at the core plane costs same-model grading. Core-plane models sometimes emit full-width citation
brackets; the parser normalises them before matching. The same yardstick scores the prompt
self-optimiser's experiments.

## Embeddings

The embedding model backs three things: the semantic tier of the four-tier deduplication filter,
the consult engine's vector search tool, and the retrieval corpora that ground the units and the
consult path. Vectors live in Qdrant. The embedder is reached through the same stack-reference
mechanism as the LLMs, so swapping it is a registry update followed by a re-embed of the corpus.

## Consult

Consult and deep consult are the one billed path. Each request chooses its plane through a model
picker against a server-side allowlist: the hosted Anthropic route by default, or the free core plane.
A chosen plane that cannot be honoured raises rather than silently downgrading. The engine runs a
tool loop over governed read tools with a visible cost, a wall-clock deadline, a stop that persists
the partial turn, and a synthesis step that is never discarded.

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

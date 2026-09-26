<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Tunables — every budget, cap, governor and threshold, in one table

**Plane** says who pays when a knob lets more work through: `ai1` is the self-hosted core plane (the
120B-class model, embeddings, the NLP models, the 8B checker; no per-run cost), `local` is a container
on this host (SearXNG, Postgres, NATS, Redis, OpenSearch; no cost), `OpenRouter` and `Anthropic` are
paid per token, `none` means no external call. **Change** says how: `env` (edit `.env`, recreate the
container), `descriptor` (registry PUT of the analyst or source descriptor, live, no roll), `pack`
(registry PUT of the action-pack descriptor, live), `stack` (registry PUT of the stack component,
live), `code` (edit and roll). Values are the reference deployment's live values; the code default is in
parentheses where it differs. The record of when a value moved is the [changelog](../CHANGELOG.md).

## 0. The money map — every path that can spend

| Path | Component | Plane | Who calls it | Spend |
|---|---|---|---|---|
| desks, compositions, voices, lenses, journal, builder, grader family F0, salience, researcher, every scheduled unit | `llm.primary.openai_compat` | ai1 | every scheduled analyst | $0 |
| embeddings, NER, geocode, translation, summaries | `embed.primary.openai_compat`, `nlp.local.legba_models` | ai1 | enrichment | $0 |
| the claim-watch post-match checker | `llm.verify.slm_8b` | ai1 | claim_watch | $0 |
| web search, first rung | `search.searxng.local` | local | auditor, researcher, builder, consult | $0 |
| web search, paid rung (the audit's second search when a reformulation is offered) | `search.serper.paid` | serper | standing_auditor | ≈ $0.35/day under a $1/day brake |
| the faithfulness judge, every read | `llm.judge.openrouter_nemotron120b.openai_compat` | OpenRouter | the judge plane, ~600 rows/day | ≈ $0.45/day |
| the external audit grader | `llm.judge.openrouter_mistral_large.openai_compat` | OpenRouter | standing_auditor, desk_reference | ≈ $1/day at width |
| the external audit second rater | `llm.audit.openrouter_llama33_70b.openai_compat` | OpenRouter | standing_auditor, 10% double-grade plus contradictions | cents/day |
| the correctness grader's paid families F2 and F3 | the two components above | OpenRouter | correctness_grader, hourly ticks from 01:20Z | ≈ 45¢ per roster under a $2.50/day ceiling |
| consult and deep consult | `llm.anthropic.opus_4_7`, `llm.anthropic.fable_5_1` | Anthropic | the operator's press only | $0 unless pressed |

## 1. Search and web egress

| Knob | Where / change | Live (default) | Limits | On hit | Plane |
|---|---|---|---|---|---|
| web_access `max_invocations_per_hour` / `api_rate_per_minute` | `descriptors/action_pack_web_access.yaml` · pack | 1,000,000 / 1,000,000 | all web search and fetch calls, shared | BLOCK | local |
| web_access `max_cost_usd_per_day` / `fallback_providers` | same · pack | $1.00 / `[search.serper.paid]` | the paid rung's daily brake | paid rung refused, free rung answers | serper |
| reference_builder `governor_override.max_invocations_per_hour` | `analyst_reference_builder.yaml` · descriptor | 1,000,000 | the builder's own web account (tighten-only merge) | BLOCK | local |
| research pack hour / minute / `max_cost_usd_per_day` | `action_pack_research.yaml` · pack | 1,000 / 100 / $2.00 | the gap-filling researcher's egress | BLOCK | local |
| discovery pack hour / minute / sources / crawl | `action_pack_discovery.yaml` (not registered) | 20 / 5 / 50 / depth 3 / 500 pages | source discovery | BLOCK | local |
| escalate 30/5 · incident_response 60/10 · journal_propose 60/30 · journal_read 600/60 · media_processing 200/30 $10 · propose_facts 60/10 · substrate_read 600/60 | pack yamls · pack | as listed | internal packs | BLOCK | none |
| `MAX_RESULTS_CAP` | `data/stack/search/base.py` · code | 30 | results any search returns | clamp | local |
| `SearxngSearchHandler.max_extra_pages` | `data/stack/search/searxng.py` · code | 1 | extra pages, only when page one came back short | stop at page one | local |
| SearchProviderConfig `max_results` / `timeout_seconds` | stack | 30 (1–50) / 15 s | per provider | clamp | local |
| web_fetch `_MAX_FETCH_BYTES` / research `_MAX_TEXT_CHARS` / timeouts | `agency/web_tools.py`, `research_tools.py` · code | 200,000 B / 200,000 chars / 15–20 s | page size handed to a model | truncate | ai1 |
| SearXNG `request_timeout` / `max_request_timeout` / `retries` / `max_redirects` / `pool_connections` | `docker/searxng/settings.yml` · local edit and restart | 10 s / 15 s / 1 / 5 / 10 | upstream engine calls | engine skipped | local |
| SearXNG `suspended_times` denied / too-many / captcha | same | 900 s / 900 s / 21,600 s | engine cool-down after a block | engine skipped | local |
| SearXNG engines | same | four answer; bing news, google news and wikidata disabled | which engines answer | — | local |
| `LEGBA_PACK_TOOL_TIMEOUT_SECONDS` / `_STALE_RECONCILE_SECONDS` | env | 60 s / 300 s | one pack-tool dispatch | fail / sweep | none |
| `LEGBA_FETCH_IMPERSONATE` | env | off | curl_cffi impersonation | — | local |
| `LEGBA_FETCH_TLS_CIPHERS` / `LEGBA_FETCH_HTTP2` | env · `data/sources/_egress.py` | `DEFAULT:@SECLEVEL=2` / on | the cipher list every guarded fetch OFFERS, and whether it offers h2 in ALPN. httpx configures both on the TRANSPORT, so before wave O the guarded transport — built with no arguments — inherited whichever handshake the base image's OpenSSL was compiled with: 60 suites on Debian 12 / OpenSSL 3.0.18 (the host), 17 on Debian 13 / OpenSSL 3.5.7 (the images). Akamai-fronted syndication (usnews.com, carrying the Reuters wire) serves the first in under a second and, for the second, resets the h2 stream or — over HTTP/1.1 — accepts the request and answers nothing, which is the empty-message `ReadTimeout` that burned the full timeout twice per URL in the 2026-09-25 reference top-ups. The pin is host-agnostic: no per-site branch exists. `@SECLEVEL=2` keeps Debian's security level while widening the offer. | empty cipher string ⇒ inherit the image's build default; `LEGBA_FETCH_HTTP2=off` ⇒ HTTP/1.1 only (also the loud fallback if `h2` is ever missing) | local |

## 2. Measurement — judge, external audit, correctness grader, reference builder

| Knob | Where / change | Live (default) | Limits | On hit | Plane |
|---|---|---|---|---|---|
| `LEGBA_JUDGE_STACK_REF` | env | the Nemotron paid route | which model judges | — | OpenRouter |
| `judge_sample_rate` / `judge_sample_always` | descriptor | 1.0 everywhere | share of findings LLM-judged | unsampled ⇒ ceiling 0.85 | OpenRouter |
| `PROVISIONAL_SCORE_CEILING` / `UNASSESSABLE_GATE_SCORE` | `provenance/judge_assessability.py` · code | 0.85 / 0.5 | score of unjudged or unassessable rows | capped | none |
| `LEGBA_JUDGE_FLOOR_ESCALATION` | env | on | re-judge an unsampled row the floor would exclude | — | OpenRouter |
| judge retries / backoff / budget / retry-after cap / max_tokens | `provenance/judge_transport.py` · code | 3 / 2→8 s / 45 s / 30 s / 16,384 | one judge call | fallback to the floor | OpenRouter |
| judge evidence envelope total / per source / grounding | `provenance/verify.py` · code | 8,000 / 6,000 / 4,800 chars | evidence the judge sees per claim | truncate | OpenRouter |
| `_CLAIM_VERDICTS_CAP` / verdict text | code | 120 / 1,200 chars | verdicts per report; partial verdicts align by `claim_index`, positional as fallback | truncate | none |
| `LEGBA_COMPOSITION_VERIFY_FLOOR` | env | 0.50 | sub-claim admission to a composition | excluded | none |
| `LEGBA_EXTERNAL_GRADING_WIDTH` | env | 1 | audit at width, not the six-claim daily sweep | — | OpenRouter |
| standing_auditor `max_claims_per_tick` / `max_claims_per_day` / `max_serp_per_day` / `max_queue_depth` | descriptor; the tick clamp reads the live pack governor | 200 / 5,000 / 10,000 / 20,000 | audit volume | queue waits | OpenRouter + serper |
| standing_auditor `serp_provider_order` | descriptor | `[searxng, serper]` | the search ladder; the reformulated query runs on the paid rung when one is declared | — | serper |
| `EGRESS_CALLS_PER_CLAIM` | code | 3 | primary search + one second search + one span fetch | — | local |
| daily sweep `MAX_CLAIMS_TOTAL` / `MAX_DESKS` / `SEARCH_LIMIT` | `standing_auditor.py` · descriptor | 6 / 3 / 5 | the non-width sweep | — | OpenRouter |
| audit `DOUBLE_GRADE_FRACTION` / overlap bands | code | 0.10 / 0.80–0.75 | second-rater sample; instrument validity | `instrument_limited` | OpenRouter |
| `LEGBA_EXTERNAL_AUDIT_WINDOW_BASIS` / `_GRACE_HOURS` | env (an instrument change) | evidence / 96 | window admissibility of audited spans | out_of_window | none |
| audit fetch timeout / page chars / SERP timeout | code | 25 s / 200,000 / 45 s | span checks | UNCHECKED | local |
| `LEGBA_LLM_ROUTE_MIN_CALLS` / `LEGBA_LLM_ROUTE_SUCCESS_FLOOR` | env | 20 calls / 5% over 24 h | a dead model route pages | severity-high alert, budget-exempt | none |
| `LEGBA_GRADER_DAILY_CEILING_USD` | env | $2.50 | the paid grader families | families skipped past the ceiling | OpenRouter |
| `LEGBA_GRADER_PASS_BUDGET_SECONDS` | env | 600 | seconds one grader tick may hold its actor turn; checked between targets | the tick ends with a receipt naming its deferred tail; `<= 0` unbounded | none |
| grader cadence / cooldown | descriptor | nine hourly ticks from 01:20Z / 3,600 s | the roster completes once a day in short turns | — | none |
| `LEGBA_GRADER_REFERENCE_GRACE_DAYS` | env | 7 | reference currency past window end | `reference_stale` | none |
| grader `max_targets_per_run` / `max_claims_per_unit` / `max_claims_per_run` / `head_window_days` | descriptor | 32 / 40 / 2,400 / 14 (5 / 40 / 400 / 14) | grading volume; the roster is least-recently-graded first | the remainder rotates in | ai1 (F0) |
| agreement bars pooled / pairwise · calibration draw | code | 0.75 / 0.70 · 30 | the instrument gate | the grader refuses without a passing row | OpenRouter |
| `LEGBA_COMPOSITION_CORRECTNESS_GATE` · `MIN_CORRECTNESS` · `MIN_COVERAGE` · `ALLOW_SINGLE_FAMILY` | env | off · 0.8 · 0.2 · off | compose vs quote per unit | demoted to quoted | none |
| `LEGBA_REFERENCE_BUILDER_ENABLED` | env | 1 | the builder | off ⇒ no references | ai1 |
| `build_max_seconds` / `build_max_tokens` (descriptor; the env mirrors `LEGBA_REFERENCE_BUILD_MAX_SECONDS` / `_MAX_TOKENS` do not override a set descriptor value) | descriptor | 900 s / 1.2 M | one build | `no_commit` | ai1 |
| builder `tool_call_cap` / `max_targets_per_run` / `window_days` / `cadence_days` / `thin_below` | descriptor | 200 / 1 / 14 / 7 / 2 | tool calls per build; targets per tick; the reference window | build ends; roster rotates | ai1 |
| builder `page_chars_to_model` / `min_developments` | descriptor | 16,000 / 5 | page text shown; commit push-back | truncate; pushes back | ai1 |
| `MANIFEST_EXCERPT_CHARS` / `MANIFEST_TOTAL_CHARS` / notes carry chars / age | `_reference_manifest.py`, `_reference_notes.py` · code | 1,400 / 26,000 / 20,000 / 24 h | what the forced commit can see | truncate | none |
| `SEARCH_RUN_LIMIT` / ratio guard after / factor | code | 2 / 12 / 3.0 | searches before a fetch is required | loop pushed | local |
| `LEGBA_REFERENCE_RETRY_BACKOFF_HOURS` / unbuildable after | env / code | 24 / 3 failures | failed-target retry | skipped | none |
| desk_reference `max_pairs_per_run` / items / search limit / window / census | descriptor | 40 / 5 / 8 / 24 h / 7 d | daily desk references, two runs a day | the remainder next day | OpenRouter + local search |

## 3. Reads — desks, compositions, voices, consult

| Knob | Where / change | Live (default) | Limits | On hit | Plane |
|---|---|---|---|---|---|
| `LEGBA_LLM_INPUT_TOKEN_BUDGET` | env | 65,536 (32,000) | evidence packed into one desk or composition prompt | the oldest evidence is not packed | ai1 |
| `method.gather.max_rounds` | descriptor (1–6) | desks unset ⇒ 1; journal and lenses 6; corpus 4 | agentic gather rounds | reflect on what it has | ai1 |
| `LEGBA_SLICE_ROW_CAP` / `LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP` / graph structure cap | env | 120 / 15 / 8 | rows a desk is shown | truncate | none |
| substrate_read `_MAX_ROW_LIMIT` and per-tool caps | code | 200; 12/20, 50, 200, 3 hops, 25/50 | one tool call | clamp | none |
| composition `BLOCK_CAP` / `CO_LEAD_MAX` / `PERIPHERY_CAP` / evidence text / admissibility | code / descriptor `time_window` | 8 / 4 / 8 / 4,000 chars / 336 h | the record's shape | truncate | none |
| voice `TENSION_RENDER_CAP` and chars · `APERTURE_NAME_CAP` and chars · spine window | code | 2 / 2,400 · 5 / 1,400 · 24 h | what the voice is handed | truncate | ai1 |
| `method.llm.max_tokens` | descriptor | 256 (arbiter) to 32,768 (consult); the core plane never sends it | output length on paid planes only | truncate | Anthropic / OpenRouter |
| `method.budget_tokens_per_day` | descriptor | 0 = unlimited on every descriptor | per-analyst daily tokens | pause until the next window | — |
| `global_budget_envelope.tokens_cap` | DB row / `/api/v1/budget/envelope` | 200,000,000/day | fleet-wide daily tokens | demote all | — |
| `cadence.cooldown_seconds` | descriptor | 0 to 21 d, modally just under the cron interval, anchored on run start | re-fire spacing | tick no-ops | none |
| consult rounds default / chat / ceiling · tools per batch · `MAX_TOOL_ROUNDS` | code | 6 / 10 / 30 · 5 · 6 | tool-loop rounds | forced final | Anthropic |
| `LEGBA_CONSULT_BUDGET_SECONDS` / `_ROUND_DEADLINE` / `_FINAL_FLOOR` / `_NATIVE_BATCH_CAP` / `_WALL_BUDGET` | env | 900 / 480 / 240 / 5 / 210 (480 / 150 / 240 / 4 / 210) | consult wall clocks | forced final, partial persisted | Anthropic |
| `LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN` / `_MAX_COST_USD_PER_RUN` / `_KEEP_FULL_ROUNDS` / `_ROUND_RESULT_BYTES` | env | 1e8 / $1e6 / 1000 / 40,000 (150k / $3 / 1 / 16,000) | one consult run; the operator's values | — | Anthropic |
| `LEGBA_RATE_LIMIT_CONSULT` / `_DEEP_CONSULT` | env | 10/min, one bucket | requests | 429 | none |
| `LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS` / sync wait / retention / remembered runs / steps | env / code | 900 s / 20 s / 900 s / 256 / 400 | run bookkeeping | — | none |
| `LEGBA_SITUATION_TRACKER_MAX_SITUATIONS` | env | 24 (12; ceiling 500) | situations per tick | the remainder next tick | ai1 |
| `grounding.max_facts` / open questions / `MAX_OPEN_QUESTIONS` | descriptor / code | 30 / 8 / 5 | the grounding preamble | truncate | none |

## 4. Flow — sources, enrichment, deduplication, events

| Knob | Where / change | Live (default) | Limits | On hit | Plane |
|---|---|---|---|---|---|
| `_POLL_BUDGET_S` / `_MAX_ENTRIES_PER_POLL` (ceilings 240 s / 1,000) | `runtime/source_actor.py` · descriptor `poll_budget_seconds`, `max_entries_per_poll` | 30 s / 100 | one pull | truncated, cursor advances | local |
| `_ENRICH_TIMEOUT_S` | code | 12 s | per-entry enrichment | entry skipped, re-pulled | ai1 |
| telegram `per_channel_timeout_seconds` / `cycle_timeout_seconds` / `poll_budget_seconds` / `max_entries_per_poll` / message limit / catch-up pages / flood abort / startup delay | descriptor | 45 s / 360 s / 240 s / 1,000 / 200 / 10 / 30 s / 60 s | the Telegram poll | channel abandoned for the poll | local |
| RSS timeout / retries / 304 refetch / skew · geojson features · json_api items · GDELT files × rows / Goldstein | descriptor / code | 30 s / 1 / 12 / 26 h · 5,000 · 100 · 2 × 75,000 / ≤ −2 | per-format pulls | truncate | local |
| `LEGBA_INTRASOURCE_DEDUP` / window | env | on / 168 h | exact-hash deduplication | duplicate skipped | none |
| salience rows per tick · batch · window | descriptor | 300 · 12 · 96 h | scoring throughput | next tick | ai1 |
| summariser 40/tick · embedder 200 · NER re-enrich 500 · translation 300 · entity resolution 500 (8/signal) · corpus index 1,000 · dedup 500 groups · coalesce 2,000 | code | as listed | enrichment throughput | next tick | ai1 |
| arbiter `MAX_LLM_TIEBREAKS` / scan facts · competing_hypotheses topics / evidence · reifier `max_candidates` · claim_watch caps | code / descriptor | 10 / 200,000 · 12 / 24 · 600 per run · 500/500/200 | per-run work | deterministic fallback | ai1 |
| `LEGBA_CONTENTION_PASS_BUDGET_SECONDS` / `LEGBA_CONTENTION_REFRESH_HOURS` | env | 120 s / 24 h | one arbiter pass; how often an unchanged dispute is re-decided (input fingerprint) | pass ends, remainder next tick | ai1 |
| `LEGBA_REIFIER_PASS_BUDGET_SECONDS` | env | 120 s | one relationship_reifier tick's actor turn; checked before each candidate, admission estimated from the run's own measured per-candidate seconds (10 s prior) | the tick ends inside its turn with a partial receipt naming `stopped_after` + a `cursor` (receipt-only, not persisted); `<= 0` unbounded | ai1 |
| coverage-floor bars (window 14 d, 20 signals, 10 days, magnitude 0.50, 15 high, share 0.10, 5 clusters, 6 h) | env `LEGBA_COVERAGE_FLOOR_*` / descriptor | as listed | when a blind spot is flagged | flag | none |
| `LEGBA_CRITIC_FANOUT_MAX` · `_FANOUT_CHUNK` | env · code | 12/tick · 5 concurrent | critic drain; worker concurrency | queued | ai1 |
| `LEGBA_EVENTS` | env | 0 | the v3 event write path; `_insert_event` refuses while off | no event rows written | none |
| event clustering slice / event scan / match / embedding / tower / lifecycle caps | descriptor `analyst_event_clustering.method.options` | 24 h · 300 / 2,000 · 0.50 / 0.97 · 30 d @ 0.50 / 50 · 5,000 | one P1b tick's candidate and maintenance work (caps lowered for the first ticks after the ~60 min held turn; widen by PUT) | next bounded tick | none |
| `LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS` | env · descriptor `pass_budget_seconds` | 120 s | one event-clustering tick's actor turn; checked between phases and between candidates | the tick ends inside its turn with a partial receipt + resume cursor on `alert_trigger_watermarks`; `<= 0` unbounded | none |
| event reconciliation event cap / evolves_from actors / gap / pass budget | descriptor `analyst_event_reconciler.method.options` | 5,000 · 2 actors · 168 h · 120 s | one deterministic edge sweep | next bounded tick | none |
| `LEGBA_EVENT_RECONCILER_PASS_BUDGET_SECONDS` | env · descriptor `pass_budget_seconds` | 120 s | one reconciler sweep's actor turn; checked between phases and between edge writes | the sweep ends inside its turn with a partial receipt; `<= 0` unbounded | none |
| `LEGBA_EDGE_TRANSITION_LEDGER` | env | 0 | the v3 entity-edge transition ledger (0206); `write_entity_edge_for_nexus` appends `entity_edge_events` rows in the edge write's transaction | no ledger rows written | none |
| `LEGBA_EVENT_CITATIONS` | env | 0 | the v3 `event:<uuid>` ref kind (P2); the citation builders expand the token through `signal_event_links` into per-signal entries at build time | the token is ordinary prose | none |
| `LEGBA_EVENT_EXPANSION_MAX_SIGNALS` | env | 4 | per-event expansion cap (`expand_event_citation`); more members stamp `event_expansion_truncated` on every emitted entry | ranked links beyond the cap are dropped, flag recorded | none |
| `LEGBA_GRAPH_PROJECTION` | env | 0 | the P4b `graph_arcs` build + read plane; the projector refuses while off and every reader answers `projection_disabled` | no builds, no arc reads | none |
| `LEGBA_GRAPH_PROJECTION_MAX_AGE_SECONDS` | env | 7,200 s | how stale `graph_arcs_meta.projected_at` may be before readers refuse | readers answer `projection_stale` | none |
| graph projector `dry_run` | descriptor `analyst_graph_projector.method.options` | false | first-activation probe: build + count `graph_arcs_new` without the swap or the meta write | nothing installed | none |

## 5. Alerts and watchdogs

| Knob | Where / change | Live (default) | Limits | On hit | Plane |
|---|---|---|---|---|---|
| `LEGBA_ALERT_DAILY_PAGE_BUDGET` / `LEGBA_ALERT_BUDGET_PER_KIND_CAP` / per-desk cap | env or descriptor `daily_page_budget`, `budget_per_kind_cap` / code | 50 / 20 / 3 (5 / 3) | pages per day, per kind, per desk; the dead-route page is exempt | deferred, recorded | local |
| `LEGBA_ALERT_NTFY_MIN_SEVERITY` / sink cooldown / steady-state cooldown | env / code | medium / 15 s / 24 h | ntfy delivery | suppressed, recorded | local |
| liveness watchdog stall / re-alert / cadence factor / empty streak / honest quiet | env | 15 min / 30 min / 2× (floor 90 min) / 5 / 36 | in-runtime alarms | alert only | none |
| host stall watchdog `MAX_AGE_SECS` / `COOLDOWN` / `GRACE` / `VERIFY_WAIT` | script env | 1,800 / 2,700 / 900 / 720 s | the restart recipe | restart sidecar and runtime, escalate on repeat | none |
| LLM heartbeat `MAX_LLM_AGE_SECS` / probes / disk warn / critical | script env | 5,400 s / 120 and 600 s / 85% / 92% | pages | ntfy | ai1 probe |
| search canary fails / cooldown | script env | 2 / 3,600 s | pages | ntfy | local |

## 6. Runtime safeties (kept; listed so nothing is hidden)

| Knob | Where | Value | Protects |
|---|---|---|---|
| `LEGBA_RECONCILE_HEAL_TIMEOUT_SECONDS` / `LEGBA_ACTOR_TURN_OP_TIMEOUT_SECONDS` / breaker trips / cool-off | env | 20 s / 30 s / 3 / 600 s | one wedged actor from freezing the plane; a long turn logs `budget_exceeded` on the reconciler's activate and releases, which is normal |
| `ReconcileLoop.run_once_timeout` / `resync_interval` | code | 90 s / 5 min | reconcile head-of-line |
| `LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS` / registry API timeout / Dapr max body | env | 180 s / 10 s / 16 Mi | invoke hangs, payload size |
| NATS JetStream memory / file · inbound messages / age · PG pool · Redis maxmemory · container memory limits (registry 1 g, runtime 4 g, postgres 12 g, opensearch 3 g, qdrant 2 g) · log rotation 100 m × 5 | compose | as listed | the host |
| Anthropic per-model output ceilings · `max_retries` 3 · the vLLM handler omits max_tokens | code | 128k · 3 · by design | provider contracts |
| traces TTL · prompt render stored · completion text stored | env / code | 45 d · 32,000 chars (sha over the full text) · 4,000 | row bloat, not prompts |
| GEPA call timeout / valset · optimizer traces / generations / rows | env / code | 120 s / 40 · 50 / 5 / 500 | optimizer runs (parked) |

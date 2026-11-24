<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Tunables

For someone adjusting a running instance. Every budget, cap, governor and threshold, where it lives
and its shipped default. Values are the code defaults, or the value the shipped descriptor or pack
file sets; `.env.example` sets a few differently, noted where it does.

**Where** says how to change it:

- `env`: edit `.env` and recreate the container that reads it.
- `descriptor` / `pack` / `stack`: a registry PUT of that analyst or source descriptor, action
  pack or stack component; it takes effect without a rebuild.
- `code`: a module constant; edit and redeploy.
- `script`: an environment variable of a host script or its cron file.

## Paths that can spend money

Self-hosted models and local containers cost nothing per call. Money moves only when a component
points at a priced provider. These knobs bound it:

| Path | Bound |
|---|---|
| any LLM component | `price_input_per_m` / `price_output_per_m` record cost on each receipt; `daily_burn_alert_usd` pages ([AI_MODELS.md](AI_MODELS.md)) |
| the paid search rung | the `web_access` pack's `max_cost_usd_per_day`; a metered rung is refused without one |
| the research pack | its `max_cost_usd_per_day` |
| the correctness grader's hosted families | `LEGBA_GRADER_DAILY_CEILING_USD` (default 0: hosted families do not run) |
| consult on a hosted route | `LEGBA_CONSULT_MAX_COST_USD_PER_RUN`, `LEGBA_CONSULT_TURN_COST_CEILING_USD` (§3) |

## 1. Search and web egress

| Knob | Where | Default | Bounds | When hit |
|---|---|---|---|---|
| `web_access` `max_invocations_per_hour` / `api_rate_per_minute` | pack (`action_pack_web_access.yaml`) | 1,000,000 / 1,000,000 | all web search and fetch calls, shared | blocked |
| `web_access` `max_cost_usd_per_day` / `fallback_providers` | pack | 1.0 USD / as declared | the paid search rung | paid rung refused; free rung answers |
| `research` hour / minute / `max_cost_usd_per_day` | pack (`action_pack_research.yaml`) | 1,000 / 100 / 2.0 USD | the researcher's egress | blocked |
| other packs' hour / minute governors | pack yamls | escalate 30/5, incident_response 60/10, journal_propose 60/30, journal_read 600/60, substrate_read 600/60 | internal tool calls | blocked |
| `media_processing` hour / minute / cost | pack | 200 / 30 / 10.0 USD | media jobs | blocked |
| `reference_builder` `governor_override.max_invocations_per_hour` | descriptor | 1,000,000 | the builder's web calls (can only tighten) | blocked |
| `MAX_RESULTS_CAP` | code (`data/stack/search/base.py`) | 30 | results per search | clamped |
| `SearxngSearchHandler.max_extra_pages` | code | 1 | extra pages when page one is short | stops at page one |
| search provider `max_results` / `timeout_seconds` | stack | 30 (1–50) / 15 s | per provider | clamped |
| web fetch bytes / research text chars | code (`agency/web_tools.py`, `research_tools.py`) | 200,000 B / 200,000 chars | page size handed to a model | truncated |
| SearXNG timeouts, retries, redirects, pool | `docker/searxng/settings.yml` | 10 s / 15 s max / 1 / 5 / 10 | upstream engine calls | engine skipped |
| SearXNG `suspended_times` (denied / too many / captcha) | same | 900 s / 900 s / 21,600 s | cool-down after a block | engine skipped |
| `LEGBA_SEARCH_DEGRADED_MIN_UNRESPONSIVE` | env | 1 | unresponsive engines at which a search with results reads as degraded | a degraded not-found reads `search_failed` |
| `LEGBA_SEARCH_CONTROL_PROBE_TTL_SECONDS` | env | 300 | how long an empty-result liveness probe is cached per provider | one probe per window |
| `LEGBA_PACK_TOOL_TIMEOUT_SECONDS` / `_STALE_RECONCILE_SECONDS` | env | 60 / 300 | one pack-tool dispatch | fails / swept |
| `LEGBA_FETCH_IMPERSONATE` | env | `off` (`on`, `fallback`) | browser-fingerprint fetches | — |
| `LEGBA_FETCH_TLS_CIPHERS` | env (`data/sources/_egress.py`) | `DEFAULT:@SECLEVEL=2` | the cipher list guarded fetches offer; empty uses the image's default | — |
| `LEGBA_FETCH_HTTP2` | env | on | whether guarded fetches offer HTTP/2 | `off` = HTTP/1.1 only |
| `fetch.http_version` | descriptor (source `fetch:` block) | `auto` | `auto` retries once on HTTP/1.1 after a CDN challenge or an HTTP/2 failure; `h2`; `http1.1` | protocol recorded on the poll outcome |

## 2. Judging and grading

| Knob | Where | Default | Bounds | When hit |
|---|---|---|---|---|
| sampling temperature | code (`legba.shared.sampling.HOUSE_TEMPERATURE`) | 1.0 on every call | any other value is refused at descriptor save | — |
| `LEGBA_VERIFY_LLM_JUDGE` | env | off (`.env.example` sets 1) | whether the LLM judge runs | off = citation floor only, labelled `judge-unavailable` |
| `LEGBA_JUDGE_STACK_REF` | env | unset: the descriptor's `judge`, else `verify` component | which model judges | — |
| `method.llm.judge_chain` | descriptor | unset: `LEGBA_JUDGE_CHAIN_DEFAULT` | fallback rungs after the judge route | 401/402/403/404 move to the next rung |
| `LEGBA_JUDGE_CHAIN_DEFAULT` | env | `llm.verify.slm_8b`; empty = no fallback | rungs when a descriptor declares none | all failed = floor critique |
| `LEGBA_JUDGE_WEAK_RUNGS` | env | `llm.verify.slm_8b` | rungs whose verdicts are marked weak | label only |
| `LEGBA_VERIFY_CLAIM_AS_FACT` | env | `soft` | fail class of a claim stated as fact (`soft` or `hard`) | span marked at write |
| `judge_sample_rate` / `judge_sample_always` | descriptor | 1.0 | share of findings judged | unjudged rows capped at 0.85 |
| `PROVISIONAL_SCORE_CEILING` / `UNASSESSABLE_GATE_SCORE` / `CARRIED_UNGRADED_GATE_SCORE` | code (`provenance/judge_assessability.py`) | 0.85 / 0.5 / 0.5 | score of unjudged, unassessable or carried rows | capped |
| `LEGBA_JUDGE_FLOOR_ESCALATION` | env | on | re-judge an unsampled row the floor would exclude | — |
| judge retries / backoff / budget / retry-after cap / `max_tokens` | code (`provenance/judge_transport.py`) | 3 / 2–8 s / 45 s / 30 s / 16,384 | one judge call | falls back to the floor |
| judge evidence per claim: total / per source / grounding | code (`provenance/verify.py`) | 8,000 / 6,000 / 4,800 chars | evidence the judge sees | truncated |
| `_CLAIM_VERDICTS_CAP` / verdict text | code | 120 / 1,200 chars | verdicts per report | truncated |
| `LEGBA_COMPOSITION_VERIFY_FLOOR` | env | 0.50 | sub-claim admission to a composition | excluded |
| `LEGBA_EXTERNAL_GRADING_WIDTH` | env | off | audit at width instead of the small daily sweep | — |
| `standing_auditor` per tick / per day / searches per day / queue | descriptor | 200 / 5,000 / 10,000 / 20,000 | audit volume; the tick is clamped to the pack governor | queue waits |
| `standing_auditor` `serp_provider_order` | descriptor | as shipped | the search ladder | — |
| `EGRESS_CALLS_PER_CLAIM` | code | 3 | searches and fetches per audited claim | — |
| daily sweep claims / desks / search limit | code (`standing_auditor.py`) | 6 / 3 / 5 | the non-width sweep | — |
| `DOUBLE_GRADE_FRACTION` | code | 0.10 | second-rater sample | — |
| `LEGBA_EXTERNAL_AUDIT_WINDOW_BASIS` / `_WINDOW_GRACE_HOURS` | env | `heads` / 0 | which window an audited span must fall in | `out_of_window` |
| audit fetch timeout / page chars / search timeout | code | 25 s / 200,000 / 45 s | span checks | `UNCHECKED` |
| `LEGBA_LLM_ROUTE_MIN_CALLS` / `_SUCCESS_FLOOR` | env | 20 / 0.05 over 24 h | when a dead model route pages | high-severity alert, budget-exempt |
| `LEGBA_LLM_INHOUSE_HOSTS` | env | derived from the core component's host | host suffixes counted as self-hosted | a self-hosted 4xx does not page `llm_route_auth_failed` |
| `LEGBA_LLM_AUTH_PAGE_DEDUP_HOURS` / `_RECEIPT_LAG_S` | env | 6 / 3,600 | one auth-failure page per component per window | — |
| `LEGBA_GRADER_DAILY_CEILING_USD` | env | 0 | spend on the grader's hosted families | families skipped |
| `LEGBA_GRADER_PASS_BUDGET_SECONDS` | env | 300 | seconds one grader tick may hold its turn; `<= 0` unbounded | tick ends, tail deferred |
| `LEGBA_GRADER_REFERENCE_GRACE_DAYS` | env | 7 | how long a reference stays current past its window | `reference_stale` |
| grader targets per run / claims per unit / claims per run / head window | descriptor | 32 / 40 / 2,400 / 14 d | grading volume, least recently graded first | rest rotates in |
| agreement bars pooled / pairwise, calibration draw | code | 0.75 / 0.70, 30 | the grading instrument's gate | grader refuses |
| `LEGBA_COMPOSITION_CORRECTNESS_GATE` | env | off | compose vs quote a unit by its correctness | — |
| `LEGBA_COMPOSITION_GATE_MIN_CORRECTNESS` / `_MIN_COVERAGE` / `_ALLOW_SINGLE_FAMILY` | env | 0.8 / 0.2 / off | the gate's bars | unit quoted, not composed |
| `LEGBA_REFERENCE_BUILDER_ENABLED` | env | off | the reference builder | no references |
| `LEGBA_REFERENCE_DETERMINISTIC_ENABLED` | env | off | the keyed reference kind | nothing fetched |
| `constants.reference_subject_tags` | profile | as the profile sets | extra subjects the builder and grader cover | — |
| `build_max_seconds` / `build_max_tokens` | descriptor (env mirrors `LEGBA_REFERENCE_BUILD_MAX_*`) | 900 s / 1,200,000 | one reference build | `no_commit` |
| builder tool calls / targets per run / window / cadence / thin bar | descriptor | 200 / 1 / 14 d / 7 d / 2 | one build | build ends; roster rotates |
| `LEGBA_REFERENCE_RETRY_BACKOFF_HOURS` | env | 24 | retry after a failed build (3 failures = unbuildable) | skipped |
| `desk_reference` pairs / items / search limit / window | descriptor | 40 / 5 / 8 / 24 h | daily desk references | rest next day |

## 3. Reads, compositions and consult

| Knob | Where | Default | Bounds | When hit |
|---|---|---|---|---|
| `LEGBA_LLM_INPUT_TOKEN_BUDGET` | env | 32,000 | evidence packed into one desk or composition prompt | oldest evidence left out |
| `method.gather.max_rounds` | descriptor (1–6) | 1 unless set | agentic gather rounds | the unit writes with what it has |
| `method.options.evidence_char_cap` | descriptor | derived from the input budget on multi-round gather; 0 = off | evidence in one gather call | older tool results replaced by a digest |
| `LEGBA_SLICE_ROW_CAP` / `LEGBA_GLOBAL_SLICE_PER_SOURCE_CAP` | env | 120 / 15 | rows a desk is shown; rows per source | truncated |
| `method.options.slice_per_source_cap` | descriptor (1–500) | unset | rows one source contributes to this analyst | strict cap, no back-fill |
| substrate_read row limit | code | 200 | one tool call | clamped |
| composition block caps | code | 8 per country/thematic tier, 16 world tier | blocks a composition carries | rest named, not carried |
| `LEGBA_STALE_READ_CADENCE_MULTIPLE` | env | 3 | a quoted unit read older than cooldown × this is marked stale | marked, still selected |
| `LEGBA_CARRY_EXPIRY_WINDOWS` | env | 3 | a status no indicator re-evidenced within cooldown × this is carried, unsupported | lifted out of the basis |
| `method.options.level_indicators` | descriptor | unset | indicators that measure a standing level | an unsupported level is set back to `not_observed` |
| `indicator_tracker` `hysteresis_runs` / `hysteresis_hours` | descriptor | 2 / one unit cadence | consecutive runs before a status change counts | the change is not confirmed |
| `method.llm.max_tokens` | descriptor | per descriptor | output length on hosted routes only | truncated |
| `method.budget_tokens_per_day` | descriptor | 0 = no per-analyst budget | daily tokens per analyst | paused until the next window |
| `global_budget_envelope.tokens_cap` | database row (`/api/v1/budget/envelope`) | no row = no cap | daily tokens across all analysts | all demoted |
| `cadence.cooldown_seconds` | descriptor | per descriptor | spacing between runs, from run start | tick does nothing |
| `LEGBA_CONSULT_BUDGET_SECONDS` / `_ROUND_DEADLINE_SECONDS` / `_FINAL_FLOOR_SECONDS` / `_WALL_BUDGET_SECONDS` | env | 480 / 150 / 240 / 210 | consult wall clocks | forced final answer; partial kept |
| `LEGBA_CONSULT_NATIVE_BATCH_CAP` | env | 4 | tool calls per round | — |
| `LEGBA_CONSULT_MAX_INPUT_TOKENS_PER_RUN` / `_MAX_COST_USD_PER_RUN` | env | 150,000 / 3.00 USD | one consult run's drilling | forced final answer |
| `LEGBA_CONSULT_KEEP_FULL_ROUNDS` / `_ROUND_RESULT_BYTES` | env | 1 / 16,000 | tool results kept in full | older rounds compacted |
| `LEGBA_CONSULT_TURN_COST_CEILING_USD` | env | unset = no per-turn ceiling | projected cost of a turn on a priced route | the turn stops drilling and answers; 0 or invalid refuses every turn |
| `LEGBA_CONSULT_DEFAULT_WINDOW_HOURS` | env | 168 | the window when a question names none | — |
| `LEGBA_CONSULT_JUDGE_STACK_REF` / `_TIMEOUT_SECONDS` / `_MAX_CLAIMS` | env | `llm.verify.slm_8b` / 90 / 40 | judging a consult answer's cited claims | not judged, or first N judged |
| `LEGBA_CONSULT_JUDGE_MAX_INPUT_TOKENS` | env | 12,000 | the consult judge's input | tail claims dropped and counted |
| `method.options.context_ceiling_tokens` | descriptor (consult) | derived from the model's context window | estimated input per consult call | older uncited results withheld |
| `LEGBA_CONSULT_INVOKE_TIMEOUT_SECONDS` | env | 900, raised to at least budget + final floor + 300 | one consult invocation | — |
| `LEGBA_RATE_LIMIT_CONSULT` / `_DEEP_CONSULT` | env | `10/minute` per principal | requests | 429 |
| `LEGBA_SITUATION_TRACKER_MAX_SITUATIONS` | env | 12 (ceiling 500) | situations per tick | rest next tick |
| `grounding.max_facts` | descriptor | as shipped | facts in the grounding preamble | truncated |
| `LEGBA_FRAME_FAMILY` | env | off | read composition groups from the `frame` vocabulary family | off = legacy id-prefix reads |
| `LEGBA_DESK_GROUNDING_INCLUDES_CONSULT_WATCHES` | env | off | consult-minted watches in the desks' open-question block | excluded |
| consult watch fold cosine / horizon | code (`consult_watch_fold.py`) | 0.86 / ±3 days | folding a new watch onto an existing one | minted unchecked past the caps |

## 4. Sources, enrichment and events

| Knob | Where | Default | Bounds | When hit |
|---|---|---|---|---|
| `poll_budget_seconds` / `max_entries_per_poll` | descriptor (ceilings 240 s / 1,000) | 30 s / 100 | one poll | truncated; cursor advances |
| `_ENRICH_TIMEOUT_S` | code | 12 s | per-entry enrichment | entry skipped |
| Telegram per-channel / cycle / poll budget | descriptor | 45 s / 360 s / 240 s | the Telegram poll | channel abandoned for the poll |
| RSS timeout / retries | descriptor | 30 s / 1 | per-feed pulls | — |
| `LEGBA_ENTITY_ALIAS_LOOKUP` | env | `scan` (`shadow`, `table`) | how entity names match aliases | unknown value = `scan` |
| `LEGBA_INTRASOURCE_DEDUP` / `_WINDOW_HOURS` | env | on / 168 | exact-hash deduplication within a source | duplicate skipped |
| `LEGBA_NLP_COMPONENT_ID` | env | `nlp.local.legba_models` | which NLP component enrichment binds | missing component fails at first use |
| `LEGBA_CONTENTION_PASS_BUDGET_SECONDS` / `_REFRESH_HOURS` | env | 120 / 24 | one arbiter pass; re-deciding an unchanged dispute | rest next tick |
| `LEGBA_REIFIER_PASS_BUDGET_SECONDS` | env | 120 | one reifier tick | partial receipt |
| `LEGBA_COVERAGE_FLOOR_*` | env or descriptor | window 14 d, 20 signals, 10 days, magnitude 0.50, 15 high, share 0.10, 5 clusters, 6 h | when a blind spot is flagged | flagged |
| `LEGBA_CRITIC_FANOUT_MAX` | env | 12 per tick | critic drain | queued |
| `LEGBA_TAG_VOCABULARY_TTL_SECONDS` | env | 300 | how long the runtime's vocabulary snapshot is used | refreshed in the background |
| `LEGBA_EVENTS` | env | off | the event write path | no event rows |
| `LEGBA_EVENT_CLUSTERING_PASS_BUDGET_SECONDS` / `LEGBA_EVENT_RECONCILER_PASS_BUDGET_SECONDS` | env | 120 / 120 | one tick; `<= 0` unbounded | partial receipt and cursor |
| `LEGBA_EDGE_TRANSITION_LEDGER` | env | off | entity-edge transition rows | none written |
| `LEGBA_DESK_ROWS_SHADOW` | env | empty | desks that run a detached contract-v2 shadow | — |
| `LEGBA_CITATION_ROWS` | env | on | citations written as rows | off = stored citations only |
| `LEGBA_CITATION_REFUSE_UNRESOLVED` | env | off | refuse a finding whose citations name no record | the run fails |
| `LEGBA_EVENT_CITATIONS` / `LEGBA_EVENT_EXPANSION_MAX_SIGNALS` | env | off / 4 | `event:<uuid>` citations and their expansion | links past the cap dropped |
| `LEGBA_GRAPH_PROJECTION` / `_MAX_AGE_SECONDS` | env | off / 7,200 | the `graph_arcs` projection | readers answer `projection_disabled` / `_stale` |
| `LEGBA_PROFILE_PROJECTION` / `_MAX_AGE_SECONDS` | env | off / 7,200 | the per-profile projection | same |
| `LEGBA_ARCHIVE_RETENTION` | env | off | removing bytes of uncited archive rows past `retention_days` (45) | rows kept as `expired_uncited` |
| `LEGBA_ANALYST_TRACES_DELETE_ENABLED` / `LEGBA_ANALYST_TRACES_TTL_DAYS` | env | off / 0 (purge disabled) | deleting old traces | nothing deleted |
| `LEGBA_ARCHIVE_CODEC` / `LEGBA_ARCHIVE_ZSTD_LEVEL` | env | `zstd` / 3 | codec of new archive objects | — |
| `LEGBA_ARCHIVE_HOT_DAYS` / `LEGBA_ARCHIVE_PACK_MB` | env | 90 / 512 | the cold-tier mover ([operations/archive-tiering.md](operations/archive-tiering.md)) | — |
| `LEGBA_ARCHIVE_COLD_BACKEND` | env | unset = no cold tier | `local` or `s3` | the mover refuses |
| `LEGBA_FETCH_RECEIPTS` / `LEGBA_FETCH_RECEIPT_MAX_BYTES` | env | on / 64 MiB | keeping fetched bodies as receipts | above the cap, hash only |
| `LEGBA_FETCH_RECEIPT_UNREFERENCED_AFTER_DAYS` / `_PRUNE_BATCH` / `_PRUNE_MAX_BATCHES` | env | 30 / 500 / 20 | pruning bytes no output references | referenced bytes never pruned |
| `prefer_ingest_copy` (evidence archiver) | descriptor | true | archive the bytes the poll kept before re-fetching | — |
| `series_change` windows and bars | descriptor | 7 d / 28 d / 2.0 σ / 5 / 14 | the change rule over live series | too little history = `insufficient` |
| `series_api` `lag_days` / `revision_days` / `backfill_days` | descriptor | 2 / 30 / 400 | how far each poll re-reads | — |
| `watch_horizon_sweep` `batch_limit` | descriptor | 500 | expired consult watches closed per tick | — |

## 5. Alerts and watchdogs

| Knob | Where | Default | Bounds | When hit |
|---|---|---|---|---|
| `LEGBA_ALERT_DAILY_PAGE_BUDGET` / `LEGBA_ALERT_BUDGET_PER_KIND_CAP` | env or descriptor | 5 / 3 | pages per day and per kind from the trigger scan | held, recorded |
| `LEGBA_ALERT_NTFY_MIN_SEVERITY` / `LEGBA_ALERT_WEBHOOK_MIN_SEVERITY` | env | `high` / `high` | sink floors for anything the page rule did not decide | skipped, recorded |
| `LEGBA_ALERT_NTFY_TOPIC_WORLD` / `_ENGINE` / `_SPLIT` | env | `world` / `engine` / on | the two outward topics | `SPLIT=0` = one topic |
| `LEGBA_ALERT_PAGE_RULE_PATH` / `LEGBA_ALERT_CRITICAL_REPAGE_HOURS` | env | the shipped `data/alerts/page_rule.yaml` / 6 | which alerts page ([ALERTING.md](ALERTING.md)) | logged only |
| `LEGBA_ALERT_SUPPRESS_LEVEL_ONLY` | env | on | an escalation at the same severity as before | recorded, not pushed |
| `LEGBA_ALERT_BAND_BATCH_MAX_LINES` | env | 8 | band moves listed per batched push | counted, not listed |
| `LEGBA_ALERT_VOLUME_DAILY_CAP_WORLD` / `_ENGINE` | env | 50 / 50 | pushes per topic per 24 h | pages `alert_volume_high` |
| `LEGBA_VOLUME_SURGE_CHECK` and thresholds | env | on; 60 min, 12 signals, 8 outlets, z 5, ratio 3, 14 d baseline | `volume_surge` | raised at medium |
| `LEGBA_SOURCE_CADENCE_STALL_CHECK` | env | on | the old per-source stall alert | skipped when off |
| `LEGBA_SEARCH_DEGRADED_PAGE_SHARE` / `_MIN_NOT_FOUND` | env | 0.20 / 20 | `search_degraded` page | high-severity page on entry |
| `LEGBA_ALERT_RAISE` / `LEGBA_ALERT_CLI_CONTAINER` | script (`scripts/lib/legba_alert.sh`) | off (the shipped cron files set 1) / `legba-legba-runtime-dapr-1` | host scripts raise through the alert path | direct push |
| host stall watchdog max age / cooldown / grace / verify wait | script | 1,800 / 2,700 / 900 / 720 s | the restart recipe | restarts sidecar and runtime |
| LLM heartbeat max age / disk warn / disk critical | script | 5,400 s / 85% / 92% | heartbeat pages | ntfy `engine` |
| disk floor warn / urgent / critical | script (`scripts/disk_floor_check.sh`) | 85 / 90 / 93% | disk pages, one per band entry | ntfy `engine` (`LEGBA_ENGINE_NTFY_URL`) |
| `LEGBA_NIGHTLY_NTFY_URL` | script | `http://127.0.0.1:8093/nightly` | nightly suite verdicts | — |

Every alert kind names its topic in `data/alerts/topics.py`; the kinds and the page rule are in
[ALERTING.md](ALERTING.md).

## 6. Runtime safeties

| Knob | Where | Default | Protects |
|---|---|---|---|
| `LEGBA_RECONCILE_HEAL_TIMEOUT_SECONDS` / `LEGBA_ACTOR_TURN_OP_TIMEOUT_SECONDS` | env | 20 / 30 | one wedged actor freezing the actor plane |
| `ReconcileLoop.run_once_timeout` / resync interval | code | 90 s / 5 min | reconcile head-of-line blocking |
| `LEGBA_LOOP_LAG_WARN_SECONDS` | env | 2; `<= 0` disables | logs `loop_lag.blocked` when the runtime's event loop stalls |
| `LEGBA_REGISTRY_WORKERS` | env | 1 | uvicorn workers behind the registry ([RUNBOOK.md](RUNBOOK.md) "Registry workers") |
| `LEGBA_ACTOR_INVOKE_TIMEOUT_SECONDS` | env | 180 | actor invocations |
| `LEGBA_CORE_LLM_MAX_CONCURRENT` / `_INTERACTIVE_RESERVED` / `_COMPONENT_ID` | env | 12 / 2 / `llm.primary.openai_compat` | in-flight calls to the core model per process ([AI_MODELS.md](AI_MODELS.md)) |
| `thinking_model_name`, `reasoning_budget_param` + `_tokens` | stack | unset | which name a thinking call sends; a reasoning budget |
| container memory limits, log rotation | compose (`mem_limit` per service; `docker-compose.yml`) | per service; logs 100 MB × 5 | the host |
| trace prompt / completion stored | code | 32,000 / 4,000 chars (hash over the full prompt) | row size |
| `LEGBA_NIGHTLY_GATE_PHASES` | script (`scripts/host_nightly_suite.sh`) | `ordered` | the test phases whose failure makes the nightly verdict FAIL; other phases are advisory |
| `LEGBA_NIGHTLY_HYGIENE` / `_TIMEOUT` / `LEGBA_NIGHTLY_REGISTRY_BASE` | script | 1 / 300 s / `http://127.0.0.1:8090` | three read-only reports after the tests; logged, never gating |

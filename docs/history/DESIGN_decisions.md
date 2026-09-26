<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Design-tier decision records

Everything dated, ticketed or narrated as "was / now" that was extracted from
`ARCHITECTURE.md`, `DESIGN.md` and `DIRECTION.md` when those three were
rewritten in the present tense. The design tier now states the rule and its
reason; this file keeps when the rule arrived, what it replaced, and the
measurement that bought it.

Chronological, oldest first, grouped by month. An entry names its source doc in
square brackets. Ticket ids are defined in `TICKETS.md`; seam numbers in
`SEAMS.md`.

---

## Undated, carried from the pivot

- **The pivot baseline.** The historical `0001`→`0031` migration chain was
  flattened into a single `0001_baseline.sql` for the clean-slate release; it
  remains in git history. A cold start applies the baseline plus the forward
  chain. [DESIGN §7]
- **`0012_analyst_outputs.sql`** created the generic kind-discriminated output
  table; **`0024_pivot_substrate.sql`** created the target-agnostic `signals`
  table; **`0027`** added finding-level supersession
  (`analyst_outputs.situation_signature`, `superseded_by`, the
  `finding_supersessions` link table); **`0028`** added `trigger_state`;
  **`0022`** added the global budget envelope and `budget_demotion_events`;
  **`0025`** added `action_pack_invocations`; **`0026`** discovery state;
  **`0014`** `source_credibility`; **`0015`** `cost_model`; **`0017`**
  `ui_panel_registrations`; **`0019`/`0020`** ISO-country and geopolitical
  vocabulary seeds. [DESIGN §7.1–§7.4]
- **Decision D1 — single-operator, single-tenant (locked).** One operator, one
  instance, one shared edge credential, one logical tenant. Not a temporary gap:
  a locked product decision. No multi-tenant, RBAC or per-role isolation is
  claimed anywhere. [DIRECTION §0; ARCHITECTURE §14]
- **Decision D3 — the no-stubs rule.** A stub or mock in a production path is
  forbidden; anything not built is declared in `SEAMS.md` and fails loud.
  `tests/test_no_undeclared_stubs.py` is the mechanical enforcement, with no
  per-line pragma escape. [DESIGN; SEAMS.md]
- **Decision F-1 — the `discovery` action pack retired.** The
  `discover_sources_tool` agency tool enqueued a `crawl_discovery` job kind that
  `default_dispatch` never registered, so an enqueued discovery job would die
  `failed: no handler`. The enqueue was dropped rather than left to fail
  downstream; the job-kind strings stay documented and nothing enqueues them
  until something consumes them. Deep crawl returns as a job handler converging
  on the same validate-before-register materializer the descriptor route uses.
  [ARCHITECTURE §5.6; DESIGN §4.8; DIRECTION §8]
- **Decision F-2 — fallback-model budget demotion.** The actor-side machinery is
  built (`_AnalystDeps.fallback_run_method`, `BudgetEnforcer.precall_check` →
  `record_demotion` → `budget_demotion_events`, `options["llm_demoted"]`
  stamping). The supply side is not: the deps builder resolves only
  `method.llm["primary"]`. So `demote_and_continue` without a wired fallback
  falls into `pause_until_next_window` — an explicit, audited pause. Deliberate:
  no fallback wired means pause loudly rather than fabricate a cheaper run.
  [ARCHITECTURE §7.2; DIRECTION §7]
- **Decision D4 — signals retention.** The `signals_retention` deterministic
  sub-handler deletes signals past a configurable `ttl_days` and co-deletes
  their value-referenced children in one transaction;
  `retain_always` / `evidence_hold` rows are never purged. Migration `0036` adds
  the purge-scan index. It ships disabled (`ttl_days <= 0`). Range-partitioning
  remains the long-term answer when volume warrants. [DIRECTION §6]
- **Decision B15 — the contention arbiter is detect-only.** It never mutates a
  fact's `value` / `valid_until` / `superseded_by` / `confidence` and never calls
  `supersede_prior_facts`. [ARCHITECTURE §5.9]
- **Review 2.4 — `integrity_verification` deleted, re-homed as
  `integrity_sweep`.** The predecessor's first check anchored on the dropped
  `events` table, so in production the whole sweep aborted, the error was
  swallowed, and it emitted a zeroed "no issues" finding — fake success. It was
  deleted under the no-stub rule. Only the checks that run against live
  pivot-era tables survive (orphan `signal_entity_links`, orphan
  `proposed_edges`, `facts` with no `evidence_set`, broken
  `analyst_outputs.superseded_by`); it refuses loud, is read-only, and the
  predecessor's destructive auto-repairs were deliberately not re-homed.
  [DIRECTION §9]
- **Correction to an earlier DIRECTION draft.** It claimed `facts.superseded_by`
  and the `nexuses` table had been dropped. Both are live: `0032` restored the
  temporal fact columns and `0033` created `nexuses`. [DIRECTION §9]
- **Review G2 — the `substrate_read` pack.** The consult kind's read tools used
  to dispatch straight at the substrate query port, bypassing the agency plane;
  the pack puts them under resolve ∩ allow ∩ applicability → governor →
  dispatch → settle, landing rows in `action_pack_invocations`. [DESIGN §4.8]
- **Review G3 — media stand-ins removed outright.** The `MediaClient` stub
  fallback and the eager tier's `EchoCaptionExtractor` placeholder caption were
  deleted. With `LEGBA_MEDIA_API_URL` unset, `MediaClient.extract` raises a typed
  `MediaEndpointNotConfiguredError`, `process_media` enqueue and execution refuse
  (terminal failed, no row written), and a `media: "eager"` descriptor refuses
  activation. A stub result is structurally unrepresentable —
  `MediaExtractionResult.source` is `Literal["hosted"]`. [DIRECTION §5]
- **Review S-1 — untrusted RSS reaching an actor.** The three-way agency gate,
  the per-pack governor, mandatory provenance and the operator-held allow leg are
  its closure. [AGENCY_GATING_MODEL.md, referenced from ARCHITECTURE §5.6]
- **B-2 — the registry API fails closed.** An unset bearer token returns HTTP 503
  on every guarded request unless `LEGBA_DEV_MODE=1` is set explicitly.
  [DESIGN §5.4]
- **B-1 — one published port.** The edge proxy is the only service allowed to
  publish on all interfaces; every other host port binds loopback.
  [docker-compose.yml, carried into DESIGN §11]
- **L-113 — dev-mode accept-all.** With the token env unset the API accepts all
  and logs once at WARN; documented behaviour, not an accident. [DIRECTION §1]
- **L-120 — the STIX bundle producer.** `FindingPayload` → `report`
  (`threat-report`); `SituationPayload` → `incident` + wrapping `report`;
  `HypothesisPayload` → `report` (`analysis`-typed); `AlertPayload` →
  `indicator` at medium+ severity, `report` below. TLP marking from the
  descriptor; `derived_from` becomes `derived-from` relationship objects; cited
  entities become `identity` / `location` objects. Bundles publish to
  `legba.outputs.stix.<target_id>`; the TAXII collection convention is
  `legba_target_<target_id>_collection`. [DIRECTION §3]
- **L-205 / P-16 — Temporal removed.** No Temporal frontend, history or matching
  cluster, no `temporal_persistence` / `temporal_visibility` database pair, no
  second worker image. The `runtime/temporal/` package was deleted (P-CUT/C-3);
  its live pieces — the workflow-I/O dataclasses and the GEPA loop body — moved
  to `runtime/dapr_workflow/gepa.py`, so the algorithm lives in one place. The
  `OptimizerDeps.temporal_client` slot is historically named and just means "the
  workflow client". The registry image likewise stopped importing torch / spacy /
  sentence-transformers / scipy. [DESIGN §6.7, §10; ARCHITECTURE §9]
- **L-377 — the MCP tool registry.** `MCP_TOOL_REGISTRY` is populated by the
  runtime at descriptor-activate time from `outputs.mcp_tool` blocks; a
  standalone `legba-mcp` process therefore saw an empty catalog until the
  built-in tool set landed. [DIRECTION §4]
- **P-07 — the async job plane.** Queue, worker pool, ledger and derived-signal
  landing all run for real; the extraction edges are the seam. [DIRECTION §5]
- **P3-1 — `proposed_edge_governance`.** Promotes or rejects the
  `proposed_edges` queue into `nexuses`, rejecting demonym and junk endpoints.
  [ARCHITECTURE §5.3]
- **P4-T8 — the optimizer honesty suite.** Guards that a candidate cannot
  auto-promote on a degenerate, absent, non-finite or non-positive delta;
  `gepa._delta_gates_ok` stamps `data.eval.promotable` at write time.
  [DESIGN §3.6]
- **A-3 / A-3a — the agency plane on-ramp.** Two built-in paths drive it in
  production: the consult kind routes its ReAct tool calls through the governed
  `substrate_read` pack, and the actor run path fires `escalate_finding` when a
  finding crosses its gate. [DESIGN §6.6]
- **A2 — per-(analyst, target) fan-out concurrency.** `_fanout_to_workers`
  chunks matched targets at `_FANOUT_CHUNK = 5` and dispatches one run per
  target to a distinct worker actor, bounded by a semaphore.
  [ARCHITECTURE §7.2]
- **LIC-2 — the archiver licence gate.** A source whose declared `license_class`
  is in the forbidden set is skipped with an honest recorded counter, never
  quietly fetched; an unknown class archives with the class recorded for later
  re-evaluation. [ARCHITECTURE §8.6]
- **Seam #11 — the `world_context` vector grounding source** was accepted but
  inert until the embedder-through-port wiring landed (L-114). Resolved.
  [ARCHITECTURE §5.8; DIRECTION §10]
- **Seam #15 — the A2A skill output router** mounts only when
  `LEGBA_A2A_ENABLED=1` with a non-empty `LEGBA_A2A_TRUSTED_KEYS` allowlist, or
  in dev mode. By default the surface is fail-closed off and answers
  `/a2a/skills` with 503 plus an enable recipe, never a silent 404.
  [DESIGN §6.1]
- **Seam #19 — `cross_source_coalesce`.** Built, off by default. It requires the
  embedding service and Qdrant ports and refuses loud when either is absent —
  emitting a `coalesce_unavailable` finding and writing zero `signal_aliases`
  rows — because there is no non-vector deterministic fallback for "same event,
  different words". [DESIGN §7.1]
- **Seam #23 — the Dapr long-activity workflow round-trip.** On daprd 1.17.9 a
  Workflow orchestrator does not reliably resume after a *long* activity (short
  ones deliver), even though the activity runs and returns; the GEPA optimizer
  and deep-consult round-trips degrade to an in-process fallback. Verified not
  our code — threads idle post-compile, reproduces on a fresh engine.
  [ARCHITECTURE §14]
- **Seam #35 — `country_assessor` retired.** It was the single largest producer
  of unverified one-pager output. [DESIGN §1.3]
- **Seams #31–#32 — the forecast-as-claim predictors** (`country_predictor`,
  `india_energy_predictor`) retired and stopped, cadence nulled. Roughly 539
  historical `prediction` rows remain in the database, unread by the spine.
  [ARCHITECTURE §5.3; DESIGN §3.6]
- **Seam #30 — `country_optimizer` cadence-frozen.** Descriptor still
  `state=active` and byte-unchanged, `cadence.fallback_schedule` nulled so the
  on-activate `if schedule:` gate registers no reminder. It had been always-on,
  unmeasured, and the source of a reminder-flood incident (a >4 MB workflow
  payload orphaning scheduler reminders); the freeze forecloses that regression
  class. [ARCHITECTURE §10; DESIGN §6.7]
- **Seam #42 — the evidence archive.** Built: content-addressed storage of
  cited-and-verified signals' original bytes. Not built: retention or expiry over
  the archive (nothing deletes evidence), an object-store backend, and coverage
  beyond cited-only. [ARCHITECTURE §8.6]
- **Seam #49 — the `claim_watch` closer.** The watcher half is built and live;
  the closing half — propagating a confirmed match back into the producer's next
  run and closing the flag by supersession — does not exist in-tree.
  [ARCHITECTURE §14]
- **Seam #53 — the GEPA optimizer plane mothballed.** See 2026-08-21 below.
- **Hypothesis lifecycle producer not registered.** `hypothesis_lifecycle`
  emitted 0 rows (gated on `active` situations that go dormant) and
  `competing_hypotheses` superseded it; its module, tests and
  deterministic-dispatch entry are kept for a possible future
  lifecycle-maintenance role. [ARCHITECTURE §5.3]
- **The time-series metrics store removed.** A dedicated metrics store was
  provisioned-but-idle with zero callers and was removed from the codebase;
  time-series metrics became a declared seam. `anomaly_detection` reads
  `time_bucket()` from the primary Postgres pool, and `search_signals` still uses
  Postgres full-text search. Earlier drafts listed every store as first-class;
  that was corrected. [ARCHITECTURE §0.1, §5.5]
- **The `::` gotcha.** A Dapr Workflow `instance_id` must not contain `::` —
  activity result parsing splits on it and hangs forever. Worker actor ids use
  `::`; workflow ids must not. [ARCHITECTURE §9]
- **Full-width citation brackets.** Core-plane models emit `【3】` / `［3］`
  instead of ASCII `[3]`; the marker parser normalises variant brackets before
  matching. A real live failure class. [DESIGN §3.5]
- **The scheduler µs unit.** The dapr-python SDK serialises the reminder period
  with a Greek-mu `µs` unit, which the scheduler honours but with a small
  per-period fire burst, absorbed by the per-(analyst, target) cooldown and
  dedupe. [DESIGN §6.3]
- **Boot-ordering caveat.** On a cold rig the wiring pass can complete with
  `trigger_regs=0` if it runs before targets and analysts reach `active`; the
  informer and the five-minute resync re-wire once they do, and until then the
  cadence heartbeat drives coverage. [DESIGN §6.6]

## 2026-02

- **2026-02-28** — the US–Iran active-conflict fact used as the worked grounding
  example ("US–Iran active conflict since 2026-02-28"). [ARCHITECTURE §5.10]

## 2026-06

- **2026-06** — substrate data inventory verified: the five backing services and
  their datasets, producers and consumers. [ARCHITECTURE §0.1]
- **2026-06** — the alert-sink audit-plumbing fix. `_emit_output_bindings` began
  threading the `pg_pool` and the persisted output row id
  (`OutputContext.alert_row_id`) so the alert sink writes its
  `alert_sink_deliveries` audit rows. The prior `alert_sink_deliveries = 0` was
  the missing plumbing, not a missing path. Residual: successful-delivery audit
  and error audit remain in separate columns; there is no single unified
  delivery-status view. [ARCHITECTURE §8.3]
- **2026-06** — all four source-first operator panels ship registered and tested
  in the console (Action-Pack Grants, Subscription Policy/Builder, Discovery
  Pipeline, Backfill Replay). Three are live end-to-end; Backfill Replay is a
  preview tier — the panel is live but its backend POST returns an honest 501,
  because the cross-plane runtime trigger is not exposed through the registry.
  [DESIGN §5.5]
- **2026-06-10** — the agency-plane cutover, live-proven. [DESIGN §6.6]
- **2026-06-23 — the AGE re-evaluation decision.** Keep the relational `nexuses`
  table as the canonical knowledge graph with networkx for compute; treat Apache
  AGE as an optional, currently-inert acceleration path the project does not
  depend on. Measured that day: roughly 4.9k nexuses against about 10 AGE edges
  and 27 vertices in the dormant graph. Rationale: at low-thousands edge counts,
  networkx over `nexuses` is simpler, transactional with the rest of the
  substrate, and already powers grounding, agency queries and the operator
  surface; a second graph engine adds a query language, an `agtype` text-parsing
  dialect, a per-connection session tax and a sync pipeline for no current
  benefit. Both AGE write legs ship off by default (`emit_graph_edges=False` in
  `fact_extractor.py`; `LEGBA_AGE_DERIVED_FROM` off in `dapr_actors.py`).
  **Falsifiable revisit trigger:** re-evaluate a native graph engine if the live
  nexus edge count exceeds about 250k, or multi-hop traversal latency in the
  recursive-CTE / networkx path exceeds about 2 s p95 for a routine analyst
  slice. The underlying investigation additionally recommended dropping AGE
  outright as an operator-gated follow-up. [ARCHITECTURE §5.5; DESIGN §7.3]
- **Pre-2026-06 drafts' "open frontier"** — altitude 0 through 3 — was closed by
  the data-analysis rigor layer. [ARCHITECTURE §1, §13]

## 2026-07

- **2026-07-01 — grounding ported off the monolith onto the units.** The
  `GroundingBlock` opt-in moved from the retired `country_assessor` onto the
  bounded units, and the raw-signal window widened from 24h to 72h so a unit
  integrates the multi-week substrate rather than only the fresh slice.
  [DIRECTION §10]
- **2026-07-06 — the journal voice moved fully to the core plane.** The NARRATE
  voice phase previously ran on a hosted Anthropic model; it moved to
  `llm.primary.openai_compat`, so the journal costs no Anthropic spend and that
  plane stays reserved for consult and deep-consult. [ARCHITECTURE §1, §8.4;
  DESIGN §7.6]
- **2026-07-06 — the audit sweep, migrations `0076`–`0080`.** Entity re-fold and
  junk gate, semantic/junk-fact close, nexus junk / self-edge / dyad
  canonicalisation, cross-correlator stale-head sweep, state-media
  `source_credibility` seed. `0077`–`0080` are reversible.
  [ARCHITECTURE §0, §13]
- **2026-07 — the breadth wave: 51 draft source descriptors.** 41 verified
  no-auth Wave-A feeds (38 `rss` + 3 `json_api`; 25 country-scoped, 16 global)
  plus 10 riding a profile-gated local RSSHub lane (compose profile
  `sources-extra`, loopback `:1200`, reached by the ordinary `rss` handler
  through the SSRF guard's `LEGBA_EGRESS_ALLOW_HOSTS` allowlist, compose default
  `rsshub`). All registered `state: draft`, activated operator-paced.
  [ARCHITECTURE §6.1; ACQUISITION.md]
- **2026-07 — the deterministic wave.** `alert_trigger_scan` every 10 minutes and
  `evidence_archiver` every 30 minutes feed the alert and archive loops; the
  daily readout family (`band_calibration_tracker`, `fact_decay_scan`,
  `source_track_record`, `narrative_mapper`, `desk_baseline`) computes derived
  sidecars. All registered as drafts an operator activates. [ARCHITECTURE §0]
- **2026-07 — two confidence-dynamics readouts, both consumption-flag-gated off
  in code.** `fact_decay_scan` (draft) computes per-class confidence-decay curves
  with corroborations as clock-resetting sightings into a `fact_decay_states`
  sidecar, and the grounding read can carry that annotation behind
  `LEGBA_FACT_DECAY_WEIGHTING` (default off; stored confidence is never mutated).
  `source_track_record` (draft) scores each source's win/loss over resolved
  contentions, consumable in arbiter tie-breaks behind
  `LEGBA_CONTENTION_EARNED_WEIGHT` (default off; never touches faithfulness).
  [DIRECTION §10]
- **2026-07 — the judge route separation.** The faithfulness judge resolves
  through its own opt-in ladder (`LEGBA_JUDGE_STACK_REF` env > `method.llm.judge`
  > `.verify` > `.primary`), so the same future second model can serve both the
  judge route and the F-2 fallback slot. F-2 itself still waits on that absent
  second model. [DIRECTION §7]
- **2026-07-25 — Docker Swarm conversion assessed on paper.** Moving the data
  layer (Postgres / Qdrant / OpenSearch / NATS / Redis) to a second node via
  `docker stack deploy`. It does not touch the three single-node truths — a data
  node *move* changes where the single-replica NATS lives, not that it is
  single-replica. Draft stack files live under `deploy/swarm/`, explicitly
  non-deployable until phase-1 validation. Nothing is deployed; compose plus
  `deploy.sh` remain the only live path. [DIRECTION §6]
- **2026-07-28 — the alert-sink wave.** Closed the loop from a verified state
  change to an operator's device without the console open: a deterministic
  trigger analyst decides what is alert-worthy and a modular sink plane decides
  where it goes. Two sinks ship (a generic webhook and a native ntfy push sink),
  with a profile-gated local ntfy service in compose. Migrations `0091`–`0105`
  land in this wave (`0095` and `0100` intentionally unused): alert-trigger
  watermarks, poll `newest_entry_ts`, band-calibration claims, the
  source-assurance ledger, correctness labels and gold-set pinning, contention
  surfacing and the tie-break cache, fact-decay states, source track records, the
  traces-retention index, narratives and echo edges, desk baselines, the evidence
  archive, and the watchlist. [ARCHITECTURE §8.5, §13]
- **2026-07-29 — geo convergence folded into `alert_trigger_scan`** as its sixth
  trigger class; the standalone `geo_convergence_scan` was retired live.
  [ARCHITECTURE §0]
- **2026-07-29 — the first out-of-plane `claim_watch` precision measurement.**
  122 labelled pairs, pooled precision 0.279 against a recommended 0.85 bar. The
  closer stayed unbuilt; the gate did its job. [ARCHITECTURE §14]

## 2026-08

- **2026-08 — the production gauge's `production_deficit`** became
  `alert_trigger_scan`'s seventh trigger class, and `situation_escalation` its
  eighth — the first class whose subject is a frame. [ARCHITECTURE §0]
- **2026-08-01 — the turn-poisoning freeze.** A strict-mode parse bug made
  `proxy.activate()` hang rather than fail. `ENSURE_ACTIVE` fires against every
  active analyst and source on every periodic resync, so each hung activate
  consumed the full 90 s `run_once` bound; the reconcile main loop is strictly
  serial, so the queue crawled at one descriptor per 90 seconds. Dapr actors are
  turn-based with reentrancy disabled, so each hung activate occupied its actor's
  turn queue indefinitely and every subsequent reminder and coalesced fire queued
  behind a turn that would never complete. Within one resync cycle the plane was
  turn-poisoned by its own durability heal. The freeze lasted 35 minutes and
  ended only when the host stall watchdog restarted the runtime. The answer
  (S-6) is the pair in `runtime/actor_turn.py`: a deadline on the reconciler's
  per-actor heal, and bounded I/O inside the turn — plus a `HealBreaker` that
  converts repeated timeouts into skip-and-retry with a cooloff. This also
  bought the cold-activation smoke step in `deploy/deploy.sh` §(e), because every
  one of ten-thousand-odd tests stayed green through it. [runtime/actor_turn.py;
  CONTRIBUTING.md; carried into DESIGN §2.2]
- **2026-08-02 — `cross_source_dedup` became a singleton.** The descriptor
  carried a bare `subscription.targets` block with no predicate, which the
  runtime reads as "fan out to every active target", so a sweep documented as
  target-agnostic ran 44 times a cadence, 43 of them pure waste; `target_id`
  never narrowed a query — the handler read it once, to suffix the finding title.
  Dropping the block is what actually made it target-agnostic. Measured: the
  analyst had been 5,496 runs and 61.9 of 73.6 analyst-hours per day, and the
  dominant Postgres load in the system; per-run cost fell from about 24 s to
  about 0.85 s once the per-candidate primary-key lookup and the per-candidate
  Qdrant round trip were replaced by one set-based gate query and one batched
  `query_batch_points`. The cost is that it leaves the reactive plane — no
  per-target trigger registration means no coalesced fire, so it runs on the
  15-minute cadence only. Sufficient by measurement (about 48k groups and 48k
  semantic candidates a day against about 4k signals/day of ingest), and the
  sweep is idempotent. [ARCHITECTURE §6.2]
- **2026-08-02 — the module-size regrowth gate.** `runtime/dapr_actors.py` had
  been decomposed in June 2026 — six modules, 2,367 lines extracted, 3,989 →
  2,641. Five weeks later it measured 3,735: it regrew 41% from the
  post-extraction floor, more than the extraction had removed. The extraction
  was correct, executed, and completely undone, because nothing in the tree
  noticed the file getting bigger again. `tests/test_module_size_gate.py` pins a
  ceiling on every module already at or past 1,500 lines, seeded at the measured
  count plus about 10% headroom, and fails on three conditions: a ceiling
  breach, a new entrant crossing the threshold unpinned, and a stale ceiling that
  stopped constraining its file (50% headroom, deliberately loose).
  [tests/test_module_size_gate.py; carried into DESIGN §2.3]
- **2026-08-03 — the correctness scorer was reading a dead table.** The per-unit
  correctness axis had been reading `unit_reference_labels`; it was repointed at
  the weekly gold-set verdicts in `correctness_labels`. [ARCHITECTURE §5.10]
- **2026-08-04 — the band-calibration population went empty, and stayed empty.**
  Band calibration resolves a claim at 14 days while the mean
  `judge_pipeline_version` stamp lifetime has been about 2.3 days, so a claim can
  never be both currently-stamped and resolved. Every stamp partitions, which is
  correct and which starves any readout needing resolved outcomes.
  [ARCHITECTURE §5.10]
- **2026-08-04 — the registry dependency outage.** `pycountry` was in
  `pyproject.toml` and in `Dockerfile.runtime` but not in the registry's explicit
  dependency list, so the registry process alone could not import
  `legba.data.filters.geocode`, which `legba.data.analysts.deterministic_handlers`
  pulls in via `entity_resolution` when the options catalog loads. Result:
  `GET /descriptors/analyst/*/typed` answered 500 for every options-bearing
  deterministic analyst and only for those — `claim_watch` and `signal_embedder`
  were silent for 14 hours while every health probe stayed green, and a
  descriptor PUT came back 422. Three env-degrade fixes (warn-only dead-options,
  the `_env_limited_dep` classifier, the catalog-unimportable skip) stop that
  *mode* recurring; the dependency line stops the specific gap existing. This is
  the origin of the leaf-module rule for registry-served routes.
  [docker/Dockerfile.registry; carried into DESIGN §3]
- **2026-08-10 — the one real GEPA compile.** A `dspy_gepa` candidate landed
  below the promotion bar: faithfulness delta −0.5354 against a +0.03 floor.
  [ARCHITECTURE §10]
- **2026-08-21 — RUST-4: the GEPA optimizer plane mothballed.** One real compile
  ever ran, its candidate failed the bar, and the manual VOICE-4 prompt wave
  shipped in its place. Code, tests and the workflow-worker image all stay — the
  worker still hosts the actively-used `deep_consult_workflow` — but both
  optimizer descriptors are annotated `state: paused` and
  `optimizer.py::run_method` refuses loud with `OptimizerMothballedError` rather
  than running. Seam #53 carries the evidence and the restore path.
  [ARCHITECTURE §10]
- **2026-08-27 — a composition is graded against the reads it cites.**
  `composition_integrity.py` added four deterministic arms over evidence the
  pass already held in `citations[].evidence_text` and had never read: scope
  laundering (a desk's "in this desk's collection" qualifier deleted at the
  composition layer), an attribution whose direction its own cited head
  contradicts (hard failure), an asserted desk-negative the desk never wrote, and
  a quote attributed to a desk that appears nowhere in its read. It carries its
  own collection-scope predicate — the shared lexicon minus the two time nouns,
  because a time bound answers *when* and only a collection bound answers *what
  was searched* — and is disjoint by construction from the unscoped-absence
  backstop, which skips any span carrying a citation marker. [ARCHITECTURE §5.10]
- **2026-08-27 — the register is bookkeeping, not evidence of currency.** A
  situation's intensity used to decay on the member clock — the `produced_at` of
  the desk reads clustered into the frame — which the pipeline's own cadence
  winds; a dozen desk reads a day at a three-day half-life sums to a permanently
  high number, and `last_event_at` was always today, so a frame could never leave
  `active`. Intensity was a measure of cron frequency, not world activity. It now
  decays against `trajectory.read_corroboration` — the newest significant ledger
  delta, which cannot be written without cited evidence and whose `occurred_at`
  *is* the evidence's — with the half-life keyed to evidence density, demoting to
  `dormant` past the desks' own 72-hour horizon. Demotion only: never promotes,
  never auto-closes. A resolution-grounded close now reaches `situations.status`
  rather than living only in the event ledger, and both register renders carry
  `last_corroborated_at`, `evidence_age` and an explicit stale /
  never-corroborated label. Verify counts `register_self_corroboration` as a soft
  failure: a claim whose citations are all register references asserting currency
  is the system citing itself. [ARCHITECTURE §5.10]
- **2026-08-27 — every composition declares the evidence window it covers.**
  `composition_window.evidence_window_span` is the true oldest and newest
  `produced_at` among the consumed heads, computed from rows already being read,
  handed to the model as a copy-only block and stamped onto the finding. It
  replaced a model-derived instant that drifted by hours. [ARCHITECTURE §5.10]
- **2026-08-27 — an analyst that does not read signals is not wired to the
  signal trigger.** The country and region compositions declare
  `subscription.targets.data_types == ["finding"]`, but
  `_wire_targets_and_triggers` was registering a per-target coalescing trigger
  for every analyst matched onto a target regardless of what it reads. With the
  accumulation floor at 2 and a long cooldown shared with the composition's own
  deliberately-late scheduled tick, two unrelated wire signals could wake a
  composition hours before that day's later units had run — and the reactive
  fire's cooldown stamp then suppressed the correctly-ordered scheduled tick
  outright. Measured: 30 of 31 country targets had desk heads landing after
  their own composition had frozen. `_analyst_ids_for_target` now excludes any
  per-target analyst whose `data_types` omits `"signal"` from trigger
  registration entirely. The ordering invariant this makes load-bearing — every
  composition's scheduled hour lands strictly after every one of its source
  units' fire hour, in both cycles — is pinned by a test against the shipped
  descriptors. [ARCHITECTURE §6.2]
- **2026-08-27 — H3: the scorecard's one-rung damper retired.** Confidence now
  decides admission only; the engine neither demotes nor promotes, and each row
  records the `damped_would_have_been` it would have shipped. H3 also forbids
  abstaining silently beside a composition that consumed a head for the same
  desk — the card bands what the prose used, or names why it cannot.
  [ARCHITECTURE §5.10]
- **2026-08-29 — the judge floor may only exclude on judged evidence.** The judge
  is sampled by a content-independent hash of the finding id, so whether a
  finding was judged has no relationship to whether it was any good — fine for
  measurement, not fine as an exclusion basis. Measured over 14 days, unsampled
  findings failed the composition's 0.50 floor at 24.2% against 3.2% for judged
  ones, the gap being almost entirely a failure class the deterministic scorer
  cannot recognise and the judge can. `judge_floor_escalation.py` now re-enters
  any unjudged finding about to be excluded through `verify_finding_faithfulness`
  at rate 1.0 first. The escalation lives at the verify boundary, not in the
  composition query, and every critique carries a `judge_trigger`
  (`sampled` / `floor_escalation`) plus its pre-escalation score, so an escalated
  verdict is never pooled with a sampled one. Kill switch:
  `LEGBA_JUDGE_FLOOR_ESCALATION` (unset = on). [ARCHITECTURE §5.10]
- **2026-08-29 — the exemption rungs became clause-scoped.** Three deterministic
  exemptions — synthesis prefixes, assessment scaffolding, absence phrasing —
  used to grant a whole-span pass off a prefix or a substring, so a sentence that
  opened with a scaffold and then named three specific facts escaped scoring
  entirely. Each rung now tests positionally against the clause that earns it,
  gated by whether the rest of the span asserts a specific fact. Strictly
  additive to the denominator (+6,772 spans, none removed); the judged arm
  replays byte-identical. [ARCHITECTURE §5.10]
- **2026-08-29 — `STAMP_EXPECTED_SHIFTS`.** A registry that lets a reader pool
  *consecutive* stamps for a metric family when the lineage's own prose
  affirmatively declares the family cannot move across that boundary, disclosing
  the exact pooled stamp set on the wire rather than widening silently. Applied
  to real lineage its yield is **zero** — which is the finding: the residual
  defect is stamp cadence, not the partition rule. [ARCHITECTURE §5.10]
- **2026-08-29 — the standing external audit.** `standing_auditor` is the first
  analyst pointed outward: it samples the world read plus a deterministically
  rotated subset of desk reads, extracts one or two checkable world-claims per
  head, checks each against live external search through the governed
  `web_access` pack, and records `SUPPORTED` / `CONTRADICTED` / `NOT_FOUND` /
  `UNCHECKED`. A contradicted verdict on a high or critical claim writes an
  `alert` row. Three design points: a heartbeat on every run including a run that
  audits nothing (a judge outage once went unnoticed for exactly the reason that
  "nothing to contradict" and "the auditor is dead" looked alike), both planes or
  neither (with no search binding it refuses to spend a core-plane call at all;
  missing database access raises, every other gap degrades with the reason named
  on the heartbeat, and the trace status stays `success` because a degraded run
  is a completed run), and its own `EXTERNAL_AUDIT_PIPELINE_VERSION` population
  key. It shipped as a draft descriptor. [ARCHITECTURE §5.10]
- **2026-08-29 — three ordered anti-noise mechanisms at the alert trigger
  tier**, with the invariant that a suppressed alert still writes its row.
  (1) The steady-state guard suppresses a `verified_finding` page only when the
  desk's banded severity is unchanged and the finding's own `severity_delta`
  reads steady or absent and the desk was paged within the cooldown (default
  24h); a rose/fell/new tag always pages, and it fails toward paging. (2) The
  daily page budget: at most 5 alerts actually page per UTC day fleet-wide,
  ranked worst-first, with a kind-diversity cap of 3 slots per trigger class —
  the cap exists because `situation_escalation` is 100% `severity=critical` and
  would otherwise win every slot every day, and a slot no other kind can fill
  stays unused rather than being backfilled. (3) The kill list:
  `contention_flip` and `geo_convergence` default off, their scans still run and
  their watermarks still advance as if fired, so re-enabling is a config flip
  rather than a backlog replay. [ARCHITECTURE §8.5]
- **2026-08-29 — a semantics-stamp mismatch is a migration, not a world event.**
  When banding semantics change, every card straddling the change moves, and both
  the band-crossing scan and the calibration tracker would read that fleet-wide
  move as deteriorations and resolvable claims — the platform paging the operator
  about its own upgrade. `classify_band_transition` now pre-empts every other
  classification when the prior and current cards' stamps differ, returning a
  low-severity `SEMANTICS_MIGRATION` regardless of direction; the scan folds
  every dimension a desk's mismatch touches into one informational alert per desk;
  and `band_calibration_tracker` logs the transition with
  `semantics_migration=true` (migration `0187`) while the aggregation filters
  `WHERE NOT semantics_migration` — an exclusion reported honestly as
  `population.excluded_semantics_migration`. A stamp absent on the prior side
  reads as differing; both cards missing the same stamp read as unchanged, so
  untouched history stays byte-identical. [ARCHITECTURE §8.5]
- **2026-08 — the arc through migration head `0192`.** Bearing edges and review
  flags, corpus tombstones, entity-graph backfills and merge repairs, the
  situation trajectory ledger `situation_events` at `0184`, the proposed-edge
  repoint at `0185`, `analyst_traces.prompt_sha256` at `0186`, the
  band-calibration `semantics_migration` flag at `0187`, the situation mega-frame
  split at `0188`, and the append-only `read_events` ledger at `0189`.
  [ARCHITECTURE §13]
- **2026-08 — the entity-identity / salience / journal-data wave**, migrations
  `0086`–`0090`. [ARCHITECTURE §13]

## 2026-09

- **2026-09-03 — `coverage_floor`, the ninth alert trigger class (#82).** Its
  subject is the gap between two of the engine's own artifacts: a foreign polity
  a desk's own salience-scored 14-day slice keeps naming that not one of its open
  situation frames names. It exists because a diagnosis proved a desk can be
  structurally silent about the war it is fighting when the register never opened
  a frame for it, and because migration `0188`'s mega-frame split retired that
  diagnosis's frame-*count* predictor (every country target now carries 7–8
  frames) without retiring the blindness. Fires `low`, interval-gated at 6h, and
  at its shipped defaults would flag 3 of 32 country targets, all with durable
  watermarks so a transition never re-fires. [ARCHITECTURE §8.5]
- **2026-09-04 — the composition demotion decision.** The composition-tier reads
  were externally graded twice at roughly 0.48 accuracy against a pre-committed
  0.75 bar, so the layer was ruled for demotion. [STATUS.md, the dated record
  behind the rule carried into the rewritten ARCHITECTURE §7]
- **2026-09-05 — the assembly regime flipped live** (`LEGBA_COMPOSITION_ASSEMBLY=1`).
  Composed prose stopped being free-text synthesis and became a quotation
  assembly — byte-identical desk spans under a deterministic title — with a
  fenced, labelled Assessment generated from the assembly's own spine
  (`derived_from == [spine_id]`, AST-guarded) carrying whatever interpretive voice
  survives, separately from the record. The first live assembled cycle proved the
  construction honest (quote fidelity 1.0, zero real quote or scope-truncation
  failures across the audited rows) and exposed a verify-plane grading defect:
  two legacy verify branches grade quote-stitched prose on the grain built for
  free-text and floor byte-correct assembly rows below the composition verify
  floor; one arm false-positives on collection-scoped absence spans; and the
  Assessment's fidelity-to-spine score reads 0.00 because the live path does not
  yet mint the evidence map the fidelity check needs. Disposed as fix-forward;
  the pre-committed rollback is one flag flip. [STATUS.md; the reason carried
  into the rewritten ARCHITECTURE §7]
- **2026-09-05 — the research / width / attention wave**, migrations `0190`–`0192`:
  the append-only external-grading ledger `external_grades` (`0190`), the
  `unit_reference_labels` reference columns — window, items, status, provenance
  (`0191`), and `source_credibility.license_class` (`0192`). All three additive
  and applied live; the code paths that read them ship behind default-off flags.
  [ARCHITECTURE §13]
- **The `claim_watch` bearing-gate rounds.** Match precision moved from 0.279
  pooled at first measurement to 0.908 on the live bearing-gated stream at round
  4, over the 0.85 bar — so building the closer became a held operator decision
  rather than an unmet measurement. [ARCHITECTURE §14]
- **Seam #50 closed — the search control-query canary.** The liveness probe fires
  reactively on an empty search; the scheduled half runs too, as a host cron line
  on a 15-minute clock that pages only after two consecutive not-live probes.
  [ARCHITECTURE §14]

## Measured live states recorded at rewrite time

These were stated as current in the design tier. They are counts and live
readings, which drift; `RELEASE_STATE.md` is the generated source for the
current values.

- Roughly 4.6k facts (about 3.7k ingestion-sourced), 4.9k nexuses (about 3.2k
  signed), 940 hypotheses (253 confirmed / 287 refuted / about 400 active).
  [ARCHITECTURE §1, §13]
- The OpenSearch corpus: 182.6k docs of which 106.8k were live and 75.9k were
  orphaned by purges that had no delete path. [ARCHITECTURE §0.1]
- Qdrant: `world_context` about 293 chunks, `tradecraft` about 1,716 chunks.
  [ARCHITECTURE §0.1]
- Redis: about 84 keys live. [ARCHITECTURE §0.1]
- Seed batch `414473c8`: about 19 seed facts and 17 seed signed nexuses.
  [ARCHITECTURE §5.7]
- About 57 head source descriptors; the 46-entry source catalog (43 `rss` +
  3 `geojson`). [ARCHITECTURE §0, §6.1]
- The retired producers' residue: about 1.2k `country_assessor` findings and
  about 539 `prediction` rows remain in the database, unread.
  [ARCHITECTURE §1, §5.3]
- The `unit_optimizer` live delta: parent 0.34 → candidate 0.29, delta −0.05.
  [ARCHITECTURE §10; DESIGN §3.6]
- The correctness gold set: a first labelled cohort of n=8; the deterministic
  reference leg still n=1, reported insufficient-sample. [DESIGN §3.6]
- Wave-2b contention tie-break: proven consulted live (called on a near-tie and
  correctly abstaining on symmetric evidence), but a successful LLM pick
  (`llm_tiebreaks >= 1`) was unobserved live. [ARCHITECTURE §5.9]
- The bare-QID canary: US head of state resolves to Donald Trump since
  2025-01-20, superseding the QID. `Q22686` carries no English `labels.en` key at
  all, so the enwiki-sitelink fallback is what resolves it.
  [ARCHITECTURE §5.8]
- Grounding RAG staggering: `vector:world_context` flipped on for
  `leadership_transition` and `internal_stability` only, other units on the
  structured `substrate` source pending review-gated expansion.
  [ARCHITECTURE §5.8; DESIGN §3.4; DIRECTION §10]
- The MCP built-in tool set: seven tools (`substrate_findings`,
  `substrate_situations`, `substrate_signals`, `lineage_walk`, `since`, `export`,
  `consult`), MCP protocol 2025-11-25, stdio transport only; a standalone
  process needs the registry reachable at `LEGBA_REGISTRY_URL`, and the
  descriptor-declared catalog still lists empty in a standalone process.
  [DIRECTION §4]
- Deprecated read routes kept serving their original shape under
  `Deprecation` / `Sunset` / `Link: rel=successor-version` headers:
  `GET /v3/sources/{id}/assurance` and `GET /api/v1/source_credibility`,
  superseded by `/v3/source-quality` (migration `0115`). A 3xx would hand callers
  a different body. [ARCHITECTURE §8.7]
- `retrieval_origin` (migration `0112`) marks a row that came in through a named
  external search provider; `NULL` — every pre-existing row, with no backfill —
  means a curated registered source. It is a code convention, not a `CHECK`, with
  one resolver serving both the archive gate and the corpus facet so the two
  cannot drift. Web-retrieved evidence resolving a calibration outcome is stamped
  `web_evidence` and lands in the weak calibration tier, structurally excluded
  from the exogenous set the headline Brier is computed over, because an
  exogenous resolution the system went and found for itself is a materially
  weaker claim — pooling them would let the system improve its own headline score
  by searching harder. [ARCHITECTURE §8.8]
- The evidence archiver fails closed on web-origin rows: it will not fetch and
  store the bytes of a web-origin row whose licence class is unset or unknown,
  recording a `skipped_license_unreviewed` sidecar row instead. Curated sources
  keep the older posture, because a registered source has been through an
  operator and an arbitrary open-web domain has not. [ARCHITECTURE §8.8]

---

# Appendix — the deferred-item designs extracted from DIRECTION

The rewritten `DIRECTION.md` compresses each standing deferral to one line
(shape, and what it waits on). These are the full designs it carried, kept
whole. They are forward design rather than history, so they are held here
pending a decision on whether they belong in a design appendix instead.

## Scoped tokens, then single sign-on

**The problem.** The registry API has exactly one credential: a single shared
bearer checked by `require_bearer` (`data/registry/api.py`). One token equals
full access — descriptor CRUD, vault credential writes, substrate reads, consult
invocation. There is no read-only token to hand a dashboard, no operator/admin
split, and no identity: the audit layer records whatever principal string
`require_bearer` returns, with `actor_role` hard-coded to `"operator"`. Perimeter
auth is the edge proxy's `basic_auth` with bearer injection (`docker/Caddyfile`,
the `basic_auth_perimeter` snippet; `header_up` swaps the browser's Basic for
the registry bearer) — one password, one identity, no session lifecycle.

**Three steps, strictly in this order.**

1. **Scoped tokens at `require_bearer`** — `read` / `operator` / `admin`.
   `require_bearer` grows into a `Principal` resolver (token →
   `{principal, scope, tenant}`); routers declare their floor through a
   dependency factory (`require_scope("operator")`) wrapping the existing
   `Depends(require_bearer)` sites — `consult_api.py::invoke_consult`,
   `substrate_reads_api.py`, `lineage_api.py`, the per-analyst runtime-eval route
   (a natural `read` floor), the descriptor and vault routers. Token records live
   in Postgres, hashed, rotatable per scope; the resolved scope is stamped into
   `AuditEntry.actor_role` instead of the constant. Dev mode (env unset →
   accept-all, logged once at WARN) survives unchanged.
2. **OIDC at the edge (`forward_auth`) before any in-app single sign-on.** The
   edge already owns the perimeter and already rewrites `Authorization`
   upstream; `forward_auth` against an OIDC provider replaces `basic_auth` in the
   same snippet, and the validated identity maps to a scoped token through the
   same `header_up` channel. The application keeps verifying exactly one thing —
   a bearer with a scope — and never grows cookie, session or redirect
   machinery. In-app single sign-on is rejected until a concrete requirement
   (per-user API keys issued from the console) forces it.
3. **Tenancy claim mapping.** The token record, and later the OIDC claim, carries
   `owner_tenant`; `require_bearer` resolves it into the `Principal`, and handlers
   thread `principal.tenant` into every substrate query. Depends on the tenancy
   item — there is nothing to enforce against until `owner_tenant` exists on the
   analysis-plane tables.

**Integration points.** `require_bearer` / `_current_token`
(`data/registry/api.py`); `AuditEntry` (`data/registry/audit.py`);
`docker/Caddyfile` (`basic_auth_perimeter`, the `handle /api/*` blocks); every
`Depends(require_bearer)` site under `data/registry/`.

## Analysis-plane tenancy enforcement

**The problem.** `owner_tenant` is threaded through the acquisition plane
end-to-end and stops dead at the analysis plane. Built: the signal contract
carries it (`data/sources/_contract.py`); `SourceActor` pins it on the row, the
NATS subject token and the binding, with the descriptor's `scope.owner_tenant`
winning over anything the handler set (`runtime/source_actor.py`); it is indexed
on `signals` (`signals_owner_tenant_idx`, `0024_pivot_substrate.sql`); the
subscription plane pins it in pushed-down SQL and re-checks it in the residual
matcher (`runtime/subscription/filter.py` — both the `owner_tenant = $n` clause
and the row-level guard); the publish subject embeds it (`signal_subject` in
`data/nats.py`); the trigger plane persists it (`trigger_state.tenant`,
`TriggerStateStore.save_dirty`).

Not built: `analyst_outputs` (`0012_analyst_outputs.sql`) has no `owner_tenant`
column, so findings, alerts and critiques are tenant-blind the moment they are
written. The substrate read APIs (`substrate_reads_api.py::list_findings` /
`list_situations` / `list_signals`) and the lineage walk
(`lineage_api.py::walk_lineage`) filter nothing by tenant. Descriptor
projections expose tenancy for sources only (`SourceDescriptorOut.owner_tenant`);
target and analyst projections have no tenancy column at all.

**The approach — prerequisite first, then enforcement.**

1. Additive migration: `analyst_outputs.owner_tenant TEXT NOT NULL DEFAULT
   'default'` plus an index, mirroring the `signals` column exactly.
2. Stamp at write time: the provenance write path
   (`data/provenance/writes.py`, the kind→table routing) takes the tenant from
   the analyst's target-descriptor scope, carried through the trigger fire →
   actor `run()` options the same way `target_id` already is.
3. Surface `owner_tenant` on the target and analyst descriptor projections so the
   console and the registry list endpoints can scope by it.
4. Then per-request enforcement: the `Principal.tenant` becomes a mandatory
   `WHERE owner_tenant = $tenant` on every substrate read and on each hop of the
   lineage walk. Deny-by-default; single-tenant installs ride the `'default'`
   tenant and notice nothing.

Ordering is the point: enforcing tenancy against tables that do not carry the
column would be theatre.

**Integration points.** `0012_analyst_outputs.sql` (the missing column);
`data/provenance/writes.py` (the stamping site); `substrate_reads_api.py` /
`lineage_api.py` (the enforcement sites); `SourceDescriptorOut.from_row`
(`data/registry/api.py`) as the projection pattern to replicate.

## Serving interop — TAXII serve and MISP sync

**The problem.** A platform whose findings cannot land in the rest of the
threat-intel world is a silo. The wire format is STIX 2.1 over TAXII 2.1.

**Built.** The bundle producer (`data/outputs/stix_bundle.py`, see L-120 above).
Pushing to an upstream TAXII 2.1 server is also built:
`data/outputs/taxii_client.py:push_bundle_to_taxii` POSTs the bundle's objects
as a TAXII envelope to
`{server_url}/{api_root}/collections/{collection_id}/objects/` over the
structural HTTP port, and `stix_bundle.emit` invokes it behind the descriptor
`outputs.stix_bundle.config.taxii` binding, best-effort and degrade-not-drop. An
un-provisioned destination raises `TaxiiServerNotConfiguredError` — a loud guard,
not a stub (seam 10).

**Not built.** Serving TAXII (a collections endpoint a peer can poll), MISP
sync, and signal→`observed-data` mapping. No live TAXII server is provisioned,
so no in-tree descriptor carries a real `taxii` binding.

**Honest note on current bindings.** The two descriptors that declared the
`outputs.stix_bundle` binding — `analyst_country_assessor` and
`analyst_country_predictor` — were both taken out of the live path by the
sequencing above. The producer and the emit dispatch are built and exercised,
but no active analyst emits a bundle; re-binding the export to a live output (the
units, the compositions, the banded scorecard) is the near step, not new
plumbing.

**The approach.**

1. TAXII upload — done. Direct HTTPS POST of a TAXII 2.1 envelope through the
   reused HTTP port; no TAXII client dependency added to the runtime image.
   Activates only when a descriptor `taxii` binding carries an
   operator-confirmed `server_url` plus optional credentials.
2. TAXII serve as a thin read-only router over stored bundles (per-target
   collections, the naming convention above), mounted on the registry behind a
   `read`-scoped token. No new service.
3. MISP sync as a push adapter: `report` → Event, `indicator` → Attribute, cited
   `identity` / `location` → MISP objects — driven off the same output-envelope
   stream the exporter consumes, so MISP and TAXII stay consistent by
   construction.
4. Signals stay out by default. Mapping the raw signal pool to `observed-data`
   would export the firehose; findings are the product. Per-descriptor opt-in
   only.

**Integration points.** `export_outputs_to_stix`, `StixBundleExporter`, `emit`,
`upload_bundle_to_taxii` (`data/outputs/stix_bundle.py`); `push_bundle_to_taxii`,
`TaxiiConfig`, `TaxiiServerNotConfiguredError` (`data/outputs/taxii_client.py`);
`discover_output_kinds` (`data/outputs/__init__.py`); `_emit_output_bindings`
(`runtime/dapr_actors.py`, threading the shared `output_http_client`); the
payload models in `data/provenance/kinds.py`.

**Since shipped.** The nearer product direction — a first-class markdown/JSON
report export (`POST /v3/export`, with live-resolved citations, verify states,
evidence hashes and receipt links) — is built. The STIX machinery is kept but
stays out of the daily flow until an emitter is re-bound.

## The MCP surface

**Built.** `ui/mcp_server.py` is a working stdio MCP server (entry point
`legba-mcp`, MCP protocol 2025-11-25). Seven built-in tools —
`substrate_findings`, `substrate_situations`, `substrate_signals`,
`lineage_walk`, `since`, `export`, `consult` — are code-constructed HTTP
wrappers over the registry, so a standalone process serves them regardless of
runtime population. Descriptor-declared tools remain the second catalog source,
merged and deduped by tool name with the built-in winning a collision. Reads and
the sanctioned consult run only; no registry mutation rides MCP
(`mcp_builtin_tools.assert_reads_and_consult_only`).

**Residual caveats.** Transport is stdio only (no HTTP/SSE). The built-in tools
are a thin HTTP client, so a standalone process needs the registry reachable at
`LEGBA_REGISTRY_URL`. The descriptor-declared catalog still lists empty in a
standalone process — only the built-ins survive without a shared-runtime
process.

**Setup, as DIRECTION carried it** (this belongs in the operate/setup tier, not
in a direction page). Env: `LEGBA_REGISTRY_URL` (the origin is taken from it;
the `/api/v1/registry` suffix is stripped since the tools address `/api/v1/...`
paths directly) and `LEGBA_REGISTRY_TOKEN` / `LEGBA_REGISTRY_API_TOKEN` for the
bearer. A `read`-scoped token serves the six read tools; `consult` needs
`operator` and blocks up to 300 s for the ReAct loop, threading that timeout to
the registry. Build the image with `docker compose --profile mcp build`, then
point the client at it per conversation with
`docker run -i --rm --network=legba_default --env-file=<repo>/.env legba/legba-mcp:latest`
— the network flag is required so the container can reach the registry host named
in `LEGBA_REGISTRY_URL`. Host-mode alternative:
`PYTHONPATH=src python -m legba.ui.mcp_server`, or the `legba-mcp` entry point
after an editable install. Logging goes to stderr so stdout stays clean for the
protocol. Every tool fails loud: a registry 4xx/5xx returns a described error
object (status plus detail), a transport failure returns `registry_unreachable`,
and an unknown tool call returns the available-tool list.

An analyst descriptor surfaces an additional tool by declaring an
`outputs.mcp_tool` binding (`tool_name`, `description`, `input_schema`, and a
`mode` — `latest_output` returns the analyst's most recent output for the bound
scope; `consult_on_demand` triggers an on-demand run with the call's args). On
activation the runtime calls `MCPToolRegistry.register_from_descriptor`; on
retire it unregisters.

**Integration points.** `create_server` (`ui/mcp_server.py`); the built-in tool
set (`ui/mcp_builtin_tools.py`); `MCPToolRegistry.register_from_descriptor` /
`.handle` (`data/outputs/mcp_tool.py`); the HTTP routers plus `since_api.py` and
`export_api.py`; `RegistryHTTPClient.request_json`
(`runtime/registry_client.py`) as the reused wire.

## Multimodal for real

**The problem.** The schema and plumbing are modality-first —
`Signal.modality` / `mime_type` / `media_ref` / `retention_class` / `object_ref`,
a modality token in every NATS subject, and a real async job plane — but the
extraction edges are not real models. The loop side is closed: a landed derived
signal is re-published into fan-out (`event_class="derived"`) with parent geo,
tags, entity classes and language inherited.

**The approach.**

1. **Actual extractor endpoints, transcription first.** The hosted models service
   grows `/transcribe`; `MediaClient.extract` already POSTs `media_ref` to the
   transcribe path and records the endpoint-reported model, so a real endpoint is
   a config change (`LEGBA_MEDIA_API_URL`) with zero plumbing. Then OCR, then
   captioning, through the same client.
2. **Object store.** The one the schema already names: a retain-tier handler that
   fetches bytes when `retention_class != reference_only`, sets `object_ref` to
   our copy, and honours `media_ref_expires_at` with a sweep that exempts
   `evidence_hold` and anything referenced through `derived_from`.
3. **Non-text renderers.** Console-side rendering of `media_ref` / `object_ref`
   per modality — audio player plus transcript signal, image plus caption, video
   keyframes. Listed here because "multimodal" is not done until an operator can
   see the media.

**Integration points.** `MediaClient.extract` / `from_env` / `has_endpoint`
(`runtime/jobs/media_client.py`); the `MediaExtractor` protocol and
`default_extractor_registry` (`data/sources/baseline.py`);
`process_media_handler` (`runtime/jobs/process_media.py`); the retention fields
on `Signal` (`data/sources/_contract.py`).

**The modality registry, as DESIGN carried it.** One key, two handler halves,
resolved most-specific-first (`mime_type` → `modality` → `binary` fallback).
Ingest: `text` passthrough is live; `structured` (GeoJSON) is live and
model-free — a whole-document structured feed is a *source kind*
(`data/sources/geojson.py`), emitting `modality="structured"` +
`mime_type="application/geo+json"` signals with geo promoted from feature
properties. Per-attachment extraction extends the `MediaExtractor` protocol and
emits a **derived** signal stamped `derived_from` the source signal.
Renderers: `modality → renderer` in `legba-ui-v3/src/lib/modalityRenderers.tsx`,
consumed by the lineage `ModalityRef`; only `text` is a real renderer, and every
other modality — including `structured` — is a badged placeholder plus a link
(`implemented:false`), so even the live GeoJSON ingest lands as a structured-badge
placeholder. Adding a type is one ingest handler plus one renderer keyed by
modality/mime — no schema or plumbing change, because the signal columns already
carry everything the registry needs. `tests/data_pkg/test_modality_seam.py`
proves a non-text signal routes, enriches with a graceful skip, and dispatches
without breaking.

## Scale-out

**The three single-node truths.**

1. **NATS is single-replica.** `NatsStore.ensure_stream` (`data/nats.py`) builds
   `StreamConfig` without `num_replicas` — every stream (`legba_signals`,
   interest retention, created by `subscription/engine.py::ensure_topology`; the
   workqueue-retention job stream from `runtime/jobs/queue.py::ensure_topology`)
   lives on one JetStream node. Node loss is stream loss; signals are also in
   Postgres, in-flight job messages are not.
2. **`signals` is a plain unpartitioned table** with thirteen indexes, plus the
   scheduled TTL purge (decision D4 above) that ships disabled.
3. **The coalescer assumes one replica per pair.** Fire-claiming is already
   multi-worker-safe — `claim_fire` is an optimistic compare-and-swap
   (`UPDATE … WHERE last_fired_at IS NOT DISTINCT FROM <expected>`,
   `runtime/triggers/state.py`) — but dirty accumulation is a read-modify-write:
   `Coalescer.on_signal` does get → `apply_dirty` → `save_dirty`, a
   last-writer-wins upsert. Two engine replicas processing signals for the same
   (analyst, target) can interleave and lose a pending-count increment. That
   costs a *late* fire — the cadence ticker still sweeps dirty pairs — not a lost
   signal, but it is a correctness ceiling on replication.

**The approach.**

1. **Three-node JetStream**, with `num_replicas` plumbed through `ensure_stream`
   (env-tunable, one for the dev rig, three in production topology).
   Interest and workqueue retention semantics are unchanged by replication.
2. **Partition `signals` by `fetched_at`** (native monthly range partitions),
   which matches the existing index access pattern. Retention becomes
   detach-and-archive, with a referenced-rows exemption: anything reachable from
   a `derived_from` array or under `evidence_hold` is copied forward before
   detach. Plain native range partitions — no extension dependency — keep the
   migration chain boring.
3. **Multi-replica coalescer**, two acceptable designs decided by load shape:
   (a) partition the pairs — deterministic subject-hash assignment of
   (analyst, target) to engine replicas through filtered durable consumers,
   keeping the read-modify-write single-writer per pair; or (b) make `save_dirty`
   atomic — move the merge into SQL (an arithmetic upsert plus a SQL-side
   seen-set union) so interleaving cannot lose counts. (a) is the default
   choice: it preserves the in-memory accumulator semantics and the seen-cap
   eviction order without a JSONB merge in SQL. The compare-and-swap already
   holds for N replicas either way.

**Integration points.** `ensure_stream` (`data/nats.py`); `ensure_topology`
(`runtime/subscription/engine.py`, `runtime/jobs/queue.py`); the `signals` DDL
(`0024_pivot_substrate.sql`); `TriggerStateStore.save_dirty` / `claim_fire` and
`Coalescer.on_signal` (`runtime/triggers/`).

## Cheap-model budget demotion

**The approach — wire `method.llm.fallback` end-to-end, symmetric with primary.**

1. Descriptor: `method.llm.fallback: <stack_ref>`. The `llm` dict is open and the
   key is already named by the `BudgetRetryPolicy` docstring in
   `data/schemas/analyst.py`.
2. Deps builder: resolve the fallback reference through the same stack-registry
   path as primary, construct a second LLM client and kind-deps bundle, and
   populate `fallback_run_method` / `fallback_kind_deps` / `fallback_llm_ref` on
   `_AnalystDeps`. Kinds whose deps embed the LLM get the same builder invoked
   twice with a different stack ref — no new kind code.
3. Accounting: demoted runs still meter into the budget ledger at the fallback
   model's per-token rate (`0015_cost_model.sql`) — demotion reduces spend, it
   does not stop metering. Demotion already clears on bucket rollover or an
   explicit clear.
4. Validation: a descriptor declaring `strategy: demote_and_continue` without a
   resolvable `method.llm.fallback` should fail activation loudly rather than
   silently behaving as pause. Today's silent equivalence is acceptable only
   while nothing can wire a fallback at all.

**Integration points.** `BudgetRetryPolicy` (`data/schemas/analyst.py`); the
strategy dispatch and demotion state in `runtime/dapr_actors.py`;
`BudgetEnforcer.precall_check` / `record_demotion` (`runtime/budget.py`); the
primary-LLM resolution to mirror in `runtime/analyst_deps_builder.py`.

## Deep-crawl discovery jobs

**The shipped path is the registry route.** A discovery descriptor runs inside
`TargetActor._run_discovery_cycle`, resolves its kind through the
discovery-kind registry (`data/discovery/registry.py`), drains `discover(ctx)`
into candidates, and hands them to the materializers —
`reconcile_discovered_targets` (`data/registry/discovered_materializer.py`) for
targets, and the source-side twin with validate-before-register
(`data/discovery/source_materializer.py`) for sources. The shipped kinds are
list- and query-driven: `country_list_discovery`, `file_sd_discovery`,
`query_source_discovery`.

**The approach — deep crawl returns as a job handler, not a new descriptor
route.**

1. Register `crawl_discovery` in `JobDispatch`. Envelope `input_refs`:
   `{seed, depth, max_pages, allow_patterns, max_sources}` plus the governor's
   crawl caps — the generic `JobEnvelope` already carries `budget_account`,
   `tenant_id` and an idempotency key, so a re-requested crawl of the same seed
   collapses.
2. The handler walks the frontier using the existing acquisition handlers rather
   than a new crawler — fetch, extract candidate feed and source URLs, score.
3. Output converges with the registry route: the handler emits `CandidateSource`
   rows into the same validate-before-register materializer the
   descriptor-driven kinds use. One registration gate regardless of how a
   candidate was found; discovered descriptors carry the `job_id` as provenance.
4. Only after the handler exists does any enqueue return — agency tool, operator
   endpoint, or a crawl-flavoured discovery kind that *defers* its heavy walk to
   the job plane, because a long-cycle crawl does not belong inside an actor's
   run slot.

**Integration points.** `JobEnvelope` / `KNOWN_JOB_KINDS`
(`data/jobs/envelope.py`); `JobDispatch.register` / `default_dispatch`
(`runtime/jobs/dispatch.py`); the worker pool (`runtime/jobs/worker.py`);
`CandidateSource` and the validate-before-register flow
(`data/discovery/source_materializer.py`, `source_validate.py`).

## Knowledge grounding — the three tiers, as DIRECTION and DESIGN carried them

**The problem.** The core plane's model carries a training cutoff that predates
the present, so on any assessment turning on current world state — who holds
office, which alliances are in force, the state of an ongoing conflict — it
backfills from a stale prior. The live failure that forced the design: the
assessor called the sitting US president a "former" president, and the signal
slice (recent headlines) rarely restates a background fact like "X is the head
of state", so the model had no in-context correction.

**The insight.** The substrate already stores the temporally-honest answer.
Temporal `facts` with `valid_from` / `valid_until` / `superseded_by`, reified
signed `nexuses`, and the seed roots are exactly a current-world-state model with
a single queryable open row per assertion. The fix is not a new store, a
fine-tune or a RAG index: curate the current data *in*, then inject it at
analysis time. This keeps the model swappable (grounding corrects whatever cutoff
the bound model has) and keeps the ground truth auditable (it is curated or seed
`facts` rows with full provenance, not prompt text).

- **Tier 0 — curate in.** The `wikidata_leaders` seed adapter pulls current heads
  of state and government from live Wikidata SPARQL and emits a
  **country-subject** office fact, split by office type —
  `'<country>' | 'head of state' | '<leader>'` (P35) distinct from
  `'<country>' | 'head of government' | '<leader>'` (P6) — keyed on the country,
  so a leader change *supersedes* the prior officeholder. Keying on the person
  could not: `supersede_prior_facts` keys on `(subject, predicate)`, and a
  subject=leader fact's subject is the person, so two open "current" rows would
  result. The curated `world_baseline` adapter emits the same shape, so a fresh
  Wikidata pull supersedes a stale curated leader for the same country. A bare
  QID is resolved through a batched `wbgetentities` label lookup preferring
  `labels.en.value` and falling back to the enwiki sitelink title; an
  unresolvable QID is dropped rather than emitted as an unreadable `Qxxxx`. The
  flaky alliances query soft-degrades rather than aborting the seed.
- **Tier 1 — inject at analysis time.** An opt-in `GroundingBlock`
  (`data/schemas/analyst.py` — `{enabled, scope, sources, max_facts}`, off by
  default) makes the deps builder install a per-run hook
  (`analyst_deps_builder._build_grounding_hook` → `SubstrateGroundingResolver`,
  `runtime/grounding.py`). The hook extracts candidate names from the in-memory
  slice and the run's target, resolves the current authoritative rows under the
  same temporal-honesty gate the analysis plane uses
  (`superseded_by IS NULL AND (valid_until IS NULL OR valid_until > now())`),
  prefers `seed` / `curated` provenance so a seeded ground truth outranks a
  hallucinated live fact, excludes bare-QID values in SQL and again in Python,
  and renders a dated "authoritative current context (treat as ground truth over
  prior knowledge)" preamble. The `inline_target` runner's GROUND phase prepends
  it. Degrade-not-drop: any resolver or read failure logs and yields nothing, and
  an empty candidate set produces no header.
- **Tier 2 — the vector `world_context` collection.** A curated
  unstructured-brief collection for free-text background the structured facts
  cannot carry, retrieved through the stack embedder port as a separate,
  non-citable preamble — opportunistic, relevance-floored, country-filtered,
  degrade-not-drop when the corpus is empty. Staggered on rather than enabled
  fleet-wide, pending review-gated expansion.

**Integration points.** `GroundingBlock` (`data/schemas/analyst.py`);
`SubstrateGroundingResolver` / `build_grounding_preamble` /
`collect_grounding_candidates` (`runtime/grounding.py`); the GROUND phase
(`data/analysts/inline_target.py`); `_build_grounding_hook`
(`runtime/analyst_deps_builder.py`); the seed adapters
(`data/seed/adapters/wikidata_leaders.py`, `world_baseline.py`); the RAG loader
(`data/rag/`); the unit descriptors carrying the `grounding:` block.

The compositions do not re-ground: they synthesise over already-grounded,
already-verified unit reads.

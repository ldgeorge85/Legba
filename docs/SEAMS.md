<!-- SPDX-FileCopyrightText: 2026 Lewis George
     SPDX-License-Identifier: AGPL-3.0-or-later -->
# Seams — the declared-seam registry

This is the single registry of intentionally not-built things in Legba. The
rule it enforces:

> A stub or mock in a production path (`src/**`) is forbidden. Anything not
> built is declared here and fails loud, or refuses activation, at its guard
> rail. It never fabricates output. A stubbed feature is not done.

`tests/test_no_undeclared_stubs.py` scans `src/legba/**` for stub markers —
`NotImplementedError` raises carrying a message, symbols named
stub/fake/mock/placeholder/echo, mock imports, a TODO'd empty return — and
fails unless every hit is either the abstract-method idiom or listed in the
[machine-readable allowlist](#machine-readable-allowlist). There is no
per-line escape: registering the seam here, with a why and a guard rail, is
the only way through the gate.

Adding an entry means two edits in this file: a row in the registry below, and
a `src/legba/<path>.py:<dotted.symbol>` line in the allowlist block. Removing
the code removes both — the test also fails on an allowlist line whose symbol
is no longer defined.

Seam numbers are stable and are cited from other docs as `SEAMS #N`. A number
is never reused; a resolved seam keeps its number and moves to the
[resolved appendix](#resolved-seams).

**The next free number is 63.**

---

## Declared seams

| # | Seam | What is not built | Guard rail |
|---|---|---|---|
| 1 | Eager media extraction | The media model. The job plumbing is whole: `process_media` lands the derived signal, republishes it into fan-out and inherits the parent's geo, tags, entity classes and language. No Whisper/VLM/OCR backend is provisioned, so there is nothing to extract with. | Refuse-loud at five points: `runtime/jobs/media_client.py:MediaClient` (not configured, or unreachable), `runtime/jobs/process_media.py:process_media_handler` (terminal failure rather than a derived signal that never re-enters fan-out), `runtime/source_actor.py:SourceCore` (an `media: "eager"` descriptor refuses activation without real extractors), `data/sources/baseline.py:_enrich_media_eager`, `data/analysts/agency/tools.py:process_media_tool`. `legba-media/app/main.py` mirrors it server-side with a 503 per extraction kind. A stub result is structurally unrepresentable — `MediaExtractionResult.source` is `Literal["hosted"]`. |
| 2 | `stream` acquisition kind | A third acquisition mode beyond `poll` and `push`, for long-lived streaming feeds. No source in the catalog needs one. | `data/schemas/source.py:SourceDescriptor` — `acquisition` is a `Literal["poll", "push"]`, so a descriptor declaring `stream` is refused at schema validation and cannot activate half-built. |
| 3 | Deep-crawl discovery jobs | — | Resolved by removal; see the [resolved appendix](#resolved-seams). |
| 4 | Fallback-model budget demotion | `method.retry.budget.strategy = "demote_and_continue"` proceeding onto a cheaper model. The dispatch, demotion state and audit rows exist; no fallback model is provisioned. | `runtime/dapr_actors.py:_AnalystDeps` — `fallback_run_method` is `None` in production, and the strategy is handled as an explicit audited pause: a `budget_demotion_events` row, a warning naming this seam, a cooldown to the bucket end, and a `BUDGET_THROTTLED` return. No cheaper-model output is fabricated. |
| 5 | Object store (media retention) | A blob store for retained media copies. Retention is reference-only today and no descriptor asks for one. | `data/discovery/deps_resolver.py:resolve_discovery_deps` — declaring `deps.object_store: true` with no injected client raises at activation. The declared shape is `data/schemas/source.py:SourceDeps`. |
| 6 | Proxy usage ledger persistence | The `proxy_usage_ledger` table and its write path for residential-proxy bandwidth attribution. It lands when a budget analyst reads from it. | `data/stack/proxy/bright_data.py:ProxyPoolHandler.report_usage` raises `UsageLedgerUnavailable` with no store wired, no table, or a failed insert. It never returns an unpersisted record as success. |
| 7 | `country_list_discovery` remote list sources | `list_source: "url:…"` and `list_source: "substrate:…"`. Only the cached ISO snapshot and `inline:<json>` are built. | `data/discovery/country_list_discovery.py:CountryListDiscovery._resolve_rows` raises with an actionable message at discovery time for both prefixes. |
| 8 | Common Crawl `S3Client` protocol surface | Nothing. This is the abstract-protocol idiom, registered because an audit named it: the production path builds a real unsigned aiobotocore client. | `data/sources/common_crawl.py:S3Client` — calling an unimplemented protocol method raises immediately. |
| 9 | Provenance INSERT routing | Nothing. A forward-compat guard so a new output table cannot be silently dropped before its INSERT routing lands. | `data/provenance/writes.py:_insert_for_spec` — the `else` branch raises with the offending table and the fix. |
| 10 | TAXII 2.1 destination | An operator-confirmed TAXII server to push STIX bundles to. The client and the wiring are real; `stix_bundle.emit` invokes it behind the descriptor binding flag, degrade-not-drop. No descriptor carries a real `server_url`, and the two descriptors that declared a bundle binding are retired, so the leg is dormant end to end. | `data/outputs/taxii_client.py:push_bundle_to_taxii` / `upload_bundle_to_taxii` raise `TaxiiServerNotConfiguredError` (a `RuntimeError`, not a stub) with no server, no client, or a cleartext non-loopback host. No allowlist line: the code fails loud, it does not stub output. |
| 11 | Consult `vector_search` embedder wiring | — | Resolved; see the appendix. The related grounding pilot is #20 and stays open. |
| 12 | Optimizer parent-prompt shape degradation | Nothing. When a prompt module imports but has an unexpected shape, `_load_parent_prompt_text` returns a clearly-marked marker string so the loop computes a delta instead of crashing; the marker is unmistakable in any audit. A module that cannot be *imported* is a dead reference, not a shape surprise, and now raises `PromptModuleImportError` — that half of the seam is retired. | `runtime/dapr_workflow/gepa.py:_load_parent_prompt_text`. |
| 13 | Non-text modality renderers | Renderers for image, audio, video and structured signal modalities. Only `text` is `implemented: true` in `MODALITY_RENDERERS`; the rest fall through to the generic payload view. | UI-side, no backend symbol — the stub scanner does not cover `legba-ui-v3/`. An unknown modality renders the payload, never a fabricated view. |
| 14 | RBAC, multi-tenancy, the wider STIX story | Role-based access control, real multi-tenant isolation (`tenant_id` is stamped through envelopes and ledgers, but there is one operating tenant), and the STIX/TAXII direction beyond #10. | The registry API requires bearer auth (`require_bearer`); the MCP and STIX export surfaces that do exist are real. Direction is [DIRECTION.md](DIRECTION.md). |
| 15 | A2A skill surface | Nothing — the router is built and mounted, but only with `LEGBA_A2A_ENABLED` plus a non-empty trusted-key allowlist. It ships operator-gated off. | With the surface disabled, `runtime/dapr_host.py` mounts a handler at the same paths returning HTTP 503 with an `a2a_skill_surface_disabled` error and the enable recipe — fail-loud, never a silent 404. |
| 16 | Scheduler-side reminder enumeration | Part 2 of orphan-reminder garbage collection: reading the scheduler's full reminder set so the sweep can also collect a reminder whose owning state row was itself lost. Part 1 ships. | No half-built path exists: part 2 sits behind an unset env flag, and part 1 acts only on provably-orphan reminders — it never fabricates a delete and never touches a live actor. |
| 17 | Runtime replica headcount | Introspecting how many sibling replicas the orchestrator actually launched. The interim guard trusts the operator-declared count. | Not a stub. `runtime/leader.py:assert_singleton_safe` raises `SingletonSafetyError` at boot when more than one replica is declared without leader election; `runtime/leader.py:LeaderLease` is the actual cross-replica correctness primitive and runs regardless. |
| 18 | Backup offsite destination | The destination wiring. The full backup, retention, timer and push logic are built; no offsite destination is configured. | `scripts/backup_scheduled.sh` (shell, outside scanner scope) warns loudly and writes an `OFFSITE_NOT_CONFIGURED.txt` marker into the generation directory rather than claiming a copy exists; with a destination set, a failed push exits non-zero so the unit goes failed. |
| 19 | Cross-source coalesce vector path | Nothing is half-built — the substrate-wide semantic/temporal linker is real, and off by default. | Not a stub. `data/analysts/deterministic_handlers/cross_source_coalesce.py:handle` emits a finding tagged `coalesce_unavailable` naming the missing ports rather than silently linking nothing. |
| 20 | Tier-2 `vector:world_context` grounding | Nothing is unbuilt — this is an active guarded pilot with open residuals: single-unit rollout, unproven faithfulness benefit, a tail-risk recurrence guard, an ephemeral rollback-state path. | Not a stub. `runtime/analyst_deps_builder.py:_build_grounding_hook` yields `None` on any grounding read failure rather than a stray or empty header; a sub-floor or empty corpus read contributes no preamble. `runtime/rag_rollback.py` reverts injection per run when the low-faithfulness tail thickens. |
| 21 | Time-series metrics store | Nothing — the metrics store was removed rather than faked. There is no metrics store at all, so nothing can silently pretend to write one. Full-text search over the signal corpus is live. | No symbol to guard: the code paths were deleted. On the search side, `runtime/substrate_query_port.py:PostgresQdrantSubstrateQueryPort.search_signals` is a real query. |
| 22 | Live GATHER tool actuation | — | Closed; see the appendix. |
| 23 | Dapr long-activity workflow round-trip | — | Resolved for the GEPA leg; see the appendix. The `deep_consult` sibling shares the fix but was not independently re-verified, so its in-process fallback stays the live default. |
| 24 | `nlp_client` boot-singleton re-resolution | — | Resolved; see the appendix. |
| 25 | Journal `change`-proposal apply path | Nothing new — the apply worker is built and import-verified, but the `change` path has not been exercised against a live registry. The `correction` and `self_revision` paths are tested end to end. | Not a stub; every apply path fails loud. `_apply_change` raises `ProposalApplyError` on an unknown operation or a bad diff shape, and a failed accept rolls forward to `archived` with the reason rather than dangling. The queue itself is the backstop: the journal writes only its own entries directly. |
| 26 | A critic and optimizer over the journal's voice | Routing the journal through a grounding-fidelity-versus-perspective critic and an optimizer over its own traces and the accept/reject log. Designed, not built; it depends on first building a critic actuator. | Nothing to guard — there is no code path, so nothing can claim the voice was vetted by a critic. |
| 27 | Journal panel first in-browser render | Nothing — the panel is built, typed and wired over the real read route; it has not been rendered in a browser. | UI-side, no backend symbol. The panel reads only real routes; an unrendered panel shows real or empty data, never a fabricated entry. |
| 28 | System Status panel first in-browser render | Nothing — same shape as #27 over the health routes. | UI-side, no backend symbol. The cadence route reads `analyst_traces` and the firing route reads `signals` plus poll outcomes; nothing is invented. |
| 29 | Semantic tier for contested-value clustering | An embedding-and-cosine tier over the contested-claims value clusterer. The shipped tier is canon plus normalized Levenshtein. | Nothing half-built. `data/provenance/value_clustering.py:cluster_values` degrades safely: two semantically equal but textually disjoint values land in separate clusters, which the arbiter treats as distinct candidates rather than as a silent merge. |
| 30 | GEPA self-optimizer cadence | Nothing — a sequenced freeze. The monolithic optimizer's `cadence.fallback_schedule` is null, so it fires on no tick. | Mechanically self-enforcing: the on-activate `if schedule:` gate in `runtime/dapr_actors.py` registers no `run_cadence` reminder for a null schedule, verifiable by the absence of a reminder registration. No allowlist line — a descriptor is outside `src/legba/**`. |
| 31 | `country_predictor` forecast-as-claim | Nothing — a sequenced retirement. Nulling the cadence was insufficient because the analyst is target-subscribed and kept firing reactively, so the live head is retired. | The lesson is the guard: nulling a cadence freezes only a cadence-only analyst. A reactive analyst must be retired, or have its subscription removed. A retired head is not reconciled. |
| 32 | `india_energy_predictor` forecast-as-claim | Nothing — a sequenced freeze, cadence-only, so the null schedule is sufficient. | Same `if schedule:` gate as #30; the liveness watchdog excludes a null-schedule analyst. |
| 33 | `journal_assessor` cadence | Nothing, and nothing is frozen — the entry and consolidation tiers run on cadence as an introspective instrument, off the fact/finding/nexus chain. The row stays so a reader who finds the historical freeze knows it is reversed. | The same reminder gate running the other way: a non-null schedule registers a `run_cadence` reminder. |
| 34 | World-assessment verdict framing | Nothing — a UI framing demotion. The world read is presented as a composed, cited finding, not as a headline verdict. | UI-side; the relabel is text and markup only, so the typecheck is the gate. |
| 35 | `country_assessor` monolithic framing | Nothing — a UI framing demotion, since followed by the backend retirement. The bounded units lead the per-country surface. | UI-side. The units it surfaces are real and measured, so nothing is fabricated and no output is hidden — only reframed. |
| 36 | Paged-human alert edge | An external sink that pages a human. The alert rewire is real: severity is a first-class indexed column, the escalation pack fires on the post-verify alert score, and delivery is durably published to the bus and audited. | Not a stub — nothing is fabricated. There is no external-sink code path, so nothing can claim a human was paged. |
| 37 | UCDP GED source | Nothing in code — token auth landed. The source is retired pending an operator-held credential, and it never ingested a signal. | Not a stub: the handler either has a token and fetches, or has none and writes nothing. Its one historical auth failure landed a loud error row with zero signals written; no event was invented. |
| 38 | Manual ingestion lanes against a live ingest | Nothing — the structured lanes (`data/seed/manual_batch.py`, `manual_schema.py`) and the vector lane (`data/rag/lane4_loader.py`) are built and covered, but have not run against a live production ingest. Format: [MANUAL_INGEST_FORMAT.md](MANUAL_INGEST_FORMAT.md). | Not a stub — every lane validates its input schema and fails loud on a malformed batch, and every write carries provenance. A bad batch is rejected, never partially written. |
| 39 | Non-Latin and Telegram NER re-enrichment | — | Resolved; the backfill drained. See the appendix. |
| 40 | Audit-remediation follow-ups | A declared backlog of deferred follow-ups from an audit whose headline defects are closed. | None of them has an in-tree path that could silently fabricate. The shipped guards degrade honestly without them: the verify guards demote rather than delete, through `effective_confidence = min(confidence, faithfulness)`. |
| 41 | Action-pack staleness eviction | Eviction or rebuild of a binding whose pack was updated after the analyst's dependencies were assembled. A periodic sweep compares content hashes and warns. | Warn-only by construction — the rider never mutates a binding, so it cannot half-apply. The drift window is bounded by the next runtime recreate, and the warning names the pack and the analyst. |
| 42 | Evidence-archive retention and object-store backend | Retention and expiry for archived evidence, and a non-filesystem backend. The archiver itself is built: it fetches the original bytes of cited signals through the guarded egress and the licence gate, stores them content-addressed, and stamps the row. | Nothing half-built: the archiver only ever adds objects and upgrades a signal's retention class, which the purge already exempts, so no purge can orphan an archive. A missing or unwritable archive root no-ops the tick loudly. |
| 43 | `signals_retention` TTL configuration | — | Resolved; an env fallback landed. See the appendix. |
| 44 | Two-tier composition evidence | — | Resolved. See the appendix. |
| 45 | Tier-aware judge rubric | — | Resolved. See the appendix. |
| 46 | Provenance-badge `fallback` signal | The backend half. The UI classifies a displayed number `live`, `fallback` or `absent`, but `fallback` is only returned when a route passes an explicit degradation flag, and most routes do not carry one yet. | The UI never fabricates a fallback state: with no explicit signal it never reports `fallback`. The failure mode is under-labelling, not a false claim. |
| 47 | Map co-mention arcs | Nothing in the renderer. The arc layer needs a signal whose `geo[]` names two or more countries, and baseline enrichment resolves each signal to a single country, so no pair forms. | The layer renders exactly what the data supports — empty — and the seam is stated in code at the render site. Nothing draws a fabricated arc. |
| 48 | `narrative_coordination` grounding on the narratives sidecar | A `"narratives"` grounding-source token: a sources-dispatch entry plus a block builder. The narratives read routes and the mapper are complete. | Nothing half-built — the token is not in the `GroundingBlock.sources` literal, so a descriptor declaring it is refused at schema validation, the same mechanism as #2. |
| 49 | The `claim_watch` closer | The correcting half. `claim_watch` matches new evidence against standing open questions from a durable watermark and flags; nothing corrects the product that rested on the stale reading, and nothing ever closes a question. | Not a stub — there is no half-built closer to guard. The watcher structurally cannot do more than flag: it writes no correction content and triggers no recomposition, true by construction of what it writes rather than by a toggle. The debt is readable and says `match_verified: false` on the wire. |
| 50 | Search control-query canary | — | Resolved; the scheduled half runs on the clock. See the appendix. |
| 51 | `watchlist.actions` | A watch declaring what to *do*. A watch row declares what to watch and a minimum severity; the table has no actions column and the scanner can only notify. | Nothing half-built — notify-only is the whole behaviour, and a watch author is never offered an action field that silently does nothing. |
| 52 | `analyst_traces.prompt_module_hash` | The binding for that one column. Its sibling `prompt_rendered` is now bound, capped, and hashed over the full prompt even when the stored text is truncated. | Nothing fabricated in either state: a NULL column read as NULL and hashed honestly, so historical receipts still verify byte for byte; a truncated value carries its marker and a digest rather than pretending to be complete. |
| 53 | GEPA optimizer plane | Nothing — the plane is mothballed by decision, not missing. | Two loud layers: `data/analysts/optimizer.py:run_method` refuses at its top with `OptimizerMothballedError` (a `RuntimeError`, same idiom as #10) for the mothballed analyst ids, and the descriptors carry no live cadence. |
| 54 | Outbound research residuals | Three residuals the write path declares rather than hides, around host licence classification, archive depth and measurement. The write path, the `research` pack and its `web_evidence` tool, the robots gate and the flag all ship. | All three fail safe: an unknown host resolves to a teaser depth with no bytes archived and a recorded skip reason, honouring the archiver's fail-closed gate rather than weakening it. |
| 55 | Correctness grader reference dependency | Nothing — the grader shipped whole, including the independent reference builder it depends on. The row stays because the dependency is the interesting part: the grader reads references and can never write one. | It fails loud and it fails empty — it never grades against a substitute. With no current reference at the stamp, `grade_target` writes nothing, calls no model, and names which gap it is: no reference at all, or a reference aged past the grace window. |
| 56 | Event causality and contradiction adjudication | A rule, model or operator lane that can honestly mint `caused_by` / `contradicts` event edges. The P1 reconciler produces only `correlated_with` and `evolves_from`; the schema's wider CHECK is vocabulary, not permission. | `data/analysts/deterministic_handlers/event_reconciler.py:write_event_edge` raises `EventEdgeUnsupportedError` with `SEAMS #56` for either type before SQL, so no deterministic path can fabricate attribution or contradiction. |
| 57 | The origin-class reader sweep | — | Resolved; the sweep landed with the collections core. See the [resolved appendix](#resolved-seams). The history WRITE path is its successor, #62. |
| 58 | In-tree OTS proof verification | Verifying a stored `receipt_anchors` OpenTimestamps proof inside Legba. The `receipt_anchor` handler mints the attestation (day × calendar rows, proof bytes stored); `status='submitted'` means the calendar answered, NOT that a verifier confirmed the proof. | Nothing claims verification: `status` is transport truth only, and the route lists the ledger without asserting it. The operator verifies a stored proof offline with the `ots` CLI (docs/ANALYSIS.md §10) — an `ots` dependency is deliberately not added. |
| 59 | Access-class enforcement (per-user / per-unit) | Reading is not gated by who or which unit is asking. The classification and the filter exist end to end — `scope.access_class` on every source descriptor, the `signals.access_class` ingest stamp (migration 0216) + backfill, the opt-in `access_class_in` filter on the findings and signals reads, and `access_ceiling_sql()` rendering the fact/finding/event inheritance rule — but nothing calls the ceiling helper and no request carries a caller identity or unit to check it against. | Fails OPEN by design during research: `access_class_clause` (`data/provenance/access.py`) is called ONLY when a caller passes `access_class_in`, so every existing read stays byte-identical with the parameter absent, and no reader applies `access_ceiling_sql()` — it is defined and exercised by its own unit test, never imported by a route. Nothing here claims enforcement. |
| 60 | Sampled layer-classification audit | The measurement of how often a curated `source_layers` row files a source under the wrong layer. `layer_divergence` counts per layer and compares a country's gap to its own baseline; every one of those numbers inherits the map's classification error, and a wrong layer is a DIRECTIONAL error — it does not blur the gap, it moves it one way — so an un-audited map cannot bound the error on a fired finding. The sampled audit (draw rows, adjudicate the layer, publish the rate beside its n) is the next lane. | Nothing is half-built and nothing claims to be audited: `deterministic_handlers/layer_divergence.py:CLASSIFICATION_AUDIT_NOTE` is stamped verbatim onto `data.classification_audit` of EVERY finding and receipt the unit writes, naming this seam, so no reader of a divergence number can mistake an un-audited map for an audited one. No allowlist line — there is no code path to fabricate from. |
| 61 | `not_collected` for SOURCES on the typed-absence route | The half of `not_collected` that asks "does any source cover this subject for this desk at all". `GET /v3/absence` emits the kind only for a bounded UNIT with no read on record. A desk's source roster is derived from in-scope signal PRODUCTION over `roster_days`, so a source that once covered the desk and has produced nothing inside the window is not on the roster and its absence is invisible; the curated coverage map that would answer the question properly (`source_layers`) is loaded for five desks. | Nothing is fabricated and nothing over-claims: every emitted source item is a source the route OBSERVED producing in scope, its proof names the `roster_days` window it was derived over, and the two source kinds are omitted entirely — and named in `not_measured` with the reason — for a desk with no `scope.geo` to resolve a roster from. The route never asserts "no source covers this" from a roster it knows is production-derived. No allowlist line: there is no code path to fabricate from. |
| 62 | Collection history WRITE path (`documents_*` loader kinds) | The half of collection loading that writes history into the tables the LIVE plane already owns. Collection SERIES are built: `observations` (migration 0220), `scripts/load_collection.py`, the `collection` descriptor family and the firewall. DOCUMENTS are not: the `documents_wacz` / `documents_cc_news` loader kinds would land pages in `signals` with a history `origin_class`, and a history FACT has an unsolved shape of its own — `idx_facts_temporal_triple_open` keys the open set on (subject, predicate, value, valid_from) with no `origin_class` leg, so an archived 2016 fact and a live 2026 fact asserting the same triple would COLLIDE at the index rather than coexist. The series loader also reads ANNUAL periods only; a sub-annual cadence refuses before any fetch rather than stamping a whole-year validity on a monthly number. | Two layers, both loud. `scripts/load_collection.py:run_load` raises `DocumentLoaderNotBuilt` at the TOP of the load — nothing fetched, nothing written — and the three `<table>_origin_class_history_writer_not_built` CHECK constraints (migration 0220, renamed from 0209's `readers_not_swept` when the reader sweep closed) refuse a history-class row at `signals`, `facts` and `events`; the constraint name is what a premature backfill sees in its error. No allowlist line: the guard is in `scripts/`, outside the scanner's `src/legba/**` scope, and it refuses rather than fabricating. Proven by `tests/data_pkg/test_collection_loader.py` and `tests/data_pkg/test_migration_0220_collections.py`. |


---

## Resolved seams

These numbers were declared seams and are confirmed resolved. The number stays
allocated so a `SEAMS #N` citation in another doc keeps resolving.

| # | Seam | Resolved by |
|---|---|---|
| 3 | Deep-crawl discovery jobs | Removal — `discover_sources_tool` is gone from `data/analysts/agency/tools.py` and `descriptors/action_pack_discovery.yaml` is retired |
| 11 | Consult `vector_search` embedder wiring | `runtime/substrate_query_port.py` embeds the query through the port-threaded embedder before the vector search |
| 22 | Live GATHER actuation of the web and propose tools | `inline_target._GATHER_TOOLS` spans read, web and write; the write-binding resolver in the actor path is live |
| 23 | Dapr long-activity workflow round-trip (GEPA leg) | Pass-by-reference training sets (`TrainingSetRef` + `materialize_training_set` in `dapr_workflow/gepa.py`) plus the body-size lever in `docker-compose.yml` |
| 24 | `nlp_client` boot-singleton cannot re-resolve | `runtime/nlp_client_factory.py:LazyNlpClient` resolves on first use and re-resolves on handler build |
| 39 | Non-Latin and Telegram NER re-enrichment backlog | The `reenrich_ner` backfill drained it; `deterministic_handlers/reenrich_ner.py` runs forward |
| 43 | `signals_retention` TTL is options-only | An env fallback in `signals_retention.py`, covered by `tests/data_pkg/test_signals_retention.py` |
| 44 | World and thematic two-tier evidence split | `LEGBA_COMPOSITION_TIERED_EVIDENCE` in `meta_findings_synthesizer.py`, covered by `tests/data_pkg/test_composition_tiered_evidence.py` |
| 45 | Tier-aware LLM-judge rubric | `verify._judge_periphery_rubric`, same test file |
| 50 | Search control-query canary, scheduled half | `scripts/host_search_canary.sh` on a 15-minute cron line, forcing a liveness probe. It pages only after two consecutive not-live probes and never restarts anything |
| 57 | The origin-class reader sweep | The collections core. Every open-row READ and SUPERSESSION site on `facts`/`events`, and every reader on the eight surfaces a collection is fenced from, now takes its predicate from `data/provenance/origin.py`; the reactive trigger plane gates the ROW (`runtime/triggers/coalescer.py:is_live_origin`). `tests/data_pkg/test_origin_gate_inventory.py` pins the classification of every `superseded_by IS NULL` site in `src/` — swept, index-predicate-only, or on a table with no `origin_class` column — and `tests/data_pkg/test_origin_firewall_surfaces.py` proves it against a synthetic 40-row history burst. The CHECK constraints stay, renamed to `<table>_origin_class_history_writer_not_built`: the readers are swept, but nothing may WRITE a history row to those tables yet (#62) |

---

## Audited identifiers that are not stubs

These trip the name scanner and were audited as real implementations. They are
allowlisted with their justification, because the registry is the escape
mechanism and there is no per-line pragma.

* `src/legba/runtime/dapr_workflow/gepa.py:StubWorkflowHandle` — a handle-shaped
  wrapper around a real in-process optimizer result; only the workflow and run
  ids are synthetic.
* `src/legba/data/sources/gdelt.py:_StubParam` — a duck-typed stand-in for
  `bigquery.ScalarQueryParameter` carrying real name/value pairs, so parameter
  iteration works whether or not the BigQuery SDK is importable. No fake
  behaviour.
* `src/legba/data/sources/gdelt.py:GDELTBigQuerySourceHandler._build_query_job_config._Stub`
  — the same pattern for the job config: the real object is used when the SDK
  imports.

---

## Machine-readable allowlist

The block between the BEGIN/END markers is parsed by
`tests/test_no_undeclared_stubs.py`. One `src/legba/<path>.py:<dotted.symbol>`
per line; a class-level entry covers symbols nested under it. Lines starting
with `#` are comments.

<!-- BEGIN SEAM ALLOWLIST -->
```
# seam 1 — eager media extraction (refuse-loud guards; stub edge removed by A-2)
src/legba/runtime/jobs/media_client.py:MediaClient
src/legba/runtime/jobs/process_media.py:process_media_handler
src/legba/data/jobs/media.py:MediaExtractionResult
# seam 6 — proxy usage ledger loud-fail guard
src/legba/data/stack/proxy/bright_data.py:ProxyPoolHandler.report_usage
src/legba/data/stack/proxy/bright_data.py:UsageLedgerUnavailable
# seam 7 — country_list_discovery url:/substrate: list sources (loud NotImplementedError)
src/legba/data/discovery/country_list_discovery.py:CountryListDiscovery._resolve_rows
# seam 56 — unearned event-edge types refuse loud before SQL
src/legba/data/analysts/deterministic_handlers/event_reconciler.py:write_event_edge
# seam 8 — common crawl S3 protocol surface (abstract idiom, registered per audit)
src/legba/data/sources/common_crawl.py:S3Client
# seam 9 — provenance INSERT routing guard
src/legba/data/provenance/writes.py:_insert_for_spec
# seam 10 — TAXII push is REAL; only an un-provisioned destination refuses
# loud (TaxiiServerNotConfiguredError is a RuntimeError guard rail, NOT a
# NotImplementedError stub — no allowlist line needed; the code never
# fabricates output).
# audited non-stub identifiers (see section above)
src/legba/runtime/dapr_workflow/gepa.py:StubWorkflowHandle
src/legba/data/sources/gdelt.py:_StubParam
src/legba/data/sources/gdelt.py:GDELTBigQuerySourceHandler._build_query_job_config._Stub
```
<!-- END SEAM ALLOWLIST -->

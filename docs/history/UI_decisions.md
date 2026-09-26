# UI decision record — extracted from docs/UI.md

Every dated statement, ticket-scoped narrative, "added on", workspace-preset
history and was/now reversal pulled out of `docs/UI.md` during the docs pass.
The design-tier page now states the shape and the reason; the sequence lives
here. Grouped by the program that produced it, newest program first. Ticket ids
are defined in `docs/TICKETS.md`.

---

## GLASS-3 — the ops deck (four visible Engine Room rows)

- Added four panel kinds as *visible* Engine Room rows rather than tabs or
  hidden kinds: `system.production_gauge` (Production Gauge),
  `system.judge_stats` (Judge Stats), `system.source_health` (Source Health),
  `system.eval_boards` (Eval Boards). All four pinned explicitly in
  `KIND_GROUP` even though the `system.*` prefix fallback would have reached
  Engine Room anyway, so the intent survives a change to the fallback.
- All four are `personal`-only: each reads operational internals (provider
  routing, source ledgers, run timings) that the `cis` lens has no use for.
- The ≤23-row sidebar budget (`navGroups.test.ts`) measures non-Engine-Room
  rows only, because Engine Room's rows collapse behind one header — so these
  cost the budget nothing, and the budget's "earn it, fold, or hide" terms did
  not bind here the way they bound `system.situations` / `system.narratives`.
- Production Gauge leads the deck because its `pages` flag is the same
  predicate the alert plane's `production_deficit` trigger fires on.
- `system.eval_boards` was made a separate kind rather than three more tabs on
  `system.eval_scorecard`: that panel was already 833 lines and answers "is this
  analyst getting better", where the boards are engine-level.

## GLASS-2 — consumers for API surfaces that had none

- `system.journal_gate` (Journal Gate) added: the `journal_proposals`
  accept/reject queue had been reachable only by curl. Pinned to Engine Room
  beside Weekly Grading — both are operator duties over a queue, not analytical
  reads. `personal`-only, because a gate that applies registry writes is
  single-operator by construction.

## D-4 / D2e

- `v4.morning_read` (Morning Read) registered **beside** `v4.assessment` rather
  than replacing it: while `LEGBA_COMPOSITION_ASSEMBLY` is off, rows carry no
  `data.data.assembly` and the legacy prose one-pager is still the right render
  of them, so the two overlap safely. The matrix was regenerated for this
  registration (56 → 57 panel kinds).
- `v4.morning_read` had no `KIND_GROUP` row at first, so the landing's headline
  product filed itself under Investigation through the `v4 →` prefix fallback.
  Given an explicit Products row afterwards, with a `TASK_ORDER` slot
  (Navigator → Morning Read → World Assessment).
- `system.read_scoreboard` (Read Scoreboard) added 2026-08-29 as the 56th
  registered kind — the one panel whose subject is the operator. It exists
  because the platform receipted ~80 write tables and had never once receipted a
  read.

## K-G4 — the graph walk

- `system.graph_walk` added and pinned to Investigation rather than left to the
  `system.*` → Engine Room fallback, spending one of the U-3 ≤22 visible sidebar
  rows deliberately. It is the one graph surface not folded into Entities,
  because it is an interactive verb over the reified `entity_edges` store that
  no other panel touches.
- `cooccurrence` edges are off by default because they were 8,722 of the graph's
  12,566 edges at the time of the build.

## U-3 — the merge train (twelve kinds retired into aliases)

- The alias table (`panel-registry/aliases.ts`) **replaced `HIDDEN_KINDS` as the
  retirement mechanism** (UI_HOLISTIC_DESIGN_2026-08-24 §4.4). At its peak,
  eighteen of sixty-seven registered kinds existed only to be invisible. Twelve
  became alias rows; the panel table went 67 → 55.
- Merges: Deep Consult → a depth toggle on Consult (`merged/Consult.tsx`);
  `v4.timeline` + `system.timeline` → one Timeline with an Events/Validity mode
  switch (the two panels were literally both named "Timeline");
  `v4.why` + `system.lineage` + `v4.flow` + `system.situations` +
  `system.narratives` → Provenance; `system.watchlist` +
  `system.alert_center` + `system.escalations` → Alerts & Watches;
  `system.entity_graph` + `system.notable_structure` → Entities. All originals
  mounted unmodified inside their survivor.
- `system.alert_center` left `PREVIEW_KINDS` when it retired into the alias
  table; its preview badge now rides the Alerts & Watches "Triggers" tab.
- Renames (U-3 §4): "KPI Strip" → "At a Glance" (the old name named the widget,
  not what an operator uses it for); "Correctness Gold Set" → "Weekly Grading"
  (the old name described the data structure, not the operator's weekly task).
- The `operations` nav group was relabeled "Engine Room"; the id stayed
  `operations` so `legba_nav_collapsed` / `DEFAULT_COLLAPSED` needed no
  migration. The raw Targets/Analysts instance groups were nested inside it,
  collapsed by default.
- Decision 3: Consult's modes widened `['personal']` → `['personal','cis']` —
  leaving the operator's main working surface personal-only made it invisible
  for half their sessions. It was also moved from Analysis to Awareness, at the
  end of the workflow order.

## UI_HOLISTIC_DESIGN_2026-08-24 — the folded catalog

- All five verb groups start collapsed with a count on each header, so the
  sidebar opens as five rows over the Desks section rather than the thirty-six-row
  catalog it had grown into.

## U-4 (COHERENCE_WAVES_PLAN_2026-07-28) and P1-7 — the Wall

- `system.wall` (The Wall) added as the mission-control anchor; shipped with an
  optional **Wall** layout preset so the default boot grid was unchanged and the
  operator opted in.
- `system.wall_movers` kept registered and hidden rather than aliased onto the
  Wall, so a saved layout holding that tile keeps rendering the quadrant it asked
  for.
- The Morning Read stance later mounted the whole Wall as its top band and
  stopped seeding the standalone movers tile: the old grid mounted a quadrant
  beside its own parent. U-4's "cold boot must answer what changed" acceptance
  was kept, by the panel that owns the answer.
- The Morning Read stance superseded the S7-T2 mission-control boot grid.
- On the first boot after workspaces shipped, a pre-existing
  `legba_layout_custom` layout is **copied** into the Morning Read slot — never
  moved, so the sidebar's Save/Restore keeps its own key and behaviour.

## S7-T2 — the shell reform

- One grouped tree replaced the prior two-level Intelligence/Operations split
  plus Monitor / Investigate / Configure / Operate sub-buckets.
- Deleted outright (no kind, no alias, because nothing renders them at all):
  `system.pulse` (Global Pulse), `system.eval`, `system.users`,
  `system.streams` (NATS tail), `registry.wirings`, `registry.mutations`,
  `registry.discovery`, `dashboard.dynamic`, `system.backfill` (Backfill
  Replay), `system.runtime` (Runtime Actor Health, duplicate of
  `system.actor_health`), `system.tenant_view`, `system.targets.roster`
  (Targets Roster — its function lives in `registry.targets`), `v4.case`
  (Casework Board — shelved, no pin entry points were ever wired).
- Tenant View's deletion is deliberate truth-in-labeling: multitenancy is not
  product-baked.
- `v4.assessment` (World Assessment) un-hidden and made the boot grid's
  right-rail report panel.
- The Monitoring preset's third tile was re-pointed at `registry.targets` after
  `system.targets.roster` was collapsed into it (#90 Wave A) and deleted (S7-T2).
- `system.backfill` left `PREVIEW_KINDS` by deletion; the catch-up replay remains
  a runtime-plane operation with no registry proxy.
- The Mutations Queue panel was deleted here, which is why the journal-proposals
  review surface was unwired until GLASS-2 added the Journal Gate.

## The redesign / #90 — the feed merge and the keystone

- `v4.feed` **deleted** (the only panel genuinely deleted rather than hidden at
  the time), subsumed by `system.findings` as the single unified findings +
  signals Live Feed.
- The Inspector became the keystone; the layout presets swapped their former
  Lineage slot for it and added a Zen focus mode. #90 added the **Workspace**
  intel-desk preset.
- Consult gained pin-to-context; backend hydration of `pinned_context` was left
  a non-blocking follow-up.
- Flow's local `selectedNodeId`, the v4 cross-room store and the `legba:open-*`
  window-event bus were all retired into the unified selection store (~20
  `legba:open-lineage` dispatchers became `selectRow()` calls).
- `legba:set-tenant` died with the Tenant View deletion.

## P0-2f — measured surfaces inside Dockview

- `useDockviewTileRedraw` and `useElementWidth` added to kill the
  blank-surface classes measured surfaces hit inside Dockview (a WebGL map or a
  recharts `ResponsiveContainer` mounting into a hidden tile; the
  `system.timeline` first-mount blank).
- `panel-registry/synthesize.ts` added: the live `ui_panel_registrations`
  surface is empty, so without synthesis every bound panel kind was
  sidebar-unreachable.
- Flow's edge-density gate added (`DENSE_EDGE_COUNT_THRESHOLD` 400; the
  `analyst_target` predicate fan-out default-hidden).

## P0-4 / C2b, P1-8, P2-1, P2-3, P2-5, P4-3, P4-4, P4-5, P5-6, A10, A-3, A7

- P0-4 / C2b — the two verify-exempt states on the verdict badge: a
  deterministic structural analyst's finding renders `unverified — structural`
  (the client mirrors the server's `STRUCTURAL_VERIFY_EXEMPT_ANALYSTS` registry
  for live-tail rows), and a structural finding re-derived from its own lineage
  and matched is stamped `verify_exempt: "structural-verified"`.
- P1-8 — the citation-chip hover verdict card.
- 2026-08-30 — all six grounding kinds carried verbatim (`lib/citationsModel.ts`).
  Before it, the citation model recognised exactly one grounding kind and guessed
  about the rest: a resolved window-ledger reference rendered as an amber
  "Unresolved citation" warning when it was neither unresolved nor a problem, and
  a prior-read reference was captioned `signal` and drilled to a signal that does
  not exist.
- P2-1 — the evidence archiver's `archived` flag + `archive_sha256` surfaced in
  the export.
- P2-3 — `/v3/eval/calibration` gained an additive `band_calibration` section;
  the Eval Scorecard does not render it (declared follow-up).
- P2-5 — `system.goldset` added (weekly correctness labeling worksheet).
- P4-3 — the World Map deepening: choropleth hover/click, deck.gl hex and heat
  density, co-mention arcs, geo-convergence markers, watch locations, the
  dual-thumb time scrubber.
- P4-4 — `system.timeline` added (the validity-window temporal view) with
  `GET /api/v1/v3/timeline` added for it.
- P4-5 — the `live | fallback | absent` ProvenanceBadge and ProvenanceCard.
- P5-6 — `system.watchlist` (server-side standing watches) added, plus the
  Inspector's "Watch this" affordance.
- A10 — the collection basket: Report Export left `PREVIEW_KINDS` and the hidden
  set, and now fronts `POST /api/v1/v3/export`. STIX was demoted to
  optional-later by operator decision; the DOM-free STIX machinery stays in the
  repo, unused.
- A-3 — live since the 2026-06-10 cutover: consult routes its ReAct tool calls
  through the governed `substrate_read` pack, and the actor run path fires
  `escalate_finding`, which is what gives Governor Events real decision volume.
- A7 — the deterministic cross-stream correlator whose active bins the map
  renders as geo-convergence markers.
- E-5 — map popups themed to the dark chrome.

## #89 — the ops surface

- `system.status` (System Status) added: the at-a-glance per-layer health view
  answering "are all sources firing? how is the queue? which cadence triggers are
  stalled?" in one page. Its Analysis layer reads `analyst_traces` rather than
  `actor_state.last_run_at`, which is NULL.
- `system.stream_lag` rolled into System Status's Queues section and hidden from
  the sidebar.
- The Queues section uses the orphan-filtered consumer-lag route — the fix for
  the "tons of targets, all with 845 pending" phantom the raw lag view showed.
- The Live Feed gained selection-follow.

## Auth chain

- 2026-06-01 — the two-layer auth chain landed: Caddy `basic_auth` perimeter plus
  registry bearer injection (OP-1, `LEGBA_REGISTRY_API_TOKEN`). Retired vhosts
  from the pre-reshape config (ui-v2, legba2/dcmaster basic_auth shells) were
  deliberately not carried forward.
- Pre-2026-08-03 the WebSocket credential travelled as a `?token=` query param,
  byte-identical to the registry admin credential and written verbatim into
  console warnings and access logs. Replaced by the `legba.bearer.v1` WebSocket
  subprotocol; the server still accepts `?token=` so a stale SPA build does not
  hard-break on deploy, logging `registry.ws.auth.deprecated_query_token` each
  time. That path is removable once no client sends one.
- W-1b §4 (resilience-observability) — `GET /api/v1/registry/healthz` upgraded
  from a liveness stub to a real readiness probe (PG + NATS), and Caddy's
  `reverse_proxy` gained active upstream health polling against it.

## Layout / naming history now stated as rules

- The Layouts menu (presets) and the workspace bar are additive and distinct:
  applying a preset clears and re-seeds the dock, where switching a workspace
  saves the outgoing stance and restores the incoming one. Presets own
  `legba_layout_custom`; workspaces own `legba_ws`. Neither writes the other's
  key. Dockview serializes no version of its own, so a workspace slot is wrapped
  as `{schemaVersion, layout}`.
- `system.report_export` left the hidden set in A10.
- The former "Runtime Actor Health" kind (`system.runtime`) duplicated
  `system.actor_health` and was deleted.
- Source-first panels (UI-2 / Tier C), Eval + Ops panels (UI-5 / Tiers E+F) and
  the product panels (UI-6 / Tier G) were the original registration waves; the
  surviving kinds are in the panel catalog.
- L-108 §1 is the original statement of the two-registry model; L-108 §6 is the
  rule that operator-category panels are `personal`-only, enforced by
  `registry.test.ts`.

## Live measurements quoted in the old page (point-in-time, not rules)

- A recent optimizer run: parent faithfulness 0.34 → candidate 0.29, delta −0.05.
- The correctness badge's example line: `correctness 90.9% (10/11) · coverage
  24.4% (n=45) · as of 2026-09-16 · single-family`.
- Graph Walk edge census: 8,722 `cooccurrence` of 12,566 total edges.
- The US country reads all-insufficient on the scorecard because its unit
  faithfulness is genuinely low.

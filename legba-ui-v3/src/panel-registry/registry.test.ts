/**
 * Tests for the bundle-time registry table — every spec'd panel kind is
 * declared, every panel_id is unique, every singleton has scopeKey=null.
 */

import { describe, it, expect } from 'vitest'
import {
  PANEL_REGISTRY,
  PANEL_ID_TO_KIND,
  SINGLETON_PANELS,
} from './registry'
import { KIND_ALIASES, resolveKind, resolveRetiredPanelId } from './aliases'
import { catalogKinds, isDrill, REGISTRY_TOTAL_CEILING, RETIREMENT_SCHEDULE } from './kindPlan'
import { findSurface, surfaceKinds } from '@/lib/surfaces'
import type { PanelKind } from '@/types'

describe('PANEL_REGISTRY', () => {
  it('declares every L-092 panel kind', () => {
    // Per legba_ui_panels_v2.md §3 and legba_panel_registration.md §2,
    // minus the retired /predictions-backed panels (Target Hypotheses,
    // Predictor Forecasts) dropped with the /predictions read endpoint.
    const kinds = Object.keys(PANEL_REGISTRY)
    expect(kinds.length).toBeGreaterThanOrEqual(30)
  })

  it('has unique panel_ids', () => {
    const seen = new Set<string>()
    for (const entry of Object.values(PANEL_REGISTRY)) {
      expect(seen.has(entry.definition.panelId)).toBe(false)
      seen.add(entry.definition.panelId)
    }
  })

  it('maps panel_id back to its kind', () => {
    for (const [kind, entry] of Object.entries(PANEL_REGISTRY)) {
      expect(PANEL_ID_TO_KIND[entry.definition.panelId]).toBe(kind)
    }
  })

  it('singletons have scopeKey=null and requiresBinding=false', () => {
    for (const kind of SINGLETON_PANELS) {
      const def = PANEL_REGISTRY[kind].definition
      expect(def.scopeKey).toBe(null)
      expect(def.requiresBinding).toBe(false)
    }
  })

  it('per-target panels are scoped on target_id', () => {
    const targets = Object.entries(PANEL_REGISTRY).filter(
      ([, e]) => e.definition.category === 'target',
    )
    // Was 10 (T1-T10); Target Hypotheses retired with /predictions. UI-B3
    // (W11): eight of the nine are tabs of ONE kind, `subject.page`; the map
    // and the clock wait for their survivors. Three bound subject kinds.
    expect(targets.map(([k]) => k).sort()).toEqual(['subject.page', 'target.map', 'target.timeline'])
    for (const [, entry] of targets) {
      expect(entry.definition.scopeKey).toBe('target_id')
      expect(entry.definition.requiresBinding).toBe(true)
    }
  })

  it('per-analyst panels are scoped on analyst_id', () => {
    const analysts = Object.entries(PANEL_REGISTRY).filter(
      ([, e]) => e.definition.category === 'analyst',
    )
    // Was 5 (A1-A5); Predictor Forecasts retired with /predictions.
    expect(analysts.length).toBe(4)
    for (const [, entry] of analysts) {
      expect(entry.definition.scopeKey).toBe('analyst_id')
    }
  })

  it('every panel declares at least one mode', () => {
    for (const entry of Object.values(PANEL_REGISTRY)) {
      expect(entry.definition.modes.length).toBeGreaterThan(0)
    }
  })

  it('operator panels are personal-only per L-108 §6', () => {
    const operator = Object.values(PANEL_REGISTRY).filter(
      (e) => e.definition.category === 'operator',
    )
    for (const entry of operator) {
      expect(entry.definition.modes).toEqual(['personal'])
    }
  })
})

/**
 * Layout-compat (COHERENCE_WAVES_PLAN_2026-07-28 §1.9 / §U-3 acceptance,
 * re-expressed for the ALIAS mechanism — UI_HOLISTIC_DESIGN_2026-08-24 §4.4).
 *
 * The requirement is unchanged and permanent: a saved Dockview layout, a ⌘K
 * deep-link or a `ui_panel_registrations` row that names a PRE-MERGE kind must
 * still render something real, never an `unknown_panel_id` placeholder.
 *
 * What changed is the price. Until this train the mechanism was `HIDDEN_KINDS`:
 * the merged-away kind stayed a FULL registry row — component import and all —
 * with `hidden = true`. Twelve of those rows existed only to be invisible.
 * They are now twelve lines in `panel-registry/aliases.ts`, and the assertions
 * below moved with them: the kind must be GONE from the registry, and it must
 * RESOLVE through the alias table onto the survivor that renders it, on the
 * tab that IS the retired surface. See `aliases.test.ts` for the exhaustive
 * table-level bar (every retired id resolves, no alias points at another
 * alias, every tab names a real tab, the fromJSON pre-pass collapses
 * duplicates).
 */
describe('U-3 layout-compat — old panel ids resolve through the alias table', () => {
  // Every kind U-3/GLASS-2 and UI-B1/merges folded away behind a
  // merged/tabbed survivor.
  const MERGED_AWAY_KINDS = [
    'v4.timeline', // → Timeline's "Events" mode (survivor: system.timeline)
    'v4.why', // → Provenance's "Why" tab (survivor: system.provenance)
    'system.lineage', // → Provenance's "Lineage" tab
    'v4.flow', // → the Registry's "Canvas" tab (re-pointed by UI-B1/merges)
    'system.situations', // → the Register's "Situations" tab (re-pointed)
    'system.narratives', // → the Register's "Narratives" tab (re-pointed)
    'system.alert_center', // → Alerts & Watches' "Triggers" tab
    'system.watchlist', // → Alerts & Watches' "Watches" tab
    'system.escalations', // → Alerts & Watches' "Deliveries" tab
    'system.deep_consult', // → Consult's "Deep" depth
    'system.entity_graph', // → Entities' "Graph" tab
    'system.notable_structure', // → Entities' "Structure" tab
    // UI-B1/merges (W8) — UI-D1 Table B's twenty
    'registry.targets', // → Registry "Subjects"
    'registry.analysts', // → Registry "Units"
    'registry.sources', // → Registry "Sources"
    'source.detail', // → Registry "Sources" (the detail beneath the list)
    'registry.stack', // → Registry "Stack"
    'registry.action_packs', // → Registry "Packs"
    'source.subscription_builder', // → Registry "Builder"
    'source.subscription_policy', // → Registry "Policy"
    'source.fanout', // → Provenance "Fan-out"
    'system.production_gauge', // → System Status "Production"
    'system.dead_letter', // → System Status "Failures"
    'system.actor_health', // → System Status "Actors"
    'system.stream_lag', // → System Status "Queues"
    'system.governor', // → Ledgers "Governor"
    'system.audit', // → Ledgers "Audit"
    'system.budget', // → Ledgers "Budget"
    'system.optimizer.diff', // → Optimizer "Diff"
    'v4.kpi', // → the Wall's header band
    'system.wall_movers', // → the Wall's "Movers" tab
    'system.navigator', // → the read (the index is the object rail's Reads)
  ]

  it('every merged-away kind is GONE from the registry — retired, not hidden', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      expect(
        PANEL_REGISTRY[kind as PanelKind],
        `${kind} should no longer cost a registry row`,
      ).toBeUndefined()
      expect(SINGLETON_PANELS as string[]).not.toContain(kind)
    }
  })

  it('every merged-away kind still RESOLVES, onto a live survivor', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      const alias = resolveKind(kind)
      expect(alias, `${kind} must resolve`).toBeDefined()
      expect(PANEL_REGISTRY[alias!.kind], `${kind} → ${alias!.kind}`).toBeDefined()
    }
  })

  it("every merged-away kind's panel_id still resolves (what a registration row actually persists)", () => {
    // `<kind>` → `<panel_id>` is the historical snake_case form; the loader
    // resolves it through PANEL_ID_ALIASES now that PANEL_ID_TO_KIND cannot.
    for (const kind of MERGED_AWAY_KINDS) {
      const panelId = kind.replace(/[.]/g, '_')
      expect(PANEL_ID_TO_KIND[panelId], `${panelId} must not be a live kind`).toBeUndefined()
      const alias = resolveRetiredPanelId(panelId)
      expect(alias, `panel_id "${panelId}" must resolve`).toBeDefined()
      expect(PANEL_REGISTRY[alias!.kind]).toBeDefined()
    }
  })

  it('the merged/survivor kinds exist, are visible, and are non-binding singletons', () => {
    const survivors: PanelKind[] = [
      'system.timeline', // Timeline (Events / Validity)
      'system.provenance', // Provenance (Why / Lineage / Fan-out)
      'system.alerts_watches', // Alerts & Watches (Watches / Triggers / Deliveries)
      'system.consult', // Consult (Chat / Deep)
      'system.entities', // Entities (List / Graph / Structure)
      // UI-B1/merges
      'system.registry', // Subjects / Units / Sources / Stack / Packs / Builder / Policy / Canvas
      'system.status', // Status / Production / Failures / Actors / Queues
      'system.ledgers', // Governor / Audit / Budget
      'system.register', // Situations / Narratives
      'system.optimizer', // Candidates / Diff
      'system.wall', // the At-a-Glance band over Overview / Movers
    ]
    for (const kind of survivors) {
      const entry = PANEL_REGISTRY[kind]
      expect(entry, `${kind} must exist`).toBeDefined()
      expect(entry.definition.requiresBinding).toBe(false)
      // system.entities is 'operator' category (personal-only per L-108 §6) —
      // still in the `＋` catalog: a survivor is never a drill.
      expect(isDrill(kind)).toBe(false)
    }
  })

  it('no survivor was itself retired (an alias must never point at an alias)', () => {
    for (const kind of MERGED_AWAY_KINDS) {
      const alias = resolveKind(kind)!
      expect(resolveKind(alias.kind)!.kind).toBe(alias.kind)
    }
  })
})

/**
 * U-4 (COHERENCE_WAVES_PLAN_2026-07-28 §U-4) — the boot-grid "what changed"
 * tile was registered for a DIFFERENT reason than the U-3 merge aliases above:
 * a brand-new capability (not a folded-away original). UI-B1/merges landed its
 * Table B row: it is the Wall's Movers tab now, still fully reachable by id.
 */
describe('System.wall_movers landed on the Wall\'s Movers tab, still fully reachable', () => {
  it('is no longer a registry row; its id and panel_id resolve onto the Wall, on Movers', () => {
    expect(PANEL_REGISTRY['system.wall_movers' as PanelKind]).toBeUndefined()
    expect(resolveKind('system.wall_movers')).toEqual({ kind: 'system.wall', tab: 'movers' })
    expect(resolveRetiredPanelId('system_wall_movers')).toEqual({ kind: 'system.wall', tab: 'movers' })
  })

  it("its Table B row is flipped to retired, with the alias's survivor and tab", () => {
    expect(RETIREMENT_SCHEDULE['system.wall_movers']).toEqual({
      survivor: 'system.wall',
      tab: 'movers',
      lands: 'UI-B1/merges',
      retired: true,
    })
  })

  it('landing it did not make system.wall (the full panel) a drill or binding', () => {
    const wall = PANEL_REGISTRY['system.wall'].definition
    expect(isDrill('system.wall')).toBe(false)
    expect(wall.requiresBinding).toBe(false)
    expect([...wall.modes].sort()).toEqual(['cis', 'personal'])
  })
})

/**
 * GLASS-2 — the three API surfaces that shipped with no consumer now have one.
 *
 * Two of the three (`system.situations`, `system.narratives`) landed as TABS of
 * the merged Provenance panel for the old ≤23-visible-row budget. UI-D1 Table A
 * gives them their own kind, `system.register` (Situations · Narratives), and
 * UI-B1/merges RE-POINTED both alias rows there: the surfaces still render
 * unmodified, and a deep-link to either id still lands on its tab.
 */
describe('GLASS-2 — the unconsumed-API consumers', () => {
  it('the journal gate is a real, catalog-listed, non-binding singleton', () => {
    const entry = PANEL_REGISTRY['system.journal_gate']
    expect(entry).toBeDefined()
    expect(entry.Component).toBeDefined()
    expect(entry.definition.requiresBinding).toBe(false)
    expect(entry.definition.scopeKey).toBe(null)
    expect(catalogKinds()).toContain('system.journal_gate')
    expect(SINGLETON_PANELS).toContain('system.journal_gate')
  })

  it('the journal gate is personal-only — it applies registry writes', () => {
    expect(PANEL_REGISTRY['system.journal_gate'].definition.modes).toEqual(['personal'])
  })

  it('the two tab-mounted surfaces resolve onto the Register, on their own tabs', () => {
    expect(resolveKind('system.situations')).toEqual({
      kind: 'system.register',
      tab: 'situations',
    })
    expect(resolveKind('system.narratives')).toEqual({
      kind: 'system.register',
      tab: 'narratives',
    })
  })

  it('their host (system.register) is a catalog kind, never a drill', () => {
    const host = PANEL_REGISTRY['system.register'].definition
    expect(isDrill('system.register')).toBe(false)
    expect(host.requiresBinding).toBe(false)
    expect(catalogKinds()).toContain('system.register')
  })
})

/**
 * The catalog after the retirement (UI_HOLISTIC_DESIGN_2026-08-24 §4.3).
 *
 * A ratchet in the direction the design pushes: the registry may not grow back
 * the rows the alias table just removed. It is deliberately a CEILING, not an
 * equality — the merge trains that follow shrink it further, and each one is
 * expected to lower this number, never raise it.
 */
describe('registry size ratchet', () => {
  it('stays at or under the post-alias count', () => {
    // 55 → 56: ONE documented exception, D2e (the read-telemetry train).
    //
    // The ratchet exists because the operator's verdict on the panel program
    // was "why are we just continuing to add more damn panels", and
    // PREMISE_REASON_TO_EXIST §4 puts "further Engine-Room observability
    // panels for an engine room with no visitor" on the kill-list. A new kind
    // therefore has to answer for itself, loudly, here.
    //
    // `system.read_scoreboard` answers: it is the ONLY panel that measures
    // whether the other 55 are read at all. It is the instrument of the
    // 90-day oracle wager (§5 Option 1), which is the thing that will decide
    // whether the panel program continues or is cut — so it is the one
    // addition whose purpose is to make deletions defensible rather than to
    // postpone them. Folding it into an existing engine panel was considered
    // and rejected: burying the wager's scoreboard inside a surface nobody
    // visits reproduces the exact failure mode under study.
    //
    // The ratchet is re-armed at 56 and the same rule applies to the next
    // one. If the wager returns a negative verdict at day 90, this row goes
    // with the rest of the deck.
    //
    // 56 → 57: a SECOND documented exception, D-4 (the reader train of the
    // composition demotion), and unlike the first it is explicitly temporary.
    //
    // `v4.morning_read` answers the ratchet's question by not being a new
    // panel at all in the sense it guards against: it renders the SAME
    // producer, from the SAME `/api/v1/findings?analyst_id=world_assessor`
    // query, that `v4.assessment` already renders — the day's read, now
    // projected from the typed `assembly.v1` payload rather than from composed
    // prose. It is the product surface of the demotion program, ruled by the
    // operator in the 2026-09-03 set (Layout B default, three grains contexted,
    // the Assessment fenced and badged), not another observability tile.
    //
    // The two rows coexist for exactly as long as the A/B does. Spec §5.2 makes
    // `LEGBA_COMPOSITION_ASSEMBLY` a runtime flag and requires the legacy prose
    // path to stay runnable for the whole program, and the grading compares the
    // two side by side — deleting the surface under comparison would delete the
    // comparison. When the flag's default flips and the A/B closes,
    // `v4.assessment` retires through `panel-registry/aliases.ts` onto
    // `v4.morning_read`, exactly as the twelve U-3/GLASS-2 originals did, and
    // this number returns to 56 with no new argument required.
    //
    // 57 → 58: a THIRD documented exception, workstation T-1's
    // `system.navigator`, and it is the one that makes the previous two
    // arguable in the first place.
    //
    // Every prior exception had to argue that it was not "another panel". This
    // one argues something else: it is the surface that stops the REGISTRY from
    // being the product. The diagnosis this ratchet was armed against is that
    // the front door is a list of panels; the Navigator is a front door that
    // lists PRODUCTS — the world read, the regional rollups, the country desks,
    // the thematic lanes, the journal — from `/api/v1/findings`, with no new
    // endpoint and no panel row among its contents.
    //
    // And it is the row that repays the others. §7 of WORKSTATION_V2_FLOW_DESIGN
    // retires twelve kinds outright and runs six merge trains to reach 24
    // registered kinds; T-2 executes it. If that number does not come down, this
    // exception was not worth taking and this line is the record of who to ask.
    //
    // 58 → 59: a FOURTH documented exception, 7b-v's
    // `system.layer_divergence`, and it answers the ratchet on the ratchet's
    // own terms rather than asking for an allowance.
    //
    // The kill-list item this ratchet enforces is "further Engine-Room
    // observability panels for an engine room with no visitor". This row is
    // neither: it is not Engine Room (it reads a country's information
    // environment, not the plumbing's health — see `navGroups.ts`), and its
    // subject is one of the four differentiators the 2026-09-23 capture named
    // as having to be VISIBLE on the reader surface: "cited source beside each
    // claim, declared gaps, confidence folds with their reason, the divergence
    // map". Three of those four had a surface already; this was the one with
    // none.
    //
    // It is also the only way the unit is READABLE AT ALL. The layer-measure unit
    // writes no table: its series and receipt ride the analyst payload, and a
    // run that fires nothing is suppressed to trace-only so an idempotent re-run does
    // not repeat "no gap change" in the feed. The consequence is that on every
    // quiet day — the overwhelming majority, and the ones that prove the
    // instrument is running rather than merely not complaining — the measure
    // exists ONLY inside `analyst_traces.output_payload`. Folding it into the
    // Live Feed or a scorecard tab was considered and is not possible: those
    // surfaces read `analyst_outputs`, which is empty on exactly those days.
    //
    // Re-armed at 59, same rule for the next one. The §7 merge trains still owe
    // this number a reduction, and this row is not an argument against that.
    //
    // 59 → 60: a FIFTH documented exception,
    // `analysis.cross_framing`, and it is the first one that costs the SIDEBAR
    // nothing.
    //
    // The kill-list item this ratchet enforces is "further Engine-Room
    // observability panels for an engine room with no visitor". This is neither
    // half of that: its subject is a published CLAIM, and its reader is the
    // person deciding whether to believe it. The three options the ratchet
    // offers are earn / fold / hide, and this row takes the third — it is
    // registered `hidden`, reached from the claim it is about (the Claims
    // panel's action, a situation row, ⌘K), so the 26-row sidebar budget is
    // untouched by this row and no visible row was bought.
    //
    // Folding it into an existing surface was considered and does not work.
    // The Inspector reads ONE record; the gap strip reads one desk's units as
    // CELLS with no sentence in them; the Morning Read reads the composition
    // top-down. The thing none of them can do is hold one claim still and put
    // every unit's own sentence beside it, which is the only arrangement in
    // which "three units rest on this same signal and frame it three different
    // ways" and "a fourth is not disagreeing, it is ABSENT" are visible at all.
    // That second reading is the one a multi-unit surface is uniquely able to
    // take and every single-unit surface discards.
    //
    // It adds NO route. Its five sections ride `/findings`, `/v3/contentions`,
    // `/v3/absence`, `/v3/layers/divergence` and `/journal?kind=lens`, all of
    // which the bundle already calls, and every derivation is pure in
    // `lib/framingModel.ts`.
    //
    // Re-armed at 60, same rule for the next one. The §7 merge trains still owe
    // this number a reduction, and a hidden row is not an argument against
    // that: it is the cheapest form of the same debt.

    //
    // 60 → 61: a SIXTH documented exception,
    // `target.desk_brief_page`, argued the same way the fourth was — the row
    // is the only way an existing product is READABLE AT ALL.
    //
    // The desk brief is not new. `POST /v3/export` has composed it since
    // 7b-iii — the composition, each unit's latest admitted read in the
    // composition's own declared order, the open-situations appendix — and
    // `lib/printDocument.ts` has laid it out for paper since k1. Since k5b it
    // also carries the desk's server-read typed absence. All of that is
    // handed to the reader as a FILE: the only way to see a desk's whole
    // picture today is to download it or print it, and the workstation, whose
    // whole claim is that the checking machinery is visible, is the one place
    // the brief cannot be read.
    //
    // It is therefore not "another panel" in the sense this ratchet guards
    // against. It adds no route, no producer and no observability tile; it
    // renders a document the platform already composes, through components
    // that already exist (`CitedProse`, `ScaleStamp`, `UnitCorrectnessBadge`,
    // `AssessmentBand`), and it is the first surface on which the four
    // differentiators the 2026-09-23 capture named — cited source beside each
    // claim, declared gaps, confidence folds with their reason, the layer
    // aperture — appear together over ONE desk rather than one per panel.
    //
    // Unlike the 57th this exception is NOT paired with a retirement, and
    // saying so is the point: nothing comes off the count for it today. What
    // would bring the number back down is the §7 merge train folding the
    // per-desk target panels (Overview / Findings / Situations / Claims) into
    // one tabbed desk surface with this page as a tab — which is a merge, not
    // an argument, and this line is the record of who to ask if it does not
    // happen.
    //
    // Re-armed at 60, same rule for the next one. The §7 merge trains still owe
    // this number a reduction, and this row is not an argument against that.
    //
    // 61 → 62: a SEVENTH documented exception, W2 S-batch's
    // `analysis.contentions`, and it takes the ratchet's "hide" option the way
    // the fifth did — registered `hidden`, ⌘K-reachable, so the 26-row sidebar
    // budget does not move.
    //
    // It is the only LIST of a record the platform already writes and already
    // serves. `GET /v3/contentions` has had a fleet-wide, newest-first mode
    // since 7a — "what did the contrary-evidence pass find last night" — and
    // no surface has ever called it without a finding id: the Inspector chip
    // reads one read's records, Cross-framing reads one claim's. So a pass that
    // stopped running, or one that started contradicting a desk wholesale, was
    // visible only to someone who happened to open the right sentence. Folding
    // it into Cross-framing was considered: that panel holds ONE claim still,
    // which is the arrangement that makes its silence readable, and a list
    // would break it.
    //
    // It adds no route and no statistic; every field is the route's own,
    // rendered through `contentionsModel` (the fences print "not measured",
    // never 0). Re-armed at 62, same rule for the next one.
    //
    // UI-B1/shell (W7) — this ceiling is now a SECOND line of the kind plan,
    // not its whole statement: `kindPlan.test.ts` holds the registry to ≤ 33
    // kinds OFF Table B's retirement schedule (UI-D1 §2.2), and this total can
    // only fall as UI-B1/merges and UI-B3 delete the scheduled rows.
    //
    // 62 → 45: UI-B1/merges (W8) — the first merge lane to pay the debt every
    // exception above recorded. Twenty Table B kinds retired into the tabs of
    // the merged wrappers (aliases.ts), three Table A survivors registered
    // (`system.registry`, `system.ledgers`, `system.register`). The ceiling
    // lives in `kindPlan.ts::REGISTRY_TOTAL_CEILING`, lowered in the same
    // commit; the exceptions above stand as the record of why each row came.
    //
    // 45 → 38: UI-B3's subject half (W11) — the eight per-subject reads
    // retired into `subject.page`'s tabs, the one kind it registered.
    //
    // 38 → 39: `system.flow` — a documented exception:
    // a read-only canvas over the descriptors as
    // the working surface for redesigning the agents. It is not observability
    // over the plumbing; it renders the registry's own objects (every edge a
    // declared field, every relation the registry cannot express listed as
    // such), and it is the surface a later "save writes a descriptor version"
    // grows from. Folding it into the Registry's Canvas tab was considered: that
    // tab is the v4 wiring editor (it writes), and the brief for this one is
    // that it never does. It takes one of the two headroom places under the
    // survivor ceiling (33) and seats in the Engine stance beside the Registry.
    expect(Object.keys(PANEL_REGISTRY).length).toBeLessThanOrEqual(REGISTRY_TOTAL_CEILING)
    expect(REGISTRY_TOTAL_CEILING).toBe(39)
  })

  it('the 62nd row is the contentions listing — a catalog kind the Evaluation section mounts', () => {
    // It was hidden for the sidebar budget; the tree is gone (UI-B1/shell), so
    // it is an ordinary `＋` catalog kind with a declared home: the Trust
    // stance seeds it (UI-D1 Table A — the stance rule, `kindPlan.test.ts`).
    if (Object.keys(PANEL_REGISTRY).length >= 62) {
      const c = PANEL_REGISTRY['analysis.contentions']
      expect(c).toBeTruthy()
      expect(c.definition.requiresBinding).toBe(false)
      expect(c.definition.scopeKey).toBeNull()
      expect(isDrill('analysis.contentions')).toBe(false)
      // U-2: its home is the operator side's Evaluation section.
      expect(surfaceKinds(findSurface('evaluation')!)).toContain('analysis.contentions')
    }
  })

  it('the 60th row is cross-framing — a DRILL, so never in the `＋` catalog', () => {
    // Same ratchet shape as the 57th–59th. If the count reaches 60, this row
    // must be why, and the reason has to still hold: it is a DRILL scoped to a
    // claim — opened from the claim it is about, never listed.
    if (Object.keys(PANEL_REGISTRY).length >= 60) {
      const cf = PANEL_REGISTRY['analysis.cross_framing']
      expect(cf).toBeTruthy()
      expect(cf.definition.requiresBinding).toBe(false)
      expect(isDrill('analysis.cross_framing')).toBe(true)
      expect(catalogKinds()).not.toContain('analysis.cross_framing')
    }
  })

  it('the desk brief page EXPIRED as a row: it is the subject page\'s Brief tab', () => {
    // The 61st row's own terms: "the moment a merged desk surface carries it
    // as a tab, this exception expires". `subject.page` carries it.
    expect('target.desk_brief_page' in PANEL_REGISTRY).toBe(false)
    expect(KIND_ALIASES['target.desk_brief_page']).toEqual({ kind: 'subject.page', tab: 'brief' })
    const page = PANEL_REGISTRY['subject.page']
    expect(page.definition.requiresBinding).toBe(true)
    expect(page.definition.scopeKey).toBe('target_id')
  })

  it('the 57th row is the demotion reader, paired with the surface it retires', () => {
    // The pairing is what makes the exception temporary rather than inherited.
    // If the count sits at 57 or above, `v4.morning_read` must be part of why —
    // and its retiree must still be registered, because the moment it is not,
    // the count should have come back down with it.
    if (Object.keys(PANEL_REGISTRY).length >= 57) {
      expect(PANEL_REGISTRY['v4.morning_read']).toBeTruthy()
      expect(PANEL_REGISTRY['v4.assessment']).toBeTruthy()
    }
  })

  it('the 58th row (the Navigator) repaid itself: retired onto the read it opened', () => {
    // The Navigator's exception promised the number would come down. It did:
    // the products index is the object rail's Reads section (UI-B1/shell), so
    // UI-B1/merges retired the kind onto the read. Its cis-mode guarantee —
    // a session never lands on a report with no way to change which report —
    // is the rail's now, and the read it aliases onto ships in both modes.
    expect(PANEL_REGISTRY['system.navigator' as PanelKind]).toBeUndefined()
    expect(resolveKind('system.navigator')).toEqual({ kind: 'v4.morning_read' })
    expect([...PANEL_REGISTRY['v4.morning_read'].definition.modes].sort()).toEqual(['cis', 'personal'])
  })

  it('the 59th row is the divergence map — the unit no other surface can read', () => {
    // Same ratchet shape as the 57th and 58th. If the count reaches 59, this
    // row must be why, and the reason has to still hold: the unit writes no
    // table and suppresses a quiet run to trace-only, so a surface reading
    // `analyst_outputs` sees nothing on the days it worked. The moment Program
    // 6 L2 gains a table that another panel can join, this exception expires.
    if (Object.keys(PANEL_REGISTRY).length >= 59) {
      const map = PANEL_REGISTRY['system.layer_divergence']
      expect(map).toBeTruthy()
      expect(map.definition.requiresBinding).toBe(false)
      expect(isDrill('system.layer_divergence')).toBe(false)
    }
  })

  it('nothing is HIDDEN any more — a drill is derived, never flagged (UI-D1 §2.2)', () => {
    // `HIDDEN_KINDS` held eight kinds for the 26-row sidebar budget. The tree
    // is gone, and "hidden" became a derived property: a kind whose params
    // need a ref is a drill. The one singleton drill left is cross-framing;
    // the optimizer diff became the Optimizer's Diff tab (UI-B1/merges), and
    // the other six former hidden kinds are a catalog kind with a stance
    // (contentions) or retired into a survivor's tab.
    const drills = Object.keys(PANEL_REGISTRY).filter(
      (k) => isDrill(k as PanelKind) && !PANEL_REGISTRY[k as PanelKind].definition.requiresBinding,
    )
    expect(drills.sort()).toEqual(['analysis.cross_framing'])
    for (const kind of [
      'system.optimizer.diff',
      'source.fanout',
      'source.subscription_builder',
      'source.subscription_policy',
      'system.stream_lag',
      'system.wall_movers',
    ]) {
      expect(RETIREMENT_SCHEDULE[kind]?.retired, `${kind} landed on its survivor`).toBe(true)
      expect(resolveKind(kind)?.kind, kind).toBe(RETIREMENT_SCHEDULE[kind].survivor)
    }
  })
})

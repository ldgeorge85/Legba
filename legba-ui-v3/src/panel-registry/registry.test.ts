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
import { resolveKind, resolveRetiredPanelId } from './aliases'
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
    // Was 10 (T1-T10); Target Hypotheses retired with /predictions.
    expect(targets.length).toBeGreaterThanOrEqual(9)
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
  // Every kind U-3/GLASS-2 folded away behind a merged/tabbed survivor.
  const MERGED_AWAY_KINDS = [
    'v4.timeline', // → Timeline's "Events" mode (survivor: system.timeline)
    'v4.why', // → Provenance's "Why" tab (survivor: system.provenance)
    'system.lineage', // → Provenance's "Lineage" tab
    'v4.flow', // → Provenance's "Flow" tab
    'system.situations', // → Provenance's "Trajectory" tab
    'system.narratives', // → Provenance's "Narratives" tab
    'system.alert_center', // → Alerts & Watches' "Triggers" tab
    'system.watchlist', // → Alerts & Watches' "Watches" tab
    'system.escalations', // → Alerts & Watches' "Deliveries" tab
    'system.deep_consult', // → Consult's "Deep" depth
    'system.entity_graph', // → Entities' "Graph" tab
    'system.notable_structure', // → Entities' "Structure" tab
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

  it('the five merged/survivor kinds exist, are visible, and are non-binding singletons', () => {
    const survivors: PanelKind[] = [
      'system.timeline', // Timeline (Events / Validity)
      'system.provenance', // Provenance (Why / Lineage / Flow / Trajectory / Narratives)
      'system.alerts_watches', // Alerts & Watches (Watches / Triggers / Deliveries)
      'system.consult', // Consult (Chat / Deep)
      'system.entities', // Entities (List / Graph / Structure)
    ]
    for (const kind of survivors) {
      const entry = PANEL_REGISTRY[kind]
      expect(entry, `${kind} must exist`).toBeDefined()
      expect(entry.definition.requiresBinding).toBe(false)
      // system.entities is 'operator' category (personal-only per L-108 §6) —
      // still visible, just not hidden.
      expect(entry.definition.hidden).not.toBe(true)
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
 * tile is registered + hidden for a DIFFERENT reason than the U-3 merge
 * aliases above: it's a brand-new capability (not a folded-away original),
 * hidden purely so it doesn't spend the U-3 ≤22-visible-row sidebar budget
 * on a tile that's already on-screen at cold boot with no user action.
 */
describe('U-4 — system.wall_movers is registered, hidden, and still fully reachable', () => {
  it('is a real, non-binding singleton with its own Component', () => {
    const entry = PANEL_REGISTRY['system.wall_movers']
    expect(entry).toBeDefined()
    expect(entry.definition.requiresBinding).toBe(false)
    expect(entry.definition.scopeKey).toBe(null)
    expect(entry.Component).toBeDefined()
  })

  it('is hidden from the sidebar (does not spend the ≤22-row budget)', () => {
    expect(PANEL_REGISTRY['system.wall_movers'].definition.hidden).toBe(true)
    expect(SINGLETON_PANELS).not.toContain('system.wall_movers')
  })

  it('has a unique panel_id that round-trips through PANEL_ID_TO_KIND (so ⌘K / a saved layout referencing it resolves)', () => {
    const panelId = PANEL_REGISTRY['system.wall_movers'].definition.panelId
    expect(PANEL_ID_TO_KIND[panelId]).toBe('system.wall_movers')
  })

  it('ships in both personal and cis modes, same as the full Wall it complements', () => {
    expect(PANEL_REGISTRY['system.wall_movers'].definition.modes).toEqual(
      PANEL_REGISTRY['system.wall'].definition.modes,
    )
  })

  it('adding this kind did not flip system.wall (the full panel) to hidden or binding', () => {
    const wall = PANEL_REGISTRY['system.wall'].definition
    expect(wall.hidden).not.toBe(true)
    expect(wall.requiresBinding).toBe(false)
  })
})

/**
 * GLASS-2 — the three API surfaces that shipped with no consumer now have one.
 *
 * Two of the three (`system.situations`, `system.narratives`) landed as TABS of
 * the merged Provenance panel. They used to be registered-but-hidden rows for
 * exactly the reason the design diagnosed — the ≤23-visible-row budget was
 * spent, and "fold into a tab or hide" was the documented escape hatch. They
 * are now alias rows: the surfaces still render (Provenance's Trajectory and
 * Narratives tabs, unmodified), and a deep-link to either id still lands on its
 * tab, without either costing a catalog row.
 */
describe('GLASS-2 — the unconsumed-API consumers', () => {
  it('the journal gate is a real, VISIBLE, non-binding singleton', () => {
    const entry = PANEL_REGISTRY['system.journal_gate']
    expect(entry).toBeDefined()
    expect(entry.Component).toBeDefined()
    expect(entry.definition.requiresBinding).toBe(false)
    expect(entry.definition.scopeKey).toBe(null)
    expect(entry.definition.hidden).not.toBe(true)
    expect(SINGLETON_PANELS).toContain('system.journal_gate')
  })

  it('the journal gate is personal-only — it applies registry writes', () => {
    expect(PANEL_REGISTRY['system.journal_gate'].definition.modes).toEqual(['personal'])
  })

  it('the two tab-mounted surfaces resolve onto Provenance, on their own tabs', () => {
    expect(resolveKind('system.situations')).toEqual({
      kind: 'system.provenance',
      tab: 'trajectory',
    })
    expect(resolveKind('system.narratives')).toEqual({
      kind: 'system.provenance',
      tab: 'narratives',
    })
  })

  it('folding the two tabs in did not hide their host (system.provenance)', () => {
    const host = PANEL_REGISTRY['system.provenance'].definition
    expect(host.hidden).not.toBe(true)
    expect(host.requiresBinding).toBe(false)
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
    // It is also the only way the unit is READABLE AT ALL. Program 6 L2 writes
    // no table: its series and receipt ride the analyst payload, and a run that
    // fires nothing is suppressed to trace-only so an idempotent re-run does
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
    // 59 → 60: a FIFTH documented exception, wave P lane B's
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
    // untouched by this lane and no visible row was bought.
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
    // 60 → 61: a SIXTH documented exception, wave P lane A's
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
    expect(Object.keys(PANEL_REGISTRY).length).toBeLessThanOrEqual(61)
  })

  it('the 60th row is cross-framing — a hidden drill that costs no sidebar row', () => {
    // Same ratchet shape as the 57th–59th. If the count reaches 60, this row
    // must be why, and the reason has to still hold: it is a DRILL scoped to a
    // claim, so it stays `hidden` and out of `SINGLETON_PANELS`. The moment it
    // gains a sidebar row, the budget in `navGroups.test.ts` must move with it
    // and be argued there — not inherited from this line.
    if (Object.keys(PANEL_REGISTRY).length >= 60) {
      const cf = PANEL_REGISTRY['analysis.cross_framing']
      expect(cf).toBeTruthy()
      expect(cf.definition.requiresBinding).toBe(false)
      expect(cf.definition.hidden).toBe(true)
      expect(SINGLETON_PANELS).not.toContain('analysis.cross_framing')
    }
  })

  it('the 61st row is the desk brief page — the composed brief, finally readable', () => {
    // Same ratchet shape as the 57th, 58th and 59th. If the count reaches 60,
    // this row must be why, and the reason has to still hold: the brief is
    // composed server-side and reachable only as a download, so the surface
    // that renders it is the only way it can be read at all. The moment a
    // merged desk surface carries it as a tab, this exception expires.
    if (Object.keys(PANEL_REGISTRY).length >= 61) {
      const page = PANEL_REGISTRY['target.desk_brief_page']
      expect(page).toBeTruthy()
      expect(page.definition.requiresBinding).toBe(true)
      expect(page.definition.scopeKey).toBe('target_id')
      expect(page.definition.hidden).not.toBe(true)
    }
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

  it('the 58th row is the Navigator — the front door that is not a panel list', () => {
    // Same ratchet shape as the 57th: if the count reaches 58, the Navigator
    // must be why. It is registered in BOTH modes on purpose — a cis session
    // that boots without its navigation rail lands on a report with no way to
    // change which report.
    if (Object.keys(PANEL_REGISTRY).length >= 58) {
      const nav = PANEL_REGISTRY['system.navigator']
      expect(nav).toBeTruthy()
      expect(nav.definition.requiresBinding).toBe(false)
      expect([...nav.definition.modes].sort()).toEqual(['cis', 'personal'])
    }
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
      expect(map.definition.hidden).not.toBe(true)
    }
  })

  it('hidden-but-registered rows are the exception, not the mechanism', () => {
    // Eighteen kinds were hidden before this train — 27% of the catalog. What
    // remains is the set with no survivor to alias onto yet (each named, with
    // its reason, in registry.ts). If this number grows, a retirement was
    // hidden instead of aliased.
    //
    // RAISED 6 → 7 on 2026-09-26 by wave P lane B, for exactly one row:
    // `analysis.cross_framing`. What this check actually guards is a
    // RETIREMENT being hidden instead of aliased — a surface quietly parked
    // rather than pointed at its survivor. This row is not a retirement and has
    // no survivor to point at: it is a drill scoped to a claim, hidden for the
    // same reason `system.optimizer.diff` and `system.wall_movers` are, and
    // hiding it is what keeps the sidebar budget at 26 rather than 27.
    //
    // The ratchet closes again at 7. A row joining this set still has to answer
    // the question it was armed for: is something being retired without an
    // alias?
    const hidden = Object.values(PANEL_REGISTRY).filter((e) => e.definition.hidden === true)
    expect(hidden.length).toBeLessThanOrEqual(7)
  })

  it('the hidden set holds no retirement without an alias', () => {
    // The ratchet's real subject, asserted directly now that its number has
    // moved: every hidden row is either a drill reached from its parent
    // surface, or a kind no shipped surface renders yet. None of them is a
    // retired id — those live in `panel-registry/aliases.ts` as data.
    const hidden = Object.entries(PANEL_REGISTRY)
      .filter(([, e]) => e.definition.hidden === true)
      .map(([kind]) => kind)
    expect(hidden.sort()).toEqual(
      [
        'analysis.cross_framing',
        'source.fanout',
        'source.subscription_builder',
        'source.subscription_policy',
        'system.optimizer.diff',
        'system.stream_lag',
        'system.wall_movers',
      ].sort(),
    )
  })
})

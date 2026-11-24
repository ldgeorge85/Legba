/**
 * applyAction — THE ONE EXECUTOR for `drive` (UI-B1/shell; UI-D1 §3, D5).
 *
 * Every producer that moves the wall compiles to the nine `case/1` actions and
 * runs through this function: ⌘K, the object rail, the `＋` catalog, missions,
 * the URL hash, the panels' own cross-links (the retired `legba:open-*`
 * bridges), and consult's presented cases. There is no second way to open a
 * tile, set the scope, focus a record or move the clock.
 *
 *   | action        | the shell call                                            |
 *   |---------------|-----------------------------------------------------------|
 *   | `open_panel`  | the singleton opener, or the bound opener for a kind that |
 *   |               | takes a subject; ids resolve through `aliases.ts`; extra  |
 *   |               | params land in `state/panelParams` for the opened tile    |
 *   | `arrange`     | the surface switcher (a surface, or a retired stance or   |
 *   |               | preset id, which the shell resolves onto its surface)     |
 *   | `set_scope`   | `useScope.setScope`, members from the shell's projectors   |
 *   | `focus`       | `useSelection.select`                                     |
 *   | `map`         | `useWorldState.setLayers` + the selection-driven camera    |
 *   | `timeline`    | `useWorldState` — THE one clock (D6)                       |
 *   | `run_mission` | a retired mission id: its surface and its window          |
 *   | `narrate`     | nothing — the case rail shows it                          |
 *   | `done`        | nothing — the player seals                                |
 *   | `panel_action`| Q-1's tenth tool: `applyPanelAction(view, action, args)`, |
 *   |               | the view's own contract; unknown view/action is REFUSED   |
 *
 * TWO DRIVERS, ONE GRAMMAR. `driver: 'case'` (consult, through the
 * `workstation_drive` gate) keeps the closed vocabulary's client-side refusals
 * — a kind outside the allowlist is `kind_not_drivable` here too, a layout or a
 * mission the vocabulary does not list is refused, and an arrangement re-opens
 * the presenting Consult tile. `driver: 'operator'` (every human producer) may
 * open any registered kind, any stance, preset or mission — the allowlist is a
 * limit on what CONSULT may drive, not on the operator.
 *
 * The opener-shaped calls close over the Dockview api in `App.tsx`, so the
 * shell REGISTERS them as a port ({@link registerShell}); this module never
 * touches Dockview. It writes only UI stores and tiles: no registry route, no
 * substrate route, and never a write (the `write` command family is executed
 * by the registry, never by the shell — `lib/commands.ts`).
 */

import type { PanelKind } from '@/types'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { resolveKind } from '@/panel-registry/aliases'
import { useScope, emptyMembers, type Scope, type ScopeKind } from '@/state/scope'
import { selectionKindOf, useSelection } from '@/state/selection'
import { useWorldState, type WorldLayer } from '@/v4/world/worldState'
import { scopeFromJournalEntry, scopeFromTarget } from '@/lib/scopeFromReport'
import { setPanelParams } from '@/state/panelParams'
import { resolveSurfaceId } from '@/lib/surfaces'
import { isLegacyPresetId } from '@/lib/layoutPresets'
import { isMapSurfaceKind, mapSurfaceDeclared, noMapSurfaceReason } from '@/lib/subjectGeometry'
import {
  CASE_VOCABULARY,
  isCaseActionName,
  isCaseScopeKind,
  isDrivableKind,
  refArg,
  windowArg,
  type CaseActionName,
  type CaseVocabulary,
  type DriveActionName,
} from '@/lib/caseModel'
import { applyPanelAction } from '@/lib/panelActions'

// ---------------------------------------------------------------------------
// The action — the `case/1` grammar without the gate's stamps.
// ---------------------------------------------------------------------------

/**
 * One drive action: the nine-name `case/1` union (C-1a §2) with its args. A
 * gate-admitted `CaseAction` is one of these plus `seq`, `cites` and
 * `rests_on`; an operator producer emits the bare form. The vocabulary JSON
 * drift test (server side) and {@link isWorkstationAction} are its guards.
 */
export interface WorkstationAction {
  /** The nine, or Q-1's `panel_action` (a consult case only — no operator
   *  producer emits it; {@link isWorkstationAction} still admits the nine alone). */
  action: DriveActionName
  args: Record<string, unknown>
  /** ISO instant a relative `timeline.span` is resolved against. */
  at?: string | null
}

/** Build an action (the producers' constructor). */
export function act(action: CaseActionName, args: Record<string, unknown> = {}, at?: string | null): WorkstationAction {
  return at === undefined ? { action, args } : { action, args, at }
}

export function isWorkstationAction(v: unknown): v is WorkstationAction {
  if (!v || typeof v !== 'object') return false
  const o = v as Record<string, unknown>
  return isCaseActionName(o.action) && !!o.args && typeof o.args === 'object' && !Array.isArray(o.args)
}

export type Driver = 'case' | 'operator'

// ---------------------------------------------------------------------------
// The shell port.
// ---------------------------------------------------------------------------

/** A split relative to an already-open tile (operator-driven opens only). */
export interface OpenPosition {
  referencePanel: string
  direction: 'right' | 'left' | 'above' | 'below' | 'within'
}

/**
 * What the shell hands the executor — every call closes over the Dockview api
 * in `App.tsx`. (C-1b's `CaseShell` port, unchanged in shape, is this.)
 */
export interface ShellPort {
  /** The active deployment mode (mode gating). Undefined = the opener gates. */
  mode?: string
  /** The singleton opener (opening on `tab` when given). Falsy when skipped. */
  openSingleton: (kind: PanelKind, tab?: string, position?: OpenPosition) => unknown
  /** The bound opener — a per-subject or per-analyst read kind on one record,
   *  opening on `tab` when the kind is tabbed (`subject.page`, UI-B3). */
  openBound: (kind: PanelKind, recordId: string, axis: 'target' | 'analyst', tab?: string) => unknown
  /** The surface switcher (the outgoing arrangement is stored). */
  switchStance: (id: string) => void
  /** A retired preset id: the shell lands it on its surface (U-2). */
  applyPreset: (id: string) => void
  /** A retired mission id: its surface and its window (U-2). */
  runMission: (id: string) => void
  /** Consult is in the frame (U-6); a case that moves the view keeps the chat open. */
  reopenConsult: () => void
  /**
   * A record Consult pulls up lands on the STAGE (U-6; brief §12), with a crumb
   * back to the read. Optional: a shell without a stage leaves the record to
   * the selection (the Record sheet follows it).
   */
  showRecord?: (ref: { kind: string; id: string; label?: string }) => void
  /** The stance the wall is on, and its live serialized arrangement. */
  currentStance: () => string | null
  serializeLayout: () => unknown | null
  /** Put the wall back on `stance` with `layout`, without saving the outgoing one. */
  restoreLayout: (stance: string | null, layout: unknown | null) => void
}

let shell: ShellPort | null = null

/** Register the shell's port; returns the unregister fn (an effect cleanup). */
export function registerShell(s: ShellPort): () => void {
  shell = s
  return () => {
    if (shell === s) shell = null
  }
}

export function getShell(): ShellPort | null {
  return shell
}

// ---------------------------------------------------------------------------
// Outcomes and options.
// ---------------------------------------------------------------------------

export type StepOutcome =
  | { state: 'executed' }
  | { state: 'refused'; reason: string }
  | { state: 'skipped'; reason: string }

const EXECUTED: StepOutcome = { state: 'executed' }

/** Side effects the case player owns (they live in its store, not the world's). */
export interface CaseSideEffects {
  /** The `map reset` camera move — the map renderers listen for it. */
  resetCamera: () => void
  /** `timeline at` — the validity Timeline centres its domain on it. */
  setTimelineAt: (atMs: number | null) => void
}

const NOOP_EFFECTS: CaseSideEffects = { resetCamera: () => {}, setTimelineAt: () => {} }

export interface ApplyOptions {
  /** Who is driving. Default `'case'` — the conservative reading. */
  driver?: Driver
  effects?: CaseSideEffects
  vocab?: CaseVocabulary
  /** The `origin` stamped on a scope or selection this action writes. */
  origin?: string
}

function modeAllows(kind: PanelKind, mode: string | undefined): boolean {
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def) return false
  if (mode === undefined) return true
  return def.modes.length === 0 || def.modes.includes(mode as never)
}

/**
 * Members come from the SHELL'S projectors, never from the action (C-1a §2):
 * a desk scopes to itself, a journal entry to its own entry, and anything whose
 * world is projected from a payload scopes DEGRADED — an empty member set
 * filters nothing and the Navigator re-projects the full world when its rows
 * arrive, the contract the `#scope=` share link keeps.
 */
export function scopeForAction(
  kind: Exclude<ScopeKind, 'none'>,
  id: string,
  label?: string,
  origin = 'case',
): Scope {
  if (kind === 'target') return scopeFromTarget(id, label, origin)
  if (kind === 'journal_entry') return scopeFromJournalEntry(id, label, origin)
  return { kind, id, label: label ?? id, members: emptyMembers(), origin }
}

/** The ref kind a kind's `subject` must be, or undefined for a singleton. */
function subjectKindFor(kind: PanelKind, requested: string, vocab: CaseVocabulary): string | undefined {
  const declared = vocab.subjectKind[kind] ?? vocab.subjectKind[requested]
  if (declared) return declared
  const def = PANEL_REGISTRY[kind]?.definition
  if (!def?.requiresBinding) return undefined
  return def.category === 'analyst' ? 'analyst' : 'target'
}

const RESERVED_PARAMS = new Set(['tab', 'position'])

function extraParams(params: Record<string, unknown>): Record<string, unknown> | null {
  const out: Record<string, unknown> = {}
  let any = false
  for (const [k, v] of Object.entries(params)) {
    if (RESERVED_PARAMS.has(k)) continue
    out[k] = v
    any = true
  }
  return any ? out : null
}

function positionArg(v: unknown): OpenPosition | undefined {
  if (!v || typeof v !== 'object') return undefined
  const p = v as Record<string, unknown>
  const dirs = ['right', 'left', 'above', 'below', 'within']
  return typeof p.referencePanel === 'string' && typeof p.direction === 'string' && dirs.includes(p.direction)
    ? { referencePanel: p.referencePanel, direction: p.direction as OpenPosition['direction'] }
    : undefined
}

// ---------------------------------------------------------------------------
// The executor.
// ---------------------------------------------------------------------------

/**
 * Run one action against the shell. Pure of the player: the caller records the
 * outcome. Never throws — a shell call that throws becomes a `skipped` step
 * with its message, because one broken tile must not abort a presentation, a
 * mission or a link.
 */
export function applyAction(
  a: WorkstationAction,
  opener: ShellPort | null = shell,
  opts: ApplyOptions = {},
): StepOutcome {
  try {
    return run(a, opener, opts)
  } catch (err) {
    return { state: 'skipped', reason: err instanceof Error ? err.message : String(err) }
  }
}

function run(a: WorkstationAction, sh: ShellPort | null, opts: ApplyOptions): StepOutcome {
  const driver: Driver = opts.driver ?? 'case'
  const effects = opts.effects ?? NOOP_EFFECTS
  const vocab = opts.vocab ?? CASE_VOCABULARY
  const origin = opts.origin ?? (driver === 'case' ? 'case' : 'operator')
  const g = a.args
  switch (a.action) {
    case 'open_panel': {
      const requested = typeof g.kind === 'string' ? g.kind : ''
      // A recorded id may since have been retired: it resolves through the
      // alias table (the fourth call site). For a case the allowlist is checked
      // on the id AS RECORDED or on the survivor — never on neither.
      const alias = requested ? resolveKind(requested) : undefined
      // UI-B4/map: a profile that declares no map geometry has no map surface
      // to open — refused by name, for either driver (UI-D1 §2.4).
      if ((isMapSurfaceKind(requested) || isMapSurfaceKind(alias?.kind)) && !mapSurfaceDeclared()) {
        return { state: 'refused', reason: noMapSurfaceReason() }
      }
      if (driver === 'case' && !isDrivableKind(requested, vocab) && !(alias && isDrivableKind(alias.kind, vocab))) {
        return { state: 'refused', reason: `kind_not_drivable: ${requested || '(none)'}` }
      }
      if (!alias) {
        return {
          state: 'skipped',
          reason: driver === 'case' ? `retired kind with no alias: ${requested}` : `unknown kind: ${requested || '(none)'}`,
        }
      }
      const kind = alias.kind
      const params = g.params && typeof g.params === 'object' ? (g.params as Record<string, unknown>) : {}
      const subject = refArg(params.subject)
      // The drill's own params go to the tile BEFORE the shell is asked, so a
      // tile that opens later (or a shell that is not mounted yet) still finds
      // them — the guarantee the `window` parks gave, without the park.
      const extra = extraParams(params)
      if (kind === 'analysis.cross_framing' && !(driver === 'operator' && !subject)) {
        // A case must name the claim's desk. The operator may open the drill
        // bare — it then reads the wall scope's desk, as it always has.
        if (!subject) return { state: 'refused', reason: 'missing subject for analysis.cross_framing' }
        if (subject.kind !== 'target') {
          return { state: 'skipped', reason: `cross-framing opens on a desk; got ${subject.kind}` }
        }
        setPanelParams(kind, { ...(extra ?? {}), origin: typeof params.origin === 'string' ? params.origin : origin })
      } else if (extra && !PANEL_REGISTRY[kind].definition.requiresBinding) {
        setPanelParams(kind, extra)
      }
      if (!sh) return { state: 'skipped', reason: 'no workstation shell to drive' }
      if (!modeAllows(kind, sh.mode)) {
        return { state: 'skipped', reason: `mode-gated: ${kind} does not ship in ${sh.mode}` }
      }
      const def = PANEL_REGISTRY[kind].definition
      const wants = subjectKindFor(kind, requested, vocab)
      if (kind !== 'analysis.cross_framing' && (def.requiresBinding || wants)) {
        if (!subject) return { state: 'refused', reason: `missing subject for ${kind}` }
        if (wants && subject.kind !== wants) {
          return { state: 'refused', reason: `invalid_arg: ${kind} takes a ${wants} subject, not ${subject.kind}` }
        }
      }
      // A case's tab is honoured only when the vocabulary names it for this
      // kind; an operator's is passed through (the panel ignores a tab it does
      // not have). The retired id's own tab (the alias) is the default.
      const askedTab = typeof params.tab === 'string' ? params.tab : undefined
      const tab =
        askedTab && (driver === 'operator' || (vocab.tabs[kind] ?? []).includes(askedTab)) ? askedTab : alias.tab
      if (kind !== 'analysis.cross_framing' && (def.requiresBinding || wants)) {
        const axis = kind.startsWith('analyst.') ? 'analyst' : 'target'
        // A tabbed bound survivor (`subject.page`) opens on its tab; the
        // other bound kinds take none, and are called exactly as before.
        if (tab) sh.openBound(kind, subject!.id, axis, tab)
        else sh.openBound(kind, subject!.id, axis)
        return EXECUTED
      }
      const position = driver === 'operator' ? positionArg(params.position) : undefined
      const opened = position ? sh.openSingleton(kind, tab, position) : sh.openSingleton(kind, tab)
      return opened ? EXECUTED : { state: 'skipped', reason: `not opened: ${kind}` }
    }

    case 'arrange': {
      const layout = typeof g.layout === 'string' ? g.layout : ''
      if (!sh) return { state: 'skipped', reason: 'no workstation shell to drive' }
      const stances: readonly string[] = driver === 'case' ? vocab.stances : []
      const isStance = (id: string) => (driver === 'case' ? stances.includes(id) : resolveSurfaceId(id) != null)
      const isPreset = (id: string) => (driver === 'case' ? vocab.presets.includes(id) : isLegacyPresetId(id))
      // The canonical args carry `layout_type`; an id is classified when a
      // record predates it. An id the driver may not arrange is refused.
      const type =
        g.layout_type === 'stance' || g.layout_type === 'preset' ? g.layout_type : isStance(layout) ? 'stance' : 'preset'
      if (type === 'stance' && isStance(layout)) {
        sh.switchStance(layout)
      } else if (type === 'preset' && isPreset(layout)) {
        sh.applyPreset(layout)
      } else {
        return { state: 'refused', reason: `invalid_arg: layout ${layout || '(none)'}` }
      }
      // A CASE's arrangement replaces the dock under the presenting Consult
      // tile; it comes back so the rail — and its STOP — stays on screen.
      if (driver === 'case') sh.reopenConsult()
      return EXECUTED
    }

    case 'set_scope': {
      const kind = g.kind
      if (!isCaseScopeKind(kind)) return { state: 'refused', reason: `invalid_arg: scope kind ${String(kind)}` }
      if (kind === 'none') {
        useScope.getState().setScope(null)
        return EXECUTED
      }
      const id = typeof g.id === 'string' ? g.id : ''
      if (!id) return { state: 'refused', reason: 'invalid_arg: scope id' }
      const label = typeof g.label === 'string' && g.label ? g.label : undefined
      useScope.getState().setScope(scopeForAction(kind, id, label, origin))
      return EXECUTED
    }

    case 'focus': {
      const ref = refArg(g.ref)
      if (!ref) return { state: 'refused', reason: 'invalid_arg: ref' }
      const label = typeof g.label === 'string' && g.label ? g.label : undefined
      useSelection.getState().select({ kind: selectionKindOf(ref.kind), id: ref.id, label, origin })
      // Consult pulling up a record: it appears on the stage (a subject is the
      // point, not a record — it re-scopes the page instead).
      if (driver === 'case' && ref.kind !== 'target' && sh?.showRecord) sh.showRecord({ kind: ref.kind, id: ref.id, label })
      return EXECUTED
    }

    case 'map': {
      // UI-B4/map: the profile is consulted first — `map.geometry: none` is a
      // refusal by name (C-1a §2), not a camera move nobody can see.
      if (!mapSurfaceDeclared()) return { state: 'refused', reason: noMapSurfaceReason() }
      // A profile that declares no map, or a mode that does not ship it,
      // refuses by name rather than moving a camera nobody can see.
      const surface = vocab.mapSurfaceKind as PanelKind | null
      if (!surface || !sh || !modeAllows(surface, sh.mode)) {
        return { state: 'skipped', reason: 'no_map_surface' }
      }
      const layers = (Array.isArray(g.layers) ? g.layers : []).filter(
        (l): l is WorldLayer => typeof l === 'string' && vocab.layers.includes(l),
      )
      // Wholesale, and only when claimed — an empty list is no claim.
      if (layers.length > 0) useWorldState.getState().setLayers(layers)
      if (g.view === 'fit_ref') {
        const ref = refArg(g.ref)
        if (!ref) return { state: 'refused', reason: 'invalid_arg: fit_ref needs a ref' }
        // The selection-driven camera move: the map flies to what is
        // selected, from the record's own geometry, or does not move at all.
        useSelection.getState().select({ kind: selectionKindOf(ref.kind), id: ref.id, origin })
      } else if (g.view === 'reset') {
        effects.resetCamera()
      }
      return EXECUTED
    }

    case 'timeline': {
      const w = windowArg(g, a.at ?? null)
      if (!w) return { state: 'refused', reason: 'invalid_arg: window' }
      // THE ONE CLOCK (D6). An operator posture (a mission) declares a SPAN:
      // the scrubber's outer span re-anchors with the window, exactly as the
      // mission chooser always did. A case writes the absolute window only.
      if (driver === 'operator' && typeof g.span === 'string') {
        useWorldState.getState().setSpan(w.endMs - w.startMs, w.endMs)
      } else {
        useWorldState.getState().setWindow(w.startMs, w.endMs)
      }
      const atMs = typeof g.at === 'string' ? Date.parse(g.at) : NaN
      effects.setTimelineAt(Number.isFinite(atMs) ? atMs : null)
      return EXECUTED
    }

    case 'run_mission': {
      const name = typeof g.name === 'string' ? g.name : ''
      if (driver === 'case' && !vocab.missions.includes(name)) {
        return { state: 'refused', reason: `invalid_arg: mission ${name || '(none)'}` }
      }
      if (!name) return { state: 'refused', reason: 'invalid_arg: mission (none)' }
      if (!sh) return { state: 'skipped', reason: 'no workstation shell to drive' }
      sh.runMission(name)
      if (driver === 'case') sh.reopenConsult()
      return EXECUTED
    }

    case 'narrate':
    case 'done':
      return EXECUTED

    case 'panel_action': {
      // Q-1 — `{view, action, args}` through the view's own contract. Every
      // refusal names what was wrong, and the case rail shows it: an unknown
      // view (`unknown view: …`), an action the view does not accept
      // (`action_not_accepted: …`), a bad argument (`invalid_arg: …`).
      const view = typeof g.view === 'string' ? g.view.trim() : ''
      const name = typeof g.action === 'string' ? g.action.trim() : ''
      if (!view || !name) {
        return { state: 'refused', reason: `invalid_arg: panel_action needs a view and an action (got view ${view || '—'}, action ${name || '—'})` }
      }
      const inner = g.args
      if (inner !== undefined && inner !== null && (typeof inner !== 'object' || Array.isArray(inner))) {
        return { state: 'refused', reason: `invalid_arg: panel_action args must be an object` }
      }
      return applyPanelAction(view, name, (inner as Record<string, unknown> | null | undefined) ?? {}, { origin })
    }
  }
}

/**
 * The operator's entry point: run one action or a list through the registered
 * shell, in order, as `driver: 'operator'`. Every human producer calls this.
 */
export function drive(
  actions: WorkstationAction | readonly WorkstationAction[],
  opts: Omit<ApplyOptions, 'driver'> = {},
): StepOutcome[] {
  const list = Array.isArray(actions) ? actions : [actions as WorkstationAction]
  return list.map((a) => applyAction(a, shell, { ...opts, driver: 'operator' }))
}

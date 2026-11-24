/**
 * address — the URL as an action list (UI-B1/shell; UI-D1 §3, §5).
 *
 * A SAVED VIEW IS A LINK (U-2 shell; brief §3–§4: "a point, a window, a
 * surface and its filters, kept as a link, not as a layout blob"). The hash
 * params compile, in this fixed order, to drive actions:
 *
 *   `#ws=<surface>`         → `arrange {layout, layout_type: 'stance'}` — a
 *                             surface id (`board`, `subject`, …) or a retired
 *                             workspace id, which resolves to the nearest
 *                             surface (`lib/surfaces.ts::resolveSurfaceId`)
 *   `#scope=<kind>:<id>`    → `set_scope {kind, id}`
 *   `#sel=<kind>:<id>`      → `focus {ref}`
 *   `#win=<span>`           → `timeline {start, end, span}` — the window, one
 *                             of the scrubber's spans (6h, 24h, 7d, 30d)
 *   `#mission=<id>`         → `run_mission {name}` — retired ids only, kept so
 *                             an old link still lands on the nearest surface
 *
 * and one param names a recorded case to REPLAY rather than actions to run:
 *
 *   `#case=<session>:<turn>` (the phone's `?case=` reads the same way)
 *
 * where `<turn>` is the persisted turn's id or, failing that, its 0-based
 * position in the session.
 *
 * The PHONE shell (M-2, `/m/`) reads this same grammar and adds ONE key for
 * the screens a phone has and the desktop does not ({@link PHONE_SCREEN_KEY}):
 *
 *   `#screen=<name>`        `consult`, `map`, `settings`, `proof`,
 *                           `read:<finding>`, `alert:<episode>`,
 *                           `subject:<subject>` (`mobile/shell/phoneAddress.ts`)
 *
 * The desktop ignores it, so a phone link opened at a desk lands on the
 * surface its `#ws=` names, with the same point and window. Together with
 * U-10's `#rec=` (the Record) these are every key either shell writes.
 *
 * Parsing is total and defensive: an id the shell
 * does not know drops that one action, never the whole address. Arrangement
 * is never part of an address.
 *
 * The inverse, {@link addressFor}, writes the same params from an action list,
 * so an address round-trips. `lib/shareState.ts` remains the hash's WRITER
 * (it mirrors the stores) and `App.tsx` executes the list.
 */
import { act, type WorkstationAction } from '@/lib/applyAction'
import { resolveSurfaceId } from '@/lib/surfaces'
import { isMissionId } from '@/lib/missions'
import { isCaseScopeKind } from '@/lib/caseModel'
import { SPAN_PRESETS } from '@/lib/mapTime'

/** The phone's screen key (M-2): a phone screen with no desktop page of its own. */
export const PHONE_SCREEN_KEY = 'screen'

/** The selection kinds a link may focus (`#sel=`). */
export const ADDRESS_SEL_KINDS: ReadonlySet<string> = new Set([
  'target',
  'entity',
  'source',
  'analyst',
  'finding',
  'situation',
  'signal',
  'report',
  'journal_entry',
])

/** A case a link asks to replay. */
export interface CaseAddress {
  session: string
  /** The turn id, or a 0-based index into the session's turns. */
  turn: string
}

export interface ParsedAddress {
  /** In the fixed order: arrange, set_scope, focus, timeline, run_mission. */
  actions: WorkstationAction[]
  case: CaseAddress | null
}

function params(raw: string): URLSearchParams {
  try {
    return new URLSearchParams(raw.replace(/^[#?]/, ''))
  } catch {
    return new URLSearchParams()
  }
}

/** `<kind>:<id>` → its halves, the id URI-decoded; null when malformed. */
function kindId(raw: string | null): { kind: string; id: string } | null {
  if (!raw) return null
  const idx = raw.indexOf(':')
  if (idx <= 0) return null
  let id = raw.slice(idx + 1)
  try {
    id = decodeURIComponent(id)
  } catch {
    return null
  }
  return id ? { kind: raw.slice(0, idx), id } : null
}

function caseParam(raw: string | null): CaseAddress | null {
  if (!raw) return null
  const idx = raw.lastIndexOf(':')
  if (idx <= 0 || idx === raw.length - 1) return null
  return { session: raw.slice(0, idx), turn: raw.slice(idx + 1) }
}

/** The window a `#win=` span names, ending at `now`; null for an unknown span. */
export function windowAction(span: string, now = Date.now()): WorkstationAction | null {
  const preset = SPAN_PRESETS.find((p) => p.id === span)
  if (!preset) return null
  return act('timeline', {
    start: new Date(now - preset.ms).toISOString(),
    end: new Date(now).toISOString(),
    span: preset.id,
  })
}

/**
 * Compile an address to its action list. Reads the hash first, then the query
 * (the mobile surface's grammar) for `case`.
 */
export function parseAddress(hash: string, search = '', now = Date.now()): ParsedAddress {
  const h = params(hash)
  const q = params(search)
  const actions: WorkstationAction[] = []

  const ws = h.get('ws')
  if (ws && resolveSurfaceId(ws)) actions.push(act('arrange', { layout: ws, layout_type: 'stance' }))

  const scope = kindId(h.get('scope'))
  if (scope && scope.kind !== 'none' && isCaseScopeKind(scope.kind)) {
    actions.push(act('set_scope', { kind: scope.kind, id: scope.id }))
  }

  const sel = kindId(h.get('sel'))
  if (sel && ADDRESS_SEL_KINDS.has(sel.kind)) actions.push(act('focus', { ref: { kind: sel.kind, id: sel.id } }))

  const win = h.get('win')
  const w = win ? windowAction(win, now) : null
  if (w) actions.push(w)

  const mission = h.get('mission')
  if (mission && isMissionId(mission)) actions.push(act('run_mission', { name: mission }))

  return { actions, case: caseParam(h.get('case')) ?? caseParam(q.get('case')) }
}

/** The action of a given name in a parsed list, if any. */
export function actionOf(list: readonly WorkstationAction[], name: WorkstationAction['action']) {
  return list.find((a) => a.action === name)
}

/**
 * Write an action list back as an address (the inverse of {@link parseAddress}).
 * Only the addressable actions are written; anything else has no URL form — a
 * URL is a saved view, not a recording.
 */
export function addressFor(actions: readonly WorkstationAction[], caseRef: CaseAddress | null = null): string {
  const p = new URLSearchParams()
  for (const a of actions) {
    const g = a.args
    if (a.action === 'arrange' && typeof g.layout === 'string' && g.layout_type !== 'preset') p.set('ws', g.layout)
    if (a.action === 'set_scope' && typeof g.kind === 'string' && typeof g.id === 'string' && g.kind !== 'none') {
      p.set('scope', `${g.kind}:${encodeURIComponent(g.id)}`)
    }
    if (a.action === 'focus' && g.ref && typeof g.ref === 'object') {
      const r = g.ref as Record<string, unknown>
      if (typeof r.kind === 'string' && typeof r.id === 'string') p.set('sel', `${r.kind}:${encodeURIComponent(r.id)}`)
    }
    if (a.action === 'timeline' && typeof g.span === 'string' && SPAN_PRESETS.some((s) => s.id === g.span)) {
      p.set('win', g.span)
    }
    if (a.action === 'run_mission' && typeof g.name === 'string') p.set('mission', g.name)
  }
  if (caseRef) p.set('case', `${caseRef.session}:${caseRef.turn}`)
  const q = p.toString()
  return q ? `#${q}` : ''
}

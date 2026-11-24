/**
 * phoneAddress — where the phone is, read from and written to the URL (M-2).
 *
 * ONE ADDRESS GRAMMAR (plan §3.3). The phone reads the desktop's hash keys
 * through `lib/address.ts` and adds only what a phone screen needs:
 *
 *   `#ws=<surface>`        the tab: `board` → Changed, `watch` → Watch,
 *                          `ledger` → Ledger, `subject` → Subjects (the
 *                          desktop's own surface ids, so a desktop link lands
 *                          on the same place)
 *   `#sel=`, `#scope=`, `#win=`   the point, the scope, the window — exactly
 *                          the desktop's (`parseAddress` → `drive`)
 *   `#rec=<kind>:<id>&cite=<n>`   the Record (U-10's key): a full page over the
 *                          screen; with `peek=1`, the peek sheet instead
 *   `#screen=<name>`       a phone screen with no desktop page of its own
 *                          (`lib/address.ts::PHONE_SCREEN_KEY`): `consult`,
 *                          `map`, `settings`, `proof`, `read:<finding id>`,
 *                          `alert:<episode id>`, `subject:<subject id>`
 *
 * THE HISTORY IS THE STACK. Every drill-in is a `pushState` of the address
 * of the screen it opens; Back (the top bar's, the browser's, Android's
 * hardware key) is `history.back()`, and the screen shown is always the one
 * the current address names. So Back works from every drill-in and a link
 * opens exactly the screen it names. Switching tabs REPLACES the entry: the
 * bottom bar is not a history.
 *
 * Pure except for {@link navigate} and {@link back}.
 */
import { PHONE_SCREEN_KEY } from '@/lib/address'
import { CHAIN_KINDS, type RecordRef } from '@/lib/recordSheet'

export type PhoneTab = 'changed' | 'watch' | 'ledger' | 'consult' | 'subjects'

/** The five, in the bar's order (plan §2.1). */
export const PHONE_TABS: readonly PhoneTab[] = ['changed', 'watch', 'ledger', 'consult', 'subjects']

/** The default tab — the landing (the owner: "we need a landing page, but also a consult tab"). */
export const DEFAULT_TAB: PhoneTab = 'changed'

/** A tab's desktop surface (`#ws=`); Consult has none. */
export const TAB_SURFACE: Record<PhoneTab, string | null> = {
  changed: 'board',
  watch: 'watch',
  ledger: 'ledger',
  consult: null,
  subjects: 'subject',
}

export function tabOfSurface(surface: string | null | undefined): PhoneTab | null {
  for (const t of PHONE_TABS) if (TAB_SURFACE[t] && TAB_SURFACE[t] === surface) return t
  return null
}

/** A page pushed over a tab's root. */
export type PhonePage =
  | { kind: 'read'; id: string }
  | { kind: 'alert'; id: string }
  | { kind: 'subject'; id: string }
  | { kind: 'map' }
  | { kind: 'proof' }
  | { kind: 'settings' }

export interface PhoneScreen {
  tab: PhoneTab
  page: PhonePage | null
  /** The Record, over the page (a full page, or the peek sheet). */
  record: { ref: RecordRef; cite: number | null } | null
  peek: boolean
}

/** The tab a page belongs to when an address names the page alone. */
const PAGE_TAB: Record<PhonePage['kind'], PhoneTab | null> = {
  read: 'changed',
  alert: 'watch',
  subject: 'subjects',
  proof: 'ledger',
  map: null,
  settings: null,
}

function params(hash: string): URLSearchParams {
  try {
    return new URLSearchParams(hash.replace(/^#/, ''))
  } catch {
    return new URLSearchParams()
  }
}

function idPart(raw: string, prefix: string): string | null {
  if (!raw.startsWith(prefix)) return null
  let id = raw.slice(prefix.length)
  try {
    id = decodeURIComponent(id)
  } catch {
    return null
  }
  return id ? id : null
}

/** `#screen=` → a page, the Consult tab, or nothing. */
export function parseScreen(raw: string | null): { page: PhonePage | null; consult: boolean } {
  if (!raw) return { page: null, consult: false }
  if (raw === 'consult') return { page: null, consult: true }
  if (raw === 'map' || raw === 'settings' || raw === 'proof') return { page: { kind: raw }, consult: false }
  for (const kind of ['read', 'alert', 'subject'] as const) {
    const id = idPart(raw, `${kind}:`)
    if (id) return { page: { kind, id }, consult: false }
  }
  return { page: null, consult: false }
}

export function screenParam(page: PhonePage | null, consult: boolean): string | null {
  if (consult) return 'consult'
  if (!page) return null
  if (page.kind === 'read' || page.kind === 'alert' || page.kind === 'subject') return `${page.kind}:${encodeURIComponent(page.id)}`
  return page.kind
}

/** The screen an address names. Total: anything unreadable falls back to the landing. */
export function phoneScreenOf(hash: string): PhoneScreen {
  const p = params(hash)
  const { page, consult } = parseScreen(p.get(PHONE_SCREEN_KEY))
  const fromWs = tabOfSurface(p.get('ws'))
  const tab: PhoneTab = consult ? 'consult' : fromWs ?? (page ? PAGE_TAB[page.kind] : null) ?? DEFAULT_TAB
  let record: PhoneScreen['record'] = null
  const rec = p.get('rec')
  if (rec) {
    const at = rec.indexOf(':')
    const kind = at > 0 ? rec.slice(0, at) : ''
    let id = at > 0 ? rec.slice(at + 1) : ''
    try {
      id = decodeURIComponent(id)
    } catch {
      id = ''
    }
    if (id && CHAIN_KINDS.has(kind)) {
      const n = Number(p.get('cite'))
      record = { ref: { kind, id }, cite: Number.isInteger(n) && n >= 1 && n < 1000 ? n : null }
    }
  }
  return { tab, page: consult ? null : page, record, peek: record !== null && p.get('peek') === '1' }
}

/**
 * The address of a screen, KEEPING the desktop keys already in `hash` that
 * are not the screen's own (`#sel=`, `#scope=`, `#win=`), so the point and
 * the window ride every phone address.
 */
export function addressOfScreen(s: PhoneScreen, hash = ''): string {
  const p = params(hash)
  for (const k of ['ws', PHONE_SCREEN_KEY, 'rec', 'cite', 'peek']) p.delete(k)
  const ws = TAB_SURFACE[s.tab]
  if (ws) p.set('ws', ws)
  const screen = screenParam(s.tab === 'consult' ? null : s.page, s.tab === 'consult')
  if (screen) p.set(PHONE_SCREEN_KEY, screen)
  if (s.record) {
    p.set('rec', `${s.record.ref.kind}:${encodeURIComponent(s.record.ref.id)}`)
    if (s.record.cite !== null) p.set('cite', String(s.record.cite))
    if (s.peek) p.set('peek', '1')
  }
  // stable order: the screen's keys first, then the desktop's
  const order = ['ws', PHONE_SCREEN_KEY, 'rec', 'cite', 'peek']
  const out = new URLSearchParams()
  for (const k of order) {
    const v = p.get(k)
    if (v !== null) out.set(k, v)
  }
  for (const [k, v] of p) if (!order.includes(k)) out.append(k, v)
  const q = out.toString()
  return q ? `#${q}` : ''
}

/** How deep a screen is: 0 a tab's root, 1 a page, 2 the Record over a page. */
export function depthOf(s: PhoneScreen): number {
  return (s.page ? 1 : 0) + (s.record ? 1 : 0)
}

/** The screen one step up (what a seeded history puts under a deep link). */
export function parentOf(s: PhoneScreen): PhoneScreen | null {
  if (s.record && s.peek) return { ...s, record: null, peek: false }
  if (s.record) return { ...s, record: null, peek: false }
  if (s.page) return { ...s, page: null }
  return null
}

// ---------------------------------------------------------------------------
// Navigation (effectful).
// ---------------------------------------------------------------------------

/** Fired after every phone navigation, so the shell re-reads the address. */
export const PHONE_NAV_EVENT = 'legba:phone-nav'

function emit(): void {
  try {
    window.dispatchEvent(new Event(PHONE_NAV_EVENT))
  } catch {
    // no window (a test without a DOM): nothing listens
  }
}

/**
 * Go to a screen. `push` adds a history entry (a drill-in: Back returns);
 * `replace` swaps the current one (a tab switch, a filter, a point).
 */
export function navigate(s: PhoneScreen, how: 'push' | 'replace' = 'push'): void {
  try {
    const next = addressOfScreen(s, window.location.hash)
    const url = `${window.location.pathname}${window.location.search}${next}`
    if (how === 'push' && next !== window.location.hash) window.history.pushState({ phone: depthOf(s) }, '', url)
    else window.history.replaceState({ phone: depthOf(s) }, '', url)
  } catch {
    // history unavailable: nothing moves
  }
  emit()
}

/** Back one screen (the top bar's back, the same as the browser's). */
export function back(): void {
  try {
    window.history.back()
  } catch {
    // nothing to go back to
  }
}

/**
 * A deep link opened cold has no entries under it, so Back would leave the
 * app. Seed the chain: the parents are replaced in underneath, the link
 * itself pushed on top — Back then walks up one screen at a time.
 */
export function seedHistory(): void {
  try {
    const here = phoneScreenOf(window.location.hash)
    const chain: PhoneScreen[] = [here]
    for (let up = parentOf(here); up; up = parentOf(up)) chain.unshift(up)
    if (chain.length < 2) return
    const hash = window.location.hash
    const base = `${window.location.pathname}${window.location.search}`
    window.history.replaceState({ phone: 0 }, '', base + addressOfScreen(chain[0], hash))
    for (const s of chain.slice(1)) window.history.pushState({ phone: depthOf(s) }, '', base + addressOfScreen(s, hash))
  } catch {
    // best-effort: the link still opens
  }
}

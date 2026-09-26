/**
 * Export collection basket (A10) — the cross-panel "add to export" store.
 *
 * The operator collects findings / analyst reports / journal entries from
 * wherever selection already flows (the Inspector's selected record, a feed
 * row's hover action, a Journal entry card) into ONE persistent basket, then
 * composes the export in the Report Export panel (`system.report_export`),
 * which POSTs the basket to `POST /api/v1/v3/export` for server-side
 * full-fidelity composition (markdown / JSON).
 *
 * Mirrors the consult panel's pin-to-context pattern (#90) — a sticky,
 * operator-curated set fed from the shared selection — but lifted into a
 * global zustand store (consult's pins are panel-local state) and persisted
 * to localStorage so the basket survives a reload mid-collection.
 *
 * The basket is capped at `BASKET_MAX_ITEMS` (mirrors the server's
 * `EXPORT_MAX_ITEMS` — the route answers an honest 413 beyond it); `add()`
 * returns false when the cap or a duplicate rejects the item so the caller's
 * affordance can say so instead of silently no-oping.
 *
 * DOM-free logic (parse/serialize/dedupe/cap) is exported for unit tests.
 *
 * `buildDeskBrief` (7b-iii, below) is the one-click "Desk brief" action: it
 * fills the basket with a desk's current picture and returns the
 * open-situations/tracked-events text the export route's `appendix` field
 * carries — see its own docstring.
 */
import { create } from 'zustand'
import { apiGet } from '@/lib/api'
import { projectAssembly, type ReadFindingRow } from '@/lib/assemblyModel'

/** The two exportable kinds the server route accepts. */
export type ExportItemKind = 'finding' | 'journal_entry'

export interface ExportBasketItem {
  kind: ExportItemKind
  id: string
  /** Human label for the basket list (falls back to the id when absent). */
  label?: string
}

/** Client cap — mirrors the server route's EXPORT_MAX_ITEMS (50). */
export const BASKET_MAX_ITEMS = 50

const STORAGE_KEY = 'legba_export_basket_v1'

/** Defensive parse of the persisted basket — malformed/unknown entries are
 *  dropped (never a crash on a stale localStorage shape), the cap enforced. */
export function parseBasket(raw: string | null): ExportBasketItem[] {
  if (!raw) return []
  let parsed: unknown
  try {
    parsed = JSON.parse(raw)
  } catch {
    return []
  }
  if (!Array.isArray(parsed)) return []
  const out: ExportBasketItem[] = []
  const seen = new Set<string>()
  for (const entry of parsed) {
    if (!entry || typeof entry !== 'object') continue
    const e = entry as Record<string, unknown>
    if (e.kind !== 'finding' && e.kind !== 'journal_entry') continue
    if (typeof e.id !== 'string' || !e.id) continue
    const key = `${e.kind}:${e.id}`
    if (seen.has(key)) continue
    seen.add(key)
    out.push({
      kind: e.kind,
      id: e.id,
      label: typeof e.label === 'string' && e.label ? e.label : undefined,
    })
    if (out.length >= BASKET_MAX_ITEMS) break
  }
  return out
}

/** Pure add — dedupe on (kind, id), cap at BASKET_MAX_ITEMS. Returns the SAME
 *  array reference when the item was rejected so callers can detect it. */
export function addToBasket(
  items: ExportBasketItem[],
  item: ExportBasketItem,
): ExportBasketItem[] {
  if (items.some((i) => i.kind === item.kind && i.id === item.id)) return items
  if (items.length >= BASKET_MAX_ITEMS) return items
  return [...items, item]
}

export function removeFromBasket(
  items: ExportBasketItem[],
  kind: ExportItemKind,
  id: string,
): ExportBasketItem[] {
  return items.filter((i) => !(i.kind === kind && i.id === id))
}

function loadInitial(): ExportBasketItem[] {
  try {
    return parseBasket(localStorage.getItem(STORAGE_KEY))
  } catch {
    // localStorage unavailable (SSR / privacy mode) — start empty.
    return []
  }
}

function persist(items: ExportBasketItem[]): void {
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify(items))
  } catch {
    // Best-effort persistence — the in-memory basket still works.
  }
}

interface ExportBasketState {
  items: ExportBasketItem[]
  /** Add one item; false when rejected (duplicate or basket full). */
  add: (item: ExportBasketItem) => boolean
  remove: (kind: ExportItemKind, id: string) => void
  clear: () => void
  has: (kind: ExportItemKind, id: string) => boolean
}

export const useExportBasket = create<ExportBasketState>((set, get) => ({
  items: loadInitial(),
  add: (item) => {
    const prev = get().items
    const next = addToBasket(prev, item)
    if (next === prev) return false
    persist(next)
    set({ items: next })
    return true
  },
  remove: (kind, id) => {
    const next = removeFromBasket(get().items, kind, id)
    persist(next)
    set({ items: next })
  },
  clear: () => {
    persist([])
    set({ items: [] })
  },
  has: (kind, id) => get().items.some((i) => i.kind === kind && i.id === id),
}))

// ---------------------------------------------------------------------------
// Desk Brief (7b-iii) — one click: the desk's current composition, each
// unit's latest admitted read (in the composition's own declared order), and
// its open situations/tracked events.
// ---------------------------------------------------------------------------

/** The record that quotes a desk's units (`docs/ANALYSIS.md` §4.2). */
const COUNTRY_COMPOSITION_ANALYST = 'country_composition'

/** Top-N open situations, worst-first — bounds an unusually busy register the
 *  same way the composition's own grounding read of it is capped. */
const DESK_BRIEF_SITUATION_LIMIT = 20
/** Tracked events per situation — mirrors `TrackedEvents`'s own `limit=50`
 *  (Situations.tsx), trimmed for a brief meant to stay skimmable. */
const DESK_BRIEF_EVENTS_PER_SITUATION = 20

/** One unit the composition's own coverage register named but carries no
 *  admitted read for (below floor, unverified, or no head in the horizon) —
 *  reported, never silently dropped from the brief. */
export interface DeskBriefMissingUnit {
  unit: string
  unitName: string | null
  status: string
}

export interface DeskBriefEvent {
  id: string
  title: string
  lifecycleState: string
}

export interface DeskBriefSituation {
  id: string
  name: string
  status: string
  category: string
  intensityScore: number
  eventCount: number
  events: DeskBriefEvent[]
}

export interface DeskBriefResult {
  targetId: string
  compositionId: string | null
  /** Basket items in reading order: the composition, then each admitted
   *  unit read in the composition's own declared (coverage) order. */
  items: ExportBasketItem[]
  /** Items `add()` rejected (the basket was already full) — an item already
   *  present in the basket is left there and is NOT reported as rejected. */
  rejected: ExportBasketItem[]
  missingUnits: DeskBriefMissingUnit[]
  situations: DeskBriefSituation[]
  /** The situations/tracked-events section for the export route's
   *  `appendix` field — `null` when the desk has no open situation. */
  appendix: string | null
}

/**
 * What {@link readDeskBrief} answers: everything {@link buildDeskBrief}
 * composes except the basket verdict, plus the composition ROW itself.
 *
 * The row is carried because the page needs what only the row has — the
 * assembly payload (the declared unit order, the coverage register, the fold
 * ledger) and the scale/method stamps — and re-fetching a row already in hand
 * to read two fields off it is a second story about one record waiting to
 * happen.
 */
export interface DeskBriefRead extends Omit<DeskBriefResult, 'rejected'> {
  composition: ReadFindingRow | null
}

/** The `/situations` and `/v3/events` wire shapes this reads — a minimal
 *  local mirror of `panels/target/Situations.tsx`'s row types, kept local so
 *  this state module stays DOM-free and importable from a test with no
 *  component in the tree. */
interface DeskBriefSituationRow {
  id: string
  name: string
  status: string
  category: string
  intensity_score: number
  event_count: number
  last_event_at: string | null
}
interface DeskBriefEventRow {
  id: string
  title: string
  lifecycle_state: string
}
interface DeskBriefPage<T> {
  data: T[]
  next_cursor: string | null
}

/** Fetchers `buildDeskBrief` calls — injectable so the ordering / missing-item
 *  / empty-desk contract is testable without mocking `fetch`. Every
 *  production caller uses the defaults below, which read the exact routes
 *  `panels/target/Overview.tsx` and `panels/target/Situations.tsx` already
 *  call — this adds no new backend surface. */
export interface DeskBriefFetchers {
  fetchComposition: (targetId: string) => Promise<ReadFindingRow | null>
  fetchOpenSituations: (targetId: string) => Promise<DeskBriefSituationRow[]>
  fetchSituationEvents: (situationId: string) => Promise<DeskBriefEventRow[]>
}

async function fetchCompositionRow(targetId: string): Promise<ReadFindingRow | null> {
  const resp = await apiGet<{ data: ReadFindingRow[] }>(
    `/findings?target_id=${encodeURIComponent(targetId)}` +
      `&analyst_id=${COUNTRY_COMPOSITION_ANALYST}&limit=1`,
  )
  return resp.data[0] ?? null
}

async function fetchOpenSituationRows(targetId: string): Promise<DeskBriefSituationRow[]> {
  const resp = await apiGet<DeskBriefPage<DeskBriefSituationRow>>(
    `/situations?target_id=${encodeURIComponent(targetId)}&limit=100`,
  )
  // "Open" mirrors Situations.tsx's own bucketing: every status but
  // `resolved`. Worst-first — the same order the composition's own grounding
  // read of this register uses (`meta_findings_synthesizer.read_open_situations`:
  // intensity_score DESC, last_event_at DESC).
  return resp.data
    .filter((s) => s.status !== 'resolved')
    .sort((a, b) => {
      if (b.intensity_score !== a.intensity_score) return b.intensity_score - a.intensity_score
      const at = a.last_event_at ? Date.parse(a.last_event_at) : -Infinity
      const bt = b.last_event_at ? Date.parse(b.last_event_at) : -Infinity
      return bt - at
    })
    .slice(0, DESK_BRIEF_SITUATION_LIMIT)
}

async function fetchSituationEventRows(situationId: string): Promise<DeskBriefEventRow[]> {
  const resp = await apiGet<DeskBriefPage<DeskBriefEventRow>>(
    `/v3/events?situation_id=${encodeURIComponent(situationId)}` +
      `&limit=${DESK_BRIEF_EVENTS_PER_SITUATION}`,
  )
  return resp.data
}

const defaultDeskBriefFetchers: DeskBriefFetchers = {
  fetchComposition: fetchCompositionRow,
  fetchOpenSituations: fetchOpenSituationRows,
  fetchSituationEvents: fetchSituationEventRows,
}

/**
 * The situations/tracked-events appendix — plain markdown, carried through
 * the export route verbatim and printed after every basket item. `null` when
 * there is nothing open, so a quiet desk's brief grows no empty section.
 */
export function buildDeskBriefAppendix(situations: DeskBriefSituation[]): string | null {
  if (situations.length === 0) return null
  const lines = ['## Open situations & tracked events', '']
  for (const s of situations) {
    lines.push(`### ${s.name}`, '')
    lines.push(
      `- status: ${s.status} · category: ${s.category} · ` +
        `intensity: ${s.intensityScore.toFixed(2)} · situation id: ${s.id}`,
    )
    if (s.events.length === 0) {
      lines.push('- tracked events: none linked yet')
    } else {
      lines.push(`- tracked events (${s.events.length}):`)
      for (const e of s.events) {
        lines.push(`  - ${e.title} — ${e.lifecycleState} (event id: ${e.id})`)
      }
    }
    lines.push('')
  }
  return lines.join('\n').trimEnd()
}

/** The `appendix` field a desk brief hands the export route (k5b). */
export interface DeskBriefAppendixPayload {
  /** The situations/tracked-events markdown, verbatim — `null` on a quiet desk. */
  markdown: string | null
  /** Always true on a desk brief: ask the route to read this desk's absences. */
  absences: true
  /** The desk. Required by the route, which never infers one from the basket. */
  scope: string
}

/**
 * The desk brief's `appendix` payload — the client's situations markdown plus
 * the ASK for the server-composed typed-absence section.
 *
 * ONE definition, because two callers send this (the Report Export panel and
 * the Target Overview header action) and a brief that carried absences from
 * one entry point and not the other would be two different documents under
 * one name. The absences themselves are never composed here: the client can
 * only ask, so the exported markdown and the exported JSON carry the same
 * block, read once, server-side.
 */
export function deskBriefAppendix(
  targetId: string,
  situationsMarkdown: string | null,
): DeskBriefAppendixPayload {
  return { markdown: situationsMarkdown, absences: true, scope: targetId }
}

/**
 * The pure core: ordering + missing-unit logic given already-fetched rows.
 * Exported so the "declared order" / "missing items reported, not dropped" /
 * "empty desk" contract is testable without touching `fetch` or the store.
 */
export function composeDeskBrief(
  targetId: string,
  composition: ReadFindingRow | null,
  situationRows: DeskBriefSituationRow[],
  eventsBySituation: DeskBriefEventRow[][],
): Omit<DeskBriefResult, 'rejected'> {
  const items: ExportBasketItem[] = []
  const missingUnits: DeskBriefMissingUnit[] = []
  let compositionId: string | null = null

  if (composition) {
    compositionId = composition.id
    items.push({
      kind: 'finding',
      id: composition.id,
      label: composition.title ?? undefined,
    })
    const assembly = projectAssembly(composition)
    if (assembly) {
      const blockByUnit = new Map(assembly.blocks.map((b) => [b.desk, b]))
      // "Declared order" = the composition's own coverage register: every
      // unit it was built over, in the order it names them (`assembly_payload
      // .py` writes `coverage[]` in the units' subscribed order). A row
      // written before the coverage ledger existed falls back to the carried
      // blocks' own ordinal order — the next-best "declared order" on the
      // payload.
      const order =
        assembly.coverage.length > 0
          ? assembly.coverage.map((c) => ({
              unit: c.unit,
              unitName: c.unit_name ?? null,
              status: c.status,
            }))
          : [...assembly.blocks]
              .sort((a, b) => a.ordinal - b.ordinal)
              .map((b) => ({ unit: b.desk, unitName: b.target_name ?? null, status: 'in_basis' }))
      for (const u of order) {
        const block = blockByUnit.get(u.unit)
        if (block) {
          items.push({
            kind: 'finding',
            id: block.finding_id,
            label: block.question || u.unitName || u.unit,
          })
        } else {
          missingUnits.push(u)
        }
      }
    }
  }

  const situations: DeskBriefSituation[] = situationRows.map((s, i) => ({
    id: s.id,
    name: s.name,
    status: s.status,
    category: s.category,
    intensityScore: s.intensity_score,
    eventCount: s.event_count,
    events: (eventsBySituation[i] ?? []).map((e) => ({
      id: e.id,
      title: e.title,
      lifecycleState: e.lifecycle_state,
    })),
  }))

  return {
    targetId,
    compositionId,
    items,
    missingUnits,
    situations,
    appendix: buildDeskBriefAppendix(situations),
  }
}

/**
 * The desk brief's READ half — the same three fetches and the same ordering,
 * with NO write to the export basket (P-A).
 *
 * `target.desk_brief_page` renders the brief as a surface, and a page that
 * silently filled the operator's collection basket every time it mounted
 * would be collecting on their behalf without being asked. It needs exactly
 * what {@link buildDeskBrief} computes and none of what it stores, so the
 * two halves are separated here rather than reimplemented there: ONE
 * definition of "what a desk brief is made of" keeps the page, the Report
 * Export panel and the Target Overview action composing the same document.
 */
export async function readDeskBrief(
  targetId: string,
  fetchers: DeskBriefFetchers = defaultDeskBriefFetchers,
): Promise<DeskBriefRead> {
  const composition = await fetchers.fetchComposition(targetId)
  const situationRows = await fetchers.fetchOpenSituations(targetId)
  const eventsBySituation = await Promise.all(
    situationRows.map((s) => fetchers.fetchSituationEvents(s.id)),
  )
  return {
    ...composeDeskBrief(targetId, composition, situationRows, eventsBySituation),
    composition,
  }
}

/**
 * The Desk Brief action (7b-iii). Fetches the desk's current composition,
 * each unit's latest admitted read (in the composition's own declared
 * order), and the desk's open situations with their tracked events; adds the
 * finding items to the export basket in reading order — composition, then
 * units — and returns the whole result, including what it could NOT resolve
 * (a unit the composition carries no admitted read for is named in
 * `missingUnits`, never silently skipped) and the situations/events
 * `appendix` for the caller to hand the export route.
 *
 * Does not itself call the export route or download anything — callers
 * (`panels/system/ReportExport.tsx`'s "Desk brief" button,
 * `panels/target/Overview.tsx`'s) compose the returned `appendix` into their
 * own `exportCollection` call so the SAME citation/verify-carrying path
 * renders it, per the wiring convention every other export item already
 * uses.
 */
export async function buildDeskBrief(
  targetId: string,
  fetchers: DeskBriefFetchers = defaultDeskBriefFetchers,
): Promise<DeskBriefResult> {
  const composed = await readDeskBrief(targetId, fetchers)

  const store = useExportBasket.getState()
  const rejected: ExportBasketItem[] = []
  for (const item of composed.items) {
    if (store.has(item.kind, item.id)) continue
    if (!store.add(item)) rejected.push(item)
  }

  return { ...composed, rejected }
}

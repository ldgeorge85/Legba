/**
 * recordSheet — THE ONE COMMAND that opens the Record sheet (U-10; brief §2
 * "The Record is a drill-in sheet behind any statement").
 *
 *   openRecord({ kind: 'finding', id }, { cite: 3, origin: 'read' })
 *
 * Every way into the sheet comes here — a cited sentence in a read, a row on
 * the Board, a Consult citation, a map mark, a link, and Consult's
 * `panel_action` (`system.inspector` → `open_record`, declared in
 * `panel-registry/contracts.ts::OPEN_RECORD` and routed by
 * `lib/panelActions.ts`). What it does, in order:
 *
 *   1. remembers WHAT the sheet is on — the record, and for a finding the one
 *      cited sentence (`cite`, its marker's number);
 *   2. makes the record THE POINT (the shared selection), so every region and
 *      Consult follow it — the sheet's pin (`components/RecordSheet.tsx`)
 *      names it to Consult while the sheet is open;
 *   3. opens the sheet through the frame port (`system.inspector`'s open
 *      lands in the sheet, never on a new column);
 *   4. writes the ADDRESS: `#rec=<kind>:<id>` and, for a sentence, `#cite=<n>`
 *      — a link that reopens the same chain (`readRecordAddress`).
 *
 * While the sheet is open the on-screen report Consult carries lists it
 * (`recordSheetOnScreen`, read by `lib/onScreen.ts`): `system.inspector:<kind>:<id>`
 * at `sheet`.
 */
import { create } from 'zustand'
import { getFramePort, setPanelActionHandler, type ViewOnScreen } from '@/lib/panelActions'
import { selectRow, type Selection } from '@/state/selection'

/** The record kinds the chain reads (the lineage route's roots, plus a series observation). */
export const CHAIN_KINDS: ReadonlySet<string> = new Set([
  'finding',
  'signal',
  'event',
  'situation',
  'fact',
  'hypothesis',
  'observation',
  'alert',
  'critique',
  'meta_finding',
])

export interface RecordRef {
  kind: string
  id: string
  label?: string
}

interface RecordSheetState {
  /** What `openRecord` last opened (null: the sheet follows the selection). */
  ref: RecordRef | null
  /** The cited sentence of a finding: its marker's number. */
  cite: number | null
  /** Whether the sheet is on screen (set by the sheet itself). */
  open: boolean
}

export const useRecordSheet = create<RecordSheetState>(() => ({ ref: null, cite: null, open: false }))

/** The view id the sheet stands for in contracts, actions and the on-screen report. */
export const RECORD_VIEW_ID = 'system.inspector'

function cleanCite(cite: unknown): number | null {
  return typeof cite === 'number' && Number.isInteger(cite) && cite >= 1 && cite < 1000 ? cite : null
}

/**
 * Open the Record sheet on one record. Returns false when nothing can be
 * opened (no id). Never throws.
 */
export function openRecord(ref: RecordRef, opts: { cite?: number | null; origin?: string } = {}): boolean {
  if (!ref || typeof ref.id !== 'string' || !ref.id || typeof ref.kind !== 'string' || !ref.kind) return false
  const cite = cleanCite(opts.cite)
  useRecordSheet.setState({ ref: { kind: ref.kind, id: ref.id, label: ref.label }, cite })
  // The point: the true kind rides `instanceKey` when the selection coerces it
  // (hypothesis → finding), so the sheet reads the right row back.
  selectRow(ref.kind, ref.id, ref.label, { origin: opts.origin ?? 'record' })
  try {
    getFramePort()?.openView(RECORD_VIEW_ID, { origin: opts.origin })
  } catch {
    // No frame (a bare panel in a test): the selection still carries the point.
  }
  writeRecordAddress({ kind: ref.kind, id: ref.id }, cite)
  return true
}

/**
 * What the sheet is on, given the point: the record `openRecord` named when
 * it is still the point (with its sentence), else the point itself.
 */
export function sheetRecordFor(selection: Selection | null, state: Pick<RecordSheetState, 'ref' | 'cite'>): {
  ref: RecordRef
  cite: number | null
} | null {
  if (!selection) return null
  if (state.ref && state.ref.id === selection.id) return { ref: state.ref, cite: state.cite }
  const coerced = selection.instanceKey && CHAIN_KINDS.has(selection.instanceKey) ? selection.instanceKey : null
  const kind = coerced ?? (selection.kind === 'report' ? 'finding' : selection.kind)
  return { ref: { kind, id: selection.id, label: selection.label }, cite: null }
}

// ---------------------------------------------------------------------------
// The address: `#rec=<kind>:<id>&cite=<n>` (beside the frame's own params).
// ---------------------------------------------------------------------------

function hashParams(): URLSearchParams {
  try {
    return new URLSearchParams(window.location.hash.replace(/^#/, ''))
  } catch {
    return new URLSearchParams()
  }
}

function replaceHash(p: URLSearchParams): void {
  try {
    const q = p.toString()
    const next = q ? `#${q}` : ''
    if (next !== window.location.hash) {
      const { pathname, search } = window.location
      window.history.replaceState(null, '', `${pathname}${search}${next}`)
    }
  } catch {
    // history unavailable — sharing is best-effort, never fatal.
  }
}

/** Write (or, with null, clear) the sheet's address. Replace, never push. */
export function writeRecordAddress(ref: { kind: string; id: string } | null, cite: number | null = null): void {
  const p = hashParams()
  if (ref) p.set('rec', `${ref.kind}:${encodeURIComponent(ref.id)}`)
  else p.delete('rec')
  if (ref && cite !== null) p.set('cite', String(cite))
  else p.delete('cite')
  replaceHash(p)
}

/** The record a link names (`#rec=` / `#cite=`), or null. */
export function readRecordAddress(hash?: string): { ref: RecordRef; cite: number | null } | null {
  let p: URLSearchParams
  try {
    p = new URLSearchParams((hash ?? window.location.hash).replace(/^#/, ''))
  } catch {
    return null
  }
  const raw = p.get('rec')
  if (!raw) return null
  const at = raw.indexOf(':')
  if (at <= 0) return null
  const kind = raw.slice(0, at)
  let id = raw.slice(at + 1)
  try {
    id = decodeURIComponent(id)
  } catch {
    return null
  }
  if (!id || !CHAIN_KINDS.has(kind)) return null
  const n = Number(p.get('cite'))
  return { ref: { kind, id }, cite: cleanCite(Number.isFinite(n) ? n : null) }
}

// ---------------------------------------------------------------------------
// Consult: the on-screen entry, and the panel action.
// ---------------------------------------------------------------------------

/** The sheet as one view in the on-screen report, while it is open. */
export function recordSheetOnScreen(selection: Selection | null): ViewOnScreen | null {
  const st = useRecordSheet.getState()
  if (!st.open) return null
  const on = sheetRecordFor(selection, st)
  if (!on) return null
  return { id: `${RECORD_VIEW_ID}:${on.ref.kind}:${on.ref.id}`.slice(0, 120), at: 'sheet', front: true }
}

/**
 * `open_record` on `system.inspector` — the router (`applyPanelAction`) has
 * already checked the ref's kind and the arguments against the contract.
 */
setPanelActionHandler(RECORD_VIEW_ID, 'open_record', (args) => {
  const ref = args.ref as RecordRef
  return openRecord(ref, { cite: args.cite as number | undefined, origin: 'panel-action' })
})

/**
 * RecordSheet — the Record, as a sheet behind a statement (U-2 shell; brief
 * §2 "Record sheet"; the owner: the Record is a drill-in, not a surface).
 *
 * The Inspector (`system.inspector`) is no longer a dock edge group that takes
 * a column from the grid. It is a sheet that slides over the main area from
 * the Consult column's left border, `--sheet-w` wide (640 px, never wider than
 * the main area), when the reader selects a record — and closes with ×,
 * Escape or Ctrl/Cmd+I. Opening it never moves a region. It sits at z 40:
 * above the map's body-portalled WebGL overlay (z 30, `TileWebGLOverlay`),
 * below the command palette and the dialogs (z 50).
 *
 * U-10: for a record the chain can read (a statement, a received item, an
 * event, a situation, a fact, a series value) the sheet shows THE CHAIN —
 * `components/record/RecordChain.tsx`: the statement → its row → the evidence
 * → the receipt → later versions → integrity. Any other record (an entity, a
 * source, a place) and the "Details" switch show the Inspector as before.
 * `lib/recordSheet.ts::openRecord` is the one command that opens it; the sheet
 * keeps its address (`#rec=<kind>:<id>&cite=<n>`) and, on load, opens what a
 * link names. While it is open, Consult's point is the record in it, and the
 * on-screen report lists it at `sheet`.
 */
import { Suspense, lazy, useEffect, useState } from 'react'
import { X } from 'lucide-react'
import { useQuery } from '@tanstack/react-query'
import type { currentMode } from '@/auth/jwt'
import { SingletonView } from '@/components/SingletonView'
import { pinForSelection, usePinScope } from '@/lib/consultContext'
import {
  CHAIN_KINDS,
  openRecord,
  readRecordAddress,
  sheetRecordFor,
  useRecordSheet,
  writeRecordAddress,
} from '@/lib/recordSheet'
import { useSelection, type SelectionKind } from '@/state/selection'

const RecordChain = lazy(() => import('@/components/record/RecordChain'))

/** What the sheet can be open on — every record kind but a subject (a subject is the point). */
const RECORD_KINDS: readonly SelectionKind[] = [
  'finding',
  'signal',
  'event',
  'situation',
  'report',
  'entity',
  'source',
  'analyst',
  'journal_entry',
]

/**
 * THE PIN HOOK on the sheet (brief §2 "Record sheet": Consult is pinned to the
 * statement while it is open). Mounted only while open, so closing the sheet
 * releases the claim and the pin goes back to what the surface reads.
 */
function SheetPin() {
  const selection = useSelection((s) => s.selection)
  // A record the chain could not read is not something Consult can be asked
  // about: its lookup is held in the shared query cache (the chain's own key),
  // and a failed one withholds the pin instead of pointing Consult at a
  // record that does not exist. A lookup still running keeps the pin.
  const lookup = useQuery({
    queryKey: ['record-lineage', selection?.kind ?? '', selection?.id ?? ''],
    enabled: false,
  })
  const unresolved = lookup.status === 'error'
  usePinScope(unresolved ? null : pinForSelection(selection, RECORD_KINDS), 'record')
  return null
}

/** A link that names a record (`#rec=`) opens it once, when the shell loads. */
function useRecordLink(): void {
  useEffect(() => {
    const named = readRecordAddress()
    if (named) openRecord(named.ref, { cite: named.cite, origin: 'link' })
  }, [])
}

function OpenSheet({ mode, onClose }: { mode: ReturnType<typeof currentMode>; onClose: () => void }) {
  const selection = useSelection((s) => s.selection)
  const ref = useRecordSheet((s) => s.ref)
  const cite = useRecordSheet((s) => s.cite)
  const on = sheetRecordFor(selection, { ref, cite })
  const chainable = on !== null && CHAIN_KINDS.has(on.ref.kind)
  const [details, setDetails] = useState(false)

  // The sheet is on screen (the on-screen report reads this), and its address
  // follows what it shows; closing clears both.
  useEffect(() => {
    useRecordSheet.setState({ open: true })
    return () => {
      useRecordSheet.setState({ open: false })
      writeRecordAddress(null)
    }
  }, [])
  const key = on ? `${on.ref.kind}:${on.ref.id}:${on.cite ?? ''}` : ''
  useEffect(() => {
    if (on && chainable) writeRecordAddress(on.ref, on.cite)
    else writeRecordAddress(null)
    setDetails(false)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key])

  return (
    <section
      role="dialog"
      aria-modal="false"
      aria-label="Record"
      data-testid="record-sheet"
      data-record={on ? `${on.ref.kind}:${on.ref.id}` : undefined}
      className="absolute inset-y-0 right-0 z-40 flex flex-col border-l border-line-strong bg-surf-4 shadow-[0_0_32px_rgba(0,0,0,0.35)]"
      style={{ width: 'min(var(--sheet-w), 100%)' }}
    >
      <SheetPin />
      <div className="flex h-8 shrink-0 items-center gap-2 border-b border-line px-3">
        <span className="text-label uppercase tracking-[0.06em] text-ink-3">Record</span>
        <span className="text-label text-ink-3">· why should I believe this?</span>
        {chainable && (
          <button
            type="button"
            onClick={() => setDetails((d) => !d)}
            aria-pressed={details}
            className="ml-auto rounded-control px-1.5 text-label text-ink-3 hover:bg-surf-3 hover:text-ink-1"
            data-testid="record-sheet-details"
            title={details ? 'Back to the chain: statement, evidence, receipts, integrity' : 'Every field of this record'}
          >
            {details ? 'The chain' : 'Details'}
          </button>
        )}
        <button
          type="button"
          onClick={onClose}
          className={`${chainable ? '' : 'ml-auto '}rounded-control p-1 text-ink-3 hover:bg-surf-3 hover:text-ink-1`}
          data-testid="record-sheet-close"
          title="Close the record (Esc, Ctrl/Cmd+I)"
        >
          <X size={14} aria-hidden />
          <span className="sr-only">Close the record</span>
        </button>
      </div>
      <div className="min-h-0 flex-1 overflow-hidden">
        {chainable && !details ? (
          <Suspense fallback={<p className="px-4 py-3 text-body text-ink-3">Reading the record…</p>}>
            <RecordChain key={key} record={on!.ref} cite={on!.cite} />
          </Suspense>
        ) : (
          <SingletonView kind="system.inspector" mode={mode} />
        )}
      </div>
    </section>
  )
}

export function RecordSheet({
  open,
  mode,
  onClose,
}: {
  open: boolean
  mode: ReturnType<typeof currentMode>
  onClose: () => void
}) {
  useRecordLink()
  if (!open) return null
  return <OpenSheet mode={mode} onClose={onClose} />
}

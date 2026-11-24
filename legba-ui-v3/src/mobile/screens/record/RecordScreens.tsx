/**
 * The Record on the phone (M-3e; plan §2.3 row 3).
 *
 *   PEEK   a statement or a citation tapped: a sheet over the screen with the
 *          statement as published, the row it comes from and how many rows
 *          rest under it — read from the chain's own first read (the lineage
 *          walk at depth 1, the same cache key as `RecordChain`, so opening
 *          the full record asks nothing again). Back, the scrim or × closes it.
 *   PAGE   "Open the record" pushes the whole chain as a page: U-10's
 *          `components/record/RecordChain.tsx`, as-is (statement → row →
 *          evidence with its receipts and later versions → integrity). Back
 *          returns to the peek, then to the screen under it.
 *
 * The narrow variant is this wrapper; the chain itself is unchanged.
 */
import { lazy, Suspense, useEffect, useRef } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ApiError, apiGet } from '@/lib/api'
import { evidenceOf, LINEAGE_KINDS, RECORD_ROUTES, rowLine, statementOf, type LineageWire } from '@/lib/recordChain'
import { useRecordSheet, type RecordRef } from '@/lib/recordSheet'
import { pinForSelection, usePinScope } from '@/lib/consultContext'
import { selectionKindOf, useSelection } from '@/state/selection'
import { back, navigate } from '@/mobile/shell/phoneAddress'
import { currentScreen } from '@/mobile/shell/phoneState'
import { AskAboutThis, Quiet } from '@/mobile/shell/ui'

const RecordChain = lazy(() => import('@/components/record/RecordChain'))

const STALE_MS = 5 * 60_000

/** The sheet is on screen while either is mounted (the on-screen report and `#rec=` read it). */
function useSheetOpen(): void {
  useEffect(() => {
    useRecordSheet.setState({ open: true })
    return () => useRecordSheet.setState({ open: false })
  }, [])
}

/** Consult is pinned to the record while it is open (the desktop sheet's rule). */
function RecordPin() {
  const selection = useSelection((s) => s.selection)
  usePinScope(pinForSelection(selection, ['finding', 'signal', 'event', 'situation', 'report']), 'record')
  return null
}

export function RecordPeek({ record, cite }: { record: RecordRef; cite: number | null }) {
  useSheetOpen()
  const traced = LINEAGE_KINDS.has(record.kind)
  const lineage = useQuery({
    queryKey: ['record-lineage', record.kind, record.id],
    queryFn: () => apiGet<LineageWire>(RECORD_ROUTES.lineage(record.kind, record.id)),
    enabled: traced,
    staleTime: STALE_MS,
    // a record that is not there is not there: no second ask
    retry: (n, e) => !(e instanceof ApiError && e.status === 404) && n < 1,
  })
  const root = lineage.data?.root ?? null
  const statement = statementOf(root, cite)
  const evidence = evidenceOf(lineage.data ?? null, statement, cite)
  const openBtn = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    openBtn.current?.focus({ preventScroll: true })
  }, [])

  return (
    <div className="ph-peek-wrap" data-testid="phone-record-peek-wrap">
      <button type="button" className="ph-scrim" aria-label="Close the record" onClick={back} />
      <section className="ph-peek" role="dialog" aria-label="Record" data-testid="record-peek" data-record={`${record.kind}:${record.id}`}>
        <RecordPin />
        <div className="ph-peek-grip" aria-hidden />
        <div className="ph-peek-head">
          <span className="ph-peek-title">Record · why believe this?</span>
          <button type="button" className="ph-icon-btn" onClick={back} data-testid="record-peek-close" aria-label="Close the record">
            ×
          </button>
        </div>
        <div className="ph-peek-body">
          {!traced ? (
            <Quiet>This kind of record cannot be traced here, so its chain cannot be read.</Quiet>
          ) : lineage.isPending ? (
            <Quiet testId="record-peek-reading">Reading the record…</Quiet>
          ) : lineage.isError || !root ? (
            <Quiet testId="record-peek-error">The record could not be read. Nothing is shown in its place.</Quiet>
          ) : (
            <>
              {statement.text ? (
                <p className="ph-statement" data-testid="record-peek-statement">
                  &ldquo;{statement.text}&rdquo;
                </p>
              ) : null}
              {statement.gap ? <Quiet>{statement.gap}</Quiet> : null}
              <p className="ph-peek-row" data-testid="record-peek-row">
                {/* the row's title, unless it is the statement already quoted above */}
                {root.title && root.title.trim() !== (statement.text ?? '').trim() ? <span className="ph-ink-1">{root.title} </span> : null}
                <span className="ph-dim">{rowLine(root)}</span>
              </p>
              <p className="ph-peek-count" data-testid="record-peek-count">
                {evidence.gap ? evidence.gap : `${evidence.total} row${evidence.total === 1 ? '' : 's'} rest under this.`}
              </p>
            </>
          )}
        </div>
        <button
          ref={openBtn}
          type="button"
          className="ph-primary"
          data-testid="record-peek-open"
          onClick={() => {
            const cur = currentScreen()
            navigate({ ...cur, record: { ref: record, cite }, peek: false }, 'push')
          }}
        >
          Open the record
        </button>
      </section>
    </div>
  )
}

export function RecordPage({ record, cite }: { record: RecordRef; cite: number | null }) {
  useSheetOpen()
  return (
    <div className="ph-record-page" data-testid="record-page">
      <RecordPin />
      <Suspense fallback={<Quiet>Reading the record…</Quiet>}>
        <RecordChain key={`${record.kind}:${record.id}:${cite ?? ''}`} record={record} cite={cite} />
      </Suspense>
      <div className="ph-screen ph-screen-tight">
        <AskAboutThis
          label={record.label ?? `this ${record.kind}`}
          pins={[{ kind: selectionKindOf(record.kind), id: record.id, label: record.label ?? undefined }]}
        />
      </div>
    </div>
  )
}

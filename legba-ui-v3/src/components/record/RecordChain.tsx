/**
 * RecordChain — the Record sheet's content (U-10): why should I believe this?
 *
 * One statement read top to bottom as a chain (`lib/recordChain.ts` is the
 * model; the routes are `RECORD_ROUTES`):
 *
 *   1  The statement, as published      the record's cited sentence or its lead
 *   2  The row it maps to               who or what wrote it, and when
 *   3  The evidence                     the rows it rests on, with source and class
 *   4    The receipt                    what was fetched, when, from where, the
 *                                       hash, the protocol, the stored version;
 *                                       "Show what was received" opens its text;
 *                                       a feed / API response opens the ITEM as
 *                                       received, cut from its bytes (B-1)
 *   5    Later versions                 the same address fetched again, and how
 *                                       the page changed after it was read
 *   6  Integrity                        the run's receipt, checkpoint and anchor
 *
 * A gap is stated in words at the step where it occurs — never an empty box,
 * never a tick by default. Reads: the lineage walk at depth 1, one receipts
 * call for at most eight evidence rows, one integrity call for the run; the
 * stored text only when asked, capped.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet, ApiError } from '@/lib/api'
import { openRecord, type RecordRef } from '@/lib/recordSheet'
import {
  LINEAGE_KINDS,
  RECORD_ROUTES,
  canReadItem,
  evidenceLabelWords,
  itemWithheldWords,
  STATE_WORDS,
  driftWords,
  evidenceOf,
  heldWords,
  hostOf,
  integrityLines,
  laterVersionsOf,
  noReceiptWords,
  receiptFacts,
  receiptIds,
  roleWords,
  rowLine,
  rowNoun,
  sourceLine,
  statementOf,
  textWithheldWords,
  when,
  SUBSTANTIVE_DRIFT,
  type EvidenceItem,
  type IntegrityState,
  type IntegrityWire,
  type LineageWire,
  type ReceiptRecordWire,
  type ReceiptsWire,
  type ItemTextWire,
  type VersionTextWire,
  type VersionWire,
} from '@/lib/recordChain'

const STALE_MS = 60_000

function errorWords(err: unknown): string {
  if (err instanceof ApiError) {
    if (err.status === 404) return 'no such record was found'
    if (err.status === 400) return 'this kind of record cannot be traced here'
    return `the route answered ${err.status}`
  }
  return 'the request failed'
}

/** A step of the chain: a number on the rule, a label, then its content. */
function Step({ n, label, children, testid }: { n: string; label: string; children: React.ReactNode; testid: string }) {
  return (
    <li className="relative pb-5 pl-8" data-testid={testid}>
      <span
        className="absolute left-0 top-0 flex h-5 w-5 items-center justify-center rounded-full border border-line-strong bg-surf-4 font-mono text-label text-ink-2"
        aria-hidden
      >
        {n}
      </span>
      <h3 className="mb-1.5 text-label uppercase tracking-[0.06em] text-ink-3">{label}</h3>
      <div className="min-w-0 space-y-1.5 text-body text-ink-1">{children}</div>
    </li>
  )
}

/** A gap, stated where it occurs. */
function Gap({ children, testid = 'record-gap' }: { children: React.ReactNode; testid?: string }) {
  return (
    <p className="border-l-2 border-line-strong pl-2 text-body text-ink-2" data-testid={testid}>
      {children}
    </p>
  )
}

function Reading({ what }: { what: string }) {
  return (
    <p className="text-body text-ink-3" data-testid="record-reading">
      Reading {what}…
    </p>
  )
}

const STATE_CLASS: Record<IntegrityState, string> = {
  verified: 'text-accent-ok',
  covered: 'text-ink-2',
  not_covered: 'text-ink-3',
  failed: 'text-accent-critical',
}

function TextView({ version }: { version: VersionWire }) {
  const q = useQuery({
    queryKey: ['record-version-text', version.id],
    queryFn: () => apiGet<VersionTextWire>(RECORD_ROUTES.text(version.id)),
    staleTime: STALE_MS,
  })
  if (q.isPending) return <Reading what="the stored text" />
  if (q.isError) return <Gap>The stored text could not be read: {errorWords(q.error)}.</Gap>
  const t = q.data
  if (!t.available) return <Gap>The receipts store is not available on this deployment.</Gap>
  if (!t.shown || t.full_text === null) return <Gap testid="record-text-withheld">{textWithheldWords(t)}</Gap>
  return (
    <div className="space-y-1" data-testid="record-text">
      <p className="text-label text-ink-3" data-testid="record-text-extent">
        {t.truncated
          ? `Showing the first ${t.chars_shown.toLocaleString('en-GB')} of ${(t.chars_total ?? t.chars_shown).toLocaleString('en-GB')} characters — truncated.`
          : `All ${t.chars_shown.toLocaleString('en-GB')} characters of the text this version stored.`}{' '}
        <span
          className={t.text_hash_ok === true ? 'text-accent-ok' : t.text_hash_ok === false ? 'text-accent-critical' : 'text-ink-3'}
          data-testid="record-text-hash"
        >
          {t.text_hash_ok === true
            ? 'Hash verified: the stored text re-hashes to its recorded sha256.'
            : t.text_hash_ok === false
              ? 'Hash failed: the stored text does not match its recorded sha256.'
              : 'No hash was recorded for this text, so it cannot be checked.'}
        </span>
      </p>
      {/* Plain text, never HTML: a <pre> renders the characters as they are. */}
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words border-l border-line bg-surf-2 p-2 font-mono text-label text-ink-1">
        {t.full_text}
      </pre>
    </div>
  )
}

function ItemView({ signalId }: { signalId: string }) {
  const q = useQuery({
    queryKey: ['record-item-text', signalId],
    queryFn: () => apiGet<ItemTextWire>(RECORD_ROUTES.item(signalId)),
    staleTime: STALE_MS,
  })
  if (q.isPending) return <Reading what="the item as received" />
  if (q.isError) return <Gap>The item could not be read: {errorWords(q.error)}.</Gap>
  const t = q.data
  if (!t.available) return <Gap>The receipts store is not available on this deployment.</Gap>
  if (!t.shown || t.full_text === null) return <Gap testid="record-item-withheld">{itemWithheldWords(t)}</Gap>
  return (
    <div className="space-y-1" data-testid="record-item">
      <p className="text-label text-ink-3" data-testid="record-item-extent">
        {t.truncated
          ? `Showing the first ${t.chars_shown.toLocaleString('en-GB')} of ${(t.chars_total ?? t.chars_shown).toLocaleString('en-GB')} characters of the item — truncated.`
          : `The item as the response carried it, ${t.chars_shown.toLocaleString('en-GB')} characters, cut from the stored bytes after they re-hashed to the receipt.`}
      </p>
      <pre className="max-h-80 overflow-auto whitespace-pre-wrap break-words border-l border-line bg-surf-2 p-2 font-mono text-label text-ink-1">
        {t.full_text}
      </pre>
    </div>
  )
}

function Receipt({ version, role, item }: { version: VersionWire; role: string; item: string | null }) {
  const [show, setShow] = useState(false)
  return (
    <div className="space-y-1 border-l border-line pl-3" data-testid="record-receipt" data-version={version.id}>
      <p className="text-label text-ink-3">Receipt · {roleWords(role)}</p>
      <dl className="grid grid-cols-[7.5rem_minmax(0,1fr)] gap-x-2 gap-y-0.5">
        {receiptFacts(version).map((f) => (
          <div key={f.label} className="contents">
            <dt className="text-label text-ink-3">{f.label}</dt>
            <dd className={`min-w-0 truncate text-label ${f.mono ? 'font-mono' : ''} text-ink-1`} title={f.title}>
              {f.value}
            </dd>
          </div>
        ))}
      </dl>
      <p className="text-label text-ink-2" data-testid="record-receipt-held">
        {heldWords(version.held).replace(/^./, (c) => c.toUpperCase())}.
      </p>
      {version.has_text ? (
        <>
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            aria-expanded={show}
            className="text-label text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink-1"
            data-testid="record-show-received"
          >
            {show ? 'Hide what was received' : 'Show what was received'}
          </button>
          {show && <TextView version={version} />}
        </>
      ) : item !== null ? (
        // B-1: a feed or API response keeps no text of its own; the item is
        // cut from its held bytes at the record's place in them.
        <>
          <button
            type="button"
            onClick={() => setShow((s) => !s)}
            aria-expanded={show}
            className="text-label text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink-1"
            data-testid="record-show-item"
          >
            {show ? 'Hide the item as received' : 'Show the item as received'}
          </button>
          {show && <ItemView signalId={item} />}
        </>
      ) : (
        // No text was stored for this version (a feed or API response is kept
        // as bytes only): said here, without a read that could only say so.
        <Gap testid="record-no-text">
          This version kept no text to show: a feed or API response is kept as bytes only, identified by the hash above.
        </Gap>
      )}
    </div>
  )
}

function Later({ rec, receipts }: { rec: ReceiptRecordWire; receipts: ReceiptsWire }) {
  const later = laterVersionsOf(rec, receipts)
  if (!later) {
    return <Gap>This item has no page address, so later versions of it are not followed.</Gap>
  }
  const changed = later.later.some((l) => l.drift && SUBSTANTIVE_DRIFT.has(l.drift)) || later.gone !== null
  return (
    <div className="space-y-1" data-testid="record-later">
      <p className={`text-body ${changed ? 'text-accent-warning' : 'text-ink-2'}`} data-testid="record-later-summary">
        {later.summary}
      </p>
      {later.later.length > 0 && (
        <ul className="space-y-0.5">
          {later.later.map(({ version, drift }) => (
            <li key={version.id} className="flex min-w-0 items-baseline gap-2 text-label">
              <span className="shrink-0 font-mono text-ink-2">{when(version.first_seen_at)}</span>
              <span className="min-w-0 truncate text-ink-1">
                version {version.id} · {drift ? driftWords(drift) : 'not compared yet'}
              </span>
            </li>
          ))}
          {later.more && <li className="text-label text-ink-3">and more later versions, not listed</li>}
        </ul>
      )}
    </div>
  )
}

function EvidenceRow({
  item,
  receipts,
  receiptsState,
}: {
  item: EvidenceItem
  receipts: ReceiptsWire | null
  receiptsState: 'reading' | 'failed' | 'ready' | 'none'
}) {
  const rec = receipts?.records.find((r) => r.id === item.id) ?? null
  const host = hostOf(item.url)
  return (
    <li className="space-y-1.5 border-t border-line pt-2 first:border-t-0 first:pt-0" data-testid="record-evidence" data-kind={item.kind} data-id={item.id}>
      <div className="min-w-0">
        <p className="text-body-lg text-ink-1">
          {item.ordinal !== null && <span className="mr-1 font-mono text-label text-ink-3">[{item.ordinal}]</span>}
          {item.title ?? <span className="text-ink-3">untitled {rowNoun(item.kind)}</span>}
        </p>
        <p className="text-label text-ink-3">
          {[sourceLine(rec) ?? (receiptsState === 'ready' && item.kind === 'signal' ? 'source not recorded' : null), host, rec?.fetched_at ? `arrived ${when(rec.fetched_at)}` : null]
            .filter(Boolean)
            .join(' · ')}
        </p>
        {item.passage && (
          <blockquote className="mt-1 border-l border-line pl-2 font-serif text-body text-ink-2" data-testid="record-passage">
            {item.passage}
          </blockquote>
        )}
      </div>
      {item.kind !== 'signal' ? (
        <div className="space-y-1">
          <Gap testid="record-evidence-row">
            This rests on another {rowNoun(item.kind)}, not on a received item: its own record carries its evidence and receipts.
          </Gap>
          <button
            type="button"
            onClick={() => openRecord({ kind: item.kind, id: item.id, label: item.title ?? undefined }, { origin: 'record' })}
            className="text-label text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink-1"
            data-testid="record-follow"
          >
            Follow it to its record
          </button>
        </div>
      ) : receiptsState === 'reading' ? (
        <Reading what="its receipt" />
      ) : receiptsState === 'failed' ? (
        <Gap>The receipt could not be read this time; nothing about it is assumed.</Gap>
      ) : receiptsState === 'none' || !receipts?.available ? (
        <Gap>Fetch receipts are not available on this deployment.</Gap>
      ) : !rec || !rec.found ? (
        <Gap>This item was not found among received items.</Gap>
      ) : rec.links.length === 0 ? (
        <>
          {evidenceLabelWords(rec.evidence) && (
            <p className="text-label text-ink-2" data-testid="record-evidence-label" data-label={rec.evidence?.label}>
              {evidenceLabelWords(rec.evidence)}
            </p>
          )}
          <Gap testid="record-no-receipt">{noReceiptWords(rec, receipts.receipts_start)}</Gap>
        </>
      ) : (
        <>
          {evidenceLabelWords(rec.evidence) && (
            <p className="text-label text-ink-2" data-testid="record-evidence-label" data-label={rec.evidence?.label}>
              {evidenceLabelWords(rec.evidence)}
            </p>
          )}
          {rec.links.map((l) => {
            const v = receipts.versions[String(l.version_id)]
            return v ? (
              <Receipt
                key={`${l.role}:${l.version_id}`}
                version={v}
                role={l.role}
                item={canReadItem(rec.evidence, v.id, l.role) ? rec.id : null}
              />
            ) : (
              <Gap key={`${l.role}:${l.version_id}`}>Receipt version {l.version_id} is linked but could not be read.</Gap>
            )
          })}
          <Later rec={rec} receipts={receipts} />
        </>
      )}
    </li>
  )
}

export default function RecordChain({ record, cite }: { record: RecordRef; cite: number | null }) {
  const traced = LINEAGE_KINDS.has(record.kind)
  const lineage = useQuery({
    queryKey: ['record-lineage', record.kind, record.id],
    queryFn: () => apiGet<LineageWire>(RECORD_ROUTES.lineage(record.kind, record.id)),
    enabled: traced,
    staleTime: STALE_MS,
  })
  const root = lineage.data?.root ?? null
  const statement = statementOf(root, cite)
  const evidence = record.kind === 'observation'
    ? { items: [{ kind: 'observation', id: record.id, title: record.label ?? null, url: null, passage: null, ordinal: null, archived: null }], total: 1, gap: null }
    : evidenceOf(lineage.data ?? null, statement, cite)
  const ids = record.kind === 'observation' ? [record.id] : receiptIds(evidence)
  const receipts = useQuery({
    queryKey: ['record-receipts', record.kind === 'observation' ? 'observation' : 'signal', ids],
    queryFn: () => apiGet<ReceiptsWire>(RECORD_ROUTES.receipts(record.kind === 'observation' ? 'observation' : 'signal', ids)),
    enabled: ids.length > 0,
    staleTime: STALE_MS,
  })
  const runId = root?.receipt?.run_id ?? null
  const integrity = useQuery({
    queryKey: ['record-integrity', runId],
    queryFn: () => apiGet<IntegrityWire>(RECORD_ROUTES.integrity(runId!)),
    enabled: runId !== null,
    staleTime: STALE_MS,
  })
  const receiptsState: 'reading' | 'failed' | 'ready' | 'none' =
    ids.length === 0 ? 'none' : receipts.isPending ? 'reading' : receipts.isError ? 'failed' : 'ready'

  return (
    <div className="h-full overflow-y-auto px-4 py-3" data-testid="record-chain" data-kind={record.kind} data-id={record.id} data-cite={cite ?? undefined}>
      <ol className="relative before:absolute before:bottom-5 before:left-[9px] before:top-5 before:w-px before:bg-line">
        <Step n="1" label="The statement, as published" testid="record-step-statement">
          {!traced && record.kind !== 'observation' ? (
            <Gap>This kind of record cannot be traced here, so its chain cannot be read.</Gap>
          ) : record.kind === 'observation' ? (
            <p className="font-serif text-heading-lg text-ink-1">{record.label ?? 'A value in a series'}</p>
          ) : lineage.isPending ? (
            <Reading what="the record" />
          ) : lineage.isError ? (
            <Gap>The record could not be read: {errorWords(lineage.error)}.</Gap>
          ) : (
            <>
              {statement.text ? (
                <p className="font-serif text-heading-lg text-ink-1" data-testid="record-statement" data-basis={statement.basis}>
                  &ldquo;{statement.text}&rdquo;
                </p>
              ) : null}
              {statement.basis === 'cited_sentence' && root?.title && (
                <p className="text-label text-ink-3">from &ldquo;{root.title}&rdquo;</p>
              )}
              {statement.gap && <Gap testid="record-statement-gap">{statement.gap}</Gap>}
            </>
          )}
        </Step>

        <Step n="2" label="The row it comes from" testid="record-step-row">
          {record.kind === 'observation' ? (
            <Gap>A series value is read through its receipt below; the value's own row cannot be opened on its own yet.</Gap>
          ) : !traced ? (
            <Gap>Not read.</Gap>
          ) : lineage.isPending ? (
            <Reading what="the row" />
          ) : !root ? (
            <Gap>No row was read.</Gap>
          ) : (
            <>
              <p className="text-body-lg">
                <span className="text-ink-3">A {rowNoun(root.row_kind)}: </span>
                {root.title ?? <span className="text-ink-3">untitled</span>}
              </p>
              <p className="text-body text-ink-2" data-testid="record-row-line">
                {rowLine(root)}
              </p>
            </>
          )}
        </Step>

        <Step n="3" label="The evidence behind it" testid="record-step-evidence">
          {lineage.isPending && traced ? (
            <Reading what="the evidence" />
          ) : evidence.gap ? (
            <Gap testid="record-evidence-gap">{evidence.gap}</Gap>
          ) : evidence.items.length === 0 ? (
            <Gap testid="record-evidence-gap">No evidence was read.</Gap>
          ) : (
            <>
              {evidence.total > evidence.items.length && (
                <p className="text-label text-ink-3" data-testid="record-evidence-count">
                  {evidence.total} rows rest under this statement; the first {evidence.items.length} are shown with their receipts.
                </p>
              )}
              <ul className="space-y-3">
                {evidence.items.map((item) => (
                  <EvidenceRow key={`${item.kind}:${item.id}`} item={item} receipts={receipts.data ?? null} receiptsState={receiptsState} />
                ))}
              </ul>
            </>
          )}
        </Step>

        <Step n="4" label="Integrity" testid="record-step-integrity">
          {!root ? (
            <Gap>{record.kind === 'observation' ? 'A received value: its check is the content hash on its receipt above.' : 'Not read.'}</Gap>
          ) : integrity.isPending && runId !== null ? (
            <Reading what="the run's receipt" />
          ) : (
            <ul className="space-y-1.5">
              {integrityLines(root, integrity.data ?? null).map((l) => (
                <li key={l.label} className="text-body" data-testid="record-integrity-line" data-state={l.state}>
                  <span className="text-ink-3">{l.label}: </span>
                  <span className={`font-medium ${STATE_CLASS[l.state]}`}>{STATE_WORDS[l.state]}</span>
                  <span className="text-ink-2"> — {l.words}</span>
                </li>
              ))}
              {integrity.isError && (
                <li>
                  <Gap>The checkpoint and the daily anchor could not be read: {errorWords(integrity.error)}.</Gap>
                </li>
              )}
            </ul>
          )}
        </Step>
      </ol>
    </div>
  )
}

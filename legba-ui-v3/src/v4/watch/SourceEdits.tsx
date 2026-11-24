/**
 * SourceEdits — the Watch surface's second panel region (U-9): pages that
 * CHANGED AFTER THEY WERE READ (evidence drift, D-1/D-2).
 *
 *   OPEN FLAGS   what changed on a page something relied on, the change in
 *                plain words, how the source acknowledged it, what relied on
 *                it (a link that makes it the point), and both passages — the
 *                one read and the one there now — with every stored version
 *                of the page one click further (`/evidence/drift/history`).
 *                The auto-close rule's SHADOW ("would close", recorded with the
 *                switch off) is marked as a shadow, never as a closure.
 *   EDIT RECORD  per source, over 30 days: pages read again, changes assessed,
 *                how each was acknowledged, history rewrites. A TRACKED MEASURE:
 *                listed by name, in plain ink, with no rank and no colour —
 *                it feeds no score and is not a verdict on the source.
 *
 * Reads, lightest first: the summary (`/evidence/drift`) for every figure;
 * the open flags only when the summary says there are some (never a list read
 * for a count); the edit record only when its tab is shown; a page's versions
 * only when asked. Until the re-reads land the summary holds zeros (the
 * server exposes no re-check schedule, so the region names no date), and the region says what will appear and why it is empty.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiGet } from '@/lib/api'
import { useBelowAdmin } from '@/auth/roles'
import { cn } from '@/lib/cn'
import { PanelTabStrip } from '@/components/PanelTabs'
import { editRecordRows, flagRows, whenText, type FlagRow } from './model'
import { useWatch, type EditsTab } from './state'
import type { DriftEditorsResponse, DriftFlagsResponse, DriftHistoryResponse, DriftSummary } from './types'
import { Figure, Notice, SubjectLink, ViewHead } from './parts'

export const DRIFT_SUMMARY_ROUTE = '/v3/evidence/drift?window_hours=168'
export const DRIFT_FLAGS_ROUTE = '/v3/evidence/drift/flags?state=open&limit=25'
export const DRIFT_EDITORS_ROUTE = '/v3/evidence/drift/editors?window_hours=720&limit=50'

const TABS = [
  { id: 'flags', label: 'Open flags' },
  { id: 'record', label: 'Edit record by source' },
] as const

function History({ address }: { address: string }) {
  const q = useQuery<DriftHistoryResponse>({
    queryKey: ['watch', 'drift-history', address],
    queryFn: () => apiGet<DriftHistoryResponse>(`/v3/evidence/drift/history?address=${encodeURIComponent(address)}`),
    staleTime: 300_000,
  })
  if (q.isPending) return <p className="text-label text-ink-3">Reading the stored versions…</p>
  if (q.isError || !q.data?.available) return <p className="text-label text-ink-3">The stored versions could not be read.</p>
  const v = q.data.versions ?? []
  if (v.length === 0) return <p className="text-label text-ink-3">No stored version of this page.</p>
  return (
    <ol className="mt-1 text-label text-ink-2" data-testid="watch-edit-versions">
      {v.map((x, i) => (
        <li key={String(x.id)} className="border-b border-line-subtle py-0.5">
          Version {i + 1}: first read {whenText(x.first_seen_at)}, last read {whenText(x.last_seen_at)}
          {typeof x.fetch_count === 'number' ? `, read ${x.fetch_count} time${x.fetch_count === 1 ? '' : 's'}` : ''}
          {typeof x.text_chars === 'number' ? `, ${x.text_chars} characters` : ''}
          {x.sha256 ? <span className="font-mono text-ink-3"> · {x.sha256.slice(0, 12)}</span> : null}
        </li>
      ))}
    </ol>
  )
}

function Flag({ row, open }: { row: FlagRow; open: boolean }) {
  const toggle = () => useWatch.setState({ openFlag: open ? null : row.id })
  return (
    <li className="border-b border-line-subtle py-2" data-testid="watch-edit-flag" data-would-close={row.wouldAutoClose ? '1' : '0'}>
      <div className="flex flex-wrap items-baseline gap-x-2">
        <a href={row.address} target="_blank" rel="noopener noreferrer" className="text-body text-ink-1 underline decoration-line-strong underline-offset-2">
          {row.page}
        </a>
        {row.source ? (
          <span className="text-label text-ink-3">
            from <SubjectLink subject={row.source} />
          </span>
        ) : null}
      </div>
      <div className="mt-0.5 text-body text-ink-2">
        <span className="text-ink-1">{row.change}</span>, {row.ack}
        {row.historyRewrite ? <span className="text-ink-1"> · an earlier version that had stood for a while was changed without a notice</span> : null}
      </div>
      <div className="mt-0.5 text-label text-ink-2">
        {row.reliedOn ? (
          <>
            Relied on by <SubjectLink subject={row.reliedOn} />
            {row.effect ? ` — ${row.effect}` : ''}
          </>
        ) : (
          'What relied on it is not recorded'
        )}
        {row.when ? <span className="text-ink-3"> · {row.when}</span> : null}
      </div>
      <div className="mt-1 flex flex-wrap items-center gap-x-3 text-label">
        {row.wouldAutoClose ? (
          <span className="rounded-control border border-dashed border-line-strong px-1.5 text-ink-2" data-testid="watch-edit-shadow" title="Recorded with the auto-close switch off: nothing was closed">
            the auto-close rule would close this (shadow: not closed)
          </span>
        ) : null}
        <button type="button" className="text-ink-2 underline" onClick={toggle} aria-expanded={open} data-testid="watch-edit-open">
          {open ? 'Hide both versions' : 'Show both versions'}
        </button>
      </div>
      {open ? (
        <div className="mt-2" data-testid="watch-edit-passages">
          {row.passages.length === 0 && row.removed.length === 0 ? (
            <p className="text-label text-ink-3">No changed passage was stored for this change.</p>
          ) : null}
          {row.passages.map((p, i) => (
            <div key={i} className="grid grid-cols-1 gap-2 border-b border-line-subtle py-1.5 md:grid-cols-2">
              <div>
                <div className="text-label uppercase tracking-[0.06em] text-ink-3">As read</div>
                <p className="text-body text-ink-2">{p.earlier}</p>
              </div>
              <div>
                <div className="text-label uppercase tracking-[0.06em] text-ink-3">As it is now</div>
                <p className="text-body text-ink-1">{p.later}</p>
              </div>
            </div>
          ))}
          {row.removed.map((r, i) => (
            <div key={`r${i}`} className="border-b border-line-subtle py-1.5">
              <div className="text-label uppercase tracking-[0.06em] text-ink-3">Removed</div>
              <p className="text-body text-ink-2 line-through decoration-line-strong">{r}</p>
            </div>
          ))}
          <div className="mt-1">
            <div className="text-label uppercase tracking-[0.06em] text-ink-3">Every stored version of this page</div>
            <History address={row.address} />
          </div>
        </div>
      ) : null}
    </li>
  )
}

function Flags({ summary }: { summary: DriftSummary }) {
  const openFlag = useWatch((s) => s.openFlag)
  const total = summary.open_flags_total ?? 0
  // RA-1: the flags quote the changed passages of the publisher's page and
  // name who closed them — an admin's read. A reader sees the count only.
  const belowAdmin = useBelowAdmin()
  const q = useQuery<DriftFlagsResponse>({
    queryKey: ['watch', 'drift-flags'],
    queryFn: () => apiGet<DriftFlagsResponse>(DRIFT_FLAGS_ROUTE),
    staleTime: 60_000,
    enabled: total > 0 && !belowAdmin,
  })
  const rows = useMemo(() => flagRows(q.data?.flags), [q.data])
  if (total === 0) {
    return (
      <Notice title="No open flags." testId="watch-edits-no-flags">
        {(summary.classified ?? 0) > 0
          ? `${summary.classified} change${summary.classified === 1 ? ' was' : 's were'} measured in the last 7 days; none touched a page anything relies on, or each has been reviewed.`
          : null}
      </Notice>
    )
  }
  if (belowAdmin) {
    return (
      <Notice title={`${total} open flag${total === 1 ? '' : 's'}.`} testId="watch-edits-flags-reader">
        Each flag quotes the changed passages of a page we read; an admin reviews them. The edit record by source is in the next tab.
      </Notice>
    )
  }
  if (q.isPending) return <Notice title="Reading the open flags…" testId="watch-edits-reading" />
  if (q.isError || !q.data?.available) return <Notice title="The open flags could not be read." testId="watch-edits-error" />
  return (
    <ul data-testid="watch-edit-flags">
      {rows.map((r) => (
        <Flag key={r.id} row={r} open={openFlag === r.id} />
      ))}
      {total > rows.length ? (
        <li className="py-1 text-label text-ink-3">
          The {rows.length} shown first are the ones to read first (history rewrites, then reversals and removals); {total - rows.length} more are open.
        </li>
      ) : null}
    </ul>
  )
}

function Record() {
  const q = useQuery<DriftEditorsResponse>({
    queryKey: ['watch', 'drift-editors'],
    queryFn: () => apiGet<DriftEditorsResponse>(DRIFT_EDITORS_ROUTE),
    staleTime: 300_000,
  })
  const rows = useMemo(() => editRecordRows(q.data?.sources), [q.data])
  const caption = (
    <p className="max-w-[70ch] text-label text-ink-3" data-testid="watch-edit-record-caption">
      A tracked measure, kept per source over the last 30 days: how many of its pages were read again, how many changes were assessed, and how each
      change was shown to readers. It feeds no score and is not a judgement of the source; sources are listed by name.
    </p>
  )
  if (q.isPending) return <>{caption}<Notice title="Reading the edit record…" testId="watch-edit-record-reading" /></>
  if (q.isError || !q.data?.available) return <>{caption}<Notice title="The edit record could not be read." testId="watch-edit-record-error" /></>
  if (rows.length === 0) {
    return (
      <>
        {caption}
        <Notice title="No source has a page that changed in substance in the last 30 days." testId="watch-edit-record-none" />
      </>
    )
  }
  const th = 'px-2 py-1 text-left text-label font-normal text-ink-3'
  const td = 'px-2 py-1 text-right font-mono text-body tabular-nums text-ink-1'
  return (
    <>
      {caption}
      <div className="overflow-x-auto">
        <table className="w-full border-collapse" data-testid="watch-edit-record">
          <thead>
            <tr className="border-b border-line-subtle">
              <th className={th}>Source</th>
              <th className={th}>Pages read again</th>
              <th className={th}>Changes assessed</th>
              <th className={th}>With a correction notice</th>
              <th className={th}>With an “updated” stamp</th>
              <th className={th}>Hidden date only</th>
              <th className={th}>Without saying so</th>
              <th className={th}>History rewrites</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <tr key={r.id} className="border-b border-line-subtle" data-testid="watch-edit-record-row">
                <td className="px-2 py-1 text-body text-ink-1">
                  <SubjectLink subject={{ kind: r.id.startsWith('host:') ? null : 'source', id: r.id, label: r.label, noun: 'source' }} />
                </td>
                <td className={td}>
                  <Figure value={r.rechecked} />
                </td>
                <td className={td}>
                  <Figure value={r.assessed} />
                </td>
                <td className={td}>
                  <Figure value={r.notice} />
                </td>
                <td className={td}>
                  <Figure value={r.stamp} />
                </td>
                <td className={td}>
                  <Figure value={r.dateOnly} />
                </td>
                <td className={td}>
                  <Figure value={r.silent} />
                </td>
                <td className={td}>
                  <Figure value={r.rewrites} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

export default function SourceEdits() {
  const tab = useWatch((s) => s.editsTab)
  const q = useQuery<DriftSummary>({
    queryKey: ['watch', 'drift-summary'],
    queryFn: () => apiGet<DriftSummary>(DRIFT_SUMMARY_ROUTE),
    staleTime: 60_000,
    refetchInterval: 300_000,
  })
  const s = q.data
  const read = !!s?.available
  const nothingYet = read && (s.classified ?? 0) === 0 && (s.open_flags_total ?? 0) === 0 && (s.auto_closed?.total ?? 0) === 0
  const shadow = s?.would_auto_close

  let body
  if (q.isPending) body = <Notice title="Reading the edit counts…" testId="watch-edits-summary-reading" />
  else if (q.isError) body = <Notice title="The edit counts could not be read." testId="watch-edits-summary-error">Nothing here is a measurement.</Notice>
  else if (!read) {
    body = (
      <Notice title="Edits are not recorded on this server yet." testId="watch-edits-unavailable">
        This server does not keep the record of pages read again, so no edit can be shown — this is not a count of zero.
      </Notice>
    )
  } else if (nothingYet && tab === 'flags') {
    body = (
      <Notice title="No edits recorded yet — and that is not a finding." testId="watch-edits-empty">
        <p>
          Here will appear the pages this system relied on that changed after it read them: what changed (a fact corrected, a claim removed, a page
          taken down), whether the source said so, what relied on the page, and the passage as read beside the passage as it is now.
        </p>
        <p className="mt-1">
          The list is empty because pages are only compared when they are read a second time. The first changes can appear after the first re-fetch of
          a cited page. Until then an empty list means “not compared yet”, never “nothing changed”.
        </p>
      </Notice>
    )
  } else body = tab === 'flags' ? <Flags summary={s} /> : <Record />

  return (
    <section className="flex h-full min-h-0 flex-col gap-2 overflow-auto bg-surf-1 px-5 py-4" data-testid="watch-edits" data-read={read ? '1' : '0'}>
      <ViewHead
        title="Changed after read"
        testId="watch-edits-head"
        line={
          <>
            <Figure value={read ? s.open_flags_total ?? null : null} testId="watch-edits-open" /> open flags ·{' '}
            <Figure value={read ? s.classified ?? null : null} /> changes measured in 7 days ·{' '}
            <Figure value={read ? s.history_rewrites ?? null : null} /> history rewrites
            {read && shadow ? (
              <span data-testid="watch-edits-shadow-count">
                {' '}
                · <Figure value={shadow.total ?? null} /> the auto-close rule would close
                {shadow.switch_on ? '' : ' (a shadow count: the switch is off, nothing was closed)'}
              </span>
            ) : null}
          </>
        }
      />
      <div className={cn(!read && 'opacity-60')}>
        <PanelTabStrip
          tabs={TABS}
          active={tab}
          onChange={(id) => useWatch.setState({ editsTab: id as EditsTab })}
          ariaLabel="Changed after read"
          testIdPrefix="watch-edits-tab"
        />
      </div>
      {body}
    </section>
  )
}

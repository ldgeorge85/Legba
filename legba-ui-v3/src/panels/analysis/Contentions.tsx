/**
 * Contentions — the contrary-evidence pass's records as a LIST
 * (`analysis.contentions`, W2 S-batch).
 *
 * Until this panel a `claim_contentions` row was reachable only one claim at a
 * time: the chip on the cited sentence it was written against, or the
 * Cross-framing drill. Nothing answered the operator's own question about the
 * pass — "what did it find last night, across the fleet?" — which is the read
 * `GET /v3/contentions` serves with no filter at all.
 *
 * WHAT A ROW IS, stated on the panel as the route states it: a RETRIEVAL
 * outcome against one published claim, never a verdict on it. The route's
 * `note` renders above the list verbatim. Each row carries the four fences'
 * readings for its decisive page (host class, the date the gate parsed, the
 * subject overlap, the independent-page count against its bar) through the
 * same `contentionFences` the drill uses, so an unmeasured fence reads "not
 * measured" here exactly as it does there — never 0. `live` vs `expired` is
 * rendered, never filtered unless asked.
 *
 * Adds no route and computes no statistic. Filters are the route's own
 * parameters (`scope`, `stance`, `live_only`); the response's `scope_field`
 * says which handle the scope matched on, so "nothing found" and "wrong
 * handle" read differently. A row with a target opens Cross-framing on its
 * exact claim id; a row with a read opens that read in the Inspector.
 */
import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'

import { PanelChrome } from '@/components/PanelChrome'
import { RecordLink } from '@/components/inspector/RecordLink'
import { cn } from '@/lib/cn'
import { apiGet } from '@/lib/api'
import {
  CONTENTION_STANCES,
  contentionFences,
  contentionsListPath,
  decisiveRef,
  scopeFieldLine,
  stanceLabel,
  type ContentionRow,
  type ContentionStance,
  type ContentionsResponse,
} from '@/lib/contentionsModel'
import { requestCrossFraming } from '@/lib/crossFramingLink'
import type { PanelProps } from '@/types'

const STANCE_TONE: Record<string, string> = {
  contradicts: 'border-accent-critical/50 bg-accent-critical/10 text-accent-critical',
  qualifies: 'border-accent-warning/50 bg-accent-warning/10 text-accent-warning',
  none_found: 'border-line bg-surf-1 text-ink-3',
  search_failed: 'border-dashed border-line-strong bg-transparent text-ink-3',
}

function hostOf(url: string): string | null {
  try {
    return new URL(url).host
  } catch {
    return null
  }
}

function Fence({ label, value }: { label: string; value: string }) {
  return (
    <span className="whitespace-nowrap">
      <span className="text-ink-3/70">{label}</span>{' '}
      <span className="text-ink-2">{value}</span>
    </span>
  )
}

function ContentionItem({ row }: { row: ContentionRow }) {
  const fences = contentionFences(row)
  const ref = decisiveRef(row)
  const host = ref?.url ? hostOf(ref.url) : null
  return (
    <li
      className="border-t border-line py-2 first:border-t-0"
      data-testid={`contention-row-${row.claim_id}`}
      data-stance={row.stance}
    >
      <div className="flex flex-wrap items-baseline gap-1.5 text-label">
        <span
          className={cn('rounded border px-1', STANCE_TONE[row.stance] ?? STANCE_TONE.none_found)}
          data-testid="contention-stance"
          title={`stance: ${row.stance} — a retrieval outcome, not a verdict`}
        >
          {stanceLabel(row.stance)}
        </span>
        {row.derivation === 'negation' && (
          <span className="text-accent-warning" title="the uncalibrated fallback rule — judge the page yourself">
            negation rule (uncalibrated)
          </span>
        )}
        <span
          className={row.live ? 'text-ink-3' : 'text-accent-warning'}
          data-testid="contention-shelf"
        >
          {row.live ? 'live' : 'expired, not re-checked'}
        </span>
        <span className="text-ink-3">· as of {row.as_of || '—'}</span>
        <span className="font-mono text-ink-3">
          · {row.target_id || '—'}
          {row.desk_key && row.desk_key !== row.target_id ? ` / ${row.desk_key}` : ''}
        </span>
      </div>

      <div className="mt-1 text-body text-ink-1" data-testid="contention-claim">
        {row.claim_text}
      </div>
      {row.statement && (
        <div className="mt-0.5 text-label italic text-ink-2">{row.statement}</div>
      )}

      <div
        className="mt-1 flex flex-wrap gap-x-3 gap-y-0.5 text-label"
        data-testid="contention-fences"
      >
        <Fence label="host" value={fences.host_class} />
        <Fence label="page date" value={fences.page_date} />
        <Fence label="subject overlap" value={fences.subject_overlap} />
        <Fence label="independence" value={fences.independent_pages} />
      </div>

      <div className="mt-1 flex flex-wrap items-baseline gap-x-3 text-label text-ink-3">
        <span>
          {row.refs.length === 0
            ? 'no page fetched'
            : `${row.refs.length} page${row.refs.length === 1 ? '' : 's'} fetched${
                host ? ` · decisive: ${host}` : ''
              }`}
        </span>
        {row.rung && <span>rung {row.rung}</span>}
        {row.finding_id && (
          <RecordLink
            kind="finding"
            id={row.finding_id}
            label="open the read"
            origin="analysis.contentions"
          />
        )}
        {row.target_id && (
          <button
            type="button"
            className="text-accent-info underline"
            data-testid={`contention-cross-framing-${row.claim_id}`}
            title="put this claim against the desk's other bounded units"
            onClick={() =>
              requestCrossFraming({
                targetId: row.target_id!,
                claimId: row.claim_id,
                claimText: row.claim_text,
                origin: 'analysis.contentions',
              })
            }
          >
            cross-framing ▸
          </button>
        )}
      </div>
    </li>
  )
}

export default function ContentionsPanel({ registration }: PanelProps) {
  const [scopeDraft, setScopeDraft] = useState('')
  const [scope, setScope] = useState('')
  const [stance, setStance] = useState<ContentionStance | ''>('')
  const [liveOnly, setLiveOnly] = useState(false)

  const path = contentionsListPath({ scope, stance: stance || null, liveOnly })
  const { data, isLoading, error, refetch, dataUpdatedAt } = useQuery<ContentionsResponse>({
    queryKey: ['contentions-listing', path],
    queryFn: () => apiGet<ContentionsResponse>(path),
    staleTime: 60_000,
  })

  const rows = data?.contentions ?? []
  const newest = rows[0]?.as_of || null
  const matched = scopeFieldLine(data)
  const fetched = dataUpdatedAt ? new Date(dataUpdatedAt).toLocaleTimeString() : null

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        data
          ? `${rows.length} record${rows.length === 1 ? '' : 's'}${
              newest ? ` · newest as of ${newest}` : ''
            }${fetched ? ` · fetched ${fetched}` : ''}`
          : undefined
      }
      onRefresh={() => refetch()}
    >
      <div className="flex h-full flex-col gap-2 overflow-hidden p-density text-body">
        <form
          className="flex flex-wrap items-center gap-2 text-label"
          onSubmit={(e) => {
            e.preventDefault()
            setScope(scopeDraft.trim())
          }}
        >
          <input
            className="min-w-[10rem] flex-1 rounded border border-line bg-surf-1 px-2 py-1 text-ink-1"
            placeholder="target or desk key (blank = fleet-wide)"
            value={scopeDraft}
            onChange={(e) => setScopeDraft(e.target.value)}
            data-testid="contentions-scope"
          />
          <select
            className="rounded border border-line bg-surf-1 px-2 py-1 text-ink-1"
            value={stance}
            onChange={(e) => setStance(e.target.value as ContentionStance | '')}
            data-testid="contentions-stance"
          >
            <option value="">every stance</option>
            {CONTENTION_STANCES.map((s) => (
              <option key={s} value={s}>
                {s}
              </option>
            ))}
          </select>
          <label className="flex items-center gap-1 text-ink-2">
            <input
              type="checkbox"
              checked={liveOnly}
              onChange={(e) => setLiveOnly(e.target.checked)}
              data-testid="contentions-live-only"
            />
            live only
          </label>
          <button type="submit" className="rounded border border-line px-2 py-1 text-ink-2">
            apply
          </button>
        </form>

        {data?.note && (
          <p
            className="rounded border border-line bg-surf-1 px-2 py-1 text-label text-ink-3"
            data-testid="contentions-note"
          >
            {data.note}
          </p>
        )}
        {matched && (
          <p className="text-label text-ink-3" data-testid="contentions-scope-field">
            {matched}
          </p>
        )}

        <div className="min-h-0 flex-1 overflow-auto" data-testid="contentions-list">
          {isLoading && <p className="text-label text-ink-3">loading…</p>}
          {error instanceof Error && (
            <p className="text-label text-accent-critical" data-testid="contentions-error">
              the contentions route could not be read: {error.message}
            </p>
          )}
          {data && rows.length === 0 && (
            <p className="text-label text-ink-3" data-testid="contentions-empty">
              No contention record matches
              {scope || stance || liveOnly ? ' these filters' : ''}. The pass writes a
              record only for a claim it ran against; an empty list is not a claim that
              nothing is contested.
            </p>
          )}
          {rows.length > 0 && (
            <ul>
              {rows.map((r) => (
                <ContentionItem key={`${r.claim_id}:${r.as_of}`} row={r} />
              ))}
            </ul>
          )}
        </div>
      </div>
    </PanelChrome>
  )
}

/**
 * Narratives (`system.narratives`) — the reified contested-claim families and
 * the directed source-echo graph.
 *
 * Three routes that existed with no consumer until this train:
 *   `GET /api/v1/v3/narratives`       — the families (carriers, first-seen, lags)
 *   `GET /api/v1/v3/narratives/echo`  — the leader→follower co-carriage edges
 *   `GET /api/v1/v3/narratives/{id}`  — one family's detail (reached by expanding
 *                                       a row; the list already carries the body)
 *
 * The server attaches an `honesty_note` to both envelopes specifically so a
 * client cannot present echo-lead as more than it is. This panel RENDERS that
 * note verbatim at the top of both modes rather than paraphrasing it, and the
 * vocabulary underneath keeps the same line: every label is about publication
 * ORDER, never influence (see `lib/narrativesModel.ts`).
 *
 * Both routes degrade rather than 500 when migration 0102 is absent — they
 * answer an empty envelope. So an empty list here means "nothing detected OR
 * the tables aren't there yet", and the empty state says exactly that instead
 * of asserting the stronger claim.
 */

import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { PanelChrome } from '@/components/PanelChrome'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { cn } from '@/lib/cn'
import { relativeTime } from '@/lib/findingsViews'
import { fetchNarrativeEcho, fetchNarratives } from '@/lib/api'
import type { Narrative, NarrativeEchoEdge } from '@/lib/api'
import {
  carrierViews,
  datedCoverage,
  echoStrengthLabel,
  echoTone,
  formatLagHours,
  honestyNote,
  narrativeStatusTone,
  narrativeTitle,
  variantViews,
} from '@/lib/narrativesModel'
import type { PanelProps } from '@/types'

type Mode = 'families' | 'echo'

const MODES: readonly PanelTabDef[] = [
  { id: 'families', label: 'Families' },
  { id: 'echo', label: 'Echo graph' },
]

export default function NarrativesPanel({ registration }: PanelProps) {
  const [mode, setMode] = useState<Mode>('families')
  const [statusFilter, setStatusFilter] = useState<'' | 'contested' | 'surfaced'>('')
  const [systematicOnly, setSystematicOnly] = useState(false)

  const families = useQuery({
    queryKey: ['narratives', statusFilter],
    queryFn: () =>
      fetchNarratives({ status: statusFilter || undefined, limit: 100 }),
    enabled: mode === 'families',
    refetchInterval: 180_000,
  })

  const echo = useQuery({
    queryKey: ['narrative_echo', systematicOnly],
    queryFn: () => fetchNarrativeEcho({ systematicOnly, limit: 200 }),
    enabled: mode === 'echo',
    refetchInterval: 180_000,
  })

  const active = mode === 'families' ? families : echo
  const note = honestyNote(mode === 'families' ? families.data : echo.data)

  return (
    <PanelChrome
      registration={registration}
      subtitle={
        mode === 'families'
          ? `${families.data?.count ?? 0} contested-claim famil${(families.data?.count ?? 0) === 1 ? 'y' : 'ies'}`
          : `${echo.data?.count ?? 0} co-carriage edge${(echo.data?.count ?? 0) === 1 ? '' : 's'}`
      }
      onRefresh={() => void active.refetch()}
      actions={
        <div className="flex items-center gap-2">
          <PanelTabStrip
            tabs={MODES}
            active={mode}
            onChange={(id) => setMode(id as Mode)}
            ariaLabel="Narratives surface"
            testIdPrefix="narratives-tab"
          />
          {mode === 'families' ? (
            <select
              value={statusFilter}
              onChange={(e) => setStatusFilter(e.target.value as '' | 'contested' | 'surfaced')}
              data-testid="narratives-status-filter"
              className="rounded border border-line bg-surf-1 px-1.5 py-0.5 text-label text-ink-2"
            >
              <option value="">all statuses</option>
              <option value="contested">contested</option>
              <option value="surfaced">surfaced</option>
            </select>
          ) : (
            <button
              type="button"
              onClick={() => setSystematicOnly((v) => !v)}
              data-testid="narratives-systematic-toggle"
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                systematicOnly
                  ? 'border-amber-500/40 bg-amber-500/15 text-amber-300'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
            >
              {systematicOnly ? 'systematic only' : 'all edges'}
            </button>
          )}
        </div>
      }
    >
      <p
        className="mb-2 rounded border border-line bg-surf-1 p-1.5 text-label text-ink-3"
        data-testid="narratives-honesty-note"
      >
        {note}
      </p>

      {active.isLoading && <div className="text-body text-ink-3">loading…</div>}
      {active.error != null && (
        <div className="text-body text-rose-300" data-testid="narratives-error">
          could not read the narratives surface
        </div>
      )}

      {mode === 'families' && !families.isLoading && families.error == null && (
        <div className="space-y-1.5" data-testid="narratives-families">
          {(families.data?.narratives.length ?? 0) === 0 ? (
            <div className="text-body text-ink-3" data-testid="narratives-families-empty">
              No contested-claim families — either none have been detected, or the
              derived tables (migration 0102) are not present yet. The route answers an
              empty envelope for both, so this surface will not claim to tell them apart.
            </div>
          ) : (
            families.data!.narratives.map((n) => <FamilyRow key={n.contention_id} n={n} />)
          )}
        </div>
      )}

      {mode === 'echo' && !echo.isLoading && echo.error == null && (
        <div className="space-y-1" data-testid="narratives-echo">
          {(echo.data?.edges.length ?? 0) === 0 ? (
            <div className="text-body text-ink-3" data-testid="narratives-echo-empty">
              No co-carriage edges{systematicOnly ? ' flagged systematic' : ''} — either
              none were computed, or the derived tables (migration 0102) are not present
              yet.
            </div>
          ) : (
            echo.data!.edges.map((e) => (
              <EchoRow key={`${e.leader_source_id}->${e.follower_source_id}`} edge={e} />
            ))
          )}
        </div>
      )}
    </PanelChrome>
  )
}

function FamilyRow({ n }: { n: Narrative }) {
  const [open, setOpen] = useState(false)
  const coverage = datedCoverage(n)
  const carriers = carrierViews(n)
  const variants = variantViews(n)

  return (
    <div
      className="rounded border border-line bg-surf-1"
      data-testid={`narrative-row-${n.contention_id}`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full flex-wrap items-center gap-2 px-2 py-1.5 text-left hover:bg-surf-2"
        data-testid={`narrative-toggle-${n.contention_id}`}
      >
        <span
          className={cn(
            'shrink-0 rounded border px-1.5 py-0.5 text-label',
            narrativeStatusTone(n.status),
          )}
        >
          {n.status}
        </span>
        <span className="min-w-0 flex-1 truncate text-body text-ink-1">
          {narrativeTitle(n)}
        </span>
        <span className="shrink-0 text-label text-ink-3">
          {n.variant_count} variant{n.variant_count === 1 ? '' : 's'} · {n.carrier_source_count}{' '}
          carrier{n.carrier_source_count === 1 ? '' : 's'}
        </span>
        {n.last_seen_at && (
          <span className="shrink-0 text-label text-ink-3">{relativeTime(n.last_seen_at)}</span>
        )}
      </button>

      {open && (
        <div className="space-y-1.5 border-t border-line px-2 py-2 text-body">
          {n.surfaced_value && (
            <div>
              <span className="text-label text-ink-3">surfaced value: </span>
              <span className="text-ink-1">{n.surfaced_value}</span>
            </div>
          )}
          <div className="text-label text-ink-3" data-testid={`narrative-coverage-${n.contention_id}`}>
            {coverage
              ? `${coverage.dated} of ${coverage.total} carriers are publish-dated — the echo timing below rests on those ${coverage.dated}.`
              : 'no carriers recorded'}
          </div>
          <div className="flex flex-wrap gap-x-4 gap-y-0.5 text-label text-ink-3">
            <span>signals: {n.signal_count}</span>
            <span>facts: {n.fact_count}</span>
            <span>span: {formatLagHours(n.span_hours)}</span>
            <span>max echo lag: {formatLagHours(n.max_echo_lag_hours)}</span>
            <span>
              first published by:{' '}
              <span className="font-mono text-ink-2">{n.lead_source_id ?? '—'}</span>
            </span>
          </div>

          {variants.length > 0 && (
            <div>
              <div className="text-label uppercase tracking-wider text-ink-3">
                Value clusters
              </div>
              <ul className="mt-0.5 space-y-0.5">
                {variants.map((v, i) => (
                  <li key={`${v.value}-${i}`} className="text-ink-2">
                    {v.value}
                    {v.count != null && <span className="text-ink-3"> ×{v.count}</span>}
                  </li>
                ))}
              </ul>
            </div>
          )}

          {carriers.length > 0 && (
            <div>
              <div className="text-label uppercase tracking-wider text-ink-3">
                Carriage order (publication order — not influence)
              </div>
              <ul className="mt-0.5 space-y-0.5" data-testid={`narrative-carriers-${n.contention_id}`}>
                {carriers.map((c, i) => (
                  <li key={`${c.sourceId}-${i}`} className="flex flex-wrap items-baseline gap-2">
                    <span className="text-ink-3">{i + 1}.</span>
                    <span className="font-mono text-ink-2">{c.sourceId}</span>
                    {c.firstSeenAt && (
                      <span className="text-label text-ink-3">
                        {relativeTime(c.firstSeenAt)}
                      </span>
                    )}
                    {c.lagHours != null && (
                      <span className="text-label text-ink-3">
                        +{formatLagHours(c.lagHours)} after lead
                      </span>
                    )}
                    {c.signalCount != null && (
                      <span className="text-label text-ink-3">{c.signalCount} sig</span>
                    )}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
      )}
    </div>
  )
}

function EchoRow({ edge }: { edge: NarrativeEchoEdge }) {
  return (
    <div
      className="flex flex-wrap items-center gap-2 rounded border border-line bg-surf-1 px-2 py-1.5 text-body"
      data-testid={`echo-row-${edge.leader_source_id}-${edge.follower_source_id}`}
    >
      <span className={cn('shrink-0 rounded border px-1.5 py-0.5 text-label', echoTone(edge))}>
        {edge.systematic ? 'systematic' : echoStrengthLabel(edge)}
      </span>
      <span className="font-mono text-ink-1">{edge.leader_source_id}</span>
      <span className="text-ink-3">published before</span>
      <span className="font-mono text-ink-1">{edge.follower_source_id}</span>
      <span className="ml-auto flex flex-wrap gap-x-3 text-label text-ink-3">
        <span>
          {edge.follow_within_count}/{edge.co_carried} co-carried within{' '}
          {formatLagHours(edge.echo_window_hours)}
        </span>
        <span>median lag {formatLagHours(edge.median_lag_hours)}</span>
        <span>
          ratio {edge.echo_ratio != null ? edge.echo_ratio.toFixed(2) : '—'}
        </span>
      </span>
    </div>
  )
}

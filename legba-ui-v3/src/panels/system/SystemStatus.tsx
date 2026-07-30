/**
 * System Status (`system.status`) — the at-a-glance, per-layer health view the
 * operator has asked for repeatedly: one page, four clearly-labelled sections,
 * each color-coded green / amber / red.
 *
 *   ACQUISITION — per-source firing matrix
 *                 (`GET /api/v1/v3/system/source-firing`): is each source
 *                 firing / silent / erroring / paused, with signal volume +
 *                 last-seen age, plus the A7 `freshness_grade` column (a
 *                 dot + closed grade — ok/stale/warn/empty/ungraded — read
 *                 against that source's own cadence-derived budget; see
 *                 `@/lib/sourceFreshness`).
 *   ANALYSIS    — per-analyst cadence
 *                 (`GET /api/v1/v3/system/analyst-cadence`): last-run age +
 *                 runs/24h + healthy / stale / never. This reads
 *                 analyst_traces (the real cadence truth) — NOT actor_state,
 *                 whose last_run_at is NULL.
 *   QUEUES      — consumer-lag rollup
 *                 (`GET /api/v1/v3/streams/consumer_lag`, already
 *                 orphan-filtered): only real backlog surfaces.
 *   INFRA       — runtime actor health
 *                 (`GET /api/v1/v3/runtime/actors`): lifecycle + error count.
 *
 * Deliberately simple: each section is a status-banner + a compact table; the
 * goal is "what is firing, what is stale, what is backed up" in one glance.
 * Polls every 10s. All four routes are independent — a failure in one section
 * never blanks the others.
 */

import { useQuery } from '@tanstack/react-query'
import { useMemo, type ReactNode } from 'react'
import { PanelChrome } from '@/components/PanelChrome'
import {
  apiGet,
  ApiError,
  getSystemSourceFiring,
  getSystemAnalystCadence,
  type SourceFiringRow,
  type AnalystCadenceRow,
} from '@/lib/api'
import { lagSeverity, sortLag, relTime, type ConsumerLagRow } from '@/lib/evalOps'
import { freshnessTone, freshnessTitle, type FreshnessTone } from '@/lib/sourceFreshness'
import type { PanelProps } from '@/types'

const POLL_MS = 10_000

// --- shared traffic-light vocabulary --------------------------------------

type Light = 'green' | 'amber' | 'red' | 'grey'

const LIGHT_PILL: Record<Light, string> = {
  green: 'bg-emerald-900 text-emerald-200',
  amber: 'bg-amber-900 text-amber-200',
  red: 'bg-rose-900 text-rose-200',
  grey: 'bg-slate-800 text-slate-400',
}
const LIGHT_DOT: Record<Light, string> = {
  green: 'bg-emerald-400',
  amber: 'bg-amber-400',
  red: 'bg-rose-400',
  grey: 'bg-slate-500',
}
const LIGHT_NUM: Record<Light, string> = {
  green: 'text-slate-300',
  amber: 'text-amber-300',
  red: 'text-rose-300',
  grey: 'text-slate-500',
}

// Age helper that prefers the server-supplied age_seconds (clock-skew-proof)
// but falls back to a timestamp when the count is absent.
function ageLabel(ageSeconds: number | null, ts: string | null): string {
  if (ageSeconds == null) return relTime(ts)
  const sec = Math.max(0, ageSeconds)
  if (sec < 60) return `${sec.toFixed(0)}s ago`
  if (sec < 3600) return `${(sec / 60).toFixed(0)}m ago`
  if (sec < 86400) return `${(sec / 3600).toFixed(1)}h ago`
  return `${(sec / 86400).toFixed(1)}d ago`
}

// ===========================================================================
// Section shell — banner (counts + worst-light) over a scrollable table body.
// ===========================================================================

interface SectionProps {
  title: string
  subtitle: string
  /** worst light across the section, drives the banner accent */
  worst: Light
  /** small per-light tallies rendered as chips in the banner */
  tally: Array<{ light: Light; label: string; n: number }>
  loading: boolean
  error: unknown
  children: ReactNode
  testid: string
}

function Section({
  title,
  subtitle,
  worst,
  tally,
  loading,
  error,
  children,
  testid,
}: SectionProps) {
  return (
    <section
      className={`rounded border bg-surface-100 mb-3 ${
        worst === 'red'
          ? 'border-rose-900/50'
          : worst === 'amber'
            ? 'border-amber-900/40'
            : 'border-slate-800'
      }`}
      data-testid={testid}
    >
      <header className="flex items-center gap-2 px-3 py-2 border-b border-slate-800 flex-wrap">
        <span className={`w-2 h-2 rounded-full shrink-0 ${LIGHT_DOT[worst]}`} />
        <span className="font-semibold text-sm tracking-wide uppercase text-slate-200">
          {title}
        </span>
        <span className="text-[11px] text-slate-500">{subtitle}</span>
        <span className="flex-1" />
        {tally
          .filter((t) => t.n > 0)
          .map((t) => (
            <span
              key={t.label}
              className={`rounded px-1.5 py-0.5 text-[10px] ${LIGHT_PILL[t.light]}`}
              data-testid={`${testid}-tally-${t.label}`}
            >
              {t.n} {t.label}
            </span>
          ))}
        {loading && <span className="text-slate-500 text-[10px]">loading…</span>}
      </header>
      <div className="px-2 py-1.5">
        {error instanceof Error && (
          <div className="text-rose-400 text-xs py-1" data-testid={`${testid}-error`}>
            error: {error.message}
          </div>
        )}
        {children}
      </div>
    </section>
  )
}

function StatusPill({ light, label, testid }: { light: Light; label: string; testid?: string }) {
  return (
    <span
      className={`rounded px-1 text-[10px] ${LIGHT_PILL[light]}`}
      data-testid={testid}
    >
      {label}
    </span>
  )
}

function EmptyRow({ children }: { children: ReactNode }) {
  return <div className="text-slate-500 text-center text-xs py-3">{children}</div>
}

// ===========================================================================
// ACQUISITION — per-source firing matrix
// ===========================================================================

function sourceLight(status: string): Light {
  switch (status) {
    case 'firing':
      return 'green'
    case 'silent':
      return 'amber'
    case 'error':
      return 'red'
    case 'paused':
      return 'grey'
    default:
      return 'grey'
  }
}

// A7 — the per-source freshness grade rides the SAME traffic-light dot/pill
// vocabulary as the row `status` column (LIGHT_DOT), via the freshness→tone
// classification in `@/lib/sourceFreshness` (kept panel-agnostic there).
const FRESHNESS_TONE_LIGHT: Record<FreshnessTone, Light> = {
  ok: 'green',
  watch: 'amber',
  bad: 'red',
  muted: 'grey',
}

function AcquisitionSection() {
  const { data, isLoading, error } = useQuery<SourceFiringRow[]>({
    queryKey: ['system-status', 'source-firing'],
    queryFn: async () => {
      try {
        return await getSystemSourceFiring()
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: POLL_MS,
  })

  const rows = useMemo(() => {
    // Worst-light first (error → silent → paused → firing), then by id.
    const order: Record<Light, number> = { red: 0, amber: 1, grey: 2, green: 3 }
    return [...(data ?? [])].sort((a, b) => {
      const d = order[sourceLight(a.status)] - order[sourceLight(b.status)]
      return d !== 0 ? d : a.source_id.localeCompare(b.source_id)
    })
  }, [data])

  const counts = useMemo(() => {
    const c = { firing: 0, silent: 0, error: 0, paused: 0 }
    for (const r of data ?? []) {
      if (r.status === 'firing') c.firing++
      else if (r.status === 'silent') c.silent++
      else if (r.status === 'error') c.error++
      else if (r.status === 'paused') c.paused++
    }
    return c
  }, [data])

  const worst: Light = counts.error > 0 ? 'red' : counts.silent > 0 ? 'amber' : 'green'

  return (
    <Section
      title="Acquisition"
      subtitle="source firing"
      worst={worst}
      tally={[
        { light: 'green', label: 'firing', n: counts.firing },
        { light: 'amber', label: 'silent', n: counts.silent },
        { light: 'red', label: 'error', n: counts.error },
        { light: 'grey', label: 'paused', n: counts.paused },
      ]}
      loading={isLoading}
      error={error}
      testid="status-acquisition"
    >
      {!isLoading && rows.length === 0 && <EmptyRow>no sources registered</EmptyRow>}
      {rows.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800">
            <tr>
              <th className="py-1 px-1 text-left">source</th>
              <th className="py-1 px-1 text-left">state</th>
              <th className="py-1 px-1 text-right">24h</th>
              <th className="py-1 px-1 text-right">7d</th>
              <th className="py-1 px-1 text-left">last seen</th>
              <th className="py-1 px-1 text-left">freshness</th>
              <th className="py-1 px-1 text-left">status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const light = sourceLight(r.status)
              const freshLight = FRESHNESS_TONE_LIGHT[freshnessTone(r.freshness_grade)]
              return (
                <tr
                  key={r.source_id}
                  className="border-b border-slate-800/40 hover:bg-surface-200"
                  data-testid={`status-source-${r.source_id}`}
                >
                  <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[180px]">
                    {r.source_id}
                  </td>
                  <td className="py-1 px-1 text-slate-500">{r.state ?? '—'}</td>
                  <td className={`py-1 px-1 text-right font-mono ${LIGHT_NUM[light]}`}>
                    {r.signals_24h}
                  </td>
                  <td className="py-1 px-1 text-right font-mono text-slate-400">
                    {r.signals_7d}
                  </td>
                  <td className="py-1 px-1 text-slate-500">
                    {ageLabel(r.age_seconds, r.last_seen_at)}
                    {r.recent_error_count > 0 && (
                      <span className="text-rose-400"> · {r.recent_error_count} err</span>
                    )}
                  </td>
                  <td className="py-1 px-1" data-testid={`status-source-freshness-${r.source_id}`}>
                    <span
                      className="inline-flex items-center gap-1"
                      title={freshnessTitle(r.freshness_grade, r.budget_minutes)}
                    >
                      <span
                        className={`h-1.5 w-1.5 shrink-0 rounded-full ${LIGHT_DOT[freshLight]}`}
                        aria-hidden
                      />
                      <span className={LIGHT_NUM[freshLight]}>{r.freshness_grade}</span>
                    </span>
                  </td>
                  <td className="py-1 px-1">
                    <StatusPill
                      light={light}
                      label={r.status}
                      testid={`status-source-pill-${r.status}`}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

// ===========================================================================
// ANALYSIS — per-analyst cadence
// ===========================================================================

function analystLight(status: string): Light {
  switch (status) {
    case 'healthy':
      return 'green'
    case 'stale':
      return 'amber'
    case 'never':
      return 'red'
    default:
      return 'grey'
  }
}

function AnalysisSection() {
  const { data, isLoading, error } = useQuery<AnalystCadenceRow[]>({
    queryKey: ['system-status', 'analyst-cadence'],
    queryFn: async () => {
      try {
        return await getSystemAnalystCadence()
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: POLL_MS,
  })

  const rows = useMemo(() => {
    // Problem-first: never → stale → healthy, then by id.
    const order: Record<Light, number> = { red: 0, amber: 1, grey: 2, green: 3 }
    return [...(data ?? [])].sort((a, b) => {
      const d = order[analystLight(a.status)] - order[analystLight(b.status)]
      return d !== 0 ? d : a.analyst_id.localeCompare(b.analyst_id)
    })
  }, [data])

  const counts = useMemo(() => {
    const c = { healthy: 0, stale: 0, never: 0 }
    for (const r of data ?? []) {
      if (r.status === 'healthy') c.healthy++
      else if (r.status === 'stale') c.stale++
      else if (r.status === 'never') c.never++
    }
    return c
  }, [data])

  const worst: Light =
    counts.never > 0 ? 'red' : counts.stale > 0 ? 'amber' : 'green'

  return (
    <Section
      title="Analysis"
      subtitle="analyst cadence"
      worst={worst}
      tally={[
        { light: 'green', label: 'healthy', n: counts.healthy },
        { light: 'amber', label: 'stale', n: counts.stale },
        { light: 'red', label: 'never', n: counts.never },
      ]}
      loading={isLoading}
      error={error}
      testid="status-analysis"
    >
      {!isLoading && rows.length === 0 && <EmptyRow>no analyst runs recorded</EmptyRow>}
      {rows.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800">
            <tr>
              <th className="py-1 px-1 text-left">analyst</th>
              <th className="py-1 px-1 text-left">last run</th>
              <th className="py-1 px-1 text-right">1h</th>
              <th className="py-1 px-1 text-right">24h</th>
              <th className="py-1 px-1 text-left">outcome</th>
              <th className="py-1 px-1 text-left">status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const light = analystLight(r.status)
              return (
                <tr
                  key={r.analyst_id}
                  className="border-b border-slate-800/40 hover:bg-surface-200"
                  data-testid={`status-analyst-${r.analyst_id}`}
                >
                  <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[180px]">
                    {r.analyst_id}
                  </td>
                  <td className={`py-1 px-1 ${LIGHT_NUM[light]}`}>
                    {ageLabel(r.age_seconds, r.last_run_at)}
                  </td>
                  <td className="py-1 px-1 text-right font-mono text-slate-400">
                    {r.runs_1h}
                  </td>
                  <td className="py-1 px-1 text-right font-mono text-slate-400">
                    {r.runs_24h}
                  </td>
                  <td className="py-1 px-1 text-slate-500 truncate max-w-[120px]">
                    {r.last_outcome ?? '—'}
                  </td>
                  <td className="py-1 px-1">
                    <StatusPill
                      light={light}
                      label={r.status}
                      testid={`status-analyst-pill-${r.status}`}
                    />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

// ===========================================================================
// QUEUES — consumer-lag rollup (already orphan-filtered server-side)
// ===========================================================================

function QueuesSection() {
  const { data, isLoading, error } = useQuery<ConsumerLagRow[]>({
    queryKey: ['system-status', 'consumer-lag'],
    queryFn: async () => {
      try {
        return await apiGet<ConsumerLagRow[]>('/v3/streams/consumer_lag')
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: POLL_MS,
  })

  // Only real backlog: show consumers that are warn/critical, worst-first.
  const backlog = useMemo(() => {
    const rows = (data ?? []).filter((r) => lagSeverity(r) !== 'ok')
    return sortLag(rows)
  }, [data])

  const counts = useMemo(() => {
    let critical = 0
    let warn = 0
    let totalPending = 0
    for (const r of data ?? []) {
      const s = lagSeverity(r)
      if (s === 'critical') critical++
      else if (s === 'warn') warn++
      totalPending += r.num_pending
    }
    return { critical, warn, totalPending, total: (data ?? []).length }
  }, [data])

  const worst: Light = counts.critical > 0 ? 'red' : counts.warn > 0 ? 'amber' : 'green'

  return (
    <Section
      title="Queues"
      subtitle={`${counts.total} consumers · ${counts.totalPending} pending`}
      worst={worst}
      tally={[
        { light: 'red', label: 'critical', n: counts.critical },
        { light: 'amber', label: 'warn', n: counts.warn },
      ]}
      loading={isLoading}
      error={error}
      testid="status-queues"
    >
      {!isLoading && backlog.length === 0 && (
        <EmptyRow>no backlog — every consumer is caught up</EmptyRow>
      )}
      {backlog.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800">
            <tr>
              <th className="py-1 px-1 text-left">consumer</th>
              <th className="py-1 px-1 text-left">scope</th>
              <th className="py-1 px-1 text-right">pending</th>
              <th className="py-1 px-1 text-right">unacked</th>
              <th className="py-1 px-1 text-right">redeliv</th>
              <th className="py-1 px-1 text-left">health</th>
            </tr>
          </thead>
          <tbody>
            {backlog.map((r) => {
              const sev = lagSeverity(r)
              const light: Light = sev === 'critical' ? 'red' : 'amber'
              return (
                <tr
                  key={`${r.stream}:${r.durable}`}
                  className="border-b border-slate-800/40 hover:bg-surface-200"
                  data-testid={`status-queue-${r.scope_id}`}
                >
                  <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[160px]">
                    {r.durable}
                  </td>
                  <td className="py-1 px-1 text-slate-500">
                    <span className="text-slate-600">{r.scope_kind}/</span>
                    {r.scope_id}
                  </td>
                  <td className={`py-1 px-1 text-right font-mono ${LIGHT_NUM[light]}`}>
                    {r.num_pending}
                  </td>
                  <td className="py-1 px-1 text-right font-mono text-slate-400">
                    {r.num_ack_pending}
                  </td>
                  <td
                    className={`py-1 px-1 text-right font-mono ${
                      r.num_redelivered > 0 ? 'text-rose-400' : 'text-slate-500'
                    }`}
                  >
                    {r.num_redelivered}
                  </td>
                  <td className="py-1 px-1">
                    <StatusPill light={light} label={sev} testid={`status-queue-sev-${sev}`} />
                  </td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

// ===========================================================================
// INFRA — runtime actor health
// ===========================================================================

interface ActorRow {
  actor_id: string
  actor_kind: string
  lifecycle: string
  last_run_at: string | null
  error_count: number
}

function infraLight(lifecycle: string, errorCount: number): Light {
  if (lifecycle === 'error' || errorCount > 0) return 'red'
  if (lifecycle === 'paused') return 'amber'
  if (lifecycle === 'retired') return 'grey'
  if (lifecycle === 'active') return 'green'
  return 'grey'
}

function InfraSection() {
  const { data, isLoading, error } = useQuery<ActorRow[]>({
    queryKey: ['system-status', 'actors'],
    queryFn: async () => {
      try {
        return await apiGet<ActorRow[]>('/v3/runtime/actors')
      } catch (e) {
        if (e instanceof ApiError && e.status === 404) return []
        throw e
      }
    },
    refetchInterval: POLL_MS,
  })

  const counts = useMemo(() => {
    let healthy = 0
    let degraded = 0 // error lifecycle or any error_count
    let paused = 0
    for (const r of data ?? []) {
      const light = infraLight(r.lifecycle, r.error_count)
      if (light === 'red') degraded++
      else if (light === 'amber') paused++
      else if (light === 'green') healthy++
    }
    return { healthy, degraded, paused, total: (data ?? []).length }
  }, [data])

  // Only surface actors that need attention (degraded/paused), worst-first.
  const attention = useMemo(() => {
    const order: Record<Light, number> = { red: 0, amber: 1, grey: 2, green: 3 }
    return [...(data ?? [])]
      .filter((r) => {
        const l = infraLight(r.lifecycle, r.error_count)
        return l === 'red' || l === 'amber'
      })
      .sort((a, b) => {
        const d =
          order[infraLight(a.lifecycle, a.error_count)] -
          order[infraLight(b.lifecycle, b.error_count)]
        return d !== 0 ? d : a.actor_id.localeCompare(b.actor_id)
      })
  }, [data])

  const worst: Light = counts.degraded > 0 ? 'red' : counts.paused > 0 ? 'amber' : 'green'

  return (
    <Section
      title="Infra"
      subtitle={`${counts.total} actors`}
      worst={worst}
      tally={[
        { light: 'green', label: 'healthy', n: counts.healthy },
        { light: 'amber', label: 'paused', n: counts.paused },
        { light: 'red', label: 'degraded', n: counts.degraded },
      ]}
      loading={isLoading}
      error={error}
      testid="status-infra"
    >
      {!isLoading && attention.length === 0 && (
        <EmptyRow>all actors healthy</EmptyRow>
      )}
      {attention.length > 0 && (
        <table className="w-full text-xs">
          <thead className="text-slate-500 text-[10px] uppercase tracking-wide border-b border-slate-800">
            <tr>
              <th className="py-1 px-1 text-left">actor</th>
              <th className="py-1 px-1 text-left">kind</th>
              <th className="py-1 px-1 text-left">lifecycle</th>
              <th className="py-1 px-1 text-right">errors</th>
              <th className="py-1 px-1 text-left">last run</th>
            </tr>
          </thead>
          <tbody>
            {attention.map((r) => {
              const light = infraLight(r.lifecycle, r.error_count)
              return (
                <tr
                  key={r.actor_id}
                  className="border-b border-slate-800/40 hover:bg-surface-200"
                  data-testid={`status-actor-${r.actor_id}`}
                >
                  <td className="py-1 px-1 font-mono text-slate-300 truncate max-w-[180px]">
                    {r.actor_id}
                  </td>
                  <td className="py-1 px-1 text-slate-500">{r.actor_kind}</td>
                  <td className="py-1 px-1">
                    <StatusPill light={light} label={r.lifecycle} />
                  </td>
                  <td
                    className={`py-1 px-1 text-right font-mono ${
                      r.error_count > 0 ? 'text-rose-400' : 'text-slate-500'
                    }`}
                  >
                    {r.error_count}
                  </td>
                  <td className="py-1 px-1 text-slate-500">{relTime(r.last_run_at)}</td>
                </tr>
              )
            })}
          </tbody>
        </table>
      )}
    </Section>
  )
}

// ===========================================================================
// Panel root
// ===========================================================================

export default function SystemStatusPanel({ registration }: PanelProps) {
  return (
    <PanelChrome
      registration={registration}
      subtitle="acquisition · analysis · queues · infra — at a glance"
    >
      <div className="text-xs" data-testid="system-status">
        <AcquisitionSection />
        <AnalysisSection />
        <QueuesSection />
        <InfraSection />
      </div>
    </PanelChrome>
  )
}

/**
 * The Wall (`system.wall`) — the mission-control anchor tile (P1-7).
 *
 * One glanceable screen, four quadrants, no scrolling of the tile itself
 * (each quadrant clips + scrolls its own overflow):
 *
 *   Q1  WORLD AT A GLANCE — the compact per-desk band grid over the SAME data
 *       as the World map's banded-verdict choropleth (`useCountryVerdicts` +
 *       `CONFIDENCE_FILL`), so grid chip and map band never disagree. The grid
 *       (not an embedded MapLibre) keeps the quadrant glanceable: the maplibre
 *       renderer needs the TileWebGLOverlay portal harness + Layer/Drawer/
 *       Scrubber chrome, which fights a quarter-tile. Chip click selects the
 *       desk target into the Inspector.
 *   Q2  MOVERS SINCE LAST VISIT — `GET /v3/since` with the client-owned cursor
 *       (`localStorage.legba_wall_cursor`; first-ever open = 24h lookback).
 *       Band changes first (direction-colored), then the superseded-reversal
 *       count, then situation lifecycle edges. Honest empty state.
 *   Q3  NEWEST HIGH-SEVERITY VERIFIED — top 5 of the since-window's verified
 *       findings (already verified-only server-side), severity-badged; each
 *       row selects into the unified selection store → Inspector.
 *   Q4  SYSTEM HEALTH — rollup over the System Status routes
 *       (`/v3/system/source-firing`, `/v3/system/analyst-cadence`): signal
 *       volume + sub-hour source liveness + stale analysts + source errors.
 *
 * Cursor lifecycle: resolved ONCE per mount (`resolveWallCursor`), so the
 * visit keeps diffing from the same anchor while polling; every successful
 * response advances `localStorage.legba_wall_cursor` to its `server_now`
 * (never backwards), so the NEXT open diffs from the last moment this wall
 * was live. All non-DOM logic lives in `lib/wallModel.ts`.
 */
import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { ArrowDownRight, ArrowUpRight, Minus } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { SeverityBadge } from '@/components/SeverityBadge'
import { apiGet, getSystemAnalystCadence, getSystemSourceFiring } from '@/lib/api'
import { relTime } from '@/lib/evalOps'
import {
  bandChangeDeskLabel,
  buildMovers,
  healthRollup,
  loadWallCursor,
  resolveWallCursor,
  sincePath,
  storeWallCursor,
  topSevereVerified,
  type BandChange,
  type SinceResponse,
  type SituationChange,
} from '@/lib/wallModel'
import { CHOROPLETH_LEGEND, CONFIDENCE_FILL, useCountryVerdicts } from '@/v4/world/countryVerdicts'
import { selectRow } from '@/state/selection'
import { ProvenanceStateBadge } from '@/components/ProvenanceBadge'
import { resolveNumberProvenance, type ProvenanceState } from '@/lib/provenance'
import type { Severity } from '@/v4/world/types'
import type { PanelProps } from '@/types'

const SINCE_POLL_MS = 60_000
const HEALTH_POLL_MS = 30_000

// ---------------------------------------------------------------------------
// Quadrant shell
// ---------------------------------------------------------------------------

function Quadrant({
  title,
  aside,
  testid,
  children,
}: {
  title: string
  aside?: React.ReactNode
  testid: string
  children: React.ReactNode
}) {
  return (
    <section
      className="flex min-h-0 flex-col rounded border border-line bg-surf-3/40 p-2"
      data-testid={testid}
    >
      <header className="mb-1.5 flex shrink-0 items-baseline justify-between gap-2">
        <span className="text-label font-semibold uppercase tracking-wider text-ink-2">
          {title}
        </span>
        {aside && <span className="min-w-0 truncate text-label text-ink-3">{aside}</span>}
      </header>
      <div className="min-h-0 flex-1 overflow-y-auto">{children}</div>
    </section>
  )
}

// ---------------------------------------------------------------------------
// Q1 — world at a glance (per-desk band grid; the choropleth's data + colors)
// ---------------------------------------------------------------------------

function BandGridQuadrant() {
  const { verdicts, isLoading } = useCountryVerdicts()
  const desks = useMemo(
    () => [...verdicts.values()].sort((a, b) => a.iso2.localeCompare(b.iso2)),
    [verdicts],
  )
  return (
    <Quadrant
      title="World at a glance"
      aside={desks.length > 0 ? `${desks.length} assessed desks` : undefined}
      testid="wall-band-grid"
    >
      {isLoading && desks.length === 0 && (
        <div className="py-4 text-center text-label text-ink-3">loading verdicts…</div>
      )}
      {!isLoading && desks.length === 0 && (
        <div className="py-4 text-center text-label text-ink-3">
          no verified country compositions yet
        </div>
      )}
      <div className="flex flex-wrap gap-1">
        {desks.map((d) => (
          <button
            key={d.iso2}
            type="button"
            onClick={() =>
              selectRow('target', d.targetId || d.iso2, d.title, { origin: 'wall' })
            }
            title={`${d.title} — confidence: ${d.verdict.confidence} · ${relTime(d.producedAt)}`}
            data-testid={`wall-desk-${d.iso2}`}
            className="inline-flex items-center gap-1 rounded border border-line bg-surf-2 px-1.5 py-0.5 font-mono text-[11px] text-ink-1 hover:border-line-strong hover:bg-surf-1"
          >
            <span
              className="h-2 w-2 rounded-sm"
              style={{ backgroundColor: CONFIDENCE_FILL[d.verdict.confidence] }}
              aria-hidden
            />
            {d.iso2}
          </button>
        ))}
      </div>
      {/* The choropleth legend — same ramp, same vocabulary as the World map. */}
      <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-[10px] text-ink-3">
        {CHOROPLETH_LEGEND.map((l) => (
          <span key={l.level} className="inline-flex items-center gap-1">
            <span
              className="h-1.5 w-1.5 rounded-sm"
              style={{ backgroundColor: CONFIDENCE_FILL[l.level] }}
              aria-hidden
            />
            {l.label}
          </span>
        ))}
      </div>
    </Quadrant>
  )
}

// ---------------------------------------------------------------------------
// Q2 + Q3 — the /since fetch (one query serves both quadrants)
// ---------------------------------------------------------------------------

/**
 * The `/v3/since` fetch + cursor lifecycle — exported so a standalone mount
 * of just the movers content (the boot-grid "Movers since last visit" tile,
 * `system.wall_movers` / `panels/system/WallMovers.tsx`) shares the EXACT
 * same cursor semantics as the full Wall: both read/advance the same
 * `legba_wall_cursor` localStorage key via `wallModel.ts`, so a desk never
 * sees two different "since" answers depending on which tile it opened.
 */
export function useSince() {
  // Resolve the cursor ONCE per mount: the whole visit diffs from the same
  // anchor; each successful poll advances the STORED cursor for the next open.
  const visit = useMemo(() => resolveWallCursor(loadWallCursor()), [])
  const q = useQuery<SinceResponse>({
    queryKey: ['wall-since', visit.cursor],
    queryFn: async () => {
      const res = await apiGet<SinceResponse>(sincePath(visit.cursor))
      storeWallCursor(res.server_now)
      return res
    },
    refetchInterval: SINCE_POLL_MS,
  })
  return { visit, ...q }
}

const TONE_TEXT: Record<'bad' | 'good' | 'neutral', string> = {
  bad: 'text-accent-critical',
  good: 'text-accent-ok',
  neutral: 'text-accent-warning',
}

function directionTone(direction: string): 'bad' | 'good' | 'neutral' {
  if (direction === 'deterioration') return 'bad'
  if (direction === 'improvement') return 'good'
  return 'neutral'
}

function BandChangeRow({ c }: { c: BandChange }) {
  const tone = directionTone(c.direction)
  const Icon = tone === 'bad' ? ArrowUpRight : tone === 'good' ? ArrowDownRight : Minus
  // U-2: the desk reads as its country name, not the raw `country_g20_br` id.
  const desk = bandChangeDeskLabel(c.target_id)
  return (
    <li className="flex items-center gap-1.5 text-[11px]" data-testid="wall-band-change" title={c.target_id}>
      <Icon className={`h-3 w-3 shrink-0 ${TONE_TEXT[tone]}`} aria-hidden />
      <span className="truncate text-ink-2">{desk}</span>
      <span className="truncate text-ink-3">{c.dimension}</span>
      <span className={`ml-auto shrink-0 font-mono ${TONE_TEXT[tone]}`}>
        {c.from_band}→{c.to_band}
      </span>
    </li>
  )
}

function SituationRow({ s }: { s: SituationChange }) {
  return (
    <li className="flex items-center gap-1.5 text-[11px]" data-testid="wall-situation-edge">
      <span className="shrink-0 rounded bg-surf-1 px-1 text-[10px] text-ink-2">{s.change}</span>
      <button
        type="button"
        className="min-w-0 truncate text-left text-ink-1 hover:underline"
        onClick={() => selectRow('situation', s.id, s.name, { origin: 'wall' })}
        title={s.name}
      >
        {s.name}
      </button>
      <span className="ml-auto shrink-0 text-ink-3">
        {s.from_status ? `${s.from_status}→` : ''}
        {s.to_status}
      </span>
    </li>
  )
}

/** The shared "since" props both the full Wall's movers quadrant and the
 *  standalone boot-grid movers tile (`WallMovers.tsx`) render from. */
export interface MoversContentProps {
  since: SinceResponse | undefined
  cursor: string
  firstVisit: boolean
  loading: boolean
  error: unknown
}

/**
 * The movers list itself — loading / error / honest-empty / grouped rows.
 * Exported UNWRAPPED (no `Quadrant` shell) so `WallMovers.tsx` can drop it
 * straight into its own `PanelChrome` without a doubled title bar (the
 * cosmetic double-chrome the U-3 merges flagged — see registry.ts's
 * `system.wall_movers` comment).
 */
export function MoversContent({ since, cursor, loading, error }: MoversContentProps) {
  const movers = since ? buildMovers(since) : null
  return (
    <>
      {loading && <div className="py-4 text-center text-label text-ink-3">diffing…</div>}
      {error instanceof Error && (
        <div className="py-2 text-label text-accent-critical">error: {error.message}</div>
      )}
      {movers && movers.isEmpty && (
        <div className="py-4 text-center text-label text-ink-3" data-testid="wall-movers-empty">
          nothing changed since {new Date(cursor).toLocaleString()}
        </div>
      )}
      {movers && !movers.isEmpty && (
        <div className="space-y-2">
          {movers.bandChanges.length > 0 && (
            <div>
              <div className="mb-0.5 text-[10px] uppercase tracking-wide text-ink-3">
                Band changes · {movers.bandTotal}
                {movers.bandTruncated ? ` (showing ${movers.bandChanges.length})` : ''}
              </div>
              <ul className="space-y-0.5">
                {movers.bandChanges.map((c) => (
                  <BandChangeRow key={`${c.target_id}:${c.dimension}:${c.changed_at}`} c={c} />
                ))}
              </ul>
            </div>
          )}
          {movers.supersededCount > 0 && (
            <div className="text-[11px] text-ink-2" data-testid="wall-superseded-count">
              <span className="font-mono text-accent-warning">{movers.supersededCount}</span>{' '}
              finding{movers.supersededCount === 1 ? '' : 's'} superseded (reversals of record)
            </div>
          )}
          {movers.situationEdges.length > 0 && (
            <div>
              <div className="mb-0.5 text-[10px] uppercase tracking-wide text-ink-3">
                Situations · {movers.situationTotal}
                {movers.situationTruncated ? ` (showing ${movers.situationEdges.length})` : ''}
              </div>
              <ul className="space-y-0.5">
                {movers.situationEdges.map((s) => (
                  <SituationRow key={s.id} s={s} />
                ))}
              </ul>
            </div>
          )}
          {movers.alertCount > 0 && (
            <div className="text-[11px] text-ink-2">
              <span className="font-mono text-accent-critical">{movers.alertCount}</span> alert
              {movers.alertCount === 1 ? '' : 's'} in the window
            </div>
          )}
        </div>
      )}
    </>
  )
}

function MoversQuadrant({ since, cursor, firstVisit, loading, error }: MoversContentProps) {
  const sinceLabel = firstVisit ? 'first visit — last 24h' : `since ${relTime(cursor)}`
  return (
    <Quadrant title="Movers since last visit" aside={sinceLabel} testid="wall-movers">
      <MoversContent since={since} cursor={cursor} firstVisit={firstVisit} loading={loading} error={error} />
    </Quadrant>
  )
}

function NewestFindingsQuadrant({
  since,
  loading,
}: {
  since: SinceResponse | undefined
  loading: boolean
}) {
  const top = since ? topSevereVerified(since.new_findings.items, 5) : []
  const total = since?.new_findings.total ?? 0
  return (
    <Quadrant
      title="Newest verified · by severity"
      aside={since ? `${total} new verified` : undefined}
      testid="wall-new-findings"
    >
      {loading && <div className="py-4 text-center text-label text-ink-3">loading…</div>}
      {since && top.length === 0 && (
        <div className="py-4 text-center text-label text-ink-3">
          no new verified findings in the window
        </div>
      )}
      <ul className="space-y-1">
        {top.map((f) => (
          <li key={f.id} data-testid="wall-finding-row">
            <button
              type="button"
              className="flex w-full items-center gap-2 rounded px-1 py-0.5 text-left text-[11px] hover:bg-surf-1"
              onClick={() =>
                selectRow('finding', f.id, f.title, {
                  origin: 'wall',
                  preview: {
                    title: f.title,
                    severity: f.severity,
                    analystId: f.analyst_id,
                    targetId: f.target_id,
                  },
                })
              }
              title={f.title}
            >
              <SeverityBadge severity={(f.severity ?? 'info') as Severity} className="shrink-0" />
              <span className="min-w-0 flex-1 truncate text-ink-1">{f.title}</span>
              <span className="shrink-0 font-mono text-[10px] text-ink-3">
                {Math.round(f.effective_confidence * 100)}%
              </span>
              <span className="shrink-0 text-[10px] text-ink-3">{relTime(f.produced_at)}</span>
            </button>
          </li>
        ))}
      </ul>
    </Quadrant>
  )
}

// ---------------------------------------------------------------------------
// Q4 — system health rollup
// ---------------------------------------------------------------------------

const WORST_DOT: Record<'green' | 'amber' | 'red', string> = {
  green: 'bg-accent-ok',
  amber: 'bg-accent-warning',
  red: 'bg-accent-critical',
}

function Stat({
  label,
  value,
  tone,
  testid,
  provenance,
}: {
  label: string
  value: string
  tone?: 'ok' | 'warn' | 'bad'
  testid?: string
  /** P4-5 live|fallback|absent stamp on this displayed number. */
  provenance?: ProvenanceState
}) {
  const color =
    tone === 'bad'
      ? 'text-accent-critical'
      : tone === 'warn'
        ? 'text-accent-warning'
        : 'text-ink-1'
  return (
    <div className="rounded border border-line bg-surf-2 px-2 py-1.5" data-testid={testid}>
      <div className="flex items-baseline justify-between gap-1">
        <div className={`font-mono text-base leading-tight ${color}`}>{value}</div>
        {provenance && <ProvenanceStateBadge state={provenance} />}
      </div>
      <div className="text-[10px] leading-tight text-ink-3">{label}</div>
    </div>
  )
}

function HealthQuadrant() {
  const sources = useQuery({
    queryKey: ['wall-source-firing'],
    queryFn: getSystemSourceFiring,
    refetchInterval: HEALTH_POLL_MS,
  })
  const analysts = useQuery({
    queryKey: ['wall-analyst-cadence'],
    queryFn: getSystemAnalystCadence,
    refetchInterval: HEALTH_POLL_MS,
  })
  const loading = sources.isLoading || analysts.isLoading
  const error = sources.error ?? analysts.error
  const h = healthRollup(sources.data ?? [], analysts.data ?? [])
  // P4-5 — live|fallback|absent on the health numbers. These roll up two live
  // routes; a number is `live` when its backing route returned rows, `absent`
  // when it returned none (an honest empty, not a fabricated zero). The routes
  // carry NO fallback-vs-live signal today, so `fallback` is never emitted here
  // — that is the seam a backend fallback-flag follow-up would fill (pass an
  // explicit `fallback` into resolveNumberProvenance once the route sets one).
  const srcLoaded = !sources.error && (sources.data?.length ?? 0) > 0
  const analystLoaded = !analysts.error && (analysts.data?.length ?? 0) > 0
  const srcState = resolveNumberProvenance({ value: srcLoaded ? 1 : null })
  const analystState = resolveNumberProvenance({ value: analystLoaded ? 1 : null })
  return (
    <Quadrant
      title="System health"
      aside={
        <span className="inline-flex items-center gap-1">
          <span className={`h-2 w-2 rounded-full ${WORST_DOT[h.worst]}`} aria-hidden />
          {h.worst}
        </span>
      }
      testid="wall-health"
    >
      {loading && <div className="py-4 text-center text-label text-ink-3">loading…</div>}
      {error instanceof Error && (
        <div className="py-2 text-label text-accent-critical">error: {error.message}</div>
      )}
      {!loading && (
        <div className="grid grid-cols-2 gap-1.5">
          {/* source-firing carries no per-hour signal count — the honest
              sub-hour liveness read is sources-seen-<1h (see wallModel). */}
          <Stat
            label={`sources w/ signal <1h (of ${h.sourcesTotal})`}
            value={String(h.sourcesSeenLastHour)}
            tone={h.sourcesTotal > 0 && h.sourcesSeenLastHour === 0 ? 'warn' : 'ok'}
            testid="wall-health-sources-1h"
            provenance={srcState}
          />
          <Stat
            label="signals 24h (all sources)"
            value={String(h.signals24h)}
            tone="ok"
            provenance={srcState}
          />
          <Stat
            label={`stale analysts (of ${h.analystsTotal}${h.analystsNever > 0 ? `, ${h.analystsNever} never ran` : ''})`}
            value={String(h.analystsStale)}
            tone={h.analystsNever > 0 ? 'bad' : h.analystsStale > 0 ? 'warn' : 'ok'}
            testid="wall-health-analysts"
            provenance={analystState}
          />
          <Stat
            label="sources erroring"
            value={String(h.sourceErrors)}
            tone={h.sourceErrors > 0 ? 'bad' : 'ok'}
            testid="wall-health-source-errors"
            provenance={srcState}
          />
        </div>
      )}
    </Quadrant>
  )
}

// ---------------------------------------------------------------------------
// Panel root — the 2×2 wall
// ---------------------------------------------------------------------------

export default function WallPanel({ registration }: PanelProps) {
  const { visit, data, isLoading, error, refetch } = useSince()
  return (
    <PanelChrome
      registration={registration}
      subtitle="world at a glance · movers since last visit"
      onRefresh={() => refetch()}
    >
      <div
        className="grid h-full min-h-[480px] grid-cols-1 grid-rows-4 gap-2 md:grid-cols-2 md:grid-rows-2"
        data-testid="wall-grid"
      >
        <BandGridQuadrant />
        <MoversQuadrant
          since={data}
          cursor={visit.cursor}
          firstVisit={visit.firstVisit}
          loading={isLoading}
          error={error}
        />
        <NewestFindingsQuadrant since={data} loading={isLoading} />
        <HealthQuadrant />
      </div>
    </PanelChrome>
  )
}

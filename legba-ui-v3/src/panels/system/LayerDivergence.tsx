/**
 * Layer Divergence (`system.layer_divergence`) — THE DIVERGENCE MAP.
 *
 * Program 6 L2's reader surface: per country, the six source layers, the three
 * gaps worth watching, and what the instrument said about each of them today.
 * It is one of the four checking surfaces the next-arc capture named as the
 * thing that has to become VISIBLE on the reader surface — "cited source beside
 * each claim, declared gaps, confidence folds with their reason, the divergence
 * map" — and it is the only one that had no surface at all.
 *
 * WHAT IT RENDERS, in the order the question is asked:
 *   1. THE RECEIPT HEADER — what the unit measures (the server's own sentence,
 *      not a copy in this bundle), the as-of UTC day, the run time, and the
 *      statistic's own stamps: method version, payload schema, window, baseline,
 *      threshold, the two-day rule and the thin floor. The August intensity
 *      re-base is the motivating case for stamping the scale on every read.
 *   2. THE AUDIT BANNER — SEAMS #60, verbatim. A source filed under the wrong
 *      layer is a DIRECTIONAL error in every number on this page, and no reader
 *      may mistake an un-audited map for an audited one.
 *   3. ONE CARD PER DESK, worst news first: the six layers as chips carrying
 *      their declaration, then the three pairs, each with its state, its
 *      sparkline, and — when it fired — a link into the finding.
 *
 * HONESTY RULES this panel holds to:
 *   * `measured: false` is a FAILED READ, not a quiet instrument. The server
 *     degrades to an all-defaults 200 rather than 500ing a polling panel, so
 *     this field is the only thing separating the two and it is rendered loudly.
 *   * "No run yet" is its own state. An empty desk list with no receipt id
 *     means the unit has never written one — not that it measured nothing.
 *   * `no_fire_reason` is shown BY NAME. "aperture_excluded" and
 *     "below_threshold" are different findings and this panel never folds them
 *     into a single grey "no".
 *   * An excluded layer shows WHY, in the operator's own sentence from the
 *     aperture declaration. A missing layer that did not say why would read as
 *     agreement, which is the exact failure the declaration exists to rule out.
 *   * A null z renders as "—", never 0.00, and the z track BREAKS across
 *     unscored days rather than drawing a line through them.
 *   * Every figure carries its unit and its as-of; the counts are stated as
 *     items/day after the wire fold, with raw and folded in the tooltip.
 */

import { useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { AlertTriangle, ChevronDown, ChevronRight, Layers } from 'lucide-react'
import { PanelChrome } from '@/components/PanelChrome'
import { RecordLink } from '@/components/inspector/RecordLink'
import { cn } from '@/lib/cn'
import { fetchLayerDivergence } from '@/lib/api'
import type {
  LayerDivergenceDesk,
  LayerDivergencePair,
  LayerDivergenceResponse,
} from '@/lib/api'
import {
  UNMEASURED,
  deskLine,
  exclusionNotes,
  formatZ,
  lastPoint,
  louderLayer,
  orderDesks,
  pairState,
  pairedSparkline,
  summaryLine,
  zSide,
  type PairState,
} from '@/lib/layerDivergence'
import type { PanelProps } from '@/types'

/** Server bounds are [1, 365]; it 422s outside them rather than clamping. */
const FIRED_WINDOWS = [7, 30, 90] as const

const STATE_TONE: Record<PairState, string> = {
  fired: 'border-severity-high/50 bg-severity-high/15 text-severity-high',
  thin: 'border-accent-warning/45 bg-accent-warning/10 text-accent-warning',
  aperture_excluded: 'border-line-strong bg-surf-1 text-ink-3',
  below_threshold: 'border-line bg-surf-1 text-ink-2',
  evaluable: 'border-line bg-surf-1 text-ink-2',
}

const STATE_LABEL: Record<PairState, string> = {
  fired: 'fired',
  thin: 'thin',
  aperture_excluded: 'not evaluable',
  below_threshold: 'below threshold',
  evaluable: 'evaluated',
}

const DECLARED_TONE: Record<string, string> = {
  present: 'border-accent-ok/40 bg-accent-ok/10 text-accent-ok',
  absent: 'border-accent-warning/45 bg-accent-warning/10 text-accent-warning',
  unmeasured: 'border-line-strong bg-surf-1 text-ink-3',
  undeclared: 'border-severity-high/40 bg-severity-high/10 text-severity-high',
}

function Stamp({ label, value }: { label: string; value: string }) {
  return (
    <span className="whitespace-nowrap text-label text-ink-3">
      <span className="text-ink-3/70">{label}</span>{' '}
      <span className="font-mono text-ink-2">{value}</span>
    </span>
  )
}

/**
 * The paired sparkline: two kept-count lines over one shared axis, and the z
 * track beneath them with the firing threshold drawn as two rules.
 */
function PairSparkline({
  pair,
  zThreshold,
  testId,
}: {
  pair: LayerDivergencePair
  zThreshold: number | null
  testId: string
}) {
  const geo = useMemo(
    () => pairedSparkline(pair.series, { zThreshold }),
    [pair.series, zThreshold],
  )
  if (geo == null) return null
  const gap = 6
  const total = geo.countHeight + gap + geo.zHeight
  return (
    <svg
      viewBox={`0 0 ${geo.width} ${total}`}
      width="100%"
      height={total}
      preserveAspectRatio="none"
      role="img"
      aria-label={
        `${pair.layer_a} and ${pair.layer_b}, items/day after the wire fold, ` +
        `over ${geo.days.length} days; z track beneath`
      }
      data-testid={testId}
      data-count-max={geo.countMax}
      data-z-max={geo.zMax ?? ''}
      data-z-segments={geo.zSegments.length}
    >
      <polyline
        points={geo.aPoints}
        fill="none"
        stroke="var(--conf-high, #79c0ff)"
        strokeWidth={1.25}
      />
      <polyline
        points={geo.bPoints}
        fill="none"
        stroke="var(--ink-3, #8b949e)"
        strokeWidth={1.25}
        strokeDasharray="3 2"
      />
      <g transform={`translate(0 ${geo.countHeight + gap})`}>
        <line
          x1={0}
          x2={geo.width}
          y1={geo.zZeroY}
          y2={geo.zZeroY}
          stroke="var(--line-1, #30363d)"
          strokeWidth={0.75}
        />
        {geo.zThresholdY != null && (
          <>
            <line
              x1={0}
              x2={geo.width}
              y1={geo.zThresholdY.hi}
              y2={geo.zThresholdY.hi}
              stroke="var(--sev-high, #db6d28)"
              strokeWidth={0.5}
              strokeDasharray="2 3"
            />
            <line
              x1={0}
              x2={geo.width}
              y1={geo.zThresholdY.lo}
              y2={geo.zThresholdY.lo}
              stroke="var(--sev-high, #db6d28)"
              strokeWidth={0.5}
              strokeDasharray="2 3"
            />
          </>
        )}
        {geo.zSegments.map((seg, i) => (
          <polyline
            key={i}
            points={seg}
            fill="none"
            stroke="var(--sev-medium, #d29922)"
            strokeWidth={1.25}
          />
        ))}
      </g>
    </svg>
  )
}

function PairRow({
  desk,
  pair,
  meaning,
  zThreshold,
  thinMinPerDay,
}: {
  desk: LayerDivergenceDesk
  pair: LayerDivergencePair
  meaning: string | undefined
  zThreshold: number | null
  thinMinPerDay: number | null
}) {
  const state = pairState(pair, desk.fired, thinMinPerDay)
  const point = lastPoint(pair)
  const louder = louderLayer(pair)
  const side = zSide(point?.z)
  const fired = desk.fired != null && desk.fired.pair_id === pair.pair_id
    ? desk.fired
    : null
  const key = `${desk.target_id}-${pair.pair_id}`

  return (
    <div
      className="grid grid-cols-[minmax(0,11rem)_minmax(0,1fr)_minmax(0,13rem)] gap-density-gap border-t border-line py-1.5"
      data-testid={`pair-row-${key}`}
    >
      <div className="min-w-0">
        <div className="flex items-center gap-1">
          <span
            className={cn(
              'rounded border px-1 py-px text-label',
              STATE_TONE[state],
            )}
            data-testid={`pair-state-${key}`}
          >
            {STATE_LABEL[state]}
          </span>
          {fired?.thin === true && state === 'fired' && (
            <span
              className="rounded border border-accent-warning/45 bg-accent-warning/10 px-1 py-px text-label text-accent-warning"
              data-testid={`pair-thin-${key}`}
            >
              thin
            </span>
          )}
        </div>
        <div className="mt-0.5 truncate font-mono text-label text-ink-2" title={pair.pair_id}>
          {pair.layer_a} ÷ {pair.layer_b}
        </div>
        {meaning && (
          <div className="mt-0.5 text-label leading-tight text-ink-3">{meaning}</div>
        )}
      </div>

      <div className="min-w-0">
        {pair.evaluable ? (
          <PairSparkline
            pair={pair}
            zThreshold={zThreshold}
            testId={`pair-spark-${key}`}
          />
        ) : (
          <div
            className="space-y-1 text-label text-ink-2"
            data-testid={`pair-exclusion-${key}`}
          >
            {exclusionNotes(pair).map((note) => (
              <div key={note.layer}>
                <span className="font-mono text-ink-1">{note.layer}</span>{' '}
                <span className="text-ink-3">declared {note.state}</span>
                {note.reason ? (
                  <span className="text-ink-3"> — {note.reason}</span>
                ) : (
                  <span className="text-ink-3">
                    {' '}
                    — no row in the aperture declaration; nobody has said either way
                  </span>
                )}
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="min-w-0 text-label">
        <div className="text-ink-3">
          did not fire:{' '}
          <span className="font-mono text-ink-2">
            {pair.no_fire_reason || 'fired'}
          </span>
        </div>
        {pair.evaluable && (
          <div className="mt-0.5 text-ink-2" data-testid={`pair-lastday-${key}`}>
            {point ? (
              <>
                <span className="font-mono">{formatZ(point.z)}</span>
                {side && (
                  <span className="text-ink-3">
                    {' '}
                    {side} its own {point.baseline?.n ?? UNMEASURED}-day baseline
                  </span>
                )}
                <div
                  className="text-ink-3"
                  title={`${pair.layer_a}: ${point.a} · ${pair.layer_b}: ${point.b} items/day after the wire fold`}
                >
                  {point.day}: {point.a} / {point.b} items/day
                  {louder && (
                    <> · {louder.layer} louder</>
                  )}
                </div>
              </>
            ) : (
              <span className="text-ink-3">{UNMEASURED} no series</span>
            )}
          </div>
        )}
        {fired && (
          <div className="mt-1" data-testid={`pair-fired-${key}`}>
            <span className="text-ink-3">{fired.direction ?? UNMEASURED}</span>
            {fired.severity && (
              <span className="text-ink-3"> · {fired.severity}</span>
            )}
            {fired.day && <span className="text-ink-3"> · {fired.day}</span>}
            <div>
              <RecordLink
                kind="finding"
                id={fired.finding_id}
                label="open the finding"
                origin="layer-divergence"
                className="text-label"
              />
            </div>
          </div>
        )}
      </div>
    </div>
  )
}

function DeskCard({
  desk,
  res,
}: {
  desk: LayerDivergenceDesk
  res: LayerDivergenceResponse
}) {
  const [open, setOpen] = useState(true)
  const meanings = useMemo(() => {
    const m = new Map<string, string>()
    for (const p of res.pairs_declared) m.set(p.pair_id, p.meaning)
    return m
  }, [res.pairs_declared])

  const reasonFor = useMemo(() => {
    const m = new Map<string, string>()
    for (const bucket of ['absent', 'unmeasured', 'undeclared'] as const) {
      for (const row of desk.aperture[bucket] ?? []) m.set(row.layer, row.reason)
    }
    return m
  }, [desk.aperture])

  const suppressed = Object.entries(desk.counts_suppressed_by_aperture)

  return (
    <section
      className="rounded border border-line bg-surf-2"
      data-testid={`desk-card-${desk.target_id}`}
    >
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-start gap-1 px-density py-1.5 text-left"
      >
        {open ? (
          <ChevronDown className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        ) : (
          <ChevronRight className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2">
            <span className="text-body-lg text-ink-1">{desk.country || UNMEASURED}</span>
            <span className="font-mono text-label text-ink-3">{desk.target_id}</span>
            <span className="font-mono text-label text-ink-3">
              map {desk.map_version || UNMEASURED}
            </span>
          </div>
          <div className="text-label text-ink-3">
            {deskLine(desk, res.thin_min_per_day)}
            {desk.rows_truncated && (
              <span className="text-accent-warning">
                {' '}
                · scan truncated — counts are a floor, not a total
              </span>
            )}
          </div>
        </div>
      </button>

      {open && (
        <div className="px-density pb-2">
          <div className="flex flex-wrap gap-1">
            {res.layer_vocab.map((layer) => {
              const row = desk.layers[layer]
              const declared = row?.declared ?? 'undeclared'
              const reason = row?.reason || reasonFor.get(layer) || ''
              return (
                <span
                  key={layer}
                  className={cn(
                    'rounded border px-1 py-px text-label',
                    DECLARED_TONE[declared] ?? DECLARED_TONE.undeclared,
                  )}
                  data-testid={`layer-chip-${desk.target_id}-${layer}`}
                  title={
                    reason
                      ? `${layer} · ${declared} — ${reason}`
                      : `${layer} · ${declared}`
                  }
                >
                  {layer} · {declared}
                  {row ? ` · ${row.sources_mapped} src` : ''}
                </span>
              )
            })}
          </div>

          {suppressed.length > 0 && (
            <div
              className="mt-1 text-label text-accent-warning"
              data-testid={`desk-suppressed-${desk.target_id}`}
            >
              the map carries sources for a layer the aperture calls absent —
              {suppressed
                .map(([layer, n]) => ` ${layer}: ${n} items suppressed`)
                .join(' ·')}
            </div>
          )}

          <div className="mt-1">
            {desk.pairs.map((pair) => (
              <PairRow
                key={pair.pair_id}
                desk={desk}
                pair={pair}
                meaning={meanings.get(pair.pair_id)}
                zThreshold={res.z_threshold}
                thinMinPerDay={res.thin_min_per_day}
              />
            ))}
          </div>
        </div>
      )}
    </section>
  )
}

export default function LayerDivergencePanel({ registration }: PanelProps) {
  const [firedDays, setFiredDays] = useState<number>(30)

  const { data, isLoading, error, refetch } = useQuery<LayerDivergenceResponse>({
    queryKey: ['layer-divergence', firedDays],
    queryFn: () => fetchLayerDivergence({ firedDays }),
    refetchInterval: 300_000,
  })

  const desks = useMemo(
    () => (data ? orderDesks(data.desks, data.thin_min_per_day) : []),
    [data],
  )

  return (
    <PanelChrome
      registration={registration}
      subtitle={data ? summaryLine(data) : 'reading the divergence receipt…'}
      onRefresh={() => refetch()}
      actions={
        <div className="flex items-center gap-1" data-testid="layer-divergence-windows">
          {FIRED_WINDOWS.map((w) => (
            <button
              key={w}
              type="button"
              onClick={() => setFiredDays(w)}
              data-testid={`layer-divergence-fired-${w}`}
              className={cn(
                'rounded border px-2 py-0.5 text-label',
                firedDays === w
                  ? 'border-line-strong bg-surf-3 text-ink-1'
                  : 'border-line text-ink-3 hover:text-ink-1',
              )}
              title={`Look back ${w} days for each desk's most recent fired divergence`}
            >
              {w}d
            </button>
          ))}
        </div>
      }
    >
      {isLoading && (
        <div className="text-body text-ink-3" data-testid="layer-divergence-loading">
          reading the divergence receipt…
        </div>
      )}

      {error != null && (
        <div
          className="rounded border border-severity-critical/40 bg-severity-critical/10 p-2 text-body text-severity-critical"
          data-testid="layer-divergence-error"
        >
          Could not read the divergence map —{' '}
          {String((error as Error).message ?? error)}
        </div>
      )}

      {data && !data.measured && (
        <div
          className="mb-2 flex items-start gap-1.5 rounded border border-severity-critical/40 bg-severity-critical/10 p-2 text-body text-severity-critical"
          data-testid="layer-divergence-unmeasured"
        >
          <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
          <span>
            The receipt could not be read. This is a failed read, not a quiet
            instrument — nothing below should be taken as "no divergence".
          </span>
        </div>
      )}

      {data && (
        <div className="space-y-2">
          <header className="rounded border border-line bg-surf-1 p-2">
            <div className="flex items-start gap-1.5">
              <Layers className="mt-0.5 h-3.5 w-3.5 shrink-0 text-ink-3" aria-hidden />
              <p className="text-body leading-snug text-ink-2">{data.unit_sentence}</p>
            </div>
            <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-0.5">
              <Stamp label="as of" value={data.as_of ? `${data.as_of} (UTC day)` : UNMEASURED} />
              <Stamp
                label="run"
                value={data.run_started_at ? new Date(data.run_started_at).toISOString() : UNMEASURED}
              />
              <Stamp label="method" value={data.method_version ?? UNMEASURED} />
              <Stamp label="payload" value={data.payload_schema ?? UNMEASURED} />
              <Stamp
                label="window"
                value={data.window_days != null ? `${data.window_days} d` : UNMEASURED}
              />
              <Stamp
                label="baseline"
                value={data.baseline_days != null ? `${data.baseline_days} d median+MAD` : UNMEASURED}
              />
              <Stamp
                label="fires at"
                value={
                  data.z_threshold != null && data.consecutive_days != null
                    ? `|z| ≥ ${data.z_threshold}σ on ${data.consecutive_days} consecutive days`
                    : UNMEASURED
                }
              />
              <Stamp
                label="thin under"
                value={
                  data.thin_min_per_day != null
                    ? `${data.thin_min_per_day} items/day`
                    : UNMEASURED
                }
              />
            </div>
          </header>

          {data.classification_audit && (
            <div
              className="flex items-start gap-1.5 rounded border border-accent-warning/45 bg-accent-warning/10 p-2 text-label text-accent-warning"
              data-testid="layer-divergence-audit"
            >
              <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden />
              <span>{data.classification_audit}</span>
            </div>
          )}

          {data.warnings.length > 0 && (
            <ul
              className="rounded border border-accent-warning/40 bg-accent-warning/5 p-2 text-label text-accent-warning"
              data-testid="layer-divergence-warnings"
            >
              {data.warnings.map((w) => (
                <li key={w}>{w}</li>
              ))}
            </ul>
          )}

          {data.measured && data.receipt_run_id == null && (
            <div
              className="rounded border border-line bg-surf-1 p-2 text-body text-ink-2"
              data-testid="layer-divergence-no-run"
            >
              No run yet — the unit has never written a receipt. Nothing here is
              a measurement of zero divergence; there is simply nothing to read.
            </div>
          )}

          {desks.map((desk) => (
            <DeskCard key={desk.target_id} desk={desk} res={data} />
          ))}

          {data.desks_unresolved.length > 0 && (
            <div
              className="rounded border border-line bg-surf-1 p-2 text-label text-ink-3"
              data-testid="layer-divergence-unresolved"
            >
              desks the run could not resolve:{' '}
              <span className="font-mono">
                {data.desks_unresolved
                  .map((u) => JSON.stringify(u))
                  .join(' · ')}
              </span>
            </div>
          )}

          <footer className="text-label text-ink-3">
            legend — solid line: layer A, items/day after the wire fold; dashed:
            layer B; the track beneath is the z against that pair's own rolling
            baseline, with the firing threshold as two dotted rules. A break in
            the z track is a day with no usable baseline, not a z of zero.
          </footer>
        </div>
      )}
    </PanelChrome>
  )
}

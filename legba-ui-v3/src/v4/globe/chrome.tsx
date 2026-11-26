/**
 * The globe's floating chrome (U-4) — one quiet style for every control over
 * the map (layout design §3, study move 1): the layer card top-left, view
 * controls top-right, the legend bottom-left, Consult's chip top-centre, and
 * the hover card at the pointer. Shared by the WebGL globe and the drawn
 * fallback, so both say the same things in the same words.
 *
 * Text is 12 px or larger everywhere (the reader floor); figures are mono
 * with their unit; a layer that has not been read says "no read yet" and a
 * measured zero says 0.
 */
import { useState, type ReactNode } from 'react'

/**
 * Which of the map's cards open at rest: neither, at every width. The map is
 * the thing to look at; open, the layer card and the legend covered about
 * three fifths of it on a laptop and still a third on a wide screen. Each is
 * one click away as a small button in its corner. The arguments (the
 * region's size, the inset preset) are kept so the callers need not change
 * when a size rule comes back.
 */
export function chromeFolds(_width: number, _height: number, _inset: boolean): { layers: boolean; legend: boolean } {
  return { layers: true, legend: true }
}
import { Info, Layers, Minus, Plus, RotateCcw, X } from 'lucide-react'
import { cn } from '@/lib/cn'
import { useCasePlayer } from '@/state/casePlayer'
import { GLOBE_LAYERS, type GlobeLayerDef, type GlobeLayerId, type LegendEntry } from './catalog'
import { T } from './style'
import type { PrepOutput } from './prep'

export const CARD = 'pointer-events-auto rounded-control border border-line bg-surf-3 shadow-[0_2px_8px_rgba(0,0,0,0.35)]'

// ---------------------------------------------------------------------------
// The marks, drawn small for the legend and the catalogue.
// ---------------------------------------------------------------------------

export function LegendMark({ mark }: { mark: LegendEntry['mark'] }) {
  const box = (children: ReactNode) => (
    <svg width="18" height="14" viewBox="0 0 18 14" aria-hidden className="shrink-0">
      {children}
    </svg>
  )
  switch (mark) {
    case 'wash1':
    case 'wash2':
    case 'wash3': {
      const i = Number(mark.slice(-1)) - 1
      return box(<rect x="1" y="2" width="16" height="10" fill={T.wash[i]} stroke={i === 2 ? T.ink : 'none'} strokeWidth="0.75" />)
    }
    case 'hatch-dense':
    case 'hatch-sparse': {
      const pitch = mark === 'hatch-dense' ? 4 : 8
      const lines = []
      for (let x = -14; x < 18; x += pitch) lines.push(<line key={x} x1={x} y1="14" x2={x + 14} y2="0" stroke={T.muted} strokeWidth="0.8" />)
      return box(
        <>
          <clipPath id={`clip-${mark}`}>
            <rect x="1" y="2" width="16" height="10" />
          </clipPath>
          <rect x="1" y="2" width="16" height="10" fill={T.land} />
          <g clipPath={`url(#clip-${mark})`}>{lines}</g>
        </>,
      )
    }
    case 'dot-good':
    case 'dot-warn':
    case 'dot-critical':
    case 'dot-none': {
      const c = mark === 'dot-good' ? T.good : mark === 'dot-warn' ? T.warn : mark === 'dot-critical' ? T.critical : T.ink
      return box(<circle cx="9" cy="7" r="3.5" fill={c} />)
    }
    case 'ring':
    case 'ring-heavy':
      return box(
        <>
          <circle cx="9" cy="7" r="5" fill="none" stroke="rgba(230,226,216,0.35)" strokeWidth={mark === 'ring' ? 1 : 1.75} />
          <path d="M9 2 A5 5 0 0 1 14 7" fill="none" stroke={T.ink} strokeWidth={mark === 'ring' ? 1.75 : 2.5} />
        </>,
      )
    case 'cluster':
      return box(<circle cx="9" cy="7" r="5.5" fill="rgba(230,226,216,0.18)" stroke={T.ink} strokeWidth="0.75" />)
    case 'point':
      return box(<circle cx="9" cy="7" r="3" fill={T.ink} />)
    case 'incident':
      return box(<path d="M9 1.5 L14.5 7 L9 12.5 L3.5 7 Z" fill={T.ground} stroke={T.warn} strokeWidth="1.25" />)
  }
}

// ---------------------------------------------------------------------------
// The layer figures: what each row can say about its own read.
// ---------------------------------------------------------------------------

export type LayerFigure = { state: 'read'; text: string } | { state: 'unread' | 'unreadable' | 'unavailable' | 'off'; text: string }

export function layerFigures(
  out: PrepOutput | null,
  reads: { changes: unknown; reads: unknown; points: unknown; incidents: unknown; forecasts: unknown; series: unknown; subjects: unknown },
  on: Record<GlobeLayerId, boolean>,
  unreadable: Partial<Record<GlobeLayerId, true>>,
  seriesAsOf: string | null,
): Record<GlobeLayerId, LayerFigure> {
  const unread: LayerFigure = { state: 'unread', text: 'no read yet' }
  const fig = (id: GlobeLayerId, ready: boolean, text: () => string): LayerFigure => {
    const def = GLOBE_LAYERS.find((l) => l.id === id)!
    if (def.unavailable) return { state: 'unavailable', text: 'not available' }
    if (!on[id]) return { state: 'off', text: '' }
    if (unreadable[id]) return { state: 'unreadable', text: 'cannot be read' }
    if (!ready || !out) return unread
    return { state: 'read', text: text() }
  }
  const asOf = seriesAsOf ? ` · as of ${new Date(seriesAsOf).toISOString().slice(5, 10).replace('-', '/')}` : ''
  return {
    change: fig('change', reads.changes != null && reads.subjects != null, () => `${out!.counts.changed} subjects · ${out!.counts.changes ?? 0} changes`),
    blanks: fig('blanks', reads.reads != null && reads.subjects != null, () => `${out!.counts.blanksDense} looked · ${out!.counts.blanksSparse} uncovered`),
    series: fig('series', reads.series != null, () => `${out!.counts.seriesPlaced} places${asOf}`),
    forecasts: fig('forecasts', reads.forecasts != null && reads.subjects != null, () => `${out!.counts.forecastsDrawn} rings${out!.counts.forecastsUnplaced ? ` · ${out!.counts.forecastsUnplaced} unplaced` : ''}`),
    events: fig('events', reads.points != null, () =>
      out!.counts.pointsTotal != null && out!.counts.pointsTotal > out!.counts.pointsDrawn
        ? `${out!.counts.pointsDrawn} of ${out!.counts.pointsTotal}`
        : `${out!.counts.pointsDrawn} in window`,
    ),
    at_sea: fig('at_sea', reads.incidents != null, () => `${out!.counts.incidentsDrawn} in window`),
    drift: { state: 'unavailable', text: 'not available' },
  }
}

// ---------------------------------------------------------------------------
// The layer card (top-left).
// ---------------------------------------------------------------------------

function LayerRow({ def, on, figure, onToggle }: { def: GlobeLayerDef; on: boolean; figure: LayerFigure; onToggle: (v: boolean) => void }) {
  const [open, setOpen] = useState(false)
  const unavailable = !!def.unavailable
  return (
    <li className="border-b border-line-subtle py-1 last:border-b-0" data-testid={`globe-layer-${def.id}`} data-on={on ? '1' : '0'}>
      <div className="flex items-center gap-2">
        <label className={cn('flex min-w-0 flex-1 items-center gap-2', unavailable ? 'cursor-not-allowed text-ink-3' : 'cursor-pointer text-ink-1')}>
          <input
            type="checkbox"
            className="h-3.5 w-3.5 shrink-0 accent-[var(--ink-1)]"
            checked={on}
            disabled={unavailable}
            onChange={(e) => onToggle(e.target.checked)}
            data-testid={`globe-layer-toggle-${def.id}`}
          />
          <span className="truncate text-label" title={unavailable ? `Needs ${def.unavailable}` : `Source: ${def.source}`}>
            {def.label}
          </span>
        </label>
        {figure.text && (
          <span
            className={cn('shrink-0 text-label', figure.state === 'read' ? 'font-mono tabular-nums text-ink-2' : 'italic text-ink-3')}
            data-testid={`globe-layer-figure-${def.id}`}
            data-state={figure.state}
          >
            {figure.text}
          </span>
        )}
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          aria-label={`What ${def.label} can see`}
          className="shrink-0 rounded-control p-0.5 text-ink-3 hover:text-ink-1"
        >
          <Info size={12} aria-hidden />
        </button>
      </div>
      {open && (
        <dl className="mt-1 space-y-0.5 pl-[22px] text-label text-ink-2">
          <div><dt className="inline text-ink-3">{unavailable ? 'Needs · ' : 'Source · '}</dt><dd className="inline">{unavailable ? def.unavailable : def.source}</dd></div>
          <div><dt className="inline text-ink-3">Object · </dt><dd className="inline">{def.object}</dd></div>
          <div><dt className="inline text-ink-3">Sees · </dt><dd className="inline">{def.sees}</dd></div>
          <div><dt className="inline text-ink-3">Cannot see · </dt><dd className="inline">{def.cannotSee}</dd></div>
          <div><dt className="inline text-ink-3">Cap · </dt><dd className="inline">{def.cap}</dd></div>
        </dl>
      )}
    </li>
  )
}

export function LayerCard({
  on,
  figures,
  onToggle,
  collapsedByDefault,
  only,
  onOpenChange,
}: {
  on: Record<GlobeLayerId, boolean>
  figures: Record<GlobeLayerId, LayerFigure>
  onToggle: (id: GlobeLayerId, v: boolean) => void
  collapsedByDefault: boolean
  /** The rows this preset lists (the inset lists fewer). */
  only?: GlobeLayerId[]
  /** Told when the card opens or folds (the legend takes the room it leaves). */
  onOpenChange?: (open: boolean) => void
}) {
  const [open, setOpenState] = useState(!collapsedByDefault)
  const setOpen = (o: boolean) => {
    setOpenState(o)
    onOpenChange?.(o)
  }
  const rows = GLOBE_LAYERS.filter((l) => !only || only.includes(l.id))
  const nOn = rows.filter((l) => on[l.id]).length
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(CARD, 'absolute left-3 top-3 z-10 flex items-center gap-1.5 px-2.5 py-1.5 text-label text-ink-2 hover:text-ink-1')}
        data-testid="globe-layers-button"
        aria-label={`Layers, ${nOn} on`}
      >
        <Layers size={13} aria-hidden />
        <span className="uppercase tracking-[0.06em]">Layers</span>
        <span className="font-mono tabular-nums text-ink-3">{nOn}</span>
      </button>
    )
  }
  return (
    <div className={cn(CARD, 'absolute left-3 top-3 z-10 max-h-[55%] w-[min(24rem,calc(100%-24px))] overflow-y-auto px-3 py-2')} data-testid="globe-layer-card">
      <div className="mb-1 flex items-center justify-between border-b border-line pb-1">
        <span className="text-label uppercase tracking-[0.06em] text-ink-3">Layers</span>
        <span className="flex items-center gap-2">
          <span className="font-mono text-label tabular-nums text-ink-3">{nOn} on</span>
          <button type="button" onClick={() => setOpen(false)} aria-label="Hide the layer list" className="text-ink-3 hover:text-ink-1">
            <X size={13} aria-hidden />
          </button>
        </span>
      </div>
      <ul>
        {rows.map((def) => (
          <LayerRow key={def.id} def={def} on={on[def.id]} figure={figures[def.id]} onToggle={(v) => onToggle(def.id, v)} />
        ))}
      </ul>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The legend (bottom-left): only the layers that are on, one line each.
// ---------------------------------------------------------------------------

export function Legend({
  on,
  figures,
  out,
  seriesUnit,
  clusterNote,
  only,
  collapsedByDefault = false,
  note = null,
  room = 'shared',
}: {
  on: Record<GlobeLayerId, boolean>
  figures: Record<GlobeLayerId, LayerFigure>
  out: PrepOutput | null
  seriesUnit: string | null
  clusterNote: string | null
  only?: GlobeLayerId[]
  collapsedByDefault?: boolean
  /** A line about how the map is drawn (a browser with no GPU), at the legend's foot. */
  note?: string | null
  /**
   * How much of the map's height the open legend may take: `shared` while the
   * layer card is open above it (two fifths), `alone` while the card is folded
   * to its button (all but the top corner). Past that the legend scrolls.
   */
  room?: 'shared' | 'alone'
}) {
  const [open, setOpen] = useState(!collapsedByDefault)
  const rows = GLOBE_LAYERS.filter((l) => on[l.id] && !l.unavailable && (!only || only.includes(l.id)))
  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(CARD, 'absolute bottom-3 left-3 z-10 px-2.5 py-1 text-label uppercase tracking-[0.06em] text-ink-2 hover:text-ink-1')}
        data-testid="globe-legend-button"
      >
        Legend
      </button>
    )
  }
  return (
    <div
      className={cn(
        CARD,
        'absolute bottom-3 left-3 z-10 max-w-[min(22.5rem,calc(100%-24px))] overflow-y-auto overscroll-contain px-3 py-2',
        room === 'alone' ? 'max-h-[calc(100%-4.5rem)]' : 'max-h-[40%]',
      )}
      data-testid="globe-legend"
      data-room={room}
    >
      {collapsedByDefault && (
        <button type="button" onClick={() => setOpen(false)} aria-label="Hide the legend" className="float-right text-ink-3 hover:text-ink-1">
          <X size={13} aria-hidden />
        </button>
      )}
      {rows.length === 0 && <div className="text-label text-ink-3">No layer is on.</div>}
      {rows.map((l) => {
        const f = figures[l.id]
        return (
          <div key={l.id} className="py-0.5">
            <div className="flex items-baseline justify-between gap-3">
              <span className="text-label uppercase tracking-[0.06em] text-ink-3">{l.label}</span>
              {f.state !== 'read' && <span className="text-label italic text-ink-3">{f.text}</span>}
            </div>
            {f.state === 'read' && (
              <div className="flex flex-wrap gap-x-3">
                {l.legend.map((e) => (
                  <span key={e.text} className="flex items-center gap-1.5 text-label text-ink-2" title={e.text}>
                    <LegendMark mark={e.mark} />
                    <span>{e.short ?? e.text}</span>
                  </span>
                ))}
              </div>
            )}
            {f.state === 'read' && l.id === 'series' && (
              <div className="pl-[26px] text-label text-ink-3">
                % of 30-day baseline · {seriesUnit ?? 'per day'}
                {out && out.counts.seriesUnplaced.length > 0 ? ` · ${out.counts.seriesUnplaced.length} unplaced` : ''}
              </div>
            )}
            {f.state === 'read' && l.id === 'events' && clusterNote && <div className="pl-[26px] text-label text-ink-3">{clusterNote}</div>}
          </div>
        )
      })}
      {!on.blanks && (
        <div className="mt-0.5 border-t border-line-subtle pt-1 text-label text-ink-3">
          Blanks are off: a quiet area may be unread, not quiet.
        </div>
      )}
      {note && (
        <div className="mt-0.5 border-t border-line-subtle pt-1 text-label text-ink-3" data-testid="globe-software-note">
          {note}
        </div>
      )}
    </div>
  )
}

// ---------------------------------------------------------------------------
// View controls (top-right).
// ---------------------------------------------------------------------------

export function ViewControls({
  projection,
  onProjection,
  onZoom,
  onReset,
  showProjection,
}: {
  projection: 'globe' | 'flat'
  onProjection: (p: 'globe' | 'flat') => void
  onZoom: (d: 1 | -1) => void
  onReset: () => void
  showProjection: boolean
}) {
  const btn = 'flex h-7 min-w-7 items-center justify-center px-1.5 text-label text-ink-2 hover:bg-surf-4 hover:text-ink-1'
  return (
    <div className="absolute right-3 top-3 z-10 flex items-start gap-2">
      {showProjection && (
        <div className={cn(CARD, 'flex overflow-hidden')} role="group" aria-label="Projection" data-testid="globe-projection">
          {(['globe', 'flat'] as const).map((p) => (
            <button
              key={p}
              type="button"
              aria-pressed={projection === p}
              onClick={() => onProjection(p)}
              className={cn(btn, 'px-2.5 capitalize', projection === p && 'bg-surf-4 text-ink-1')}
              data-testid={`globe-projection-${p}`}
            >
              {p}
            </button>
          ))}
        </div>
      )}
      <div className={cn(CARD, 'flex flex-col overflow-hidden')}>
        <button type="button" className={btn} onClick={() => onZoom(1)} aria-label="Zoom in" data-testid="globe-zoom-in">
          <Plus size={14} aria-hidden />
        </button>
        <button type="button" className={cn(btn, 'border-t border-line-subtle')} onClick={() => onZoom(-1)} aria-label="Zoom out" data-testid="globe-zoom-out">
          <Minus size={14} aria-hidden />
        </button>
        <button type="button" className={cn(btn, 'border-t border-line-subtle')} onClick={onReset} aria-label="Back to the home view" data-testid="globe-reset">
          <RotateCcw size={13} aria-hidden />
        </button>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Consult's chip (top-centre): what a Consult turn pulled up, with Undo.
// ---------------------------------------------------------------------------

export function ConsultChip() {
  const player = useCasePlayer((s) => s.player)
  const restore = useCasePlayer((s) => s.restore)
  const stop = useCasePlayer((s) => s.stop)
  const dismiss = useCasePlayer((s) => s.dismiss)
  if (!player) return null
  const pulled = player.steps.filter(
    (s) => s.state === 'executed' && s.action && ['map', 'focus', 'timeline', 'set_scope'].includes(s.action.action),
  )
  if (pulled.length === 0) return null
  const live = player.status === 'live' || player.status === 'replay' || player.status === 'paused_gap'
  return (
    <div className="pointer-events-none absolute inset-x-0 top-3 z-20 flex justify-center px-24">
      <div
        className="pointer-events-auto flex max-w-full items-center gap-2 rounded-control border border-point/60 bg-surf-3 px-2.5 py-1 text-label text-ink-1 shadow-[0_2px_8px_rgba(0,0,0,0.35)]"
        data-testid="globe-consult-chip"
      >
        <span className="h-2 w-2 shrink-0 rounded-full bg-point" aria-hidden />
        <span className="truncate">
          Pulled up by Consult: {pulled.map((s) => s.label).join(' · ')}
        </span>
        {live && (
          <button type="button" onClick={() => void stop()} className="shrink-0 text-ink-2 underline decoration-dotted hover:text-ink-1">
            Stop
          </button>
        )}
        <button type="button" onClick={() => restore()} className="shrink-0 text-point underline decoration-dotted hover:text-ink-1" data-testid="globe-consult-undo">
          Undo
        </button>
        {!live && (
          <button type="button" onClick={() => dismiss()} aria-label="Keep this view" className="shrink-0 text-ink-3 hover:text-ink-1">
            <X size={12} aria-hidden />
          </button>
        )}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// The hover card.
// ---------------------------------------------------------------------------

export interface HoverInfo {
  x: number
  y: number
  title: string
  lines: string[]
}

export function HoverCard({ hover, width }: { hover: HoverInfo | null; width: number }) {
  if (!hover) return null
  const left = Math.min(hover.x + 14, Math.max(8, width - 300))
  return (
    <div
      className={cn(CARD, 'pointer-events-none absolute z-20 w-max max-w-[17.5rem] px-2.5 py-1.5')}
      style={{ left, top: Math.max(8, hover.y + 14) }}
      data-testid="globe-hover"
    >
      <div className="text-body text-ink-1">{hover.title}</div>
      {hover.lines.map((l, i) => (
        <div key={i} className="text-label text-ink-2">
          {l}
        </div>
      ))}
    </div>
  )
}

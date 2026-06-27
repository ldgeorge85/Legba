/**
 * GraphControls — the shared chrome that overlays a cytoscape canvas: the
 * entity-type FILTER CHIPS, the relationship-type COLOUR LEGEND, and the
 * zoom (+/−/fit) buttons. Restores the old "Knowledge Graph" controls
 * (screenshot 19) on top of the crash-safe cytoscape mount (#90).
 *
 * Reused by all three graph surfaces:
 *   - the full entity graph (`panels/system/EntityGraph`) — chips + legend + zoom,
 *   - the Why ego-graph (`v4/why/EntityGraph`)            — chips + legend + zoom,
 *   - the Why lineage DAG (`v4/why/LineageGraph`)         — a lighter variant:
 *     the "chips" are lineage row-kinds and the "relationship legend" keys to
 *     lineage edge kinds, so the same widget serves the DAG.
 *
 * Colours come from the shared palettes in `@/lib/graphModel`
 * (`entityClassColor` / `relationshipColor`) so the chips/legend stay in
 * lock-step with the cytoscape stylesheet. The component is purely presentational
 * + controlled — selection state lives in the panel.
 */
import { ZoomIn, ZoomOut, Maximize, Eye, EyeOff } from 'lucide-react'
import { cn } from '@/lib/cn'

/** One filterable category (a node class, or a lineage row-kind). */
export interface FilterChip {
  /** Stable key (the entity_class / row_kind string). */
  id: string
  /** Display label (defaults to a Title-Cased id). */
  label?: string
  /** Swatch colour (hex). */
  color: string
}

/** One legend entry (a relationship type, or a lineage edge kind). */
export interface LegendEntry {
  id: string
  label?: string
  color: string
}

export interface GraphControlsProps {
  /** Node-type filter chips. Empty ⇒ the chip row is omitted. */
  chips?: FilterChip[]
  /** Currently-active chip ids (selected = accent fill, unselected = muted). */
  activeChips?: ReadonlySet<string>
  /** Toggle a single chip. */
  onToggleChip?: (id: string) => void
  /** Select all / clear all chips (the "All" pseudo-chip). */
  onSelectAllChips?: () => void
  onClearChips?: () => void

  /** Relationship colour legend entries. Empty ⇒ the legend is omitted. */
  legend?: LegendEntry[]

  /** Show-orphans toggle. Omit the handler to hide the control entirely. */
  showOrphans?: boolean
  onToggleOrphans?: () => void

  /** Zoom handlers (omit any to hide that button). */
  onZoomIn?: () => void
  onZoomOut?: () => void
  onFit?: () => void

  /** Lighter chrome for the lineage DAG (smaller, less prominent labels). */
  variant?: 'full' | 'light'
  /** A short label for the chip-row heading (default "Show"). */
  chipsLabel?: string
  /** A short label for the legend-row heading (default "Edges"). */
  legendLabel?: string
}

/** Title-case a camelCase / snake_case id for a human-readable chip label. */
function humanize(id: string): string {
  const spaced = id
    .replace(/[_-]+/g, ' ')
    .replace(/([a-z\d])([A-Z])/g, '$1 $2')
    .trim()
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

/** A tiny round colour swatch. */
function Swatch({ color, className }: { color: string; className?: string }) {
  return (
    <span
      className={cn('inline-block rounded-full shrink-0', className)}
      style={{ backgroundColor: color }}
      aria-hidden
    />
  )
}

export function GraphControls({
  chips = [],
  activeChips,
  onToggleChip,
  onSelectAllChips,
  onClearChips,
  legend = [],
  showOrphans,
  onToggleOrphans,
  onZoomIn,
  onZoomOut,
  onFit,
  variant = 'full',
  chipsLabel = 'Show',
  legendLabel = 'Edges',
}: GraphControlsProps) {
  const light = variant === 'light'
  const allActive = chips.length > 0 && chips.every((c) => activeChips?.has(c.id) ?? true)
  const hasZoom = !!(onZoomIn || onZoomOut || onFit)

  return (
    <div className="pointer-events-none absolute inset-0 z-10 flex flex-col">
      {/* Top stack — chip row + legend, floated over the canvas top-left. */}
      <div className="pointer-events-none flex flex-col gap-1.5 p-2">
        {chips.length > 0 && (
          <div
            className={cn(
              'pointer-events-auto flex flex-wrap items-center gap-1',
              'rounded-md border border-line bg-surf-2/90 px-2 py-1.5 backdrop-blur',
            )}
            data-testid="graph-controls-chips"
          >
            <span className="mr-0.5 text-label font-medium uppercase tracking-wide text-ink-3">
              {chipsLabel}
            </span>
            {/* "All" pseudo-chip — select every type at once. */}
            {(onSelectAllChips || onClearChips) && (
              <button
                type="button"
                onClick={() => (allActive ? onClearChips?.() : onSelectAllChips?.())}
                className={cn(
                  'rounded px-1.5 py-0.5 text-label font-medium transition-colors',
                  allActive
                    ? 'bg-accent-info/20 text-ink-1 ring-1 ring-accent-info/50'
                    : 'bg-surf-1 text-ink-3 hover:text-ink-2',
                )}
                data-testid="graph-chip-all"
              >
                All
              </button>
            )}
            {chips.map((c) => {
              const active = activeChips?.has(c.id) ?? true
              return (
                <button
                  key={c.id}
                  type="button"
                  onClick={() => onToggleChip?.(c.id)}
                  aria-pressed={active}
                  title={c.label ?? humanize(c.id)}
                  className={cn(
                    'inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-label transition-colors',
                    active
                      ? 'bg-surf-1 text-ink-1 ring-1 ring-line-strong'
                      : 'text-ink-3 opacity-60 hover:opacity-90',
                  )}
                  data-testid={`graph-chip-${c.id}`}
                >
                  <Swatch
                    color={c.color}
                    className={cn(light ? 'h-2 w-2' : 'h-2.5 w-2.5', !active && 'opacity-40')}
                  />
                  {c.label ?? humanize(c.id)}
                </button>
              )
            })}
            {onToggleOrphans && (
              <button
                type="button"
                onClick={onToggleOrphans}
                title={showOrphans ? 'Hide disconnected nodes' : 'Show disconnected nodes'}
                className={cn(
                  'ml-1 inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-label transition-colors',
                  showOrphans
                    ? 'bg-surf-1 text-ink-2 ring-1 ring-line'
                    : 'text-ink-3 hover:text-ink-2',
                )}
                data-testid="graph-toggle-orphans"
              >
                {showOrphans ? <Eye size={12} /> : <EyeOff size={12} />}
                singletons
              </button>
            )}
          </div>
        )}

        {legend.length > 0 && (
          <div
            className={cn(
              'pointer-events-auto flex flex-wrap items-center gap-x-2.5 gap-y-1',
              'rounded-md border border-line bg-surf-2/90 px-2 py-1.5 backdrop-blur',
              light && 'gap-x-2',
            )}
            data-testid="graph-controls-legend"
          >
            <span className="mr-0.5 text-label font-medium uppercase tracking-wide text-ink-3">
              {legendLabel}
            </span>
            {legend.map((e) => (
              <span
                key={e.id}
                className="inline-flex items-center gap-1 text-label text-ink-2"
                title={e.label ?? humanize(e.id)}
              >
                <Swatch color={e.color} className={light ? 'h-1.5 w-1.5' : 'h-2 w-2'} />
                {e.label ?? humanize(e.id)}
              </span>
            ))}
          </div>
        )}
      </div>

      {/* Zoom cluster — bottom-right, over the canvas. */}
      {hasZoom && (
        <div
          className="pointer-events-auto absolute bottom-2 right-2 flex flex-col overflow-hidden rounded-md border border-line bg-surf-2/90 backdrop-blur"
          data-testid="graph-controls-zoom"
        >
          {onZoomIn && (
            <ZoomButton onClick={onZoomIn} title="Zoom in" data-testid="graph-zoom-in">
              <ZoomIn size={14} />
            </ZoomButton>
          )}
          {onZoomOut && (
            <ZoomButton onClick={onZoomOut} title="Zoom out" data-testid="graph-zoom-out">
              <ZoomOut size={14} />
            </ZoomButton>
          )}
          {onFit && (
            <ZoomButton onClick={onFit} title="Fit to view" data-testid="graph-zoom-fit">
              <Maximize size={14} />
            </ZoomButton>
          )}
        </div>
      )}
    </div>
  )
}

function ZoomButton({
  onClick,
  title,
  children,
  ...rest
}: {
  onClick: () => void
  title: string
  children: React.ReactNode
} & React.HTMLAttributes<HTMLButtonElement>) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={title}
      aria-label={title}
      className="flex h-7 w-7 items-center justify-center border-line text-ink-2 transition-colors hover:bg-surf-1 hover:text-ink-1 [&:not(:last-child)]:border-b"
      {...rest}
    >
      {children}
    </button>
  )
}

/**
 * ReadGeoLens — the read's evidence on a small drawn map (U-4 replaced the
 * Leaflet mini-map with this SVG one when Leaflet was retired).
 *
 * Self-contained: it does not read the shared window or the map's layers —
 * the read defines its own scope. The view is fitted to the evidence points;
 * the directly cited ones are emphasised and the rest are faded context.
 * Clicking a point selects its row (every region follows, the Record sheet
 * opens). The country outlines are projected in the globe's prep worker.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { selectRow } from '@/state/selection'
import type { GeoPoint } from '@/lib/geoPoints'
import { svgProject } from './prep'
import { T } from './style'
import { usePrep } from './usePrep'

function pointColor(p: GeoPoint): string {
  if (p.kind === 'finding') return T.warn
  if (p.kind === 'entity') return T.good
  return T.ink
}

export function ReadGeoLens({ points, evidenceIds, selectedId }: { points: GeoPoint[]; evidenceIds: Set<string>; selectedId?: string | null }) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [w, setW] = useState(0)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => setW(Math.round(el.clientWidth))
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  // a fixed 2:1 world canvas; the viewBox crops it to the evidence
  const W = 1440
  const H = 655
  const { paths } = usePrep(null, w > 0 ? { width: W, height: H } : null)

  const view = useMemo(() => {
    if (points.length === 0) return `0 0 ${W} ${H}`
    let x0 = Infinity
    let y0 = Infinity
    let x1 = -Infinity
    let y1 = -Infinity
    for (const p of points) {
      const [x, y] = svgProject(p.lon, p.lat, W, H)
      x0 = Math.min(x0, x)
      y0 = Math.min(y0, y)
      x1 = Math.max(x1, x)
      y1 = Math.max(y1, y)
    }
    const pad = 60
    const bw = Math.max(240, x1 - x0 + pad * 2)
    const bh = Math.max(120, y1 - y0 + pad * 2)
    return `${(x0 + x1) / 2 - bw / 2} ${(y0 + y1) / 2 - bh / 2} ${bw} ${bh}`
  }, [points])

  if (points.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center bg-surf-1 px-4 text-center text-body text-ink-3" data-testid="read-geo-lens-empty">
        No geo-placeable evidence for this read.
      </div>
    )
  }

  return (
    <div ref={wrapRef} className="h-full w-full" style={{ background: T.ground }} data-testid="read-geo-lens">
      <svg width="100%" height="100%" viewBox={view} preserveAspectRatio="xMidYMid meet" role="img" aria-label="Where the read's evidence is">
        <rect x={-W} y={-H} width={W * 3} height={H * 3} fill={T.sea} />
        {(paths ?? []).map((c) => (
          <path key={c.iso || c.name} d={c.d} fill={T.land} stroke="rgba(143,154,162,0.4)" strokeWidth="0.6" vectorEffect="non-scaling-stroke" />
        ))}
        {points.map((p) => {
          const [x, y] = svgProject(p.lon, p.lat, W, H)
          const ev = evidenceIds.has(p.id)
          const sel = selectedId != null && p.id === selectedId
          return (
            <g key={`${p.kind}-${p.id}`} style={{ cursor: 'pointer' }} onClick={() => selectRow(p.kind, p.id, p.title, { origin: 'read-geo-lens' })}>
              <title>{p.title}</title>
              <circle
                cx={x}
                cy={y}
                r={ev ? 5 : 3.5}
                fill={pointColor(p)}
                fillOpacity={ev ? 0.9 : 0.35}
                stroke={sel ? T.accent : T.ground}
                strokeWidth={sel ? 2 : 1}
                vectorEffect="non-scaling-stroke"
              />
            </g>
          )
        })}
      </svg>
    </div>
  )
}

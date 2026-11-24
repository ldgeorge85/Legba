/**
 * The drawn fallback (U-4 degradation ladder, step 3) — the same map without
 * WebGL: a static SVG world in plate carrée with the change wash, the blanks
 * hatched, series glyphs, forecast rings, events and situations gridded into
 * clusters, incidents at sea, and the one magenta point. Clicks set the same
 * shared selection the globe sets; the chrome is the globe's own.
 *
 * It is what a browser without WebGL2 gets (maplibre-gl 5 needs WebGL2), what
 * a lost context degrades to, and what replaced Leaflet as the safe renderer.
 * The country outlines are projected to path strings in the prep worker;
 * nothing heavier than a few hundred elements is drawn here.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import { useSelection } from '@/state/selection'
import { chromeFolds, HoverCard, LayerCard, Legend, type HoverInfo } from './chrome'
import { GLOBE_CAPS } from './catalog'
import { RING_STEPS, svgProject } from './prep'
import { T } from './style'
import {
  INSET_LAYERS,
  countryHover,
  forecastHover,
  incidentHover,
  pointHover,
  selectRecord,
  selectSubject,
  subjectForCountry,
  useGlobeModel,
  type GlobePreset,
} from './useGlobeModel'

const STATUS: Record<string, string> = { good: T.good, warn: T.warn, critical: T.critical, none: T.ink }
/** Grid cell for clustering events in the drawn map (px). */
const CELL = 40

function arcPath(cx: number, cy: number, r: number, share: number): string {
  if (share <= 0) return ''
  if (share >= 1) return `M${cx} ${cy - r}A${r} ${r} 0 1 1 ${cx - 0.01} ${cy - r}`
  const a = -Math.PI / 2 + share * Math.PI * 2
  const x = cx + r * Math.cos(a)
  const y = cy + r * Math.sin(a)
  return `M${cx} ${cy - r}A${r} ${r} 0 ${share > 0.5 ? 1 : 0} 1 ${x.toFixed(1)} ${y.toFixed(1)}`
}

export default function SvgWorld({ preset, active, reason }: { preset: GlobePreset; active: boolean; reason: string }) {
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const [size, setSize] = useState<{ width: number; height: number } | null>(null)
  const [wrapH, setWrapH] = useState(0)
  useEffect(() => {
    const el = wrapRef.current
    if (!el) return
    const measure = () => {
      const w = Math.round(el.clientWidth)
      if (w > 0) setSize({ width: w, height: Math.round(w / 2.2) })
      setWrapH(Math.round(el.clientHeight) || Math.round(w / 2.2))
    }
    measure()
    const ro = new ResizeObserver(measure)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])
  const model = useGlobeModel(preset, active, size)
  const { on, setOn, out, figures, paths, selection, selectedAt, reads } = model
  const [hover, setHover] = useState<HoverInfo | null>(null)
  const W = size?.width ?? 0
  const H = size?.height ?? 0
  const P = (lon: number, lat: number) => svgProject(lon, lat, W, H)

  const washBy = useMemo(() => new Map((out?.wash.features ?? []).map((f) => [String(f.properties.iso), Number(f.properties.step)])), [out])
  const hatchBy = useMemo(() => new Map((out?.hatch.features ?? []).map((f) => [String(f.properties.iso), String(f.properties.kind)])), [out])

  // events: gridded into CELL-px clusters (the drawn equivalent of the globe's clustering)
  const clusters = useMemo(() => {
    if (!out || !on.events || !W) return []
    const cells = new Map<string, { x: number; y: number; n: number; first: Record<string, unknown> }>()
    for (const f of out.points.features.slice(0, GLOBE_CAPS.events)) {
      const [lon, lat] = (f.geometry as { coordinates: [number, number] }).coordinates
      const [x, y] = P(lon, lat)
      const k = `${Math.floor(x / CELL)}:${Math.floor(y / CELL)}`
      const c = cells.get(k)
      if (c) c.n++
      else cells.set(k, { x, y, n: 1, first: f.properties })
    }
    return [...cells.values()]
  }, [out, on.events, W, H]) // eslint-disable-line react-hooks/exhaustive-deps

  const selIsos = new Set(selectedAt?.isos ?? [])
  const dim = !!selectedAt
  const ready = !!out && !!paths
  const pt = (e: React.MouseEvent) => {
    const r = wrapRef.current!.getBoundingClientRect()
    return { x: e.clientX - r.left, y: e.clientY - r.top }
  }

  return (
    <div
      ref={wrapRef}
      className="relative h-full w-full overflow-hidden"
      style={{ background: T.ground }}
      data-testid="globe-map"
      data-renderer="svg"
      data-preset={preset}
      data-ready={ready ? '1' : '0'}
      data-fallback-reason={reason}
    >
      {size && (
        <svg
          width="100%"
          height="100%"
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="xMidYMid meet"
          role="img"
          aria-label="World map (drawn)"
          onClick={() => useSelection.getState().clear()}
        >
          <defs>
            <pattern id="hatch-dense" width="6" height="6" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="6" stroke={T.muted} strokeOpacity="0.55" strokeWidth="1" />
            </pattern>
            <pattern id="hatch-sparse" width="12" height="12" patternUnits="userSpaceOnUse" patternTransform="rotate(45)">
              <line x1="0" y1="0" x2="0" y2="12" stroke={T.muted} strokeOpacity="0.35" strokeWidth="1" />
            </pattern>
          </defs>
          <rect x="0" y="0" width={W} height={H} fill={T.sea} />
          {(paths ?? []).map((c) => {
            const step = on.change ? washBy.get(c.iso) ?? 0 : 0
            const hatch = on.blanks ? hatchBy.get(c.iso) : undefined
            const facts = out?.countries[c.iso]
            const op = dim && selIsos.size > 0 && !selIsos.has(c.iso) ? 0.45 : 1
            return (
              <g
                key={c.iso || c.name}
                opacity={op}
                onClick={(e) => {
                  e.stopPropagation()
                  const s = subjectForCountry(facts)
                  if (s) selectSubject(s.id, s.name)
                }}
                onMouseMove={(e) => {
                  const p = pt(e)
                  setHover(facts ? countryHover(facts, p.x, p.y, null) : null)
                }}
                onMouseLeave={() => setHover(null)}
                style={{ cursor: facts ? 'pointer' : 'default' }}
              >
                <path d={c.d} fill={step ? T.wash[step - 1] : T.land} stroke="rgba(143,154,162,0.4)" strokeWidth="0.5" />
                {hatch && <path d={c.d} fill={`url(#hatch-${hatch})`} />}
                {selIsos.has(c.iso) && <path d={c.d} fill="none" stroke={T.accent} strokeWidth="1.5" />}
              </g>
            )
          })}
          {on.at_sea &&
            out?.incidents.features.map((f) => {
              const [lon, lat] = (f.geometry as { coordinates: [number, number] }).coordinates
              const [x, y] = P(lon, lat)
              return (
                <path
                  key={String(f.properties.id)}
                  d={`M${x} ${y - 5}L${x + 5} ${y}L${x} ${y + 5}L${x - 5} ${y}Z`}
                  fill={T.ground}
                  stroke={T.warn}
                  strokeWidth="1.25"
                  opacity={dim && selection?.id !== f.properties.id ? 0.45 : 1}
                  style={{ cursor: 'pointer' }}
                  onClick={(e) => {
                    e.stopPropagation()
                    selectRecord('signal', String(f.properties.id), String(f.properties.title ?? ''))
                  }}
                  onMouseMove={(e) => {
                    const p = pt(e)
                    setHover(incidentHover(f.properties, p.x, p.y))
                  }}
                  onMouseLeave={() => setHover(null)}
                />
              )
            })}
          {clusters.map((c, i) => (
            <g
              key={i}
              style={{ cursor: 'pointer' }}
              onClick={(e) => {
                e.stopPropagation()
                if (c.n === 1) selectRecord(c.first.kind === 'situation' ? 'situation' : 'event', String(c.first.id), String(c.first.title ?? ''))
              }}
              onMouseMove={(e) => {
                const p = pt(e)
                setHover(c.n === 1 ? pointHover(c.first, p.x, p.y) : { x: p.x, y: p.y, title: `${c.n} events and situations here`, lines: ['Zoom is not available in the drawn map'] })
              }}
              onMouseLeave={() => setHover(null)}
            >
              <circle cx={c.x} cy={c.y} r={c.n === 1 ? 3.5 : Math.min(18, 8 + Math.sqrt(c.n) * 2)} fill={c.n === 1 ? T.ink : 'rgba(230,226,216,0.16)'} stroke={c.n === 1 ? T.ground : T.ink} strokeWidth="0.75" />
              {c.n > 1 && (
                <text x={c.x} y={c.y + 4} textAnchor="middle" fill={T.ink} style={{ font: '500 12px var(--font-mono)' }}>
                  {c.n}
                </text>
              )}
            </g>
          ))}
          {on.forecasts &&
            out?.forecasts.features.map((f) => {
              const [lon, lat] = (f.geometry as { coordinates: [number, number] }).coordinates
              const [x, y] = P(lon, lat)
              const heavy = f.properties.heavy === 1
              const share = Number(f.properties.step) / RING_STEPS
              return (
                <g
                  key={String(f.properties.id)}
                  opacity={dim && !selIsos.size ? 0.45 : 1}
                  style={{ cursor: 'pointer' }}
                  onClick={(e) => {
                    e.stopPropagation()
                    selectSubject(String(f.properties.subject), String(f.properties.subject))
                  }}
                  onMouseMove={(e) => {
                    const p = pt(e)
                    setHover(forecastHover(f.properties, p.x, p.y, null))
                  }}
                  onMouseLeave={() => setHover(null)}
                >
                  <circle cx={x} cy={y} r="9" fill="none" stroke="rgba(230,226,216,0.35)" strokeWidth={heavy ? 2 : 1.25} />
                  <path d={arcPath(x, y, 9, share)} fill="none" stroke={T.ink} strokeWidth={heavy ? 3 : 2} />
                  <circle cx={x} cy={y} r="1.5" fill={T.ink} />
                </g>
              )
            })}
          {on.series &&
            (() => {
              // a label that would overlap a larger move's label folds to its dot
              const taken: number[][] = []
              return out?.seriesGlyphs.map((g) => {
              const [x, y] = P(g.lon, g.lat)
              const box = [x - 4, y - 8, x + 62, y + 8]
              const folded = taken.some((b) => box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1])
              if (!folded) taken.push(box)
              return (
                <g key={g.key} opacity={dim ? 0.45 : 1}>
                  <circle cx={x} cy={y} r="4" fill={STATUS[g.status]} stroke={T.ground} strokeWidth="1" />
                  <title>{`${g.name}: ${g.latest} on ${g.day}, ${g.pct == null ? 'no baseline yet' : `${Math.round(g.pct)} % of the 30-day baseline`}`}</title>
                  {!folded && (
                    <text x={x + 7} y={y + 4} fill={T.ink} style={{ font: '500 12px var(--font-mono)' }}>
                      {g.pct == null ? '–' : `${Math.round(g.pct)} %`}
                      {g.revised ? ' r' : ''}
                    </text>
                  )}
                </g>
              )
              })
            })()}
          {selectedAt && !selectedAt.bounds && (() => {
            const [x, y] = P(selectedAt.lon, selectedAt.lat)
            return <circle cx={x} cy={y} r="14" fill="none" stroke={T.accent} strokeWidth="2" data-testid="globe-point-ring" />
          })()}
        </svg>
      )}
      {size && <LayerCard on={on} figures={figures} onToggle={setOn} collapsedByDefault={chromeFolds(size.width, wrapH, preset === 'inset').layers} only={preset === 'inset' ? INSET_LAYERS : undefined} />}
      <Legend on={on} figures={figures} out={out} seriesUnit={reads.seriesUnit} clusterNote={null} only={preset === 'inset' ? INSET_LAYERS : undefined} collapsedByDefault={chromeFolds(size?.width ?? 0, wrapH, preset === 'inset').legend} />
      <div className="pointer-events-none absolute bottom-3 right-3 z-10 max-w-[16rem] text-right text-label text-ink-3" data-testid="globe-fallback-note">
        Drawn map: {reason}
      </div>
      <HoverCard hover={hover} width={size?.width ?? 600} />
    </div>
  )
}

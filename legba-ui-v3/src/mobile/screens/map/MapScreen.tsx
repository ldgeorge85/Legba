/**
 * The Map on the phone (M-3f; plan §2.3 row "Map").
 *
 *   AT REST   the drawn map (`v4/globe/SvgWorld`, as-is): the same layers,
 *             the change wash, the blanks and the one magenta point, in SVG —
 *             no WebGL and no map library. Its own chrome carries the layer
 *             card and the legend.
 *   ON DEMAND "Open the globe" loads the map library (a dynamic import, the
 *             only one the phone's import lint allows) and shows U-4's
 *             `GlobeMap` in the same box. A lost WebGL context, or a globe
 *             that cannot start, says "Map paused. Tap to reload." and falls
 *             back to the drawn map until tapped.
 *   A PLACE   tapping a country makes its subject the point; a bar under the
 *             map then opens that subject's page. A record tapped (an event,
 *             a situation) opens the Record's peek, like any statement.
 */
import { lazy, Suspense, useState } from 'react'
import SvgWorld from '@/v4/globe/SvgWorld'
import { useSelection } from '@/state/selection'
import { navigate } from '@/mobile/shell/phoneAddress'
import { Quiet, WindowChips } from '@/mobile/shell/ui'

const GlobeView = lazy(() => import('./GlobeView'))

export default function MapScreen() {
  const [globe, setGlobe] = useState<'off' | 'on' | 'paused'>('off')
  const [why, setWhy] = useState<string | null>(null)
  const point = useSelection((s) => s.selection)
  const subject = point && point.kind === 'target' ? point : null
  return (
    <div className="ph-map" data-testid="phone-map" data-globe={globe}>
      <WindowChips />
      <div className="ph-map-box" data-testid="phone-map-box">
        {globe === 'on' ? (
          <Suspense fallback={<Quiet testId="phone-globe-loading">Loading the globe…</Quiet>}>
            <GlobeView
              onFallback={(reason) => {
                setWhy(reason)
                setGlobe('paused')
              }}
            />
          </Suspense>
        ) : (
          <SvgWorld preset="board" active reason={globe === 'paused' ? (why ?? 'the globe paused') : 'the globe opens on request'} />
        )}
      </div>
      <div className="ph-map-actions">
        {globe === 'on' ? (
          <button type="button" className="ph-btn" data-testid="phone-globe-close" onClick={() => setGlobe('off')}>
            Back to the drawn map
          </button>
        ) : globe === 'paused' ? (
          <button type="button" className="ph-btn" data-testid="phone-globe-reload" onClick={() => setGlobe('on')}>
            Map paused. Tap to reload.
          </button>
        ) : (
          <button type="button" className="ph-btn" data-testid="phone-globe-open" onClick={() => setGlobe('on')}>
            Open the globe
          </button>
        )}
      </div>
      {globe === 'off' ? <Quiet>The drawn map. The globe loads only when you ask for it.</Quiet> : globe === 'paused' ? <Quiet>The globe paused: {why}.</Quiet> : null}
      {subject ? (
        <button
          type="button"
          className="ph-row ph-row-nav ph-map-point"
          data-testid="phone-map-point"
          onClick={() => navigate({ tab: 'subjects', page: { kind: 'subject', id: subject.id }, record: null, peek: false }, 'push')}
        >
          <span className="ph-row-head ph-point">{subject.label ?? subject.id}</span>
          <span className="ph-row-meta">Open this subject's page</span>
        </button>
      ) : (
        <Quiet>Tap a place to make it the point.</Quiet>
      )}
    </div>
  )
}

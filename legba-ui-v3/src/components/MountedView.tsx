/**
 * MountedView — a built component a surface region hosts that is not a panel
 * kind (`lib/surfaces.ts::MOUNTED_VIEWS`; the registry's kind ceiling holds).
 *
 * The Ledger page's views (U-11): `view:forecasts` (the forecasts, the
 * page's stage), `view:track_record` (the scored record behind its
 * five-resolution gate) and `view:proof` (the evaluation record); and the
 * Gate's `view:resolutions` (proposed resolutions awaiting a person, operator
 * side). Each reads its own routes (`panels/ledger/ledgerQueries.ts`).
 */
import { lazy, Suspense, useEffect, type ComponentType, type LazyExoticComponent } from 'react'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { emitRead } from '@/lib/readTelemetry'
import type { MountedViewId } from '@/lib/surfaces'

const VIEWS: Record<MountedViewId, LazyExoticComponent<ComponentType>> = {
  'view:forecasts': lazy(() => import('@/panels/ledger/LedgerForecasts')),
  'view:track_record': lazy(() => import('@/panels/ledger/TrackRecord')),
  'view:proof': lazy(() => import('@/panels/ledger/ProofRecord')),
  'view:resolutions': lazy(() => import('@/panels/ledger/ResolutionQueue')),
}

export function MountedView({ view }: { view: MountedViewId }) {
  useEffect(() => {
    emitRead('panel_open', { subjectKind: 'panel', subjectId: view })
  }, [view])
  const View = VIEWS[view]
  return (
    <div className="h-full w-full overflow-auto bg-surf-1 px-5 py-4" data-testid={`mounted-view-${view}`}>
      <PanelErrorBoundary label={view}>
        <Suspense fallback={null}>
          <View />
        </Suspense>
      </PanelErrorBoundary>
    </div>
  )
}

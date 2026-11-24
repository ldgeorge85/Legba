/**
 * PhoneViewHost — a view the frame can show, rendered on the phone by its id
 * (M-2), through the SAME registry mechanisms the desktop's `ViewHost` uses:
 *
 *   a view registered with its component (`panel-registry/contracts.ts::
 *   registerPanel`, e.g. the Watch views)  → `frameViewComponent(id)`
 *   a mounted view (`lib/surfaces.ts::MOUNTED_VIEWS`, the Ledger's)
 *                                          → `components/MountedView`
 *
 * Why not `ViewHost` itself: it also renders dock singletons through
 * `SingletonView`, which imports the Dockview panel-api context — a module
 * the phone's import lint forbids (`tools/lint-phone-imports.mjs`). The
 * phone shows no dock singleton, so it needs only these two branches; an id
 * that is neither says so instead of rendering nothing.
 */
import { frameViewComponent } from '@/panel-registry/contracts'
import { MountedView } from '@/components/MountedView'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { isMountedViewId } from '@/lib/surfaces'
import { Quiet } from './ui'

export function PhoneViewHost({ view }: { view: string }) {
  if (isMountedViewId(view)) return <MountedView view={view} />
  const Own = frameViewComponent(view)
  if (Own) {
    return (
      <PanelErrorBoundary label={view}>
        <Own binding={null} instanceId={view} />
      </PanelErrorBoundary>
    )
  }
  return <Quiet testId="phone-view-missing">This view is shown on the desktop only.</Quiet>
}

/**
 * ViewHost — renders ANY view the frame can show, by id (U-6): a registry
 * kind (a singleton, or a bound read `<kind>:<record>`), a mounted view (the
 * forecast ledger), a slot (an honest empty region), or a view registered at
 * runtime with its own component (`panel-registry/contracts.ts::
 * registerPanel`). The stage renders through it; the dock's `slot` component
 * renders the non-kind views through it too. The frame never names a kind.
 */
import { Suspense, useEffect } from 'react'
import type { currentMode } from '@/auth/jwt'
import type { PanelKind } from '@/types'
import { SingletonView, PanelLoading } from '@/components/SingletonView'
import { MountedView } from '@/components/MountedView'
import { RegionSlot } from '@/components/RegionSlot'
import { UnboundPanelPlaceholder } from '@/components/PanelChrome'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { resolveKind } from '@/panel-registry/aliases'
import { extractScope, resolvePanel } from '@/panel-registry/loader'
import { frameViewComponent } from '@/panel-registry/contracts'
import { boundRegistration } from '@/lib/boundRegistration'
import { boundPinFor, usePinScope } from '@/lib/consultContext'
import { emitRead } from '@/lib/readTelemetry'
import { isMountedViewId, isSlotId } from '@/lib/surfaces'

type Mode = ReturnType<typeof currentMode>

function BoundView({
  kind,
  recordId,
  axis,
  tab,
  mode,
  pinSurface,
}: {
  kind: PanelKind
  recordId: string
  axis: 'target' | 'analyst'
  tab?: string
  mode: Mode
  pinSurface: string
}) {
  const alias = resolveKind(kind)
  const k = (alias?.kind ?? kind) as PanelKind
  const registration = boundRegistration(k, recordId, mode, axis)
  // THE PIN HOOK: a bound read on screen pins Consult to its record.
  usePinScope(registration ? boundPinFor(registration.binding, null) : null, pinSurface)
  if (!registration) return <UnboundPanelPlaceholder panelId={kind} descriptorId={recordId} />
  const resolved = resolvePanel(registration)
  if (!resolved.ok) return <UnboundPanelPlaceholder panelId={resolved.panel_id} descriptorId={recordId} />
  const Component = resolved.Component
  return (
    <PanelErrorBoundary label={registration.id}>
      <PanelTierProvider tier={resolved.definition.tier ?? 'live'}>
        <Suspense fallback={<PanelLoading />}>
          <Component
            registration={registration}
            scope={extractScope(registration)}
            mode={mode}
            initialTab={tab ?? alias?.tab ?? resolved.tab}
          />
        </Suspense>
      </PanelTierProvider>
    </PanelErrorBoundary>
  )
}

export function ViewHost({
  view,
  recordId,
  axis = 'target',
  tab,
  mode,
  instanceId,
  pinSurface = 'stage',
}: {
  view: string
  recordId?: string
  axis?: 'target' | 'analyst'
  tab?: string
  mode: Mode
  instanceId?: string
  /** The name the pin census prints for a bound read here. */
  pinSurface?: string
}) {
  const id = instanceId ?? (recordId ? `${view}:${recordId}` : view)
  const Own = frameViewComponent(view)
  const builtIn = isMountedViewId(view) || isSlotId(view)
  // READ TELEMETRY: a view opened outside the dock's `default` component (the
  // stage, a registered view) counts as an open; a mounted view counts itself
  // and a slot is not a read.
  const counted = !builtIn
  useEffect(() => {
    if (counted) emitRead('panel_open', { subjectKind: 'panel', subjectId: view })
  }, [counted, view, recordId])
  if (Own && !builtIn) {
    return (
      <PanelErrorBoundary label={id}>
        <Own binding={recordId ? { [axis === 'analyst' ? 'analyst_id' : 'target_id']: recordId } : null} instanceId={id} />
      </PanelErrorBoundary>
    )
  }
  if (isMountedViewId(view)) return <MountedView view={view} />
  if (isSlotId(view)) return <RegionSlot slot={view} />
  if (recordId) {
    return <BoundView kind={view as PanelKind} recordId={recordId} axis={axis} tab={tab} mode={mode} pinSurface={pinSurface} />
  }
  return <SingletonView kind={view as PanelKind} mode={mode} tab={tab} />
}

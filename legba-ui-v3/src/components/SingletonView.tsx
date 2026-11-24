/**
 * SingletonView — one registry kind rendered against a synthetic
 * registration, with no binding (U-2 shell).
 *
 * The Dockview tile renders singletons through this, and so do the two pieces
 * of fixed frame that are not tiles: the Consult column and the Record sheet.
 * One code path, so a kind mounted in the frame is indistinguishable from the
 * same kind in a region (error boundary, tier, Suspense, the retired-id alias
 * and its tab).
 */
import { Suspense } from 'react'
import type { PanelKind, PanelRegistration } from '@/types'
import type { currentMode } from '@/auth/jwt'
import { PANEL_REGISTRY } from '@/panel-registry/registry'
import { resolveKind } from '@/panel-registry/aliases'
import { UnboundPanelPlaceholder } from '@/components/PanelChrome'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelErrorBoundary } from '@/components/PanelErrorBoundary'
import { PanelLoading } from '@/components/PanelLoading'
import { DockviewPanelApiProvider, type TilePanelApi } from '@/components/DockviewPanelApiContext'

type Mode = ReturnType<typeof currentMode>

/** The registration a singleton renders against — its id is the store key. */
export function syntheticRegistration(kind: PanelKind, mode: Mode): PanelRegistration | null {
  const entry = PANEL_REGISTRY[kind]
  if (!entry) return null
  return {
    id: `singleton:${kind}`,
    panel_id: entry.definition.panelId,
    descriptor_id: '(singleton)',
    descriptor_version: '00000000',
    descriptor_family: 'target',
    analyst_id: null,
    title: entry.definition.defaultTitle,
    mode,
    layout_slot: kind,
    data_query: {},
    binding: {},
    retired: false,
    created_at: new Date(0).toISOString(),
    retired_at: null,
  }
}

// M-2 — its own module (the phone and the Watch views load it without this file).
export { PanelLoading }

export function SingletonView({
  kind: requested,
  mode,
  tab,
  api = null,
}: {
  kind: PanelKind
  mode: Mode
  tab?: string
  /** The Dockview tile api when rendered in a region; null in the frame. */
  api?: TilePanelApi | null
}) {
  // A saved layout or deep link may name a RETIRED kind: it resolves onto its
  // survivor and the tab that IS the retired surface (panel-registry/aliases).
  const alias = resolveKind(requested)
  const kind = alias?.kind ?? requested
  const entry = PANEL_REGISTRY[kind]
  const registration = syntheticRegistration(kind, mode)
  if (!entry || !registration) {
    return <UnboundPanelPlaceholder panelId={requested} descriptorId="(no descriptor — singleton)" />
  }
  const Component = entry.Component
  return (
    <DockviewPanelApiProvider value={api}>
      <PanelErrorBoundary label={registration.id}>
        <PanelTierProvider tier={entry.definition.tier ?? 'live'}>
          <Suspense fallback={<PanelLoading />}>
            <Component registration={registration} scope={{}} mode={mode} initialTab={tab ?? alias?.tab} />
          </Suspense>
        </PanelTierProvider>
      </PanelErrorBoundary>
    </DockviewPanelApiProvider>
  )
}

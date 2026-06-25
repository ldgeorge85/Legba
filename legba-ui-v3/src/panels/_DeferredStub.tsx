/**
 * Generic deferred-panel placeholder.
 *
 * For panels declared in `legba_ui_panels_v2.md` but deferred past the L-204
 * 8-panel-minimum cut. Each one ships a real chrome + a description card
 * pointing to the spec section, so the registry → loader → render path is
 * end-to-end exercised even when the body isn't filled in.
 *
 * When a deferred panel gets a real implementation, the corresponding
 * `panels/<dir>/<Name>.tsx` file replaces this default-export and the
 * `panel-registry/registry.ts` import re-points automatically.
 */

import { PanelChrome } from '@/components/PanelChrome'
import type { PanelProps } from '@/types'

export interface StubSpec {
  title: string
  spec: string
  description: string
}

export function makeStubPanel(spec: StubSpec) {
  return function DeferredPanel({ registration, scope, mode }: PanelProps) {
    return (
      <PanelChrome registration={registration} title={registration.title || spec.title}>
        <div className="text-xs space-y-2">
          <div className="bg-accent-warning/10 border border-accent-warning/40 rounded p-2 text-accent-warning">
            <strong>Deferred panel.</strong> Specified in <code>{spec.spec}</code> — body
            implementation lands post-L-204 first wave.
          </div>
          <p className="text-slate-300">{spec.description}</p>
          <dl className="grid grid-cols-2 gap-y-1 mt-3 text-[11px] text-slate-400">
            <dt>panel_id</dt>
            <dd className="font-mono">{registration.panel_id}</dd>
            <dt>descriptor</dt>
            <dd className="font-mono">{registration.descriptor_id}</dd>
            <dt>mode</dt>
            <dd>{mode}</dd>
            <dt>scope</dt>
            <dd className="font-mono">{JSON.stringify(scope)}</dd>
            <dt>data_query</dt>
            <dd className="font-mono break-all">{JSON.stringify(registration.data_query)}</dd>
          </dl>
        </div>
      </PanelChrome>
    )
  }
}

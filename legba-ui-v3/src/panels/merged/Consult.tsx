/**
 * Consult (`system.consult`) — U-3 merge set 4: Deep Consult becomes a depth
 * toggle on Consult.
 *
 * Chat Consult (`system.consult`) answers synchronously in the envelope, no
 * durable row. Deep Consult (`system.deep_consult`) is architecturally
 * different — it submits a DETACHED Dapr Workflow (`POST /api/v1/
 * deep_consult`) and polls a task id — so this wrapper doesn't try to unify
 * the two calls; it just puts a Chat/Deep switch above them and mounts
 * whichever original, UNMODIFIED panel component the operator picked, inside
 * `PanelEmbedProvider` so its own (otherwise-standalone) `PanelChrome`
 * header/border stays suppressed — this wrapper's header is the ONLY chrome
 * that renders (the "double chrome" fix).
 *
 * `system.deep_consult` stays registered (hidden from the sidebar) pointing
 * at the SAME untouched component — see panel-registry/registry.ts
 * HIDDEN_KINDS — so a saved layout referencing the old id keeps resolving
 * exactly as before.
 */
import { useState } from 'react'
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import ConsultPanel from '@/panels/system/Consult'
import DeepConsultPanel from '@/panels/system/DeepConsult'

type Depth = 'chat' | 'deep'

const TABS: readonly PanelTabDef[] = [
  { id: 'chat', label: 'Chat' },
  { id: 'deep', label: 'Deep' },
]

export default function ConsultMerged(props: PanelProps) {
  const [depth, setDepth] = useState<Depth>('chat')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Consult</span>
        <PanelTabStrip
          tabs={TABS}
          active={depth}
          onChange={(id) => setDepth(id as Depth)}
          ariaLabel="Consult depth"
          testIdPrefix="consult-depth"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {depth === 'chat' ? <ConsultPanel {...props} /> : <DeepConsultPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}

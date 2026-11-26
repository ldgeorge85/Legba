/**
 * Consult (`system.consult`) — the one Consult panel.
 *
 * Deep Consult is a mode switch inside the Consult composer (see
 * `panels/system/Consult.tsx`), not a second tab or form. This wrapper is kept
 * so the registered kind and its retired address (`system.deep_consult`, which
 * resolves here with the initial tab "deep") keep landing on the same panel;
 * inside the frame the dock's own header names Consult, so there is no strip.
 */
import type { PanelProps } from '@/types'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import ConsultPanel from '@/panels/system/Consult'
import { useConsultFrame } from '@/components/ConsultFrameContext'

export default function ConsultMerged(props: PanelProps) {
  const frame = useConsultFrame()
  return (
    <div className={frame ? 'flex h-full w-full flex-col' : 'flex h-full w-full flex-col bg-surf-2'}>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          <ConsultPanel {...props} />
        </PanelEmbedProvider>
      </div>
    </div>
  )
}

/**
 * Register (`system.register`) — UI-B1/merges, UI-D1 Table A "Frames:
 * Situations · Narratives".
 *
 * The FRAME register: what the platform is tracking as a standing frame, and
 * how each one moved.
 *   - Situations → the situation register's frames and each frame's
 *                  append-only trajectory ledger (`system.situations`)
 *   - Narratives → reified contested-claim families and the source-echo graph
 *                  (`system.narratives`)
 *
 * Both used to be Provenance's Trajectory and Narratives tabs (GLASS-2): they
 * folded there for the old sidebar budget, not because they answer
 * Provenance's question. Table A gives them their own kind; the two retired
 * ids are RE-POINTED here in `panel-registry/aliases.ts` (Table B: "re-pointed,
 * not new"). Absence contract: intensity is read on its stated scale and a
 * pre-rebase value is not compared — the Situations child already prints the
 * scale stamp beside every intensity. Both children render UNMODIFIED.
 */
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import SituationTrajectoryPanel from '@/panels/system/SituationTrajectory'
import NarrativesPanel from '@/panels/system/Narratives'
import { useMergedTab } from './useMergedTab'
import { registerPinFor, usePinScope } from '@/lib/consultContext'
import { useSelection } from '@/state/selection'

type Tab = 'situations' | 'narratives'

export const REGISTER_TABS: readonly PanelTabDef[] = [
  { id: 'situations', label: 'Situations' },
  { id: 'narratives', label: 'Narratives' },
]

export default function RegisterMerged(props: PanelProps) {
  const [tab, setTab] = useMergedTab<Tab>('system.register', props.initialTab, REGISTER_TABS, 'situations')
  // THE PIN HOOK (UI-B6): on the Situations tab the frame the reader picked
  // is what they are on; the Narratives tab selects no object and pins
  // nothing — switching to it releases the Register's claim.
  const selection = useSelection((s) => s.selection)
  usePinScope(registerPinFor(tab, selection), 'register')
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Register</span>
        <PanelTabStrip
          tabs={REGISTER_TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Frame register"
          testIdPrefix="register-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'situations' && <SituationTrajectoryPanel {...props} />}
          {tab === 'narratives' && <NarrativesPanel {...props} />}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}

/**
 * Optimizer (`system.optimizer`) — UI-B1/merges, UI-D1 Table A "absorbs
 * `system.optimizer.diff` (drill tab)".
 *
 *   - Candidates → the GEPA candidate queue (the original `system.optimizer`)
 *   - Diff       → the candidate-vs-parent prompt-module diff
 *                  (`system.optimizer.diff`, formerly a singleton drill)
 *
 * The diff was a DRILL opened from the queue it sits beside, so it is the
 * queue's second tab now. The queue's "view prompt-module diff" action still
 * drives `open_panel(system.optimizer.diff, {candidate_id})`; the executor
 * resolves the retired id onto this kind and writes the candidate to
 * `state/panelParams` under THIS kind, so the wrapper relays it to the diff's
 * own key (the child reads `system.optimizer.diff`, unmodified) and switches
 * to the Diff tab — on first open and while mounted alike.
 */
import type { PanelProps } from '@/types'
import { PanelTabStrip, type PanelTabDef } from '@/components/PanelTabs'
import { PanelTierProvider } from '@/components/PanelTierContext'
import { PanelEmbedProvider } from '@/components/PanelEmbedContext'
import OptimizerPanel from '@/panels/system/Optimizer'
import OptimizerDiffPanel from '@/panels/system/OptimizerDiff'
import { setPanelParams, usePanelOpenParams } from '@/state/panelParams'
import { useMergedTab } from './useMergedTab'

type Tab = 'candidates' | 'diff'

export const OPTIMIZER_TABS: readonly PanelTabDef[] = [
  { id: 'candidates', label: 'Candidates' },
  // The diff route is the operator review aid over the GEPA loop — the
  // guarded-preview tier the retired kind carried, now on its tab.
  { id: 'diff', label: 'Diff', badge: 'preview' },
]

/** The key the (unmodified) diff child reads its candidate from. */
const DIFF_PARAMS_KEY = 'system.optimizer.diff'

export default function OptimizerMerged(props: PanelProps) {
  const [tab, setTab] = useMergedTab<Tab>('system.optimizer', props.initialTab, OPTIMIZER_TABS, 'candidates')
  usePanelOpenParams('system.optimizer', (params) => {
    if (typeof params.candidate_id !== 'string' || !params.candidate_id) return
    setPanelParams(DIFF_PARAMS_KEY, { ...params })
    setTab('diff')
  })
  return (
    <div className="flex h-full w-full flex-col bg-surf-2">
      <div className="flex items-center gap-2 border-b border-line bg-surf-3 px-density py-1.5">
        <span className="text-label uppercase tracking-wider text-ink-3">Optimizer</span>
        <PanelTabStrip
          tabs={OPTIMIZER_TABS}
          active={tab}
          onChange={(id) => setTab(id as Tab)}
          ariaLabel="Optimizer surface"
          testIdPrefix="optimizer-tab"
        />
      </div>
      <div className="min-h-0 flex-1">
        <PanelEmbedProvider>
          {tab === 'candidates' && <OptimizerPanel {...props} />}
          {tab === 'diff' && (
            <PanelTierProvider tier="preview">
              <OptimizerDiffPanel {...props} />
            </PanelTierProvider>
          )}
        </PanelEmbedProvider>
      </div>
    </div>
  )
}
